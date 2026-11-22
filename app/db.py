import json
import os
import sqlite3
import threading
import time

from . import config

_local = threading.local()

# ---- in-memory read cache ---------------------------------------------------------
# Picks, library and feedback are read on nearly every request but only change when a
# sync or a user action writes them, so cache the parsed rows and drop the cache on write.
_gen = 0
_soft = 0      # counts partial clears, so a read that was being built during one isn't stored
_cache = {}
_cache_lock = threading.Lock()

# Cached reads that your own actions (hide, rate, watchlist, don't-learn) can't change: they hold what a sync wrote.
# Anything not named here is dropped on such an action, so a new cached read is safe by default.
SYNC_DATA = ("library", "library_map", "recs", "recs_map", "libpicks", "meta", "scored_all")


def bump(keep=()):
    """Drop cached reads after a write. keep names the ones this write can't have changed."""
    global _gen, _soft
    with _cache_lock:
        if keep:
            _soft += 1
            for k in [k for k in _cache if (k[0] if isinstance(k, tuple) else k) not in keep]:
                del _cache[k]
        else:
            _gen += 1
            _cache.clear()


def bump_user():
    """After a change to feedback, the watchlist or don't-learn: picks, library and the model stay loaded."""
    bump(keep=SYNC_DATA)


def generation():
    return _gen


def cached(key, fn):
    hit = _cache.get(key)
    if hit is not None and hit[0] == _gen:
        return hit[1]
    stamp = (_gen, _soft)
    val = fn()
    with _cache_lock:
        if stamp == (_gen, _soft):   # nothing was written while this was being read
            _cache[key] = (_gen, val)
    return val

