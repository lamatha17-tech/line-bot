import os
import sys
import logging
import re
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageSendMessage
import google.generativeai as genai
from dotenv import load_dotenv

# โหลดค่าตัวแปรสภาพแวดล้อมจากไฟล์ .env
load_dotenv()

# ตั้งค่า Logging เพื่อใช้ตรวจสอบการทำงาน
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ดึงค่า API Keys จาก Environment Variables
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET or not GEMINI_API_KEY:
    logger.error("กรุณาตั้งค่า LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET และ GEMINI_API_KEY ในไฟล์ .env")
    sys.exit(1)

# ตั้งค่า Line Bot API และ Webhook Handler
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ตั้งค่า Google Gemini API
genai.configure(api_key=GEMINI_API_KEY)

# คำสั่งระบบ (System Prompt) เพื่อกำหนดบทบาทและข้อมูลสินค้าให้ AI
# คุณสามารถแก้ไขข้อมูลราคาสินค้าในส่วนนี้ได้ตามต้องการ
system_instruction = """
คุณคือแอดมินตอบแชท Line OA ของร้านขายการ์ดงานแต่งงานและของชำร่วย (ONESTUDIO)
หน้าที่ของคุณคือตอบคำถามลูกค้าอย่างสุภาพ เป็นกันเอง (ลงท้ายด้วย ค่ะ/ครับ) และให้ข้อมูลราคาที่ถูกต้อง

--- โปรโมชั่นราคาการ์ดแต่งงาน (ขนาด 4x6 นิ้ว, พิมพ์บนกระดาษอาร์ตการ์ด 210 แกรม) ---
- แบบพิมพ์หน้าเดียว ราคา 3.3 บาท/ใบ
- แบบพิมพ์สองหน้า ราคา 5 บาท/ใบ
- ฟรี! ซองสีชมพู และสีฟ้า
- (ตัวเลือกเสริม) ซองสีครีม บวกเพิ่ม 1.5 บาท/ใบ, ซองสีเขียว บวกเพิ่ม 2 บาท/ใบ
*หากลูกค้าสนใจการ์ดขนาดอื่น ให้แจ้งว่าสามารถทำได้และให้รอแอดมินมาเช็คราคาให้ค่ะ

--- ข้อมูลราคาสินค้าของชำร่วย (ทุกแบบสั่งขั้นต่ำ 50 ชิ้น, ออกแบบให้ฟรี, และสามารถแจ้งโทนสีให้เข้ากับธีมงานของการ์ดได้) ---

1. พวงกุญแจหนังแบบแบน
- 100 ชิ้นขึ้นไป ราคา 9 บาท/ชิ้น
- 300 ชิ้นขึ้นไป ราคา 8.5 บาท/ชิ้น
- 500 ชิ้นขึ้นไป ราคา 8 บาท/ชิ้น

2. พวงกุญแจ PU ถัก
- 100 ชิ้นขึ้นไป ราคา 7 บาท/ชิ้น
- 300 ชิ้นขึ้นไป ราคา 6.5 บาท/ชิ้น
- 500 ชิ้นขึ้นไป ราคา 6 บาท/ชิ้น

3. น้ำหอม (ขวดจิ๋ว)
- 100 ชิ้นขึ้นไป ราคา 8 บาท/ชิ้น
- 300 ชิ้นขึ้นไป ราคา 7 บาท/ชิ้น
- 500 ชิ้นขึ้นไป ราคา 6 บาท/ชิ้น

4. ดินสอไม้ มินิมอล
- 100 ชิ้นขึ้นไป ราคา 6 บาท/ชิ้น
- 300 ชิ้นขึ้นไป ราคา 5.5 บาท/ชิ้น
- 500 ชิ้นขึ้นไป ราคา 5 บาท/ชิ้น

5. กระจกวิเศษ (กลม)
- 100 ชิ้นขึ้นไป ราคา 7 บาท/ชิ้น
- 300 ชิ้นขึ้นไป ราคา 6 บาท/ชิ้น
- 500 ชิ้นขึ้นไป ราคา 5.8 บาท/ชิ้น

หากลูกค้าถามถึงสินค้าที่ไม่มีในรายการ หรือถามเรื่องรายละเอียดแพ็คเกจเพิ่มเติม ให้ตอบว่า "รอสักครู่นะคะ เดี๋ยวแอดมินมาเช็ครายละเอียดให้เพิ่มเติมค่ะ"
- หากลูกค้าถามหา "ราคาการ์ด" หรือ "ขอดูแบบการ์ดแต่งงาน" ให้ตอบข้อมูลเบื้องต้นและแนบลิงก์นี้: https://drive.google.com/file/d/1LwOI8PZ8CAngJP9q-ZIPBkpCb7NvDYjp/view?usp=drive_link
- หากลูกค้าถามหา "ราคาของชำร่วย" หรือ "แคตตาล็อกของชำร่วยแบบอื่นๆ" ให้ตอบข้อมูลเบื้องต้นและแนบลิงก์โฟลเดอร์นี้: https://drive.google.com/drive/folders/1_bBAYU1n_TaK5eJdp_Q1rjBfhWxKPwCV?usp=drive_link
"""

