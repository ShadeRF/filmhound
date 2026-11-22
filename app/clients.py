"""Thin API clients for Plex, TMDB, IMDb datasets, Seerr, Radarr and Sonarr.

Internal keys: movies use their TMDB id, TV shows use the NEGATIVE TMDB id, so both
share one id space everywhere (cache, library, feedback, recs) without collisions.
"""
import csv
import gzip
import io
import logging
import re
import time

import requests

log = logging.getLogger("filmhound")
from . import __version__

UA = {"User-Agent": f"Filmhound/{__version__} (self-hosted recommendations)"}


class APIError(Exception):
    pass


def key_for(media, tmdb_id):
    return -int(tmdb_id) if media == "tv" else int(tmdb_id)


def split_key(key):
    return ("tv", -key) if key < 0 else ("movie", key)


# ======================= Plex =======================

class Plex:
    def __init__(self, url, token, libraries=None):
        if not url or not token:
            raise APIError("PLEX_URL and PLEX_TOKEN must be set")
        self.url = url
        self.libraries = libraries or []
        self.s = requests.Session()
        self.s.headers.update({**UA, "Accept": "application/json", "X-Plex-Token": token,
                               "X-Plex-Client-Identifier": "filmhound-recs", "X-Plex-Product": "Filmhound"})

    def _get(self, path, **params):
        r = self.s.get(self.url + path, params=params, timeout=60)
        if r.status_code == 401:
            raise APIError("Plex rejected the token (401)")
        r.raise_for_status()
        return r.json().get("MediaContainer", {})

    def sections(self, kind):
        secs = [d for d in self._get("/library/sections").get("Directory", []) if d.get("type") == kind]
        if self.libraries:
            wanted = {n.lower() for n in self.libraries}
            secs = [d for d in secs if d.get("title", "").lower() in wanted]
        return secs

    def _items(self, kind, plex_type):
        for sec in self.sections(kind):
            start, size = 0, 500
            while True:
                mc = self._get(f"/library/sections/{sec['key']}/all", type=plex_type, includeGuids=1,
                               **{"X-Plex-Container-Start": start, "X-Plex-Container-Size": size})
                items = mc.get("Metadata", [])
                for it in items:
                    it["_section"] = sec["key"]
                    yield it
                total = mc.get("totalSize", mc.get("size", 0))
                start += len(items)
                if not items or start >= total:
                    break

    def movies(self):
        for m in self._items("movie", 1):
            yield {
                "media": "movie",
                "plex_key": m.get("ratingKey"),
                "title": m.get("title"),
                "year": m.get("year"),
                "view_count": m.get("viewCount", 0) or 0,
                "leaf_count": None,
                "last_viewed_at": m.get("lastViewedAt"),
                "user_rating": m.get("userRating"),  # 0-10 (stars x2)
                "added_at": m.get("addedAt"),
                "section": m.get("_section"),
                **parse_plex_guids(m),
            }

    def shows(self):
        for m in self._items("show", 2):
            yield {
                "media": "tv",
                "plex_key": m.get("ratingKey"),
                "title": m.get("title"),
                "year": m.get("year"),
                "view_count": m.get("viewedLeafCount", 0) or 0,   # episodes watched
                "leaf_count": m.get("leafCount", 0) or 0,         # episodes in library
                "last_viewed_at": m.get("lastViewedAt"),
                "user_rating": m.get("userRating"),
                "added_at": m.get("addedAt"),
                "section": m.get("_section"),
                **parse_plex_guids(m),
            }

    def machine_id(self):
        return self._get("/").get("machineIdentifier")

    def rate(self, rating_key, rating):
        """Set a user rating (0-10, stars x2) on a Plex item."""
        r = self.s.put(self.url + "/:/rate", params={"key": rating_key, "identifier": "com.plexapp.plugins.library",
                                                     "rating": rating}, timeout=30)
        r.raise_for_status()

    def push_collection(self, section, title, rating_keys, plex_type):
        """Create or replace a regular collection in a library section."""
        machine = self.machine_id()
        for c in self._get(f"/library/sections/{section}/collections").get("Metadata", []) or []:
            if c.get("title") == title:
                self.s.delete(f"{self.url}/library/collections/{c['ratingKey']}", timeout=30).raise_for_status()
        if not rating_keys:
            return
        uri = f"server://{machine}/com.plexapp.plugins.library/library/metadata/{','.join(map(str, rating_keys))}"
        r = self.s.post(self.url + "/library/collections", params={"type": plex_type, "title": title, "smart": 0,
                                                                   "sectionId": section, "uri": uri}, timeout=60)
        r.raise_for_status()


