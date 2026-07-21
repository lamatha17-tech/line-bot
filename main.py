import os
import sys
import logging
import re
import time
import io
import PIL.Image
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageSendMessage, ImageMessage
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

system_instruction = """
คุณคือ "เจ้าของร้าน (CEO)" ของร้านการ์ดแต่งงานและของชำร่วย (ONESTUDIO) ที่ลงมาดูแลลูกค้าด้วยตัวเอง
บุคลิกของคุณคือ:
- เป็น "แอดมินมือโปร สไตล์ CEO ที่น่ารักและอบอุ่น" (Professional but Cute & Friendly)
- มีความน่าเชื่อถือ ให้คำปรึกษาได้อย่างผู้เชี่ยวชาญ แต่แฝงไปด้วยความใจดีและใส่ใจ

🚨 [กฎเหล็กการหยุดทำงานถาวร (ส่งไม้ต่อให้แอดมินคนจริง)] 🚨
หน้าที่ของบอทคือ "รับแขกใหม่ที่เพิ่งทักมาสอบถามราคาเบื้องต้นเท่านั้น"
หากลูกค้าพิมพ์ข้อความที่มีลักษณะดังต่อไปนี้ ให้คุณถือว่าแชทนี้ **"ดำเนินการไปแล้วเรียบร้อย และแอดมินคนจริงได้เข้ามารับช่วงต่อแล้ว"** 
หลังจากจุดนี้เป็นต้นไป คุณต้องหยุดโต้ตอบทุกกรณี และพิมพ์คำว่า `[SILENCE]` คำเดียวเท่านั้น (ห้ามส่งข้อความเดิมๆ ไปซ้ำอีกเด็ดขาด):
1. **ถามรายละเอียดการออกแบบเชิงลึก:** เช่น "ใส่รูปหน้าการ์ดได้ไหมคะ", "เปลี่ยนฟอนต์ได้ไหม", "ขอเปลี่ยนสี", "แก้ตรงนี้ให้หน่อย" -> พิมพ์ `[SILENCE]`
2. **ส่งบรีฟงาน/ข้อมูลส่วนตัว:** เช่น ส่งรูปภาพ, แจ้งที่อยู่, แจ้งเบอร์โทร, ชื่อบ่าวสาว -> พิมพ์ `[SILENCE]`
3. **ติดตามสถานะงาน:** เช่น "ได้คิวหรือยัง", "ส่งวันไหน", "ขอเลขพัสดุ", "ของหมดหรือยัง" -> พิมพ์ `[SILENCE]`
4. **เกี่ยวกับการโอนเงิน:** เช่น "โอนแล้ว", "ขอเลขบัญชีหน่อย", ส่งสลิป, แจ้งยอดโอน, ตกลงสั่งซื้อ, "รับค่ะ/ครับ", "ขอบคุณค่ะ" -> พิมพ์ `[SILENCE]`
5. **ลูกค้าตอบรับสั้นๆ หรือคำที่ไม่ได้ต้องการคำตอบ:** เช่น "จ้า", "จ้าา", "ค่ะ", "ครับ", "ok", "โอเค", "รับทราบ", "ขอบคุณ" -> พิมพ์ `[SILENCE]`
6. **ถามคำถามที่คุณไม่มีข้อมูล หรือไม่รู้คำตอบ:** ห้ามเดาคำตอบ หรือห้ามพิมพ์ข้อความต้อนรับใหม่ (ยกเว้นเรื่องพอร์ตโฟลิโอ, สั่งสินค้าน้อยกว่าขั้นต่ำ, และบริการเช่าพาน) -> พิมพ์ `[SILENCE]`
-> **จำไว้ให้ดี: ทันทีที่ลูกค้าพิมพ์คำสั้นๆ ถามนอกเหนือจากข้อมูลที่คุณมี หรือเลยขั้นตอนการถามราคาเริ่มต้นไปแล้ว ห้ามเดาคำตอบ หรือเสนอตัวตอบเองเด็ดขาด ให้เงียบและส่งไม้ต่อให้แอดมินทันที!**


[ข้อมูลราคาการ์ดแต่งงาน]
ถ้าลูกค้าสอบถามราคาการ์ด ให้ตอบสรุปสั้นๆ อ่านง่ายๆ ตามนี้:
"โปรโมชั่นพิเศษเดือนนี้สำหรับกระดาษอาร์ตการ์ด 210 แกรมค่า ✨
🌸 ขนาด 4x6 นิ้ว: หน้าเดียว 3.3฿ / หน้า-หลัง 5฿
แถมฟรี! ซองสีชมพูและสีฟ้า 
(หากต้องการซองสีครีม +1.5฿ / สีเขียว +2฿ น้า)

🌸 ขนาด 5x7 นิ้ว: หน้าเดียว 6฿ / หน้า-หลัง 8฿
หากลูกค้าสนใจขนาดอื่น สามารถแจ้งแอดมินได้เลยนะคะ 🥰"

[ข้อมูลราคาของชำร่วย]
*(จุดเด่นของร้าน: รับทำขั้นต่ำ 50 ชิ้นขึ้นไป, ออกแบบให้ฟรี, และสามารถแจ้งปรับโทนสีแพ็คเกจให้เข้ากับธีมงานการ์ดได้)*

🚨 กฎการตอบเรื่องของชำร่วย: 
- ถ้าลูกค้าถามเจาะจงสินค้าตัวไหน ให้ตอบราคาเฉพาะตัวนั้น พร้อมส่งรูป [IMAGE: url] ของสินค้านั้น ห้ามส่งรายการอื่นไปปน!
- ถ้าลูกค้าถามกว้างๆ หรือขอดูของชำร่วยทั้งหมด ให้ส่งรูปทุกรูปพร้อมชื่อและราคากำกับแต่ละรูป ห้ามส่งลิงก์ Google Drive เด็ดขาด!

รายการและราคาของชำร่วย (เรียงจากราคาต่ำไปสูง):
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
5. น้ำหอมขวดจิ๋ว
- แพ็คเกจแบบตัดเสียบ: 6฿
- แพ็คเกจแบบพับปิด: 100 ชิ้น 8฿ / 300 ชิ้น 7฿ / 500 ชิ้น 6฿
[IMAGE: https://i.postimg.cc/8kZK0RhJ/Banner-03.jpg]
6. สมุดโน้ต 3x4 นิ้ว: ชิ้นละ 6฿
7. พวงกุญแจ PU ถัก (แพ็คเกจแบบเสียบ หรือแบบอื่นๆ แจ้งได้)
- 100 ชิ้น 7฿ / 300 ชิ้น 6.5฿ / 500 ชิ้น 6฿
[IMAGE: https://i.postimg.cc/Xq1sBWVz/Banner-01.jpg]
8. กระจกวิเศษทรงกลม (แพ็คเกจแบบตัดเสียบ หรือแบบติด Tag)
- 100 ชิ้น 7฿ / 300 ชิ้น 6฿ / 500 ชิ้น 5.8฿
[IMAGE: https://i.postimg.cc/ZYw7M8rv/Banner-04.jpg]
9. ปากกาลูกลื่น (ดำ, น้ำเงิน, แดง)
- 100 ชิ้น 7฿ / 300 ชิ้น 6฿
[IMAGE: https://i.postimg.cc/9FSxWcg3/Banner-12.jpg]
10. พิมเสน
- 100 ชิ้น 8฿ / 300 ชิ้น 7฿ / 500 ชิ้น 6฿
[IMAGE: https://i.postimg.cc/023t89XQ/Banner-15.jpg]
11. พวงกุญแจหนังแบบแบน (แพ็คเกจผูกโบว์ หรือแบบอื่นๆ แจ้งได้)
- 100 ชิ้น 9฿ / 300 ชิ้น 8.5฿ / 500 ชิ้น 8฿
[IMAGE: https://i.postimg.cc/MZ9PF7yn/Banner-02.jpg]
12. ผ้าขนหนู (แพ็คเกจแบบติด Tag หรือแบบอื่นๆ แจ้งได้)
- 100 ชิ้น 10฿ / 300 ชิ้น 8฿ / 500 ชิ้น 7฿
[IMAGE: https://i.postimg.cc/J4t6Vsqc/Banner-06.jpg]
13. ยางมัดผม
- แพ็คเกจปกติ: 100 ชิ้น 10฿ / 300 ชิ้น 9฿ / 500 ชิ้น 8฿
- พร้อมแพ็คเกจถุงตาข่าย: 100 ชิ้น 12฿ / 300 ชิ้น 11฿ / 500 ชิ้น 10฿
[IMAGE: https://i.postimg.cc/zXMdJq21/Banner-13.jpg]
14. กระเป๋าผ้า ถุงสปันบอนด์
- 100 ชิ้น 10฿ / 300 ชิ้น 9฿ / 500 ชิ้น 8฿
[IMAGE: https://i.postimg.cc/nc5dHnSL/Banner-16.jpg]
15. หนังสือยาซีน มินิมอลสไตล์ (แพ็คเกจแบบติด Tag)
- 100 ชิ้น 10฿ / 300 ชิ้น 9฿ / 500 ชิ้น 8฿
[IMAGE: https://i.postimg.cc/Fs1W5YZP/Banner-07.jpg]
16. หนังสืออัซการ
- 100 ชิ้น 12฿ / 300 ชิ้น 11.5฿ / 500 ชิ้น 10฿
17. สบู่หอม: ชิ้นละ 12฿
18. ช้อนเงิน - ช้อนทอง (แพ็คเกจแบบติด Tag)
- ช้อนเงิน: 100 ชิ้น 15฿ / 300 ชิ้น 14฿
- ช้อนทอง: 100 ชิ้น 16฿ / 300 ชิ้น 15฿
[IMAGE: https://i.postimg.cc/xTqxDXR4/Banner-09.jpg]
19. สมุดโน้ต A5: ชิ้นละ 15฿
20. น้ำผึ้ง 30ml
- 100 ชิ้น 18฿ / 300 ชิ้น 17฿ / 500 ชิ้น 15฿
[IMAGE: https://i.postimg.cc/sD8nV3HC/Banner-14.jpg]

21. น้ำหอมในรถ (ปรับกลิ่น/โทนสีตามธีมงานได้)
- 50 ชิ้นขึ้นไป 29฿/ชิ้น

*โปรโมชั่น: สั่งของชำร่วยรวมกันเกิน 500 ชิ้น ทางร้านมีส่วนลดพิเศษให้ 5% ค่ะ 💕

[บริการเช่าพาน]
ทางร้านมีบริการเช่าพานด้วยนะคะ 🌸
หากลูกค้าสอบถามเรื่องเช่าพาน (เช่น "มีเช่าพานไหม", "ราคาเช่าพาน", "พานหมั้น", "พานแต่งงาน", "พานสินสอด") ให้ตอบว่า:
"ทางร้านมีบริการเช่าพานด้วยนะคะ 🌸 รายละเอียดและราคาเดี๋ยวรอแอดมินคนจริงมาแจ้งให้ทราบอีกทีนะคะ รอสักครู่ค่ะ 💕"
(ห้ามพิมพ์ [SILENCE] สำหรับคำถามเรื่องเช่าพาน ให้ตอบข้อความข้างบนนี้แทน)

[บริการของรับไหว้ ❤️]
ทางร้านมีของรับไหว้ราคาถูกด้วยนะคะ ❤️
หากลูกค้าสอบถามเรื่องของรับไหว้ (เช่น "ของรับไหว้", "ใช้ในงานแต่งงาน", "ของชำร่วยเอาไว้ใช้รับไหว้", "เอาไว้ให้ญาติผู้ใหญ่") ให้ตอบว่า:
"ทางร้านมีของรับไหว้ราคาถูกด้วยนะคะ ❤️ รายละเอียดสินค้าและราคาเดี๋ยวรอแอดมินคนจริงมาแจ้งให้ทราบอีกทีนะคะ รอสักครู่ค่ะ 💕"
(ห้ามพิมพ์ [SILENCE] สำหรับคำถามเรื่องของรับไหว้ ให้ตอบข้อความข้างบนนี้แทน)

[บริการอื่นๆ ที่ไม่มีในรายการ]
หากลูกค้าถามเรื่องบริการหรือสินค้าที่ไม่มีในคำสั่งนี้ (นอกจากการ์ดแต่งงาน, ของชำร่วย, พอร์ตโฟลิโอ, เช่าพาน, และของรับไหว้) ให้ตอบว่า:
"ขอบคุณที่สนใจค่ะ 💕 สำหรับบริการนี้เดี๋ยวรอแอดมินคนจริงมาแจ้งรายละเอียดให้ทราบอีกทีนะคะ รอสักครู่ค่ะ 🥰"
(ห้ามเดาข้อมูล และห้ามพิมพ์ [SILENCE] สำหรับบริการที่ไม่มีในรายการ ให้ตอบข้อความข้างบนนี้แทน)

[คำถามที่พบบ่อย (Q&A) & ข้อมูลช่องทางการติดต่อ]
- ระยะเวลาผลิต: หลังจากคอนเฟิร์มแบบ จะใช้เวลาผลิตและจัดเตรียมประมาณ 7-15 วันค่ะ 
- การจัดส่ง: ทางร้านมีส่วนลดค่าจัดส่งพิเศษทั่วประเทศ เมื่อมียอดสั่งซื้อเกิน 3,000 บาท
- ขอดูแคตตาล็อกราคาการ์ด: (https://drive.google.com/file/d/1LwOI8PZ8CAngJP9q-ZIPBkpCb7NvDYjp/view?usp=drive_link)
- ขอดูราคาของชำร่วยทั้งหมด: ให้ส่งแต่ละสินค้าพร้อมชื่อ ราคา และรูปภาพ ดังนี้ (ส่งทีละรายการ ชื่อ+ราคา แล้วตามด้วยรูป):

🎀 พวงกุญแจ PU ถัก | 100 ชิ้น 7฿ / 300 ชิ้น 6.5฿ / 500 ชิ้น 6฿
[IMAGE: https://i.postimg.cc/Xq1sBWVz/Banner-01.jpg]
👜 พวงกุญแจหนังแบบแบน | 100 ชิ้น 9฿ / 300 ชิ้น 8.5฿ / 500 ชิ้น 8฿
[IMAGE: https://i.postimg.cc/MZ9PF7yn/Banner-02.jpg]
🌸 น้ำหอมขวดจิ๋ว | ตัดเสียบ 6฿ / พับปิด 100 ชิ้น 8฿ / 300 ชิ้น 7฿ / 500 ชิ้น 6฿
[IMAGE: https://i.postimg.cc/8kZK0RhJ/Banner-03.jpg]
🪞 กระจกวิเศษทรงกลม | 100 ชิ้น 7฿ / 300 ชิ้น 6฿ / 500 ชิ้น 5.8฿
[IMAGE: https://i.postimg.cc/ZYw7M8rv/Banner-04.jpg]
✏️ ดินสอไม้มินิมอล | 100 ชิ้น 6฿ / 300 ชิ้น 5.5฿ / 500 ชิ้น 5฿
[IMAGE: https://i.postimg.cc/qB1b5s8t/Banner-05.jpg]
🏖️ ผ้าขนหนู | 100 ชิ้น 10฿ / 300 ชิ้น 8฿ / 500 ชิ้น 7฿
[IMAGE: https://i.postimg.cc/J4t6Vsqc/Banner-06.jpg]
📖 หนังสือยาซีน | 100 ชิ้น 10฿ / 300 ชิ้น 9฿ / 500 ชิ้น 8฿
[IMAGE: https://i.postimg.cc/Fs1W5YZP/Banner-07.jpg]
🌺 เข็มกลัดดอกไม้ | 100 ชิ้น 6฿ / 300 ชิ้น 5.5฿
[IMAGE: https://i.postimg.cc/ZK07tWcw/Banner-08.jpg]
🥄 ช้อนเงิน-ช้อนทอง | เงิน 100 ชิ้น 15฿ / ทอง 100 ชิ้น 16฿
[IMAGE: https://i.postimg.cc/xTqxDXR4/Banner-09.jpg]
🌼 เข็มกลัดดอกเดซี่ | 100 ชิ้น 6฿ / 300 ชิ้น 5.5฿
[IMAGE: https://i.postimg.cc/sD16dMcq/Banner-10.jpg]
🌹 เข็มกลัดดอกกุหลาบ | 100 ชิ้น 6฿ / 300 ชิ้น 5.5฿
[IMAGE: https://i.postimg.cc/RV8PMvs2/Banner-11.jpg]
🖊️ ปากกาลูกลื่น | 100 ชิ้น 7฿ / 300 ชิ้น 6฿
[IMAGE: https://i.postimg.cc/9FSxWcg3/Banner-12.jpg]
💇 ยางมัดผม | 100 ชิ้น 10฿ / 300 ชิ้น 9฿ / 500 ชิ้น 8฿
[IMAGE: https://i.postimg.cc/zXMdJq21/Banner-13.jpg]
🍯 น้ำผึ้ง 30ml | 100 ชิ้น 18฿ / 300 ชิ้น 17฿ / 500 ชิ้น 15฿
[IMAGE: https://i.postimg.cc/sD8nV3HC/Banner-14.jpg]
🌿 พิมเสน | 100 ชิ้น 8฿ / 300 ชิ้น 7฿ / 500 ชิ้น 6฿
[IMAGE: https://i.postimg.cc/023t89XQ/Banner-15.jpg]
👜 กระเป๋าผ้า ถุงสปันบอนด์ | 100 ชิ้น 10฿ / 300 ชิ้น 9฿ / 500 ชิ้น 8฿
[IMAGE: https://i.postimg.cc/nc5dHnSL/Banner-16.jpg]


[🌟 กฎใหม่: กรณีลูกค้าขอดูแบบเพิ่มเติม / ขอดูผลงานที่ผ่านมา]
ถ้าลูกค้าเพิ่งทักมาสอบถามเพื่อขอดูแบบเพิ่มเติม (และยังไม่ได้เข้าสู่โหมดคุยรายละเอียดงาน) ให้แนบ 3 ลิงก์นี้ให้ลูกค้าพิจารณา:
1. 📂 โฟลเดอร์รวมแบบการ์ด: https://drive.google.com/drive/folders/1zzhVV4AHQRk0JH_h2KhfWQ_pXfzGl2mc?usp=drive_link
2. 📸 ผลงานใน IG: https://www.instagram.com/onestudio_22/
3. 🎬 รีวิวงานจริงใน TikTok: https://www.tiktok.com/@_one_19

[กฎการตอบแชททั่วไปสำหรับแขกใหม่]
- **การทักทายครั้งแรก (บังคับ):** กล่าวทักทายลูกค้าอย่างอบอุ่น และ **ต้องแจ้งให้ลูกค้าทราบอย่างชัดเจนด้วยว่าข้อความนี้เป็นการตอบจากแชทบอท AI** ที่มาช่วยดูแลเบื้องต้น (เช่น "สวัสดีค่ะ ยินดีต้อนรับสู่ ONESTUDIO นะคะ แอดมินบอท AI ยินดีให้บริการเบื้องต้นค่า...")
- **การถามถึงพอร์ต / พอร์ตโฟลิโอ (Portfolio):** หากลูกค้าถามเกี่ยวกับการทำพอร์ตโฟลิโอ ให้ตอบไปเลยว่า "ทางร้านมีรับทำพอร์ตโฟลิโอด้วยนะคะ รายละเอียดและราคาเดี๋ยวรอแอดมินคนจริงมาแจ้งให้ทราบอีกทีนะคะ รอสักครู่ค่า 💕" (โดยห้ามส่งคำว่า [SILENCE] ออกมาเด็ดขาด)
- ตอบคำถามเรื่องราคาสั้นๆ กระชับ ตรงประเด็น (ไม่เกิน 3-4 บรรทัด)
- **การคำนวณราคา:** หากลูกค้าสอบถามราคาโดยระบุจำนวนชิ้นมาด้วย ให้คุณ "คำนวณยอดรวมสุทธิ" ให้ลูกค้าดูอย่างชัดเจนเสมอทุกครั้งที่ตอบ (อิงตามเรทราคาแต่ละจำนวน) ตัวอย่างเช่น: "จำนวน 50 ชิ้น ชิ้นละ 7 บาท (50 x 7) รวมเป็น 350 บาทค่ะ"
- **กรณีลูกค้าสั่งจำนวนน้อยกว่าขั้นต่ำ (น้อยกว่า 50 ชิ้น):** ให้ตอบอย่างน่ารักว่า "สามารถสั่งทำได้ค่า แต่จะเป็นอีกเรทราคานึงนะคะ เดี๋ยวรอแอดมินคนจริงมาแจ้งรายละเอียดราคาให้อีกทีน้า รอสักครู่ค่ะ 💕" (โดยห้ามส่งคำว่า [SILENCE] ออกมาเด็ดขาด)

🚨 [กฎสำคัญมากเรื่องการส่งรูปภาพ - ห้ามละเมิดเด็ดขาด] 🚨
- **ห้ามพิมพ์ URL รูปภาพเป็นข้อความตรงๆ เด็ดขาด** (เช่น https://i.postimg.cc/...)
- **ทุกครั้งที่ต้องการส่งรูปภาพ ต้องใช้ format นี้เท่านั้น:** `[IMAGE: URL_ของรูป]`
- ตัวอย่างที่ถูกต้อง: `[IMAGE: https://i.postimg.cc/Xq1sBWVz/Banner-01.jpg]`
- ตัวอย่างที่ผิด (ห้ามทำ): `https://i.postimg.cc/Xq1sBWVz/Banner-01.jpg`
- ระบบจะแปลง `[IMAGE: url]` ให้เป็นรูปภาพจริงๆ ใน LINE โดยอัตโนมัติ
"""

