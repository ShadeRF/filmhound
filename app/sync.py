"""Orchestrates syncing data in and (re)computing recommendations."""
import json
import logging
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import config, db, engine
from .clients import TMDB, Plex, Radarr, Seerr, Sonarr, imdb_ratings_rows, key_for

log = logging.getLogger("filmhound")

MEDIA = {
    "movie": {"label": "movies", "min_votes": lambda: config.MIN_VOTES,
              "pool": lambda: config.CANDIDATE_POOL, "max": lambda: config.MAX_RECS},
    "tv": {"label": "shows", "min_votes": lambda: config.MIN_VOTES_TV,
           "pool": lambda: config.CANDIDATE_POOL_TV, "max": lambda: config.MAX_RECS_TV},
}


def friendly_error(e):
    from .security import redact
    return redact(_friendly_error(e))


def _friendly_error(e):
    """Turn network exceptions into something a person can act on."""
    import requests
    from urllib.parse import urlparse
    url = getattr(getattr(e, "request", None), "url", "") or ""
    host = urlparse(url).netloc
    name = ("Plex" if config.PLEX_URL and host in config.PLEX_URL else "TMDB" if "themoviedb" in host
            else "Seerr" if config.SEERR_URL and host in config.SEERR_URL else host or "a server")
    if isinstance(e, requests.exceptions.ConnectionError):
        return f"Couldn't reach {name}{f' at {host}' if host and name != host else ''}. Check the address in Settings and that it's running."
    if isinstance(e, requests.exceptions.Timeout):
        return f"{name} took too long to answer. Try again in a minute."
    if isinstance(e, requests.exceptions.HTTPError) and e.response is not None:
        return f"{name} answered with an error ({e.response.status_code})."
    return str(e)


