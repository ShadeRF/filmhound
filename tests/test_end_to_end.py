"""End-to-end test with fake Plex/TMDB/Seerr — no network needed.

Universe: a noir cluster (director 1, keyword 'neo-noir') and a rom-com cluster
(director 2, keyword 'romance'). The library is mostly noir, watched and highly rated,
plus one rom-com rated 2/10. Noir candidates should win; the blocklisted and
already-owned titles must never appear.
"""
import os
import random
import sys
import tempfile
import time
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["MIN_VOTES"] = "10"

from app import config, db, engine  # noqa: E402
from app.sync import Syncer  # noqa: E402

config.DATA_DIR = os.environ["DATA_DIR"]
config.MIN_VOTES = 10
random.seed(1)
SOON = (date.today() + timedelta(days=200)).isoformat()   # within Coming soon's window


def make_movie(i, cluster):
    noir = cluster == "noir"
    return {
        "id": i, "title": f"{cluster.title()} {i}", "original_title": "", "release_date": f"{1950 if noir else 2005}-01-01",
        "overview": "…", "poster_path": None, "backdrop_path": None, "runtime": 100,
        "genres": [{"id": 80 if noir else 35, "name": "Crime" if noir else "Comedy"}],
        "original_language": "en", "countries": ["US"], "vote_average": 7.2, "vote_count": 900,
        "popularity": 10, "adult": False, "status": "Released", "imdb_id": f"tt{i:07d}", "collection": None,
        "keywords": [{"id": 1 if noir else 2, "name": "neo-noir" if noir else "romance"}],
        "crew": [{"id": 1 if noir else 2, "name": "Noir Director" if noir else "RomCom Director", "job": "Director"}],
        "cast": [{"id": 100 + (i % 5), "name": f"Actor {i % 5}"}],
        "recommendations": [], "similar": [], "v": 2, "providers": {"region": "US", "flatrate": [], "rent": [], "buy": []},
        "trailer": None, "backdrop_path": None,
    }


NOIR = list(range(1, 41))
ROM = list(range(101, 141))
UNIVERSE = {i: make_movie(i, "noir") for i in NOIR} | {i: make_movie(i, "romcom") for i in ROM}
for i, m in UNIVERSE.items():
    same = NOIR if i in NOIR else ROM
    m["recommendations"] = [engine_b for engine_b in
                            ({"id": j, "title": UNIVERSE_TITLE, "release_date": "2000-01-01", "vote_average": 7, "vote_count": 900,
                              "popularity": 5, "adult": False}
                             for j, UNIVERSE_TITLE in ((j, f"t{j}") for j in random.sample([x for x in same if x != i], 12)))]

# TV universe (internal keys are negative)
DRAMA = list(range(-201, -221, -1))
COMEDY = list(range(-301, -321, -1))
for grp, name, creator, net in ((DRAMA, "Drama", 7, "HBO"), (COMEDY, "Sitcom", 8, "NBC")):
    for k in grp:
        UNIVERSE[k] = {**make_movie(-k, "noir" if grp is DRAMA else "romcom"), "id": k, "title": f"{name} {-k}",
                       "release_date": "2010-01-01", "media": "tv", "tvdb_id": 9000 - k, "seasons": 3,
                       "networks": [{"id": 1 if grp is DRAMA else 2, "name": net}],
                       "crew": [{"id": creator, "name": f"Creator {creator}", "job": "Creator"}]}
        UNIVERSE[k]["recommendations"] = [{"id": j, "title": f"t{j}", "release_date": "2010-01-01", "vote_average": 8,
                                           "vote_count": 500, "popularity": 5, "adult": False}
                                          for j in random.sample([x for x in grp if x != k], 8)]
SHOWS = DRAMA[:5]

OWNED = NOIR[:15] + [101]
UNWATCHED = NOIR[15:18] + [102]
BLOCKED = {20}


class FakePlex:
    def movies(self):
        now = time.time()
        for i in OWNED:
            yield {"media": "movie", "plex_key": str(i), "title": UNIVERSE[i]["title"], "year": 1950, "added_at": now,
                   "view_count": 1, "leaf_count": None, "last_viewed_at": now - 86400 * 30,
                   "user_rating": 2.0 if i == 101 else 9.0, "tmdb_id": i, "imdb_id": None, "tvdb_id": None}
        for i in UNWATCHED:
            yield {"media": "movie", "plex_key": str(i), "title": UNIVERSE[i]["title"], "year": 1950, "added_at": now,
                   "view_count": 0, "leaf_count": None, "last_viewed_at": None, "section": "1",
                   "user_rating": None, "tmdb_id": i, "imdb_id": None, "tvdb_id": None}

    def shows(self):
        now = time.time()
        for k in SHOWS:
            yield {"media": "tv", "plex_key": str(k), "title": UNIVERSE[k]["title"], "year": 2010, "added_at": now,
                   "view_count": 20, "leaf_count": 30, "last_viewed_at": now - 86400 * 10,
                   "user_rating": None, "tmdb_id": -k, "imdb_id": None, "tvdb_id": None}


