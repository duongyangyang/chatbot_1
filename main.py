"""
Backend chính: FastAPI + OpenAI-compatible AI + Push Notification + Reminder
"""
import os, json, asyncio
from datetime import datetime, timedelta
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pywebpush import webpush, WebPushException

# Tải biến môi trường từ .env (nếu có) trước khi đọc cấu hình.
# Tìm cả ở cwd và cạnh file main.py để chắc chắn nạp được dù chạy từ thư mục khác.
load_dotenv()
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

app = FastAPI()

# ── Config ──────────────────────────────────────────────────────────────
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.vilao.ai/v1")
VAPID_PRIVATE   = os.getenv("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC    = os.getenv("VAPID_PUBLIC_KEY", "")
VAPID_EMAIL     = os.getenv("VAPID_EMAIL", "mailto:you@example.com")

# pywebpush KHÔNG parse được PEM dưới dạng chuỗi (Vapid.from_string chỉ nhận
# DER/raw base64, gặp PEM thì lỗi "ASN.1 parsing error: invalid length").
# Nên dựng sẵn object Vapid từ PEM rồi truyền object vào webpush().
_VAPID_INSTANCE = None
if VAPID_PRIVATE:
    try:
        from py_vapid import Vapid
        _VAPID_INSTANCE = Vapid.from_pem(VAPID_PRIVATE.encode())
    except Exception as e:
        print(f"[vapid] Không nạp được PEM private key: {e}")

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

@app.get("/index.html")
async def index_html():
    # Service worker precache yêu cầu /index.html phải 200 (nếu 404, addAll fail → SW không cài được)
    return FileResponse("index.html")

@app.get("/sw.js")
async def sw():
    return FileResponse("sw.js", media_type="application/javascript")

@app.get("/manifest.json")
async def manifest():
    return FileResponse("manifest.json")

# ── Route: VAPID public key (cho client dùng khi subscribe push) ──────────
@app.get("/vapid-public-key")
async def vapid_public_key():
    if not VAPID_PUBLIC:
        return JSONResponse(
            {"error": "VAPID_PUBLIC_KEY chưa được cấu hình trên server."},
            status_code=500,
        )
    return JSONResponse({"publicKey": VAPID_PUBLIC})

# ── Route: Debug push (kiểm tra trạng thái VAPID + số thiết bị đã đăng ký) ──
def _vapid_public_valid(key: str) -> bool:
    """True nếu key giải mã ra đúng 65 byte, byte đầu = 0x04 (P-256 uncompressed)."""
    import base64
    if not key:
        return False
    try:
        raw = base64.urlsafe_b64decode(key + "=" * (-len(key) % 4))
        return len(raw) == 65 and raw[0] == 0x04
    except Exception:
        return False

def _vapid_keys_match() -> bool:
    """True nếu public key sinh ra đúng từ private key đang có."""
    try:
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization
        import base64
        key = serialization.load_pem_private_key(VAPID_PRIVATE.encode(), password=None)
        if not isinstance(key.curve, ec.SECP256R1):
            return False
        derived = base64.urlsafe_b64encode(
            key.public_key().public_bytes(
                serialization.Encoding.X962,
                serialization.PublicFormat.UncompressedPoint,
            )
        ).decode().rstrip("=")
        return derived == VAPID_PUBLIC
    except Exception as e:
        print(f"[vapid] keys_match check error: {e}")
        return False

@app.get("/debug-push")
async def debug_push():
    return JSONResponse({
        "vapid_public_set": bool(VAPID_PUBLIC),
        "vapid_public_valid": _vapid_public_valid(VAPID_PUBLIC),
        "vapid_private_set": bool(VAPID_PRIVATE),
        "vapid_keys_match": _vapid_keys_match(),
        "vapid_email": VAPID_EMAIL,
        "subscribers": len(push_subscriptions),
    })

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
        reminder_status = None
        if reminder:
            reminders.append(reminder)
            reminder_status = schedule_reminder(reminder)

        # Làm sạch reply (bỏ thẻ <reminder>)
        import re
        reply = re.sub(r'<reminder>.*?</reminder>', '', reply_raw, flags=re.DOTALL).strip()

        # Báo lại kết quả đặt nhắc để user biết có lên lịch hay không
        if reminder:
            if reminder_status and reminder_status.get("ok"):
                reply += f"\n\n✅ Đã đặt nhắc lúc {reminder_status['run_at']}: {reminder_status['message']}"
            else:
                err = reminder_status.get("error", "?") if reminder_status else "?"
                reply += f"\n\n⚠️ Đặt nhắc không thành công ({err}). Hãy ghi rõ giờ HH:MM và ngày YYYY-MM-DD."

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
def send_push(title: str, body: str) -> dict:
    """Gửi push notification đến tất cả thiết bị đã đăng ký. Trả về tóm tắt + in lỗi ra log."""
    summary = {"sent": 0, "failed": [], "skipped": ""}
    if not _VAPID_INSTANCE:
        summary["skipped"] = "VAPID private key chưa nạp được (kiểm tra định dạng PEM trong .env)"
        print(f"[push] skip: {summary['skipped']}")
        return summary
    if not push_subscriptions:
        summary["skipped"] = "chưa có subscription nào"
        print("[push] skip: chưa có subscription")
        return summary

    payload = json.dumps({"title": title, "body": body})
    dead = []

    for i, sub in enumerate(push_subscriptions):
        endpoint = sub.get("endpoint", "?")
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=_VAPID_INSTANCE,
                vapid_claims={"sub": VAPID_EMAIL},
            )
            summary["sent"] += 1
            print(f"[push] OK -> {endpoint[:70]}")
        except WebPushException as e:
            msg = str(e)
            print(f"[push] FAIL [{i}] {endpoint[:70]} -> {msg}")
            summary["failed"].append({"endpoint": endpoint, "error": msg})
            if "410" in msg or "404" in msg:   # subscription hết hạn
                dead.append(sub)
        except Exception as e:
            print(f"[push] ERROR [{i}] {type(e).__name__}: {e}")
            summary["failed"].append({"endpoint": endpoint, "error": f"{type(e).__name__}: {e}"})

    for d in dead:
        push_subscriptions.remove(d)
    return summary


