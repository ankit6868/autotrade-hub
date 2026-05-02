"""Capture full-page screenshots of every AutoTrade Hub page for the
feature/use-case PDF."""
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

OUT = Path(__file__).parent / "screenshots"
OUT.mkdir(exist_ok=True)

PAGES = [
    ("01-dashboard",           "/",                   "Dashboard"),
    ("02-setup",               "/setup",              "Setup Wizard"),
    ("03-upload-strategy",     "/strategy/upload",    "Upload Strategy"),
    ("04-strategy-editor",     "/strategy/editor",    "Strategy Editor"),
    ("05-strategy-templates",  "/strategy/templates", "Strategy Templates"),
    ("06-opportunities",       "/opportunities",      "Opportunity Scanner"),
    ("07-backtest",            "/backtest",           "Backtesting"),
    ("08-paper-trade",         "/paper-trade",        "Paper Trading"),
    ("09-live-trading",        "/live",               "Live Trading"),
    ("10-auto-trade",          "/auto-trade",         "Auto-Trade Engine"),
    ("11-history",             "/history",            "Trade History"),
]

BASE = "http://localhost:3000"

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900},
                                   device_scale_factor=1.5)
        page = ctx.new_page()
        for slug, path, title in PAGES:
            print(f"-> {title} ({path})")
            page.goto(BASE + path, wait_until="networkidle", timeout=60_000)
            # Opportunities page triggers a scan that can take ~10s
            if path == "/opportunities":
                # Wait for either the spinner to disappear or results to show
                try:
                    page.wait_for_function(
                        "() => !document.body.innerText.includes('Scanning market')",
                        timeout=30_000,
                    )
                except Exception:
                    pass
            elif path == "/backtest":
                # Let recharts render
                time.sleep(1.5)
            else:
                time.sleep(1.2)
            out = OUT / f"{slug}.png"
            page.screenshot(path=str(out), full_page=True)
            print(f"  saved {out}")
        browser.close()

if __name__ == "__main__":
    main()
