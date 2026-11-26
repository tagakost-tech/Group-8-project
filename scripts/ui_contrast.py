"""Measure the app's colour pairs against WCAG 2.2 AA, reading static/app/app.css.

    python scripts/ui_contrast.py

Two token sets: the normal dark page (`:root`) and higher contrast
(`[data-contrast="more"]` on top of it). Semi-transparent colours are laid
over the surface they sit on before measuring. Text needs 4.5:1. In higher
contrast, card edges and field borders also need 3:1 (non-text contrast).
Exit status 1 if any pair fails.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

CSS = Path(__file__).resolve().parent.parent / "static" / "app" / "app.css"

TEXT = [
    ("--label", "--bg"),
    ("--label", "--bg-1"),
    ("--label", "--bg-2"),
    ("--label-2", "--bg"),
    ("--label-2", "--bg-1"),
    ("--label-2", "--bg-2"),
    ("--label-3", "--bg"),
    ("--label-3", "--bg-1"),
    ("--accent", "--bg"),
    ("--accent", "--bg-1"),
    ("#ffffff", "--accent-btn"),
    ("--green", "--bg-1"),
    ("--orange", "--bg-1"),
    ("--red", "--bg-1"),
    ("--accent", "--accent-fill@--bg-1"),
    ("--green", "--green-fill@--bg-1"),
    ("--orange", "--orange-fill@--bg-1"),
    ("--red", "--red-fill@--bg-1"),
]
EDGES = [("--hair", "--bg"), ("--sep", "--bg-1"), ("--sep", "--bg-2")]


def blocks(css: str) -> tuple[dict, dict]:
    def grab(selector: str) -> dict:
        m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
        if not m:
            raise SystemExit(f"{selector} not found in {CSS}")
        return dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", m.group(1)))

    return grab(":root"), grab('[data-contrast="more"]')


def rgba(value: str) -> tuple[float, float, float, float]:
    v = value.strip()
    if v.startswith("#"):
        h = v[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 1.0
    m = re.match(r"rgba?\(([^)]*)\)", v)
    if not m:
        raise ValueError(f"not a colour: {value}")
    parts = [float(x) for x in re.split(r"[ ,/]+", m.group(1).strip()) if x]
    return parts[0], parts[1], parts[2], parts[3] if len(parts) > 3 else 1.0


def over(top, bottom):
    a = top[3]
    return tuple(top[i] * a + bottom[i] * (1 - a) for i in range(3)) + (1.0,)


def resolve(token: str, tokens: dict, ground=None):
    if "@" in token:
        top, under = token.split("@")
        return over(resolve(top, tokens), resolve(under, tokens))
    colour = rgba(tokens[token]) if token.startswith("--") else rgba(token)
    if colour[3] < 1:
        colour = over(colour, ground or rgba(tokens["--bg"]))
    return colour


def lum(c) -> float:
    def ch(x):
        x /= 255
        return x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4

    return 0.2126 * ch(c[0]) + 0.7152 * ch(c[1]) + 0.0722 * ch(c[2])


def ratio(a, b) -> float:
    la, lb = lum(a), lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def main() -> int:
    base, more = blocks(CSS.read_text(encoding="utf-8"))
    sets = {"dark": base, "dark, higher contrast": {**base, **more}}
    failures = 0
    for name, tokens in sets.items():
        print(f"\n{name}")
        pairs = [(f, b, 4.5) for f, b in TEXT]
        if "higher" in name:
            pairs += [(f, b, 3.0) for f, b in EDGES]
        for fg, bg, need in pairs:
            ground = resolve(bg, tokens)
            r = ratio(resolve(fg, tokens, ground), ground)
            ok = r >= need
            failures += not ok
            print(f"  {'ok  ' if ok else 'FAIL'} {fg:>14} on {bg:<22} {r:5.2f}:1 (needs {need})")
        if "higher" in name:
            for key in ("--label-2", "--label-3"):
                ground = resolve("--bg-1", tokens)
                if ratio(resolve(key, tokens, ground), ground) <= ratio(resolve(key, base, ground), ground):
                    failures += 1
                    print(f"  FAIL higher contrast does not raise {key}")
    print(f"\n{failures} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
