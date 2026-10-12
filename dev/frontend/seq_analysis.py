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


def _count_stops(seq, frame, stops):
    n = 0
    for i in range(frame, len(seq) - 2, 3):
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
    Codons with N or ambiguities never count as stops.
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
    return f"{words[0]} {second}" if _EPITHET_RE.match(second) else None


def parse_header(header):
    """Fields of a FASTA header.

    Understands the viewer's own export (processid|sampleid|BIN|taxon|marker),
    BOLD's (processid|taxon|marker|accession), an identifier followed by the
    name (">MN123456.1 Carollia perspicillata voucher...", as GenBank and
    most sources write it) and a name joined to the identifier
    (">Carollia_perspicillata_ABC123"). Anything else keeps its first word
    as the identifier.
    """
    header = header.strip()
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
    """How many records bring a species name, a BIN and a country: what a
    FASTA's headers gave."""
    return {"n": len(records),
            "species": sum(1 for r in records if r.get("species")),
            "bin": sum(1 for r in records if r.get("bin")),
            "country": sum(1 for r in records if r.get("country"))}


def parse_fasta(text):
    """Records of a FASTA text, in file order."""
    records = []
    header, chunks = None, []

    def flush():
        if header is not None:
            rec = parse_header(header)
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


def haplotypes(records):
    """Haplotypes per species, comparing sequences without alignment.

    Sequences with Ns or ambiguities are left out (one N would make a new
    haplotype). A sequence contained in a longer one counts as that same
    haplotype, since barcodes of one haplotype differ mostly in where they
    were trimmed; it joins the longest one that contains it.
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


def concordance(records):
    """Species ↔ BIN concordance of the records that have both.

    A BIN holding more than one species, or a species spread over several
    BINs, is where an identification or the BIN itself deserves a look.
    """
    bins_of = defaultdict(Counter)
    species_of = defaultdict(Counter)
    countries_of = defaultdict(set)
    n_species_only = n_bin_only = 0
    for rec in records:
        if len(rec["clean"]) < MIN_LENGTH:
            continue
        sp, b = rec.get("species") or "", rec.get("bin") or ""
        if sp and rec.get("country"):
            countries_of[sp].add(rec["country"])
        if sp and b:
            bins_of[sp][b] += 1
            species_of[b][sp] += 1
        elif sp:
            n_species_only += 1
        elif b:
            n_bin_only += 1

    def listing(counter):
        return [{"name": k, "n": v} for k, v in counter.most_common()]

    shared_bins = [{"bin": b, "n": sum(c.values()), "species": listing(c)}
                   for b, c in species_of.items() if len(c) > 1]
    split_species = [{"species": s, "n": sum(c.values()), "bins": listing(c)}
                     for s, c in bins_of.items() if len(c) > 1]
    shared_bins.sort(key=lambda x: -x["n"])
    split_species.sort(key=lambda x: -x["n"])

    return {
        "species": len(bins_of),
        "bins": len(species_of),
        "paired_records": sum(sum(c.values()) for c in bins_of.values()),
        "species_only": n_species_only,
        "bin_only": n_bin_only,
        "concordant_species": sum(
            1 for s, c in bins_of.items()
            if len(c) == 1 and len(species_of[next(iter(c))]) == 1),
        "shared_bins": shared_bins,
        "split_species": split_species,
        "countries": {s: len(c) for s, c in countries_of.items()},
    }


def taxonomy_overview(records):
    """How many distinct taxa per rank the records hold."""
    ranks = ("kingdom", "phylum", "class", "order", "family", "genus", "species", "bin")
    return {r: len({rec.get(r) for rec in records if rec.get(r)}) for r in ranks}


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


def run_basic(records, marker, forced_code=None, total=None):
    """Every phase-1 analysis over `records`, all of them of `marker`
    ("" when unknown), ready to send as JSON."""
    for rec in records:
        rec["marker"] = marker
        rec["clean"] = clean_sequence(rec.get("seq"))
    rows, skipped = composition(records, forced_code)
    usable = [r for r in records if len(r["clean"]) >= MIN_LENGTH]
    return {
        "marker": marker,
        "summary": _summary(rows, skipped, total if total is not None else len(records)),
        "sequences": rows,
        "haplotypes": haplotypes(usable),
        "concordance": concordance(usable),
        "taxonomy": taxonomy_overview(usable),
    }
