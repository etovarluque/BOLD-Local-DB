# Changelog

All notable changes to this project are documented in this file.

This log starts at `1.1.0`. Changes before that are only in the git history
(`git log`), since versions weren't tracked release-by-release until now.

## [Unreleased]

### Added
- Species delimitation: a *What these results say* section after the ASAP
  and ABGD tables reads the results for users new to them. It proposes the
  partition to start from (ABGD's when it holds over several consecutive
  priors, otherwise ASAP's best), explains the prior P, gap, threshold, score
  and p-value with the analysis' own values, says whether the two methods
  agree and which named species are lumped or split, and warns about species
  with a single sequence or too few sequences. The rows it refers to are
  marked in the tables, and a button shows the proposed partition. The
  reading downloads on its own as PDF (through the print dialog), TXT or
  Markdown, for whoever wants it apart from the results. Built
  from fixed rules, so the same data always get the same reading.
- NJ tree: a *What this tree says* section under the tree reads it the same
  way: which named species and genera form their own clade and which don't
  (with whom they mix, and how far apart), whether the chosen MOTUs are
  clades, sequences far from the rest of their species (and species that
  only fail to be a clade because of one), and the lack of support values.
  The server works out the facts with the real distances; the MOTU check
  follows the partition chosen.
- Both readings abbreviate repeated genera in lists, group unnamed groups,
  and, for COI, warn when the chosen partition lumps named species more than
  2 % apart.

- Haplotype network: a *What this network says* section under the network
  reads it the same way. It groups the haplotypes into lineages (less than
  2 % apart) and says whether they match the BINs, species and countries;
  points out far sequences, naming the species the database files their BIN
  under; reads the diversity with Grant & Bowen's (1998) classes, Tajima's
  D with its p-value and the network's shape (star or not); and warns about
  unsampled haplotypes and long branches.
- Haplotype network: the Tajima's D tile shows its p-value (Tajima's 1989
  beta distribution).

- Sequence analysis from a FASTA: species names are also read from
  headers like `>MN123456.1 Genus species voucher…` (GenBank and most
  sources) and `>Genus_species_id`. After the analysis, a notice says how
  many sequences carried a species name, a BIN and a country, and what
  won't be available without them; a note under the FASTA box lists the
  headers recognised.

- Composition and quality: a *What these sequences say* section after the
  flags reads them the same way. It sums up the barcode standard, the lengths
  (short, much longer, two common lengths) and the stop codons, naming the
  sequences with the most and the project most of them come from; when stops
  affect nearly every sequence it proposes another genetic code, with a button
  that runs the analysis again; and it points out reversed sequences, those
  left out of the haplotypes for their Ns, and those whose GC is far from the
  rest (much longer sequences, which span another stretch of the gene, are
  told apart).
- Haplotypes: a *What these haplotypes say* section after the table reads
  it the same way: the diversity of each species with 10 or more sequences
  in Grant & Bowen's classes, those dominated by one haplotype, the chance
  that the next sequence is a new haplotype (Good's estimate), and an
  approximate count without the extra haplotypes that come from sequences
  trimmed at different ends (matched over their whole shared stretch). It
  also points out provisional names, species with few sequences or many
  left out, and unnamed sequences counted together (with how many BINs they
  span), and a button opens the network of the species with the most
  sequences.
- Species and BINs: a *What the species and BINs say* section after the
  tables reads them the same way. It asks the whole database whose each BIN
  is (at most 40 BINs, indexed, milliseconds), so it can tell records filed
  in another species' BIN (probable misidentifications, with their IDs and
  whether the BIN is of another genus, and pairs of species confused in one
  country) from species spread over several BINs of their own (with the
  countries of each) and truly shared BINs, and lists provisional names,
  species with a BIN of their own and records that can't be compared.
- Distances: a *What the distances say* section at the end of the tab reads
  them the same way. It checks whether each sequence's nearest neighbour is
  of its own species, explains why each species lacks a barcode gap (shared
  sequences, isolated records stretching its largest distance, extremes
  that touch, provisional names), and points out lineages within species
  (sequences chained less than 2 % apart, with their countries, BINs and
  distance to the largest) and isolated records. With a single species it
  reads how much it varies.

