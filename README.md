# AnkiForge

**Convert medical physics quiz questions into Anki flashcard decks — entirely in your browser.**

[**Try it live →**](https://xzhangfox.github.io/anki-converter/)

---

## What it does

Medical physics board exams (ABR, CAMPEP) require memorizing hundreds of facts across radiation physics, dosimetry, imaging, and treatment planning. [OncologyMedicalPhysics.com](https://oncologymedicalphysics.com) offers practice quizzes covering exactly this material — but reading a quiz is not the same as learning it.

AnkiForge bridges the gap: paste the HTML from any WatuPro quiz page, and AnkiForge produces a ready-to-import `.apkg` file with one flashcard per question. The front of each card shows the question and all answer choices; the back reveals the correct answer(s) in bold, plus the full explanation.

All processing happens locally in your browser. No data is sent to any server.

---

## How to use it

### 1. Get the quiz HTML

1. Open a quiz page on [OncologyMedicalPhysics.com](https://oncologymedicalphysics.com/abr-part-1-general-full-exam/) in your browser
2. **Answer all questions** so the page shows the results with correct answers and explanations
3. Press `Cmd+A` → `Cmd+C` to select and copy the entire page source  
   *(Or: right-click → View Page Source → Select All → Copy)*

### 2. Convert

1. Open [AnkiForge](https://xzhangfox.github.io/anki-converter/)
2. Paste the HTML into the input box — the app validates it instantly
3. Optionally edit the deck name
4. Click **Convert & Preview** — flip through the cards to review them

### 3. Download & Import

1. Click **Download .apkg**
2. Open Anki → **File → Import** → select the downloaded file

Your deck appears immediately, ready for review.

---

## Features

- **Zero setup** — runs entirely in the browser via WebAssembly (sql.js) and JSZip; no Python, no Node, no install
- **Live validation** — detects whether the pasted HTML contains WatuPro quiz content before you convert
- **Card preview** — flip-card UI lets you review every card before downloading
- **MathJax support** — LaTeX math expressions (`\(...\)` and `\[...\]`) render natively in Anki
- **Correct answers highlighted** — back of each card bolds the correct choice(s) and shows the explanation
- **Auto deck name** — inferred from the page `<title>` tag

---

## Tech stack

| Layer | Library |
|---|---|
| SQLite in browser | [sql.js](https://github.com/sql-js/sql.js) (SQLite → WebAssembly) |
| ZIP packaging | [JSZip](https://stuk.github.io/jszip/) |
| SHA-1 checksum | Web Crypto API (`crypto.subtle`) |
| HTML parsing | Browser-native `DOMParser` |
| Deployment | GitHub Pages (static, free) |

The `.apkg` format is a ZIP archive containing a SQLite database (`collection.anki2`) with notes and cards, plus a `media` manifest. AnkiForge builds this entirely in-memory and offers it as a download Blob.

---

## Python scripts (advanced)

For power users who want to work with existing `.apkg` files locally:

### `apkg_editor.py`
Read and edit any `.apkg` file, including modern Anki 2.1.55+ packages (zstd-compressed SQLite + protobuf media manifest).

```python
from apkg_editor import ApkgEditor

with ApkgEditor("MyDeck.apkg") as apkg:
    print(apkg.deck_names())
    for note in apkg.notes():
        print(note["flds"])
    apkg.save("MyDeck_edited.apkg")
```

### `html_to_apkg.py`
Command-line converter supporting three modes:

```bash
# Convert a WatuPro quiz HTML file to .apkg
python html_to_apkg.py watupro abr-part-1.html output.apkg

# Extract notes from an existing .apkg to HTML for editing
python html_to_apkg.py extract input.apkg output.html

# Convert edited HTML back to .apkg
python html_to_apkg.py convert edited.html output.apkg
```

Requirements: `pip install zstandard`

---

## Limitations

- Only works with **WatuPro quiz plugin** pages (the format used by OncologyMedicalPhysics.com)
- Images in cards require an internet connection to display in Anki (cross-origin restrictions prevent embedding them at conversion time)
- Tested against ABR Part 1 and Radiation Physics question sets

---

## License

MIT
