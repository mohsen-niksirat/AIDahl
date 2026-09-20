# AIDahl — Telegram AI Bot (Dahl + multi-provider BYOK + Manus Agent)

OpenAI-compatible Telegram assistant with **BYOK** (users bring their own API keys), plus optional **Manus Agent** mode for image/edit/research tasks.

> Docs: [English](#english) · [فارسی](#persian) · [آموزش کامل فارسی](docs/SETUP_FA.md) · [العربية](#arabic) · [Русский](#russian)

**Full setup tutorial (FA):** [docs/SETUP_FA.md](docs/SETUP_FA.md)  
**Privacy:** [privacy_policy.md](privacy_policy.md)  
**Prompts gallery:** https://mohsen-niksirat.github.io/promptopia/  
**Release:** `1.0.0-final` (`BOT_VERSION`)

---

## English

### Features
- Multi-turn chat history (configurable context length)
- **Settings**: provider/model, reply format (HTML / Markdown / plain), history size, streaming
- **BYOK**: `/keys` — Dahl, OpenRouter, Groq, Cerebras, SiliconFlow, Mistral, GitHub Models, custom, **Manus**
- **Streaming** replies (live Telegram message updates)
- **Lifetime + daily token quotas** with near-limit warnings
- **Storage hygiene**: only the **active** conversation stays in Supabase; new chat purges old server-side history
- **Manus Agent** (`/manus`) — create/edit images via official Manus API v2; **multi-turn follow-up** on the same task; **concurrency queue** for free-tier RAM
- **Promptopia** button — open ready-made prompts in the browser (`/prompts`)
- **Admin stats** `/admin`
- **Debug tools**: `/manusdebug`, `/manusreset`

### Stack
| Piece | Choice |
|--------|--------|
| Bot | Python 3.10+, `pyTelegramBotAPI` |
| HTTP | `httpx` only (Deployka free ~128 MB friendly) |
| DB | Supabase PostgreSQL via PostgREST |
| Chat models | OpenAI-compatible `POST /v1/chat/completions` (+ stream) |
| Manus | REST `https://api.manus.ai/v2` (`task.create` / `task.sendMessage` / poll) |

### Quick start
1. Telegram bot → [@BotFather](https://t.me/BotFather) → `BOT_TOKEN`
2. [Supabase](https://supabase.com) project → URL + key
3. Optional shared Dahl key: [inference.dahl.global/account](https://inference.dahl.global/account)
4. Configure env:

```bash
cp .env.example .env
```

5. Run SQL **in order** in Supabase SQL Editor:
   - `sql/01_user_api_keys.sql`
   - `sql/02_user_settings.sql`
   - `sql/03_phase5.sql`
   - `sql/04_storage_lifetime.sql`
6. Run locally:

```bash
pip install -r requirements.txt
python main.py
```

7. **Deployka**: deploy `main.py` + set env (include `ALLOW_SHARED_KEY` if the host requires it).  
8. BotFather `/setcommands` — see [docs/SETUP_FA.md](docs/SETUP_FA.md) §2 for the full list.

### Bot commands (BotFather `/setcommands`)
```text
start - شروع و منوی اصلی
help - راهنما
settings - تنظیمات (مدل، فرمت، استریم، تاریخچه)
keys - کلیدهای API شخصی (BYOK)
setkey - همان keys — ثبت کلید
byok - همان keys
manus - دستیار Manus (تصویر/ویرایش/agent)
agent - همان manus
manusqueue - وضعیت صف Manus
queue - همان manusqueue
manusreset - پاک‌سازی قفل/صف Manus
manusdebug - عیب‌یابی Manus
prompts - پرامپت‌های آماده (مرورگر)
promptopia - همان prompts
gallery - همان prompts
chats - لیست و تعویض گفتگوها
chatlist - همان chats
clear - گفتگوی جدید (ریست context)
newchat - همان clear
admin - آمار ادمین
cancel - لغو عملیات جاری
```

### Manus agent mode
Manus is **not** an OpenAI chat provider. The bot:
1. `POST /v2/task.create` (new chat) or `POST /v2/task.sendMessage` (follow-up on same task)
2. Polls `task.listMessages` (+ deep file harvest + task.detail)
3. Downloads media **with** `x-manus-api-key` and sends **photo preview + original file** to Telegram

**Get a key:** [manus.im/app#settings/developers](https://manus.im/app#settings/developers) → Create API Key (shown once).

**Follow-up edits:** after each result, send the next photo/text — it continues the **same** Manus task (not a new chat). `/manus` → new task; buttons to end follow-up mode.

**Queue:** `MANUS_MAX_CONCURRENT` (default 2) + wait queue so free-tier Nano does not OOM. Status: `/manusqueue`. Stuck locks: `/manusreset`.

**Env:** `MANUS_API_KEY` (optional shared), `MANUS_LOCALE` (leave empty; **do not** send invalid codes like `fa`), `MANUS_TIMEOUT_SEC`, `MANUS_FOLLOWUP_TTL_SEC`, `MANUS_STALE_LOCK_SEC`.

Users can send **photo + caption** for image edit prompts (inline base64 to Manus).

### Storage policy (DB hygiene)
- **Only the current conversation** (last ~40 messages) stays in Supabase.
- Starting a **new chat** deletes previous conversations/messages from the server.
- Telegram chat history is **not** deleted — it stays in the user’s Telegram app.
- `usage` table is **not written** by default (`SKIP_USAGE_TABLE=true`); quotas live in `user_quotas`.
- **Lifetime token cap** per user (default **3,000,000**) + warn at **75%** (`LIFETIME_WARN_PCT`).
- Daily shared-key quota remains (default 50k/day). Lifetime cap applies to all users.

Run SQL: `sql/03_phase5.sql` then `sql/04_storage_lifetime.sql`.

### Capacity (free tier rough estimate)

| Constraint | Typical free limit | Effect |
|------------|-------------------|--------|
| Deployka Free Nano | **~128 MB RAM**, 1 container | Baseline ~30–40 MB; Manus queue required |
| Supabase Free | ~500 MB DB | Hygiene policy keeps DB small |
| Manus API | ~**10 task.create/min** per account | Bot queue + per-user 1 job |
| Telegram | Flood limits | Streaming OK per chat |

**Rough concurrency:** ~**5–10** simultaneous text chats safe; ~**2–5** Manus jobs at once with queue; ~**50–150** light daily actives.

See capacity details (FA): [docs/SETUP_FA.md](docs/SETUP_FA.md)

### Environment
See [`.env.example`](.env.example).

### Database
`users`, `conversations` (active only), `messages` (trimmed), `user_api_keys`, `user_quotas`  
SQL: `sql/01` → `02` → `03` → `04`.

### Security
- Never commit `.env`. Rotate leaked tokens.
- BYOK keys are **masked** in UI.
- Bot tables are backend-only — protect `SUPABASE_KEY`.
- Multi-account farming of free quotas may violate provider ToS.
- Privacy: [privacy_policy.md](privacy_policy.md)

---

## Persian

### امکانات
- چت چندنوبتی + استریم  
- **BYOK** + **Manus** (تصویر/ادیت) + **صف همزمانی**  
- **ادامه ادیت در همان گفتگوی Manus**  
- **سهمیه کلی + روزانه** با هشدار  
- **فقط گفتگوی فعلی** روی سرور (پاک‌سازی با چت جدید)  
- **پرامپت‌های آماده** (`/prompts`)  
- `/manusdebug` `/manusreset`  

### راه‌اندازی
**[docs/SETUP_FA.md](docs/SETUP_FA.md)** — SQL 01 تا 04 + BotFather + Deployka  

### دستورات
`/start` `/settings` `/keys` `/manus` `/prompts` `/chats` `/clear` `/admin` `/manusdebug`

---

## Arabic
Quick start: English Quick start + SQL `01`–`04`. Full guide (FA): [docs/SETUP_FA.md](docs/SETUP_FA.md).  
Privacy: [privacy_policy.md](privacy_policy.md).

## Russian
Quick start: см. English. SQL `01`–`04`. Guide (FA): [docs/SETUP_FA.md](docs/SETUP_FA.md).  
Privacy: [privacy_policy.md](privacy_policy.md).

---

## Repository layout

```
AIDahl/
├── main.py
├── requirements.txt
├── .env.example
├── .gitignore
├── privacy_policy.md
├── docs/
│   └── SETUP_FA.md
├── sql/
│   ├── 01_user_api_keys.sql
│   ├── 02_user_settings.sql
│   ├── 03_phase5.sql
│   └── 04_storage_lifetime.sql
└── README.md
```

## Links
- Dahl: https://inference.dahl.global · Docs: https://inference.dahl.global/docs/  
- Manus API: https://open.manus.ai/docs · Key: https://manus.im/app#settings/developers  
- Prompts: https://mohsen-niksirat.github.io/promptopia/  
- Supabase: https://supabase.com  

## License
Use at your own risk. Respect each provider’s terms.
