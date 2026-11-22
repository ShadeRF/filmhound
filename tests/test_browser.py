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


def lib_modes(page, attr):
    """{library: chosen mode} as the Use / Own only / Ignore buttons show it."""
    return page.evaluate(f"""Object.fromEntries([...document.querySelectorAll('[{attr}].on')]
        .map(b => [b.getAttribute('{attr}'), b.dataset.mode]))""")


def test_setup_walkthrough_libraries_use_own_only_or_ignore(page, site):
    config = site["config"]
    keep = {k: getattr(config, k) for k in ("PLEX_URL", "PLEX_TOKEN", "PLEX_LIBRARIES", "NOSEED_LIBRARIES")}
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
        assert lib_modes(page, "data-wz-lib") == {"Movies": "use", "TV Shows": "use", "Kids": "use"}, "every library starts on Use"
        rows = page.locator(".wz-libs .librow")
        assert rows.count() == 3 and rows.nth(2).locator(".libname").inner_text().strip() == "Kids Movies"
        assert rows.nth(0).locator(".libname").inner_text().strip() == "Movies", "the name already says Movies"
        assert "Own only counts as yours" in page.locator(".wz-libs").inner_text()
        page.click('[data-wz-lib="Kids"][data-mode=own]')
        page.click('[data-wz-lib="TV Shows"][data-mode=ignore]')
        page.locator('[data-wz="test"]').click()    # anything that redraws the step keeps the choices
        settle(page)
        assert lib_modes(page, "data-wz-lib") == {"Movies": "use", "TV Shows": "ignore", "Kids": "own"}
        # what the walkthrough saves with the TMDB step
        assert page.evaluate("[wz.vals.PLEX_LIBRARIES, wz.vals.NOSEED_LIBRARIES]") == [["Movies", "Kids"], ["Kids"]]
    finally:
        for k, v in keep.items():
            setattr(config, k, v)


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


def test_setup_library_rows_line_up_with_the_fields(page, site):
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
        rows, url = page.locator(".wz-libs .librows").bounding_box(), page.locator('[data-wz-k="PLEX_URL"]').bounding_box()
        assert abs(rows["x"] - url["x"]) < 4, "the library rows are indented past the fields"
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


def test_dont_learn_from_says_what_is_left_out(page):
    go(page, "settings")
    sec = page.locator("#set-taste")
    for label in ("Genres to leave out", "Titles left out"):
        assert label in sec.inner_text(), label
    assert "Libraries to leave out" not in sec.inner_text(), "libraries are chosen on the Plex card now"
    assert page.locator("#nsSum").inner_text() == "Nothing is left out yet."
    page.click("[data-nsg=Horror]")
    assert page.locator("#nsSum").inner_text() == "Left out: Horror"
    assert "on" in page.get_attribute("[data-nsg=Horror]", "class")   # the chosen chip is highlighted
    page.click("[data-nsg=Horror]")
    assert page.locator("#nsSum").inner_text() == "Nothing is left out yet."


def test_plex_card_saves_use_own_only_and_ignore(page, site):
    config = site["config"]
    keep = {k: getattr(config, k) for k in ("PLEX_LIBRARIES", "NOSEED_LIBRARIES")}
    def plex_open():
        page.evaluate("document.getElementById('svc-plex').open = true")
        page.wait_for_selector("#plexLibs .librow")
    try:
        go(page, "settings")
        plex_open()
        assert lib_modes(page, "data-lib") == {"Movies": "use", "TV Shows": "use", "Kids": "use"}
        help_ = page.locator("#plexLibs .help").inner_text()
        assert help_ == ("Use shapes your picks. Own only counts as yours, so nothing in it is suggested to you, but it doesn't "
                         "shape your picks: good for a kids' library or someone else's shows. Ignore isn't read at all.")

        def save():
            page.click("#saveBtn")
            settle(page)
            return page.locator("#toast").inner_text()
        # Own only: still read, left out of your taste, named in the summary
        page.click('[data-lib="Kids"][data-mode=own]')
        assert page.locator("#savebar").evaluate("el => el.classList.contains('on')"), "the change didn't mark Settings unsaved"
        assert page.locator("#nsSum").inner_text() == "Left out: the Kids library (own only)"
        assert "Sync now to apply" in save()
        assert config.PLEX_LIBRARIES == [] and config.NOSEED_LIBRARIES == ["Kids"]
        # Ignore: not read, and no longer own only
        plex_open()
        page.click('[data-lib="Kids"][data-mode=ignore]')
        assert page.locator("#nsSum").inner_text() == "Nothing is left out yet."
        assert "Sync now to apply" in save()
        assert config.PLEX_LIBRARIES == ["Movies", "TV Shows"] and config.NOSEED_LIBRARIES == []
        # the last library can't be ignored: nothing would be read
        plex_open()
        page.click('[data-lib="Movies"][data-mode=ignore]')
        page.click('[data-lib="TV Shows"][data-mode=ignore]')
        assert "at least one library" in page.locator("#toast").inner_text()
        assert lib_modes(page, "data-lib") == {"Movies": "ignore", "TV Shows": "use", "Kids": "ignore"}
        page.click("#discard")
        # Use again for everything: an empty list means every library
        plex_open()
        page.click('[data-lib="Kids"][data-mode=use]')
        save()
        assert config.PLEX_LIBRARIES == [] and config.NOSEED_LIBRARIES == []
        # older setups carry over: unticked libraries show as Ignore, left-out ones as Own only
        config.save({"PLEX_LIBRARIES": ["movies", "Kids"], "NOSEED_LIBRARIES": ["kids"]})
        page.reload()
        settle(page)
        plex_open()
        assert lib_modes(page, "data-lib") == {"Movies": "use", "TV Shows": "ignore", "Kids": "own"}
        assert page.locator("#nsSum").inner_text() == "Left out: the Kids library (own only)"
    finally:
        config.save(keep)


