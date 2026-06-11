"""
concat_and_upload.py
Downloads 2–5 videos from Google Drive, concatenates them in order,
and uploads the result to YouTube with a scheduled publish time.

Usage:
  python concat_and_upload.py \\
    --links "https://drive.google.com/..." "https://drive.google.com/..." \\
    --title "My Compilation Title" \\
    --description "Description here" \\
    --tags "tag1,tag2,tag3" \\
    --publish-hours-from-now 2 \\
    --gap-seconds 0.9 \\
    --output-type full   # or: shorts
"""

import argparse
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request


# ── Google Drive download ─────────────────────────────────────────────────────

def extract_file_id(link: str) -> str:
    """Extract file ID from any Google Drive share URL format."""
    patterns = [
        r"/file/d/([a-zA-Z0-9_-]+)",
        r"id=([a-zA-Z0-9_-]+)",
        r"/d/([a-zA-Z0-9_-]+)",
    ]
    for pat in patterns:
        m = re.search(pat, link)
        if m:
            return m.group(1)
    raise ValueError(f"Cannot extract file ID from: {link}")


def download_from_drive(file_id: str, dest: Path) -> None:
    """Download a file from Google Drive, handling large-file confirm tokens."""
    api_key = os.environ.get("GDRIVE_API_KEY", "").strip()
    session  = requests.Session()

    # Try direct download first
    url  = f"https://drive.google.com/uc?export=download&id={file_id}"
    resp = session.get(url, stream=True, timeout=60)

    # Handle Google's virus-scan confirmation page for large files
    if "Content-Disposition" not in resp.headers:
        token = None
        for key, val in resp.cookies.items():
            if key.startswith("download_warning"):
                token = val
                break
        if token:
            resp = session.get(url, params={"confirm": token}, stream=True, timeout=300)

    resp.raise_for_status()

    total = int(resp.headers.get("Content-Length", 0))
    downloaded = 0
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = int(downloaded / total * 100)
                    print(f"  Downloading... {pct}%", end="\r")
    print(f"  Downloaded: {dest.name} ({downloaded / 1024 / 1024:.1f} MB)    ")


# ── FFmpeg concat ─────────────────────────────────────────────────────────────

def get_video_info(path: Path) -> dict:
    """Return {width, height, has_audio} for a video file."""
    r = subprocess.run([
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "json", str(path)
    ], capture_output=True, text=True)
    info = {"width": 1080, "height": 1920, "has_audio": True}
    try:
        streams = json.loads(r.stdout).get("streams", [])
        if streams:
            info["width"]  = streams[0].get("width",  1080)
            info["height"] = streams[0].get("height", 1920)
    except Exception:
        pass

    # Check for audio stream
    ra = subprocess.run([
        "ffprobe", "-v", "error",
        "-select_streams", "a:0",
        "-show_entries", "stream=codec_type",
        "-of", "csv=p=0", str(path)
    ], capture_output=True, text=True)
    info["has_audio"] = bool(ra.stdout.strip())
    return info


def concat_videos(
    video_paths: list,
    output_path: Path,
    gap_seconds: float = 0.9,
) -> None:
    """
    Concatenate videos with a black gap between each.
    All videos are re-encoded to the same resolution as the first video
    so the concat demuxer works reliably.
    """
    info   = get_video_info(video_paths[0])
    w, h   = info["width"], info["height"]
    fps    = 30

    print(f"[Concat] Output resolution: {w}×{h}")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        # Re-encode each video to uniform format
        norm_paths = []
        for i, vp in enumerate(video_paths):
            norm = tmp / f"norm_{i:02d}.mp4"
            print(f"[Concat] Normalising clip {i+1}/{len(video_paths)}: {Path(vp).name}")
            cmd = [
                "ffmpeg", "-y", "-i", str(vp),
                "-vf", (
                    f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                    f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,"
                    f"setsar=1,fps={fps}"
                ),
                "-c:v", "libx264", "-preset", "fast", "-crf", "22",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k",
                "-ar", "44100", "-ac", "2",
                str(norm),
            ]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(f"Normalise clip {i+1} failed:\n{r.stderr[-2000:]}")
            norm_paths.append(norm)

        # Create black gap clip
        gap_path = tmp / "gap.mp4"
        subprocess.run([
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"color=black:s={w}x{h}:r={fps}:d={gap_seconds}",
            "-f", "lavfi", "-i", f"aevalsrc=0:c=stereo:s=44100:d={gap_seconds}",
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k",
            str(gap_path),
        ], capture_output=True)

        # Build concat list: clip, gap, clip, gap, ..., clip
        concat_file = tmp / "concat.txt"
        lines = []
        for i, np in enumerate(norm_paths):
            lines.append(f"file '{np.resolve()}'\n")
            if i < len(norm_paths) - 1:
                lines.append(f"file '{gap_path.resolve()}'\n")
        concat_file.write_text("".join(lines), encoding="utf-8")

        # Final concat pass
        print(f"[Concat] Joining {len(norm_paths)} clips...")
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0", "-i", str(concat_file),
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            str(output_path),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"Concat failed:\n{r.stderr[-3000:]}")

    # Get final duration
    r = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "csv=p=0", str(output_path)
    ], capture_output=True, text=True)
    dur = float(r.stdout.strip() or 0)
    size_mb = output_path.stat().st_size / 1024 / 1024
    print(f"[Concat] ✅ {output_path.name} — {dur:.1f}s, {size_mb:.1f} MB")


