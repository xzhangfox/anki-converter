#!/usr/bin/env python3
"""
html_to_apkg.py — Bidirectional converter: Anki APKG ↔ HTML
             and  WatuPro quiz HTML → Anki APKG

Modes
-----
  extract  APKG → HTML        self-contained HTML; images embedded as base64,
                               MathJax rendered via CDN (\\( \\) and \\[ \\] syntax)
  convert  HTML → APKG        parse <div class="anki-card"> blocks, include
                               images, preserve MathJax, output .apkg
  watupro  WatuPro HTML → APKG  parse WatuPro quiz plugin pages
                               (e.g. oncologymedicalphysics.com); downloads
                               images automatically

HTML card format (convert mode, both directions)
-------------------------------------------------
  <div class="anki-card">
    <div class="front">...question HTML, \\(MathJax\\), <img src="...">...</div>
    <div class="back">...answer HTML...</div>
  </div>

Requirements
------------
  pip install zstandard   (only needed when reading modern anki21b source decks)

Usage
-----
  python3 html_to_apkg.py extract "Radiation Physics.apkg" radiation.html
  python3 html_to_apkg.py convert  radiation.html  "Radiation Physics New.apkg"
  python3 html_to_apkg.py watupro  abr-part-1.html "ABR Part 1.apkg" --deck "ABR Part 1"
"""

import base64
import hashlib
import html.parser
import json
import mimetypes
import os
import random
import re
import shutil
import urllib.request
import sqlite3
import string
import struct
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# zstandard (optional – required only for modern anki21b source decks)
# ---------------------------------------------------------------------------
try:
    import zstandard as zstd
    _HAVE_ZSTD = True
except ImportError:
    _HAVE_ZSTD = False


# ---------------------------------------------------------------------------
# Card CSS embedded in the new APKG template
# ---------------------------------------------------------------------------
_CARD_CSS = """\
.card {
    font-family: Arial, sans-serif;
    font-size: 18px;
    line-height: 1.65;
    text-align: left;
    color: #1a1a1a;
    background-color: #ffffff;
    padding: 20px 28px;
    max-width: 820px;
    margin: 0 auto;
}
ul, ol { margin: 6px 0; padding-left: 26px; }
li     { margin: 3px 0; }
table  { border-collapse: collapse; margin: 10px 0; }
td, th { border: 1px solid #c8c8c8; padding: 5px 11px; }
th     { background: #f4f4f4; font-weight: 600; }
img    { max-width: 100%; height: auto; border-radius: 4px; }
hr#answer { border: none; border-top: 1px solid #d0d0d0; margin: 18px 0; }
sup, sub { font-size: 0.78em; }
b, strong { color: #111; }
"""

