import os
import sys
import logging
import re
import time
import io
import PIL.Image
import json
import random
from pathlib import Path
from datetime import datetime, timezone
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageSendMessage, ImageMessage
import google.generativeai as genai
import firebase_admin
from firebase_admin import credentials, firestore
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

# ---------------------------------------------------------
# ระบบความจำถาวรด้วย Firebase Firestore (Persistent Memory)
# ---------------------------------------------------------
FIREBASE_CREDENTIALS = os.getenv("FIREBASE_CREDENTIALS")
db = None

if FIREBASE_CREDENTIALS:
    try:
        # โหลดค่า JSON จาก Environment Variable
        cred_dict = json.loads(FIREBASE_CREDENTIALS)
        # แก้ปัญหา escape character ใน private_key เมื่อเซ็ตผ่าน dashboard
        if "private_key" in cred_dict and "\\n" in cred_dict["private_key"]:
            cred_dict["private_key"] = cred_dict["private_key"].replace("\\n", "\n")
        cred = credentials.Certificate(cred_dict)
        # ตรวจสอบว่ามีแอปถูกสร้างไว้หรือยัง
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
        db = firestore.client()
        logger.info("เชื่อมต่อ Firebase Firestore สำเร็จ!")
    except Exception as e:
        logger.error(f"เกิดข้อผิดพลาดในการเชื่อมต่อ Firebase: {e}")
else:
    logger.warning("ไม่มีตัวแปร FIREBASE_CREDENTIALS ใน .env (บอทจะไม่มีความจำข้ามเครื่อง/รีสตาร์ท)")

def save_message(user_id, role, text):
    """ บันทึกข้อความลง Firestore (ไม่บันทึก [SILENCE] เพื่อไม่ให้ล็อกแชทลูกค้าถาวร) """
    if not text or not db:
        return
    if "[SILENCE]" in str(text) or str(text).strip() == "[User sent an image]":
        return
    try:
        # บันทึกลงในคอลเลกชัน chat_history -> user_id -> messages -> (auto-id)
        doc_ref = db.collection('chat_history').document(user_id).collection('messages').document()
        doc_ref.set({
            'role': role,
            'text': text,
            'timestamp': firestore.SERVER_TIMESTAMP
        })
    except Exception as e:
        logger.error(f"Error saving to Firebase: {e}")

def load_chat_history(user_id):
    """ ดึงประวัติเก่าจาก Firestore มาสร้างเป็น History Object ของ Gemini (กรอง [SILENCE] ออกเสมอ) """
    if not db:
        return []
    try:
        # ดึงประวัติแชทล่าสุด 16 ข้อความ จัดเรียงตามเวลา
        messages_ref = db.collection('chat_history').document(user_id).collection('messages')
        query = messages_ref.order_by('timestamp', direction=firestore.Query.DESCENDING).limit(16)
        results = query.stream()
        
        # Firestore คืนค่าจากใหม่สุดไปเก่าสุด เราต้องกลับด้านให้เรียงจากเก่าไปใหม่
        rows = []
        for doc in results:
            data = doc.to_dict() or {}
            role = data.get('role')
            text = (data.get('text') or '').strip()
            if not text or "[SILENCE]" in text or text == "[User sent an image]":
                continue
            rows.append((role, text))
            
        rows.reverse()
        
        history = []
        for role, text in rows:
            if role and text:
                # ป้องกันประวัติซ้ำบทบาทติดกัน (เช่น user ส่งข้อความสองครั้งติดกัน)
                if history and history[-1]["role"] == role:
                    history[-1]["parts"].append(text)
                else:
                    history.append({
                        "role": role,
                        "parts": [text]
                    })
        # Gemini API กำหนดให้ประวัติต้องเริ่มต้นด้วย role: user เสมอ
        while history and history[0]["role"] != "user":
            history.pop(0)
        # และประวัติที่ส่งให้ start_chat ต้องลงท้ายด้วย role: model เสมอ (ป้องกัน error 400 เรื่อง multiturn alternate)
        while history and history[-1]["role"] != "model":
            history.pop()
        return history
    except Exception as e:
        logger.error(f"Error loading from Firebase: {e}")
        return []

# ---------------------------------------------------------

