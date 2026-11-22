"""Shared filter parsing/application for picks, deeper picks and your-library lists."""
from dataclasses import dataclass, field

from fastapi import Query

from . import config


def _csv(v):
    return [x for x in (v or "").split(",") if x]


@dataclass
class Filters:
    genres: list = field(default_factory=list)
    genre_mode: str = "any"
    exclude_genres: list = field(default_factory=list)
    year_min: int = 0
    year_max: int = 9999
    min_rating: float = 0
    min_votes: int = 0
    max_runtime: int = 0
    languages: list = field(default_factory=list)
    networks: list = field(default_factory=list)
    streaming: str = ""        # "mine" = only titles streaming on your services
    seed: int = 0              # "because you watched X" (internal key)
    match: str = ""            # a taste label, e.g. "neo-noir"
    sort: str = "score"
    q: str = ""

    def __getitem__(self, k):   # dict-style access for older call sites
        return getattr(self, k)


def parse(genres: str = "", genre_mode: str = "any", exclude_genres: str = "", year_min: int = 0,
          year_max: int = 9999, min_rating: float = 0, min_votes: int = 0, max_runtime: int = 0,
          languages: str = "", networks: str = "", streaming: str = "", seed: int = 0, match: str = "",
          sort: str = "score", q: str = Query("")) -> Filters:
    return Filters(_csv(genres), genre_mode, _csv(exclude_genres), year_min, year_max, min_rating, min_votes,
                   max_runtime, _csv(languages), _csv(networks), streaming, seed, match, sort, q)


def streams_on_mine(r):
    mine = set(config.MY_PROVIDERS)
    p = r.get("providers") or {}
    have = {str(x["id"]) for k in ("flatrate", "free", "ads") for x in p.get(k, [])}
    return bool(mine & have)


def passes(r, f: Filters):
    g = set(r.get("genres") or [])
    inc, exc = set(f.genres), set(f.exclude_genres)
    if inc and not (inc <= g if f.genre_mode == "all" else inc & g):
        return False
    if exc & g:
        return False
    y = r.get("year")
    if (f.year_min or f.year_max < 9999) and not (y and f.year_min <= y <= f.year_max):
        return False
    if f.min_rating and (r.get("imdb_rating") or r.get("tmdb_rating") or 0) < f.min_rating:
        return False
    if f.min_votes and (r.get("imdb_votes") or r.get("tmdb_votes") or 0) < f.min_votes:
        return False
    if f.max_runtime and r.get("runtime") and r["runtime"] > f.max_runtime:
        return False
    if f.languages and r.get("language") not in f.languages:
        return False
    if f.networks and not set(f.networks) & set(r.get("networks") or []):
        return False
    if f.streaming == "mine" and not streams_on_mine(r):
        return False
    if f.seed and not any(b.get("tmdb_id") == f.seed for b in (r.get("because") or []) if isinstance(b, dict)):
        return False
    if f.match and f.match not in (r.get("matches") or []):
        return False
    if f.q:
        ql = f.q.lower()
        people = (r.get("directors") or []) + (r.get("creators") or [])
        if ql not in (r.get("title") or "").lower() and not any(ql in (p or "").lower() for p in people):
            return False
    return True


SORTS = {
    "score": lambda r: -(r.get("score") or 0),
    "rating": lambda r: -(r.get("imdb_rating") or r.get("tmdb_rating") or 0),
    "newest": lambda r: -(r.get("year") or 0),
    "oldest": lambda r: (r.get("year") or 9999),
    "obscure": lambda r: (r.get("popularity") or 0) - 40 * (r.get("score") or 0),
    "popular": lambda r: -(r.get("popularity") or 0),
    "added": lambda r: -(r.get("added_at") or 0),
}


def apply(rows, f: Filters):
    rows = [r for r in rows if passes(r, f)]
    if f.seed:   # strongest connection to that title first
        rows.sort(key=lambda r: -next((b.get("share", 0) for b in r.get("because") or [] if b.get("tmdb_id") == f.seed), 0) * r["score"])
    else:
        rows.sort(key=SORTS.get(f.sort, SORTS["score"]))
    return rows