SCHEMA = """
CREATE TABLE IF NOT EXISTS library (
    tmdb_id INTEGER PRIMARY KEY,
    plex_key TEXT,
    title TEXT,
    year INTEGER,
    view_count INTEGER DEFAULT 0,
    last_viewed_at INTEGER,
    user_rating REAL,
    added_at INTEGER,
    leaf_count INTEGER
);
CREATE TABLE IF NOT EXISTS tmdb (
    tmdb_id INTEGER PRIMARY KEY,
    json TEXT NOT NULL,
    fetched_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS imdb_ratings (
    imdb_id TEXT PRIMARY KEY,
    rating REAL,
    votes INTEGER
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS tmdb_fetched ON tmdb(fetched_at);   -- small, so counting cached titles doesn't walk the big table
CREATE TABLE IF NOT EXISTS feedback (
    tmdb_id INTEGER PRIMARY KEY,
    action TEXT NOT NULL,          -- dismissed | liked | disliked | requested
    title TEXT,
    at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS recs (
    tmdb_id INTEGER PRIMARY KEY,
    score REAL NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cands (
    tmdb_id INTEGER PRIMARY KEY,   -- internal key (negative = TV)
    collab REAL NOT NULL,
    year INTEGER,
    votes INTEGER,
    rating REAL,
    because TEXT,
    title TEXT
);
CREATE INDEX IF NOT EXISTS cands_collab ON cands(collab DESC);
CREATE TABLE IF NOT EXISTS libpicks (
    tmdb_id INTEGER PRIMARY KEY,
    score REAL NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watchlist (
    tmdb_id INTEGER PRIMARY KEY,   -- internal key (negative = TV)
    title TEXT,
    at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS noseed (
    tmdb_id INTEGER PRIMARY KEY,   -- titles that should not shape your taste
    title TEXT,
    at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def path():
    return os.path.join(config.DATA_DIR, "filmhound.db")


def _migrate_old_name():
    """This app used to be called Marquee: pick up its database if that's what's on disk."""
    new, old = path(), os.path.join(config.DATA_DIR, "marquee.db")
    if not os.path.exists(new) and os.path.exists(old):
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(old + suffix):
                os.replace(old + suffix, new + suffix)


def conn():
    c = getattr(_local, "conn", None)
    if c is None:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        _migrate_old_name()
        c = sqlite3.connect(path(), timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")   # safe with WAL, and makes the many small commits during a sync cheap
        c.execute("PRAGMA journal_size_limit=33554432")   # don't leave a huge write-ahead file behind after the IMDb reload
        _local.conn = c
    return c


def init():
    c = conn()
    c.executescript(SCHEMA)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(library)")}
    if "leaf_count" not in cols:  # added with TV support
        c.execute("ALTER TABLE library ADD COLUMN leaf_count INTEGER")
    if "section" not in cols:
        c.execute("ALTER TABLE library ADD COLUMN section TEXT")
    fcols = {r["name"] for r in c.execute("PRAGMA table_info(feedback)")}
    if "rating" not in fcols:
        c.execute("ALTER TABLE feedback ADD COLUMN rating REAL")
    if "source" not in fcols:
        c.execute("ALTER TABLE feedback ADD COLUMN source TEXT")
    c.commit()


def _get_meta(key):
    row = conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return None if row is None else json.loads(row["value"])


def get_meta(key, default=None):
    if key.startswith(("model_", "taste_")):   # large blobs read per request: cache them
        v = cached(("meta", key), lambda: _get_meta(key))
    else:
        v = _get_meta(key)
    return default if v is None else v


def set_meta(key, value):
    conn().execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, json.dumps(value)))
    conn().commit()
    if key.startswith(("model_", "taste_")):
        bump()


# ---------- library ----------

def replace_library(rows):
    c = conn()
    c.execute("DELETE FROM library")
    c.executemany(
        "INSERT OR REPLACE INTO library(tmdb_id, plex_key, title, year, view_count, last_viewed_at, user_rating, added_at, leaf_count, section)"
        " VALUES (:key, :plex_key, :title, :year, :view_count, :last_viewed_at, :user_rating, :added_at, :leaf_count, :section)",
        [{"section": None, "leaf_count": None, **r} for r in rows],
    )
    c.commit()
    bump()


def library():
    return list(cached("library", lambda: [dict(r) for r in conn().execute("SELECT * FROM library")]))


def library_map():
    return cached("library_map", lambda: {r["tmdb_id"]: r for r in library()})


def library_counts():
    row = conn().execute("SELECT SUM(tmdb_id > 0) AS movies, SUM(tmdb_id < 0) AS shows FROM library").fetchone()
    return row["movies"] or 0, row["shows"] or 0


# ---------- tmdb cache ----------

def tmdb_get_many(ids):
    out = {}
    ids = list(ids)
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        q = "SELECT tmdb_id, json, fetched_at FROM tmdb WHERE tmdb_id IN (%s)" % ",".join("?" * len(chunk))
        for r in conn().execute(q, chunk):
            out[r["tmdb_id"]] = (json.loads(r["json"]), r["fetched_at"])
    return out


def tmdb_brief(ids):
    """{id: {title, poster_path, release_date}} without parsing each title's whole record."""
    out = {}
    ids = list(ids)
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        q = ("SELECT tmdb_id, json_extract(json, '$.title') AS title, json_extract(json, '$.poster_path') AS poster_path,"
             " json_extract(json, '$.release_date') AS release_date FROM tmdb WHERE tmdb_id IN (%s)" % ",".join("?" * len(chunk)))
        for r in conn().execute(q, chunk):
            out[r["tmdb_id"]] = {"title": r["title"], "poster_path": r["poster_path"], "release_date": r["release_date"]}
    return out


def owned_genres():
    """[{name, count}] for every TMDB genre on titles you own (movies and shows together), most common first."""
    def read():
        q = ("SELECT json_extract(g.value, '$.name') AS name, COUNT(DISTINCT l.tmdb_id) AS n"
             " FROM library l JOIN tmdb t ON t.tmdb_id = l.tmdb_id, json_each(t.json, '$.genres') g"
             " WHERE json_valid(t.json) GROUP BY name HAVING name IS NOT NULL AND name != '' ORDER BY n DESC, name")
        return [{"name": r["name"], "count": r["n"]} for r in conn().execute(q)]
    return list(cached("owned_genres", read))


