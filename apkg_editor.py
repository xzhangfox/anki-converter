"""
apkg_editor.py — Read and edit Anki .apkg files.

Supports both Anki legacy format (collection.anki2 / .anki21) and the
modern backend format (collection.anki21b, introduced in Anki ≥ 2.1.55).

An .apkg file is a ZIP archive containing:
  Legacy  : collection.anki2   — plain SQLite
  Modern  : collection.anki21b — zstd-compressed SQLite
  media   : Legacy → plain JSON {"0": "filename", ...}
            Modern → zstd-compressed protobuf (MediaEntries)
  0, 1, … : media files (numeric zip entries)

Requirements
------------
  pip install zstandard   # only needed for modern .apkg files

Usage examples
--------------
# Inspect
with ApkgEditor("deck.apkg") as apkg:
    print(apkg.deck_names())
    for note in apkg.notes():
        fnames = apkg.field_names(note["mid"])
        for name, val in zip(fnames, note["fields"]):
            print(f"  [{name}] {val[:80]}")

# Edit a note field and save
with ApkgEditor("deck.apkg") as apkg:
    notes = apkg.notes()
    notes[0]["fields"][1] = "Updated back text"
    apkg.update_note(notes[0])
    apkg.save("deck_edited.apkg")
"""

import json
import os
import re
import shutil
import sqlite3
import struct
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# zstandard — optional import (only required for modern anki21b format)
# ---------------------------------------------------------------------------
try:
    import zstandard as zstd
    _HAVE_ZSTD = True
except ImportError:
    _HAVE_ZSTD = False


# ---------------------------------------------------------------------------
# Minimal protobuf helpers (no external library needed)
# ---------------------------------------------------------------------------

def _pb_read_varint(data: bytes, pos: int):
    """Read a protobuf varint from data starting at pos. Returns (value, new_pos)."""
    result = 0
    shift = 0
    while True:
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return result, pos


def _pb_write_varint(value: int) -> bytes:
    """Encode an integer as a protobuf varint."""
    out = []
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _pb_write_field_len(field_num: int, data: bytes) -> bytes:
    """Encode a length-delimited field (wire type 2)."""
    tag = _pb_write_varint((field_num << 3) | 2)
    return tag + _pb_write_varint(len(data)) + data


def _pb_write_field_varint(field_num: int, value: int) -> bytes:
    """Encode a varint field (wire type 0)."""
    tag = _pb_write_varint((field_num << 3) | 0)
    return tag + _pb_write_varint(value)


def _parse_media_protobuf(data: bytes) -> Dict[str, str]:
    """
    Parse the zstd-decompressed media protobuf into {index_str: filename}.

    The format is:
        message MediaEntries { repeated MediaEntry entries = 1; }
        message MediaEntry   { string name = 1; uint32 size = 2; bytes sha1 = 3; }

    Entries are ordered — the i-th entry maps to zip file named str(i).
    """
    media_map: Dict[str, str] = {}
    pos = 0
    entry_index = 0

    while pos < len(data):
        tag, pos = _pb_read_varint(data, pos)
        field_num = tag >> 3
        wire_type = tag & 0x07

        if wire_type == 2:  # length-delimited
            length, pos = _pb_read_varint(data, pos)
            payload = data[pos:pos + length]
            pos += length

            if field_num == 1:  # MediaEntry sub-message
                # Parse sub-message to extract name (field 1)
                filename = None
                sub_pos = 0
                while sub_pos < len(payload):
                    sub_tag, sub_pos = _pb_read_varint(payload, sub_pos)
                    sub_field = sub_tag >> 3
                    sub_wire = sub_tag & 0x07
                    if sub_wire == 2:
                        sub_len, sub_pos = _pb_read_varint(payload, sub_pos)
                        sub_data = payload[sub_pos:sub_pos + sub_len]
                        sub_pos += sub_len
                        if sub_field == 1:  # name
                            filename = sub_data.decode("utf-8")
                    elif sub_wire == 0:
                        _, sub_pos = _pb_read_varint(payload, sub_pos)
                    else:
                        break  # unexpected wire type, stop parsing sub-message

                if filename:
                    media_map[str(entry_index)] = filename
                entry_index += 1
        elif wire_type == 0:
            _, pos = _pb_read_varint(data, pos)
        elif wire_type == 5:
            pos += 4
        elif wire_type == 1:
            pos += 8
        else:
            break  # unknown wire type

    return media_map