### Changed
- Sequence analysis: tabs and views show what the data received allow,
  whatever the source (a search without names is read like a FASTA without
  them). A view the data can't fill is struck through but still opens, to
  say what is missing and how to get it (*Species and BINs* with no record
  holding both a name and a BIN); a view with sections hidden carries an
  orange dot and says which, and why (no names: the barcode gap, the
  identification check and the species-against-MOTUs table; no country: the
  colour by country). Readings no longer point to a view that is struck
  through. The server now sends the field counts for every source, with the
  number of countries.
- Sequence analysis: the seven views are grouped in four tabs. *Quality*;
  *Haplotypes* with the table and the network as two views; *Species and
  BINs*; and *Distances and delimitation* with distances, the NJ tree and the
  MOTUs as three views under one options block (filled in once instead of on
  each tab). Each tab goes back to the view last seen in it. The readings
  point to the views by their new names ("Haplotypes › Table",
  "Distances and delimitation › NJ tree"), naming the tab once when they cite
  two views of it.
- Haplotype network: a species with two sequences (offered by the
  haplotypes table) can now be drawn; alignment groups of two were dropped
  and the network said there were no sequences. The reading only calls the
  network "a single lineage" when every haplotype is chained to the rest:
  otherwise it says how many sequences form the lineage and how many lie
  apart, or that the groups are too small to be lineages (3 sequences of 2
  species, 33 mutations apart, read as one lineage). Whose a BIN is skips
  provisional names (a lineage of Heliconius erato was said to carry the BIN
  of "Heliconius petiveranaDHJ01"). With markers other than COI the 2 %
  lineage cut is flagged as a reference only, nuclear rDNA (ITS, 18S, 28S)
  gets a caution, and isolated sequences of non-mitochondrial markers no
  longer mention NUMTs. Error messages say what to do when a species was
  already chosen or MSN was used, and mention a wrong genetic code when
  stops left too few sequences. Wording: singulars, "in all 3 countries",
  "1 haplotype" in the species list.
- NJ tree reading: "only 0 of the 126 species" (rbcL of Salix) now reads
  that none forms its own group; one sequence per species says the tree
  can't be checked for that reason; a single MOTU reads "the only MOTU"
  ("1 of 1 MOTUs don't" before); and the next step no longer asks to look
  at "these cases" when there are none. Lists with semicolons end "; and
  …" (their items may hold "and"), and the subsample note reads "one
  sequence per species".
- Species delimitation reading: a steady ABGD partition is recommended only
  when its barcode gap could be a jump between species (0.5–20 %). Solenopsis
  got "the most reliable proposal" of 757 MOTUs out of 1,515 sequences with a
  gap of 0.08 %, and Bombus 6 MOTUs lumping 258 species with one of 28 %; the
  reading now says why it starts from ASAP instead, and when ASAP's threshold
  is as implausible (ITS of Penicillium, Carex) that neither method can
  delimit species here. With one sequence per species it warns that the
  methods have no variation within species to work with. Two groups of one
  species read "2 groups of Astyanax mutator" (not "Astyanax mutator and A.
  mutator"), MOTUs joining the same species are listed once with how many
  they are, names with "?" are provisional, and lists separated by
  semicolons end "; and 10 more" (here and in the network reading). The
  English next steps use straight quotes.
- Distances reading: the species named as "the trouble" are those with the
  most sequences mixed with another species (it named the three with the
  most sequences, or the first three alphabetically when subsampled: Danaus
  chrysippus with 11 of 254 instead of D. montezuma with 30 of 32).
  Provisional names no longer count among the species with or without a
  barcode gap, and the genus/species ratio leaves them out ("Conus sp."
  mixes species and put the mean within species at 12.6 % in 12S); trailing
  capitals ("Lumbricus rubellus CEA"), hybrids and "indet." count as
  provisional in distances and delimitation too. With one sequence per
  species it says so and asks for more; when most sequences were left out
  for stop codons (a wrong genetic code) the reading and the error message
  say so. Wording: "all its 5 sequences", "all 9 species", lineages just
  past 2 % shown with two decimals, and lists cut as "a, b and 5 more".
- Species and BINs reading: records count as out of place only in a BIN
  that another species holds more than half of. A BIN of many species (70
  Acropora species in BOLD:AAF5735, none with even a sixth of it) is now
  read as shared instead of calling 256 records misidentifications, and a
  species whose records all sit in another species' BIN (Artemia monica)
  shares it rather than straying into it, unless it also has a BIN of its
  own. A species' records are never both out of place and sharing a BIN;
  small BINs listed as a species' own are only those it holds; species
  inside another's BIN or out of place no longer appear among those whose
  name and BIN agree. Hybrids and "indet." count as provisional names, and
  "Unspecified country" and "Exception - …" aren't taken as places (here,
  in distances and in the network). The sentence on shared BINs depends on
  how many records each species has, the pair of species confused in a
  country is said once, long lists are cut, and the species left out of
  the out-of-place list are counted on a line of their own.