def tmdb_put(tmdb_id, data):
    conn().execute(
        "INSERT OR REPLACE INTO tmdb(tmdb_id, json, fetched_at) VALUES (?, ?, ?)",
        (tmdb_id, json.dumps(data, separators=(",", ":")), int(time.time())),
    )


def commit():
    conn().commit()


# ---------- imdb ----------

def imdb_replace(rows_iter):
    """Swap in a fresh set of IMDb ratings. If the download or the write fails, the old ratings stay."""
    rows = iter(rows_iter)
    first = next(rows, None)      # the download happens here, before anything is deleted or locked
    if first is None:
        raise ValueError("IMDb sent an empty ratings file")
    c = conn()
    c.commit()                    # finish earlier work so a rollback below only undoes this swap
    try:
        c.execute("BEGIN")        # explicit, so rebuilding the table is part of the swap and rolls back with it
        # Rebuilt each time: installs from before 0.10.2 get the compact layout (each id stored once, not twice).
        c.execute("DROP TABLE IF EXISTS imdb_ratings")
        c.execute("CREATE TABLE imdb_ratings (imdb_id TEXT PRIMARY KEY, rating REAL, votes INTEGER) WITHOUT ROWID")
        batch, n = [first], 0
        for row in rows:
            batch.append(row)
            if len(batch) >= 50000:
                c.executemany("INSERT OR REPLACE INTO imdb_ratings VALUES (?, ?, ?)", batch)
                n += len(batch)
                batch = []
        if batch:
            c.executemany("INSERT OR REPLACE INTO imdb_ratings VALUES (?, ?, ?)", batch)
            n += len(batch)
        c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('imdb_count', ?)", (json.dumps(n),))
        c.commit()
    except BaseException:
        c.rollback()
        raise
    bump()


def imdb_count():
    """How many IMDb ratings are loaded: remembered at load time, counted once for installs that loaded before that."""
    n = _get_meta("imdb_count")
    if n is None:
        n = conn().execute("SELECT COUNT(*) FROM imdb_ratings").fetchone()[0]
        set_meta("imdb_count", n)
    return n


def imdb_get_many(imdb_ids):
    out = {}
    ids = [i for i in imdb_ids if i]
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        q = "SELECT * FROM imdb_ratings WHERE imdb_id IN (%s)" % ",".join("?" * len(chunk))
        for r in conn().execute(q, chunk):
            out[r["imdb_id"]] = (r["rating"], r["votes"])
    return out


# ---------- feedback ----------

def feedback():
    return dict(cached("feedback", lambda: {r["tmdb_id"]: dict(r) for r in conn().execute("SELECT * FROM feedback")}))


def set_feedback(tmdb_id, action, title=None, rating=None, source=None, commit=True):
    conn().execute(
        "INSERT OR REPLACE INTO feedback(tmdb_id, action, title, at, rating, source) VALUES (?, ?, ?, ?, ?, ?)",
        (tmdb_id, action, title, int(time.time()), rating, source),
    )
    if commit:
        conn().commit()
        bump_user()     # with commit=False the caller commits, then calls bump_user() itself


def set_library_rating(key, rating):
    conn().execute("UPDATE library SET user_rating=? WHERE tmdb_id=?", (rating, key))
    conn().commit()
    bump()


def delete_feedback(tmdb_id):
    conn().execute("DELETE FROM feedback WHERE tmdb_id=?", (tmdb_id,))
    conn().commit()
    bump_user()


# ---------- recs ----------

def replace_recs(recs, media):
    c = conn()
    c.execute("DELETE FROM recs WHERE tmdb_id %s 0" % ("<" if media == "tv" else ">"))
    c.executemany(
        "INSERT INTO recs(tmdb_id, score, data) VALUES (?, ?, ?)",
        [(r["key"], r["score"], json.dumps(r, separators=(",", ":"))) for r in recs],
    )
    c.commit()
    bump()


