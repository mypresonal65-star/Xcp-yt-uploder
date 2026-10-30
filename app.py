import os
import re
import uuid
import time
import asyncio
import logging
import shutil
import subprocess
from typing import List, Optional, Dict, Any
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, urljoin

import requests
from fastapi import FastAPI, HTTPException, BackgroundTasks, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl

import yt_dlp

from contextlib import asynccontextmanager

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("media_downloader")

# Base paths
BASE_DIR = Path(__file__).resolve().parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
STATIC_DIR = BASE_DIR / "static"
if not (STATIC_DIR / "index.html").exists():
    if (BASE_DIR / "website" / "static" / "index.html").exists():
        STATIC_DIR = BASE_DIR / "website" / "static"
    elif (BASE_DIR.parent / "website" / "static" / "index.html").exists():
        STATIC_DIR = BASE_DIR.parent / "website" / "static"
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR.mkdir(parents=True, exist_ok=True)

# ClassPlus Access Token (Can be overridden via env CP_TOKEN or token_cp)
CP_TOKEN = os.environ.get(
    "CP_TOKEN", 
    os.environ.get("token_cp", "eyJjb3Vyc2VJZCI6IjQ1NjY4NyIsInR1dG9ySWQiOm51bGwsIm9yZ0lkIjo0ODA2MTksImNhdGVnb3J5SWQiOm51bGx9r")
)

# Optional YouTube cookies path
COOKIES_FILE = os.environ.get(
    "COOKIES_FILE", 
    str(BASE_DIR.parent / "Banana_bot--main" / "youtube_cookies.txt")
)

# =========================================================================
#  Telegram Default Credentials (Pre-configured directly in code)
# =========================================================================
DEFAULT_TG_API_ID = os.environ.get("TG_API_ID", "37748306")
DEFAULT_TG_API_HASH = os.environ.get("TG_API_HASH", "745b95ee77934f04e84594d23b13c0ef")
DEFAULT_TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "8880313683:AAH3B1w0rLsjny3y6sI6ENu2Wp9rBpJhUOI")
DEFAULT_TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")

# In-memory Task Store and Queue
tasks_lock = asyncio.Lock()
tasks_db: Dict[str, Dict[str, Any]] = {}
download_queue: asyncio.Queue = asyncio.Queue()
MAX_CONCURRENT_WORKERS = int(os.environ.get("MAX_CONCURRENT_WORKERS", 5))
thread_pool = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_WORKERS)

async def auto_clean_downloads_loop():
    """
    Background worker running every 60 seconds.
    Automatically deletes files from DOWNLOADS_DIR:
    1) If uploaded to YouTube or Telegram: deleted exactly 1 hour (3600s) after latest upload completes.
    2) If not uploaded: deleted 1 hour after download completes / file creation.
    Never deletes files while upload is actively in progress.
    Keeps the YouTube watch link & task entry intact in UI while reclaiming 100% disk space.
    """
    ONE_HOUR = 3600
    while True:
        try:
            await asyncio.sleep(60)  # Check every 60 seconds
            now = time.time()
            deleted_count = 0

            # Map filename and path to task
            file_to_task = {}
            for t in tasks_db.values():
                fn = t.get("resolved_filename")
                fp = t.get("file_path")
                if fn:
                    file_to_task[fn] = t
                if fp:
                    file_to_task[Path(fp).name] = t

            for item in DOWNLOADS_DIR.glob("*"):
                if item.name == ".gitkeep":
                    continue
                if item.is_file():
                    fname = item.name
                    task = file_to_task.get(fname)

                    # SAFETY CHECK: Never delete if currently uploading
                    if task and (
                        task.get("status") == "uploading" or 
                        task.get("youtube_status") == "uploading" or 
                        task.get("telegram_status") == "uploading"
                    ):
                        continue

                    should_delete = False
                    reason = ""

                    # Case 1: Uploaded to YouTube or Telegram -> Wait 1 hour after latest upload completion
                    upload_times = [t for t in [task.get("uploaded_at"), task.get("uploaded_to_tg_at")] if t] if task else []
                    if upload_times:
                        time_since_upload = now - max(upload_times)
                        if time_since_upload >= ONE_HOUR:
                            should_delete = True
                            reason = "1 hr after upload"
                    # Case 2: General download older than 1 hour
                    else:
                        age = now - item.stat().st_mtime
                        if age >= ONE_HOUR:
                            should_delete = True
                            reason = "older than 1 hr"

                    if should_delete:
                        try:
                            item.unlink(missing_ok=True)
                            deleted_count += 1
                            logger.info(f"🧹 Auto-cleaned video from disk ({reason}): {fname}")
                            if task:
                                task["file_path"] = None
                                task["download_url"] = None
                                task["filesize_str"] = f"Auto-cleaned ({reason})"
                        except Exception as e:
                            logger.warning(f"Failed to auto-delete {fname}: {e}")

            if deleted_count > 0:
                logger.info(f"🧹 Auto-cleanup finished: Reclaimed space from {deleted_count} video(s).")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in auto_clean_downloads_loop: {e}")



@asynccontextmanager
async def lifespan(app: FastAPI):
    worker_tasks = [
        asyncio.create_task(queue_worker_loop(f"worker-{i+1}"))
        for i in range(MAX_CONCURRENT_WORKERS)
    ]
    cleaner_task = asyncio.create_task(auto_clean_downloads_loop())
    logger.info(f"Universal Web Video Downloader with {MAX_CONCURRENT_WORKERS} concurrent workers & 1-hour auto-cleanup loop initialized.")
    yield
    for wt in worker_tasks:
        wt.cancel()
    cleaner_task.cancel()

