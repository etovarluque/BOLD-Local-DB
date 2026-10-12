# Changelog

All notable changes to this project are documented in this file.

This log starts at `1.1.0`. Changes before that are only in the git history
(`git log`), since versions weren't tracked release-by-release until now.

## [2.2.0]

### Upgrade notes
- Update from Home (*Update*). The database doesn't change: no rebuild needed.

### Added
- **Species delimitation (ASAP and ABGD)** in the sequence analysis. With
  *Delimit species* ticked, computing distances also groups the sequences
  into MOTUs with ASAP (its 10 best partitions) and ABGD (initial and
  recursive partitions for ten prior distances), up to 2,000 sequences per
  alignment group. A new *Species (MOTUs)* tab compares the partition chosen
  with the names and BINs: species that match one MOTU, are split into
  several or share one, and MOTUs identical to a BIN. The NJ tree can be
  coloured and collapsed by MOTU and show it in the labels. Downloads: the
  MOTU of each sequence (CSV) and, in the results ZIP, the partitions, the
  MOTUs and the concordance with ASAP's best partition. Both methods are
  implemented in numpy (no extra programs) and were checked against the
  original programs; ASAP stops at 10,000 coalescent replicates per
  partition.
- The guide has a section on the delimitation and how to read it.
- **Haplotype networks (TCS and MSN)** in the sequence analysis: a new
  *Haplotype network* tab builds the network of one species (also from a
  *Network* button on each row of the Haplotypes tab) or of every sequence,
  up to 2,000 sequences (300 haplotypes with TCS, 600 with MSN). Haplotypes
  are compared over the positions every sequence has complete, so missing
  ends and Ns don't make haplotypes of their own. The page shows Hd, π,
  segregating sites and Tajima's D, draws the network with pies by country,
  species, BIN or MOTU and a tick per mutation (circles can be dragged), and
  exports it as SVG, PNG or PDF, with the haplotypes as CSV or FASTA and a
  NEXUS for PopART. TCS and MSN follow PopART's algorithms and give the same
  networks.

## [2.1.0]

### Upgrade notes
- Update from Home (*Update*). The database doesn't change: no rebuild needed.

### Added
- **Automatic identification check** in the sequence analysis. Computing
  distances now flags, at the top of the Distances tab, the records nearly
  identical (≤1 %) to another species with no sequence of their own species
  within 3 %: likely misidentifications or swapped tubes. It also lists
  species filed under the same epithet in two genera (renamed species) and
  pairs of congeneric species that share sequences. Interim names (sp., cf.,
  aff., with numbers) are left out. Downloadable as CSV and in the results
  ZIP (`identification_check.csv`).
- The guide explains, step by step, what a distance is and how to read the
  barcode gap, with a section on the new check and an FAQ entry.

## [2.0.0]

### Upgrade notes
- **Rebuild the database** (Option 1 or 2 on Home). Databases built until
  now kept only one sequence per specimen and left out ~400,000 sequences of
  its other markers (see *Fixed*). The viewer still opens them, but the
  Creator marks them as outdated and the update refuses them. The current
  `.filt.tsv` can be reused: Step 1 doesn't need repeating.
- **New folder layout and launcher.** Copy the contents of the new release
  into your current folder and open *Start BOLD-Local-DB*: on its first start
  the program moves the database, logs and settings from `app/` into
  `data/database/` and `data/state/`, keeps your field selection, and removes
  `app/`, the old `dev/` launch scripts and the old shortcuts. Make the
  shortcuts again from Home. If the database is open at that moment (a
  viewer of the old version still running), it stays in `app/` and is moved
  on a later start; it works the same meanwhile.

### Added
- **One launcher, no Python to install.** *Start BOLD-Local-DB* (`.bat`,
  `.command`, `.sh`) replaces the "step 0" shortcut generators and the four
  launch scripts. The first time, it downloads uv (pinned version, from
  GitHub, SHA-256 checked), which installs its own Python and the packages
  (`dev/pyproject.toml`, locked in `dev/uv.lock`) into `.runtime/`, about
  150 MB, once. Without internet the first time, it falls back to a Python
  already installed. `--viewer` opens the viewer instead.
