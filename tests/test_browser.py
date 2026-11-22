"""The real pages in a real browser (Chrome), against a test library, with nothing reaching the internet.

Needs Playwright and Google Chrome: pip install playwright. Skipped when either is missing, except in the build,
which sets REQUIRE_BROWSER=1 so these can never be skipped quietly there.
"""
import os
import socket
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())

import pytest  # noqa: E402

REQUIRED = os.environ.get("REQUIRE_BROWSER") == "1"
try:
    from playwright.sync_api import Error as PlaywrightError, sync_playwright  # noqa: E402
except ImportError:
    if REQUIRED:
        raise
    pytest.skip("Playwright isn't installed (pip install playwright)", allow_module_level=True)

PLEX_SECTIONS = [{"key": "1", "title": "Movies", "type": "movie"}, {"key": "2", "title": "TV Shows", "type": "show"},
                 {"key": "3", "title": "Kids", "type": "movie"}]


class FakePlexAPI:
    """Stands in for Plex in the running app: answers Test, records ratings."""
    rated = []

    def __init__(self, *a, **k):
        pass

    def _get(self, path, **params):
        return {"friendlyName": "Test Plex", "version": "1.40.0"} if path == "/" else {"Directory": PLEX_SECTIONS}

    def rate(self, rating_key, rating):
        FakePlexAPI.rated.append((rating_key, rating))

    def machine_id(self):
        return "test-machine"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def site():
    import requests
    import uvicorn
    from app import config, db
    import app.main as m
    import test_end_to_end as t

    db.init()
    s = t.Syncer(lambda: t.FakePlex(), lambda: t.FakeTMDB(), lambda: t.FakeSeerr(), lambda: iter([("tt0000025", 8.4, 50000)]))
    s.run(full=True)
    assert s.state["error"] is None, s.state
    # every "Because you watched …" row gets an apostrophe in its title, which once broke See all
    db.conn().execute("UPDATE library SET title = 'Ocean''s ' || title WHERE title NOT LIKE 'Ocean''s %'")
    db.commit()
    db.bump()

    test_settings = {"PLEX_URL": "http://plex.test:32400", "PLEX_TOKEN": "test-token", "TMDB_API_KEY": "test-key", "SYNC_HOUR": -1}
    keep = {k: getattr(config, k) for k in test_settings}
    config.save(test_settings)   # saved, not just set: saving anything else reloads every setting from disk
    real_request, real_plex, real_rescore = requests.Session.request, m.Plex, m.schedule_rescore

    def offline(self, method, url, *a, **k):
        raise requests.ConnectionError(f"offline test: {url}")
    requests.Session.request = offline          # the app can't reach TMDB, Plex or anything else
    m.Plex, m.schedule_rescore = FakePlexAPI, (lambda *a, **k: None)

    server = uvicorn.Server(uvicorn.Config(m.app, host="127.0.0.1", port=_free_port(), log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "the app didn't start"
    yield {"base": f"http://127.0.0.1:{server.config.port}/", "m": m, "config": config, "db": db}
    server.should_exit = True
    thread.join(10)
    requests.Session.request, m.Plex, m.schedule_rescore = real_request, real_plex, real_rescore
    config.save(keep)
    db.conn().execute("UPDATE library SET title = substr(title, 9) WHERE title LIKE 'Ocean''s %'")
    db.commit()
    db.bump()


@pytest.fixture(scope="module")
def browser():
    pw = sync_playwright().start()
    b = None
    for opts in ({"channel": "chrome"}, {}):   # the installed Chrome, else Playwright's own (playwright install chromium)
        try:
            b = pw.chromium.launch(**opts)
            break
        except PlaywrightError:
            continue
    if b is None:
        pw.stop()
        if REQUIRED:
            raise RuntimeError("No browser for the browser tests: install Google Chrome, or run: playwright install chromium")
        pytest.skip("No Chrome found for the browser tests")
    yield b
    b.close()
    pw.stop()


@pytest.fixture
def page(browser, site):
    """A fresh browser tab on the app. Fails the test on any script error in the page."""
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    base = site["base"]

    def only_local(route):   # posters, logos and favicons come from the internet: not in tests
        url = route.request.url
        route.continue_() if url.startswith(base) or url.startswith(("data:", "blob:")) else route.abort()
    ctx.route("**/*", only_local)
    p = ctx.new_page()
    p.errors = []
    p.on("pageerror", lambda e: p.errors.append(f"script error: {e}"))
    p.on("console", lambda msg: p.errors.append(f"console: {msg.text}")
         if msg.type == "error" and "Failed to load resource" not in msg.text else None)
    p.base = base
    yield p
    errors = list(p.errors)
    ctx.close()
    assert not errors, errors


def go(page, where):
    page.goto(page.base + "#/" + where)
    settle(page)


def settle(page):
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(150)


def view(page):
    return page.locator(".view.on").first


def test_every_page_loads(page):
    for where, sec in (("home", "v-home"), ("picks", "v-grid"), ("library", "v-grid"), ("watchlist", "v-list"),
                       ("rate", "v-rate"), ("history", "v-history"), ("settings", "v-settings"), ("gaps", "v-list"),
                       ("upcoming", "v-list"), ("search/noir", "v-list"), ("search/100%25", "v-list"),
                       ("person/100", "v-person"), ("like/movie/1", "v-list"), ("person", "v-home"), ("nonsense", "v-home")):
        go(page, where)
        assert view(page).get_attribute("id") == sec, where
        text = view(page).inner_text()
        assert text.strip() and "undefined" not in text and "[object Object]" not in text, (where, text[:200])
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "the page scrolls sideways"


def test_hiding_shows_everywhere_and_undo_brings_it_back(page):
    go(page, "picks")
    card = page.locator("#grid .card[data-key]").first
    key = card.get_attribute("data-key")
    card.hover()
    card.locator(".quick[data-hide]").click()
    settle(page)
    go(page, "home")
    assert page.locator(f'#homeRows .card[data-key="{key}"]').count() == 0, "a hidden title still shows on For You"
    go(page, "picks")
    assert page.locator(f'#grid .card[data-key="{key}"]').count() == 0
    go(page, "history")
    page.locator('#pills [data-a="dismissed"]').click()
    hidden = page.locator("#hiddenGrid .hcard")
    assert hidden.count() == 1
    hidden.first.locator("button").click()
    settle(page)
    go(page, "picks")
    assert page.locator(f'#grid .card[data-key="{key}"]').count() == 1, "restoring didn't bring it back"


def test_fast_page_switching_ends_on_the_last_page(page):
    go(page, "home")
    for v in ("watchlist", "gaps", "upcoming", "history", "rate", "picks", "library", "gaps", "watchlist"):
        page.evaluate("v => location.hash = '#/' + v", v)
    settle(page)
    page.wait_for_timeout(500)
    assert view(page).get_attribute("id") == "v-list" and page.locator("#listBody h2").first.inner_text() == "Watchlist"


def test_see_all_works_on_titles_with_an_apostrophe(page):
    go(page, "home")
    row = page.locator("#homeRows .hrow", has=page.locator("h3", has_text="Because you watched Ocean's")).first
    assert row.count() == 1, "the test library should give a 'Because you watched' row"
    row.locator("a.see").click()
    settle(page)
    assert view(page).get_attribute("id") == "v-grid" and page.locator("#grid .card").count() > 0
    assert "Ocean's" in page.locator("#activeRow").inner_text(), "See all lost which title it was for"


def test_rate_page_takes_one_rating_per_press(page, site):
    db = site["db"]
    FakePlexAPI.rated.clear()
    # the test library has rated everything it watched: un-rate a few so the Rate page has work
    unrated = [r["tmdb_id"] for r in db.library() if r["view_count"] and r["user_rating"] and r["tmdb_id"] > 0][:4]
    before = {k: db.library_map()[k]["user_rating"] for k in unrated}
    for k in unrated:
        db.set_library_rating(k, None)
    try:
        _rate_presses(page)
    finally:
        for k, v in before.items():
            db.set_library_rating(k, v)


def _rate_presses(page):
    go(page, "rate")
    title = lambda: page.locator(".rate-info h2").first.inner_text()   # noqa: E731
    first = title()
    page.keyboard.press("Control+s")      # the browser's shortcut, not "skip"
    page.wait_for_timeout(200)
    assert title() == first
    page.keyboard.press("4")
    page.keyboard.press("4")              # a fast double press rates one title, not two
    settle(page)
    assert len(FakePlexAPI.rated) == 1 and FakePlexAPI.rated[0][1] == 8
    second = title()
    assert second != first
    page.keyboard.press("Shift+Digit3")   # half stars: Shift+3 is 2½
    settle(page)
    assert FakePlexAPI.rated[-1][1] == 5 and title() != second


def test_setup_walkthrough_keeps_every_library_ticked(page, site):
    config = site["config"]
    keep = config.PLEX_URL, config.PLEX_TOKEN
    config.PLEX_URL, config.PLEX_TOKEN = "", ""     # a new install
    try:
        page.goto(page.base)
        settle(page)
        assert page.evaluate("location.hash") == "#/setup"
        page.locator('[data-wz="next"]').click()
        page.locator('[data-wz-k="PLEX_URL"]').fill("http://plex.test:32400")
        page.locator('[data-wz-k="PLEX_TOKEN"]').fill("test-token")
        page.locator('[data-wz="test"]').click()
        settle(page)
        boxes = page.locator("[data-wz-lib]")
        assert boxes.count() == 3 and all(boxes.nth(i).is_checked() for i in range(3))
        boxes.nth(1).uncheck()
        boxes.nth(1).check()                        # all ticked again means "every library"
        page.locator('[data-wz="test"]').click()    # anything that redraws the step
        settle(page)
        boxes = page.locator("[data-wz-lib]")
        assert all(boxes.nth(i).is_checked() for i in range(3)), "boxes show unticked though every library will be read"
        boxes.nth(2).uncheck()
        page.locator('[data-wz="test"]').click()
        settle(page)
        assert [page.locator("[data-wz-lib]").nth(i).is_checked() for i in range(3)] == [True, True, False]
    finally:
        config.PLEX_URL, config.PLEX_TOKEN = keep


def test_phone_layout(browser, site):
    ctx = browser.new_context(viewport={"width": 390, "height": 800}, is_mobile=True, has_touch=True)
    ctx.route("**/*", lambda r: r.continue_() if r.request.url.startswith(site["base"]) else r.abort())
    p = ctx.new_page()
    errors = []
    p.on("pageerror", lambda e: errors.append(str(e)))
    try:
        for where in ("home", "picks", "rate", "settings"):
            p.goto(site["base"] + "#/" + where)
            p.wait_for_load_state("networkidle")
            assert p.evaluate("document.documentElement.scrollWidth <= innerWidth"), f"{where} scrolls sideways on a phone"
            box = p.locator("#gsearch").bounding_box()
            assert box and box["width"] > 60 and box["x"] + box["width"] <= 390, "search should fit in the phone header"
            gear = p.locator("#gear").bounding_box()
            assert abs(gear["y"] - box["y"]) < 12 and gear["x"] + gear["width"] <= 390, "the header should stay on one line on a phone"
        assert not errors, errors
    finally:
        ctx.close()