def _build_media_protobuf(media_map: Dict[str, str], work_dir: Path) -> bytes:
    """
    Re-encode the media map as a protobuf MediaEntries message.
    Entries are written in numeric index order.
    """
    entries_bytes = b""
    for idx in sorted(media_map, key=lambda k: int(k) if k.isdigit() else 0):
        filename = media_map[idx]
        media_file = work_dir / idx

        # Build MediaEntry sub-message
        entry = _pb_write_field_len(1, filename.encode("utf-8"))
        if media_file.exists():
            size = media_file.stat().st_size
            entry += _pb_write_field_varint(2, size)
            # sha1 field (field 3) — compute if possible
            try:
                import hashlib
                sha1 = hashlib.sha1(media_file.read_bytes()).digest()
                entry += _pb_write_field_len(3, sha1)
            except Exception:
                pass

        entries_bytes += _pb_write_field_len(1, entry)

    return entries_bytes


# ---------------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------------

def _split_fields(field_str: str) -> List[str]:
    """Anki stores note fields as a \\x1f-separated string."""
    return field_str.split("\x1f")


def _join_fields(fields: List[str]) -> str:
    return "\x1f".join(fields)


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class ApkgEditor:
    """
    Context-manager wrapper around an Anki .apkg file.

    Supports both legacy (anki2/anki21) and modern (anki21b) formats.

    with ApkgEditor("my_deck.apkg") as apkg:
        notes = apkg.notes()
        notes[0]["fields"][0] = "Updated"
        apkg.update_note(notes[0])
        apkg.save("my_deck_edited.apkg")
    """

    def __init__(self, apkg_path: str):
        self.apkg_path = Path(apkg_path)
        self._tmpdir: Optional[tempfile.TemporaryDirectory] = None
        self._work_dir: Optional[Path] = None
        self._db_path: Optional[Path] = None
        self._db_format: str = "legacy"   # "legacy" or "modern"
        self._conn: Optional[sqlite3.Connection] = None
        self.media_map: Dict[str, str] = {}   # index_str -> original filename

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "ApkgEditor":
        self._tmpdir = tempfile.TemporaryDirectory()
        self._work_dir = Path(self._tmpdir.name)
        self._extract()
        self._open_db()
        return self

    def __exit__(self, *_):
        self._close_db()
        if self._tmpdir:
            self._tmpdir.cleanup()

    # ------------------------------------------------------------------
    # Internal: extract / detect format / open DB
    # ------------------------------------------------------------------

    def _extract(self):
        with zipfile.ZipFile(self.apkg_path, "r") as zf:
            zf.extractall(self._work_dir)

        # Detect DB file and format
        anki21b = self._work_dir / "collection.anki21b"
        anki21  = self._work_dir / "collection.anki21"
        anki2   = self._work_dir / "collection.anki2"

        if anki21b.exists():
            if not _HAVE_ZSTD:
                raise ImportError(
                    "This .apkg uses the modern Anki format (anki21b) which requires "
                    "zstandard. Install it with: pip install zstandard"
                )
            # Decompress zstd-compressed SQLite to a plain file
            dec_path = self._work_dir / "_collection_dec.db"
            dctx = zstd.ZstdDecompressor()
            with open(anki21b, "rb") as fin, open(dec_path, "wb") as fout:
                dctx.copy_stream(fin, fout)
            self._db_path = dec_path
            self._db_format = "modern"
        elif anki21.exists():
            self._db_path = anki21
            self._db_format = "legacy"
        elif anki2.exists():
            self._db_path = anki2
            self._db_format = "legacy"
        else:
            raise FileNotFoundError("No Anki collection database found in .apkg")

        # Parse media manifest
        media_file = self._work_dir / "media"
        if media_file.exists():
            raw = media_file.read_bytes()
            if raw[:4] == b"\x28\xb5\x2f\xfd":  # zstd magic
                if not _HAVE_ZSTD:
                    raise ImportError("Media file requires zstandard: pip install zstandard")
                dctx = zstd.ZstdDecompressor()
                decompressed = dctx.decompress(raw, max_output_size=50 * 1024 * 1024)
                self.media_map = _parse_media_protobuf(decompressed)
            else:
                try:
                    self.media_map = json.loads(raw.decode("utf-8"))
                except Exception:
                    self.media_map = {}

    def _open_db(self):
        self._conn = sqlite3.connect(self._db_path)
        # The modern Anki DB uses a custom 'unicase' collation — register a
        # case-insensitive equivalent so SQLite doesn't refuse to open queries.
        self._conn.create_collation(
            "unicase",
            lambda a, b: (a.casefold() > b.casefold()) - (a.casefold() < b.casefold()),
        )
        self._conn.row_factory = sqlite3.Row

    def _close_db(self):
        if self._conn:
            self._conn.commit()
            self._conn.close()
            self._conn = None

    def _require_open(self):
        if self._conn is None:
            raise RuntimeError("ApkgEditor is not open — use it as a context manager.")

    # ------------------------------------------------------------------
    # Read: decks
    # ------------------------------------------------------------------

    def deck_names(self) -> List[str]:
        """Return the names of all decks."""
        self._require_open()
        if self._db_format == "modern":
            rows = self._conn.execute("SELECT name FROM decks").fetchall()
            return [r["name"] for r in rows]
        else:
            row = self._conn.execute("SELECT decks FROM col").fetchone()
            if not row or not row["decks"]:
                return []
            return [d["name"] for d in json.loads(row["decks"]).values()]

    def decks_raw(self) -> List[Dict]:
        """Return a list of deck dicts (id, name, and raw data)."""
        self._require_open()
        if self._db_format == "modern":
            rows = self._conn.execute("SELECT id, name, mtime_secs, usn FROM decks").fetchall()
            return [dict(r) for r in rows]
        else:
            row = self._conn.execute("SELECT decks FROM col").fetchone()
            if not row or not row["decks"]:
                return []
            return list(json.loads(row["decks"]).values())

    # ------------------------------------------------------------------
    # Read: note types / models
    # ------------------------------------------------------------------

    def models(self) -> Dict[str, Any]:
        """
        Return note types keyed by model id string.
        Each entry has at least: id, name, flds (list of field dicts with 'name').
        """
        self._require_open()
        if self._db_format == "modern":
            result = {}
            for nt in self._conn.execute("SELECT id, name FROM notetypes").fetchall():
                mid = str(nt["id"])
                fields = self._conn.execute(
                    "SELECT ord, name FROM fields WHERE ntid=? ORDER BY ord", (nt["id"],)
                ).fetchall()
                result[mid] = {
                    "id":   nt["id"],
                    "name": nt["name"],
                    "flds": [{"ord": f["ord"], "name": f["name"]} for f in fields],
                }
            return result
        else:
            row = self._conn.execute("SELECT models FROM col").fetchone()
            if not row or not row["models"]:
                return {}
            return json.loads(row["models"])

    def model_names(self) -> List[str]:
        self._require_open()
        if self._db_format == "modern":
            rows = self._conn.execute("SELECT name FROM notetypes").fetchall()
            return [r["name"] for r in rows]
        else:
            return [m["name"] for m in self.models().values()]

    def field_names(self, model_id) -> List[str]:
        """Return field names (in order) for a given model/notetype id."""
        models = self.models()
        model = models.get(str(model_id))
        if model is None:
            raise KeyError(f"Model {model_id!r} not found")
        return [f["name"] for f in model["flds"]]

    # ------------------------------------------------------------------
    # Read: notes
    # ------------------------------------------------------------------

    def notes(self) -> List[Dict]:
        """
        Return all notes as a list of dicts:
            id, guid, mid (model id), tags (list), fields (list of str)
        """
        self._require_open()
        rows = self._conn.execute(
            "SELECT id, guid, mid, tags, flds FROM notes"
        ).fetchall()
        return [
            {
                "id":     row["id"],
                "guid":   row["guid"],
                "mid":    row["mid"],
                "tags":   row["tags"].strip().split() if row["tags"].strip() else [],
                "fields": _split_fields(row["flds"]),
            }
            for row in rows
        ]

    def note_by_id(self, note_id: int) -> Optional[Dict]:
        """Fetch a single note by its id."""
        self._require_open()
        row = self._conn.execute(
            "SELECT id, guid, mid, tags, flds FROM notes WHERE id=?", (note_id,)
        ).fetchone()
        if row is None:
            return None
        return {
            "id":     row["id"],
            "guid":   row["guid"],
            "mid":    row["mid"],
            "tags":   row["tags"].strip().split() if row["tags"].strip() else [],
            "fields": _split_fields(row["flds"]),
        }

    # ------------------------------------------------------------------
    # Read: cards
    # ------------------------------------------------------------------

    def cards(self) -> List[Dict]:
        """Return all cards with scheduling info."""
        self._require_open()
        rows = self._conn.execute(
            "SELECT id, nid, did, ord, due, type, queue, ivl, factor FROM cards"
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Write: update a note
    # ------------------------------------------------------------------

    def update_note(self, note: Dict):
        """
        Persist changes to a note back to the database.
        Pass the dict from notes() / note_by_id() with modified 'fields' or 'tags'.
        """
        self._require_open()
        flds  = _join_fields(note["fields"])
        tags  = (" " + " ".join(note["tags"]) + " ") if note["tags"] else " "
        sfld  = _strip_html(note["fields"][0]) if note["fields"] else ""
        mod   = int(time.time())
        self._conn.execute(
            "UPDATE notes SET flds=?, tags=?, sfld=?, mod=? WHERE id=?",
            (flds, tags, sfld, mod, note["id"]),
        )

    def update_all_notes(self, notes: List[Dict]):
        """Convenience: update multiple notes."""
        for note in notes:
            self.update_note(note)

    # ------------------------------------------------------------------
    # Write: add a note
    # ------------------------------------------------------------------

    def add_note(
        self,
        model_id: int,
        deck_id: int,
        fields: List[str],
        tags: Optional[List[str]] = None,
    ) -> int:
        """
        Insert a new note (and one card per template) into the collection.
        Returns the new note id.
        """
        self._require_open()
        import random, string

        now_ms = int(time.time() * 1000)
        note_id = now_ms
        guid = "".join(random.choices(string.ascii_letters + string.digits, k=10))
        flds = _join_fields(fields)
        tag_str = (" " + " ".join(tags) + " ") if tags else " "
        sfld = _strip_html(fields[0]) if fields else ""
        mod = int(time.time())

        self._conn.execute(
            "INSERT INTO notes (id, guid, mid, mod, usn, tags, flds, sfld, csum, flags, data) "
            "VALUES (?, ?, ?, ?, -1, ?, ?, ?, 0, 0, '')",
            (note_id, guid, model_id, mod, tag_str, flds, sfld),
        )

        # Determine how many templates this model has
        if self._db_format == "modern":
            tmpl_rows = self._conn.execute(
                "SELECT ord FROM templates WHERE ntid=? ORDER BY ord", (model_id,)
            ).fetchall()
            num_templates = len(tmpl_rows) if tmpl_rows else 1
        else:
            models = self.models()
            model = models.get(str(model_id), {})
            num_templates = len(model.get("tmpls", [{}]))

        for ord_ in range(num_templates):
            card_id = note_id + ord_
            self._conn.execute(
                "INSERT INTO cards "
                "(id, nid, did, ord, mod, usn, type, queue, due, ivl, factor, reps, lapses, left, odue, odid, flags, data) "
                "VALUES (?, ?, ?, ?, ?, -1, 0, 0, ?, 0, 0, 0, 0, 0, 0, 0, 0, '')",
                (card_id, note_id, deck_id, ord_, mod, note_id),
            )

        return note_id

    # ------------------------------------------------------------------
    # Write: delete a note
    # ------------------------------------------------------------------

    def delete_note(self, note_id: int):
        """Delete a note and all its associated cards."""
        self._require_open()
        self._conn.execute("DELETE FROM cards WHERE nid=?", (note_id,))
        self._conn.execute("DELETE FROM notes WHERE id=?", (note_id,))

    # ------------------------------------------------------------------
    # Media helpers
    # ------------------------------------------------------------------

    def list_media(self) -> Dict[str, str]:
        """Return {index: original_filename} for all media files."""
        return dict(self.media_map)

    def add_media(self, src_path: str) -> str:
        """
        Copy a file into the package as a new media entry.
        Returns the original filename (use it in field HTML, e.g. <img src="...">).
        """
        src = Path(src_path)
        if not src.exists():
            raise FileNotFoundError(src_path)
        used = {int(k) for k in self.media_map if k.isdigit()}
        new_index = str(max(used, default=-1) + 1)
        shutil.copy2(src, self._work_dir / new_index)
        self.media_map[new_index] = src.name
        return src.name

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def save(self, output_path: Optional[str] = None):
        """
        Write the (possibly modified) collection back to a .apkg file.
        output_path defaults to overwriting the original file.
        """
        self._require_open()
        self._conn.commit()

        out = Path(output_path) if output_path else self.apkg_path

        # Rebuild the media manifest
        if self._db_format == "modern":
            pb_bytes = _build_media_protobuf(self.media_map, self._work_dir)
            cctx = zstd.ZstdCompressor()
            self._work_dir.joinpath("media").write_bytes(cctx.compress(pb_bytes))
        else:
            self._work_dir.joinpath("media").write_text(
                json.dumps(self.media_map), encoding="utf-8"
            )

        # For the modern format, re-compress the SQLite DB back to zstd
        if self._db_format == "modern":
            dec_path = self._work_dir / "_collection_dec.db"
            anki21b_path = self._work_dir / "collection.anki21b"
            cctx = zstd.ZstdCompressor()
            with open(dec_path, "rb") as fin, open(anki21b_path, "wb") as fout:
                cctx.copy_stream(fin, fout)

        # Repack into zip — exclude the decompressed helper file
        skip = {"_collection_dec.db"}
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            for item in self._work_dir.iterdir():
                if item.name not in skip:
                    zf.write(item, item.name)

        print(f"Saved → {out}")


# ---------------------------------------------------------------------------
# CLI demo / inspection
# ---------------------------------------------------------------------------

def _demo(apkg_path: str):
    print(f"Opening: {apkg_path}\n")
    with ApkgEditor(apkg_path) as apkg:
        print(f"Format  : {apkg._db_format}")

        print("\n=== Decks ===")
        for name in apkg.deck_names():
            print(f"  {name}")

        print("\n=== Note types ===")
        for name in apkg.model_names():
            print(f"  {name}")

        notes = apkg.notes()
        print(f"\n=== Notes ({len(notes)} total) — first 5 ===")
        for note in notes[:5]:
            try:
                fnames = apkg.field_names(note["mid"])
            except KeyError:
                fnames = [f"Field {i}" for i in range(len(note["fields"]))]
            print(f"  id={note['id']}  tags={note['tags']}")
            for fname, fval in zip(fnames, note["fields"]):
                preview = _strip_html(fval)[:100].replace("\n", " ")
                print(f"    [{fname}] {preview}")

        print(f"\n=== Cards ({len(apkg.cards())} total) ===")

        media = apkg.list_media()
        print(f"\n=== Media ({len(media)} files) — first 5 ===")
        for idx, fname in list(media.items())[:5]:
            print(f"  {idx} → {fname}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python3 apkg_editor.py <deck.apkg>")
        sys.exit(1)
    _demo(sys.argv[1])
