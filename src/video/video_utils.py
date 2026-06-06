"""
video_utils.py
FFmpeg-based video builder with ASS subtitles.
- Shorts: portrait 1080×1920
- Full episode: landscape 1920×1080
Word-level karaoke subtitles: black inactive text, single accent colour box
with white text for the active word — colour is consistent across entire video.
"""

import os
import random
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional

from PIL import Image, ImageDraw, ImageFont

# ── Portrait (Shorts) ──────────────────────────────────────────────────────
SHORT_WIDTH  = 1080
SHORT_HEIGHT = 1920

# ── Landscape (Full episode) ───────────────────────────────────────────────
FULL_WIDTH  = 1920
FULL_HEIGHT = 1080

FPS               = 30
FONT_SIZE         = 72       # shorts
FONT_SIZE_FULL    = 60       # full episode (wider line, can be slightly smaller)
SUBTITLE_Y_MARGIN = 280      # from bottom of frame

# Inactive word colour — BLACK (ASS BGR: 00 00 00 00 → &H00000000)
INACTIVE_FG  = "&H00000000"
OUTLINE_COL  = "&H00FFFFFF"   # white outline so black text reads on any panel
SHADOW_COL   = "&H44FFFFFF"

# Single accent colour used for every highlighted word in the video.
# Deep accent blue — visible on both dark and light manhwa panels.
HIGHLIGHT_BOX_COLOUR = "&H00C8640A"   # ASS BGR → orange-amber box
HIGHLIGHT_TEXT       = "&H00FFFFFF"   # white text on box

TRANSITIONS = ["fade", "slideleft", "slideright", "slideup", "wipeleft", "wiperight"]

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def find_font() -> str:
    for p in _FONT_CANDIDATES:
        if os.path.exists(p):
            return p
    return ""


def _pil_font(size: int):
    for p in _FONT_CANDIDATES:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


# ── Image prep ──────────────────────────────────────────────────────────────

