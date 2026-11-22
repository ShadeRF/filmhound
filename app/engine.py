"""Recommendation engine.

Two stages:
  1. Candidate generation: every movie you own/watched/liked "votes" for the titles TMDB
     lists as its recommendations/similar, weighted by how strongly that seed reflects
     your taste (rating > watched recently > watched > owned-but-unwatched).
  2. Scoring: the top candidates are scored on three signals —
       collab   how many of your movies point at it, and how strongly
       content  similarity to your taste profile (directors, writers, cast, keywords,
                genres, decade, language, collection) with TF-IDF weighting
       quality  Bayesian-weighted IMDb (or TMDB) rating
     and each pick carries its reasons ("Because you watched …", "Directed by …").
"""
import math
import time
from collections import defaultdict
from datetime import date

FEATURE_WEIGHTS = {
    "d": 3.0,    # director
    "w": 2.0,    # writer / screenplay
    "p": 1.0,    # director of photography
    "m": 0.8,    # composer
    "c": 1.0,    # cast
    "k": 1.5,    # keyword
    "g": 0.7,    # genre
    "y": 0.6,    # decade
    "l": 0.6,    # original language
    "o": 2.0,    # collection / franchise
    "n": 1.0,    # TV network
}

SCORE_WEIGHTS = {"collab": 0.45, "content": 0.35, "quality": 0.20}


# ---------------------------------------------------------------- seeds

def seed_weight(row, now=None, owned_weight=0.8):
    """How strongly a library item represents your taste. Negative = dislike."""
    now = now or time.time()
    r = row.get("user_rating")
    if r is not None and r > 0:
        w = (r - 5.0) / 2.5          # 10 -> 2.0, 8 -> 1.2, 6 -> 0.4, 4 -> -0.4, 2 -> -1.2
    elif (row.get("view_count") or 0) > 0:
        leaves = row.get("leaf_count")
        if leaves:   # TV: how much of the show you've watched (sampling one episode isn't a vote)
            frac = min(1.0, row["view_count"] / leaves)
            w = 0.4 + 0.8 * frac if row["view_count"] >= 3 or frac >= 0.5 else 0.3
        else:
            w = 1.0
    else:
        w = owned_weight              # owned but never watched: you chose to collect it
    lv = row.get("last_viewed_at")
    if lv and w > 0:
        days = max(0.0, (now - lv) / 86400)
        w *= 1.0 + 0.5 * math.exp(-days / 365)
    return w


def seed_kind(row):
    if row.get("user_rating"):
        return "rated"
    if (row.get("view_count") or 0) > 0:
        if row.get("leaf_count") and row["view_count"] < row["leaf_count"]:
            return "partial"
        return "watched"
    return "owned"


def build_seeds(library, feedback, now=None, owned_weight=0.8):
    """Return ({tmdb_id: (weight, title)}, soft_negatives, {tmdb_id: info}).
    Soft negatives (dismissed) are used only by the content signal; info describes
    why each seed counts (rated / watched / owned / liked) for explanations."""
    seeds, info = {}, {}
    for row in library:
        tid = row.get("tmdb_id")
        if tid:
            seeds[tid] = (seed_weight(row, now, owned_weight), row.get("title"))
            info[tid] = {"kind": seed_kind(row), "rating": row.get("user_rating"),
                         "last_viewed_at": row.get("last_viewed_at"),
                         "watched_eps": row.get("view_count") if row.get("leaf_count") else None,
                         "total_eps": row.get("leaf_count")}
    soft_neg = set()
    for tid, fb in feedback.items():
        a = fb["action"]
        if a == "liked":
            seeds[tid] = (1.5, fb.get("title"))
            info[tid] = {"kind": "liked"}
        elif a == "disliked":
            seeds[tid] = (-1.0, fb.get("title"))
            info[tid] = {"kind": "disliked"}
        elif a == "seen":   # watched outside Plex (rated here, or imported from Letterboxd/Trakt)
            r = fb.get("rating")
            if not r and tid in seeds and (info[tid].get("rating") or seeds[tid][0] >= 0.9):
                continue   # "watched" with no rating adds nothing to a title Plex already has rated or watched
            w = (r - 5.0) / 2.5 if r else 0.9
            if w != 0:
                seeds[tid] = (w, fb.get("title"))
            info[tid] = {"kind": "seen", "rating": r, "source": fb.get("source")}
        elif a == "dismissed":
            soft_neg.add(tid)
    return seeds, soft_neg, info


