"""
get_youtube_token.py
Run this LOCALLY (not in CI) to get your YouTube OAuth2 refresh token.
This is a one-time setup step.

Usage:
  python scripts/get_youtube_token.py \
    --client-id YOUR_CLIENT_ID \
    --client-secret YOUR_CLIENT_SECRET
"""

import argparse
import json
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def main():
    parser = argparse.ArgumentParser(description="Get YouTube OAuth2 refresh token")
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--client-secret", required=True)
    args = parser.parse_args()

    client_config = {
        "installed": {
            "client_id": args.client_id,
            "client_secret": args.client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }

    flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)
    creds = flow.run_local_server(port=8080, prompt="consent", access_type="offline")

    print("\n" + "="*60)
    print("✅ Authorization successful!")
    print("="*60)
    print(f"\nYOUTUBE_REFRESH_TOKEN:\n{creds.refresh_token}")
    print(f"\nYOUTUBE_CLIENT_ID:\n{args.client_id}")
    print(f"\nYOUTUBE_CLIENT_SECRET:\n{args.client_secret}")
    print("\n" + "="*60)
    print("Add these to your GitHub repository secrets.")
    print("="*60)


if __name__ == "__main__":
    main()
