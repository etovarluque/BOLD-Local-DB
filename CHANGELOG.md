# Changelog

All notable changes to this project are documented in this file.

This log starts at `1.1.0`. Changes before that are only in the git history
(`git log`), since versions weren't tracked release-by-release until now.

## [Unreleased]

### Changed
- Batch search with two fields: `summary.csv` also lists, at the end and at
  0, the term × Field 2 value combinations with no records, as the
  single-field mode does with terms. Above a million they're left out (Excel
  couldn't open the file) and the final message says so.
- Batch search with two fields: a root `not_found.txt` lists the terms with
  no records under any Field 2 value, as in the single-field mode, and Field 2
  values with no records no longer get a folder (they still show up with 0 in
  `summary_by_field2.csv`).
- Batch search with two fields is faster when Field 1 finds few records or
  Field 2 isn't indexed: the filter starts from the Field 1 results instead
  of scanning the whole table.
- Batch search: a three-word taxon (`Panthera leo persica`) is searched in
  Subspecies when the database has that field indexed.
- Batch search messages no longer say "1 terms" or "1 records found"; with a
  single term or value and no results, they name it.

### Fixed
- Two batch searches started in the same second wrote to the same ZIP, and
  if one failed or was cancelled it deleted the other's.
- Batch search: different terms that map to the same file name (`A.B` and
  `AB`, or long names sharing a prefix) overwrote each other when the ZIP was
  extracted. The second one now gets a `_2`, `_3`... suffix.
- Batch search: a Field 2 file with only blank lines emptied the result.
  It's now rejected with a message, as a blank Field 1 is.
- Batch search: when the Field 2 filter discarded every record, the message
  said none of the Field 1 terms had been found.