class Syncer:
    def __init__(self, plex_factory=None, tmdb_factory=None, seerr_factory=None, imdb_rows=None,
                 radarr_factory=None, sonarr_factory=None):
        self.plex_factory = plex_factory or (lambda: Plex(config.PLEX_URL, config.PLEX_TOKEN, config.PLEX_LIBRARIES))
        self.tmdb_factory = tmdb_factory or (lambda: TMDB(config.TMDB_API_KEY))
        self.seerr_factory = seerr_factory or (lambda: Seerr(config.SEERR_URL, config.SEERR_API_KEY))
        self.imdb_rows = imdb_rows or imdb_ratings_rows
        self.radarr_factory = radarr_factory or (lambda: Radarr(config.RADARR_URL, config.RADARR_API_KEY))
        self.sonarr_factory = sonarr_factory or (lambda: Sonarr(config.SONARR_URL, config.SONARR_API_KEY))
        self.lock = threading.Lock()
        self.state = {"running": False, "step": "idle", "progress": None, "error": None}

    # ------------------------------------------------------------ helpers
    def _step(self, step, progress=None):
        self.state.update(step=step, progress=progress)
        log.info("%s %s", step, progress or "")

    def _ensure_details(self, tmdb, keys, label, upgrade=False):
        """Fetch TMDB details for keys that are missing or stale; return {key: details}.
        upgrade=True also refetches entries from before streaming/trailer data was cached."""
        keys = list({k for k in keys if k})
        have = db.tmdb_get_many(keys)
        cutoff = time.time() - config.TMDB_CACHE_DAYS * 86400

        def stale(k):
            if k not in have or have[k][1] < cutoff:
                return True
            d = have[k][0]
            if d.get("missing"):
                return False
            if k < 0 and "tvdb_id" not in d:
                return True
            return upgrade and (d.get("v", 1) < 2 or (d.get("providers") or {}).get("region") != config.WATCH_REGION)
        todo = [k for k in keys if stale(k)]
        done = 0
        if todo:
            with ThreadPoolExecutor(max_workers=config.TMDB_WORKERS) as ex:
                futs = {ex.submit(tmdb.details, k): k for k in todo}
                for f in as_completed(futs):
                    k = futs[f]
                    try:
                        d = f.result()
                        db.tmdb_put(k, d)
                        db.commit()   # straight away: an open write would block ratings and hides until the next lookup returns
                        have[k] = (d, time.time())
                    except Exception as e:  # keep going on single failures
                        log.warning("TMDB %s failed: %s", k, e)
                    done += 1
                    if done % 50 == 0:
                        self._step(label, f"{done}/{len(todo)}")
            db.commit()
        return {k: v[0] for k, v in have.items()}

    # ------------------------------------------------------------ public
    @staticmethod
    def _forget(*names):
        """A service that's no longer connected: drop the lists it gave us, so its titles stop being left out of picks."""
        for name in names:
            if db.get_meta(name):
                db.set_meta(name, [])

    def start(self, full=True):
        if self.state["running"]:
            return False
        t = threading.Thread(target=self.run, args=(full,), daemon=True)
        t.start()
        return True

    def run(self, full=True):
        if not self.lock.acquire(blocking=False):
            return
        self.state.update(running=True, error=None)
        try:
            if full:
                self.sync_sources()
            for media in ("movie", "tv"):
                self.compute(media)
            if full:
                from . import extras
                for name, fn in (("collection gaps", extras.build_gaps), ("coming soon", extras.build_upcoming)):
                    try:
                        fn(self.tmdb_factory(), self._step)
                    except Exception as e:   # extras never fail the whole sync
                        log.warning("%s failed: %s", name, e)
            db.set_meta("last_sync" if full else "last_compute", int(time.time()))
            self._step("done")
        except Exception as e:
            msg = friendly_error(e)
            if msg != str(e):
                log.error("sync failed: %s (%s)", msg, e)
            else:
                log.error("sync failed: %s\n%s", e, traceback.format_exc())
            self.state.update(error=msg, step="failed")
        finally:
            self.state["running"] = False
            self.lock.release()
            if threading.current_thread() is not threading.main_thread():
                db.close()

    def sync_sources(self):
        tmdb = self.tmdb_factory()
        plex = self.plex_factory()

        rows = []
        for media, fetch in (("movie", plex.movies), ("tv", plex.shows)):
            self._step(f"Reading Plex {MEDIA[media]['label']}")
            for m in fetch():
                if not m["tmdb_id"]:
                    for ext, src in ((m.get("imdb_id"), "imdb_id"), (m.get("tvdb_id"), "tvdb_id")):
                        if ext and not m["tmdb_id"]:
                            try:
                                m["tmdb_id"] = tmdb.find(ext, src, media)
                            except Exception as e:
                                log.warning("could not resolve %s: %s", ext, e)
                if m["tmdb_id"]:
                    m["key"] = key_for(media, m["tmdb_id"])
                    rows.append(m)
        dedup = {}
        for m in rows:  # same title in two libraries / editions: keep the most-watched copy
            cur = dedup.get(m["key"])
            if not cur or (m["view_count"] or 0) > (cur["view_count"] or 0) or (m["user_rating"] and not cur["user_rating"]):
                dedup[m["key"]] = m
        db.replace_library(list(dedup.values()))
        try:
            db.set_meta("plex_sections", {d["key"]: {"title": d.get("title"), "type": d.get("type")}
                                          for d in plex._get("/library/sections").get("Directory", [])})
        except Exception as e:
            log.warning("could not read Plex libraries: %s", e)
        try:
            db.set_meta("plex_machine", plex.machine_id())
        except Exception as e:
            log.warning("could not read Plex machine id: %s", e)
        n_tv = sum(1 for k in dedup if k < 0)
        self._step("Plex library read", f"{len(dedup) - n_tv} movies, {n_tv} shows")

        if config.IMDB_ENABLED:
            last = db.get_meta("imdb_at", 0)
            if time.time() - last > 7 * 86400:
                self._step("Downloading IMDb ratings")
                try:
                    db.imdb_replace(self.imdb_rows())
                    db.set_meta("imdb_at", int(time.time()))
                except Exception as e:
                    log.warning("IMDb download failed (continuing with TMDB ratings): %s", e)

        try:
            seerr = self.seerr_factory()
            if seerr.enabled:
                self._step("Reading Seerr blocklist and requests")
                db.set_meta("seerr_block", sorted(seerr.blocklisted_keys()))
                db.set_meta("seerr_requested", sorted(seerr.requested_keys()))
            else:
                self._forget("seerr_block", "seerr_requested")
        except Exception as e:
            log.warning("Seerr read failed (continuing): %s", e)

        try:
            radarr = self.radarr_factory()
            if radarr.enabled:
                self._step("Reading Radarr")
                db.set_meta("radarr_ids", sorted(radarr.keys()))
            else:
                self._forget("radarr_ids")
        except Exception as e:
            log.warning("Radarr read failed (continuing): %s", e)

        from . import imports
        if config.LETTERBOXD_USERNAME:
            try:
                self._step("Reading Letterboxd diary")
                imports.letterboxd_rss(config.LETTERBOXD_USERNAME)
            except Exception as e:
                log.warning("Letterboxd RSS failed (continuing): %s", e)
        if config.TRAKT_CLIENT_ID and config.TRAKT_USERNAME:
            try:
                self._step("Syncing Trakt history")
                imports.trakt_sync()
            except Exception as e:
                log.warning("Trakt sync failed (continuing): %s", e)

        if config.RATINGS_TO_PLEX in ("fill", "overwrite"):
            try:
                self._step("Copying ratings to Plex")
                imports.push_ratings_to_plex()
            except Exception as e:
                log.warning("Copying ratings to Plex failed (continuing): %s", e)

        try:
            sonarr = self.sonarr_factory()
            if sonarr.enabled:
                self._step("Reading Sonarr")
                keys, tvdb = sonarr.keys()
                db.set_meta("sonarr_keys", sorted(keys))
                db.set_meta("sonarr_tvdb", sorted(tvdb))
            else:
                self._forget("sonarr_keys", "sonarr_tvdb")
        except Exception as e:
            log.warning("Sonarr read failed (continuing): %s", e)

    def compute(self, media):
        tv = media == "tv"
        label = MEDIA[media]["label"]
        tmdb = self.tmdb_factory()
        library = [r for r in db.library() if (r["tmdb_id"] < 0) == tv]
        feedback = {k: v for k, v in db.feedback().items() if (k < 0) == tv}
        # "don't learn from": whole Plex libraries, single titles (genres are applied once details are in)
        sections = db.get_meta("plex_sections", {})
        skip_libs = {s.lower() for s in config.NOSEED_LIBRARIES}
        noseed = db.noseed()
        learn_from = [r for r in library if r["tmdb_id"] not in noseed
                      and (sections.get(str(r.get("section")), {}).get("title") or "").lower() not in skip_libs]
        seeds, soft_neg, seed_info = engine.build_seeds(learn_from, {k: v for k, v in feedback.items() if k not in noseed},
                                                        owned_weight=config.OWNED_WEIGHT)
        for k, w in db.watchlist().items():   # saving something for later is a mild "I like the look of this"
            if (k < 0) == tv and k not in seeds and k not in noseed:
                seeds[k] = (0.5, w["title"])
                seed_info[k] = {"kind": "watchlist"}
        if not seeds:
            db.replace_recs([], media)
            return

        step = f"Fetching TMDB data for your {label}"
        self._step(step)
        details = self._ensure_details(tmdb, list(seeds) + list(soft_neg), step)
        if config.NOSEED_GENRES:
            skip_genres = set(config.NOSEED_GENRES)
            for k in [k for k in seeds if skip_genres & {g["name"] for g in (details.get(k) or {}).get("genres", [])}]:
                seeds.pop(k, None)

        exclude = {r["tmdb_id"] for r in library} | set(feedback)
        exclude |= set(db.get_meta("seerr_block", [])) | set(db.get_meta("seerr_requested", []))
        exclude |= set(db.get_meta("radarr_ids", [])) | set(db.get_meta("sonarr_keys", []))

        self._step(f"Finding {label} candidates")
        cands = engine.generate_candidates(seeds, details, exclude, MEDIA[media]["min_votes"]())
        def _year(b):
            rd = b.get("release_date") or ""
            return int(rd[:4]) if len(rd) >= 4 else None
        db.replace_cands([(k, round(c["collab"], 5), _year(c["brief"]), c["brief"].get("vote_count") or 0,
                           c["brief"].get("vote_average") or 0,
                           json.dumps({str(s): round(v, 4) for s, v in sorted(c["because"].items(), key=lambda kv: -kv[1])[:8]}),
                           c["brief"].get("title")) for k, c in cands.items()], media)
        years = [(details.get(r["tmdb_id"]) or {}).get("release_date", "")[:4] for r in library]
        top = engine.stratified_pool(cands, [int(y) for y in years if y.isdigit()], MEDIA[media]["pool"]())
        cands = {k: cands[k] for k in top}

        step = f"Fetching TMDB data for {label} candidates"
        self._step(step)
        details.update(self._ensure_details(tmdb, top, step, upgrade=True))

        if tv:  # Sonarr may only know a show by its TVDB id
            sonarr_tvdb = set(db.get_meta("sonarr_tvdb", []))
            cands = {k: v for k, v in cands.items() if details.get(k, {}).get("tvdb_id") not in sonarr_tvdb}

        imdb = db.imdb_get_many([details[k].get("imdb_id") for k in cands if k in details])

        self._step(f"Scoring {label}")
        engine.score.last_taste = []
        engine.score.last_model = None
        recs = engine.score(seeds, soft_neg, cands, details, imdb, MEDIA[media]["max"](), seed_info)
        db.replace_recs(recs, media)
        db.set_meta(f"taste_{media}", engine.score.last_taste)
        if engine.score.last_model:
            self._step(f"Ranking unwatched {label} you own")
            self.rediscover(media, library, seeds, details, engine.score.last_model)
            m = engine.score.last_model
            m["pos"] = {k: round(v, 6) for k, v in m["pos"].items() if v > 1e-5}
            m["neg"] = {k: round(v, 6) for k, v in m["neg"].items() if v > 1e-5}
            db.set_meta(f"model_{media}", m)
        nets = db.get_meta("network_ids", {})
        for d in details.values():
            for n in d.get("networks") or []:
                nets[n["name"]] = n["id"]
        db.set_meta("network_ids", nets)
        self._step(f"Scored {label}", f"{len(recs)} recommendations")

    def rediscover(self, media, library, seeds, details, model):
        """Rank titles you own but haven't watched by how well they fit your taste."""
        unwatched = {r["tmdb_id"]: r for r in library if not r.get("view_count") and not r.get("user_rating")}
        collab = {}
        for sid, (w, _) in seeds.items():
            if w < 0.5:   # only titles you actually watched, rated or liked vote here
                continue
            d = details.get(sid) or {}
            for lst, factor in (("recommendations", 1.0), ("similar", 0.5)):
                for rank, b in enumerate(d.get(lst, [])):
                    if b["id"] in unwatched and b["id"] != sid:
                        c = collab.setdefault(b["id"], [0.0, {}])
                        v = w * factor * max(0.1, 1.0 - rank / 25.0)
                        c[0] += v
                        c[1][sid] = c[1].get(sid, 0) + v
        imdb = db.imdb_get_many([(details.get(k) or {}).get("imdb_id") for k in unwatched])
        out = []
        for k, row in unwatched.items():
            d = details.get(k)
            if not d or d.get("missing"):
                continue
            c = collab.get(k, [0.0, {}])
            r = engine.score_item(model, k, d, c[0], c[1], imdb)
            r.update(plex_key=row.get("plex_key"), section=row.get("section"), added_at=row.get("added_at"), owned=True)
            out.append(r)
        out.sort(key=lambda r: -r["score"])
        db.replace_libpicks(out, media)
