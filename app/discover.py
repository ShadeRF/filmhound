"""Endless "more from your taste" feed.

Once your scored picks run out, keep going by paging TMDB Discover through a set of
"streams", one per strong feature of your taste profile (a keyword you love, a director,
a writer, an actor, a network, a genre pairing). Streams are interleaved round-robin so
the feed stays varied, and everything you own, hid, requested or already saw in your
scored picks is filtered out.
"""
import itertools
import json
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from . import config, db, engine
from .filters import passes
from .clients import TMDB, key_for

PAGES_PER_CALL = 2
MAX_ROUNDS = 60           # pages per stream before we call it exhausted

_cache = OrderedDict()
_cache_lock = threading.Lock()


def _cached_get(tmdb, path, params):
    k = path + "?" + "&".join(f"{a}={b}" for a, b in sorted(params.items()))
    with _cache_lock:
        hit = _cache.get(k)
        if hit and time.time() - hit[0] < 6 * 3600:
            _cache.move_to_end(k)
            return hit[1]
    d = tmdb._get(path, **params) or {}
    with _cache_lock:
        _cache[k] = (time.time(), d)
        while len(_cache) > 800:
            _cache.popitem(last=False)
    return d


def genre_map(tmdb, media):
    """{name: id} and {id: name} for TMDB genres, cached in meta."""
    m = db.get_meta(f"genres_{media}")
    if not m:
        d = tmdb._get(f"/genre/{media}/list") or {}
        m = {g["name"]: g["id"] for g in d.get("genres", [])}
        db.set_meta(f"genres_{media}", m)
    return m, {v: k for k, v in m.items()}


def streams(media, taste, genre_ids):
    """Ordered list of (label, params) built from the strongest taste features."""
    out, genres = [], []
    caps = {"k": 22, "d": 8, "w": 6, "c": 8, "n": 6}
    used = {k: 0 for k in caps}
    for t in taste:
        typ, _, fid = t["f"].partition(":")
        if typ == "g":
            genres.append(fid)
            continue
        if typ not in caps or used[typ] >= caps[typ]:
            continue
        if media == "tv" and typ in ("d", "w", "c"):
            continue   # TMDB's TV discover can't filter by people
        if media == "movie" and typ == "n":
            continue
        param = {"k": "with_keywords", "d": "with_crew", "w": "with_crew", "c": "with_cast", "n": "with_networks"}[typ]
        label = t["label"] if typ != "k" else t["label"].capitalize()
        out.append((label, {param: fid}))
        used[typ] += 1
    names = {str(v): k for k, v in genre_ids.items()}
    for a, b in list(itertools.combinations(genres[:5], 2))[:6]:
        out.append((f"{names.get(a, a)} + {names.get(b, b)}", {"with_genres": f"{a},{b}"}))
    # interleave types so consecutive pages aren't all keywords
    buckets = {}
    for label, p in out:
        buckets.setdefault(next(iter(p)), []).append((label, p))
    mixed = []
    for group in itertools.zip_longest(*buckets.values()):
        mixed.extend(x for x in group if x)
    return mixed


def filter_params(media, f, genre_ids):
    tv = media == "tv"
    today = date.today().isoformat()
    datef = "first_air_date" if tv else "primary_release_date"
    p = {"include_adult": "false", "vote_count.gte": max(config.MIN_VOTES_TV if tv else config.MIN_VOTES, f["min_votes"])}
    lte = today
    if f["year_max"] < 9999:
        lte = min(today, f"{f['year_max']}-12-31")
    p[f"{datef}.lte"] = lte
    if f["year_min"]:
        p[f"{datef}.gte"] = f"{f['year_min']}-01-01"
    if f["min_rating"]:
        p["vote_average.gte"] = f["min_rating"]
    if f["max_runtime"]:
        p["with_runtime.lte"] = f["max_runtime"]
    inc = [str(genre_ids[g]) for g in f["genres"] if g in genre_ids]
    exc = [str(genre_ids[g]) for g in f["exclude_genres"] if g in genre_ids]
    if inc:
        p["with_genres"] = ("," if f["genre_mode"] == "all" else "|").join(inc)
    if exc:
        p["without_genres"] = ",".join(exc)
    if len(f["languages"]) == 1:
        p["with_original_language"] = f["languages"][0]
    if tv and f["networks"]:
        ids = db.get_meta("network_ids", {})
        ids = [str(ids[n]) for n in f["networks"] if n in ids]
        if ids:
            p["with_networks"] = "|".join(ids)
    sort = {"rating": "vote_average.desc", "popular": "popularity.desc", "newest": f"{datef}.desc",
            "oldest": f"{datef}.asc"}.get(f["sort"], "vote_average.desc")
    p["sort_by"] = sort
    return p


def already_wanted():
    """Titles already on their way: in Radarr or Sonarr, requested (or blocked) in Seerr, or sent from Filmhound."""
    keys = {k for k, f in db.feedback().items() if f["action"] in ("requested", "added")}
    for m in ("seerr_block", "seerr_requested", "radarr_ids", "sonarr_keys"):
        keys |= set(db.get_meta(m, []))
    return keys


