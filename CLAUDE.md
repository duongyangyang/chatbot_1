# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A single-user PWA personal AI assistant ("Trợ lý AI"), in Vietnamese. A FastAPI backend serves a self-contained `index.html` SPA and proxies chat to an OpenAI-compatible endpoint. Conversations, push subscriptions, and reminders live in process memory. The app is installable (manifest + service worker) and supports scheduled Web Push notifications.

## Commands

```bash
# Install deps into the existing .venv (Python 3.9)
source .venv/bin/activate
pip install -r requirements.txt

# Run the dev server (serves on http://127.0.0.1:8000)
uvicorn main:app --reload

# Run a single uvicorn worker without reload
uvicorn main:app
```

There is no test suite, linter, or formatter configured. Verify changes by running the app and exercising the chat / settings / notification flows in a browser.

## Architecture

**`main.py`** — the entire backend in one file:
- Serves `/` → `index.html`, `/sw.js`, `/manifest.json`, and mounts `/static`.
- `POST /chat` — accepts `message`, `api_key`, `base_url`, `model` from the JSON body. The OpenAI client is constructed **per request** from the body (falling back to env vars), so each user supplies their own credentials. Reply history is kept in the in-memory `chat_history` (capped at last 20 messages).
- `POST /subscribe` — stores a Web Push subscription object in `push_subscriptions`.
- `GET /vapid-public-key` — returns the server's VAPID public key (base64url) so the browser can call `pushManager.subscribe({applicationServerKey})`. Without this the client has no key and subscription silently fails.
- `GET /test-push` — manual smoke test: sends one push to all registered subscribers.
- **Reminder flow**: the system prompt instructs the model to emit a `<reminder>{"time","date","message"}</reminder>` JSON tag when the user asks to set one. `extract_reminder()` parses it, the tag is stripped from the visible reply, and `schedule_reminder()` registers an APScheduler one-shot job in `Asia/Ho_Chi_Minh` timezone that fires `send_push()`.
- `send_push()` uses `pywebpush` with VAPID env vars; 410-Gone subscriptions are pruned.
- Scheduler starts/stops on FastAPI `startup`/`shutdown` events.

**Push notification data flow**: server loads VAPID keys from env (`.env` via `python-dotenv`) → client fetches `GET /vapid-public-key` → `subscribeToNotifications()` calls `pushManager.subscribe({applicationServerKey})` → POSTs the subscription to `/subscribe` → server stores it → `send_push()` / scheduled reminders push via `pywebpush`. The `sw.js` `push` handler renders the notification. On iOS this only works when the app is installed to the Home Screen (standalone mode) and on iOS 16.4+; the client guards for this and instructs the user to "Add to Home Screen".

**`index.html`** — the full SPA (markup + CSS + JS inline). Config (base URL, API key, model) is stored in `localStorage` under `cfg_baseurl` / `cfg_apikey` / `cfg_model` and posted to `/chat` on each message. The settings modal is the only way to enter credentials; `isConfigured()` gates sending. Service worker registration + PushManager subscription live here. Note: push subscription references `window.VAPID_PUBLIC_KEY`, which must be injected (e.g. by templating `index.html`) for Web Push to actually subscribe.

**`sw.js`** — service worker: cache-first fallback for GET, plus `push` / `notificationclick` handlers (Vietnamese copy).

**`manifest.json`** — PWA manifest; theme `#0f0f0f`. Icons (`icon-192.png`, `icon-512.png`) are referenced but not present in the repo.

## Configuration / env vars

Read by `main.py` via `os.getenv` with defaults (`.env` auto-loaded by `python-dotenv`); all are optional for the server to boot but push notifications need the VAPID pair (a key pair is generated into `.env` by default — see below):
- `OPENAI_API_KEY`, `OPENAI_BASE_URL` (default `https://api.vilao.ai/v1`) — server-side fallback only; runtime values come from the client.
- `VAPID_PRIVATE_KEY`, `VAPID_PUBLIC_KEY`, `VAPID_EMAIL` (default `mailto:you@example.com`).

VAPID keys are a P-256 EC pair. The public key is the uncompressed point (65 bytes) base64url-encoded. To regenerate, run a small script using `cryptography` (P-256, PKCS8 PEM private + X962 uncompressed public). `.env` is gitignored.

## Gotchas

- All state (`chat_history`, `push_subscriptions`, `reminders`, scheduled jobs) is in-memory and lost on restart. Comments indicate Redis/Postgres as the intended future store.
- No auth — anyone who can reach the server can drive it and register push subscriptions. Treat as a personal/local app.
- `httpx` is pinned `<0.28` for compatibility with the pinned `openai==1.14.3`.
- Default model string in `/chat` is `krr/claude-haiku-4-5-20251001` (a provider-specific ID), only used when the client omits `model`.
