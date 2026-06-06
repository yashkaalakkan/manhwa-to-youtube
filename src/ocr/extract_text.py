"""
extract_text.py
Uses Tesseract OCR to extract speech bubble text from manga/manhwa pages.
Outputs a JSON file with per-page text and bounding boxes.
"""

import argparse
import json
import re
from pathlib import Path

import cv2
import numpy as np
import pytesseract
from PIL import Image

LANG_MAP = {
    "en": "eng",
    "ja": "jpn",
    "ko": "kor",
    "zh": "chi_sim",
    "fr": "fra",
    "de": "deu",
    "es": "spa",
    "pt": "por",
    "hi": "hin",
}


def preprocess_page(image_path: Path) -> np.ndarray:
    img = cv2.imread(str(image_path))
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    denoised = cv2.fastNlMeansDenoising(gray, h=10)
    thresh = cv2.adaptiveThreshold(
        denoised, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY, 11, 2
    )
    return thresh


def extract_text_from_page(image_path: Path, lang: str = "eng") -> dict:
    processed = preprocess_page(image_path)
    custom_config = r"--oem 3 --psm 1"
    data = pytesseract.image_to_data(
        processed, lang=lang,
        config=custom_config,
        output_type=pytesseract.Output.DICT,
    )
    plain_text = pytesseract.image_to_string(processed, lang=lang, config=custom_config)

    words = []
    for i in range(len(data["text"])):
        word = data["text"][i].strip()
        conf = int(data["conf"][i])
        if word and conf > 40:
            words.append({
                "word": word,
                "conf": conf,
                "x": data["left"][i],
                "y": data["top"][i],
                "w": data["width"][i],
                "h": data["height"][i],
                "block": data["block_num"][i],
                "line": data["line_num"][i],
            })

    lines = {}
    for w in words:
        key = (w["block"], w["line"])
        lines.setdefault(key, []).append(w)

    line_texts = [
        " ".join(w["word"] for w in sorted(group, key=lambda x: x["x"]))
        for group in sorted(lines.values(), key=lambda g: (g[0]["y"], g[0]["x"]))
    ]

    return {
        "path": str(image_path),
        "raw_text": plain_text.strip(),
        "lines": line_texts,
        "words": words,
    }


def get_page_number(path: Path) -> int:
    """Extract page number from filename like page_0001.jpg → 1."""
    m = re.search(r"page_0*(\d+)", path.name)
    return int(m.group(1)) if m else -1


def main():
    parser = argparse.ArgumentParser(description="OCR manga/manhwa pages")
    parser.add_argument("--pages-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--language", default="en")
    parser.add_argument("--skip-pages", default="1",
                        help="Comma-separated page numbers to skip (default: 1)")
    args = parser.parse_args()

    pages_dir   = Path(args.pages_dir)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    lang = LANG_MAP.get(args.language, "eng")

    # Parse skip list
    skip = {
        int(x.strip())
        for x in args.skip_pages.split(",")
        if x.strip().isdigit()
    }
    if skip:
        print(f"[OCR] Skipping page(s): {sorted(skip)}")

    # Load page paths from manifest or glob
    manifest_path = Path("pipeline/manifest.json")
    if manifest_path.exists():
        with open(manifest_path) as f:
            manifest = json.load(f)
        page_paths = [Path(p["path"]) for p in manifest["pages"]]
    else:
        exts = ["*.jpg", "*.jpeg", "*.png", "*.webp"]
        page_paths = sorted(
            [p for ext in exts for p in pages_dir.glob(ext)],
            key=lambda x: x.name,
        )

    # Filter out skipped pages
    page_paths = [p for p in page_paths if get_page_number(p) not in skip]

    print(f"[OCR] Processing {len(page_paths)} pages with lang={lang}...")

    results = []
    for i, page_path in enumerate(page_paths, start=1):
        print(f"[OCR] Page {i}/{len(page_paths)}: {page_path.name}")
        try:
            result = extract_text_from_page(page_path, lang=lang)
            result["page_index"] = i
            results.append(result)
        except Exception as e:
            print(f"[OCR] ⚠️  Error on page {i}: {e}")
            results.append({
                "page_index": i, "path": str(page_path),
                "raw_text": "", "lines": [], "words": [],
            })

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    total_words = sum(len(r["words"]) for r in results)
    print(f"[OCR] ✅ Extracted {total_words} words across {len(results)} pages → {output_path}")


if __name__ == "__main__":
    main()