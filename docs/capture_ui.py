"""Capture a screenshot of the current primary display and save to docs/ui_screenshots."""
import sys
import os
from pathlib import Path

OUT_DIR = Path(__file__).parent / "ui_screenshots"
OUT_DIR.mkdir(exist_ok=True)

name = sys.argv[1] if len(sys.argv) > 1 else "shot"

try:
    import mss
    from PIL import Image
    with mss.mss() as sct:
        mon = sct.monitors[1]  # primary
        img = sct.grab(mon)
        pil = Image.frombytes("RGB", img.size, img.rgb)
        out = OUT_DIR / f"{name}.png"
        pil.save(out, "PNG", optimize=True)
        print(f"Saved {out} ({pil.size[0]}x{pil.size[1]})")
except ImportError:
    # fallback
    import pyautogui
    img = pyautogui.screenshot()
    out = OUT_DIR / f"{name}.png"
    img.save(out)
    print(f"Saved {out} ({img.size[0]}x{img.size[1]}) via pyautogui")
