"""Settings: built-in defaults, overridden by environment variables, overridden by
whatever was saved from the Settings page (DATA_DIR/settings.json).

Other modules read values as module attributes (config.PLEX_URL), so saving new
settings takes effect immediately without a restart.
"""
import json
import os
import sys
import threading

DATA_DIR = os.environ.get("DATA_DIR", "/data")

# name: (type, default, secret)
FIELDS = {
    "PLEX_URL": (str, "", False),
    "PLEX_TOKEN": (str, "", True),
    "PLEX_LIBRARIES": (list, [], False),     # empty = every movie and TV library
    "TMDB_API_KEY": (str, "", True),         # v3 key or v4 read token (eyJ...)
    "SEERR_URL": (str, "", False),
    "SEERR_API_KEY": (str, "", True),
    "RADARR_URL": (str, "", False),
    "RADARR_API_KEY": (str, "", True),
    "SONARR_URL": (str, "", False),
    "SONARR_API_KEY": (str, "", True),
    "IMDB_ENABLED": (bool, True, False),
    "SYNC_HOUR": (int, 4, False),            # -1 disables the nightly sync
    "MIN_VOTES": (int, 150, False),          # for recent films; older decades need far fewer (see engine.votes_needed)
    "MIN_VOTES_TV": (int, 60, False),
    "CANDIDATE_POOL": (int, 4000, False),
    "CANDIDATE_POOL_TV": (int, 2000, False),
    "MAX_RECS": (int, 0, False),             # 0 = keep every scored candidate
    "MAX_RECS_TV": (int, 0, False),
    "OWNED_WEIGHT": (float, 0.8, False),
    "NOSEED_LIBRARIES": (list, [], False),   # Plex libraries that count as owned but don't shape your taste
    "NOSEED_GENRES": (list, [], False),      # genres whose titles don't shape your taste (e.g. Family, Animation)     # a curated library is a strong taste signal, even unwatched
    "WATCH_REGION": (str, "US", False),
    "MY_PROVIDERS": (list, [], False),        # TMDB provider ids you subscribe to
    "LETTERBOXD_USERNAME": (str, "", False),
    "RATINGS_TO_PLEX": (str, "off", False),  # off | fill (only unrated in Plex) | overwrite
    "TRAKT_CLIENT_ID": (str, "", True),
    "TRAKT_USERNAME": (str, "", False),
    "AUTH_USERNAME": (str, "", False),
    "AUTH_PASSWORD_HASH": (str, "", True),    # set through the Settings page, never stored in plain text
    "AUTH_SECRET": (str, "", True),
    "PLEX_COLLECTION": (str, "Filmhound: Watch Next", False),
    "TMDB_WORKERS": (int, 8, False),
    "TMDB_CACHE_DAYS": (int, 30, False),
}
URL_FIELDS = {"PLEX_URL", "SEERR_URL", "RADARR_URL", "SONARR_URL"}

_lock = threading.Lock()
_mod = sys.modules[__name__]


def _settings_path():
    return os.path.join(DATA_DIR, "settings.json")


def coerce(name, value):
    typ = FIELDS[name][0]
    if typ is bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if typ is list:
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        return [v.strip() for v in str(value).split(",") if v.strip()]
    if typ is int:
        return int(float(value))
    if typ is float:
        return float(value)
    v = str(value).strip()
    return v.rstrip("/") if name in URL_FIELDS else v


def _saved():
    try:
        with open(_settings_path()) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def sources():
    """Where each value currently comes from: default / env / saved."""
    saved = _saved()
    return {n: "saved" if n in saved else "env" if n in os.environ else "default" for n in FIELDS}


def load():
    saved = _saved()
    for name, (typ, default, _) in FIELDS.items():
        value = default
        if name in os.environ:
            try:
                value = coerce(name, os.environ[name])
            except ValueError:
                pass
        if name in saved:
            try:
                value = coerce(name, saved[name])
            except ValueError:
                pass
        setattr(_mod, name, value)


def save(updates):
    """Persist changed values. Returns the list of fields that changed."""
    with _lock:
        saved = _saved()
        changed = []
        for name, value in updates.items():
            if name not in FIELDS:
                continue
            v = coerce(name, value)
            if v != getattr(_mod, name):
                changed.append(name)
            saved[name] = v
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = _settings_path() + ".tmp"
        with open(tmp, "w") as f:
            json.dump(saved, f, indent=2)
        os.chmod(tmp, 0o600)   # holds API keys and tokens
        os.replace(tmp, _settings_path())
        load()
        return changed


def mask(value):
    if not value:
        return ""
    return "•" * 8 + value[-4:] if len(value) > 6 else "•" * 8


def public():
    """Settings for the UI, with secrets masked."""
    out = {}
    for name, (_, _, secret) in FIELDS.items():
        v = getattr(_mod, name)
        out[name] = mask(v) if secret else v
    return out


def is_masked(value):
    return isinstance(value, str) and value.startswith("•" * 8)


def configured():
    return bool(getattr(_mod, "PLEX_URL") and getattr(_mod, "PLEX_TOKEN") and getattr(_mod, "TMDB_API_KEY"))


load()
