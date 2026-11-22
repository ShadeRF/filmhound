"""Collection gaps, coming soon for you, search, and "more like this"."""
import logging
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from . import config, db, discover, engine
from .clients import TMDB, key_for

log = logging.getLogger("filmhound")
WEEK = 7 * 86400
UPCOMING_PEOPLE = 40          # living people whose upcoming work is looked at
UPCOMING_LOOK = 120           # how far down your taste to look for them
UPCOMING_STREAMS = 24         # Discover searches per media, one per strong part of your taste
UPCOMING_KEEP = {"movie": 80, "tv": 40}
UPCOMING_YEARS = 3            # how far ahead to look
UPCOMING_REFRESH_DAYS = 14    # re-read an upcoming title's details this often
UPCOMING_PEOPLE_NUDGE = 0.05  # a little extra for each person you like on it (at most two)


def _cached_tmdb(tmdb, meta_key, path, max_age, **params):
    v = db.get_meta(meta_key)
    if v and time.time() - v.get("_at", 0) < max_age:
        return v
    d = tmdb._get(path, **params) or {}
    d["_at"] = time.time()
    db.set_meta(meta_key, d)
    return d


def person(tmdb, pid):
    return _cached_tmdb(tmdb, f"person_{pid}", f"/person/{pid}", WEEK, append_to_response="combined_credits")


def _year(rd):
    return int(rd[:4]) if rd and len(rd) >= 4 and rd[:4].isdigit() else None


def _item(m, media, **extra):
    rd = m.get("release_date") or m.get("first_air_date") or ""
    return {"key": key_for(media, m["id"]), "tmdb_id": m["id"], "media": media,
            "title": m.get("title") or m.get("name"), "year": _year(rd), "date": rd,
            "poster_path": m.get("poster_path"), "overview": m.get("overview"),
            "tmdb_rating": m.get("vote_average"), "tmdb_votes": m.get("vote_count"),
            "popularity": m.get("popularity"), "genres": [], "because": [], "matches": [], **extra}


def _parallel(fn, args, workers=6):
    out = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for a, r in zip(args, ex.map(lambda a: _safe(fn, a), args)):
            out[a] = r
    return out


def _safe(fn, a):
    try:
        return fn(a)
    except Exception as e:
        log.warning("TMDB lookup failed for %s: %s", a, e)
        return None


# ------------------------------------------------------------------ collection gaps

def build_gaps(tmdb=None, step=lambda *a: None):
    """Movies missing from franchises/collections you own, and from directors you collect."""
    tmdb = tmdb or TMDB(config.TMDB_API_KEY)
    lib = [r for r in db.library() if r["tmdb_id"] > 0]
    owned = {r["tmdb_id"] for r in lib}
    det = {k: v[0] for k, v in db.tmdb_get_many(list(owned)).items()}
    today = date.today().isoformat()

    colls = defaultdict(list)
    for k, d in det.items():
        c = d.get("collection")
        if c:
            colls[c["id"]].append(k)
    step("Checking your collections", f"{len(colls)} collections")
    fetched = {}
    for cid in colls:   # sequential through the meta cache so DB writes stay on this thread
        v = db.get_meta(f"coll_{cid}")
        if v and time.time() - v.get("_at", 0) < 2 * WEEK:
            fetched[cid] = v
    todo = [c for c in colls if c not in fetched]
    for cid, d in _parallel(lambda c: tmdb._get(f"/collection/{c}"), todo).items():
        if d:
            d["_at"] = time.time()
            db.set_meta(f"coll_{cid}", d)
            fetched[cid] = d
    groups = []
    for cid, have in colls.items():
        c = fetched.get(cid) or {}
        parts = [p for p in c.get("parts", []) if p.get("release_date")]
        missing = [_item(p, "movie", upcoming=p["release_date"] > today) for p in parts
                   if p["id"] not in owned and (p.get("vote_count") or 0) >= 5]
        if missing and parts:
            groups.append({"type": "collection", "id": cid, "name": c.get("name"), "poster_path": c.get("poster_path"),
                           "owned": len([p for p in parts if p["id"] in owned]), "total": len(parts), "items": missing})

    # directors you collect: 3+ of their films in your library
    directors = Counter()
    names = {}
    for d in det.values():
        for x in d.get("crew", []):
            if x["job"] == "Director":
                directors[x["id"]] += 1
                names[x["id"]] = (x["name"], x.get("profile"))
    top = [pid for pid, n in directors.most_common(40) if n >= 3]
    step("Checking directors you collect", f"{len(top)} directors")
    people = {}
    for pid in top:
        v = db.get_meta(f"person_{pid}")
        if v and time.time() - v.get("_at", 0) < WEEK:
            people[pid] = v
    for pid, p in _parallel(lambda i: tmdb._get(f"/person/{i}", append_to_response="combined_credits"),
                            [p for p in top if p not in people]).items():
        if p:
            p["_at"] = time.time()
            db.set_meta(f"person_{pid}", p)
            people[pid] = p
    for pid in top:
        p = people.get(pid) or {}
        films = {m["id"]: m for m in (p.get("combined_credits") or {}).get("crew", [])
                 if m.get("job") == "Director" and m.get("media_type") == "movie" and m.get("release_date")
                 and (m.get("vote_count") or 0) >= 25}
        missing = [_item(m, "movie", upcoming=m["release_date"] > today) for m in films.values() if m["id"] not in owned]
        if missing:
            missing.sort(key=lambda x: x["date"])
            groups.append({"type": "director", "id": pid, "name": names[pid][0], "profile": names[pid][1],
                           "owned": directors[pid], "total": directors[pid] + len(missing), "items": missing})
    groups.sort(key=lambda g: (-g["owned"] / g["total"], -g["owned"]))
    db.set_meta("gaps", {"_at": time.time(), "groups": groups})
    return len(groups)


