"""Watch history from outside Plex: Letterboxd (export file + RSS feed) and Trakt.

Everything lands in the feedback table as action="seen" with an optional 0-10 rating and a
source, so it trains your taste and drops those titles from your picks.
"""
import csv
import io
import logging
import threading
import time
import xml.etree.ElementTree as ET
import re
import zipfile
from concurrent.futures import ThreadPoolExecutor

import requests

from . import config, db
from .clients import TMDB, UA, APIError, Plex, key_for

log = logging.getLogger("filmhound")
state = {"running": False, "source": None, "done": 0, "total": 0, "matched": 0, "message": None, "error": None}
_lock = threading.Lock()
KEEP = ("dismissed", "requested", "added")   # never overwrite these user decisions


def _record(entries, source):
    """entries: list of (key, title, rating_0_10_or_None). Returns count written."""
    fb = db.feedback()
    n = 0
    for key, title, rating in entries:
        cur = fb.get(key)
        if cur and (cur["action"] in KEEP or (cur["action"] == "seen" and cur.get("source") in (None, "filmhound"))):
            continue   # a choice you made in Filmhound wins over an import
        if cur and cur["action"] == "seen" and rating is None and cur.get("rating") is not None:
            continue   # don't drop a rating we already have
        if cur and cur["action"] == "seen" and cur.get("rating") == rating and cur.get("source") == source:
            continue   # nothing new: leave it, so History keeps the date it was first seen
        db.set_feedback(key, "seen", title, rating=rating, source=source, commit=False)
        n += 1
    db.commit()
    if n:
        db.bump_user()   # after the commit, so nothing re-reads the old rows in between
    return n


def _run(source, fn, *args):
    if not _lock.acquire(blocking=False):
        raise APIError("An import is already running")

    def go():
        state.update(running=True, source=source, done=0, total=0, matched=0, message=None, error=None)
        try:
            state["message"] = fn(*args)
        except Exception as e:
            log.exception("import failed")
            state["error"] = str(e)
        finally:
            state["running"] = False
            _lock.release()
            db.close()
            from .main import syncer   # re-score with the new history
            syncer.start(full=False)
    threading.Thread(target=go, daemon=True).start()


# ---------------------------------------------------------------- Letterboxd export

def _read_letterboxd(data, filename):
    """Return {(name, year): rating_or_None} from a Letterboxd export (.zip) or one of its CSVs."""
    files = {}
    if filename.lower().endswith(".zip") or data[:2] == b"PK":
        from .security import MAX_UNZIPPED, zip_read
        budget = [MAX_UNZIPPED]
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            # top-level CSVs only (the export also has deleted/ and orphaned/ folders)
            for n in sorted(z.namelist(), key=lambda n: n.count("/")):
                base = n.rsplit("/", 1)[-1]
                if base in ("watched.csv", "ratings.csv", "diary.csv") and base not in files:
                    files[base] = zip_read(z, n, budget).decode("utf-8-sig")
    else:
        files[filename.rsplit("/", 1)[-1] or "watched.csv"] = data.decode("utf-8-sig")
    if not files:
        raise APIError("That doesn't look like a Letterboxd export. Upload the .zip from letterboxd.com/settings/data.")
    out = {}
    for name in ("watched.csv", "diary.csv", "ratings.csv"):   # ratings last so they win
        if name not in files:
            continue
        for row in csv.DictReader(io.StringIO(files[name])):
            title, year = (row.get("Name") or "").strip(), (row.get("Year") or "").strip()
            if not title:
                continue
            k = (title, year)
            rating = row.get("Rating")
            r = float(rating) * 2 if rating not in (None, "") else None
            if r is not None or k not in out:
                out[k] = r if r is not None else out.get(k)
    # a generic CSV (single file) with Name/Year but none of the known names
    return out