def test_plex_card_library_rows_on_a_phone(browser, site):
    ctx = browser.new_context(viewport={"width": 390, "height": 800}, is_mobile=True, has_touch=True)
    ctx.route("**/*", lambda r: r.continue_() if r.request.url.startswith(site["base"]) else r.abort())
    p = ctx.new_page()
    try:
        p.goto(site["base"] + "#/settings")
        p.wait_for_load_state("networkidle")
        p.evaluate("document.getElementById('svc-plex').open = true")
        p.wait_for_selector("#plexLibs .librow")
        row = p.locator("#plexLibs .librow").nth(2)
        name, seg = row.locator(".libname").bounding_box(), row.locator(".seg").bounding_box()
        assert seg["y"] >= name["y"] + name["height"] - 1, "the buttons should sit under the name on a phone"
        assert seg["x"] + seg["width"] <= 390 and p.evaluate("document.documentElement.scrollWidth <= innerWidth")
    finally:
        ctx.close()


def test_more_genres_lists_every_genre_you_own(page):
    go(page, "settings")
    page.wait_for_selector("[data-nsg-more]")
    assert page.locator("[data-nsg-more]").inner_text() == "More genres…"
    assert page.locator(".nsg-all").count() == 0
    page.click("[data-nsg-more]")
    chips = page.locator(".nsg-all [data-nsg]")
    assert [chips.nth(i).inner_text().split()[0] for i in range(chips.count())] == ["Crime", "Comedy"], "most common first"
    assert page.locator(".nsg-all [data-nsg=Comedy] small").inner_text() == "2"
    assert "Comedy" not in page.locator("#nsgQuick").inner_text()
    page.click(".nsg-all [data-nsg=Comedy]")
    assert "on" in page.get_attribute(".nsg-all [data-nsg=Comedy]", "class")
    assert page.locator("#nsSum").inner_text() == "Left out: Comedy"
    assert page.locator("#savebar").evaluate("el => el.classList.contains('on')")
    assert page.locator("[data-nsg-more]").inner_text() == "Fewer genres"
    page.click("[data-nsg-more]")
    assert page.locator(".nsg-all").count() == 0
    quick = page.locator("#nsgQuick [data-nsg=Comedy]")
    assert quick.count() == 1 and "on" in quick.get_attribute("class"), "a chosen genre is hidden when the list is folded"
    page.click("#discard")
    assert page.locator("#nsgQuick [data-nsg=Comedy]").count() == 0


def test_more_genres_before_the_first_sync(page):
    page.route("**/api/noseed/genres", lambda r: r.fulfill(json={"genres": []}))
    go(page, "settings")
    page.wait_for_selector(".nsg-none")
    assert page.locator(".nsg-none").inner_text() == "Sync once to list your genres."
    assert page.locator("[data-nsg-more]").count() == 0


