"""
Backend chính: FastAPI + OpenAI-compatible AI + Push Notification + Task/Job system
Refactored theo thu_ky_kim_refactor.md:
  - Bảng tasks (có remind_at, repeat_rule)
  - Bảng memories (thay long_term_memory)
  - Bảng jobs (thay reports/schedules/morning_planning)
  - API tối giản: /chat /history /tasks /memory /pending
"""
import os, json, asyncio, sqlite3, base64, tempfile, re
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

load_dotenv()
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

app = FastAPI()

# ── Config ──────────────────────────────────────────────────────────────
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.vilao.ai/v1")
VAPID_PRIVATE   = os.getenv("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC    = os.getenv("VAPID_PUBLIC_KEY", "")
VAPID_EMAIL     = os.getenv("VAPID_EMAIL", "mailto:you@example.com")
GITHUB_BACKUP_TOKEN  = os.getenv("GITHUB_BACKUP_TOKEN", "")
GITHUB_BACKUP_OWNER  = os.getenv("GITHUB_BACKUP_OWNER", "")
GITHUB_BACKUP_REPO   = os.getenv("GITHUB_BACKUP_REPO", "")
GITHUB_BACKUP_BRANCH = os.getenv("GITHUB_BACKUP_BRANCH", "main")
GITHUB_BACKUP_PATH   = os.getenv("GITHUB_BACKUP_PATH", "memory.db")
try:
    BACKUP_INTERVAL_MINUTES = max(1, int(os.getenv("BACKUP_INTERVAL_MINUTES", "10")))
except ValueError:
    BACKUP_INTERVAL_MINUTES = 10

_VAPID_INSTANCE = None
if VAPID_PRIVATE:
    try:
        from py_vapid import Vapid
        _VAPID_INSTANCE = Vapid.from_pem(VAPID_PRIVATE.encode())
    except Exception as e:
        print(f"[vapid] Không nạp được PEM private key: {e}")

push_subscriptions: list[dict] = []
_backup_dirty = True
_backup_last_success = ""
_backup_last_error = ""
_backup_last_attempt = ""

# ── SQLite ──────────────────────────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory.db")

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _now_iso() -> str:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
    except Exception:
        return datetime.now().isoformat()

_DEFAULT_PROFILE = {
    "ten":      "Nguyễn Hoàng Dương hoặc yangd hoặc rhy",
    "nghe":     "Sinh viên đẹp trai",
    "so_thich": "đánh cầu, chụp ảnh",
    "khu_vuc":  "đang ở Thượng Hải, quê nhà Hà Nội",
    "ghi_chu":  "Chú ý: Trả lời ngắn gọn (dưới 120 chữ là tốt nhất, có thể dài hơn nếu thực sự cần thiết), rất rất hạn chế dùng icon, không dùng dấu <---> và hạn chế để dòng trắng, nói chuyện cute đáng yêu.",
}

def db_init():
    """Tạo tất cả bảng theo schema mới (refactored)."""
    with db() as c:
        c.executescript("""
            -- Bảng chính: tin nhắn hội thoại
            CREATE TABLE IF NOT EXISTS messages (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              role TEXT NOT NULL,
              content TEXT NOT NULL,
              metadata_json TEXT,
              created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            -- Bảng tasks: task + event + reminder gộp lại (remind_at thay cho bảng reminders riêng)
            CREATE TABLE IF NOT EXISTS tasks (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              notes TEXT,
              status TEXT DEFAULT 'pending',
              priority INTEGER DEFAULT 3,
              start_at DATETIME,
              due_at DATETIME,
              remind_at DATETIME,
              repeat_rule TEXT,
              completed_at DATETIME,
              created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            -- Bảng memories: thay long_term_memory + journals
            CREATE TABLE IF NOT EXISTS memories (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              content TEXT NOT NULL,
              type TEXT,
              importance INTEGER DEFAULT 1,
              last_used_at DATETIME,
              created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            -- Bảng jobs: proactive jobs (morning_planning, daily_review...) + pending reports
            CREATE TABLE IF NOT EXISTS jobs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              type TEXT NOT NULL,
              payload_json TEXT,
              status TEXT DEFAULT 'pending',
              run_at DATETIME,
              created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            -- Bảng profile + config (single-row)
            CREATE TABLE IF NOT EXISTS user_profile (
              id INTEGER PRIMARY KEY CHECK(id=1),
              ten TEXT DEFAULT '',
              nghe TEXT DEFAULT '',
              so_thich TEXT DEFAULT '',
              khu_vuc TEXT DEFAULT '',
              ghi_chu TEXT DEFAULT '',
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS life_os_state (
              id INTEGER PRIMARY KEY CHECK(id=1),
              current_life_phase TEXT DEFAULT '',
              top_3_priorities TEXT DEFAULT '',
              current_risks TEXT DEFAULT '',
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS app_config (
              id INTEGER PRIMARY KEY CHECK(id=1),
              base_url TEXT DEFAULT '',
              model TEXT DEFAULT '',
              api_key_hint TEXT DEFAULT '',
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS push_subscriptions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              endpoint TEXT NOT NULL UNIQUE,
              subscription_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            -- Goals (giữ nguyên cấu trúc phân tầng)
            CREATE TABLE IF NOT EXISTS goals (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              description TEXT DEFAULT '',
              level TEXT NOT NULL,
              period TEXT DEFAULT '',
              parent_id INTEGER,
              status TEXT DEFAULT 'active',
              progress_note TEXT DEFAULT '',
              created_at TEXT NOT NULL
            );
        """)

        # Migration: copy dữ liệu cũ nếu tồn tại
        try:
            old_conv = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='conversations'").fetchone()
            if old_conv:
                count = c.execute("SELECT COUNT(*) n FROM messages").fetchone()["n"]
                if count == 0:
                    c.execute("""INSERT INTO messages(role, content, created_at)
                                 SELECT role, content, created_at FROM conversations""")
                    print("[migration] copied conversations → messages")
        except Exception as e:
            print(f"[migration] conversations: {e}")

        try:
            old_ltm = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='long_term_memory'").fetchone()
            if old_ltm:
                count = c.execute("SELECT COUNT(*) n FROM memories").fetchone()["n"]
                if count == 0:
                    c.execute("""INSERT INTO memories(content, type, importance, created_at)
                                 SELECT content, source, CAST(confidence*5 AS INTEGER), created_at
                                 FROM long_term_memory""")
                    print("[migration] copied long_term_memory → memories")
        except Exception as e:
            print(f"[migration] long_term_memory: {e}")

        try:
            old_rem = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='reminders'").fetchone()
            if old_rem:
                unfired = c.execute("SELECT * FROM reminders WHERE fired=0").fetchall()
                for r in unfired:
                    c.execute("""INSERT OR IGNORE INTO tasks(title, notes, status, remind_at, created_at)
                                 VALUES(?, ?, 'pending', ?, ?)""",
                              (r["message"], "Migrated from reminders", r["run_at"], r["created_at"]))
                if unfired:
                    print(f"[migration] migrated {len(unfired)} reminders → tasks")
        except Exception as e:
            print(f"[migration] reminders: {e}")

        # Seed single-row tables
        c.execute("""INSERT OR IGNORE INTO user_profile(id,ten,nghe,so_thich,khu_vuc,ghi_chu,updated_at)
                     VALUES(1,?,?,?,?,?,?)""",
                  (_DEFAULT_PROFILE["ten"], _DEFAULT_PROFILE["nghe"], _DEFAULT_PROFILE["so_thich"],
                   _DEFAULT_PROFILE["khu_vuc"], _DEFAULT_PROFILE["ghi_chu"], _now_iso()))
        c.execute("""INSERT OR IGNORE INTO life_os_state(id,current_life_phase,top_3_priorities,current_risks,updated_at)
                     VALUES(1,'','','',?)""", (_now_iso(),))
        c.execute("""INSERT OR IGNORE INTO app_config(id,base_url,model,api_key_hint,updated_at)
                     VALUES(1,'','','',?)""", (_now_iso(),))


# ── GitHub backup ────────────────────────────────────────────────────────
def _mark_backup_dirty():
    global _backup_dirty
    _backup_dirty = True

def _github_backup_configured():
    return bool(GITHUB_BACKUP_TOKEN and GITHUB_BACKUP_OWNER and GITHUB_BACKUP_REPO and GITHUB_BACKUP_PATH)

def _github_headers():
    return {"Authorization": f"Bearer {GITHUB_BACKUP_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}

def _github_contents_url():
    path = GITHUB_BACKUP_PATH.strip("/")
    return f"https://api.github.com/repos/{GITHUB_BACKUP_OWNER}/{GITHUB_BACKUP_REPO}/contents/{path}"

def _download_backup_from_github() -> Optional[bytes]:
    if not _github_backup_configured():
        return None
    try:
        with httpx.Client(timeout=30.0) as client:
            r = client.get(_github_contents_url(), headers=_github_headers(),
                           params={"ref": GITHUB_BACKUP_BRANCH})
        if r.status_code == 404:
            print("[backup] chưa có file backup trên GitHub")
            return None
        r.raise_for_status()
        content = (r.json().get("content") or "").replace("\n", "")
        return base64.b64decode(content) if content else None
    except Exception as e:
        print(f"[backup] download error: {e}")
        return None

def _restore_db_from_github_if_needed():
    if os.path.exists(DB_PATH) and os.path.getsize(DB_PATH) > 0:
        return False
    blob = _download_backup_from_github()
    if not blob:
        return False
    with open(DB_PATH, "wb") as f:
        f.write(blob)
    print(f"[backup] restored memory.db từ GitHub ({len(blob)} bytes)")
    return True

def _snapshot_db_to_temp():
    fd, path = tempfile.mkstemp(prefix="memory-backup-", suffix=".db")
    os.close(fd)
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return path

def _upload_backup_to_github():
    tmp = _snapshot_db_to_temp()
    try:
        with open(tmp, "rb") as f:
            encoded = base64.b64encode(f.read()).decode()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    sha = None
    with httpx.Client(timeout=60.0) as client:
        current = client.get(_github_contents_url(), headers=_github_headers(),
                             params={"ref": GITHUB_BACKUP_BRANCH})
        if current.status_code == 200:
            sha = current.json().get("sha")
        elif current.status_code != 404:
            current.raise_for_status()
        payload = {"message": "Backup memory.db", "content": encoded, "branch": GITHUB_BACKUP_BRANCH}
        if sha:
            payload["sha"] = sha
        r = client.put(_github_contents_url(), headers=_github_headers(), json=payload)
        r.raise_for_status()
        data = r.json()
    return {"ok": True, "sha": (data.get("content") or {}).get("sha", "")}

def backup_memory_db(force: bool = False) -> dict:
    global _backup_dirty, _backup_last_success, _backup_last_error, _backup_last_attempt
    _backup_last_attempt = _now_iso()
    if not _github_backup_configured():
        _backup_last_error = "chưa cấu hình GitHub backup"
        return {"ok": False, "skipped": _backup_last_error}
    if not os.path.exists(DB_PATH) or os.path.getsize(DB_PATH) == 0:
        return {"ok": False, "skipped": "memory.db trống"}
    if not force and not _backup_dirty:
        return {"ok": True, "skipped": "không có thay đổi mới"}
    try:
        out = _upload_backup_to_github()
        _backup_dirty = False
        _backup_last_success = _now_iso()
        _backup_last_error = ""
        return out
    except Exception as e:
        _backup_last_error = f"{type(e).__name__}: {e}"
        return {"ok": False, "error": _backup_last_error}

def _register_backup_job():
    if not _github_backup_configured():
        return
    scheduler.add_job(lambda: backup_memory_db(force=False), "interval",
                      minutes=BACKUP_INTERVAL_MINUTES, id="github-memory-backup", replace_existing=True)


# ── Messages (chat history) ──────────────────────────────────────────────
def msg_add(role: str, content: str, metadata: dict = None):
    meta = json.dumps(metadata, ensure_ascii=False) if metadata else None
    with db() as c:
        c.execute("INSERT INTO messages(role, content, metadata_json, created_at) VALUES(?,?,?,?)",
                  (role, content, meta, _now_iso()))
    _mark_backup_dirty()

def msg_recent(n: int = 20) -> list[dict]:
    with db() as c:
        rows = c.execute(
            "SELECT role, content FROM messages ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

def msg_list(n: int = 100) -> list[dict]:
    with db() as c:
        rows = c.execute(
            "SELECT role, content, created_at, metadata_json FROM messages ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
        out = []
        for r in reversed(rows):
            d = {"role": r["role"], "content": r["content"], "created_at": r["created_at"], "trace": None}
            try:
                if r["metadata_json"]:
                    md = json.loads(r["metadata_json"])
                    d["trace"] = md.get("trace")
            except Exception:
                d["trace"] = None
            out.append(d)
        return out

def msg_clear():
    with db() as c:
        c.execute("DELETE FROM messages")
    _mark_backup_dirty()


# ── Tasks CRUD ───────────────────────────────────────────────────────────
def task_create(title: str, notes: str = "", due_at: str = "", remind_at: str = "",
                start_at: str = "", priority: int = 3, repeat_rule: str = "") -> dict:
    try:
        priority = max(1, min(4, int(priority)))
    except (TypeError, ValueError):
        priority = 3
    with db() as c:
        cur = c.execute(
            """INSERT INTO tasks(title, notes, status, priority, start_at, due_at, remind_at, repeat_rule)
               VALUES(?,?,'pending',?,?,?,?,?)""",
            (title, notes or "", priority, start_at or None, due_at or None,
             remind_at or None, repeat_rule or None),
        )
        _mark_backup_dirty()
        return {"id": cur.lastrowid, "title": title}

def task_list(status: str = "pending") -> list[dict]:
    with db() as c:
        if status == "all":
            rows = c.execute("SELECT * FROM tasks ORDER BY COALESCE(due_at,'9999-12-31'), id").fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM tasks WHERE status=? ORDER BY COALESCE(due_at,'9999-12-31'), id",
                (status,)).fetchall()
        return [dict(r) for r in rows]

def task_complete(task_id: int) -> dict:
    with db() as c:
        cur = c.execute("UPDATE tasks SET status='done', completed_at=? WHERE id=?",
                        (_now_iso(), task_id))
        if cur.rowcount == 0:
            return {"ok": False, "error": f"không có task id {task_id}"}
        _mark_backup_dirty()
        return {"ok": True, "id": task_id}

def task_update(task_id: int, **fields) -> dict:
    allowed = {"title", "notes", "status", "priority", "due_at", "remind_at", "start_at", "repeat_rule"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed and v is not None:
            sets.append(f"{k}=?")
            vals.append(v)
    if not sets:
        return {"ok": False, "error": "không có trường hợp lệ"}
    vals.append(task_id)
    with db() as c:
        cur = c.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", vals)
        if cur.rowcount == 0:
            return {"ok": False, "error": f"không có task id {task_id}"}
        _mark_backup_dirty()
        return {"ok": True, "id": task_id}

def task_delete(task_id: int) -> dict:
    with db() as c:
        cur = c.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        if cur.rowcount == 0:
            return {"ok": False, "error": f"không có task id {task_id}"}
        _mark_backup_dirty()
        return {"ok": True, "id": task_id}

def task_pending_reminders() -> list[dict]:
    """Lấy task có remind_at trong tương lai gần (chưa fired)."""
    now = _now_iso()
    with db() as c:
        rows = c.execute(
            "SELECT * FROM tasks WHERE remind_at IS NOT NULL AND remind_at > ? AND status='pending' ORDER BY remind_at",
            (now,)).fetchall()
        return [dict(r) for r in rows]


# ── Memories CRUD ─────────────────────────────────────────────────────────
def memory_save(content: str, type: str = "note", importance: int = 1) -> dict:
    with db() as c:
        cur = c.execute(
            "INSERT INTO memories(content, type, importance, last_used_at) VALUES(?,?,?,?)",
            (content, type or "note", importance, _now_iso()),
        )
        _mark_backup_dirty()
        return {"id": cur.lastrowid}

def memory_search(query: str, limit: int = 10) -> list[dict]:
    like = f"%{query}%"
    with db() as c:
        rows = c.execute(
            "SELECT * FROM memories WHERE content LIKE ? ORDER BY importance DESC, last_used_at DESC LIMIT ?",
            (like, limit)).fetchall()
        return [dict(r) for r in rows]

def memory_list(limit: int = 50) -> list[dict]:
    with db() as c:
        rows = c.execute(
            "SELECT * FROM memories ORDER BY importance DESC, created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

def memory_update(memory_id: int, **fields) -> dict:
    allowed = {"content", "type", "importance"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed and v is not None:
            sets.append(f"{k}=?"); vals.append(v)
    if not sets:
        return {"ok": False, "error": "không có trường hợp lệ"}
    vals.append(memory_id)
    with db() as c:
        cur = c.execute(f"UPDATE memories SET {', '.join(sets)} WHERE id=?", vals)
        if cur.rowcount == 0:
            return {"ok": False, "error": f"không có memory id {memory_id}"}
        _mark_backup_dirty()
        return {"ok": True, "id": memory_id}

def memory_delete(memory_id: int) -> dict:
    with db() as c:
        cur = c.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        if cur.rowcount == 0:
            return {"ok": False, "error": f"không có memory id {memory_id}"}
        _mark_backup_dirty()
        return {"ok": True, "id": memory_id}


# ── Jobs CRUD (proactive: morning_planning, daily_review…) ───────────────
def job_create(type: str, payload: dict = None, run_at: str = None) -> dict:
    with db() as c:
        cur = c.execute(
            "INSERT INTO jobs(type, payload_json, status, run_at) VALUES(?,?,'pending',?)",
            (type, json.dumps(payload or {}, ensure_ascii=False), run_at),
        )
        _mark_backup_dirty()
        return {"id": cur.lastrowid, "type": type}

def job_list_pending() -> list[dict]:
    with db() as c:
        rows = c.execute(
            "SELECT * FROM jobs WHERE status='pending' ORDER BY id DESC"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d.get("payload_json") or "{}")
            except Exception:
                d["payload"] = {}
            out.append(d)
        return out

def job_mark_read(job_id: int):
    with db() as c:
        c.execute("UPDATE jobs SET status='done' WHERE id=?", (job_id,))
    _mark_backup_dirty()

def job_update_payload(job_id: int, payload: dict):
    with db() as c:
        c.execute("UPDATE jobs SET payload_json=? WHERE id=?",
                  (json.dumps(payload, ensure_ascii=False), job_id))
    _mark_backup_dirty()


def _mark_pending_read_by_type(job_type: str):
    """Dismiss các job pending cũ cùng loại (tránh trùng card khi trigger thủ công).
    Bỏ qua job interactive (form daily_review đang chờ user tick)."""
    with db() as c:
        rows = c.execute(
            "SELECT id, payload_json FROM jobs WHERE type=? AND status='pending'", (job_type,)
        ).fetchall()
    for r in rows:
        try:
            pl = json.loads(r["payload_json"] or "{}")
        except Exception:
            pl = {}
        if pl.get("interactive"):
            continue
        job_mark_read(r["id"])


# ── User profile / Life OS state / App config ───────────────────────────
def profile_get() -> dict:
    with db() as c:
        r = c.execute("SELECT * FROM user_profile WHERE id=1").fetchone()
        return dict(r) if r else dict(_DEFAULT_PROFILE)

def profile_update(**fields) -> dict:
    allowed = {"ten", "nghe", "so_thich", "khu_vuc", "ghi_chu"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed and v is not None:
            sets.append(f"{k}=?"); vals.append(v)
    if not sets:
        return {"ok": False, "error": "không có trường hợp lệ"}
    sets.append("updated_at=?"); vals.append(_now_iso())
    with db() as c:
        c.execute(f"UPDATE user_profile SET {', '.join(sets)} WHERE id=1", vals)
    _mark_backup_dirty()
    return {"ok": True, "updated": list(fields.keys())}

def life_state_get() -> dict:
    with db() as c:
        r = c.execute("SELECT * FROM life_os_state WHERE id=1").fetchone()
        return dict(r) if r else {}

def life_state_update(**fields) -> dict:
    allowed = {"current_life_phase", "top_3_priorities", "current_risks"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed and v is not None:
            sets.append(f"{k}=?"); vals.append(v)
    if not sets:
        return {"ok": False, "error": "không có trường hợp lệ"}
    sets.append("updated_at=?"); vals.append(_now_iso())
    with db() as c:
        c.execute(f"UPDATE life_os_state SET {', '.join(sets)} WHERE id=1", vals)
    _mark_backup_dirty()
    return {"ok": True}

def config_get() -> dict:
    with db() as c:
        r = c.execute("SELECT * FROM app_config WHERE id=1").fetchone()
        return dict(r) if r else {"base_url": "", "model": "", "api_key_hint": ""}

def config_set(base_url: str = "", model: str = "", api_key: str = ""):
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
    _mark_backup_dirty()

# ── Goals CRUD ───────────────────────────────────────────────────────────
def goal_create(title: str, level: str, period: str = "", parent_id=None, description: str = "") -> dict:
    if level not in ("year", "month", "week"):
        return {"ok": False, "error": f"level phải là year/month/week"}
    with db() as c:
        cur = c.execute(
            """INSERT INTO goals(title, description, level, period, parent_id, status, progress_note, created_at)
               VALUES(?,?,?,?,?,'active','',?)""",
            (title, description or "", level, period or "", parent_id, _now_iso()),
        )
        _mark_backup_dirty()
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
        _mark_backup_dirty()
        return {"ok": True, "id": goal_id}


# ── Timezone ──────────────────────────────────────────────────────────────
from zoneinfo import ZoneInfo
TZ = ZoneInfo("Asia/Shanghai")

_WEEKDAYS_VI = ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật"]

def _season_vi(month: int) -> str:
    if month in (3, 4, 5): return "Xuân"
    if month in (6, 7, 8): return "Hè"
    if month in (9, 10, 11): return "Thu"
    return "Đông"


# ── System prompt ─────────────────────────────────────────────────────────
SYSTEM_PROMPT_TEMPLATE = """Bạn là trợ lý AI cá nhân thân thiết của tôi — một thư ký Kim giúp quản lý công việc, mục tiêu, lịch trình, ký ức và trí nhớ.
Vai trò: như một trợ lý giám đốc thực sự — quản lý task, goal, lên lịch, tóm kết, nhắc nhở, tâm sự.
Tính cách: thông minh, gần gũi, thi thoảng hài hước nhẹ nhàng.
Ngôn ngữ: tiếng Việt, tự nhiên.

# Thông tin về tôi
- Tên: {ten}
- Nghề nghiệp: {nghe}
- Sở thích: {so_thich}
- Khu vực: {khu_vuc}
- Ghi chú: {ghi_chu}

# Nhận thức thời gian
- Hiện tại: {now} ({weekday}), {date_full}
- Múi giờ: Asia/Shanghai (UTC+8)
- Mùa: {season} (Bắc bán cầu)

# Task & Reminder (tuân thủ nghiêm)
- CHỈ tạo task khi: (a) user yêu cầu rõ "tạo/thêm/nhắc/lên lịch/ghi việc", hoặc (b) cùng lúc chốt kế hoạch có thời gian cụ thể.
- KHÔNG tạo task khi: user chỉ chia sẻ, tâm sự, hỏi ý kiến, kể chuyện, nhắc việc chung chung không thời gian.
- KHÔNG tách 1 câu của user thành nhiều task. 1 ý = 1 task.
- title: ngắn, bắt đầu bằng động từ, cụ thể (VD "Gửi báo cáo tuần", "Mua sữa"). Tránh title mơ hồ như "Việc của Dương".
- priority: 1=khẩn/hậu quả lớn, 2=quan trọng, 3=bình thường, 4=khi rảnh. Không rõ → 3.
- remind_at/due_at/start_at: CHỈ đặt khi user nói thời gian. Không tự bịa thời gian.
- Khi chưa rõ (title, thời gian, mức ưu tiên) → HỎI lại user 1 câu, KHÔNG tự tạo.
- Nếu nghĩ đã có task cùng nội dung → gọi list_tasks kiểm tra, rồi dùng update_task để sửa, KHÔNG tạo mới.
- Reminder là thuộc tính remind_at của task — KHÔNG tạo reminder riêng.
- Khi user hoàn thành việc → complete_task. Khi user muốn xoá → delete_task. Khi muốn sửa/chuyển ngày → update_task.

# Lên lịch / Tổng kết
- Khi user yêu cầu lên lịch ngày: đề xuất lịch cân bằng (gồm work chính, ≥1 vận động, ≥1 học tập, có nghỉ) bằng markdown theo khung giờ. Dựa list_tasks (tránh trùng) + list_goals + get_life_state. KHÔNG tự tạo task/remind — chỉ tạo khi user xác nhận rõ sau khi xem lịch.
- Khi user yêu cầu tổng kết cuối ngày: gợi ý user xem lại tasks (list_tasks) và chia sẻ việc đã làm; tóm tắt ngắn. Tổng kết tự động (cron) do hệ thống chạy riêng.

# Memory
- Khi tôi chia sẻ thông tin quan trọng: gọi save_memory(content, type, importance).
- Khi cần nhớ lại: gọi search_memory(query).
- type: fact | preference | event | journal | note

Phần trả lời ngắn gọn, thân thiện."""

def build_system_prompt() -> str:
    now = datetime.now(TZ)
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


# ── Tool specs (refactored) ───────────────────────────────────────────────
TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "get_current_time",
        "description": "Lấy thời gian thực theo múi giờ chỉ định.",
        "parameters": {"type": "object", "properties": {
            "timezone": {"type": "string", "description": "Múi giờ IANA, mặc định Asia/Shanghai"}
        }},
    }},
    {"type": "function", "function": {
        "name": "create_task",
        "description": "Tạo MỘT task khi user yêu cầu rõ (tạo/nhắc/lên lịch/ghi việc) hoặc khi chốt kế hoạch có thời gian cụ thể. KHÔNG tạo task khi user chỉ chia sẻ, tâm sự, hỏi ý kiến. KHÔNG tách 1 câu thành nhiều task (1 ý = 1 task). Reminder là thuộc tính remind_at của task (KHÔNG cần tạo reminder riêng).",
        "parameters": {"type": "object",
            "properties": {
                "title": {"type": "string", "description": "Ngắn, bắt đầu bằng động từ, cụ thể. ĐÚNG: 'Gửi báo cáo tuần', 'Mua sữa'. SAI: 'Việc của Dương', 'Xử lý việc'."},
                "notes": {"type": "string", "description": "Context/lý do tại sao cần làm (1 câu ngắn)."},
                "due_at": {"type": "string", "description": "Hạn chót 'YYYY-MM-DD HH:MM'. Chỉ đặt khi user nói thời gian. Có thể nhập tương đối: 'hôm nay 18:00', 'mai 9:00'."},
                "remind_at": {"type": "string", "description": "Thời gian nhắc 'YYYY-MM-DD HH:MM'. Chỉ đặt khi user muốn được nhắc trước."},
                "start_at": {"type": "string", "description": "Thời gian bắt đầu 'YYYY-MM-DD HH:MM'."},
                "priority": {"type": "integer", "enum": [1, 2, 3, 4],
                             "description": "1=khẩn/có hậu quả lớn, 2=quan trọng cần làm sớm, 3=bình thường (mặc định), 4=khi rảnh. Không rõ → 3."},
                "repeat_rule": {"type": "string", "description": "Quy tắc lặp: daily|weekly|monthly."},
            },
            "required": ["title"],
        },
    }},
    {"type": "function", "function": {
        "name": "list_tasks",
        "description": "Liệt kê task theo status (pending/done/all).",
        "parameters": {"type": "object", "properties": {
            "status": {"type": "string", "enum": ["pending", "done", "all"]}
        }},
    }},
    {"type": "function", "function": {
        "name": "complete_task",
        "description": "Đánh dấu task đã xong.",
        "parameters": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
    }},
    {"type": "function", "function": {
        "name": "update_task",
        "description": "Sửa task đã có (đổi title, notes, due_at, remind_at, priority, status, repeat_rule). Dùng khi user muốn sửa/chuyển ngày/đánh dấu xong task CŨ — KHÔNG tạo task mới khi trùng. Để đánh dấu xong cũng có thể dùng complete_task.",
        "parameters": {"type": "object",
            "properties": {
                "id": {"type": "integer"},
                "title": {"type": "string"},
                "notes": {"type": "string"},
                "due_at": {"type": "string", "description": "Hạn chót 'YYYY-MM-DD HH:MM'. Gửi chuỗi rỗng để xoá hạn."},
                "remind_at": {"type": "string", "description": "Nhắc 'YYYY-MM-DD HH:MM'. Gửi chuỗi rỗng để xoá nhắc."},
                "start_at": {"type": "string"},
                "priority": {"type": "integer", "enum": [1, 2, 3, 4]},
                "status": {"type": "string", "enum": ["pending", "done"]},
                "repeat_rule": {"type": "string"},
            },
            "required": ["id"],
        },
    }},
    {"type": "function", "function": {
        "name": "delete_task",
        "description": "Xoá vĩnh viễn task (khi user yêu cầu xoá rõ). KHÔNG dùng để 'đánh dấu xong' — dùng complete_task cho việc đó.",
        "parameters": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
    }},
    {"type": "function", "function": {
        "name": "save_memory",
        "description": "Lưu ký ức/thông tin quan trọng vào bộ nhớ dài hạn.",
        "parameters": {"type": "object",
            "properties": {
                "content": {"type": "string"},
                "type": {"type": "string", "enum": ["fact", "preference", "event", "journal", "note"],
                         "description": "Loại ký ức"},
                "importance": {"type": "integer", "description": "1-5, mặc định 1"},
            },
            "required": ["content"],
        },
    }},
    {"type": "function", "function": {
        "name": "search_memory",
        "description": "Tìm kiếm trong bộ nhớ dài hạn theo từ khoá.",
        "parameters": {"type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }},
    {"type": "function", "function": {
        "name": "create_goal",
        "description": "Tạo mục tiêu phân tầng: year/month/week.",
        "parameters": {"type": "object",
            "properties": {
                "title": {"type": "string"},
                "level": {"type": "string", "enum": ["year", "month", "week"]},
                "period": {"type": "string", "description": "VD: 2026 / 2026-06 / 2026-W26"},
                "parent_id": {"type": "integer"},
                "description": {"type": "string"},
            },
            "required": ["title", "level"],
        },
    }},
    {"type": "function", "function": {
        "name": "list_goals",
        "description": "Liệt kê mục tiêu active.",
        "parameters": {"type": "object",
            "properties": {
                "level": {"type": "string", "enum": ["year", "month", "week"]},
                "period": {"type": "string"},
            },
        },
    }},
    {"type": "function", "function": {
        "name": "update_goal",
        "description": "Cập nhật goal (status, progress_note, v.v.).",
        "parameters": {"type": "object",
            "properties": {
                "id": {"type": "integer"},
                "status": {"type": "string", "enum": ["active", "done", "paused"]},
                "progress_note": {"type": "string"},
                "title": {"type": "string"},
                "period": {"type": "string"},
            },
            "required": ["id"],
        },
    }},
    {"type": "function", "function": {
        "name": "get_life_state",
        "description": "Đọc trạng thái Life OS (phase, priorities, risks) + goals active.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "update_profile",
        "description": "Cập nhật thông tin cá nhân (ten, nghe, so_thich, khu_vuc, ghi_chu).",
        "parameters": {"type": "object",
            "properties": {
                "ten": {"type": "string"}, "nghe": {"type": "string"},
                "so_thich": {"type": "string"}, "khu_vuc": {"type": "string"},
                "ghi_chu": {"type": "string"},
            },
        },
    }},
    {"type": "function", "function": {
        "name": "get_context",
        "description": "Lấy context tổng hợp (tasks, goals, memories gần đây) để lập kế hoạch/tổng kết.",
        "parameters": {"type": "object", "properties": {}},
    }},
]

MAX_TOOL_ROUNDS = 8
MAX_CREATE_TASKS_PER_TURN = 3


# ── Tool implementations ──────────────────────────────────────────────────
def _tool_get_current_time(timezone: str = "Asia/Shanghai") -> str:
    try:
        tz = ZoneInfo(timezone)
    except Exception:
        tz = TZ
    now = datetime.now(tz)
    return (f"{now.strftime('%H:%M')} ({_WEEKDAYS_VI[now.weekday()]}), "
            f"{now.day}/{now.month}/{now.year}, mùa {_season_vi(now.month)}")

def _tool_create_task(title, notes="", due_at="", remind_at="", start_at="",
                      priority=3, repeat_rule="") -> str:
    # Dedup mềm: nếu đã có task mở cùng tên → gợi ý update thay vì tạo mới.
    with db() as c:
        dup = c.execute(
            "SELECT id FROM tasks WHERE status='pending' AND title=? ORDER BY id DESC LIMIT 1",
            (title,)).fetchone()
    if dup:
        return (f"Đã có task mở cùng tên #{dup['id']}: {title}. "
                f"Nếu muốn đổi ngày/ưu tiên hãy dùng update_task(id={dup['id']}, ...) thay vì tạo mới.")
    t = task_create(title, notes=notes, due_at=due_at, remind_at=remind_at,
                    start_at=start_at, priority=priority, repeat_rule=repeat_rule)
    result = f"Đã tạo task id={t['id']}: {title}"
    # Lên lịch nhắc nếu có remind_at
    if remind_at:
        try:
            dt = datetime.strptime(remind_at, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
            _schedule_task_reminder(t['id'], dt, title)
            result += f" (nhắc lúc {remind_at})"
        except Exception as e:
            result += f" (lỗi đặt nhắc: {e})"
    return result

def _tool_list_tasks(status="pending") -> str:
    rows = task_list(status)
    if not rows:
        return f"Không có task nào (status={status})."
    lines = []
    for r in rows:
        due = f", hạn {r['due_at']}" if r["due_at"] else ""
        remind = f", nhắc {r['remind_at']}" if r["remind_at"] else ""
        lines.append(f"#{r['id']} [p{r['priority']}][{r['status']}] {r['title']}{due}{remind}")
    return "\n".join(lines)

def _tool_complete_task(id) -> str:
    r = task_complete(int(id))
    return f"OK: task {id} -> done" if r.get("ok") else f"Lỗi: {r.get('error')}"

def _tool_update_task(id, **kwargs) -> str:
    # Nếu cập nhật remind_at → đặt lại lịch nhắc
    rid = int(id)
    r = task_update(rid, **kwargs)
    if r.get("ok") and kwargs.get("remind_at"):
        try:
            t = task_list("all")
            task_info = next((x for x in t if x["id"] == rid), None)
            dt = datetime.strptime(kwargs["remind_at"], "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
            title = (task_info or {}).get("title", f"Task {rid}")
            _schedule_task_reminder(rid, dt, title)
        except Exception:
            pass
    return f"OK: đã cập nhật task {id}" if r.get("ok") else f"Lỗi: {r.get('error')}"

def _tool_delete_task(id) -> str:
    r = task_delete(int(id))
    return f"OK: đã xóa task {id}" if r.get("ok") else f"Lỗi: {r.get('error')}"

def _tool_save_memory(content, type="note", importance=1) -> str:
    r = memory_save(content, type=type, importance=importance)
    return f"Đã lưu memory id={r['id']} (type={type})"

def _tool_search_memory(query) -> str:
    rows = memory_search(query)
    if not rows:
        return f"Không tìm thấy gì cho '{query}'."
    lines = [f"[{r['type']}] {r['content'][:200]}" for r in rows]
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

def _tool_get_life_state() -> str:
    s = life_state_get()
    goals = goal_list()
    g_lines = [f"- [{r['level']}] {r['title']}" for r in goals]
    return (f"Life phase: {s.get('current_life_phase') or '(chưa rõ)'}\n"
            f"Top priorities: {s.get('top_3_priorities') or '[]'}\n"
            f"Risks: {s.get('current_risks') or '[]'}\n"
            f"Active goals:\n" + ("\n".join(g_lines) if g_lines else "- (không có)"))

def _tool_update_profile(**kwargs) -> str:
    r = profile_update(**kwargs)
    return f"OK: đã cập nhật profile" if r.get("ok") else f"Lỗi: {r.get('error')}"

def task_list_today() -> dict:
    """Trả {pending_today, done_today, date} theo ngày local Asia/Shanghai.
    Lưu ý lệch tz: created_at lưu UTC (CURRENT_TIMESTAMP), completed_at lưu local ISO (_now_iso),
    due_at/remind_at/start_at là text local do user/LLM nhập."""
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    yesterday = (datetime.now(TZ) - timedelta(days=1)).strftime("%Y-%m-%d")
    with db() as c:
        pend = c.execute(
            "SELECT * FROM tasks WHERE status='pending' AND ("
            "substr(due_at,1,10)=? OR substr(remind_at,1,10)=? OR substr(start_at,1,10)=? "
            "OR substr(created_at,1,10)=? OR substr(created_at,1,10)=?)",
            (today, today, today, today, yesterday)).fetchall()
        done = c.execute(
            "SELECT * FROM tasks WHERE status='done' AND substr(completed_at,1,10)=?", (today,)).fetchall()

    def is_today_local(row):
        for col in ("due_at", "remind_at", "start_at"):
            v = row.get(col)
            if v and str(v)[:10] == today:
                return True
        # created_at: UTC → local
        try:
            ca = str(row.get("created_at") or "").replace(" ", "T")
            local = datetime.fromisoformat(ca).replace(tzinfo=ZoneInfo("UTC")).astimezone(TZ)
            if local.strftime("%Y-%m-%d") == today:
                return True
        except Exception:
            pass
        return False

    pending_today = [dict(r) for r in pend if is_today_local(dict(r))]
    done_today = [dict(r) for r in done]
    return {"pending_today": pending_today, "done_today": done_today, "date": today}

def _build_context_bundle() -> str:
    now_str = datetime.now(TZ).strftime("%Y-%m-%d")
    tasks = task_list("pending")
    goals = goal_list()
    state = life_state_get()
    recent_mem = memory_search(now_str[:7], 5)  # ký ức tháng này

    def fmt_task(r):
        due = f" (hạn {r['due_at']})" if r["due_at"] else ""
        remind = f" (nhắc {r['remind_at']})" if r["remind_at"] else ""
        return f"#{r['id']} {r['title']}{due}{remind}"

    return (
        f"## Hôm nay: {now_str}\n"
        f"## Life OS: phase={state.get('current_life_phase','')} | priorities={state.get('top_3_priorities','')}\n"
        f"## Tasks đang mở ({len(tasks)})\n" +
        ("\n".join(fmt_task(r) for r in tasks) or "(không có)") + "\n"
        f"## Goals active\n" +
        ("\n".join(f"[{r['level']}][{r['period']}] {r['title']}" for r in goals) or "(không có)") + "\n"
        f"## Memories gần đây\n" +
        ("\n".join(r["content"][:120] for r in recent_mem) or "(không có)")
    )

def _tool_get_context() -> str:
    return _build_context_bundle()

TOOL_FUNCTIONS = {
    "get_current_time": _tool_get_current_time,
    "create_task": _tool_create_task,
    "list_tasks": _tool_list_tasks,
    "complete_task": _tool_complete_task,
    "update_task": _tool_update_task,
    "delete_task": _tool_delete_task,
    "save_memory": _tool_save_memory,
    "search_memory": _tool_search_memory,
    "create_goal": _tool_create_goal,
    "list_goals": _tool_list_goals,
    "update_goal": _tool_update_goal,
    "get_life_state": _tool_get_life_state,
    "update_profile": _tool_update_profile,
    "get_context": _tool_get_context,
}

def _run_tool(name: str, args_json: str) -> str:
    fn = TOOL_FUNCTIONS.get(name)
    if not fn:
        return f"Lỗi: tool '{name}' không tồn tại."
    try:
        args = json.loads(args_json) if args_json else {}
    except Exception as e:
        return f"Lỗi parse tham số: {e}"
    try:
        import inspect
        params = inspect.signature(fn).parameters
        has_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        clean = dict(args) if has_varkw else {k: v for k, v in args.items() if k in params}
        return str(fn(**clean))
    except Exception as e:
        return f"Lỗi khi chạy tool {name}: {type(e).__name__}: {e}"


# ── Thinking tag splitter ─────────────────────────────────────────────────
def _split_thinking(text: str):
    thoughts = []
    if not text:
        return text, []
    names = "think|thinking|reasoning|thought|analysis|reason"
    pat_bal = re.compile("<(" + names + r")(\s[^>]*)?>(.*?)</\1\s*>", re.I | re.S)
    def _bal(m):
        inner = m.group(3).strip()
        if inner:
            thoughts.append(inner)
        return ""
    text = pat_bal.sub(_bal, text)
    pat_pipe = re.compile(r"<\|thinking\|>(.*?)<\|/thinking\|>", re.S)
    text = pat_pipe.sub(lambda m: (thoughts.append(m.group(1).strip()), "")[1], text)
    pat_open = re.compile("<(" + names + r")(\s[^>]*)?>(.*)$", re.I | re.S)
    m = pat_open.search(text)
    if m and m.group(3).strip():
        thoughts.append(m.group(3).strip())
        text = text[:m.start()]
    pat_close = re.compile("</(" + names + r")\s*>", re.I)
    m = pat_close.search(text)
    if m:
        before = text[:m.start()].strip()
        if before:
            thoughts.append(before)
        text = text[m.end():]
    return text.strip(), thoughts


# ── Scheduler & Task Reminders ────────────────────────────────────────────
scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")

def _fire_task_reminder(task_id: int, message: str):
    send_push("Nhắc nhở", message)
    print(f"[reminder] fired task_id={task_id}: {message}")

def _schedule_task_reminder(task_id: int, dt: datetime, title: str):
    """Lên lịch nhắc cho task (dùng remind_at)."""
    now = datetime.now(TZ)
    if dt <= now:
        return
    scheduler.add_job(
        lambda tid=task_id, t=title: _fire_task_reminder(tid, t),
        'date', run_date=dt,
        id=f"task-remind-{task_id}",
        replace_existing=True,
    )
    print(f"[reminder] scheduled task={task_id} at {dt.strftime('%Y-%m-%d %H:%M')}")

def _reschedule_task_reminders():
    """Khởi động lại: lên lịch lại remind_at cho các task còn pending."""
    rows = task_pending_reminders()
    now = datetime.now(TZ)
    count = 0
    for r in rows:
        try:
            dt = datetime.strptime(r["remind_at"], "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
            if dt > now:
                _schedule_task_reminder(r["id"], dt, r["title"])
                count += 1
        except Exception as e:
            print(f"[reminder] resume error task_id={r['id']}: {e}")
    if count:
        print(f"[reminder] resumed {count} task reminders")


# ── Push notification ─────────────────────────────────────────────────────
def _push_sub_endpoint(sub: dict) -> str:
    return str((sub or {}).get("endpoint") or "").strip()

def push_sub_add(sub: dict) -> dict:
    endpoint = _push_sub_endpoint(sub)
    if not endpoint:
        return {"ok": False, "error": "subscription thiếu endpoint"}
    raw = json.dumps(sub, ensure_ascii=False, sort_keys=True)
    now = _now_iso()
    with db() as c:
        c.execute("""INSERT INTO push_subscriptions(endpoint, subscription_json, created_at, updated_at)
                     VALUES(?,?,?,?)
                     ON CONFLICT(endpoint) DO UPDATE SET subscription_json=excluded.subscription_json, updated_at=excluded.updated_at""",
                  (endpoint, raw, now, now))
    global push_subscriptions
    push_subscriptions = [s for s in push_subscriptions if _push_sub_endpoint(s) != endpoint]
    push_subscriptions.append(sub)
    _mark_backup_dirty()
    return {"ok": True, "endpoint": endpoint}

def push_sub_load() -> int:
    global push_subscriptions
    loaded = []
    with db() as c:
        rows = c.execute("SELECT subscription_json FROM push_subscriptions ORDER BY id").fetchall()
    for r in rows:
        try:
            sub = json.loads(r["subscription_json"] or "{}")
            if _push_sub_endpoint(sub):
                loaded.append(sub)
        except Exception:
            continue
    push_subscriptions = loaded
    return len(loaded)

def push_sub_remove(endpoint: str):
    endpoint = (endpoint or "").strip()
    if not endpoint:
        return
    with db() as c:
        c.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (endpoint,))
    global push_subscriptions
    push_subscriptions = [s for s in push_subscriptions if _push_sub_endpoint(s) != endpoint]
    _mark_backup_dirty()

def send_push(title: str, body: str) -> dict:
    summary = {"sent": 0, "failed": [], "skipped": ""}
    if not _VAPID_INSTANCE:
        summary["skipped"] = "VAPID private key chưa nạp được"
        return summary
    if not push_subscriptions:
        summary["skipped"] = "chưa có subscription nào"
        return summary
    payload = json.dumps({"title": title, "body": body})
    dead = []
    for sub in push_subscriptions:
        endpoint = sub.get("endpoint", "?")
        try:
            webpush(subscription_info=sub, data=payload,
                    vapid_private_key=_VAPID_INSTANCE,
                    vapid_claims={"sub": VAPID_EMAIL})
            summary["sent"] += 1
        except WebPushException as e:
            msg = str(e)
            summary["failed"].append({"endpoint": endpoint, "error": msg})
            if "410" in msg or "404" in msg:
                dead.append(sub)
        except Exception as e:
            summary["failed"].append({"endpoint": endpoint, "error": f"{type(e).__name__}: {e}"})
    for d in dead:
        push_sub_remove(_push_sub_endpoint(d))
    return summary


# ── Proactive jobs ────────────────────────────────────────────────────────
def _server_client():
    cfg = config_get()
    key = (cfg.get("api_key_hint") or OPENAI_API_KEY).strip()
    url = (cfg.get("base_url") or OPENAI_BASE_URL).strip()
    mdl = (cfg.get("model") or "krr/claude-haiku-4-5-20251001").strip()
    if not key:
        return None, None
    return OpenAI(api_key=key, base_url=url), mdl

_JOB_DIRECTIVES = {
    "morning_planning": (
        "Lên lịch cho NGÀY MAI. KHÔNG gọi create_task/update_task/complete_task/delete_task. "
        "Chỉ đề xuất lịch bằng JSON. Yêu cầu cân bằng: ít nhất 1 slot vận động (exercise), "
        "1 slot học tập (learning), các slot work chính, và slot nghỉ (rest). "
        "Dựa vào tasks đang mở (không trùng), mục tiêu gần (tuần/tháng) và mục tiêu xa (năm). "
        "Trả về ĐÚNG MỘT khối ```json [{\"time\":\"HH:MM\",\"title\":\"...\","
        "\"category\":\"work|exercise|learning|rest|other\",\"priority\":1-4,\"remind\":true|false}]``` "
        "không kèm giải thích ngoài khối JSON. time là giờ bắt đầu trong ngày mai."
    ),
    "daily_review": (
        "Tổng kết cuối ngày: hoàn thành gì, trì hoãn gì, tâm trạng. "
        "Gọi get_context để lấy dữ liệu. Trả markdown tiếng Việt ngắn gọn."
    ),
    "weekly_review": (
        "Tổng kết cuối tuần: hoàn thành/chưa xong/rủi ro/đề xuất tuần sau. "
        "Gọi get_context. Trả markdown tiếng Việt."
    ),
    "monthly_review": (
        "Tổng kết cuối tháng: mục tiêu tháng, tỷ lệ hoàn thành, vấn đề, kế hoạch tháng sau. "
        "Gọi get_context + list_goals. Trả markdown tiếng Việt."
    ),
}

PLANNING_TOOLS = [t for t in TOOL_SPECS if t["function"]["name"]
                  in ("list_tasks", "list_goals", "get_life_state", "get_context", "search_memory", "get_current_time")]

def _build_planning_bundle() -> str:
    today = datetime.now(TZ)
    tomorrow = (today + timedelta(days=1)).strftime("%Y-%m-%d")
    tasks = task_list("pending")
    state = life_state_get()
    goals_all = goal_list()
    near = [g for g in goals_all if g["level"] in ("week", "month")]
    far = [g for g in goals_all if g["level"] == "year"]
    return (
        f"## Ngày lên lịch: {tomorrow}\n"
        f"## Life OS: phase={state.get('current_life_phase','')} | priorities={state.get('top_3_priorities','')}\n"
        f"## Tasks đang mở ({len(tasks)}) — tránh trùng:\n" +
        ("\n".join(f"- {r['title']} (hạn {r['due_at'] or '—'})" for r in tasks) or "(không có)") + "\n"
        f"## Mục tiêu gần (tuần/tháng):\n" +
        ("\n".join(f"- [{g['level']}][{g['period']}] {g['title']}" for g in near) or "(không có)") + "\n"
        f"## Mục tiêu xa (năm):\n" +
        ("\n".join(f"- {g['title']}" for g in far) or "(không có)")
    )

def _parse_slots_json(reply: str) -> list:
    """Tìm khối ```json [...] ``` hoặc mảng [...] đầu tiên trong reply. Trả list slot đã validate."""
    import re as _re
    if not reply:
        return []
    candidates = []
    m = _re.search(r"```(?:json)?\s*(\[[\s\S]*?\])\s*```", reply, _re.I)
    if m:
        candidates.append(m.group(1))
    m2 = _re.search(r"(\[[\s\S]*?\])", reply)
    if m2:
        candidates.append(m2.group(1))
    for cand in candidates:
        try:
            arr = json.loads(cand)
            if isinstance(arr, list):
                slots = []
                for s in arr:
                    if not isinstance(s, dict):
                        continue
                    title = str(s.get("title", "")).strip()
                    time_str = str(s.get("time", "")).strip()
                    if not title or not time_str:
                        continue
                    # validate HH:MM
                    try:
                        h, mn = map(int, time_str.split(":")[:2])
                        if not (0 <= h <= 23 and 0 <= mn <= 59):
                            continue
                    except Exception:
                        continue
                    cat = str(s.get("category", "other")).lower()
                    if cat not in ("work", "exercise", "learning", "rest", "other"):
                        cat = "other"
                    try:
                        prio = max(1, min(4, int(s.get("priority", 3))))
                    except Exception:
                        prio = 3
                    slots.append({"time": f"{h:02d}:{mn:02d}", "title": title, "category": cat,
                                  "priority": prio, "remind": bool(s.get("remind", True))})
                if slots:
                    return slots
        except Exception:
            continue
    return []

def task_exists_today(title: str, date: str) -> bool:
    with db() as c:
        r = c.execute(
            "SELECT 1 FROM tasks WHERE title=? AND status='pending' AND ("
            "substr(start_at,1,10)=? OR substr(due_at,1,10)=?) LIMIT 1",
            (title, date, date)).fetchone()
    return bool(r)

def task_create_from_slot(slot: dict, date: str) -> dict:
    title = str(slot.get("title", "")).strip()
    if not title:
        return {"ok": False, "skipped": "no title"}
    if task_exists_today(title, date):
        return {"ok": False, "skipped": "dup", "title": title}
    time_str = str(slot.get("time", "09:00"))
    try:
        h, m = map(int, time_str.split(":")[:2])
    except Exception:
        return {"ok": False, "skipped": "bad time"}
    start_at = f"{date} {h:02d}:{m:02d}"
    remind_at = ""
    remind_min = h * 60 + m - 15  # nhắc 15 phút trước
    if 360 <= remind_min <= 1320 and slot.get("remind", True):  # chỉ trong 06:00–22:00
        rh, rm = divmod(remind_min, 60)
        remind_at = f"{date} {rh:02d}:{rm:02d}"
    try:
        priority = max(1, min(4, int(slot.get("priority", 3))))
    except Exception:
        priority = 3
    notes = f"category: {slot.get('category', 'other')}"
    t = task_create(title, notes=notes, start_at=start_at, due_at=start_at,
                    remind_at=remind_at, priority=priority)
    if remind_at:
        try:
            dt = datetime.strptime(remind_at, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
            _schedule_task_reminder(t["id"], dt, title)
        except Exception:
            pass
    return {"ok": True, "id": t["id"], "title": title, "remind_at": remind_at}

def _run_morning_planning(client, mdl, push: bool = True) -> dict:
    tomorrow = (datetime.now(TZ) + timedelta(days=1)).strftime("%Y-%m-%d")
    bundle = _build_planning_bundle()
    sys_prompt = build_system_prompt() + "\n\n# Yêu cầu lượt này\n" + _JOB_DIRECTIVES["morning_planning"]
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": f"Context:\n{bundle}\n\nHãy lên lịch ngày mai."},
    ]
    reply = ""
    try:
        tools_ok = True
        for _ in range(4):
            try:
                resp = client.chat.completions.create(
                    model=mdl, messages=messages, tools=PLANNING_TOOLS,
                    tool_choice="auto", max_tokens=1200)
            except Exception as e:
                # Proxy có thể strip tools → thử lại không tools.
                if tools_ok:
                    tools_ok = False
                    resp = client.chat.completions.create(model=mdl, messages=messages, max_tokens=1200)
                else:
                    raise
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
            reply = (last["content"] if last else "").strip()

        reply, _ = _split_thinking(reply)
        slots = _parse_slots_json(reply)
        if not slots:
            jid = job_create("morning_planning", payload={"content": reply, "date": tomorrow, "slots": None})
            if push:
                send_push("Lịch ngày mai", "Mở app để xem (không lấy được slots JSON)")
            print(f"[proactive] morning_planning no-slots job={jid['id']}")
            return {"job": "morning_planning", "ok": True, "content": reply, "slots": None,
                    "job_id": jid["id"], "date": tomorrow}
        jid = job_create("morning_planning", payload={"slots": slots, "date": tomorrow, "content": reply})
        if push:
            send_push("Lịch ngày mai sẵn sàng", "Mở app để duyệt")
        print(f"[proactive] morning_planning slots={len(slots)} job={jid['id']}")
        return {"job": "morning_planning", "ok": True, "content": reply, "slots": slots,
                "job_id": jid["id"], "date": tomorrow}
    except Exception as e:
        print(f"[proactive] morning_planning error: {type(e).__name__}: {e}")
        return {"job": "morning_planning", "ok": False, "error": f"{type(e).__name__}: {e}"}

def _run_proactive_job(job_type: str, push: bool = True) -> dict:
    result = {"job": job_type, "ok": False, "content": "", "error": None, "job_id": None}
    client, mdl = _server_client()
    if not client:
        result["error"] = "chưa có API key"
        print(f"[proactive] {job_type} skip: {result['error']}")
        return result
    if job_type == "morning_planning":
        return _run_morning_planning(client, mdl, push=push)
    bundle = _build_context_bundle()
    sys_prompt = build_system_prompt() + "\n\n# Yêu cầu lượt này\n" + _JOB_DIRECTIVES.get(job_type, "")
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": f"Context:\n{bundle}\n\nHãy thực hiện yêu cầu."},
    ]
    reply = ""
    try:
        for _ in range(4):
            resp = client.chat.completions.create(
                model=mdl, messages=messages, tools=TOOL_SPECS,
                tool_choice="auto", max_tokens=1400)
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

        reply, _ = _split_thinking(reply)

        # Lưu vào jobs table
        jid = job_create(job_type, payload={"content": reply, "period": datetime.now(TZ).strftime("%Y-%m-%d")})
        # Update payload với content
        job_update_payload(jid["id"], {"content": reply, "period": datetime.now(TZ).strftime("%Y-%m-%d")})

        push_title = {
            "morning_planning": "📋 Lịch ngày mai sẵn sàng",
            "daily_review": "📝 Tổng kết ngày",
            "weekly_review": "📅 Tổng kết tuần",
            "monthly_review": "🗓️ Tổng kết tháng",
        }.get(job_type, "Trợ lý Kim")
        if push:
            send_push(push_title, "Mở app để xem chi tiết")
        result.update({"ok": True, "content": reply, "job_id": jid["id"]})
        print(f"[proactive] {job_type} done")
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        print(f"[proactive] {job_type} error: {result['error']}")
    return result

def _run_daily_review_interactive() -> dict:
    """Daily review hardcode: tạo job pending (interactive) + push nudge. KHÔNG dùng LLM."""
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    jid = job_create("daily_review", payload={"interactive": True, "date": today, "content": ""})
    send_push("Tổng kết ngày", "Mở app để tổng kết hôm nay")
    print(f"[proactive] daily_review interactive job={jid['id']}")
    return {"ok": True, "job_id": jid["id"], "interactive": True}

def _register_cron_jobs():
    scheduler.add_job(lambda: _run_proactive_job('morning_planning'), 'cron',
                      hour=7, minute=30, id='morning_planning', replace_existing=True)
    scheduler.add_job(_run_daily_review_interactive, 'cron',
                      hour=22, minute=0, id='daily_review', replace_existing=True)
    scheduler.add_job(lambda: _run_proactive_job('weekly_review'), 'cron',
                      day_of_week='sun', hour=21, minute=0, id='weekly_review', replace_existing=True)
    scheduler.add_job(lambda: _run_proactive_job('monthly_review'), 'cron',
                      day='last', hour=21, minute=0, id='monthly_review', replace_existing=True)


# ── Static files ──────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def index():
    return FileResponse("index.html")

@app.get("/index.html")
async def index_html():
    return FileResponse("index.html")

@app.get("/sw.js")
async def sw():
    return FileResponse("sw.js", media_type="application/javascript")

@app.get("/manifest.json")
async def manifest():
    return FileResponse("manifest.json")

@app.head("/ping")
async def ping():
    return {"status": "ok"}

@app.get("/config")
async def public_config():
    cfg = config_get()
    return JSONResponse({"base_url": cfg.get("base_url", ""), "model": cfg.get("model", "")})

@app.get("/vapid-public-key")
async def vapid_public_key():
    if not VAPID_PUBLIC:
        return JSONResponse({"error": "VAPID_PUBLIC_KEY chưa cấu hình"}, status_code=500)
    return JSONResponse({"publicKey": VAPID_PUBLIC})

@app.post("/subscribe")
async def subscribe(request: Request):
    sub = await request.json()
    r = push_sub_add(sub)
    if not r.get("ok"):
        return JSONResponse(r, status_code=400)
    return JSONResponse({"status": "ok"})


# ── POST /chat ────────────────────────────────────────────────────────────
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

    config_set(base_url=base_url, model=model, api_key=api_key)
    client = OpenAI(api_key=api_key, base_url=base_url)

    msg_add("user", user_msg)
    history = msg_recent(20)

    try:
        messages = [{"role": "system", "content": build_system_prompt()}, *history]
        reply = ""
        trace = []
        create_task_count = 0

        for _ in range(MAX_TOOL_ROUNDS):
            response = client.chat.completions.create(
                model=model, messages=messages,
                tools=TOOL_SPECS, tool_choice="auto", max_tokens=1200)
            msg = response.choices[0].message
            messages.append(msg.model_dump(exclude_none=True))

            if msg.content and msg.tool_calls:
                clean_c, ths = _split_thinking(msg.content)
                for t in ths + ([clean_c] if clean_c else []):
                    trace.append({"kind": "thought", "text": t[:400]})

            if not msg.tool_calls:
                reply, ths = _split_thinking(msg.content or "")
                for t in ths:
                    trace.append({"kind": "thought", "text": t[:400]})
                break

            for tc in msg.tool_calls:
                # Guard: chặn tạo task quá nhiều trong 1 lượt (chống LLM tạo bừa).
                if tc.function.name == "create_task":
                    create_task_count += 1
                    if create_task_count > MAX_CREATE_TASKS_PER_TURN:
                        result = (f"Đã tạo đủ task trong lượt này (tối đa {MAX_CREATE_TASKS_PER_TURN}). "
                                  f"Hãy gộp các việc còn lại hoặc đợi lượt sau.")
                    else:
                        result = _run_tool(tc.function.name, tc.function.arguments)
                else:
                    result = _run_tool(tc.function.name, tc.function.arguments)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
                trace.append({
                    "kind": "tool", "name": tc.function.name,
                    "args": tc.function.arguments or "",
                    "result": (result or "")[:800],
                })
        else:
            last = next((m for m in reversed(messages)
                         if m.get("role") == "assistant" and m.get("content")), None)
            reply = (last["content"] if last else "").strip() or "⚠️ Quá nhiều bước, thử lại nhé."
            reply, ths = _split_thinking(reply)
            for t in ths:
                trace.append({"kind": "thought", "text": t[:400]})

        # Lưu trace vào metadata_json (cap kích thước) để hiển thị lại khi load app.
        trace_to_store = trace[-24:] if trace else None
        if trace_to_store and len(json.dumps(trace_to_store, ensure_ascii=False)) > 20000:
            trace_to_store = trace_to_store[-12:]
        msg_add("assistant", reply, metadata={"trace": trace_to_store} if trace_to_store else None)
        out = {"reply": reply}
        if trace:
            out["trace"] = trace
        return JSONResponse(out)

    except Exception as e:
        return JSONResponse({"reply": f"Lỗi: {str(e)}"}, status_code=500)


# ── GET /history ──────────────────────────────────────────────────────────
@app.get("/history")
async def history():
    return JSONResponse({"items": msg_list(100)})

@app.post("/reset-chat")
async def reset_chat():
    msg_clear()
    return JSONResponse({"ok": True})


# ── Tasks API ─────────────────────────────────────────────────────────────
@app.post("/tasks")
async def create_task_endpoint(request: Request):
    data = await request.json()
    title = data.get("title", "").strip()
    if not title:
        return JSONResponse({"error": "title bắt buộc"}, status_code=400)
    t = task_create(
        title=title,
        notes=data.get("notes", ""),
        due_at=data.get("due_at", ""),
        remind_at=data.get("remind_at", ""),
        start_at=data.get("start_at", ""),
        priority=int(data.get("priority", 3)),
        repeat_rule=data.get("repeat_rule", ""),
    )
    # Lên lịch nhắc nếu có
    if data.get("remind_at"):
        try:
            dt = datetime.strptime(data["remind_at"], "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
            _schedule_task_reminder(t["id"], dt, title)
        except Exception:
            pass
    return JSONResponse({"ok": True, **t})

@app.get("/tasks")
async def list_tasks_endpoint(status: str = "pending"):
    return JSONResponse({"items": task_list(status)})

@app.post("/tasks/{task_id}/complete")
async def complete_task_endpoint(task_id: int):
    r = task_complete(task_id)
    if not r.get("ok"):
        return JSONResponse(r, status_code=404)
    return JSONResponse(r)

@app.patch("/tasks/{task_id}")
async def update_task_endpoint(task_id: int, request: Request):
    data = await request.json()
    fields = {}
    for k in ("title", "notes", "status", "priority", "due_at", "remind_at", "start_at", "repeat_rule"):
        if k not in data:
            continue
        v = data[k]
        # Chuỗi rỗng cho các cột thời gian → xoá (NULL); các trường text giữ nguyên.
        if k in ("due_at", "remind_at", "start_at") and v == "":
            v = None
        if v is not None or k in ("due_at", "remind_at", "start_at"):
            fields[k] = v
    r = task_update(task_id, **fields)
    if not r.get("ok"):
        return JSONResponse(r, status_code=404)
    # Đặt lại lịch nhắc nếu remind_at đổi; gỡ nếu xoá.
    if "remind_at" in fields:
        if fields["remind_at"]:
            try:
                t = next((x for x in task_list("all") if x["id"] == task_id), None)
                dt = datetime.strptime(fields["remind_at"], "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
                _schedule_task_reminder(task_id, dt, (t or {}).get("title", f"Task {task_id}"))
            except Exception:
                pass
        else:
            try:
                scheduler.remove_job(f"task-remind-{task_id}")
            except Exception:
                pass
    return JSONResponse(r)

@app.delete("/tasks/{task_id}")
async def delete_task_endpoint(task_id: int):
    try:
        scheduler.remove_job(f"task-remind-{task_id}")
    except Exception:
        pass
    r = task_delete(task_id)
    if not r.get("ok"):
        return JSONResponse(r, status_code=404)
    return JSONResponse(r)


# ── Memory API ────────────────────────────────────────────────────────────
@app.post("/memory/save")
async def memory_save_endpoint(request: Request):
    data = await request.json()
    content = data.get("content", "").strip()
    if not content:
        return JSONResponse({"error": "content bắt buộc"}, status_code=400)
    r = memory_save(content, type=data.get("type", "note"), importance=int(data.get("importance", 1)))
    return JSONResponse({"ok": True, **r})

@app.post("/memory/search")
async def memory_search_endpoint(request: Request):
    data = await request.json()
    query = data.get("query", "").strip()
    if not query:
        return JSONResponse({"error": "query bắt buộc"}, status_code=400)
    results = memory_search(query)
    return JSONResponse({"items": results})

@app.get("/memories")
async def memories_endpoint(limit: int = 50):
    limit = max(1, min(int(limit), 200))
    return JSONResponse({"items": memory_list(limit)})

@app.patch("/memories/{memory_id}")
async def update_memory_endpoint(memory_id: int, request: Request):
    data = await request.json()
    fields = {k: v for k, v in data.items()
              if k in ("content", "type", "importance") and v is not None}
    r = memory_update(memory_id, **fields)
    if not r.get("ok"):
        return JSONResponse(r, status_code=404)
    return JSONResponse(r)

@app.delete("/memories/{memory_id}")
async def delete_memory_endpoint(memory_id: int):
    r = memory_delete(memory_id)
    if not r.get("ok"):
        return JSONResponse(r, status_code=404)
    return JSONResponse(r)


# ── Pending API (jobs chưa đọc) ───────────────────────────────────────────
@app.get("/pending")
async def pending():
    """Trả jobs pending để frontend hiển thị (morning_planning, daily_review…)."""
    items = job_list_pending()
    # Chuyển đổi format để tương thích frontend
    out = []
    for job in items:
        payload = job.get("payload") or {}
        item = {
            "id": job["id"],
            "type": job["type"],
            "period": payload.get("period", "") or payload.get("date", ""),
            "content": payload.get("content", ""),
            "interactive": bool(payload.get("interactive", False)),
            "created_at": job.get("created_at") or "",
        }
        if job["type"] == "morning_planning" and payload.get("slots"):
            item["schedule_id"] = job["id"]
            item["schedule_slots"] = payload["slots"]
        out.append(item)
    return JSONResponse({"items": out})

@app.get("/daily-review/today")
async def daily_review_today():
    """Trả tasks hôm nay + job daily_review pending interactive của hôm nay (nếu có)."""
    data = task_list_today()
    with db() as c:
        r = c.execute(
            "SELECT id FROM jobs WHERE type='daily_review' AND status='pending' "
            "AND payload_json LIKE ? ORDER BY id DESC LIMIT 1",
            (f'%{data["date"]}"%',)).fetchone()
    return JSONResponse({
        "job_id": r["id"] if r else None,
        "date": data["date"],
        "pending_today": data["pending_today"],
        "done_today": data["done_today"],
    })

def _llm_summarize_day(done_titles, still_open_titles, other_done, bundle):
    """Một lượt LLM (không tool) viết tổng kết cuối ngày. Trả str hoặc None nếu fail/không key.
    Bundle đã chứa context (tasks/goals/state/memories) nên không cần tool call."""
    client, mdl = _server_client()
    if not client:
        return None
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    sys_prompt = build_system_prompt() + "\n\n# Yêu cầu lượt này\n" + _JOB_DIRECTIVES["daily_review"]
    done_blk = ("\n".join(f"- {t}" for t in done_titles) or "(không có)")
    open_blk = ("\n".join(f"- {t}" for t in still_open_titles) or "(không có)")
    other_blk = other_done or "(không)"
    user = (
        f"## Ngày: {today}\n## Context:\n{bundle}\n\n"
        f"## Dữ liệu người dùng tổng kết:\n"
        f"### Đã hoàn thành:\n{done_blk}\n"
        f"### Còn dang dở:\n{open_blk}\n"
        f"### Việc khác:\n{other_blk}\n\n"
        f"Viết tổng kết cuối ngày bằng markdown tiếng Việt, ngắn gọn, ấm áp, có tinh thần động viên. "
        f"Gợi ý ưu tiên cho ngày mai nếu có việc dang dở. Dữ liệu đã sẵn sàng, KHÔNG cần gọi get_context "
        f"hay tool nào."
    )
    try:
        resp = client.chat.completions.create(
            model=mdl,
            messages=[{"role": "system", "content": sys_prompt}, {"role": "user", "content": user}],
            max_tokens=800,
        )
        reply, _ = _split_thinking((resp.choices[0].message.content or "").strip())
        return reply or None
    except Exception as e:
        print(f"[daily-review] LLM summarize error: {type(e).__name__}: {e}")
        return None


@app.post("/daily-review/submit")
async def daily_review_submit(request: Request):
    """Tổng kết ngày: complete các task được tick + dùng LLM viết summary (fallback hardcode nếu fail)."""
    data = await request.json()
    job_id = data.get("job_id")
    completed = data.get("completed_task_ids", []) or []
    other_done = (data.get("other_done", "") or "").strip()
    today = datetime.now(TZ).strftime("%Y-%m-%d")

    done_titles = []
    for tid in completed:
        try:
            tid = int(tid)
        except (TypeError, ValueError):
            continue
        with db() as c:
            row = c.execute("SELECT title FROM tasks WHERE id=?", (tid,)).fetchone()
        if row:
            task_complete(tid)
            done_titles.append(row["title"])

    td = task_list_today()
    completed_int = []
    for x in completed:
        try:
            completed_int.append(int(x))
        except (TypeError, ValueError):
            continue
    still_open = [t for t in td["pending_today"] if t["id"] not in completed_int]
    still_open_titles = [t["title"] for t in still_open]

    # Thử LLM tổng kết; nếu fail/không key → fallback hardcode.
    summary = None
    try:
        summary = _llm_summarize_day(done_titles, still_open_titles, other_done, _build_context_bundle())
    except Exception as e:
        print(f"[daily-review] summarize call error: {e}")
    if not summary:
        parts = [f"## Tổng kết ngày {today}"]
        if done_titles:
            parts.append("**Đã hoàn thành:**\n" + "\n".join(f"- {t}" for t in done_titles))
        if still_open:
            parts.append("**Còn dang dở:**\n" + "\n".join(f"- {t['title']}" for t in still_open))
        if other_done:
            parts.append(f"**Ghi chú thêm:**\n{other_done}")
        if not done_titles and not still_open and not other_done:
            parts.append("Hôm nay chưa có việc nào được ghi nhận. Ngày mai cố gắng nhé!")
        summary = "\n\n".join(parts)

    memory_save(summary, type="journal", importance=3)

    if job_id:
        try:
            job_mark_read(int(job_id))
        except Exception:
            pass
    else:
        job_create("daily_review", payload={"interactive": True, "date": today,
                                            "content": summary, "submitted": True})

    return JSONResponse({"ok": True, "text": summary})

@app.post("/pending/read")
async def pending_read(request: Request):
    data = await request.json()
    jid = data.get("id")
    try:
        job_mark_read(int(jid))
        return JSONResponse({"ok": True})
    except (TypeError, ValueError):
        return JSONResponse({"error": "id không hợp lệ"}, status_code=400)

@app.post("/approve-morning")
async def approve_morning(request: Request):
    """User duyệt lịch morning planning → tạo tasks từ slots (dedup + remind + no-late-night)."""
    data = await request.json()
    job_id = data.get("job_id")
    slots = data.get("slots", []) or []

    # Lấy slots từ job payload nếu không gửi (Duyệt thẳng).
    if not slots and job_id:
        with db() as c:
            r = c.execute("SELECT payload_json, status FROM jobs WHERE id=?", (int(job_id),)).fetchone()
        if r:
            try:
                payload = json.loads(r["payload_json"] or "{}")
            except Exception:
                payload = {}
            slots = payload.get("slots") or []
            if r["status"] == "done":
                return JSONResponse({"ok": False, "error": "đã duyệt"}, status_code=400)

    if not slots:
        return JSONResponse({"ok": False, "error": "không có slots"}, status_code=400)

    tomorrow = (datetime.now(TZ) + timedelta(days=1)).strftime("%Y-%m-%d")
    seen_times = set()
    created, skipped = [], []
    for s in sorted(slots, key=lambda x: x.get("time", "99:99")):
        t = s.get("time", "")
        if t in seen_times:
            skipped.append({"title": s.get("title", ""), "reason": "overlap"})
            continue
        seen_times.add(t)
        res = task_create_from_slot(s, tomorrow)
        if res.get("ok"):
            created.append(res)
        else:
            skipped.append({"title": s.get("title", ""), "reason": res.get("skipped")})

    if job_id:
        try:
            job_update_payload(int(job_id), {"slots": slots, "date": tomorrow, "approved": True})
            job_mark_read(int(job_id))
        except Exception:
            pass
    return JSONResponse({"ok": True, "created": len(created), "skipped": len(skipped),
                         "tasks": created, "skipped_details": skipped})


@app.post("/proactive/run")
async def proactive_run(request: Request):
    """Trigger proactive job thủ công (KHÔNG push). Kết quả trả về để hiển thị trong chat.
    morning_planning có slots → giữ pending để user duyệt; còn lại → mark read (tránh trùng /pending)."""
    data = await request.json()
    job = (data.get("job") or "").strip()
    if job not in _JOB_DIRECTIVES:
        return JSONResponse({"ok": False, "error": f"job không hợp lệ: {job}"}, status_code=400)
    _mark_pending_read_by_type(job)               # dedup: gỡ job pending cũ cùng loại
    result = _run_proactive_job(job, push=False)
    jid = result.get("job_id")
    if jid is not None and not (job == "morning_planning" and result.get("slots")):
        try:
            job_mark_read(int(jid))
        except Exception:
            pass
    return JSONResponse(result)


# ── Debug endpoints ───────────────────────────────────────────────────────
@app.get("/debug-push")
async def debug_push():
    return JSONResponse({
        "vapid_public_set": bool(VAPID_PUBLIC),
        "vapid_private_set": bool(VAPID_PRIVATE),
        "vapid_email": VAPID_EMAIL,
        "subscribers": len(push_subscriptions),
    })

@app.get("/debug-backup")
async def debug_backup(run: bool = False):
    result = None
    if run:
        result = backup_memory_db(force=True)
    return JSONResponse({
        "configured": _github_backup_configured(),
        "dirty": _backup_dirty,
        "last_attempt": _backup_last_attempt,
        "last_success": _backup_last_success,
        "last_error": _backup_last_error,
        "run_result": result,
    })

@app.post("/debug-backup-now")
async def debug_backup_now():
    return JSONResponse(backup_memory_db(force=True))

@app.get("/debug-proactive")
async def debug_proactive(job: str = "daily_review"):
    if job not in _JOB_DIRECTIVES:
        return JSONResponse({"error": f"job không hợp lệ: {job}"}, status_code=400)
    return JSONResponse(_run_proactive_job(job))

@app.get("/debug-review-nudge")
async def debug_review_nudge():
    """Test daily review interactive (KHÔNG LLM): tạo job pending + push."""
    return JSONResponse(_run_daily_review_interactive())

@app.get("/debug-seed-morning")
async def debug_seed_morning():
    """Test card duyệt morning planning (KHÔNG cần LLM): insert job với slots mẫu."""
    tomorrow = (datetime.now(TZ) + timedelta(days=1)).strftime("%Y-%m-%d")
    slots = [
        {"time": "07:00", "title": "Tập chạy buổi sáng", "category": "exercise", "priority": 2, "remind": True},
        {"time": "09:00", "title": "Làm bài tập Toán", "category": "learning", "priority": 1, "remind": True},
        {"time": "14:00", "title": "Viết báo cáo tuần", "category": "work", "priority": 1, "remind": True},
        {"time": "18:00", "title": "Nghỉ + đi dạo", "category": "rest", "priority": 4, "remind": False},
        {"time": "20:00", "title": "Đọc sách", "category": "learning", "priority": 3, "remind": True},
    ]
    jid = job_create("morning_planning", payload={"slots": slots, "date": tomorrow,
                                                  "content": "Lịch mẫu để test duyệt."})
    return JSONResponse({"ok": True, "job_id": jid["id"], "date": tomorrow, "slots": slots})

@app.get("/debug-state")
async def debug_state():
    with db() as c:
        counts = {}
        for t in ["messages", "tasks", "memories", "jobs", "goals"]:
            try:
                counts[t] = c.execute(f"SELECT count(*) n FROM {t}").fetchone()["n"]
            except Exception:
                counts[t] = None
    return JSONResponse({
        "now": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S %Z"),
        "life_os_state": life_state_get(),
        "profile": {k: v for k, v in profile_get().items()},
        "app_config": {k: v for k, v in config_get().items() if k != "api_key_hint"},
        "counts": counts,
        "pending_jobs": len(job_list_pending()),
    })

@app.get("/debug-tasks")
async def debug_tasks():
    return JSONResponse({
        "pending": task_list("pending"),
        "pending_reminders": task_pending_reminders(),
        "today": task_list_today(),
        "scheduled_jobs": [{"id": j.id, "next_run": str(j.next_run_time)} for j in scheduler.get_jobs()],
    })


# ── Startup / Shutdown ────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    _restore_db_from_github_if_needed()
    db_init()
    push_sub_load()
    _reschedule_task_reminders()
    _register_cron_jobs()
    _register_backup_job()
    scheduler.start()

@app.on_event("shutdown")
async def shutdown():
    backup_memory_db(force=False)
    scheduler.shutdown()