def import_letterboxd_file(data, filename):
    films = _read_letterboxd(data, filename)
    if not films:
        raise APIError("No films found in that file")
    _run("letterboxd", _match_and_record, films)


def _match_and_record(films):
    tmdb = TMDB(config.TMDB_API_KEY)
    cache = db.get_meta("lb_match", {})
    state["total"] = len(films)
    todo = [(t, y) for t, y in films if cache.get(f"{t}|{y}") is None]   # new films, and ones not found last time
    failed = object()

    def find(ty):
        t, y = ty
        try:
            d = tmdb._get("/search/movie", query=t, **({"year": y} if y else {})) or {}
            res = d.get("results") or []
            if not res and y:
                res = (tmdb._get("/search/movie", query=t) or {}).get("results") or []
            return ty, res[0]["id"] if res else None
        except Exception:
            return ty, failed   # TMDB didn't answer: not the same as "no such film", so don't remember it

    with ThreadPoolExecutor(max_workers=config.TMDB_WORKERS) as ex:
        for (t, y), tid in ex.map(find, todo):
            if tid is not failed:
                cache[f"{t}|{y}"] = tid
            state["done"] += 1
    state["done"] = state["total"]
    db.set_meta("lb_match", cache)
    db.set_meta("lb_export_at", int(time.time()))
    entries = [(key_for("movie", cache[f"{t}|{y}"]), t, r) for (t, y), r in films.items() if cache.get(f"{t}|{y}")]
    state["matched"] = len(entries)
    n = _record(entries, "letterboxd")
    pushed, _ = push_ratings_to_plex()
    extra = f" Copied {pushed:,} ratings to Plex." if pushed else ""
    owned = {r["tmdb_id"] for r in db.library()}
    in_lib = sum(1 for k, _, _ in entries if k in owned)
    return (f"Imported {len(entries):,} of {len(films):,} films from Letterboxd ({n:,} new or updated, "
            f"{in_lib:,} of them are in your Plex library).{extra}")


# ---------------------------------------------------------------- Letterboxd RSS

NS = {"letterboxd": "https://letterboxd.com", "tmdb": "https://themoviedb.org"}


def letterboxd_rss(username):
    """Pull recent diary entries (ratings + TMDB ids) from a public Letterboxd profile."""
    if not re.fullmatch(r"[A-Za-z0-9_]{1,40}", username or ""):
        raise APIError("That isn't a valid Letterboxd username")
    r = requests.get(f"https://letterboxd.com/{username}/rss/", headers=UA, timeout=30)
    if r.status_code == 404:
        raise APIError(f"No Letterboxd profile named {username}")
    r.raise_for_status()
    if b"<!DOCTYPE" in r.content[:2048] or b"<!ENTITY" in r.content:   # a feed never needs these; refuse entity tricks
        raise APIError("Letterboxd sent an unexpected feed")
    root = ET.fromstring(r.content)
    entries = []
    for item in root.iter("item"):
        tid = item.findtext("tmdb:movieId", namespaces=NS)
        if not tid:
            continue
        rating = item.findtext("letterboxd:memberRating", namespaces=NS)
        title = item.findtext("letterboxd:filmTitle", namespaces=NS)
        entries.append((key_for("movie", int(tid)), title, float(rating) * 2 if rating else None))
    n = _record(entries, "letterboxd")
    return len(entries), n


# ---------------------------------------------------------------- Trakt

def _trakt(path):
    r = requests.get(f"https://api.trakt.tv{path}", timeout=60, headers={
        **UA, "Content-Type": "application/json", "trakt-api-version": "2", "trakt-api-key": config.TRAKT_CLIENT_ID})
    if r.status_code in (401, 403):
        raise APIError("Trakt refused: check the Client ID, and that the profile is public")
    if r.status_code == 404:
        raise APIError("Trakt user not found")
    r.raise_for_status()
    return r.json()


