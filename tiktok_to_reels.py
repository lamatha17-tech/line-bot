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
import subprocess
import shutil
import base64
import time
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

STATE_FILE = Path("/tmp/synced_reels.json") if os.getenv("VERCEL") else Path(__file__).parent / "synced_reels.json"
TEMP_DIR = Path("/tmp/temp_reels") if os.getenv("VERCEL") else Path(__file__).parent / "temp_reels"
try:
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    pass

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


def save_synced_id(video_id: str, title: str = "", fb_post_id: str = "", cover: str = "", duration: int = 0):
    """Save newly posted video ID to history."""
    try:
        from datetime import datetime
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
                "title": title or f"TikTok Video {video_id}",
                "cover": cover,
                "duration": duration,
                "fb_post_id": fb_post_id,
                "tiktok_url": f"https://www.tiktok.com/@_one_19/video/{video_id}" if str(video_id).isdigit() else "",
                "synced_at": datetime.now().strftime("%d/%m/%Y %H:%M")
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


def audit_and_enhance_video(input_path: Path, output_path: Optional[Path] = None) -> Tuple[Path, Dict]:
    """
    ตรวจสอบคุณภาพคลิปและปรับแต่งให้ได้มาตรฐานสูงสุดสำหรับ Facebook Reels:
    1. ตรวจสอบความยาว (Reels สูงสุด 90 วินาที)
    2. ตรวจสอบสัดส่วน (สัดส่วนแนวตั้ง 9:16 ขนาด 1080x1920) หากไม่ใช่แนวตั้ง ปรับเบลอพื้นหลังให้อัตโนมัติ
    3. ปรับสมดุลเสียง (Audio Normalization) ให้เสียงพูด/ดนตรีชัดเจนบนลำโพงมือถือ
    4. ใส่ Faststart flag (-movflags +faststart) เพื่อให้ Reels โหลดเล่นทันทีบนหน้าฟีด Facebook
    """
    ffmpeg_bin = shutil.which("ffmpeg") or "/usr/local/bin/ffmpeg"
    ffprobe_bin = shutil.which("ffprobe") or "/usr/local/bin/ffprobe"

    audit_report = {
        "original_file": str(input_path),
        "duration": 0,
        "width": 0,
        "height": 0,
        "aspect_ratio": "unknown",
        "audio_detected": False,
        "enhanced": False,
        "actions_taken": []
    }

    if not Path(ffmpeg_bin).exists() or not Path(ffprobe_bin).exists():
        logger.warning("FFmpeg/FFprobe not found. Skipping video enhancement.")
        return input_path, audit_report

    try:
        # Step 1: Probe video info
        probe_cmd = [
            ffprobe_bin, "-v", "error",
            "-show_entries", "stream=width,height,duration,codec_type",
            "-show_entries", "format=duration",
            "-of", "json", str(input_path)
        ]
        res = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=15)
        if res.returncode == 0:
            meta = json.loads(res.stdout)
            streams = meta.get("streams", [])
            v_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
            a_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

            if v_stream:
                w = int(v_stream.get("width", 0))
                h = int(v_stream.get("height", 0))
                audit_report["width"] = w
                audit_report["height"] = h
                if w > 0 and h > 0:
                    ratio = w / h
                    if 0.5 <= ratio <= 0.65:
                        audit_report["aspect_ratio"] = "9:16 (Vertical Optimal)"
                    elif 0.9 <= ratio <= 1.1:
                        audit_report["aspect_ratio"] = "1:1 (Square)"
                    else:
                        audit_report["aspect_ratio"] = f"{w}:{h} (Horizontal/Other)"

            dur = 0
            if meta.get("format", {}).get("duration"):
                dur = float(meta["format"]["duration"])
            elif v_stream and v_stream.get("duration"):
                dur = float(v_stream["duration"])
            audit_report["duration"] = round(dur, 2)
            audit_report["audio_detected"] = a_stream is not None

        # Step 2: Determine if enhancement is needed
        out_file = output_path or (input_path.parent / f"enhanced_{input_path.name}")
        w = audit_report.get("width", 0)
        h = audit_report.get("height", 0)
        dur = audit_report.get("duration", 0)

        vf_filters = []
        af_filters = []

        # Reels vertical format check: If not 9:16 vertical, format it
        if w > 0 and h > 0:
            ratio = w / h
            if ratio > 0.65:
                # Create blurred background 1080x1920 with centered foreground video
                vf_filters.append(
                    "split[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:20[bg];[b]scale=1080:1920:force_original_aspect_ratio=decrease[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2"
                )
                audit_report["actions_taken"].append("ปรับสัดส่วนเป็น 9:16 แนวตั้ง (ใส่พื้นหลังเบลอสวยงาม)")
            elif w != 1080 or h != 1920:
                vf_filters.append("scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2")
                audit_report["actions_taken"].append("ปรับความละเอียดคมชัด 1080x1920 Full HD")

        # Audio normalization: Boost quiet audio
        if audit_report["audio_detected"]:
            af_filters.append("loudnorm=I=-16:TP=-1.5:LRA=11")
            audit_report["actions_taken"].append("ปรับสมดุลเสียงให้ดังชัดเจน ไร้เสียงแตก (Audio Normalization)")

        # Faststart + H264 encode
        cmd = [ffmpeg_bin, "-y", "-i", str(input_path)]

        if dur > 90:
            cmd.extend(["-t", "90"])
            audit_report["actions_taken"].append("ตัดคลิปให้ไม่เกิน 90 วินาทีตามเกณฑ์ Reels")

        if vf_filters:
            cmd.extend(["-filter:v", ",".join(vf_filters)])

        if af_filters:
            cmd.extend(["-filter:a", ",".join(af_filters)])

        cmd.extend([
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "22",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            str(out_file)
        ])

        logger.info(f"Running video enhancement: {' '.join(cmd[:6])}...")
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        if p.returncode == 0 and out_file.exists() and out_file.stat().st_size > 0:
            audit_report["enhanced"] = True
            audit_report["final_size_kb"] = round(out_file.stat().st_size / 1024, 1)
            logger.info(f"Video enhancement successful: {audit_report['actions_taken']}")
            return out_file, audit_report
        else:
            logger.warning(f"FFmpeg failed with code {p.returncode}: {p.stderr[-200:] if p.stderr else ''}")
            return input_path, audit_report
    except Exception as e:
        logger.error(f"Error enhancing video: {e}")
        return input_path, audit_report


