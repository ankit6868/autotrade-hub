"""
AutoTrade Hub — Screenshot capture script
Run: python docs/take_screenshots.py
Saves all screenshots to docs/screenshots/
"""
import os, time
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = "http://localhost:3000"
OUT  = Path("docs/screenshots")
OUT.mkdir(parents=True, exist_ok=True)

PAGES = [
    ("01_signin",        "/sign-in",           "Sign In Page"),
    ("02_dashboard",     "/",                  "Dashboard"),
    ("03_setup",         "/setup",             "Setup Wizard"),
    ("04_strategy_upload", "/strategy/upload", "Strategy Upload / AI Converter"),
    ("05_templates",     "/strategy/templates","Strategy Templates"),
    ("06_opportunities", "/opportunities",     "Opportunities Scanner"),
    ("07_backtest",      "/backtest",          "Backtesting"),
    ("08_paper_trade",   "/paper-trade",       "Paper Trading"),
    ("09_live_trade",    "/live",              "Live Trading"),
    ("10_trade_history", "/trade/history",     "Trade History"),
]

def take(page, path: str, name: str, wait_ms=3000):
    print(f"  Capturing: {name} ({path})")
    try:
        page.goto(BASE + path, wait_until="networkidle", timeout=20000)
        page.wait_for_timeout(wait_ms)
        # Full-page screenshot
        outfile = OUT / f"{name}.png"
        page.screenshot(path=str(outfile), full_page=True)
        size = outfile.stat().st_size // 1024
        print(f"    ✅ Saved {outfile.name} ({size} KB)")
        return str(outfile)
    except Exception as e:
        print(f"    ❌ Failed: {e}")
        return None

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=False, slow_mo=200)
    ctx = browser.new_context(
        viewport={"width": 1440, "height": 900},
        # Try to load existing Chrome profile for auth session
    )
    page = ctx.new_page()

    # Start from sign-in
    print("Starting screenshot capture...\n")
    saved = []

    for (slug, path, label) in PAGES:
        result = take(page, path, slug + "_" + label.replace(" ", "_").replace("/", ""), 4000)
        if result:
            saved.append((label, result))

    # Extra: scanner with results loaded
    print("\n  Waiting for scanner to load results...")
    page.goto(BASE + "/opportunities", wait_until="networkidle", timeout=20000)
    page.wait_for_timeout(8000)  # scanner takes ~8s
    fname = "11_Opportunities_Scanner_Results"
    page.screenshot(path=str(OUT / f"{fname}.png"), full_page=True)
    print(f"  ✅ Scanner results saved")

    # Extra: backtest preset buttons closeup
    page.goto(BASE + "/backtest", wait_until="networkidle", timeout=15000)
    page.wait_for_timeout(2000)
    fname = "12_Backtest_Presets"
    page.screenshot(path=str(OUT / f"{fname}.png"), full_page=False)
    print(f"  ✅ Backtest presets saved")

    browser.close()

print(f"\n✅ Done! {len(saved)+2} screenshots saved to docs/screenshots/")
print(f"📁 Open: {OUT.resolve()}")
