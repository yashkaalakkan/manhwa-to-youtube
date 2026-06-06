"""
build_full_episode.py
Builds the full episode long-form video in LANDSCAPE 1920x1080.
Cover (5s) + all pages animated + ASS subtitles + audio fades.
Every 5 shorts are also compiled into a single landscape compilation video.
"""

import argparse
import json
import math
import random
import tempfile
from pathlib import Path
from typing import Optional

from video_utils import (
    TRANSITIONS as ANIMATIONS,
    FULL_WIDTH,
    FULL_HEIGHT,
    FONT_SIZE_FULL,
    build_video_ffmpeg,
    generate_ass_subtitles,
    prepare_cover_image,
    prepare_page_image,
)

COVER_DURATION_S  = 5.0
MIN_PAGE_DUR_S    = 3.0
SHORTS_PER_VIDEO  = 5   # every 5 shorts become 1 compiled long video


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

        cover_dst = tmp / "cover.jpg"
        prepare_cover_image(
            manhwa, episode, part=None,
            cover_image_path=cover_image, dst=cover_dst,
            width=FULL_WIDTH, height=FULL_HEIGHT,
        )
        print(f"[FullEp] Cover prepared (landscape {FULL_WIDTH}×{FULL_HEIGHT})")

        page_dsts = []
        for i, idx in enumerate(page_indices):
            dst = tmp / f"page_{i:04d}.jpg"
            prepare_page_image(all_pages[idx], dst, width=FULL_WIDTH, height=FULL_HEIGHT)
            page_dsts.append(dst)
        print(f"[FullEp] {n_pages} pages prepped (landscape)")

        offset_words = [
            {**w, "start": w["start"] + COVER_DURATION_S,
                  "end":   w["end"]   + COVER_DURATION_S}
            for w in timing_words
        ]

        ass_path = tmp / "subs.ass"
        generate_ass_subtitles(
            offset_words,
            COVER_DURATION_S + content_dur,
            ass_path,
            font_size=FONT_SIZE_FULL,
        )
        print(f"[FullEp] Subtitles: {len(offset_words)} words")

        pool = ANIMATIONS * (n_pages // len(ANIMATIONS) + 1)
        random.shuffle(pool)
        animations = pool[:n_pages]

        print(f"[FullEp] FFmpeg encoding ({COVER_DURATION_S}s cover + {content_dur:.1f}s content, landscape)...")
        build_video_ffmpeg(
            page_images=page_dsts,
            cover_image=cover_dst,
            audio_path=audio_path,
            ass_path=ass_path,
            output_path=output_path,
            animations=animations,
            cover_duration_s=COVER_DURATION_S,
            content_duration_s=content_dur,
            width=FULL_WIDTH,
            height=FULL_HEIGHT,
        )

    print(f"[FullEp] ✅ → {output_path}")


def build_compilation(
    short_video_paths: list,
    output_path: Path,
    manhwa: str,
    episode: int,
    compilation_num: int,
) -> None:
    """
    Concatenate multiple short videos into one landscape compilation video.
    Uses FFmpeg concat demuxer — no re-encoding of individual clips.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp      = Path(tmpdir)
        listfile = tmp / "concat.txt"
        lines    = [f"file '{p.resolve()}'\n" for p in short_video_paths]
        listfile.write_text("".join(lines), encoding="utf-8")

        import subprocess
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(listfile),
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            str(output_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"FFmpeg concat failed:\n{result.stderr[-3000:]}")

    print(f"[Compile] ✅ Compilation {compilation_num} ({len(short_video_paths)} shorts) → {output_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages-dir",    required=True)
    parser.add_argument("--audio-dir",    required=True)
    parser.add_argument("--scripts",      required=True)
    parser.add_argument("--output",       required=True)
    parser.add_argument("--manhwa",       required=True)
    parser.add_argument("--episode",      required=True, type=int)
    # optional: path to shorts output dir for compilation step
    parser.add_argument("--shorts-dir",   default="")
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

    print(f"[FullEp] {len(all_pages)} pages | {audio_duration:.1f}s audio | landscape {FULL_WIDTH}×{FULL_HEIGHT}")

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

    # ── Compile every 5 shorts into a long landscape video ──────────────
    if args.shorts_dir:
        shorts_dir = Path(args.shorts_dir)
        short_files = sorted(shorts_dir.glob(f"short_ep{args.episode:02d}_part*.mp4"))
        if short_files:
            n_compilations = math.ceil(len(short_files) / SHORTS_PER_VIDEO)
            print(f"\n[Compile] {len(short_files)} shorts → {n_compilations} compilation video(s)")
            for ci in range(n_compilations):
                batch       = short_files[ci * SHORTS_PER_VIDEO : (ci + 1) * SHORTS_PER_VIDEO]
                comp_path   = output_path.parent / f"compilation_ep{args.episode:02d}_vol{ci+1:02d}.mp4"
                build_compilation(batch, comp_path, args.manhwa, args.episode, ci + 1)


if __name__ == "__main__":
    main()