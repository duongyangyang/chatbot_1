# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A single-user PWA personal AI life & work assistant ("Trợ lý AI" / "Life OS"), in Vietnamese. A FastAPI backend serves a self-contained `index.html` SPA and proxies chat to an OpenAI-compatible endpoint, with **native function-calling tools** (time, task CRUD, events, goals, journals, memory search, schedule generation/approval, reviews, profile updates). Conversations, tasks, events, goals, journals, memory, reports, and schedules are persisted in **SQLite** (`memory.db`); only push subscriptions stay in RAM. The app is installable (manifest + service worker), supports scheduled Web Push notifications, and runs **proactive scheduled jobs** (morning planning, daily/weekly/monthly review) that call the LLM server-side, store a report, push a nudge, and surface in-app on next open. A `/db` viewer lets the (non-technical) user read the database in a browser.

## Tổng kết nhanh (bản hiện tại)

Một assistant AI cá nhân dạng PWA, giao tiếp hoàn toàn bằng chat tiếng Việt, có trí nhớ dài hạn và vòng lặp **Mục tiêu → Kế hoạch → Thực hiện → Đánh giá → Điều chỉnh**. Phạm vi MVP đã hoàn thành:

- **Chat tự nhiên** + agent loop function-calling (`MAX_TOOL_ROUNDS=8`), lịch sử lưu SQLite.
- **Task** (CRUD + priority/deadline), **Event** (lịch hẹn, tự đặt nhắc push), **Goal** phân tầng năm→tháng→tuần (qua `parent_id`).
- **Morning Planning**: model gọi `generate_schedule` → lưu nháp → frontend render card **Duyệt/Sửa** (marker `<<<SCHEDULE id="..">>>`). Chỉ khi user bấm Duyệt mới tạo events + nhắc (human-in-the-loop).
- **Daily/Weekly/Monthly Review**: 4 cron job chạy server-side, sinh báo cáo → lưu `reports` → push nudge → hiện trong app khi mở (`/pending` + `loadPending`).
- **Memory**: nhật ký (mood/tags), long-term memory, `search_memory` (LIKE, không vector DB), `user_profile` + `life_os_state` lưu DB sửa qua chat.
- **Push notification** (Web Push/VAPID), **PWA** installable, **DB viewer** `/db`.
- **Không có**: multi-user, vector DB, agent framework, RAG phức tạp, web search/weather (deferred).

Trạng thái xác minh: đã test tool dispatch, DB, routes, cron registration, masking; **chưa test luồng LLM thật** (không có API key thật trên máy dev). Khi set key thật + chat 1 lần → chạy `/debug-proactive?job=morning_planning&dry=true` để confirm.

## DB schema (`memory.db`)

| Bảng | Cột chính | Ghi chú |
|---|---|---|
| `tasks` | id, title, note, due_at, priority, status, created_at | open/done |
| `reminders` | id, run_at, message, fired, source | source ∈ manual/event/schedule; mọi nhắc (kể cả event/schedule) đều ở đây |
| `conversations` | id, role, content, created_at | lịch sử chat, `conv_recent(20)` |
| `events` | id, title, note, start_at, end_at, location, all_day, status, source, schedule_id, created_at | sự kiện/lịch hẹn |
| `goals` | id, title, description, level, period, parent_id, status, progress_note, created_at | level ∈ year/month/week |
| `journals` | id, mood, content, tags, created_at | nhật ký + tâm trạng |
| `long_term_memory` | id, content, confidence, source, created_at, last_used_at | `add_journal` tự lưu 1 row |
| `user_profile` | id=1, ten, nghe, so_thich, khu_vuc, ghi_chu, updated_at | single-row, sửa qua `update_profile` |
| `life_os_state` | id=1, current_life_phase, top_3_priorities, current_risks, updated_at | single-row, đọc đầu tiên khi planning/review |
| `reports` | id, type, period, content, schedule_id, read, pushed, created_at | output proactive, unread→`/pending` |
| `schedules` | id, date, slots(JSON), status, created_at, approved_at | draft → approved qua `/approve-schedule` |
| `app_config` | id=1, base_url, model, api_key_hint, updated_at | last-used config; **api_key_hint CHE ở mọi GET** (`***`) |

