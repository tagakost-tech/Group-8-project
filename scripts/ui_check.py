"""Screenshot every screen of the app shell and assert what a person relies on.

    python scripts/ui_check.py --url http://127.0.0.1:5000 --out shots/
    (logins from UI_CHECK_USER / UI_CHECK_PASSWORD)

For each screen, at 1440 and 390 px wide, at 100% and 150% text, and with
the Readability switches off and on (higher contrast + easier reading), it
checks:
  - no console errors and no request to any other host;
  - no sideways scroll;
  - no button, link-button, field or switch shorter than 28 px;
  - none of the banned words for the handling state ("reviewed", "checked",
    "rivista", "controllata") and never "Paese", in any label: pills,
    headings, buttons, menu, table and list titles. Content the client wrote
    ("made and checked in the same plant") is not a label;
  - the text really is 1.5 times larger at 150%, and higher contrast and
    easier reading really change the page;
  - Omega's logo is shown in the sidebar on wide screens.
Then it opens the Readability panel, checks Escape closes it, and checks a
choice survives a reload.

Exit status 1 and a list of problems if any check fails.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

SCREENS = ["today", "review", "leads", "markets", "network", "setup"]
BANNED = re.compile(r"\b(reviewed|checked|rivista|controllata|paese)\b", re.IGNORECASE)
LABELS = (
    ".pill, h1, h2, h3, button, .btn, .nav a, .tile-label, th, dt, .badge, "
    ".row-title, label > span, legend, .segmented, .group-title, .section-label"
)
MODES = {"plain": "", "contrast+easy": "&contrast=more&easy=on"}


def main() -> int:
    ap = argparse.ArgumentParser(description="Screenshot and assert the app shell.")
    ap.add_argument("--url", required=True)
    ap.add_argument("--out", default="shots")
    ap.add_argument("--quick", action="store_true", help="plain mode at 100%% only")
    args = ap.parse_args()
    user = os.environ.get("UI_CHECK_USER", "")
    password = os.environ.get("UI_CHECK_PASSWORD", "")
    if not user or not password:
        print("Set UI_CHECK_USER and UI_CHECK_PASSWORD.")
        return 2
    base = args.url.rstrip("/")
    host = urlparse(base).netloc
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    shots = 0

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        errors: list[str] = []
        outside: list[str] = []
        page.on("console", lambda m: m.type == "error" and errors.append(m.text))
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("request", lambda r: urlparse(r.url).netloc not in (host, "") and outside.append(r.url))

        page.goto(f"{base}/login")
        page.fill("input[name=username]", user)
        page.fill("input[name=password]", password)
        page.click("button[type=submit]")
        page.wait_for_url(f"{base}/**")

        modes = {"plain": ""} if args.quick else MODES
        sizes = [100] if args.quick else [100, 150]
        baseline: dict[str, dict] = {}
        for mode, extra in modes.items():
            for width in (1440, 390):
                for size in sizes:
                    page.set_viewport_size({"width": width, "height": 900 if width > 400 else 844})
                    for screen in SCREENS:
                        where = f"{screen} {mode} {width}px {size}%"
                        errors.clear()
                        outside.clear()
                        try:
                            _open(page, f"{base}/#{screen}&text={size}{extra}", screen)
                        except PlaywrightTimeout:
                            problems.append(f"{where}: the screen did not open from its address")
                            continue
                        page.screenshot(path=str(out / f"{screen}-{mode}-{width}-{size}.png"), full_page=True)
                        shots += 1
                        problems += [f"{where}: console error: {e}" for e in errors]
                        problems += [f"{where}: request to another host: {u}" for u in outside]
                        sw = page.evaluate("document.documentElement.scrollWidth")
                        if sw > width:
                            problems.append(f"{where}: sideways scroll ({sw}px wide)")
                        small = page.evaluate(
                            """() => [...document.querySelectorAll(
                                 'button, a.btn, input:not([type=checkbox]):not([type=radio]):not([type=hidden]), select, textarea, [role=switch]')]
                               .filter(el => el.offsetParent !== null && !el.closest('[hidden]'))
                               .filter(el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height < 28; })
                               .map(el => (el.id || el.className || el.tagName) + ' ' + Math.round(el.getBoundingClientRect().height) + 'px')"""
                        )
                        problems += [f"{where}: control under 28px: {s}" for s in small]
                        text = page.evaluate(
                            "(sel) => [...document.querySelectorAll(sel)].filter(el => el.offsetParent !== null).map(el => el.innerText).join('\\n')",
                            LABELS,
                        )
                        for m in sorted({m.group(0) for m in BANNED.finditer(text)}):
                            problems.append(f"{where}: banned word on screen: {m!r}")
                        look = _look(page)
                        key = f"{screen} {width}"
                        if mode == "plain" and size == 100:
                            baseline[key] = look
                        elif key in baseline:
                            b = baseline[key]
                            if size == 150 and look["h1"] < b["h1"] * 1.45:
                                problems.append(f"{where}: headings did not grow at 150% ({b['h1']} -> {look['h1']}px)")
                            if size == 150 and look["root"] != 24:
                                problems.append(f"{where}: root text is {look['root']}px at 150%, not 24px")
                            if mode != "plain" and look["sub"] == b["sub"]:
                                problems.append(f"{where}: higher contrast did not change secondary text ({look['sub']})")
                            if mode != "plain" and look["spacing"] == b["spacing"]:
                                problems.append(f"{where}: easier reading did not change letter spacing")
                        if width == 1440:
                            problems += [f"{where}: {x}" for x in _logo(page)]
        problems += _panel(page, base)
        browser.close()

    print(f"{shots} screenshots in {out}")
    if problems:
        print(f"{len(problems)} problems:")
        for x in problems:
            print("  -", x)
        return 1
    print("No problems found.")
    return 0


def _open(page, url: str, screen: str) -> None:
    page.goto(url)
    page.reload()
    page.wait_for_selector(f"#screen-{screen}:not([hidden])", timeout=8000)
    page.wait_for_load_state("networkidle")


def _look(page) -> dict:
    return page.evaluate(
        """() => { const h1 = document.querySelector('.screen:not([hidden]) h1');
             const cs = getComputedStyle(document.body);
             return { root: parseFloat(getComputedStyle(document.documentElement).fontSize),
                      h1: h1 ? parseFloat(getComputedStyle(h1).fontSize) : 0,
                      sub: getComputedStyle(document.documentElement).getPropertyValue('--label-2').trim(),
                      spacing: cs.letterSpacing }; }"""
    )


def _logo(page) -> list[str]:
    info = page.evaluate(
        """() => { const el = document.querySelector('.sidebar .brand-logo'); if (!el) return null;
             const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
             return { loaded: el.complete && el.naturalWidth > 0, width: r.width, filter: cs.filter,
                      src: el.getAttribute('src') }; }"""
    )
    if info is None:
        return ["no logo in the sidebar"]
    out = []
    if not info["loaded"] or info["width"] < 24:
        out.append(f"the logo is not shown ({info['width']:.0f}px)")
    if info["filter"] not in ("none", ""):
        out.append(f"the logo has a filter: {info['filter']}")
    if "omega-logo" not in (info["src"] or ""):
        out.append(f"the logo is not Omega's file: {info['src']}")
    return out


def _panel(page, base: str) -> list[str]:
    out = []
    page.set_viewport_size({"width": 1440, "height": 900})
    _open(page, f"{base}/#today", "today")
    page.evaluate("localStorage.removeItem('omega-read')")
    _open(page, f"{base}/#today", "today")
    if not page.locator("#readBtn").count():
        return ["there is no Aa (Readability) button"]
    page.click("#readBtn")
    if page.is_hidden("#readPanel"):
        return ["the Aa button does not open the Readability panel"]
    page.click("#sizeUp")
    page.click("#sizeUp")
    page.click("#easySw")
    page.keyboard.press("Escape")
    if not page.is_hidden("#readPanel"):
        out.append("Escape does not close the Readability panel")
    page.reload()
    page.wait_for_selector("#screen-today:not([hidden])")
    root = page.evaluate("parseFloat(getComputedStyle(document.documentElement).fontSize)")
    easy = page.evaluate("document.documentElement.dataset.easy")
    if root != 19.2 or easy != "on":
        out.append(f"the choice did not survive a reload (root {root}px, easy {easy})")
    page.click("#readBtn")
    page.click("#readReset")
    root = page.evaluate("parseFloat(getComputedStyle(document.documentElement).fontSize)")
    if root != 16:
        out.append(f"Reset left the text at {root}px")
    return out


if __name__ == "__main__":
    sys.exit(main())
