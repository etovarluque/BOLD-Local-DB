"""Sequence analyses of the viewer's "Sequence analysis" tab (phase 1).

Analyses that need no alignment, so they run on tens of thousands of
sequences in seconds: composition and quality, a translation check for stop
codons, haplotypes per species, and the species/BIN concordance of the
records. Standard library only, like the rest of the viewer.

A run covers one marker. Lengths, composition and haplotypes of different
genes can't be compared, and the marker decides whether and how sequences are
translated; the caller picks the marker (see `marker_counts`) and passes it
to `run_basic`, which applies it to every record.

Every function takes records as dicts with these keys (any may be missing,
except `seq`): id, processid, sampleid, bin, species, genus, family, order,
class, phylum, kingdom, country, marker, seq.
"""

import re
import statistics
from collections import Counter, defaultdict

# ── Limits ────────────────────────────────────────────────────────────────────

# Most sequences analysed in one run. Everything here is linear in the number
# of sequences, except haplotypes, which are linear in sequences × haplotypes
# per species; the cap keeps a run within seconds on an ordinary computer.
MAX_SEQUENCES = 20000

# Largest FASTA accepted, in characters (≈ 50 MB)
MAX_FASTA_CHARS = 50_000_000

# Sequences shorter than this (bp, after removing gaps and end Ns) are left
# out, as the FASTA export does: they are fragments with nothing to analyse.
MIN_LENGTH = 50

# BOLD's barcode standard for COI-5P: at least 500 bp, under 1% Ns, no stop
# codons.
BARCODE_MIN_BP = 500
BARCODE_MAX_N_FRACTION = 0.01

# Width of the bars of the length histogram, in bp
HISTOGRAM_BIN_BP = 25

# Reading guide of the quality view (see `guide`): a sequence whose GC is
# further from the median than GUIDE_GC_POINTS percentage points, or than
# GUIDE_GC_SD robust standard deviations, is unlike the rest; one longer
# than GUIDE_LONG times the median likely spans more of the gene; two
# lengths each held by GUIDE_TWO_LENGTHS of the sequences point to two
# primer pairs; stops in GUIDE_CODE_SHARE of the translated sequences point
# to a wrong genetic code, and another code that clears GUIDE_CODE_CLEARS of
# them is offered. Sequences much longer or shorter than the median (by
# GUIDE_LONG times) aren't flagged for their GC, which may only reflect the
# stretch they span.
GUIDE_GC_POINTS = 4.0
GUIDE_GC_SD = 5
# As many apart as GUIDE_GC_GROUP of the sequences (and GUIDE_GC_GROUP_MIN)
# are a group with a composition of its own rather than suspects
GUIDE_GC_GROUP = 0.1
GUIDE_GC_GROUP_MIN = 10
GUIDE_LONG = 1.5
GUIDE_TWO_LENGTHS = 0.15
GUIDE_CODE_SHARE = 0.2
GUIDE_CODE_CLEARS = 0.8
# Items listed by name in each point
GUIDE_LIST = 5

# Reading guide of the haplotypes view (see `haplotype_guide`): species with
# at least GUIDE_HAP_MIN sequences are read (fewer make Hd unstable), up to
# GUIDE_HAP_SPECIES of them. Haplotypes equal over their whole overlap of
# at least GUIDE_HAP_OVERLAP bp (one starting inside the other) were likely
# one, trimmed at different ends; species with more than GUIDE_HAP_MAX_REPS
# haplotypes aren't checked.
GUIDE_HAP_MIN = 10
GUIDE_HAP_SPECIES = 100
GUIDE_HAP_OVERLAP = 200
GUIDE_HAP_MAX_REPS = 5000
_HAP_KMER = 30

# Reading guide of the species & BINs view (see `concordance_guide`): whose
# a BIN is comes from the whole database, asked for at most
# GUIDE_CONC_LOOKUP BINs. A species' BIN with GUIDE_CONC_OWN or more of its
# records is a lineage of its own; another species holding GUIDE_CONC_SHARED
# of a BIN's records (and at least 3) shares it.
GUIDE_CONC_LOOKUP = 40
GUIDE_CONC_OWN = 5
GUIDE_CONC_SHARED = 0.1
# Records are out of place only in a BIN whose owner holds this share of it
GUIDE_CONC_OWNER = 0.5
GUIDE_CONC_SPECIES = 8

# ── Genetic codes ─────────────────────────────────────────────────────────────

# NCBI translation tables, as their 64 amino acids in TCAG codon order
_CODE_TABLES = {
    1:  "FFLLSSSSYY**CC*WLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG",
    2:  "FFLLSSSSYY**CCWWLLLLPPPPHHQQRRRRIIMMTTTTNNKKSS**VVVVAAAADDEEGGGG",
    4:  "FFLLSSSSYY**CCWWLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG",
    5:  "FFLLSSSSYY**CCWWLLLLPPPPHHQQRRRRIIMMTTTTNNKKSSSSVVVVAAAADDEEGGGG",
    9:  "FFLLSSSSYY**CCWWLLLLPPPPHHQQRRRRIIIMTTTTNNNKSSSSVVVVAAAADDEEGGGG",
    11: "FFLLSSSSYY**CC*WLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG",
    13: "FFLLSSSSYY**CCWWLLLLPPPPHHQQRRRRIIMMTTTTNNKKSSGGVVVVAAAADDEEGGGG",
}

GENETIC_CODES = sorted(_CODE_TABLES)

# Stop codons of each table, the only thing the check needs
_STOPS = {}
for _code, _aas in _CODE_TABLES.items():
    _bases = "TCAG"
    _STOPS[_code] = frozenset(
        a + b + c
        for i, a in enumerate(_bases)
        for j, b in enumerate(_bases)
        for k, c in enumerate(_bases)
        if _aas[16 * i + 4 * j + k] == "*")

# Protein-coding markers, by the marker code BOLD uses (uppercased). Only
# markers that are a stretch of one gene with no introns are translated:
# everything else (rRNA, ITS, spacers, introns, regions spanning several
# genes, pseudogenes such as COI-NUMT, and nuclear genes whose fragments
# often carry introns, such as EF1-alpha or CAD) would show stop codons that
# mean nothing.
_MITO_MARKERS = {"COI-5P", "COI-3P", "COI-5P3P", "COII", "COIII", "COXIII",
                 "CYTB", "ATP6", "ATP8", "ND1", "ND2", "ND3", "ND4", "ND4L",
                 "ND5", "ND5-0", "ND6", "MATR"}
# Plastid genes, and Wolbachia's (BOLD files them under the host): code 11
_PLASTID_MARKERS = {"RBCL", "RBCLA", "MATK", "TUFA", "RPOB", "RPOC1", "PSBA",
                    "PSBA-3P", "YCF1", "COXA", "WSP", "FBPA", "HCPA"}
