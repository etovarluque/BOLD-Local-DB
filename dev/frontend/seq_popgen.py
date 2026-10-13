"""Genetic diversity within species and its geographic structure: part of
the viewer's "Sequence analysis" tab (Haplotypes).

Each species is aligned on its own, as for the haplotype network, and
compared over the columns all its sequences cover with an A, C, G or T.
Over those columns:

- Nucleotide diversity (pi), segregating sites and Tajima's D (Tajima
  1989), as the network computes them.
- Structure between zones (countries): PhiST from an AMOVA on the pairwise
  differences (Excoffier, Smouse & Quattro 1992), with a permutation test
  that shuffles the sequences among zones.
- Isolation by distance: a Mantel test (Mantel 1967) between the genetic
  distances (proportion of differing sites) and the great-circle distances
  of the sequences with coordinates, permuting the sequences of one matrix.

Sequences without a species name are taken as one group, and bare
"Genus sp." names as theirs: their diversity mixes species, so they get neither
Tajima's D nor a structure. Other provisional names ("cf.", "aff.",
lineage codes) usually stand for one lineage and are read as species.
"""

import math
import random
import re
import time
from collections import Counter, defaultdict

import numpy as np

import seq_analysis
import seq_distance
import seq_network

# Most sequences per species (a fixed random sample of larger ones), and
# fewest to measure diversity
MAX_SPECIES_SEQUENCES = 300
MIN_SPECIES_SEQUENCES = 2
# Structure: zones with fewer sequences of the species are left out, and it
# takes two zones; Mantel takes this many sequences with coordinates at
# this many distinct places (a fixed sample of MAX_MANTEL_SEQUENCES at most,
# to keep the permutations quick), and plots at most MANTEL_MAX_POINTS pairs
MIN_ZONE_SEQUENCES = 5
MIN_MANTEL_SEQUENCES = 10
MIN_MANTEL_PLACES = 3
MAX_MANTEL_SEQUENCES = 150
MANTEL_MAX_POINTS = 1500
PERMUTATIONS = 999
EARTH_KM = 6371.0

# Reading guide. Species are read with GUIDE_MIN_N aligned sequences; pi is
# high when it doubles the median of the others and passes GUIDE_HIGH_PI (or
# GUIDE_HIGH_PI_ALONE with fewer than three species to compare). PhiST is
# strong from GUIDE_STRONG_PHI, moderate from GUIDE_MODERATE_PHI, and
# GUIDE_SPLIT_PHI without isolation by distance suggests separate groups.
# Zones under GUIDE_SMALL_ZONE sequences make PhiST uncertain.
GUIDE_MIN_N = 10
GUIDE_HIGH_PI = 0.01
GUIDE_HIGH_PI_ALONE = 0.02
GUIDE_STRONG_PHI = 0.25
GUIDE_MODERATE_PHI = 0.05
GUIDE_SPLIT_PHI = 0.5
GUIDE_SMALL_ZONE = 10
ALPHA = 0.05

_COORD_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*[,;\s]\s*(-?\d+(?:\.\d+)?)")


def parse_coord(value):
    """(lat, lon) of a coordinates field ("[-0.65, -76.45]"), or None."""
    m = _COORD_RE.search(value or "")
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def coord_of(rec):
    """(lat, lon) of a record: its coordinates field, or separate latitude
    and longitude (as the LGC database keeps them)."""
    if rec.get("lat") not in (None, "") and rec.get("lon") not in (None, ""):
        try:
            return parse_coord(f"{float(rec['lat'])}, {float(rec['lon'])}")
        except (TypeError, ValueError):
            return None
    return parse_coord(rec.get("coord"))


def mixed_name(species):
    """A name that may lump several species: none, the genus alone, or a
    bare "Genus sp.", "spp." or "indet." ("Astyanax sp. 2 RSS-2016" names
    one lineage)."""
    words = (species or "").split()
    return len(words) < 2 or (len(words) == 2 and words[1].rstrip(".").lower() in ("sp", "spp", "indet"))


def _zone(rec):
    country = (rec.get("country") or "").strip()
    return "" if seq_analysis.no_place(country) else country


