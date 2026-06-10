"""
build_full_episode.py
Builds the full episode landscape video (1920x1080) by combining ALL chapters.

Called ONCE after all chapters are processed. It:
1. Concatenates full_chunk.wav from each chapter into one audio track
2. Merges all chapters' panels in order
3. Renders one landscape video: cover + all panels + synced subtitles
"""

import argparse
import json
import math
import os
import random
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

import numpy as np
import soundfile as sf

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

sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.memory import load_memory, save_memory, build_context_prompt, update_memory

COVER_DURATION_S = 5.0
MIN_PAGE_DUR_S   = 3.0
SHORTS_PER_COMPILATION = 5


def concat_audio(chunk_paths: list[Path], output_path: Path) -> tuple[float, list[dict]]:
    """
    Concatenate multiple wav files into one.
    Returns (total_duration, merged_word_timing_list).
    Word timings are offset so they align to the correct position in the combined audio.
    """
    all_samples = []
    total_dur   = 0.0
    sample_rate = 24000

    for path in chunk_paths:
        data, sr = sf.read(str(path))
        if sr != sample_rate:
            raise RuntimeError(f"Sample rate mismatch: {path} is {sr}Hz, expected {sample_rate}Hz")
        all_samples.append(data)
        total_dur += len(data) / sr

    combined = np.concatenate(all_samples)
    sf.write(str(output_path), combined, sample_rate)
    print(f"[FullEp] Combined audio: {len(chunk_paths)} chunks → {total_dur:.1f}s")
    return total_dur, combined


