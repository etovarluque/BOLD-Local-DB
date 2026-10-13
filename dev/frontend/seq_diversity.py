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
- Between zones (presence/absence): Sorensen similarity and Baselga's
  (2010) split of Sorensen dissimilarity into turnover (beta-sim) and
  nestedness (beta-sne), per pair and over all zones; units found in one
  zone only and the most widespread ones.

Zones with fewer than MIN_ZONE_SEQUENCES sequences are left out.
"""

import math
import time
from collections import Counter, defaultdict

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
GUIDE_SAME_RICHNESS = 0.5


def _zone(rec, zoning):
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
    sp = (rec.get("species") or "").strip()
    return "" if seq_popgen.mixed_name(sp) else sp


def chao1(counts):
    """Bias-corrected Chao1 and its log-normal 95 % interval."""
    n = sum(counts)
    s = len(counts)
    f1 = sum(1 for c in counts if c == 1)
    f2 = sum(1 for c in counts if c == 2)
    k = (n - 1) / n if n else 0
    est = s + k * f1 * (f1 - 1) / (2 * (f2 + 1))
    # Variance (Chao 1987, bias-corrected form)
    if f2 > 0:
        r = f1 / f2
        var = f2 * (k * r ** 2 / 2 + k ** 2 * r ** 3 + k ** 2 * r ** 4 / 4)
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
        a = (n - 1) * (f1 - 1) / ((n - 1) * (f1 - 1) + 2) if f1 > 1 else 1.0
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
    return {"shared": a, "only_a": b, "only_b": c, "sorensen": 1 - sor,
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


def run_diversity(records, marker, unit="bin", zoning="country"):
    """Richness, completeness and between-zone composition of the records."""
    if unit not in UNITS or zoning not in ZONINGS:
        raise ValueError(unit if unit not in UNITS else zoning)
    start = time.time()
    by_zone = defaultdict(Counter)
    places = defaultdict(set)
    no_unit = no_zone = 0
    names = Counter()   # species names under each BIN, for the lists
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
        z = _zone(rec, zoning)
        if not z:
            no_zone += 1
            continue
        by_zone[z][u] += 1
        c = seq_popgen.coord_of(rec)
        if c:
            places[z].add(c)
        if unit == "bin" and rec.get("species") and not seq_popgen.mixed_name(rec["species"]):
            names[(u, rec["species"])] += 1

    # Provinces or regions all of one country: the country needn't be said
    if zoning in ("province", "region") and by_zone:
        tails = {z[z.rfind(" ("):] if z.endswith(")") and " (" in z else None for z in by_zone}
        if len(tails) == 1 and None not in tails:
            cut = len(tails.pop())
            by_zone = defaultdict(Counter, {z[:-cut]: c for z, c in by_zone.items()})
            places = defaultdict(set, {z[:-cut]: p for z, p in places.items()})
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
        "pairs": pairs,
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
    }
