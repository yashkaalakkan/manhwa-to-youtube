#!/usr/bin/env bash
# process_chapter.sh
# Handles ONE chapter: download → OCR → script (1 short) → TTS
# Does NOT build any video — that happens after all chapters are done.
# Usage: process_chapter.sh <manhwa> <episode> <chapter_num> <slot> <drive_link> <cover_link> <language> <skip_pages>
# chapter_num = actual chapter number (e.g. 16) used in metadata/titles
# slot        = position in this run (1–5) used for directory naming (always pipeline/ch1–ch5)
set -euo pipefail

MANHWA="$1"
EPISODE="$2"
CHAPTER="$3"          # actual chapter number — passed to scripts for titles/metadata
SLOT="$4"             # slot 1-5 — used for pipeline/pages directory names
DRIVE_LINK="${5:-}"
COVER_LINK="${6:-}"
LANGUAGE="${7:-en}"
SKIP_PAGES="${8:-auto}"

PAGES_DIR="./episode_pages_ch${SLOT}"
PIPELINE_DIR="./pipeline/ch${SLOT}"
AUDIO_DIR="${PIPELINE_DIR}/audio"

mkdir -p "$PAGES_DIR" "$PIPELINE_DIR"

echo ""
echo "════════════════════════════════════════════════"
echo " Chapter ${CHAPTER} (slot ${SLOT}) | Episode ${EPISODE} | ${MANHWA}"
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