`db_init()` tạo bảng + seed 3 single-row (user_profile, life_os_state, app_config) trên startup.

## Routes

| Method | Path | Mục đích |
|---|---|---|
| GET | `/`, `/index.html`, `/sw.js`, `/manifest.json` | SPA + service worker + manifest |
| POST | `/chat` | chat chính (agent loop, persist config, parse schedule marker) |
| POST | `/subscribe` | đăng ký Web Push subscription (RAM) |
| GET | `/vapid-public-key` | public key cho `pushManager.subscribe` |
| GET | `/test-push` · POST `/test-push-delayed` | gửi/thử push |
| POST | `/approve-schedule` | user bấm Duyệt → tạo events + nhắc |
| GET | `/pending` · POST `/pending/read` | báo cáo chưa đọc + đánh dấu đã xem |
| GET | `/db` · `/debug-db` | xem DB (HTML / JSON, che API key) |
| GET | `/debug-push` `/debug-tools` `/debug-reminders` `/debug-proactive` `/debug-state` · `/vapid-public-key` | debug (JSON) |

`/debug-proactive?job=<morning_planning|daily_review|weekly_review|monthly_review>&dry=true` — chạy job ngay không push, cách chính test proactive.

## Tools (function-calling, 20)

| Nhóm | Tools |
|---|---|
| Thời gian | `get_current_time` |
| Task | `create_task`, `list_tasks`, `complete_task`, `update_task`, `delete_task` |
| Event/Reminder | `create_event` (tự đặt nhắc), `list_events`, `set_reminder` |
| Goal | `create_goal`, `list_goals`, `update_goal` |
| Journal/Memory | `add_journal`, `search_memory` (LIKE qua memory+journal+chat) |
| Life OS | `get_life_state` (đọc đầu), `update_profile` |
| Lịch | `generate_schedule` (lưu nháp, trả id); `apply_schedule` **không** là tool — chỉ UI |
| Review | `daily_review`, `weekly_review`, `monthly_review` (trả context bundle) |

Mỗi tool trả **string** đút lại role `tool`. `_run_tool` truyền hết args nếu fn có `**kwargs`.

## Commands

```bash
# Cài deps. Lưu ý: .venv trong repo là Python 3.9, nhưng fastapi==0.138.1
# yêu cầu Python >=3.10 → phải tạo venv bằng Python 3.10+ trên server thật.
source .venv/bin/activate
pip install -r requirements.txt

# Chạy server. PHẢI dùng đúng 1 worker (mặc định) — state lưu trong RAM,
# nhiều worker sẽ làm /subscribe và /test-push rơi vào worker khác nhau.
uvicorn main:app --host 0.0.0.0 --port 8000
```

There is no test suite, linter, or formatter configured. Verify changes by running the app and exercising the chat / settings / notification flows in a browser. Debug endpoints (`/debug-push`, `/debug-reminders`, `/debug-tools`, `/debug-state`, `/debug-proactive`, `/debug-db`, `/vapid-public-key`, `/test-push`) return JSON and are the primary way to diagnose push/reminder/tool/proactive issues without shell access. `/db` renders the whole `memory.db` as HTML (read-only, API key masked).

## Architecture

