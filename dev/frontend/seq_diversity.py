"""Diversity by zone: part of the viewer's "Sequence analysis" tab
(Diversity).

Each sequence counts once for its unit (a BIN by default, or a species
name) in its zone (country, province, region, or a 1-degree grid cell from
the coordinates). Counts are barcoding effort, not field abundance, so
only richness and composition are read from them:

- Richness and completeness per zone: observed units, singletons and
  doubletons, Chao1 with its log-normal 95 % interval (Chao 1987), sample
  coverage (Chao & Jost 2012) and the individual-based rarefaction and
  extrapolation curve (Chao et al. 2014), with every zone rarefied to the
  smallest one for a comparison at equal effort.
- Taxonomic distinctness per zone (Clarke & Warwick 1998): the average
  taxonomic distance between two units (delta+) and its variance (lambda+),
  along the genus, family, order, class and phylum of each unit, compared
  with the values of random sets of the same richness taken from all the
  units of the analysis (the funnel). Skipped when every unit shares a genus.
- Between zones (presence/absence): Jaccard and Sorensen similarity and Baselga's
  (2010) split of Sorensen dissimilarity into turnover (beta-sim) and
  nestedness (beta-sne), per pair and over all zones; units found in one
  zone only and the most widespread ones.

Zones with fewer than MIN_ZONE_SEQUENCES sequences are left out.
"""

import math
import time
from collections import Counter, defaultdict

import numpy as np

import seq_analysis
import seq_popgen

MIN_ZONE_SEQUENCES = 20
CURVE_POINTS = 40
# Extrapolation reaches this multiple of a zone's sequences
EXTRAPOLATE = 2
MAX_LIST = 40
ZONINGS = ("country", "province", "region", "grid")
UNITS = ("bin", "species")

# Reading guide: a zone is nearly complete from GUIDE_COMPLETE coverage,
# poorly sampled under GUIDE_POOR;
# nestedness dominates a pair from GUIDE_NESTED of its dissimilarity;
# zones rarefied within GUIDE_SAME_RICHNESS units of each other are alike.
GUIDE_COMPLETE = 0.95
GUIDE_POOR = 0.8
GUIDE_NESTED = 0.5
GUIDE_FEW_NAMES = 0.5

# Taxonomic distinctness: ranks above the unit, nearest first; random sets
# drawn per funnel size, and the sizes; a fixed seed so a rerun is identical
RANKS = ("genus", "family", "order", "class", "phylum")
FUNNEL_DRAWS = 500
FUNNEL_SIZES = 30
FUNNEL_SEED = 20240613
MIN_DISTINCT_UNITS = 3
GUIDE_SAME_RICHNESS = 0.5


def zone_of(rec, zoning):
    if zoning == "grid":
        c = seq_popgen.coord_of(rec)
        if not c:
            return ""
        lat, lon = math.floor(c[0]), math.floor(c[1])
        ns, ew = ("N" if lat >= 0 else "S"), ("E" if lon >= 0 else "W")
        return f"{abs(lat)}°{ns} {abs(lon)}°{ew}"
    value = (rec.get(zoning) or "").strip()
    if zoning == "country" and seq_analysis.no_place(value):
        return ""
    if zoning != "country" and value:
        # Provinces and regions repeat across countries
        country = (rec.get("country") or "").strip()
        if country and not seq_analysis.no_place(country):
            value = f"{value} ({country})"
    return value


def _unit(rec, unit):
    if unit == "bin":
        return (rec.get("bin") or "").strip()
    # Settled species only, by genus and epithet, as counted elsewhere
    return seq_analysis.species_key(rec.get("species"))


