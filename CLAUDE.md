# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A single-user PWA personal AI assistant ("Trợ lý AI"), in Vietnamese. A FastAPI backend serves a self-contained `index.html` SPA and proxies chat to an OpenAI-compatible endpoint, with **native function-calling tools** (time, task CRUD, reminders) and an agent loop. Conversations, tasks, and reminders are persisted in **SQLite** (`assistant.db`); only push subscriptions stay in RAM. The app is installable (manifest + service worker) and supports scheduled Web Push notifications.

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

There is no test suite, linter, or formatter configured. Verify changes by running the app and exercising the chat / settings / notification flows in a browser. Debug endpoints (`/debug-push`, `/debug-reminders`, `/debug-tools`, `/vapid-public-key`, `/test-push`) return JSON and are the primary way to diagnose push/reminder/tool issues without shell access.

## Architecture

**`main.py`** — the entire backend in one file:
- Serves `/` → `index.html`, **`/index.html`** (separate route — required because `sw.js` precaches it; a 404 here makes `caches.addAll` fail and the SW never installs, killing push silently), `/sw.js`, `/manifest.json`, and mounts `/static`.
- `POST /chat` — accepts `message`, `api_key`, `base_url`, `model` from the JSON body. The OpenAI client is constructed **per request** from the body (falling back to env vars). Runs an **agent loop** (max `MAX_TOOL_ROUNDS=6`): each turn the model may emit `tool_calls` (native function calling via `TOOL_SPECS`); `_run_tool()` executes them, appends `role:"tool"` results, and re-calls the model until it returns a plain reply. Conversation history is read from SQLite (`conv_recent(20)`). The `<reminder>` tag is kept as a **fallback** (when the model uses the tag instead of the `set_reminder` tool, or when the proxy strips `tools`) — its reply is appended with `✅ Đã đặt nhắc lúc …` / `⚠️ …`.
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

**Push data flow**: `.env` VAPID keys loaded via `python-dotenv` → at startup a `Vapid` object is built from the PEM private key → client fetches `GET /vapid-public-key` → `subscribeToNotifications()` calls `pushManager.subscribe({applicationServerKey})` → POSTs subscription to `/subscribe` → `send_push()` / scheduled reminders sign with the `Vapid` object and POST to each subscription's endpoint (Apple/Google/Mozilla push service) → `sw.js` `push` handler shows the notification. On iOS this only works when the app is installed to the Home Screen (standalone) on iOS 16.4+; the client guards for this.

**`index.html`** — the full SPA (markup + CSS + JS inline). Config (base URL, API key, model) is in `localStorage` (`cfg_baseurl` / `cfg_apikey` / `cfg_model`) and posted to `/chat` each message. The settings modal is the only way to enter credentials; `isConfigured()` gates sending. The 🔔 button fetches `/vapid-public-key` into `window.VAPID_PUBLIC_KEY`, subscribes, and only turns green after the subscription POST succeeds (earlier versions turned green prematurely, masking failures). SW registration auto-re-subscribes on page load if permission is granted and the app is standalone.

**`sw.js`** — service worker. Precaches `['/', '/index.html', '/manifest.json']` **per-file** (not `addAll` — a single 404 must not break install); cache version `assistant-v2`. Network-first fetch with cache fallback, plus `push` / `notificationclick` handlers (Vietnamese copy).

**`manifest.json`** — PWA manifest; theme `#0f0f0f`, `display: standalone`. Icons (`icon-192.png`, `icon-512.png`) are referenced but not present in the repo.

**Tools & storage** — `main.py` defines `TOOL_SPECS` (OpenAI function-calling schemas) + `TOOL_FUNCTIONS` (name→callable) + `_run_tool()` dispatcher. Phase-1 tools: `get_current_time`, `create_task`, `list_tasks`, `complete_task`, `update_task`, `delete_task`, `set_reminder`. Each tool returns a **string** fed back to the model as a `role:"tool"` message. State lives in SQLite (`assistant.db` next to `main.py`): tables `tasks`, `reminders`, `conversations`; `db_init()` runs on startup. `chat_history`/`reminders` lists were replaced by `conv_recent()`/`reminder_*()` helpers. `set_reminder` (and the `<reminder>` tag fallback) persist a row via `schedule_reminder_at()` and schedule an APScheduler job whose callback `send_push()`es then `reminder_mark_fired()`s. On startup `_reschedule_reminders()` re-registers unfired rows (APScheduler jobs are RAM and lost on restart) — rows whose time has already passed are marked fired silently. `push_subscriptions` is **still RAM** (single-worker constraint still applies for push). Weather / web search / `create_plan` are deferred to a later phase.

## Configuration / env vars