- **Desktop and menu shortcuts** made from Home (and offered once on the
  first start): *BOLD-Local-DB* and *BOLD-Local-DB Viewer*, with icons, on
  the desktop and/or in the Start menu (Windows), Applications/Launchpad
  (macOS, as a small `.app`, which Gatekeeper doesn't stop) or the
  applications menu (Linux, marked as trusted). *Remove* deletes exactly
  what was made; if the folder moves they're remade on the next start.
- **Program updates from GitHub Releases.** Home says when a newer version
  is out; *Update* downloads the zip for this system, and the next start
  swaps the program folder, keeping the previous one until the new version
  has run. If it fails to start, the launcher restores the previous one.
  `data/` is never touched. Clones of the repository keep using `git pull`.
- **Releases**: `dev/tools/build_release.py` builds one zip per system and
  `.github/workflows/release.yml` publishes them when a `v*` tag is pushed,
  with the version's CHANGELOG section as notes. A second workflow checks
  the zips, the uv bootstrap, the packages and the shortcuts on Windows,
  macOS and Linux on every push.
- Viewer: a **help icon** next to each panel title (field search, full-text
  search, both batch searches, sequence analysis, results) opens the user
  guide, in the page's language, at that section in a new tab. The viewer
  serves the guide at `/guide/`.
- Home: **Exports folder** button, and **Uninstall…** (removes the shortcuts
  and opens the folder to delete; nothing else is installed outside it).
- **GenBank batch** tab, ported from BarcodeSuite: batch-searches NCBI
  GenBank (nucleotide) online with one list of terms, optionally crossed with
  a second criterion (a single value filters every term; a list is crossed
  with each). Downloads a ZIP with `genbank_counts.csv` (records per query,
  plus optional per-term report columns: marker/gene, moltype, division,
  country) and, optionally, compiled metadata (TSV) and FASTA, plus one folder
  per term. Optional NCBI API key (remembered in the browser), per-query
  record cap, stop with partial results, and the search survives a page
  reload. Requests that fail after retries are logged and marked `ERROR`
  instead of counting as 0 records. One GenBank search runs at a time.
- **Sequence analysis** tab (phase 1, no alignment): composition and quality
  (length histogram, GC, GC3, Ns, ambiguities, BOLD barcode standard),
  translation check for stop codons with the genetic code taken from the
  marker and the taxonomy (or chosen by hand; mitochondrial, plastid,
  Wolbachia and intron-free nuclear genes such as H3 or RAG1 are translated;
  non-coding markers, pseudogenes, multi-gene regions and genes with introns
  are not), sequences submitted reversed detected and read on the reverse
  strand, haplotypes and haplotype
  diversity per species, and species–BIN concordance (BINs with several
  species, species split over several BINs). Runs on the checked records (new
  *Analyse* button on the selection bar), the search on screen or a FASTA
  file, up to 20,000 sequences. Each run covers a single marker: when the
  input holds several, or a FASTA doesn't name it, the tab asks which one.
  Results download as CSV.
- **Sequence analysis, phase 2** (*Distances* and *NJ tree* views, computed
  on demand from the same input): sequences aligned to a reference picked
  from them (placed by shared k-mers; a banded gapped alignment only where an
  indel shows), K2P, Jukes-Cantor (JC69) or p-distances, BOLD-style distance summary (within
  species, genus and family), distribution of intra- vs interspecific
  distances, barcode gap per species (largest intraspecific distance against
  the nearest neighbour, with a scatter plot), and a midpoint-rooted
  neighbour-joining tree. The tree viewer collapses clades by species or BIN
  (automatically above 300 tips) or by clicking a node, colours tips by
  species, BIN, genus, country or species–BIN discordance (eight colours,
  the rest as "Other"; optionally the labels too, darkened where needed to
  stay readable), shows a phylogram or a cladogram, has adjustable row
  height and font size (labels hidden below 5 px rows), chooses which labels
  to show (species, ID, BIN, country), highlights matches of a search, and
  exports the figure as shown as SVG, PNG (2× or 4×, scaled down when the
  image would be too large for the browser) or PDF (through the print
  dialog). Up to 5,000 sequences (1,000 for
  non-coding markers) and 2,000 for the tree, with an optional cap of
  sequences per species applied first; sequences with stop codons are left
  out. Sequences that don't align to a common reference (one marker name can
  hold different genes, e.g. BOLD's "16S" covers bacterial and animal
  mitochondrial 16S) are split into alignment groups, each with its own
  reference, distances and tree, chosen in the tree view; the page lists the
  groups and their taxa. A ZIP holds the alignment (FASTA), the distance matrix, the tree
  (Newick) and the tables. Adds numpy as a dependency (installed
  automatically).

