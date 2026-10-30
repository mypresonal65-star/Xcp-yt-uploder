"""
YouTube Uploader Module using YouTube Data API v3
Supports OAuth 2.0 with token auto-refresh and chunked resumable video upload.
"""

import os
import sys
import json
import time
import logging
from pathlib import Path
from typing import Optional, Dict, Any, Callable

logger = logging.getLogger("youtube_uploader")

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
BASE_DIR = Path(__file__).resolve().parent
TOKEN_FILE = BASE_DIR / "token.json"
CLIENT_SECRETS_FILE = BASE_DIR / "client_secrets.json"

# Cloudflare D1 Auth API URL (set via env var or cloudflare_api.txt)
CLOUDFLARE_AUTH_API = os.environ.get("CLOUDFLARE_AUTH_API", "").rstrip("/")
if not CLOUDFLARE_AUTH_API and (BASE_DIR / "cloudflare_api.txt").exists():
    try:
        CLOUDFLARE_AUTH_API = (BASE_DIR / "cloudflare_api.txt").read_text().strip().rstrip("/")
    except Exception:
        pass


def get_youtube_service(user_id: Optional[str] = None):
    """
    Initializes and returns an authenticated YouTube Data API service instance.
    Checks:
    1. Cloudflare D1 Auth API (if user_id provided)
    2. Environment variable YOUTUBE_TOKEN_JSON
    3. Local token.json file
    """
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError:
        logger.error("Google API client libraries are not installed. Please run: pip install -r requirements.txt")
        return None

    creds = None

    # 1. Try Cloudflare D1 API if user_id is given and CLOUDFLARE_AUTH_API configured
    if user_id and CLOUDFLARE_AUTH_API:
        try:
            import requests
            cf_url = f"{CLOUDFLARE_AUTH_API}/api/user/token?user_id={user_id}"
            logger.info(f"Fetching YouTube credentials for user '{user_id}' from Cloudflare D1...")
            resp = requests.get(cf_url, timeout=10)
            if resp.status_code == 200:
                tdata = resp.json()
                if tdata.get("authenticated") and tdata.get("access_token"):
                    creds = Credentials(
                        token=tdata.get("access_token"),
                        refresh_token=tdata.get("refresh_token"),
                        token_uri="https://oauth2.googleapis.com/token",
                        client_id=tdata.get("client_id"),
                        client_secret=tdata.get("client_secret"),
                        scopes=SCOPES
                    )
                    logger.info(f"Successfully loaded credentials for user '{user_id}' from Cloudflare D1.")
            else:
                logger.warning(f"Cloudflare auth API returned status {resp.status_code} for user {user_id}")
        except Exception as e:
            logger.error(f"Error fetching token from Cloudflare API: {e}")

    # 2. Try to load token from environment variable
    if not creds:
        env_token = os.environ.get("YOUTUBE_TOKEN_JSON")
        if env_token:
            try:
                token_data = json.loads(env_token)
                creds = Credentials.from_authorized_user_info(token_data, SCOPES)
                logger.info("Loaded YouTube credentials from YOUTUBE_TOKEN_JSON environment variable.")
            except Exception as e:
                logger.warning(f"Failed to parse YOUTUBE_TOKEN_JSON env var: {e}")

    # 3. Try to load token from local token.json file
    if not creds and TOKEN_FILE.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
            logger.info("Loaded YouTube credentials from local token.json.")
        except Exception as e:
            logger.warning(f"Failed to load token from {TOKEN_FILE}: {e}")

    # 4. Check validity & refresh if expired
    if creds:
        if creds.expired and creds.refresh_token:
            try:
                logger.info("Refreshing expired YouTube OAuth access token...")
                creds.refresh(Request())
                if TOKEN_FILE.exists():
                    try:
                        with open(TOKEN_FILE, "w") as f:
                            f.write(creds.to_json())
                    except Exception:
                        pass
            except Exception as e:
                logger.error(f"Error refreshing YouTube credentials: {e}")
                return None

        if creds.valid:
            return build("youtube", "v3", credentials=creds)

    logger.warning("No valid YouTube credentials found.")
    return None