# ------------------------------------------------------------------ coming soon

def build_upcoming(tmdb=None, step=lambda *a: None):
    """Unreleased movies and shows that fit your taste. Found two ways, through what the people your taste leans on
    have coming, and through Discover searches for your strongest keywords, genres and people, then scored against
    your taste like your picks, so what's kept is what you'd most likely want to see."""
    tmdb = tmdb or TMDB(config.TMDB_API_KEY)
    today = date.today()
    tomorrow = (today + timedelta(days=1)).isoformat()
    horizon = date(today.year + UPCOMING_YEARS, 12, 31).isoformat()
    found = {}   # key -> {"m": TMDB listing, "media", "people": [why]}

    def add(m, media, why=None):
        rd = m.get("release_date") or m.get("first_air_date") or ""
        if media not in ("movie", "tv") or not m.get("id") or not tomorrow <= rd <= horizon or m.get("adult"):
            return
        f = found.setdefault(key_for(media, m["id"]), {"m": m, "media": media, "people": []})
        if why and why not in f["people"]:
            f["people"].append(why)

    # 1. what the people your taste leans on have coming. Strongest first, movie and TV people in turn; look well
    #    past the 40 used, because a taste built on older films leans on people who have died.
    lists = [[(int(t["f"][2:]), t["label"]) for t in db.get_meta(f"taste_{media}", []) if t["f"][:2] in ("d:", "w:", "c:")]
             for media in ("movie", "tv")]
    candidates, seen = [], set()
    for i in range(max(map(len, lists), default=0)):
        for lst in lists:
            if i < len(lst) and lst[i][0] not in seen and len(candidates) < UPCOMING_LOOK:
                seen.add(lst[i][0])
                candidates.append(lst[i])
    step("Looking for upcoming releases", f"{len(candidates)} people")
    fetched = {}
    for pid, _ in candidates:
        v = db.get_meta(f"person_{pid}")
        if v and time.time() - v.get("_at", 0) < WEEK:
            fetched[pid] = v
    for pid, p in _parallel(lambda i: tmdb._get(f"/person/{i}", append_to_response="combined_credits"),
                            [p for p, _ in candidates if p not in fetched]).items():
        if p:
            p["_at"] = time.time()
            db.set_meta(f"person_{pid}", p)
            fetched[pid] = p
    people = [pid for pid, _ in candidates if fetched.get(pid) and not fetched[pid].get("deathday")][:UPCOMING_PEOPLE]
    for pid in people:
        p = fetched[pid]
        cc = p.get("combined_credits") or {}
        for role, lst in (("cast", cc.get("cast", [])), ("crew", cc.get("crew", []))):
            for m in lst:
                if role == "crew" and m.get("job") not in ("Director", "Screenplay", "Writer", "Creator", "Executive Producer"):
                    continue
                if role == "cast" and ((m.get("order") or 0) > 6 or (m.get("character") or "").lower() in ("self", "himself", "herself")):
                    continue   # small parts, and appearances as themselves in documentaries
                add(m, m.get("media_type"), f"{p.get('name')} ({m.get('character') or 'cast'})" if role == "cast"
                    else f"{m.get('job')}: {p.get('name')}")

    # 2. Discover: what's coming that matches your strongest keywords, genres and people, plus what's most anticipated
    step("Looking for upcoming releases", "searching by your taste")
    jobs = []
    for media in ("movie", "tv"):
        datef = "first_air_date" if media == "tv" else "primary_release_date"
        base = {"include_adult": "false", f"{datef}.gte": tomorrow, f"{datef}.lte": horizon, "sort_by": "popularity.desc"}
        try:
            gids = discover.genre_map(tmdb, media)[0]
        except Exception as e:
            log.warning("TMDB genres failed: %s", e)
            gids = {}
        jobs += [(media, {**base, "page": pg}) for pg in (1, 2)]
        jobs += [(media, {**base, **p}) for _, p in discover.streams(media, db.get_meta(f"taste_{media}", []), gids)[:UPCOMING_STREAMS]]
    for i, d in _parallel(lambda i: tmdb._get(f"/discover/{jobs[i][0]}", **jobs[i][1]), list(range(len(jobs)))).items():
        for m in (d or {}).get("results", []):
            add(m, jobs[i][0])

    # 3. leave out what you own, hid or have seen; read details for the rest (refreshed every couple of weeks,
    #    since cast, keywords and dates fill in as a release gets closer)
    gone = (set(db.library_map()) | {k for k, f in db.feedback().items() if f["action"] in ("dismissed", "seen")}
            | discover.already_wanted())   # in Radarr/Sonarr or requested: you have it coming already
    keys = [k for k in found if k not in gone]
    have = db.tmdb_get_many(keys)
    stale = time.time() - UPCOMING_REFRESH_DAYS * 86400
    todo = [k for k in keys if k not in have or have[k][1] < stale]
    step("Looking for upcoming releases", f"reading {len(todo)} titles")
    for k, d in _parallel(tmdb.details, todo, workers=max(1, config.TMDB_WORKERS)).items():
        if d:
            db.tmdb_put(k, d)
            db.commit()   # straight away, so the write lock is never held across a lookup
            have[k] = (d, time.time())

    # 4. score each against your taste, as picks are, and keep the best
    out = []
    for media in ("movie", "tv"):
        model = db.get_meta(f"model_{media}")
        scored = []
        for k in keys:
            f = found[k]
            if f["media"] != media:
                continue
            d = (have.get(k) or ({}, 0))[0]
            if model and d and not d.get("missing"):
                r = engine.score_item(model, k, d, 0.0, {}, {})
            else:   # no details, or no taste yet (first sync): keep it unscored
                r = {**_item(f["m"], media), "score": 0.0}
            rd = (d or {}).get("release_date") or f["m"].get("release_date") or f["m"].get("first_air_date") or ""
            if not tomorrow <= rd <= horizon:
                continue   # the date moved since it was found
            if not r.get("matches") and not f["people"]:
                continue   # nothing to say about why you'd want it
            r.update(date=rd, year=_year(rd), upcoming=True, people=f["people"],
                     score=round(r["score"] + UPCOMING_PEOPLE_NUDGE * min(2, len(f["people"])), 4))
            scored.append(r)
        scored.sort(key=lambda r: -r["score"])
        out += scored[:UPCOMING_KEEP[media]]
    db.set_meta("upcoming", {"_at": time.time(), "items": out})
    return len(out)