class FakeTMDB:
    calls = 0

    def details(self, i):
        FakeTMDB.calls += 1
        return UNIVERSE.get(i, {"id": i, "missing": True})

    def find(self, *a):
        return None

    def _get(self, path, **params):
        # just enough of TMDB for collection gaps / coming soon
        if path == "/person/1":
            crew = [{"id": i, "title": UNIVERSE[i]["title"], "release_date": "1950-01-01", "job": "Director", "media_type": "movie",
                     "vote_count": 900, "poster_path": "/p.jpg"} for i in NOIR[:22]]
            crew.append({"id": 999, "title": "Noir Future", "release_date": SOON, "job": "Director", "media_type": "movie",
                         "vote_count": 30, "poster_path": "/p.jpg"})
            return {"id": 1, "name": "Noir Director", "combined_credits": {"cast": [], "crew": crew}}
        if path.startswith("/person/"):
            return {"id": int(path.split("/")[2]), "name": "Someone", "combined_credits": {"cast": [], "crew": []}}
        return None


class FakeSeerr:
    enabled = True

    def blocklisted_keys(self):
        return BLOCKED | {-215}

    def requested_keys(self):
        return set()


def test_pipeline():
    db.init()
    s = Syncer(lambda: FakePlex(), lambda: FakeTMDB(), lambda: FakeSeerr(), lambda: iter([("tt0000025", 8.4, 50000)]))
    s.run(full=True)
    assert s.state["error"] is None, s.state
    recs = db.recs("movie")
    ids = [r["key"] for r in recs]
    assert recs, "no recommendations produced"
    assert not set(ids) & set(OWNED + UNWATCHED), "owned movie recommended"
    lib = db.libpicks("movie")
    assert [r["key"] for r in lib][:3] == sorted(NOIR[15:18], key=lambda k: -next(x["score"] for x in lib if x["key"] == k)), lib
    assert lib[-1]["key"] == 102, "unwatched rom-com should rank last"
    assert lib[0]["plex_key"] == str(lib[0]["key"]) and lib[0]["because"], lib[0]
    assert 20 not in ids, "blocklisted movie recommended"
    top10 = ids[:10]
    assert all(i in NOIR for i in top10), f"top picks should be noir: {top10}"
    assert any("Directed by Noir Director" in r["matches"] for r in recs[:5])
    assert recs[0]["because"] and recs[0]["because"][0]["kind"] == "rated", recs[0]["because"]
    assert all(r["media"] == "movie" and r["key"] > 0 for r in recs)

    tv = db.recs("tv")
    tv_keys = [r["key"] for r in tv]
    assert tv, "no TV recs"
    assert all(k < 0 for k in tv_keys) and not set(tv_keys) & set(SHOWS)
    assert -215 not in tv_keys, "blocklisted show recommended"
    assert all(k in DRAMA for k in tv_keys[:5]), tv_keys[:5]
    assert any("Created by Creator 7" in r["matches"] for r in tv[:3]), tv[0]["matches"]
    assert tv[0]["because"][0]["kind"] == "partial" and tv[0]["because"][0]["total_eps"] == 30
    assert tv[0]["tvdb_id"] and tv[0]["tmdb_id"] == -tv[0]["key"]
    imdb25 = next((r for r in recs if r["tmdb_id"] == 25), None)
    assert imdb25 and imdb25["imdb_rating"] == 8.4

    # cache: a recompute should not refetch anything
    before = FakeTMDB.calls
    s.run(full=False)
    assert FakeTMDB.calls == before

    # a 5-star "seen it" acts as a strong seed and is excluded from picks
    db.set_feedback(top10[1], "seen", "y", rating=10.0, source="filmhound")
    s.run(full=False)
    assert top10[1] not in [r["key"] for r in db.recs("movie")]

    # feedback: disliking a noir pick removes it; liking works as a seed
    db.set_feedback(top10[0], "dismissed", "x")
    s.run(full=False)
    assert top10[0] not in [r["key"] for r in db.recs("movie")]
    assert len(db.recs("tv")) == len(tv), "movie recompute clobbered TV recs"

    # collection/director gaps and coming soon were built during the full sync
    gaps = db.get_meta("gaps")["groups"]
    d = next(g for g in gaps if g["type"] == "director" and g["id"] == 1)
    missing = {i["key"] for i in d["items"]}
    assert missing and not missing & set(OWNED + UNWATCHED), missing
    assert 999 in missing and next(i for i in d["items"] if i["key"] == 999)["upcoming"]
    assert d["owned"] == 18
    up = db.get_meta("upcoming")["items"]
    assert [i["key"] for i in up] == [999] and "Director: Noir Director" in up[0]["people"]

    # watchlist nudges taste; "don't learn from" drops a seed
    db.set_noseed(NOIR[0], "x")
    s.run(full=False)
    assert str(NOIR[0]) not in db.get_meta("model_movie")["seeds"] and NOIR[0] not in db.get_meta("model_movie")["seeds"]
    db.set_noseed(NOIR[0], "x", on=False)
    db.set_watchlist(ROM[5], "rom")
    s.run(full=False)
    seeds = db.get_meta("model_movie")["seeds"]
    assert str(ROM[5]) in seeds or ROM[5] in seeds
    print(f"OK — {len(recs)} recs, top: {[r['title'] for r in recs[:5]]}")
    print("movie reasons:", recs[0]["because"][0], recs[0]["matches"])
    print(f"TV: {len(tv)} recs, top: {[r['title'] for r in tv[:4]]}, matches: {tv[0]['matches']}")