def upload_video_to_youtube(
    file_path: str,
    title: str,
    description: str = "",
    privacy_status: str = "unlisted",
    tags: Optional[list] = None,
    progress_callback: Optional[Callable[[float], None]] = None,
    user_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Uploads a local video file to YouTube via YouTube Data API v3 resumable upload.
    Supports user_id for multi-user Cloudflare token retrieval.
    """
    if not os.path.exists(file_path):
        return {"success": False, "error": f"File not found: {file_path}"}

    youtube = get_youtube_service(user_id=user_id)
    if not youtube:
        return {
            "success": False,
            "error": "YouTube API not authenticated. Please connect your YouTube channel or generate token."
        }

    try:
        from googleapiclient.http import MediaFileUpload
        from googleapiclient.errors import HttpError
    except ImportError:
        return {"success": False, "error": "google-api-python-client is not installed."}

    # Format title & description
    clean_title = (title.strip() or "Uploaded Video")[:100]
    clean_desc = description.strip() or "Uploaded via OmniStream Universal Video Downloader"
    if tags is None:
        tags = ["OmniStream", "Video"]

    # Allowed privacy statuses
    valid_statuses = ["unlisted", "private", "public"]
    status_choice = privacy_status.lower() if privacy_status.lower() in valid_statuses else "unlisted"

    body = {
        "snippet": {
            "title": clean_title,
            "description": clean_desc,
            "tags": tags,
            "categoryId": "27"  # Education
        },
        "status": {
            "privacyStatus": status_choice,
            "selfDeclaredMadeForKids": False
        }
    }

    logger.info(f"Initiating YouTube upload for '{clean_title}' (Status: {status_choice})...")

    # Resumable upload chunk size: 5 MB (5 * 1024 * 1024)
    chunk_size = 5 * 1024 * 1024
    media = MediaFileUpload(file_path, chunksize=chunk_size, resumable=True)

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media
    )

    response = None
    last_reported = -1

    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                percent = round(status.progress() * 100, 1)
                if percent != last_reported:
                    last_reported = percent
                    logger.info(f"YouTube Upload Progress: {percent}%")
                    if progress_callback:
                        progress_callback(percent)
        except HttpError as e:
            if e.resp.status in [500, 502, 503, 504]:
                logger.warning(f"Recoverable HTTP error {e.resp.status}, retrying in 5s...")
                time.sleep(5)
            else:
                logger.error(f"YouTube API HttpError: {e}")
                return {"success": False, "error": str(e)}
        except Exception as e:
            logger.error(f"Error during YouTube upload chunk: {e}")
            return {"success": False, "error": str(e)}

    video_id = response.get("id")
    if video_id:
        video_url = f"https://youtu.be/{video_id}"
        logger.info(f"✅ Video successfully uploaded to YouTube: {video_url}")
        if progress_callback:
            progress_callback(100.0)
        return {
            "success": True,
            "video_id": video_id,
            "url": video_url,
            "privacy": status_choice
        }

    return {"success": False, "error": "Unknown response from YouTube API."}


def interactive_authenticate():
    """
    CLI helper to authenticate with Google and create token.json.
    Requires client_secrets.json downloaded from Google Cloud Console.
    """
    print("=" * 65)
    print("🎬 YouTube OAuth 2.0 Authentication Setup")
    print("=" * 65)

    if not CLIENT_SECRETS_FILE.exists():
        print(f"\n⚠️ Missing client_secrets.json at: {CLIENT_SECRETS_FILE}")
        print("\n👉 How to get client_secrets.json:")
        print("1. Go to Google Cloud Console: https://console.cloud.google.com/")
        print("2. Create a new project or select an existing one.")
        print("3. In 'APIs & Services' -> 'Library', enable 'YouTube Data API v3'.")
        print("4. Go to 'APIs & Services' -> 'OAuth consent screen', select 'External' and save.")
        print("   (Add your email as a Test User under 'Test users').")
        print("5. Go to 'Credentials' -> 'Create Credentials' -> 'OAuth client ID'.")
        print("   - Application type: 'Desktop app'")
        print("6. Download the JSON file and save it as 'client_secrets.json' inside the website/ folder.")
        print("\nOnce saved, run this script again: python youtube_uploader.py")
        return

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Error: Please run 'pip install google-auth-oauthlib google-api-python-client'")
        return

    print("Starting OAuth flow. Your browser will open for Google login...")
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRETS_FILE), SCOPES)
    creds = flow.run_local_server(port=0)

    with open(TOKEN_FILE, "w") as token:
        token.write(creds.to_json())

    print(f"\n✅ SUCCESS! Authentication completed.")
    print(f"📂 Saved credentials to: {TOKEN_FILE}")
    print("\n💡 Tip for Cloud / Heroku / Render deployments:")
    print("You can copy the entire content of token.json and set it as an")
    print("Environment Variable named YOUTUBE_TOKEN_JSON on your cloud platform!")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--auth":
        interactive_authenticate()
    else:
        # Check if already authenticated
        srv = get_youtube_service()
        if srv:
            print("✅ YouTube API is already authenticated and ready!")
            print(f"Token file: {TOKEN_FILE}")
        else:
            interactive_authenticate()
