"""
build_shorts.py
Builds exactly 4 short videos per episode (~60s each).
Pages are split evenly into 4 parts regardless of count.
All animation/subtitle rendering is done natively in FFmpeg — no Python frame loops.
"""

import argparse
import json
import random
import tempfile
from pathlib import Path
from typing import List, Optional

from video_utils import (
    TRANSITIONS as ANIMATIONS,
    build_video_ffmpeg,
    generate_ass_subtitles,
    prepare_cover_image,
    prepare_page_image,
)

NUM_SHORTS       = 4
COVER_DURATION_S = 3.0
MIN_PAGE_DUR_S   = 2.5


def split_pages_evenly(all_pages: dict, n_parts: int) -> List[List[int]]:
    """Split page indices into n_parts equal chunks."""
    indices = sorted(all_pages.keys())
    size    = max(len(indices) // n_parts, 1)
    chunks  = []
    for i in range(n_parts):
        start = i * size
        # Last chunk gets any remainder
        end = start + size if i < n_parts - 1 else len(indices)
        chunk = indices[start:end]
        if chunk:
            chunks.append(chunk)
    return chunks


def build_short(
    pages: List[Path],
    audio_path: Path,
    timing_words: List[dict],
    output_path: Path,
    manhwa: str,
    episode: int,
    part: int,
    cover_image: Optional[Path],
    audio_duration: float,
) -> None:
    content_duration = max(audio_duration, len(pages) * MIN_PAGE_DUR_S)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        # Prepare cover
        cover_dst = tmp / "cover.jpg"
        prepare_cover_image(manhwa, episode, part, cover_image, cover_dst)

        # Prepare pages (one resize per page — no frame loop)
        page_dsts = []
        for i, pg in enumerate(pages):
            dst = tmp / f"page_{i:03d}.jpg"
            prepare_page_image(pg, dst)
            page_dsts.append(dst)
        print(f"  [Short {part}] {len(page_dsts)} pages prepped")

        # Offset word timestamps by cover duration
        offset_words = [
            {**w, "start": w["start"] + COVER_DURATION_S,
                  "end":   w["end"]   + COVER_DURATION_S}
            for w in timing_words
        ]

        # Generate ASS subtitles
        ass_path = tmp / "subs.ass"
        generate_ass_subtitles(offset_words, COVER_DURATION_S + content_duration, ass_path)
        print(f"  [Short {part}] Subtitles: {len(offset_words)} words")

        # Pick animations (one per page, shuffled)
        pool = ANIMATIONS.copy()
        random.shuffle(pool)
        animations = [pool[i % len(pool)] for i in range(len(page_dsts))]

        # Single FFmpeg call — everything in one pass
        print(f"  [Short {part}] FFmpeg encoding ({COVER_DURATION_S}s cover + {content_duration:.1f}s content)...")
        build_video_ffmpeg(
            page_images=page_dsts,
            cover_image=cover_dst,
            audio_path=audio_path,
            ass_path=ass_path,
            output_path=output_path,
            animations=animations,
            cover_duration_s=COVER_DURATION_S,
            content_duration_s=content_duration,
        )

    print(f"  [Short {part}] ✅ → {output_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages-dir",       required=True)
    parser.add_argument("--audio-dir",       required=True)
    parser.add_argument("--scripts",         required=True)
    parser.add_argument("--output-dir",      required=True)
    parser.add_argument("--manhwa",          required=True)
    parser.add_argument("--episode",         required=True, type=int)
    parser.add_argument("--pages-per-short", default="auto",
                        help="Ignored — always 4 parts per episode")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    with open(args.scripts, encoding="utf-8") as f:
        scripts = json.load(f)

    timing_path = Path(args.scripts).parent / "audio_timing.json"
    with open(timing_path, encoding="utf-8") as f:
        timing_data = json.load(f)

    # Load pages/panels from manifest
    manifest_path = Path("pipeline/manifest.json")
    if manifest_path.exists():
        with open(manifest_path) as f:
            manifest = json.load(f)

        # Prefer panels if available (panel splitting was run)
        if manifest.get("use_panels") and manifest.get("panels"):
            all_pages   = {p["index"]: Path(p["path"]) for p in manifest["panels"]}
            cover_image = Path(manifest["cover"]) if manifest.get("cover") else None
            print(f"[Shorts] Using {len(all_pages)} panels (panel-split mode)")
        else:
            # Fall back to raw pages, but exclude skipped pages
            skip = set(manifest.get("skip_pages", []))
            all_pages = {
                p["index"]: Path(p["path"])
                for p in manifest["pages"]
                if p["index"] not in skip
            }
            cover_image = Path(manifest["cover"]) if manifest.get("cover") else None
            print(f"[Shorts] Using {len(all_pages)} pages (skip: {sorted(skip)})")
    else:
        page_files = sorted(
            list(Path(args.pages_dir).glob("page_*.jpg")) +
            list(Path(args.pages_dir).glob("page_*.png"))
        )
        all_pages   = {i + 1: p for i, p in enumerate(page_files)}
        cover_image = None

    # Always split into exactly 4 parts
    page_chunks = split_pages_evenly(all_pages, NUM_SHORTS)
    actual_parts = len(page_chunks)
    print(f"[Shorts] {len(all_pages)} pages → {actual_parts} parts")

    for part, page_indices in enumerate(page_chunks, start=1):
        pages = [all_pages[i] for i in page_indices]

        # Match to script for this part
        short_script = next((s for s in scripts["shorts"] if s["part"] == part), None)
        timing       = next((t for t in timing_data["shorts"] if t["part"] == part), None)

        audio_path = Path(args.audio_dir) / f"short_part_{part:02d}.wav"
        if not audio_path.exists():
            print(f"[Shorts] ⚠️  No audio for part {part} — skipping")
            continue
        if not timing:
            print(f"[Shorts] ⚠️  No timing for part {part} — skipping")
            continue

        output_path = output_dir / f"short_ep{args.episode:02d}_part{part:02d}.mp4"
        print(f"\n[Shorts] Part {part}/{actual_parts}: pages {page_indices[0]}–{page_indices[-1]}, {timing['duration']:.1f}s audio")

        build_short(
            pages=pages,
            audio_path=audio_path,
            timing_words=timing["words"],
            output_path=output_path,
            manhwa=args.manhwa,
            episode=args.episode,
            part=part,
            cover_image=cover_image,
            audio_duration=timing["duration"],
        )

    print(f"\n[Shorts] ✅ All {actual_parts} shorts built → {output_dir}")


if __name__ == "__main__":
    main()