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


def test_saved_keys_show_only_their_last_characters_and_typed_keys_can_be_revealed(page):
    go(page, "settings")
    page.evaluate("document.getElementById('svc-plex').open = true")
    field = page.locator('#settingsBody [data-k="PLEX_TOKEN"]')
    eye = field.locator("xpath=following-sibling::button[@data-eye]")
    note = page.locator("#svc-plex .saved-note")
    # the saved token never reaches the page; its last four characters show so you can tell which one it is
    assert "test-token" not in page.content()
    assert field.get_attribute("type") == "text" and field.input_value() == "••••••••oken"
    assert eye.is_hidden() and note.is_visible()
    # a new key stays hidden while you type, and the eye shows it only on request
    field.fill("brand-new-token")
    assert field.get_attribute("type") == "password" and eye.is_visible() and note.is_hidden()
    eye.click()
    assert field.get_attribute("type") == "text" and eye.get_attribute("aria-pressed") == "true"
    eye.click()
    assert field.get_attribute("type") == "password"
    field.fill("")
    assert eye.is_hidden()


def test_rate_page_seen_it_without_a_rating_is_k(page, site):
    db = site["db"]
    go(page, "rate")
    page.locator('#rateSeg [data-mode="picks"]').click()
    settle(page)
    seen = page.locator('[data-ra="seen"]')
    assert seen.inner_text().startswith("Seen it") and seen.locator("kbd").inner_text() == "K"
    assert "K" in page.locator(".rate-help .keys-help").first.inner_text()
    title = lambda: page.locator(".rate-info h2").first.inner_text()   # noqa: E731
    first = title()
    before = set(db.feedback())
    page.keyboard.press("k")
    settle(page)
    added = [k for k in db.feedback() if k not in before]
    try:
        assert title() != first and len(added) == 1
        assert db.feedback()[added[0]]["action"] == "seen" and db.feedback()[added[0]]["rating"] is None
    finally:
        for k in added:
            db.delete_feedback(k)
        db.commit()
        db.bump()


def test_drawer_wording_and_why_this_pick_bars(page):
    go(page, "picks")
    page.locator("#grid .card[data-key]").first.click()
    settle(page)
    actions = page.locator("#drawer .d-actions").inner_text()
    assert "Seen it" in actions and "Not interested" in actions and "I've seen it" not in actions
    assert page.locator("#drawer .seed .pct").count() == 0, "the percentage next to each reason should be gone"
    meters = page.locator("#drawer .seed .meter")
    assert meters.count() > 0 and meters.first.get_attribute("aria-label") == "How much this pushed the pick"
    page.locator("#drawer .d-actions button", has_text="Not interested").click()
    page.wait_for_timeout(300)
    assert page.locator("#toast").inner_text().startswith("Not interested in ")
    page.locator("#toast button", has_text="Undo").click()
    settle(page)


def test_settings_order_icons_and_checkboxes(page):
    go(page, "settings")
    order = page.evaluate("[...document.querySelectorAll('#setnav [data-jump]')].map(b => b.dataset.jump)")
    assert order == ["connections", "history", "taste", "picks", "plex", "sync", "signin"]
    for card in ("plex", "tmdb", "seerr", "radarr", "sonarr", "letterboxd", "trakt"):
        assert page.locator(f"#svc-{card} summary img.svc").count() == 1, f"{card} has no icon"
    assert page.locator("#settingsBody .switch").count() == 0
    assert page.locator('#settingsBody .opt input[type=checkbox][data-k="IMDB_ENABLED"]').count() == 1
    # Plex test: name, version, then what was found
    page.evaluate("document.getElementById('svc-plex').open = true")
    page.locator('[data-test="plex"]').click()
    settle(page)
    assert page.locator("#tres-plex").inner_text() == "✓ Connected to Test Plex v1.40.0 · 3 libraries"
    # Picks to keep: 0 means keep all, and the slider can get back to it
    page.evaluate("document.querySelector('[data-fold=picks-adv]').open = true")
    for k in ("MAX_RECS", "MAX_RECS_TV"):
        slider = page.locator(f'#settingsBody input[type=range][data-k="{k}"]')
        assert slider.get_attribute("min") == "0"
        out = slider.locator("xpath=following-sibling::output")
        slider.evaluate("el => { el.value = 500; el.dispatchEvent(new Event('input', { bubbles: true })); }")
        assert out.inner_text() == "500"
        slider.evaluate("el => { el.value = 0; el.dispatchEvent(new Event('input', { bubbles: true })); }")
        assert out.inner_text() == "All"
    page.locator("#discard").click()
    # Reset all feedback asks first and says what it clears; saying no changes nothing
    asked = []
    page.once("dialog", lambda d: (asked.append(d.message), d.dismiss()))
    page.locator('[data-maint="clear-feedback"]').click()
    settle(page)
    assert page.locator('[data-maint="clear-feedback"]').inner_text() == "Reset all feedback"
    assert asked and all(w in asked[0] for w in ("Not interested", "Seen it", "Letterboxd", "Trakt", "Requested", "Plex"))
    assert page.locator(".resetfb .muted").inner_text().endswith(("title", "titles"))