# ------------------------------------------------------------------ search & more like this

def search(q, media="all"):
    tmdb = TMDB(config.TMDB_API_KEY)
    d = tmdb._get("/search/multi", query=q, include_adult="false") or {}
    out, people = [], []
    for m in d.get("results", [])[:30]:
        mt = m.get("media_type")
        if mt in ("movie", "tv") and (media == "all" or media == mt):
            out.append(_item(m, mt))
        elif mt == "person" and m.get("profile_path"):
            people.append({"id": m["id"], "name": m.get("name"), "profile": m.get("profile_path"),
                           "known_for": m.get("known_for_department")})
    return out, people[:8]


def more_like(media, tmdb_id):
    """Titles like this one, scored against your taste."""
    from .discover import ensure_details
    tmdb = TMDB(config.TMDB_API_KEY)
    k = key_for(media, tmdb_id)
    base = ensure_details(tmdb, [k]).get(k) or {}
    extra = tmdb._get(f"/{media}/{tmdb_id}/recommendations", page=2) or {}
    cands = {}
    page2 = [{"id": key_for(media, m["id"]), "adult": m.get("adult")} for m in extra.get("results", [])]
    for lst, factor in ((base.get("recommendations", []), 1.0), (page2, 0.8), (base.get("similar", []), 0.5)):
        for rank, b in enumerate(lst):   # already internal keys (negative = TV)
            if b.get("adult"):
                continue
            ck = b["id"]
            cands[ck] = max(cands.get(ck, 0), factor * max(0.1, 1 - rank / 40))
    details = ensure_details(tmdb, list(cands))
    model = db.get_meta(f"model_{media}")
    imdb = db.imdb_get_many([d.get("imdb_id") for d in details.values()])
    owned, fb = db.library_map(), db.feedback()
    out = []
    for ck, w in cands.items():
        d = details.get(ck)
        if not d or d.get("missing") or not d.get("poster_path") or ck == k:
            continue
        f = fb.get(ck)
        if f and f["action"] == "dismissed":
            continue
        if model:
            r = engine.score_item(model, ck, d, 0.0, {}, imdb)
            r["score"] = round(0.5 * w + 0.5 * r["score"], 4)   # how alike they are, then how much it's your thing
        else:
            r = _item({"id": abs(ck), "title": d.get("title"), "release_date": d.get("release_date"),
                       "poster_path": d.get("poster_path"), "vote_average": d.get("vote_average")}, media)
            r["score"] = round(w, 4)
        r["owned"] = ck in owned
        r["seen"] = bool(f and f["action"] == "seen")
        out.append(r)
    out.sort(key=lambda r: -r["score"])
    return {"title": base.get("title"), "year": _year(base.get("release_date")), "poster_path": base.get("poster_path"),
            "backdrop_path": base.get("backdrop_path"), "items": out}
