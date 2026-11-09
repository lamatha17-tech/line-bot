"""
TikTok to Facebook Reels Auto-Sync Module
ONESTUDIO Wedding Cards & Souvenirs
- Fetches latest TikTok videos without watermark (No Watermark MP4)
- Formats caption with high-converting LINE OA Call-to-Action
- Publishes automatically to Facebook Page as Facebook Reels
- Deduplicates using synced_reels.json so no duplicate posts occur
"""

import os
import json
import logging
import requests
from typing import List, Dict, Optional, Tuple
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

logger = logging.getLogger("tiktok_to_reels")
logger.setLevel(logging.INFO)
if not logger.handlers:
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s"))
    logger.addHandler(ch)

STATE_FILE = Path(__file__).parent / "synced_reels.json"
TEMP_DIR = Path(__file__).parent / "temp_reels"
TEMP_DIR.mkdir(exist_ok=True)

# Default shop promotion CTA
DEFAULT_LINE_CTA = (
    "\n\n───────────────────\n"
    "💍 ONESTUDIO - การ์ดแต่งงาน & ของชำร่วย ดีไซน์สวย พรีเมียม\n"
    "💌 ปรึกษาแบบ / เช็คคิวงาน / สอบถามราคา:\n"
    "👉 LINE OA: @795ncmnv (มี @ ด้วยนะคะ)\n"
    "👉 คลิกลิงก์ทักแชท: {line_url}"
)


def load_synced_ids() -> List[str]:
    """Load previously posted TikTok video IDs to prevent duplicates."""
    if not STATE_FILE.exists():
        return []
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("synced_ids", [])
    except Exception as e:
        logger.error(f"Error loading synced IDs: {e}")
        return []