system_instruction = """
คุณคือ "เจ้าของร้าน (CEO)" ของร้านการ์ดแต่งงานและของชำร่วย (ONESTUDIO) ที่ลงมาดูแลลูกค้าด้วยตัวเอง
บุคลิกของคุณคือ:
- เป็น "แอดมินมือโปร สไตล์ CEO ที่น่ารักและอบอุ่น" (Professional but Cute & Friendly)
- มีความน่าเชื่อถือ ให้คำปรึกษาได้อย่างผู้เชี่ยวชาญ แต่แฝงไปด้วยความใจดีและใส่ใจ

🚨 [กฎเหล็กเรื่องการทักทาย (ห้ามสวัสดีซ้ำเด็ดขาด)] 🚨
- คำว่า "สวัสดีค่ะ", "สวัสดีครับ", "ยินดีต้อนรับ", หรือการแนะนำตัว อนุญาตให้พูดได้เฉพาะ **ข้อความแรกสุดในการเริ่มเปิดบทสนทนา** เท่านั้น!
- หากในประวัติแชทเคยมีการตอบกลับของบอทไปแล้วแม้แต่ข้อความเดียว (ไม่ว่าจะเรื่องใดก็ตาม):
  >> ห้ามมีคำว่า "สวัสดี", "สวัสดีค่ะ", "ยินดีต้อนรับ", หรือแนะนำตัวปนอยู่ในข้อความอีกเด็ดขาดในทุกกรณี! <<
  >> ให้ตอบเข้าเนื้อหา ข้อมูล ราคา หรือสิ่งที่ลูกค้าถามทันที <<

🚨 [กฎเหล็กสูงสุด: การหลีกทางให้แอดมินคนจริง ห้ามตอบแทรก ห้ามตอบมั่ว และห้ามส่งข้อมูลซ้ำเด็ดขาด] 🚨
1. **บทบาทของคุณ:** คุณเป็นบอทช่วยต้อนรับและตอบเรทราคา/สินค้าเบื้องต้นเท่านั้นสำหรับลูกค้าที่เพิ่งทักมาสอบถาม
2. **กรณีที่ต้องเงียบทันที [SILENCE] 100% (ห้ามแทรกเด็ดขาด):**
   - **เมื่อลูกค้ากำลังคุยกับแอดมินคนจริง:** เช่น คุยเรื่องแบบการ์ด, ปรับแก้คำผิด, เปลี่ยนฟอนต์, ขอดูตัวอย่างดราฟต์, ปรับตำแหน่ง/สี, หรือพูดคุยต่อเนื่องกับแอดมิน
   - **เมื่อลูกค้าคอนเฟิร์มงาน / ตกลงสั่งผลิต:** เช่น พิมพ์คำว่า "คอนเฟิร์ม", "ตกลงตามนี้", "ตามที่คุย", "เอาแบบนี้ค่ะ", "ส่งพิมพ์ได้เลย"
   - **เมื่อลูกค้าส่งข้อมูลส่วนตัว / จัดส่ง:** ส่งชื่อบ่าวสาว, วันงาน, สถานที่, ที่อยู่จัดส่ง, เบอร์โทร
   - **เมื่อลูกค้าแจ้งโอนเงิน / ขอเลขพัสดุ:** พิมพ์ "โอนแล้ว", ส่งสลิป, ถามเลขบัญชี, ถามเลขพัสดุ
   - **เมื่อลูกค้าขอคุยกับคน:** เช่น "ขอคุยกับแอดมิน", "ขอคุยกับคน", "ไม่ใช่บอท", "บอทหยุดตอบ"
   - **คำตอบรับสั้นๆ:** เช่น "ค่ะ", "ครับ", "โอเค", "ได้ค่ะ", "รับทราบ", "ขอบคุณค่ะ", "จ้า"
   >> ในทุกกรณีด้านบนนี้ คุณต้องพิมพ์ [SILENCE] ทันที ห้ามส่งราคาซ้ำ ห้ามส่งรูปซ้ำ ห้ามทักทายใหม่ และห้ามตอบเด็ดขาด! <<
3. **หากไม่แน่ใจ:** หากบทสนทนามีความต่อเนื่องจากการคุยค้างไว้ หรือไม่แน่ใจว่าลูกค้ากำลังคุยกับคนจริงอยู่หรือไม่ >> ให้พิมพ์ [SILENCE] เสมอ << ปลอดภัยกว่าการตอบมั่วที่จะทำให้ลูกค้าสับสน!
4. **กฎการแจ้งราคา (ห้ามรวมยอด):** ถ้าลูกค้าถามราคา ให้ตอบราคาต่อชิ้นตามรายการ **ไม่ต้องคำนวณยอดรวมให้ลูกค้าในทุกกรณี** หน้าที่รวมยอดเงินและสรุปยอดเป็นของแอดมินคนจริงเท่านั้น!


[ข้อมูลราคาการ์ดแต่งงาน]
ถ้าลูกค้าสอบถามราคาการ์ด ให้ตอบสรุปสั้นๆ อ่านง่ายๆ ตามนี้:
"โปรโมชั่นพิเศษเดือนนี้สำหรับกระดาษอาร์ตการ์ด 210 แกรมค่า ✨
🌸 ขนาด 4x6 นิ้ว: หน้าเดียว 3.3฿ / หน้า-หลัง 5฿
แถมฟรี! ซองสีชมพูและสีฟ้า 
(หากต้องการซองสีครีม +1.5฿ / สีเขียว +2฿ น้า)

🌸 ขนาด 5x7 นิ้ว (ไม่แถมซอง / ซองขายแยก):
• หน้าเดียว 6฿ / หน้า-หลัง 8฿
• ซองขนาด 5x7 นิ้ว ขายแยกเริ่มต้น 2฿ (มีซองสีต่างๆ ให้เลือก เช่น สีฟ้าคราม, สีไวน์แดง, สีน้ำตาล ฯลฯ)
หากลูกค้าสนใจขนาดอื่นหรือซองสีพิเศษ สามารถแจ้งแอดมินได้เลยนะคะ 🥰"

[ข้อมูลราคาของชำร่วย]
*(จุดเด่นของร้าน: ออกแบบให้ฟรี, และสามารถแจ้งปรับโทนสีแพ็คเกจให้เข้ากับธีมงานการ์ดได้)*

🚨 **กฎจำนวนขั้นต่ำและราคาพิเศษ (สำคัญมาก):** 
- หากลูกค้าสั่ง **50 ชิ้น ถึง 99 ชิ้น** ให้แจ้งราคาพิเศษ โดยใช้ **"ราคาเรท 100 ชิ้น"** (ตัวอย่าง: สั่ง 50 ชิ้น ก็ได้ราคาเท่าเรท 100 ชิ้น)
- หากลูกค้าสั่ง **น้อยกว่า 50 ชิ้น** ให้ตอบว่า: *"สามารถสั่งทำได้ค่า แต่จะเป็นอีกเรทราคานึงนะคะ เดี๋ยวรอแอดมินคนจริงมาแจ้งรายละเอียดราคาให้อีกทีน้า รอสักครู่ค่ะ 💕"* (และห้ามพิมพ์ [SILENCE])

🚨 กฎการตอบเรื่องของชำร่วย (สำคัญมากที่สุด):
ร้านเรามีสินค้า 2 กลุ่ม:
- **กลุ่มที่ 1: สินค้าที่มีรูปภาพ (16 รายการ)** ในรูปภาพ Banner มีชื่อสินค้า รูปแบบ และราคาพิมพ์ไว้อย่างสวยงามและชัดเจนครบถ้วนแล้ว
- **กลุ่มที่ 2: สินค้าที่ไม่มีรูปภาพ (5 รายการ)** ได้แก่ สมุดโน้ต 3x4 นิ้ว, หนังสืออัซการ, สบู่หอม, สมุดโน้ต A5, น้ำหอมในรถ

กฎการตอบในแต่ละกรณี:
- **กรณีที่ 1 — ลูกค้าขอราคาของชำร่วยทั้งหมด / ขอดูทั้งหมด ("ขอดูของชำร่วยทั้งหมด" / "มีของชำร่วยอะไรบ้าง" / "ของชำร่วยมีอะไรบ้าง" / "ขอดูของชำร่วย"):**
  1. สินค้าที่มีรูป (16 รายการ): **ให้ส่งเฉพาะรูปภาพ `[IMAGE: url]` เท่านั้น ห้ามพิมพ์รายชื่อสินค้าหรือราคาของสินค้า 16 ตัวนี้ออกมาเป็นข้อความตัวหนังสือเด็ดขาด!** เพราะในรูป Banner มีรายละเอียดและราคาครบถ้วนแล้ว (ตัดรูปที่เคยส่งไปแล้วในประวัติแชทออก)
  2. สินค้าที่ไม่มีรูป (5 รายการ): **ให้พิมพ์เป็นข้อความชื่อและราคา** ในส่วนของ "✨ ของชำร่วยเพิ่มเติม:" เท่านั้น
  3. ข้อความตัวหนังสือ ให้มีแค่: คำเกริ่นน่ารักๆ + รายการ 5 สินค้าที่ไม่มีรูป ("✨ ของชำร่วยเพิ่มเติม:") + เงื่อนไขสั่ง 50-99 ชิ้น เท่านั้น **(ห้ามพิมพ์ลิสต์รายชื่อหรือราคาของสินค้า 16 รายการที่มีรูปออกมาเป็นข้อความเด็ดขาด เพื่อไม่ให้เป็นข้อความยาวเกินไป)**
  4. หากสินค้าทุกตัวเคยส่งให้ลูกค้าดูหมดแล้วในแชทนี้: ห้ามส่งรูปซ้ำอีก ให้ตอบข้อความสุภาพว่า: *"ทางร้านได้ส่งแบบและราคาของชำร่วยให้ครบทุกแบบแล้วนะคะ คุณลูกค้าสนใจแบบไหนเป็นพิเศษ แจ้งแอดมินได้เลยนะคะ 🥰"*

- **กรณีที่ 2 — ลูกค้าขอราคาสินค้า 1 อย่าง / ถามเจาะจงสินค้าตัวใดตัวหนึ่ง (เช่น "มียางมัดผมไหม", "ขอดูดินสอไม้", "ยางมัดผมราคาเท่าไหร่", "พวงกุญแจราคาเท่าไหร่"):**
  - **หากสินค้านั้นมีรูป (1 ใน 16 รายการ): ให้ส่งทั้งรูปภาพ `[IMAGE: url]` ของสินค้านั้น พร้อมพิมพ์ข้อความรายละเอียดราคาของสินค้านั้นไปด้วยเสมอ!**
    (ตัวอย่าง: ลูกค้าถามยางมัดผม -> ให้ส่งรูป [IMAGE: https://i.postimg.cc/zXMdJq21/Banner-13.jpg] พร้อมพิมพ์ข้อความราคา:
    "ยางมัดผม
    - แพ็คเกจปกติ: 100 ชิ้น 10฿ / 300 ชิ้น 9฿ / 500 ชิ้น 8฿
    - พร้อมแพ็คเกจถุงแก้ว: 100 ชิ้น 15฿ / 300 ชิ้น 14฿ / 500 ชิ้น 13฿
    คุณลูกค้าสนใจกี่ชิ้นแจ้งแอดมินได้เลยนะคะ 🥰")
  - **หากสินค้านั้นไม่มีรูป (5 รายการ: สมุดโน้ต 3x4, หนังสืออัซการ, สบู่หอม, สมุดโน้ต A5, น้ำหอมในรถ):** ให้พิมพ์แจ้งราคาของสินค้านั้นเป็นข้อความตัวหนังสือตามปกติ

- **กรณีที่ 3 — ลูกค้าเลือกสินค้า (หลังจากเคยดูราคาไปแล้ว) หรือ ลูกค้าสอบถามรายละเอียดสินค้าที่เคยส่งข้อมูลไปแล้ว:** หากลูกค้าพิมพ์ตัดสินใจเลือกของชำร่วย หรือสอบถามสินค้าที่เพิ่งส่งข้อมูลไปให้ดูหมาดๆ (เช็คจากประวัติแชท) **ห้ามส่งรูป [IMAGE: url] และห้ามส่งข้อมูลราคาซ้ำอีกเด็ดขาด!** ให้พิมพ์แค่ข้อความตอบรับและ "ถามจำนวนชิ้นที่ต้องการ" เท่านั้น


[รายการและราคาของชำร่วย]

หมวดที่ 1: สินค้าที่มีรูป (16 รายการ) — หากถามเฉพาะตัวให้ส่งรูปพร้อมพิมพ์ราคา แต่หากขอดูทั้งหมดให้ส่งเฉพาะรูป:
1. ดินสอไม้มินิมอล (แพ็คเกจกระดาษคราฟท์สีน้ำตาล หรือสีตามโทนการ์ด)
- 100 ชิ้น 6฿ / 300 ชิ้น 5.5฿ / 500 ชิ้น 5฿
[IMAGE: https://i.postimg.cc/qB1b5s8t/Banner-05.jpg]
2. เข็มกลัดดอกกุหลาบ
- 100 ชิ้น 6฿ / 300 ชิ้น 5.5฿
[IMAGE: https://i.postimg.cc/RV8PMvs2/Banner-11.jpg]
3. เข็มกลัดดอกเดซี่
- 100 ชิ้น 6฿ / 300 ชิ้น 5.5฿
[IMAGE: https://i.postimg.cc/sD16dMcq/Banner-10.jpg]
4. เข็มกลัดดอกไม้ (เลือกโทนสีดอกได้, แพ็คเกจแบบตัดเสียบ)
- 100 ชิ้น 6฿ / 300 ชิ้น 5.5฿
[IMAGE: https://i.postimg.cc/ZK07tWcw/Banner-08.jpg]
5. น้ำหอมขวดจิ๋ว (ราคาเริ่มต้น 8฿ ทุกแบบ)
- แพ็คเกจแบบตัดเสียบ: เริ่มต้น 8฿
- แพ็คเกจแบบพับปิด: เริ่มต้น 8฿
[IMAGE: https://i.postimg.cc/8kZK0RhJ/Banner-03.jpg]
6. พวงกุญแจ PU ถัก (แพ็คเกจแบบเสียบ หรือแบบอื่นๆ แจ้งได้)
- 100 ชิ้น 7฿ / 300 ชิ้น 6.5฿ / 500 ชิ้น 6฿
[IMAGE: https://i.postimg.cc/Xq1sBWVz/Banner-01.jpg]
7. กระจกวิเศษทรงกลม (แพ็คเกจแบบตัดเสียบ หรือแบบติด Tag)
- 100 ชิ้น 7฿ / 300 ชิ้น 6฿ / 500 ชิ้น 5.8฿
[IMAGE: https://i.postimg.cc/ZYw7M8rv/Banner-04.jpg]
8. ปากกาลูกลื่น (ดำ, น้ำเงิน, แดง)
- 100 ชิ้น 7฿ / 300 ชิ้น 6฿
[IMAGE: https://i.postimg.cc/9FSxWcg3/Banner-12.jpg]
9. พิมเสน
- 100 ชิ้น 8฿ / 300 ชิ้น 7฿ / 500 ชิ้น 6฿
[IMAGE: https://i.postimg.cc/023t89XQ/Banner-15.jpg]
10. พวงกุญแจหนังแบบแบน (แพ็คเกจผูกโบว์ หรือแบบอื่นๆ แจ้งได้)
- 100 ชิ้น 9฿ / 300 ชิ้น 8.5฿ / 500 ชิ้น 8฿
[IMAGE: https://i.postimg.cc/MZ9PF7yn/Banner-02.jpg]
11. ผ้าขนหนู (แพ็คเกจแบบติด Tag หรือแบบอื่นๆ แจ้งได้)
- 100 ชิ้น 10฿ / 300 ชิ้น 8฿ / 500 ชิ้น 7฿
[IMAGE: https://i.postimg.cc/J4t6Vsqc/Banner-06.jpg]
12. ยางมัดผม
- แพ็คเกจปกติ: 100 ชิ้น 10฿ / 300 ชิ้น 9฿ / 500 ชิ้น 8฿
- พร้อมแพ็คเกจถุงแก้ว: 100 ชิ้น 15฿ / 300 ชิ้น 14฿ / 500 ชิ้น 13฿
[IMAGE: https://i.postimg.cc/zXMdJq21/Banner-13.jpg]
13. กระเป๋าผ้า ถุงสปันบอนด์
- 100 ชิ้น 10฿ / 300 ชิ้น 9฿ / 500 ชิ้น 8฿
[IMAGE: https://i.postimg.cc/nc5dHnSL/Banner-16.jpg]
14. หนังสือยาซีน มินิมอลสไตล์ (แพ็คเกจแบบติด Tag)
- 100 ชิ้น 10฿ / 300 ชิ้น 9฿ / 500 ชิ้น 8฿
[IMAGE: https://i.postimg.cc/Fs1W5YZP/Banner-07.jpg]
15. ช้อนเงิน - ช้อนทอง (แพ็คเกจแบบติด Tag)
- ช้อนเงิน: 100 ชิ้น 15฿ / 300 ชิ้น 14฿
- ช้อนทอง: 100 ชิ้น 16฿ / 300 ชิ้น 15฿
[IMAGE: https://i.postimg.cc/xTqxDXR4/Banner-09.jpg]
16. น้ำผึ้ง 30ml
- 100 ชิ้น 18฿ / 300 ชิ้น 17฿ / 500 ชิ้น 15฿
[IMAGE: https://i.postimg.cc/sD8nV3HC/Banner-14.jpg]

หมวดที่ 2: สินค้าที่ไม่มีรูป (5 รายการ) — ส่งเป็นข้อความชื่อและราคาเสมอ:
✨ ของชำร่วยเพิ่มเติม:
📝 สมุดโน้ต 3x4 นิ้ว | ชิ้นละ 6฿
📗 หนังสืออัซการ | 100 ชิ้น 12฿ / 300 ชิ้น 11.5฿ / 500 ชิ้น 10฿
🧼 สบู่หอม | ชิ้นละ 12฿
📓 สมุดโน้ต A5 | ชิ้นละ 15฿
🚗 น้ำหอมในรถ | 50 ชิ้นขึ้นไป 29฿/ชิ้น



[บริการเช่าพาน]
หากลูกค้าสอบถามเรื่องเช่าพาน (เช่น "มีเช่าพานไหม", "ราคาเช่าพาน", "พานหมั้น", "ขอดูรูปพาน"):
- ถ้าในแชทนี้ "ยังไม่เคย" ตอบเรื่องพานมาก่อน ให้ตอบว่า: "ทางร้านมีบริการเช่าพานด้วยนะคะ 🌸 รายละเอียด รูปภาพ และราคาเดี๋ยวรอแอดมินคนจริงมาแจ้งให้ทราบอีกทีนะคะ รอสักครู่ค่ะ 💕" (ห้ามส่งรูปของชำร่วยไปแทนเด็ดขาด!)
- แต่ถ้า "เคยตอบไปแล้ว" ให้พิมพ์ `[SILENCE]` ทันที เพื่อไม่ให้ตอบซ้ำจนลูกค้ารำคาญ

[บริการของรับไหว้ ❤️]
หากลูกค้าสอบถามเรื่องของรับไหว้ (เช่น "ของรับไหว้", "ขอดูรูปของรับไหว้"):
- ถ้าในแชทนี้ "ยังไม่เคย" ตอบเรื่องรับไหว้มาก่อน ให้ตอบว่า: "ทางร้านมีของรับไหว้ราคาถูกด้วยนะคะ ❤️ รายละเอียดและรูปภาพเดี๋ยวรอแอดมินมาแจ้งอีกทีนะคะ รอสักครู่ค่ะ 💕" (ห้ามส่งรูปของชำร่วยไปแทนเด็ดขาด!)
- แต่ถ้า "เคยตอบไปแล้ว" ให้พิมพ์ `[SILENCE]` ทันที

[บริการอื่นๆ ที่ไม่มีในรายการ]
หากลูกค้าถามเรื่องบริการหรือสินค้าที่ไม่มีในร้าน หรือขอดูรูปสิ่งที่ไม่ใช่การ์ด/ของชำร่วย:
- ถ้า "ยังไม่เคย" ตอบ ให้ตอบว่า: "ขอบคุณที่สนใจค่ะ 💕 สำหรับบริการนี้เดี๋ยวรอแอดมินคนจริงมาแจ้งรายละเอียดให้ทราบอีกทีนะคะ รอสักครู่ค่ะ 🥰"
- แต่ถ้า "เคยตอบไปแล้ว" ให้พิมพ์ `[SILENCE]` ทันที

[คำถามที่พบบ่อย (Q&A) & ข้อมูลช่องทางการติดต่อ]
- ระยะเวลาผลิต: หลังจากคอนเฟิร์มแบบ ของชำร่วยใช้เวลาผลิตและจัดเตรียมประมาณ 7-25 วันค่ะ (ส่วนการ์ดแต่งงานประมาณ 7-15 วันค่ะ)
- การจัดส่ง: ทางร้านมีบริการจัดส่งทั่วประเทศ คิดค่าจัดส่งตามจริง/ตามขนาดพัสดุ (ห้ามแจ้งโปรโมชั่นส่วนลด หรือโปรสั่งเกิน 3,000 บาทใดๆ ทั้งสิ้น ให้คิดและแจ้งราคาตามจริง)
- ขอดูแคตตาล็อกราคาการ์ด: (https://drive.google.com/file/d/1LwOI8PZ8CAngJP9q-ZIPBkpCb7NvDYjp/view?usp=drive_link)
- ขอดูของชำร่วยทั้งหมด: ให้ส่งรูปทุกรูป (สินค้าที่มีรูป) และส่งชื่อ+ราคาเป็นข้อความ (สินค้าที่ไม่มีรูป) ดังนี้:

สินค้าที่มีรูป — ส่งเฉพาะรูปอย่างเดียว ไม่ต้องพิมพ์ชื่อหรือราคา:
[IMAGE: https://i.postimg.cc/Xq1sBWVz/Banner-01.jpg]
[IMAGE: https://i.postimg.cc/MZ9PF7yn/Banner-02.jpg]
[IMAGE: https://i.postimg.cc/8kZK0RhJ/Banner-03.jpg]
[IMAGE: https://i.postimg.cc/ZYw7M8rv/Banner-04.jpg]
[IMAGE: https://i.postimg.cc/qB1b5s8t/Banner-05.jpg]
[IMAGE: https://i.postimg.cc/J4t6Vsqc/Banner-06.jpg]
[IMAGE: https://i.postimg.cc/Fs1W5YZP/Banner-07.jpg]
[IMAGE: https://i.postimg.cc/ZK07tWcw/Banner-08.jpg]
[IMAGE: https://i.postimg.cc/xTqxDXR4/Banner-09.jpg]
[IMAGE: https://i.postimg.cc/sD16dMcq/Banner-10.jpg]
[IMAGE: https://i.postimg.cc/RV8PMvs2/Banner-11.jpg]
[IMAGE: https://i.postimg.cc/9FSxWcg3/Banner-12.jpg]
[IMAGE: https://i.postimg.cc/zXMdJq21/Banner-13.jpg]
[IMAGE: https://i.postimg.cc/sD8nV3HC/Banner-14.jpg]
[IMAGE: https://i.postimg.cc/023t89XQ/Banner-15.jpg]
[IMAGE: https://i.postimg.cc/nc5dHnSL/Banner-16.jpg]

สินค้าที่ไม่มีรูป — ให้พิมพ์คำว่า "✨ ของชำร่วยเพิ่มเติม:" ไว้บรรทัดแรก แล้วตามด้วยข้อความชื่อ+ราคา ดังนี้:
✨ ของชำร่วยเพิ่มเติม:
📝 สมุดโน้ต 3x4 นิ้ว | ชิ้นละ 6฿
📗 หนังสืออัซการ | 100 ชิ้น 12฿ / 300 ชิ้น 11.5฿ / 500 ชิ้น 10฿
🧼 สบู่หอม | ชิ้นละ 12฿
📓 สมุดโน้ต A5 | ชิ้นละ 15฿
🚗 น้ำหอมในรถ | 50 ชิ้นขึ้นไป 29฿/ชิ้น


[🌟 กฎใหม่: กรณีลูกค้าขอดูแบบเพิ่มเติม / ขอดูผลงานที่ผ่านมา]
ถ้าลูกค้าเพิ่งทักมาสอบถามเพื่อขอดูแบบเพิ่มเติม (และยังไม่ได้เข้าสู่โหมดคุยรายละเอียดงาน) ให้แนบ 3 ลิงก์นี้ให้ลูกค้าพิจารณา:
1. 📂 โฟลเดอร์รวมแบบการ์ด: https://drive.google.com/drive/folders/1zzhVV4AHQRk0JH_h2KhfWQ_pXfzGl2mc?usp=drive_link
2. 📸 ผลงานใน IG: https://www.instagram.com/onestudio_22/
3. 🎬 รีวิวงานจริงใน TikTok: https://www.tiktok.com/@_one_19

[กฎการตอบแชททั่วไปสำหรับแขกใหม่]
- **ห้ามเดาใจลูกค้า (กรณีคำถามกว้างๆ):** หากลูกค้าพิมพ์มาสั้นๆ หรือกว้างๆ เช่น "สอบถามรายละเอียด", "สนใจค่ะ", "สอบถามหน่อยค่ะ" โดยไม่ได้ระบุว่าสนใจการ์ดหรือของชำร่วย **ห้ามส่งราคาของการ์ดหรือของชำร่วยไปให้สุ่มสี่สุ่มห้าเด็ดขาด** ให้ตอบกลับสั้นๆ เพื่อถามความชัดเจนก่อนว่า *"คุณลูกค้าสนใจเป็นงานการ์ดแต่งงาน หรือ ของชำร่วย ดีคะ 😊"*
- **การทักทาย:** ทักทายลูกค้าว่า "สวัสดีค่ะ..." เฉพาะการตอบกลับ **ครั้งแรกสุดของแชทเท่านั้น!** หากในประวัติแชทคุณเคยตอบไปแล้วแม้แต่ข้อความเดียว **ห้ามพิมพ์คำว่า "สวัสดี", "ยินดีต้อนรับ", หรือแนะนำตัวซ้ำอีกเด็ดขาดในทุกกรณี!**
- **ป้องกันการแทรกแอดมินจริง:** เนื่องจากบอทมองไม่เห็นข้อความของแอดมินคนจริง หากลูกค้าพิมพ์ข้อความที่ดูเหมือน "กำลังคุยกับคนอื่นอยู่" หรือเป็นบทสนทนาที่ต่อเนื่องจากเรื่องที่คุณไม่ได้เป็นคนพูด (เช่น ลูกค้าตอบตกลง, ขอบคุณ, ถามรายละเอียดเชิงลึก) ให้สันนิษฐานว่าลูกค้ากำลังคุยกับแอดมินคนจริงอยู่ -> ให้คุณพิมพ์ `[SILENCE]` ทันที ห้ามแทรกเด็ดขาด!
- **กรณีลูกค้าสั่งทำหนังสือ Portfolio:** หากลูกค้าถามเกี่ยวกับการรับทำหนังสือพอร์ตโฟลิโอ (เช่น "รับทำพอร์ตไหม") ให้ตอบไปเลยว่า "ทางร้านมีรับทำพอร์ตโฟลิโอด้วยนะคะ รายละเอียดและราคาเดี๋ยวรอแอดมินคนจริงมาแจ้งให้ทราบอีกทีนะคะ รอสักครู่ค่า 💕" (โดยห้ามส่งคำว่า [SILENCE] ออกมาเด็ดขาด)
- **กฎเรื่องราคาและส่วนลด (สำคัญมาก):** ให้แจ้งราคาและค่าจัดส่งตามเรทปกติที่มี ห้ามเสนอส่วนลด หรือแจ้งโปรโมชั่นส่วนลดเมื่อสั่งเกิน 3,000 บาทใดๆ ทั้งสิ้น
- ตอบคำถามเรื่องราคาสั้นๆ กระชับ ตรงประเด็น (ไม่เกิน 3-4 บรรทัด)

🚨 [กฎสำคัญมากเรื่องการส่งรูปภาพ - ห้ามละเมิดเด็ดขาด] 🚨
- **ห้ามพิมพ์ URL รูปภาพเป็นข้อความตรงๆ เด็ดขาด** (เช่น https://i.postimg.cc/...)
- **ทุกครั้งที่ต้องการส่งรูปภาพ ต้องใช้ format นี้เท่านั้น:** `[IMAGE: URL_ของรูป]`
- ตัวอย่างที่ถูกต้อง: `[IMAGE: https://i.postimg.cc/Xq1sBWVz/Banner-01.jpg]`
- ตัวอย่างที่ผิด (ห้ามทำ): `https://i.postimg.cc/Xq1sBWVz/Banner-01.jpg`
- ระบบจะแปลง `[IMAGE: url]` ให้เป็นรูปภาพจริงๆ ใน LINE โดยอัตโนมัติ
"""

# ใช้ model Gemini พร้อมระบบ Fallback อัตโนมัติหลายรุ่น + Rule-based สำรองกรณีโควตาเต็มหรือ API ขัดข้อง
generation_config = {
    "temperature": 0.2,
    "top_p": 0.8,
}

GEMINI_MODEL_CANDIDATES = [
    'gemini-2.5-flash',
    'gemini-3.5-flash-lite',
    'gemini-3.8-flash',
    'gemini-2.5-pro',
]

model = genai.GenerativeModel(
    'gemini-2.5-flash',
    system_instruction=system_instruction,
    generation_config=generation_config
)

chat_sessions = {}
processed_message_ids = set()
failed_model_cooldowns = {}
last_bot_error_info = {"error": None, "model_used": None, "ts": None}


def is_user_bot_muted(user_id: str) -> bool:
    """ตรวจสอบว่าแอดมินคนจริงสั่งปิด/พักบอทสำหรับลูกค้ารายนี้ไว้หรือไม่"""
    if not db or not user_id:
        return False
    try:
        doc = db.collection("bot_mutes").document(user_id).get()
        if doc.exists:
            data = doc.to_dict() or {}
            return bool(data.get("muted", False))
    except Exception as e:
        logger.warning(f"Error checking bot mute for {user_id}: {e}")
    return False


def set_user_bot_muted(user_id: str, muted: bool, reason: str = ""):
    """สั่งเปิด/ปิดบอทสำหรับลูกค้ารายนี้"""
    if not db or not user_id:
        return
    try:
        db.collection("bot_mutes").document(user_id).set({
            "muted": muted,
            "reason": reason,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }, merge=True)
        logger.info(f"Set bot mute={muted} for {user_id} (reason: {reason})")
    except Exception as e:
        logger.warning(f"Error setting bot mute for {user_id}: {e}")