**`main.py`** — the entire backend in one file:
- Serves `/` → `index.html`, **`/index.html`** (separate route — required because `sw.js` precaches it; a 404 here makes `caches.addAll` fail and the SW never installs, killing push silently), `/sw.js`, `/manifest.json`, and mounts `/static`.
- `POST /chat` — accepts `message`, `api_key`, `base_url`, `model` from the JSON body. The OpenAI client is constructed **per request** from the body (falling back to env vars). **Persists the supplied `api_key`/`base_url`/`model` into the `app_config` row** so scheduled (server-side) jobs can call the LLM without a request — the key is stored server-side and **never returned by any GET endpoint**. Runs an **agent loop** (max `MAX_TOOL_ROUNDS=8`): each turn the model may emit `tool_calls` (native function calling via `TOOL_SPECS`); `_run_tool()` executes them (handles `**kwargs` tools by passing all args when the signature has VAR_KEYWORD), appends `role:"tool"` results, and re-calls the model until it returns a plain reply. Conversation history is read from SQLite (`conv_recent(20)`). After the reply, two post-processing passes run: (1) the `<reminder>` tag fallback (when the model uses the tag instead of `set_reminder`, or the proxy strips `tools`); (2) the **schedule-approval marker** `<<<SCHEDULE id="<id>">>>` — the model emits this after calling `generate_schedule`; `/chat` parses it, looks up the `schedules` row, strips the marker from the displayed text, and returns `{"reply", "schedule": {id, date, slots}}` so the frontend renders a Duyệt/Sửa card. The `<reminder>` tag is appended with `✅ Đã đặt nhắc lúc …` / `⚠️ …`.
- `POST /subscribe` — stores a Web Push subscription in the in-memory `push_subscriptions` list.
- `GET /vapid-public-key` — returns the VAPID public key (base64url) for the browser's `pushManager.subscribe({applicationServerKey})`.
- `GET /test-push` — sends one push immediately and returns a per-subscriber summary `{sent, failed, skipped}` (errors logged as `[push] …`).
- `POST /test-push-delayed` — schedules a push N seconds out (default 30) via APScheduler; used by the in-app "Test thông báo" button.
- `GET /debug-push` — `{vapid_public_set, vapid_public_valid, vapid_private_set, vapid_keys_match, vapid_email, subscribers}`.
- `GET /debug-tools` — sends a minimal message with `TOOL_SPECS` and reports `supports_tools` (whether the model/proxy returned `tool_calls`). Use this to verify the OpenAI-compatible endpoint passes the `tools` param through — some proxies strip it, in which case the agent loop degrades to plain chat and only the `<reminder>` tag fallback works.
- `GET /debug-reminders` — current time, `reminders_in_db` (recent rows from SQLite), and `scheduled_jobs` (APScheduler `next_run_time`).
- **Reminder flow**: the system prompt (templated with the current `Asia/Shanghai` time/date each request) instructs the model to emit a `<reminder>{"time","date","message"}</reminder>` tag. `extract_reminder()` finds the first `{…}` JSON inside the tag (tolerant of markdown/prose), `schedule_reminder()` registers an APScheduler one-shot in `Asia/Shanghai` that fires `send_push("⏰ Nhắc nhở", msg)`, and returns a status dict.
- **Push delivery**: `send_push()` uses `pywebpush`. The VAPID private key is loaded **once at startup** into a `Vapid` object via `Vapid.from_pem()` and passed as an object to `webpush(vapid_private_key=_VAPID_INSTANCE)`. Do NOT pass the PEM string directly — see Gotchas. 410/404-Gone subscriptions are pruned.
- Scheduler starts/stops on FastAPI `startup`/`shutdown` events.
- **Proactive jobs (Life OS loop)**: `_register_cron_jobs()` (called from `startup`) arms 4 APScheduler cron jobs in `Asia/Shanghai`: `morning_planning` 07:30, `daily_review` 22:00, `weekly_review` Sun 21:00, `monthly_review` last-day 21:00. Each calls `_run_proactive_job(job_type, dry=False)` → `_server_client()` builds an `OpenAI` client from the `app_config` row (last-used key/url/model, env fallback; returns `(None,None)` if no key → job logs `[proactive] ... skip` and pushes nothing) → `_build_context_bundle()` gathers open tasks, events (7d), active goals, last 7 journals, life_os_state, last 10 conversations → a constrained agent loop (max 4 rounds, same `TOOL_SPECS`) runs with a per-job directive → result is stored as a `reports` row (`read=0`) and, unless `dry`, `send_push()` a short nudge and `report_mark_pushed()`. Morning planning expects the model to call `generate_schedule` (stores a `schedules` draft) and emit the `<<<SCHEDULE id="..">>>` marker; the report row links `schedule_id`. **`GET /debug-proactive?job=<type>&dry=true` runs a job synchronously now without pushing — the primary way to test proactive without waiting for the cron time.**
- **Pending surfacing**: `GET /pending` returns unread `reports` (and, for `morning_planning` rows whose `schedule_id` is still `draft`, the linked slots). `index.html` `loadPending()` fetches on load and prepends an AI-style card (review text, or a schedule Duyệt/Sửa card for planning) with a "Đã xem" button → `POST /pending/read {id}` marks `read=1`.
- **Schedule approval (human-in-the-loop)**: `POST /approve-schedule {schedule_id}` → `schedule_approve()` marks the `schedules` row `approved`, creates an `events` row per slot (`source='schedule'`, `schedule_id`), and arms a `reminders` job 15 min before each slot. `apply_schedule` is intentionally **not** in `TOOL_SPECS` — the model must not self-approve; only the UI button triggers it.
- `GET /db` renders all tables of `memory.db` as read-only HTML (API key masked to `***`); `GET /debug-db?table=&limit=` returns one table as JSON. `_mask_row()` hides `api_key_hint`.