# Nuclear genes usually sequenced without introns: standard code
_NUCLEAR_MARKERS = {"H3", "RAG1", "RAG2", "RHO", "WNT1", "MC1R"}

MARKER_KINDS = {**{m: "mito" for m in _MITO_MARKERS},
                **{m: "plastid" for m in _PLASTID_MARKERS},
                **{m: "nuclear" for m in _NUCLEAR_MARKERS}}

# Mitochondrial code by taxon, checked from the most specific rank
_MITO_CODE_BY_CLASS = {"ascidiacea": 13}
_MITO_CODE_BY_PHYLUM = {
    "chordata": 2,
    "echinodermata": 9, "hemichordata": 9, "platyhelminthes": 9,
    "cnidaria": 4, "porifera": 4, "ctenophora": 4, "placozoa": 4,
}
_MITO_CODE_BY_KINGDOM = {"fungi": 4, "protista": 4}

# Code for a mitochondrial marker of a record with no usable taxonomy
DEFAULT_MITO_CODE = 5


def marker_kind(marker):
    """'mito', 'plastid', 'nuclear', 'noncoding' (not translated), or None
    when the marker is unknown."""
    if not marker or not marker.strip():
        return None
    return MARKER_KINDS.get(marker.strip().upper(), "noncoding")


def genetic_code_for(rec, forced=None):
    """Translation table for a record, or None when it isn't translated.

    `forced` (a table number) overrides the automatic choice for every coding
    marker, and is the only way a sequence with no marker gets translated;
    non-coding markers are never translated.
    """
    kind = marker_kind(rec.get("marker"))
    if kind == "noncoding" or (kind is None and not forced):
        return None
    if forced:
        return forced
    if kind == "plastid":
        return 11
    if kind == "nuclear":
        return 1
    for key, table in (("class", _MITO_CODE_BY_CLASS),
                       ("phylum", _MITO_CODE_BY_PHYLUM),
                       ("kingdom", _MITO_CODE_BY_KINGDOM)):
        value = (rec.get(key) or "").strip().lower()
        if value in table:
            return table[value]
    if (rec.get("kingdom") or "").strip().lower() == "plantae":
        # Plant mitochondria use the standard code
        return 1
    return DEFAULT_MITO_CODE


# ── Sequences ─────────────────────────────────────────────────────────────────

_COMPLEMENT = str.maketrans("ACGTRYKMBVDHN", "TGCAYRMKVBHDN")
_GAPS_RE = re.compile(r"[-.\s]")
_END_N_RE = re.compile(r"^N+|N+$")
_AMBIGUOUS = set("RYKMSWBDHV")
_VALID = set("ACGTN") | _AMBIGUOUS


def clean_sequence(seq):
    """Uppercase, without gaps, whitespace or Ns at either end."""
    s = _GAPS_RE.sub("", (seq or "").upper()).replace("U", "T")
    return _END_N_RE.sub("", s)


def reverse_complement(seq):
    return seq.translate(_COMPLEMENT)[::-1]


def _last_codon(seq, frame):
    """Start of the last whole codon of `seq` read in `frame`."""
    return frame + (len(seq) - frame) // 3 * 3 - 3


def _count_stops(seq, frame, stops):
    """Stop codons in `frame`, but for one in the last codon: a sequence
    that runs to the end of the gene ends on its real stop codon."""
    n = 0
    for i in range(frame, _last_codon(seq, frame), 3):
        if seq[i:i + 3] in stops:
            n += 1
    return n


def check_translation(seq, code):
    """Best reading frame of `seq` under `code` and its stop codons.

    Barcodes are fragments with no known frame, so the three forward frames
    are tried and the one with fewest stops wins. The reverse strand is only
    looked at when every forward frame has stops: if one of its frames has
    none, the sequence was most likely submitted reversed, and that frame is
    the one returned. Taking the best of all six frames every time would let
    a chance stop-free reverse frame hide real stops.
    Codons with N or ambiguities never count as stops, nor does a stop in the
    last codon (the end of the gene).
    Returns (the sequence as read: `seq` or its reverse complement,
    frame 0-2 on it, stops in that frame, whether it was reversed).
    """
    stops = _STOPS[code]
    counts = [_count_stops(seq, f, stops) for f in range(3)]
    best = min(range(3), key=lambda f: counts[f])
    if counts[best] > 0:
        rc = reverse_complement(seq)
        rc_counts = [_count_stops(rc, f, stops) for f in range(3)]
        rc_best = min(range(3), key=lambda f: rc_counts[f])
        if rc_counts[rc_best] == 0:
            return rc, rc_best, 0, True
    return seq, best, counts[best], False


def gc3(seq, frame):
    """GC at the third codon position, in % of the unambiguous ones."""
    gc = at = 0
    for i in range(frame + 2, len(seq), 3):
        b = seq[i]
        if b in "GC":
            gc += 1
        elif b in "AT":
            at += 1
    return 100.0 * gc / (gc + at) if gc + at else None


# ── FASTA ─────────────────────────────────────────────────────────────────────

_MARKER_RE = re.compile(r"^(CO[IX]|CYTB|ND\d|ATP\d|RBCL|MATK|ITS|5-8S|12S|16S|18S|28S|D-LOOP|"
                        r"TRN|PSB|RPO|H3|RAG\d|RHO|WNT|EF1|CAD)", re.IGNORECASE)


def _taxon_fields(taxon):
    """species/genus from a FASTA taxon field ("Genus_species" or "Genus species")."""
    taxon = (taxon or "").replace("_", " ").strip()
    if not taxon:
        return {}
    words = taxon.split()
    if len(words) >= 2 and words[0][:1].isupper() and words[1][:1].islower():
        return {"species": taxon, "genus": words[0]}
    return {"taxon": taxon}


_GENUS_RE = re.compile(r"^[A-Z][a-z]{2,}$")
_EPITHET_RE = re.compile(r"^[a-z][a-z-]{2,}$")
# Words that stand for an epithet, the first two followed by one
_QUALIFIERS = {"cf.": True, "aff.": True, "cf": True, "aff": True, "sp.": False, "sp": False}
# Words GenBank puts after a name that look like an epithet but are not one
# ("Diptera voucher ROM 1", "Carollia isolate X")
_NOT_EPITHETS = {"voucher", "isolate", "specimen", "sample", "clone", "haplotype", "strain", "mitochondrion",
                 "mitochondrial", "cytochrome", "partial", "complete", "genomic", "unverified", "uncultured"}


def _species_in(words):
    """"Genus epithet" at the start of `words` ("Carollia perspicillata
    voucher...", "Carollia cf. brevicauda", "Carollia sp. ROM 1"), or None."""
    if len(words) < 2 or not _GENUS_RE.match(words[0]):
        return None
    second = words[1]
    if second.lower() in _QUALIFIERS:
        if _QUALIFIERS[second.lower()] and len(words) > 2 and _EPITHET_RE.match(words[2]):
            return f"{words[0]} {second} {words[2]}"
        return f"{words[0]} sp." if not _QUALIFIERS[second.lower()] else None
    return f"{words[0]} {second}" if _EPITHET_RE.match(second) and second.lower() not in _NOT_EPITHETS else None


