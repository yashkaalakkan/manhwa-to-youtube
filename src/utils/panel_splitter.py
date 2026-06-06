"""
panel_splitter.py
Splits tall manhwa/manga pages into individual panels by detecting
horizontal white-space gaps between panels.

Each input page image → N panel images saved as:
  <output_dir>/page_XXXX_panel_YY.jpg

Usage (standalone):
  python src/utils/panel_splitter.py --pages-dir ./episode_pages --output-dir ./episode_panels

Called automatically by drive_downloader.py after PDF conversion.
"""

import argparse
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

# ── Tuning constants ────────────────────────────────────────────────────────
# A row is "white" if ≥ this fraction of pixels are near-white
WHITE_ROW_THRESHOLD   = 0.92
# Pixel brightness ≥ this is considered white (0-255)
WHITE_PIXEL_VALUE     = 230
# A gap must be at least this many rows tall to count as a separator
MIN_GAP_HEIGHT_PX     = 18
# Minimum panel height in pixels (smaller crops are discarded as noise)
MIN_PANEL_HEIGHT_PX   = 120
# Pad each panel by this many pixels (avoids slicing into speech bubbles)
PANEL_PADDING_PX      = 4


def _white_rows(gray: np.ndarray) -> np.ndarray:
    """Return a boolean array: True where a row is predominantly white."""
    row_means = (gray >= WHITE_PIXEL_VALUE).mean(axis=1)
    return row_means >= WHITE_ROW_THRESHOLD


def _find_gap_ranges(is_white: np.ndarray, min_gap: int) -> list[tuple[int, int]]:
    """Return list of (start_row, end_row) for white gaps >= min_gap tall."""
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


def split_page_into_panels(
    image_path: Path,
    output_dir: Path,
    page_stem: str,
) -> list[Path]:
    """
    Split one page image into panels.
    Returns list of saved panel paths (in top-to-bottom order).
    If no gaps are found, the original page is returned as-is (1 panel).
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    img = Image.open(image_path).convert("RGB")
    arr = np.array(img)
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)

    is_white = _white_rows(gray)
    gaps = _find_gap_ranges(is_white, MIN_GAP_HEIGHT_PX)

    # Build panel vertical slices from the gaps
    height = arr.shape[0]
    split_points = [0]
    for gap_start, gap_end in gaps:
        mid = (gap_start + gap_end) // 2
        split_points.append(mid)
    split_points.append(height)

    panels = []
    for i in range(len(split_points) - 1):
        y1 = max(0, split_points[i] - PANEL_PADDING_PX)
        y2 = min(height, split_points[i + 1] + PANEL_PADDING_PX)
        panel_h = y2 - y1
        if panel_h < MIN_PANEL_HEIGHT_PX:
            continue  # skip tiny slivers (headers, watermarks)
        panel_img = img.crop((0, y1, img.width, y2))
        dest = output_dir / f"{page_stem}_panel_{i:02d}.jpg"
        panel_img.save(str(dest), "JPEG", quality=92)
        panels.append(dest)

    if not panels:
        # Fallback: no valid panels found, save whole page as single panel
        dest = output_dir / f"{page_stem}_panel_00.jpg"
        img.save(str(dest), "JPEG", quality=92)
        panels = [dest]

    return panels


def split_all_pages(
    page_paths: list[Path],
    output_dir: Path,
    skip_pages: set[int] | None = None,
) -> tuple[list[Path], dict]:
    """
    Split all pages into panels.
    Returns:
      - flat list of panel paths (skipped pages excluded)
      - panel_map: {original_page_index: [panel_path, ...]}
    """
    skip_pages = skip_pages or set()
    panel_map: dict[int, list[Path]] = {}
    all_panels: list[Path] = []

    import re
    def page_num(p: Path) -> int:
        m = re.search(r"page_0*(\d+)", p.name)
        return int(m.group(1)) if m else -1

    for page_path in page_paths:
        pg_num = page_num(page_path)
        if pg_num in skip_pages:
            print(f"[Panels] Skipping page {pg_num} ({page_path.name})")
            continue

        panels = split_page_into_panels(
            image_path=page_path,
            output_dir=output_dir,
            page_stem=page_path.stem,
        )
        panel_map[pg_num] = [str(p) for p in panels]
        all_panels.extend(panels)
        print(f"[Panels] Page {pg_num} → {len(panels)} panel(s)")

    return all_panels, panel_map


def main():
    parser = argparse.ArgumentParser(description="Split manhwa pages into individual panels")
    parser.add_argument("--pages-dir",  required=True, help="Directory with page_XXXX.jpg files")
    parser.add_argument("--output-dir", required=True, help="Where to save panel images")
    parser.add_argument("--skip-pages", default="",    help="Comma-separated page numbers to skip")
    parser.add_argument("--manifest",   default="pipeline/manifest.json",
                        help="Path to manifest.json (updated in-place with panel info)")
    args = parser.parse_args()

    pages_dir  = Path(args.pages_dir)
    output_dir = Path(args.output_dir)
    skip_pages = {
        int(x.strip())
        for x in args.skip_pages.split(",")
        if x.strip().isdigit()
    }

    # Load page list from manifest or glob
    manifest_path = Path(args.manifest)
    if manifest_path.exists():
        with open(manifest_path) as f:
            manifest = json.load(f)
        page_paths = [Path(p["path"]) for p in manifest["pages"]]
    else:
        exts = ["*.jpg", "*.jpeg", "*.png"]
        page_paths = sorted(
            [p for ext in exts for p in pages_dir.glob(ext)],
            key=lambda x: x.name,
        )

    all_panels, panel_map = split_all_pages(page_paths, output_dir, skip_pages)

    # Update manifest with panel info
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
        print(f"[Panels] ✅ Manifest updated with {len(all_panels)} panels → {manifest_path}")
    else:
        # Save a standalone panel manifest
        panel_manifest = {
            "panels": [
                {"index": i + 1, "path": str(p), "original_name": p.name}
                for i, p in enumerate(all_panels)
            ],
            "panel_map": {str(k): v for k, v in panel_map.items()},
            "total_panels": len(all_panels),
        }
        out = output_dir / "panel_manifest.json"
        with open(out, "w") as f:
            json.dump(panel_manifest, f, indent=2)
        print(f"[Panels] ✅ Panel manifest saved → {out}")

    print(f"[Panels] Total: {len(all_panels)} panels from {len(page_paths)} pages")


if __name__ == "__main__":
    main()
