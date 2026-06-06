"""
upload_shorts.py
Uploads all shorts in a fast batch (3min gaps to avoid 429).
Each short is scheduled to AUTO-PUBLISH every 2 hours apart.

Schedule logic:
  Part 1  → now + 2hrs
  Part 2  → now + 4hrs
  Part 3  → now + 6hrs
  ...
  Part N  → now + N*2hrs
"""

import argparse
import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

UPLOAD_GAP_SECONDS = 180       # 3 min between each upload call
PUBLISH_GAP_HOURS = 2          # 2 hrs between each short going public


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


def calc_publish_time(base: datetime, part_index: int) -> str:
    """
    part_index is 1-based.
    Part 1 → base + 2hrs
    Part 2 → base + 4hrs
    etc.
    """
    publish_at = base + timedelta(hours=PUBLISH_GAP_HOURS * part_index)
    return publish_at.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def upload_video(youtube, video_path: Path, title: str, description: str,
                 tags: list, publish_at: str) -> str:
    if "#Shorts" not in title:
        title = f"{title} #Shorts"
    if "#Shorts" not in description:
        description = f"{description}\n\n#Shorts #Manhwa #Anime #Webtoon"

    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "tags": tags[:500],
            "categoryId": "24",
            "defaultLanguage": "en",
        },
        "status": {
            "privacyStatus": "private",
            "publishAt": publish_at,
            "selfDeclaredMadeForKids": False,
            "madeForKids": False,
        },
    }

    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        resumable=True,
        chunksize=1024 * 1024 * 10,
    )

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    print(f"  Uploading {video_path.name}...")
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"  Progress: {int(status.progress() * 100)}%", end="\r")

    video_id = response["id"]
    print(f"  ✅ https://youtube.com/shorts/{video_id}  →  publishes at {publish_at}")
    return video_id


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--shorts-dir", required=True)
    parser.add_argument("--scripts", required=True)
    parser.add_argument("--manhwa", required=True)
    parser.add_argument("--episode", required=True, type=int)
    parser.add_argument("--language", default="en")
    parser.add_argument("--start-from-part", type=int, default=1,
                        help="Resume from this part number (default: 1)")
    args = parser.parse_args()

    shorts_dir = Path(args.shorts_dir)

    with open(args.scripts, encoding="utf-8") as f:
        scripts = json.load(f)

    youtube = get_youtube_client()

    short_files = sorted(shorts_dir.glob(f"short_ep{args.episode:02d}_part*.mp4"))
    total = len(short_files)

    if not short_files:
        print(f"[Upload] ❌ No short files found in {shorts_dir}")
        return

    # Base time — all schedule calculations branch from this moment
    base_time = datetime.now(timezone.utc)

    print(f"[Upload] {total} shorts to upload")
    print(f"[Upload] Publish schedule: every {PUBLISH_GAP_HOURS}hrs starting {PUBLISH_GAP_HOURS}hrs from now")
    print(f"[Upload] Part 1 publishes at: {calc_publish_time(base_time, 1)}")
    print(f"[Upload] Part {total} publishes at: {calc_publish_time(base_time, total)}")
    print(f"[Upload] Upload gap between calls: {UPLOAD_GAP_SECONDS // 60} min\n")

    # Load existing log to support resume
    log_path = Path("pipeline/upload_log_shorts.json")
    upload_log = []
    if log_path.exists():
        with open(log_path) as f:
            upload_log = json.load(f)
    already_done = {u["part"] for u in upload_log if u.get("status") == "success"}

    for i, video_path in enumerate(short_files, start=1):
        if i < args.start_from_part:
            print(f"[Upload] Skipping part {i} (--start-from-part={args.start_from_part})")
            continue
        if i in already_done:
            print(f"[Upload] Skipping part {i} (already uploaded)")
            continue

        short_script = next((s for s in scripts["shorts"] if s["part"] == i), None)
        if short_script:
            meta = short_script["metadata"]
            title = meta.get("title", f"{args.manhwa} Episode {args.episode} Part {i}")
            description = meta.get("description", "")
            tags = meta.get("tags", [args.manhwa, "manhwa", "anime"])
        else:
            title = f"{args.manhwa} Episode {args.episode} Part {i}"
            description = f"Watch {args.manhwa} Episode {args.episode} Part {i}!\n\n#manhwa #anime"
            tags = [args.manhwa, "manhwa", "anime", "shorts"]

        publish_at = calc_publish_time(base_time, i)
        print(f"[Upload] Part {i}/{total}: scheduled → {publish_at}")

        try:
            video_id = upload_video(
                youtube,
                video_path=video_path,
                title=title,
                description=description,
                tags=tags,
                publish_at=publish_at,
            )
            upload_log.append({
                "part": i,
                "video_id": video_id,
                "title": title,
                "scheduled_at": publish_at,
                "status": "success",
            })
        except Exception as e:
            print(f"[Upload] ❌ Failed part {i}: {e}")
            upload_log.append({"part": i, "status": "failed", "error": str(e)})

        # Save log after every upload so resume works even if job dies mid-way
        with open(log_path, "w") as f:
            json.dump(upload_log, f, indent=2)

        # 3min gap between uploads to avoid 429
        if i < total:
            print(f"[Upload] Waiting {UPLOAD_GAP_SECONDS // 60}min before next upload...")
            time.sleep(UPLOAD_GAP_SECONDS)

    success = sum(1 for u in upload_log if u.get("status") == "success")
    print(f"\n[Upload] ✅ {success}/{total} shorts uploaded & scheduled")
    print(f"[Upload] Schedule summary saved → {log_path}")


if __name__ == "__main__":
    main()