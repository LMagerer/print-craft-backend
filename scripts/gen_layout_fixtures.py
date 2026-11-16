"""Generate text-layout parity fixtures for the frontend (``frontend/src/lib/__fixtures__/layout.json``).

The browser preview re-implements ``app.services.text_layout`` in TypeScript. This script records the
Python results for a set of tricky cases; ``textLayout.test.ts`` asserts the TS port produces the same
line breaks, positions and auto-shrink sizes. Re-run after changing either implementation:

    cd backend && .venv/bin/python scripts/gen_layout_fixtures.py
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.font_manager import load_metrics  # noqa: E402
from app.services.text_layout import LayoutParams, layout_text  # noqa: E402

FONTS = {"noto": ROOT / "fonts" / "NotoSans-Regular.ttf", "montserrat": ROOT / "fonts" / "Montserrat-Regular.ttf"}
OUT = ROOT.parent / "frontend" / "src" / "lib" / "__fixtures__" / "layout.json"

BASE = dict(box_x=10.0, box_y=20.0, box_w=300.0, box_h=60.0, size_px=40.0, min_size_px=12.0, step_px=2.0833)

CASES: list[dict] = [
    {"name": "single line left", "text": "Hello World", "font": "noto"},
    {"name": "centre/middle", "text": "Anna Müller", "font": "noto", "params": {"align": "center", "valign": "middle"}},
    {"name": "right/bottom", "text": "Åström", "font": "noto", "params": {"align": "right", "valign": "bottom"}},
    {"name": "overflow no shrink", "text": "Maximilian-Alexander von Hohenzollern", "font": "noto"},
    {"name": "auto shrink single line", "text": "Maximilian-Alexander von Hohenzollern", "font": "noto", "params": {"auto_shrink": True}},
    {"name": "shrink hits minimum", "text": "Supercalifragilisticexpialidocious and more words", "font": "noto", "params": {"auto_shrink": True, "min_size_px": 30.0}},
    {"name": "wrap", "text": "Head of Research and Development Department", "font": "noto", "params": {"wrap": True, "box_h": 200.0, "size_px": 30.0}},
    {"name": "wrap + shrink", "text": "Head of Research and Development Department Europe", "font": "noto", "params": {"wrap": True, "auto_shrink": True, "box_h": 90.0}},
    {"name": "wrap long word breaks", "text": "Donaudampfschifffahrtsgesellschaftskapitän", "font": "noto", "params": {"wrap": True, "box_h": 300.0, "box_w": 200.0}},
    {"name": "explicit newlines", "text": "Line one\nLine two\n\nLine four", "font": "noto", "params": {"box_h": 300.0, "size_px": 24.0, "align": "center"}},
    {"name": "letter spacing", "text": "SPACED OUT", "font": "noto", "params": {"letter_spacing_px": 4.0}},
    {"name": "line height", "text": "a b c d e f g h i j k l m n o p", "font": "noto", "params": {"wrap": True, "line_height": 1.6, "box_h": 300.0, "box_w": 120.0}},
    {"name": "multiple spaces", "text": "A   B  C", "font": "noto"},
    {"name": "fallback font", "text": "Ωμέγα Жук Ελλάδα", "font": "montserrat", "fallback": "noto", "params": {"auto_shrink": True}},
    {"name": "missing glyphs", "text": "Name 名前 ✓", "font": "noto"},
    {"name": "empty", "text": "", "font": "noto"},
    {"name": "fractional sizes", "text": "Precision 1/3", "font": "noto", "params": {"size_px": 33.3333, "box_w": 157.25, "box_x": 3.7, "box_y": 1.1, "align": "center", "valign": "middle"}},
]


def subset(metrics, chars: set[int]) -> dict:
    data = metrics.to_json()
    data["advances"] = {str(cp): adv for cp, adv in metrics.advances.items() if cp in chars}
    return data


def main() -> None:
    metrics = {k: load_metrics(str(p)) for k, p in FONTS.items()}
    used = {ord(c) for case in CASES for c in case["text"]} | {ord(" "), ord("-")}
    cases = []
    for case in CASES:
        params = LayoutParams(**{**BASE, **case.get("params", {})})
        fb = case.get("fallback")
        result = layout_text(case["text"], params, metrics[case["font"]], metrics[fb] if fb else None)
        cases.append({**case, "params": asdict(params), "expected": asdict(result)})
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"fonts": {k: subset(m, used) for k, m in metrics.items()}, "cases": cases}, ensure_ascii=False, indent=1), "utf-8")
    print(f"Wrote {len(cases)} cases to {OUT}")


if __name__ == "__main__":
    main()
