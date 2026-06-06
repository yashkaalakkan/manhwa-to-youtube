"""
upload_full_episode.py
Uploads the full episode video scheduled to publish AFTER all shorts are done.

Schedule logic:
  Reads upload_log_shorts.json to find how many shorts there are.
  Full episode publishes at: base_time + (total_shorts * 2hrs) + 2hrs
  e.g. 11 shorts → publishes at T + 24hrs
"""

import argparse
import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

PUBLISH_GAP_HOURS = 2


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


def calc_full_episode_publish_time(base: datetime, total_shorts: int) -> str:
    """
    Full episode publishes after all shorts:
    total_shorts * 2hrs + 2hrs extra buffer
    e.g. 11 shorts → T + 24hrs
    """
    hours = PUBLISH_GAP_HOURS * total_shorts + PUBLISH_GAP_HOURS
    publish_at = base + timedelta(hours=hours)
    return publish_at.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", required=True)
    parser.add_argument("--scripts", required=True)
    parser.add_argument("--manhwa", required=True)
    parser.add_argument("--episode", required=True, type=int)
    parser.add_argument("--language", default="en")
    args = parser.parse_args()

    video_path = Path(args.video)
    if not video_path.exists():
        raise FileNotFoundError(f"Full episode video not found: {video_path}")

    with open(args.scripts, encoding="utf-8") as f:
        scripts = json.load(f)

    # Figure out total shorts from log or scripts
    shorts_log_path = Path("pipeline/upload_log_shorts.json")
    total_shorts = 0
    shorts_links = ""

    if shorts_log_path.exists():
        with open(shorts_log_path) as f:
            shorts_log = json.load(f)
        successful = [u for u in shorts_log if u.get("status") == "success"]
        total_shorts = len(successful)
        shorts_links = "\n".join(
            f"Part {u['part']}: https://youtube.com/shorts/{u['video_id']}"
            for u in successful
        )
    else:
        # Fallback: count from scripts
        total_shorts = scripts.get("total_parts", 0)

    # Safely get full episode metadata — key may be absent in older pipeline runs
    full_ep = scripts.get("full_episode") or {}
    meta = full_ep.get("metadata") or {}
    title = meta.get("title", f"{args.manhwa} Episode {args.episode} | Full Episode")
    description = meta.get("description", f"Watch {args.manhwa} Episode {args.episode} — Full Episode!\n\n#manhwa #anime #webtoon")
    tags = meta.get("tags", [args.manhwa, "manhwa", "anime", "full episode", "webtoon"])

    if shorts_links:
        description += f"\n\n📱 Watch as Shorts:\n{shorts_links}"

    # Calculate publish time based on total shorts
    base_time = datetime.now(timezone.utc)
    publish_at = calc_full_episode_publish_time(base_time, total_shorts)

    print(f"[FullUpload] Total shorts: {total_shorts}")
    print(f"[FullUpload] Full episode will publish at: {publish_at}")
    print(f"[FullUpload]   ({PUBLISH_GAP_HOURS * total_shorts + PUBLISH_GAP_HOURS}hrs from now)")

    youtube = get_youtube_client()

    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "tags": tags,
            "categoryId": "24",
            "defaultLanguage": "en",
        },
        "status": {
            "privacyStatus": "private",
            "publishAt": publish_at,
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        resumable=True,
        chunksize=1024 * 1024 * 20,
    )

    print(f"[FullUpload] Uploading: {title}")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"  Progress: {int(status.progress() * 100)}%", end="\r")

    video_id = response["id"]
    print(f"\n[FullUpload] ✅ https://youtube.com/watch?v={video_id}")
    print(f"[FullUpload]    Publishes at: {publish_at}")

    with open("pipeline/upload_log_full.json", "w") as f:
        json.dump({
            "video_id": video_id,
            "title": title,
            "scheduled_at": publish_at,
            "total_shorts": total_shorts,
            "status": "success",
        }, f, indent=2)


if __name__ == "__main__":
    main()