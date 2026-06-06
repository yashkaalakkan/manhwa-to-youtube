#!/usr/bin/env bash
# process_chapter.sh
# Usage: process_chapter.sh <manhwa> <episode> <chapter_num> <drive_link> <cover_link> <language> <skip_pages>
set -euo pipefail

MANHWA="$1"
EPISODE="$2"
CHAPTER="$3"
DRIVE_LINK="$4"
COVER_LINK="${5:-}"
LANGUAGE="${6:-en}"
SKIP_PAGES="${7:-1}"

# Each chapter gets its own isolated pipeline dir and output dirs
PAGES_DIR="./episode_pages_ch${CHAPTER}"
PIPELINE_DIR="./pipeline/ch${CHAPTER}"
AUDIO_DIR="${PIPELINE_DIR}/audio"
SHORTS_DIR="./output/shorts"
FULL_OUT="./output/full_episode_ch${CHAPTER}.mp4"

mkdir -p "$PAGES_DIR" "$PIPELINE_DIR" "$SHORTS_DIR"

echo ""
echo "════════════════════════════════════════════════"
echo " Chapter ${CHAPTER} | Episode ${EPISODE} | ${MANHWA}"
echo "════════════════════════════════════════════════"

# ── 1. Download + convert PDF ────────────────────────────────────────────
python src/utils/drive_downloader.py \
  --file-link "$DRIVE_LINK" \
  --output-dir "$PAGES_DIR" \
  --cover-link "$COVER_LINK" \
  --skip-pages "$SKIP_PAGES" \
  --dpi 150

# ── 2. OCR ───────────────────────────────────────────────────────────────
python src/ocr/extract_text.py \
  --pages-dir "$PAGES_DIR" \
  --output "${PIPELINE_DIR}/raw_text.json" \
  --language "$LANGUAGE" \
  --skip-pages "$SKIP_PAGES"

# ── 3. Generate narration (third-person) + metadata ─────────────────────
python src/script/generate_script.py \
  --raw-text "${PIPELINE_DIR}/raw_text.json" \
  --manhwa "$MANHWA" \
  --episode "$EPISODE" \
  --language "$LANGUAGE" \
  --output "${PIPELINE_DIR}/scripts.json"

# ── 4. TTS + WhisperX alignment (drift-free) ────────────────────────────
python src/tts/generate_audio.py \
  --scripts "${PIPELINE_DIR}/scripts.json" \
  --output-dir "$AUDIO_DIR" \
  --language "$LANGUAGE"

# ── 5. Build shorts (portrait 1080×1920, 1 chapter = 1 short per chunk) ─
python src/video/build_shorts.py \
  --pages-dir "$PAGES_DIR" \
  --audio-dir "$AUDIO_DIR" \
  --scripts "${PIPELINE_DIR}/scripts.json" \
  --output-dir "$SHORTS_DIR" \
  --manhwa "$MANHWA" \
  --episode "${EPISODE}_ch${CHAPTER}"

# ── 6. Build full episode (landscape 1920×1080) + 5-short compilations ──
python src/video/build_full_episode.py \
  --pages-dir "$PAGES_DIR" \
  --audio-dir "$AUDIO_DIR" \
  --scripts "${PIPELINE_DIR}/scripts.json" \
  --output "$FULL_OUT" \
  --manhwa "$MANHWA" \
  --episode "${EPISODE}_ch${CHAPTER}" \
  --shorts-dir "$SHORTS_DIR"

echo "✅ Chapter ${CHAPTER} done"