Read by `main.py` via `os.getenv` with defaults (`.env` auto-loaded by `python-dotenv`, which searches both cwd and the directory of `main.py`); all are optional for the server to boot but push needs the VAPID pair (a key pair is generated into `.env` by default):
- `OPENAI_API_KEY`, `OPENAI_BASE_URL` (default `https://api.vilao.ai/v1`) — server-side fallback only; runtime values come from the client.
- `VAPID_PRIVATE_KEY`, `VAPID_PUBLIC_KEY`, `VAPID_EMAIL` (default `mailto:you@example.com`).

VAPID keys are a P-256 EC pair: private key as PKCS8 PEM, public key as the uncompressed point (65 bytes, first byte `0x04`) base64url-encoded. In `.env` the multi-line PEM **must be wrapped in double quotes with `\n` escapes** so `python-dotenv` parses it into real newlines; an unquoted/broken PEM makes `Vapid.from_pem()` fail at startup (visible as `[vapid] Không nạp được PEM private key` in the log and `_VAPID_INSTANCE` stays `None`). To regenerate, run a `cryptography` script (P-256, PKCS8 PEM private + X962 uncompressed public) and overwrite the two `VAPID_*` lines. `.env` is gitignored. **Changing the key pair invalidates existing subscriptions** (push service returns 410/403) — users must re-tap 🔔 to subscribe with the new public key.

## Gotchas

- **Tasks/reminders/history are in SQLite (`assistant.db`)** and survive restart; `_reschedule_reminders()` re-arms unfired reminders on startup. **Push subscriptions are still in-memory** and are lost on restart — after every restart the client must re-open the app and tap 🔔 to re-subscribe, otherwise pushes/reminder delivery silently fail (`send_push` reports `skipped: chưa có subscription`).
- **Run a single uvicorn worker.** With `--workers >1` or gunicorn multi-worker, `/subscribe` and `/test-push` hit different processes and subscriptions appear missing. (Tasks/reminders/history are now SQLite so they would survive multi-worker, but push subs and in-flight APScheduler jobs would still split.)
- **The OpenAI-compatible proxy must pass the `tools` parameter.** Some proxies strip `tools`/`tool_calls`; then the agent loop degrades to plain chat and only the `<reminder>` tag fallback works. Hit `GET /debug-tools` to confirm `supports_tools: true`. If false, switch `base_url`/`model` (set in the app's Settings) before relying on tools.
- **Tool-calling models vary in reliability.** Haiku-4-5 handles a single simple tool fine but struggles with multi-step (search→plan→create several tasks). For real "executive assistant" multi-step, expect to move to a Sonnet-4.6 / GPT-4o-class model. `MAX_TOOL_ROUNDS=6` caps the loop to avoid runaway cost; if exceeded, the last assistant text is returned (or a `⚠️ Quá nhiều bước` fallback).
- **The model should call `set_reminder` for reminders**, but may instead emit the `<reminder>` tag. Both are handled (`extract_reminder` is the fallback). If `reminders_in_db` in `/debug-reminders` is empty after a reminder request AND `supports_tools` is true, the prompt/model is the cause, not the scheduler.
- **pywebpush `from_string` cannot parse a PEM string.** Passing the PEM directly to `webpush(vapid_private_key=<PEM string>)` makes pywebpush call `Vapid.from_string()`, which strips newlines but keeps the `-----BEGIN…-----` headers, base64-decodes garbage, and throws `ValueError: Could not deserialize key data… ASN.1 parsing error: invalid length`. The fix in place: build a `Vapid` object via `Vapid.from_pem()` at startup and pass the object (pywebpush then takes the `isinstance(Vapid01)` branch). Do not revert to passing the string.
- **Service worker install breaks on any precache 404.** `caches.addAll([...])` is atomic — if any listed URL 404s, install fails, `navigator.serviceWorker.ready` hangs forever, and `pushManager.subscribe()` never runs (no error, button never turns green). That's why `/index.html` has its own route and `sw.js` precaches per-file. If push silently does nothing, check server logs for a 404 on a precached asset.
- **The model must emit the `<reminder>` tag** (or call `set_reminder`) for reminders to work; some models leak chain-of-thought ("I need to return a reminder in JSON format…") instead. The system prompt is templated with the current `Asia/Shanghai` time/date so the model can compute relative times and default dates.
- No auth — anyone who can reach the server can drive it and register push subscriptions. Treat as a personal/local app.
- `httpx` is pinned `<0.28` for compatibility with the pinned `openai==1.14.3`. `python-dotenv` and `py_vapid` (transitive via `pywebpush`) round out the deps.
- Default model string in `/chat` is `krr/claude-haiku-4-5-20251001` (a provider-specific ID), only used when the client omits `model`.