def test_up_next_tab_switches_between_collections_and_coming_soon(page):
    go(page, "home")
    tabs = page.evaluate("[...document.querySelectorAll('#nav button')].map(b => b.textContent)")
    assert tabs == ["For You", "All Picks", "Up next", "Your Library", "Watchlist", "Rate", "History"]
    assert page.locator("#subnav").is_hidden()
    page.click('#nav [data-view="next"]')
    settle(page)
    assert page.evaluate("location.hash") == "#/gaps", "Up next opens Collections first"
    tab = page.locator('#nav [data-view="next"]')
    switch = page.locator("#subnav")
    assert "on" in tab.get_attribute("class") and switch.is_visible()
    assert switch.locator("button").all_inner_texts() == ["Collections", "Coming soon"]
    assert switch.locator("button.on").inner_text() == "Collections"
    assert page.locator("#listBody h1, #listBody h2").first.inner_text() == "Finish what you started"
    where = switch.bounding_box()
    switch.locator("button", has_text="Coming soon").click()
    settle(page)
    assert page.evaluate("location.hash") == "#/upcoming"
    assert "on" in tab.get_attribute("class") and switch.locator("button.on").inner_text() == "Coming soon"
    assert page.locator("#listBody h1, #listBody h2").first.inner_text() == "Coming soon for you"
    assert switch.bounding_box() == where, "the switch row moved between the two pages"
    switch.locator("button", has_text="Collections").click()
    settle(page)
    assert page.evaluate("location.hash") == "#/gaps" and switch.bounding_box() == where
    # old addresses still work, and #/next opens the first page
    for addr, on in (("upcoming", "Coming soon"), ("gaps", "Collections"), ("next", "Collections")):
        go(page, "home")
        go(page, addr)
        assert "on" in tab.get_attribute("class") and switch.locator("button.on").inner_text() == on, addr
    go(page, "picks")
    assert switch.is_hidden() and "on" not in (tab.get_attribute("class") or "")


def test_up_next_switch_on_a_phone(browser, site):
    ctx = browser.new_context(viewport={"width": 390, "height": 800}, is_mobile=True, has_touch=True)
    ctx.route("**/*", lambda r: r.continue_() if r.request.url.startswith(site["base"]) else r.abort())
    p = ctx.new_page()
    errors = []
    p.on("pageerror", lambda e: errors.append(str(e)))
    try:
        p.goto(site["base"] + "#/upcoming")
        p.wait_for_load_state("networkidle")
        p.wait_for_timeout(200)
        sw = p.locator("#subnav")
        assert sw.is_visible() and p.evaluate("document.documentElement.scrollWidth <= innerWidth")
        # too narrow for its buttons: it scrolls sideways and fades its right edge, like the filter bar
        p.evaluate("const s = document.getElementById('subnav'); s.style.width = '150px'; dispatchEvent(new Event('resize'))")
        p.wait_for_timeout(100)
        assert p.evaluate("document.getElementById('subnav').classList.contains('fade-right')")
        p.evaluate("document.getElementById('subnav').scrollLeft = 99999")
        p.wait_for_timeout(100)
        assert not p.evaluate("document.getElementById('subnav').classList.contains('fade-right')")
        p.evaluate("document.getElementById('subnav').style.width = ''")
        # it tucks away with the tabs while scrolling down, and comes back scrolling up
        p.evaluate("document.querySelector('main').style.minHeight = '4000px'")
        for _ in range(4):
            p.mouse.wheel(0, 300)
            p.wait_for_timeout(150)
        assert p.locator("#nav").is_hidden() and sw.is_hidden()
        p.mouse.wheel(0, -200)
        p.wait_for_timeout(150)
        assert p.locator("#nav").is_visible() and sw.is_visible()
        assert not errors, errors
    finally:
        ctx.close()


def test_a_collection_with_nothing_left_disappears_after_not_interested(page):
    go(page, "gaps")
    page.evaluate('''() => {
        const box = document.getElementById('listBody');
        box.innerHTML = `<div class="gap" id="g1"><div class="hscroll"><div class="card" data-key="1"></div></div></div>
                         <div class="gap" id="g2"><div class="hscroll"><div class="card" data-key="2"></div><div class="card" data-key="3"></div></div></div>`;
        document.querySelector('[data-key="1"]').remove(); pruneEmptyGroups();
    }''')
    assert page.locator("#g1").count() == 0 and page.locator("#g2").count() == 1   # the emptied collection is gone, the other stays
    page.evaluate("() => { document.querySelectorAll('#g2 .card').forEach(c => c.remove()); pruneEmptyGroups(); }")
    assert "Your collections are complete" in page.locator("#listBody").inner_text()


def test_row_arrows_scroll_on_every_page(page):
    go(page, "home")
    row = page.locator("#homeRows .hwrap").first
    row.hover()
    row.locator(".scroll-btn.r").click()
    page.wait_for_function("document.querySelector('#homeRows .hscroll').scrollLeft > 0")
    go(page, "gaps")   # Finish what you started builds its rows with the same arrows
    assert page.evaluate("loadGaps.toString().includes('scroll-btn r')")