# ---------------------------------------------------------------------------
# Self-contained HTML template (extract mode)
# ---------------------------------------------------------------------------
_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<script>
MathJax = {{
  tex: {{
    inlineMath: [['\\\\(', '\\\\)']],
    displayMath: [['\\\\[', '\\\\]']],
    processEscapes: true,
    tags: 'ams'
  }},
  options: {{ skipHtmlTags: ['script','noscript','style','textarea','pre'] }}
}};
</script>
<script async src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-chtml.js"></script>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
    font-family: Arial, sans-serif;
    font-size: 16px;
    background: #f7f7f7;
    color: #1a1a1a;
    padding: 30px 16px;
}}
h1 {{ font-size: 1.5em; margin-bottom: 24px; color: #333; text-align: center; }}
.anki-card {{
    background: #fff;
    border: 1px solid #ddd;
    border-radius: 8px;
    margin-bottom: 20px;
    overflow: hidden;
    box-shadow: 0 1px 4px rgba(0,0,0,.07);
}}
.anki-card-header {{
    background: #f0f0f0;
    font-size: 0.78em;
    color: #888;
    padding: 4px 14px;
    border-bottom: 1px solid #e0e0e0;
}}
.front, .back {{
    padding: 16px 20px;
    line-height: 1.65;
}}
.front {{ border-bottom: 1px solid #eee; }}
.front::before {{
    content: "QUESTION";
    display: block;
    font-size: 0.68em;
    font-weight: 700;
    letter-spacing: .08em;
    color: #bbb;
    margin-bottom: 8px;
}}
.back::before {{
    content: "ANSWER";
    display: block;
    font-size: 0.68em;
    font-weight: 700;
    letter-spacing: .08em;
    color: #bbb;
    margin-bottom: 8px;
}}
ul, ol  {{ margin: 6px 0; padding-left: 26px; }}
li      {{ margin: 3px 0; }}
table   {{ border-collapse: collapse; margin: 8px 0; max-width: 100%; }}
td, th  {{ border: 1px solid #ccc; padding: 5px 10px; }}
th      {{ background: #f4f4f4; font-weight: 600; }}
img     {{ max-width: 100%; height: auto; border-radius: 3px; margin: 4px 0; }}
sup, sub {{ font-size: 0.78em; }}
</style>
</head>
<body>
<h1>{title}</h1>
{cards_html}
</body>
</html>
"""

_CARD_HTML_SNIPPET = """\
<div class="anki-card" id="card-{num}">
  <div class="anki-card-header">#{num}</div>
  <div class="front">{front}</div>
  <div class="back">{back}</div>
</div>
"""


# ===========================================================================
# PART 1 — APKG reader helpers (shared with apkg_editor.py logic)
# ===========================================================================

def _pb_varint(data: bytes, pos: int) -> Tuple[int, int]:
    result = shift = 0
    while True:
        b = data[pos]; pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return result, pos


def _parse_media_protobuf(data: bytes) -> Dict[str, str]:
    """zstd-decoded protobuf MediaEntries → {index_str: filename}."""
    media_map: Dict[str, str] = {}
    pos = 0; idx = 0
    while pos < len(data):
        tag, pos = _pb_varint(data, pos)
        fn, wt = tag >> 3, tag & 7
        if wt == 2:
            ln, pos = _pb_varint(data, pos)
            payload = data[pos:pos + ln]; pos += ln
            if fn == 1:
                sp = 0; fname = None
                while sp < len(payload):
                    st, sp = _pb_varint(payload, sp)
                    sf, sw = st >> 3, st & 7
                    if sw == 2:
                        sl, sp = _pb_varint(payload, sp)
                        sd = payload[sp:sp + sl]; sp += sl
                        if sf == 1:
                            fname = sd.decode("utf-8")
                    elif sw == 0:
                        _, sp = _pb_varint(payload, sp)
                    else:
                        break
                if fname:
                    media_map[str(idx)] = fname
                idx += 1
        elif wt == 0:
            _, pos = _pb_varint(data, pos)
        elif wt == 5:
            pos += 4
        elif wt == 1:
            pos += 8
        else:
            break
    return media_map


def _load_apkg(apkg_path: str) -> Tuple[sqlite3.Connection, Dict[str, bytes], str]:
    """
    Extract and open an APKG, returning
      (sqlite_connection, {filename: bytes} media_dict, tmpdir_path).
    Caller must clean up tmpdir_path.
    """
    tmp = tempfile.mkdtemp()
    with zipfile.ZipFile(apkg_path, "r") as zf:
        zf.extractall(tmp)

    # Detect DB format
    anki21b = os.path.join(tmp, "collection.anki21b")
    anki21  = os.path.join(tmp, "collection.anki21")
    anki2   = os.path.join(tmp, "collection.anki2")

    if os.path.exists(anki21b):
        if not _HAVE_ZSTD:
            raise ImportError("pip install zstandard  (needed for modern anki21b format)")
        dec = os.path.join(tmp, "_dec.db")
        dctx = zstd.ZstdDecompressor()
        with open(anki21b, "rb") as fin, open(dec, "wb") as fout:
            dctx.copy_stream(fin, fout)
        db_path = dec
    elif os.path.exists(anki21):
        db_path = anki21
    else:
        db_path = anki2

    conn = sqlite3.connect(db_path)
    conn.create_collation(
        "unicase",
        lambda a, b: (a.casefold() > b.casefold()) - (a.casefold() < b.casefold()),
    )
    conn.row_factory = sqlite3.Row

    # Parse media manifest
    media_raw = open(os.path.join(tmp, "media"), "rb").read()
    if media_raw[:4] == b"\x28\xb5\x2f\xfd":   # zstd magic
        dec_media = zstd.ZstdDecompressor().decompress(
            media_raw, max_output_size=50 * 1024 * 1024
        )
        media_index = _parse_media_protobuf(dec_media)  # {idx: filename}
    else:
        media_index = json.loads(media_raw.decode("utf-8"))

    # Build filename → bytes map
    media_files: Dict[str, bytes] = {}
    for idx, fname in media_index.items():
        fpath = os.path.join(tmp, idx)
        if os.path.exists(fpath):
            media_files[fname] = open(fpath, "rb").read()

    return conn, media_files, tmp


def _field_names(conn: sqlite3.Connection, model_id: int) -> List[str]:
    """Return field names for a model, works for both legacy and modern formats."""
    # Modern format: fields table
    rows = conn.execute(
        "SELECT name FROM fields WHERE ntid=? ORDER BY ord", (model_id,)
    ).fetchall()
    if rows:
        return [r["name"] for r in rows]
    # Legacy format: JSON in col.models
    col = conn.execute("SELECT models FROM col").fetchone()
    if col and col["models"]:
        models = json.loads(col["models"])
        m = models.get(str(model_id))
        if m:
            return [f["name"] for f in m["flds"]]
    return ["Front", "Back"]


# ===========================================================================
# PART 2 — HTML parser: extract card divs → [{front, back}]
# ===========================================================================

class _CardHTMLParser(html.parser.HTMLParser):
    """
    Parses HTML containing <div class="anki-card"> blocks and extracts
    the innerHTML of <div class="front"> and <div class="back"> children.

    Handles arbitrarily nested HTML inside front/back.
    MathJax notation (\\( \\) and \\[ \\]) is preserved verbatim.
    """

    VOID = frozenset(
        "area base br col embed hr img input link meta param source track wbr".split()
    )

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.cards: List[Dict[str, str]] = []
        # State machine: idle → in_card → in_front/in_back → in_card → idle
        self._state = "idle"        # idle | in_card | in_front | in_back
        self._card_div_depth = 0   # tracks outer card div nesting
        self._inner_div_depth = 0  # tracks divs inside front/back content
        self._content = ""
        self._cur_front = ""
        self._cur_back  = ""

    def _rebuild_open(self, tag: str, attrs: list) -> str:
        parts = [f"<{tag}"]
        for name, val in attrs:
            if val is None:
                parts.append(f" {name}")
            else:
                parts.append(f' {name}="{val}"')
        return "".join(parts) + ">"

    def _has_class(self, attrs: list, cls: str) -> bool:
        d = dict(attrs)
        return cls in d.get("class", "").split()

    def handle_starttag(self, tag, attrs):
        state = self._state

        if state == "idle":
            if tag == "div" and self._has_class(attrs, "anki-card"):
                self._state = "in_card"
                self._card_div_depth = 1
                self._cur_front = ""
                self._cur_back  = ""

        elif state == "in_card":
            if tag == "div":
                self._card_div_depth += 1
                if self._has_class(attrs, "front"):
                    self._state = "in_front"
                    self._inner_div_depth = 0
                    self._content = ""
                    return
                if self._has_class(attrs, "back"):
                    self._state = "in_back"
                    self._inner_div_depth = 0
                    self._content = ""
                    return

        elif state in ("in_front", "in_back"):
            if tag == "div":
                self._inner_div_depth += 1
            # All tags inside front/back are added verbatim to content
            self._content += self._rebuild_open(tag, attrs)

    def handle_endtag(self, tag):
        state = self._state

        if state == "in_card":
            if tag == "div":
                self._card_div_depth -= 1
                if self._card_div_depth == 0:
                    self.cards.append({
                        "front": self._cur_front.strip(),
                        "back":  self._cur_back.strip(),
                    })
                    self._state = "idle"

        elif state in ("in_front", "in_back"):
            if tag == "div":
                if self._inner_div_depth == 0:
                    # This </div> closes the front/back div itself
                    if state == "in_front":
                        self._cur_front = self._content
                    else:
                        self._cur_back = self._content
                    self._state = "in_card"
                    # The front/back div was counted when it opened — balance it
                    self._card_div_depth -= 1
                    return
                self._inner_div_depth -= 1
            if tag not in self.VOID:
                self._content += f"</{tag}>"

    def handle_data(self, data):
        if self._state in ("in_front", "in_back"):
            self._content += data

    def handle_entityref(self, name):
        if self._state in ("in_front", "in_back"):
            self._content += f"&{name};"

    def handle_charref(self, name):
        if self._state in ("in_front", "in_back"):
            self._content += f"&#{name};"


def parse_cards_from_html(html_text: str) -> List[Dict[str, str]]:
    """
    Parse an HTML string containing <div class="anki-card"> blocks.
    Returns list of {front, back} dicts (inner HTML).
    """
    parser = _CardHTMLParser()
    parser.feed(html_text)
    return parser.cards


# ===========================================================================
# PART 3 — Image helpers
# ===========================================================================

def _mime_for_filename(name: str) -> str:
    mt, _ = mimetypes.guess_type(name)
    return mt or "application/octet-stream"


def _ext_for_mime(mime: str) -> str:
    return {
        "image/jpeg": ".jpg",
        "image/png":  ".png",
        "image/gif":  ".gif",
        "image/webp": ".webp",
        "image/svg+xml": ".svg",
    }.get(mime, ".bin")


def _img_to_data_uri(filename: str, data: bytes) -> str:
    mime = _mime_for_filename(filename)
    b64  = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _embed_images_in_html(html_text: str, media_files: Dict[str, bytes]) -> str:
    """
    Replace <img src="filename"> with:
      <img src="data:mime;base64,..." data-filename="original_name.ext">

    The data-filename attribute lets html_to_apkg recover the original
    filename on the return trip, instead of generating a hash-based name.
    """
    def replacer(m):
        full_tag = m.group(0)
        pre  = m.group(1)   # everything before the src value
        src  = m.group(2)
        post = m.group(3)   # closing quote + rest of tag up to >

        if src.startswith("data:"):
            return full_tag   # already embedded

        fname = os.path.basename(src)
        if fname not in media_files:
            return full_tag   # media file not found, leave unchanged

        data_uri = _img_to_data_uri(fname, media_files[fname])
        # Inject data-filename alongside the base64 src so the filename
        # survives the round-trip.  Avoid double-injection if already present.
        if 'data-filename=' not in full_tag:
            return f'{pre}{data_uri}" data-filename="{fname}{post}'
        return f'{pre}{data_uri}{post}'

    # Match both single- and double-quoted src attributes (capture full tag)
    pattern = r'(<img\b[^>]*?\bsrc=")([^"]+)("(?:[^>]*)?>|")'
    result = re.sub(pattern, replacer, html_text, flags=re.IGNORECASE | re.DOTALL)
    return result


def _extract_images_from_html(
    html_text: str,
    media_dir: Optional[str] = None,
) -> Tuple[str, Dict[str, bytes]]:
    """
    Scan HTML for <img> tags and collect their image data.

    Returns:
      (modified_html, {filename: bytes})
      - data: URIs are decoded and replaced with a plain filename
      - plain filenames pointing to media_dir are read
      - unresolvable srcs are left unchanged
    """
    collected: Dict[str, bytes] = {}
    counter = [0]

    def replacer(m):
        full_tag = m.group(0)
        pre  = m.group(1)
        src  = m.group(2)
        post = m.group(3)

        if src.startswith("data:"):
            # Prefer the original filename stored in data-filename attribute
            fn_match = re.search(r'data-filename="([^"]+)"', full_tag)
            orig_name = fn_match.group(1) if fn_match else None

            data_match = re.match(r"data:([^;]+);base64,(.+)", src, re.DOTALL)
            if data_match:
                mime = data_match.group(1)
                raw  = base64.b64decode(data_match.group(2))
                if orig_name:
                    fname = orig_name
                else:
                    ext   = _ext_for_mime(mime)
                    fname = hashlib.sha1(raw).hexdigest()[:16] + ext
                collected[fname] = raw
                # Restore the tag with just the plain filename src
                clean_tag = re.sub(r'\s*data-filename="[^"]+"', '', full_tag)
                clean_tag = re.sub(r'(<img\b[^>]*?\bsrc=")[^"]+(")', f'\\g<1>{fname}\\2', clean_tag, flags=re.IGNORECASE)
                return clean_tag

        else:
            fname = os.path.basename(src)
            if media_dir:
                candidate = os.path.join(media_dir, fname)
                if os.path.exists(candidate):
                    collected[fname] = open(candidate, "rb").read()
                    return full_tag
            if os.path.exists(fname):
                collected[fname] = open(fname, "rb").read()

        return full_tag  # leave as-is if nothing resolved

    pattern = r'(<img\b[^>]*?\bsrc=")([^"]+)("(?:[^>]*)?>|")'
    result  = re.sub(pattern, replacer, html_text, flags=re.IGNORECASE | re.DOTALL)
    return result, collected


# ===========================================================================
# PART 4 — APKG builder (legacy anki2 format — always importable by Anki)
# ===========================================================================

_CREATE_COL = """
CREATE TABLE col (
    id      integer primary key,
    crt     integer not null,
    mod     integer not null,
    scm     integer not null,
    ver     integer not null,
    dty     integer not null,
    usn     integer not null,
    ls      integer not null,
    conf    text not null,
    models  text not null,
    decks   text not null,
    dconf   text not null,
    tags    text not null
);"""

_CREATE_NOTES = """
CREATE TABLE notes (
    id    integer primary key,
    guid  text not null,
    mid   integer not null,
    mod   integer not null,
    usn   integer not null,
    tags  text not null,
    flds  text not null,
    sfld  integer not null,
    csum  integer not null,
    flags integer not null,
    data  text not null
);"""

_CREATE_CARDS = """
CREATE TABLE cards (
    id      integer primary key,
    nid     integer not null,
    did     integer not null,
    ord     integer not null,
    mod     integer not null,
    usn     integer not null,
    type    integer not null,
    queue   integer not null,
    due     integer not null,
    ivl     integer not null,
    factor  integer not null,
    reps    integer not null,
    lapses  integer not null,
    left    integer not null,
    odue    integer not null,
    odid    integer not null,
    flags   integer not null,
    data    text not null
);"""

_CREATE_REVLOG = """
CREATE TABLE revlog (
    id      integer primary key,
    cid     integer not null,
    usn     integer not null,
    ease    integer not null,
    ivl     integer not null,
    lastIvl integer not null,
    factor  integer not null,
    time    integer not null,
    type    integer not null
);"""

_CREATE_GRAVES = """
CREATE TABLE graves (
    usn  integer not null,
    oid  integer not null,
    type integer not null
);"""

_CREATE_INDEXES = """
CREATE INDEX ix_notes_usn  on notes (usn);
CREATE INDEX ix_cards_usn  on cards (usn);
CREATE INDEX ix_revlog_usn on revlog (usn);
CREATE INDEX ix_cards_nid  on cards (nid);
CREATE INDEX ix_cards_sched on cards (did, queue, due);
CREATE INDEX ix_revlog_cid on revlog (cid);
CREATE INDEX ix_notes_csum on notes (csum);
"""


def _csum(sfld: str) -> int:
    """Anki's checksum: first 8 hex chars of SHA-1 of the sort field, as int."""
    return int(hashlib.sha1(sfld.encode("utf-8")).hexdigest()[:8], 16)


def _guid() -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=10))


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


def _build_col_json(model_id: int, deck_id: int, deck_name: str) -> Tuple[str, str, str, str]:
    """Return (conf, models, decks, dconf) JSON strings for the col row."""
    now = int(time.time())

    conf = {
        "nextPos": 1,
        "estTimes": True,
        "activeDecks": [deck_id],
        "sortType": "noteFld",
        "timeLim": 0,
        "sortBackwards": False,
        "addToCur": True,
        "curDeck": deck_id,
        "newBury": True,
        "newSpread": 0,
        "dueCounts": True,
        "curModel": str(model_id),
        "collapseTime": 1200,
    }

    model = {
        "id": model_id,
        "name": "Basic",
        "type": 0,
        "mod": now,
        "usn": -1,
        "sortf": 0,
        "did": deck_id,
        "tmpls": [
            {
                "name": "Card 1",
                "ord": 0,
                "qfmt": "{{Front}}",
                "afmt": "{{FrontSide}}\n\n<hr id=answer>\n\n{{Back}}",
                "bqfmt": "",
                "bafmt": "",
                "did": None,
                "bfont": "",
                "bsize": 0,
            }
        ],
        "flds": [
            {
                "name": "Front", "ord": 0, "sticky": False, "rtl": False,
                "font": "Arial", "size": 20, "media": [],
            },
            {
                "name": "Back", "ord": 1, "sticky": False, "rtl": False,
                "font": "Arial", "size": 20, "media": [],
            },
        ],
        "css": _CARD_CSS,
        "latexPre": (
            "\\documentclass[12pt]{article}\n"
            "\\special{papersize=3in,5in}\n"
            "\\usepackage[utf8]{inputenc}\n"
            "\\usepackage{amssymb,amsmath}\n"
            "\\pagestyle{empty}\n"
            "\\setlength{\\parindent}{0in}\n"
            "\\begin{document}\n"
        ),
        "latexPost": "\\end{document}",
        "latexsvg": False,
        "req": [[0, "any", [0]]],
    }

    default_deck = {
        "id": 1,
        "mod": 0,
        "name": "Default",
        "usn": 0,
        "lrnToday": [0, 0],
        "revToday": [0, 0],
        "newToday": [0, 0],
        "timeToday": [0, 0],
        "collapsed": True,
        "browserCollapsed": True,
        "desc": "",
        "dyn": 0,
        "conf": 1,
        "extendNew": 0,
        "extendRev": 50,
    }

    target_deck = {
        "id": deck_id,
        "mod": now,
        "name": deck_name,
        "usn": -1,
        "lrnToday": [0, 0],
        "revToday": [0, 0],
        "newToday": [0, 0],
        "timeToday": [0, 0],
        "collapsed": False,
        "browserCollapsed": False,
        "desc": "",
        "dyn": 0,
        "conf": 1,
        "extendNew": 0,
        "extendRev": 50,
    }

    dconf = {
        "1": {
            "id": 1,
            "mod": 0,
            "name": "Default",
            "usn": 0,
            "maxTaken": 60,
            "autoplay": True,
            "timer": 0,
            "replayq": True,
            "new": {
                "bury": False,
                "delays": [1, 10],
                "initialFactor": 2500,
                "ints": [1, 4, 7],
                "order": 1,
                "perDay": 20,
                "separate": True,
            },
            "lapse": {
                "delays": [10],
                "leechAction": 0,
                "leechFails": 8,
                "minInt": 1,
                "mult": 0,
            },
            "rev": {
                "bury": False,
                "ease4": 1.3,
                "fuzz": 0.05,
                "ivlFct": 1,
                "maxIvl": 36500,
                "minSpace": 1,
                "perDay": 100,
            },
            "dyn": False,
        }
    }

    return (
        json.dumps(conf),
        json.dumps({str(model_id): model}),
        json.dumps({"1": default_deck, str(deck_id): target_deck}),
        json.dumps(dconf),
    )


def build_apkg(
    cards: List[Dict[str, str]],
    media_files: Dict[str, bytes],
    deck_name: str,
    output_path: str,
):
    """
    Build a legacy anki2 APKG from card dicts and media.

    cards       : list of {front: str, back: str}  (inner HTML)
    media_files : {filename: bytes}
    deck_name   : name of the deck in Anki
    output_path : destination .apkg file
    """
    now_ms  = int(time.time() * 1000)
    model_id = now_ms
    deck_id  = now_ms + 1
    now      = int(time.time())

    conf_j, models_j, decks_j, dconf_j = _build_col_json(model_id, deck_id, deck_name)

    tmp = tempfile.mkdtemp()
    try:
        db_path = os.path.join(tmp, "collection.anki2")
        conn = sqlite3.connect(db_path)
        c = conn.cursor()

        for stmt in [_CREATE_COL, _CREATE_NOTES, _CREATE_CARDS,
                     _CREATE_REVLOG, _CREATE_GRAVES]:
            c.execute(stmt)
        for stmt in _CREATE_INDEXES.strip().split("\n"):
            if stmt.strip():
                c.execute(stmt.strip())

        c.execute(
            "INSERT INTO col VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (1, now, now, now, 11, 0, 0, 0, conf_j, models_j, decks_j, dconf_j, "{}"),
        )

        for i, card in enumerate(cards):
            note_id = now_ms + i * 2
            card_id = now_ms + i * 2 + 1

            front = card.get("front", "")
            back  = card.get("back", "")
            flds  = f"{front}\x1f{back}"
            sfld  = _strip_html(front)[:255]
            cs    = _csum(sfld)

            c.execute(
                "INSERT INTO notes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (note_id, _guid(), model_id, now, -1, " ", flds, sfld, cs, 0, ""),
            )
            c.execute(
                "INSERT INTO cards VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (card_id, note_id, deck_id, 0, now, -1, 0, 0, i + 1, 0, 0, 0, 0, 0, 0, 0, 0, ""),
            )

        conn.commit()
        conn.close()

        # Media: write files numbered 0, 1, 2, ... + JSON index
        media_index: Dict[str, str] = {}
        for idx, (fname, data) in enumerate(media_files.items()):
            fpath = os.path.join(tmp, str(idx))
            with open(fpath, "wb") as f:
                f.write(data)
            media_index[str(idx)] = fname

        with open(os.path.join(tmp, "media"), "w", encoding="utf-8") as f:
            json.dump(media_index, f)

        # Pack into ZIP
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for item in os.listdir(tmp):
                zf.write(os.path.join(tmp, item), item)

    finally:
        shutil.rmtree(tmp)


# ===========================================================================
# PART 5 — Public API
# ===========================================================================

def extract_apkg_to_html(apkg_path: str, output_path: str):
    """
    Extract all notes from an APKG into a self-contained HTML file.

    Images are embedded as base64 data URIs.
    MathJax (\\( \\) and \\[ \\] notation) is rendered via CDN.
    The output can be opened in any browser without an internet connection
    except for the MathJax CDN (or serve it offline with a local MathJax copy).
    """
    conn, media_files, tmp = _load_apkg(apkg_path)
    try:
        notes = conn.execute(
            "SELECT id, mid, flds FROM notes ORDER BY id"
        ).fetchall()

        cards_html_parts = []
        for i, note in enumerate(notes, start=1):
            fields = note["flds"].split("\x1f")
            front  = fields[0] if len(fields) > 0 else ""
            back   = fields[1] if len(fields) > 1 else ""

            # Embed images
            front = _embed_images_in_html(front, media_files)
            back  = _embed_images_in_html(back,  media_files)

            cards_html_parts.append(
                _CARD_HTML_SNIPPET.format(num=i, front=front, back=back)
            )

        title = Path(apkg_path).stem
        full_html = _HTML_TEMPLATE.format(
            title=title,
            cards_html="\n".join(cards_html_parts),
        )

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(full_html)

        print(f"Extracted {len(notes)} notes → {output_path}")

    finally:
        conn.close()
        shutil.rmtree(tmp)


def html_to_apkg(
    html_path: str,
    output_path: str,
    deck_name: Optional[str] = None,
    media_dir: Optional[str] = None,
):
    """
    Convert an HTML file containing <div class="anki-card"> blocks into an APKG.

    html_path   : path to the HTML file
    output_path : path for the generated .apkg
    deck_name   : Anki deck name (defaults to HTML <title> or filename stem)
    media_dir   : directory to search for locally-referenced image files

    MathJax notation (\\( \\) and \\[ \\]) is preserved verbatim — Anki
    renders it natively.  Images may be base64 data URIs or local filenames.
    """
    with open(html_path, "r", encoding="utf-8") as f:
        html_text = f.read()

    # Determine deck name from title tag if not provided
    if not deck_name:
        m = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.IGNORECASE | re.DOTALL)
        deck_name = m.group(1).strip() if m else Path(html_path).stem

    # Parse card divs
    cards = parse_cards_from_html(html_text)
    if not cards:
        print("Warning: no <div class=\"anki-card\"> blocks found in the HTML.")
        return

    # Collect images from all front/back content
    all_media: Dict[str, bytes] = {}
    processed_cards = []
    for card in cards:
        front, front_media = _extract_images_from_html(card["front"], media_dir)
        back,  back_media  = _extract_images_from_html(card["back"],  media_dir)
        all_media.update(front_media)
        all_media.update(back_media)
        processed_cards.append({"front": front, "back": back})

    build_apkg(processed_cards, all_media, deck_name, output_path)
    print(
        f"Converted {len(processed_cards)} cards, "
        f"{len(all_media)} media files → {output_path}"
    )


# ===========================================================================
# PART 6 — CLI
# ===========================================================================

def _usage():
    print(__doc__)


# ===========================================================================
# PART 7 — WatuPro quiz HTML parser
# ===========================================================================

def _watupro_blocks(html_text: str) -> List[str]:
    """
    Extract each WatuPro question block as a raw HTML substring.
    Uses bracket-counting to handle arbitrarily nested divs inside each block.
    """
    opener = '<div '
    marker = 'class="watupro-choices-columns show-question'
    blocks: List[str] = []
    search_from = 0

    while True:
        # Find next opening <div with the watupro class
        start = html_text.find(marker, search_from)
        if start == -1:
            break
        # Walk backwards to the '<div ' that precedes the class attr
        div_start = html_text.rfind('<div ', 0, start)
        if div_start == -1:
            search_from = start + 1
            continue

        # Count div nesting to find matching </div>
        depth = 0
        i = div_start
        end = -1
        while i < len(html_text):
            if html_text[i:i+4] == '<div':
                depth += 1
                i += 4
            elif html_text[i:i+6] == '</div>':
                depth -= 1
                if depth == 0:
                    end = i + 6
                    break
                i += 6
            else:
                i += 1

        if end == -1:
            search_from = start + 1
            continue

        blocks.append(html_text[div_start:end])
        search_from = end

    return blocks


def _inner_html(block: str, open_tag_pattern: str) -> str:
    """
    Extract innerHTML of the first element matching open_tag_pattern
    (a regex that matches the opening tag).  Handles nested divs.
    """
    m = re.search(open_tag_pattern, block, re.IGNORECASE | re.DOTALL)
    if not m:
        return ""
    # Determine which tag we opened
    tag_match = re.match(r'<(\w+)', m.group(0))
    tag = tag_match.group(1).lower() if tag_match else 'div'

    pos = m.end()
    depth = 1
    content_start = pos

    while pos < len(block) and depth > 0:
        next_open  = block.find(f'<{tag}',  pos)
        next_close = block.find(f'</{tag}>', pos)

        if next_close == -1:
            break

        if next_open != -1 and next_open < next_close:
            depth += 1
            pos = next_open + len(tag) + 1
        else:
            depth -= 1
            if depth == 0:
                return block[content_start:next_close]
            pos = next_close + len(tag) + 3  # len('</x>')

    return ""


def _download_image(url: str, cache_dir: str) -> Optional[Tuple[str, bytes]]:
    """
    Download an image from a URL, using a local cache.
    Returns (filename, bytes) or None on failure.
    """
    fname = os.path.basename(url.split("?")[0]) or hashlib.md5(url.encode()).hexdigest() + ".bin"
    cache_path = os.path.join(cache_dir, fname)

    if os.path.exists(cache_path):
        return fname, open(cache_path, "rb").read()

    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; AnkiConverter/1.0)"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
        with open(cache_path, "wb") as f:
            f.write(data)
        print(f"  Downloaded: {fname}")
        return fname, data
    except Exception as e:
        print(f"  Warning: could not download {url!r}: {e}")
        return None


def _replace_urls_with_filenames(
    html_frag: str, url_to_fname: Dict[str, str]
) -> str:
    """Replace all external image URLs in an HTML fragment with local filenames."""
    def sub(m):
        url = m.group(2)
        fname = url_to_fname.get(url)
        return f'{m.group(1)}{fname}{m.group(3)}' if fname else m.group(0)

    result = re.sub(r'(<img\b[^>]*?\bsrc=")([^"]+)(")', sub, html_frag, flags=re.IGNORECASE)
    return re.sub(r"(<img\b[^>]*?\bsrc=')([^']+)(')", sub, result, flags=re.IGNORECASE)


def _clean_html(fragment: str) -> str:
    """Remove trailing &nbsp; paragraphs and collapse whitespace."""
    fragment = re.sub(r'(\s*<p>\s*(&nbsp;|\s)*</p>\s*)+$', '', fragment.strip())
    return fragment.strip()


def parse_watupro_html(
    html_path: str,
    image_cache_dir: Optional[str] = None,
) -> Tuple[List[Dict[str, str]], Dict[str, bytes]]:
    """
    Parse a WatuPro quiz HTML page into Anki cards.

    Returns:
      (cards, media_files)
      cards       : list of {front, back}
      media_files : {filename: bytes}  for all downloaded images
    """
    with open(html_path, "r", encoding="utf-8", errors="replace") as f:
        html_text = f.read()

    if image_cache_dir is None:
        image_cache_dir = os.path.join(
            os.path.dirname(os.path.abspath(html_path)), "_img_cache"
        )
    os.makedirs(image_cache_dir, exist_ok=True)

    # --- collect all external image URLs from question blocks ---
    blocks = _watupro_blocks(html_text)
    print(f"Found {len(blocks)} questions.")

    all_urls: List[str] = []
    for b in blocks:
        all_urls.extend(re.findall(r'src="(https?://[^"]+)"', b, re.IGNORECASE))
    all_urls = list(dict.fromkeys(all_urls))  # unique, order-preserved

    # Download images
    url_to_fname: Dict[str, str] = {}
    media_files:  Dict[str, bytes] = {}
    if all_urls:
        print(f"Downloading {len(all_urls)} image(s)...")
    for url in all_urls:
        result = _download_image(url, image_cache_dir)
        if result:
            fname, data = result
            url_to_fname[url] = fname
            media_files[fname] = data

    # --- parse each block into Front / Back ---
    cards: List[Dict[str, str]] = []
    for block in blocks:
        # ── Question stem ────────────────────────────────────────────────
        qcontent = _inner_html(block, r'<div[^>]+class="show-question-content"[^>]*>')
        # Strip the leading question-number span
        qcontent = re.sub(
            r'^<span[^>]+class="watupro_num"[^>]*>\d+\.\s*</span>\s*',
            '', qcontent.strip(), flags=re.IGNORECASE
        )
        qcontent = _replace_urls_with_filenames(qcontent.strip(), url_to_fname)

        # ── Choices ──────────────────────────────────────────────────────
        choice_items = re.findall(
            r'<li class="answer([^"]*)">(.*?)</li>', block, re.DOTALL | re.IGNORECASE
        )
        correct_texts: List[str] = []
        all_choices_html = ""
        for classes, content in choice_items:
            is_correct = "correct-answer" in classes
            # Extract visible text from <span class="answer">
            span_m = re.search(r'<span class="answer">(.*?)</span>', content, re.DOTALL | re.IGNORECASE)
            choice_html = span_m.group(1) if span_m else content
            choice_html = _replace_urls_with_filenames(choice_html, url_to_fname)
            all_choices_html += f"<li>{choice_html}</li>\n"
            if is_correct:
                correct_texts.append(choice_html)

        # ── Explanation / Solution ───────────────────────────────────────
        feedback_inner = _inner_html(block, r'<div[^>]+class="watupro-main-feedback"[^>]*>')
        # Remove the bold "Solution" heading
        feedback_inner = re.sub(r'\s*<strong>Solution</strong>\s*', '', feedback_inner, flags=re.IGNORECASE)
        feedback_inner = _replace_urls_with_filenames(feedback_inner, url_to_fname)
        feedback_inner = _clean_html(feedback_inner)

        # ── Build Front ──────────────────────────────────────────────────
        front = qcontent
        if all_choices_html:
            front += f"\n<ul>\n{all_choices_html}</ul>"

        # ── Build Back ───────────────────────────────────────────────────
        if correct_texts:
            if len(correct_texts) == 1:
                back = f"<b>{correct_texts[0]}</b>"
            else:
                items = "".join(f"<li><b>{t}</b></li>\n" for t in correct_texts)
                back = f"<ul>\n{items}</ul>"
        else:
            back = ""

        if feedback_inner:
            back += ("\n<hr>\n" if back else "") + feedback_inner

        cards.append({"front": front.strip(), "back": back.strip()})

    return cards, media_files


def watupro_to_apkg(
    html_path: str,
    output_path: str,
    deck_name: Optional[str] = None,
    image_cache_dir: Optional[str] = None,
):
    """
    Convert a WatuPro quiz HTML page into an Anki APKG.

    html_path       : path to the saved webpage HTML
    output_path     : destination .apkg file
    deck_name       : Anki deck name (defaults to page <title>)
    image_cache_dir : directory for downloaded image cache
    """
    with open(html_path, "r", encoding="utf-8", errors="replace") as f:
        html_text = f.read()

    if not deck_name:
        m = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.IGNORECASE | re.DOTALL)
        if m:
            # Strip site name suffix (e.g. "ABR Part 1: General – Test Your Way | OncologyMedicalPhysics.com")
            deck_name = m.group(1).split("|")[0].strip()
        else:
            deck_name = Path(html_path).stem

    cards, media_files = parse_watupro_html(html_path, image_cache_dir)

    if not cards:
        print("No questions found. Make sure the HTML contains WatuPro quiz markup.")
        return

    build_apkg(cards, media_files, deck_name, output_path)
    print(
        f"Created '{deck_name}': {len(cards)} cards, "
        f"{len(media_files)} media files → {output_path}"
    )


def main():
    import sys
    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help"):
        _usage()
        return

    mode = args[0].lower()

    if mode == "extract":
        if len(args) < 2:
            print("Usage: python3 html_to_apkg.py extract <deck.apkg> [output.html]")
            sys.exit(1)
        apkg_in  = args[1]
        html_out = args[2] if len(args) > 2 else Path(apkg_in).with_suffix(".html")
        extract_apkg_to_html(apkg_in, str(html_out))

    elif mode == "convert":
        if len(args) < 2:
            print("Usage: python3 html_to_apkg.py convert <input.html> [output.apkg] [--deck \"Name\"] [--media dir]")
            sys.exit(1)
        html_in   = args[1]
        apkg_out  = None
        deck_name = None
        media_dir = None
        i = 2
        while i < len(args):
            if args[i] == "--deck" and i + 1 < len(args):
                deck_name = args[i + 1]; i += 2
            elif args[i] == "--media" and i + 1 < len(args):
                media_dir = args[i + 1]; i += 2
            elif apkg_out is None and not args[i].startswith("--"):
                apkg_out = args[i]; i += 1
            else:
                i += 1
        if apkg_out is None:
            apkg_out = str(Path(html_in).with_suffix(".apkg"))
        html_to_apkg(html_in, apkg_out, deck_name=deck_name, media_dir=media_dir)

    elif mode == "watupro":
        if len(args) < 2:
            print("Usage: python3 html_to_apkg.py watupro <input.html> [output.apkg] [--deck \"Name\"] [--cache dir]")
            sys.exit(1)
        html_in        = args[1]
        apkg_out       = None
        deck_name      = None
        img_cache_dir  = None
        i = 2
        while i < len(args):
            if args[i] == "--deck" and i + 1 < len(args):
                deck_name = args[i + 1]; i += 2
            elif args[i] == "--cache" and i + 1 < len(args):
                img_cache_dir = args[i + 1]; i += 2
            elif apkg_out is None and not args[i].startswith("--"):
                apkg_out = args[i]; i += 1
            else:
                i += 1
        if apkg_out is None:
            apkg_out = str(Path(html_in).with_suffix(".apkg"))
        watupro_to_apkg(html_in, apkg_out, deck_name=deck_name, image_cache_dir=img_cache_dir)

    else:
        print(f"Unknown mode: {mode!r}  (use 'extract', 'convert', or 'watupro')")
        _usage()
        sys.exit(1)


if __name__ == "__main__":
    main()
