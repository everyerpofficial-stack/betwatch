"""End-to-end browser test of BetWatch. Needs a running server with data and Playwright's Chromium.
Run: BETWATCH_E2E_URL=http://127.0.0.1:8000 pytest tests/test_e2e.py
"""
import os
import pytest

URL = os.environ.get("BETWATCH_E2E_URL")
pytestmark = pytest.mark.skipif(not URL, reason="set BETWATCH_E2E_URL to a running server")


def test_dashboard_flows():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))

        # 1. Dashboard load & theme toggle
        pg.goto(URL + "/?time=all", wait_until="domcontentloaded")
        pg.wait_for_selector(".row")
        assert pg.locator(".kpi").count() == 7

        # Theme toggle
        theme_btn = pg.locator("#theme-toggle")
        if theme_btn.is_visible():
            initial_theme = pg.locator("html").get_attribute("data-theme") or "light"
            theme_btn.click()
            new_theme = pg.locator("html").get_attribute("data-theme")
            assert new_theme != initial_theme
            theme_btn.click()  # switch back

        # 2. Hover shows summary preview with 150ms delay
        first_row = pg.locator(".row").first
        first_row.hover()
        pg.wait_for_timeout(200)
        assert first_row.locator(".pop").is_visible()
        assert first_row.locator(".prow").count() >= 1

        # 3. Active filter chips
        pg.fill("#subject", "Mahadev")
        pg.wait_for_timeout(450)
        pg.wait_for_selector("#active-filter-chips:not([hidden])")
        assert pg.locator("#active-chips-container .act-chip").count() >= 1
        # Click clear on active chip
        pg.click("#active-chips-container .act-chip-remove >> nth=0")
        pg.wait_for_timeout(400)

        # 4. Channel check / ranking click
        pg.click("[data-tab=channels]")
        pg.wait_for_selector("#view .lst")
        pg.locator("#view .lst:not(.zero)").first.click()
        pg.wait_for_selector("text=YES — COVERED")

        # 5. Check all tabs render cleanly
        for tab in ("journos", "az", "live", "video", "social", "feed"):
            pg.click(f"[data-tab={tab}]")
            pg.wait_for_selector("#view")

        # 6. Story Details page (/story/{id})
        story_row = pg.locator(".row").first
        story_id = story_row.get_attribute("data-story")
        if story_id:
            pg.goto(f"{URL}/story/{story_id}", wait_until="domcontentloaded")
            pg.wait_for_selector(".story-title")
            assert pg.locator(".summary-box").is_visible()
            assert pg.locator(".posts-table").is_visible()

        # 7. Channel Profile page (/channel/{id})
        pg.goto(f"{URL}/channel/1", wait_until="domcontentloaded")
        pg.wait_for_selector("h1")
        assert pg.locator(".prof-grid").is_visible()

        # 8. Report endpoints respond with valid PDFs
        resp_subj = pg.request.get(f"{URL}/api/reports/subject.pdf?time=all")
        assert resp_subj.status == 200
        assert resp_subj.headers.get("content-type") == "application/pdf"
        assert resp_subj.body().startswith(b"%PDF-")

        resp_ch = pg.request.get(f"{URL}/api/reports/channel/1.pdf")
        assert resp_ch.status == 200
        assert resp_ch.headers.get("content-type") == "application/pdf"
        assert resp_ch.body().startswith(b"%PDF-")

        assert not errors, f"Browser console errors encountered: {errors}"
        b.close()