app = FastAPI(
    title="Universal Web Video Downloader",
    description="Clean, queue-based video & media downloader API with ClassPlus and Akamai DRM support",
    version="1.1.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)



def sanitize_filename(name: str) -> str:
    """Sanitize string to be safe for filenames across Windows and Linux."""
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = name.strip()
    return name if name else "video"


def format_bytes(bytes_count: Optional[float]) -> str:
    """Format bytes into readable string."""
    if not bytes_count or bytes_count <= 0:
        return "N/A"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_count < 1024.0:
            return f"{bytes_count:.1f} {unit}"
        bytes_count /= 1024.0
    return f"{bytes_count:.1f} PB"


def format_seconds(seconds: Optional[float]) -> str:
    """Format seconds into HH:MM:SS or MM:SS."""
    if seconds is None or seconds < 0:
        return "--:--"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


# YouTube Uploader integration
try:
    from youtube_uploader import upload_video_to_youtube, get_youtube_service
    YOUTUBE_UPLOADER_AVAILABLE = True
except Exception as e:
    logger.warning(f"YouTube uploader module not available: {e}")
    YOUTUBE_UPLOADER_AVAILABLE = False


# Telegram Uploader integration (Pyrofork MTProto up to 2GB)
TG_IMPORT_ERROR = None
try:
    from telegram_uploader import (
        upload_video_to_telegram,
        validate_telegram_credentials,
        PYROGRAM_AVAILABLE as TELEGRAM_UPLOADER_AVAILABLE,
        PYROGRAM_IMPORT_ERROR
    )
    if not TELEGRAM_UPLOADER_AVAILABLE:
        TG_IMPORT_ERROR = PYROGRAM_IMPORT_ERROR or "Pyrofork/Pyrogram library missing"
except Exception as e:
    logger.warning(f"Telegram uploader module not available: {e}")
    TELEGRAM_UPLOADER_AVAILABLE = False
    TG_IMPORT_ERROR = str(e)


# --- Models ---
class SingleDownloadRequest(BaseModel):
    url: str
    custom_name: Optional[str] = None
    resolution: str = "720"  # 'best', '1080', '720', '480', '360', 'audio'
    upload_to_youtube: bool = False
    privacy_status: str = "unlisted"  # 'unlisted', 'private', 'public'
    user_id: Optional[str] = None
    upload_to_telegram: bool = False
    tg_bot_token: Optional[str] = None
    tg_api_id: Optional[str] = None
    tg_api_hash: Optional[str] = None
    tg_chat_id: Optional[str] = None


class BatchItemRequest(BaseModel):
    url: str
    custom_name: Optional[str] = None
    resolution: Optional[str] = "720"
    upload_to_youtube: bool = False
    privacy_status: Optional[str] = "unlisted"
    user_id: Optional[str] = None
    upload_to_telegram: bool = False
    tg_bot_token: Optional[str] = None
    tg_api_id: Optional[str] = None
    tg_api_hash: Optional[str] = None
    tg_chat_id: Optional[str] = None


class BatchDownloadRequest(BaseModel):
    items: Optional[List[BatchItemRequest]] = None
    raw_text: Optional[str] = None
    default_resolution: str = "720"
    upload_to_youtube: bool = False
    default_privacy: str = "unlisted"
    user_id: Optional[str] = None
    upload_to_telegram: bool = False
    tg_bot_token: Optional[str] = None
    tg_api_id: Optional[str] = None
    tg_api_hash: Optional[str] = None
    tg_chat_id: Optional[str] = None


class TelegramManualUploadRequest(BaseModel):
    bot_token: Optional[str] = None
    api_id: Optional[str] = None
    api_hash: Optional[str] = None
    chat_id: Optional[str] = None



# --- DRM & ClassPlus URL Resolver Helpers ---
def extract_keys_from_mpd(mpd_content: str) -> List[str]:
    """Extract decryption keys from MPD content."""
    keys = []
    try:
        kid_pattern = r'default_KID="([^"]+)"'
        kid_matches = re.findall(kid_pattern, mpd_content)
        for kid in kid_matches:
            clean_kid = kid.replace('-', '').lower()
            keys.append(f"{clean_kid}:00000000000000000000000000000000")
        return keys
    except Exception as e:
        logger.warning(f"Error extracting keys from MPD: {e}")
        return []


def get_mps_and_keys(api_url: str) -> tuple:
    """
    Fetch MPD and keys from ClassPlus API or direct manifest.
    Supports both L1 (key + userIds) and L2 (hdnts only) links.
    """
    try:
        if api_url.endswith('.mpd') or '/manifest' in api_url:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'application/dash+xml,application/xml,text/xml,*/*'
            }
            response = requests.get(api_url, headers=headers, timeout=30)
            response.raise_for_status()
            mpd_content = response.text
            keys = extract_keys_from_mpd(mpd_content)
            return api_url, keys

        response = requests.get(api_url, timeout=30)
        response_json = response.json()
        mpd = response_json.get('MPD') or api_url
        keys = response_json.get('KEYS') or []

        if not keys and isinstance(mpd, str) and ('<MPD' in mpd or mpd.endswith('.mpd')):
            if '<MPD' in mpd:
                keys = extract_keys_from_mpd(mpd)
            else:
                _, keys = get_mps_and_keys(mpd)

        return mpd, keys
    except Exception as e:
        logger.warning(f"Error in get_mps_and_keys: {e}")
        return api_url, []


def parse_m3u8_variant(master_url: str, m3u8_text: str, target_res: Optional[str] = None) -> Optional[str]:
    """
    Parses multi-variant HLS master playlist (master.m3u8) to select the best stream
    matching the target resolution (or highest quality), preserving query parameters (edge-cache-token).
    """
    try:
        lines = [line.strip() for line in m3u8_text.splitlines() if line.strip()]
        variants = []
        current_inf = None
        for line in lines:
            if line.startswith("#EXT-X-STREAM-INF:"):
                current_inf = line
            elif current_inf and not line.startswith("#"):
                m_res = re.search(r'RESOLUTION=\d+x(\d+)', current_inf)
                m_bw = re.search(r'BANDWIDTH=(\d+)', current_inf)
                height = int(m_res.group(1)) if m_res else 0
                bandwidth = int(m_bw.group(1)) if m_bw else 0

                full_uri = urljoin(master_url, line)
                parsed_master = urlparse(master_url)
                parsed_variant = urlparse(full_uri)

                # Ensure query string from master_url (edge-cache-token, etc.) is retained
                if parsed_master.query and not parsed_variant.query:
                    full_uri = f"{full_uri}?{parsed_master.query}"
                elif parsed_master.query and parsed_variant.query and parsed_master.query not in parsed_variant.query:
                    full_uri = f"{full_uri}&{parsed_master.query}"

                variants.append({
                    "height": height,
                    "bandwidth": bandwidth,
                    "url": full_uri
                })
                current_inf = None

        if not variants:
            return None

        # Filter by requested resolution
        if target_res and str(target_res).lower() != "best":
            try:
                target_h = int(target_res)
                matching = [v for v in variants if v["height"] <= target_h and v["height"] > 0]
                if matching:
                    matching.sort(key=lambda x: (x["height"], x["bandwidth"]), reverse=True)
                    return matching[0]["url"]
            except (ValueError, TypeError):
                pass

        # Default to highest available
        variants.sort(key=lambda x: (x["height"], x["bandwidth"]), reverse=True)
        return variants[0]["url"]
    except Exception as e:
        logger.debug(f"Error parsing m3u8 variants: {e}")
        return None


