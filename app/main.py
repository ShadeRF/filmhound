import json
import logging
import os
import threading
import time
from datetime import datetime

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import requests

from . import security, auth, config, db, discover, engine, filters, imports

from .clients import TMDB, UA, APIError, Plex, Radarr, Seerr, Sonarr, key_for
from . import sync as sync_mod
from .sync import Syncer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
security.install_log_redaction()
log = logging.getLogger("filmhound")

STATIC = os.path.join(os.path.dirname(__file__), "static")
@asynccontextmanager
async def lifespan(_app):
    security.install_log_redaction()   # uvicorn's own log handlers exist by now
    startup()
    yield


app = FastAPI(title="Filmhound", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.middleware("http")(auth.middleware)
app.middleware("http")(security.middleware)   # outermost: runs before sign-in
app.add_middleware(GZipMiddleware, minimum_size=2048)   # pick lists are big, repetitive JSON


@app.exception_handler(HTTPException)
async def _http_error(_request, exc):
    """Error messages often quote what a service said, which can include the address we called: strip keys from them."""
    detail = security.redact(exc.detail) if isinstance(exc.detail, str) else exc.detail
    return JSONResponse({"detail": detail}, status_code=exc.status_code, headers=exc.headers)


@app.exception_handler(APIError)
@app.exception_handler(requests.exceptions.RequestException)
async def _service_error(_request, exc):
    """A service (Plex, TMDB and the rest) failed somewhere a route didn't expect: say so plainly instead of a bare server error."""
    return JSONResponse({"detail": sync_mod.friendly_error(exc)}, status_code=502)


@app.middleware("http")
async def cache_headers(request, call_next):
    resp = await call_next(request)
    p = request.url.path
    if p.startswith("/static/") and request.url.query:      # versioned assets (?v=N)
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif p.startswith("/static/icon") or p.endswith((".png", ".webmanifest")):
        resp.headers["Cache-Control"] = "public, max-age=86400"
    elif p == "/" or p.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def _fast(data):
    """Send data that is already plain JSON as it is. Returning it normally makes FastAPI walk every item of a big list first."""
    return Response(json.dumps(data, separators=(",", ":"), ensure_ascii=False, allow_nan=False), media_type="application/json")


syncer = Syncer()

_rescore_timer = None
_rescore_lock = threading.Lock()


def schedule_rescore(delay=45):
    """Re-score picks shortly after the last rating/hide/restore, so there's no button to remember.
    Repeated changes push the timer back; a running sync is waited out."""
    global _rescore_timer

    def fire():
        if syncer.state["running"]:
            schedule_rescore(30)
        elif config.configured():
            syncer.start(full=False)
    with _rescore_lock:
        if _rescore_timer:
            _rescore_timer.cancel()
        _rescore_timer = threading.Timer(delay, fire)
        _rescore_timer.daemon = True
        _rescore_timer.start()


def _scheduler():
    last_day = None
    while True:
        now = datetime.now()
        if config.SYNC_HOUR >= 0 and now.hour == config.SYNC_HOUR and last_day != now.date() and config.configured():
            if syncer.start(full=True):   # busy (a re-score is running): try again next minute
                last_day = now.date()
        time.sleep(60)


def startup():
    db.init()
    if config.configured() and not db.get_meta("last_sync"):
        syncer.start(full=True)
    threading.Thread(target=_scheduler, daemon=True).start()


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.get("/favicon.ico")
def favicon():
    return FileResponse(os.path.join(STATIC, "favicon-32.png"), media_type="image/png")


@app.get("/api/status")
def status():
    n_movies, n_tv = db.library_counts()
    return {
        **syncer.state,
        "last_sync": db.get_meta("last_sync"),
        "movie_count": n_movies,
        "show_count": n_tv,
        "seerr": bool(config.SEERR_URL and config.SEERR_API_KEY),
        "radarr": bool(config.RADARR_URL and config.RADARR_API_KEY),
        "sonarr": bool(config.SONARR_URL and config.SONARR_API_KEY),
        "configured": config.configured(),
        "version": __import__("app").__version__,
        "plex_machine": db.get_meta("plex_machine"),
        "plex_url": config.PLEX_URL,
        "radarr_url": config.RADARR_URL, "sonarr_url": config.SONARR_URL, "seerr_url": config.SEERR_URL,
        "letterboxd": config.LETTERBOXD_USERNAME, "letterboxd_export": imports.letterboxd_export_done(),
        "letterboxd_export_at": db.get_meta("lb_export_at"), "trakt": bool(config.TRAKT_CLIENT_ID and config.TRAKT_USERNAME),
        "my_provider_ids": config.MY_PROVIDERS, "region": config.WATCH_REGION, "user": config.AUTH_USERNAME,
        "my_providers": len(config.MY_PROVIDERS),
        "auth": auth.enabled(),
        "import": imports.state,
    }


@app.post("/api/sync")
def sync(full: bool = True):
    if not config.configured():
        raise HTTPException(400, "Finish setup first: Plex URL, Plex token and TMDB key are needed (Settings).")
    return {"started": syncer.start(full=full)}


@app.post("/api/recompute")
def recompute():
    """Re-score using your latest feedback without re-reading Plex."""
    return {"started": syncer.start(full=False)}


def _visible(media):
    def build():
        fb = db.feedback()
        return [r for r in db.recs(media) if r["key"] not in fb]
    return list(db.cached(("visible", media), build))


def _csv(v):
    return [x for x in (v or "").split(",") if x]


@app.get("/api/recs")
def recs(media: str = "movie", offset: int = 0, limit: int = 60, f: filters.Filters = Depends(filters.parse)):
    rows = filters.apply(_visible(media), f)
    return _fast({"total": len(rows), "items": rows[offset:offset + limit]})


@app.get("/api/facets")
def facets(media: str = "movie"):
    return db.cached(("facets", media), lambda: _facets(media))


def _facets(media):
    genres, langs, nets, years = {}, {}, {}, []
    for r in _visible(media):
        for g in r["genres"]:
            genres[g] = genres.get(g, 0) + 1
        if r["language"]:
            langs[r["language"]] = langs.get(r["language"], 0) + 1
        for n in r.get("networks") or []:
            nets[n] = nets.get(n, 0) + 1
        if r["year"]:
            years.append(r["year"])
    top = lambda d: [{"name": k, "count": d[k]} for k in sorted(d, key=lambda k: -d[k])]
    return {"genres": top(genres), "languages": top(langs), "networks": top(nets)[:60],
            "year_min": min(years) if years else 1920, "year_max": max(years) if years else 2026}


class Feedback(BaseModel):
    tmdb_id: int
    media: str = "movie"
    action: str
    title: str | None = None
    rating: float | None = None   # 0-10 (stars x2), for action "seen"


@app.post("/api/feedback")
def feedback(fb: Feedback):
    if fb.action not in ("dismissed", "liked", "disliked", "seen"):
        raise HTTPException(400, "action must be dismissed, seen, liked or disliked")
    k = key_for(fb.media, fb.tmdb_id)
    if fb.action == "seen":
        _rate(k, fb.rating, fb.title)
    else:
        db.set_feedback(k, fb.action, fb.title)
    schedule_rescore()
    return {"ok": True}


def _rate(key, rating, title=None):
    """A rating for a title in Plex goes back to Plex; anything else is remembered here."""
    lib = db.library_map().get(key)
    if lib and rating:
        try:
            Plex(config.PLEX_URL, config.PLEX_TOKEN).rate(lib["plex_key"], rating)
        except Exception as e:
            raise HTTPException(502, f"Couldn't save the rating to Plex: {e}")
        db.set_library_rating(key, rating)
        return "plex"
    db.set_feedback(key, "seen", title, rating=rating, source="filmhound")
    return "filmhound"


@app.get("/api/feedback")
def feedback_list():
    rows = sorted(db.feedback().values(), key=lambda r: -r["at"])
    cache = db.tmdb_brief([r["tmdb_id"] for r in rows])
    out = []
    for r in rows:
        k = r["tmdb_id"]
        d = cache.get(k, {})
        rd = d.get("release_date") or ""
        out.append({**r, "key": k, "media": "tv" if k < 0 else "movie", "tmdb_id": abs(k),
                    "rating": r.get("rating"), "source": r.get("source"),
                    "title": r.get("title") or d.get("title"), "poster_path": d.get("poster_path"),
                    "year": int(rd[:4]) if rd[:4].isdigit() else None})
    return _fast(out)


@app.delete("/api/feedback/{key}")
def feedback_undo(key: int):
    db.delete_feedback(key)
    schedule_rescore()
    return {"ok": True}


class RequestBody(BaseModel):
    tmdb_id: int
    media: str = "movie"
    title: str | None = None


@app.post("/api/request")
def request_title(body: RequestBody):
    try:
        Seerr(config.SEERR_URL, config.SEERR_API_KEY).request(body.media, body.tmdb_id)
    except APIError as e:
        raise HTTPException(502, str(e))
    except Exception as e:
        raise HTTPException(502, f"Could not reach Seerr: {e}")
    db.set_feedback(key_for(body.media, body.tmdb_id), "requested", body.title)
    return {"ok": True}


def _arr(app_name):
    if app_name == "radarr":
        return Radarr(config.RADARR_URL, config.RADARR_API_KEY)
    if app_name == "sonarr":
        return Sonarr(config.SONARR_URL, config.SONARR_API_KEY)
    raise HTTPException(404, "unknown app")


@app.get("/api/arr/{app_name}/options")
def arr_options(app_name: str):
    try:
        return _arr(app_name).options()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Could not reach {app_name.title()}: {e}")


class ArrAdd(BaseModel):
    tmdb_id: int
    title: str | None = None
    tvdb_id: int | None = None
    profile_id: int
    root: str
    search: bool = True
    monitor: str = "all"


@app.post("/api/arr/{app_name}/add")
def arr_add(app_name: str, body: ArrAdd):
    try:
        _arr(app_name).add(body.tmdb_id, body.profile_id, body.root, body.search,
                           monitor=body.monitor, tvdb_id=body.tvdb_id)
    except HTTPException:
        raise
    except APIError as e:
        raise HTTPException(502, str(e))
    except Exception as e:
        raise HTTPException(502, f"Could not reach {app_name.title()}: {e}")
    media = "tv" if app_name == "sonarr" else "movie"
    db.set_feedback(key_for(media, body.tmdb_id), "added", body.title)
    return {"ok": True}


# ======================= settings =======================

@app.get("/api/settings")
def get_settings():
    lib = db.library()
    n_tv = sum(1 for r in lib if r["tmdb_id"] < 0)
    c = db.conn()
    try:
        size = os.path.getsize(db.path())
    except OSError:
        size = 0
    return {
        "values": config.public(),
        "sources": config.sources(),
        "configured": config.configured(),
        "stats": {
            "movies": len(lib) - n_tv, "shows": n_tv,
            "cached": c.execute("SELECT COUNT(*) FROM tmdb").fetchone()[0],
            "imdb": db.imdb_count(),
            "feedback": c.execute("SELECT COUNT(*) FROM feedback").fetchone()[0],
            "db_bytes": size,
            "last_sync": db.get_meta("last_sync"),
            "imdb_at": db.get_meta("imdb_at"),
        },
    }


def _resolve(values):
    """Merge submitted values over current ones; masked secrets mean 'unchanged'."""
    out = {}
    for name in config.FIELDS:
        if name in values and not config.is_masked(values[name]):
            out[name] = config.coerce(name, values[name])
        else:
            out[name] = getattr(config, name)
    return out


@app.put("/api/settings")
def put_settings(values: dict):
    _guard_secrets(values)
    updates = {k: v for k, v in values.items()
               if k in config.FIELDS and not config.is_masked(v) and not k.startswith("AUTH_")}
    try:
        changed = config.save(updates)
    except ValueError as e:
        raise HTTPException(400, f"Invalid value: {e}")
    return {"changed": changed, "configured": config.configured(), **get_settings()}


@app.post("/api/settings/adopt-env")
def adopt_env():
    """Copy every value that currently comes from environment variables into settings.json."""
    src = config.sources()
    config.save({k: getattr(config, k) for k, v in src.items() if v == "env" and not k.startswith("AUTH_")})
    return get_settings()


def _ping(url, path, key=None, header="X-Api-Key"):
    r = requests.get(url + path, headers={**UA, "Accept": "application/json", **({header: key} if key else {})}, timeout=10)
    if r.status_code in (401, 403):
        raise APIError("The API key was rejected")
    r.raise_for_status()
    return r.json()


SECRET_PAIRS = (("PLEX_URL", "PLEX_TOKEN"), ("SEERR_URL", "SEERR_API_KEY"), ("RADARR_URL", "RADARR_API_KEY"), ("SONARR_URL", "SONARR_API_KEY"),)


def _guard_secrets(values):
    bad = security.secret_host_guard(values, {k: getattr(config, k) for k in config.FIELDS}, SECRET_PAIRS)
    if bad:
        what = bad[1].replace("_", " ").title().replace("Api", "API")
        raise HTTPException(400, f"You changed the address, so enter the {what} again. Saved keys are only sent to the address they were saved with.")


@app.post("/api/settings/test/{service}")
def test_service(service: str, values: dict):
    _guard_secrets(values)
    v = _resolve(values)
    try:
        if service == "plex":
            if not v["PLEX_URL"] or not v["PLEX_TOKEN"]:
                raise APIError("Enter the Plex URL and token")
            plex = Plex(v["PLEX_URL"], v["PLEX_TOKEN"])
            info = plex._get("/")
            libs = [{"title": d.get("title"), "type": d.get("type")}
                    for d in plex._get("/library/sections").get("Directory", []) if d.get("type") in ("movie", "show")]
            found = f"{len(libs)} librar{'y' if len(libs) == 1 else 'ies'}" if libs else "no movie or TV libraries"
            return {"ok": True, "message": f"Connected to {info.get('friendlyName', 'Plex')} v{str(info.get('version', '?')).split('-')[0]} · {found}",
                    "libraries": libs}
        if service == "tmdb":
            if not v["TMDB_API_KEY"]:
                raise APIError("Enter a TMDB key")
            TMDB(v["TMDB_API_KEY"])._get("/configuration")
            return {"ok": True, "message": "Connected to TMDB"}
        if service == "seerr":
            if not v["SEERR_URL"] or not v["SEERR_API_KEY"]:
                raise APIError("Enter the Seerr URL and API key")
            st = _ping(v["SEERR_URL"], "/api/v1/status")
            me = _ping(v["SEERR_URL"], "/api/v1/auth/me", v["SEERR_API_KEY"])
            return {"ok": True, "message": f"Connected to Seerr v{st.get('version', '?')} · signed in as {me.get('displayName') or me.get('email') or 'API user'}"}
        if service in ("radarr", "sonarr"):
            url, key = v[f"{service.upper()}_URL"], v[f"{service.upper()}_API_KEY"]
            if not url or not key:
                raise APIError(f"Enter the {service.title()} URL and API key")
            st = _ping(url, "/api/v3/system/status", key)
            arr = (Radarr if service == "radarr" else Sonarr)(url, key)
            opts = arr.options()
            return {"ok": True, "message": f"Connected to {st.get('appName', service.title())} v{st.get('version', '?')} · "
                                           f"{len(opts['profiles'])} quality profiles, {len(opts['roots'])} root folders"}
        raise HTTPException(404, "unknown service")
    except HTTPException:
        raise
    except APIError as e:
        return {"ok": False, "message": security.redact(e)}
    except requests.exceptions.ConnectionError:
        return {"ok": False, "message": "Couldn't connect. Check the address and port, and that the app is running."}
    except requests.exceptions.Timeout:
        return {"ok": False, "message": "Timed out. Is the address right?"}
    except Exception as e:
        return {"ok": False, "message": security.redact(f"Failed: {e}")}


@app.post("/api/maintenance/{action}")
def maintenance(action: str):
    c = db.conn()
    if action == "clear-feedback":
        c.execute("DELETE FROM feedback")
        # the imported Letterboxd history is gone too, so Settings asks for the export again
        c.execute("DELETE FROM meta WHERE key IN ('lb_export_at', 'lb_match')")
        schedule_rescore(5)
    elif action == "clear-cache":
        if syncer.state["running"]:
            raise HTTPException(409, "Wait for the current sync to finish")
        c.execute("DELETE FROM tmdb")
    elif action == "refresh-imdb":
        db.set_meta("imdb_at", 0)
    else:
        raise HTTPException(404, "unknown action")
    c.commit()
    db.bump()   # after the commit, so nothing re-reads the old rows in between
    return {"ok": True}


# ======================= endless feed =======================

@app.get("/api/more")
def more(media: str = "movie", cursor: str = "", f: filters.Filters = Depends(filters.parse)):
    if not config.TMDB_API_KEY or f.seed or f.match:
        return {"items": [], "cursor": cursor, "done": True}
    try:
        items, nxt, done = discover.more(media, f, cursor)
    except APIError as e:
        raise HTTPException(502, str(e))
    return {"items": items, "cursor": nxt, "done": done}


def _title_detail(key):
    have = db.tmdb_get_many([key])
    d = have[key][0] if key in have else None
    if not d or (not d.get("missing") and (d.get("v", 1) < 2 or not any("profile" in c for c in d.get("cast", [])[:1]))):
        try:
            fresh = TMDB(config.TMDB_API_KEY).details(key)
        except APIError as e:
            if not d:
                raise HTTPException(502, str(e))
            fresh = None
        if fresh and (not fresh.get("missing") or not d):
            d = fresh
            db.tmdb_put(key, d)
            db.commit()
    if d.get("missing"):
        raise HTTPException(404, "Not found on TMDB")
    return d


@app.get("/api/title")
def title(media: str, tmdb_id: int):
    """Full details for any title: cast with photos, crew, streaming, trailer, and a score if we have a model."""
    k = key_for(media, tmdb_id)
    d = _title_detail(k)
    imdb = db.imdb_get_many([d.get("imdb_id")])
    model = db.get_meta(f"model_{media}")
    if model:
        out = engine.score_item(model, k, d, 0.0, {}, imdb)
        rec = db.recs_map(media).get(k) or next((r for r in db.libpicks(media) if r["key"] == k), None)
        if rec:   # keep the real score + reasons from the full pipeline
            out.update({x: rec[x] for x in ("score", "components", "because", "seed_count", "matches") if x in rec})
    else:
        im = imdb.get(d.get("imdb_id") or "")
        rd = d.get("release_date") or ""
        out = {"key": k, "tmdb_id": tmdb_id, "media": media, "title": d.get("title"), "year": int(rd[:4]) if len(rd) >= 4 else None,
               "overview": d.get("overview"), "poster_path": d.get("poster_path"), "runtime": d.get("runtime"),
               "genres": [g["name"] for g in d.get("genres", [])], "imdb_id": d.get("imdb_id"),
               "imdb_rating": im[0] if im else None, "imdb_votes": im[1] if im else None,
               "tmdb_rating": d.get("vote_average"), "tmdb_votes": d.get("vote_count"), "because": [], "matches": [],
               "providers": d.get("providers"), "trailer": d.get("trailer"), "backdrop_path": d.get("backdrop_path")}
    out["cast"] = d.get("cast", [])
    out["crew"] = d.get("crew", [])
    lib = db.library_map().get(k)
    out["owned"] = bool(lib)
    if lib:
        out.update(plex_key=lib["plex_key"], user_rating=lib["user_rating"], view_count=lib["view_count"])
    fb = db.feedback().get(k)
    out["feedback"] = fb["action"] if fb else None
    out["my_rating"] = (lib or {}).get("user_rating") or (fb or {}).get("rating")
    out["watchlist"] = k in db.watchlist()
    out["noseed"] = k in db.noseed()
    return out


@app.get("/api/person/{person_id}")
def person(person_id: int):
    """An actor's or filmmaker's work, marked with what you own, have seen, or would like."""
    tmdb = TMDB(config.TMDB_API_KEY)
    cache_key = f"person_{person_id}"
    p = db.get_meta(cache_key)
    if not p or time.time() - p.get("_at", 0) > 7 * 86400:
        p = tmdb._get(f"/person/{person_id}", append_to_response="combined_credits")
        if not p:
            raise HTTPException(404, "Person not found")
        p["_at"] = time.time()
        db.set_meta(cache_key, p)
    owned = db.library_map()
    fb = db.feedback()

    def build_scored():
        out = {}
        for media in ("movie", "tv"):
            for r in db.recs(media) + db.libpicks(media):
                out.setdefault(r["key"], r)
        return out
    scored = db.cached("scored_all", build_scored)
    works = {}
    cc = p.get("combined_credits") or {}
    for role, items in (("cast", cc.get("cast", [])), ("crew", cc.get("crew", []))):
        for m in items:
            media = m.get("media_type")
            if media not in ("movie", "tv") or m.get("adult"):
                continue
            if media == "tv" and role == "cast" and (m.get("episode_count") or 99) < 3 and not m.get("character"):
                continue
            if media == "tv" and any(x in (m.get("character") or "").lower() for x in ("self", "himself", "herself", "host")):
                continue   # talk-show and documentary appearances
            k = key_for(media, m["id"])
            rd = m.get("release_date") or m.get("first_air_date") or ""
            w = works.setdefault(k, {
                "key": k, "tmdb_id": m["id"], "media": media, "title": m.get("title") or m.get("name"),
                "year": int(rd[:4]) if len(rd) >= 4 else None, "poster_path": m.get("poster_path"),
                "overview": m.get("overview"), "tmdb_rating": m.get("vote_average"), "tmdb_votes": m.get("vote_count"),
                "popularity": m.get("popularity"), "genres": [], "roles": [], "released": bool(rd) and rd <= time.strftime("%Y-%m-%d"),
            })
            role_txt = (m.get("character") or "") if role == "cast" else m.get("job", "")
            if role_txt and role_txt not in w["roles"]:
                w["roles"].append(role_txt)
    out = []
    for k, w in works.items():
        lib, f = owned.get(k), fb.get(k)
        s = scored.get(k)
        w.update(owned=bool(lib), watched=bool(lib and (lib["view_count"] or lib["user_rating"])) or bool(f and f["action"] == "seen"),
                 hidden=bool(f and f["action"] == "dismissed"), requested=bool(f and f["action"] in ("requested", "added")),
                 score=s["score"] if s else None, because=s.get("because", []) if s else [], matches=s.get("matches", []) if s else [])
        if s:
            w.update(genres=s.get("genres", []), imdb_rating=s.get("imdb_rating"), year=s.get("year") or w["year"])
        out.append(w)
    out.sort(key=lambda w: (-(w["popularity"] or 0)))
    known = p.get("known_for_department")
    return {"id": person_id, "name": p.get("name"), "profile": p.get("profile_path"), "known_for": known,
            "biography": p.get("biography"), "birthday": p.get("birthday"), "deathday": p.get("deathday"),
            "place_of_birth": p.get("place_of_birth"), "imdb_id": p.get("imdb_id"), "works": out}


# ======================= your library (owned, unwatched) =======================

def _visible_lib(media):
    fb = db.feedback()
    lib = db.library_map()
    out = []
    for r in db.libpicks(media):
        cur = lib.get(r["key"])
        if r["key"] in fb or not cur or cur["view_count"] or cur["user_rating"]:
            continue   # watched or rated since the last sync
        out.append(r)
    return out


@app.get("/api/library")
def library(media: str = "movie", offset: int = 0, limit: int = 60, f: filters.Filters = Depends(filters.parse)):
    everything = _visible_lib(media)
    rows = filters.apply(everything, f)
    # "all" is before filters: the Plex collection is made from the whole list
    return _fast({"total": len(rows), "all": len(everything), "items": rows[offset:offset + limit]})


class CollectionBody(BaseModel):
    media: str = "movie"
    limit: int = 50


@app.post("/api/plex/collection")
def plex_collection(body: CollectionBody):
    rows = _visible_lib(body.media)[:max(1, min(body.limit, 500))]
    by_section = {}
    for r in rows:
        if r.get("section") and r.get("plex_key"):
            by_section.setdefault(r["section"], []).append(r["plex_key"])
    if not by_section:
        raise HTTPException(400, "Nothing to add yet. Run a sync first.")
    plex = Plex(config.PLEX_URL, config.PLEX_TOKEN)
    try:
        for section, keys in by_section.items():
            plex.push_collection(section, config.PLEX_COLLECTION, keys, 2 if body.media == "tv" else 1)
    except Exception as e:
        raise HTTPException(502, f"Plex refused: {e}")
    return {"ok": True, "count": sum(len(v) for v in by_section.values()), "name": config.PLEX_COLLECTION}


# ======================= quick rating session =======================

@app.get("/api/rate/queue")
def rate_queue(media: str = "movie", mode: str = "library", limit: int = 40, skip: str = ""):
    """library: things you watched in Plex but never rated (most watched first).
    picks: your top recommendations, in case you've already seen them elsewhere."""
    skip_keys = {int(x) for x in skip.split(",") if x.strip().lstrip("-").isdigit()}
    fb = db.feedback()
    tv = media == "tv"
    if mode == "picks":
        rows = [r for r in _visible(media) if r["key"] not in skip_keys][:limit]
        return {"items": [{k: r.get(k) for k in ("key", "tmdb_id", "media", "title", "year", "poster_path", "overview",
                                                 "genres", "directors", "creators", "score")} for r in rows]}
    lib = [r for r in db.library() if (r["tmdb_id"] < 0) == tv and r["view_count"] and not r["user_rating"]
           and r["tmdb_id"] not in skip_keys and r["tmdb_id"] not in fb]
    lib.sort(key=lambda r: (-(r["view_count"] or 0) if not tv else 0, -(r["last_viewed_at"] or 0)))
    lib = lib[:limit]
    det = db.tmdb_get_many([r["tmdb_id"] for r in lib])
    out = []
    for r in lib:
        d = det.get(r["tmdb_id"], ({}, 0))[0]
        rd = d.get("release_date") or ""
        out.append({"key": r["tmdb_id"], "tmdb_id": abs(r["tmdb_id"]), "media": media, "title": r["title"],
                    "year": r["year"] or (int(rd[:4]) if len(rd) >= 4 else None), "poster_path": d.get("poster_path"),
                    "overview": d.get("overview"), "genres": [g["name"] for g in d.get("genres", [])],
                    "directors": [x["name"] for x in d.get("crew", []) if x["job"] == "Director"],
                    "creators": [x["name"] for x in d.get("crew", []) if x["job"] == "Creator"],
                    "view_count": r["view_count"], "leaf_count": r["leaf_count"], "last_viewed_at": r["last_viewed_at"]})
    remaining = sum(1 for r in db.library() if (r["tmdb_id"] < 0) == tv and r["view_count"] and not r["user_rating"]
                    and r["tmdb_id"] not in fb)
    return {"items": out, "remaining": remaining}


class RateBody(BaseModel):
    key: int
    rating: float | None = None   # 1-10; None = "seen it, no rating"
    title: str | None = None


@app.post("/api/rate")
def rate(body: RateBody):
    where = _rate(body.key, body.rating, body.title)
    schedule_rescore(90)   # rating sessions come in bursts
    return {"ok": True, "saved_to": where}


# ======================= streaming services =======================

def _cached_meta(key, days, fn):
    v = db.get_meta(key)
    if v and time.time() - v.get("_at", 0) < days * 86400:
        return v["data"]
    data = fn()
    db.set_meta(key, {"_at": time.time(), "data": data})
    return data


@app.get("/api/providers")
def providers(region: str = ""):
    region = region or config.WATCH_REGION
    if not config.TMDB_API_KEY:
        return []

    def fetch():
        tmdb = TMDB(config.TMDB_API_KEY)
        seen = {}
        for media in ("movie", "tv"):
            for p in (tmdb._get(f"/watch/providers/{media}", watch_region=region) or {}).get("results", []):
                prio = (p.get("display_priorities") or {}).get(region, p.get("display_priority", 999))
                cur = seen.get(p["provider_id"])
                if not cur or prio < cur["priority"]:
                    seen[p["provider_id"]] = {"id": str(p["provider_id"]), "name": p["provider_name"], "logo": p.get("logo_path"), "priority": prio}
        return sorted(seen.values(), key=lambda p: p["priority"])
    return _cached_meta(f"providers_{region}", 7, fetch)


@app.get("/api/regions")
def regions():
    if not config.TMDB_API_KEY:
        return []
    return _cached_meta("regions", 30, lambda: sorted(
        [{"code": r["iso_3166_1"], "name": r["english_name"]} for r in
         (TMDB(config.TMDB_API_KEY)._get("/watch/providers/regions") or {}).get("results", [])], key=lambda r: r["name"]))


# ======================= home rows =======================

def _seed_index(media):
    """seed key -> [(pick, share)], strongest first. Built once per data change."""
    def build():
        idx = {}
        for r in _visible(media):
            for b in r.get("because") or []:
                if isinstance(b, dict) and b.get("tmdb_id") is not None:
                    idx.setdefault(b["tmdb_id"], []).append((r, b.get("share") or 0))
        for v in idx.values():
            v.sort(key=lambda rs: -rs[1] * rs[0]["score"])
        return idx
    return db.cached(("seedidx", media), build)


@app.get("/api/home")
def home(media: str = "movie"):
    rows_all = _visible(media)
    if not rows_all:
        return _fast({"rows": []})
    year_now = int(time.strftime("%Y"))
    used = {}

    def take(items, n=24):
        return [r for r in items][:n]

    rows = [{"id": "top", "title": "Top picks for you", "items": take(rows_all), "see_all": {}}]
    wl = [r for r in watchlist_get(media)["items"] if not r["owned"]]
    if wl:
        rows.append({"id": "watchlist", "title": "Your watchlist", "items": wl[:24], "see_all": {"view": "watchlist"}})

    # Because you watched: your most recent strongly-liked titles that drive several picks
    library = db.library()
    lib = sorted([r for r in library if (r["tmdb_id"] < 0) == (media == "tv") and (r["view_count"] or r["user_rating"])
                  and (not r["user_rating"] or r["user_rating"] >= 6)], key=lambda r: -(r["last_viewed_at"] or 0))
    seen_fb = sorted([(k, v) for k, v in db.feedback().items() if v["action"] == "seen" and (k < 0) == (media == "tv")
                      and (v.get("rating") or 0) >= 7], key=lambda kv: -kv[1]["at"])
    owned_new = sorted([r for r in library if (r["tmdb_id"] < 0) == (media == "tv") and not r["view_count"] and not r["user_rating"]],
                       key=lambda r: -(r["added_at"] or 0))
    seeds = [(r["tmdb_id"], r["title"], "watched") for r in lib[:40]] + [(k, v["title"], "watched") for k, v in seen_fb[:20]]
    owned_seeds = [(r["tmdb_id"], r["title"], "have") for r in owned_new[:60]]
    import random
    rnd = random.Random(int(time.time() // 3600))   # rotate which titles get a row, hourly
    rnd.shuffle(owned_seeds)
    made = {"watched": 0, "have": 0}
    index = _seed_index(media)
    for sid, title, kind in seeds + owned_seeds:
        if made[kind] >= (3 if kind == "watched" else 2):
            continue
        items = [r for r, _ in index.get(sid, [])]
        if len(items) >= 6:
            label = f"Because you watched {title}" if kind == "watched" else f"Because you have {title}"
            rows.append({"id": f"seed{sid}", "title": label, "items": take(items), "see_all": {"seed": sid, "seed_title": title, "seed_label": label}})
            made[kind] += 1

    if config.MY_PROVIDERS:
        mine = [r for r in rows_all if filters.streams_on_mine(r)]
        if len(mine) >= 6:
            rows.append({"id": "mine", "title": "Streaming on your services", "items": take(mine), "see_all": {"streaming": "mine"}})

    lib_rows = _visible_lib(media)
    if len(lib_rows) >= 6:
        rows.append({"id": "lib", "title": "Already in your library, not watched yet", "items": take(lib_rows), "see_all": {"tab": "library"}})

    up = upcoming(media)["items"]
    if len(up) >= 2:
        rows.append({"id": "upcoming", "title": "Coming soon for you", "items": up[:24], "see_all": {"view": "upcoming"}})
    if media == "movie":
        groups = _gaps()["groups"]
        g_items, seen_g = [], set()
        for rnd_i in range(4):   # round-robin across collections/directors keeps the row varied
            for g in groups:
                avail = [i for i in g["items"] if not i.get("upcoming") and i["key"] not in seen_g]
                if len(avail) > 0 and len(g_items) < 24:
                    seen_g.add(avail[0]["key"])
                    g_items.append({**avail[0], "gap": f"{g['owned']} of {g['total']} · {g['name']}"})
        if len(g_items) >= 4:
            rows.append({"id": "gaps", "title": "Finish what you started", "subtitle": "Missing pieces of collections and directors you own",
                         "items": g_items[:24], "see_all": {"view": "gaps"}})

    taste = db.get_meta(f"taste_{media}", [])
    kw = 0
    for t in taste:
        if not t["f"].startswith(("k:", "d:")):
            continue
        items = [r for r in rows_all if t["label"] in (r.get("matches") or [])]
        if len(items) >= 8:
            label = t["label"]
            title = f"More {label}" if t["f"].startswith("k:") else label
            rows.append({"id": f"m{t['f']}", "title": title[0].upper() + title[1:], "items": take(items), "see_all": {"match": label}})
            kw += 1
        if kw >= 3:
            break

    gems = sorted([r for r in rows_all if (r.get("tmdb_votes") or 0) < 1500 and r["score"] >= rows_all[min(len(rows_all) - 1, 150)]["score"]],
                  key=lambda r: -r["score"])
    if len(gems) >= 6:
        rows.append({"id": "gems", "title": "Hidden gems", "subtitle": "Strong matches most people haven't seen", "items": take(gems), "see_all": {"sort": "obscure"}})
    new = [r for r in rows_all if (r["year"] or 0) >= year_now - 2]
    if len(new) >= 6:
        rows.append({"id": "new", "title": "Recent releases for you", "items": take(new), "see_all": {"year_min": year_now - 2}})
    classics = [r for r in rows_all if r["year"] and r["year"] < 1975]
    if len(classics) >= 6:
        rows.append({"id": "classics", "title": "Classics for you", "items": take(classics), "see_all": {"year_max": 1974}})
    return _fast({"rows": rows})


# ======================= imports =======================

@app.post("/api/import/letterboxd")
async def import_letterboxd(request: Request, filename: str = "export.zip"):
    data = await security.read_upload(request, 50 * 1024 * 1024, "That file is too big for a Letterboxd export")
    if not data:
        raise HTTPException(400, "Empty upload")
    try:
        imports.import_letterboxd_file(data, filename)
    except APIError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"Couldn't read that file: {e}")
    return {"started": True}


@app.post("/api/import/{source}")
def import_source(source: str):
    try:
        if source == "trakt":
            imports.import_trakt()
        elif source == "letterboxd-rss":
            imports.import_letterboxd_rss()
        elif source in ("plex-fill", "plex-overwrite"):
            imports.push_to_plex_job(source.split("-")[1])
        else:
            raise HTTPException(404, "unknown source")
    except APIError as e:
        raise HTTPException(400, str(e))
    return {"started": True}


@app.get("/api/import")
def import_status():
    return imports.state


# ======================= watchlist, don't-learn, search, gaps, coming soon =======================

class KeyBody(BaseModel):
    key: int
    title: str = ""
    on: bool = True


def _decorate(items):
    """Owned / watchlist / feedback flags for list results that didn't come from the scored pipeline."""
    owned, wl, fb = db.library_map(), db.watchlist(), db.feedback()
    for it in items:
        it["owned"] = it["key"] in owned
        it["watchlist"] = it["key"] in wl
        f = fb.get(it["key"])
        it["feedback"] = f["action"] if f else None
    return items


@app.post("/api/watchlist")
def watchlist_set(body: KeyBody):
    db.set_watchlist(body.key, body.title, body.on)
    schedule_rescore()
    return {"ok": True, "watchlist": body.on}


@app.get("/api/watchlist")
def watchlist_get(media: str = "all"):
    from .discover import ensure_details
    wl = db.watchlist()
    keys = [k for k in wl if media == "all" or (k < 0) == (media == "tv")]
    det = ensure_details(TMDB(config.TMDB_API_KEY), keys) if config.TMDB_API_KEY else {}
    recs = {**db.recs_map("movie"), **db.recs_map("tv")}
    out = []
    for k in keys:
        d = det.get(k) or {}
        rd = d.get("release_date") or ""
        r = recs.get(k) or {}
        out.append({"key": k, "tmdb_id": abs(k), "media": "tv" if k < 0 else "movie", "title": d.get("title") or wl[k]["title"],
                    "year": int(rd[:4]) if len(rd) >= 4 else None, "poster_path": d.get("poster_path"),
                    "genres": [g["name"] for g in d.get("genres", [])], "score": r.get("score"), "because": r.get("because", []),
                    "matches": r.get("matches", []), "tmdb_rating": d.get("vote_average"), "added": wl[k]["at"]})
    return {"items": _decorate(out)}


@app.post("/api/noseed")
def noseed_set(body: KeyBody):
    db.set_noseed(body.key, body.title, body.on)
    schedule_rescore()
    return {"ok": True, "noseed": body.on}


@app.get("/api/noseed")
def noseed_get():
    return {"items": [{"key": k, "title": v["title"], "media": "tv" if k < 0 else "movie", "at": v["at"]} for k, v in db.noseed().items()]}


@app.get("/api/noseed/genres")
def noseed_genres():
    """Every genre on the titles you own, with how many titles carry it. Read from stored TMDB details only."""
    return {"genres": db.owned_genres()}


@app.get("/api/plex/sections")
def plex_sections():
    return {"sections": [{"key": k, **v} for k, v in db.get_meta("plex_sections", {}).items() if v.get("type") in ("movie", "show")]}


@app.get("/api/search")
def search_all(q: str, media: str = "all"):
    from . import extras
    if not q.strip():
        return {"items": [], "people": []}
    try:
        items, people = extras.search(q.strip(), media)
    except Exception as e:
        raise HTTPException(502, sync_mod.friendly_error(e))
    return {"items": _decorate(items), "people": people}


@app.get("/api/like/{media}/{tmdb_id}")
def more_like(media: str, tmdb_id: int):
    from . import extras
    try:
        res = extras.more_like(media, tmdb_id)
    except Exception as e:
        raise HTTPException(502, sync_mod.friendly_error(e))
    _decorate(res["items"])
    return res


@app.get("/api/gaps")
def gaps(kind: str = "all"):
    return _fast(_gaps(kind))


def _gaps(kind="all"):
    g = db.get_meta("gaps") or {"groups": []}
    groups = [x for x in g["groups"] if kind == "all" or x["type"] == kind]
    wanted = discover.already_wanted()   # checked when shown, so adding one in Radarr hides it straight away
    out = []
    for x in groups:
        _decorate(x["items"])
        keep, got, requested, hidden = [], 0, 0, 0   # say where the titles that aren't shown went
        for i in x["items"]:
            if i["owned"]:
                got += 1
            elif i["feedback"] == "dismissed":
                hidden += 1
            elif i["key"] in wanted:
                requested += 1
            else:
                keep.append(i)
        if keep:
            out.append({**x, "items": keep, "owned": x["owned"] + got, "requested": requested, "hidden": hidden})
    return {"at": g.get("_at"), "groups": out}


@app.get("/api/upcoming")
def upcoming(media: str = "all"):
    u = db.get_meta("upcoming") or {"items": []}
    items = [i for i in u["items"] if media == "all" or i["media"] == media]
    wanted = discover.already_wanted()   # checked when shown, so adding one in Radarr hides it straight away
    items = [i for i in _decorate(items) if i["feedback"] != "dismissed" and i["key"] not in wanted]
    return {"at": u.get("_at"), "items": items}


# ======================= auth =======================

@app.get("/login")
def login_page():
    return auth.login_page()


class LoginBody(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def login(body: LoginBody, request: Request, response: Response):
    if not auth.enabled():
        return {"ok": True}
    ip = security.client_ip(request)
    if security.login_blocked(ip):
        raise HTTPException(429, "Too many wrong passwords. Wait 15 minutes and try again.")
    if body.username != config.AUTH_USERNAME or not auth.check_password(body.password, config.AUTH_PASSWORD_HASH):
        security.login_failed(ip)
        time.sleep(1)
        raise HTTPException(401, "Wrong username or password")
    security.login_ok(ip)
    auth.set_cookie(response, request, body.username)
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(auth.COOKIE)
    return {"ok": True}


class AuthBody(BaseModel):
    username: str = ""
    password: str = ""
    enabled: bool = True


@app.post("/api/auth")
def set_auth(body: AuthBody, request: Request, response: Response):
    """Turn sign-in on (with a new username/password) or off. Already behind the middleware."""
    if not body.enabled:
        config.save({"AUTH_USERNAME": "", "AUTH_PASSWORD_HASH": ""})
        return {"ok": True, "auth": False}
    if not body.username.strip() or len(body.password) < 8:
        raise HTTPException(400, "Pick a username and a password of at least 8 characters")
    config.save({"AUTH_USERNAME": body.username.strip(), "AUTH_PASSWORD_HASH": auth.hash_password(body.password)})
    auth.set_cookie(response, request, body.username.strip())
    return {"ok": True, "auth": True}


@app.get("/api/health")
def health():
    from . import __version__
    return {"ok": True, "version": __version__}