def test_seed_weights():
    now = time.time()
    assert engine.seed_weight({"user_rating": 10}, now) == 2.0
    assert engine.seed_weight({"user_rating": 2}, now) < 0
    assert engine.seed_weight({"view_count": 0}, now) == 0.8
    recent = engine.seed_weight({"view_count": 1, "last_viewed_at": now}, now)
    old = engine.seed_weight({"view_count": 1, "last_viewed_at": now - 86400 * 2000}, now)
    assert recent > old >= 1.0


def test_plex_guids():
    from app.clients import parse_plex_guids
    assert parse_plex_guids({"Guid": [{"id": "imdb://tt0068646"}, {"id": "tmdb://238"}]}) == {"tmdb_id": 238, "imdb_id": "tt0068646", "tvdb_id": None}
    assert parse_plex_guids({"guid": "com.plexapp.agents.imdb://tt0068646?lang=en"})["imdb_id"] == "tt0068646"
    assert parse_plex_guids({"guid": "com.plexapp.agents.thetvdb://81189?lang=en"})["tvdb_id"] == 81189
    assert parse_plex_guids({"guid": "com.plexapp.agents.themoviedb://238?lang=en"})["tmdb_id"] == 238


if __name__ == "__main__":
    test_seed_weights()
    test_plex_guids()
    test_pipeline()


def test_letterboxd_export_status():
    """The Rate page and Settings nudge toward the full export until one has been imported."""
    from app import db, imports
    db.init()
    db.set_meta("lb_export_at", None)
    db.set_meta("lb_match", None)
    assert not imports.letterboxd_export_done()
    db.set_meta("lb_match", {"Love, Rosie|2014": 200727})   # exports uploaded before lb_export_at existed still count
    assert imports.letterboxd_export_done()