# How a header is split when the user assigns its parts by hand
FASTA_SEPARATORS = {"|": "|", "space": None, "_": "_", ",": ",", ";": ";", "tab": "\t"}
FASTA_FIELDS = ("id", "taxon", "bin", "marker")


def clean_fasta_map(raw):
    """A manual assignment of header parts to fields, validated:
    {"sep": key of FASTA_SEPARATORS, "fields": {field: [part indexes]}}.
    None when absent or empty, so the headers are read automatically."""
    if not isinstance(raw, dict) or raw.get("sep") not in FASTA_SEPARATORS:
        return None
    given = raw.get("fields") if isinstance(raw.get("fields"), dict) else {}
    fields = {}
    for name in FASTA_FIELDS:
        idx = given.get(name)
        if isinstance(idx, list):
            idx = [i for i in idx if isinstance(i, int) and not isinstance(i, bool) and 0 <= i < 100][:6]
            if idx:
                fields[name] = idx
    return {"sep": raw["sep"], "fields": fields} if fields else None


def _parse_mapped(header, mapping):
    """A header read with the parts the user assigned to each field. Several
    parts of one field are joined with a space ("Genus" + "species")."""
    sep = FASTA_SEPARATORS[mapping["sep"]]
    parts = [p.strip() for p in (header.split(sep) if sep else header.split())]

    def pick(name):
        return " ".join(parts[i] for i in mapping["fields"].get(name, [])
                        if i < len(parts) and parts[i]).strip()

    words = header.split()
    rec = {"id": pick("id") or (words[0] if words else "")}
    taxon = pick("taxon")
    if taxon:
        names = taxon.replace("_", " ").split()
        species = _species_in(names)
        if species and species.endswith(" sp."):
            # "Insecta sp.": only the genus-or-higher name is known
            rec["taxon"] = names[0]
        elif species:
            rec.update(species=species, genus=species.split()[0])
        else:
            rec["taxon"] = " ".join(names)
    for name in ("bin", "marker"):
        value = pick(name)
        if value:
            rec[name] = value
    return rec


def parse_header(header, mapping=None):
    """Fields of a FASTA header.

    Understands the viewer's own export (processid|sampleid|BIN|taxon|marker),
    BOLD's (processid|taxon|marker|accession), an identifier followed by the
    name (">MN123456.1 Carollia perspicillata voucher...", as GenBank and
    most sources write it) and a name joined to the identifier
    (">Carollia_perspicillata_ABC123"). Anything else keeps its first word
    as the identifier.
    """
    header = header.strip()
    if mapping:
        return _parse_mapped(header, mapping)
    parts = [p.strip() for p in header.split("|")]
    rec = {"id": header.split()[0] if header else ""}
    if len(parts) >= 3 and (parts[2].upper().startswith("BOLD:") or parts[2] == ""):
        rec.update(processid=parts[0], sampleid=parts[1], bin=parts[2])
        if len(parts) >= 4:
            rec.update(_taxon_fields(parts[3]))
        if len(parts) >= 5:
            rec["marker"] = parts[4]
        rec["id"] = parts[0]
    elif len(parts) >= 3 and _MARKER_RE.match(parts[2]):
        rec.update(processid=parts[0], marker=parts[2])
        rec.update(_taxon_fields(parts[1]))
        rec["id"] = parts[0]
    elif len(parts) == 1 and header:
        # GenBank marks some records "UNVERIFIED: Genus species..."
        words = [w for w in header.split()[1:] if not w.endswith(":")]
        species = _species_in(words) or _species_in(header.split()[0].split("_"))
        if species:
            rec.update(species=species, genus=species.split()[0])
    return rec


def field_counts(records):
    """How many records bring a species name, a BIN, a country and
    coordinates, and how many countries: what the views that need them can
    show. The big_* counts are zones with enough sequences for the
    Diversity tab, to pick its default zones."""
    import seq_popgen  # it imports this module
    import seq_diversity
    places = [r.get("country") for r in records if not no_place(r.get("country"))]
    provinces = Counter((r.get("country"), r.get("province")) for r in records if r.get("province"))
    cells = Counter(seq_diversity.zone_of(r, "grid") for r in records)
    cells.pop("", None)
    big = lambda counts: sum(1 for n in counts.values() if n >= seq_diversity.MIN_ZONE_SEQUENCES)
    return {"n": len(records),
            "species": sum(1 for r in records if r.get("species")),
            "bin": sum(1 for r in records if r.get("bin")),
            "country": len(places),
            "countries": len(set(places)),
            "provinces": len(provinces),
            "coords": sum(1 for r in records if seq_popgen.coord_of(r)),
            "big_countries": big(Counter(places)),
            "big_provinces": big(provinces),
            "big_cells": big(cells)}


def parse_fasta(text, mapping=None):
    """Records of a FASTA text, in file order. `mapping` (see
    clean_fasta_map) reads the headers by the parts the user assigned."""
    records = []
    header, chunks = None, []

    def flush():
        if header is not None:
            rec = parse_header(header, mapping)
            rec["header"] = header
            rec["seq"] = "".join(chunks)
            records.append(rec)

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(">"):
            flush()
            header, chunks = line[1:], []
        elif header is not None:
            chunks.append(line)
    flush()
    return records


def resolve_taxon_names(records, lookup):
    """Ranks of the taxon names that are not species (`lookup`, given names,
    returns {name: rank}). A name the database doesn't know but whose first
    word it does ("Diptera voucher X", "Insecta sp.") is cut to that word in
    the records; what neither knows stays as written, with no rank."""
    names = sorted({r["taxon"] for r in records if r.get("taxon") and not r.get("species")})
    ranks = lookup(names)
    firsts = {n: n.split()[0] for n in names if n not in ranks and " " in n}
    if firsts:
        found = lookup(sorted(set(firsts.values())))
        for r in records:
            first = firsts.get(r.get("taxon"))
            if first in found:
                r["taxon"] = first
        ranks.update(found)
    return ranks


def fasta_preview(records, ranks=None, n=5):
    """What was read from the first headers, for the user to check."""
    ranks = ranks or {}
    out = []
    for r in records[:n]:
        name = r.get("species") or r.get("taxon") or ""
        out.append({"header": r.get("header", ""),
                    "id": r.get("processid") or r.get("id") or "",
                    "taxon": name,
                    "rank": "species" if r.get("species") else ranks.get(name, ""),
                    "bin": r.get("bin") or "", "marker": r.get("marker") or ""})
    return out