**Push data flow**: `.env` VAPID keys loaded via `python-dotenv` → at startup a `Vapid` object is built from the PEM private key → client fetches `GET /vapid-public-key` → `subscribeToNotifications()` calls `pushManager.subscribe({applicationServerKey})` → POSTs subscription to `/subscribe` → `send_push()` / scheduled reminders sign with the `Vapid` object and POST to each subscription's endpoint (Apple/Google/Mozilla push service) → `sw.js` `push` handler shows the notification. On iOS this only works when the app is installed to the Home Screen (standalone) on iOS 16.4+; the client guards for this.

**`index.html`** — the full SPA (markup + CSS + JS inline). Config (base URL, API key, model) is in `localStorage` (`cfg_baseurl` / `cfg_apikey` / `cfg_model`) and posted to `/chat` each message. The settings modal is the only way to enter credentials; `isConfigured()` gates sending. The 🔔 button fetches `/vapid-public-key` into `window.VAPID_PUBLIC_KEY`, subscribes, and only turns green after the subscription POST succeeds (earlier versions turned green prematurely, masking failures). SW registration auto-re-subscribes on page load if permission is granted and the app is standalone. **AI replies are rendered as markdown** via a small inline `renderMarkdown()` (headings, bold/italic, lists, inline + block code, links) — no dep. **`/chat` also returns a `trace` array** (`{kind:"thought"|"tool", name, args, result}`) which `renderTrace()` shows as collapsible 🔧 tool-call chips + 💭 thought lines (like Claude/Codex); the API doesn't expose reasoning tokens, so "thinking" = the visible tool-call sequence + any assistant text emitted between calls.

**`sw.js`** — service worker. Precaches `['/', '/index.html', '/manifest.json']` **per-file** (not `addAll` — a single 404 must not break install); cache version `assistant-v4`. Network-first fetch with cache fallback, plus `push` / `notificationclick` handlers (Vietnamese copy). Bump the version whenever `index.html`/`sw.js` change so clients invalidate the stale SW.

**`manifest.json`** — PWA manifest; theme `#0f0f0f`, `display: standalone`. Icons (`icon-192.png`, `icon-512.png`) are referenced but not present in the repo.