def prepare_page_image(src: Path, dst: Path, width: int = SHORT_WIDTH, height: int = SHORT_HEIGHT) -> None:
    """Fit panel inside target dimensions with white padding — never stretched or cropped."""
    img   = Image.open(src).convert("RGB")
    scale = min(width / img.width, height / img.height)
    new_w = int(img.width  * scale)
    new_h = int(img.height * scale)
    img   = img.resize((new_w, new_h), Image.LANCZOS)
    canvas = Image.new("RGB", (width, height), (255, 255, 255))
    canvas.paste(img, ((width - new_w) // 2, (height - new_h) // 2))
    canvas.save(dst, "JPEG", quality=92)


def prepare_cover_image(
    manhwa: str,
    episode: int,
    part: Optional[int],
    cover_image_path: Optional[Path],
    dst: Path,
    width: int = SHORT_WIDTH,
    height: int = SHORT_HEIGHT,
) -> None:
    def _outlined(draw, x, y, text, font, fill, outline=(0, 0, 0), ow=4):
        for dx in range(-ow, ow + 1):
            for dy in range(-ow, ow + 1):
                if dx or dy:
                    draw.text((x + dx, y + dy), text, font=font, fill=outline)
        draw.text((x, y), text, font=font, fill=fill)

    w, h = width, height

    if cover_image_path and Path(cover_image_path).exists():
        base  = Image.open(cover_image_path).convert("RGB")
        ratio = max(w / base.width, h / base.height)
        base  = base.resize((int(base.width * ratio), int(base.height * ratio)), Image.LANCZOS)
        bx    = (base.width  - w) // 2
        by    = (base.height - h) // 2
        base  = base.crop((bx, by, bx + w, by + h))
    else:
        base = Image.new("RGB", (w, h))
        px   = base.load()
        for py in range(h):
            r = int(15 + 40 * py / h); g = 0; b = int(30 + 60 * py / h)
            for ppx in range(w):
                px[ppx, py] = (r, g, b)

    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 140))
    base    = Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")
    draw    = ImageDraw.Draw(base)
    ft      = _pil_font(76)
    fep     = _pil_font(62)
    fpt     = _pil_font(52)

    def cx(text, font):
        bb = draw.textbbox((0, 0), text, font=font)
        return (w - (bb[2] - bb[0])) // 2

    _outlined(draw, cx(manhwa, ft),   h // 2 - 180, manhwa,      ft,  (255, 215,   0))
    ep_text = f"Episode {episode}"
    _outlined(draw, cx(ep_text, fep), h // 2 - 80,  ep_text,     fep, (255, 255, 255))
    if part is not None:
        pt_text = f"Part {part}"
        _outlined(draw, cx(pt_text, fpt), h // 2 + 20, pt_text, fpt, (180, 180, 255))

    base.save(dst, "JPEG", quality=93)


# ── ASS subtitle generation ─────────────────────────────────────────────────

def _ass_time(s: float) -> str:
    h  = int(s // 3600)
    m  = int((s % 3600) // 60)
    sc = int(s % 60)
    cs = int(round((s - int(s)) * 100))
    return f"{h}:{m:02d}:{sc:02d}.{cs:02d}"


def generate_ass_subtitles(
    words: List[dict],
    total_duration: float,
    output_path: Path,
    font_size: int = FONT_SIZE,
) -> None:
    """
    Generate ASS subtitle file with word-level karaoke highlighting.

    Style rules (per user spec #3 / #11):
    - All inactive words: BLACK text, no box
    - Active (highlighted) word: WHITE text, accent-colour opaque background box
    - ONE consistent accent colour for the whole video (HIGHLIGHT_BOX_COLOUR)
    - Timestamps extend to the next word's start so the window never goes blank
    """
    font_path = find_font()
    font_name = "NotoSans Bold"
    if font_path:
        font_name = Path(font_path).stem.replace("-", " ").replace("_", " ")

    header = f"""\
[Script Info]
ScriptType: v4.00+
PlayResX: {SHORT_WIDTH}
PlayResY: {SHORT_HEIGHT}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_name},{font_size},{INACTIVE_FG},{INACTIVE_FG},{OUTLINE_COL},{SHADOW_COL},-1,0,0,0,100,100,2,0,1,2,1,2,40,40,{SUBTITLE_Y_MARGIN},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    WINDOW = 5

    for i, word in enumerate(words):
        t_start = word["start"]
        # Extend to next word start so caption window stays visible through pauses
        if i + 1 < len(words):
            t_end = words[i + 1]["start"]
        else:
            t_end = total_duration
        # Never shrink below the word's own end
        t_end = max(t_end, word["end"])

        win_start = max(0, i - WINDOW // 2)
        win_end   = min(len(words), win_start + WINDOW)
        if win_end == len(words):
            win_start = max(0, win_end - WINDOW)
        window = words[win_start:win_end]

        parts = []
        for j, w in enumerate(window):
            global_j = win_start + j
            text     = w["word"].upper()

            if global_j == i:
                # ── Active word: accent box + white bold text ──────────────
                # dur_cs spans the full display window (t_start → t_end) so
                # the box stays on screen through inter-word pauses.
                dur_cs = max(1, int(round((t_end - t_start) * 100)))
                styled = (
                    f"{{\\kf{dur_cs}"
                    f"\\bord0\\shad0"
                    f"\\BorderStyle3"
                    f"\\4c{HIGHLIGHT_BOX_COLOUR}"   # box fill colour
                    f"\\1c{HIGHLIGHT_TEXT}"          # text colour = white
                    f"\\b1}}"
                    f"{text}"
                    f"{{\\r}}"   # reset style for remaining words
                )
            else:
                # ── Inactive word: black text, no box ─────────────────────
                dur_cs = max(1, int(round((w["end"] - w["start"]) * 100)))
                styled = f"{{\\k{dur_cs}\\1c{INACTIVE_FG}}}{text}"

            parts.append(styled)

        line = " ".join(parts)
        events.append(
            f"Dialogue: 0,{_ass_time(t_start)},{_ass_time(t_end)},"
            f"Default,,0,0,0,,{line}\n"
        )

    output_path.write_text(header + "".join(events), encoding="utf-8")


# ── FFmpeg video builder ─────────────────────────────────────────────────────

def build_video_ffmpeg(
    page_images: List[Path],
    cover_image: Path,
    audio_path: Path,
    ass_path: Path,
    output_path: Path,
    animations: List[str],
    cover_duration_s: float = 3.0,
    content_duration_s: float = 60.0,
    fade_dur: float = 0.5,
    width: int = SHORT_WIDTH,
    height: int = SHORT_HEIGHT,
) -> None:
    n_pages      = len(page_images)
    per_page_dur = content_duration_s / max(n_pages, 1)
    per_page_dur = max(1.0, min(per_page_dur, 4.0))
    trans_dur    = min(0.3, per_page_dur * 0.15)

    all_images = [cover_image] + list(page_images)
    durations  = [cover_duration_s] + [per_page_dur] * n_pages
    total_segs = len(all_images)
    total_dur  = cover_duration_s + per_page_dur * n_pages

    # ── FIX #4: audio fade-out starts 0.5s before end of AUDIO, not video ──
    # This prevents the voice being clipped if video is marginally shorter
    audio_fade_st = max(0.0, content_duration_s + cover_duration_s - fade_dur)
    video_fade_st = max(0.0, total_dur - fade_dur)

    inputs = []
    for img, dur in zip(all_images, durations):
        inputs += ["-loop", "1", "-t", f"{dur + trans_dur:.3f}", "-i", str(img)]
    inputs += ["-i", str(audio_path)]
    audio_idx = total_segs

    filter_parts = []
    for idx in range(total_segs):
        lbl = f"s{idx}"
        filter_parts.append(
            f"[{idx}:v]scale={width}:{height}:"
            f"force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:0xFFFFFF,"
            f"setsar=1,fps={FPS}[{lbl}]"
        )

    if total_segs == 1:
        filter_parts.append("[s0]copy[xout]")
    else:
        prev_label = "s0"
        offset     = durations[0] - trans_dur
        trans_list = animations if animations else TRANSITIONS
        for i in range(1, total_segs):
            out_label = "xout" if i == total_segs - 1 else f"x{i}"
            trans     = trans_list[(i - 1) % len(trans_list)]
            filter_parts.append(
                f"[{prev_label}][s{i}]xfade=transition={trans}"
                f":duration={trans_dur:.3f}:offset={offset:.3f}[{out_label}]"
            )
            prev_label = out_label
            offset    += durations[i] - trans_dur

    ass_safe = str(ass_path).replace("\\", "/").replace(":", "\\:")
    filter_parts.append(f"[xout]ass='{ass_safe}'[subv]")
    filter_parts.append(
        f"[subv]fade=t=in:st=0:d={fade_dur},"
        f"fade=t=out:st={video_fade_st:.3f}:d={fade_dur}[fv]"
    )
    # Audio fade anchored to audio duration, not video duration
    filter_parts.append(
        f"[{audio_idx}:a]afade=t=in:st=0:d={fade_dur},"
        f"afade=t=out:st={audio_fade_st:.3f}:d={fade_dur}[fa]"
    )

    cmd = (
        ["ffmpeg", "-y"]
        + inputs
        + [
            "-filter_complex", "; ".join(filter_parts),
            "-map", "[fv]",
            "-map", "[fa]",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "22",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "192k",
            "-shortest",
            "-movflags", "+faststart",
            str(output_path),
        ]
    )

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg failed:\n{result.stderr[-4000:]}")