def save_synced_id(video_id: str, title: str = "", fb_post_id: str = ""):
    """Save newly posted video ID to history."""
    try:
        history = []
        synced_ids = []
        if STATE_FILE.exists():
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                synced_ids = data.get("synced_ids", [])
                history = data.get("history", [])

        if video_id not in synced_ids:
            synced_ids.append(video_id)
            history.insert(0, {
                "tiktok_id": video_id,
                "title": title,
                "fb_post_id": fb_post_id,
                "synced_at": requests.utils.default_user_agent() # timestamp fallback
            })
            # keep max 200 history items
            history = history[:200]

            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump({"synced_ids": synced_ids, "history": history}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Error saving synced ID: {e}")


def get_tiktok_user_videos(username: str, count: int = 5) -> List[Dict]:
    """
    Fetch user's latest videos from TikTok using TikWM API (Free, zero-watermark).
    """
    clean_user = username.lstrip("@").strip()
    if not clean_user:
        return []

    url = f"https://www.tikwm.com/api/user/posts?unique_id={clean_user}&count={count}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        res = requests.get(url, headers=headers, timeout=20)
        data = res.json()
        if data.get("code") == 0 and "data" in data and "videos" in data["data"]:
            videos = []
            for v in data["data"]["videos"]:
                videos.append({
                    "id": str(v.get("video_id") or v.get("id")),
                    "title": v.get("title", ""),
                    "play_url": v.get("play", ""),          # Watermark-free MP4 URL
                    "hdplay_url": v.get("hdplay", ""),      # HD version if available
                    "cover": v.get("cover", ""),
                    "duration": v.get("duration", 0),
                    "created_at": v.get("create_time", 0)
                })
            return videos
        else:
            logger.warning(f"TikWM returned code != 0 or no videos: {data.get('msg', 'Unknown')}")
            return []
    except Exception as e:
        logger.error(f"Failed to fetch TikTok videos for @{clean_user}: {e}")
        return []


def get_single_tiktok_video(video_url: str) -> Optional[Dict]:
    """
    Fetch a single TikTok video details without watermark via TikWM API.
    """
    api_url = "https://www.tikwm.com/api/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    payload = {"url": video_url, "hd": 1}

    try:
        res = requests.post(api_url, data=payload, headers=headers, timeout=25)
        data = res.json()
        if data.get("code") == 0 and "data" in data:
            v = data["data"]
            return {
                "id": str(v.get("id", "")),
                "title": v.get("title", ""),
                "play_url": v.get("play", ""),
                "hdplay_url": v.get("hdplay", ""),
                "cover": v.get("cover", ""),
                "duration": v.get("duration", 0)
            }
        else:
            logger.warning(f"TikWM video parse failed: {data.get('msg')}")
            return None
    except Exception as e:
        logger.error(f"Failed to fetch TikTok video {video_url}: {e}")
        return None


def download_video(video_url: str, output_path: Path) -> bool:
    """Download video binary from URL to local path."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    try:
        with requests.get(video_url, headers=headers, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(output_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    f.write(chunk)
        return output_path.exists() and output_path.stat().st_size > 0
    except Exception as e:
        logger.error(f"Download video failed from {video_url}: {e}")
        return False


def format_reels_caption(original_title: str, line_oa_url: str = "https://lin.ee/NeVVWUU") -> str:
    """
    Format title for Facebook Reels with Gemini AI copywriting & LINE OA CTA.
    """
    gemini_key = os.getenv("GEMINI_API_KEY")
    if gemini_key:
        try:
            import google.generativeai as genai
            genai.configure(api_key=gemini_key)
            prompt = (
                "คุณคือนักเขียนคอนเทนต์และก๊อปปี้ไรท์เตอร์มืออาชีพของร้าน 'ONESTUDIO การ์ดแต่งงานและของชำร่วย'\n"
                f"จงนำหัวข้อคลิปจาก TikTok นี้: '{original_title}' มาแต่งเป็นแคปชั่น Facebook Reels ที่น่าสนใจเพื่อเพิ่มยอดการเข้าถึงและยอดสั่งซื้อ\n\n"
                "โครงสร้างที่ต้องมี:\n"
                "1. พาดหัวเปิดตัวดึงดูดสายตาคนกำลังจะแต่งงาน (พร้อมใส่อิโมจิสวยงาม)\n"
                "2. เล่าจุดเด่นของผลงาน ความประณีต โทนสี หรือความคุ้มค่า (อ่านง่าย สบายตา เว้นบรรทัดสวยงาม)\n"
                "3. ประโยคชวนคอมเมนต์สร้างปฏิสัมพันธ์ (Engagement Hook เช่น 'เจ้าสาวชอบโทนสีนี้ไหมคะ?')\n"
                "4. ข้อความปิดการขาย:\n"
                "───────────────────\n"
                "💍 ONESTUDIO - การ์ดแต่งงาน & ของชำร่วย ดีไซน์สวย พรีเมียม\n"
                "💌 ปรึกษาแบบ / เช็คคิวงาน / สอบถามราคา:\n"
                "👉 LINE OA: @795ncmnv (มี @ ด้วยนะคะ)\n"
                f"👉 คลิกลิงก์ทักแชท: {line_oa_url}\n"
                "5. แฮชแท็ก 5-7 อัน เช่น #การ์ดแต่งงาน #ของชำร่วยงานแต่ง #ของชำร่วยมินิมอล #เตรียมงานแต่ง #การ์ดมงคลสมรส #ONESTUDIO\n\n"
                "หมายเหตุ: ตอบกลับมาเฉพาะข้อความแคปชั่นภาษาไทย ไม่ต้องมีคำอธิบายอื่น"
            )
            for model_name in ["gemini-1.5-flash", "gemini-1.5-pro", "gemini-pro"]:
                try:
                    m = genai.GenerativeModel(model_name)
                    res = m.generate_content(prompt)
                    ai_text = (res.text or "").strip()
                    if ai_text and len(ai_text) > 30:
                        logger.info(f"Generated viral AI caption with {model_name} successfully!")
                        return ai_text
                except Exception as me:
                    logger.warning(f"Model {model_name} error: {me}")
                    continue
        except Exception as e:
            logger.warning(f"AI caption generation fallback: {e}")

    # Fallback to standard formatted caption
    caption = original_title.strip()
    cta = DEFAULT_LINE_CTA.format(line_url=line_oa_url)
    return f"{caption}{cta}"


def publish_facebook_reel(
    page_id: str,
    access_token: str,
    video_file_path: Path,
    caption: str
) -> Tuple[bool, str]:
    """
    Publish a video as Facebook Reels to Facebook Page via Meta Graph API.
    Flow:
    1. Start Reel Upload Session
    2. Upload video binary
    3. Finish Reel Publish
    """
    if not page_id or not access_token:
        return False, "Missing FB_PAGE_ID or FB_PAGE_ACCESS_TOKEN"

    file_size = video_file_path.stat().st_size
    api_version = "v19.0"

    # Step 1: Initialize Reel Upload
    init_url = f"https://graph.facebook.com/{api_version}/{page_id}/video_reels"
    init_payload = {
        "upload_phase": "start",
        "access_token": access_token
    }

    try:
        init_res = requests.post(init_url, data=init_payload, timeout=30).json()
        video_id = init_res.get("video_id")
        upload_url = init_res.get("upload_url")

        if not video_id:
            # Fallback to standard Page Video upload if video_reels init fails
            logger.warning(f"Reels init failed ({init_res}), trying standard video upload...")
            return publish_facebook_standard_video(page_id, access_token, video_file_path, caption)

        logger.info(f"Reel upload started. Video ID: {video_id}")

        # Step 2: Upload Video File Binary to upload_url (or rupload)
        headers = {
            "Authorization": f"OAuth {access_token}",
            "offset": "0",
            "file_size": str(file_size)
        }
        with open(video_file_path, "rb") as video_file:
            upload_target = upload_url if upload_url else f"https://rupload.facebook.com/video-upload/{api_version}/{video_id}"
            upload_res = requests.post(
                upload_target,
                headers=headers,
                data=video_file,
                timeout=180
            ).json()

        logger.info(f"Upload chunk response: {upload_res}")

        # Step 3: Finish and Publish Reel
        finish_url = f"https://graph.facebook.com/{api_version}/{page_id}/video_reels"
        finish_payload = {
            "upload_phase": "finish",
            "access_token": access_token,
            "video_id": video_id,
            "video_state": "PUBLISHED",
            "description": caption
        }

        finish_res = requests.post(finish_url, data=finish_payload, timeout=30).json()
        if finish_res.get("success") is True or "video_id" in finish_res:
            logger.info(f"Successfully published Reel to Facebook! Video ID: {video_id}")
            return True, video_id
        else:
            return False, f"Finish error: {finish_res}"

    except Exception as e:
        logger.error(f"Error publishing Facebook Reel: {e}")
        return False, str(e)


def publish_facebook_standard_video(
    page_id: str,
    access_token: str,
    video_file_path: Path,
    caption: str
) -> Tuple[bool, str]:
    """Fallback standard video upload if Reels API endpoint needs standard permission."""
    api_version = "v19.0"
    url = f"https://graph.facebook.com/{api_version}/{page_id}/videos"
    data = {
        "access_token": access_token,
        "description": caption
    }
    try:
        with open(video_file_path, "rb") as f:
            files = {"source": f}
            res = requests.post(url, data=data, files=files, timeout=180).json()

        if "id" in res:
            logger.info(f"Published standard Facebook video with ID: {res['id']}")
            return True, res["id"]
        return False, f"Upload error: {res}"
    except Exception as e:
        logger.error(f"Standard video upload failed: {e}")
        return False, str(e)


def sync_latest_tiktok_to_facebook(
    tiktok_username: Optional[str] = None,
    specific_video_url: Optional[str] = None,
    max_videos: int = 1
) -> Dict:
    """
    Main orchestrator function:
    1. Finds unposted videos from TikTok (without watermark)
    2. Downloads to temp
    3. Posts to Facebook Page as Reels
    4. Records state & deletes temp file
    """
    username = tiktok_username or os.getenv("TIKTOK_USERNAME", "")
    page_id = os.getenv("FB_PAGE_ID", "")
    page_token = os.getenv("FB_PAGE_ACCESS_TOKEN", "")
    line_oa_url = os.getenv("LINE_OA_LINK", "https://lin.ee/xxxx")

    result = {
        "success": False,
        "synced_count": 0,
        "details": [],
        "errors": []
    }

    if not page_id or not page_token:
        result["errors"].append("ยังไม่ได้ตั้งค่า FB_PAGE_ID หรือ FB_PAGE_ACCESS_TOKEN ใน .env")
        return result

    synced_ids = load_synced_ids()
    videos_to_process = []

    # Case 1: Specific URL provided
    if specific_video_url:
        logger.info(f"Fetching specific video: {specific_video_url}")
        v = get_single_tiktok_video(specific_video_url)
        if v:
            videos_to_process.append(v)
        else:
            result["errors"].append(f"ไม่สามารถดึงข้อมูลคลิปจาก {specific_video_url}")
            return result
    # Case 2: Auto-detect from TikTok username
    elif username:
        logger.info(f"Fetching latest videos for TikTok @{username}")
        user_videos = get_tiktok_user_videos(username, count=max_videos + 3)
        for v in user_videos:
            if v["id"] not in synced_ids:
                videos_to_process.append(v)
                if len(videos_to_process) >= max_videos:
                    break
    else:
        result["errors"].append("กรุณาระบุ TikTok Username หรือ URL คลิป")
        return result

    if not videos_to_process:
        result["success"] = True
        result["message"] = "ไม่มีคลิปใหม่ที่ยังไม่ได้โพสต์ (คลิปทั้งหมดซิงค์เรียบร้อยแล้ว)"
        return result

    for v in videos_to_process:
        v_id = v["id"]
        v_title = v.get("title", "ONESTUDIO การ์ดแต่งงานและของชำร่วย")
        download_url = v.get("hdplay_url") or v.get("play_url")

        if not download_url:
            result["errors"].append(f"ไม่พบคลิปแบบไม่มีลายน้ำสำหรับ ID: {v_id}")
            continue

        temp_video_path = TEMP_DIR / f"tiktok_{v_id}.mp4"
        logger.info(f"Downloading TikTok video {v_id} without watermark...")

        download_ok = download_video(download_url, temp_video_path)
        if not download_ok:
            result["errors"].append(f"ดาวน์โหลดคลิป {v_id} ล้มเหลว")
            continue

        caption = format_reels_caption(v_title, line_oa_url=line_oa_url)

        logger.info(f"Publishing to Facebook Page Reels ({page_id})...")
        pub_ok, pub_info = publish_facebook_reel(page_id, page_token, temp_video_path, caption)

        # Cleanup local file immediately
        try:
            if temp_video_path.exists():
                temp_video_path.unlink()
        except Exception:
            pass

        if pub_ok:
            save_synced_id(v_id, title=v_title, fb_post_id=pub_info)
            result["synced_count"] += 1
            result["details"].append({
                "tiktok_id": v_id,
                "title": v_title,
                "fb_post_id": pub_info,
                "status": "published"
            })
        else:
            result["errors"].append(f"โพสต์คลิป {v_id} ไปยัง Facebook ไม่สำเร็จ: {pub_info}")

    result["success"] = result["synced_count"] > 0 or len(result["errors"]) == 0
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="TikTok to Facebook Reels Auto-Sync")
    parser.add_argument("--user", type=str, help="TikTok username (without @)")
    parser.add_argument("--url", type=str, help="Single TikTok video URL to sync")
    parser.add_argument("--max", type=int, default=1, help="Max videos to sync")
    args = parser.parse_args()

    print("🚀 Starting TikTok to Facebook Reels sync test...")
    res = sync_latest_tiktok_to_facebook(
        tiktok_username=args.user,
        specific_video_url=args.url,
        max_videos=args.max
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))