**Tools & storage** — `main.py` defines `TOOL_SPECS` (OpenAI function-calling schemas) + `TOOL_FUNCTIONS` (name→callable) + `_run_tool()` dispatcher. Tools: `get_current_time`, `create_task`, `list_tasks`, `complete_task`, `update_task`, `delete_task`, `set_reminder`, `create_event`, `list_events`, `create_goal`, `list_goals`, `update_goal`, `add_journal`, `search_memory`, `get_life_state`, `update_profile`, `generate_schedule`, `daily_review`, `weekly_review`, `monthly_review`. Each tool returns a **string** fed back to the model as a `role:"tool"` message. `apply_schedule` is a function but **not** a tool (UI-only). State lives in SQLite (`memory.db` next to `main.py`): tables `tasks`, `reminders` (with `source`), `conversations`, `events`, `goals` (year/month/week + `parent_id`), `journals`, `long_term_memory`, `user_profile` (single-row id=1, replaces the old hardcoded `USER_PROFILE` dict; editable via `update_profile`), `life_os_state` (single-row, read-first for planning/review), `reports`, `schedules`, `app_config` (single-row, last-used client config incl. server-side API key). `db_init()` runs on startup and seeds the single-row tables. `set_reminder`/`create_event`/`schedule_approve` persist reminder rows via `schedule_reminder_at(source=...)` and schedule an APScheduler job whose callback `send_push()`es then `reminder_mark_fired()`s. On startup `_reschedule_reminders()` re-registers unfired rows (APScheduler jobs are RAM and lost on restart) — rows whose time has already passed are marked fired silently; `_register_cron_jobs()` re-arms the 4 proactive cron jobs (also RAM). `search_memory` does SQL `LIKE` across `long_term_memory` + `journals` + `conversations` (no vector DB). `push_subscriptions` is **still RAM** (single-worker constraint still applies for push). Weather / web search are deferred.

## Configuration / env vars

Read by `main.py` via `os.getenv` with defaults (`.env` auto-loaded by `python-dotenv`, which searches both cwd and the directory of `main.py`); all are optional for the server to boot but push needs the VAPID pair (a key pair is generated into `.env` by default):
- `OPENAI_API_KEY`, `OPENAI_BASE_URL` (default `https://api.vilao.ai/v1`) — server-side fallback only; runtime values come from the client.
- `VAPID_PRIVATE_KEY`, `VAPID_PUBLIC_KEY`, `VAPID_EMAIL` (default `mailto:you@example.com`).

VAPID keys are a P-256 EC pair: private key as PKCS8 PEM, public key as the uncompressed point (65 bytes, first byte `0x04`) base64url-encoded. In `.env` the multi-line PEM **must be wrapped in double quotes with `\n` escapes** so `python-dotenv` parses it into real newlines; an unquoted/broken PEM makes `Vapid.from_pem()` fail at startup (visible as `[vapid] Không nạp được PEM private key` in the log and `_VAPID_INSTANCE` stays `None`). To regenerate, run a `cryptography` script (P-256, PKCS8 PEM private + X962 uncompressed public) and overwrite the two `VAPID_*` lines. `.env` is gitignored. **Changing the key pair invalidates existing subscriptions** (push service returns 410/403) — users must re-tap 🔔 to subscribe with the new public key.

## Gotchas