# ── Route: Test push (gửi 1 thông báo thử để kiểm tra) ─────────────────────
@app.get("/test-push")
async def test_push():
    if not _VAPID_INSTANCE:
        return JSONResponse({"error": "VAPID private key chưa nạp được (kiểm tra PEM trong .env)"}, status_code=500)
    if not push_subscriptions:
        return JSONResponse({"error": "Chưa có thiết bị nào đăng ký nhận thông báo. Hãy mở app, bật thông báo trước."}, status_code=400)
    return JSONResponse(send_push("🧪 Kiểm tra", "Thông báo thử từ server"))

# ── Route: Test push có trì hoãn (lên lịch gửi sau N giây) ─────────────────
@app.post("/test-push-delayed")
async def test_push_delayed(request: Request):
    data = await request.json() if request.headers.get("content-type") == "application/json" else {}
    delay = data.get("delay", 10)
    try:
        delay = int(delay)
        if delay < 1 or delay > 3600:
            delay = 30
    except (TypeError, ValueError):
        delay = 30

    if not _VAPID_INSTANCE:
        return JSONResponse({"error": "VAPID private key chưa nạp được (kiểm tra PEM trong .env)"}, status_code=500)
    if not push_subscriptions:
        return JSONResponse({"error": "Chưa có thiết bị nào đăng ký. Hãy bấm 🔔 Thông báo trên app trước."}, status_code=400)

    run_at = datetime.now() + timedelta(seconds=delay)
    scheduler.add_job(
        lambda: send_push("🧪 Kiểm tra", f"Thông báo thử (sau {delay}s)"),
        'date', run_date=run_at,
    )
    return JSONResponse({"status": "scheduled", "in_seconds": delay})


# ── Reminder scheduler ────────────────────────────────────────────────────
from zoneinfo import ZoneInfo
TZ = ZoneInfo("Asia/Shanghai")  # múi giờ Trung Quốc (UTC+8)
scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")

def schedule_reminder(reminder: dict) -> dict:
    """Lên lịch nhắc nhở. Trả về {ok, run_at, message, error} để báo lại user."""
    msg = str(reminder.get('message', '')).strip()
    try:
        time_str = str(reminder.get('time', '')).strip()
        # Chấp nhận HH:MM hoặc HH:MM:SS → lấy HH:MM
        if ':' in time_str:
            time_str = time_str[:5]
        date_str = str(reminder.get('date', '')).strip() or datetime.now(TZ).strftime('%Y-%m-%d')
        dt_str = f"{date_str} {time_str}"
        dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
        now = datetime.now(TZ)
        if dt <= now:
            print(f"[reminder] bỏ qua (đã qua giờ): {dt_str} | now={now.strftime('%Y-%m-%d %H:%M')}")
            return {"ok": False, "error": f"giờ đã qua ({dt_str})", "run_at": dt_str, "message": msg}
        scheduler.add_job(
            lambda m=msg: send_push("⏰ Nhắc nhở", m),
            'date', run_date=dt,
        )
        print(f"[reminder] ĐÃ LÊN LỊCH: {dt_str} ({dt.isoformat()}) -> {msg}")
        return {"ok": True, "run_at": dt_str, "message": msg}
    except Exception as e:
        print(f"[reminder] schedule error: {e} | raw={reminder}")
        return {"ok": False, "error": str(e), "run_at": "", "message": msg}


@app.get("/debug-reminders")
async def debug_reminders():
    """Liệt kê reminder đã nhận và các job đang chờ — để kiểm tra xem có lên lịch chưa."""
    return JSONResponse({
        "now": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S %Z"),
        "reminders_received": reminders,
        "scheduled_jobs": [
            {"id": j.id, "next_run": str(j.next_run_time)}
            for j in scheduler.get_jobs()
        ],
    })


# ── Startup ───────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    scheduler.start()

@app.on_event("shutdown")
async def shutdown():
    scheduler.shutdown()