# ใช้ model gemini-1.5-flash พร้อมกับส่ง System Instruction เข้าไป
model = genai.GenerativeModel(
    'gemini-2.5-flash',
    system_instruction=system_instruction
)

# สร้าง FastAPI App
app = FastAPI(title="Line OA Gemini Agent")

@app.get("/")
def read_root():
    return {"message": "Hello from Line OA Gemini Agent!"}

@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Endpoint สำหรับรับ Webhook จาก Line
    """
    # ตรวจสอบ Signature จาก Header
    signature = request.headers.get("x-line-signature", "")
    
    # อ่าน body ของ Request
    body = await request.body()
    body_str = body.decode("utf-8")
    
    # ใช้ BackgroundTasks เพื่อตอบกลับ HTTP 200 อย่างรวดเร็วก่อนประมวลผลข้อความ
    # ป้องกัน Line Webhook Timeout (Line ต้องการการตอบกลับภายในไม่กี่วินาที)
    background_tasks.add_task(handle_line_webhook, body_str, signature)
    
    return {"status": "ok"}

def handle_line_webhook(body: str, signature: str):
    """
    ฟังก์ชันสำหรับให้ WebhookHandler ประมวลผล Signature และแจกจ่าย Event
    """
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logger.error("Invalid signature. ตรวจสอบ LINE_CHANNEL_SECRET อีกครั้ง")
    except Exception as e:
        logger.error(f"Error handling webhook: {e}")

@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    """
    ฟังก์ชันสำหรับจัดการ Event ประเภทข้อความ (Text) ที่ส่งมาจากผู้ใช้
    """
    user_text = event.message.text
    reply_token = event.reply_token
    user_id = event.source.user_id
    
    logger.info(f"Received message from user {user_id}: {user_text}")
    
    try:
        # ส่งข้อความไปประมวลผลที่ Gemini API
        response = model.generate_content(user_text)
        
        # ข้อความที่ได้จาก Gemini
        bot_reply = response.text
        
        # ค้นหาแท็ก [IMAGE: url] ด้วย Regex
        image_urls = re.findall(r'\[IMAGE:\s*(https?://[^\s\]]+)\]', bot_reply)
        
        # ลบแท็กออกจากข้อความที่จะส่งให้ผู้ใช้
        clean_text = re.sub(r'\[IMAGE:\s*https?://[^\s\]]+\]', '', bot_reply).strip()
        
        messages = []
        if clean_text:
            messages.append(TextSendMessage(text=clean_text))
            
        # สร้าง ImageSendMessage จาก URLs ที่หาเจอ (ส่งได้สูงสุด 4 รูป + 1 ข้อความ = 5 ข้อความตามลิมิตของ Line)
        for url in image_urls[:4]:
            messages.append(ImageSendMessage(
                original_content_url=url,
                preview_image_url=url
            ))
            
        if not messages:
            messages.append(TextSendMessage(text="..."))
            
        # ส่งข้อความและรูปภาพกลับไปยัง Line
        line_bot_api.reply_message(
            reply_token,
            messages
        )
        logger.info(f"Replied to user {user_id} with {len(messages)} messages.")
        
    except Exception as e:
        logger.error(f"Error generating content or replying: {e}")
        # กรณีเกิดข้อผิดพลาด ส่งข้อความขออภัย
        try:
            line_bot_api.reply_message(
                reply_token,
                TextSendMessage(text="ขออภัยค่ะ ระบบขัดข้องชั่วคราว กรุณาลองใหม่อีกครั้ง")
            )
        except Exception as reply_err:
            logger.error(f"Failed to send error message: {reply_err}")