# ── YouTube upload ────────────────────────────────────────────────────────────

def get_youtube_client():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YOUTUBE_CLIENT_ID"],
        client_secret=os.environ["YOUTUBE_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    creds.refresh(Request())
    return build("youtube", "v3", credentials=creds)


def upload_to_youtube(
    youtube,
    video_path: Path,
    title: str,
    description: str,
    tags: list,
    publish_at: str,
    is_short: bool = False,
) -> str:
    if is_short and "#Shorts" not in title:
        title = f"{title} #Shorts"

    body = {
        "snippet": {
            "title":           title[:100],
            "description":     description[:5000],
            "tags":            tags[:500],
            "categoryId":      "24",
            "defaultLanguage": "en",
        },
        "status": {
            "privacyStatus":            "private",
            "publishAt":                publish_at,
            "selfDeclaredMadeForKids":  False,
            "madeForKids":              False,
        },
    }

    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        resumable=True,
        chunksize=10 * 1024 * 1024,
    )

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"  Progress: {int(status.progress() * 100)}%", end="\r")

    video_id = response["id"]
    url = f"https://youtube.com/shorts/{video_id}" if is_short else f"https://youtube.com/watch?v={video_id}"
    print(f"  ✅ {url}  →  publishes at {publish_at}")
    return video_id


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Download, concat, and upload videos to YouTube")
    parser.add_argument("--links",       nargs="+", required=True,
                        help="Google Drive share links in order (2–5 videos)")
    parser.add_argument("--title",       required=True,
                        help="YouTube video title")
    parser.add_argument("--description", default="",
                        help="YouTube video description")
    parser.add_argument("--tags",        default="manhwa,anime,webtoon",
                        help="Comma-separated tags")
    parser.add_argument("--publish-hours-from-now", type=float, default=2.0,
                        help="Hours from now to schedule publish (default: 2)")
    parser.add_argument("--gap-seconds", type=float, default=0.9,
                        help="Black gap between clips in seconds (default: 0.9)")
    parser.add_argument("--output-type", choices=["full", "shorts"], default="full",
                        help="'shorts' appends #Shorts to title (default: full)")
    parser.add_argument("--output-dir",  default="./output",
                        help="Where to save the concat video (default: ./output)")
    args = parser.parse_args()

    if len(args.links) < 2:
        print("❌ Need at least 2 Drive links to concat")
        sys.exit(1)
    if len(args.links) > 5:
        print("❌ Maximum 5 Drive links supported")
        sys.exit(1)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    tags = [t.strip() for t in args.tags.split(",") if t.strip()]

    # ── Step 1: Download all videos ──────────────────────────────────────────
    print(f"\n[Step 1] Downloading {len(args.links)} video(s) from Drive...")
    downloaded = []
    for i, link in enumerate(args.links, start=1):
        print(f"\n  Video {i}/{len(args.links)}: {link[:60]}...")
        try:
            file_id = extract_file_id(link)
            dest    = output_dir / f"input_{i:02d}.mp4"
            download_from_drive(file_id, dest)
            downloaded.append(dest)
        except Exception as e:
            print(f"  ❌ Failed to download video {i}: {e}")
            sys.exit(1)

    # ── Step 2: Concat ───────────────────────────────────────────────────────
    print(f"\n[Step 2] Concatenating {len(downloaded)} video(s) with {args.gap_seconds}s gaps...")
    output_video = output_dir / "concat_output.mp4"
    try:
        concat_videos(downloaded, output_video, gap_seconds=args.gap_seconds)
    except Exception as e:
        print(f"❌ Concat failed: {e}")
        sys.exit(1)

    # ── Step 3: Upload to YouTube ────────────────────────────────────────────
    print(f"\n[Step 3] Uploading to YouTube...")
    publish_at = (
        datetime.now(timezone.utc) + timedelta(hours=args.publish_hours_from_now)
    ).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    print(f"  Title:      {args.title}")
    print(f"  Publish at: {publish_at}")
    print(f"  Type:       {args.output_type}")

    try:
        youtube  = get_youtube_client()
        video_id = upload_to_youtube(
            youtube,
            video_path   = output_video,
            title        = args.title,
            description  = args.description,
            tags         = tags,
            publish_at   = publish_at,
            is_short     = (args.output_type == "shorts"),
        )
    except Exception as e:
        print(f"❌ Upload failed: {e}")
        sys.exit(1)

    # ── Save result log ───────────────────────────────────────────────────────
    log = {
        "video_id":    video_id,
        "title":       args.title,
        "scheduled_at": publish_at,
        "source_links": args.links,
        "status":      "success",
    }
    log_path = output_dir / "concat_upload_log.json"
    log_path.write_text(json.dumps(log, indent=2), encoding="utf-8")

    print(f"\n✅ Done! Video scheduled → {publish_at}")
    print(f"   Log saved → {log_path}")


if __name__ == "__main__":
    main()