def test_coming_soon_skips_people_who_have_died():
    """A taste built on classic films leans on people who have died; they mustn't use up every place."""
    from app import extras
    db.init()
    movie = [{"f": f"d:{i}", "label": f"Directed by Person {i}", "w": 1.0 - i / 1000} for i in range(1, 61)]
    tv = [{"f": "c:500", "label": "TV Actor", "w": 0.9}, {"f": "c:1", "label": "Person 1", "w": 0.8}]
    db.set_meta("taste_movie", movie)
    db.set_meta("taste_tv", tv)
    dead = set(range(1, 51))           # the 50 strongest movie people are long gone

    class People:
        asked = []

        def details(self, key):
            return {"id": key, "missing": True}

        def _get(self, path, **params):
            if not path.startswith("/person/"):
                return {}   # no Discover results in this test
            pid = int(path.split("/")[2])
            People.asked.append(pid)
            work = {"id": 10000 + pid, "title": f"New {pid}", "release_date": SOON, "job": "Director",
                    "media_type": "movie", "vote_count": 5, "poster_path": "/p.jpg"}
            return {"id": pid, "name": f"Person {pid}", "deathday": "1970-01-01" if pid in dead else None,
                    "combined_credits": {"cast": [], "crew": [work]}}
    for pid in [1, 500] + list(range(2, 61)):
        db.set_meta(f"person_{pid}", None)
    try:
        n = extras.build_upcoming(People())
        keys = {i["key"] for i in db.get_meta("upcoming")["items"]}
        assert n == 11 and keys == {10000 + p for p in [500] + list(range(51, 61))}, sorted(keys)
        assert People.asked.count(1) == 1, "someone in both lists was looked up twice"
        # a long movie list alone could fill every place looked at; TV people are taken in turn, so they still count
        db.set_meta("taste_movie", [{"f": f"d:{i}", "label": f"P{i}", "w": 1.0} for i in range(1, 200)])
        dead.update(range(1, 200))
        for pid in range(1, 200):
            db.set_meta(f"person_{pid}", None)   # forget last week's lookups
        extras.build_upcoming(People())
        assert {i["key"] for i in db.get_meta("upcoming")["items"]} == {10500}
    finally:
        db.set_meta("taste_movie", None)
        db.set_meta("taste_tv", None)
        for pid in [500] + list(range(1, 200)):
            db.set_meta(f"person_{pid}", None)   # these made-up people mustn't leak into other tests


def test_coming_soon_ranks_what_fits_your_taste():
    """Upcoming titles found by Discover are scored against your taste: the noir you'd want, not the rom-com."""
    from app import extras
    db.init()
    s = Syncer(lambda: FakePlex(), lambda: FakeTMDB(), lambda: FakeSeerr(), lambda: iter([]))
    s.run(full=True)
    noir, romcom, owned = 7001, 7002, OWNED[0]
    for k, cluster in ((noir, "noir"), (romcom, "romcom")):
        UNIVERSE[k] = {**make_movie(k, cluster), "release_date": SOON, "vote_count": 0, "vote_average": 0}

    class WithDiscover(FakeTMDB):
        def _get(self, path, **params):
            if path == "/discover/movie" and params.get("page") == 1:
                return {"results": [{"id": k, "title": UNIVERSE[k]["title"], "release_date": SOON, "poster_path": "/p.jpg"}
                                    for k in (romcom, noir)] + [{"id": owned, "title": "Owned", "release_date": SOON}]}
            if path.startswith("/genre/"):
                return {"genres": [{"id": 80, "name": "Crime"}, {"id": 35, "name": "Comedy"}]}
            if path.startswith("/discover/"):
                return {"results": []}
            return super()._get(path, **params)
    try:
        extras.build_upcoming(WithDiscover())
        up = [i["key"] for i in db.get_meta("upcoming")["items"]]
        assert noir in up and owned not in up, up
        assert romcom not in up or up.index(noir) < up.index(romcom), up
        item = next(i for i in db.get_meta("upcoming")["items"] if i["key"] == noir)
        assert item["matches"] and item["date"] == SOON and item["upcoming"]
        assert 999 in up and "Director: Noir Director" in next(i for i in db.get_meta("upcoming")["items"] if i["key"] == 999)["people"]
        # already in Radarr, or sent from Filmhound: you have it coming, so it isn't suggested
        from fastapi.testclient import TestClient
        import app.main as m
        c = TestClient(m.app)
        db.set_meta("radarr_ids", [noir])
        db.set_feedback(999, "added", "Noir Future")
        shown = [i["key"] for i in c.get("/api/upcoming?media=movie").json()["items"]]
        assert noir not in shown and 999 not in shown, "hidden straight away, without waiting for a sync"
        assert all(999 not in [i["key"] for i in g["items"]] for g in c.get("/api/gaps").json()["groups"])
        extras.build_upcoming(WithDiscover())
        rebuilt = [i["key"] for i in db.get_meta("upcoming")["items"]]
        assert noir not in rebuilt and 999 not in rebuilt, "and they don't use up places in the next build"
    finally:
        db.set_meta("radarr_ids", [])
        db.delete_feedback(999)
        UNIVERSE.pop(noir, None)
        UNIVERSE.pop(romcom, None)