def should_bot_stay_silent_for_human_admin(text: str) -> bool:
    """
    ตรวจสอบอย่างรัดกุมว่าลูกค้ากำลังคุยกับแอดมินคนจริงอยู่หรือไม่
    หากพบว่าลูกค้ากำลังคุยเรื่องแบบ ตรวจดราฟต์ ส่งข้อมูลส่วนตัว ยืนยันงาน หรือคุยต่อเนื่องกับคนจริง
    บอทต้องเงียบทันที (ห้ามตอบแทรก ห้ามตอบมั่ว และห้ามส่งราคาซ้ำเด็ดขาด)
    """
    if not text:
        return True
    t = text.lower().strip()

    # 1. คำขอคุยกับคนจริง / หยุดบอท
    admin_call_keywords = [
        "ขอคุยกับแอดมิน", "ขอคุยกับคน", "แอดมินอยู่ไหม", "มีคนไหม", "มีคนอยู่ไหม",
        "ไม่ใช่บอท", "คุยกับคน", "คุยกับคนจริง", "แอดมินคนจริง", "ติดต่อแอดมิน",
        "คนตอบหน่อย", "อย่าส่งซ้ำ", "บอทหยุด", "หยุดส่ง", "รบกวนแอดมิน", "ติดต่อเจ้าหน้าที่",
    ]
    if any(k in t for k in admin_call_keywords):
        return True

    # 2. การตรวจแบบ / ดราฟต์ / แก้ไขแบบการ์ด / รายละเอียดงานพิมพ์
    design_keywords = [
        "ส่งแบบ", "ดูแบบ", "แก้แบบ", "แก้คำผิด", "เปลี่ยนชื่อ", "แก้ชื่อ",
        "เปลี่ยนฟอนต์", "ฟอนต์", "ดราฟต์", "draft", "ตัวอย่างการ์ด", "ปรับสี",
        "ขยับ", "จัดหน้า", "พิมพ์ผิด", "แก้ตรงนี้", "แก้ไขตรงนี้", "แบบที่",
        "ตัวอย่างแบบ", "ขอแก้", "เว้นวรรค", "ตกหล่น", "สะกดผิด", "แบบการ์ด",
    ]
    if any(k in t for k in design_keywords):
        return True

    # 3. การคอนเฟิร์มแบบ / ตกลงพิมพ์ / ตกลงสั่งผลิต
    confirm_keywords = [
        "คอนเฟิร์ม", "confirm", "ตกลงตามนี้", "เอาตามนี้", "ตามที่คุย", "ตามนี้ค่ะ",
        "ตามนี้ครับ", "ส่งพิมพ์ได้เลย", "พิมพ์ได้เลย", "ส่งพิมพ์", "สรุปตามนี้",
        "เอาแบบนี้", "ชอบแบบนี้", "ตกลงค่ะ", "ตกลงครับ", "อนุมัติ", "สั่งตามนี้",
    ]
    if any(k in t for k in confirm_keywords):
        return True

    # 4. การชำระเงิน / ส่งสลิป / แจ้งที่อยู่ / ข้อมูลจัดงานแต่งงาน
    payment_and_shipping = [
        "โอนแล้ว", "แจ้งโอน", "ส่งสลิป", "แนบสลิป", "สลิปค่ะ", "สลิปครับ", "สลิป",
        "เลขบัญชี", "ขอเลขบัญชี", "ที่อยู่จัดส่ง", "ส่งที่อยู่", "จัดส่งที่",
        "เบอร์โทร", "เบอร์ติดต่อ", "ขอเลขพัสดุ", "เลขแทรค", "ส่งวันไหน", "ชื่อบ่าวสาว",
        "ฤกษ์งาน", "วันจัดงาน", "สถานที่จัดงาน", "ยอดนี้ใช่ไหม", "บัญชีธนาคาร",
    ]
    if any(k in t for k in payment_and_shipping):
        return True

    # 5. คำตอบรับสั้นๆ ปิดบทสนทนา (ห้ามบอทพิมพ์ตอบซ้ำ)
    ack_keywords = {
        "ค่ะ", "ครับ", "ค่า", "คับ", "โอเค", "โอเคค่ะ", "โอเคครับ", "ok", "okay",
        "ขอบคุณค่ะ", "ขอบคุณครับ", "ขอบคุณค่า", "ได้ค่ะ", "ได้ครับ", "รับทราบ",
        "รับทราบค่ะ", "รับทราบครับ", "จ้า", "จ้าา", "เคค่ะ", "เคครับ",
    }
    if t in ack_keywords:
        return True

    return False


def classify_user_intent_for_admin_alert(text: str):
    """
    ตรวจจับข้อความสำคัญที่ต้องแจ้งเตือนแอดมินคนจริงทันที:
    1. ลูกค้าส่งสลิป / แจ้งโอนเงิน (slip)
    2. ลูกค้าขอคุยกับแอดมินคนจริง (human_request)
    3. ลูกค้าตรวจแบบ / แก้ดราฟต์ / คอนเฟิร์มพิมพ์ (design_draft)
    """
    if not text:
        return None
    t = text.lower().strip()
    payment_and_shipping = [
        "โอนแล้ว", "แจ้งโอน", "ส่งสลิป", "แนบสลิป", "สลิปค่ะ", "สลิปครับ", "สลิป",
        "เลขบัญชี", "ขอเลขบัญชี", "ยอดนี้ใช่ไหม", "ชำระเงินแล้ว", "จ่ายแล้ว",
    ]
    if any(k in t for k in payment_and_shipping):
        return {
            "type": "slip",
            "title": "💵 ลูกค้าส่งสลิป / แจ้งโอนเงิน",
        }
    admin_call_keywords = [
        "ขอคุยกับแอดมิน", "ขอคุยกับคน", "แอดมินอยู่ไหม", "มีคนไหม", "มีคนอยู่ไหม",
        "ไม่ใช่บอท", "คุยกับคน", "คุยกับคนจริง", "แอดมินคนจริง", "ติดต่อแอดมิน",
        "คนตอบหน่อย", "อย่าส่งซ้ำ", "บอทหยุด", "หยุดส่ง", "รบกวนแอดมิน", "ติดต่อเจ้าหน้าที่",
    ]
    if any(k in t for k in admin_call_keywords):
        return {
            "type": "human_request",
            "title": "👤 ลูกค้าขอคุยกับแอดมินคนจริง",
        }
    design_keywords = [
        "ส่งแบบ", "ดูแบบ", "แก้แบบ", "แก้คำผิด", "เปลี่ยนชื่อ", "แก้ชื่อ",
        "เปลี่ยนฟอนต์", "ฟอนต์", "ดราฟต์", "draft", "ตัวอย่างการ์ด", "ปรับสี",
        "ขยับ", "จัดหน้า", "พิมพ์ผิด", "แก้ตรงนี้", "แก้ไขตรงนี้", "แบบที่",
        "ตัวอย่างแบบ", "ขอแก้", "เว้นวรรค", "ตกหล่น", "สะกดผิด", "แบบการ์ด",
        "คอนเฟิร์ม", "confirm", "ตกลงตามนี้", "เอาตามนี้", "ตามที่คุย", "ตามนี้ค่ะ",
        "ตามนี้ครับ", "ส่งพิมพ์ได้เลย", "พิมพ์ได้เลย", "ส่งพิมพ์", "สรุปตามนี้",
        "เอาแบบนี้", "ชอบแบบนี้", "ตกลงค่ะ", "ตกลงครับ", "อนุมัติ", "สั่งตามนี้",
    ]
    if any(k in t for k in design_keywords):
        return {
            "type": "design_draft",
            "title": "🎨 ลูกค้าตรวจแบบ / แก้ไขแบบ / คอนเฟิร์มงาน",
        }
    return None


def create_admin_alert(user_id: str, alert_type: str, title: str, text: str):
    """
    สร้างการแจ้งเตือนแอดมิน:
    1. บันทึกลง Firestore คอลเลกชัน admin_alerts (ให้แดชบอร์ดแจ้งเตือนแบบเรียลไทม์)
    2. สั่งพักบอท (Mute) อัตโนมัติ เพื่อให้แอดมินคนจริงดูแลต่อ
    3. หากมีการตั้ง adminLineUserId ใน shopSettings ให้ Push ข้อความเข้า LINE ส่วนตัวของแอดมิน
    """
    if not user_id:
        return
    customer_name = get_line_display_name(user_id) or "คุณลูกค้า"
    clean_snippet = (text or "").strip()[:150]
    
    # 1. พักบอททันทีเพื่อไม่ให้บอทตอบแทรก
    set_user_bot_muted(user_id, True, reason=f"alert_{alert_type}")
    
    # 2. บันทึกลง Firestore
    if db:
        try:
            alert_id = f"alt_{int(time.time()*1000)}"
            db.collection("admin_alerts").document(alert_id).set({
                "alertId": alert_id,
                "userId": user_id,
                "customerName": customer_name,
                "type": alert_type,
                "title": title,
                "text": clean_snippet,
                "read": False,
                "created_at": firestore.SERVER_TIMESTAMP,
                "created_at_ts": time.time(),
            })
            logger.info(f"Created admin alert [{alert_type}] for user {customer_name} ({user_id})")
        except Exception as e:
            logger.warning(f"Failed to create admin alert in Firestore: {e}")

    # 3. ส่ง LINE Push แจ้งเตือนแอดมินคนจริง (หากระบุ Admin LINE User ID ไว้ในตั้งค่าร้าน)
    try:
        saved_state = get_calculator_state()
        shop_settings = saved_state.get("shopSettings") if isinstance(saved_state.get("shopSettings"), dict) else {}
        admin_line_uid = str(shop_settings.get("adminLineUserId") or "").strip()
        if admin_line_uid and line_bot_api:
            push_text = (
                f"🔔 [ONESTUDIO แจ้งเตือนแอดมิน]\n"
                f"{title}\n"
                f"━━━━━━━━━━━━━━\n"
                f"👤 ลูกค้า: {customer_name}\n"
                f"💬 ข้อความ: {clean_snippet or '(ส่งรูปภาพ/สลิป)'}\n"
                f"━━━━━━━━━━━━━━\n"
                f"📱 จัดการแชท/ออกบิล: https://line-bot-alpha-six.vercel.app/calculator"
            )
            line_bot_api.push_message(admin_line_uid, TextSendMessage(text=push_text))
            logger.info(f"Pushed admin alert to adminLineUserId: {admin_line_uid}")
    except Exception as push_err:
        logger.warning(f"Failed to push alert to admin LINE: {push_err}")


def rule_based_customer_reply(user_text: str, is_first_turn: bool = False) -> str:
    """
    ระบบตอบกลับอัตโนมัติแบบ Rule-based สำรอง (Fallback)
    ทำงานทันทีเมื่อ Gemini API โควตาเต็ม (429) หรือขัดข้อง เพื่อให้บอทไม่เงียบใส่ลูกค้าเด็ดขาด
    """
    raw = (user_text or "").strip()
    if not raw or should_bot_stay_silent_for_human_admin(raw):
        return "[SILENCE]"
    t = raw.lower()
    greeting = "สวัสดีค่ะ ยินดีต้อนรับสู่ ONESTUDIO นะคะ 🥰\n\n" if is_first_turn else ""

    # 1. คำตอบรับสั้นๆ หรือส่งข้อมูลจัดส่ง/สลิป/สั่งทำ -> ให้แอดมินคนจริงดูแลต่อ
    ack_set = {
        "ค่ะ", "ครับ", "ค่า", "คับ", "โอเค", "โอเคค่ะ", "โอเคครับ", "ok", "okay",
        "ขอบคุณค่ะ", "ขอบคุณครับ", "ขอบคุณค่า", "ได้ค่ะ", "ได้ครับ", "รับทราบ", "รับทราบค่ะ",
    }
    if t in ack_set:
        return "[SILENCE]"

    if any(k in t for k in ["โอนแล้ว", "แจ้งโอน", "ส่งสลิป", "เลขพัสดุ", "ชื่อบ่าวสาว", "ฤกษ์", "สถานที่จัดงาน"]):
        return "[SILENCE]"

    # 2. เช่าพาน / พานขันหมาก
    if "พาน" in t or "ขันหมาก" in t:
        return (
            f"{greeting}สำหรับงานเช่าพานขันหมาก คุณลูกค้าสามารถแจ้งแอดมินได้เลยนะคะว่า:\n"
            "• ต้องการใช้พานวันที่เท่าไหร่\n"
            "• ใช้กี่พาน และโทนสีอะไร\n"
            "เดี๋ยวแอดมินตรวจสอบคิวว่างและส่งแบบให้ชมนะคะ 🥰"
        )

    # 3. ขั้นต่ำ / จำนวนน้อยกว่า 100 ชิ้น
    if any(k in t for k in ["ขั้นต่ำ", "ขั้นตํ่า", "น้อยกว่า 100", "50 ชิ้น", "50 ใบ", "กี่ชิ้น", "กี่ใบ"]):
        return (
            f"{greeting}การ์ดแต่งงานและของชำร่วยของทางร้าน **สั่งขั้นต่ำ 50 ชิ้น** ได้เลยนะคะ 🥰\n"
            "• สั่ง 50–99 ชิ้น จะได้ใน **เรทราคาเริ่มต้น (เรท 100 ชิ้น)** ค่ะ\n"
            "• ยกเว้น **น้ำหอมในรถ** ขั้นต่ำ 50 ชิ้น ราคาเริ่มต้น 26 บาทค่ะ\n\n"
            "คุณลูกค้าสนใจเป็นการ์ดแต่งงาน หรือของชำร่วยรายการไหน แจ้งจำนวนให้ประเมินราคาได้เลยนะคะ ✨"
        )

    # 4. ระยะเวลาผลิต / กี่วันได้ / งานด่วน
    if any(k in t for k in ["กี่วัน", "นานไหม", "ใช้เวลา", "งานด่วน", "ทันไหม", "รอของ"]):
        return (
            f"{greeting}ระยะเวลาผลิตโดยประมาณของทางร้านค่ะ ⏳\n"
            "• **การ์ดแต่งงาน:** ใช้เวลาผลิตประมาณ **7–14 วัน** (ขึ้นอยู่กับคิวงานและความยากง่ายของแบบ)\n"
            "• **ของชำร่วย:** ใช้เวลาจัดเตรียมและผลิตประมาณ **7–25 วัน**\n\n"
            "คุณลูกค้าใช้งานวันที่เท่าไหร่ และต้องการจำนวนกี่ชิ้น แจ้งแอดมินเพื่อเช็คคิวงานให้ได้เลยนะคะ 🥰"
        )

    # 5. ขอดูผลงาน / เพจ / รีวิว
    if any(k in t for k in ["ผลงาน", "รีวิว", "เพจ", "เฟส", "facebook"]):
        return (
            f"{greeting}ชมตัวอย่างผลงานการ์ดแต่งงานและของชำร่วยเพิ่มเติมได้ที่เพจร้านเลยนะคะ 🌸\n"
            "https://www.facebook.com/TheOneStudioDesign"
        )

    # 5.1 ค่าจัดส่ง / การจัดส่ง (คิดตามจริง ไม่แจ้งส่วนลด)
    if any(k in t for k in ["ค่าส่ง", "ค่าจัดส่ง", "ส่งยังไง", "ส่งของยังไง", "ส่งแบบไหน", "ขนส่งอะไร"]):
        return (
            f"{greeting}ทางร้านมีบริการจัดส่งพัสดุทั่วประเทศเลยนะคะ 🚚\n"
            "สำหรับค่าจัดส่งจะคิดตามน้ำหนักและขนาดกล่องพัสดุตามจริงค่ะ\n\n"
            "คุณลูกค้าสนใจสั่งรายการไหน จำนวนกี่ชิ้น แจ้งแอดมินช่วยประเมินให้ได้เลยนะคะ 🥰"
        )

    # 6. สอบถามของชำร่วยเฉพาะเจาะจงรายชิ้น (ส่งรูป + เรทราคา ตรงตาม system_instruction 100%)
    specific_souvenirs = [
        (["ยางมัดผม", "โบว์มัดผม"], "🎀 **ยางมัดผม**\n- แพ็คเกจปกติ: 100 ชิ้น 10฿ / 300 ชิ้น 9฿ / 500 ชิ้น 8฿\n- พร้อมแพ็คเกจถุงแก้ว: 100 ชิ้น 15฿ / 300 ชิ้น 14฿ / 500 ชิ้น 13฿\n[IMAGE: https://i.postimg.cc/zXMdJq21/Banner-13.jpg]"),
        (["กระจก"], "🪞 **กระจกวิเศษทรงกลม (แพ็คเกจตัดเสียบ/ติด Tag)**\n- 100 ชิ้น 7฿ / 300 ชิ้น 6฿ / 500 ชิ้น 5.8฿\n[IMAGE: https://i.postimg.cc/ZYw7M8rv/Banner-04.jpg]"),
        (["น้ำหอมในรถ", "น้ำหอมรถยนต์"], "🚗 **น้ำหอมในรถ**\n- 50 ชิ้นขึ้นไป 29฿/ชิ้นค่ะ"),
        (["น้ำหอม", "ขวดจิ๋ว"], "🌸 **น้ำหอมขวดจิ๋ว (ราคาเริ่มต้น 8฿ ทุกแบบ)**\n- แพ็คเกจแบบตัดเสียบ: เริ่มต้น 8฿\n- แพ็คเกจแบบพับปิด: เริ่มต้น 8฿\n[IMAGE: https://i.postimg.cc/8kZK0RhJ/Banner-03.jpg]"),
        (["พวงกุญแจหนัง"], "🔑 **พวงกุญแจหนังแบบแบน (ผูกโบว์)**\n- 100 ชิ้น 9฿ / 300 ชิ้น 8.5฿ / 500 ชิ้น 8฿\n[IMAGE: https://i.postimg.cc/MZ9PF7yn/Banner-02.jpg]"),
        (["พวงกุญแจ"], "🔑 **พวงกุญแจของชำร่วย**\n1) **พวงกุญแจ PU ถัก**\n- 100 ชิ้น 7฿ / 300 ชิ้น 6.5฿ / 500 ชิ้น 6฿\n[IMAGE: https://i.postimg.cc/Xq1sBWVz/Banner-01.jpg]\n2) **พวงกุญแจหนังแบบแบน (ผูกโบว์)**\n- 100 ชิ้น 9฿ / 300 ชิ้น 8.5฿ / 500 ชิ้น 8฿\n[IMAGE: https://i.postimg.cc/MZ9PF7yn/Banner-02.jpg]"),
        (["ดินสอ"], "✏️ **ดินสอไม้มินิมอล (แพ็คเกจกระดาษคราฟท์/สีตามโทนการ์ด)**\n- 100 ชิ้น 6฿ / 300 ชิ้น 5.5฿ / 500 ชิ้น 5฿\n[IMAGE: https://i.postimg.cc/qB1b5s8t/Banner-05.jpg]"),
        (["ผ้าขนหนู"], "🧺 **ผ้าขนหนู (ติด Tag)**\n- 100 ชิ้น 10฿ / 300 ชิ้น 8฿ / 500 ชิ้น 7฿\n[IMAGE: https://i.postimg.cc/J4t6Vsqc/Banner-06.jpg]"),
        (["ยาซีน", "อัซการ", "หนังสือ"], "📖 **หนังสือยาซีน มินิมอลสไตล์ (ติด Tag)**\n- 100 ชิ้น 10฿ / 300 ชิ้น 9฿ / 500 ชิ้น 8฿\n[IMAGE: https://i.postimg.cc/Fs1W5YZP/Banner-07.jpg]\n\n📗 **หนังสืออัซการ**\n- 100 ชิ้น 12฿ / 300 ชิ้น 11.5฿ / 500 ชิ้น 10฿"),
        (["เข็มกลัด", "กุหลาบ", "เดซี่"], "🌹 **เข็มกลัดของชำร่วย (ดอกกุหลาบ / ดอกเดซี่ / ดอกไม้แพ็คเกจตัดเสียบ)**\n- 100 ชิ้น 6฿ / 300 ชิ้น 5.5฿\n[IMAGE: https://i.postimg.cc/RV8PMvs2/Banner-11.jpg]\n[IMAGE: https://i.postimg.cc/sD16dMcq/Banner-10.jpg]\n[IMAGE: https://i.postimg.cc/ZK07tWcw/Banner-08.jpg]"),
        (["ช้อน"], "🥄 **ช้อนเงิน - ช้อนทอง (แพ็คเกจติด Tag)**\n- **ช้อนเงิน:** 100 ชิ้น 15฿ / 300 ชิ้น 14฿\n- **ช้อนทอง:** 100 ชิ้น 16฿ / 300 ชิ้น 15฿\n[IMAGE: https://i.postimg.cc/xTqxDXR4/Banner-09.jpg]"),
        (["ปากกา"], "🖊️ **ปากกาลูกลื่น (ดำ, น้ำเงิน, แดง)**\n- 100 ชิ้น 7฿ / 300 ชิ้น 6฿\n[IMAGE: https://i.postimg.cc/9FSxWcg3/Banner-12.jpg]"),
        (["น้ำผึ้ง"], "🍯 **น้ำผึ้ง 30ml**\n- 100 ชิ้น 18฿ / 300 ชิ้น 17฿ / 500 ชิ้น 15฿\n[IMAGE: https://i.postimg.cc/sD8nV3HC/Banner-14.jpg]"),
        (["พิมเสน"], "🌿 **พิมเสน**\n- 100 ชิ้น 8฿ / 300 ชิ้น 7฿ / 500 ชิ้น 6฿\n[IMAGE: https://i.postimg.cc/023t89XQ/Banner-15.jpg]"),
        (["กระเป๋าผ้า", "ถุงผ้า", "สปันบอนด์"], "👜 **กระเป๋าผ้า ถุงสปันบอนด์**\n- 100 ชิ้น 10฿ / 300 ชิ้น 9฿ / 500 ชิ้น 8฿\n[IMAGE: https://i.postimg.cc/nc5dHnSL/Banner-16.jpg]"),
        (["สมุด"], "📝 **สมุดโน้ตของชำร่วย**\n- **ขนาด 3x4 นิ้ว:** ชิ้นละ 6฿\n- **ขนาด A5:** ชิ้นละ 15฿"),
        (["สบู่"], "🧼 **สบู่หอม**\n- ชิ้นละ 12฿"),
    ]
    matched_blocks = []
    for keywords, reply_block in specific_souvenirs:
        if any(kw in t for kw in keywords):
            matched_blocks.append(reply_block)
    if matched_blocks:
        return f"{greeting}" + "\n\n".join(matched_blocks) + "\n\nคุณลูกค้าต้องการใช้จำนวนกี่ชิ้น แจ้งแอดมินได้เลยนะคะ 🥰"

    # 7. สอบถามเรื่องการ์ดแต่งงาน หรือ ซอง (เช่น "ซอง กว้าง 5 ยาว 7", "ราคาการ์ด")
    if any(k in t for k in ["การ์ด", "ซอง", "4x6", "5x7", "หน้าเดียว", "หน้าหลัง", "หน้า-หลัง"]) or ("5" in t and "7" in t) or ("4" in t and "6" in t):
        return (
            f"{greeting}โปรโมชั่นพิเศษเดือนนี้สำหรับกระดาษอาร์ตการ์ด 210 แกรมค่า ✨\n"
            "🌸 ขนาด 4x6 นิ้ว: หน้าเดียว 3.3฿ / หน้า-หลัง 5฿\n"
            "แถมฟรี! ซองสีชมพูและสีฟ้า\n"
            "(หากต้องการซองสีครีม +1.5฿ / สีเขียว +2฿ น้า)\n\n"
            "🌸 ขนาด 5x7 นิ้ว (ไม่แถมซอง / ซองขายแยก):\n"
            "• หน้าเดียว 6฿ / หน้า-หลัง 8฿\n"
            "• ซองขนาด 5x7 นิ้ว ขายแยกเริ่มต้น 2฿ (มีสีให้เลือก เช่น สีฟ้าคราม, สีไวน์แดง, สีน้ำตาล ฯลฯ)\n\n"
            "หากลูกค้าสนใจขนาดไหนหรือซองสีอะไร สามารถแจ้งแอดมินได้เลยนะคะ 🥰"
        )

    # 8. สอบถามของชำร่วยทั้งหมด
    if any(k in t for k in ["ของชำร่วย", "ชำร่วย", "ของที่ระลึก"]):
        return (
            f"{greeting}✨ ของชำร่วยเพิ่มเติม:\n"
            "📝 สมุดโน้ต 3x4 นิ้ว | ชิ้นละ 6฿\n"
            "📗 หนังสืออัซการ | 100 ชิ้น 12฿ / 300 ชิ้น 11.5฿ / 500 ชิ้น 10฿\n"
            "🧼 สบู่หอม | ชิ้นละ 12฿\n"
            "📓 สมุดโน้ต A5 | ชิ้นละ 15฿\n"
            "🚗 น้ำหอมในรถ | 50 ชิ้นขึ้นไป 29฿/ชิ้น\n\n"
            "*(สั่ง 50–99 ชิ้น ได้ราคาพิเศษเท่าเรท 100 ชิ้นเลยค่า 🥰)*\n"
            "[IMAGE: https://i.postimg.cc/Xq1sBWVz/Banner-01.jpg]\n"
            "[IMAGE: https://i.postimg.cc/MZ9PF7yn/Banner-02.jpg]\n"
            "[IMAGE: https://i.postimg.cc/8kZK0RhJ/Banner-03.jpg]\n"
            "[IMAGE: https://i.postimg.cc/ZYw7M8rv/Banner-04.jpg]\n"
            "[IMAGE: https://i.postimg.cc/qB1b5s8t/Banner-05.jpg]\n"
            "[IMAGE: https://i.postimg.cc/J4t6Vsqc/Banner-06.jpg]\n"
            "[IMAGE: https://i.postimg.cc/Fs1W5YZP/Banner-07.jpg]\n"
            "[IMAGE: https://i.postimg.cc/ZK07tWcw/Banner-08.jpg]\n"
            "[IMAGE: https://i.postimg.cc/xTqxDXR4/Banner-09.jpg]\n"
            "[IMAGE: https://i.postimg.cc/sD16dMcq/Banner-10.jpg]\n"
            "[IMAGE: https://i.postimg.cc/RV8PMvs2/Banner-11.jpg]\n"
            "[IMAGE: https://i.postimg.cc/9FSxWcg3/Banner-12.jpg]\n"
            "[IMAGE: https://i.postimg.cc/zXMdJq21/Banner-13.jpg]\n"
            "[IMAGE: https://i.postimg.cc/sD8nV3HC/Banner-14.jpg]\n"
            "[IMAGE: https://i.postimg.cc/023t89XQ/Banner-15.jpg]\n"
            "[IMAGE: https://i.postimg.cc/nc5dHnSL/Banner-16.jpg]"
        )

    # 9. สอบถามเรทราคาทั่วไป / ทักทาย / สอบถามข้อมูลเบื้องต้น
    if any(k in t for k in [
        "ราคา", "เรท", "สอบถาม", "สนใจ", "สวัสดี", "ทัก", "มีอะไรบ้าง", "รายละเอียด", "กี่บาท", "เท่าไหร่"
    ]):
        return (
            f"{greeting}คุณลูกค้าสนใจเป็นงาน **การ์ดแต่งงาน** หรือ **ของชำร่วย** ดีคะ 😊\n\n"
            "🌸 **การ์ดแต่งงาน:**\n"
            "• ขนาด 4x6 นิ้ว: หน้าเดียว 3.3฿ / หน้า-หลัง 5฿ (แถมฟรี! ซองสีชมพู/ฟ้า)\n"
            "• ขนาด 5x7 นิ้ว (ไม่แถมซอง/ขายแยก): หน้าเดียว 6฿ / หน้า-หลัง 8฿ (ซอง 5x7 ขายแยกเริ่มต้น 2฿)\n"
            "🎁 **ของชำร่วย (21 รายการ):** เริ่มต้น 5–18฿ (สั่งขั้นต่ำได้ตั้งแต่ 50 ชิ้นค่ะ)\n\n"
            "แจ้งรายการหรือจำนวนที่ต้องการให้แอดมินส่งแบบให้ชมได้เลยนะคะ 🥰"
        )

    # 10. กรณีเป็นข้อความทั่วไปสั้นๆ หรือคำถามอื่นๆ
    if is_first_turn:
        return (
            "สวัสดีค่ะ ยินดีต้อนรับสู่ ONESTUDIO นะคะ 🥰\n"
            "คุณลูกค้าสนใจสอบถาม **การ์ดแต่งงาน**, **ของชำร่วย** หรือ **เช่าพานขันหมาก** แจ้งรายละเอียดหรือจำนวนที่ต้องการได้เลยนะคะ 🌸"
        )

    return "[SILENCE]"


