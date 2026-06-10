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
        try:
            base  = Image.open(cover_image_path).convert("RGB")
            ratio = max(w / base.width, h / base.height)
            base  = base.resize((int(base.width * ratio), int(base.height * ratio)), Image.LANCZOS)
            bx    = (base.width  - w) // 2
            by    = (base.height - h) // 2
            base  = base.crop((bx, by, bx + w, by + h))
            print(f"  [Cover] ✅ Using provided cover image ({base.width}×{base.height})")
        except Exception as e:
            print(f"  [Cover] ⚠️  Cover image failed to load ({e}) — using gradient fallback")
            cover_image_path = None
            base = Image.new("RGB", (w, h))
    else:
        if cover_image_path:
            print(f"  [Cover] ⚠️  Cover image not found at: {cover_image_path} — using gradient fallback")
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
    Karaoke word-highlight subtitles — TikTok/Reels style:
    - 4 words visible at once on one line
    - Active word: solid purple box behind it, white bold text
    - Inactive words: white bold text, black outline
    - Positioned upper-third of frame
    - { and } in text are escaped so libass doesn't treat them as tags
    
    Implementation: uses TWO styles defined in header.
      "Base" — inactive style (BorderStyle=1, outline only)
      "HL"   — active style   (BorderStyle=3, opaque box)
    Each dialogue line = one word's worth of display time.
    Active word switches to HL style inline via {\rHL}, rest stay Base.
    """
    font_path = find_font()
    font_name = "Noto Sans Bold"
    if font_path:
        stem = Path(font_path).stem
        font_name = stem.replace("-", " ").replace("_", " ")

    sub_margin_v = max(80, int(height * 0.10))

    # Inactive style: white text, thick black outline, no box
    # Active  style: white text, purple opaque box (BorderStyle=3)
    # Alignment=8 = top-center
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {width}\n"
        f"PlayResY: {height}\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        # Base: white, black outline, BorderStyle=1
        f"Style: Base,{font_name},{font_size},"
        "&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,"
        f"-1,0,0,0,100,100,2,0,1,4,0,8,40,40,{sub_margin_v},1\n"
        # HL: white bold, purple box, BorderStyle=3
        f"Style: HL,{font_name},{font_size},"
        "&H00FFFFFF,&H00FFFFFF,&H00C832C8,&H00C832C8,"
        f"-1,0,0,0,100,100,2,0,3,4,0,8,40,40,{sub_margin_v},1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    def _safe(text: str) -> str:
        """Escape { and } so libass doesn't eat them as override tags."""
        return text.replace("{", "\\{").replace("}", "\\}")

    events = []
    WINDOW = 4

    for i, word in enumerate(words):
        t_start = word["start"]
        t_end   = words[i + 1]["start"] if i + 1 < len(words) else total_duration
        t_end   = max(t_end, word["end"] + 0.05)

        win_s = max(0, i - 1)
        win_e = min(len(words), win_s + WINDOW)
        win_s = max(0, win_e - WINDOW)
        window = words[win_s:win_e]

        parts = []
        for j, w in enumerate(window):
            gidx = win_s + j
            text = _safe(w["word"].upper())
            if gidx == i:
                # Switch to HL style for this word, then reset back to Base
                parts.append(f"{{\\rHL\\b1}}{text}{{\\rBase}}")
            else:
                parts.append(f"{{\\rBase}}{text}")

        line = " ".join(parts)
        events.append(
            f"Dialogue: 0,{_ass_time(t_start)},{_ass_time(t_end)},"
            f"Base,,0,0,0,,{line}\n"
        )

    output_path.write_text(header + "".join(events), encoding="utf-8")



# ── drawtext subtitle burn ────────────────────────────────────────────────────