def test_unrated_watch_history_never_overrides_plex():
    lib = [{"tmdb_id": 1, "title": "Hated", "user_rating": 2},
           {"tmdb_id": 2, "title": "Loved", "user_rating": 10},
           {"tmdb_id": 3, "title": "Owned, unwatched", "view_count": 0},
           {"tmdb_id": 4, "title": "Rated in Plex, re-rated on Letterboxd", "user_rating": 2}]
    seen = {"action": "seen", "title": "x", "rating": None, "source": "trakt"}
    fb = {1: seen, 2: seen, 3: seen, 4: {**seen, "rating": 9}, 5: seen}
    seeds, _, info = engine.build_seeds(lib, fb)
    assert seeds[1][0] < 0 and info[1]["kind"] == "rated", "a 1-star Plex rating turned into a like"
    assert seeds[2][0] == 2.0
    assert seeds[3][0] == 0.9      # owned but only watched elsewhere: the watch counts
    assert seeds[4][0] > 1         # an actual rating from the import still wins
    assert seeds[5][0] == 0.9      # not in Plex at all


def test_failed_imdb_download_keeps_old_ratings():
    db.init()
    db.imdb_replace(iter([("tt1", 8.0, 1000), ("tt2", 7.0, 500)]))

    def broken_download():
        raise OSError("connection reset")
        yield

    def dies_halfway():
        yield "tt9", 1.0, 1
        raise OSError("connection reset")

    for bad in (broken_download(), dies_halfway(), iter([])):
        try:
            db.imdb_replace(bad)
            raise AssertionError("a failed download was accepted")
        except (OSError, ValueError):
            pass
        db.set_meta("anything", 1)   # the next write on this connection must not commit a half-finished swap
        assert db.imdb_get_many(["tt1", "tt2", "tt9"]) == {"tt1": (8.0, 1000), "tt2": (7.0, 500)}
    db.imdb_replace(iter([("tt3", 9.0, 10)]))
    assert db.imdb_get_many(["tt1", "tt3"]) == {"tt3": (9.0, 10)}


def test_disconnected_service_stops_hiding_titles():
    db.init()
    db.set_meta("radarr_ids", [5, 6])
    db.set_meta("sonarr_keys", [-7])
    db.set_meta("sonarr_tvdb", [1234])
    s = Syncer(lambda: FakePlex(), lambda: FakeTMDB(), lambda: FakeSeerr(), lambda: iter([("tt0000025", 8.4, 50000)]))
    s.run(full=True)     # Radarr and Sonarr have no address in this test: they're not connected
    assert s.state["error"] is None, s.state
    assert db.get_meta("radarr_ids") == [] and db.get_meta("sonarr_keys") == [] and db.get_meta("sonarr_tvdb") == []
    assert db.get_meta("seerr_block"), "a service that is still connected keeps its list"

    class SeerrOff:
        enabled = False
    s.seerr_factory = SeerrOff
    s.run(full=True)
    assert db.get_meta("seerr_block") == [] and db.get_meta("seerr_requested") == []


def test_dont_learn_from_covers_ratings_and_imports(monkeypatch):
    db.init()
    key = ROM[20]
    db.set_feedback(key, "seen", "Loved rom-com", rating=10, source="letterboxd")
    db.set_noseed(key, "Loved rom-com")
    got = {}
    real = engine.build_seeds

    def spy(library, feedback, **kw):
        got["feedback"] = feedback
        return real(library, feedback, **kw)
    monkeypatch.setattr(engine, "build_seeds", spy)
    s = Syncer(lambda: FakePlex(), lambda: FakeTMDB(), lambda: FakeSeerr(), lambda: iter([]))
    try:
        s.compute("movie")
        assert key not in got["feedback"], "a title marked 'don't learn from this' still shaped the picks"
        db.set_noseed(key, None, on=False)
        s.compute("movie")
        assert key in got["feedback"]
    finally:
        db.set_noseed(key, None, on=False)
        db.delete_feedback(key)


def test_imports_leave_unchanged_entries_alone():
    from app import imports
    db.init()
    key = 987654
    try:
        assert imports._record([(key, "Some Film", 8.0)], "trakt") == 1
        db.conn().execute("UPDATE feedback SET at = 1000 WHERE tmdb_id = ?", (key,))
        db.commit()
        db.bump()
        assert imports._record([(key, "Some Film", 8.0)], "trakt") == 0, "an unchanged entry was counted as new"
        assert db.feedback()[key]["at"] == 1000, "the nightly sync moved an old entry to the top of History"
        assert imports._record([(key, "Some Film", 9.0)], "trakt") == 1
        assert db.feedback()[key]["rating"] == 9.0 and db.feedback()[key]["at"] > 1000
    finally:
        db.delete_feedback(key)