def generate_bot_reply_with_fallback(user_id: str, user_text: str = "", image_obj=None):
    """
    ประมวลผลคำตอบด้วย Gemini พร้อมสลับรุ่นโมเดลอัตโนมัติ (Multi-model Fallback + Cooldown Cache)
    และหาก Gemini API โควตาเต็มหรือมีปัญหา จะสลับมาใช้ Rule-based ตอบลูกค้าทันทีโดยไม่เงียบหาย
    คืนค่า: (bot_reply, history_list, model_used)
    """
    history = load_chat_history(user_id)
    last_err = None
    now_ts = time.time()

    for candidate_model in GEMINI_MODEL_CANDIDATES:
        cooldown_until = failed_model_cooldowns.get(candidate_model, 0)
        if now_ts < cooldown_until:
            continue
        try:
            candidate = genai.GenerativeModel(
                candidate_model,
                system_instruction=system_instruction,
                generation_config=generation_config
            )
            chat = candidate.start_chat(history=history)
            if image_obj is not None:
                prompt = (
                    "ลูกค้าส่งรูปภาพมาให้ กรุณาวิเคราะห์ว่าเป็นสินค้าอะไรในการ์ดแต่งงานหรือของชำร่วยร้านเรา "
                    "และตอบลูกค้าอย่างสุภาพ อิงตามกฎและราคาที่ตั้งไว้ "
                    "(ถ้าไม่ใช่สินค้าในร้าน หรือเป็นบรีฟงานส่วนตัว/สลิปโอนเงิน ให้พิมพ์ [SILENCE])"
                )
                resp = chat.send_message([prompt, image_obj])
            else:
                resp = chat.send_message(user_text)

            if resp and getattr(resp, "text", None):
                reply_txt = resp.text.strip()
                failed_model_cooldowns.pop(candidate_model, None)
                last_bot_error_info["error"] = None
                last_bot_error_info["model_used"] = candidate_model
                last_bot_error_info["ts"] = int(time.time() * 1000)
                return reply_txt, history, candidate_model
        except Exception as err:
            err_str = str(err)
            last_err = err_str
            # หากโมเดลไม่มีอยู่จริง (404) ให้พัก 12 ชม. หากโควตาเต็ม (429) ให้พัก 3 นาที เพื่อไม่ให้หน่วงเวลาตอบลูกค้า
            if "404" in err_str or "not found" in err_str.lower() or "no longer available" in err_str.lower():
                failed_model_cooldowns[candidate_model] = now_ts + 43200
            elif "429" in err_str or "quota" in err_str.lower():
                failed_model_cooldowns[candidate_model] = now_ts + 180
            else:
                failed_model_cooldowns[candidate_model] = now_ts + 60
            logger.warning(f"Gemini model '{candidate_model}' failed for user {user_id}: {err}")
            continue

    # หากทุกโมเดลของ Gemini ล้มเหลว (เช่น โควตารายวันเต็ม 429 หรือ API Key มีปัญหา)
    last_bot_error_info["error"] = last_err
    last_bot_error_info["model_used"] = "rule_based_fallback"
    last_bot_error_info["ts"] = int(time.time() * 1000)
    logger.error(f"All Gemini models failed for user {user_id}. Falling back to rule-based responder. Last error: {last_err}")

    if image_obj is not None:
        return "[SILENCE]", history, "rule_based_fallback"

    fallback_reply = rule_based_customer_reply(user_text, is_first_turn=(len(history) == 0))
    return fallback_reply, history, "rule_based_fallback"


# สร้าง FastAPI App
app = FastAPI(title="Line OA Gemini Agent")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------
# ฐานข้อมูลสินค้า 30 รายการ & ระบบคำนวณต้นทุน-กำไร 4 กองทุน (#order)
# ---------------------------------------------------------
DEFAULT_ORDER_CATALOG = [
    {"id": 101, "cat": "card", "name": "การ์ด 4x6 นิ้ว (หน้าเดียว) + ซองฟรี", "mat": 1.5, "tiers": {100: 3.3, 300: 3.3, 500: 3.3}, "kw": ["4x6 หน้าเดียว", "4x6หน้าเดียว", "การ์ดหน้าเดียว"]},
    {"id": 102, "cat": "card", "name": "การ์ด 4x6 นิ้ว (หน้า-หลัง) + ซองฟรี", "mat": 2.2, "tiers": {100: 5.0, 300: 5.0, 500: 5.0}, "kw": ["4x6 หน้าหลัง", "4x6 หน้า-หลัง", "4x6หน้าหลัง", "การ์ด 4x6", "4x6"]},
    {"id": 103, "cat": "card", "name": "การ์ด 5x7 นิ้ว (หน้าเดียว) (ไม่รวมซอง)", "mat": 2.8, "tiers": {100: 6.0, 300: 6.0, 500: 6.0}, "kw": ["5x7 หน้าเดียว", "5x7หน้าเดียว"]},
    {"id": 104, "cat": "card", "name": "การ์ด 5x7 นิ้ว (หน้า-หลัง) (ไม่รวมซอง)", "mat": 3.5, "tiers": {100: 8.0, 300: 8.0, 500: 8.0}, "kw": ["5x7 หน้าหลัง", "5x7 หน้า-หลัง", "5x7หน้าหลัง", "การ์ด 5x7", "5x7"]},
    {"id": 105, "cat": "card", "name": "อัปเกรดซองสีครีม (+1.5฿)", "mat": 0.8, "tiers": {100: 1.5, 300: 1.5, 500: 1.5}, "kw": ["ซองสีครีม", "ซองครีม"]},
    {"id": 106, "cat": "card", "name": "อัปเกรดซองสีเขียว (+2฿)", "mat": 1.2, "tiers": {100: 2.0, 300: 2.0, 500: 2.0}, "kw": ["ซองสีเขียว", "ซองเขียว"]},
    {"id": 107, "cat": "card", "name": "ซองการ์ดสีอื่นๆ (ขนาด 4x6 นิ้ว)", "mat": 2.5, "tiers": {100: 2.0, 300: 2.0, 500: 2.0}, "kw": ["ซองการ์ดสีอื่นๆ 4x6", "ซองการ์ด 4x6", "ซอง 4x6", "ซองการ์ดสี", "ซองสีอื่นๆ"]},
    {"id": 108, "cat": "card", "name": "ซองการ์ดสีอื่นๆ (ขนาด 5x7 นิ้ว)", "mat": 2.5, "tiers": {100: 2.0, 300: 2.0, 500: 2.0}, "kw": ["ซองการ์ดสีอื่นๆ 5x7", "ซองการ์ด 5x7", "ซอง 5x7"]},
    {"id": 201, "cat": "souvenir", "name": "ดินสอไม้มินิมอล", "mat": 2.5, "tiers": {100: 6.0, 300: 5.5, 500: 5.0}, "kw": ["ดินสอไม้มินิมอล", "ดินสอไม้", "ดินสอ"]},
    {"id": 202, "cat": "souvenir", "name": "เข็มกลัดดอกกุหลาบ", "mat": 2.8, "tiers": {100: 6.0, 300: 5.5, 500: 5.5}, "kw": ["เข็มกลัดดอกกุหลาบ", "ดอกกุหลาบ", "กุหลาบ"]},
    {"id": 203, "cat": "souvenir", "name": "เข็มกลัดดอกเดซี่", "mat": 2.8, "tiers": {100: 6.0, 300: 5.5, 500: 5.5}, "kw": ["เข็มกลัดดอกเดซี่", "ดอกเดซี่", "เดซี่"]},
    {"id": 204, "cat": "souvenir", "name": "เข็มกลัดดอกไม้ (ตัดเสียบ)", "mat": 2.8, "tiers": {100: 6.0, 300: 5.5, 500: 5.5}, "kw": ["เข็มกลัดดอกไม้", "เข็มกลัด"]},
    {"id": 205, "cat": "souvenir", "name": "น้ำหอมขวดจิ๋ว (ตัดเสียบ)", "mat": 5.0, "tiers": {100: 8.0, 300: 8.0, 500: 8.0}, "kw": ["น้ำหอมตัดเสียบ", "น้ำหอมขวดจิ๋วตัดเสียบ", "น้ำหอมขวดจิ๋ว", "น้ำหอม"]},
    {"id": 206, "cat": "souvenir", "name": "น้ำหอมขวดจิ๋ว (พับปิด)", "mat": 5.5, "tiers": {100: 8.0, 300: 8.0, 500: 8.0}, "kw": ["น้ำหอมพับปิด", "น้ำหอมขวดจิ๋วพับปิด"]},
    {"id": 207, "cat": "souvenir", "name": "สมุดโน้ต 3x4 นิ้ว", "mat": 3.0, "tiers": {100: 6.0, 300: 6.0, 500: 6.0}, "kw": ["สมุดโน้ต 3x4", "สมุด 3x4", "สมุดโน้ต", "สมุด"]},
    {"id": 208, "cat": "souvenir", "name": "พวงกุญแจ PU ถัก", "mat": 3.0, "tiers": {100: 7.0, 300: 6.5, 500: 6.0}, "kw": ["พวงกุญแจ pu", "pu ถัก", "กุญแจถัก", "พวงกุญแจ"]},
    {"id": 209, "cat": "souvenir", "name": "กระจกวิเศษทรงกลม", "mat": 3.0, "tiers": {100: 7.0, 300: 6.0, 500: 5.8}, "kw": ["กระจกวิเศษ", "กระจก"]},
    {"id": 210, "cat": "souvenir", "name": "ปากกาลูกลื่น", "mat": 3.0, "tiers": {100: 7.0, 300: 6.0, 500: 6.0}, "kw": ["ปากกาลูกลื่น", "ปากกา"]},
    {"id": 211, "cat": "souvenir", "name": "พิมเสน", "mat": 5.0, "tiers": {100: 8.0, 300: 7.0, 500: 6.0}, "kw": ["พิมเสน"]},
    {"id": 212, "cat": "souvenir", "name": "พวงกุญแจหนังแบบแบน", "mat": 5.0, "tiers": {100: 9.0, 300: 8.5, 500: 8.0}, "kw": ["พวงกุญแจหนัง", "กุญแจหนัง"]},
    {"id": 213, "cat": "souvenir", "name": "ผ้าขนหนู (ติด Tag)", "mat": 5.0, "tiers": {100: 10.0, 300: 8.0, 500: 7.0}, "kw": ["ผ้าขนหนู"]},
    {"id": 214, "cat": "souvenir", "name": "ยางมัดผม (แพ็คเกจปกติ)", "mat": 5.0, "tiers": {100: 10.0, 300: 9.0, 500: 8.0}, "kw": ["ยางมัดผมปกติ", "ยางมัดผม"]},
    {"id": 215, "cat": "souvenir", "name": "ยางมัดผม (พร้อมถุงแก้ว)", "mat": 9.0, "tiers": {100: 15.0, 300: 14.0, 500: 13.0}, "kw": ["ยางมัดผมถุงแก้ว", "ถุงแก้ว", "ยางมัดผมถุงตาข่าย", "ถุงตาข่าย"]},
    {"id": 216, "cat": "souvenir", "name": "กระเป๋าผ้า ถุงสปันบอนด์", "mat": 5.0, "tiers": {100: 10.0, 300: 9.0, 500: 8.0}, "kw": ["กระเป๋าผ้า", "สปันบอนด์", "ถุงผ้า"]},
    {"id": 217, "cat": "souvenir", "name": "หนังสือยาซีน มินิมอลสไตล์", "mat": 5.0, "tiers": {100: 10.0, 300: 9.0, 500: 8.0}, "kw": ["หนังสือยาซีน", "ยาซีน"]},
    {"id": 218, "cat": "souvenir", "name": "หนังสืออัซการ", "mat": 5.8, "tiers": {100: 12.0, 300: 11.5, 500: 10.0}, "kw": ["หนังสืออัซการ", "อัซการ"]},
    {"id": 219, "cat": "souvenir", "name": "สบู่หอม", "mat": 6.0, "tiers": {100: 12.0, 300: 12.0, 500: 12.0}, "kw": ["สบู่หอม", "สบู่"]},
    {"id": 220, "cat": "souvenir", "name": "ช้อนเงิน (ติด Tag)", "mat": 8.5, "tiers": {100: 15.0, 300: 14.0, 500: 14.0}, "kw": ["ช้อนเงิน"]},
    {"id": 221, "cat": "souvenir", "name": "ช้อนทอง (ติด Tag)", "mat": 9.0, "tiers": {100: 16.0, 300: 15.0, 500: 15.0}, "kw": ["ช้อนทอง"]},
    {"id": 222, "cat": "souvenir", "name": "สมุดโน้ต A5", "mat": 7.5, "tiers": {100: 15.0, 300: 15.0, 500: 15.0}, "kw": ["สมุดโน้ต a5", "สมุด a5", "a5"]},
    {"id": 223, "cat": "souvenir", "name": "น้ำผึ้ง 30ml", "mat": 11.0, "tiers": {100: 18.0, 300: 17.0, 500: 15.0}, "kw": ["น้ำผึ้ง"]},
    {"id": 224, "cat": "souvenir", "name": "น้ำหอมในรถ (50 ชิ้นขึ้นไป)", "mat": 15.5, "tiers": {100: 29.0, 300: 29.0, 500: 29.0}, "kw": ["น้ำหอมในรถ"]},
]