# ---------------------------------------------------------------- stage 1

def released(brief, today=None):
    rd = brief.get("release_date") or ""
    if len(rd) < 10:
        return False
    today = today or date.today().isoformat()
    return rd <= today


def _year(rd):
    return int(rd[:4]) if rd and len(rd) >= 4 and rd[:4].isdigit() else None


def votes_needed(min_votes, year):
    """Older films never collect as many TMDB votes, so the bar drops with age."""
    if not year:
        return min_votes
    if year < 1950:
        return int(min_votes * 0.15)
    if year < 1970:
        return int(min_votes * 0.3)
    if year < 1990:
        return int(min_votes * 0.6)
    return min_votes


def era_weight(seed_year, cand_year):
    """A 2018 shark movie 'recommending' a 1932 thriller is mostly TMDB popularity noise;
    links between titles from the same era count fully, distant ones count a little."""
    if not seed_year or not cand_year:
        return 1.0
    return 0.25 + 0.75 * math.exp(-abs(seed_year - cand_year) / 18.0)


def generate_candidates(seeds, details, exclude, min_votes):
    """seeds: {id: (w, title)}; details: {id: trimmed tmdb movie}.
    Returns {cand_id: {"collab": float, "because": {seed_id: contrib}, "brief": {...}}}."""
    cands = {}
    for sid, (w, title) in seeds.items():
        d = details.get(sid)
        if not d or d.get("missing") or w == 0:
            continue
        sy = _year(d.get("release_date"))
        for lst, factor in (("recommendations", 1.0), ("similar", 0.5)):
            for rank, b in enumerate(d.get(lst, [])):
                cid = b["id"]
                if cid in exclude or b.get("adult"):
                    continue
                cy = _year(b.get("release_date"))
                if (b.get("vote_count") or 0) < votes_needed(min_votes, cy) or not released(b):
                    continue
                contrib = w * factor * max(0.1, 1.0 - rank / 25.0) * era_weight(sy, cy)
                c = cands.setdefault(cid, {"collab": 0.0, "because": defaultdict(float), "brief": b})
                c["collab"] += contrib if contrib > 0 else contrib * 0.5
                if contrib > 0:
                    c["because"][sid] += contrib
    return {k: v for k, v in cands.items() if v["collab"] > 0}


