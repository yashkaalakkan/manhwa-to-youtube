"""
build_full_episode.py
Builds the full episode long-form video using FFmpeg natively.
Cover (5s) + all pages animated + ASS subtitles + audio fades.
No Python frame loops.
"""

import argparse
import json
import random
import tempfile
from pathlib import Path
from typing import Optional

from video_utils import (
    TRANSITIONS as ANIMATIONS,
    build_video_ffmpeg,
    generate_ass_subtitles,
    prepare_cover_image,
    prepare_page_image,
)

COVER_DURATION_S = 5.0
MIN_PAGE_DUR_S   = 3.0


def build_full_episode(
    all_pages: dict,
    audio_path: Path,
    timing_words: list,
    output_path: Path,
    manhwa: str,
    episode: int,
    cover_image: Optional[Path],
    audio_duration: float,
) -> None:
    page_indices = sorted(all_pages.keys())
    n_pages      = len(page_indices)
    content_dur  = max(audio_duration, n_pages * MIN_PAGE_DUR_S)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        # Prepare cover (part=None → no "Part X of 4" label)
        cover_dst = tmp / "cover.jpg"
        prepare_cover_image(manhwa, episode, part=None,
                            cover_image_path=cover_image, dst=cover_dst)
        print(f"[FullEp] Cover prepared")

        # Prepare all pages
        page_dsts = []
        for i, idx in enumerate(page_indices):
            dst = tmp / f"page_{i:04d}.jpg"
            prepare_page_image(all_pages[idx], dst)
            page_dsts.append(dst)
        print(f"[FullEp] {n_pages} pages prepped")

        # Offset word timestamps by cover duration
        offset_words = [
            {**w, "start": w["start"] + COVER_DURATION_S,
                  "end":   w["end"]   + COVER_DURATION_S}
            for w in timing_words
        ]

        # Generate ASS subtitles
        ass_path = tmp / "subs.ass"
        generate_ass_subtitles(offset_words, COVER_DURATION_S + content_dur, ass_path)
        print(f"[FullEp] Subtitles: {len(offset_words)} words")

        # Shuffle animations across pages
        pool = ANIMATIONS * (n_pages // len(ANIMATIONS) + 1)
        random.shuffle(pool)
        animations = pool[:n_pages]

        print(f"[FullEp] FFmpeg encoding ({COVER_DURATION_S}s cover + {content_dur:.1f}s content)...")
        build_video_ffmpeg(
            page_images=page_dsts,
            cover_image=cover_dst,
            audio_path=audio_path,
            ass_path=ass_path,
            output_path=output_path,
            animations=animations,
            cover_duration_s=COVER_DURATION_S,
            content_duration_s=content_dur,
        )

    print(f"[FullEp] ✅ → {output_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages-dir",  required=True)
    parser.add_argument("--audio-dir",  required=True)
    parser.add_argument("--scripts",    required=True)
    parser.add_argument("--output",     required=True)
    parser.add_argument("--manhwa",     required=True)
    parser.add_argument("--episode",    required=True, type=int)
    args = parser.parse_args()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(args.scripts, encoding="utf-8") as f:
        scripts = json.load(f)

    timing_path = Path(args.scripts).parent / "audio_timing.json"
    with open(timing_path, encoding="utf-8") as f:
        timing_data = json.load(f)

    manifest_path = Path("pipeline/manifest.json")
    if manifest_path.exists():
        with open(manifest_path) as f:
            manifest = json.load(f)
        if manifest.get("use_panels") and manifest.get("panels"):
            all_pages   = {p["index"]: Path(p["path"]) for p in manifest["panels"]}
            cover_image = Path(manifest["cover"]) if manifest.get("cover") else None
            print(f"[FullEp] Using {len(all_pages)} panels (panel-split mode)")
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

    full_audio     = Path(args.audio_dir) / "full_episode.wav"
    timing_words   = timing_data["full_episode"]["words"]
    audio_duration = timing_data["full_episode"]["duration"]

    print(f"[FullEp] {len(all_pages)} pages | {audio_duration:.1f}s audio")

    build_full_episode(
        all_pages=all_pages,
        audio_path=full_audio,
        timing_words=timing_words,
        output_path=output_path,
        manhwa=args.manhwa,
        episode=args.episode,
        cover_image=cover_image,
        audio_duration=audio_duration,
    )


if __name__ == "__main__":
    main()