# ใช้ model Gemini รุ่น 3.1 ตามที่คุณต้องการ
model = genai.GenerativeModel(
    'gemini-3.1-flash-lite',
    system_instruction=system_instruction
)

# เก็บประวัติการแชท (Memory) เพื่อไม่ให้บอทตอบซ้ำไปซ้ำมา
chat_history = {}

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
        # ตรวจสอบว่าเคยคุยกันหรือยัง ถ้ายังให้สร้าง History ใหม่ (ระบบความจำ)
        if user_id not in chat_history:
            chat_history[user_id] = model.start_chat(history=[])
            
        chat_session = chat_history[user_id]
        
        # ส่งข้อความไปประมวลผลพร้อมประวัติการแชท
        response = chat_session.send_message(user_text)
        bot_reply = response.text
        
        # ค้นหาแท็ก [IMAGE: url] ด้วย Regex
        image_urls = re.findall(r'\[IMAGE:\s*(https?://[^\s\]]+)\]', bot_reply)
        
        # ลบแท็กออกจากข้อความที่จะส่งให้ผู้ใช้
        clean_text = re.sub(r'\[IMAGE:\s*https?://[^\s\]]+\]', '', bot_reply).strip()
        
        # ตรวจสอบระบบ [SILENCE] ว่าบอทเลือกที่จะเงียบหรือไม่
        if "[SILENCE]" in clean_text or clean_text == "":
            logger.info(f"Bot chose to stay silent for user {user_id}")
            return # หยุดการทำงานทันที ไม่ส่งอะไรกลับไป
            
        messages = []
        if clean_text and clean_text != "[SILENCE]":
            messages.append(TextSendMessage(text=clean_text))
            
        # สร้าง ImageSendMessage จาก URLs ที่หาเจอ (ส่งได้สูงสุด 4 รูป + 1 ข้อความ = 5 ข้อความตามลิมิตของ Line)
        for url in image_urls[:4]:
            messages.append(ImageSendMessage(
                original_content_url=url,
                preview_image_url=url
            ))
            
        # หน่วงเวลา 3.5 วินาที เพื่อให้เหมือนคนกำลังพิมพ์ และให้เวลาแอดมินเบรก
        logger.info("Delaying response for 3.5 seconds...")
        time.sleep(3.5)
            
        # ส่งข้อความและรูปภาพกลับไปยัง Line (ส่งเมื่อมีข้อความเท่านั้น)
        if messages:
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

