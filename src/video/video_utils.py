"""
video_utils.py
Fast video building using FFmpeg scale/pad/xfade — no zoompan.
Each panel is letterboxed to 1080x1920, joined with xfade transitions.
ASS subtitles burned in via libass.
"""

import os
import random
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional

from PIL import Image, ImageDraw, ImageFont

SHORT_WIDTH       = 1080
SHORT_HEIGHT      = 1920
FPS               = 30
FONT_SIZE         = 72
SUBTITLE_Y_MARGIN = 280

INACTIVE_FG = "&H00FFFFFF"
OUTLINE_COL = "&H00000000"
SHADOW_COL  = "&H88000000"

# Random highlight box colours (BGR hex for ASS \4c)
HIGHLIGHT_COLOURS = [
    "&H00CC0000",   # blue
    "&H0000AA00",   # green
    "&H000000CC",   # red
    "&H00CC6600",   # orange
    "&H00990099",   # purple
    "&H0000AAAA",   # yellow
    "&H00AA0055",   # pink-blue
]

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

def prepare_page_image(src: Path, dst: Path) -> None:
    """Fit panel inside 1080x1920 with white padding — never stretched or cropped."""
    img   = Image.open(src).convert("RGB")
    scale = min(SHORT_WIDTH / img.width, SHORT_HEIGHT / img.height)
    new_w = int(img.width  * scale)
    new_h = int(img.height * scale)
    img   = img.resize((new_w, new_h), Image.LANCZOS)
    canvas = Image.new("RGB", (SHORT_WIDTH, SHORT_HEIGHT), (255, 255, 255))
    canvas.paste(img, ((SHORT_WIDTH - new_w) // 2, (SHORT_HEIGHT - new_h) // 2))
    canvas.save(dst, "JPEG", quality=92)


def prepare_cover_image(
    manhwa: str,
    episode: int,
    part: Optional[int],
    cover_image_path: Optional[Path],
    dst: Path,
) -> None:
    def _outlined(draw, x, y, text, font, fill, outline=(0, 0, 0), ow=4):
        for dx in range(-ow, ow + 1):
            for dy in range(-ow, ow + 1):
                if dx or dy:
                    draw.text((x + dx, y + dy), text, font=font, fill=outline)
        draw.text((x, y), text, font=font, fill=fill)

    w, h = SHORT_WIDTH, SHORT_HEIGHT

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

    _outlined(draw, cx(manhwa, ft),   h // 2 - 180, manhwa,           ft,  (255, 215,   0))
    ep_text = f"Episode {episode}"
    _outlined(draw, cx(ep_text, fep), h // 2 - 80,  ep_text,          fep, (255, 255, 255))
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
) -> None:
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
Style: Default,{font_name},{FONT_SIZE},{INACTIVE_FG},{INACTIVE_FG},{OUTLINE_COL},{SHADOW_COL},-1,0,0,0,100,100,2,0,1,3,2,2,40,40,{SUBTITLE_Y_MARGIN},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    WINDOW = 5

    # Pre-assign a random colour to each word (consistent per word, not per render)
    word_colours = [random.choice(HIGHLIGHT_COLOURS) for _ in words]

    for i, word in enumerate(words):
        t_start = word["start"]
        # Extend end to the next word's start (or total_duration for the last word).
        # This keeps the caption window visible through pauses and "..." beats
        # instead of going blank between word.end and the next word.start.
        if i + 1 < len(words):
            t_end = words[i + 1]["start"]
        else:
            t_end = total_duration
        # Never shrink below the word's own end timestamp
        t_end = max(t_end, word["end"])

        win_start = max(0, i - WINDOW // 2)
        win_end   = min(len(words), win_start + WINDOW)
        if win_end == len(words):
            win_start = max(0, win_end - WINDOW)
        window = words[win_start:win_end]

        parts = []
        for j, w in enumerate(window):
            global_j = win_start + j
            if global_j == i:
                # Active word: show for the full display window (t_start → t_end)
                # so the highlight doesn't vanish during inter-word pauses.
                dur_cs = max(1, int(round((t_end - t_start) * 100)))
            else:
                dur_cs = max(1, int(round((w["end"] - w["start"]) * 100)))
            text = w["word"].upper()

            if global_j == i:
                # Active word: coloured background box, white bold text
                colour = word_colours[global_j]
                # BorderStyle 3 = opaque box. Use a single clean override block.
                styled = (
                    f"{{\\kf{dur_cs}"
                    f"\\bord0\\shad0"
                    f"\\BorderStyle3"
                    f"\\4c{colour}"
                    f"\\1c&H00FFFFFF&"
                    f"\\b1}}"
                    f"{text}"
                    f"{{\\r}}"   # reset to Default style for next words
                )
            else:
                styled = f"{{\\k{dur_cs}}}{text}"

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
    fade_dur: float = 0.3,
) -> None:
    n_pages      = len(page_images)
    # Drive page duration by audio — never pad with silence
    per_page_dur = content_duration_s / max(n_pages, 1)
    # Clamp: min 1s per panel, max 4s (fast scroll if many panels)
    per_page_dur = max(1.0, min(per_page_dur, 4.0))
    trans_dur    = min(0.3, per_page_dur * 0.15)

    all_images = [cover_image] + list(page_images)
    durations  = [cover_duration_s] + [per_page_dur] * n_pages
    total_segs = len(all_images)
    total_dur  = cover_duration_s + per_page_dur * n_pages
    fade_out_st = max(0.0, total_dur - fade_dur)

    inputs = []
    for img, dur in zip(all_images, durations):
        inputs += ["-loop", "1", "-t", f"{dur + trans_dur:.3f}", "-i", str(img)]
    inputs += ["-i", str(audio_path)]
    audio_idx = total_segs

    filter_parts = []
    seg_labels   = []
    for idx in range(total_segs):
        lbl = f"s{idx}"
        filter_parts.append(
            f"[{idx}:v]scale={SHORT_WIDTH}:{SHORT_HEIGHT}:"
            f"force_original_aspect_ratio=decrease,"
            f"pad={SHORT_WIDTH}:{SHORT_HEIGHT}:(ow-iw)/2:(oh-ih)/2:0xFFFFFF,"
            f"setsar=1,fps={FPS}[{lbl}]"
        )
        seg_labels.append(lbl)

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
        f"fade=t=out:st={fade_out_st:.3f}:d={fade_dur}[fv]"
    )
    filter_parts.append(
        f"[{audio_idx}:a]afade=t=in:st=0:d={fade_dur},"
        f"afade=t=out:st={fade_out_st:.3f}:d={fade_dur}[fa]"
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