def chao1(counts):
    """Bias-corrected Chao1 and its log-normal 95 % interval."""
    n = sum(counts)
    s = len(counts)
    f1 = sum(1 for c in counts if c == 1)
    f2 = sum(1 for c in counts if c == 2)
    k = (n - 1) / n if n else 0
    est = s + k * f1 * (f1 - 1) / (2 * (f2 + 1))
    # Variance of the bias-corrected form (Chao 2005; EstimateS, SpadeR)
    if f2 > 0:
        var = (k * f1 * (f1 - 1) / (2 * (f2 + 1))
               + k ** 2 * f1 * (2 * f1 - 1) ** 2 / (4 * (f2 + 1) ** 2)
               + k ** 2 * f1 ** 2 * f2 * (f1 - 1) ** 2 / (4 * (f2 + 1) ** 4))
    else:
        var = k * f1 * (f1 - 1) / 2 + k ** 2 * f1 * (2 * f1 - 1) ** 2 / 4 - k ** 2 * f1 ** 4 / (4 * est) if est else 0
    t = est - s
    if t <= 0 or var <= 0:
        return est, s, est
    c = math.exp(1.96 * math.sqrt(math.log(1 + var / t ** 2)))
    return est, s + t / c, s + t * c


def coverage(counts):
    """Sample coverage: the share of the zone's sequences that belong to
    units already seen (Chao & Jost 2012)."""
    n = sum(counts)
    f1 = sum(1 for c in counts if c == 1)
    f2 = sum(1 for c in counts if c == 2)
    if n < 2:
        return 0.0
    if f2 > 0:
        a = (n - 1) * f1 / ((n - 1) * f1 + 2 * f2)
    else:
        # A single singleton and no doubleton: no unit left unseen (f0 = 0)
        a = (n - 1) * (f1 - 1) / ((n - 1) * (f1 - 1) + 2) if f1 > 1 else 0.0
    return max(0.0, 1 - f1 / n * a)


def _log_choose(a, b):
    return math.lgamma(a + 1) - math.lgamma(b + 1) - math.lgamma(a - b + 1)


def rarefy(counts, m):
    """Expected units in a random subsample of m sequences (Hurlbert 1971)."""
    n = sum(counts)
    if m >= n:
        return float(len(counts))
    total = 0.0
    for c in counts:
        if n - c >= m:
            total += 1 - math.exp(_log_choose(n - c, m) - _log_choose(n, m))
        else:
            total += 1
    return total


def extrapolate(counts, m):
    """Expected units with m > n sequences (Chao et al. 2014)."""
    n = sum(counts)
    s = len(counts)
    f1 = sum(1 for c in counts if c == 1)
    f2 = sum(1 for c in counts if c == 2)
    f0 = (n - 1) / n * f1 * (f1 - 1) / 2 if f2 == 0 else (n - 1) / n * f1 ** 2 / (2 * f2)
    if f0 <= 0:
        return float(s)
    return s + f0 * (1 - (1 - f1 / (n * f0 + f1)) ** (m - n))


def curve(counts):
    """Rarefaction up to n and extrapolation up to EXTRAPOLATE·n:
    [[m, units, extrapolated]]."""
    n = sum(counts)
    top = EXTRAPOLATE * n
    steps = sorted({max(1, round(top * i / CURVE_POINTS)) for i in range(1, CURVE_POINTS + 1)} | {n})
    return [[m, round(rarefy(counts, m) if m <= n else extrapolate(counts, m), 2), m > n] for m in steps]


def beta_pair(a_set, b_set):
    """Sorensen similarity and Baselga's turnover / nestedness of a pair."""
    a = len(a_set & b_set)
    b = len(a_set - b_set)
    c = len(b_set - a_set)
    sor = (b + c) / (2 * a + b + c) if (a + b + c) else 0.0
    sim = min(b, c) / (a + min(b, c)) if (a + min(b, c)) else 0.0
    union = a + b + c
    return {"shared": a, "only_a": b, "only_b": c, "sorensen": 1 - sor,
            "jaccard": a / union if union else 1.0,
            "beta_sor": sor, "beta_sim": sim, "beta_sne": sor - sim}


def beta_multi(sets):
    """Baselga's multiple-site Sorensen dissimilarity and its split."""
    total = set().union(*sets)
    st = len(total)
    si = sum(len(s) for s in sets)
    mins = maxs = 0
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            b = len(sets[i] - sets[j])
            c = len(sets[j] - sets[i])
            mins += min(b, c)
            maxs += max(b, c)
    x = si - st
    sor = (mins + maxs) / (2 * x + mins + maxs) if (x + mins + maxs) else 0.0
    sim = mins / (x + mins) if (x + mins) else 0.0
    return {"beta_sor": sor, "beta_sim": sim, "beta_sne": sor - sim}