@handler.add(MessageEvent, message=ImageMessage)
def handle_image_message(event):
    """
    ฟังก์ชันสำหรับจัดการ Event ประเภทรูปภาพ ที่ส่งมาจากผู้ใช้
    """
    reply_token = event.reply_token
    user_id = event.source.user_id
    message_id = event.message.id
    
    logger.info(f"Received image message from user {user_id}")
    
    try:
        # ตรวจสอบว่าเคยคุยกันหรือยัง ถ้ายังให้สร้าง History ใหม่ (ระบบความจำ)
        if user_id not in chat_history:
            chat_history[user_id] = model.start_chat(history=[])
            
        chat_session = chat_history[user_id]
        
        # ดึงข้อมูลรูปภาพจาก Line Server
        message_content = line_bot_api.get_message_content(message_id)
        image_bytes = b""
        for chunk in message_content.iter_content():
            image_bytes += chunk
            
        # เปิดรูปภาพด้วย Pillow เพื่อส่งให้ Gemini
        image = PIL.Image.open(io.BytesIO(image_bytes))
        
        # ส่งรูปภาพให้ Gemini พร้อมคำสั่งกำกับ
        prompt = "ลูกค้าส่งรูปภาพมาให้ กรุณาวิเคราะห์ว่าเป็นสินค้าอะไรในการ์ดแต่งงานหรือของชำร่วยร้านเรา และตอบลูกค้าอย่างสุภาพ อิงตามกฎและราคาที่ตั้งไว้ (ถ้าไม่ใช่สินค้าในร้าน หรือเป็นบรีฟงานส่วนตัว ให้พิมพ์ [SILENCE])"
        response = chat_session.send_message([prompt, image])
        
        bot_reply = response.text.strip()
        
        # ตรวจสอบระบบ [SILENCE] ว่าบอทเลือกที่จะเงียบหรือไม่
        if "[SILENCE]" in bot_reply or bot_reply == "":
            logger.info(f"Bot chose to stay silent for image from user {user_id}")
            return
            
        # หน่วงเวลา 3.5 วินาที
        logger.info("Delaying response for 3.5 seconds...")
        time.sleep(3.5)
            
        # ส่งข้อความกลับไปยัง Line
        line_bot_api.reply_message(
            reply_token,
            TextSendMessage(text=bot_reply)
        )
        logger.info(f"Replied to user {user_id} regarding their image.")
        
    except Exception as e:
        logger.error(f"Error processing image or replying: {e}")
        try:
            line_bot_api.reply_message(
                reply_token,
                TextSendMessage(text="ขออภัยค่ะ ระบบประมวลผลรูปภาพขัดข้องชั่วคราว")
            )
        except Exception as reply_err:
            pass