def rank_counts(records, ranks=None):
    """Records by the level of the taxon name their header gave: a rank,
    "unknown" for a name no rank of the database knows, "none" for no name."""
    ranks = ranks or {}
    counts = Counter()
    for r in records:
        if r.get("species"):
            counts["species"] += 1
        elif r.get("taxon"):
            counts[ranks.get(r["taxon"]) or "unknown"] += 1
        else:
            counts["none"] += 1
    return dict(counts)


# ── Analyses ──────────────────────────────────────────────────────────────────

def _label(rec):
    return rec.get("processid") or rec.get("id") or ""


def composition(records, forced_code=None):
    """Per-sequence composition, quality flags and translation check.

    Returns (rows, skipped): one dict per analysed sequence, and how many
    were left out for being shorter than MIN_LENGTH.
    """
    rows, skipped = [], 0
    for rec in records:
        seq = rec["clean"]
        n = len(seq)
        if n < MIN_LENGTH:
            skipped += 1
            continue
        counts = Counter(seq)
        a, c, g, t = counts["A"], counts["C"], counts["G"], counts["T"]
        n_n = counts["N"]
        ambiguous = sum(v for k, v in counts.items() if k in _AMBIGUOUS)
        invalid = sum(v for k, v in counts.items() if k not in _VALID)
        acgt = a + c + g + t
        row = {
            "id": _label(rec),
            "species": rec.get("species") or rec.get("taxon") or "",
            "bin": rec.get("bin") or "",
            "marker": rec.get("marker") or "",
            "length": n,
            "A": a, "C": c, "G": g, "T": t,
            "N": n_n, "ambiguous": ambiguous, "invalid": invalid,
            "gc": round(100.0 * (g + c) / acgt, 2) if acgt else None,
            "gc3": None, "code": None, "frame": None, "stops": None,
            "reversed": False,
        }
        code = genetic_code_for(rec, forced_code)
        if code is not None:
            read, frame, n_stops, rev = check_translation(seq, code)
            if rev:
                # Read on the reverse strand from here on, haplotypes included
                rec["clean"] = read
            gc3_value = gc3(read, frame)
            # Reverse-strand frames are shown as -1, -2, -3
            row.update(code=code, frame=-(frame + 1) if rev else frame + 1,
                       stops=n_stops, reversed=rev,
                       gc3=round(gc3_value, 2) if gc3_value is not None else None)

        flags = []
        if n_n / n > BARCODE_MAX_N_FRACTION:
            flags.append("many_n")
        if ambiguous:
            flags.append("ambiguous")
        if invalid:
            flags.append("invalid")
        if row["reversed"]:
            flags.append("reversed")
        elif row["stops"]:
            flags.append("stops")
        # The barcode standard only applies to COI-5P (or to unlabelled
        # sequences, which are usually COI-5P barcodes)
        marker = (rec.get("marker") or "").upper()
        if marker in ("", "COI-5P"):
            row["barcode"] = (n >= BARCODE_MIN_BP and "many_n" not in flags
                              and "invalid" not in flags and not row["stops"]
                              and not row["reversed"])
            if n < BARCODE_MIN_BP:
                flags.append("short")
        else:
            row["barcode"] = None
        row["flags"] = flags
        rows.append(row)
    return rows, skipped


