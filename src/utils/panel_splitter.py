"""
panel_splitter.py
Splits tall manhwa pages into individual art panels by detecting wide
white-space gaps between panels.

Rules:
- Gap must be >= 100px tall (filters out in-panel speech-bubble whitespace)
- Panel must be >= 300px tall (filters out thin dividers / trailing blanks)
- Panel must have colorvar >= 15 AND edge_density >= 3
  (filters out blank white panels and pure-text notification boxes)

Each input page → N panel images saved as:
  <output_dir>/page_XXXX_panel_YY.jpg
"""

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

# ── Tuning constants ─────────────────────────────────────────────────────────
WHITE_ROW_THRESHOLD  = 0.92   # fraction of row pixels that must be near-white
WHITE_PIXEL_VALUE    = 230    # brightness threshold (0-255)
MIN_GAP_HEIGHT_PX    = 100   # gaps smaller than this are ignored (was 18 — too low)
MIN_PANEL_HEIGHT_PX  = 300   # panels shorter than this are discarded
PANEL_PADDING_PX     = 6     # extra rows kept above/below each panel
MIN_COLOR_VAR        = 15.0  # std-dev of RGB — below this = blank/white panel
MIN_EDGE_DENSITY     = 3.0   # Canny edge mean — below this = no art content


def _white_rows(gray: np.ndarray) -> np.ndarray:
    row_means = (gray >= WHITE_PIXEL_VALUE).mean(axis=1)
    return row_means >= WHITE_ROW_THRESHOLD


def _find_gap_ranges(is_white: np.ndarray, min_gap: int) -> list[tuple[int, int]]:
    gaps = []
    in_gap = False
    gap_start = 0
    for i, white in enumerate(is_white):
        if white and not in_gap:
            in_gap = True
            gap_start = i
        elif not white and in_gap:
            in_gap = False
            if i - gap_start >= min_gap:
                gaps.append((gap_start, i))
    if in_gap and len(is_white) - gap_start >= min_gap:
        gaps.append((gap_start, len(is_white)))
    return gaps


def _is_real_panel(arr: np.ndarray) -> bool:
    """Return True only if the crop contains actual drawn art."""
    color_var = arr.std()
    if color_var < MIN_COLOR_VAR:
        return False
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, 50, 150)
    edge_density = edges.mean()
    if edge_density < MIN_EDGE_DENSITY:
        return False
    return True


def split_page_into_panels(
    image_path: Path,
    output_dir: Path,
    page_stem: str,
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)

    img = Image.open(image_path).convert("RGB")
    arr = np.array(img)
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)

    is_white = _white_rows(gray)
    gaps = _find_gap_ranges(is_white, MIN_GAP_HEIGHT_PX)

    height = arr.shape[0]
    split_points = [0]
    for gap_start, gap_end in gaps:
        split_points.append((gap_start + gap_end) // 2)
    split_points.append(height)

    panels = []
    panel_idx = 0
    for i in range(len(split_points) - 1):
        y1 = max(0, split_points[i] - PANEL_PADDING_PX)
        y2 = min(height, split_points[i + 1] + PANEL_PADDING_PX)
        if y2 - y1 < MIN_PANEL_HEIGHT_PX:
            continue
        crop_arr = arr[y1:y2]
        if not _is_real_panel(crop_arr):
            print(f"  [skip] {page_stem} slice y={y1}-{y2} — blank/text-only")
            continue
        panel_img = Image.fromarray(crop_arr)
        dest = output_dir / f"{page_stem}_panel_{panel_idx:02d}.jpg"
        panel_img.save(str(dest), "JPEG", quality=92)
        panels.append(dest)
        panel_idx += 1

    if not panels:
        # Fallback: whole page is one panel (e.g. single-panel chapter covers)
        dest = output_dir / f"{page_stem}_panel_00.jpg"
        img.save(str(dest), "JPEG", quality=92)
        panels = [dest]

    return panels


def split_all_pages(
    page_paths: list[Path],
    output_dir: Path,
    skip_pages: set[int] | None = None,
) -> tuple[list[Path], dict]:
    import re
    skip_pages = skip_pages or set()
    panel_map: dict[int, list[str]] = {}
    all_panels: list[Path] = []

    def page_num(p: Path) -> int:
        m = re.search(r"page_0*(\d+)", p.name)
        return int(m.group(1)) if m else -1

    for page_path in sorted(page_paths, key=lambda x: x.name):
        pg_num = page_num(page_path)
        if pg_num in skip_pages:
            print(f"[Panels] Skipping page {pg_num} ({page_path.name})")
            continue
        panels = split_page_into_panels(page_path, output_dir, page_path.stem)
        panel_map[pg_num] = [str(p) for p in panels]
        all_panels.extend(panels)
        print(f"[Panels] Page {pg_num} → {len(panels)} panel(s)")

    return all_panels, panel_map


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages-dir",  required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--skip-pages", default="")
    parser.add_argument("--manifest",   default="pipeline/manifest.json")
    args = parser.parse_args()

    pages_dir  = Path(args.pages_dir)
    output_dir = Path(args.output_dir)
    skip_pages = {int(x.strip()) for x in args.skip_pages.split(",") if x.strip().isdigit()}

    manifest_path = Path(args.manifest)
    if manifest_path.exists():
        with open(manifest_path) as f:
            manifest = json.load(f)
        page_paths = [Path(p["path"]) for p in manifest["pages"]]
    else:
        page_paths = sorted(pages_dir.glob("page_*.jpg"))

    all_panels, panel_map = split_all_pages(page_paths, output_dir, skip_pages)

    if manifest_path.exists():
        with open(manifest_path) as f:
            manifest = json.load(f)
        manifest["panels"] = [
            {"index": i + 1, "path": str(p), "original_name": p.name}
            for i, p in enumerate(all_panels)
        ]
        manifest["panel_map"] = {str(k): v for k, v in panel_map.items()}
        manifest["use_panels"] = True
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)

    print(f"\n[Panels] ✅ {len(all_panels)} real panels from {len(page_paths)} pages")


if __name__ == "__main__":
    main()