- **Tasks/reminders/goals/journals/reports/schedules are in SQLite (`memory.db`)** and survive restart; `_reschedule_reminders()` re-arms unfired reminders (including event + schedule reminders, all live in the `reminders` table) on startup, and `_register_cron_jobs()` re-arms the 4 proactive cron jobs. **Push subscriptions are still in-memory** and are lost on restart — after every restart the client must re-open the app and tap 🔔 to re-subscribe, otherwise pushes/reminder/proactive-nudge delivery silently fail (`send_push` reports `skipped: chưa có subscription`). The old `assistant.db` is abandoned (fresh start) — do not rely on it.
- **Proactive jobs need a server-side API key.** Scheduled jobs run without a request, so they use the `app_config` row (last-used config persisted by `/chat`). Until the user has chatted at least once (or `OPENAI_API_KEY` is set in `.env`), `/debug-proactive?job=...` returns `error: chưa có API key` and the cron jobs no-op. Verify with `/debug-state` → `app_config`.
- **Run a single uvicorn worker.** With `--workers >1` or gunicorn multi-worker, `/subscribe` and `/test-push` hit different processes and subscriptions appear missing. (SQLite state survives multi-worker, but push subs, in-flight APScheduler jobs, **and the 4 proactive cron jobs** would each split across workers → duplicate nudges/reports.)
- **The OpenAI-compatible proxy must pass the `tools` parameter.** Some proxies strip `tools`/`tool_calls`; then the agent loop degrades to plain chat, the schedule marker is never produced, and only the `<reminder>` tag fallback works. The expanded `TOOL_SPECS` (schedule `slots` array, goal `level` enum) makes stripping more likely — re-run `GET /debug-tools` after changes. If `supports_tools:false`, switch `base_url`/`model` before relying on tools.
- **Tool-calling models vary in reliability.** Haiku-4-5 handles a single simple tool fine but struggles with multi-step (morning planning needs get_life_state→list_goals/tasks→generate_schedule, 3-4 rounds). For real "executive assistant" multi-step, expect to move to a Sonnet-4.6 / GPT-4o-class model. `MAX_TOOL_ROUNDS=8` caps the chat loop; the proactive loop is capped at 4. If exceeded, the last assistant text is returned (or a `⚠️ Quá nhiều bước` fallback). If morning_planning's model fails to call `generate_schedule`, the job still stores a `reports` row with text (no Duyệt card).
- **The schedule marker is load-bearing.** The model must call `generate_schedule` (which returns the `id`) and then emit `<<<SCHEDULE id="<id>">>>` in its reply. `/chat` strips the marker and returns `data.schedule`; if the model emits the marker without calling the tool, the lookup fails, `data.schedule` is null, and the raw marker is stripped from the displayed text (no card). The frontend never parses the marker.
- **The model should call `set_reminder` for reminders**, but may instead emit the `<reminder>` tag. Both are handled (`extract_reminder` is the fallback). If `reminders_in_db` in `/debug-reminders` is empty after a reminder request AND `supports_tools` is true, the prompt/model is the cause, not the scheduler.
- **pywebpush `from_string` cannot parse a PEM string.** Passing the PEM directly to `webpush(vapid_private_key=<PEM string>)` makes pywebpush call `Vapid.from_string()`, which strips newlines but keeps the `-----BEGIN…-----` headers, base64-decodes garbage, and throws `ValueError: Could not deserialize key data… ASN.1 parsing error: invalid length`. The fix in place: build a `Vapid` object via `Vapid.from_pem()` at startup and pass the object (pywebpush then takes the `isinstance(Vapid01)` branch). Do not revert to passing the string.
- **Service worker install breaks on any precache 404.** `caches.addAll([...])` is atomic — if any listed URL 404s, install fails, `navigator.serviceWorker.ready` hangs forever, and `pushManager.subscribe()` never runs (no error, button never turns green). That's why `/index.html` has its own route and `sw.js` precaches per-file. If push silently does nothing, check server logs for a 404 on a precached asset.
- **The model must emit the `<reminder>` tag** (or call `set_reminder`) for reminders to work; some models leak chain-of-thought ("I need to return a reminder in JSON format…") instead. The system prompt is templated with the current `Asia/Shanghai` time/date so the model can compute relative times and default dates.
- No auth — anyone who can reach the server can drive it and register push subscriptions. Treat as a personal/local app.
- `httpx` is pinned `<0.28` for compatibility with the pinned `openai==1.14.3`. `python-dotenv` and `py_vapid` (transitive via `pywebpush`) round out the deps.
- Default model string in `/chat` is `krr/claude-haiku-4-5-20251001` (a provider-specific ID), only used when the client omits `model`.
