"""
build_shorts.py
Builds ONE short video per chapter (portrait 1080x1920).
All panels of the chapter scroll across the short's duration.
Called once per chapter with --chapter N.
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

COVER_DURATION_S = 3.0
MIN_PAGE_DUR_S   = 1.5
MAX_PAGE_DUR_S   = 4.0


def build_short(
    pages: List[Path],
    audio_path: Path,
    timing_words: List[dict],
    output_path: Path,
    manhwa: str,
    episode: int,
    chapter: int,
    cover_image: Optional[Path],
    audio_duration: float,
) -> None:
    per_panel = audio_duration / max(len(pages), 1)
    per_panel = max(MIN_PAGE_DUR_S, min(per_panel, MAX_PAGE_DUR_S))

    # Trim panels if audio is too short to show all
    max_panels = max(1, int(audio_duration / MIN_PAGE_DUR_S))
    if len(pages) > max_panels:
        print(f"  [Short] {len(pages)} panels → trimmed to {max_panels} to fit {audio_duration:.1f}s audio")
        pages = pages[:max_panels]

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        cover_dst = tmp / "cover.jpg"
        prepare_cover_image(manhwa, episode, chapter, cover_image, cover_dst)

        page_dsts = []
        for i, pg in enumerate(pages):
            dst = tmp / f"page_{i:03d}.jpg"
            prepare_page_image(pg, dst)
            page_dsts.append(dst)

        print(f"  [Short] Ch{chapter}: {len(page_dsts)} panels | {audio_duration:.1f}s audio | {per_panel:.1f}s/panel")

        offset_words = [
            {**w, "start": w["start"] + COVER_DURATION_S,
                  "end":   w["end"]   + COVER_DURATION_S}
            for w in timing_words
        ]

        ass_path = tmp / "subs.ass"
        generate_ass_subtitles(offset_words, COVER_DURATION_S + audio_duration, ass_path)

        pool       = ANIMATIONS.copy()
        random.shuffle(pool)
        animations = [pool[i % len(pool)] for i in range(len(page_dsts))]

        build_video_ffmpeg(
            page_images=page_dsts,
            cover_image=cover_dst,
            audio_path=audio_path,
            ass_path=ass_path,
            output_path=output_path,
            animations=animations,
            cover_duration_s=COVER_DURATION_S,
            content_duration_s=audio_duration,
        )

    print(f"  [Short] ✅ → {output_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages-dir",  required=True)
    parser.add_argument("--audio-dir",  required=True)
    parser.add_argument("--scripts",    required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--manhwa",     required=True)
    parser.add_argument("--episode",    required=True, type=int)
    parser.add_argument("--chapter",    required=True, type=int)
    parser.add_argument("--manifest",   default="")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    with open(args.scripts, encoding="utf-8") as f:
        scripts = json.load(f)

    timing_path = Path(args.scripts).parent / "audio_timing.json"
    with open(timing_path, encoding="utf-8") as f:
        timing_data = json.load(f)

    # Load panels from this chapter's manifest
    manifest_path = Path(args.manifest) if args.manifest else \
                    Path(args.scripts).parent / "manifest.json"

    if manifest_path.exists():
        with open(manifest_path) as f:
            manifest = json.load(f)
        if manifest.get("use_panels") and manifest.get("panels"):
            all_pages   = {p["index"]: Path(p["path"]) for p in manifest["panels"]}
            cover_image = Path(manifest["cover"]) if manifest.get("cover") else None
            print(f"[Short] {len(all_pages)} panels loaded (panel-split mode)")
        else:
            skip = set(manifest.get("skip_pages", []))
            all_pages = {
                p["index"]: Path(p["path"])
                for p in manifest["pages"]
                if p["index"] not in skip
            }
            cover_image = Path(manifest["cover"]) if manifest.get("cover") else None
    else:
        page_files = sorted(
            list(Path(args.pages_dir).glob("page_*.jpg")) +
            list(Path(args.pages_dir).glob("page_*.png"))
        )
        all_pages   = {i + 1: p for i, p in enumerate(page_files)}
        cover_image = None

    pages   = [all_pages[i] for i in sorted(all_pages.keys())]
    short   = scripts["shorts"][0]   # always 1 short per chapter
    timing  = timing_data["shorts"][0]

    audio_path  = Path(args.audio_dir) / "short_part_01.wav"
    output_path = output_dir / f"short_ep{args.episode:02d}_ch{args.chapter:02d}.mp4"

    print(f"\n[Short] Building chapter {args.chapter} short → {output_path.name}")

    build_short(
        pages=pages,
        audio_path=audio_path,
        timing_words=timing["words"],
        output_path=output_path,
        manhwa=args.manhwa,
        episode=args.episode,
        chapter=args.chapter,
        cover_image=cover_image,
        audio_duration=timing["duration"],
    )

    print(f"\n[Short] ✅ Chapter {args.chapter} short done")


if __name__ == "__main__":
    main()