def extract_video_keyframes(video_path: Path, num_frames: int = 3) -> List[dict]:
    """
    สกัดภาพ Keyframes คมชัดจากคลิปวิดีโอ (เช่น ที่ช่วง 25%, 50%, 75% ของคลิป)
    เพื่อให้ Gemini AI 'ดูภาพสินค้าจริงในคลิป' ก่อนคิดแคปชั่นและแฮชแท็ก
    """
    ffmpeg_bin = shutil.which("ffmpeg") or "/usr/local/bin/ffmpeg"
    ffprobe_bin = shutil.which("ffprobe") or "/usr/local/bin/ffprobe"
    if not Path(ffmpeg_bin).exists() or not Path(ffprobe_bin).exists() or not video_path.exists():
        return []

    keyframes = []
    try:
        probe_cmd = [
            ffprobe_bin, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json", str(video_path)
        ]
        res = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=10)
        dur = 5.0
        if res.returncode == 0:
            dur = float(json.loads(res.stdout).get("format", {}).get("duration", 5.0))

        timestamps = [dur * 0.25, dur * 0.50, dur * 0.75]
        for i, ts in enumerate(timestamps[:num_frames]):
            frame_path = TEMP_DIR / f"frame_{int(time.time())}_{i}.jpg"
            cmd = [
                ffmpeg_bin, "-y",
                "-ss", str(round(ts, 2)),
                "-i", str(video_path),
                "-vframes", "1",
                "-q:v", "2",
                str(frame_path)
            ]
            subprocess.run(cmd, capture_output=True, timeout=10)
            if frame_path.exists() and frame_path.stat().st_size > 0:
                with open(frame_path, "rb") as f:
                    b64_data = base64.b64encode(f.read()).decode("utf-8")
                keyframes.append({
                    "mime_type": "image/jpeg",
                    "data": b64_data
                })
                try:
                    frame_path.unlink()
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"Keyframe extraction failed: {e}")
    return keyframes