def get_line_display_name(user_id: str) -> str:
    """ดึงชื่อจริง (Display Name) ของลูกค้าจาก LINE Profile API อัตโนมัติ"""
    if not user_id:
        return ""
    try:
        profile = line_bot_api.get_profile(user_id)
        if profile and getattr(profile, "display_name", None):
            return profile.display_name.strip()
    except Exception as e:
        logger.warning(f"Could not fetch LINE profile for {user_id}: {e}")
    return ""


def record_recent_line_customer(user_id: str, last_text: str = "") -> str:
    """ดึงชื่อ LINE จริงของลูกค้าที่ทักเข้ามาและบันทึกลง Firestore เพื่อให้หน้าเว็บและ #order ดึงไปใช้ทันที"""
    display_name = get_line_display_name(user_id)
    if not display_name:
        return ""
    if db:
        try:
            doc_ref = db.collection("onestudio_admin").document("calculator_state")
            doc = doc_ref.get()
            state = doc.to_dict() if doc.exists else {}
            recent = state.get("recentCustomers", [])
            if not isinstance(recent, list):
                recent = []
            # ลบรายการซ้ำของ user_id เดิมออกแล้วดันชื่อล่าสุดขึ้นบนสุด
            recent = [c for c in recent if isinstance(c, dict) and c.get("userId") != user_id]
            recent.insert(0, {
                "userId": user_id,
                "displayName": display_name,
                "lastText": (last_text or "")[:60],
                "updatedAt": int(time.time()),
            })
            doc_ref.set({
                "latestLineCustomer": display_name,
                "recentCustomers": recent[:15],
                "updated_at": firestore.SERVER_TIMESTAMP,
            }, merge=True)
        except Exception as e:
            logger.warning(f"Failed to record recent LINE customer: {e}")
    return display_name


def match_catalog_product_smart(seg_lower: str, explicit_unit: float = None, custom_catalog: list = None):
    """จับคู่สินค้าจากข้อความแต่ละบรรทัด พร้อมแยกแยะรุ่นย่อยจากเรทราคาต่อชิ้นที่แอดมินพิมพ์ (เช่น 100*7฿ vs 100*8฿) และรองรับสินค้าที่แก้ไข/เพิ่มใหม่จากหน้าเว็บ"""
    by_id = {p["id"]: dict(p) for p in DEFAULT_ORDER_CATALOG}
    extra_custom_prods = []

    if isinstance(custom_catalog, list):
        for c in custom_catalog:
            if not isinstance(c, dict) or "id" not in c:
                continue
            cid = int(c["id"])
            if cid in by_id:
                if c.get("name"):
                    by_id[cid]["name"] = str(c["name"])
                if cid in (103, 104):
                    by_id[cid]["name"] = re.sub(r'\s*\+\s*ซองฟรี', '', by_id[cid]["name"]).replace(" (ไม่รวมซอง)", "") + " (ไม่รวมซอง)"
                if c.get("mat") is not None:
                    by_id[cid]["mat"] = float(c["mat"])
                if isinstance(c.get("tiers"), dict):
                    t = c["tiers"]
                    by_id[cid]["tiers"] = {
                        100: float(t.get("100", t.get(100, by_id[cid]["tiers"][100]))),
                        300: float(t.get("300", t.get(300, by_id[cid]["tiers"][300]))),
                        500: float(t.get("500", t.get(500, by_id[cid]["tiers"][500]))),
                    }
            else:
                cname = str(c.get("name") or "").strip()
                if cname:
                    clean_kw = re.sub(r'^[^\wก-๙]+', '', cname).strip().lower()
                    t = c.get("tiers") if isinstance(c.get("tiers"), dict) else {}
                    base_p = float(c.get("defaultPrice") or t.get("100") or t.get(100) or 5.0)
                    extra_custom_prods.append({
                        "id": cid,
                        "name": cname,
                        "cat": str(c.get("cat") or "ของชำร่วย"),
                        "mat": float(c.get("mat") or 0.0),
                        "tiers": {
                            100: float(t.get("100", t.get(100, base_p))),
                            300: float(t.get("300", t.get(300, base_p))),
                            500: float(t.get("500", t.get(500, base_p))),
                        },
                        "kw": [kw for kw in [clean_kw, cname.lower()] if kw],
                    })

    # ตรวจสอบสินค้าที่แอดมินเพิ่มใหม่ในแคตตาล็อกก่อน
    for cprod in extra_custom_prods:
        for kw in cprod["kw"]:
            if kw and kw in seg_lower:
                return cprod

    # กฎแยกแยะซองการ์ด (ทั้งอัปเกรดซองสีครีม/เขียว และซองการ์ดสีอื่นๆ ทั้งขนาด 4x6 และ 5x7 ต้นทุน 2.5฿)
    is_envelope_line = (
        ("ซอง" in seg_lower)
        and not any(k in seg_lower for k in ["แถมซอง", "+ ซอง", "+ซอง", "ซองฟรี", "หน้าเดียว", "หน้าหลัง", "หน้า-หลัง", "1 หน้า", "2 หน้า", "สองหน้า"])
        and (
            seg_lower.strip().startswith(("ซอง", "อัปเกรดซอง", "เปลี่ยนซอง", "เพิ่มซอง", "✉️"))
            or "ซองการ์ด" in seg_lower
            or "ซองสี" in seg_lower
            or "การ์ด" not in seg_lower
        )
    )
    if is_envelope_line:
        is_5x7_env = "5x7" in seg_lower or "5*7" in seg_lower or ("กว้าง 5" in seg_lower and "7" in seg_lower)
        is_4x6_env = "4x6" in seg_lower or "4*6" in seg_lower or ("กว้าง 4" in seg_lower and "6" in seg_lower)
        other_color_words = [
            "แดง", "ไวน์", "เบอร์กันดี", "น้ำตาล", "ทอง", "โรสโกลด์", "พิ้งโกลด์", "ชมพู",
            "ฟ้า", "น้ำเงิน", "กรม", "เทา", "เงิน", "ม่วง", "ส้ม", "เหลือง", "ดำ", "ขาว", "พาสเทล", "อื่นๆ"
        ]
        has_other_color = any(c in seg_lower for c in other_color_words)

        # กรณีอัปเกรดซองสีครีม (+1.5฿ ทุน 0.8฿) หรืออัปเกรดซองสีเขียว (+2฿ ทุน 1.2฿)
        if not has_other_color and "ซองการ์ด" not in seg_lower and not is_5x7_env and not is_4x6_env:
            if "ครีม" in seg_lower and (explicit_unit is None or explicit_unit <= 1.7):
                return by_id[105]
            if "เขียว" in seg_lower and (explicit_unit is None or explicit_unit <= 2.2):
                return by_id[106]
            if explicit_unit is not None and explicit_unit <= 1.6 and "สี" not in seg_lower:
                return by_id[105]
            if explicit_unit is not None and explicit_unit <= 2.1 and "สี" not in seg_lower:
                return by_id[106]

        # ซองการ์ดสีอื่นๆ ทั้งขนาด 4x6 และ 5x7 นิ้ว (ต้นทุน 2.5฿ เท่ากันทั้งสองขนาด ดึงสีและราคาตามที่สรุป)
        if is_5x7_env:
            return by_id[108]
        return by_id[107]

    # กฎแยกแยะการ์ดแต่งงานจากคำสำคัญหรือราคาขายต่อชิ้น (*ราคา฿)
    if "การ์ด" in seg_lower:
        is_5x7 = "5x7" in seg_lower or "5*7" in seg_lower
        is_double = "หน้า-หลัง" in seg_lower or "หน้าหลัง" in seg_lower or "2 หน้า" in seg_lower or "สองหน้า" in seg_lower
        is_single = "หน้าเดียว" in seg_lower or "1 หน้า" in seg_lower
        if is_5x7:
            if is_double or (explicit_unit is not None and explicit_unit >= 7.0):
                return by_id[104]
            return by_id[103]
        if "4x6" in seg_lower or "4*6" in seg_lower:
            if is_double or (explicit_unit is not None and explicit_unit >= 4.2):
                return by_id[102]
            return by_id[101]
        if explicit_unit is not None:
            if explicit_unit <= 3.8:
                return by_id[101]
            if explicit_unit <= 5.2:
                return by_id[102]
            if explicit_unit <= 6.5:
                return by_id[103]
            return by_id[104]
        if is_double:
            return by_id[102]
        if is_single:
            return by_id[101]
        return by_id[102]

    # กฎแยกแยะชื่อย่อที่แอดมินใช้บ่อยโดยดูจากคำสำคัญและราคาขายต่อชิ้น (*ราคา฿)
    if "พวงกุญแจ" in seg_lower or "กุญแจ" in seg_lower:
        if "หนัง" in seg_lower or "แบน" in seg_lower or "โบว์" in seg_lower or (explicit_unit is not None and explicit_unit >= 8):
            return by_id[212]
        return by_id[208]

    if "น้ำหอม" in seg_lower:
        if "ในรถ" in seg_lower or "รถยนต์" in seg_lower or (explicit_unit is not None and explicit_unit >= 20):
            return by_id[224]
        if "พับปิด" in seg_lower or "พับ" in seg_lower:
            return by_id[206]
        return by_id[205]

    if "ยางมัดผม" in seg_lower or "ถุงแก้ว" in seg_lower or "ถุงตาข่าย" in seg_lower:
        if "ถุงแก้ว" in seg_lower or "ถุงตาข่าย" in seg_lower or (explicit_unit is not None and explicit_unit >= 13):
            return by_id[215]
        return by_id[214]

    if "สมุด" in seg_lower:
        if "a5" in seg_lower or (explicit_unit is not None and explicit_unit >= 13):
            return by_id[222]
        return by_id[207]

    if "ช้อน" in seg_lower:
        if "ทอง" in seg_lower or (explicit_unit is not None and explicit_unit >= 16):
            return by_id[221]
        return by_id[220]

    sorted_cat = sorted(by_id.values(), key=lambda x: len(x["kw"][0]), reverse=True)
    for prod in sorted_cat:
        for kw in prod["kw"]:
            if kw.lower() in seg_lower:
                return prod
    return None


def _parse_mul_operands(val1_str: str, u1: str, val2_str: str, u2: str, total_str: str = None):
    """แยกแยะทั้งรูปแบบ จำนวน*ราคา฿ (100*18฿=฿1,800.00) และ ราคา฿*จำนวน (7.5฿*100=฿750.00 หรือ 3฿*100=฿300.00)"""
    v1 = float(val1_str.replace(',', ''))
    v2 = float(val2_str.replace(',', ''))
    u1_clean = (u1 or '').strip().lower()
    u2_clean = (u2 or '').strip().lower()
    price_units = {'฿', 'บาท'}
    qty_units = {'ชิ้น', 'ใบ', 'ซอง', 'อัน', 'ขวด', 'เล่ม', 'ชุด'}

    if u1_clean in price_units or u2_clean in qty_units:
        unit_sell, qty = v1, int(round(v2))
    elif u2_clean in price_units or u1_clean in qty_units:
        qty, unit_sell = int(round(v1)), v2
    elif '.' in val1_str and '.' not in val2_str:
        unit_sell, qty = v1, int(round(v2))
    elif '.' in val2_str and '.' not in val1_str:
        qty, unit_sell = int(round(v1)), v2
    elif v1 < 30 and v2 >= 30:
        unit_sell, qty = v1, int(round(v2))
    else:
        qty, unit_sell = int(round(v1)), v2

    explicit_total = float(total_str.replace(',', '')) if total_str else None
    return qty, unit_sell, explicit_total