def test_letterboxd_upload_retries_films_tmdb_could_not_answer(monkeypatch):
    from app import imports
    db.init()
    db.set_meta("lb_match", {"Old Miss|1990": None})     # not found by an earlier upload: worth another try
    tmdb_up = {"ok": False}

    class FlakyTMDB:
        def __init__(self, key):
            pass

        def _get(self, path, query=None, **kw):
            if query == "Flaky Film" and not tmdb_up["ok"]:
                raise OSError("TMDB is down")
            return {"results": [] if query == "Nobody Made This" else [{"id": {"Flaky Film": 31, "Fine Film": 32, "Old Miss": 33}[query]}]}
    monkeypatch.setattr(imports, "TMDB", FlakyTMDB)
    films = {("Flaky Film", "2001"): 8.0, ("Fine Film", "2002"): None, ("Nobody Made This", "2003"): None, ("Old Miss", "1990"): None}
    try:
        imports._match_and_record(films)
        cache = db.get_meta("lb_match")
        assert "Flaky Film|2001" not in cache, "a film TMDB couldn't answer for was remembered as not found"
        assert cache["Fine Film|2002"] == 32 and cache["Nobody Made This|2003"] is None and cache["Old Miss|1990"] == 33
        tmdb_up["ok"] = True
        imports._match_and_record(films)       # uploading again picks up what was missed
        assert db.get_meta("lb_match")["Flaky Film|2001"] == 31 and db.feedback()[31]["rating"] == 8.0
    finally:
        for k in (31, 32, 33):
            db.delete_feedback(k)
        db.set_meta("lb_match", None)
        db.set_meta("lb_export_at", None)


def test_hiding_a_title_keeps_picks_loaded_but_updates_every_list():
    """A hide, rating or watchlist change must show up everywhere at once without reloading what a sync wrote."""
    from fastapi.testclient import TestClient
    import app.main as m
    db.init()
    s = Syncer(lambda: FakePlex(), lambda: FakeTMDB(), lambda: FakeSeerr(), lambda: iter([("tt0000025", 8.4, 50000)]))
    s.run(full=True)
    c = TestClient(m.app)

    def home_keys():
        return {i["key"] for row in c.get("/api/home?media=movie").json()["rows"] for i in row["items"]}
    first = c.get("/api/recs?media=movie").json()
    victim = first["items"][0]["key"]
    assert victim in home_keys()
    facets_before = c.get("/api/facets?media=movie").json()
    loaded = db.recs_map("movie"), db.library_map(), db.libpicks("movie")[0]
    try:
        r = c.post("/api/feedback", json={"tmdb_id": abs(victim), "media": "movie", "action": "dismissed", "title": "x"})
        assert r.status_code == 200
        assert db.recs_map("movie") is loaded[0] and db.library_map() is loaded[1] and db.libpicks("movie")[0] is loaded[2], \
            "a hide reloaded the picks or the library"
        after = c.get("/api/recs?media=movie").json()
        assert after["total"] == first["total"] - 1 and victim not in [i["key"] for i in after["items"]]
        assert victim not in home_keys()
        assert sum(g["count"] for g in c.get("/api/facets?media=movie").json()["genres"]) < sum(g["count"] for g in facets_before["genres"])
        assert any(x["key"] == victim for x in c.get("/api/feedback").json())
        # undo brings it straight back
        assert c.delete(f"/api/feedback/{victim}").status_code == 200
        assert c.get("/api/recs?media=movie").json()["total"] == first["total"] and victim in home_keys()
        # watchlist and don't-learn show at once too
        db.set_watchlist(victim, "x")
        assert victim in db.watchlist() and db.recs_map("movie") is loaded[0]
        db.set_noseed(victim, "x")
        assert victim in db.noseed()
        # something a sync writes still reloads everything
        db.replace_recs(db.recs("movie"), "movie")
        assert db.recs_map("movie") is not loaded[0]
    finally:
        db.delete_feedback(victim)
        db.set_watchlist(victim, None, on=False)
        db.set_noseed(victim, None, on=False)