def format_reels_caption(
    original_title: str,
    video_path: Optional[Path] = None,
    line_oa_url: str = "https://lin.ee/NeVVWUU"
) -> str:
    """
    วิเคราะห์คลิปจริง และคิดแคปชั่น+แฮชแท็กใหม่แบบไวรัลทุกครั้งด้วย Gemini AI:
    1. ดึง Keyframes ภาพจริงจากคลิป ให้ AI วิเคราะห์สินค้า, โทนสี, ดีเทล และลวดลาย
    2. พาดหัวหยุดนิ้ว (Hook 3 วินาทีแรก) ที่ตรงกับภาพที่เห็นในคลิปเป๊ะๆ
    3. บรรยายจุดเด่นและความประณีตของสินค้าที่โชว์ในคลิป
    4. คำถามชวนคอมเมนต์สร้างปฏิสัมพันธ์ (Engagement Boost)
    5. ปิดการขายพร้อมช่องทางติดต่อ LINE OA: @795ncmnv
    6. ชุดแฮชแท็ก 7-10 อัน ที่เจาะจงสินค้าในคลิปและติดเทรนด์งานแต่ง
    """
    gemini_key = os.getenv("GEMINI_API_KEY")
    if gemini_key:
        try:
            import google.generativeai as genai
            genai.configure(api_key=gemini_key)

            # สกัดภาพ Keyframes จากคลิปจริง (ถ้ามีไฟล์)
            keyframes = []
            if video_path and video_path.exists():
                keyframes = extract_video_keyframes(video_path, num_frames=3)
                if keyframes:
                    logger.info(f"Extracted {len(keyframes)} keyframes from video for Gemini Vision analysis!")

            prompt = (
                "คุณคือนักเขียนคอนเทนต์และก๊อปปี้ไรท์เตอร์มือทองอันดับหนึ่งของร้าน 'ONESTUDIO การ์ดแต่งงานและของชำร่วย'\n"
            )
            if keyframes:
                prompt += (
                    "**คำสั่งพิเศษ: จงดูและวิเคราะห์ภาพจริงจากคลิปวิดีโอ (Keyframes) ที่แนบมานี้อย่างละเอียด**\n"
                    "- วิเคราะห์สินค้าที่ปรากฏในคลิปจริงว่าเป็นอะไร (เช่น การ์ดแต่งงาน, ซองจดหมายยุโรป, ของชำร่วย, กล่องของขวัญ, เทียนหอม, แท็กห้อย ฯลฯ)\n"
                    "- วิเคราะห์โทนสีและสไตล์ที่เห็นในคลิป (เช่น เขียวโอลีฟ, ชมพูพีช, โทนเอิร์ธโทน, ฟอยล์ทองหรูหรา, มินิมอลเรียบหรู ฯลฯ)\n"
                    "- ดึงจุดเด่น ความประณีต และเทกเจอร์ที่เห็นในภาพมาเป็นจุดขาย\n\n"
                )

            prompt += (
                f"หัวข้อเดิมของคลิป: '{original_title}'\n\n"
                "จงแต่งแคปชั่น Facebook Reels ที่น่าสนใจเพื่อดึงดูดกลุ่มว่าที่บ่าวสาว ยอดวิว ยอดแชร์ และยอดสั่งซื้อสูงสุด\n\n"
                "โครงสร้างแคปชั่นที่ต้องมีให้ครบถ้วน:\n"
                "1. บรรทัดแรก: พาดหัวสะดุดตาหยุดนิ้วคนดู (Hook) ด้วยอิโมจิสวยงาม อิงจากสินค้าและโทนสีที่เห็นจริงในคลิป\n"
                "2. เนื้อหา: บรรยายความประณีต โทนสี หรือจุดเด่นของงานให้อ่านง่าย สบายตา เว้นวรรคสวยงาม\n"
                "3. ประโยคชวนคุย/สร้างปฏิสัมพันธ์ (Engagement Boost เช่น 'เจ้าสาวชอบโทนสีนี้ไหมคะ? คอมเมนต์บอกกันหน่อยน้า 💕')\n"
                "4. ข้อความปิดการขาย (Call to Action):\n"
                "───────────────────\n"
                "💍 ONESTUDIO - การ์ดแต่งงาน & ของชำร่วย ดีไซน์สวย พรีเมียม\n"
                "💌 ปรึกษาแบบ / เช็คคิวงาน / สอบถามราคา:\n"
                "👉 LINE OA: @795ncmnv (มี @ ด้วยนะคะ)\n"
                f"👉 คลิกลิงก์ทักแชท: {line_oa_url}\n"
                "5. แฮชแท็กติดเทรนด์ 7-10 อัน ที่เจาะจงสินค้าและโทนสีในคลิป (เช่น #การ์ดแต่งงาน #ของชำร่วยงานแต่ง #เตรียมงานแต่ง #การ์ดมงคลสมรส #ไอเดียงานแต่ง #ONESTUDIO)\n\n"
                "หมายเหตุ: ตอบกลับมาเฉพาะข้อความแคปชั่นภาษาไทยพร้อมแฮชแท็กเท่านั้น ไม่ต้องมีคำอธิบายอื่น"
            )

            contents = [*keyframes, prompt] if keyframes else [prompt]

            for model_name in ["gemini-2.5-flash", "gemini-3.5-flash-lite", "gemini-flash-latest", "gemini-3.1-pro-preview"]:
                try:
                    m = genai.GenerativeModel(model_name)
                    res = m.generate_content(contents)
                    ai_text = (res.text or "").strip()
                    if ai_text and len(ai_text) > 30:
                        logger.info(f"Generated visual-aware viral AI caption with {model_name} successfully!")
                        return ai_text
                except Exception as me:
                    logger.warning(f"Model {model_name} error: {me}")
                    continue
        except Exception as e:
            logger.warning(f"AI caption generation fallback: {e}")

    # Fallback to standard formatted caption with full hashtags
    caption = original_title.strip()
    cta = DEFAULT_LINE_CTA.format(line_url=line_oa_url)
    hashtags = "\n\n#การ์ดแต่งงาน #ของชำร่วยงานแต่ง #ของชำร่วยมินิมอล #เตรียมงานแต่ง #ไอเดียงานแต่ง #การ์ดมงคลสมรส #ONESTUDIO"
    return f"{caption}{cta}{hashtags}"


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

        # Step 2.5: ตรวจสอบคุณภาพคลิป & ปรับแต่งให้เหมาะกับ Facebook Reels ที่สุด (FFmpeg)
        logger.info(f"Auditing and enhancing video {v_id} for optimal Facebook Reels reach...")
        final_video_path, audit_report = audit_and_enhance_video(temp_video_path)

        # Step 2.6: วิเคราะห์คลิปจริง และคิดแคปชั่น+แฮชแท็กใหม่แบบไวรัลทุกครั้งด้วย Gemini AI (Vision + NLP)
        logger.info(f"Analyzing actual video frames and generating viral AI caption with Gemini...")
        caption = format_reels_caption(v_title, video_path=final_video_path, line_oa_url=line_oa_url)

        logger.info(f"Publishing to Facebook Page Reels ({page_id})...")
        pub_ok, pub_info = publish_facebook_reel(page_id, page_token, final_video_path, caption)

        # Cleanup local files immediately
        for f_path in [temp_video_path, final_video_path]:
            try:
                if f_path.exists():
                    f_path.unlink()
            except Exception:
                pass

        if pub_ok:
            save_synced_id(v_id, title=v_title, fb_post_id=pub_info, cover=v.get("cover", ""), duration=v.get("duration", 0))
            result["synced_count"] += 1
            result["details"].append({
                "tiktok_id": v_id,
                "title": v_title,
                "fb_post_id": pub_info,
                "caption": caption,
                "audit": audit_report,
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