- Batch search: a term with accented letters didn't find the same value
  written with different capitalization (`Mérida` didn't find `MÉRIDA`).
- Batch search: with the FASTA option, a term whose sequences were all
  shorter than 50 bp left an empty `.fasta` in the ZIP.
- Viewer: after closing the browser window, the server console could stay
  open for about 3 minutes (the browser often lost the goodbye message). One
  tab now keeps a connection open to the server and, as soon as it drops, the
  server stops and its console closes within about 15 s, even if the browser
  was closed abruptly.

## [1.2.0]

### Upgrade notes
- Existing databases keep working in the viewer as they are. To use the new
  **Update** option, rebuild the database once (Option 1 or 2): databases
  built by earlier versions don't store the record fingerprints it needs.
- Batch search output changed: `all_results.tsv` no longer repeats records
  that match several terms, FASTA headers carry the most specific taxon
  available (not only the species), and summary CSV headers follow the UI
  language. Scripts that parse these files may need adjusting.

### Added
- **Update the database instead of rebuilding it** (Option 3 on the
  Creator's Home screen). BOLD publishes a full package every week; the
  update runs Step 1 on the new package, compares it record by record with
  the current database and applies only what's new, modified or removed.
  Indexes and full-text search are kept up to date along the way, so Steps 3
  and 4 (most of a rebuild's time) aren't repeated.
  - Step 2 now stores a fingerprint of every record (`bold_row_hash` table).
    Databases built by earlier versions don't have it and must be rebuilt
    once before they can be updated.
  - Everything is applied in a single transaction (WAL mode): a failed or
    stopped update leaves the database exactly as it was, and the viewer
    keeps working while it runs.
  - If more than 10% of the records changed, nothing is modified and a full
    rebuild is recommended (`UPDATE_MAX_CHANGE`).
  - How much faster it is depends on the machine (SSD and RAM vs. database
    size); Home shows the computer's RAM next to the database size so the
    user can choose between updating and rebuilding.
- "Keep the .tar.gz when done" option in Step 1, so changing the field
  selection later doesn't require downloading the package again. "Free disk
  space" on Home can still delete it.
- `bold_meta` table in the database (record count, source file, build and
  update dates). The viewer reads the total from it instead of running
  `COUNT(*)` on every page load.
- Step 2 reports how many records were skipped for a repeated `processid`.
- Step 3 drops the indexes of fields no longer marked for indexing.
- Real progress bar for CSV and FASTA exports (estimated from the search
  count; it never showed before).
- The viewer opens the browser itself once it's listening
  (`server.py --open-browser`, used by the launchers and by "Open viewer"),
  and a second launch just opens the browser instead of failing on the busy
  port.

### Changed
- **Viewer layout**:
  - A "Field" label fills the first condition's AND/OR slot, so all rows
    align.
  - "+ Add condition" sits on the left and "Search" on the right.
  - "Column order" moved from the field-search form to the results toolbar
    (it now also applies to full-text results), next to "Show/hide columns"
    (renamed from "Columns") and "Expand rows", in that order, with matching
    ▼/▲ arrows.
  - Results header: "Clear results" is a discreet link; "Export CSV" uses a
    pastel emerald; the query time is smaller and muted; "(50 rpp)" now reads
    "(50 per page)".
- **Creator appearance**: a palette named by role with readable contrast
  (secondary text went from 2.1:1 to 6.1:1, small buttons from 2.9:1 to
  5.1:1, disabled buttons from 1.3:1 to 3.7:1), the platform's own UI font
  instead of a hardcoded "Segoe UI", the first installed monospace font for
  paths and the log, and ✓/○ step markers from the same text font.
- Home buttons are enabled only when they can run: a package in the drop
  zone, or a pending step whose input is already on disk. The tooltip says
  why when they're disabled.
- The newest package in `data/raw/` is chosen by the release date in its name
  (`BOLD_Public.02-Oct-2026`), and other packages are no longer deleted
  automatically.
- `app/` is synchronised with `dev/frontend` every time the Creator starts,
  so an updated viewer actually reaches users (it was copied only once).
- Batch search: `all_results.tsv` lists every matching record once, totals
  count distinct records, summary headers follow the UI language, and FASTA
  headers use the most specific taxon available, like the FASTA export.
- The viewer opens the database read-only and no longer runs in Flask debug
  mode.
- Autocomplete starts at 3 characters (below that the trigram index can't be
  used and every keystroke scanned the whole table).
- The viewer shuts down a few seconds after its last browser tab closes,
  instead of on the page's `unload` event.
- Server console messages are in English.
- The per-step time and disk-space estimates were removed from the Creator:
  they vary too much between machines to be useful. The import progress now
  shows the percentage done instead of rows per second.
- The English UI calls the Home panel "Start".
- `0_Generate_Shortcuts_*` reports whether it tidied the project root.

### Fixed
- Full-text search failed with a syntax error on ordinary input such as
  `Cote d'Ivoire`, `Peru/Bolivia`, `Panthera, onca` or `Panthera (onca)`.
  Parentheses now group terms, and `a AND NOT b` means `a NOT b` instead of
  searching the word "NOT".
- Reloading the viewer page (F5), or closing one of two tabs, stopped the
  server.
- Results stayed on screen after switching tabs, and "Export", "Export
  FASTA" and pagination acted on the active tab or on the form as edited
  since, instead of on the search shown in the table.
- A `.tar.gz` still being copied into `data/raw/` already counted as Step 1's
  input, and closing the Creator mid-copy left a truncated package behind.
- After an interrupted rebuild, Step 2 could take a half-filled table for a
  finished one and skip itself from then on.
- Closing the Creator (or changing its language) while a step was running
  killed the thread outright, which could corrupt the database; it now stops
  the step safely first.
- Launching a sequence while another step was running put two steps on the
  same database.
- After "Free disk space", Option 2 stopped at Step 1 ("no .tar.gz found")
  instead of skipping the steps already done.
- Pressing Stop as a step finished let the sequence go on to the next one.
- Stop didn't interrupt the full-text index rebuild (Step 4), which kept
  running until it finished on its own.
- The elapsed-time clock kept running after a step in a sequence was stopped
  or failed.
- Range comparisons on "Base count" ignored its index.
- CSV export with "only visible columns" dropped columns without a friendly
  name.
- An export with no usable sequences offered a download of an empty or
  missing file, and the export dialog could keep polling forever.
- In English, each step's input/output paths weren't separated from its
  description, and several Creator messages were hardcoded in Spanish.
- Log lines lost their timestamp when they started with a blank line.
- Sequences made only of alignment gaps (`---`) were imported as records
  without a sequence, and stray spaces were kept in plain-ASCII values.
- Step 4 ignored the database path used by Steps 2 and 3.
- `0_Generate_Shortcuts_*` moved `.git` out of the project root when run in a
  git clone.

### Security
- Extracting the `.tar.gz` no longer follows `../` paths or links outside
  `data/processed/`.
- Removed the `/shutdown` endpoint, which any web page could call; presence
  messages now reject requests from other origins.
- Flask's interactive debugger is no longer enabled.
- Values from the database are no longer inserted as HTML in the
  autocomplete list and progress messages.

## [1.1.2]

### Added
- The BOLD file drop zone is also on the Creator's Home screen, not only in
  Step 1, so the whole process can be started from Home.
- `0_Generate_Shortcuts_macOS.command`, the macOS counterpart of the Windows
  and Linux "step 0" scripts. Linux and macOS shortcuts are now created at the
  project root, like the Windows ones.
- After creating the shortcuts, the project root of a downloaded copy is
  tidied: repository files and the other systems' "step 0" scripts move into
  `project_files/`, leaving only what the user needs to double-click.

### Changed
- Step 1 normalises the sequence field: alignment gaps (`-`) are removed and
  bases are uppercased.

### Fixed
- The Creator's version string was stuck at `1.0.0`.

## [1.1.1]

### Fixed
- Full-text search and advanced field search both failed to match anything
  when the user typed accented characters (e.g. searching "México" or
  "Perú" returned zero results). `bold_db_creator.py` strips accents from
  every text field on import, so the table never has an accented value to
  compare against. Both search paths now strip accents from the user's
  input the same way (`_strip_accents`), matching what Batch Search already
  did.

## [1.1.0]

### Added
- Case-sensitive option for full-text search. The FTS index uses a trigram
  tokenizer that's inherently case-insensitive, so this is implemented as a
  cheap post-filter (`instr()`, case-sensitive in SQLite) applied to the
  already-narrow `MATCH` results.

### Fixed
- FASTA/CSV/batch export threads could hang indefinitely at 0% progress.
  They read the UI language via Flask's `request` object, but ran on plain
  `threading.Thread`s with no request context, so the very first status
  update raised `RuntimeError` and silently killed the thread.
- The FASTA/CSV export loading-dialog titles were hardcoded in Spanish and
  ignored the selected UI language.

## [1.0.0]

Initial tagged release.
