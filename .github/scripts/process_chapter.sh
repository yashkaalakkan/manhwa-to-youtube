#!/usr/bin/env bash
# process_chapter.sh
# Handles ONE chapter: download → OCR → script (1 short) → TTS
# Does NOT build any video — that happens after all chapters are done.
# Usage: process_chapter.sh <manhwa> <episode> <chapter_num> <drive_link> <cover_link> <language> <skip_pages>
set -euo pipefail

MANHWA="$1"
EPISODE="$2"
CHAPTER="$3"
DRIVE_LINK="$4"
COVER_LINK="${5:-}"
LANGUAGE="${6:-en}"
SKIP_PAGES="${7:-auto}"

PAGES_DIR="./episode_pages_ch${CHAPTER}"
PIPELINE_DIR="./pipeline/ch${CHAPTER}"
AUDIO_DIR="${PIPELINE_DIR}/audio"

mkdir -p "$PAGES_DIR" "$PIPELINE_DIR"

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
  --dpi 150 \
  --manifest-out "${PIPELINE_DIR}/manifest.json"

# ── 2. OCR ───────────────────────────────────────────────────────────────
python src/ocr/extract_text.py \
  --pages-dir "$PAGES_DIR" \
  --output "${PIPELINE_DIR}/raw_text.json" \
  --language "$LANGUAGE" \
  --skip-pages "$SKIP_PAGES" \
  --manifest "${PIPELINE_DIR}/manifest.json"

# ── 3. Generate narration — 1 short script per chapter ──────────────────
python src/script/generate_script.py \
  --raw-text "${PIPELINE_DIR}/raw_text.json" \
  --manhwa "$MANHWA" \
  --episode "$EPISODE" \
  --chapter "$CHAPTER" \
  --language "$LANGUAGE" \
  --output "${PIPELINE_DIR}/scripts.json" \
  --manifest "${PIPELINE_DIR}/manifest.json"

# ── 4. TTS ───────────────────────────────────────────────────────────────
python src/tts/generate_audio.py \
  --scripts "${PIPELINE_DIR}/scripts.json" \
  --output-dir "$AUDIO_DIR" \
  --language "$LANGUAGE"

echo "✅ Chapter ${CHAPTER} prepared (pages + OCR + script + audio)"