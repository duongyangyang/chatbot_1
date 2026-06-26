"""
Backend chính: FastAPI + OpenAI-compatible AI + Push Notification + Reminder
"""
import os, json, asyncio
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pywebpush import webpush, WebPushException

app = FastAPI()

# ── Config ──────────────────────────────────────────────────────────────
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.vilao.ai/v1")
VAPID_PRIVATE   = os.getenv("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC    = os.getenv("VAPID_PUBLIC_KEY", "")
VAPID_EMAIL     = os.getenv("VAPID_EMAIL", "mailto:you@example.com")

# client được tạo động theo config của từng request

# In-memory storage (thay bằng Redis/PostgreSQL sau)
chat_history: list[dict] = []
push_subscriptions: list[dict] = []
reminders: list[dict] = []

# ── System prompt ────────────────────────────────────────────────────────
SYSTEM_PROMPT = """Bạn là trợ lý AI cá nhân thân thiết của tôi.
Vai trò: như một trợ lý giám đốc thực sự — quản lý lịch, nhắc nhở, task, tóm tắt thông tin.
Tính cách: thông minh, gần gũi, thi thoảng hài hước nhẹ nhàng. 
Ngôn ngữ: tiếng Việt, tự nhiên như bạn bè.
Nếu user muốn đặt nhắc nhở, trả về JSON trong thẻ <reminder>:
<reminder>{"time": "HH:MM", "date": "YYYY-MM-DD", "message": "nội dung nhắc"}</reminder>
Phần còn lại trả lời bình thường."""

# ── Routes: Static files ─────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def index():
    return FileResponse("index.html")

@app.get("/sw.js")
async def sw():
    return FileResponse("sw.js", media_type="application/javascript")

@app.get("/manifest.json")
async def manifest():
    return FileResponse("manifest.json")

# ── Route: Chat ───────────────────────────────────────────────────────────
@app.post("/chat")
async def chat(request: Request):
    data = await request.json()
    user_msg = data.get("message", "").strip()
    if not user_msg:
        return JSONResponse({"reply": "Bạn chưa nhắn gì."})

    api_key  = data.get("api_key", OPENAI_API_KEY).strip()
    base_url = data.get("base_url", OPENAI_BASE_URL).strip()
    model    = data.get("model", "krr/claude-haiku-4-5-20251001").strip()

    if not api_key:
        return JSONResponse({"reply": "⚙️ Bạn chưa cài API key. Nhấn nút Cài đặt để điền thông tin."})

    client = OpenAI(api_key=api_key, base_url=base_url)

    # Thêm vào history
    chat_history.append({"role": "user", "content": user_msg})

    # Giữ tối đa 20 tin gần nhất
    history = chat_history[-20:]

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                *history,
            ],
            max_tokens=1000,
        )
        reply_raw = response.choices[0].message.content or ""

        # Parse reminder nếu có
        reminder = extract_reminder(reply_raw)
        if reminder:
            reminders.append(reminder)
            schedule_reminder(reminder)

        # Làm sạch reply (bỏ thẻ <reminder>)
        import re
        reply = re.sub(r'<reminder>.*?</reminder>', '', reply_raw, flags=re.DOTALL).strip()

        chat_history.append({"role": "assistant", "content": reply})
        return JSONResponse({"reply": reply})

    except Exception as e:
        return JSONResponse({"reply": f"Lỗi: {str(e)}"}, status_code=500)


def extract_reminder(text: str) -> Optional[dict]:
    import re
    match = re.search(r'<reminder>(.*?)</reminder>', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except:
            return None
    return None


# ── Route: Push subscription ──────────────────────────────────────────────
@app.post("/subscribe")
async def subscribe(request: Request):
    sub = await request.json()
    if sub not in push_subscriptions:
        push_subscriptions.append(sub)
    return JSONResponse({"status": "ok"})


# ── Push notification helper ──────────────────────────────────────────────
def send_push(title: str, body: str):
    """Gửi push notification đến tất cả thiết bị đã đăng ký."""
    if not VAPID_PRIVATE or not push_subscriptions:
        return

    payload = json.dumps({"title": title, "body": body})
    dead = []

    for sub in push_subscriptions:
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=VAPID_PRIVATE,
                vapid_claims={"sub": VAPID_EMAIL}
            )
        except WebPushException as e:
            if "410" in str(e):   # subscription expired
                dead.append(sub)

    for d in dead:
        push_subscriptions.remove(d)


# ── Reminder scheduler ────────────────────────────────────────────────────
scheduler = AsyncIOScheduler(timezone="Asia/Ho_Chi_Minh")

def schedule_reminder(reminder: dict):
    """Lên lịch nhắc nhở từ dict {time, date, message}."""
    try:
        dt_str = f"{reminder.get('date', datetime.now().strftime('%Y-%m-%d'))} {reminder['time']}"
        dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
        if dt > datetime.now():
            scheduler.add_job(
                lambda msg=reminder['message']: send_push("⏰ Nhắc nhở", msg),
                'date', run_date=dt
            )
    except Exception as e:
        print(f"Schedule error: {e}")


# ── Startup ───────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    scheduler.start()

@app.on_event("shutdown")
async def shutdown():
    scheduler.shutdown()
