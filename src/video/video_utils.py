"""
video_utils.py
FFmpeg-based video builder with ASS subtitles.
- Shorts: portrait 1080×1920
- Full episode: landscape 1920×1080

Subtitle style (karaoke word-highlight):
  - Inactive words: white text, black outline — readable on any panel
  - Active word: white bold text on a solid coloured box (opaque fill)
  - Implemented via BorderStyle=3 (opaque box) with correct ASS override tags
"""

import os
import random
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional

from PIL import Image, ImageDraw, ImageFont

# ── Dimensions ──────────────────────────────────────────────────────────────
SHORT_WIDTH  = 1080
SHORT_HEIGHT = 1920
FULL_WIDTH   = 1920
FULL_HEIGHT  = 1080

FPS               = 30
FONT_SIZE         = 72
FONT_SIZE_FULL    = 60
SUBTITLE_Y_MARGIN = 280      # px from bottom

# ── Subtitle colours (ASS = &HAABBGGRR) ─────────────────────────────────────
# Inactive: white text, black border — visible on dark and light panels
INACTIVE_TEXT    = "&H00FFFFFF"   # white
INACTIVE_OUTLINE = "&H00000000"   # black border

# Active highlight box — vivid purple (matches reference image style)
HIGHLIGHT_BOX    = "&H00C83200"   # solid box fill  (ASS BGR: 0x0032C8 = purple-blue)
HIGHLIGHT_TEXT   = "&H00FFFFFF"   # white text on box

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


# ── Image prep ───────────────────────────────────────────────────────────────

def prepare_page_image(src: Path, dst: Path,
                       width: int = SHORT_WIDTH,
                       height: int = SHORT_HEIGHT) -> None:
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
        for py_i in range(h):
            r = int(15 + 40 * py_i / h); g = 0; b = int(30 + 60 * py_i / h)
            for ppx in range(w):
                px[ppx, py_i] = (r, g, b)

    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 140))
    base    = Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")
    draw    = ImageDraw.Draw(base)
    ft  = _pil_font(76)
    fep = _pil_font(62)
    fpt = _pil_font(52)

    def cx(text, font):
        bb = draw.textbbox((0, 0), text, font=font)
        return (w - (bb[2] - bb[0])) // 2

    _outlined(draw, cx(manhwa, ft),   h // 2 - 180, manhwa,       ft,  (255, 215,   0))
    ep_text = f"Episode {episode}"
    _outlined(draw, cx(ep_text, fep), h // 2 - 80,  ep_text,      fep, (255, 255, 255))
    if part is not None:
        pt_text = f"Chapter {part}"
        _outlined(draw, cx(pt_text, fpt), h // 2 + 20, pt_text,   fpt, (180, 180, 255))

    base.save(dst, "JPEG", quality=93)


# ── ASS subtitle generation ──────────────────────────────────────────────────

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
    width: int = SHORT_WIDTH,
    height: int = SHORT_HEIGHT,
) -> None:
    """
    Word-level karaoke subtitles matching the reference image style:
    - Inactive words: white text, black outline
    - Active word: white bold text inside a solid coloured highlight box
    - Box implemented with BorderStyle=3 + BackColour in the active word's style override
    - Window of 5 words shown at a time, active word centred when possible
    - Each dialogue line covers exactly one word's display window (start→next word start)
    """
    font_path = find_font()
    font_name = "NotoSans Bold"
    if font_path:
        font_name = Path(font_path).stem.replace("-", " ").replace("_", " ")

    # Base style: white text, black outline, no box (BorderStyle=1)
    header = f"""\
[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_name},{font_size},{INACTIVE_TEXT},{INACTIVE_TEXT},{INACTIVE_OUTLINE},&H00000000,-1,0,0,0,100,100,2,0,1,3,0,2,40,40,{SUBTITLE_Y_MARGIN},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    WINDOW = 5

    for i, word in enumerate(words):
        t_start = word["start"]
        # Hold until next word starts (keeps text on screen through pauses)
        t_end = words[i + 1]["start"] if i + 1 < len(words) else total_duration
        t_end = max(t_end, word["end"] + 0.05)   # always at least word's own end + tiny buffer

        # 5-word sliding window, active word as centred as possible
        half   = WINDOW // 2
        win_s  = max(0, i - half)
        win_e  = min(len(words), win_s + WINDOW)
        win_s  = max(0, win_e - WINDOW)   # re-anchor if we hit the end
        window = words[win_s:win_e]

        parts = []
        for j, w in enumerate(window):
            global_idx = win_s + j
            text = w["word"].upper()

            if global_idx == i:
                # ── ACTIVE word: solid highlight box ─────────────────────
                # \bord0\shad0     → remove outline/shadow from THIS word
                # \3c{BOX}         → outline colour = box colour (fills box)
                # \4c{BOX}         → shadow colour  = box colour (fills box shadow area)
                # \1c{WHITE}       → text colour white
                # \b1              → bold
                # \p0 / BorderStyle override via style reset doesn't work inline,
                # so we use the well-known trick: set \bord to a small positive
                # value + BorderStyle3 equivalent is achieved by \3c=\4c=box colour.
                # The most reliable cross-renderer approach is: \bord4\shad0\3c\4c.
                parts.append(
                    f"{{\\bord6\\shad0"
                    f"\\3c{HIGHLIGHT_BOX}\\4c{HIGHLIGHT_BOX}"
                    f"\\1c{HIGHLIGHT_TEXT}\\b1}}"
                    f"{text}"
                    f"{{\\r}}"
                )
            else:
                # ── INACTIVE word: white text, black outline, no box ──────
                parts.append(
                    f"{{\\bord3\\shad0\\1c{INACTIVE_TEXT}\\3c{INACTIVE_OUTLINE}\\b0}}"
                    f"{text}"
                )

        line = "  ".join(parts)   # double-space between words for readability
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
    page_durations: Optional[List[float]] = None,   # per-panel durations from semantic selector
    fade_dur: float = 0.4,
    width: int = SHORT_WIDTH,
    height: int = SHORT_HEIGHT,
) -> None:
    """
    Build the video so total duration = cover_duration_s + content_duration_s exactly.
    If page_durations is provided (from semantic selector), use those per-panel times.
    Otherwise distribute content_duration_s evenly across all panels.
    Audio fade anchored to audio end so voice is never clipped.
    """
    n_pages = len(page_images)

    if page_durations and len(page_durations) == n_pages:
        panel_durs = page_durations
    else:
        per = max(1.0, min(content_duration_s / max(n_pages, 1), 6.0))
        panel_durs = [per] * n_pages

    # Use the smallest panel duration to set transition length
    min_dur   = min(panel_durs) if panel_durs else 1.0
    trans_dur = min(0.3, min_dur * 0.15)

    all_images = [cover_image] + list(page_images)
    durations  = [cover_duration_s] + panel_durs
    total_segs = len(all_images)

    # Total video duration matches audio exactly
    total_dur      = cover_duration_s + content_duration_s
    audio_fade_st  = max(0.0, total_dur - fade_dur)
    video_fade_st  = max(0.0, total_dur - fade_dur)

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
            "-t", f"{total_dur:.3f}",   # hard duration cap = cover + audio
            "-movflags", "+faststart",
            str(output_path),
        ]
    )

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg failed:\n{result.stderr[-4000:]}")