def _lineage(rec):
    """Genus up to phylum of a record; "" for a missing rank."""
    genus = (rec.get("genus") or "").strip()
    if not genus and rec.get("species"):
        genus = rec["species"].split()[0]
    return tuple([genus] + [(rec.get(r) or "").strip() for r in RANKS[1:]])


def _distances(codes):
    """Pairs of units at each taxonomic distance: 1 = same genus, up to
    len(RANKS) + 1 = no rank in common. codes has one row per unit and one
    column per rank (-1 = unknown, matches nothing; distinctness fills
    missing ranks, so it passes none)."""
    s = len(codes)
    shared = []
    for k in range(codes.shape[1]):
        col = codes[:, k]
        col = col[col >= 0]
        counts = np.bincount(col) if len(col) else np.zeros(1, dtype=np.int64)
        shared.append(int((counts * (counts - 1) // 2).sum()))
    # Pairs sharing a rank also share the ones above; inconsistent
    # taxonomies (one genus under two families) can break that, so keep the
    # cumulative counts non-decreasing
    for k in range(1, len(shared)):
        shared[k] = max(shared[k], shared[k - 1])
    out, prev = [], 0
    for v in shared:
        out.append(v - prev)
        prev = v
    out.append(s * (s - 1) // 2 - prev)
    return out


def _delta(dist):
    """Delta+ and lambda+ from the pairs per distance, scaled so the
    largest distance weighs 100."""
    total = sum(dist)
    if not total:
        return None, None
    w = [100 * (i + 1) / len(dist) for i in range(len(dist))]
    mean = sum(n * x for n, x in zip(dist, w)) / total
    return mean, sum(n * (x - mean) ** 2 for n, x in zip(dist, w)) / total


def _draws(codes, m, rng):
    """Delta+ and lambda+ of FUNNEL_DRAWS random sets of m units, all at
    once: the same steps as _distances and _delta, one row per set."""
    top, nranks = codes.shape
    picks = np.argpartition(rng.random((FUNNEL_DRAWS, top)), m - 1, axis=1)[:, :m] if m < top \
        else np.tile(np.arange(top), (FUNNEL_DRAWS, 1))
    total = m * (m - 1) // 2
    shared = np.zeros((FUNNEL_DRAWS, nranks), dtype=np.int64)
    for k in range(nranks):
        sub = codes[picks, k]
        width = int(codes[:, k].max()) + 2
        # One bin per (set, code); unknown (-1) goes to a bin left out
        flat = (sub + 1) + np.arange(FUNNEL_DRAWS)[:, None] * width
        counts = np.bincount(flat.ravel(), minlength=FUNNEL_DRAWS * width).reshape(FUNNEL_DRAWS, width)[:, 1:]
        shared[:, k] = (counts * (counts - 1) // 2).sum(axis=1)
    shared = np.maximum.accumulate(shared, axis=1)
    dist = np.diff(np.hstack([np.zeros((FUNNEL_DRAWS, 1), dtype=np.int64), shared,
                              np.full((FUNNEL_DRAWS, 1), total)]), axis=1)
    w = 100 * np.arange(1, nranks + 2) / (nranks + 1)
    mean = dist @ w / total
    var = (dist * (w[None, :] - mean[:, None]) ** 2).sum(axis=1) / total
    return mean, var


def _funnel_at(funnel, s):
    """The funnel's values at s units, linear between the sizes drawn."""
    for lo, hi in zip(funnel, funnel[1:]):
        if lo["s"] <= s <= hi["s"]:
            k = (s - lo["s"]) / (hi["s"] - lo["s"])
            return {key: lo[key] + k * (hi[key] - lo[key]) for key in lo}
    return funnel[-1] if s >= funnel[-1]["s"] else funnel[0]


def distinctness(unit_rows, zone_sets):
    """Delta+ / lambda+ of each zone and the funnel of random sets.

    unit_rows: {unit: lineage}; zone_sets: {zone: set of units}. None when
    there is nothing to compare (fewer than MIN_DISTINCT_UNITS units with a
    genus); "skipped" when they all share one genus."""
    units = sorted(u for u, lin in unit_rows.items() if lin[0])
    if len(units) < MIN_DISTINCT_UNITS:
        return None
    if len({unit_rows[u][0] for u in units}) < 2:
        return {"skipped": "one_genus", "genus": unit_rows[units[0]][0], "units": len(units)}
    index = {u: i for i, u in enumerate(units)}
    # A missing rank takes the commonest value the other units of its rank
    # below hold (the family of the other Aedes), or one of its own from it
    # ("?Aedes"), so units sharing that lower rank still share this one and
    # every pair keeps its level
    filled = {u: list(unit_rows[u]) for u in units}
    for k in range(1, len(RANKS)):
        known = defaultdict(Counter)
        for lin in filled.values():
            if lin[k]:
                known[lin[k - 1]][lin[k]] += 1
        for lin in filled.values():
            if not lin[k]:
                common = known[lin[k - 1]].most_common(1)
                lin[k] = common[0][0] if common else "?" + lin[k - 1]
    codes = np.full((len(units), len(RANKS)), -1, dtype=np.int64)
    for k in range(len(RANKS)):
        seen = {}
        for u in units:
            codes[index[u], k] = seen.setdefault(filled[u][k], len(seen))
    # A rank every unit shares adds nothing: drop it, so the largest
    # distance (100) is the deepest split in the analysis (Clarke & Warwick)
    codes = codes[:, [k for k in range(len(RANKS)) if (codes[:, k] != 0).any()]]

    def measure(rows):
        return _delta(_distances(codes[rows]))

    zones = {}
    for z, us in zone_sets.items():
        rows = np.array(sorted(index[u] for u in us if u in index), dtype=np.int64)
        d, lam = measure(rows) if len(rows) >= 2 else (None, None)
        zones[z] = {"taxed": len(rows), "delta": d, "lambda": lam}
    top = len(units)
    biggest = max((v["taxed"] for v in zones.values()), default=0)
    span = min(top, max(biggest, 3)) - 2
    # Denser at small sizes, where the funnel widens fast
    sizes = sorted({2 + round(span * (i / FUNNEL_SIZES) ** 2) for i in range(FUNNEL_SIZES + 1)})
    rng = np.random.default_rng(FUNNEL_SEED)
    funnel = []
    for m in sizes:
        ds, ls = _draws(codes, m, rng)
        dlo, dhi = np.percentile(ds, [2.5, 97.5])
        llo, lhi = np.percentile(ls, [2.5, 97.5])
        funnel.append({"s": m, "mean": float(np.mean(ds)), "lo": float(dlo), "hi": float(dhi),
                       "l_mean": float(np.mean(ls)), "l_lo": float(llo), "l_hi": float(lhi)})
    for v in zones.values():
        v["delta_state"] = v["lambda_state"] = None
        if v["delta"] is not None:
            f = _funnel_at(funnel, v["taxed"])
            v["expected"] = f["mean"]
            v["delta_state"] = "low" if v["delta"] < f["lo"] else "high" if v["delta"] > f["hi"] else "inside"
            # With a single rank left, lambda+ follows from delta+ alone
            if codes.shape[1] > 1:
                v["lambda_state"] = "high" if v["lambda"] > f["l_hi"] else "low" if v["lambda"] < f["l_lo"] else "inside"
    master = measure(np.arange(top))
    return {"skipped": None, "units": top, "master_delta": master[0], "master_lambda": master[1],
            "draws": FUNNEL_DRAWS, "funnel": funnel, "zones": zones}


def run_diversity(records, marker, unit="bin", zoning="country"):
    """Richness, completeness and between-zone composition of the records."""
    if unit not in UNITS or zoning not in ZONINGS:
        raise ValueError(unit if unit not in UNITS else zoning)
    start = time.time()
    by_zone = defaultdict(Counter)
    places = defaultdict(set)
    no_unit = no_zone = 0
    names = Counter()   # species names under each BIN, for the lists
    lineage = defaultdict(Counter)   # lineages seen for each unit
    named = 0
    for rec in records:
        if not rec.get("seq"):
            continue
        u = _unit(rec, unit)
        if rec.get("species"):
            named += 1
        if not u:
            no_unit += 1
            continue
        z = zone_of(rec, zoning)
        if not z:
            no_zone += 1
            continue
        by_zone[z][u] += 1
        lineage[u][_lineage(rec)] += 1
        c = seq_popgen.coord_of(rec)
        if c:
            places[z].add(c)
        if unit == "bin" and rec.get("species") and not seq_popgen.mixed_name(rec["species"]):
            names[(u, rec["species"])] += 1

    # Provinces or regions kept all of one country: the country needn't be
    # said (zones left out for their size don't count, and keep theirs)
    if zoning in ("province", "region") and by_zone:
        tail = lambda z: z[z.rfind(" ("):] if z.endswith(")") and " (" in z else None
        tails = {tail(z) for z, c in by_zone.items() if sum(c.values()) >= MIN_ZONE_SEQUENCES}
        if len(tails) == 1 and None not in tails:
            t = tails.pop()
            short = lambda z: z[:-len(t)] if tail(z) == t else z
            # A zone of the same name with no country joins it, rather than
            # one replacing the other
            merged_zone, merged_places = defaultdict(Counter), defaultdict(set)
            for z, c in by_zone.items():
                merged_zone[short(z)].update(c)
            for z, p in places.items():
                merged_places[short(z)] |= p
            by_zone, places = merged_zone, merged_places
    sizes = {z: sum(c.values()) for z, c in by_zone.items()}
    kept = sorted((z for z in by_zone if sizes[z] >= MIN_ZONE_SEQUENCES), key=lambda z: (-sizes[z], z))
    small = sorted((z for z in by_zone if sizes[z] < MIN_ZONE_SEQUENCES), key=lambda z: (-sizes[z], z))
    common = min((sizes[z] for z in kept), default=0)

    zones = []
    for z in kept:
        counts = list(by_zone[z].values())
        est, lo, hi = chao1(counts)
        zones.append({
            "zone": z, "n": sizes[z], "places": len(places[z]), "units": len(counts),
            "f1": sum(1 for c in counts if c == 1), "f2": sum(1 for c in counts if c == 2),
            "chao1": est, "chao1_lo": lo, "chao1_hi": hi, "coverage": coverage(counts),
            "rarefied": rarefy(counts, common) if len(kept) > 1 else None,
            "exclusive": 0, "curve": curve(counts),
        })

    sets = {z: set(by_zone[z]) for z in kept}
    presence = Counter(u for z in kept for u in sets[z])
    for row in zones:
        row["exclusive"] = sum(1 for u in sets[row["zone"]] if presence[u] == 1)
    pairs = []
    for i, a in enumerate(kept):
        for b in kept[i + 1:]:
            pairs.append({"a": a, "b": b, **beta_pair(sets[a], sets[b])})
    unit_rows = {}
    for u in presence:
        # The commonest lineage with a genus, else the commonest
        ranked = lineage[u].most_common()
        unit_rows[u] = next((lin for lin, _ in ranked if lin[0]), ranked[0][0])
    # With one zone the funnel would compare it with itself
    taxo = distinctness(unit_rows, sets) if len(kept) > 1 else None
    if taxo and not taxo["skipped"]:
        for row in zones:
            row["distinct"] = taxo["zones"][row["zone"]]
    top_name = {}
    for (u, sp), n in names.most_common():
        top_name.setdefault(u, sp)
    where = defaultdict(list)
    for z in kept:
        for u in sets[z]:
            where[u].append(z)
    listed = lambda us: [{"unit": u, "name": top_name.get(u, ""), "zones": where[u],
                          "n": sum(by_zone[z][u] for z in where[u])} for u in us]
    exclusive = sorted((u for u in presence if presence[u] == 1),
                       key=lambda u: (-sum(by_zone[z][u] for z in where[u]), u))
    widespread = sorted((u for u in presence if presence[u] > 1), key=lambda u: (-presence[u], u))

    result = {
        "marker": marker, "unit": unit, "zoning": zoning, "min_zone": MIN_ZONE_SEQUENCES,
        "received": len(records), "no_unit": no_unit, "no_zone": no_zone, "named": named,
        "zones": zones, "common_n": common,
        "small_zones": [[z, sizes[z]] for z in small],
        "pairs": pairs, "distinctness": taxo,
        "multi": beta_multi([sets[z] for z in kept]) if len(kept) > 1 else None,
        "exclusive": listed(exclusive[:MAX_LIST]), "exclusive_n": len(exclusive),
        "widespread": listed(widespread[:MAX_LIST]), "units_total": len(presence),
    }
    result["guide"] = guide(result)
    result["seconds"] = round(time.time() - start, 3)
    return result


def guide(r):
    """Facts for the readings of both views; the page words them."""
    zones = r["zones"]
    rows = [{"zone": z["zone"], "n": z["n"], "units": z["units"], "chao1": z["chao1"],
             "missing": max(0.0, z["chao1"] - z["units"]), "coverage": z["coverage"],
             "rarefied": z["rarefied"], "f1": z["f1"],
             "state": "complete" if z["coverage"] >= GUIDE_COMPLETE
             else "poor" if z["coverage"] < GUIDE_POOR else "incomplete"}
            for z in zones]
    order = sorted(rows, key=lambda x: -(x["rarefied"] or 0))
    pairs = r["pairs"]
    poor = {x["zone"] for x in rows if x["state"] == "poor"}
    nested = [p for p in pairs if p["beta_sor"] > 0 and p["beta_sne"] / p["beta_sor"] >= GUIDE_NESTED]
    by_sim = sorted(pairs, key=lambda p: -p["sorensen"])
    total = r["received"]
    return {
        "zones": rows, "n_zones": len(rows), "common_n": r["common_n"],
        "richest_obs": max(rows, key=lambda x: x["units"])["zone"] if rows else None,
        "rarefied_order": [[x["zone"], x["rarefied"]] for x in order] if len(rows) > 1 else [],
        "rarefied_alike": len(rows) > 1 and order[0]["rarefied"] - order[-1]["rarefied"] < GUIDE_SAME_RICHNESS,
        "small_n": len(r["small_zones"]), "small_seqs": sum(n for _, n in r["small_zones"]),
        "few_names": r["unit"] == "species" and total and r["named"] / total < GUIDE_FEW_NAMES,
        "named_share": r["named"] / total if total else 0,
        "no_unit": r["no_unit"], "no_zone": r["no_zone"], "received": total,
        "multi": r["multi"],
        "most_similar": by_sim[0] if by_sim else None,
        "least_similar": by_sim[-1] if len(by_sim) > 1 else None,
        "nested": [{"a": p["a"], "b": p["b"], "sne": p["beta_sne"], "sor": p["beta_sor"],
                    "poor": [z for z in (p["a"], p["b"]) if z in poor]} for p in nested],
        "exclusive_n": r["exclusive_n"], "units_total": r["units_total"],
        "exclusive_by_zone": sorted([[z["zone"], z["exclusive"]] for z in zones], key=lambda x: -x[1]),
        "widespread": r["widespread"][:3],
        "complete": GUIDE_COMPLETE, "poor": GUIDE_POOR,
        "distinct": _distinct_guide(r),
    }


def _distinct_guide(r):
    """Facts for the reading of delta+ / lambda+, or None without them."""
    t = r.get("distinctness")
    if not t:
        return None
    if t["skipped"]:
        return {"skipped": t["skipped"], "genus": t.get("genus")}
    rows = [{"zone": z["zone"], "units": z["distinct"]["taxed"], "delta": z["distinct"]["delta"],
             "expected": z["distinct"].get("expected"), "state": z["distinct"]["delta_state"],
             "lambda_state": z["distinct"]["lambda_state"]}
            for z in r["zones"] if z["distinct"]["delta"] is not None]
    return {"skipped": None, "zones": rows, "master": t["master_delta"],
            "low": [x["zone"] for x in rows if x["state"] == "low"],
            "high": [x["zone"] for x in rows if x["state"] == "high"],
            "inside": [x["zone"] for x in rows if x["state"] == "inside"],
            "uneven": [x["zone"] for x in rows if x["lambda_state"] == "high"]}