def merge_word_timings(chapter_timings: list[dict]) -> list[dict]:
    """
    Merge per-chapter word timing lists, offsetting each chapter's
    timestamps by the cumulative duration of previous chapters.
    """
    merged  = []
    offset  = 0.0
    for ct in chapter_timings:
        dur   = ct["duration"]
        for w in ct["words"]:
            merged.append({
                "word":  w["word"],
                "start": round(w["start"] + offset, 3),
                "end":   round(w["end"]   + offset, 3),
            })
        offset += dur
    return merged


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
        prepare_cover_image(manhwa, episode, part=None,
                            cover_image_path=cover_image, dst=cover_dst,
                            width=FULL_WIDTH, height=FULL_HEIGHT)
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
        ass_path.write_text("", encoding="utf-8")
        print(f"[FullEp] Subtitles: {len(offset_words)} words")

        pool = ANIMATIONS * (n_pages // len(ANIMATIONS) + 1)
        random.shuffle(pool)
        animations = pool[:n_pages]

        print(f"[FullEp] FFmpeg encoding ({COVER_DURATION_S}s cover + {content_dur:.1f}s content)...")
        build_video_ffmpeg(
            page_images=page_dsts,
            cover_image=cover_dst,
            audio_path=audio_path,
            output_path=output_path,
            animations=animations,
            cover_duration_s=COVER_DURATION_S,
            content_duration_s=content_dur,
            subtitle_words=offset_words,
            width=FULL_WIDTH,
            height=FULL_HEIGHT,
        )

    print(f"[FullEp] ✅ → {output_path}")


def build_compilation(short_video_paths: list, output_path: Path,
                      manhwa: str, episode: int, compilation_num: int,
                      gap_seconds: float = 0.9) -> None:
    """
    Concatenate short videos into one compilation with a black gap between each.
    gap_seconds: silence+black pause inserted between each short (default 0.9s)
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        # Get dimensions from first video
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "csv=p=0", str(short_video_paths[0])],
            capture_output=True, text=True
        )
        w, h = (1080, 1920)  # portrait default
        if probe.returncode == 0 and probe.stdout.strip():
            try:
                parts = probe.stdout.strip().split(",")
                w, h  = int(parts[0]), int(parts[1])
            except Exception:
                pass

        # Create a black gap video
        gap_path = tmp / "gap.mp4"
        subprocess.run([
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"color=black:s={w}x{h}:r=30:d={gap_seconds}",
            "-f", "lavfi", "-i", f"aevalsrc=0:c=stereo:s=44100:d={gap_seconds}",
            "-c:v", "libx264", "-preset", "fast", "-crf", "22", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            str(gap_path),
        ], capture_output=True)

        # Build concat list: short, gap, short, gap, ..., short (no trailing gap)
        listfile = tmp / "concat.txt"
        lines = []
        for i, p in enumerate(short_video_paths):
            lines.append(f"file '{p.resolve()}'\n")
            if i < len(short_video_paths) - 1:
                lines.append(f"file '{gap_path.resolve()}'\n")
        listfile.write_text("".join(lines), encoding="utf-8")

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

    print(f"[Compile] ✅ Compilation {compilation_num} ({len(short_video_paths)} shorts, {gap_seconds}s gaps) → {output_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manhwa",      required=True)
    parser.add_argument("--episode",     required=True, type=int)
    parser.add_argument("--output",      required=True)
    parser.add_argument("--shorts-dir",  required=True,
                        help="Directory where per-chapter shorts were saved")
    parser.add_argument("--chapters",    required=True, type=int,
                        help="Total number of chapters processed")
    parser.add_argument("--cover-link",  default="")
    parser.add_argument("--groq-api-key", default="")
    args = parser.parse_args()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # ── Collect per-chapter data ──────────────────────────────────────────
    all_pages         = {}
    chunk_timings     = []
    chunk_audio_paths = []
    combined_narration_parts = []
    cover_image       = None
    global_panel_idx  = 1

    for ch in range(1, args.chapters + 1):
        pipeline_dir = Path(f"pipeline/ch{ch}")
        manifest_path = pipeline_dir / "manifest.json"
        timing_path   = pipeline_dir / "audio_timing.json"

        if not manifest_path.exists():
            print(f"[FullEp] ⚠️  No manifest for chapter {ch} — skipping")
            continue

        with open(manifest_path) as f:
            manifest = json.load(f)

        # Panels
        if manifest.get("use_panels") and manifest.get("panels"):
            ch_pages = {p["index"]: Path(p["path"]) for p in manifest["panels"]}
        else:
            skip     = set(manifest.get("skip_pages", []))
            ch_pages = {
                p["index"]: Path(p["path"])
                for p in manifest["pages"]
                if p["index"] not in skip
            }

        for local_idx in sorted(ch_pages.keys()):
            all_pages[global_panel_idx] = ch_pages[local_idx]
            global_panel_idx += 1

        # Cover image (use chapter 1's)
        if ch == 1 and manifest.get("cover"):
            cover_image = Path(manifest["cover"])

        # Narration chunk for memory
        scripts_path = pipeline_dir / "scripts.json"
        if scripts_path.exists():
            with open(scripts_path) as f:
                ch_scripts = json.load(f)
            chunk_narr = ch_scripts.get("full_episode_chunk", {}).get("narration", "")
            if chunk_narr:
                combined_narration_parts.append(chunk_narr)

        # Audio chunk
        chunk_path = pipeline_dir / "audio" / "full_chunk.wav"
        if not chunk_path.exists():
            print(f"[FullEp] ⚠️  No full_chunk.wav for chapter {ch} — skipping audio")
            continue
        chunk_audio_paths.append(chunk_path)

        # Timing
        if timing_path.exists():
            with open(timing_path) as f:
                td = json.load(f)
            if td.get("full_episode_chunk"):
                chunk_timings.append(td["full_episode_chunk"])

    # ── Full episode = all shorts concatenated with 0.9s black gap ────────
    # Much simpler and more reliable than rebuilding video from panels+audio.
    # Each short already has correct subtitles and audio — just join them.
    shorts_dir_path = Path(args.shorts_dir)
    short_files_for_ep = sorted(
        shorts_dir_path.glob(f"short_ep{args.episode:02d}_ch*.mp4")
    )
    if short_files_for_ep:
        print(f"[FullEp] Building full episode from {len(short_files_for_ep)} shorts (0.9s gaps)")
        build_compilation(
            short_video_paths = short_files_for_ep,
            output_path       = output_path,
            manhwa            = args.manhwa,
            episode           = args.episode,
            compilation_num   = 0,
            gap_seconds       = 0.9,
        )
    else:
        print(f"[FullEp] ⚠️  No short files found — full episode video not built")
        return

    # ── Update series memory with full episode narration ──────────────────
    groq_api_key = args.groq_api_key or os.environ.get("GROQ_API_KEY", "")
    if groq_api_key and combined_narration_parts:
        try:
            import time
            from groq import Groq
            combined_narration = " ".join(combined_narration_parts)

            def _groq_call(client, prompt, max_tokens=400, retries=3):
                import re
                for attempt in range(retries):
                    try:
                        resp = client.chat.completions.create(
                            model="llama-3.1-8b-instant",
                            messages=[{"role": "user", "content": prompt}],
                            temperature=0.7, max_tokens=max_tokens,
                        )
                        return resp.choices[0].message.content.strip()
                    except Exception as e:
                        if attempt < retries - 1:
                            time.sleep(12)
                        else:
                            raise

            client = Groq(api_key=groq_api_key)
            memory = load_memory(args.manhwa)
            memory = update_memory(memory, args.episode, combined_narration, client, _groq_call)
            save_memory(args.manhwa, memory)
            print(f"[Memory] ✅ Series memory updated for episode {args.episode}")
        except Exception as e:
            print(f"[Memory] ⚠️  Could not update memory: {e}")
    else:
        print(f"[Memory] Skipping memory update (no GROQ_API_KEY or no narration)")
    shorts_dir  = Path(args.shorts_dir)
    short_files = sorted(shorts_dir.glob(f"short_ep{args.episode:02d}_ch*.mp4"))
    if short_files:
        n_compilations = math.ceil(len(short_files) / SHORTS_PER_COMPILATION)
        print(f"\n[Compile] {len(short_files)} shorts → {n_compilations} compilation(s)")
        for ci in range(n_compilations):
            batch     = short_files[ci * SHORTS_PER_COMPILATION:(ci + 1) * SHORTS_PER_COMPILATION]
            comp_path = output_path.parent / f"compilation_ep{args.episode:02d}_vol{ci+1:02d}.mp4"
            build_compilation(batch, comp_path, args.manhwa, args.episode, ci + 1)


if __name__ == "__main__":
    main()