# BOLD-Local-DB

**Version: 1.0.0**

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

- Windows, macOS or Linux
- Python 3 (dependencies auto-install on first run: PySide6, Flask, etc.)
- A [BOLD Systems](https://bench.boldsystems.org/) account, to download data packages
- ~80 GB of free disk space, for the downloaded package and the resulting database

## Quick start

1. Download a data package (`.tar.gz`) from
   [BOLD Systems — Data Packages](https://bench.boldsystems.org/index.php/datapackages/Latest).
2. Build the database — double-click the **"1"** shortcut for your OS:
   - Windows: `1_Create_DB.lnk` (project root)
   - macOS: `macos/1_Create_DB.command`
   - Linux: `linux/1_Create_DB.desktop`
3. Open the web viewer at `http://127.0.0.1:5001` — double-click the **"2"** shortcut:
   - Windows: `2_Open_web_viewer_BOLD_DB.lnk` (project root)
   - macOS: `macos/2_Open_web_viewer_BOLD_DB.command`
   - Linux: `linux/2_Open_web_viewer_BOLD_DB.desktop`

The Windows shortcuts live at the project root; the macOS and Linux ones are
grouped in their own `macos/`/`linux/` folders to keep the root uncluttered.
All of them just call the matching script in `dev/`
(`bold_db_creator.bat`/`.sh`, `launch_bold_db.bat`/`.sh`) — use those
directly instead if you'd rather run them from a terminal.

**If you move this folder** to another drive, computer or user account, the
Windows and Linux shortcuts embed an absolute path and stop working — run
the matching repair script once to regenerate them:

| OS | Repair command |
|---|---|
| Windows | `repair_shortcuts.bat` (project root) |
| Linux | `linux/repair_shortcuts.sh` |
| macOS | not needed — `.command` files locate themselves automatically |

**First-run OS quirks:**
- **macOS**: Gatekeeper blocks scripts downloaded from the internet the
  first time — right-click the `.command` file → *Open* once to allow it.
- **Linux**: GNOME/KDE require marking a downloaded `.desktop` launcher as
  trusted — right-click it → *Allow Launching* (or *Trust*) once.
- If double-clicking doesn't work at all, run the `.sh` script directly
  from a terminal instead.

## Project structure

```
BOLD_DB/
├── 1_Create_DB.lnk                        # Windows shortcut
├── 2_Open_web_viewer_BOLD_DB.lnk          # Windows shortcut
├── repair_shortcuts.bat                   # regenerates the Windows shortcuts
├── macos/
│   ├── 1_Create_DB.command                # macOS shortcut
│   └── 2_Open_web_viewer_BOLD_DB.command  # macOS shortcut
├── linux/
│   ├── 1_Create_DB.desktop                # Linux shortcut
│   ├── 2_Open_web_viewer_BOLD_DB.desktop  # Linux shortcut
│   └── repair_shortcuts.sh                # regenerates the Linux shortcuts
├── dev/
│   ├── bold_db_creator.py     # Steps 1-4: TSV filtering, SQLite load, indexing, FTS
│   ├── bold_db_creator.bat    # Windows launcher, called by 1_Create_DB.lnk
│   ├── bold_db_creator.sh     # macOS/Linux launcher, called by the "1" shortcuts
│   ├── launch_bold_db.bat     # Windows launcher, called by 2_Open_web_viewer_BOLD_DB.lnk
│   ├── launch_bold_db.sh      # macOS/Linux launcher, called by the "2" shortcuts
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

## License

[Polyform Noncommercial 1.0.0](LICENSE) — free to use, modify and redistribute
for any noncommercial purpose. Commercial use (selling it, offering it as a
paid service, etc.) is not permitted.