- Haplotypes reading: names that stop at the genus ("Amanita sp.") are no
  longer read as species (they may hold several): they are listed apart with
  their HD, and are never the species to start with nor the network offered.
  "Most" now means more than half (exactly half is said as such), species
  that lose every sequence to Ns are told apart from those losing some, and
  the trimming list reads "Apis mellifera (504 → about 320)". Wording: "all
  5" and "both" instead of "5 of the 5", "all 19 of its sequences are one
  haplotype", a capital on "An unnamed group", and a next step that doesn't
  point to BINs when the input has none.
- Composition and quality reading: sequences whose GC is far from the rest
  but which are much shorter than the median (fragments: a short stretch has
  a composition of its own) are now told apart like the much longer ones,
  instead of being called possible contamination; when those far from the
  rest are many (10 or more, and at least 10 % of the sequences) they are
  read as a group with a composition of its own. With a forced genetic code
  that leaves stops everywhere, the automatic code is proposed whenever it
  clears them, before a code that only has fewer stop codons. Wording fixes:
  singulars, "all from project …", sequences with a single stop each, and
  "2 of the 4" instead of "most" for half of them.
- Sequence analysis: every *What … say* reading has an orange warning
  triangle next to its title; hovering it (or reaching it with the keyboard)
  shows that automatic readings may contain errors or inaccuracies.
- Sequence analysis: *Results of the search on screen* is now the first and
  default source, and the sequences box ends level with the genetic code
  select (the FASTA box and the Analyse button sit on a row below).
- Species and BINs: the tile and table title of species spread over
  several BINs read "Split into several MOTUs" (a key shared with the MOTU
  view); they now read "Species in several BINs" again.
- Translation check: a stop in the last codon no longer counts. Sequences
  that run to the end of the gene (complete COI genes from GenBank, 1,545 bp)
  ended on their real stop codon and were flagged, left out of the distances
  and counted as failing the barcode standard.
- Export copies in `data/exports/` only last while the viewer is open: the
  folders are emptied when it starts, since files from earlier sessions could
  no longer be downloaded from it anyway (the browser already saved its copy
  to the downloads folder). Within a session they still keep the 20 most
  recent files. The cleanup now runs after the "already running" check, so a
  second launch doesn't empty the running viewer's.
- The analyses' results ZIP is no longer written to disk: it is kept
  compressed in memory for the last 5 analyses and sent when you press the
  button (`data/exports/analysis_exports/` is removed).
- Creator: *Open the viewer* on Start is green once there is a database to
  open; the *Completed* badge of a step is as tall as the buttons beside it.
- Creator: the FTS step's log says the progress bar moves without a
  percentage, instead of saying it doesn't move.
- User guide: what the database update needs (the new package's filtered
  TSV; the previous one isn't needed).

## [2.2.1]

### Upgrade notes
- Update from Home (*Update*). The database doesn't change: no rebuild needed.

### Fixed
- Species delimitation: ABGD could take minutes looking for the barcode gap
  when the gaps found with successive window sizes kept disagreeing. The
  search now stops after the same budget as when there is no gap, keeping
  the last window's answer.
- With sequences in several alignment groups, each group keeps the MOTU
  partition chosen for it: opening another group's tree no longer resets it
  to ASAP's best partition or changes the group shown on the *Species
  (MOTUs)* tab. Colouring the haplotype network by MOTU uses each group's
  chosen partition.
- Haplotype network: releasing a dragged circle outside the figure now ends
  the drag.

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
