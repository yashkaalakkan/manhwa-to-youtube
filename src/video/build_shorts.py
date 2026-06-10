"""
build_shorts.py  —  Builds ONE short video per chapter (portrait 1080×1920).

Panel selection — AI-GUIDED MODE:
  Asks the LLM which panel numbers to use, in what order, based on the
  narration text. The LLM knows the narration and knows the page count,
  so it picks panels that actually match what's being described.
  Falls back to proportional sync if LLM call fails.

Duplicate prevention: a panel is never shown twice in a row.
Min short duration: narration is padded to at least MIN_SHORT_WORDS if too short.
"""

import argparse
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Optional, Tuple, Dict

sys.path.insert(0, str(Path(__file__).parent))
from video_utils import (
    TRANSITIONS as ANIMATIONS,
    build_video_ffmpeg,
    generate_ass_subtitles,
    prepare_cover_image,
    prepare_page_image,
)

COVER_DURATION_S = 3.0
MIN_PANEL_DUR_S  = 2.0   # minimum seconds per panel
MAX_PANEL_DUR_S  = 3.5   # maximum seconds per panel — keeps video dynamic
MIN_SHORT_WORDS  = 80    # floor for narration length (~32s at 2.5 w/s)


# ── AI panel selection ────────────────────────────────────────────────────────

def _llm_call(prompt: str, max_tokens: int = 300) -> Optional[str]:
    """Call Gemini (primary) or Groq (fallback). Returns None on failure."""
    # Gemini
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if key:
        try:
            import google.generativeai as genai
            genai.configure(api_key=key)
            model = genai.GenerativeModel("gemini-2.5-flash-lite-preview-06-17")
            resp  = model.generate_content(
                prompt,
                generation_config=genai.GenerationConfig(
                    max_output_tokens=max_tokens, temperature=0.3)
            )
            return resp.text.strip()
        except Exception as e:
            print(f"  [PanelAI] Gemini failed ({e}) — trying Groq")

    # Groq fallback
    for env in ["GROQ_API_KEY"] + [f"GROQ_API_KEY_{i}" for i in range(2, 6)]:
        groq_key = os.environ.get(env, "").strip()
        if not groq_key:
            continue
        try:
            from groq import Groq
            client = Groq(api_key=groq_key)
            resp   = client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3, max_tokens=max_tokens,
            )
            return resp.choices[0].message.content.strip()
        except Exception:
            continue
    return None


def panels_from_timeline(
    panels: List[Path],
    page_timeline: List[dict],
    timing_words: List[dict],
    audio_duration: float,
) -> List[Tuple[Path, float]]:
    """
    Convert the page_timeline from scripts.json into (panel, duration) pairs.

    page_timeline entries: {page, narration_word_start, narration_word_end}
    timing_words entries:  {word, start, end}  — actual timestamps from stable-ts

    For each timeline segment:
    - Map narration_word_start → actual audio timestamp using timing_words
    - Map narration_word_end   → actual audio timestamp
    - Show the panel for that exact duration
    - Pick the best panel from that page (avoid consecutive duplicates)
    """
    def page_of(path: Path) -> Optional[int]:
        m = re.search(r"page_0*(\d+)", path.name)
        return int(m.group(1)) if m else None

    page_to_panels: Dict[int, List[Path]] = {}
    for panel in panels:
        pg = page_of(panel)
        if pg is not None:
            page_to_panels.setdefault(pg, []).append(panel)

    n_words = len(timing_words)

    def word_idx_to_time(idx: int) -> float:
        if not timing_words:
            return audio_duration * idx / max(n_words, 1)
        idx = max(0, min(idx, n_words - 1))
        return timing_words[idx]["start"]

    result: List[Tuple[Path, float]] = []
    last_panel = None

    for i, seg in enumerate(page_timeline):
        pg      = int(seg.get("page", 0))
        w_start = int(seg.get("narration_word_start", 0))
        w_end   = int(seg.get("narration_word_end",   n_words))

        t_start = word_idx_to_time(w_start)
        t_end   = word_idx_to_time(w_end) if w_end < n_words else audio_duration
        dur     = max(MIN_PANEL_DUR_S, min(t_end - t_start, MAX_PANEL_DUR_S))

        pg_panels = page_to_panels.get(pg, [])
        if not pg_panels:
            for offset in [1, -1, 2, -2, 3, -3]:
                pg_panels = page_to_panels.get(pg + offset, [])
                if pg_panels:
                    break
        if not pg_panels:
            continue

        # Avoid consecutive same panel — cycle through panels on this page
        panel = pg_panels[0]
        if panel == last_panel and len(pg_panels) > 1:
            panel = pg_panels[1]
        elif panel == last_panel:
            continue

        result.append((panel, dur))
        last_panel = panel

    if not result:
        print("  [Panels] ⚠️  Timeline empty — falling back to proportional")
        return _proportional_fallback(panels, audio_duration)

    print(f"  [Panels] ✅ {len(result)} panels from narration timeline (exact page sync)")
    return result