def test_history_and_settings_read_without_parsing_everything():
    from fastapi.testclient import TestClient
    import app.main as m
    db.init()
    c = TestClient(m.app)
    db.tmdb_put(424242, {"id": 424242, "title": "From The Cache", "poster_path": "/p.jpg", "release_date": "1994-05-06", "overview": "x" * 500})
    db.tmdb_put(424243, {"id": 424243, "title": "No Date", "poster_path": None, "release_date": ""})
    db.commit()
    try:
        db.set_feedback(424242, "seen", None, rating=8.0, source="letterboxd")
        db.set_feedback(424243, "dismissed", "Kept Title")
        db.set_feedback(-424244, "dismissed", "Show With No Record")
        rows = {x["key"]: x for x in c.get("/api/feedback").json()}
        assert rows[424242]["title"] == "From The Cache" and rows[424242]["year"] == 1994 and rows[424242]["poster_path"] == "/p.jpg"
        assert rows[424242]["rating"] == 8.0 and rows[424242]["media"] == "movie" and rows[424242]["tmdb_id"] == 424242
        assert rows[424243]["title"] == "Kept Title" and rows[424243]["year"] is None
        assert rows[-424244]["media"] == "tv" and rows[-424244]["tmdb_id"] == 424244 and rows[-424244]["poster_path"] is None
    finally:
        for k in (424242, 424243, -424244):
            db.delete_feedback(k)
    # IMDb: compact table, count remembered at load time, old ratings kept when a load fails
    db.imdb_replace(iter([("tt1", 8.0, 1000), ("tt2", 7.0, 500), ("tt3", 6.0, 50)]))
    layout = db.conn().execute("SELECT sql FROM sqlite_master WHERE name='imdb_ratings'").fetchone()[0]
    assert "WITHOUT ROWID" in layout
    assert db.imdb_count() == 3 and c.get("/api/settings").json()["stats"]["imdb"] == 3

    def dies_halfway():
        yield "tt9", 1.0, 1
        raise OSError("connection reset")
    try:
        db.imdb_replace(dies_halfway())
    except OSError:
        pass
    assert db.imdb_count() == 3 and db.imdb_get_many(["tt1", "tt9"]) == {"tt1": (8.0, 1000)}
    assert "WITHOUT ROWID" in db.conn().execute("SELECT sql FROM sqlite_master WHERE name='imdb_ratings'").fetchone()[0]
    assert any(r[1] == "tmdb_fetched" for r in db.conn().execute("PRAGMA index_list(tmdb)"))


def test_old_imdb_table_is_converted_on_the_next_load():
    db.init()
    c = db.conn()
    c.execute("DROP TABLE imdb_ratings")
    c.execute("CREATE TABLE imdb_ratings (imdb_id TEXT PRIMARY KEY, rating REAL, votes INTEGER)")   # the layout before 0.10.2
    c.execute("INSERT INTO imdb_ratings VALUES ('tt5', 5.0, 5)")
    c.execute("DELETE FROM meta WHERE key = 'imdb_count'")
    c.commit()
    assert db.imdb_count() == 1                      # counted once, then remembered
    assert db.imdb_get_many(["tt5"]) == {"tt5": (5.0, 5)}
    db.imdb_replace(iter([("tt6", 6.0, 6), ("tt7", 7.0, 7)]))
    assert "WITHOUT ROWID" in c.execute("SELECT sql FROM sqlite_master WHERE name='imdb_ratings'").fetchone()[0]
    assert db.imdb_count() == 2 and db.imdb_get_many(["tt5", "tt6"]) == {"tt6": (6.0, 6)}


def test_reset_all_feedback_clears_history_and_the_letterboxd_marker():
    """Reset all feedback empties History, and Settings asks for the Letterboxd export again."""
    from fastapi.testclient import TestClient
    import app.main as m
    from app import imports
    db.init()
    c = TestClient(m.app)
    db.set_feedback(999001, "dismissed", "Gone")
    db.set_feedback(999002, "seen", "Seen", rating=8, source="letterboxd")
    db.set_meta("lb_export_at", 123)
    db.set_meta("lb_match", {"Seen|2000": 999002})
    db.commit()
    assert imports.letterboxd_export_done()
    assert c.post("/api/maintenance/clear-feedback").status_code == 200
    assert c.get("/api/feedback").json() == []
    assert not imports.letterboxd_export_done()


def test_library_says_how_many_there_are_before_filters():
    """Your Library offers collection sizes from everything unwatched, whatever the filters show."""
    from fastapi.testclient import TestClient
    import app.main as m
    db.init()
    s = Syncer(lambda: FakePlex(), lambda: FakeTMDB(), lambda: FakeSeerr(), lambda: iter([("tt0000025", 8.4, 50000)]))
    s.run(full=True)
    c = TestClient(m.app)
    everything = c.get("/api/library?media=movie").json()
    assert everything["all"] == everything["total"]
    narrowed = c.get("/api/library?media=movie&q=zzzz-no-such-title").json()
    assert narrowed["total"] == 0 and narrowed["all"] == everything["all"]


