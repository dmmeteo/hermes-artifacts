"""Regenerate tests/fixtures/chart.png and chart.webp by drawing on a Chromium canvas."""

import base64
from pathlib import Path

from playwright.sync_api import sync_playwright

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
DRAW = """(type) => {
  const c = document.createElement('canvas'); c.width = 640; c.height = 360;
  const g = c.getContext('2d');
  g.fillStyle = '#f4efe6'; g.fillRect(0, 0, 640, 360);
  g.fillStyle = '#123636'; g.font = 'bold 26px sans-serif'; g.fillText(type.toUpperCase() + ' weekly artifacts', 32, 52);
  const values = [12, 19, 9, 24, 31, 27, 35];
  values.forEach((v, i) => { g.fillStyle = i % 2 ? '#7fd6cf' : '#0b6b6b'; g.fillRect(40 + i * 82, 330 - v * 8, 56, v * 8); });
  return c.toDataURL('image/' + type, 0.9).split(',')[1];
}"""

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    for kind in ("png", "webp"):
        data = base64.b64decode(page.evaluate(DRAW, kind))
        (FIXTURES / f"chart.{kind}").write_bytes(data)
        print(kind, len(data))
    browser.close()