def resolve_media_url(url: str, requested_resolution: Optional[str] = "720") -> Dict[str, Any]:
    """
    Resolves ClassPlus, Akamai (L1/L2), ClassX (Google Edge Cache), Testbook, and DRM URLs before downloading.
    """
    base_url = url.strip()
    resolved_url = base_url
    keys_string = ""
    extra_headers: Dict[str, str] = {}
    media_type = "Standard"

    # 1. VisionIAS support
    if "visionias" in resolved_url:
        try:
            headers = {
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Referer': 'http://www.visionias.in/',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            resp = requests.get(resolved_url, headers=headers, timeout=15)
            m = re.search(r'(https://.*?playlist\.m3u8.*?)["\']', resp.text)
            if m:
                resolved_url = m.group(1)
                media_type = "VisionIAS Stream"
        except Exception as e:
            logger.warning(f"VisionIAS resolve error: {e}")

    # 2. PW Live / Multiverse support
    elif "d1d34p8vz63oiq" in resolved_url or "sec1.pw.live" in resolved_url:
        resolved_url = f"https://download.asmultiverse.com?Vurl={resolved_url}"
        media_type = "PW Stream"

    # 3. Encrypted AppX support
    elif 'encrypted.m' in resolved_url:
        if '*' in resolved_url:
            resolved_url = resolved_url.split('*')[0]
        media_type = "AppX Encrypted"

    # ============================================================
    #  🔥 ClassX (Google Edge Cache Signed HLS Streams)
    # ============================================================
    elif any(k in resolved_url.lower() for k in ["classx.co.in", "transcoded-videos.classx"]):
        media_type = "ClassX Stream"

        # Extract tenant name from path /videos/{tenant}-data/...
        m_tenant = re.search(r'/videos/([a-zA-Z0-9_\-]+?)(?:-data)?/', resolved_url)
        tenant_name = m_tenant.group(1) if m_tenant else "web"

        # Auto-determine Referer & Origin for tenant
        auto_referer = f"https://{tenant_name}.classx.co.in/"
        auto_origin = f"https://{tenant_name}.classx.co.in"

        extra_headers['User-Agent'] = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
        extra_headers['Referer'] = auto_referer
        extra_headers['Origin'] = auto_origin
        extra_headers['Accept'] = '*/*'
        extra_headers['Accept-Encoding'] = 'gzip, deflate, br'
        extra_headers['Sec-Fetch-Mode'] = 'cors'
        extra_headers['Sec-Fetch-Site'] = 'cross-site'
        extra_headers['Sec-Fetch-Dest'] = 'empty'

        # Parse multi-variant if m3u8
        if ".m3u8" in resolved_url:
            logger.info("🎯 ClassX HLS stream detected, checking multi-variant resolution...")
            try:
                resp = requests.get(resolved_url, headers=extra_headers, timeout=6)
                if resp.status_code in [403, 404]:
                    # Fallback to transcoded-videos.classx.co.in if tenant subdomain was strictly restricted
                    fallback_headers = extra_headers.copy()
                    fallback_headers['Referer'] = "https://transcoded-videos.classx.co.in/"
                    fallback_headers['Origin'] = "https://transcoded-videos.classx.co.in"
                    fb_resp = requests.get(resolved_url, headers=fallback_headers, timeout=6)
                    if fb_resp.status_code == 200:
                        extra_headers.update(fallback_headers)
                        resp = fb_resp

                if resp.status_code == 200 and "#EXT-X-STREAM-INF" in resp.text:
                    variant_url = parse_m3u8_variant(resolved_url, resp.text, requested_resolution or "720")
                    if variant_url:
                        resolved_url = variant_url
                        logger.info(f"✅ Selected ClassX variant stream URL: {resolved_url[:80]}...")
            except Exception as e:
                logger.debug(f"ClassX m3u8 variant check skipped: {e}")

    # ============================================================
    #  🔥 FIXED: ClassPlus / Akamai Logic (L1 + L2)
    # ============================================================
    elif ('classplusapp' in resolved_url or "testbook.com" in resolved_url or 
          "classplusapp.com/drm" in resolved_url or "media-cdn.classplusapp.com/drm" in resolved_url or 
          "akamai-cdn.classplusapp.com" in resolved_url or "hdnts=" in resolved_url or "hdntl=" in resolved_url):
        
        # Determine L1 / L2 type
        is_l1 = ('key=' in resolved_url and 'userIds=' in resolved_url)
        is_l2 = ('hdnts=' in resolved_url or 'hdntl=' in resolved_url) and not is_l1

        if is_l1:
            logger.info("✅ L1 link detected (key + userIds) — preserving all parameters")
            media_type = "ClassPlus Akamai (L1)"
        elif is_l2:
            logger.info("✅ L2 link detected (hdnts/hdntl token)")
            media_type = "ClassPlus Akamai (L2)"
        else:
            logger.info("ℹ️ Standard ClassPlus / Akamai link")
            media_type = "ClassPlus / Akamai"

        # Standard headers for ClassPlus & Akamai CDN EdgeAuth
        extra_headers['User-Agent'] = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
        extra_headers['Referer'] = 'https://web.classplusapp.com/'
        extra_headers['Origin'] = 'https://web.classplusapp.com'
        extra_headers['Accept'] = '*/*'
        extra_headers['Accept-Encoding'] = 'gzip, deflate, br'
        extra_headers['Accept-Language'] = 'en-US,en;q=0.9'
        extra_headers['Sec-Fetch-Mode'] = 'cors'
        extra_headers['Sec-Fetch-Site'] = 'cross-site'
        extra_headers['Sec-Fetch-Dest'] = 'empty'
        extra_headers['x-cdn-tag'] = 'empty'

        # Check if URL is ALREADY a direct Akamai/ClassPlus HLS stream (.m3u8)
        if ".m3u8" in resolved_url:
            logger.info("🎯 Direct Akamai / ClassPlus HLS stream detected, checking multi-variant resolution...")
            try:
                resp = requests.get(resolved_url, headers=extra_headers, timeout=5)
                if resp.status_code == 200 and "#EXT-X-STREAM-INF" in resp.text:
                    variant_url = parse_m3u8_variant(resolved_url, resp.text, requested_resolution or "720")
                    if variant_url:
                        resolved_url = variant_url
                        logger.info(f"✅ Selected ClassPlus variant stream URL: {resolved_url[:80]}...")
            except Exception as e:
                logger.debug(f"Direct ClassPlus m3u8 pre-resolve skipped: {e}")

            return {
                "url": resolved_url,
                "keys_string": keys_string,
                "extra_headers": extra_headers,
                "media_type": media_type
            }

        # Check if URL is ALREADY a direct Akamai/ClassPlus DASH stream (.mpd)
        if ".mpd" in resolved_url or "/drm/" in resolved_url:
            logger.info("🎯 Direct Akamai / ClassPlus MPD DASH stream detected!")
            return {
                "url": resolved_url,
                "keys_string": keys_string,
                "extra_headers": extra_headers,
                "media_type": "ClassPlus DRM (MPD)"
            }

        # ---------- EXTRACT contentHashId / contentHashIdl ----------
        content_id = None
        hash_patterns = [
            r'contentHashId=([^&]+)',
            r'contentHashIdl=([^&]+)',
            r'[?&]contentHashId=([^&]+)',
            r'[?&]contentHashIdl=([^&]+)',
        ]
        for pattern in hash_patterns:
            match = re.search(pattern, resolved_url)
            if match:
                content_id = match.group(1).split('&')[0]
                break

        # Also try to get vidKey from URL path
        vidkey = None
        vid_patterns = [
            r'/lc/([^/]+)/',
            r'vidKey=([^&]+)',
        ]
        for pattern in vid_patterns:
            match = re.search(pattern, resolved_url)
            if match:
                vidkey = match.group(1)
                break

        logger.info(f"🔑 ClassPlus content_id: {content_id}")
        logger.info(f"🔑 ClassPlus vidkey: {vidkey}")

        # ---------- BUILD HEADERS ----------
        headers = {
            'host': 'api.classplusapp.com',
            'x-access-token': f'{CP_TOKEN}',    
            'accept-language': 'EN',
            'api-version': '18',
            'app-version': '1.4.73.2',
            'build-number': '35',
            'connection': 'Keep-Alive',
            'content-type': 'application/json',
            'device-details': 'Xiaomi_Redmi 7_SDK-32',
            'device-id': 'c28d3cb16bbdac01',
            'region': 'IN',
            'user-agent': 'Mobile-Android',
            'webengage-luid': '00000187-6fe4-5d41-a530-26186858be4c',
            'accept-encoding': 'gzip'
        }

        # ---------- USE content_id OR vidkey ----------
        api_content_id = content_id or vidkey

        if not api_content_id:
            # If no content_id found, try to extract from URL
            match = re.search(r'/([a-zA-Z0-9]+-[0-9]+[a-z]?)/', resolved_url)
            if match:
                api_content_id = match.group(1)

        if api_content_id:
            params = {
                'contentId': api_content_id,
                'offlineDownload': "false"
            }

            try:
                res = requests.get(
                    "https://api.classplusapp.com/cams/uploader/video/jw-signed-url",
                    params=params,
                    headers=headers,
                    timeout=15
                ).json()

                logger.info(f"📦 ClassPlus API Response: {res}")

                if isinstance(res, dict) and res.get("url"):
                    resolved_url = res["url"]
                    logger.info(f"✅ ClassPlus API Success - URL: {resolved_url[:50]}...")
                elif isinstance(res, dict) and 'drmUrls' in res and 'manifestUrl' in res['drmUrls']:
                    mpd_url = res['drmUrls']['manifestUrl']
                    mpd, keys = get_mps_and_keys(mpd_url)
                    resolved_url = mpd if (isinstance(mpd, str) and not mpd.startswith('<?xml') and '<MPD' not in mpd) else mpd_url
                    keys_string = " ".join([f"--key {key}" for key in keys])
                    media_type = "ClassPlus DRM (MPD)"
                else:
                    logger.warning(f"⚠️ ClassPlus API returned no URL: {res}")
                    resolved_url = base_url

            except Exception as e:
                logger.warning(f"⚠️ ClassPlus API Error: {e}")
                resolved_url = base_url
        else:
            logger.info("⚠️ No contentId or vidkey found, using original URL")
            resolved_url = base_url

    return {
        "url": resolved_url,
        "keys_string": keys_string,
        "extra_headers": extra_headers,
        "media_type": media_type
    }


# --- Worker Loop ---
def sync_download_worker(task_id: str):
    """Synchronous worker executed in ThreadPool for yt-dlp with ClassPlus/DRM support."""
    task = tasks_db.get(task_id)
    if not task:
        return

    original_url = task["url"]
    custom_name = task.get("custom_name") or ""
    resolution = task.get("resolution", "720")

    task["status"] = "downloading"
    task["started_at"] = time.time()
    
    # Resolve ClassPlus / Akamai / ClassX / DRM URL
    resolved_info = resolve_media_url(original_url, requested_resolution=resolution)
    target_url = resolved_info["url"]
    keys_string = resolved_info.get("keys_string", "")
    extra_headers = resolved_info.get("extra_headers", {})
    task["media_type"] = resolved_info.get("media_type", "Standard")
    task["resolved_url"] = target_url
    logger.info(f"Task {task_id}: Resolved link [{task['media_type']}] -> {target_url[:75]}...")

    # yt-dlp format selection
    if resolution == "audio":
        format_spec = "bestaudio/best"
        outtmpl = str(DOWNLOADS_DIR / f"{task_id}_%(title)s.%(ext)s")
        if custom_name.strip():
            safe_name = sanitize_filename(custom_name)
            outtmpl = str(DOWNLOADS_DIR / f"{task_id}_{safe_name}.%(ext)s")
        postprocessors = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]
    else:
        if resolution == "best":
            format_spec = "bestvideo+bestaudio/best"
        else:
            try:
                res_num = int(resolution)
                format_spec = f"bestvideo[height<={res_num}]+bestaudio/best[height<={res_num}]/best"
            except ValueError:
                format_spec = "bestvideo+bestaudio/best"

        outtmpl = str(DOWNLOADS_DIR / f"{task_id}_%(title)s.%(ext)s")
        if custom_name.strip():
            safe_name = sanitize_filename(custom_name)
            outtmpl = str(DOWNLOADS_DIR / f"{task_id}_{safe_name}.%(ext)s")
        postprocessors = []

    def progress_hook(d):
        if task.get("cancelled", False):
            raise Exception("Download cancelled by user.")

        status = d.get("status")
        if status == "downloading":
            total_bytes = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            frag_index = d.get("fragment_index")
            frag_count = d.get("fragment_count")

            # Accurate progress for both single files and fragmented streams
            if frag_count and frag_index:
                task["progress"] = round((frag_index / frag_count) * 100, 1)
                frag_str = f" • Frag {frag_index}/{frag_count}"
            elif total_bytes > 0:
                task["progress"] = round((downloaded / total_bytes) * 100, 1)
                frag_str = ""
            else:
                task["progress"] = 0.0
                frag_str = ""

            speed = d.get("speed")
            speed_val = f"{format_bytes(speed)}/s" if speed else "Downloading..."
            task["speed_str"] = f"{speed_val}{frag_str}"
            task["eta_str"] = format_seconds(d.get("eta"))
            task["filesize_str"] = format_bytes(total_bytes) if total_bytes > 0 else "Estimating..."

            filename = d.get("filename")
            if filename and not task.get("resolved_filename"):
                task["resolved_filename"] = Path(filename).name
        elif status == "finished":
            task["progress"] = 99.0
            task["status"] = "processing"
            task["speed_str"] = "Finalizing..."
            task["eta_str"] = "00:00"

    # Full browser headers to prevent CDN rate-limiting / throttle clamping
    browser_headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': '*/*',
        'Accept-Encoding': 'gzip, deflate, br',
        'Accept-Language': 'en-US,en;q=0.9',
        'Sec-Ch-Ua': '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        'Sec-Ch-Ua-Mobile': '?0',
        'Sec-Ch-Ua-Platform': '"Windows"',
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'same-site',
    }
    if extra_headers:
        browser_headers.update(extra_headers)

    ydl_opts = {
        'format': format_spec,
        'outtmpl': outtmpl,
        'progress_hooks': [progress_hook],
        'quiet': True,
        'no_warnings': True,
        'no_color': True,
        'retries': 50,
        'fragment_retries': 50,
        'noplaylist': True,
        'nocheckcertificate': True,
        'allow_unplayable_formats': True,
        'concurrent_fragment_downloads': 32,  # 🚀 MAX SPEED: 32 parallel fragment downloads
        'buffersize': 64 * 1024 * 1024,       # 64MB RAM buffer for zero-lag disk I/O
        'http_chunk_size': 10 * 1024 * 1024,  # 10MB chunk pipeline
        'socket_timeout': 15,
        'http_headers': browser_headers,       # Full browser identity for every chunk
    }

    # ClassPlus / Akamai CDN Turbo Download Optimizations (MAX SPEED)
    is_classplus_stream = (
        any(k in target_url.lower() for k in ["classplusapp", "akamai", "media-cdn", "testbook", "cpvod", "hdnts=", "hdntl="]) or
        "ClassPlus" in task.get("media_type", "") or
        "Akamai" in task.get("media_type", "")
    )
    if is_classplus_stream:
        ydl_opts['concurrent_fragment_downloads'] = 32  # 🚀 32 parallel chunk streams for ClassPlus
        ydl_opts['buffersize'] = 64 * 1024 * 1024       # 64MB non-blocking buffer
        ydl_opts['http_chunk_size'] = 10 * 1024 * 1024  # 10MB HTTP chunk size
        ydl_opts['socket_timeout'] = 20
        ydl_opts['retries'] = 25
        ydl_opts['fragment_retries'] = 25
        ydl_opts['hls_use_mpegts'] = True
        ydl_opts['live_from_start'] = True

        # Akamai CDN token preservation on video fragments, variant playlists & AES keys
        tok_akamai = re.search(r'[?&]((?:hdnt[sl]|key|token)=[^&]+)', target_url)
        if tok_akamai:
            tok_str = tok_akamai.group(1)
            ydl_opts.setdefault('extractor_args', {})['generic'] = {
                'fragment_query': [tok_str],
                'variant_query': [tok_str],
                'key_query': [tok_str]
            }
        else:
            parsed_u = urlparse(target_url)
            if parsed_u.query:
                ydl_opts.setdefault('extractor_args', {})['generic'] = {
                    'fragment_query': [parsed_u.query],
                    'variant_query': [parsed_u.query],
                    'key_query': [parsed_u.query]
                }

    # ClassX Google Edge Cache stream optimizations
    is_classx_stream = (
        "classx.co.in" in target_url or
        "transcoded-videos.classx" in target_url or
        task.get("media_type") == "ClassX Stream"
    )
    if is_classx_stream:
        ydl_opts['concurrent_fragment_downloads'] = 32  # 🚀 32 parallel streams
        ydl_opts['buffersize'] = 64 * 1024 * 1024
        ydl_opts['http_chunk_size'] = 10 * 1024 * 1024
        tok_m = re.search(r'[?&](edge-cache-token=[^&]+)', target_url)
        if tok_m:
            tok_str = tok_m.group(1)
            ydl_opts.setdefault('extractor_args', {})['generic'] = {
                'fragment_query': [tok_str],
                'variant_query': [tok_str]
            }

    if postprocessors:
        ydl_opts['postprocessors'] = postprocessors

    if Path(COOKIES_FILE).exists() and ("youtube.com" in target_url or "youtu.be" in target_url):
        ydl_opts['cookiefile'] = COOKIES_FILE

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Extract info first
            try:
                info = ydl.extract_info(target_url, download=False)
                extracted_title = info.get('title', 'Video')
            except Exception as e:
                logger.warning(f"Metadata extraction fallback for {task_id}: {e}")
                extracted_title = custom_name.strip() or f"Video_{task_id}"

            # If title is generic (e.g. 'master', 'index', 'Video'), extract clean identifier from Akamai / ClassX path
            if (not extracted_title or extracted_title.lower() in ["master", "index", "playlist", "video"]):
                m_media = re.search(r'/media/(\d+)/[^/]+/([a-zA-Z0-9]+)/', target_url)
                m_classx = re.search(r'/videos/([a-zA-Z0-9_\-]+?)(?:-data)?/([0-9\-]+)/', target_url)
                if m_media:
                    extracted_title = f"ClassPlus_{m_media.group(1)}_{m_media.group(2)}"
                elif m_classx:
                    tenant_clean = m_classx.group(1).replace("onlinetutoria", "")
                    content_clean = m_classx.group(2).split("-")[0]
                    extracted_title = f"ClassX_{tenant_clean}_{content_clean}"
                else:
                    extracted_title = f"ClassX_{task_id}" if is_classx_stream else f"Video_{task_id}"

            task["display_title"] = custom_name.strip() if custom_name.strip() else extracted_title

            # Perform download
            ydl.download([target_url])

        # Decrypt DRM if keys_string exists and mp4decrypt is available
        if keys_string and '--key' in keys_string:
            mp4decrypt_bin = shutil.which("mp4decrypt")
            if mp4decrypt_bin:
                task["status"] = "processing"
                task["speed_str"] = "Decrypting DRM..."
                task_files = list(DOWNLOADS_DIR.glob(f"{task_id}_*"))
                for tf in task_files:
                    if tf.suffix in [".mp4", ".m4a"]:
                        dec_file = tf.with_name(f"dec_{tf.name}")
                        decrypt_cmd = f'"{mp4decrypt_bin}" {keys_string} "{tf}" "{dec_file}"'
                        logger.info(f"Decrypting with mp4decrypt: {decrypt_cmd}")
                        subprocess.run(decrypt_cmd, shell=True)
                        if dec_file.exists() and dec_file.stat().st_size > 0:
                            tf.unlink(missing_ok=True)
                            dec_file.rename(tf)
            else:
                logger.info(f"DRM keys found ({keys_string}) but mp4decrypt not installed in PATH.")

        # Find the final file on disk
        candidates = list(DOWNLOADS_DIR.glob(f"{task_id}_*"))
        if candidates:
            final_file = candidates[0]
            task["file_path"] = str(final_file)
            task["resolved_filename"] = final_file.name
            friendly_name = final_file.name.replace(f"{task_id}_", "")
            task["friendly_filename"] = friendly_name
            task["filesize_str"] = format_bytes(final_file.stat().st_size)
            task["download_url"] = f"/api/files/{task_id}"

            # Auto-upload to YouTube if enabled
            if task.get("upload_to_youtube"):
                task["status"] = "uploading"
                task["speed_str"] = "Uploading to YouTube..."
                task["progress"] = 0.0
                logger.info(f"Task {task_id}: Starting auto-upload to YouTube...")

                def yt_progress(pct):
                    task["progress"] = pct
                    task["speed_str"] = f"YouTube: {pct}%"

                yt_title = task.get("display_title") or friendly_name
                if YOUTUBE_UPLOADER_AVAILABLE:
                    yt_res = upload_video_to_youtube(
                        file_path=str(final_file),
                        title=yt_title,
                        description=f"Auto-downloaded & uploaded via OmniStream\nOriginal Source: {original_url}",
                        privacy_status=task.get("privacy_status", "unlisted"),
                        progress_callback=yt_progress,
                        user_id=task.get("user_id")
                    )
                    if yt_res.get("success"):
                        task["youtube_url"] = yt_res.get("url")
                        task["youtube_status"] = "uploaded"
                        task["uploaded_at"] = time.time()
                        logger.info(f"Task {task_id}: YouTube upload succeeded -> {yt_res.get('url')}")
                    else:
                        task["youtube_status"] = f"Upload failed: {yt_res.get('error')}"
                        logger.warning(f"Task {task_id}: YouTube upload failed -> {yt_res.get('error')}")
                else:
                    task["youtube_status"] = "YouTube uploader module not configured."

            # Auto-upload to Telegram if enabled
            if task.get("upload_to_telegram") and not task.get("cancelled"):
                task["status"] = "uploading"
                task["speed_str"] = "Uploading to Telegram..."
                task["progress"] = 0.0
                task["telegram_status"] = "uploading"
                logger.info(f"Task {task_id}: Starting auto-upload to Telegram...")

                def tg_progress(pct, spd_str):
                    task["progress"] = pct
                    task["speed_str"] = f"TG: {pct}% ({spd_str})"

                tg_title = task.get("display_title") or friendly_name
                bot_token = task.get("tg_bot_token") or DEFAULT_TG_BOT_TOKEN
                api_id = task.get("tg_api_id") or DEFAULT_TG_API_ID
                api_hash = task.get("tg_api_hash") or DEFAULT_TG_API_HASH
                chat_id = task.get("tg_chat_id") or DEFAULT_TG_CHAT_ID

                if TELEGRAM_UPLOADER_AVAILABLE and bot_token and api_id and api_hash and chat_id:
                    try:
                        caption_text = f"🎬 **{tg_title}**"
                        tg_res = asyncio.run(upload_video_to_telegram(
                            file_path=str(final_file),
                            caption=caption_text,
                            bot_token=bot_token,
                            api_id=api_id,
                            api_hash=api_hash,
                            chat_id=chat_id,
                            progress_callback=tg_progress
                        ))
                        if tg_res.get("success"):
                            task["telegram_url"] = tg_res.get("url")
                            task["telegram_message_id"] = tg_res.get("message_id")
                            task["telegram_status"] = "uploaded"
                            task["uploaded_to_tg_at"] = time.time()
                            logger.info(f"Task {task_id}: Telegram upload succeeded -> {tg_res.get('url') or tg_res.get('message_id')}")
                        else:
                            task["telegram_status"] = f"Upload failed: {tg_res.get('error')}"
                            logger.warning(f"Task {task_id}: Telegram upload failed -> {tg_res.get('error')}")
                    except Exception as e:
                        task["telegram_status"] = f"Upload error: {str(e)}"
                        logger.error(f"Task {task_id}: Telegram upload exception: {e}")
                else:
                    missing = []
                    if not TELEGRAM_UPLOADER_AVAILABLE:
                        missing.append(f"Server library missing ({TG_IMPORT_ERROR or 'run: pip install pyrofork tgcrypto'})")
                    if not bot_token: missing.append("Bot Token")
                    if not api_id: missing.append("API ID")
                    if not api_hash: missing.append("API Hash")
                    if not chat_id: missing.append("Chat ID")
                    task["telegram_status"] = f"Missing TG configs: {', '.join(missing)}"

            task["status"] = "completed"
            task["progress"] = 100.0
            task["completed_at"] = time.time()
            logger.info(f"Task {task_id} completed successfully: {final_file.name}")
        else:
            task["status"] = "failed"
            task["error_message"] = "Output file not found after download completed."
    except Exception as e:
        logger.error(f"Error downloading task {task_id}: {str(e)}")
        task["status"] = "failed"
        task["error_message"] = str(e)
    finally:
        task["finished_at"] = time.time()


async def queue_worker_loop(worker_name: str = "worker"):
    """Background task to pull items from the download queue."""
    loop = asyncio.get_running_loop()
    while True:
        try:
            task_id = await download_queue.get()
            task = tasks_db.get(task_id)
            if task and not task.get("cancelled", False):
                logger.info(f"[{worker_name}] Starting download task: {task_id}")
                await loop.run_in_executor(thread_pool, sync_download_worker, task_id)
            download_queue.task_done()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[{worker_name}] Queue worker exception: {e}")
            await asyncio.sleep(1)


# Cloudflare D1 Auth API URL (configurable via env or cloudflare_api.txt)
CLOUDFLARE_AUTH_API = os.environ.get("CLOUDFLARE_AUTH_API", "").rstrip("/")
if not CLOUDFLARE_AUTH_API and (BASE_DIR / "cloudflare_api.txt").exists():
    try:
        CLOUDFLARE_AUTH_API = (BASE_DIR / "cloudflare_api.txt").read_text().strip().rstrip("/")
    except Exception:
        pass


# --- Helper Functions ---
def create_task_entry(
    url: str, 
    custom_name: Optional[str], 
    resolution: str,
    upload_to_youtube: bool = False,
    privacy_status: str = "unlisted",
    user_id: Optional[str] = None,
    upload_to_telegram: bool = False,
    tg_bot_token: Optional[str] = None,
    tg_api_id: Optional[str] = None,
    tg_api_hash: Optional[str] = None,
    tg_chat_id: Optional[str] = None
) -> str:
    task_id = str(uuid.uuid4())[:8]
    is_cp = any(k in url.lower() for k in ["classplusapp", "testbook.com", "akamai", "media-cdn"])
    is_classx = any(k in url.lower() for k in ["classx.co.in", "transcoded-videos.classx"])
    tasks_db[task_id] = {
        "id": task_id,
        "url": url.strip(),
        "custom_name": custom_name.strip() if custom_name else "",
        "display_title": custom_name.strip() if custom_name else url.strip(),
        "resolution": resolution,
        "media_type": "ClassPlus / Akamai" if is_cp else ("ClassX Stream" if is_classx else "Standard"),
        "resolved_url": None,
        "upload_to_youtube": upload_to_youtube,
        "privacy_status": privacy_status,
        "user_id": user_id,
        "youtube_url": None,
        "youtube_status": None,
        "upload_to_telegram": upload_to_telegram,
        "tg_bot_token": tg_bot_token or DEFAULT_TG_BOT_TOKEN,
        "tg_api_id": tg_api_id or DEFAULT_TG_API_ID,
        "tg_api_hash": tg_api_hash or DEFAULT_TG_API_HASH,
        "tg_chat_id": tg_chat_id or DEFAULT_TG_CHAT_ID,
        "telegram_url": None,
        "telegram_status": None,
        "telegram_message_id": None,
        "uploaded_to_tg_at": None,
        "status": "queued",
        "progress": 0.0,
        "speed_str": "--",
        "eta_str": "--",
        "filesize_str": "--",
        "file_path": None,
        "resolved_filename": None,
        "friendly_filename": None,
        "download_url": None,
        "error_message": None,
        "cancelled": False,
        "created_at": time.time(),
        "started_at": None,
        "completed_at": None,
        "uploaded_at": None,
    }
    return task_id



# --- API Routes ---
def background_yt_upload(task_id: str):
    task = tasks_db.get(task_id)
    if not task:
        return
    file_path = task.get("file_path")
    if not file_path or not os.path.exists(file_path):
        task["youtube_status"] = "Error: File missing from disk"
        return
    task["status"] = "uploading"
    task["speed_str"] = "Uploading to YouTube..."
    task["progress"] = 0.0

    def yt_progress(pct):
        task["progress"] = pct
        task["speed_str"] = f"YouTube: {pct}%"

    yt_title = task.get("display_title") or task.get("friendly_filename") or "Uploaded Video"
    if YOUTUBE_UPLOADER_AVAILABLE:
        yt_res = upload_video_to_youtube(
            file_path=file_path,
            title=yt_title,
            description=f"Auto-downloaded & uploaded via OmniStream\nOriginal Source: {task.get('url', '')}",
            privacy_status=task.get("privacy_status", "unlisted"),
            progress_callback=yt_progress,
            user_id=task.get("user_id")
        )
        if yt_res.get("success"):
            task["youtube_url"] = yt_res.get("url")
            task["youtube_status"] = "uploaded"
            task["uploaded_at"] = time.time()
            task["status"] = "completed"
            task["progress"] = 100.0
            logger.info(f"Task {task_id}: YouTube upload succeeded -> {yt_res.get('url')}")
        else:
            task["youtube_status"] = f"Upload failed: {yt_res.get('error')}"
            task["status"] = "completed"
            task["progress"] = 100.0
            logger.warning(f"Task {task_id}: YouTube upload failed -> {yt_res.get('error')}")


def background_tg_upload(task_id: str):
    task = tasks_db.get(task_id)
    if not task:
        return
    file_path = task.get("file_path")
    if not file_path or not os.path.exists(file_path):
        task["telegram_status"] = "Error: File missing from disk"
        return
    task["status"] = "uploading"
    task["speed_str"] = "Uploading to Telegram..."
    task["progress"] = 0.0
    task["telegram_status"] = "uploading"

    def tg_progress(pct, spd_str):
        task["progress"] = pct
        task["speed_str"] = f"TG: {pct}% ({spd_str})"

    tg_title = task.get("display_title") or task.get("friendly_filename") or "Uploaded Video"
    bot_token = task.get("tg_bot_token") or DEFAULT_TG_BOT_TOKEN
    api_id = task.get("tg_api_id") or DEFAULT_TG_API_ID
    api_hash = task.get("tg_api_hash") or DEFAULT_TG_API_HASH
    chat_id = task.get("tg_chat_id") or DEFAULT_TG_CHAT_ID

    if TELEGRAM_UPLOADER_AVAILABLE and bot_token and api_id and api_hash and chat_id:
        try:
            caption_text = f"🎬 **{tg_title}**"
            tg_res = asyncio.run(upload_video_to_telegram(
                file_path=file_path,
                caption=caption_text,
                bot_token=bot_token,
                api_id=api_id,
                api_hash=api_hash,
                chat_id=chat_id,
                progress_callback=tg_progress
            ))
            if tg_res.get("success"):
                task["telegram_url"] = tg_res.get("url")
                task["telegram_message_id"] = tg_res.get("message_id")
                task["telegram_status"] = "uploaded"
                task["uploaded_to_tg_at"] = time.time()
                task["status"] = "completed"
                task["progress"] = 100.0
                logger.info(f"Task {task_id}: Telegram manual upload succeeded -> {tg_res.get('url') or tg_res.get('message_id')}")
            else:
                task["telegram_status"] = f"Upload failed: {tg_res.get('error')}"
                task["status"] = "completed"
                task["progress"] = 100.0
                logger.warning(f"Task {task_id}: Telegram manual upload failed -> {tg_res.get('error')}")
        except Exception as e:
            task["telegram_status"] = f"Upload error: {str(e)}"
            task["status"] = "completed"
            task["progress"] = 100.0
            logger.error(f"Task {task_id}: Telegram upload exception -> {e}")
    else:
        missing = []
        if not TELEGRAM_UPLOADER_AVAILABLE:
            missing.append(f"Server library missing ({TG_IMPORT_ERROR or 'run: pip install pyrofork tgcrypto'})")
        if not bot_token: missing.append("Bot Token")
        if not api_id: missing.append("API ID")
        if not api_hash: missing.append("API Hash")
        if not chat_id: missing.append("Chat ID")
        task["telegram_status"] = f"Missing TG configs: {', '.join(missing)}"
        task["status"] = "completed"


@app.get("/api/youtube/status")
async def get_youtube_status(user_id: Optional[str] = Query(default=None)):
    # 1. Check Cloudflare if configured and user_id passed
    if user_id and CLOUDFLARE_AUTH_API:
        try:
            cf_res = requests.get(f"{CLOUDFLARE_AUTH_API}/api/user/token?user_id={user_id}", timeout=5)
            cdata = cf_res.json() if "application/json" in cf_res.headers.get("content-type", "") else {}
            if cf_res.status_code == 200:
                return {
                    "available": True,
                    "authenticated": cdata.get("authenticated", False),
                    "channel_name": cdata.get("channel_name", "Connected"),
                    "mode": "cloudflare"
                }
            elif cf_res.status_code == 404:
                return {
                    "available": True,
                    "authenticated": False,
                    "mode": "cloudflare"
                }
        except Exception as e:
            logger.warning(f"Error checking user status with Cloudflare API: {e}")

    # 2. Local fallback
    srv = get_youtube_service(user_id=user_id) if YOUTUBE_UPLOADER_AVAILABLE else None
    return {
        "available": YOUTUBE_UPLOADER_AVAILABLE,
        "authenticated": srv is not None,
        "mode": "local"
    }


@app.post("/api/user/save-secrets")
async def save_user_secrets(payload: dict):
    user_id = payload.get("user_id")
    client_secrets = payload.get("client_secrets")
    if not user_id or not client_secrets:
        raise HTTPException(status_code=400, detail="user_id and client_secrets are required")

    if not CLOUDFLARE_AUTH_API:
        # Save locally if no Cloudflare configured
        try:
            with open(BASE_DIR / "client_secrets.json", "w") as f:
                json.dump(client_secrets, f, indent=2)
            return {"success": True, "message": "Saved client_secrets.json locally on server."}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    try:
        cf_res = requests.post(
            f"{CLOUDFLARE_AUTH_API}/api/user/save-secrets",
            json={"user_id": user_id, "client_secrets": client_secrets},
            timeout=10
        )
        return cf_res.json()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to connect to Cloudflare API: {str(e)}")


@app.get("/api/user/login-url")
async def get_user_login_url(user_id: str):
    if not CLOUDFLARE_AUTH_API:
        return {"url": None, "error": "Cloudflare Auth API URL is not set"}
    return {"url": f"{CLOUDFLARE_AUTH_API}/api/auth/google/login?user_id={user_id}"}


@app.post("/api/youtube/upload/{task_id}")
async def manual_youtube_upload(task_id: str, background_tasks: BackgroundTasks):
    task = tasks_db.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    srv = get_youtube_service(user_id=task.get("user_id")) if YOUTUBE_UPLOADER_AVAILABLE else None
    if not srv:
        raise HTTPException(status_code=400, detail="YouTube API not authenticated. Please connect your YouTube channel first.")
    
    file_path = task.get("file_path")
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Downloaded video file not found on disk")

    background_tasks.add_task(background_yt_upload, task_id)
    return {"success": True, "message": "YouTube upload started"}


@app.get("/api/telegram/config")
async def get_telegram_config():
    """Returns Telegram configuration status so client only needs to provide Channel ID."""
    return {
        "api_id_configured": bool(DEFAULT_TG_API_ID),
        "api_hash_configured": bool(DEFAULT_TG_API_HASH),
        "bot_token_configured": bool(DEFAULT_TG_BOT_TOKEN),
        "default_api_id": DEFAULT_TG_API_ID,
        "default_api_hash": DEFAULT_TG_API_HASH,
        "default_bot_token": DEFAULT_TG_BOT_TOKEN,
        "default_chat_id": DEFAULT_TG_CHAT_ID,
        "pyrogram_available": TELEGRAM_UPLOADER_AVAILABLE,
        "import_error": TG_IMPORT_ERROR
    }


@app.post("/api/telegram/test")
async def test_telegram_creds(payload: dict):
    bot_token = (payload.get("bot_token") or "").strip() or DEFAULT_TG_BOT_TOKEN
    chat_id = (payload.get("chat_id") or "").strip()

    if not bot_token:
        return {
            "valid": False, 
            "error": "Bot token not configured on server and not provided in request.",
            "bot_username": None,
            "pyrogram_available": TELEGRAM_UPLOADER_AVAILABLE
        }

    res = validate_telegram_credentials(bot_token)
    res["pyrogram_available"] = TELEGRAM_UPLOADER_AVAILABLE

    # If chat_id is provided, verify whether bot can access the channel/chat
    if res.get("valid") and chat_id:
        try:
            check_cid = chat_id if chat_id.startswith(("-100", "@")) else f"-100{chat_id}" if chat_id.lstrip("-").isdigit() else chat_id
            test_res = requests.get(
                f"https://api.telegram.org/bot{bot_token}/getChat",
                params={"chat_id": check_cid},
                timeout=5
            ).json()
            if test_res.get("ok"):
                chat_info = test_res.get("result", {})
                res["chat_connected"] = True
                res["chat_title"] = chat_info.get("title") or chat_info.get("username") or str(chat_id)
                res["formatted_chat_id"] = str(chat_info.get("id", check_cid))
            else:
                orig_res = requests.get(
                    f"https://api.telegram.org/bot{bot_token}/getChat",
                    params={"chat_id": chat_id},
                    timeout=5
                ).json()
                if orig_res.get("ok"):
                    chat_info = orig_res.get("result", {})
                    res["chat_connected"] = True
                    res["chat_title"] = chat_info.get("title") or chat_info.get("username") or str(chat_id)
                    res["formatted_chat_id"] = str(chat_info.get("id", chat_id))
                else:
                    res["chat_connected"] = False
                    res["chat_error"] = test_res.get("description", "Bot could not find channel. Ensure Bot is added as Administrator with Post permission.")
        except Exception as e:
            res["chat_connected"] = False
            res["chat_error"] = f"Could not verify channel access: {e}"

    if not TELEGRAM_UPLOADER_AVAILABLE:
        res["warning"] = f"Bot token is valid, but Pyrofork MTProto library is not installed: {TG_IMPORT_ERROR or 'run: pip install pyrofork tgcrypto'}"
    return res


@app.post("/api/telegram/upload/{task_id}")
async def manual_telegram_upload(
    task_id: str, 
    payload: TelegramManualUploadRequest, 
    background_tasks: BackgroundTasks
):
    if not TELEGRAM_UPLOADER_AVAILABLE:
        raise HTTPException(
            status_code=400,
            detail=f"Telegram uploader disabled: Pyrofork library is missing on server. Run: !pip install pyrofork tgcrypto in your notebook/server. ({TG_IMPORT_ERROR or ''})"
        )

    task = tasks_db.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    file_path = task.get("file_path")
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Downloaded video file not found on disk")

    if payload.bot_token: task["tg_bot_token"] = payload.bot_token
    if payload.api_id: task["tg_api_id"] = payload.api_id
    if payload.api_hash: task["tg_api_hash"] = payload.api_hash
    if payload.chat_id: task["tg_chat_id"] = payload.chat_id

    background_tasks.add_task(background_tg_upload, task_id)
    return {"success": True, "message": "Telegram upload started in background"}


@app.post("/api/download/single")
async def download_single(req: SingleDownloadRequest):
    url = req.url.strip()
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail="Invalid URL provided. Please provide a full HTTP/HTTPS URL.")

    task_id = create_task_entry(
        url=url, 
        custom_name=req.custom_name, 
        resolution=req.resolution,
        upload_to_youtube=req.upload_to_youtube,
        privacy_status=req.privacy_status,
        user_id=req.user_id,
        upload_to_telegram=req.upload_to_telegram,
        tg_bot_token=req.tg_bot_token,
        tg_api_id=req.tg_api_id,
        tg_api_hash=req.tg_api_hash,
        tg_chat_id=req.tg_chat_id
    )
    await download_queue.put(task_id)
    return {"success": True, "task": tasks_db[task_id]}


@app.post("/api/download/batch")
async def download_batch(req: BatchDownloadRequest):
    added_tasks = []

    # Case 1: Structured items list
    if req.items:
        for item in req.items:
            u = item.url.strip()
            if u.startswith("http://") or u.startswith("https://"):
                res = item.resolution or req.default_resolution
                yt_up = item.upload_to_youtube or req.upload_to_youtube
                priv = item.privacy_status or req.default_privacy
                uid = item.user_id or req.user_id
                tg_up = item.upload_to_telegram or req.upload_to_telegram
                b_tok = item.tg_bot_token or req.tg_bot_token
                a_id = item.tg_api_id or req.tg_api_id
                a_hash = item.tg_api_hash or req.tg_api_hash
                c_id = item.tg_chat_id or req.tg_chat_id
                tid = create_task_entry(
                    url=u, 
                    custom_name=item.custom_name, 
                    resolution=res, 
                    upload_to_youtube=yt_up, 
                    privacy_status=priv, 
                    user_id=uid,
                    upload_to_telegram=tg_up,
                    tg_bot_token=b_tok,
                    tg_api_id=a_id,
                    tg_api_hash=a_hash,
                    tg_chat_id=c_id
                )
                await download_queue.put(tid)
                added_tasks.append(tasks_db[tid])

    # Case 2: Raw multiline text
    if req.raw_text:
        lines = [line.strip() for line in req.raw_text.splitlines() if line.strip()]
        for line in lines:
            extracted_url = None
            extracted_name = None

            if " | " in line:
                parts = line.split(" | ", 1)
                if parts[0].startswith("http"):
                    extracted_url, extracted_name = parts[0], parts[1]
                else:
                    extracted_name, extracted_url = parts[0], parts[1]
            elif " : " in line:
                parts = line.split(" : ", 1)
                if parts[0].startswith("http"):
                    extracted_url, extracted_name = parts[0], parts[1]
                else:
                    extracted_name, extracted_url = parts[0], parts[1]
            elif "://" in line:
                match = re.search(r'(https?://[^\s]+)', line)
                if match:
                    extracted_url = match.group(1)
                    name_candidate = line.replace(extracted_url, "").strip(" -:|")
                    if name_candidate:
                        extracted_name = name_candidate

            if extracted_url and (extracted_url.startswith("http://") or extracted_url.startswith("https://")):
                tid = create_task_entry(
                    url=extracted_url, 
                    custom_name=extracted_name, 
                    resolution=req.default_resolution,
                    upload_to_youtube=req.upload_to_youtube,
                    privacy_status=req.default_privacy,
                    user_id=req.user_id,
                    upload_to_telegram=req.upload_to_telegram,
                    tg_bot_token=req.tg_bot_token,
                    tg_api_id=req.tg_api_id,
                    tg_api_hash=req.tg_api_hash,
                    tg_chat_id=req.tg_chat_id
                )
                await download_queue.put(tid)
                added_tasks.append(tasks_db[tid])

    if not added_tasks:
        raise HTTPException(status_code=400, detail="No valid URLs found in request.")

    return {"success": True, "count": len(added_tasks), "tasks": added_tasks}



@app.get("/api/queue")
async def get_queue(user_id: Optional[str] = Query(None)):
    if user_id:
        user_tasks = [t for t in tasks_db.values() if t.get("user_id") == user_id]
    else:
        user_tasks = list(tasks_db.values())
    # Return tasks sorted by creation time (most recent first)
    sorted_tasks = sorted(user_tasks, key=lambda t: t.get("created_at", 0), reverse=True)
    return {"tasks": sorted_tasks}


@app.get("/api/queue/{task_id}")
async def get_task_status(task_id: str):
    task = tasks_db.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.delete("/api/queue/{task_id}")
async def cancel_or_delete_task(task_id: str, user_id: Optional[str] = Query(None)):
    task = tasks_db.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    if user_id and task.get("user_id") and task.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Unauthorized to delete this task")

    task["cancelled"] = True
    # If file exists, clean up
    file_path = task.get("file_path")
    if file_path and os.path.exists(file_path):
        try:
            os.remove(file_path)
        except Exception as e:
            logger.warning(f"Failed to delete file {file_path}: {e}")

    tasks_db.pop(task_id, None)
    return {"success": True, "message": f"Task {task_id} deleted."}


@app.post("/api/queue/clear")
async def clear_finished_tasks(user_id: Optional[str] = Query(None)):
    to_delete = [
        tid for tid, t in tasks_db.items()
        if t.get("status") in ("completed", "failed") and (not user_id or t.get("user_id") == user_id)
    ]
    for tid in to_delete:
        tasks_db.pop(tid, None)
    return {"success": True, "cleared_count": len(to_delete)}


@app.get("/api/files/{task_id}")
async def download_completed_file(task_id: str):
    task = tasks_db.get(task_id)
    if not task or task.get("status") != "completed":
        raise HTTPException(status_code=404, detail="File is not available or download not completed.")

    file_path = task.get("file_path")
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Downloaded file not found on disk.")

    friendly_name = task.get("friendly_filename") or Path(file_path).name
    return FileResponse(
        path=file_path,
        filename=friendly_name,
        media_type="application/octet-stream"
    )


@app.post("/api/storage/clean")
async def clean_storage_manually(user_id: Optional[str] = Query(None)):
    """Manually deletes downloaded video files for the requesting user (or all if user_id is omitted)."""
    count = 0
    freed_bytes = 0
    target_tasks = [t for t in tasks_db.values() if not user_id or t.get("user_id") == user_id]

    for t in target_tasks:
        file_path = t.get("file_path")
        if file_path and os.path.exists(file_path):
            try:
                fsize = os.path.getsize(file_path)
                os.remove(file_path)
                freed_bytes += fsize
                count += 1
                t["file_path"] = None
                t["download_url"] = None
                t["filesize_str"] = "Cleaned"
            except Exception as e:
                logger.warning(f"Could not delete {file_path}: {e}")

    return {
        "success": True,
        "deleted_files": count,
        "freed_space": format_bytes(freed_bytes)
    }


@app.get("/api/stats")
async def get_system_stats(user_id: Optional[str] = Query(None)):
    target_tasks = [t for t in tasks_db.values() if not user_id or t.get("user_id") == user_id]
    total = len(target_tasks)
    active = sum(1 for t in target_tasks if t.get("status") in ("queued", "downloading", "processing", "uploading"))
    completed = sum(1 for t in target_tasks if t.get("status") == "completed")
    failed = sum(1 for t in target_tasks if t.get("status") == "failed")

    # Compute target user's files size
    total_size = 0
    for t in target_tasks:
        fp = t.get("file_path")
        if fp and os.path.exists(fp):
            try:
                total_size += os.path.getsize(fp)
            except Exception:
                pass

    user_queue_len = sum(1 for t in target_tasks if t.get("status") == "queued")

    return {
        "total_tasks": total,
        "active_tasks": active,
        "completed_tasks": completed,
        "failed_tasks": failed,
        "disk_usage": format_bytes(total_size),
        "queue_length": user_queue_len
    }


# Mount static assets
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=True)
