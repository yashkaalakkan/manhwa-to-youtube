"""
generate_audio.py
1. Generates TTS audio using Kokoro
2. Runs WhisperX forced alignment for accurate word-level timestamps
   Falls back to proportional estimation if WhisperX fails.

Key sync fix: WhisperX is run with condition_on_previous_text=False to prevent
   timestamp drift across the audio file.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

VOICE_MAP = {
    "en": "am_adam",
    "es": "ef_dora",
    "fr": "ff_siwis",
    "de": "df_eva",
    "ja": "jf_alpha",
    "ko": "kf_amu",
    "pt": "bf_emma",
    "hi": "hf_alpha",
}
SAMPLE_RATE = 24000


def load_kokoro():
    try:
        from kokoro_onnx import Kokoro
        return Kokoro("kokoro-v1.0.onnx", "voices-v1.0.bin")
    except ImportError:
        raise ImportError("kokoro-onnx not installed")


def generate_tts(kokoro, narration: str, voice: str, output_path: Path) -> float:
    """Generate audio file, return duration in seconds."""
    if not narration.strip():
        silence = np.zeros(SAMPLE_RATE, dtype=np.float32)
        sf.write(str(output_path), silence, SAMPLE_RATE)
        return 1.0

    samples, sample_rate = kokoro.create(narration, voice=voice, speed=1.0, lang="en-us")
    max_val = np.max(np.abs(samples))
    if max_val > 0:
        samples = samples / max_val * 0.9
    sf.write(str(output_path), samples, sample_rate)
    return len(samples) / sample_rate


def align_with_whisperx(audio_path: Path, narration: str, language: str) -> list[dict] | None:
    """
    Run WhisperX forced alignment for accurate word timestamps.
    - Uses 'small' model for better accuracy than 'tiny'
    - condition_on_previous_text=False prevents cumulative drift
    Returns list of {word, start, end} or None if alignment fails.
    """
    try:
        import whisperx
        import torch

        device  = "cpu"
        compute = "int8"

        print(f"    [WhisperX] Loading model (small, no drift mode)...")
        model = whisperx.load_model(
            "small", device, compute_type=compute, language=language[:2]
        )

        audio  = whisperx.load_audio(str(audio_path))
        result = model.transcribe(
            audio,
            batch_size=8,
            language=language[:2],
            # Disable conditioning on previous text — this is the main cause
            # of subtitle drift when WhisperX tries to "continue" context.
            condition_on_previous_text=False,
        )

        # Forced alignment
        model_a, metadata = whisperx.load_align_model(
            language_code=language[:2], device=device
        )
        aligned = whisperx.align(
            result["segments"], model_a, metadata, audio, device,
            return_char_alignments=False,
        )

        # Extract word-level timestamps
        words = []
        for seg in aligned.get("word_segments", []):
            w = seg.get("word", "").strip()
            if w:
                clean = re.sub(r"[^\w']", "", w)
                if clean:
                    words.append({
                        "word":  clean,
                        "start": round(seg.get("start", 0), 3),
                        "end":   round(seg.get("end",   0), 3),
                    })

        if words:
            print(f"    [WhisperX] ✅ Aligned {len(words)} words (drift-free)")
            return words

    except ImportError:
        print(f"    [WhisperX] Not installed — using proportional estimation")
    except Exception as e:
        print(f"    [WhisperX] Failed ({e}) — using proportional estimation")

    return None


def proportional_timestamps(narration: str, duration: float) -> list[dict]:
    """
    Fallback: distribute duration proportionally by word length.
    Pause tokens ('...', '—', '–') get extra time so captions
    don't race through dramatic beats.
    """
    words = re.findall(r"\S+", narration)

    def _weight(w: str) -> float:
        letters = len(re.sub(r"[^a-zA-Z]", "", w))
        pauses  = w.count(".") + w.count("…") + w.count("—") + w.count("–")
        return max(letters + pauses * 1.5, 1.0)

    weights = [_weight(w) for w in words]
    total   = sum(weights)
    result  = []
    cursor  = 0.0
    for word, weight in zip(words, weights):
        dur = (weight / total) * duration
        clean = re.sub(r"[^\w']", "", word)
        if clean:
            result.append({
                "word":  clean,
                "start": round(cursor, 3),
                "end":   round(cursor + dur, 3),
            })
        cursor += dur
    return result


def process_narration(kokoro, narration: str, voice: str, audio_path: Path, language: str) -> dict:
    """Generate audio + get accurate word timestamps."""
    if not narration.strip():
        return {"duration": 1.0, "words": [], "audio_path": str(audio_path)}

    duration = generate_tts(kokoro, narration, voice, audio_path)
    print(f"    [TTS] {duration:.1f}s audio generated")

    words = align_with_whisperx(audio_path, narration, language)
    if words is None:
        words = proportional_timestamps(narration, duration)
        print(f"    [TTS] Proportional timestamps: {len(words)} words")

    return {
        "duration":   round(duration, 3),
        "words":      words,
        "audio_path": str(audio_path),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scripts",    required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--language",   default="en")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    with open(args.scripts, encoding="utf-8") as f:
        scripts = json.load(f)

    voice = VOICE_MAP.get(args.language, "af_heart")
    print(f"[TTS] Voice: {voice} | Language: {args.language}")
    print(f"[TTS] Loading Kokoro...")
    kokoro = load_kokoro()

    # Install WhisperX if needed
    try:
        import whisperx
        print("[TTS] WhisperX available — drift-free forced alignment ✅")
    except ImportError:
        print("[TTS] WhisperX not found — installing...")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "whisperx", "-q"],
            capture_output=True
        )
        try:
            import whisperx
            print("[TTS] WhisperX installed ✅")
        except ImportError:
            print("[TTS] WhisperX install failed — will use proportional estimation")

    timing_data = {"shorts": [], "full_episode": None}

    for short in scripts["shorts"]:
        part       = short["part"]
        audio_path = output_dir / f"short_part_{part:02d}.wav"
        print(f"\n[TTS] Short {part}/{len(scripts['shorts'])}")
        timing = process_narration(kokoro, short["narration"], voice, audio_path, args.language)
        timing_data["shorts"].append({"part": part, **timing})

    print(f"\n[TTS] Full episode...")
    full_path   = output_dir / "full_episode.wav"
    full_timing = process_narration(
        kokoro, scripts["full_episode"]["narration"], voice, full_path, args.language
    )
    timing_data["full_episode"] = full_timing

    timing_output = Path(args.scripts).parent / "audio_timing.json"
    with open(timing_output, "w", encoding="utf-8") as f:
        json.dump(timing_data, f, ensure_ascii=False, indent=2)

    print(f"\n[TTS] ✅ All audio generated → {output_dir}")
    print(f"[TTS]    Timing → {timing_output}")


if __name__ == "__main__":
    main()