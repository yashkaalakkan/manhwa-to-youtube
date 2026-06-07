"""
build_shorts.py
Builds ONE short video per chapter (portrait 1080×1920).

Panel selection — NARRATIVE SYNC MODE:
  Maps each narration word to its source page using proportional position
  alignment (OCR pages are fed to the LLM in order, narration flows in
  the same order). Panel switches happen at the exact word timestamp when
  narration crosses into that page's content — like a human editor who
  watches the video and cuts panels to match what the narrator is saying.

  Example:
    OCR: page 1 = 40 words, page 2 = 60 words, page 3 = 30 words  (130 total)
    Narration: 90 words over 68 seconds
    → page 1 covers narration words  0–27  (0.0s – 20.5s)
    → page 2 covers narration words 28–69  (20.5s – 52.3s)
    → page 3 covers narration words 70–89  (52.3s – 68.0s)
    Panel switches at 20.5s and 52.3s exactly.
"""

import argparse
import json
import random
import re
import tempfile
from pathlib import Path
from typing import List, Optional, Dict, Tuple

import sys
sys.path.insert(0, str(Path(__file__).parent))

from video_utils import (
    TRANSITIONS as ANIMATIONS,
    build_video_ffmpeg,
    generate_ass_subtitles,
    prepare_cover_image,
    prepare_page_image,
)

COVER_DURATION_S = 3.0
MIN_PANEL_DUR_S  = 1.0   # never show a panel for less than 1s
MAX_PANEL_DUR_S  = 8.0   # never show a panel for more than 8s


# ── Narrative sync panel selection ───────────────────────────────────────────

def _page_num_from_path(path: Path) -> Optional[int]:
    """page_0003_panel_01.jpg → 3,  page_0003.jpg → 3"""
    m = re.search(r"page_0*(\d+)", path.name)
    return int(m.group(1)) if m else None


def _build_page_word_ranges(ocr_pages: List[dict]) -> List[Tuple[int, int, int]]:
    """
    Returns list of (page_index, cumulative_start_word, cumulative_end_word)
    sorted by page_index.

    Pages with fewer than MIN_PAGE_WORDS are excluded — their panels will
    never be selected because the narration doesn't meaningfully describe them.
    This covers:
      - Pure action panels (no speech bubbles, minimal text)
      - Chapter title/cover pages
      - Transition/blank separator pages
    """
    MIN_PAGE_WORDS = 8   # pages with fewer words than this are skipped

    ranges  = []
    cursor  = 0
    skipped = 0
    for page in sorted(ocr_pages, key=lambda p: p["page_index"]):
        words = page.get("raw_text", "").split()
        if len(words) < MIN_PAGE_WORDS:
            skipped += 1
            continue
        ranges.append((page["page_index"], cursor, cursor + len(words)))
        cursor += len(words)

    if skipped:
        print(f"  [Short] Skipped {skipped} low-text page(s) (< {MIN_PAGE_WORDS} words) "
              f"— their panels won't appear in the video")
    return ranges


def sync_panels_to_narration(
    panels: List[Path],
    ocr_pages: List[dict],
    timing_words: List[dict],
    audio_duration: float,
) -> List[Tuple[Path, float]]:
    """
    Returns [(panel_path, display_duration_seconds), ...].

    Steps:
    1. Build cumulative word ranges per page from OCR
    2. Map each narration word index → source page via proportional position
    3. Find timestamp boundaries where source page changes
    4. Pick the best panel for each page segment
    5. Return (panel, duration) pairs clipped to MIN/MAX
    """
    if not ocr_pages or not timing_words:
        # Fallback: uniform
        per = max(MIN_PANEL_DUR_S, min(audio_duration / max(len(panels), 1), MAX_PANEL_DUR_S))
        return [(p, per) for p in panels]

    page_ranges = _build_page_word_ranges(ocr_pages)
    if not page_ranges:
        per = max(MIN_PANEL_DUR_S, min(audio_duration / max(len(panels), 1), MAX_PANEL_DUR_S))
        return [(p, per) for p in panels]

    total_ocr_words = page_ranges[-1][2]  # cumulative end of last page
    n_narration     = len(timing_words)

    # Map narration word index → source page_index
    def narration_idx_to_page(nar_idx: int) -> int:
        # Position of this narration word as fraction through the narration
        frac = nar_idx / max(n_narration - 1, 1)
        # Map to OCR word position
        ocr_pos = frac * total_ocr_words
        for (pg, start, end) in page_ranges:
            if start <= ocr_pos < end:
                return pg
        return page_ranges[-1][0]  # last page for anything that overshoots

    # Build segments: list of (page_index, start_time, end_time)
    segments: List[Tuple[int, float, float]] = []
    current_page = narration_idx_to_page(0)
    seg_start    = timing_words[0]["start"]

    for i in range(1, n_narration):
        pg = narration_idx_to_page(i)
        if pg != current_page:
            segments.append((current_page, seg_start, timing_words[i]["start"]))
            current_page = pg
            seg_start    = timing_words[i]["start"]

    # Final segment runs to end of audio
    segments.append((current_page, seg_start, audio_duration))

    # Build page → panels lookup
    page_to_panels: Dict[int, List[Path]] = {}
    for panel in panels:
        pg = _page_num_from_path(panel)
        if pg is not None:
            page_to_panels.setdefault(pg, []).append(panel)

    # Assign panels to segments
    result: List[Tuple[Path, float]] = []
    used_panel_idx: Dict[int, int] = {}  # page → which panel index we're on

    for (pg, t_start, t_end) in segments:
        duration = t_end - t_start
        duration = max(MIN_PANEL_DUR_S, min(duration, MAX_PANEL_DUR_S))

        pg_panels = page_to_panels.get(pg, [])
        if not pg_panels:
            # No panels for this page — try adjacent pages
            for offset in [1, -1, 2, -2]:
                pg_panels = page_to_panels.get(pg + offset, [])
                if pg_panels:
                    break
        if not pg_panels:
            continue  # skip entirely if really nothing nearby

        # Cycle through panels of this page across multiple segments
        idx = used_panel_idx.get(pg, 0)
        panel = pg_panels[idx % len(pg_panels)]
        used_panel_idx[pg] = idx + 1

        result.append((panel, duration))

    if not result:
        print("  [Short] ⚠️  Sync selection yielded nothing — falling back to uniform")
        per = max(MIN_PANEL_DUR_S, min(audio_duration / max(len(panels), 1), MAX_PANEL_DUR_S))
        return [(p, per) for p in panels]

    unique_pages = len(set(_page_num_from_path(p) for p, _ in result))
    print(f"  [Short] Narrative sync: {len(result)} panel segment(s) across "
          f"{unique_pages} page(s) — cuts aligned to narration timing")
    return result


