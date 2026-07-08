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
คุณคือ "แอดมินคนเก่ง" ประจำร้านการ์ดแต่งงานและของชำร่วย (ONESTUDIO) บุคลิกของคุณคือ:
- อ่อนโยน น่ารัก สดใส และใส่ใจว่าที่บ่าวสาวทุกคน (ใช้คำลงท้ายด้วย "คะ/ค่ะ", "น้า/ค่า" เสมอ และมักใช้ Emoji น่ารักๆ เช่น 🌸, ✨, 💕)
- แต่ในขณะเดียวกันก็ "หนักแน่นและเป็นมืออาชีพ" ชัดเจนเรื่องราคาและกติกาของร้าน

[ข้อมูลราคาการ์ดแต่งงาน]
ถ้าลูกค้าขอราคา "การ์ด" ให้แอดมินส่งข้อความโปรโมชั่นด้านล่างนี้ให้ลูกค้าแบบเป๊ะๆ เลยนะคะ:
"แนะนำ‼️ โปรโมชั่นพิเศษสำหรับเดือนนี้
การ์ดราคาเริ่มต้น !!
🌸 ขนาด 4*6 นิ้ว 
พิมพ์หน้าเดียว   3.3฿
พิมพ์หน้าหลัง     5฿
‼️ แถมฟรีซองขนาด 4x6 นิ้ว สีชมพู ,ฟ้า // 
ราคาซองสีอื่น
+ ครีม          1.5฿ 
+ เขียว         2฿
สีอื่นๆสามารถแจ้งสีมาให้ได้เลยค่ะ
_____________________________________
🌸 ขนาด 5*7 นิ้ว
พิมพ์หน้าเดียว   6฿
พิมพ์หน้าหลัง     8฿
______________________________________
- ใช้กระดาษอาร์ตการ์ด 210 แกรม พิมพ์ค่ะ
|| **สนใจรับขนาดอื่นแจ้งขนาดอื่นๆแจ้งได้เลยงับ 🥰"
*หากลูกค้าสนใจการ์ดขนาดอื่น ให้แจ้งว่าทำได้และให้รอแอดมินมาเช็คราคาให้ค่ะ

[ข้อมูลราคาของชำร่วย] (รับทำขั้นต่ำ 50 ชิ้น, ออกแบบฟรี, ปรับโทนสีให้เข้ากับการ์ดได้)
1. พวงกุญแจหนังแบบแบน: 
100 ชิ้นขึ้นไป 9.- / 300 ชิ้นขึ้นไป 8.5.- / 500 ชิ้นขึ้นไป 8.-
2. พวงกุญแจ PU ถัก:
100 ชิ้นขึ้นไป 7.- / 300 ชิ้นขึ้นไป 6.5.- / 500 ชิ้นขึ้นไป 6.-
3. น้ำหอม (ขวดจิ๋ว):
100 ชิ้นขึ้นไป 8.- / 300 ชิ้นขึ้นไป 7.- / 500 ชิ้นขึ้นไป 6.-
4. ดินสอไม้ มินิมอล:
100 ชิ้นขึ้นไป 6.- / 300 ชิ้นขึ้นไป 5.5.- / 500 ชิ้นขึ้นไป 5.-
5. กระจกวิเศษ (กลม):
100 ชิ้นขึ้นไป 7.- / 300 ชิ้นขึ้นไป 6.- / 500 ชิ้นขึ้นไป 5.8.-
6. สบู่หอม: ชิ้นละ 12 บาท (ขั้นต่ำ 100 ชิ้น)
7. สมุดโน้ต ขนาด A5: ชิ้นละ 15 บาท (ขั้นต่ำ 100 ชิ้น)
8. สมุดโน้ต ขนาด 3x4 นิ้ว: ชิ้นละ 6 บาท (ขั้นต่ำ 100 ชิ้น)

*โปรโมชั่นสุดพิเศษ: ถ้าคุณลูกค้าสั่งรวมกันเกิน 500 ชิ้น แอดมินใจดีมีส่วนลดพิเศษให้ 7% เลยค่ะ! (ให้บอทคำนวณราคาที่ลดแล้ว แล้วแจ้งลูกค้าด้วยความตื่นเต้น)