def _align(records):
    """Codes over the columns every kept sequence covers, and the records
    kept (None when they don't align or share too few columns)."""
    groups, _ = seq_distance.align_groups([r["clean"] for r in records], min_size=2)
    if not groups:
        return None, None
    g = max(groups, key=lambda g: len(g["members"]))
    recs = [records[i] for i, _ in g["members"]]
    codes = seq_distance.encode([a["row"] for _, a in g["members"]])
    keep, core = seq_network.core_columns(codes)
    codes = codes[keep][:, core]
    recs = [r for r, k in zip(recs, keep) if k]
    if len(recs) < MIN_SPECIES_SEQUENCES or codes.shape[1] < seq_network.MIN_CORE_SITES:
        return None, None
    return codes, recs


def _within_sums(d, onehot):
    """Sum of the differences within each zone, for every column block of
    `onehot` (n × zones·k)."""
    return (onehot * (d @ onehot)).sum(axis=0)


def phi_st(d, labels, permutations=PERMUTATIONS, rng=None):
    """AMOVA PhiST between the zones in `labels` (ints 0..P-1) from the
    pairwise differences `d`, and its permutation p-value."""
    n = len(labels)
    P = int(labels.max()) + 1
    sizes = np.bincount(labels, minlength=P).astype(float)
    total = d.sum() / 2 / n
    df_ap, df_wp = P - 1, n - P
    n0 = (n - (sizes ** 2).sum() / n) / df_ap
    d = d.astype(float)

    def phi(within):
        ssd_wp = (within / 2 / sizes).sum()
        msd_wp = ssd_wp / df_wp
        sigma_a = ((total - ssd_wp) / df_ap - msd_wp) / n0
        denom = sigma_a + msd_wp
        return sigma_a / denom if denom > 0 else 0.0

    eye = np.eye(P)
    observed = phi(_within_sums(d, eye[labels]))
    rng = rng or np.random.default_rng(0)
    hits, done, chunk = 0, 0, max(1, 4000 // P)
    while done < permutations:
        k = min(chunk, permutations - done)
        perms = np.stack([eye[rng.permutation(labels)] for _ in range(k)], axis=1)  # n × k × P
        within = _within_sums(d, perms.reshape(n, k * P)).reshape(k, P)
        for w in within:
            if phi(w) >= observed - 1e-12:
                hits += 1
        done += k
    return float(observed), (hits + 1) / (permutations + 1)


def _km(coords):
    """Great-circle distances (km) between every two (lat, lon)."""
    lat, lon = np.radians(coords[:, 0]), np.radians(coords[:, 1])
    dlat = lat[:, None] - lat[None, :]
    dlon = lon[:, None] - lon[None, :]
    a = np.sin(dlat / 2) ** 2 + np.cos(lat)[:, None] * np.cos(lat)[None, :] * np.sin(dlon / 2) ** 2
    return 2 * EARTH_KM * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def mantel(gen, geo, permutations=PERMUTATIONS, rng=None):
    """Pearson r between two distance matrices (upper triangles) and its
    one-sided permutation p-value (positive association)."""
    n = gen.shape[0]
    iu = np.triu_indices(n, 1)
    x = gen[iu] - gen[iu].mean()
    sx = math.sqrt(float((x ** 2).sum()))
    y0 = geo[iu]
    if sx == 0 or y0.std() == 0:
        return None, None

    def r_of(y):
        y = y - y.mean()
        sy = math.sqrt(float((y ** 2).sum()))
        return float((x * y).sum() / (sx * sy)) if sy else 0.0

    observed = r_of(y0)
    rng = rng or np.random.default_rng(1)
    # Permuting the sequences of one matrix: r against the other's pairs
    # in the new order, a block of permutations at a time
    i, j = iu
    hits, done, chunk = 0, 0, max(1, 2_000_000 // len(x))
    while done < permutations:
        k = min(chunk, permutations - done)
        perms = np.stack([rng.permutation(n) for _ in range(k)])
        y = geo[perms[:, i], perms[:, j]]
        y = y - y.mean(axis=1, keepdims=True)
        sy = np.sqrt((y ** 2).sum(axis=1))
        r = (y @ x) / (sx * np.where(sy > 0, sy, 1))
        hits += int((r >= observed - 1e-12).sum())
        done += k
    return observed, (hits + 1) / (permutations + 1)


def _structure(species, codes, recs, d):
    """Structure between zones and isolation by distance of one species, or
    None when it has fewer than two zones with enough sequences."""
    zones = [_zone(r) for r in recs]
    counts = Counter(z for z in zones if z)
    kept = sorted((z for z, c in counts.items() if c >= MIN_ZONE_SEQUENCES), key=lambda z: (-counts[z], z))
    out = {"species": species, "n": len(recs), "zones": [], "phi_st": None, "phi_p": None,
           "left_out": 0, "mantel": None}
    if len(kept) >= 2:
        index = {z: i for i, z in enumerate(kept)}
        rows = [i for i, z in enumerate(zones) if z in index]
        labels = np.array([index[zones[i]] for i in rows])
        sub = d[np.ix_(rows, rows)]
        out["phi_st"], out["phi_p"] = phi_st(sub, labels)
        out["left_out"] = len(recs) - len(rows)
        sites = codes.shape[1]
        for z in kept:
            idx = [i for i in rows if zones[i] == z]
            m = d[np.ix_(idx, idx)]
            k = len(idx)
            out["zones"].append({"zone": z, "n": k,
                                 "pi": float(m.sum() / (k * (k - 1)) / sites) if k > 1 else 0.0})

    coords = [coord_of(r) for r in recs]
    idx = [i for i, c in enumerate(coords) if c]
    places = {coords[i] for i in idx}
    if len(idx) >= MIN_MANTEL_SEQUENCES and len(places) >= MIN_MANTEL_PLACES:
        with_coords = len(idx)
        if len(idx) > MAX_MANTEL_SEQUENCES:
            idx = sorted(random.Random(0).sample(idx, MAX_MANTEL_SEQUENCES))
        xy = np.array([coords[i] for i in idx])
        geo = _km(xy)
        gen = d[np.ix_(idx, idx)] / codes.shape[1]
        r, p = mantel(gen, geo)
        if r is not None:
            iu = np.triu_indices(len(idx), 1)
            pairs = list(zip(geo[iu].round(1).tolist(), (100 * gen[iu]).round(3).tolist()))
            if len(pairs) > MANTEL_MAX_POINTS:
                pairs = random.Random(0).sample(pairs, MANTEL_MAX_POINTS)
            out["mantel"] = {"n": len(idx), "with_coords": with_coords, "places": len(places), "r": r, "p": p,
                             "max_km": float(geo.max()), "pairs": pairs}
    if out["phi_st"] is None and out["mantel"] is None:
        return None
    return out


def guide(species, structure):
    """Facts for the readings of the haplotype table (diversity, Tajima's D)
    and of the geographic structure; the page words them."""
    read = [x for x in species if x["species"] and not x["mixed"] and x["used"] >= GUIDE_MIN_N]
    pis = sorted(x["pi"] for x in read)
    median = float(np.median(pis)) if pis else None
    if len(read) >= 3:
        high = [x for x in read if x["pi"] >= max(2 * median, GUIDE_HIGH_PI)]
    else:
        high = [x for x in read if x["pi"] >= GUIDE_HIGH_PI_ALONE]
    high.sort(key=lambda x: -x["pi"])
    sig = [x for x in read if x["tajima_p"] is not None and x["tajima_p"] < ALPHA]
    row = lambda x: {"species": x["species"], "n": x["used"], "pi": x["pi"], "d": x["tajima_d"],
                     "p": x["tajima_p"], "zones": x["zones"]}
    unnamed = next((x for x in species if not x["species"] and x["used"] >= GUIDE_MIN_N), None)
    mixed = [x for x in species if x["species"] and x["mixed"] and x["used"] >= GUIDE_MIN_N]
    high_names = {x["species"] for x in high}

    tested = [s for s in structure if s["phi_st"] is not None]
    rows = []
    for s in tested:
        m = s["mantel"]
        ibd = bool(m and m["p"] < ALPHA and m["r"] > 0)
        sig_phi = s["phi_p"] < ALPHA
        phi = s["phi_st"]
        kind = ("flow" if not sig_phi or phi < GUIDE_MODERATE_PHI
                else "moderate" if phi < GUIDE_STRONG_PHI
                else "gradient" if ibd else "split")
        rows.append({"species": s["species"], "n": s["n"], "phi": phi, "phi_p": s["phi_p"], "kind": kind,
                     "very": phi >= GUIDE_SPLIT_PHI, "ibd": ibd,
                     "r": m["r"] if m else None, "mantel_p": m["p"] if m else None,
                     "km": m["max_km"] if m else None,
                     "zones": [[z["zone"], z["n"]] for z in s["zones"]],
                     "small": [z["zone"] for z in s["zones"] if z["n"] < GUIDE_SMALL_ZONE],
                     "high_pi": s["species"] in high_names})
    mantel_only = [{"species": s["species"], "n": s["mantel"]["n"], "r": s["mantel"]["r"],
                    "p": s["mantel"]["p"], "km": s["mantel"]["max_km"]}
                   for s in structure if s["phi_st"] is None and s["mantel"]]
    return {
        "min_n": GUIDE_MIN_N, "read_n": len(read), "median_pi": median,
        "high_pi": [row(x) for x in high], "alone": len(read) < 3,
        "single": row(read[0]) if len(read) == 1 else None,
        "tajima_neg": [row(x) for x in sorted(sig, key=lambda x: x["tajima_d"]) if x["tajima_d"] < 0],
        "tajima_pos": [row(x) for x in sorted(sig, key=lambda x: -x["tajima_d"]) if x["tajima_d"] > 0],
        "tajima_n": sum(1 for x in read if x["tajima_d"] is not None),
        "unnamed": {"n": unnamed["used"], "pi": unnamed["pi"]} if unnamed else None,
        "mixed": [[x["species"], x["used"], x["pi"]] for x in mixed],
        "struct": rows, "mantel_only": mantel_only,
        "min_zone": MIN_ZONE_SEQUENCES, "small_zone": GUIDE_SMALL_ZONE,
        "strong_phi": GUIDE_STRONG_PHI, "moderate_phi": GUIDE_MODERATE_PHI,
    }


def run_popgen(records, marker, forced_code=None):
    """Diversity of each species and its structure between zones, for the
    Haplotypes tab."""
    start = time.time()
    for rec in records:
        rec["marker"] = marker
        rec["clean"] = seq_analysis.clean_sequence(rec.get("seq"))
    usable, excluded = seq_distance.usable_records(records, forced_code)
    groups = defaultdict(list)
    for rec in usable:
        # As the haplotype table names them (a FASTA's taxon field included)
        groups[rec.get("species") or rec.get("taxon") or ""].append(rec)

    rng = random.Random(0)
    species, structure = [], []
    for name in sorted(groups, key=lambda s: (s == "", s)):
        recs = groups[name]
        if len(recs) < MIN_SPECIES_SEQUENCES:
            continue
        sampled = len(recs) > MAX_SPECIES_SEQUENCES
        if sampled:
            keep = sorted(rng.sample(range(len(recs)), MAX_SPECIES_SEQUENCES))
            recs = [recs[i] for i in keep]
        codes, kept = _align(recs)
        if codes is None:
            continue
        of_row, _ = seq_network.haplotypes(codes)
        stats = seq_network.diversity(codes, of_row)
        row = {"species": name, "n": len(groups[name]), "used": len(kept), "sampled": sampled,
               "sites": int(codes.shape[1]), "pi": stats["pi"], "k": stats["k"],
               "segregating": stats["segregating"],
               "mixed": mixed_name(name),
               "tajima_d": None if mixed_name(name) else stats["tajima_d"],
               "tajima_p": None if mixed_name(name) else stats["tajima_p"],
               "zones": len({z for z in map(_zone, kept) if z})}
        species.append(row)
        if not mixed_name(name):
            s = _structure(name, codes, kept, seq_network.hamming(codes))
            if s:
                structure.append(s)
    structure.sort(key=lambda s: (s["phi_st"] is None, -(s["phi_st"] or 0), s["species"]))
    return {"marker": marker, "zone": "country", "species": species, "structure": structure,
            "guide": guide(species, structure),
            "excluded": {k: v for k, v in excluded.items() if v},
            "params": {"max_species": MAX_SPECIES_SEQUENCES, "min_zone": MIN_ZONE_SEQUENCES,
                       "min_mantel": MIN_MANTEL_SEQUENCES, "permutations": PERMUTATIONS},
            "seconds": round(time.time() - start, 2)}