# ── Video builder ─────────────────────────────────────────────────────────────

def build_short(
    panels_with_durations: List[Tuple[Path, float]],
    audio_path: Path,
    timing_words: List[dict],
    output_path: Path,
    manhwa: str,
    episode: int,
    chapter: int,
    cover_image: Optional[Path],
    audio_duration: float,
) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        cover_dst = tmp / "cover.jpg"
        prepare_cover_image(manhwa, episode, chapter, cover_image, cover_dst)

        page_dsts = []
        page_durs = []
        for i, (panel_path, dur) in enumerate(panels_with_durations):
            dst = tmp / f"page_{i:03d}.jpg"
            prepare_page_image(panel_path, dst)
            page_dsts.append(dst)
            page_durs.append(dur)

        n = len(page_dsts)
        avg_dur = audio_duration / max(n, 1)
        print(f"  [Short] Ch{chapter}: {n} panels | {audio_duration:.1f}s audio | "
              f"~{avg_dur:.1f}s/panel avg")

        # Offset subtitle word timestamps by cover duration
        offset_words = [
            {**w,
             "start": w["start"] + COVER_DURATION_S,
             "end":   w["end"]   + COVER_DURATION_S}
            for w in timing_words
        ]

        ass_path = tmp / "subs.ass"
        generate_ass_subtitles(
            offset_words,
            COVER_DURATION_S + audio_duration,
            ass_path,
        )

        pool       = ANIMATIONS.copy()
        random.shuffle(pool)
        animations = [pool[i % len(pool)] for i in range(n)]

        build_video_ffmpeg(
            page_images        = page_dsts,
            page_durations     = page_durs,
            cover_image        = cover_dst,
            audio_path         = audio_path,
            ass_path           = ass_path,
            output_path        = output_path,
            animations         = animations,
            cover_duration_s   = COVER_DURATION_S,
            content_duration_s = audio_duration,
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

    # OCR data — extract_text.py writes to raw_text.json
    ocr_path  = Path(args.scripts).parent / "raw_text.json"
    ocr_pages = []
    if ocr_path.exists():
        with open(ocr_path, encoding="utf-8") as f:
            ocr_pages = json.load(f)
        print(f"[Short] OCR data loaded: {len(ocr_pages)} pages")
    else:
        print("[Short] ⚠️  raw_text.json not found — falling back to uniform panel timing")

    # Load panels from manifest
    manifest_path = (Path(args.manifest) if args.manifest
                     else Path(args.scripts).parent / "manifest.json")

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
        page_files  = sorted(
            list(Path(args.pages_dir).glob("page_*.jpg")) +
            list(Path(args.pages_dir).glob("page_*.png"))
        )
        all_pages   = {i + 1: p for i, p in enumerate(page_files)}
        cover_image = None

    panels = [all_pages[i] for i in sorted(all_pages.keys())]
    timing = timing_data["shorts"][0]
    audio_path  = Path(args.audio_dir) / "short_part_01.wav"
    output_path = output_dir / f"short_ep{args.episode:02d}_ch{args.chapter:02d}.mp4"

    print(f"\n[Short] Building chapter {args.chapter} short → {output_path.name}")

    # Narrative sync panel selection
    panels_with_durations = sync_panels_to_narration(
        panels       = panels,
        ocr_pages    = ocr_pages,
        timing_words = timing["words"],
        audio_duration = timing["duration"],
    )

    build_short(
        panels_with_durations = panels_with_durations,
        audio_path            = audio_path,
        timing_words          = timing["words"],
        output_path           = output_path,
        manhwa                = args.manhwa,
        episode               = args.episode,
        chapter               = args.chapter,
        cover_image           = cover_image,
        audio_duration        = timing["duration"],
    )

    print(f"\n[Short] ✅ Chapter {args.chapter} short done")


if __name__ == "__main__":
    main()