FONT_PATH   = "/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf"
FONT_SIZE   = 68          # px
WORD_PAD_X  = 14          # px padding each side of active word box
WORD_PAD_Y  = 10          # px padding top/bottom of active word box
LINE_Y_FRAC = 0.12        # subtitle Y position as fraction from top of frame
ACTIVE_COL  = "0x9B30FF"  # purple box
INACTIVE_COL= "white"
SHADOW_COL  = "black"
WINDOW_SIZE = 4           # words visible at once


def _measure_word(text: str, font_size: int = FONT_SIZE) -> int:
    """Return pixel width of text rendered at font_size using Poppins Bold."""
    try:
        from PIL import ImageFont
        fnt = ImageFont.truetype(FONT_PATH, font_size)
        bb  = fnt.getbbox(text)
        return bb[2] - bb[0]
    except Exception:
        # Fallback: ~0.55 * font_size per character
        return int(len(text) * font_size * 0.55)


def _esc(text: str) -> str:
    """Escape characters that break FFmpeg drawtext."""
    return (text
        .replace("\\", "\\\\")
        .replace("'",  "\\'")
        .replace(":",  "\\:")
        .replace("%",  "\\%")
        .replace("{",  "")
        .replace("}",  ""))


def build_drawtext_filters(
    words: List[dict],
    total_duration: float,
    width:  int = SHORT_WIDTH,
    height: int = SHORT_HEIGHT,
) -> str:
    """
    Build an FFmpeg -vf filter string that burns word-highlight subtitles
    directly onto the video using drawbox + drawtext.

    For each word i:
    - Show a sliding window of WINDOW_SIZE words
    - Active word (i): purple filled box behind it + white bold text
    - Inactive words: white text with black shadow (no box)
    - Each word/box has enable='between(t,start,end)' for frame-accurate timing
    - All positions calculated from real pixel widths so text never overflows

    Returns a comma-separated filter string ready for -vf.
    """
    if not words:
        return "null"

    filters = []

    for i, word in enumerate(words):
        t_start = word["start"]
        t_end   = words[i + 1]["start"] if i + 1 < len(words) else total_duration
        t_end   = max(t_end, word["end"] + 0.04)
        enable  = f"between(t\\,{t_start:.3f}\\,{t_end:.3f})"

        # Sliding window
        win_s = max(0, i - 1)
        win_e = min(len(words), win_s + WINDOW_SIZE)
        win_s = max(0, win_e - WINDOW_SIZE)
        window = words[win_s:win_e]

        # Measure total line width for centering
        word_widths = [_measure_word(w["word"].upper()) for w in window]
        spacing     = int(FONT_SIZE * 0.25)
        total_w     = sum(word_widths) + spacing * (len(window) - 1)
        line_x      = max(20, (width - total_w) // 2)
        line_y      = int(height * LINE_Y_FRAC)

        x = line_x
        for j, w in enumerate(window):
            gidx = win_s + j
            text  = _esc(w["word"].upper())
            ww    = word_widths[j]

            if gidx == i:
                # Active: purple box + white text
                bx = x - WORD_PAD_X
                by = line_y - WORD_PAD_Y
                bw = ww + WORD_PAD_X * 2
                bh = FONT_SIZE + WORD_PAD_Y * 2
                filters.append(
                    f"drawbox=x={bx}:y={by}:w={bw}:h={bh}"
                    f":color={ACTIVE_COL}@1.0:t=fill:enable=\'{enable}\'"
                )
                filters.append(
                    f"drawtext=fontfile={FONT_PATH}:text=\'{text}\':"
                    f"fontsize={FONT_SIZE}:fontcolor=white:"
                    f"x={x}:y={line_y}:enable=\'{enable}\'"
                )
            else:
                # Inactive: white text, black shadow
                filters.append(
                    f"drawtext=fontfile={FONT_PATH}:text=\'{text}\':"
                    f"fontsize={FONT_SIZE}:fontcolor=white:"
                    f"shadowcolor=black:shadowx=3:shadowy=3:"
                    f"x={x}:y={line_y}:enable=\'{enable}\'"
                )

            x += ww + spacing

    return ",".join(filters)

def build_video_ffmpeg(
    page_images: List[Path],
    cover_image: Path,
    audio_path: Path,
    ass_path: Path,           # kept for build_shorts compat — now ignored (drawtext used)
    output_path: Path,
    animations: List[str],    # kept for compat — transitions removed (concat used)
    cover_duration_s: float = 3.0,
    content_duration_s: float = 60.0,
    page_durations: Optional[List[float]] = None,
    fade_dur: float = 0.3,
    width: int = SHORT_WIDTH,
    height: int = SHORT_HEIGHT,
) -> None:
    """
    Reliable panel-slideshow video builder using concat demuxer.

    Why not xfade filter_complex?
      With 20-30 panels the filter_complex string becomes enormous and FFmpeg
      on GitHub Actions silently fails, producing a vertical image stack.

    Approach:
      1. Encode each panel (cover + content panels) as a tiny individual clip
      2. Write a concat list file
      3. Join all clips + audio in one final ffmpeg pass
      4. No subtitles (ASS rendering was broken on CI — removed per user request)
    """
    n_pages = len(page_images)

    if page_durations and len(page_durations) == n_pages:
        panel_durs = page_durations
    else:
        per = max(1.0, min(content_duration_s / max(n_pages, 1), 4.0))
        panel_durs = [per] * n_pages

    total_dur = cover_duration_s + content_duration_s

    all_images = [cover_image] + list(page_images)
    durations  = [cover_duration_s] + panel_durs

    clips_dir = output_path.parent / f"_clips_{output_path.stem}"
    clips_dir.mkdir(parents=True, exist_ok=True)

    clip_paths = []
    for i, (img, dur) in enumerate(zip(all_images, durations)):
        clip_path = clips_dir / f"clip_{i:03d}.mp4"
        cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-t", f"{dur:.3f}", "-i", str(img),
            "-vf", (
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=white,"
                f"setsar=1,fps={FPS}"
            ),
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
            "-pix_fmt", "yuv420p",
            "-an",   # no audio in individual clips
            str(clip_path),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"Clip {i} encode failed:\n{r.stderr[-2000:]}")
        clip_paths.append(clip_path)

    # Write concat list
    concat_file = clips_dir / "concat.txt"
    concat_file.write_text(
        "".join(f"file '{p.resolve()}'\n" for p in clip_paths),
        encoding="utf-8"
    )

    # Final pass: concat video clips + add audio
    audio_fade_st = max(0.0, total_dur - fade_dur)
    # Build drawtext subtitle filter from word timings in ass_path's sibling
    # We reuse the word timings written by build_shorts via a sidecar approach:
    # build_shorts passes timing_words → stored as _subtitle_words attr on ass_path
    subtitle_words = getattr(ass_path, "_words", [])
    subtitle_vf = build_drawtext_filters(
        subtitle_words, total_dur, width, height
    ) if subtitle_words else "null"

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", str(concat_file),
        "-i", str(audio_path),
        "-filter_complex",
        (
            f"[0:v]fade=t=in:st=0:d={fade_dur},"
            f"fade=t=out:st={max(0, total_dur - fade_dur):.3f}:d={fade_dur},"
            f"{subtitle_vf}[fv];"
            f"[1:a]afade=t=in:st=0:d={fade_dur},"
            f"afade=t=out:st={audio_fade_st:.3f}:d={fade_dur}[fa]"
        ),
        "-map", "[fv]", "-map", "[fa]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "22",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-t", f"{total_dur:.3f}",
        "-movflags", "+faststart",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Final concat failed:\n{result.stderr[-3000:]}")

    # Cleanup individual clips
    import shutil
    shutil.rmtree(clips_dir, ignore_errors=True)
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
        try:
            base  = Image.open(cover_image_path).convert("RGB")
            ratio = max(w / base.width, h / base.height)
            base  = base.resize((int(base.width * ratio), int(base.height * ratio)), Image.LANCZOS)
            bx    = (base.width  - w) // 2
            by    = (base.height - h) // 2
            base  = base.crop((bx, by, bx + w, by + h))
            print(f"  [Cover] ✅ Using provided cover image ({base.width}×{base.height})")
        except Exception as e:
            print(f"  [Cover] ⚠️  Cover image failed to load ({e}) — using gradient fallback")
            cover_image_path = None
            base = Image.new("RGB", (w, h))
    else:
        if cover_image_path:
            print(f"  [Cover] ⚠️  Cover image not found at: {cover_image_path} — using gradient fallback")
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
    Karaoke word-highlight subtitles — TikTok/Reels style:
    - 4 words visible at once on one line
    - Active word: solid purple box behind it, white bold text
    - Inactive words: white bold text, black outline
    - Positioned upper-third of frame
    - { and } in text are escaped so libass doesn't treat them as tags
    
    Implementation: uses TWO styles defined in header.
      "Base" — inactive style (BorderStyle=1, outline only)
      "HL"   — active style   (BorderStyle=3, opaque box)
    Each dialogue line = one word's worth of display time.
    Active word switches to HL style inline via {\rHL}, rest stay Base.
    """
    font_path = find_font()
    font_name = "Noto Sans Bold"
    if font_path:
        stem = Path(font_path).stem
        font_name = stem.replace("-", " ").replace("_", " ")

    sub_margin_v = max(80, int(height * 0.10))

    # Inactive style: white text, thick black outline, no box
    # Active  style: white text, purple opaque box (BorderStyle=3)
    # Alignment=8 = top-center
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {width}\n"
        f"PlayResY: {height}\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        # Base: white, black outline, BorderStyle=1
        f"Style: Base,{font_name},{font_size},"
        "&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,"
        f"-1,0,0,0,100,100,2,0,1,4,0,8,40,40,{sub_margin_v},1\n"
        # HL: white bold, purple box, BorderStyle=3
        f"Style: HL,{font_name},{font_size},"
        "&H00FFFFFF,&H00FFFFFF,&H00C832C8,&H00C832C8,"
        f"-1,0,0,0,100,100,2,0,3,4,0,8,40,40,{sub_margin_v},1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    def _safe(text: str) -> str:
        """Escape { and } so libass doesn't eat them as override tags."""
        return text.replace("{", "\\{").replace("}", "\\}")

    events = []
    WINDOW = 4

    for i, word in enumerate(words):
        t_start = word["start"]
        t_end   = words[i + 1]["start"] if i + 1 < len(words) else total_duration
        t_end   = max(t_end, word["end"] + 0.05)

        win_s = max(0, i - 1)
        win_e = min(len(words), win_s + WINDOW)
        win_s = max(0, win_e - WINDOW)
        window = words[win_s:win_e]

        parts = []
        for j, w in enumerate(window):
            gidx = win_s + j
            text = _safe(w["word"].upper())
            if gidx == i:
                # Switch to HL style for this word, then reset back to Base
                parts.append(f"{{\\rHL\\b1}}{text}{{\\rBase}}")
            else:
                parts.append(f"{{\\rBase}}{text}")

        line = " ".join(parts)
        events.append(
            f"Dialogue: 0,{_ass_time(t_start)},{_ass_time(t_end)},"
            f"Base,,0,0,0,,{line}\n"
        )

    output_path.write_text(header + "".join(events), encoding="utf-8")