def ai_select_panels(
    panels: List[Path],
    narration: str,
    ocr_pages: List[dict],
    audio_duration: float,
    timing_words: List[dict] = None,
) -> List[Tuple[Path, float]]:
    """
    Fallback AI panel selection — only used when page_timeline is missing
    from scripts.json (e.g. LLM returned plain text instead of JSON).
    Asks the AI for page assignments with timestamps.
    """
    def page_of(path: Path) -> Optional[int]:
        m = re.search(r"page_0*(\d+)", path.name)
        return int(m.group(1)) if m else None

    page_to_panels: Dict[int, List[Path]] = {}
    for i, panel in enumerate(panels):
        pg = page_of(panel)
        if pg is not None:
            page_to_panels.setdefault(pg, []).append(panel)

    page_summaries = []
    for p in sorted(ocr_pages, key=lambda x: x["page_index"]):
        words = p.get("raw_text", "").split()
        if not words:
            continue
        pg_idx   = p["page_index"]
        pg_preview = " ".join(words[:12])
        page_summaries.append(f"Page {pg_idx}: {pg_preview}")

    timed_words = ""
    if timing_words:
        sample = timing_words[::max(1, len(timing_words) // 25)]
        parts = [f"{w['word']}@{w['start']:.1f}s" for w in sample]
        timed_words = "\nTIMESTAMPS: " + ", ".join(parts)

    target_min = max(15, int(audio_duration / MAX_PANEL_DUR_S))
    target_max = max(25, int(audio_duration / MIN_PANEL_DUR_S))

    prompt = f"""Video editor syncing manga panels to narration ({audio_duration:.0f}s).

NARRATION: {narration[:400]}
{timed_words}

PAGES: {chr(10).join(page_summaries[:20])}

Pick {target_min}–{target_max} panels with timestamps. Each panel 2–3.5s.
JSON only: [{{"page":3,"at":0.0}},{{"page":7,"at":2.5}}]"""

    raw = _llm_call(prompt, max_tokens=500)
    assignments = []
    if raw:
        m = re.search(r'\[.*?\]', raw, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(0))
                if parsed and isinstance(parsed[0], dict) and "page" in parsed[0]:
                    assignments = parsed
            except Exception:
                pass

    if not assignments:
        return _proportional_fallback(panels, audio_duration)

    result = []
    last_panel = None
    for i, a in enumerate(assignments):
        pg  = int(a.get("page", 0))
        t_s = float(a.get("at", 0.0))
        t_e = float(assignments[i+1]["at"]) if i+1 < len(assignments) else audio_duration
        dur = max(MIN_PANEL_DUR_S, min(t_e - t_s, MAX_PANEL_DUR_S))

        pg_panels = page_to_panels.get(pg, [])
        if not pg_panels:
            for offset in [1,-1,2,-2]:
                pg_panels = page_to_panels.get(pg+offset, [])
                if pg_panels: break
        if not pg_panels:
            continue

        panel = pg_panels[0]
        if panel == last_panel and len(pg_panels) > 1:
            panel = pg_panels[1]
        elif panel == last_panel:
            continue
        result.append((panel, dur))
        last_panel = panel

    if not result:
        return _proportional_fallback(panels, audio_duration)

    print(f"  [Panels] ✅ {len(result)} panels from AI fallback")
    return result


def _proportional_fallback(panels: List[Path], audio_duration: float) -> List[Tuple[Path, float]]:
    """Simple proportional fallback — all panels, even duration."""
    per = max(MIN_PANEL_DUR_S, min(audio_duration / max(len(panels), 1), MAX_PANEL_DUR_S))
    # Deduplicate consecutive identical panels
    result = []
    prev = None
    for p in panels:
        if p != prev:
            result.append((p, per))
            prev = p
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
        avg = audio_duration / max(n, 1)
        print(f"  [Short] Ch{chapter}: {n} panels | {audio_duration:.1f}s audio | ~{avg:.1f}s/panel")

        # Pass word timings to build_video_ffmpeg via sidecar attribute
        # (avoids changing the function signature across all callers)
        ass_path = tmp / "subs.ass"
        ass_path.write_text("", encoding="utf-8")
        # Offset word timestamps by cover duration so subtitles start after cover
        offset_words = [
            {**w,
             "start": round(w["start"] + COVER_DURATION_S, 3),
             "end":   round(w["end"]   + COVER_DURATION_S, 3)}
            for w in timing_words
        ]
        ass_path._words = offset_words  # picked up by build_video_ffmpeg

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

    ocr_path  = Path(args.scripts).parent / "raw_text.json"
    ocr_pages = []
    if ocr_path.exists():
        with open(ocr_path, encoding="utf-8") as f:
            ocr_pages = json.load(f)

    # Load panels
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

    narration     = scripts["shorts"][0]["narration"]
    page_timeline = scripts["shorts"][0].get("page_timeline", [])

    print(f"\n[Short] Building chapter {args.chapter} short → {output_path.name}")

    if page_timeline:
        # Use the page timeline generated alongside the narration — exact sync
        print(f"[Short] Using page_timeline from scripts.json ({len(page_timeline)} segments)")
        panels_with_durations = panels_from_timeline(
            panels        = panels,
            page_timeline = page_timeline,
            timing_words  = timing["words"],
            audio_duration = timing["duration"],
        )
    else:
        # Fallback — LLM returned plain text instead of JSON
        print("[Short] No page_timeline in scripts.json — using AI fallback")
        panels_with_durations = ai_select_panels(
            panels         = panels,
            narration      = narration,
            ocr_pages      = ocr_pages,
            audio_duration = timing["duration"],
            timing_words   = timing["words"],
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