def trakt_sync():
    if not (config.TRAKT_CLIENT_ID and config.TRAKT_USERNAME):
        raise APIError("Add your Trakt Client ID and username in Settings")
    u = config.TRAKT_USERNAME
    entries = {}
    for media, kind in (("movie", "movies"), ("tv", "shows")):
        obj = "movie" if media == "movie" else "show"
        for w in _trakt(f"/users/{u}/watched/{kind}"):
            m = w.get(obj) or {}
            tid = (m.get("ids") or {}).get("tmdb")
            if tid:
                entries[key_for(media, tid)] = (m.get("title"), None)
        for rt in _trakt(f"/users/{u}/ratings/{kind}"):
            m = rt.get(obj) or {}
            tid = (m.get("ids") or {}).get("tmdb")
            if tid:
                entries[key_for(media, tid)] = (m.get("title"), float(rt.get("rating") or 0) or None)
    n = _record([(k, t, r) for k, (t, r) in entries.items()], "trakt")
    return len(entries), n


def import_trakt():
    def go():
        total, n = trakt_sync()
        pushed, _ = push_ratings_to_plex()
        state.update(total=total, done=total, matched=total)
        return f"Synced {total:,} titles from Trakt ({n:,} new or updated)." + (f" Copied {pushed:,} ratings to Plex." if pushed else "")
    _run("trakt", go)


def letterboxd_export_done():
    """Whether a full Letterboxd export has been imported. lb_match only exists after one, so this also
    recognises exports uploaded before lb_export_at was recorded."""
    return bool(db.get_meta("lb_export_at") or db.get_meta("lb_match"))


def import_letterboxd_rss():
    def go():
        total, n = letterboxd_rss(config.LETTERBOXD_USERNAME)
        pushed, _ = push_ratings_to_plex()
        state.update(total=total, done=total, matched=total)
        msg = f"Read {total} recent diary entries from Letterboxd ({n} new or updated)." + (f" Copied {pushed} ratings to Plex." if pushed else "")
        if not letterboxd_export_done():
            msg += " The diary feed only has your latest entries. Upload your Letterboxd export once to bring in your full history and every rating."
        return msg
    if not config.LETTERBOXD_USERNAME:
        raise APIError("Add your Letterboxd username in Settings")
    _run("letterboxd", go)


# ---------------------------------------------------------------- ratings -> Plex

def push_ratings_to_plex(mode=None, sources=("letterboxd", "trakt")):
    """Copy imported ratings onto the matching titles in your Plex library.
    mode "fill" only rates titles that have no Plex rating; "overwrite" also replaces different ones."""
    mode = mode or config.RATINGS_TO_PLEX
    if mode not in ("fill", "overwrite"):
        return 0, 0
    lib = {r["tmdb_id"]: r for r in db.library()}
    todo = []
    for k, f in db.feedback().items():
        r = lib.get(k)
        if not r or f["action"] != "seen" or not f.get("rating") or f.get("source") not in sources:
            continue
        cur = r.get("user_rating")
        if cur is None or (mode == "overwrite" and abs(cur - f["rating"]) > 0.01):
            todo.append((k, r["plex_key"], f["rating"], r["title"]))
    if not todo:
        return 0, 0
    plex = Plex(config.PLEX_URL, config.PLEX_TOKEN)
    done = failed = 0
    state["total"] = len(todo)
    for k, plex_key, rating, title in todo:
        try:
            plex.rate(plex_key, rating)
            db.set_library_rating(k, rating)
            done += 1
        except Exception as e:
            log.warning("could not rate %s in Plex: %s", title, e)
            failed += 1
        state["done"] = done + failed
    return done, failed


def push_to_plex_job(mode):
    def go():
        done, failed = push_ratings_to_plex(mode)
        if not done and not failed:
            return "Nothing to copy: every matched title already has that rating in Plex."
        return f"Copied {done:,} ratings to Plex" + (f" ({failed} failed)" if failed else "") + "."
    _run("plex", go)