# ── drawtext subtitle burn ────────────────────────────────────────────────────

FONT_PATH   = "/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf"
FONT_SIZE   = 68          # px
WORD_PAD_X  = 14          # px padding each side of active word box
WORD_PAD_Y  = 10          # px padding top/bottom of active word box
LINE_Y_FRAC = 0.12        # subtitle Y position as fraction from top of frame
ACTIVE_COL  = "0x9B30FF"  # purple box
INACTIVE_COL= "white"
SHADOW_COL  = "black"
WINDOW_SIZE = 4           # words visible at once


def _measure_word(text: str, font_size: int = FONT_SIZE) -> int:
    """Return pixel width of text rendered at font_size using Poppins Bold."""
    try:
        from PIL import ImageFont
        fnt = ImageFont.truetype(FONT_PATH, font_size)
        bb  = fnt.getbbox(text)
        return bb[2] - bb[0]
    except Exception:
        # Fallback: ~0.55 * font_size per character
        return int(len(text) * font_size * 0.55)


def _esc(text: str) -> str:
    """Escape characters that break FFmpeg drawtext."""
    return (text
        .replace("\\", "\\\\")
        .replace("'",  "\\'")
        .replace(":",  "\\:")
        .replace("%",  "\\%")
        .replace("{",  "")
        .replace("}",  ""))