def test_genre_list_counts_every_genre_you_own_without_asking_tmdb():
    """Don't learn from › More genres: every TMDB genre on titles you own, movies and shows together, most common first."""
    from fastapi.testclient import TestClient
    import app.main as m
    db.init()
    s = Syncer(lambda: FakePlex(), lambda: FakeTMDB(), lambda: FakeSeerr(), lambda: iter([]))
    s.run(full=True)
    c = TestClient(m.app)
    calls = FakeTMDB.calls
    got = c.get("/api/noseed/genres").json()["genres"]
    owned_noir = len(OWNED + UNWATCHED) - 2 + len(SHOWS)   # 101 and 102 are the rom-coms
    assert got == [{"name": "Crime", "count": owned_noir}, {"name": "Comedy", "count": 2}], got
    assert FakeTMDB.calls == calls, "listing genres asked TMDB"
    # before the first sync there is nothing to list
    db.replace_library([])
    try:
        assert c.get("/api/noseed/genres").json() == {"genres": []}
    finally:
        s.run(full=True)


def test_library_modes_are_stored_as_before():
    """Plex card: Use / Own only / Ignore are stored in PLEX_LIBRARIES and NOSEED_LIBRARIES, so older setups carry over."""
    from fastapi.testclient import TestClient
    import app.main as m
    from app.clients import Plex
    db.init()
    c = TestClient(m.app)
    keep = {k: getattr(config, k) for k in ("PLEX_LIBRARIES", "NOSEED_LIBRARIES", "SYNC_HOUR")}
    try:
        # Movies: Use, Kids: Own only, 4K: Ignore
        r = c.put("/api/settings", json={"PLEX_LIBRARIES": ["Movies", "Kids"], "NOSEED_LIBRARIES": ["Kids"]}).json()
        assert {"PLEX_LIBRARIES", "NOSEED_LIBRARIES"} <= set(r["changed"])
        assert r["values"]["PLEX_LIBRARIES"] == ["Movies", "Kids"] and r["values"]["NOSEED_LIBRARIES"] == ["Kids"]
        # saving anything else leaves them exactly as they were (no migration)
        c.put("/api/settings", json={"SYNC_HOUR": 3})
        assert config.PLEX_LIBRARIES == ["Movies", "Kids"] and config.NOSEED_LIBRARIES == ["Kids"]
        # Ignore: the library isn't read at all
        p = Plex("http://plex.test", "t", config.PLEX_LIBRARIES)
        p._get = lambda path, **k: {"Directory": [{"key": "1", "title": "Movies", "type": "movie"},
                                                  {"key": "3", "title": "kids", "type": "movie"},
                                                  {"key": "4", "title": "4K", "type": "movie"}]}
        assert [d["title"] for d in p.sections("movie")] == ["Movies", "kids"]
        # every library on Use again: an empty list means all of them
        c.put("/api/settings", json={"PLEX_LIBRARIES": [], "NOSEED_LIBRARIES": []})
        assert Plex("http://plex.test", "t", config.PLEX_LIBRARIES).libraries == []
    finally:
        config.save(keep)


def test_own_only_library_counts_as_owned_but_does_not_shape_picks(monkeypatch):
    db.init()
    s = Syncer(lambda: FakePlex(), lambda: FakeTMDB(), lambda: FakeSeerr(), lambda: iter([]))
    s.run(full=True)
    sections = db.get_meta("plex_sections", {})
    got = {}
    real = engine.build_seeds

    def spy(library, feedback, **kw):
        got["keys"] = {r["tmdb_id"] for r in library}
        return real(library, feedback, **kw)
    monkeypatch.setattr(engine, "build_seeds", spy)
    db.set_meta("plex_sections", {"1": {"title": "Kids", "type": "movie"}})   # the unwatched titles live in section 1
    monkeypatch.setattr(config, "NOSEED_LIBRARIES", ["kids"])
    try:
        s.compute("movie")
        assert not got["keys"] & set(UNWATCHED), "an own-only library shaped the picks"
        assert set(OWNED) <= got["keys"]
        assert not {r["key"] for r in db.recs("movie")} & set(UNWATCHED), "an own-only title was suggested"
    finally:
        db.set_meta("plex_sections", sections)
        monkeypatch.setattr(config, "NOSEED_LIBRARIES", [])
        s.compute("movie")