def stratified_pool(cands, library_years, size):
    """Pick which candidates get fully scored. Instead of purely 'most pointed-at' (which
    favours recent, popular titles), give each decade a share of the pool matching how
    much of your library comes from that decade, then fill the rest by score."""
    if not cands:
        return []
    by_collab = sorted(cands, key=lambda k: -cands[k]["collab"])
    decades = defaultdict(int)
    for y in library_years:
        if y:
            decades[y // 10 * 10] += 1
    total = sum(decades.values())
    if not total:
        return by_collab[:size]
    chosen, seen = [], set()
    buckets = defaultdict(list)
    for k in by_collab:
        y = _year(cands[k]["brief"].get("release_date"))
        buckets[y // 10 * 10 if y else None].append(k)
    reserve = int(size * 0.6)   # 60% by your library's era mix, 40% purely by score
    for dec, n in decades.items():
        for k in buckets.get(dec, [])[:max(10, int(reserve * n / total))]:
            if k not in seen:
                seen.add(k); chosen.append(k)
    for k in by_collab:
        if len(chosen) >= size:
            break
        if k not in seen:
            seen.add(k); chosen.append(k)
    return chosen[:size]


# ---------------------------------------------------------------- stage 2

def features(d):
    """Sparse feature dict {feature_key: raw_weight} plus labels for explanations."""
    f, labels = {}, {}

    def add(key, w, label):
        f[key] = max(f.get(key, 0), w)
        labels[key] = label

    for c in d.get("crew", []):
        job = c["job"]
        if job == "Director":
            add(f"d:{c['id']}", FEATURE_WEIGHTS["d"], f"Directed by {c['name']}")
        elif job == "Creator":
            add(f"d:{c['id']}", FEATURE_WEIGHTS["d"], f"Created by {c['name']}")
        elif job in ("Screenplay", "Writer"):
            add(f"w:{c['id']}", FEATURE_WEIGHTS["w"], f"Written by {c['name']}")
        elif job == "Director of Photography":
            add(f"p:{c['id']}", FEATURE_WEIGHTS["p"], f"Shot by {c['name']}")
        elif job == "Original Music Composer":
            add(f"m:{c['id']}", FEATURE_WEIGHTS["m"], f"Score by {c['name']}")
    for i, c in enumerate(d.get("cast", [])[:6]):
        add(f"c:{c['id']}", FEATURE_WEIGHTS["c"] * (1.0 if i < 3 else 0.6), f"Stars {c['name']}")
    for k in d.get("keywords", []):
        add(f"k:{k['id']}", FEATURE_WEIGHTS["k"], k["name"])
    for g in d.get("genres", []):
        add(f"g:{g['id']}", FEATURE_WEIGHTS["g"], g["name"])
    rd = d.get("release_date") or ""
    if len(rd) >= 4:
        dec = int(rd[:4]) // 10 * 10
        add(f"y:{dec}", FEATURE_WEIGHTS["y"], f"{dec}s")
    if d.get("original_language"):
        add(f"l:{d['original_language']}", FEATURE_WEIGHTS["l"], f"Language: {d['original_language']}")
    for nw in d.get("networks") or []:
        add(f"n:{nw['id']}", FEATURE_WEIGHTS["n"], f"On {nw['name']}")
    if d.get("collection"):
        add(f"o:{d['collection']['id']}", FEATURE_WEIGHTS["o"], d["collection"]["name"])
    return f, labels


def _normalize(v):
    n = math.sqrt(sum(x * x for x in v.values()))
    return {k: x / n for k, x in v.items()} if n else {}


def _cos(a, b):
    if len(a) > len(b):
        a, b = b, a
    return sum(x * b.get(k, 0.0) for k, x in a.items())


def bayes(rating, votes, m, prior=6.3):
    if not rating or not votes:
        return None
    return (votes / (votes + m)) * rating + (m / (votes + m)) * prior


def quality_of(d, imdb):
    r = imdb.get(d.get("imdb_id") or "")
    if r and r[1] >= 50:
        q = bayes(r[0], r[1], 2500)
    else:
        q = bayes(d.get("vote_average"), d.get("vote_count"), 400)
    if q is None:
        return 0.3
    return min(1.0, max(0.0, (q - 5.0) / 3.5))   # 5.0 -> 0, 8.5+ -> 1


def taste_matches(feature_keys, taste, n=4):
    """Labels of the user's strongest taste features that this title has."""
    have = set(feature_keys)
    return [t["label"] for t in taste if t["f"] in have and not t["f"].startswith(("y:", "l:"))][:n]


def build_model(seeds, soft_neg, details, extra_keys=(), seed_info=None):
    """Everything needed to score any title later: IDF, taste vectors, seed display info."""
    seed_info = seed_info or {}
    feats = {}

    def fv(i):
        if i not in feats:
            d = details.get(i)
            feats[i] = features(d) if d and not d.get("missing") else ({}, {})
        return feats[i]

    universe = [i for i in list(seeds) + list(extra_keys) if details.get(i) and not details[i].get("missing")]
    df = defaultdict(int)
    for i in universe:
        for k in fv(i)[0]:
            df[k] += 1
    n = len(universe) or 1
    idf = {k: round(math.log((n + 1) / (c + 1)) + 1.0, 4) for k, c in df.items()}
    model = {"idf": idf, "idf_default": math.log(n + 1) + 1.0}

    pos, neg = defaultdict(float), defaultdict(float)
    for sid, (w, _) in seeds.items():
        target = pos if w > 0 else neg
        for k, x in item_vec(model, fv(sid)[0]).items():
            target[k] += abs(w) * x
    for sid in soft_neg:
        for k, x in item_vec(model, fv(sid)[0]).items():
            neg[k] += 0.4 * x
    model["pos"], model["neg"] = _normalize(pos), _normalize(neg)

    all_labels, seed_disp = {}, {}
    for sid, (w, title) in seeds.items():
        if w <= 0:
            continue
        all_labels.update(fv(sid)[1])
        sd = details.get(sid) or {}
        inf = seed_info.get(sid, {})
        rd = sd.get("release_date") or ""
        seed_disp[sid] = {
            "tmdb_id": sid, "title": sd.get("title") or title,
            "year": int(rd[:4]) if len(rd) >= 4 else None, "poster_path": sd.get("poster_path"),
            "kind": inf.get("kind", "owned"), "rating": inf.get("rating"), "source": inf.get("source"),
            "last_viewed_at": inf.get("last_viewed_at"),
            "watched_eps": inf.get("watched_eps"), "total_eps": inf.get("total_eps"),
        }
    model["seeds"] = seed_disp
    model["taste"] = [{"f": k, "label": all_labels.get(k, k), "w": round(v, 5)}
                      for k, v in sorted(model["pos"].items(), key=lambda kv: -kv[1])[:200]]
    return model


def item_vec(model, f):
    idf, dflt = model["idf"], model["idf_default"]
    return _normalize({k: w * idf.get(k, dflt) for k, w in f.items()})


def score_item(model, key, d, collab, because, imdb):
    """Score one title against the model. Uses the model's normalizers so scores are
    comparable with the precomputed picks."""
    f, labels = features(d)
    cv = item_vec(model, f)
    pos, neg = model["pos"], model["neg"]
    content = _cos(pos, cv) - 0.5 * _cos(neg, cv)
    collab_n = min(1.0, math.log1p(collab) / model.get("max_collab", 1.0)) if collab > 0 else 0.0
    content_n = min(1.0, max(0.0, content) / (model.get("max_content") or 1.0))
    qual = quality_of(d, imdb)
    total = (SCORE_WEIGHTS["collab"] * collab_n + SCORE_WEIGHTS["content"] * content_n
             + SCORE_WEIGHTS["quality"] * qual)
    contrib = sorted(((pos.get(k, 0) * x, k) for k, x in cv.items()), reverse=True)
    matches = [labels[k] for v, k in contrib if v > 0 and not k.startswith(("y:", "l:"))][:4]

    because = {int(k): v for k, v in (because or {}).items()}
    total_pos = sum(because.values()) or 1.0
    seeds = model.get("seeds", {})
    bec = []
    for sid, v in sorted(because.items(), key=lambda kv: -kv[1])[:4]:
        sd = seeds.get(sid) or seeds.get(str(sid))
        if sd:
            bec.append({**sd, "share": round(v / total_pos, 3)})
    im = imdb.get(d.get("imdb_id") or "")
    rd, lad = d.get("release_date") or "", d.get("last_air_date") or ""
    return {
        "key": key, "tmdb_id": abs(key), "media": "tv" if key < 0 else "movie",
        "tvdb_id": d.get("tvdb_id"), "seasons": d.get("seasons"), "status": d.get("status"),
        "end_year": int(lad[:4]) if key < 0 and len(lad) >= 4 else None,
        "networks": [n["name"] for n in d.get("networks") or []],
        "creators": [x["name"] for x in d.get("crew", []) if x["job"] == "Creator"],
        "title": d.get("title"), "year": int(rd[:4]) if len(rd) >= 4 else None,
        "overview": d.get("overview"), "poster_path": d.get("poster_path"), "runtime": d.get("runtime"),
        "backdrop_path": d.get("backdrop_path"), "providers": d.get("providers"), "trailer": d.get("trailer"),
        "genres": [g["name"] for g in d.get("genres", [])], "language": d.get("original_language"),
        "directors": [x["name"] for x in d.get("crew", []) if x["job"] == "Director"],
        "imdb_id": d.get("imdb_id"), "imdb_rating": im[0] if im else None, "imdb_votes": im[1] if im else None,
        "tmdb_rating": d.get("vote_average"), "tmdb_votes": d.get("vote_count"), "popularity": d.get("popularity"),
        "score": round(total, 4),
        "components": {"collab": round(collab_n, 3), "content": round(content_n, 3), "quality": round(qual, 3)},
        "because": bec, "seed_count": len(because), "matches": matches,
    }


def score(seeds, soft_neg, cands, details, imdb, max_recs=500, seed_info=None):
    model = build_model(seeds, soft_neg, details, list(cands), seed_info)
    rows = []
    for cid, c in cands.items():
        d = details.get(cid)
        if not d or d.get("missing"):
            continue
        cv = item_vec(model, features(d)[0])
        rows.append((cid, c, d, _cos(model["pos"], cv) - 0.5 * _cos(model["neg"], cv)))
    model["max_collab"] = max((math.log1p(c["collab"]) for _, c, _, _ in rows), default=1.0) or 1.0
    model["max_content"] = max((r[3] for r in rows), default=1.0) or 1.0
    score.last_model = model
    score.last_taste = model["taste"]
    out = [score_item(model, cid, d, c["collab"], c["because"], imdb) for cid, c, d, _ in rows]
    out.sort(key=lambda r: -r["score"])
    return out[:max_recs] if max_recs else out
