"""
Backend chính: FastAPI + OpenAI-compatible AI + Push Notification + Reminder
"""
import os, json, asyncio, sqlite3
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
# → push_subscriptions vẫn RAM (cần re-tap 🔔 sau restart); task/reminder/history
#   đã xuống SQLite (sống sót restart).
push_subscriptions: list[dict] = []

# ── SQLite storage ───────────────────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assistant.db")


def db():
    """Mở connection SQLite. row_factory=Row để truy cập theo tên cột."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _now_iso() -> str:
    """ISO timestamp theo Asia/Shanghai (dùng cho created_at/run_at)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
    except Exception:
        return datetime.now().isoformat()


def db_init():
    """Tạo bảng nếu chưa có. Gọi trên startup."""
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              note TEXT DEFAULT '',
              due_at TEXT,
              priority TEXT DEFAULT 'normal',
              status TEXT DEFAULT 'open',
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reminders(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              run_at TEXT NOT NULL,
              message TEXT NOT NULL,
              fired INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS conversations(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              role TEXT NOT NULL,
              content TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            """
        )


# ── Task CRUD ────────────────────────────────────────────────────────────
def task_create(title: str, note: str = "", due_at: str = "", priority: str = "normal") -> dict:
    with db() as c:
        cur = c.execute(
            "INSERT INTO tasks(title, note, due_at, priority, created_at) VALUES(?,?,?,?,?)",
            (title, note or "", due_at or None, priority or "normal", _now_iso()),
        )
        return {"id": cur.lastrowid, "title": title}


def task_list(status: str = "open") -> list[dict]:
    with db() as c:
        rows = c.execute(
            "SELECT * FROM tasks WHERE status=? ORDER BY COALESCE(due_at,'9999-12-31'), id",
            (status,),
        ).fetchall()
        return [dict(r) for r in rows]


def task_complete(task_id: int) -> dict:
    with db() as c:
        cur = c.execute("UPDATE tasks SET status='done' WHERE id=?", (task_id,))
        if cur.rowcount == 0:
            return {"ok": False, "error": f"không có task id {task_id}"}
        return {"ok": True, "id": task_id, "status": "done"}


def task_update(task_id: int, **fields) -> dict:
    allowed = {"title", "note", "due_at", "priority", "status"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed and v is not None:
            sets.append(f"{k}=?")
            vals.append(v if v != "" or k != "due_at" else None)
    if not sets:
        return {"ok": False, "error": "không có trường hợp lệ để cập nhật"}
    vals.append(task_id)
    with db() as c:
        cur = c.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", vals)
        if cur.rowcount == 0:
            return {"ok": False, "error": f"không có task id {task_id}"}
        return {"ok": True, "id": task_id, "updated": [s.split('=')[0] for s in sets]}


def task_delete(task_id: int) -> dict:
    with db() as c:
        cur = c.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        if cur.rowcount == 0:
            return {"ok": False, "error": f"không có task id {task_id}"}
        return {"ok": True, "id": task_id, "deleted": True}


# ── Reminder CRUD ─────────────────────────────────────────────────────────
def reminder_insert(run_at: str, message: str) -> int:
    with db() as c:
        cur = c.execute(
            "INSERT INTO reminders(run_at, message, fired) VALUES(?,?,0)",
            (run_at, message),
        )
        return cur.lastrowid


def reminder_list_unfired() -> list[dict]:
    with db() as c:
        rows = c.execute("SELECT * FROM reminders WHERE fired=0 ORDER BY run_at").fetchall()
        return [dict(r) for r in rows]


def reminder_mark_fired(reminder_id: int):
    with db() as c:
        c.execute("UPDATE reminders SET fired=1 WHERE id=?", (reminder_id,))


def reminder_recent(limit: int = 20) -> list[dict]:
    with db() as c:
        rows = c.execute(
            "SELECT * FROM reminders ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# ── Conversation history ──────────────────────────────────────────────────
def conv_add(role: str, content: str):
    with db() as c:
        c.execute(
            "INSERT INTO conversations(role, content, created_at) VALUES(?,?,?)",
            (role, content, _now_iso()),
        )


def conv_recent(n: int = 20) -> list[dict]:
    with db() as c:
        rows = c.execute(
            "SELECT role, content FROM conversations ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

# ── Thông tin cá nhân (BẠN SỬA Ở ĐÂY) ───────────────────────────────────
# Điền thông tin của bạn vào các giá trị bên dưới. Chưa biết thì để "".
USER_PROFILE = {
    "ten":        "Nguyễn Hoàng Dương hoặc yangd hoặc rhy",   # ví dụ: "Dương"
    "nghe":       "Sinh viên đẹp trai",   # ví dụ: "giám đốc / sinh viên / kỹ sư"
    "so_thich":   "đánh cầu, chụp ảnh",   # ví dụ: "cà phê, chạy bộ, đọc sách, du lịch"
    "khu_vuc":    "đang ở Thượng Hải, quê nhà Hà Nội",   # ví dụ: "Thượng Hải, Trung Quốc"
    "ghi_chu":    "Nói chuyện cute đáng yêu",   # thêm gì tùy thích, ví dụ: "dậy sớm 6h, ngôn ngữ V-AI"
}

# ── System prompt (template, được bơm thông tin + thời gian thực mỗi request) ─
SYSTEM_PROMPT_TEMPLATE = """Bạn là trợ lý AI cá nhân thân thiết của tôi.
Vai trò: như một trợ lý giám đốc thực sự — quản lý lịch, nhắc nhở, task, tóm tắt thông tin.
Tính cách: thông minh, gần gũi, thi thoảng hài hước nhẹ nhàng.
Ngôn ngữ: tiếng Việt, tự nhiên.

# Thông tin về tôi
- Tên: {ten}
- Nghề nghiệp: {nghe}
- Sở thích: {so_thich}
- Khu vực: {khu_vuc}
- Ghi chú: {ghi_chu}
Những trường "(chưa rõ)" là vì tôi chưa điền — đừng đoán, cứ hỏi lại nếu cần.

# Nhận thức thời gian / thế giới
- Hiện tại: {now} ({weekday}), {date_full}
- Múi giờ: Asia/Shanghai (UTC+8)
- Mùa: {season} (Bắc bán cầu)
Căn cứ vào thời gian thực trên để tính "sáng/chiều/tối", "hôm nay/ngày mai/tuần sau",
thứ trong tuần, cuối tuần, v.v. khi đặt nhắc nhở hoặc trả lời. Khi nhắc thời gian
cho user, dùng múi giờ Asia/Shanghai.

# Đặt nhắc nhở
Nếu user muốn đặt nhắc nhở, trả về JSON trong thẻ <reminder>:
<reminder>{{"time": "HH:MM", "date": "YYYY-MM-DD", "message": "nội dung nhắc"}}</reminder>
Phần còn lại trả lời bình thường."""

# Thứ trong tuần theo tiếng Việt (Monday=0 ...)
_WEEKDAYS_VI = ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm",
                "Thứ Sáu", "Thứ Bảy", "Chủ Nhật"]


def _season_vi(month: int) -> str:
    """Mùa theo Bắc bán cầu."""
    if month in (3, 4, 5):
        return "Xuân"
    if month in (6, 7, 8):
        return "Hè"
    if month in (9, 10, 11):
        return "Thu"
    return "Đông"


def build_system_prompt() -> str:
    """Bơm thông tin cá nhân + thời gian thực (Asia/Shanghai) vào system prompt."""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
    except Exception:
        # Fallback: giờ máy nếu không có tzdata (không mong đợi trên server).
        now = datetime.now()
    return SYSTEM_PROMPT_TEMPLATE.format(
        ten=USER_PROFILE["ten"] or "(chưa rõ)",
        nghe=USER_PROFILE["nghe"] or "(chưa rõ)",
        so_thich=USER_PROFILE["so_thich"] or "(chưa rõ)",
        khu_vuc=USER_PROFILE["khu_vuc"] or "(chưa rõ)",
        ghi_chu=USER_PROFILE["ghi_chu"] or "(không)",
        now=now.strftime("%H:%M"),
        weekday=_WEEKDAYS_VI[now.weekday()],
        date_full=f"{now.day}/{now.month}/{now.year}",
        season=_season_vi(now.month),
    )

# ── Tools (native function calling) ──────────────────────────────────────
# Mỗi tool trả string để đút lại cho model ở role "tool". Schema theo format
# OpenAI function-calling; model sẽ trả tool_calls nếu proxy hỗ trợ.

TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "Lấy thời gian thực hiện tại (giờ, thứ, ngày, mùa) theo múi giờ chỉ định. Dùng khi user hỏi giờ/ngày hoặc khi cần tính thời gian để đặt nhắc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "timezone": {
                        "type": "string",
                        "description": "Múi giờ IANA, mặc định Asia/Shanghai",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_task",
            "description": "Tạo một công việc/task mới cho user. Trả về id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Tên ngắn gọn của công việc"},
                    "note": {"type": "string", "description": "Ghi chú chi tiết (tùy chọn)"},
                    "due_at": {
                        "type": "string",
                        "description": "Hạn chót định dạng YYYY-MM-DD HH:MM (tùy chọn)",
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["low", "normal", "high"],
                        "description": "Mức ưu tiên, mặc định normal",
                    },
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tasks",
            "description": "Liệt kê công việc của user. Mặc định chỉ lấy task đang mở (chưa xong).",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["open", "done", "all"],
                        "description": "Lọc theo trạng thái, mặc định open",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_task",
            "description": "Đánh dấu một task đã hoàn thành theo id.",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "integer"}},
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_task",
            "description": "Cập nhật một hoặc nhiều trường của task theo id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "title": {"type": "string"},
                    "note": {"type": "string"},
                    "due_at": {"type": "string", "description": "YYYY-MM-DD HH:MM"},
                    "priority": {"type": "string", "enum": ["low", "normal", "high"]},
                    "status": {"type": "string", "enum": ["open", "done"]},
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_task",
            "description": "Xóa vĩnh viễn một task theo id.",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "integer"}},
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_reminder",
            "description": "Đặt nhắc nhở push notification vào một thời điểm cụ thể. Yêu cầu giờ HH:MM và ngày YYYY-MM-DD.",
            "parameters": {
                "type": "object",
                "properties": {
                    "time": {"type": "string", "description": "Giờ định dạng HH:MM (24h)"},
                    "date": {"type": "string", "description": "Ngày định dạng YYYY-MM-DD"},
                    "message": {"type": "string", "description": "Nội dung nhắc"},
                },
                "required": ["time", "date", "message"],
            },
        },
    },
]

MAX_TOOL_ROUNDS = 6


def _tool_get_current_time(timezone: str = "Asia/Shanghai") -> str:
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(timezone)
    except Exception:
        tz = TZ  # fallback Asia/Shanghai (định nghĩa ở section scheduler)
    now = datetime.now(tz)
    return (
        f"{now.strftime('%H:%M')} ({_WEEKDAYS_VI[now.weekday()]}), "
        f"{now.day}/{now.month}/{now.year}, mùa {_season_vi(now.month)}, "
        f"múi giờ {timezone}"
    )


def _tool_create_task(title, note="", due_at="", priority="normal") -> str:
    t = task_create(title, note=note, due_at=due_at, priority=priority)
    return f"Đã tạo task id={t['id']}: {t['title']}"


def _tool_list_tasks(status="open") -> str:
    rows = task_list("all" if status == "all" else status)
    if not rows:
        return f"Không có task nào (status={status})."
    lines = []
    for r in rows:
        due = f", hạn {r['due_at']}" if r["due_at"] else ""
        lines.append(f"#{r['id']} [{r['status']}/{r['priority']}] {r['title']}{due}")
    return "\n".join(lines)


def _tool_complete_task(id) -> str:
    r = task_complete(int(id))
    return f"OK: task {id} -> done" if r.get("ok") else f"Lỗi: {r.get('error')}"


def _tool_update_task(id, **kwargs) -> str:
    r = task_update(int(id), **kwargs)
    return f"OK: đã cập nhật task {id}" if r.get("ok") else f"Lỗi: {r.get('error')}"


def _tool_delete_task(id) -> str:
    r = task_delete(int(id))
    return f"OK: đã xóa task {id}" if r.get("ok") else f"Lỗi: {r.get('error')}"


def _tool_set_reminder(time, date, message) -> str:
    status = schedule_reminder({"time": time, "date": date, "message": message})
    if status.get("ok"):
        return f"Đã đặt nhắc lúc {status['run_at']}: {status['message']}"
    return f"Lỗi đặt nhắc: {status.get('error')}"


TOOL_FUNCTIONS = {
    "get_current_time": _tool_get_current_time,
    "create_task": _tool_create_task,
    "list_tasks": _tool_list_tasks,
    "complete_task": _tool_complete_task,
    "update_task": _tool_update_task,
    "delete_task": _tool_delete_task,
    "set_reminder": _tool_set_reminder,
}


def _run_tool(name: str, args_json: str) -> str:
    """Dispatch một tool call. Trả string để đút lại cho model (role 'tool')."""
    fn = TOOL_FUNCTIONS.get(name)
    if not fn:
        return f"Lỗi: tool '{name}' không tồn tại."
    try:
        args = json.loads(args_json) if args_json else {}
    except Exception as e:
        return f"Lỗi parse tham số: {e}"
    try:
        # Chỉ truyền tham số mà fn chấp nhận (loại bỏ thừa từ model).
        import inspect
        params = inspect.signature(fn).parameters
        clean = {k: v for k, v in args.items() if k in params}
        return str(fn(**clean))
    except Exception as e:
        return f"Lỗi khi chạy tool {name}: {type(e).__name__}: {e}"


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

# ── Route: Debug tools (kiểm tra proxy/model có hỗ trợ function calling) ────
@app.get("/debug-tools")
async def debug_tools(model: str = "", base_url: str = "", api_key: str = ""):
    """
    Gửi 1 message tối giản kèm 1 tool (get_current_time) để xem model/proxy
    có trả tool_calls không. Trả {supports_tools, tool_calls, content, error}.
    Dùng env làm mặc định; có thể override qua query params.
    """
    key  = (api_key or OPENAI_API_KEY).strip()
    url  = (base_url or OPENAI_BASE_URL).strip()
    mdl  = (model or "krr/claude-haiku-4-5-20251001").strip()
    if not key:
        return JSONResponse(
            {"supports_tools": False, "error": "Chưa có API key (env hoặc ?api_key=)."},
            status_code=400,
        )
    try:
        client = OpenAI(api_key=key, base_url=url)
        resp = client.chat.completions.create(
            model=mdl,
            messages=[
                {"role": "system", "content": "Bạn là trợ lý. Khi cần thời gian, hãy gọi tool."},
                {"role": "user", "content": "Bây giờ là mấy giờ?"},
            ],
            tools=TOOL_SPECS,
            tool_choice="auto",
            max_tokens=500,
        )
        msg = resp.choices[0].message
        tool_calls = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                tool_calls.append(
                    {"name": tc.function.name, "arguments": tc.function.arguments}
                )
        return JSONResponse({
            "supports_tools": bool(tool_calls),
            "model": mdl,
            "tool_calls": tool_calls,
            "content": msg.content or "",
        })
    except Exception as e:
        return JSONResponse(
            {"supports_tools": False, "model": mdl, "error": f"{type(e).__name__}: {e}"},
            status_code=500,
        )


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

    # Lưu user msg vào history (DB) và lấy 20 tin gần nhất
    conv_add("user", user_msg)
    history = conv_recent(20)

    try:
        messages = [
            {"role": "system", "content": build_system_prompt()},
            *history,
        ]

        # Agent loop: model có thể gọi tool → chạy → đút kết quả → gọi lại.
        reply = ""
        for _ in range(MAX_TOOL_ROUNDS):
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=TOOL_SPECS,
                tool_choice="auto",
                max_tokens=1200,
            )
            msg = response.choices[0].message
            # Giữ nguyên assistant message (kèm tool_calls) để gửi lại vòng sau.
            messages.append(msg.model_dump(exclude_none=True))

            if not msg.tool_calls:
                reply = (msg.content or "").strip()
                break

            # Chạy từng tool call, đút kết quả về role "tool".
            for tc in msg.tool_calls:
                result = _run_tool(tc.function.name, tc.function.arguments)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    }
                )
        else:
            # Vượt quá số vòng cho phép — lấy nội dung assistant cuối nếu có.
            last = next(
                (m for m in reversed(messages) if m.get("role") == "assistant" and m.get("content")),
                None,
            )
            reply = (last["content"] if last else "").strip() or "⚠️ Quá nhiều bước, thử lại nhé."

        # Fallback: model có thể vẫn dùng thẻ <reminder> thay vì tool set_reminder
        # (hoặc proxy strip tools). Vẫn xử lý như cũ.
        reminder = extract_reminder(reply)
        reminder_status = None
        if reminder:
            reminder_status = schedule_reminder(reminder)

        # Làm sạch thẻ <reminder> khỏi reply hiển thị
        import re
        reply = re.sub(r'<reminder>.*?</reminder>', '', reply, flags=re.DOTALL).strip()

        if reminder:
            if reminder_status and reminder_status.get("ok"):
                reply += f"\n\n✅ Đã đặt nhắc lúc {reminder_status['run_at']}: {reminder_status['message']}"
            else:
                err = reminder_status.get("error", "?") if reminder_status else "?"
                reply += f"\n\n⚠️ Đặt nhắc không thành công ({err}). Hãy ghi rõ giờ HH:MM và ngày YYYY-MM-DD."

        conv_add("assistant", reply)
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


def _fire_reminder(reminder_id: int, message: str):
    """Callback APScheduler: gửi push rồi đánh dấu fired trong DB."""
    send_push("⏰ Nhắc nhở", message)
    try:
        reminder_mark_fired(reminder_id)
    except Exception as e:
        print(f"[reminder] mark_fired error id={reminder_id}: {e}")


def schedule_reminder_at(dt: datetime, message: str) -> dict:
    """Lên lịch 1 nhắc tại dt, persist vào DB. Trả {ok, run_at, message, id, error}."""
    dt_str = dt.strftime("%Y-%m-%d %H:%M")
    now = datetime.now(TZ)
    if dt <= now:
        print(f"[reminder] bỏ qua (đã qua giờ): {dt_str} | now={now.strftime('%Y-%m-%d %H:%M')}")
        return {"ok": False, "error": f"giờ đã qua ({dt_str})", "run_at": dt_str, "message": message}
    rid = reminder_insert(dt_str, message)
    scheduler.add_job(
        lambda rid=rid, m=message: _fire_reminder(rid, m),
        'date', run_date=dt,
        id=f"reminder-{rid}",
        replace_existing=True,
    )
    print(f"[reminder] ĐÃ LÊN LỊCH: {dt_str} ({dt.isoformat()}) -> {message} (id={rid})")
    return {"ok": True, "run_at": dt_str, "message": message, "id": rid}


def schedule_reminder(reminder: dict) -> dict:
    """Parse {time,date,message} rồi gọi schedule_reminder_at. Dùng cho cả tool và fallback thẻ."""
    msg = str(reminder.get('message', '')).strip()
    try:
        time_str = str(reminder.get('time', '')).strip()
        if ':' in time_str:
            time_str = time_str[:5]
        date_str = str(reminder.get('date', '')).strip() or datetime.now(TZ).strftime('%Y-%m-%d')
        dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
        return schedule_reminder_at(dt, msg)
    except Exception as e:
        print(f"[reminder] schedule error: {e} | raw={reminder}")
        return {"ok": False, "error": str(e), "run_at": "", "message": msg}


def _reschedule_reminders():
    """Khởi động lại: lên lịch lại các reminder chưa fired trong DB (APScheduler mất khi restart)."""
    rows = reminder_list_unfired()
    now = datetime.now(TZ)
    scheduled = 0
    for r in rows:
        try:
            dt = datetime.strptime(r["run_at"], "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
            if dt <= now:
                # đã qua giờ khi server đang tắt → đánh dấu fired, không spam.
                reminder_mark_fired(r["id"])
                continue
            scheduler.add_job(
                lambda rid=r["id"], m=r["message"]: _fire_reminder(rid, m),
                'date', run_date=dt,
                id=f"reminder-{r['id']}",
                replace_existing=True,
            )
            scheduled += 1
        except Exception as e:
            print(f"[reminder] resume error id={r['id']}: {e}")
    if rows:
        print(f"[reminder] resumed {scheduled}/{len(rows)} unfired from DB")


@app.get("/debug-reminders")
async def debug_reminders():
    """Liệt kê reminder trong DB và các job APScheduler đang chờ."""
    return JSONResponse({
        "now": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S %Z"),
        "reminders_in_db": reminder_recent(50),
        "scheduled_jobs": [
            {"id": j.id, "next_run": str(j.next_run_time)}
            for j in scheduler.get_jobs()
        ],
    })


# ── Startup ───────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    db_init()
    scheduler.start()
    _reschedule_reminders()

@app.on_event("shutdown")
async def shutdown():
    scheduler.shutdown()