### Changed
- **Folder layout**: the program lives in `.program/` (hidden; `dev/` in the
  repository) and everything the user has in `data/`: `database/` (the
  database, was `app/`), `state/` (settings, logs, field selection, the
  package name the viewer shows, the shortcuts made), plus `raw/`,
  `processed/` and `exports/` as before. The viewer runs straight from the
  program folder: the copy of it in `app/` is gone. The field selection is
  saved in `data/state/`, so an update can't overwrite it.
- The Creator installs `PySide6-Essentials` (the Qt modules it uses) instead
  of the full PySide6, and the viewer no longer installs pip when its
  packages are already there.
- **Sequence analysis** tab, easier to scan: the input (sequences, marker,
  genetic code) folds into a one-line summary after each run, with *Change*
  to reopen it; the summary figures come in three labelled groups
  (composition, quality and translation, taxonomy); the views use the same
  tabs as the search methods, with counts on them (flagged sequences,
  haplotypes, species–BIN cases to review, in orange when they need a look);
  firmer borders on the source choices and selects; download buttons (CSV,
  ZIP, tree figures) in the export green.
- **User guide** (Spanish and English) brought up to date: the Start screen
  and per-step controls, Step 2 storing one row per sequence, the record
  card, record selection, and a new *Sequence analysis* section explaining
  every option and view, how to interpret them, with worked examples. It also
  corrects how field-search conditions combine: AND and NOT bind before OR,
  as in SQL, rather than being evaluated top to bottom.
- The header marks the database's BOLD package date with a yellow badge
  ("50 days old") once it is over a month old.
- The *Batch search* tab is now called **Local BOLD batch** (*Lote en BOLD
  local*), to tell it apart from the new GenBank one, which searches online.
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
- Rebuilding the database no longer touches the current one until the new
  one is finished. Steps 2-4 build it in `app/bold_db.db.building` and it
  replaces `app/bold_db.db` when the run ends, so the viewer keeps serving
  the previous version (with its indexes) the whole time. A rebuild that fails
  or is stopped leaves the current database as it was; before, stopping
  Step 2 deleted it. The new copy needs as much free disk space as the
  current database; Step 2 warns when the disk looks too small.
- The Creator and the viewer share one definition of the database (file and
  table names, version), in `dev/frontend/db_contract.py`. The database now
  records its layout version (`schema_version` in `bold_meta`): the viewer
  shows a clear message instead of SQL errors when the database is missing,
  unreadable or in a layout it doesn't read, and the update refuses a
  database in another layout. Existing databases count as layout 1 and need
  no rebuild.
- The viewer notices when the database file is replaced or updated while it
  runs, and re-reads its columns and full-text index availability instead of
  keeping the ones from startup.

### Fixed
- **Sequences were left out of the database.** Each row of a BOLD package is
  a sequence, not a specimen: a specimen sequenced for several markers
  (COI-5P + 16S, matK + rbcLa, ITS2...) has one row per marker under the same
  processid. Step 2 used processid as the table's key and kept whichever row
  came first in the file, so ~400,000 sequences (about 2%, mostly non-COI
  markers) never reached the database, and some specimens appeared under a
  marker other than the one searched for. Every row with a sequence is now
  stored (database layout 2), and searching by BOLD ID returns one row per
  sequence. The update matches records by processid + marker + content, and
  no longer skipped rows without a processid either.
- Data validation: Step 1 reports how many rows of the package it kept, how
  many it left out for having no sequence (the only reason a row may be left
  out) and how many lines were broken and unreadable. Step 2 checks that
  every row read is stored and matches Step 1's count, and discards the new
  database otherwise; the update checks that the database ends up with
  exactly the package's rows, and rolls back otherwise.
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