def recs(media=None):
    return list(cached(("recs", media), lambda: _recs(media)))


def recs_map(media):
    return cached(("recs_map", media), lambda: {r["key"]: r for r in recs(media)})


def _recs(media=None):
    where = "" if media is None else ("WHERE tmdb_id < 0" if media == "tv" else "WHERE tmdb_id > 0")
    out = []
    for r in conn().execute(f"SELECT tmdb_id, data FROM recs {where} ORDER BY score DESC"):
        d = json.loads(r["data"])
        d.setdefault("key", r["tmdb_id"])
        d.setdefault("media", "tv" if r["tmdb_id"] < 0 else "movie")
        out.append(d)
    return out


# ---------- full candidate pool (for scoring deeper on demand) ----------

def replace_cands(rows, media):
    c = conn()
    c.execute("DELETE FROM cands WHERE tmdb_id %s 0" % ("<" if media == "tv" else ">"))
    c.executemany("INSERT OR REPLACE INTO cands VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    c.commit()
    bump()


def cands_page(media, after_collab, after_key, limit, year_min=0, year_max=9999, min_votes=0, min_rating=0.0):
    """Candidates in descending collab order, keyset-paginated."""
    sign = "<" if media == "tv" else ">"
    q = (f"SELECT * FROM cands WHERE tmdb_id {sign} 0 AND (collab < ? OR (collab = ? AND tmdb_id > ?))"
         " AND votes >= ? AND rating >= ?")
    args = [after_collab, after_collab, after_key, min_votes, min_rating]
    if year_min:
        q += " AND year >= ?"; args.append(year_min)
    if year_max < 9999:
        q += " AND year <= ?"; args.append(year_max)
    q += " ORDER BY collab DESC, tmdb_id ASC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn().execute(q, args)]


# ---------- rediscover: owned-but-unwatched ranked by taste ----------

def replace_libpicks(rows, media):
    c = conn()
    c.execute("DELETE FROM libpicks WHERE tmdb_id %s 0" % ("<" if media == "tv" else ">"))
    c.executemany("INSERT INTO libpicks(tmdb_id, score, data) VALUES (?, ?, ?)",
                  [(r["key"], r["score"], json.dumps(r, separators=(",", ":"))) for r in rows])
    c.commit()
    bump()


def libpicks(media):
    where = "WHERE tmdb_id < 0" if media == "tv" else "WHERE tmdb_id > 0"
    return list(cached(("libpicks", media), lambda: [json.loads(r["data"]) for r in
                                                     conn().execute(f"SELECT data FROM libpicks {where} ORDER BY score DESC")]))


# ---------- watchlist & "don't learn from" ----------

def _keyed(table):
    return {r["tmdb_id"]: dict(r) for r in conn().execute(f"SELECT * FROM {table} ORDER BY at DESC")}


def watchlist():
    return dict(cached("watchlist", lambda: _keyed("watchlist")))


def set_watchlist(key, title, on=True):
    if on:
        conn().execute("INSERT OR REPLACE INTO watchlist(tmdb_id, title, at) VALUES (?, ?, ?)", (key, title, int(time.time())))
    else:
        conn().execute("DELETE FROM watchlist WHERE tmdb_id=?", (key,))
    conn().commit()
    bump_user()


def noseed():
    return dict(cached("noseed", lambda: _keyed("noseed")))


def set_noseed(key, title, on=True):
    if on:
        conn().execute("INSERT OR REPLACE INTO noseed(tmdb_id, title, at) VALUES (?, ?, ?)", (key, title, int(time.time())))
    else:
        conn().execute("DELETE FROM noseed WHERE tmdb_id=?", (key,))
    conn().commit()
    bump_user()


def close():
    """Close this thread's connection (sync threads call this when they finish)."""
    c = getattr(_local, "conn", None)
    if c is not None:
        c.close()
        _local.conn = None