def _length_histogram(lengths):
    if not lengths:
        return []
    lo = min(lengths) // HISTOGRAM_BIN_BP * HISTOGRAM_BIN_BP
    hi = max(lengths) // HISTOGRAM_BIN_BP * HISTOGRAM_BIN_BP
    # At most ~60 bars: widen them for very spread lengths
    width = HISTOGRAM_BIN_BP
    while (hi - lo) / width > 60:
        width *= 2
    lo = lo // width * width
    counts = Counter((x - lo) // width for x in lengths)
    return [{"from": lo + i * width, "to": lo + (i + 1) * width - 1, "n": counts.get(i, 0)}
            for i in range((hi - lo) // width + 1)]


def _summary(rows, skipped, total):
    lengths = [r["length"] for r in rows]
    gcs = [r["gc"] for r in rows if r["gc"] is not None]
    gc3s = [r["gc3"] for r in rows if r["gc3"] is not None]
    flag_counts = Counter(f for r in rows for f in r["flags"])
    translated = [r for r in rows if r["stops"] is not None]

    def stats(values, digits=1):
        if not values:
            return None
        return {"min": round(min(values), digits), "max": round(max(values), digits),
                "mean": round(statistics.fmean(values), digits),
                "median": round(statistics.median(values), digits)}

    return {
        "received": total,
        "analysed": len(rows),
        "skipped_short": skipped,
        "length": stats(lengths, 0),
        "gc": stats(gcs),
        "gc3": stats(gc3s),
        "histogram": _length_histogram(lengths),
        "codes": Counter(r["code"] for r in translated).most_common(),
        "translated": len(translated),
        "with_stops": sum(1 for r in translated if r["stops"]),
        "barcode_applicable": sum(1 for r in rows if r["barcode"] is not None),
        "barcode_compliant": sum(1 for r in rows if r["barcode"]),
        "flags": dict(flag_counts),
    }


def haplotypes(records, reps_out=None):
    """Haplotypes per species, comparing sequences without alignment.

    Sequences with Ns or ambiguities are left out (one N would make a new
    haplotype). A sequence contained in a longer one counts as that same
    haplotype, since barcodes of one haplotype differ mostly in where they
    were trimmed; it joins the longest one that contains it.
    `reps_out`, a dict, receives each species' haplotype sequences.
    """
    groups = defaultdict(list)
    excluded = Counter()
    for rec in records:
        seq = rec["clean"]
        if len(seq) < MIN_LENGTH:
            continue
        species = rec.get("species") or rec.get("taxon") or ""
        if set(seq) - set("ACGT"):
            excluded[species] += 1
            continue
        groups[species].append(seq)

    result = []
    for species in sorted(set(groups) | set(excluded), key=lambda s: (s == "", s)):
        seqs = groups.get(species, [])
        reps, counts, exact = [], [], {}
        # Longest first, so a fragment always finds its full haplotype.
        # Most sequences repeat one already seen exactly: the dict answers
        # those without scanning every haplotype.
        for seq in sorted(seqs, key=len, reverse=True):
            i = exact.get(seq)
            if i is None:
                i = next((k for k, rep in enumerate(reps)
                          if len(rep) > len(seq) and seq in rep), None)
                if i is None:
                    i = len(reps)
                    reps.append(seq)
                    counts.append(0)
                exact[seq] = i
            counts[i] += 1
        n = len(seqs)
        if reps_out is not None:
            reps_out[species] = reps
        hd = None
        if n >= 2:
            hd = n / (n - 1) * (1 - sum((c / n) ** 2 for c in counts))
        result.append({
            "species": species,
            "n": n,
            "excluded": excluded.get(species, 0),
            "haplotypes": len(reps),
            "singletons": sum(1 for c in counts if c == 1),
            "hd": round(hd, 4) if hd is not None else None,
            "counts": sorted(counts, reverse=True),
        })
    return result


def _joined_haplotypes(reps):
    """How many haplotypes are left when those equal over their whole
    overlap (at least GUIDE_HAP_OVERLAP bp, one starting inside the other:
    trimmed at different ends) are joined. An estimate: a short haplotype
    that fits two others joins them too. None past GUIDE_HAP_MAX_REPS."""
    if len(reps) > GUIDE_HAP_MAX_REPS:
        return None
    # Where each k-mer starts in each haplotype, but at its very start
    index = defaultdict(list)
    for a, s in enumerate(reps):
        for p in range(1, len(s) - GUIDE_HAP_OVERLAP + 1):
            index[s[p:p + _HAP_KMER]].append((a, p))
    parent = list(range(len(reps)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for b, s in enumerate(reps):
        for a, p in index.get(s[:_HAP_KMER], ()):
            overlap = len(reps[a]) - p
            if a != b and overlap < len(s) and reps[a][p:] == s[:overlap]:
                parent[find(a)] = find(b)
    return len({find(i) for i in range(len(reps))})


def _genus_only(species):
    """A name that stops at the genus: "Amanita sp.", "Russula spp.", or
    the genus alone (a FASTA's taxon field)."""
    words = species.split()
    return len(words) == 1 or (len(words) == 2 and words[1].rstrip(".").lower() in ("sp", "spp"))


def haplotype_guide(records, haps, reps):
    """Facts for the reading of the haplotypes view, which the page words.

    `haps` is `haplotypes(records)`, and `reps` the haplotype sequences it
    put out per species. Names that stop at the genus ("Amanita sp.") may
    hold several species: they are listed apart, not read as species.
    """
    genus_only = sorted((h for h in haps if h["species"] and _genus_only(h["species"]) and h["n"] >= GUIDE_HAP_MIN),
                        key=lambda h: (-h["n"], h["species"]))
    named = [h for h in haps if h["species"] and not _genus_only(h["species"])]
    big = sorted((h for h in named if h["n"] >= GUIDE_HAP_MIN), key=lambda h: (-h["n"], h["species"]))
    few = sorted((h for h in named if 2 <= h["n"] < GUIDE_HAP_MIN), key=lambda h: (-h["n"], h["species"]))
    species = []
    for h in big[:GUIDE_HAP_SPECIES]:
        species.append({k: h[k] for k in ("species", "n", "haplotypes", "singletons", "hd", "excluded")})
        species[-1]["top"] = round(h["counts"][0] / h["n"], 3)
        species[-1]["joined"] = _joined_haplotypes(reps.get(h["species"], []))

    unnamed = next((h for h in haps if not h["species"]), None)
    if unnamed:
        bins = {r.get("bin") for r in records
                if not (r.get("species") or r.get("taxon")) and r.get("bin")}
        unnamed = {"n": unnamed["n"], "haplotypes": unnamed["haplotypes"],
                   "hd": unnamed["hd"], "bins": len(bins)}
    heavy = [{"species": h["species"], "excluded": h["excluded"], "total": h["n"] + h["excluded"]}
             for h in haps
             if h["excluded"] >= 3 and h["excluded"] >= 0.2 * (h["n"] + h["excluded"])]
    largest = max((h for h in named if h["n"] >= 2), key=lambda h: h["n"], default=None)
    worst = max(haps, key=lambda h: h["excluded"], default=None)
    return {
        "species": species,
        "species_n": len(big),
        "few": [[h["species"], h["n"]] for h in few[:8]],
        "few_n": len(few),
        "single_n": sum(1 for h in named if h["n"] == 1),
        "unnamed": unnamed,
        "excluded": sum(h["excluded"] for h in haps),
        "excluded_most": ([worst["species"], worst["excluded"], worst["n"] + worst["excluded"]]
                          if worst and worst["excluded"] else None),
        "excluded_heavy": heavy[:GUIDE_LIST],
        "largest": largest["species"] if largest else None,
        # [name, sequences, Hd] of names that stop at the genus
        "genus_only": [[h["species"], h["n"], h["hd"]] for h in genus_only[:GUIDE_LIST]],
        "genus_only_n": len(genus_only),
        "min_n": GUIDE_HAP_MIN,
        "overlap_bp": GUIDE_HAP_OVERLAP,
    }


def concordance(records):
    """Species ↔ BIN concordance of the records that have both.

    A BIN holding more than one species, or a species spread over several
    BINs, is where an identification or the BIN itself deserves a look.
    Species are the settled names (species_key): a provisional one ("sp.",
    "cf.") is no species of its own, so it neither counts nor makes a BIN
    shared, though the BIN lists it.
    """
    bins_of = defaultdict(Counter)
    species_of = defaultdict(Counter)
    names_of = defaultdict(Counter)
    countries_of = defaultdict(set)
    n_species_only = n_bin_only = 0
    for rec in records:
        if len(rec["clean"]) < MIN_LENGTH:
            continue
        name, b = rec.get("species") or "", rec.get("bin") or ""
        sp = species_key(name)
        if sp and not no_place(rec.get("country")):
            countries_of[sp].add(rec["country"])
        if sp and b:
            bins_of[sp][b] += 1
        if name and b:
            names_of[b][name] += 1
            if sp:
                species_of[b][sp] += 1
        elif name:
            n_species_only += 1
        elif b:
            n_bin_only += 1

    def listing(counter):
        return [{"name": k, "n": v} for k, v in counter.most_common()]

    shared_bins = [{"bin": b, "n": sum(names_of[b].values()), "species": listing(names_of[b])}
                   for b, c in species_of.items() if len(c) > 1]
    split_species = [{"species": s, "n": sum(c.values()), "bins": listing(c)}
                     for s, c in bins_of.items() if len(c) > 1]
    shared_bins.sort(key=lambda x: -x["n"])
    split_species.sort(key=lambda x: -x["n"])

    return {
        "species": len(bins_of),
        "bins": len(names_of),
        "paired_records": sum(sum(c.values()) for c in names_of.values()),
        "species_only": n_species_only,
        "bin_only": n_bin_only,
        "concordant_species": sum(
            1 for s, c in bins_of.items()
            if len(c) == 1 and len(species_of[next(iter(c))]) == 1),
        "shared_bins": shared_bins,
        "split_species": split_species,
        "countries": {s: len(c) for s, c in countries_of.items()},
    }


_PROVISIONAL_WORDS = {"sp", "spp", "cf", "aff", "nr", "gr", "indet"}
# Country values that name no place: BOLD's "Unrecoverable", "Unspecified
# country" and "Exception - Culture", "Exception - Zoological Park"...
_NO_PLACE = ("unrecoverable", "unspecified", "exception", "?")


def no_place(country):
    """Whether a country value names no place (empty, or a placeholder)."""
    return not country or country.strip().lower().startswith(_NO_PLACE)


def _binomial(species):
    """Genus and epithet of a name ("Carollia brevicauda PS1" is Carollia
    brevicauda)."""
    words = species.split()
    return " ".join(words[:2]) if len(words) >= 2 else species


def _provisional(species):
    """A name that doesn't settle a species: "Aedes sp.", "Carollia cf.
    sowelli", "Carollia brevicauda PS1", or a hybrid ("Oncorhynchus mykiss
    x Salmo salar")."""
    words = species.split()
    return (len(words) < 2 or words[1].rstrip(".").lower() in _PROVISIONAL_WORDS
            or any(c.isdigit() or c == "?" for c in species) or (len(words) > 2 and words[2][:1].isupper())
            or any(w.rstrip(".").lower() in _PROVISIONAL_WORDS | {"x", "×", "complex"} for w in words[1:]))


def species_key(species):
    """The species a name settles, by genus and epithet ("Carollia
    perspicillata azteca" is Carollia perspicillata), or "" for a
    provisional one ("Myotis sp.", "Sturnira cf. ludovici"): what counts
    as a species wherever species are counted."""
    species = (species or "").strip()
    return "" if not species or _provisional(species) else _binomial(species)


def _species_bins(records):
    """{species: Counter(BIN)} and {(species, BIN): [records]} of the
    records with both."""
    bins, members = defaultdict(Counter), defaultdict(list)
    for rec in records:
        sp, b = rec.get("species") or "", rec.get("bin") or ""
        if sp and b and len(rec["clean"]) >= MIN_LENGTH:
            bins[sp][b] += 1
            members[(sp, b)].append(rec)
    return bins, members


def concordance_bins(records):
    """The BINs whose species the database should be asked for, most
    telling first: those outside a species' main BIN (where a misidentified
    record would sit), then the main ones (to tell a shared BIN)."""
    bins, _ = _species_bins(records)
    minor, main = Counter(), Counter()
    for counts in bins.values():
        (top, n), *rest = counts.most_common()
        main[top] += n
        for b, k in rest:
            minor[b] += k
    order = [b for b, _ in minor.most_common()] + [b for b, _ in main.most_common() if b not in minor]
    return order[:GUIDE_CONC_LOOKUP], len(order) > GUIDE_CONC_LOOKUP


def concordance_guide(records, conc, bin_species=None, capped=False):
    """Facts for the reading of the species & BINs view, which the page
    words.

    `bin_species` holds, for some BINs, the species the whole database files
    under them ({bin: {species: [[name, n]], total: n}}); other BINs are
    judged by these records alone.
    """
    bin_species = bin_species or {}
    bins, members = _species_bins(records)
    in_data = defaultdict(Counter)
    for sp, counts in bins.items():
        for b, n in counts.items():
            in_data[b][sp] = n

    def holders(b):
        """[[species, n]] of a BIN, its total and where they come from.
        Records analysed but missing from the database (a FASTA) count too:
        each species takes the larger of both counts."""
        if b in bin_species:
            counts = {sp: n for sp, n in bin_species[b]["species"]}
            extra = 0
            for sp, n in in_data[b].items():
                if n > counts.get(sp, 0):
                    extra += n - counts.get(sp, 0)
                    counts[sp] = n
            listed = sorted(([sp, n] for sp, n in counts.items()), key=lambda x: -x[1])
            return listed, bin_species[b]["total"] + extra, "db"
        listed = [[s, n] for s, n in in_data[b].most_common()]
        return listed, sum(n for _, n in listed), "data"

    def owner(b):
        listed, total, src = holders(b)
        named = [x for x in listed if not _provisional(x[0])] or listed
        return (named[0][0], named[0][1], total, src) if named else (None, 0, total, src)

    def countries(sp, b):
        # BOLD fills unknown places with "Unrecoverable"
        return [c for c, _ in Counter(r["country"] for r in members[(sp, b)]
                                      if not no_place(r.get("country"))).most_common(2)]

    def ids(sp, b, n):
        return [r.get("processid") or r.get("id") for r in members[(sp, b)]][:3] if n <= 3 else []

    strays, own_split, own_small, shared, provisional = [], [], [], {}, {}
    stray_bins, stray_pairs, overview = set(), set(), []
    for sp, counts in sorted(bins.items(), key=lambda x: (-sum(x[1].values()), x[0])):
        total = sum(counts.values())
        main = counts.most_common(1)[0][0]
        overview.append([sp, total, main, counts[main], len(counts)])
        if _provisional(sp):
            o = owner(main)[0]
            if o and _binomial(o) != _binomial(sp):
                provisional.setdefault(main, {"bin": main, "owner": o, "names": []})["names"].append([sp, counts[main]])
            continue
        items, own = [], []
        # A BIN of its own with several records, besides the main one
        owns_other = any(b != main and k >= GUIDE_CONC_OWN and _binomial(owner(b)[0] or sp) == _binomial(sp)
                         for b, k in counts.items())
        for b, n in counts.most_common():
            o, o_n, b_total, src = owner(b)
            entry = {"bin": b, "n": n, "countries": countries(sp, b),
                     "owned": not o or _binomial(o) == _binomial(sp)}
            # A record is out of place only in a BIN that another species
            # holds more than half of. A BIN of many species, none with half
            # of it (COI doesn't separate them), is theirs as well; so is the
            # main BIN of a species with no other BIN of its own when it
            # holds several of its records (the whole species sits there:
            # shared, not an error).
            mine = (not o or _binomial(o) == _binomial(sp) or o_n <= GUIDE_CONC_OWNER * b_total
                    or (b == main and (n >= max(3, GUIDE_CONC_SHARED * b_total)
                                       or (n >= GUIDE_CONC_OWN and not owns_other))))
            if mine:
                own.append(entry)
                if b == main:
                    listed, b_total, src = holders(b)
                    # Others sharing it: a tenth of the BIN, or any with a
                    # few records when this species doesn't hold half of it
                    least = max(3, GUIDE_CONC_SHARED * b_total) if n >= GUIDE_CONC_OWNER * b_total else 3
                    others = [x for x in listed if not _provisional(x[0]) and _binomial(x[0]) != _binomial(sp)
                              and x[1] >= least]
                    if others:
                        s = shared.setdefault(b, {"bin": b, "species": [], "others": others, "src": src,
                                                  "genera": set()})
                        s["species"].append([sp, n])
                        s["genera"].update(_binomial(x[0]).split()[0] for x in others + [[sp]])
            else:
                entry.update(owner=o, owner_n=o_n, src=src, ids=ids(sp, b, n),
                             other_genus=o.split()[0] != sp.split()[0])
                items.append(entry)
        if items:
            stray_bins.update(x["bin"] for x in items)
            stray_pairs.update((_binomial(sp), x["bin"]) for x in items)
            strays.append({"species": sp, "n": total, "records": sum(x["n"] for x in items),
                           "bins_n": len(items), "items": items[:GUIDE_LIST]})
        big = [x for x in own if x["n"] >= GUIDE_CONC_OWN]
        small = [x for x in own if x["n"] < GUIDE_CONC_OWN and x["bin"] != main and x["owned"]]
        if len(big) >= 2:
            own_split.append({"species": sp, "n": total, "bins_n": len(big), "bins": big[:GUIDE_CONC_SPECIES],
                              "small": [x["n"] for x in small], "top": round(own[0]["n"] / total, 3) if own else 0})
        elif small:
            own_small.append({"species": sp, "n": total, "bins_n": len(small), "bins": small[:GUIDE_LIST]})

    # Two species with records in each other's BIN, in one country
    reciprocal = []
    by_sp = {x["species"]: x for x in strays}
    for x in strays:
        for it in x["items"]:
            y = next((by_sp[s] for s in by_sp if _binomial(s) == _binomial(it["owner"])), None)
            if not y or x["species"] >= y["species"]:
                continue
            back = [j for j in y["items"] if _binomial(j["owner"]) == _binomial(x["species"])]
            places = set(it["countries"]) & {c for j in back for c in j["countries"]}
            if back and places and not any(r[:2] == [x["species"], y["species"]] for r in reciprocal):
                reciprocal.append([x["species"], y["species"], sorted(places)[0]])

    named_bins = {b for (sp, b) in members if not _provisional(sp)}
    bin_only = [r for r in records if r.get("bin") and not r.get("species") and len(r["clean"]) >= MIN_LENGTH]
    # One BIN of its own here, and not shared in the database either
    # Species whose records in a BIN are out of place don't share it as well
    for b in list(shared):
        s = shared[b]
        s["others"] = [x for x in s["others"] if (_binomial(x[0]), b) not in stray_pairs]
        if not s["others"] and len(s["species"]) < 2:
            del shared[b]
            continue
        s["genera"] = len({_binomial(x[0]).split()[0] for x in s["others"] + s["species"]})
    in_shared = {sp for s in shared.values() for sp, _ in s["species"]}
    elsewhere = {x["species"] for x in strays} | {sp for p in provisional.values() for sp, _ in p["names"]}
    # Species, as species_key: a provisional name is never concordant, nor
    # keeps the species of its BIN from being so
    concordant = sorted({species_key(sp) for sp, counts in bins.items()
                         if species_key(sp) and len(counts) == 1
                         and {species_key(s) for s in in_data[next(iter(counts))]} - {""} == {species_key(sp)}
                         and sp not in in_shared and sp not in elsewhere
                         and _binomial(owner(next(iter(counts)))[0] or sp) == _binomial(sp)})
    for s in shared.values():
        s["provisional"] = provisional.pop(s["bin"], {"names": []})["names"]
    return {
        "species_n": conc["species"],
        "paired": conc["paired_records"],
        # [species, records, main BIN, its records, BINs], most records first
        "overview": overview[:GUIDE_CONC_SPECIES],
        "concordant_n": len(concordant),
        "concordant": concordant[:GUIDE_CONC_SPECIES],
        "strays": strays[:GUIDE_CONC_SPECIES],
        "strays_n": len(strays),
        "stray_records": sum(x["records"] for x in strays),
        "stray_bins": len(stray_bins),
        "reciprocal": reciprocal[:3],
        "own_split": own_split[:GUIDE_CONC_SPECIES],
        "own_split_n": len(own_split),
        "own_small": own_small[:GUIDE_LIST],
        "shared": sorted(shared.values(), key=lambda s: -sum(n for _, n in s["species"]))[:GUIDE_CONC_SPECIES],
        "shared_n": len(shared),
        "provisional": list(provisional.values())[:GUIDE_LIST],
        "species_only": conc["species_only"],
        "bin_only": len(bin_only),
        "bin_only_named": sum(1 for r in bin_only if r["bin"] in named_bins),
        "looked_up": len(bin_species),
        "capped": capped,
        "min_own": GUIDE_CONC_OWN,
    }


def taxonomy_overview(records):
    """How many distinct taxa per rank the records hold. Species are the
    settled names by genus and epithet (a subspecies is its species); the
    provisional ones ("sp.", "cf.", "PS1"...) are counted apart."""
    ranks = ("kingdom", "phylum", "class", "order", "family", "genus", "bin")
    out = {r: len({rec.get(r) for rec in records if rec.get(r)}) for r in ranks}
    names = {rec.get("species") for rec in records if rec.get("species")}
    out["species"] = len({species_key(s) for s in names} - {""})
    # Taxa: the finest rank each record names, as the viewer's header counts them
    fine = ("species", "genus", "family", "order", "class", "phylum", "kingdom")
    out["taxa"] = len({next(((r, rec[r].strip()) for r in fine if (rec.get(r) or "").strip()), None)
                       for rec in records} - {None})
    out["species_provisional"] = sum(1 for s in names if not species_key(s))
    return out


def marker_counts(records):
    """[(marker, records)] of `records`, most common first. Records with no
    marker (a plain FASTA) count under "". Case doesn't matter ("atp6" and
    "ATP6" are one marker), as in the database's marker index."""
    counts, spelling = Counter(), {}
    for rec in records:
        m = (rec.get("marker") or "").strip()
        counts[m.upper()] += 1
        spelling.setdefault(m.upper(), m)
    return [(spelling[k], n) for k, n in counts.most_common()]


def same_marker(a, b):
    return (a or "").strip().upper() == (b or "").strip().upper()


# ── Reading guide ─────────────────────────────────────────────────────────────

_PROJECT_RE = re.compile(r"^([A-Z]{2,8})\d+-\d{2}$")


def _project(rec):
    """BOLD project code of a record's process ID ("GBAAY" of
    "GBAAY23973-24"), or ''."""
    m = _PROJECT_RE.match(rec.get("processid") or rec.get("id") or "")
    return m.group(1) if m else ""


def _better_code(pairs, forced_code):
    """Another genetic code that clears the stops of most of the sequences
    in `pairs` ((record, row) with stops), as {code, auto, cleared}, or None.
    When a code was forced, the automatic choice (the taxonomy's) wins if it
    clears enough: a code with fewer stop codons always clears a few more by
    chance."""
    used = {row["code"] for _, row in pairs}
    candidates = []
    if forced_code:
        candidates.append(("auto", genetic_code_for))
    candidates += [(c, lambda rec, c=c: c) for c in GENETIC_CODES if c not in used]
    best = None
    for code, code_of in candidates:
        codes = [code_of(rec) for rec, _ in pairs]
        auto = code == "auto"
        if auto:
            # The automatic choice helps only where it differs from the forced code
            if all(c in used for c in codes):
                continue
            code = Counter(codes).most_common(1)[0][0]
        cleared = sum(1 for (rec, _), c in zip(pairs, codes)
                      if c is not None and check_translation(rec["clean"], c)[2] == 0)
        if cleared >= GUIDE_CODE_CLEARS * len(pairs) and (not best or cleared > best["cleared"]):
            best = {"code": code, "auto": auto, "cleared": cleared}
            if auto:
                break
    return best


def guide(records, rows, forced_code=None):
    """Facts for the reading of the quality view, which the page words.

    `records` are those analysed (as many as `rows`, in the same order:
    those of at least MIN_LENGTH), with their `clean` sequence as read.
    """
    pairs = list(zip(records, rows))
    n = len(rows)
    if not n:
        return None
    item = lambda rec, row, **kw: {"id": row["id"], "species": row["species"],
                                   "length": row["length"], **kw}

    lengths = sorted(row["length"] for row in rows)
    median = statistics.median(lengths)
    top = Counter(lengths).most_common(2)
    two = None
    if (n >= 20 and len(top) == 2 and top[1][1] >= GUIDE_TWO_LENGTHS * n
            and abs(top[0][0] - top[1][0]) > HISTOGRAM_BIN_BP):
        two = [list(x) for x in top]
    long_rows = [row for row in rows if row["length"] > GUIDE_LONG * median]

    # Composition: sequences unlike the rest, by a robust spread (median
    # absolute deviation) with a floor, so a very uniform set doesn't flag
    # ordinary variation
    gcs = [row["gc"] for row in rows if row["gc"] is not None]
    gc = None
    if len(gcs) >= 5:
        gc_median = statistics.median(gcs)
        mad = statistics.median(abs(g - gc_median) for g in gcs)
        limit = max(GUIDE_GC_POINTS, GUIDE_GC_SD * 1.4826 * mad)
        out = sorted(((rec, row) for rec, row in pairs
                      if row["gc"] is not None and abs(row["gc"] - gc_median) > limit),
                     key=lambda p: -abs(p[1]["gc"] - gc_median))
        # Much longer sequences span another stretch of the gene, and much
        # shorter ones only part of it (a short stretch has a composition of
        # its own): apart, as they aren't suspect
        odd = lambda row: row["length"] > GUIDE_LONG * median or row["length"] < median / GUIDE_LONG
        apart = [p for p in out if odd(p[1])]
        out = [p for p in out if not odd(p[1])]
        # So many apart that they are a group of their own (another species
        # or lineage with its own composition), not contamination
        group = len(out) >= max(GUIDE_GC_GROUP_MIN, GUIDE_GC_GROUP * n)
        gc = {"median": round(gc_median, 1), "limit": round(limit, 1),
              "outliers_n": len(out),
              "group": group,
              "group_species": [list(x) for x in Counter(
                  row["species"] for _, row in out if row["species"]).most_common(3)] if group else [],
              "outliers": [item(rec, row, gc=row["gc"]) for rec, row in out[:GUIDE_LIST]],
              "apart_n": len(apart),
              "apart_short": sum(1 for _, row in apart if row["length"] < median),
              "apart": [item(rec, row, gc=row["gc"]) for rec, row in apart[:GUIDE_LIST]]}

    # Stop codons: where they come from, and whether another code clears them
    stops = [(rec, row) for rec, row in pairs if row["stops"]]
    translated = sum(1 for row in rows if row["stops"] is not None)
    projects = Counter(p for p in (_project(rec) for rec, _ in stops) if p)
    species = Counter(row["species"] for _, row in stops if row["species"])
    better = None
    if len(stops) >= 3 and len(stops) >= GUIDE_CODE_SHARE * translated:
        better = _better_code(stops, forced_code)
    # Sequences free of stops that end on one in their frame (not counted)
    terminal = []
    for rec, row in pairs:
        if row["code"] is None or row["stops"]:
            continue
        frame = abs(row["frame"]) - 1
        i = _last_codon(rec["clean"], frame)
        if i >= frame and rec["clean"][i:i + 3] in _STOPS[row["code"]]:
            terminal.append((rec, row))
    # Coding sequences translated with the default code for want of taxonomy
    default_code = 0
    if not forced_code:
        default_code = sum(
            1 for rec, row in pairs
            if row["code"] == DEFAULT_MITO_CODE and marker_kind(rec.get("marker")) == "mito"
            and not any((rec.get(k) or "").strip() for k in ("class", "phylum", "kingdom")))

    reversed_ids = [row["id"] for row in rows if row["reversed"]]
    return {
        "n": n,
        "length": {"median": median, "min": lengths[0], "max": lengths[-1],
                   "q05": lengths[int(.05 * (n - 1))], "q95": lengths[int(.95 * (n - 1))],
                   "two": two, "long_n": len(long_rows),
                   "long_max": lengths[-1] if long_rows else None},
        "gc": gc,
        "stops_n": len(stops),
        "stops": [item(rec, row, stops=row["stops"])
                  for rec, row in sorted(stops, key=lambda p: -p[1]["stops"])[:GUIDE_LIST]],
        "stops_project": list(projects.most_common(1)[0]) if projects else None,
        "stops_species": [list(x) for x in species.most_common(3)],
        "better_code": better,
        "forced_code": forced_code,
        "terminal_n": len(terminal),
        "terminal": [item(rec, row) for rec, row in terminal[:GUIDE_LIST]],
        "default_code": default_code,
        "reversed_n": len(reversed_ids),
        "reversed": reversed_ids[:GUIDE_LIST],
        # Left out of the haplotypes: any N or ambiguity
        "with_n": sum(1 for rec, _ in pairs if set(rec["clean"]) - set("ACGT")),
        "species_n": len({species_key(row["species"]) for row in rows} - {""}),
    }


def run_basic(records, marker, forced_code=None, total=None, bin_lookup=None):
    """Every phase-1 analysis over `records`, all of them of `marker`
    ("" when unknown), ready to send as JSON.

    `bin_lookup`, given a list of BINs, returns the species the database
    files under each ({bin: {species: [[name, n]], total: n}}).
    """
    for rec in records:
        rec["marker"] = marker
        rec["clean"] = clean_sequence(rec.get("seq"))
    rows, skipped = composition(records, forced_code)
    usable = [r for r in records if len(r["clean"]) >= MIN_LENGTH]
    reps = {}
    haps = haplotypes(usable, reps)
    conc = concordance(usable)
    lookup, capped = concordance_bins(usable)
    bin_species = bin_lookup(lookup) if bin_lookup and lookup else {}
    return {
        "marker": marker,
        "summary": _summary(rows, skipped, total if total is not None else len(records)),
        "sequences": rows,
        "guide": guide(usable, rows, forced_code),
        "haplotypes": haps,
        "haplotype_guide": haplotype_guide(usable, haps, reps),
        "concordance": conc,
        "concordance_guide": concordance_guide(usable, conc, bin_species, capped),
        "taxonomy": taxonomy_overview(usable),
    }