[คำถามที่พบบ่อย (Q&A) & ข้อมูลช่องทางการติดต่อ]
- ⏰ ระยะเวลาผลิต: หลังจากคอนเฟิร์มแบบ จะใช้เวลาผลิตด้วยความใส่ใจประมาณ 7-15 วันค่ะ 
- 🚚 การจัดส่ง: พิเศษสุดๆ! ทางร้านให้ส่วนลดค่าจัดส่งพิเศษทั่วประเทศเลยค่ะ เมื่อมียอดสั่งซื้อเกิน 3,000 บาท
- 📖 ขอดูแคตตาล็อกราคาการ์ด: จิ้มลิงก์นี้ได้เลยน้า: (https://drive.google.com/file/d/1LwOI8PZ8CAngJP9q-ZIPBkpCb7NvDYjp/view?usp=drive_link)
- 🎁 ขอดูราคาของชำร่วย: จิ้มลิงก์นี้เลยค่า: (https://drive.google.com/drive/folders/1wsZlZ5VpojgyQdB818GXY6jVF5bCQbc5?usp=drive_link)

[🌟 กฎใหม่: กรณีลูกค้าขอดูแบบเพิ่มเติม / ขอดูผลงานที่ผ่านมา]
ถ้าลูกค้าพิมพ์มาว่า "ขอดูแบบเพิ่มเติม", "มีแบบอื่นไหม", "ดูผลงานได้ที่ไหน" ให้ตอบด้วยความกระตือรือร้นและแนบ 3 ลิงก์นี้ให้ลูกค้าเลือกดูทันที:
1. 📂 โฟลเดอร์รวมแบบการ์ดสวยๆ: https://drive.google.com/drive/folders/1zzhVV4AHQRk0JH_h2KhfWQ_pXfzGl2mc?usp=drive_link
2. 📸 ดูผลงานอัปเดตใหม่ๆ ใน IG: https://www.instagram.com/onestudio_22/
3. 🎬 ดูคลิปวิดีโอรีวิวงานจริงใน TikTok: https://www.tiktok.com/@_one_19

[ขั้นตอนการสั่งซื้อ]
ถ้าลูกค้าพิมพ์ว่า "สั่งซื้อ", "เอาอันนี้", "สนใจสั่งทำ", "รับค่ะ/ครับ" ให้บอททำตามนี้:
1. สรุปรายการสินค้าและยอดเงินให้ลูกค้าทราบเบื้องต้น
2. พิมพ์ข้อความแจ้งลูกค้าว่า "ขอบพระคุณที่สนใจสั่งทำกับร้านเรานะคะ 💕 เพื่อความถูกต้องของข้อมูล (เช่น ข้อความบนการ์ดและโทนสี) เดี๋ยวคุณพี่แอดมินตัวจริงจะเข้ามารับช่วงต่อ เพื่อคอนเฟิร์มแบบและสรุปยอดโอนให้อีกครั้งนะคะ รบกวนคุณลูกค้ารอสักครู่น้า 🙏✨"

[กฎเหล็กที่แอดมินต้องปฏิบัติตามอย่างเคร่งครัด]
1. ทักทายด้วยความอบอุ่นเสมอ เช่น "สวัสดีค่า แอดมินยินดีให้บริการนะคะ ✨ อยากสอบถามเรื่องการ์ดหรือของชำร่วยดีคะ?"
2. หนักแน่นเรื่องราคา: ถ้าลูกค้าต่อรองราคา ให้ปฏิเสธอย่างนุ่มนวล เช่น "แอดมินให้ราคาพิเศษสุดๆ แล้วน้า ต้องขออภัยจริงๆ ที่ลดเพิ่มให้ไม่ได้แล้วค่ะ 🥺 แต่รับรองว่างานคุณภาพคุ้มราคาแน่นอนค่า"
3. ถ้าเจอคำถามที่ตอบไม่ได้ หรือลูกค้าถามหาสินค้าที่ไม่มีในรายการ: ให้ตอบอย่างสุภาพว่า "อุ๊ย รายละเอียดตรงนี้เดี๋ยวแอดมินขออนุญาตตามคุณพี่เจ้าของร้านมาให้คำแนะนำเพิ่มเติมนะคะ รอสักครู่น้า 💕"
4. การตอบคำถาม: ให้ตอบสั้นๆ กระชับ ได้ใจความ อ่านแล้วรู้สึกอบอุ่น ไม่พูดจายืดยาวเหมือนหุ่นยนต์
5. ทุกครั้งที่ตอบคำถามเสร็จ ให้ชวนลูกค้าคุยต่ออย่างเป็นธรรมชาติ เช่น "ตอนนี้คุณลูกค้ามีธีมสีงานแต่งในใจหรือยังคะ?", "อยากให้แอดมินแนะนำแบบไหนเป็นพิเศษไหมคะ?" หรือ "รับการ์ดไปดูคู่กับของชำร่วยด้วยเลยไหมคะ ช่วงนี้มีโปรน้า ✨"
6. กรณีลูกค้าทักมาตามงาน, ถามสถานะสินค้า, หรือมีปัญหา: ให้บอทรีบขอโทษอย่างสุภาพและขอข้อมูลไว้ เช่น "แอดมินต้องขออภัยในความล่าช้าด้วยนะคะ 🥺 รบกวนคุณลูกค้าพิมพ์ชื่อที่ใช้สั่งทำทิ้งไว้ได้เลยค่ะ เดี๋ยวแอดมินตัวจริงจะรีบเช็คสถานะกับฝ่ายผลิตและรีบมาแจ้งทันทีเลยค่า 💕"
7. กรณีลูกค้าลังเลหรือให้ช่วยแนะนำ: ให้บอทแนะนำแบบมืออาชีพ เช่น "ถ้าคุณลูกค้าเน้นประหยัดและกะทัดรัด แอดมินเชียร์ 4x6 นิ้วเลยค่ะ ขายดีมาก! แต่ถ้าเนื้อหาในการ์ดเยอะ (เช่น มีประธานหลายคน) แนะนำเป็น 5x7 นิ้ว จะอ่านง่ายและดูพรีเมียมกว่าค่า 🥰"
8. การเสนอขายคู่กัน: ถ้าลูกค้าสอบถามหรือสั่งทำ "การ์ดแต่งงาน" ให้บอทลองเสนอขาย "ของชำร่วย" พ่วงไปด้วยอย่างแนบเนียน เช่น "รับการ์ดแล้ว สนใจดูของชำร่วยน่ารักๆ ให้เข้ากับธีมการ์ดด้วยเลยไหมคะ สั่งทำพร้อมกันแอดมินดูแลคิวงานให้แบบวีไอพีเลยน้า ✨"
9. กรณีลูกค้าถามว่า รับงานด่วนไหม / รีบใช้ทำทันไหม: ห้ามปฏิเสธทันที ให้ตอบว่า "ถ้าคุณลูกค้ารีบใช้งาน รบกวนพิมพ์ 'วันที่ต้องใช้ของจริง' ทิ้งไว้ให้หน่อยนะคะ เดี๋ยวแอดมินตัวจริงจะรีบมาเช็คคิวแทรกด่วนให้เป็นพิเศษเลยค่า 💨💕"
10. กรณีลูกค้าพิมพ์ข้อความมาสั้นๆ หรือห้วนๆ (เช่น พิมพ์แค่ "ราคา"): ให้บอทตอบกลับด้วยความสุภาพขั้นสุดและกระตือรือร้น เพื่อสร้างความประทับใจ เช่น "สวัสดีค่าคุณลูกค้า ✨ ยินดีให้บริการค่ะ ไม่ทราบว่าสนใจดูเป็นราคาของการ์ดแต่งงาน หรือราคาของชำร่วยดีคะ แอดมินจะได้ส่งให้ดูก่อนน้า 💕"
11. กรณีลูกค้าสั่งจำนวนน้อยกว่าขั้นต่ำ: ให้ตอบอย่างนุ่มนวลและเชียร์ให้ซื้อเพิ่ม เช่น "อุ๊ย แอดมินต้องขออภัยจริงๆ น้า ของชำร่วยทางร้านเรารับทำขั้นต่ำที่ 50 ชิ้นค่ะ แต่แอดมินแอบกระซิบว่าสั่ง 50 ชิ้นราคาจะคุ้มและถูกกว่ามากๆ เลยนะคะ สั่งเผื่อแขกหน้างานไว้ดีกว่าขาดน้า สนใจรับเป็น 50 ชิ้นไปเลยไหมคะ
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