def extract_order_items_from_text(raw_text: str, custom_mat_map: dict, custom_catalog: list = None) -> list:
    """สกัดรายการสินค้าทั้งรูปแบบ จำนวน*ราคา฿ (100*18฿=฿1,800) และ ราคา฿*จำนวน (7.5฿*100=฿750, 3฿*100=฿300) รวมถึงขนาด 5*7 / 4*6"""
    detected = []
    lines = raw_text.splitlines()
    raw_lower_all = raw_text.lower()
    order_has_5x7_card = bool(re.search(r'5\s*[\*x×]\s*7', raw_lower_all))
    order_has_4x6_card = bool(re.search(r'4\s*[\*x×]\s*6', raw_lower_all))

    mul_pattern = re.compile(
        r'(\d[\d,]*(?:\.\d+)?)\s*(฿|บาท|ชิ้น|ใบ|ซอง|อัน|ขวด|เล่ม|ชุด)?\s*[\*x×]\s*(\d[\d,]*(?:\.\d+)?)\s*(฿|บาท|ชิ้น|ใบ|ซอง|อัน|ขวด|เล่ม|ชุด)?(?:\s*=\s*฿?\s*([\d,]+(?:\.\d+)?)\s*(?:฿|บาท)?)?',
        flags=re.IGNORECASE,
    )

    for raw_line in lines:
        line_str = raw_line.strip()
        if not line_str:
            continue
        # ข้ามบรรทัดหัวคำสั่งหรือบรรทัดสรุปยอดท้ายบิล (แต่ไม่ข้าม "ยอดการ์ด...")
        if re.match(r'^(?:#\s*(?:order|oder|ออเดอร์|บิล)\s*$|ค่าส่ง|ส่งจริง|ยอดรวม|ยอดโอน|รวมทั้งสิ้น|มัดจำ|ส่วนลด|สถานะ|รอโอน|โอนเต็ม|จ่ายเต็ม|ชำระเต็ม)', line_str, flags=re.IGNORECASE):
            continue
        # ลบคำว่า #order / #oder ที่อยู่ต้นบรรทัด (กรณีพิมพ์บรรทัดเดียวกัน)
        line_str = re.sub(r'^#\s*(?:order|oder|ออเดอร์|บิล)\s*', '', line_str, flags=re.IGNORECASE).strip()
        if not line_str:
            continue

        # แยกด้วยเครื่องหมายจุลภาค เฉพาะจุลภาคที่ไม่ได้อยู่ในตัวเลขหลักพัน (เช่น 1,000)
        segments = re.split(r',(?!\d)', line_str)
        for seg in segments:
            seg_clean = seg.strip()
            if not seg_clean:
                continue
            # ตัดคำเกี่ยวกับค่าส่ง/ส่วนลดท้ายบรรทัดออกก่อนวิเคราะห์สินค้า
            seg_clean = re.sub(r'(ค่าส่ง\s*฿?\s*[\d,]+(?:\.\d+)?|ส่งจริง\s*฿?\s*[\d,]+(?:\.\d+)?|(?:ส่วนลด|ลด)\s*฿?\s*[\d,]+(?:\.\d+)?|ยอด(?:รวม|โอน)[^\n]*)', '', seg_clean, flags=re.IGNORECASE).strip()
            # ตัดเลขข้อนำหน้า เช่น "1. ", "2) ", "- "
            seg_clean = re.sub(r'^\s*(?:\d+(?:\.\d+)?\s*[\.\)\-]|\-|•)\s*', '', seg_clean).strip()
            if not seg_clean:
                continue

            # ตรวจจับการระบุต้นทุนต่อชิ้นในวงเล็บ เช่น "(ทุน 150)" หรือ "ทุน 3.5฿"
            explicit_mat = None
            mat_m = re.search(r'\(?\s*ทุน(?:ต่อชิ้น|ของ)?\s*฿?\s*(\d+(?:\.\d+)?)\s*(?:฿|บาท)?\s*\)?', seg_clean, flags=re.IGNORECASE)
            if mat_m:
                explicit_mat = float(mat_m.group(1))
                seg_clean = (seg_clean[:mat_m.start()] + " " + seg_clean[mat_m.end():]).strip()

            qty = None
            explicit_unit = None
            explicit_total = None
            mul_m = None

            # ค้นหาตัวคูณราคา/จำนวน โดยข้ามตัวเลขขนาดการ์ด/สมุด เช่น 4*6, 5*7, 3*4 ที่ไม่มีหน่วยเงินหรือเครื่องหมาย =
            all_mul_matches = list(mul_pattern.finditer(seg_clean))
            for cand in all_mul_matches:
                g1, u1, g2, u2, g_tot = cand.group(1), cand.group(2), cand.group(3), cand.group(4), cand.group(5)
                is_dim = (g1 in ('3', '4', '5') and g2 in ('4', '6', '7') and not u1 and not u2 and not g_tot)
                if is_dim:
                    continue
                mul_m = cand
                # หากเจอตัวที่มีเครื่องหมาย = หรือมีหน่วย ฿ ให้ใช้ตัวนั้นทันที
                if g_tot or u1 or u2:
                    break

            if mul_m:
                qty, explicit_unit, explicit_total = _parse_mul_operands(
                    mul_m.group(1), mul_m.group(2), mul_m.group(3), mul_m.group(4), mul_m.group(5)
                )
            else:
                # ตัดชื่อขนาดเช่น 4x6, 4*6, 5x7, 5*7, 3x4, 30ml, a5 ออกก่อนหาตัวเลขจำนวนชิ้น
                for_num = re.sub(r'(4\s*[\*x×]\s*6|5\s*[\*x×]\s*7|3\s*[\*x×]\s*4|30\s*ml|a5|5\.1|5\.2|13\.1|13\.2|18\.1|18\.2)', ' ', seg_clean, flags=re.IGNORECASE)
                num_m = re.search(r'(\d[\d,]*)', for_num)
                if num_m:
                    val = int(num_m.group(1).replace(',', ''))
                    if val >= 10:
                        qty = val

            prod = match_catalog_product_smart(seg_clean.lower(), explicit_unit, custom_catalog)
            if prod:
                if qty is None:
                    qty = 100
                # กรณีซองการ์ดสีอื่นๆ ที่ในบรรทัดไม่ได้ระบุขนาดซ้ำ แต่ในออเดอร์เดียวกันมีการ์ด 5x7 ให้ผูกเป็นซอง 5x7 (108) อัตโนมัติ (ต้นทุน 2.5฿ เท่ากันทั้งสองขนาด)
                if prod["id"] == 107 and order_has_5x7_card and not order_has_4x6_card and not re.search(r'4\s*[\*x×]\s*6', seg_clean):
                    prod = match_catalog_product_smart(seg_clean.lower() + " 5x7", explicit_unit, custom_catalog) or prod

                default_tier_sell = prod["tiers"][500] if qty >= 500 else (prod["tiers"][300] if qty >= 300 else prod["tiers"][100])
                unit_sell = explicit_unit if explicit_unit is not None else default_tier_sell
                line_total = round(explicit_total if explicit_total is not None else (qty * unit_sell), 1)
                mat_cost = explicit_mat if explicit_mat is not None else custom_mat_map.get(prod["id"], prod["mat"])
                item_display_name = prod["name"]

                # กรณีซองการ์ดสีอื่นๆ (107 = 4x6 นิ้ว, 108 = 5x7 นิ้ว) ให้ดึงสีและชื่อตามที่สรุปไว้ในบิล พร้อมใช้ต้นทุน 2.5฿ ทั้งสองขนาด
                if prod["id"] in (107, 108):
                    if explicit_mat is None and prod["id"] not in custom_mat_map:
                        mat_cost = 2.5
                    if mul_m:
                        raw_env_label = (seg_clean[:mul_m.start()] + " " + seg_clean[mul_m.end():]).strip(" -•:=")
                    else:
                        raw_env_label = re.sub(r'\b' + str(qty) + r'\s*(?:ชิ้น|ใบ|ซอง|อัน)?\b', '', seg_clean, count=1).strip(" -•:=")
                    if raw_env_label:
                        has_5x7_label = bool(re.search(r'5\s*[\*x×]\s*7', raw_env_label))
                        has_4x6_label = bool(re.search(r'4\s*[\*x×]\s*6', raw_env_label))
                        if prod["id"] == 108 and not has_5x7_label:
                            size_suffix = " (5x7 นิ้ว)"
                        elif prod["id"] == 107 and not has_4x6_label and order_has_4x6_card:
                            size_suffix = " (4x6 นิ้ว)"
                        else:
                            size_suffix = ""
                        prefix = "" if raw_env_label.startswith("✉️") else "✉️ "
                        item_display_name = f"{prefix}{raw_env_label}{size_suffix}"

                detected.append({
                    "id": prod["id"],
                    "name": item_display_name,
                    "cat": prod["cat"],
                    "qty": qty,
                    "unit_sell": unit_sell,
                    "unit_mat": mat_cost,
                    "line_total": line_total,
                    "line_mat": round(qty * mat_cost, 1),
                })
            elif mul_m and qty and explicit_unit is not None:
                # รองรับสินค้าพิเศษ / สินค้าใหม่นอกแคตตาล็อกอัตโนมัติ (เช่น "6. ค่าบล็อกฟอยล์ 1*300฿=฿300.00")
                raw_custom_name = (seg_clean[:mul_m.start()] + " " + seg_clean[mul_m.end():]).strip(" -•:=")
                custom_name = f"✨ {raw_custom_name}" if raw_custom_name else "✨ สินค้าพิเศษ (นอกแคตตาล็อก)"
                line_total = round(explicit_total if explicit_total is not None else (qty * explicit_unit), 1)
                mat_cost = explicit_mat if explicit_mat is not None else 0.0
                detected.append({
                    "id": 0,
                    "name": custom_name,
                    "cat": "สินค้าพิเศษ",
                    "qty": qty,
                    "unit_sell": explicit_unit,
                    "unit_mat": mat_cost,
                    "line_total": line_total,
                    "line_mat": round(qty * mat_cost, 1),
                })

    # บังคับให้ขนาดการ์ด (4x6 หรือ 5x7) และขนาดซองการ์ด (107=4x6, 108=5x7) ในบิลเดียวกันเป็นขนาดเดียวกันเสมอ
    card_ids_in_order = [it["id"] for it in detected if it["id"] in (101, 102, 103, 104)]
    target_env_size = None
    if any(cid in (103, 104) for cid in card_ids_in_order):
        target_env_size = "5x7"
    elif any(cid in (101, 102) for cid in card_ids_in_order):
        target_env_size = "4x6"

    if target_env_size:
        for it in detected:
            if it["id"] in (107, 108):
                it["id"] = 108 if target_env_size == "5x7" else 107
                base_label = re.sub(r'\s*\((?:ขนาด\s*)?(?:4\s*[\*x×]\s*6|5\s*[\*x×]\s*7)\s*(?:นิ้ว)?\)', '', it["name"], flags=re.IGNORECASE).strip()
                base_label = re.sub(r'\s*(?:4\s*[\*x×]\s*6|5\s*[\*x×]\s*7)\s*(?:นิ้ว)?', '', base_label, flags=re.IGNORECASE).strip()
                it["name"] = f"{base_label} (ขนาด {target_env_size} นิ้ว)"

    return detected


def parse_and_build_order_summary(raw_text: str, user_id: str = None):
    """สกัดคำสั่ง #oder / #order พร้อมดึงชื่อจริงของลูกค้าจาก LINE OA, คำนวณกำไร 4 กองทุน และซิงค์เข้าหน้าแอปทันที"""
    discount_match = re.search(r'(?:ส่วนลด|ลด)\s*฿?\s*([\d,]+(?:\.\d+)?)', raw_text)
    free_ship = "ส่งฟรี" in raw_text
    ship_charge_match = re.search(r'ค่าส่ง\s*฿?\s*([\d,]+(?:\.\d+)?)', raw_text)
    ship_cost_match = re.search(r'ส่งจริง\s*฿?\s*([\d,]+(?:\.\d+)?)', raw_text)
    transfer_match = re.search(r'ยอดโอน(?:สุทธิ)?\s*฿?\s*([\d,]+(?:\.\d+)?)', raw_text)

    # ตรวจจับการติ๊ก/ระบุสถานะการชำระเงินจากคำสั่ง (รอโอน / มัดจำ 50% / โอนเต็ม)
    if re.search(r'(โอนเต็ม|จ่ายเต็ม|ชำระเต็ม|โอนครบ|จ่ายครบ|100\s*%)', raw_text, flags=re.IGNORECASE):
        order_status = "paid_full"
    elif re.search(r'(รอโอน|ยังไม่โอน|แจ้งยอด)', raw_text, flags=re.IGNORECASE):
        order_status = "pending"
    else:
        order_status = "deposit_50"

    ship_charge = 0 if free_ship else (int(float(ship_charge_match.group(1).replace(',', ''))) if ship_charge_match else 0)
    ship_cost = int(float(ship_cost_match.group(1).replace(',', ''))) if ship_cost_match else (100 if free_ship else ship_charge)

    # โหลดต้นทุนของที่แอดมินตั้งค่าไว้ การตั้งค่าร้าน (shopSettings) และชื่อลูกค้า LINE ล่าสุดจาก Firestore (ถ้ามี)
    custom_mat_map = {}
    custom_catalog = []
    saved_state = {}
    if db:
        try:
            doc = db.collection("onestudio_admin").document("calculator_state").get()
            if doc.exists:
                saved_state = doc.to_dict() or {}
                if saved_state.get("catalogVersion") == 4:
                    custom_catalog = saved_state.get("catalog", []) if isinstance(saved_state.get("catalog"), list) else []
                    for item in custom_catalog:
                        if isinstance(item, dict) and "id" in item and "mat" in item:
                            custom_mat_map[int(item["id"])] = float(item["mat"])
        except Exception as e:
            logger.warning(f"Failed to load custom catalog from Firestore: {e}")

    shop_settings = saved_state.get("shopSettings") if isinstance(saved_state.get("shopSettings"), dict) else {}
    bank_info = str(shop_settings.get("bankInfo") or "").strip()
    lead_time_text = str(
        shop_settings.get("leadTimeText")
        or "✨ ออกแบบฟรี! ของชำร่วยใช้เวลาผลิต 7–25 วัน (การ์ด 7–15 วัน) หลังคอนเฟิร์มแบบนะคะ"
    ).strip()
    supplies_pct = float(shop_settings.get("suppliesPct", 10) or 10)
    wage_pct = float(shop_settings.get("wagePct", 30) or 30)
    house_pct = float(shop_settings.get("housePct", 30) or 30)
    vehicle_pct = float(shop_settings.get("vehiclePct", 30) or 30)

    # ดึงชื่อลูกค้าจริงจาก LINE OA ล่าสุดก่อนเสมอ (เพื่อไม่ให้ดึงชื่อแอดมินแทนชื่อลูกค้า)
    cust_match = re.search(r'(?:#\s*(?:order|oder|ออเดอร์|บิล)\s*)?(คุณ[^\s,]+)', raw_text, re.IGNORECASE)
    recent_list = saved_state.get("recentCustomers", []) if isinstance(saved_state.get("recentCustomers"), list) else []
    if not recent_list and db:
        recent_list = discover_real_line_customers_from_firestore([])

    latest_cust_uid = recent_list[0].get("userId", "") if recent_list and isinstance(recent_list[0], dict) else ""
    if cust_match:
        customer = cust_match.group(1)
    else:
        line_name = (
            saved_state.get("latestLineCustomer")
            or (recent_list[0].get("displayName", "") if recent_list and isinstance(recent_list[0], dict) else "")
            or get_line_display_name(user_id)
            or ""
        )
        if line_name:
            customer = line_name if line_name.startswith("คุณ") else f"คุณ{line_name}"
        else:
            customer = "คุณลูกค้า (LINE)"

    # 1) สกัดสินค้าจากข้อความคำสั่ง #oder / #order ที่แอดมินพิมพ์
    detected = extract_order_items_from_text(raw_text, custom_mat_map, custom_catalog)

    # 2) หากแอดมินพิมพ์แค่ #oder / #order เฉยๆ ให้ดึงรายการสินค้าจากข้อความแชทล่าสุดของลูกค้าใน LINE OA อัตโนมัติ!
    if not detected and db and (latest_cust_uid or user_id):
        target_uid = latest_cust_uid or user_id
        try:
            msgs = list(
                db.collection("chat_history")
                .document(target_uid)
                .collection("messages")
                .order_by("timestamp", direction=firestore.Query.DESCENDING)
                .limit(8)
                .stream()
            )
            combined_user_text = "\n".join(
                str((m.to_dict() or {}).get("text", ""))
                for m in msgs
                if (m.to_dict() or {}).get("role") == "user"
            )
            if combined_user_text:
                detected = extract_order_items_from_text(combined_user_text, custom_mat_map, custom_catalog)
        except Exception as e:
            logger.warning(f"Failed to auto-extract items from customer chat_history: {e}")

    if not detected:
        return None

    subtotal = sum(i["line_total"] for i in detected)
    discount = int(float(discount_match.group(1).replace(',', ''))) if discount_match else 0
    if transfer_match and not discount_match:
        explicit_net = int(float(transfer_match.group(1).replace(',', '')))
        if 0 < explicit_net < (subtotal + ship_charge):
            discount = int((subtotal + ship_charge) - explicit_net)

    net_rev = max(0, subtotal + ship_charge - discount)
    deposit = round(net_rev / 2)
    remaining_amt = max(0, net_rev - deposit)
    total_mat = sum(i["line_mat"] for i in detected)
    total_cost = total_mat + ship_cost
    profit = net_rev - total_cost

    supplies = max(0, profit * (supplies_pct / 100.0))
    rem_pct = wage_pct + house_pct + vehicle_pct
    rem = max(0, profit - supplies)
    if rem_pct > 0:
        wage = rem * (wage_pct / rem_pct)
        house = rem * (house_pct / rem_pct)
        vehicle = rem * (vehicle_pct / rem_pct)
    else:
        wage = rem / 3
        house = rem / 3
        vehicle = rem / 3

    ord_no = f"ORD-{random.randint(100000, 999999)}"
    now_ts = int(time.time())
    order_date_str = time.strftime("%d/%m/%Y", time.localtime(now_ts + 7 * 3600))

    order_items_payload = [
        {
            "prodId": i["id"],
            "name": i["name"],
            "qty": i["qty"],
            "unitSell": i["unit_sell"],
            "unitMat": i.get("unit_mat", 0),
            "lineTotal": i["line_total"],
        }
        for i in detected
    ]

    # 3) สกัดวันนัดส่งมอบ / วันจัดงาน / สถานะการผลิต / ขนส่ง / เลขพัสดุ จากข้อความ (ถ้ามี)
    due_match = re.search(r'(?:กำหนดส่ง|วันนัดส่ง|นัดส่ง|วันงาน|ส่งก่อน|ส่งวันที่|นัดรับ)\s*[:=]?\s*([0-9]{1,2}[/-][0-9]{1,2}(?:[/-][0-9]{2,4})?|[^\n,]+)', raw_text, re.IGNORECASE)
    due_date = due_match.group(1).strip() if due_match else ""

    prod_status = "draft"
    t_raw = raw_text.lower()
    if any(k in t_raw for k in ["ส่งแล้ว", "จัดส่งแล้ว", "จัดส่งเรียบร้อย", "shipped"]):
        prod_status = "shipped"
    elif any(k in t_raw for k in ["กำลังผลิต", "ส่งพิมพ์", "พิมพ์แล้ว", "สั่งพิมพ์แล้ว", "ผลิตอยู่", "in production"]):
        prod_status = "production"
    elif any(k in t_raw for k in ["รอคอนเฟิร์ม", "รอแบบ", "ดราฟต์", "draft", "แก้แบบ"]):
        prod_status = "draft"

    courier = ""
    for c_name in ["Flash Express", "Flash", "Kerry", "KEX", "ไปรษณีย์ไทย", "EMS", "J&T Express", "J&T"]:
        if c_name.lower() in t_raw:
            courier = c_name
            break
    track_match = re.search(r'(?:เลขพัสดุ|tracking|track|เลขแทรค|แทรค)\s*[:=]?\s*([A-Za-z0-9_-]{8,30})', raw_text, re.IGNORECASE)
    tracking_no = track_match.group(1).strip() if track_match else ""

    latest_order_payload = {
        "ordNo": ord_no,
        "orderDate": order_date_str,
        "dueDate": due_date,
        "customerName": customer,
        "rawCommand": raw_text,
        "shipCharge": ship_charge,
        "shipCost": ship_cost,
        "discount": discount,
        "netRevenue": net_rev,
        "totalMaterial": total_mat,
        "totalCost": total_cost,
        "grossProfit": profit,
        "supplies": supplies,
        "wage": wage,
        "house": house,
        "vehicle": vehicle,
        "status": order_status,
        "prodStatus": prod_status,
        "courier": courier,
        "trackingNo": tracking_no,
        "items": order_items_payload,
        "updatedAt": now_ts,
    }

    # บันทึกทั้งประวัติสะสม (orderHistory พร้อมรายละเอียดสินค้าเพื่อเปิดบิลย้อนหลังได้) และออเดอร์ล่าสุด (latestOrder) ลง Firestore
    if db:
        try:
            history = saved_state.get("orderHistory", [])
            if not isinstance(history, list):
                history = []
            history.insert(0, {
                "ordNo": ord_no,
                "orderDate": order_date_str,
                "dueDate": due_date,
                "customerName": customer,
                "shipCharge": ship_charge,
                "shipCost": ship_cost,
                "discount": discount,
                "netRevenue": net_rev,
                "totalMaterial": total_cost,
                "grossProfit": profit,
                "supplies": supplies,
                "wage": wage,
                "house": house,
                "vehicle": vehicle,
                "status": order_status,
                "prodStatus": prod_status,
                "courier": courier,
                "trackingNo": tracking_no,
                "items": order_items_payload,
            })
            history[0]["vehicle"] = vehicle
            db.collection("onestudio_admin").document("calculator_state").set(
                {
                    "stateVersion": 2,
                    "latestOrder": latest_order_payload,
                    "orderHistory": history[:200],
                    "updated_at": firestore.SERVER_TIMESTAMP,
                },
                merge=True,
            )
            # เมื่อออกบิลออเดอร์แล้ว ให้พักบอทสำหรับลูกค้ารายนี้อัตโนมัติ เพื่อให้แอดมินคนจริงเข้าดูแลต่อ
            target_mute_uid = user_id or latest_cust_uid
            if target_mute_uid:
                set_user_bot_muted(target_mute_uid, True, reason=f"order_{ord_no}")
        except Exception as e:
            logger.warning(f"Failed to save #order to Firestore: {e}")

    items_str = "\n".join(
        f"{idx+1}. {i['name']}\n   {i['qty']:,} ชิ้น × {i['unit_sell']:g}฿ = {i['line_total']:,.0f} บาท"
        for idx, i in enumerate(detected)
    )
    ship_str = f"{ship_charge:,} บาท" if ship_charge > 0 else "ส่งฟรี! 🎁"
    disc_str = f"\nส่วนลดพิเศษ: -{discount:,} บาท" if discount > 0 else ""
    bank_block = f"\n🏦 ช่องทางชำระเงิน:\n{bank_info}\n" if bank_info else ""

    if order_status == "paid_full":
        payment_status_line = f"☑️ สถานะ: โอนเต็มจำนวนแล้ว (100% = {net_rev:,.0f} บาท) ✅"
    elif order_status == "pending":
        payment_status_line = f"⏳ สถานะ: รอโอน (มัดจำ 50% = {deposit:,.0f} บาท / โอนเต็ม = {net_rev:,.0f} บาท)"
    else:
        payment_status_line = f"💳 สถานะ: มัดจำเริ่มงาน 50% ({deposit:,.0f} บาท) • คงเหลือ {remaining_amt:,.0f} บาท"

    due_line = f"📅 กำหนดส่งมอบ / วันงาน: {due_date}\n" if due_date else ""
    track_line = f"📦 เลขพัสดุ ({courier or 'จัดส่ง'}): {tracking_no}\n" if tracking_no else ""

    customer_bill = (
        f"🌸 ใบสรุปรายการคำสั่งซื้อ (ONESTUDIO) 🌸\n"
        f"เลขที่ออเดอร์: {ord_no} ({order_date_str})\n"
        f"ลูกค้า: {customer}\n"
        f"{due_line}"
        f"━━━━━━━━━━━━━━\n"
        f"{items_str}\n"
        f"━━━━━━━━━━━━━━\n"
        f"ยอดรวมค่าสินค้า: {subtotal:,.0f} บาท\n"
        f"🚚 ค่าจัดส่ง: {ship_str}{disc_str}\n"
        f"💰 ยอดชำระสุทธิ: {net_rev:,.0f} บาท\n"
        f"{payment_status_line}\n"
        f"{track_line}"
        f"{bank_block}"
        f"━━━━━━━━━━━━━━\n"
        f"{lead_time_text}\n"
        f"ขอบพระคุณที่ไว้วางใจให้ ONESTUDIO ดูแลในวันสำคัญนะคะ 💕🙏"
    )

    admin_summary = (
        f"🔒 [สรุปกำไร 4 กองทุน - {customer} ({ord_no})]\n"
        f"🛒 ทุนของ ({total_mat:,.0f}฿) + ส่งจริง ({ship_cost:,.0f}฿) = -{total_cost:,.0f}฿\n"
        f"💰 กำไรสุทธิแบ่ง 4 กอง: {profit:,.0f}฿\n"
        f"• 👤 ค่าแรงตัวเอง ({wage_pct:g}%): {wage:,.0f}฿\n"
        f"• 🛍️ ซื้อของอื่นๆ ({supplies_pct:g}%): {supplies:,.0f}฿\n"
        f"• 🏠 ค่างวดบ้าน ({house_pct:g}%): {house:,.0f}฿\n"
        f"• 🚗 ค่างวดรถ ({vehicle_pct:g}%): {vehicle:,.0f}฿\n"
        f"📱 ดูแดชบอร์ดสะสมบน iPhone: https://line-bot-alpha-six.vercel.app/calculator"
    )
    return customer_bill, admin_summary


