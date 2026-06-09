"""
drive_downloader.py
Downloads a single episode PDF from a PUBLIC Google Drive share link,
then converts each PDF page into a numbered image file.

How to make your Drive file public:
  Right-click the PDF → Share → "Anyone with the link" → Viewer → Done

Accepted link formats:
  https://drive.google.com/file/d/FILE_ID/view?usp=sharing
  https://drive.google.com/open?id=FILE_ID
  Raw file ID: 1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

import requests

DRIVE_API = "https://www.googleapis.com/drive/v3"


def extract_file_id(link_or_id: str) -> str:
    """Extract file ID from any Drive link format or raw ID."""
    link_or_id = link_or_id.strip()
    # Raw ID — no slashes or dots
    if re.match(r'^[a-zA-Z0-9_-]{25,}$', link_or_id):
        return link_or_id
    # /file/d/FILE_ID/...
    m = re.search(r'/file/d/([a-zA-Z0-9_-]+)', link_or_id)
    if m:
        return m.group(1)
    # ?id=FILE_ID or &id=FILE_ID
    m = re.search(r'[?&]id=([a-zA-Z0-9_-]+)', link_or_id)
    if m:
        return m.group(1)
    raise ValueError(f"Could not extract file ID from: {link_or_id}")


def get_file_name(file_id: str, api_key: str) -> str:
    """Get the original filename from Drive metadata."""
    resp = requests.get(
        f"{DRIVE_API}/files/{file_id}",
        params={"fields": "name", "key": api_key},
        timeout=15,
    )
    if resp.status_code == 403:
        print("[Drive] ❌ 403 — make sure the file is set to 'Anyone with the link can view'")
        sys.exit(1)
    resp.raise_for_status()
    return resp.json().get("name", "episode.pdf")


def _download_image(file_id: str, dest_path: Path, api_key: str) -> None:
    """
    Download an image file from Google Drive and verify it opens correctly.
    Uses the same download mechanism as download_pdf but validates the result
    is a real image (not an HTML error/login page).
    """
    import io
    # Try direct download URL first
    url = f"https://drive.google.com/uc?export=download&id={file_id}"
    session = requests.Session()
    resp = session.get(url, stream=True, timeout=60)

    # Handle Google's confirmation page (large files)
    if "Content-Disposition" not in resp.headers:
        token = None
        for key, val in resp.cookies.items():
            if key.startswith("download_warning"):
                token = val
                break
        if token:
            resp = session.get(url, params={"confirm": token}, stream=True, timeout=120)

    resp.raise_for_status()
    raw = b"".join(resp.iter_content(chunk_size=1024 * 1024))

    # Verify it's a valid image before saving
    try:
        from PIL import Image as _Image
        img = _Image.open(io.BytesIO(raw)).convert("RGB")
        img.save(str(dest_path), "JPEG", quality=95)
        print(f"[Drive] Cover verified: {img.width}×{img.height}px")
    except Exception:
        # Fallback: try thumbnail URL which always returns an image
        thumb_url = f"https://drive.google.com/thumbnail?id={file_id}&sz=w1080"
        resp2 = requests.get(thumb_url, timeout=30)
        resp2.raise_for_status()
        img = _Image.open(io.BytesIO(resp2.content)).convert("RGB")
        img.save(str(dest_path), "JPEG", quality=95)
        print(f"[Drive] Cover via thumbnail: {img.width}×{img.height}px")


def download_pdf(file_id: str, dest_path: Path, api_key: str) -> None:
    """Download a public Drive PDF file."""
    url = f"https://drive.google.com/uc?export=download&id={file_id}"
    session = requests.Session()

    resp = session.get(url, params={"key": api_key}, stream=True, timeout=60)

    # Handle Google's large-file virus-scan confirmation page
    if "Content-Disposition" not in resp.headers:
        token = None
        for key, val in resp.cookies.items():
            if key.startswith("download_warning"):
                token = val
                break
        if token:
            resp = session.get(
                url, params={"confirm": token}, stream=True, timeout=120
            )

    resp.raise_for_status()

    total = int(resp.headers.get("Content-Length", 0))
    downloaded = 0
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = int(downloaded / total * 100)
                    print(f"\r[Drive] Downloading... {pct}%", end="", flush=True)
    print()


def pdf_to_images(pdf_path: Path, output_dir: Path, dpi: int = 150) -> list[Path]:
    """
    Convert each PDF page to a JPEG image.
    Uses pdf2image (poppler) — installed via apt in the workflow.
    Falls back to PyMuPDF (fitz) if available.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        from pdf2image import convert_from_path
        print(f"[PDF] Converting with pdf2image (poppler) at {dpi} DPI...")
        pages = convert_from_path(str(pdf_path), dpi=dpi, fmt="jpeg", thread_count=2)
        paths = []
        for i, page in enumerate(pages, start=1):
            dest = output_dir / f"page_{i:04d}.jpg"
            page.save(str(dest), "JPEG", quality=92)
            paths.append(dest)
            print(f"[PDF] Page {i}/{len(pages)} → {dest.name}")
        return paths

    except ImportError:
        pass

    try:
        import fitz  # PyMuPDF
        print(f"[PDF] Converting with PyMuPDF at {dpi} DPI...")
        doc = fitz.open(str(pdf_path))
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        paths = []
        for i, page in enumerate(doc, start=1):
            dest = output_dir / f"page_{i:04d}.jpg"
            pix = page.get_pixmap(matrix=mat)
            pix.save(str(dest))
            paths.append(dest)
            print(f"[PDF] Page {i}/{len(doc)} → {dest.name}")
        return paths

    except ImportError:
        print("[PDF] ❌ Neither pdf2image nor PyMuPDF found.")
        print("       Run: pip install pdf2image  and  apt install poppler-utils")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Download a PDF episode from public Google Drive and convert to images"
    )
    parser.add_argument("--file-link", required=True,
                        help="Public Drive share link or raw file ID of the PDF")
    parser.add_argument("--output-dir", required=True,
                        help="Directory to save extracted page images")
    parser.add_argument("--cover-link", default="",
                        help="Optional: public Drive link for a separate cover image")
    parser.add_argument("--dpi", type=int, default=150,
                        help="DPI for PDF→image conversion (default 150; higher = better quality but slower)")
    parser.add_argument("--skip-pages", default="auto",
                        help="'auto' skips first + last 2 pages, or comma-separated numbers")
    parser.add_argument("--manifest-out", default="",
                        help="Path to write manifest.json (default: pipeline/manifest.json)")
    args = parser.parse_args()

    api_key = os.environ.get("GDRIVE_API_KEY")
    if not api_key:
        print("[Drive] ❌ GDRIVE_API_KEY environment variable not set")
        sys.exit(1)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Manifest path: explicit --manifest-out or fallback to pipeline/manifest.json
    manifest_path = Path(args.manifest_out) if args.manifest_out else Path("pipeline/manifest.json")
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    # ── Download PDF ────────────────────────────────────────────────────────
    try:
        file_id = extract_file_id(args.file_link)
    except ValueError as e:
        print(f"[Drive] ❌ {e}")
        sys.exit(1)

    print(f"[Drive] File ID: {file_id}")
    original_name = get_file_name(file_id, api_key)
    print(f"[Drive] File name: {original_name}")

    if not original_name.lower().endswith(".pdf"):
        print(f"[Drive] ⚠️  File doesn't look like a PDF ({original_name}). Proceeding anyway...")

    pdf_path = Path("/tmp/episode.pdf")
    print(f"[Drive] Downloading PDF...")
    download_pdf(file_id, pdf_path, api_key)
    print(f"[Drive] PDF saved ({pdf_path.stat().st_size // 1024} KB)")

    # ── Convert PDF pages to images ─────────────────────────────────────────
    print(f"[PDF] Converting pages to images...")
    page_paths = pdf_to_images(pdf_path, output_dir, dpi=args.dpi)
    print(f"[PDF] ✅ {len(page_paths)} pages extracted")

    # ── Optional cover image ────────────────────────────────────────────────
    cover_path = None
    if args.cover_link.strip():
        try:
            cover_id   = extract_file_id(args.cover_link)
            cover_dest = output_dir / "cover.jpg"
            print(f"[Drive] Downloading cover image...")
            _download_image(cover_id, cover_dest, api_key)
            cover_path = str(cover_dest)
            print(f"[Drive] Cover saved → {cover_dest}")
        except Exception as e:
            print(f"[Drive] ⚠️  Could not download cover: {e} — continuing without it")

    # ── Parse skip list ──────────────────────────────────────────────────────
    skip_input  = args.skip_pages.strip()
    total_pages = len(page_paths)

    if skip_input.lower() == "auto":
        if total_pages >= 3:
            skip_pages = {1, total_pages - 1, total_pages}
        elif total_pages >= 1:
            skip_pages = {1}
        else:
            skip_pages = set()
        print(f"[Drive] Auto-skip: pages {sorted(skip_pages)} (of {total_pages} total)")
    else:
        skip_pages = {
            int(x.strip())
            for x in skip_input.split(",")
            if x.strip().isdigit()
        }
        if skip_pages:
            print(f"[Drive] Will skip page(s): {sorted(skip_pages)}")

    # ── Save manifest ────────────────────────────────────────────────────────
    manifest = {
        "pages": [
            {"index": i + 1, "path": str(p), "original_name": p.name}
            for i, p in enumerate(page_paths)
        ],
        "cover": cover_path,
        "total_pages": len(page_paths),
        "source_file": original_name,
        "file_id": file_id,
        "skip_pages": sorted(skip_pages),
    }
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"[Drive] ✅ Manifest saved → {manifest_path}")

    # ── Split pages into panels ──────────────────────────────────────────────
    print(f"[Drive] Splitting pages into panels...")
    sys.path.insert(0, str(Path(__file__).parent))
    from panel_splitter import split_all_pages
    panels_dir = output_dir / "panels"
    all_panels, panel_map = split_all_pages(page_paths, panels_dir, skip_pages)

    # Update manifest with panel info
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
    print(f"[Drive] ✅ {len(all_panels)} panels extracted → {panels_dir}")


if __name__ == "__main__":
    main()