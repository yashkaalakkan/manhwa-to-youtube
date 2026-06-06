# 🎬 Manhwa → YouTube Automation

Fully automated pipeline: **manga/manhwa pages → OCR → AI narration → TTS → animated video → YouTube Shorts + Full Episode**

Triggered manually via GitHub Actions `workflow_dispatch`.

---

## 📁 Project Structure

```
manhwa-to-youtube/
├── .github/workflows/
│   └── post_episode.yml       # Main GitHub Actions workflow
├── src/
│   ├── utils/drive_downloader.py   # Download pages from Google Drive
│   ├── ocr/extract_text.py         # Tesseract OCR on manga panels
│   ├── script/generate_script.py   # Groq LLM → narration + metadata
│   ├── tts/generate_audio.py       # Kokoro TTS + word timestamps
│   ├── video/
│   │   ├── video_utils.py          # Animations, subtitles, covers
│   │   ├── build_shorts.py         # Build short <60s videos
│   │   └── build_full_episode.py   # Build full episode video
│   └── upload/
│       ├── upload_shorts.py        # Staggered YouTube Shorts upload
│       └── upload_full_episode.py  # Full episode upload
├── requirements.txt
└── README.md
```

---

## ⚙️ Setup

### 1. Fork / clone this repo

```bash
git clone https://github.com/YOUR_USERNAME/manhwa-to-youtube
cd manhwa-to-youtube
```

### 2. Set up GitHub Secrets

Go to **Settings → Secrets and variables → Actions → New repository secret**

| Secret Name | How to get it |
|---|---|
| `GDRIVE_CREDENTIALS` | JSON from Google Cloud service account (see below) |
| `GROQ_API_KEY` | From [console.groq.com](https://console.groq.com) |
| `YOUTUBE_CLIENT_ID` | From Google Cloud OAuth2 credentials |
| `YOUTUBE_CLIENT_SECRET` | From Google Cloud OAuth2 credentials |
| `YOUTUBE_REFRESH_TOKEN` | Run the OAuth helper below |

---

### 3. Google Drive Setup

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create a project → Enable **Google Drive API**
3. Create a **Service Account** → Download JSON credentials
4. Copy the JSON contents → paste as `GDRIVE_CREDENTIALS` secret
5. **Share your Drive episode folder** with the service account email

---

### 4. YouTube API + OAuth Setup

1. In Google Cloud Console → Enable **YouTube Data API v3**
2. Create **OAuth 2.0 credentials** (Desktop app type)
3. Run the helper script locally to get your refresh token:

```bash
pip install google-auth-oauthlib
python scripts/get_youtube_token.py \
  --client-id YOUR_CLIENT_ID \
  --client-secret YOUR_CLIENT_SECRET
```

This opens a browser → authorize → prints your `YOUTUBE_REFRESH_TOKEN`

---

### 5. Kokoro TTS Model Files

Kokoro requires model files downloaded separately:

```bash
# Download from GitHub releases
wget https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files/kokoro-v0_19.onnx
wget https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files/voices-v1.0.bin
```

Add these to your repo root (they're used by the action) or host them on your Drive and download in the workflow.

> **Note:** Files are ~300MB total. Consider storing on Drive and downloading at runtime.

---

## 🚀 Running the Workflow

1. Go to your repo on GitHub
2. Click **Actions** → **🎬 Post Manhwa Episode to YouTube**
3. Click **Run workflow** and fill in:

| Input | Example | Description |
|---|---|---|
| `manhwa_name` | `Solo Leveling` | Title of the manhwa |
| `episode_number` | `1` | Episode number |
| `drive_folder_id` | `1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs` | Google Drive folder ID |
| `cover_image_id` | `1abc...xyz` | Optional Drive file ID for cover |
| `language` | `en` | Language for TTS + subtitles |
| `pages_per_short` | `auto` | Pages per short (or number) |
| `dry_run` | `false` | True = generate but don't upload |

---

## 🎬 What Gets Created

### Per Episode:
- **N Shorts** (each < 60s):
  - `Episode X Part 1` — `Episode X Part N`
  - Custom cover with episode + part burned in
  - Animated page transitions (zoom, pan, slide, fade)
  - Word-highlighted glowing subtitles synced to TTS
  - Posted with **18-minute gaps** between each

- **1 Full Episode Video** (long-form):
  - Same pages, fresh continuous narration
  - Cover with episode number only
  - Posted after all shorts are done
  - Description includes links to all shorts

---

## 🎨 Video Features

- **Animations:** zoom-in, zoom-out, pan left/right, slide up, crossfade
- **Subtitles:** Bold white text, active word highlighted in gold with glow
- **Cover:** Dynamic cover with manhwa title + episode info burned in
- **Format:** 1080×1920 (vertical/Shorts format), 30fps, H.264

---

## ⏱️ Time Budget (GitHub 6hr limit)

| Step | Estimated Time |
|---|---|
| Download pages | 2–5 min |
| OCR extraction | 3–8 min |
| Script generation | 2–4 min |
| TTS audio | 5–15 min |
| Build shorts (per short) | 8–20 min |
| Build full episode | 15–40 min |
| Upload shorts (N × 18min gap) | N × 18 min |
| Upload full episode | 5–15 min |

For a 20-page episode → ~4 shorts → ~72min gaps + ~90min generation = ~2.5 hrs total ✅

---

## 🔑 Supported Languages

| Code | Language | Kokoro Voice |
|---|---|---|
| `en` | English | af_heart |
| `es` | Spanish | ef_dora |
| `fr` | French | ff_siwis |
| `de` | German | df_eva |
| `ja` | Japanese | jf_alpha |
| `ko` | Korean | kf_amu |
| `pt` | Portuguese | bf_emma |
| `hi` | Hindi | hf_alpha |

---

## 🛡️ YouTube Safety

- **18-minute delays** between each short upload
- Uploads use OAuth2 (not API keys) — safer for long-running sessions
- Metadata (titles, tags) are AI-generated and unique per video
- `dry_run: true` lets you test without actually posting

---

## 📝 Notes

- OCR accuracy depends on scan quality. Clean, high-res scans work best.
- Kokoro TTS word timestamps are estimated (proportional to word length). For perfect sync, replace with a forced-aligner like `aeneas` or `WhisperX`.
- The pipeline saves all intermediate files as GitHub Actions artifacts (3-day retention) for debugging.