def parse_plex_guids(m):
    tmdb = imdb = tvdb = None
    for g in m.get("Guid", []) or []:
        gid = g.get("id", "")
        if gid.startswith("tmdb://"):
            tmdb = _int(gid[7:])
        elif gid.startswith("imdb://"):
            imdb = gid[7:]
        elif gid.startswith("tvdb://"):
            tvdb = _int(gid[7:])
    legacy = m.get("guid", "") or ""
    if tmdb is None:
        mt = re.search(r"themoviedb://(\d+)", legacy)
        if mt:
            tmdb = int(mt.group(1))
    if imdb is None:
        mi = re.search(r"(tt\d+)", legacy)
        if mi:
            imdb = mi.group(1)
    if tvdb is None:
        mv = re.search(r"thetvdb://(\d+)", legacy)
        if mv:
            tvdb = int(mv.group(1))
    return {"tmdb_id": tmdb, "imdb_id": imdb, "tvdb_id": tvdb}


def _int(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


# ======================= TMDB =======================

class TMDB:
    BASE = "https://api.themoviedb.org/3"

    def __init__(self, key):
        if not key:
            raise APIError("TMDB_API_KEY must be set")
        self.s = requests.Session()
        self.s.headers.update(UA)
        self.params = {}
        if key.startswith("eyJ"):
            self.s.headers["Authorization"] = f"Bearer {key}"
        else:
            self.params = {"api_key": key}

    def _get(self, path, **params):
        for attempt in range(5):
            r = self.s.get(self.BASE + path, params={**self.params, **params}, timeout=30)
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", 2)) + attempt)
                continue
            if r.status_code == 404:
                return None
            if r.status_code == 401:
                raise APIError("TMDB rejected the API key (401)")
            if r.status_code >= 500:
                time.sleep(1 + attempt)
                continue
            r.raise_for_status()
            return r.json()
        raise APIError(f"TMDB request kept failing: {path}")

    def find(self, external_id, source, media):
        d = self._get(f"/find/{external_id}", external_source=source) or {}
        res = d.get("tv_results" if media == "tv" else "movie_results") or []
        return res[0]["id"] if res else None

    def details(self, key):
        """Fetch by internal key (negative = TV)."""
        media, tid = split_key(key)
        d = self._get(f"/{media}/{tid}",
                      append_to_response="keywords,credits,recommendations,similar,external_ids,watch/providers,videos")
        if not d:
            return {"id": key, "missing": True}
        return trim(d, media)


def _brief(m, media):
    return {
        "id": key_for(media, m["id"]),
        "title": m.get("title") or m.get("name"),
        "release_date": m.get("release_date") or m.get("first_air_date") or "",
        "vote_average": m.get("vote_average") or 0,
        "vote_count": m.get("vote_count") or 0,
        "popularity": m.get("popularity") or 0,
        "adult": m.get("adult", False),
    }


def _providers(d):
    """Streaming/rent/buy availability for the configured region (from JustWatch via TMDB)."""
    from . import config
    reg = ((d.get("watch/providers") or {}).get("results") or {}).get(config.WATCH_REGION) or {}
    out = {"region": config.WATCH_REGION, "link": reg.get("link")}
    for kind in ("flatrate", "free", "ads", "rent", "buy"):
        out[kind] = [{"id": p["provider_id"], "name": p["provider_name"], "logo": p.get("logo_path")}
                     for p in sorted(reg.get(kind, []), key=lambda p: p.get("display_priority", 99))[:8]]
    return out


def _trailer(d):
    vids = [v for v in (d.get("videos") or {}).get("results", []) if v.get("site") == "YouTube"]
    vids.sort(key=lambda v: (v.get("type") != "Trailer", not v.get("official"), v.get("iso_639_1") != "en"))
    return vids[0]["key"] if vids else None


CREW_JOBS = ("Director", "Screenplay", "Writer", "Director of Photography", "Original Music Composer")


def trim(d, media):
    """Keep only what scoring and the UI need, so the cache stays small."""
    credits = d.get("credits") or {}
    crew = [{"id": c["id"], "name": c["name"], "job": c["job"], "profile": c.get("profile_path")}
            for c in credits.get("crew", []) if c.get("job") in CREW_JOBS]
    if media == "tv":
        crew = [{"id": c["id"], "name": c["name"], "job": "Creator", "profile": c.get("profile_path")}
                for c in d.get("created_by", [])] + [c for c in crew if c["job"] != "Director"]
    cast = [{"id": c["id"], "name": c["name"], "character": c.get("character") or "", "profile": c.get("profile_path")}
            for c in credits.get("cast", [])[:14]]
    kw = d.get("keywords") or {}
    keywords = kw.get("keywords") or kw.get("results") or []
    coll = d.get("belongs_to_collection")
    ext = d.get("external_ids") or {}
    runtime = d.get("runtime")
    if media == "tv":
        ert = d.get("episode_run_time") or []
        runtime = ert[0] if ert else None
    out = {
        "id": key_for(media, d["id"]),
        "tmdb_id": d["id"],
        "media": media,
        "title": d.get("title") or d.get("name"),
        "release_date": d.get("release_date") or d.get("first_air_date") or "",
        "overview": d.get("overview") or "",
        "poster_path": d.get("poster_path"),
        "backdrop_path": d.get("backdrop_path"),
        "runtime": runtime,
        "genres": [{"id": g["id"], "name": g["name"]} for g in d.get("genres", [])],
        "original_language": d.get("original_language"),
        "vote_average": d.get("vote_average") or 0,
        "vote_count": d.get("vote_count") or 0,
        "popularity": d.get("popularity") or 0,
        "adult": d.get("adult", False),
        "status": d.get("status"),
        "imdb_id": d.get("imdb_id") or ext.get("imdb_id"),
        "tvdb_id": ext.get("tvdb_id"),
        "collection": {"id": coll["id"], "name": coll["name"]} if coll else None,
        "networks": [{"id": n["id"], "name": n["name"]} for n in d.get("networks", [])[:2]],
        "seasons": d.get("number_of_seasons"),
        "last_air_date": d.get("last_air_date"),
        "keywords": [{"id": k["id"], "name": k["name"]} for k in keywords],
        "crew": crew,
        "cast": cast,
        "providers": _providers(d),
        "trailer": _trailer(d),
        "v": 2,
        "recommendations": [_brief(m, media) for m in (d.get("recommendations") or {}).get("results", [])],
        "similar": [_brief(m, media) for m in (d.get("similar") or {}).get("results", [])],
    }
    return out


# ======================= IMDb datasets =======================

IMDB_RATINGS_URL = "https://datasets.imdbws.com/title.ratings.tsv.gz"


def imdb_ratings_rows():
    """Download IMDb's official ratings dataset and yield (tconst, rating, votes)."""
    r = requests.get(IMDB_RATINGS_URL, headers=UA, timeout=300)
    r.raise_for_status()
    with gzip.open(io.BytesIO(r.content), "rt", encoding="utf-8") as f:
        reader = csv.reader(f, delimiter="\t")
        next(reader, None)
        for row in reader:
            if len(row) == 3:
                try:
                    yield row[0], float(row[1]), int(row[2])
                except ValueError:
                    continue


# ======================= Seerr =======================

class Seerr:
    def __init__(self, url, key):
        self.enabled = bool(url and key)
        self.url = url
        self.s = requests.Session()
        self.s.headers.update({**UA, "X-Api-Key": key, "Accept": "application/json"})

    def _paged(self, path, extra=None):
        out, skip = [], 0
        while True:
            r = self.s.get(f"{self.url}/api/v1{path}", params={"take": 100, "skip": skip, **(extra or {})}, timeout=30)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            d = r.json()
            res = d.get("results", [])
            out.extend(res)
            total = (d.get("pageInfo") or {}).get("results", len(out))
            skip += len(res)
            if not res or skip >= total:
                return out

    def blocklisted_keys(self):
        if not self.enabled:
            return set()
        for path in ("/blocklist", "/blacklist"):  # renamed in late 2025
            res = self._paged(path)
            if res is not None:
                return {key_for(i.get("mediaType"), i["tmdbId"]) for i in res
                        if i.get("mediaType") in ("movie", "tv") and i.get("tmdbId")}
        return set()

    def requested_keys(self):
        if not self.enabled:
            return set()
        res = self._paged("/request", {"filter": "all"}) or []
        return {key_for(i.get("type"), (i.get("media") or {}).get("tmdbId")) for i in res
                if i.get("type") in ("movie", "tv") and (i.get("media") or {}).get("tmdbId")}

    def request(self, media, tmdb_id):
        if not self.enabled:
            raise APIError("Seerr is not configured")
        body = {"mediaType": media, "mediaId": int(tmdb_id)}
        if media == "tv":
            body["seasons"] = "all"
        r = self.s.post(f"{self.url}/api/v1/request", json=body, timeout=30)
        if r.status_code >= 400:
            try:
                msg = r.json().get("message", r.text)
            except ValueError:
                msg = r.text
            raise APIError(f"Seerr refused the request: {msg}")
        return r.json()


# ======================= Radarr / Sonarr =======================

class _Arr:
    name = "arr"

    def __init__(self, url, key):
        self.enabled = bool(url and key)
        self.url = url
        self.s = requests.Session()
        self.s.headers.update({**UA, "X-Api-Key": key, "Accept": "application/json"})

    def _get(self, path, **params):
        r = self.s.get(f"{self.url}/api/v3{path}", params=params, timeout=60)
        if r.status_code == 401:
            raise APIError(f"{self.name} rejected the API key (401)")
        r.raise_for_status()
        return r.json()

    def _post(self, path, body):
        r = self.s.post(f"{self.url}/api/v3{path}", json=body, timeout=60)
        if r.status_code >= 400:
            try:
                d = r.json()
                msg = d[0].get("errorMessage") if isinstance(d, list) and d else d.get("message", r.text)
            except ValueError:
                msg = r.text
            raise APIError(f"{self.name} refused: {msg}")
        return r.json()

    def options(self):
        profiles = [{"id": p["id"], "name": p["name"]} for p in self._get("/qualityprofile")]
        roots = [{"path": f["path"], "free": f.get("freeSpace")} for f in self._get("/rootfolder")]
        return {"profiles": profiles, "roots": roots}


class Radarr(_Arr):
    name = "Radarr"

    def keys(self):
        if not self.enabled:
            return set()
        return {m["tmdbId"] for m in self._get("/movie") if m.get("tmdbId")}

    def add(self, tmdb_id, profile_id, root, search=True, **_):
        if not self.enabled:
            raise APIError("Radarr is not configured")
        found = self._get("/movie/lookup/tmdb", tmdbId=int(tmdb_id))
        movie = found[0] if isinstance(found, list) and found else found
        if not movie or not movie.get("tmdbId"):
            raise APIError("Radarr couldn't find that movie")
        if movie.get("id"):
            raise APIError("That movie is already in Radarr")
        movie.update({
            "qualityProfileId": int(profile_id),
            "rootFolderPath": root,
            "monitored": True,
            "minimumAvailability": "released",
            "addOptions": {"searchForMovie": bool(search), "monitor": "movieOnly"},
        })
        return self._post("/movie", movie)


class Sonarr(_Arr):
    name = "Sonarr"

    def keys(self):
        """Return TV keys (negative TMDB ids) and tvdb ids of series already in Sonarr."""
        if not self.enabled:
            return set(), set()
        series = self._get("/series")
        return ({-s["tmdbId"] for s in series if s.get("tmdbId")},
                {s["tvdbId"] for s in series if s.get("tvdbId")})

    def options(self):
        out = super().options()
        try:  # Sonarr v3 only; v4 dropped language profiles
            out["languages"] = [{"id": p["id"], "name": p["name"]} for p in self._get("/languageprofile")]
        except Exception:
            out["languages"] = []
        return out

    def add(self, tmdb_id, profile_id, root, search=True, monitor="all", tvdb_id=None, **_):
        if not self.enabled:
            raise APIError("Sonarr is not configured")
        if not tvdb_id:
            raise APIError("No TVDB id for this show, so Sonarr can't look it up")
        found = self._get("/series/lookup", term=f"tvdb:{int(tvdb_id)}")
        series = found[0] if isinstance(found, list) and found else None
        if not series:
            raise APIError("Sonarr couldn't find that show")
        if series.get("id"):
            raise APIError("That show is already in Sonarr")
        series.update({
            "qualityProfileId": int(profile_id),
            "rootFolderPath": root,
            "monitored": True,
            "seasonFolder": True,
            "addOptions": {"monitor": monitor, "searchForMissingEpisodes": bool(search),
                           "searchForCutoffUnmetEpisodes": False},
        })
        langs = self.options().get("languages") or []
        if langs:
            series["languageProfileId"] = langs[0]["id"]
        return self._post("/series", series)