@app.get("/")
def read_root():
    return {"message": "Hello from Line OA Gemini Agent! Firebase Memory is active."}


@app.get("/calculator", response_class=HTMLResponse)
@app.get("/app", response_class=HTMLResponse)
def serve_calculator_app():
    """เปิดหน้าเว็บแอปคำนวณต้นทุน-กำไร 4 กองทุนสำหรับ iPhone / มือถือ"""
    html_path = Path(__file__).parent / "calculator.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>ไม่พบไฟล์ calculator.html</h1>", status_code=404)


def discover_real_line_customers_from_firestore(existing_recent: list = None) -> list:
    """สแกนประวัติแชทจริงของ LINE Bot ใน Firestore (chat_history) แล้วดึงชื่อจริงจาก LINE Profile API"""
    if not db:
        return existing_recent or []
    DEMO_NAMES = {"คุณแพรว", "คุณแพรว (line)", "แพรว 🌸", "minny 💕", "เมย์ พัชรพร"}
    cleaned = []
    seen_uids = set()
    for c in (existing_recent or []):
        if isinstance(c, dict):
            dname = (c.get("displayName") or "").strip()
            uid = (c.get("userId") or "").strip()
            if dname and dname.lower() not in DEMO_NAMES and uid:
                cleaned.append(c)
                seen_uids.add(uid)
    try:
        doc_refs = list(db.collection("chat_history").list_documents(page_size=20))
        discovered = []
        for uref in doc_refs:
            uid = uref.id
            if not uid or uid in seen_uids:
                continue
            last_text = ""
            last_ts = 0
            try:
                msgs = list(
                    uref.collection("messages")
                    .order_by("timestamp", direction=firestore.Query.DESCENDING)
                    .limit(4)
                    .stream()
                )
                for m in msgs:
                    mdata = m.to_dict() or {}
                    ts = mdata.get("timestamp")
                    if ts and hasattr(ts, "timestamp") and last_ts == 0:
                        last_ts = int(ts.timestamp())
                    if mdata.get("role") == "user" and mdata.get("text") and not last_text:
                        last_text = str(mdata.get("text"))[:60]
            except Exception:
                pass
            display_name = get_line_display_name(uid)
            if display_name:
                discovered.append({
                    "userId": uid,
                    "displayName": display_name,
                    "lastText": last_text,
                    "updatedAt": last_ts or int(time.time()),
                })
                seen_uids.add(uid)
        merged = cleaned + discovered
        merged.sort(key=lambda x: x.get("updatedAt", 0), reverse=True)
        return merged[:15]
    except Exception as e:
        logger.warning(f"Failed to discover real LINE customers from chat_history: {e}")
        return cleaned[:15]


@app.get("/api/calculator-state")
def get_calculator_state(refresh: int = 0):
    """โหลดข้อมูลต้นทุนสินค้า ยอดสะสม เป้าหมายรายเดือน และรายชื่อลูกค้าจริงจาก LINE OA"""
    if not db:
        return JSONResponse({"ok": False, "reason": "no_db"})
    try:
        doc_ref = db.collection("onestudio_admin").document("calculator_state")
        doc = doc_ref.get()
        data = (doc.to_dict() or {}) if doc.exists else {}

        state_version = data.get("stateVersion", 0)
        order_history = data.get("orderHistory", [])
        if not isinstance(order_history, list):
            order_history = []
        recent_customers = data.get("recentCustomers", [])
        if not isinstance(recent_customers, list):
            recent_customers = []
        latest_customer = data.get("latestLineCustomer", "")

        needs_save = False
        DEMO_NAMES = {"คุณแพรว", "คุณแพรว (line)", "คุณแพรว & คุณนนท์", "แพรว 🌸", "minny 💕", "เมย์ พัชรพร"}

        # รีเซ็ตข้อมูลตัวอย่าง (คุณแพรว ฯลฯ) ออกทั้งหมด และเชื่อมข้อมูลจริงจาก LINE Bot
        if state_version < 2:
            order_history = [
                h for h in order_history
                if isinstance(h, dict) and (h.get("customerName") or "").strip().lower() not in DEMO_NAMES
            ]
            recent_customers = [
                c for c in recent_customers
                if isinstance(c, dict) and (c.get("displayName") or "").strip().lower() not in DEMO_NAMES
            ]
            if latest_customer.strip().lower() in DEMO_NAMES:
                latest_customer = ""
            state_version = 2
            needs_save = True

        if not recent_customers or refresh == 1:
            recent_customers = discover_real_line_customers_from_firestore(recent_customers)
            if recent_customers and not latest_customer:
                latest_customer = recent_customers[0].get("displayName", "")
            elif recent_customers and refresh == 1:
                latest_customer = recent_customers[0].get("displayName", "")
            needs_save = True

        if needs_save:
            doc_ref.set({
                "stateVersion": 2,
                "orderHistory": order_history,
                "recentCustomers": recent_customers,
                "latestLineCustomer": latest_customer,
                "updated_at": firestore.SERVER_TIMESTAMP,
            }, merge=True)

        return JSONResponse({
            "ok": True,
            "stateVersion": 2,
            "catalogVersion": data.get("catalogVersion", 0),
            "catalog": data.get("catalog", []),
            "shopSettings": data.get("shopSettings", {}),
            "latestOrder": data.get("latestOrder"),
            "orderHistory": order_history,
            "monthlyTargets": data.get("monthlyTargets", {"wage": 15000, "house": 15000, "vehicle": 10000}),
            "latestLineCustomer": latest_customer,
            "recentCustomers": recent_customers,
        })
    except Exception as e:
        logger.error(f"Error loading calculator state: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/line-customer-messages")
def get_line_customer_messages(userId: str = ""):
    """ดึงข้อความแชทจริงล่าสุดของลูกค้าจาก LINE Bot (คอลเลกชัน chat_history)"""
    if not db or not userId:
        return JSONResponse({"ok": False, "messages": []})
    try:
        messages_ref = db.collection("chat_history").document(userId).collection("messages")
        docs = list(messages_ref.order_by("timestamp", direction=firestore.Query.DESCENDING).limit(12).stream())
        docs.reverse()
        messages = []
        for d in docs:
            m = d.to_dict() or {}
            text = (m.get("text") or "").strip()
            role = m.get("role") or "user"
            if not text or "[SILENCE]" in text:
                continue
            clean = re.sub(r'\[IMAGE:\s*https?://[^\s\]]+\]', '🖼️ [ส่งรูปภาพตัวอย่างสินค้า]', text).strip()
            messages.append({
                "role": role,
                "text": clean[:500],
            })
        return JSONResponse({"ok": True, "userId": userId, "messages": messages})
    except Exception as e:
        logger.warning(f"Error loading customer messages for {userId}: {e}")
        return JSONResponse({"ok": False, "messages": []})


@app.get("/api/bot-mute-status")
def get_bot_mute_status(userId: str = ""):
    """ตรวจสอบสถานะพักบอท (แอดมินคนจริงคุยเอง) สำหรับลูกค้ารายนี้"""
    if not userId:
        return JSONResponse({"ok": False, "reason": "missing_userId"})
    muted = is_user_bot_muted(userId)
    return JSONResponse({"ok": True, "userId": userId, "muted": muted})


@app.post("/api/toggle-bot-mute")
async def toggle_bot_mute_api(request: Request):
    """สั่งเปิด/พักบอทสำหรับลูกค้ารายนี้จากหน้าแดชบอร์ด"""
    try:
        data = await request.json()
        user_id = str(data.get("userId") or "").strip()
        muted = bool(data.get("muted", False))
        if not user_id:
            return JSONResponse({"ok": False, "reason": "missing_userId"}, status_code=400)
        set_user_bot_muted(user_id, muted, reason="admin_dashboard_toggle")
        return JSONResponse({"ok": True, "userId": user_id, "muted": muted})
    except Exception as e:
        logger.error(f"Error toggling bot mute: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/admin-alerts")
def get_admin_alerts():
    """ดึงรายการแจ้งเตือนที่ยังไม่ได้อ่านสำหรับแสดงบนแดชบอร์ดแบบเรียลไทม์"""
    if not db:
        return JSONResponse({"ok": True, "alerts": []})
    try:
        docs = list(
            db.collection("admin_alerts")
            .where("read", "==", False)
            .order_by("created_at_ts", direction=firestore.Query.DESCENDING)
            .limit(20)
            .stream()
        )
        alerts = [d.to_dict() for d in docs if d.to_dict()]
        return JSONResponse({"ok": True, "alerts": alerts})
    except Exception as e:
        logger.warning(f"Error fetching admin alerts: {e}")
        return JSONResponse({"ok": False, "alerts": []})


@app.post("/api/dismiss-alert")
async def dismiss_admin_alert(request: Request):
    """ทำเครื่องหมายว่าอ่านหรือปิดการแจ้งเตือนแล้ว"""
    if not db:
        return JSONResponse({"ok": False, "reason": "no_db"})
    try:
        data = await request.json()
        alert_id = str(data.get("alertId") or "").strip()
        dismiss_all = bool(data.get("all", False))
        user_id = str(data.get("userId") or "").strip()

        if dismiss_all:
            docs = list(db.collection("admin_alerts").where("read", "==", False).limit(50).stream())
            for d in docs:
                d.reference.update({"read": True, "read_at": firestore.SERVER_TIMESTAMP})
            return JSONResponse({"ok": True, "dismissed": len(docs)})
        elif alert_id:
            db.collection("admin_alerts").document(alert_id).update({
                "read": True,
                "read_at": firestore.SERVER_TIMESTAMP
            })
            return JSONResponse({"ok": True, "alertId": alert_id})
        elif user_id:
            docs = list(db.collection("admin_alerts").where("userId", "==", user_id).where("read", "==", False).stream())
            for d in docs:
                d.reference.update({"read": True, "read_at": firestore.SERVER_TIMESTAMP})
            return JSONResponse({"ok": True, "userId": user_id, "dismissed": len(docs)})
        return JSONResponse({"ok": False, "reason": "invalid_params"}, status_code=400)
    except Exception as e:
        logger.error(f"Error dismissing alert: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/calculator-state")