def test_your_library_page(page):
    go(page, "library")
    page.wait_for_selector("#libHead h2")
    assert page.locator("#libHead h2").inner_text() == "Your Library"
    sub = page.locator("#libHead p").inner_text()
    assert "you own but haven't watched, best for you first" in sub
    n = int(sub.split()[0].replace(",", ""))
    sizes = page.evaluate("[...document.querySelectorAll('#collN option')].map(o => +o.value)")
    assert sizes and max(sizes) <= n and (n > 250 or n in sizes), (n, sizes)
    assert page.locator("#libHead label", has_text="Collection size").count() == 1
    assert page.locator("#pushColl").inner_text() == "Make Plex collection"
    assert "unwatched" not in page.locator("#count").inner_text()


def test_header_search_clears_when_you_leave_search(page):
    go(page, "search/noir")
    assert page.locator("#gq").input_value() == "noir"
    go(page, "picks")
    assert page.locator("#gq").input_value() == ""


def test_coming_soon_shows_the_date_once(page):
    go(page, "upcoming")
    cards = page.locator("#listBody .card[data-key]")
    assert cards.count() > 0, "the test library should have something coming soon"
    for i in range(cards.count()):
        c = cards.nth(i)
        badge = c.locator(".match").inner_text()
        assert badge, "the date belongs on the poster"
        assert badge not in c.inner_text().replace(badge, "", 1), "the date shows twice"


def test_empty_watchlist_never_says_hover(page):
    go(page, "watchlist")
    assert "hover" not in view(page).inner_text().lower()


def test_setup_library_chips_line_up_with_the_fields(page, site):
    config = site["config"]
    keep = config.PLEX_URL, config.PLEX_TOKEN
    config.PLEX_URL, config.PLEX_TOKEN = "", ""
    try:
        page.goto(page.base)
        settle(page)
        page.locator('[data-wz="next"]').click()
        page.locator('[data-wz-k="PLEX_URL"]').fill("http://plex.test:32400")
        page.locator('[data-wz-k="PLEX_TOKEN"]').fill("test-token")
        page.locator('[data-wz="test"]').click()
        settle(page)
        chips, url = page.locator(".wz-libs").bounding_box(), page.locator('[data-wz-k="PLEX_URL"]').bounding_box()
        assert abs(chips["x"] - url["x"]) < 4, "the library chips are indented past the fields"
    finally:
        config.PLEX_URL, config.PLEX_TOKEN = keep


def test_phone_header_tucks_away_while_scrolling_down(browser, site):
    ctx = browser.new_context(viewport={"width": 390, "height": 800}, is_mobile=True, has_touch=True)
    ctx.route("**/*", lambda r: r.continue_() if r.request.url.startswith(site["base"]) else r.abort())
    p = ctx.new_page()
    errors = []
    p.on("pageerror", lambda e: errors.append(str(e)))
    try:
        p.goto(site["base"] + "#/picks")
        p.wait_for_load_state("networkidle")
        p.wait_for_timeout(200)
        # filter buttons stay on one line, and the right edge fades while more are off-screen
        assert p.evaluate("[...document.querySelectorAll('#filters .fbtn')].every(b => getComputedStyle(b).whiteSpace === 'nowrap')")
        assert p.evaluate("document.getElementById('filters').classList.contains('fade-right')")
        p.evaluate("document.getElementById('filters').scrollLeft = 99999")
        p.wait_for_timeout(100)
        assert not p.evaluate("document.getElementById('filters').classList.contains('fade-right')")
        # the first card isn't hidden under the header
        first = p.locator("#grid .card").first.bounding_box()
        assert first["y"] >= p.locator("header").bounding_box()["height"] - 1
        for _ in range(4):
            p.mouse.wheel(0, 300)
            p.wait_for_timeout(150)
        assert p.locator("#nav").is_hidden() and p.locator("#filters").is_hidden()
        assert p.locator("#gsearch").is_visible() and p.locator("#gear").is_visible()
        assert p.locator("#gear").bounding_box()["y"] >= 0, "the top bar should stay pinned"
        p.mouse.wheel(0, -200)
        p.wait_for_timeout(150)
        assert p.locator("#nav").is_visible() and p.locator("#filters").is_visible()
        # touch screens: no keyboard help on the Rate page, and "Tap" rather than "Click"
        p.goto(site["base"] + "#/rate")
        p.wait_for_load_state("networkidle")
        help_text = p.locator(".rate-help").first.inner_text()
        assert "Tap the left half" in help_text and "Click" not in help_text and "Keys:" not in help_text
        assert not errors, errors
    finally:
        ctx.close()