def excluded_keys(media):
    def build():
        keys = set(db.library_map()) | set(db.feedback())
        keys |= set(db.recs_map(media))
        for m in ("seerr_block", "seerr_requested", "radarr_ids", "sonarr_keys"):
            keys |= set(db.get_meta(m, []))
        return keys
    return db.cached(("excluded", media), build)


def ensure_details(tmdb, keys):
    keys = [k for k in dict.fromkeys(keys)]
    have = {k: v[0] for k, v in db.tmdb_get_many(keys).items()}
    todo = [k for k in keys if k not in have or (not have[k].get("missing") and (
        have[k].get("v", 1) < 2 or (have[k].get("providers") or {}).get("region") != config.WATCH_REGION))]
    if todo:
        with ThreadPoolExecutor(max_workers=config.TMDB_WORKERS) as ex:
            for k, d in zip(todo, ex.map(lambda k: _safe_details(tmdb, k), todo)):
                if d is not None:
                    db.tmdb_put(k, d)   # sqlite writes stay on this thread
                    db.commit()         # straight away, so the write lock isn't held while the next lookup is on its way
                    have[k] = d
    return have


def _safe_details(tmdb, k):
    try:
        return tmdb.details(k)
    except Exception:
        return None


def _score_batch(media, keys, collab_info, tmdb, f, label=None):
    model = db.get_meta(f"model_{media}")
    if not model:
        return []
    details = ensure_details(tmdb, keys)
    imdb = db.imdb_get_many([d.get("imdb_id") for d in details.values()])
    out = []
    for k in keys:
        d = details.get(k)
        if not d or d.get("missing") or not d.get("poster_path") or d.get("adult"):
            continue
        rd = d.get("release_date") or ""
        if len(rd) < 10 or rd > date.today().isoformat():
            continue
        collab, because = collab_info.get(k, (0.0, {}))
        r = engine.score_item(model, k, d, collab, because, imdb)
        if not passes(r, f):
            continue
        if label:
            r["source"], r["stream"] = "taste", label
        else:
            r["source"] = "deep"
        out.append(r)
    out.sort(key=lambda r: -r["score"])
    return out


def more(media, f, cursor):
    """Endless scored feed. Returns (items, next_cursor, done).

    Phase 1 ("d:<collab>:<key>"): every other candidate your own titles point to,
    in order of how many point to it, scored on demand.
    Phase 2 ("s:<round>.<stream>"): TMDB Discover pages built from your taste features.
    """
    if f["q"]:
        return [], cursor, True
    tmdb = TMDB(config.TMDB_API_KEY)
    skip = excluded_keys(media)
    cursor = cursor or "d:inf:0"

    if cursor.startswith("d:"):
        _, c_collab, c_key = cursor.split(":")
        after_collab, after_key = float(c_collab), int(c_key)
        min_votes = 0   # the votes filter is checked on full details in passes()
        found = []
        for _ in range(6):   # pull candidates until a batch survives exclusions + filters
            rows = db.cands_page(media, after_collab, after_key, 60, f["year_min"], f["year_max"],
                                 min_votes, max(0.0, f["min_rating"] - 0.8) if f["min_rating"] else 0.0)
            if not rows:
                return found, "s:0.0", False
            after_collab, after_key = rows[-1]["collab"], rows[-1]["tmdb_id"]
            rows = [r for r in rows if r["tmdb_id"] not in skip]
            info = {r["tmdb_id"]: (r["collab"], json.loads(r["because"] or "{}")) for r in rows}
            found += _score_batch(media, [r["tmdb_id"] for r in rows], info, tmdb, f)
            if len(found) >= 12:
                break
        return found, f"d:{after_collab}:{after_key}", False

    # ---- phase 2: taste streams
    taste = db.get_meta(f"taste_{media}", [])
    if not taste:
        return [], cursor, True
    gmap, gnames = genre_map(tmdb, media)
    sts = streams(media, taste, gmap)
    if not sts:
        return [], cursor, True
    base = filter_params(media, f, gmap)
    rnd, si = (int(x) for x in cursor[2:].split("."))
    plan = []
    while len(plan) < PAGES_PER_CALL and rnd < MAX_ROUNDS:
        label, sp = sts[si]
        params = {**base, **sp, "page": rnd + 1}
        if "with_genres" in sp and "with_genres" in base:
            params["with_genres"] = sp["with_genres"] + "," + base["with_genres"].replace("|", ",") \
                if f["genre_mode"] == "all" else sp["with_genres"]
        plan.append((label, params))
        si += 1
        if si >= len(sts):
            si, rnd = 0, rnd + 1
    if not plan:
        return [], cursor, True
    path = f"/discover/{media}"
    with ThreadPoolExecutor(max_workers=PAGES_PER_CALL) as ex:
        pages = list(ex.map(lambda lp: (lp[0], _cached_get(tmdb, path, lp[1])), plan))
    items, seen = [], set()
    for label, page in pages:
        keys = []
        for m in page.get("results", []):
            k = key_for(media, m["id"])
            if k in skip or k in seen:
                continue
            seen.add(k)
            keys.append(k)
        items += _score_batch(media, keys, {}, tmdb, f, label)
    return items, f"s:{rnd}.{si}", rnd >= MAX_ROUNDS