def build_drawtext_filters(
    words: List[dict],
    total_duration: float,
    width:  int = SHORT_WIDTH,
    height: int = SHORT_HEIGHT,
) -> str:
    """
    Build an FFmpeg -vf filter string that burns word-highlight subtitles
    directly onto the video using drawbox + drawtext.

    For each word i:
    - Show a sliding window of WINDOW_SIZE words
    - Active word (i): purple filled box behind it + white bold text
    - Inactive words: white text with black shadow (no box)
    - Each word/box has enable='between(t,start,end)' for frame-accurate timing
    - All positions calculated from real pixel widths so text never overflows

    Returns a comma-separated filter string ready for -vf.
    """
    if not words:
        return "null"

    filters = []

    for i, word in enumerate(words):
        t_start = word["start"]
        t_end   = words[i + 1]["start"] if i + 1 < len(words) else total_duration
        t_end   = max(t_end, word["end"] + 0.04)
        enable  = f"between(t\\,{t_start:.3f}\\,{t_end:.3f})"

        # Sliding window
        win_s = max(0, i - 1)
        win_e = min(len(words), win_s + WINDOW_SIZE)
        win_s = max(0, win_e - WINDOW_SIZE)
        window = words[win_s:win_e]

        # Measure total line width for centering
        word_widths = [_measure_word(w["word"].upper()) for w in window]
        spacing     = int(FONT_SIZE * 0.25)
        total_w     = sum(word_widths) + spacing * (len(window) - 1)
        line_x      = max(20, (width - total_w) // 2)
        line_y      = int(height * LINE_Y_FRAC)

        x = line_x
        for j, w in enumerate(window):
            gidx = win_s + j
            text  = _esc(w["word"].upper())
            ww    = word_widths[j]

            if gidx == i:
                # Active: purple box + white text
                bx = x - WORD_PAD_X
                by = line_y - WORD_PAD_Y
                bw = ww + WORD_PAD_X * 2
                bh = FONT_SIZE + WORD_PAD_Y * 2
                filters.append(
                    f"drawbox=x={bx}:y={by}:w={bw}:h={bh}"
                    f":color={ACTIVE_COL}@1.0:t=fill:enable=\'{enable}\'"
                )
                filters.append(
                    f"drawtext=fontfile={FONT_PATH}:text=\'{text}\':"
                    f"fontsize={FONT_SIZE}:fontcolor=white:"
                    f"x={x}:y={line_y}:enable=\'{enable}\'"
                )
            else:
                # Inactive: white text, black shadow
                filters.append(
                    f"drawtext=fontfile={FONT_PATH}:text=\'{text}\':"
                    f"fontsize={FONT_SIZE}:fontcolor=white:"
                    f"shadowcolor=black:shadowx=3:shadowy=3:"
                    f"x={x}:y={line_y}:enable=\'{enable}\'"
                )

            x += ww + spacing

    return ",".join(filters)

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