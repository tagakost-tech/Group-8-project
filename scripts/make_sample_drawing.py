"""Draw an invented part as a CAD-style PDF, for demos of drawing-to-quote.

    py -3.11 scripts/make_sample_drawing.py   -> assets/sample-drawing-stepped-pin.pdf

The dimensions are text on the page, as a CAD export keeps them, so the
drawing reader finds them the way it would on a buyer's own drawing.
"""

from pathlib import Path

import fitz  # PyMuPDF

OUT = Path(__file__).resolve().parent.parent / "assets" / "sample-drawing-stepped-pin.pdf"


def main() -> None:
    doc = fitz.open()
    page = doc.new_page(width=842, height=595)  # A4 landscape
    ink = (0.1, 0.15, 0.2)
    s = 7.0  # 1 mm = 7 pt
    x0, y0 = 160, 300
    # side view: head 12 x 4, collar 8 x 2, shank 6 to 40 mm overall
    head = fitz.Rect(x0, y0 - 6 * s, x0 + 4 * s, y0 + 6 * s)
    collar = fitz.Rect(head.x1, y0 - 4 * s, head.x1 + 2 * s, y0 + 4 * s)
    shank = fitz.Rect(collar.x1, y0 - 3 * s, x0 + 40 * s, y0 + 3 * s)
    for r in (head, collar, shank):
        page.draw_rect(r, color=ink, width=1.2)
    for i in range(int((40 - 18) / 1)):  # rolled thread, 22 mm
        x = shank.x1 - i * s
        page.draw_line((x, shank.y0), (x - s / 2, shank.y1), color=ink, width=0.4)
    page.draw_line((x0 - 20, y0), (shank.x1 + 20, y0), color=(0.5, 0.5, 0.5), width=0.4, dashes="[6 3] 0")

    def label(x, y, text, size=10):
        page.insert_text((x, y), text, fontsize=size, color=ink)

    label(x0 - 10, y0 - 6 * s - 12, "Ø12 head")
    label(collar.x0, y0 - 4 * s - 26, "Ø8 collar  2 mm")
    label(collar.x1 + 30, y0 - 3 * s - 10, "Ø6 shank")
    label(shank.x1 - 160, y0 + 3 * s + 18, "M6x1  b: 22")
    label(x0, y0 + 6 * s + 30, "L=40 ±0.1     H: 4")
    # title block
    tb = fitz.Rect(520, 440, 820, 575)
    page.draw_rect(tb, color=ink, width=1)
    rows = [
        "STEPPED PIN WITH COLLAR  (invented demo part)",
        "Material: C10C (1.0214)    Surface: zinc flake",
        "Tolerances unless stated: ±0.1    Thread 6g",
        "Annual volume: 400.000 pcs",
        "Drawing DEMO-001  rev A   scale 7:1",
    ]
    for i, row in enumerate(rows):
        label(tb.x0 + 8, tb.y0 + 20 + i * 22, row, size=9)
        if i:
            page.draw_line((tb.x0, tb.y0 + 6 + i * 22), (tb.x1, tb.y0 + 6 + i * 22), color=ink, width=0.4)
    OUT.parent.mkdir(exist_ok=True)
    doc.save(OUT)
    print(OUT)


if __name__ == "__main__":
    main()
