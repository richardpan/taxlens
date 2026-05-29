"""Capture per-section PNG screenshots of docs/mockups.html into docs/screenshots/."""
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
MOCKUP = ROOT / "docs" / "mockups.html"
OUT = ROOT / "docs" / "screenshots"
OUT.mkdir(parents=True, exist_ok=True)

SECTIONS = [
    ("import", "01-import.png"),
    ("dashboard", "02-dashboard.png"),
    ("year", "03-year-detail.png"),
    ("math", "04-show-the-math.png"),
    ("compare", "05-compare.png"),
]

with sync_playwright() as p:
    browser = p.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1280, "height": 900},
                              device_scale_factor=2)
    page = ctx.new_page()
    page.goto(MOCKUP.as_uri())
    page.wait_for_timeout(2500)
    for sid, fname in SECTIONS:
        el = page.locator(f"#{sid}")
        el.scroll_into_view_if_needed()
        page.wait_for_timeout(400)
        el.screenshot(path=str(OUT / fname))
        print(f"wrote {fname}")
    browser.close()
print("done")
