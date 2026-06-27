"""
Backend chính: FastAPI + OpenAI-compatible AI + Push Notification + Reminder
"""
import os, json, asyncio, sqlite3
from datetime import datetime, timedelta
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
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

# ── SQLite storage (memory.db) ──────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory.db")


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


# Thông tin cá nhân mặc định (seed vào user_profile row id=1 khi init).
# Sau khi chạy lần đầu, thông tin nằm trong DB và có thể sửa qua chat (update_profile).
_DEFAULT_PROFILE = {
    "ten":      "Nguyễn Hoàng Dương hoặc yangd hoặc rhy",
    "nghe":     "Sinh viên đẹp trai",
    "so_thich": "đánh cầu, chụp ảnh",
    "khu_vuc":  "đang ở Thượng Hải, quê nhà Hà Nội",
    "ghi_chu":  "Chú ý: Hạn chế dùng icon, không dùng dấu <---> và hạn chế để dòng trắng, nói chuyện cute đáng yêu.",
}


def db_init():
    """Tạo tất cả bảng nếu chưa có + seed các single-row table. Gọi trên startup."""
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
              fired INTEGER DEFAULT 0,
              source TEXT DEFAULT 'manual'
            );
            CREATE TABLE IF NOT EXISTS conversations(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              role TEXT NOT NULL,
              content TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              note TEXT DEFAULT '',
              start_at TEXT,
              end_at TEXT,
              location TEXT DEFAULT '',
              all_day INTEGER DEFAULT 0,
              status TEXT DEFAULT 'confirmed',
              source TEXT DEFAULT 'manual',
              schedule_id INTEGER,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS goals(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              description TEXT DEFAULT '',
              level TEXT NOT NULL,           -- year | month | week
              period TEXT DEFAULT '',         -- vd: 2026 / 2026-06 / 2026-W26
              parent_id INTEGER,
              status TEXT DEFAULT 'active',
              progress_note TEXT DEFAULT '',
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS journals(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              mood TEXT DEFAULT '',
              content TEXT NOT NULL,
              tags TEXT DEFAULT '',
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS long_term_memory(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              content TEXT NOT NULL,
              confidence REAL DEFAULT 0.5,
              source TEXT DEFAULT 'journal',
              created_at TEXT NOT NULL,
              last_used_at TEXT
            );
            CREATE TABLE IF NOT EXISTS user_profile(
              id INTEGER PRIMARY KEY CHECK(id=1),
              ten TEXT DEFAULT '',
              nghe TEXT DEFAULT '',
              so_thich TEXT DEFAULT '',
              khu_vuc TEXT DEFAULT '',
              ghi_chu TEXT DEFAULT '',
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS life_os_state(
              id INTEGER PRIMARY KEY CHECK(id=1),
              current_life_phase TEXT DEFAULT '',
              top_3_priorities TEXT DEFAULT '',   -- JSON array
              current_risks TEXT DEFAULT '',       -- JSON array
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reports(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              type TEXT NOT NULL,             -- morning_planning | daily_review | weekly_review | monthly_review
              period TEXT DEFAULT '',
              content TEXT NOT NULL,
              schedule_id INTEGER,
              read INTEGER DEFAULT 0,
              pushed INTEGER DEFAULT 0,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS schedules(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              date TEXT NOT NULL,
              slots TEXT NOT NULL,            -- JSON array of {time,title,type,event_id?}
              status TEXT DEFAULT 'draft',    -- draft | approved
              created_at TEXT NOT NULL,
              approved_at TEXT
            );
            CREATE TABLE IF NOT EXISTS app_config(
              id INTEGER PRIMARY KEY CHECK(id=1),
              base_url TEXT DEFAULT '',
              model TEXT DEFAULT '',
              api_key_hint TEXT DEFAULT '',   -- API key thật, chỉ dùng server-side, KHÔNG trả ra GET endpoint
              updated_at TEXT NOT NULL
            );
            """
        )
        # Seed single-row tables (chỉ khi chưa có row id=1).
        c.execute("INSERT OR IGNORE INTO user_profile(id, ten, nghe, so_thich, khu_vuc, ghi_chu, updated_at) VALUES(1,?,?,?,?,?,?)",
                  (_DEFAULT_PROFILE["ten"], _DEFAULT_PROFILE["nghe"], _DEFAULT_PROFILE["so_thich"],
                   _DEFAULT_PROFILE["khu_vuc"], _DEFAULT_PROFILE["ghi_chu"], _now_iso()))
        c.execute("INSERT OR IGNORE INTO life_os_state(id, current_life_phase, top_3_priorities, current_risks, updated_at) VALUES(1,'','','',?)",
                  (_now_iso(),))
        c.execute("INSERT OR IGNORE INTO app_config(id, base_url, model, api_key_hint, updated_at) VALUES(1,'','','',?)",
                  (_now_iso(),))


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
def reminder_insert(run_at: str, message: str, source: str = "manual") -> int:
    with db() as c:
        cur = c.execute(
            "INSERT INTO reminders(run_at, message, fired, source) VALUES(?,?,0,?)",
            (run_at, message, source),
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

# ── User profile / Life OS state / App config (single-row tables) ───────────
def profile_get() -> dict:
    with db() as c:
        r = c.execute("SELECT * FROM user_profile WHERE id=1").fetchone()
        return dict(r) if r else dict(_DEFAULT_PROFILE)


def profile_update(**fields) -> dict:
    allowed = {"ten", "nghe", "so_thich", "khu_vuc", "ghi_chu"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed and v is not None:
            sets.append(f"{k}=?")
            vals.append(v)
    if not sets:
        return {"ok": False, "error": "không có trường hợp lệ"}
    sets.append("updated_at=?")
    vals.append(_now_iso())
    with db() as c:
        c.execute(f"UPDATE user_profile SET {', '.join(sets)} WHERE id=1", vals)
        return {"ok": True, "updated": [f for f in fields if f in allowed]}


def life_state_get() -> dict:
    with db() as c:
        r = c.execute("SELECT * FROM life_os_state WHERE id=1").fetchone()
        return dict(r) if r else {}


def life_state_update(**fields) -> dict:
    allowed = {"current_life_phase", "top_3_priorities", "current_risks"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed and v is not None:
            sets.append(f"{k}=?")
            vals.append(v)
    if not sets:
        return {"ok": False, "error": "không có trường hợp lệ"}
    sets.append("updated_at=?")
    vals.append(_now_iso())
    with db() as c:
        c.execute(f"UPDATE life_os_state SET {', '.join(sets)} WHERE id=1", vals)
        return {"ok": True, "updated": [f for f in fields if f in allowed]}


def config_get() -> dict:
    """Lấy last-used config (api_key_hint chỉ dùng nội bộ server-side)."""
    with db() as c:
        r = c.execute("SELECT * FROM app_config WHERE id=1").fetchone()
        return dict(r) if r else {"base_url": "", "model": "", "api_key_hint": ""}


def config_set(base_url: str = "", model: str = "", api_key: str = "") -> None:
    """Lưu last-used config để scheduled job có thể gọi LLM mà không cần request."""
    sets, vals = [], []
    if base_url:
        sets.append("base_url=?"); vals.append(base_url)
    if model:
        sets.append("model=?"); vals.append(model)
    if api_key:
        sets.append("api_key_hint=?"); vals.append(api_key)
    if not sets:
        return
    sets.append("updated_at=?"); vals.append(_now_iso())
    with db() as c:
        c.execute(f"UPDATE app_config SET {', '.join(sets)} WHERE id=1", vals)


# ── Event CRUD ────────────────────────────────────────────────────────────
def event_create(title: str, start_at: str = "", end_at: str = "", note: str = "",
                 location: str = "", all_day: bool = False, source: str = "manual",
                 schedule_id=None) -> dict:
    with db() as c:
        cur = c.execute(
            """INSERT INTO events(title, note, start_at, end_at, location, all_day,
               status, source, schedule_id, created_at) VALUES(?,?,?,?,?,?, 'confirmed', ?,?,?)""",
            (title, note or "", start_at or None, end_at or None, location or "",
             1 if all_day else 0, source, schedule_id, _now_iso()),
        )
        return {"id": cur.lastrowid, "title": title, "start_at": start_at}


def event_list(date: str = "", upcoming: int = 7) -> list[dict]:
    """Sự kiện: lọc theo ngày cụ thể, hoặc upcoming ngày tới (kể từ hôm nay)."""
    with db() as c:
        if date:
            rows = c.execute(
                "SELECT * FROM events WHERE substr(start_at,1,10)=? ORDER BY start_at",
                (date,),
            ).fetchall()
        else:
            start = datetime.now(TZ).strftime("%Y-%m-%d")
            rows = c.execute(
                """SELECT * FROM events
                   WHERE start_at IS NOT NULL AND substr(start_at,1,10) >= ?
                   ORDER BY start_at LIMIT ?""",
                (start, int(upcoming) * 20),
            ).fetchall()
        return [dict(r) for r in rows]


def event_update(event_id: int, **fields) -> dict:
    allowed = {"title", "note", "start_at", "end_at", "location", "all_day", "status"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed and v is not None:
            sets.append(f"{k}=?")
            vals.append(v if v != "" or k not in ("start_at", "end_at") else None)
    if not sets:
        return {"ok": False, "error": "không có trường hợp lệ"}
    vals.append(event_id)
    with db() as c:
        cur = c.execute(f"UPDATE events SET {', '.join(sets)} WHERE id=?", vals)
        if cur.rowcount == 0:
            return {"ok": False, "error": f"không có event id {event_id}"}
        return {"ok": True, "id": event_id}


# ── Goal CRUD (phân tầng year/month/week) ──────────────────────────────────
_LEVEL_ORDER = {"year": 0, "month": 1, "week": 2}


def goal_create(title: str, level: str, period: str = "", parent_id=None,
                description: str = "") -> dict:
    if level not in _LEVEL_ORDER:
        return {"ok": False, "error": f"level phải là year/month/week, được '{level}'"}
    with db() as c:
        if parent_id:
            prow = c.execute("SELECT level FROM goals WHERE id=?", (parent_id,)).fetchone()
            if not prow:
                return {"ok": False, "error": f"không có goal cha id {parent_id}"}
        cur = c.execute(
            """INSERT INTO goals(title, description, level, period, parent_id, status, progress_note, created_at)
               VALUES(?,?,?,?,?, 'active', '', ?)""",
            (title, description or "", level, period or "", parent_id, _now_iso()),
        )
        return {"id": cur.lastrowid, "title": title, "level": level}


def goal_list(level: str = "", period: str = "") -> list[dict]:
    q = "SELECT * FROM goals WHERE status='active'"
    params = []
    if level:
        q += " AND level=?"; params.append(level)
    if period:
        q += " AND period=?"; params.append(period)
    q += " ORDER BY CASE level WHEN 'year' THEN 0 WHEN 'month' THEN 1 ELSE 2 END, id"
    with db() as c:
        return [dict(r) for r in c.execute(q, params).fetchall()]


def goal_update(goal_id: int, **fields) -> dict:
    allowed = {"title", "description", "level", "period", "parent_id", "status", "progress_note"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed and v is not None:
            sets.append(f"{k}=?"); vals.append(v)
    if not sets:
        return {"ok": False, "error": "không có trường hợp lệ"}
    vals.append(goal_id)
    with db() as c:
        cur = c.execute(f"UPDATE goals SET {', '.join(sets)} WHERE id=?", vals)
        if cur.rowcount == 0:
            return {"ok": False, "error": f"không có goal id {goal_id}"}
        return {"ok": True, "id": goal_id}


# ── Journal + Long-term memory ──────────────────────────────────────────────
def journal_add(mood: str = "", content: str = "", tags: str = "") -> int:
    with db() as c:
        cur = c.execute(
            "INSERT INTO journals(mood, content, tags, created_at) VALUES(?,?,?,?)",
            (mood or "", content, tags or "", _now_iso()),
        )
        jid = cur.lastrowid
        # Lưu tạm vào long_term_memory (MVP: dùng raw journal để search, chưa extract).
        c.execute(
            "INSERT INTO long_term_memory(content, confidence, source, created_at, last_used_at) VALUES(?,0.4,'journal',?,?)",
            (content, _now_iso(), _now_iso()),
        )
        return jid


def journal_recent(limit: int = 7) -> list[dict]:
    with db() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM journals ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


def lmem_add(content: str, confidence: float = 0.5, source: str = "chat") -> int:
    with db() as c:
        cur = c.execute(
            "INSERT INTO long_term_memory(content, confidence, source, created_at, last_used_at) VALUES(?,?,?,?,?)",
            (content, confidence, source, _now_iso(), _now_iso()),
        )
        return cur.lastrowid


def lmem_search(query: str, limit: int = 10) -> list[dict]:
    """Tìm kiếm memory: LIKE qua long_term_memory + journals + conversations (MVP, không vector)."""
    like = f"%{query}%"
    out = []
    with db() as c:
        for r in c.execute(
            "SELECT id, content, 'memory' AS kind, created_at FROM long_term_memory WHERE content LIKE ? LIMIT ?",
            (like, limit)).fetchall():
            out.append(dict(r))
        if len(out) < limit:
            for r in c.execute(
                "SELECT id, content, 'journal' AS kind, created_at FROM journals WHERE content LIKE ? LIMIT ?",
                (like, limit - len(out))).fetchall():
                out.append(dict(r))
        if len(out) < limit:
            for r in c.execute(
                "SELECT id, content, 'chat' AS kind, created_at FROM conversations WHERE content LIKE ? LIMIT ?",
                (like, limit - len(out))).fetchall():
                out.append(dict(r))
    return out


# ── Schedule (daily plan, draft → approved) ────────────────────────────────
def schedule_create_draft(date: str, slots: list) -> dict:
    """Lưu bản nháp lịch ngày. slots = list of {time,title,type?}. Trả {id, slots}."""
    cleaned = []
    for s in slots or []:
        if isinstance(s, dict) and s.get("time") and s.get("title"):
            cleaned.append({"time": str(s["time"])[:5], "title": str(s["title"]),
                            "type": s.get("type", "task")})
    with db() as c:
        cur = c.execute(
            "INSERT INTO schedules(date, slots, status, created_at) VALUES(?,?,'draft',?)",
            (date, json.dumps(cleaned, ensure_ascii=False), _now_iso()),
        )
        return {"id": cur.lastrowid, "date": date, "slots": cleaned}


def schedule_get(schedule_id: int) -> dict:
    with db() as c:
        r = c.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
        if not r:
            return {}
        d = dict(r)
        try:
            d["slots"] = json.loads(d.get("slots") or "[]")
        except Exception:
            d["slots"] = []
        return d


def schedule_approve(schedule_id: int) -> dict:
    """Đánh dấu approved + tạo events + reminder cho từng slot. Trả {ok, events}."""
    s = schedule_get(schedule_id)
    if not s:
        return {"ok": False, "error": f"không có schedule id {schedule_id}"}
    created_events = []
    with db() as c:
        c.execute("UPDATE schedules SET status='approved', approved_at=? WHERE id=?",
                  (_now_iso(), schedule_id))
    for slot in s["slots"]:
        try:
            dt = datetime.strptime(f"{s['date']} {slot['time']}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
            e = event_create(title=slot["title"], start_at=f"{s['date']} {slot['time']}",
                             source="schedule", schedule_id=schedule_id)
            created_events.append(e["id"])
            # Nhắc 15 phút trước mỗi slot (nếu tương lai).
            remind_at = dt - timedelta(minutes=15)
            if remind_at > datetime.now(TZ):
                schedule_reminder_at(remind_at, f"🔔 Sắp tới: {slot['title']} ({slot['time']})",
                                     source="schedule")
        except Exception as ex:
            print(f"[schedule] slot skip: {slot} -> {ex}")
    return {"ok": True, "id": schedule_id, "events": created_events}


# ── Reports (output của proactive jobs, unread until opened) ───────────────
def report_insert(rtype: str, period: str, content: str, schedule_id=None) -> int:
    with db() as c:
        cur = c.execute(
            "INSERT INTO reports(type, period, content, schedule_id, read, pushed, created_at) VALUES(?,?,?,?,0,0,?)",
            (rtype, period, content, schedule_id, _now_iso()),
        )
        return cur.lastrowid


def report_mark_pushed(rid: int):
    with db() as c:
        c.execute("UPDATE reports SET pushed=1 WHERE id=?", (rid,))


def report_unread() -> list[dict]:
    with db() as c:
        rows = c.execute("SELECT * FROM reports WHERE read=0 ORDER BY id DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if d.get("schedule_id"):
                s = schedule_get(d["schedule_id"])
                if s and s.get("status") == "draft":
                    d["schedule_slots"] = s["slots"]
            out.append(d)
        return out


def report_mark_read(rid: int):
    with db() as c:
        c.execute("UPDATE reports SET read=1 WHERE id=?", (rid,))


# ── System prompt (template, được bơm thông tin + thời gian thực mỗi request) ─
SYSTEM_PROMPT_TEMPLATE = """Bạn là trợ lý AI cá nhân thân thiết của tôi — một "Life OS" giúp quản lý công việc, mục tiêu, lịch trình, nhật ký và trí nhớ.
Vai trò: như một trợ lý giám đốc thực sự — quản lý task, event, goal, lên lịch, tóm kết, nhắc nhở, tâm sự.
Tính cách: thông minh, gần gũi, thi thoảng hài hước nhẹ nhàng.
Ngôn ngữ: tiếng Việt, tự nhiên.

# Thông tin về tôi
- Tên: {ten}
- Nghề nghiệp: {nghe}
- Sở thích: {so_thich}
- Khu vực: {khu_vuc}
- Ghi chú: {ghi_chu}
Những trường "(chưa rõ)" là vì tôi chưa điền — đừng đoán, cứ hỏi lại nếu cần.
Khi tôi chia sẻ thông tin cá nhân mới (đổi nghề, sở thích mới, khung giờ...), hãy gọi update_profile để ghi nhớ.

# Nhận thức thời gian / thế giới
- Hiện tại: {now} ({weekday}), {date_full}
- Múi giờ: Asia/Shanghai (UTC+8)
- Mùa: {season} (Bắc bán cầu)
Căn cứ vào thời gian thực để tính "sáng/chiều/tối", "hôm nay/ngày mai/tuần sau",
thứ trong tuần, cuối tuần khi đặt nhắc/event/lịch. Khi nhắc thời gian cho user, dùng múi giờ Asia/Shanghai.

# Vòng lặp Life OS: Mục tiêu → Kế hoạch → Thực hiện → Đánh giá → Điều chỉnh
- Goal phân tầng: year → month → week, liên kết qua parent_id. Mỗi task/schedule nên phục vụ một goal tuần/tháng.
- Khi tôi nói về mục tiêu dài hạn, gọi create_goal với level phù hợp.
- Khi tôi chia sẻ tâm trạng/suy nghĩ, gọi add_journal (mood, tags) để lưu nhật ký.
- Trước mỗi lượt lập kế hoạch / tổng kết / tư vấn, hãy gọi get_life_state để nắm bối cảnh.
- Để nhớ lại thông tin cũ, gọi search_memory(query).

# Đặt nhắc / sự kiện
- Nhắc việc ngắn hạn hoặc cuộc hẹn: dùng create_event (có cả title + start_at) — nó sẽ tự đặt nhắc push.
- Vẫn có thể dùng set_reminder cho nhắc thuần. Nếu proxy strip tools, dùng thẻ <reminder>{{"time","date","message"}}</reminder> làm fallback.

# Lên lịch ngày (Morning Planning) — PHẢI CÓ PHÊ DUYỆT
Khi tôi yêu cầu lên lịch/lịch ngày: sinh các slot (vd {{"time":"08:00","title":"...","type":"work"}}),
rồi gọi generate_schedule(date, slots) để lưu bản nháp. Tuyệt đối KHÔNG tự apply_schedule —
phải đợi tôi bấm "Duyệt". Sau khi gọi generate_schedule, trình bày lịch trong tin nhắn và kèm
marker <<<SCHEDULE id="<id>">>> (id là giá trị tool trả về). Nếu tôi muốn sửa, sinh lại rồi gọi generate_schedule lần nữa.

# Tổng kết
Khi tôi nhờ tổng kết: gọi daily_review / weekly_review / monthly_review để lấy dữ liệu, rồi tự viết tóm tắt
(hoàn thành, chưa hoàn thành, rủi ro, đề xuất). Có thể update goal progress_note hoặc life_os_state nếu thấy cần.

Phần trả lời cho user ngắn gọn, thân thiện."""

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
    """Bơm thông tin cá nhân (từ DB) + thời gian thực (Asia/Shanghai) vào system prompt."""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
    except Exception:
        now = datetime.now()
    p = profile_get()
    return SYSTEM_PROMPT_TEMPLATE.format(
        ten=p.get("ten") or "(chưa rõ)",
        nghe=p.get("nghe") or "(chưa rõ)",
        so_thich=p.get("so_thich") or "(chưa rõ)",
        khu_vuc=p.get("khu_vuc") or "(chưa rõ)",
        ghi_chu=p.get("ghi_chu") or "(không)",
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
    {
        "type": "function",
        "function": {
            "name": "create_event",
            "description": "Tạo sự kiện/cuộc hẹn có giờ cụ thể. Tự động đặt nhắc push trước sự kiện. Dùng cho lịch hẹn, khám, họp.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Tên sự kiện"},
                    "start_at": {"type": "string", "description": "Thời gian bắt đầu YYYY-MM-DD HH:MM"},
                    "end_at": {"type": "string", "description": "Thời gian kết thúc (tùy chọn)"},
                    "note": {"type": "string"},
                    "location": {"type": "string"},
                    "all_day": {"type": "boolean", "description": "Cả ngày, mặc định false"},
                },
                "required": ["title", "start_at"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_events",
            "description": "Liệt kê sự kiện: theo ngày cụ thể (date=YYYY-MM-DD) hoặc upcoming ngày tới.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Ngày YYYY-MM-DD (tùy chọn)"},
                    "upcoming": {"type": "integer", "description": "Số ngày tới kể từ hôm nay, mặc định 7"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_goal",
            "description": "Tạo mục tiêu phân tầng: year (năm), month (tháng), week (tuần). Liên kết goal con với goal cha qua parent_id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "level": {"type": "string", "enum": ["year", "month", "week"]},
                    "period": {"type": "string", "description": "VD: 2026 / 2026-06 / 2026-W26"},
                    "parent_id": {"type": "integer", "description": "id goal cha (tùy chọn)"},
                    "description": {"type": "string"},
                },
                "required": ["title", "level"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_goals",
            "description": "Liệt kê mục tiêu đang active, lọc theo level/period (tùy chọn).",
            "parameters": {
                "type": "object",
                "properties": {
                    "level": {"type": "string", "enum": ["year", "month", "week"]},
                    "period": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_goal",
            "description": "Cập nhật goal: status (active/done/paused), progress_note, period, v.v.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "status": {"type": "string", "enum": ["active", "done", "paused"]},
                    "progress_note": {"type": "string"},
                    "title": {"type": "string"},
                    "period": {"type": "string"},
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_journal",
            "description": "Ghi nhật ký cá nhân: tâm trạng, suy nghĩ, ghi chú. Dùng khi user tâm sự hoặc chia sẻ cảm xúc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "mood": {"type": "string", "description": "VD: stress, vui, mệt mỏi"},
                    "content": {"type": "string", "description": "Nội dung nhật ký"},
                    "tags": {"type": "string", "description": "Tag phân tách dấu phẩy, VD: work,stress"},
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_memory",
            "description": "Tìm trong trí nhớ dài hạn + nhật ký + lịch sử chat theo từ khoá. Dùng khi cần nhớ lại điều đã nói.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_life_state",
            "description": "Đọc trạng thái Life OS hiện tại (current_life_phase, top priorities, risks) + goals active. Gọi đầu mỗi lượt lập kế hoạch/tổng kết/tư vấn.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_profile",
            "description": "Cập nhật thông tin cá nhân của user (ten, nghe, so_thich, khu_vuc, ghi_chu) khi user chia sẻ thông tin mới.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ten": {"type": "string"},
                    "nghe": {"type": "string"},
                    "so_thich": {"type": "string"},
                    "khu_vuc": {"type": "string"},
                    "ghi_chu": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_schedule",
            "description": "Lưu bản NHÁP lịch ngày (slots) để user duyệt. KHÔNG tự apply — tool này chỉ lưu draft và trả schedule_id. Sau khi gọi, kèm marker <<<SCHEDULE id=\"<id>\">>> vào tin nhắn trình cho user duyệt.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Ngày YYYY-MM-DD"},
                    "slots": {
                        "type": "array",
                        "description": "Danh sách slot trong ngày",
                        "items": {
                            "type": "object",
                            "properties": {
                                "time": {"type": "string", "description": "HH:MM"},
                                "title": {"type": "string", "description": "Nội dung việc"},
                                "type": {"type": "string", "description": "work|study|fitness|personal|break"},
                            },
                            "required": ["time", "title"],
                        },
                    },
                    "focus": {"type": "string", "description": "Trọng tâm ngày (tùy chọn)"},
                },
                "required": ["date", "slots"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "daily_review",
            "description": "Lấy ngữ cảnh tổng kết ngày (task đã xong/chưa, sự kiện, journal hôm nay, life state). Trả data để bạn tự viết tóm tắt ngày cho user.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "weekly_review",
            "description": "Lấy ngữ cảnh tổng kết tuần (task tuần, goal tuần/tháng, journal). Trả data để bạn viết báo cáo tuần.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "monthly_review",
            "description": "Lấy ngữ cảnh tổng kết tháng (goal tháng, task tháng, journal). Trả data để bạn viết báo cáo tháng.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

MAX_TOOL_ROUNDS = 8


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


def _tool_create_event(title, start_at, end_at="", note="", location="", all_day=False) -> str:
    e = event_create(title=title, start_at=start_at, end_at=end_at, note=note,
                     location=location, all_day=all_day, source="manual")
    # Tự đặt nhắc push lúc start_at nếu là thời điểm tương lai.
    try:
        dt = datetime.strptime(start_at, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
        if dt > datetime.now(TZ):
            msg = f"📅 Sự kiện: {title}" + (f" @ {location}" if location else "")
            schedule_reminder_at(dt, msg)
    except Exception:
        pass  # vẫn giữ event dù đặt nhắc lỗi
    return f"Đã tạo sự kiện id={e['id']}: {title} lúc {start_at}"


def _tool_list_events(date="", upcoming=7) -> str:
    rows = event_list(date=date, upcoming=upcoming)
    if not rows:
        return f"Không có sự kiện nào (date={date or 'all'}, upcoming={upcoming})."
    lines = []
    for r in rows:
        loc = f" @ {r['location']}" if r["location"] else ""
        lines.append(f"#{r['id']} {r['start_at']}{loc} — {r['title']}")
    return "\n".join(lines)


def _tool_create_goal(title, level, period="", parent_id=None, description="") -> str:
    r = goal_create(title=title, level=level, period=period, parent_id=parent_id, description=description)
    if r.get("ok") is False:
        return f"Lỗi: {r.get('error')}"
    return f"Đã tạo goal id={r['id']} [{level}] {r['title']}"


def _tool_list_goals(level="", period="") -> str:
    rows = goal_list(level=level, period=period)
    if not rows:
        return "Không có goal active nào."
    lines = []
    for r in rows:
        pid = f" (cha={r['parent_id']})" if r["parent_id"] else ""
        per = f" [{r['period']}]" if r["period"] else ""
        lines.append(f"#{r['id']} [{r['level']}]{per} {r['title']}{pid}")
    return "\n".join(lines)


def _tool_update_goal(id, **kwargs) -> str:
    r = goal_update(int(id), **kwargs)
    return f"OK: đã cập nhật goal {id}" if r.get("ok") else f"Lỗi: {r.get('error')}"


def _tool_add_journal(content, mood="", tags="") -> str:
    jid = journal_add(mood=mood, content=content, tags=tags)
    return f"Đã ghi nhật ký id={jid}" + (f" (mood={mood})" if mood else "")


def _tool_search_memory(query) -> str:
    rows = lmem_search(query)
    if not rows:
        return f"Không tìm thấy gì cho '{query}'."
    lines = [f"[{r['kind']}] {r['content'][:200]}" for r in rows]
    return "\n".join(lines)


def _tool_get_life_state() -> str:
    s = life_state_get()
    goals = goal_list()
    import json as _json
    pri = s.get("top_3_priorities") or "[]"
    risks = s.get("current_risks") or "[]"
    g_lines = [f"- [{r['level']}] {r['title']}" for r in goals]
    return (
        f"Life phase: {s.get('current_life_phase') or '(chưa rõ)'}\n"
        f"Top priorities: {pri}\n"
        f"Risks: {risks}\n"
        f"Active goals:\n" + ("\n".join(g_lines) if g_lines else "- (không có)")
    )


def _tool_update_profile(**kwargs) -> str:
    r = profile_update(**kwargs)
    return f"OK: đã cập nhật profile {r.get('updated')}" if r.get("ok") else f"Lỗi: {r.get('error')}"


def _tool_generate_schedule(date, slots, focus="") -> str:
    """Lưu bản nháp lịch, trả schedule_id để model bọc vào marker <<<SCHEDULE id=...>>>."""
    r = schedule_create_draft(date, slots)
    return f"Đã lưu lịch nháp id={r['id']} cho ngày {date} ({len(r['slots'])} slot). Hãy kèm marker <<<SCHEDULE id=\"{r['id']}\">>> và trình cho user duyệt, KHÔNG tự apply."


def _build_context_bundle() -> str:
    """Gom context từ DB để review/planning dùng (share cho on-demand + proactive)."""
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    tasks = task_list("open")
    events = event_list(upcoming=7)
    goals = goal_list()
    journals = journal_recent(7)
    state = life_state_get()
    conv = conv_recent(10)

    def _fmt(rows, fmt):
        return "\n".join(fmt(r) for r in rows) or "(không có)"

    def _task(r):
        due = " (hạn %s)" % r["due_at"] if r["due_at"] else ""
        return "#%s [%s] %s%s" % (r["id"], r["priority"], r["title"], due)

    def _event(r):
        return "%s %s" % (r["start_at"], r["title"])

    def _goal(r):
        return "[%s][%s] %s" % (r["level"], r["period"], r["title"])

    def _journal(r):
        return "%s (%s): %s" % (r["created_at"][:10], r["mood"], (r["content"] or "")[:120])

    def _conv(r):
        return "%s: %s" % (r["role"], (r["content"] or "")[:120])

    return (
        "## Hôm nay: %s (%s)\n" % (today, datetime.now(TZ).strftime("%A"))
        + "## Life OS state\nphase=%s | priorities=%s | risks=%s\n" % (
            state.get("current_life_phase", ""), state.get("top_3_priorities", ""),
            state.get("current_risks", ""))
        + "## Task đang mở\n" + _fmt(tasks, _task) + "\n"
        + "## Sự kiện 7 ngày tới\n" + _fmt(events, _event) + "\n"
        + "## Goal active\n" + _fmt(goals, _goal) + "\n"
        + "## Nhật ký 7 ngày gần nhất\n" + _fmt(journals, _journal) + "\n"
        + "## Chat gần đây\n" + _fmt(conv, _conv)
    )


def _tool_daily_review() -> str:
    return _build_context_bundle()


def _tool_weekly_review() -> str:
    return _build_context_bundle()


def _tool_monthly_review() -> str:
    return _build_context_bundle()


TOOL_FUNCTIONS = {
    "get_current_time": _tool_get_current_time,
    "create_task": _tool_create_task,
    "list_tasks": _tool_list_tasks,
    "complete_task": _tool_complete_task,
    "update_task": _tool_update_task,
    "delete_task": _tool_delete_task,
    "set_reminder": _tool_set_reminder,
    "create_event": _tool_create_event,
    "list_events": _tool_list_events,
    "create_goal": _tool_create_goal,
    "list_goals": _tool_list_goals,
    "update_goal": _tool_update_goal,
    "add_journal": _tool_add_journal,
    "search_memory": _tool_search_memory,
    "get_life_state": _tool_get_life_state,
    "update_profile": _tool_update_profile,
    "generate_schedule": _tool_generate_schedule,
    "daily_review": _tool_daily_review,
    "weekly_review": _tool_weekly_review,
    "monthly_review": _tool_monthly_review,
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
        # Nếu fn có **kwargs → truyền hết args (các tool update_* nhận trường động).
        # Nếu không → chỉ truyền tham số được khai báo (loại bỏ thừa từ model).
        import inspect
        params = inspect.signature(fn).parameters
        has_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        if has_varkw:
            clean = dict(args)
        else:
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

# ── Route: Keep-alive ping ─────────────────────────────────────────────────
@app.get("/ping")
async def ping():
    return {"status": "ok"}

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

    # Lưu last-used config để scheduled job (morning planning/review) gọi được LLM.
    config_set(base_url=base_url, model=model, api_key=api_key)

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
        trace = []  # hiển thị tool calls/thinking cho frontend (giống Claude/Codex)
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

            # Nếu model viết lời dẫn/thinking kèm tool_calls → ghi là "thought".
            if msg.content and msg.tool_calls:
                clean_c, ths = _split_thinking(msg.content)
                for t in ths + ([clean_c] if clean_c else []):
                    trace.append({"kind": "thought", "text": t[:400]})

            if not msg.tool_calls:
                reply, ths = _split_thinking(msg.content or "")
                for t in ths:
                    trace.append({"kind": "thought", "text": t[:400]})
                break

            # Chạy từng tool call, đút kết quả về role "tool" + ghi trace.
            for tc in msg.tool_calls:
                result = _run_tool(tc.function.name, tc.function.arguments)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    }
                )
                trace.append({
                    "kind": "tool",
                    "name": tc.function.name,
                    "args": tc.function.arguments or "",
                    "result": (result or "")[:800],
                })
        else:
            # Vượt quá số vòng cho phép — lấy nội dung assistant cuối nếu có.
            last = next(
                (m for m in reversed(messages) if m.get("role") == "assistant" and m.get("content")),
                None,
            )
            reply = (last["content"] if last else "").strip() or "⚠️ Quá nhiều bước, thử lại nhé."
            reply, ths = _split_thinking(reply)
            for t in ths:
                trace.append({"kind": "thought", "text": t[:400]})

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

        # Schedule approval: model bọc <<<SCHEDULE id="..">>> trong reply. Parse, tra DB,
        # trả về frontend để render nút Duyệt/Sửa. Strip marker khỏi text hiển thị.
        schedule_payload = None
        m_sched = re.search(r'<<<SCHEDULE\s+id="(\d+)"\s*>>>', reply)
        if m_sched:
            sid = int(m_sched.group(1))
            s = schedule_get(sid)
            if s:
                schedule_payload = {"id": sid, "date": s["date"], "slots": s["slots"]}
        reply = re.sub(r'<<<SCHEDULE\s+id="\d+"\s*>>>', '', reply).strip()

        conv_add("assistant", reply)
        out = {"reply": reply}
        if schedule_payload:
            out["schedule"] = schedule_payload
        if trace:
            out["trace"] = trace
        return JSONResponse(out)

    except Exception as e:
        return JSONResponse({"reply": f"Lỗi: {str(e)}"}, status_code=500)



def _split_thinking(text: str):
    """Tách block thinking/reasoning bị proxy leak ra khỏi content.
    Trả (clean_text, [thoughts]). Hỗ trợ: block cân bằng, open không close
    (leak đến cuối), close không open (proxy strip open)."""
    import re
    thoughts = []
    if not text:
        return text, []
    names = "think|thinking|reasoning|thought|analysis|reason"
    # 1) block cân bằng: <tag>...</tag>  (\1 backref khớp tên tag)
    pat_bal = re.compile("<(" + names + r")(\s[^>]*)?>(.*?)</\1\s*>", re.I | re.S)
    def _bal(m):
        inner = m.group(3).strip()
        if inner:
            thoughts.append(inner)
        return ""
    text = pat_bal.sub(_bal, text)
    # 1b) <|thinking|>...</|/thinking|>
    pat_pipe = re.compile(r"<\|thinking\|>(.*?)<\|/thinking\|>", re.S)
    text = pat_pipe.sub(lambda m: (thoughts.append(m.group(1).strip()), "")[1], text)
    # 2) open không có close -> leak đến cuối: phần còn lại là thinking
    pat_open = re.compile("<(" + names + r")(\s[^>]*)?>(.*)$", re.I | re.S)
    m = pat_open.search(text)
    if m and m.group(3).strip():
        thoughts.append(m.group(3).strip())
        text = text[:m.start()]
    # 3) close không có open -> proxy strip open: phần trước close là thinking
    pat_close = re.compile("</(" + names + r")\s*>", re.I)
    m = pat_close.search(text)
    if m:
        before = text[:m.start()].strip()
        if before:
            thoughts.append(before)
        text = text[m.end():]
    return text.strip(), thoughts



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
    delay = data.get("delay", 5)
    try:
        delay = int(delay)
        if delay < 1 or delay > 3600:
            delay = 5
    except (TypeError, ValueError):
        delay = 5

    if not _VAPID_INSTANCE:
        return JSONResponse({"error": "VAPID private key chưa nạp được (kiểm tra PEM trong .env)"}, status_code=500)
    if not push_subscriptions:
        return JSONResponse({"error": "Chưa có thiết bị nào đăng ký. Hãy bấm 🔔 Thông báo trên app trước."}, status_code=400)

    run_at = datetime.now(TZ) + timedelta(seconds=delay)
    scheduler.add_job(
        lambda: send_push("🧪 Kiểm tra", f"Thông báo thử (sau {delay}s)"),
        'date', run_date=run_at,
    )
    return JSONResponse({"status": "scheduled", "in_seconds": delay})


# ── Route: Phê duyệt lịch ngày (Morning Planning) ──────────────────────────
@app.post("/approve-schedule")
async def approve_schedule(request: Request):
    """User bấm Duyệt → apply_schedule (tạo events + reminder). Có edits → regenerate."""
    data = await request.json()
    sid = data.get("schedule_id")
    edits = (data.get("edits") or "").strip()
    try:
        sid = int(sid)
    except (TypeError, ValueError):
        return JSONResponse({"error": "schedule_id không hợp lệ"}, status_code=400)

    if edits:
        # User muốn sửa → không tự apply; trả gợi ý để model regenerate qua chat.
        return JSONResponse({"ok": False, "needs_regen": True,
                             "hint": f"Sửa lịch: {edits}. Hãy gửi lại để tôi lên lịch mới."})

    r = schedule_approve(sid)
    if r.get("ok"):
        return JSONResponse({"ok": True, "id": sid, "events": r.get("events", [])})
    return JSONResponse({"error": r.get("error", "?")}, status_code=400)


# ── Reminder scheduler ────────────────────────────────────────────────────
from zoneinfo import ZoneInfo
TZ = ZoneInfo("Asia/Shanghai")  # múi giờ Trung Quốc (UTC+8)
scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")


def _fire_reminder(reminder_id: int, message: str):
    """Callback APScheduler: gửi push rồi đánh dấu fired trong DB."""
    send_push("Nhắc nhở", message)
    try:
        reminder_mark_fired(reminder_id)
    except Exception as e:
        print(f"[reminder] mark_fired error id={reminder_id}: {e}")


def schedule_reminder_at(dt: datetime, message: str, source: str = "manual") -> dict:
    """Lên lịch 1 nhắc tại dt, persist vào DB. Trả {ok, run_at, message, id, error}."""
    dt_str = dt.strftime("%Y-%m-%d %H:%M")
    now = datetime.now(TZ)
    if dt <= now:
        print(f"[reminder] bỏ qua (đã qua giờ): {dt_str} | now={now.strftime('%Y-%m-%d %H:%M')}")
        return {"ok": False, "error": f"giờ đã qua ({dt_str})", "run_at": dt_str, "message": message}
    rid = reminder_insert(dt_str, message, source=source)
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


# ── Proactive jobs (morning planning + daily/weekly/monthly review) ─────────
def _server_client():
    """Lấy OpenAI client + model từ app_config (last-used) hoặc env, cho scheduled job."""
    cfg = config_get()
    key = (cfg.get("api_key_hint") or OPENAI_API_KEY).strip()
    url = (cfg.get("base_url") or OPENAI_BASE_URL).strip()
    mdl = (cfg.get("model") or "krr/claude-haiku-4-5-20251001").strip()
    if not key:
        return None, None
    return OpenAI(api_key=key, base_url=url), mdl


def _period_str(job_type: str) -> str:
    now = datetime.now(TZ)
    if job_type == "daily_review":
        return now.strftime("%Y-%m-%d")
    if job_type == "monthly_review":
        return now.strftime("%Y-%m")
    if job_type == "weekly_review":
        monday = now - timedelta(days=now.weekday())
        return f"{monday.strftime('%Y-%m-%d')} tuần"
    if job_type == "morning_planning":
        return (now + timedelta(days=1)).strftime("%Y-%m-%d")
    return now.strftime("%Y-%m-%d")


_DIRECTIVES = {
    "morning_planning": (
        "Bạn đang tự lên lịch cho ngày mai. Dựa vào context, sinh các slot công việc hợp lý "
        "(sáng làm việc chính, có giờ nghỉ/tập thể dục). Gọi generate_schedule(date=<ngày mai>, slots=[...]) "
        "để lưu bản nháp. KHÔNG tự apply_schedule. Sau đó viết lời ngắn giới thiệu lịch và kèm marker "
        "<<<SCHEDULE id=\"<id>\">>> (id từ kết quả tool)."
    ),
    "daily_review": (
        "Bạn đang tổng kết cuối ngày. Dựa vào context, viết tóm tắt: hôm nay hoàn thành gì, việc nào "
        "trì hoãn, tâm trạng (nếu có journal). Có thể gọi update_goal/update_profile nếu cần. "
        "Trả về markdown tiếng Việt."
    ),
    "weekly_review": (
        "Bạn đang tổng kết cuối tuần. Viết báo cáo: hoàn thành / chưa hoàn thành / rủi ro deadline / "
        "đề xuất tuần sau. Có thể update life_os_state qua update tool (chưa có tool đó thì ghi trong text). "
        "Trả markdown tiếng Việt."
    ),
    "monthly_review": (
        "Bạn đang tổng kết cuối tháng. Viết báo cáo: mục tiêu tháng, tỷ lệ hoàn thành, vấn đề lớn, "
        "kế hoạch tháng sau. Trả markdown tiếng Việt."
    ),
}


def _run_proactive_job(job_type: str, dry: bool = False) -> dict:
    """Chạy 1 proactive job: gọi LLM → lưu report/schedule → push nudge (trừ dry)."""
    import re
    result = {"job": job_type, "period": _period_str(job_type), "ok": False,
              "content": "", "schedule_id": None, "error": None}
    client, mdl = _server_client()
    if not client:
        result["error"] = "chưa có API key (gửi 1 tin nhắn để lưu config, hoặc set OPENAI_API_KEY)"
        print(f"[proactive] {job_type} skip: {result['error']}")
        return result

    tomorrow = (datetime.now(TZ) + timedelta(days=1)).strftime("%Y-%m-%d")
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    bundle = _build_context_bundle()
    sys_prompt = build_system_prompt() + "\n\n# Yêu cầu lượt này\n" + _DIRECTIVES.get(job_type, "")

    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": f"Context:\n{bundle}\n\nHãy thực hiện yêu cầu. "
                                    + (f"Ngày cần lên lịch: {tomorrow}." if job_type == "morning_planning" else f"Hôm nay: {today}.")},
    ]

    reply = ""
    schedule_id = None
    try:
        for _ in range(4):
            resp = client.chat.completions.create(
                model=mdl, messages=messages, tools=TOOL_SPECS,
                tool_choice="auto", max_tokens=1400,
            )
            msg = resp.choices[0].message
            messages.append(msg.model_dump(exclude_none=True))
            if not msg.tool_calls:
                reply = (msg.content or "").strip()
                break
            for tc in msg.tool_calls:
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": _run_tool(tc.function.name, tc.function.arguments)})
        else:
            last = next((m for m in reversed(messages) if m.get("role") == "assistant" and m.get("content")), None)
            reply = (last["content"] if last else "").strip() or "(không có nội dung)"

        # Bỏ thinking tag bị leak, rồi mới xử lý marker + lưu report.
        reply, _ = _split_thinking(reply)

        # Bắt schedule_id từ marker nếu model đã gọi generate_schedule.
        m_sched = re.search(r'<<<SCHEDULE\s+id="(\d+)"\s*>>>', reply)
        if m_sched:
            schedule_id = int(m_sched.group(1))
        reply_clean = re.sub(r'<<<SCHEDULE\s+id="\d+"\s*>>>', '', reply).strip()

        rid = report_insert(job_type, result["period"], reply_clean, schedule_id=schedule_id)
        if not dry:
            report_mark_pushed(rid)
            push_title = {"morning_planning": "📋 Lịch ngày mai sẵn sàng",
                          "daily_review": "📝 Tổng kết ngày",
                          "weekly_review": "📅 Tổng kết tuần",
                          "monthly_review": "🗓️ Tổng kết tháng"}.get(job_type, "Trợ lý AI")
            send_push(push_title, "Mở app để xem chi tiết")

        result.update({"ok": True, "content": reply_clean, "schedule_id": schedule_id})
        print(f"[proactive] {job_type} done (report id={rid}, schedule={schedule_id})")
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        print(f"[proactive] {job_type} error: {result['error']}")
    return result


def _register_cron_jobs():
    """Đăng ký 4 cron job proactive (RAM, mất khi restart → gọi lại trong startup + recovery)."""
    scheduler.add_job(lambda: _run_proactive_job('morning_planning'), 'cron',
                      hour=7, minute=30, id='morning_planning', replace_existing=True)
    scheduler.add_job(lambda: _run_proactive_job('daily_review'), 'cron',
                      hour=22, minute=0, id='daily_review', replace_existing=True)
    scheduler.add_job(lambda: _run_proactive_job('weekly_review'), 'cron',
                      day_of_week='sun', hour=21, minute=0, id='weekly_review', replace_existing=True)
    scheduler.add_job(lambda: _run_proactive_job('monthly_review'), 'cron',
                      day='last', hour=21, minute=0, id='monthly_review', replace_existing=True)


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


# ── Routes: Pending reports + debug proactive/state ───────────────────────
@app.get("/pending")
async def pending():
    """Báo cáo/tổng kết chưa đọc (proactive jobs sinh ra). Frontend fetch khi mở app."""
    return JSONResponse({"items": report_unread()})


@app.post("/pending/read")
async def pending_read(request: Request):
    data = await request.json()
    rid = data.get("id")
    try:
        report_mark_read(int(rid))
        return JSONResponse({"ok": True})
    except (TypeError, ValueError):
        return JSONResponse({"error": "id không hợp lệ"}, status_code=400)


@app.get("/debug-proactive")
async def debug_proactive(job: str = "daily_review", dry: bool = True):
    """Chạy ngay 1 proactive job. dry=1 (mặc định) skip push. Cách test không đợi giờ thật."""
    if job not in _DIRECTIVES:
        return JSONResponse({"error": f"job không hợp lệ: {job} (chọn {list(_DIRECTIVES)})"}, status_code=400)
    return JSONResponse(_run_proactive_job(job, dry=dry))


@app.get("/debug-state")
async def debug_state():
    """Tổng quan: life_os_state, profile, count các bảng (KHÔNG trả api_key)."""
    with db() as c:
        counts = {}
        for t in ["tasks", "reminders", "conversations", "events", "goals",
                  "journals", "long_term_memory", "reports", "schedules"]:
            try:
                counts[t] = c.execute(f"SELECT count(*) n FROM {t}").fetchone()["n"]
            except Exception:
                counts[t] = None
    prof = profile_get()
    prof.pop("api_key_hint", None)
    return JSONResponse({
        "now": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S %Z"),
        "life_os_state": life_state_get(),
        "profile": prof,
        "app_config": {k: v for k, v in config_get().items() if k != "api_key_hint"},
        "counts": counts,
        "pending_reports": len(report_unread()),
    })


# ── DB viewer (xem trực tiếp memory.db trên trình duyệt) ──────────────────
_SENSITIVE_COLS = {"api_key_hint"}


def _mask_row(table: str, row: dict) -> dict:
    r = dict(row)
    if table == "app_config" and "api_key_hint" in r:
        r["api_key_hint"] = "***" if r["api_key_hint"] else ""
    return r


def db_dump(limit: int = 200) -> dict:
    """Trả {table: [rows]} cho tất cả bảng (che api_key_hint)."""
    out = {}
    with db() as c:
        names = [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()]
        for t in names:
            try:
                rows = c.execute(f"SELECT * FROM {t} LIMIT ?", (int(limit),)).fetchall()
                out[t] = [_mask_row(t, dict(r)) for r in rows]
            except Exception as e:
                out[t] = [{"error": str(e)}]
    return out


@app.get("/debug-db")
async def debug_db(table: str = "", limit: int = 200):
    """JSON của 1 bảng cụ thể (che api_key_hint), hoặc tất cả bảng nếu không chỉ định."""
    if table:
        with db() as c:
            rows = c.execute(f"SELECT * FROM {table} LIMIT ?", (int(limit),)).fetchall()
            return JSONResponse({"table": table, "rows": [_mask_row(table, dict(r)) for r in rows]})
    return JSONResponse(db_dump(limit))


@app.get("/db")
async def db_viewer():
    """Trang HTML xem toàn bộ memory.db (cho user non-technical). Che api_key_hint."""
    dump = db_dump(200)
    import html as _html
    parts = [
        "<!DOCTYPE html><html lang='vi'><head><meta charset='UTF-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        "<title>memory.db</title><style>",
        "body{background:#0f0f0f;color:#e8e8e8;font-family:Inter,system-ui,sans-serif;padding:16px;}",
        "h1{font-size:18px;margin:0 0 4px} .sub{color:#666;font-size:12px;margin-bottom:16px}",
        "h2{font-size:14px;color:#4f8ef7;margin:20px 0 6px;border-bottom:1px solid #2a2a2a;padding-bottom:4px}",
        "table{border-collapse:collapse;width:100%;font-size:12px;margin-bottom:8px}",
        "th,td{border:1px solid #2a2a2a;padding:5px 7px;text-align:left;vertical-align:top}",
        "th{background:#1a1a1a;color:#999} td{word-break:break-word;white-space:pre-wrap}",
        "tr:nth-child(even){background:#161616} .empty{color:#555;font-style:italic}",
        "</style></head><body>",
        "<h1>🗄️ memory.db</h1>",
        "<div class='sub'>Bản sao (read-only) của database — che API key. "
        f"Cập nhật lúc {datetime.now(TZ).strftime('%Y-%m-%d %H:%M')}</div>",
    ]
    for t, rows in dump.items():
        parts.append(f"<h2>{t} ({len(rows)})</h2>")
        if not rows:
            parts.append("<div class='empty'>(trống)</div>")
            continue
        cols = list(rows[0].keys())
        parts.append("<table><tr>" + "".join(f"<th>{_html.escape(c)}</th>" for c in cols) + "</tr>")
        for r in rows:
            parts.append("<tr>" + "".join(
                f"<td>{_html.escape(str(r.get(c, '')))}</td>" for c in cols) + "</tr>")
        parts.append("</table>")
    parts.append("</body></html>")
    return HTMLResponse("".join(parts))


# ── Startup ───────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    db_init()
    scheduler.start()
    _reschedule_reminders()
    _register_cron_jobs()

@app.on_event("shutdown")
async def shutdown():
    scheduler.shutdown()
