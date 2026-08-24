# BOLD-Local-DB

Desktop tool to build and browse a local, **offline** SQLite database from
[BOLD Systems](https://www.boldsystems.org/) (Barcode of Life Data System) TSV exports.

## What it does

Two programs, used in order:

1. **BOLD DB Creator** (`dev/bold_db_creator.py`) — filters the raw BOLD
   TSV/`tar.gz` export, loads it into SQLite, indexes the selected fields and
   builds full-text search.

   > **Note:** only records with a DNA sequence (`nuc` field) are kept —
   > records without a sequence are discarded during Step 1 (TSV filtering)
   > and never make it into the SQLite database.
2. **Web viewer** (`dev/frontend/server.py`) — local Flask web app to search,
   filter and export records (CSV / FASTA) from the database. Works fully
   offline once the database exists.

### Screenshots

| Full-text search | Advanced field search | Batch search |
|---|---|---|
| ![Full-text search](dev/screenshots/full_text_search.png) | ![Advanced field search](dev/screenshots/advanced_field_search.png) | ![Batch search](dev/screenshots/batch_search.png) |

## Requirements

- Windows
- Python 3 (dependencies auto-install on first run: PySide6, Flask, etc.)

## Quick start

1. Run `1_Create_DB.lnk` (or `dev/bold_db_creator.bat`) to build the database
   from a BOLD export.
2. Run `2_Open_web_viewer_BOLD_DB.lnk` (or `dev/launch_bold_db.bat`) to open
   the web viewer at `http://127.0.0.1:5001`.

If you move this folder to another drive or computer, run
`repair_shortcuts.bat` once — it regenerates the two shortcuts to point at the
new location.

## Project structure

```
BOLD_DB/
├── 1_Create_DB.lnk
├── 2_Open_web_viewer_BOLD_DB.lnk
├── repair_shortcuts.bat
├── dev/
│   ├── bold_db_creator.py     # Steps 1-4: TSV filtering, SQLite load, indexing, FTS
│   ├── fields_config.json     # Fields kept from the BOLD TSV + which are indexed
│   ├── frontend/
│   │   ├── server.py          # Local web server (search / filter / export)
│   │   ├── static/
│   │   └── templates/
│   └── screenshots/
├── data/                      # raw / processed / exports (not versioned)
└── manual/                    # user guide (ES/EN)
```

The generated database and raw/processed data are **not** part of this
repository (see `.gitignore`).

## Documentation

- [Guía de uso (ES)](manual/guia_de_uso.html)
- [User guide (EN)](manual/user_guide.html)