async def save_calculator_state(request: Request):
    """บันทึกข้อมูลต้นทุนสินค้า ยอดสะสม และเป้าหมายรายเดือนลง Firebase Firestore"""
    if not db:
        return JSONResponse({"ok": False, "reason": "no_db"})
    try:
        payload = await request.json()
        update_data = {"stateVersion": 2, "updated_at": firestore.SERVER_TIMESTAMP}
        DEMO_NAMES = {"คุณแพรว", "คุณแพรว (line)", "คุณแพรว & คุณนนท์"}
        if "catalogVersion" in payload:
            update_data["catalogVersion"] = payload["catalogVersion"]
        if "catalog" in payload and isinstance(payload["catalog"], list):
            update_data["catalog"] = payload["catalog"]
        if "orderHistory" in payload and isinstance(payload["orderHistory"], list):
            clean_hist = [
                h for h in payload["orderHistory"]
                if isinstance(h, dict) and (h.get("customerName") or "").strip().lower() not in DEMO_NAMES
            ]
            update_data["orderHistory"] = clean_hist[:200]
        if "monthlyTargets" in payload and isinstance(payload["monthlyTargets"], dict):
            update_data["monthlyTargets"] = payload["monthlyTargets"]
        if "shopSettings" in payload and isinstance(payload["shopSettings"], dict):
            update_data["shopSettings"] = payload["shopSettings"]
        if "latestOrder" in payload and isinstance(payload["latestOrder"], dict):
            update_data["latestOrder"] = payload["latestOrder"]
        db.collection("onestudio_admin").document("calculator_state").set(update_data, merge=True)
        return JSONResponse({"ok": True})
    except Exception as e:
        logger.error(f"Error saving calculator state: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@app.post("/webhook")
async def webhook(request: Request):
    """
    Endpoint สำหรับรับ Webhook จาก Line
    (ประมวลผลทันทีเพื่อรองรับทั้ง Serverless อย่าง Vercel และ Standalone Server)
    """
    # ตรวจสอบ Signature จาก Header
    signature = request.headers.get("x-line-signature", "")
    
    # อ่าน body ของ Request
    body = await request.body()
    body_str = body.decode("utf-8")
    
    handle_line_webhook(body_str, signature)
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
    รองรับการรวมข้อความที่พิมพ์ส่งรัวๆ เข้ามาด้วยกัน (Debounce Buffer)
    """
    user_text = event.message.text
    reply_token = event.reply_token
    user_id = event.source.user_id
    message_id = event.message.id
    
    logger.info(f"Received message from user {user_id}: {user_text}")
    
    # -------------------------------------------------------------
    # ระบบป้องกัน Webhook Retry ซ้ำซ้อน (ป้องกันบอทตอบซ้ำ 2-3 รอบจาก LINE timeout retry)
    # -------------------------------------------------------------
    global processed_message_ids
    if message_id in processed_message_ids:
        logger.info(f"Duplicate message_id {message_id} detected in memory, skipping.")
        return
    processed_message_ids.add(message_id)
    if len(processed_message_ids) > 1000:
        processed_message_ids.pop()

    if db:
        try:
            msg_doc_ref = db.collection('processed_message_ids').document(message_id)
            if msg_doc_ref.get().exists:
                logger.info(f"Duplicate message_id {message_id} found in Firestore, skipping.")
                return
            msg_doc_ref.set({"created_at": firestore.SERVER_TIMESTAMP})
        except Exception as dedup_err:
            logger.warning(f"Deduplication check error: {dedup_err}")

    # -------------------------------------------------------------
    # 0. คำสั่งพักบอท / เปิดบอท สำหรับแอดมินคนจริง (#pause, #mute, #พักบอท, #ปิดบอท, #unpause, #เปิดบอท)
    # -------------------------------------------------------------
    clean_cmd = user_text.strip().lower()
    if re.match(r'^#\s*(?:pause|mute|พักบอท|ปิดบอท|แอดมิน|admin)\b', clean_cmd):
        set_user_bot_muted(user_id, True, reason="admin_command_mute")
        line_bot_api.reply_message(
            reply_token,
            TextSendMessage(text="🔴 แอดมินเข้าดูแล: บอทหยุดตอบลูกค้ารายนี้แล้วค่ะ\n(เมื่อคุยจบ สามารถพิมพ์ #เปิดบอท หรือ #unpause เพื่อให้บอทกลับมาตอบตามปกติได้นะคะ)")
        )
        return

    if re.match(r'^#\s*(?:unpause|unmute|เปิดบอท|บอท)\b', clean_cmd):
        set_user_bot_muted(user_id, False, reason="admin_command_unmute")
        line_bot_api.reply_message(
            reply_token,
            TextSendMessage(text="🟢 เปิดบอทตอบอัตโนมัติสำหรับลูกค้ารายนี้เรียบร้อยแล้วค่ะ 🥰")
        )
        return

    # -------------------------------------------------------------
    # 1. ระบบคำสั่งพิเศษ #oder / #order / #ออเดอร์ / #บิล สำหรับแอดมินสรุปบิลและคำนวณกำไร 4 กองทุนอัตโนมัติ
    # -------------------------------------------------------------
    if re.match(r'^#\s*(?:order|oder|ออเดอร์|บิล)\b', user_text.strip(), flags=re.IGNORECASE):
        try:
            parsed_order = parse_and_build_order_summary(user_text.strip(), user_id=user_id)
            if parsed_order:
                customer_bill, admin_summary = parsed_order
                line_bot_api.reply_message(
                    reply_token,
                    [
                        TextSendMessage(text=customer_bill),
                        TextSendMessage(text=admin_summary),
                    ]
                )
                logger.info(f"Replied with #order bill and 4-fund summary for {user_id}")
                return
            else:
                line_bot_api.reply_message(
                    reply_token,
                    TextSendMessage(
                        text=(
                            "⚠️ ไม่พบชื่อสินค้าในคำสั่งค่ะ\n"
                            "ลองพิมพ์: #oder ตามด้วยชื่อสินค้าและจำนวนชิ้น เช่น\n"
                            "• #oder การ์ด 4x6 หน้าหลัง 300, น้ำผึ้ง 300 ค่าส่ง 80\n"
                            "• #oder ดินสอไม้ 500 ส่งฟรี"
                        )
                    )
                )
                return
        except Exception as ord_err:
            logger.error(f"Error processing #order command: {ord_err}")
            return

    # -------------------------------------------------------------
    # 2. ตรวจสอบว่าลูกค้ารายนี้ถูกพักบอทไว้ หรือกำลังคุยกับแอดมินคนจริงหรือไม่
    # -------------------------------------------------------------
    if is_user_bot_muted(user_id):
        logger.info(f"Bot is currently MUTED for user {user_id}. Admin is handling this chat.")
        record_recent_line_customer(user_id, user_text)
        save_message(user_id, "user", user_text)
        return

    # ตรวจจับข้อความสำคัญสำหรับแจ้งเตือนแอดมิน (สลิปโอนเงิน / ขอคุยกับคน / ตรวจแบบ)
    admin_alert_info = classify_user_intent_for_admin_alert(user_text)
    if admin_alert_info:
        logger.info(f"Triggered admin alert [{admin_alert_info['type']}] for user {user_id}")
        create_admin_alert(user_id, admin_alert_info["type"], admin_alert_info["title"], user_text)
        record_recent_line_customer(user_id, user_text)
        save_message(user_id, "user", user_text)
        return

    if should_bot_stay_silent_for_human_admin(user_text):
        logger.info(f"Customer {user_id} is talking with human admin. Bot stays silent.")
        record_recent_line_customer(user_id, user_text)
        save_message(user_id, "user", user_text)
        return

    # -------------------------------------------------------------
    # ระบบ Debounce / Message Buffering (รอให้ลูกค้าส่งรัวๆ ให้หมดก่อนค่อยตอบ)
    # -------------------------------------------------------------
    if db:
        now = time.time()
        action_id = f"{now}_{message_id}"
        buffer_ref = db.collection('message_buffers').document(user_id)
        
        try:
            # เก็บข้อความเข้า Array แบบ Atomic เพื่อไม่ให้ข้อความตกหล่น
            buffer_ref.set({
                "latest_action_id": action_id,
                "latest_reply_token": reply_token,
                "messages": firestore.ArrayUnion([{"id": message_id, "text": user_text, "t": now}]),
                "updated_at": now
            }, merge=True)
        except Exception as buffer_err:
            logger.error(f"Error buffering message: {buffer_err}")
            action_id = None
        
        if action_id:
            # รอ 0.8 วินาที เพื่อดูว่าลูกค้ากำลังพิมพ์ข้อความถัดไปเข้ามาติดๆ กันหรือไม่ (ลดเวลาเพื่อป้องกัน LINE reply_token timeout)
            time.sleep(0.8)
            
            try:
                doc = buffer_ref.get()
                if not doc.exists:
                    return
                data = doc.to_dict() or {}
                # หากมีข้อความใหม่กว่าเข้ามา action_id ล่าสุดจะไม่ตรงกับรอบนี้ -> ให้รอบใหม่เป็นคนตอบ
                if data.get("latest_action_id") != action_id:
                    logger.info(f"Skipping reply for older message ({action_id}) as newer messages arrived.")
                    return
                
                # หากรอบนี้คือข้อความสุดท้าย ให้ดึงข้อความทั้งหมดที่สะสมไว้มาตอบพร้อมกัน
                raw_msgs = data.get("messages", [])
                sorted_msgs = sorted(raw_msgs, key=lambda x: x.get("t", 0) if isinstance(x, dict) else 0)
                all_texts = [m.get("text", "") for m in sorted_msgs if isinstance(m, dict) and m.get("text")]
                if not all_texts:
                    all_texts = [user_text]
                
                # รวบเป็นข้อความเดียวคั่นด้วยขึ้นบรรทัดใหม่
                user_text = "\n".join(all_texts)
                reply_token = data.get("latest_reply_token", reply_token)
                
                # ลบ buffer ออกเพื่อพร้อมรับบทสนทนาใหม่ในครั้งต่อไป
                try:
                    buffer_ref.delete()
                except Exception:
                    pass
                    
                logger.info(f"Aggregated {len(all_texts)} messages for user {user_id}: {user_text}")
            except Exception as e:
                logger.error(f"Error checking message buffer: {e}")
    
    try:
        # ส่งข้อความไปประมวลผลผ่านระบบ Multi-model + Rule-based Fallback
        bot_reply, history_list, model_used = generate_bot_reply_with_fallback(user_id, user_text=user_text)
        logger.info(f"Generated reply for {user_id} via {model_used}")
        
        # ตรวจสอบระบบ [SILENCE] ว่าบอทเลือกที่จะเงียบหรือไม่ (ไม่บันทึก [SILENCE] ลง DB เพื่อไม่ให้ล็อกแชทลูกค้าถาวร)
        if not bot_reply or "[SILENCE]" in bot_reply:
            logger.info(f"Bot chose to stay silent for user {user_id}")
            record_recent_line_customer(user_id, user_text)
            return # หยุดการทำงานทันที ไม่ส่งอะไรกลับไป

        # บันทึกสิ่งที่ user พิมพ์ และสิ่งที่บอทตอบ ลง DB เฉพาะเมื่อเป็นคำตอบจริงเท่านั้น
        save_message(user_id, "user", user_text)
        time.sleep(0.01)
        save_message(user_id, "model", bot_reply)
        
        # ค้นหาแท็ก [IMAGE: url] ด้วย Regex
        image_urls = re.findall(r'\[IMAGE:\s*(https?://[^\s\]]+)\]', bot_reply)
        
        # ลบแท็กออกจากข้อความที่จะส่งให้ผู้ใช้
        clean_text = re.sub(r'\[IMAGE:\s*https?://[^\s\]]+\]', '', bot_reply)
        # ลบ whitespace บนบรรทัดว่างๆ และยุบรวม \n ที่ติดกันเกิน 2 ตัวให้เหลือแค่ 2 ตัว (เว้นพารากราฟเดียว)
        clean_text = re.sub(r'^[ \t]+$', '', clean_text, flags=re.MULTILINE)
        clean_text = re.sub(r'\n{3,}', '\n\n', clean_text).strip()

        # ตรวจสอบประวัติการคุยก่อนหน้ารอบนี้ เพื่อป้องกัน:
        # 1. บอทเผลอทักทาย "สวัสดี" ซ้ำในรอบที่ไม่ใช่รอบแรกสุด
        # 2. ป้องกันการส่งรูปสินค้าเดิมซ้ำ หากเคยส่งไปแล้วในแชทนี้ (ส่งเฉพาะที่ยังไม่เคยส่ง)
        is_subsequent_turn = len(history_list) > 0
        past_image_urls = set()
        try:
            for h in history_list:
                role = h.get("role", "") if isinstance(h, dict) else getattr(h, "role", "")
                if role == "model":
                    parts = h.get("parts", []) if isinstance(h, dict) else getattr(h, "parts", [])
                    for part in parts:
                        part_text = part if isinstance(part, str) else getattr(part, "text", str(part))
                        past_image_urls.update(re.findall(r'\[IMAGE:\s*(https?://[^\s\]]+)\]', part_text))
        except Exception as hist_err:
            logger.warning(f"Error inspecting chat history: {hist_err}")

        # 1. ตัดคำทักทาย "สวัสดี" ออก หากไม่ใช่รอบแรกสุดของแชท
        if is_subsequent_turn and clean_text:
            clean_text = re.sub(
                r'^(สวัสดีค่ะ|สวัสดีครับ|สวัสดีค่า|สวัสดี|ยินดีต้อนรับค่ะ|ยินดีต้อนรับครับ|ยินดีต้อนรับ)[^\n]*\n*',
                '',
                clean_text,
                flags=re.IGNORECASE
            ).strip()

        # 2. กรองรูปภาพ ให้ส่งเฉพาะสินค้าที่ยังไม่เคยส่ง
        if past_image_urls and image_urls:
            unique_new_image_urls = [u for u in image_urls if u not in past_image_urls]
            if len(unique_new_image_urls) < len(image_urls):
                logger.info(f"Filtered out {len(image_urls) - len(unique_new_image_urls)} duplicate images already sent in history.")
                image_urls = unique_new_image_urls
                if not image_urls and not clean_text:
                    clean_text = "ทางร้านได้ส่งแบบและราคาของชำร่วยให้ชมในแชทด้านบนครบถ้วนแล้วนะคะ คุณลูกค้าสนใจแบบไหนเป็นพิเศษ แจ้งแอดมินได้เลยนะคะ 🥰"

        # 3. หากมีการส่งรูปภาพหลายรายการ (กรณีลูกค้าขอดูของชำร่วยทั้งหมด) ให้ตัดข้อความราคาของสินค้าที่มีรูปออก เพื่อไม่ให้ส่งข้อความยาวซ้ำซ้อน
        # แต่ถ้าลูกค้าถามสินค้าแค่ 1 อย่าง (len(image_urls) <= 1) จะไม่ตัด เพื่อให้ส่งทั้งรูปภาพพร้อมข้อความราคาตามที่ต้องการ
        if len(image_urls) > 1 and clean_text:
            # ตัดบล็อกข้อความรายการสินค้าที่มีรูปภาพออก เฉพาะในกรณีขอดูของชำร่วยทั้งหมด
            pattern = r'(?:\d+[\.\)]\s*)?(?:พวงกุญแจ\s*PU|พวงกุญแจหนัง|น้ำหอมขวดจิ๋ว|กระจกวิเศษ|ดินสอไม้|ผ้าขนหนู|หนังสือยาซีน|เข็มกลัด[^\n]*|ช้อนเงิน[^\n]*|ปากกาลูกลื่น|ยางมัดผม|น้ำผึ้ง[^\n]*|พิมเสน|กระเป๋าผ้า[^\n]*)[^\n]*(?:\n\s*[-–•]\s*[^\n]+)+'
            clean_text = re.sub(pattern, '', clean_text, flags=re.IGNORECASE)
            clean_text = re.sub(r'\n{3,}', '\n\n', clean_text).strip()
            
            # หากข้อความว่างเปล่าหลังจากตัดรายการออก ให้ใช้ข้อความแนะนำสั้นๆ
            if not clean_text:
                clean_text = "ทางร้านได้ส่งรูปและราคาของชำร่วยให้ชมตามในรูปภาพเลยนะคะ สนใจแบบไหนแจ้งแอดมินได้เลยค่า 🥰"
            
        if not clean_text and not image_urls:
            logger.info(f"No content to send for user {user_id}")
            record_recent_line_customer(user_id, user_text)
            return
            
        messages = []
        if clean_text:
            messages.append(TextSendMessage(text=clean_text))

        # คำนวณจำนวนรูปที่ reply_message รับได้ (LINE จำกัด 5 messages รวม text)
        first_batch_limit = 4 if clean_text else 5
        first_batch_urls = image_urls[:first_batch_limit]
        remaining_urls = image_urls[first_batch_limit:]

        for url in first_batch_urls:
            messages.append(ImageSendMessage(
                original_content_url=url,
                preview_image_url=url
            ))

        # ส่ง batch แรกด้วย reply_message (ต้องใช้ reply_token) - ส่งฟรี ไม่กินโควตา
        if messages:
            line_bot_api.reply_message(
                reply_token,
                messages
            )
            logger.info(f"Replied to user {user_id} with {len(messages)} messages.")

        # ส่งรูปที่เหลือด้วย push_message แบ่ง batch ละ 5 รูป (ดัก try-except เพื่อไม่ให้ crash ถ้าโควตารายเดือนเต็ม)
        if remaining_urls:
            try:
                for i in range(0, len(remaining_urls), 5):
                    batch = remaining_urls[i:i+5]
                    push_msgs = [
                        ImageSendMessage(original_content_url=url, preview_image_url=url)
                        for url in batch
                    ]
                    line_bot_api.push_message(user_id, push_msgs)
                    logger.info(f"Pushed batch {i//5 + 1}: {len(push_msgs)} images to user {user_id}.")
            except Exception as push_err:
                logger.warning(f"LINE push_message limit reached or failed: {push_err}")

        # บันทึกชื่อจริงจาก LINE Profile ของลูกค้าหลังจากส่งข้อความตอบกลับแล้ว เพื่อไม่ให้หน่วงเวลา reply_token
        record_recent_line_customer(user_id, user_text)
        
    except Exception as e:
        logger.error(f"Error generating content or replying: {e}")
        record_recent_line_customer(user_id, user_text)
        return

@handler.add(MessageEvent, message=ImageMessage)
def handle_image_message(event):
    """
    ฟังก์ชันสำหรับจัดการ Event ประเภทรูปภาพ ที่ส่งมาจากผู้ใช้
    """
    reply_token = event.reply_token
    user_id = event.source.user_id
    message_id = event.message.id
    
    logger.info(f"Received image message from user {user_id}")

    if is_user_bot_muted(user_id):
        logger.info(f"Bot is MUTED for {user_id}. Image received, alerting admin.")
        create_admin_alert(user_id, "image_sent", "🖼️ ลูกค้าส่งรูปภาพ (สลิป/ตัวอย่างแบบ)", "ลูกค้าส่งรูปภาพเข้ามาในแชท")
        return

    try:
        # ดึงข้อมูลรูปภาพจาก Line Server
        message_content = line_bot_api.get_message_content(message_id)
        image_bytes = b""
        for chunk in message_content.iter_content():
            image_bytes += chunk
            
        # เปิดรูปภาพด้วย Pillow เพื่อส่งให้ Gemini
        image = PIL.Image.open(io.BytesIO(image_bytes))
        
        bot_reply, _, model_used = generate_bot_reply_with_fallback(user_id, image_obj=image)
        logger.info(f"Generated image reply for {user_id} via {model_used}")
        
        # ตรวจสอบระบบ [SILENCE] ว่าบอทเลือกที่จะเงียบหรือไม่ (ไม่บันทึก [User sent an image] หรือ [SILENCE] ลง DB เพื่อไม่ให้ประวัติแชทรวน)
        if not bot_reply or "[SILENCE]" in bot_reply:
            logger.info(f"Bot chose to stay silent for image from user {user_id}")
            create_admin_alert(user_id, "image_sent", "🖼️ ลูกค้าส่งรูปภาพ (สลิป/ตัวอย่างแบบ)", "ลูกค้าส่งรูปภาพเข้ามาในแชท")
            return
            
        clean_text = re.sub(r'\[IMAGE:\s*https?://[^\s\]]+\]', '', bot_reply).strip()
        if not clean_text:
            return

        # บันทึกการตอบกลับลง DB เฉพาะกรณีที่บอทตอบจริง
        save_message(user_id, "user", "ลูกค้าส่งรูปภาพสอบถามสินค้า")
        time.sleep(0.01)
        save_message(user_id, "model", clean_text)
            
        # ส่งข้อความกลับไปยัง Line
        line_bot_api.reply_message(
            reply_token,
            TextSendMessage(text=clean_text)
        )
        logger.info(f"Replied to user {user_id} regarding their image.")
        
    except Exception as e:
        logger.error(f"Error processing image or replying: {e}")
        return


@app.get("/api/bot-health")
async def get_bot_health(probe: bool = False):
    """ตรวจสอบสถานะการทำงานของ LINE Bot, Gemini API และระบบ Fallback"""
    probe_result = {}
    if probe:
        for m_name in GEMINI_MODEL_CANDIDATES:
            try:
                m = genai.GenerativeModel(m_name)
                r = m.generate_content("ตอบสั้นๆ ว่า OK")
                probe_result[m_name] = {"ok": True, "reply": (r.text or "").strip()[:40]}
                break
            except Exception as err:
                probe_result[m_name] = {"ok": False, "error": str(err)[:200]}
    return {
        "ok": True,
        "candidates": GEMINI_MODEL_CANDIDATES,
        "ruleBasedFallbackEnabled": True,
        "lastBotInfo": last_bot_error_info,
        "probe": probe_result if probe else None,
    }

