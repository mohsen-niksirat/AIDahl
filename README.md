# AIDahl — Telegram AI Bot (Dahl + multi-provider BYOK + Manus Agent)

OpenAI-compatible Telegram assistant with **BYOK** (users bring their own API keys), plus optional **Manus Agent** mode for image/edit/research tasks.

> Docs: [English](#english) · [فارسی](#persian) · [آموزش کامل فارسی](docs/SETUP_FA.md) · [العربية](#arabic) · [Русский](#russian)

**Full setup tutorial (FA):** [docs/SETUP_FA.md](docs/SETUP_FA.md)

---

## English

### Features
- Multi-turn chat history (configurable context length)
- **Settings**: provider/model, reply format (HTML / Markdown / plain), history size, streaming
- **BYOK**: `/keys` — Dahl, OpenRouter, Groq, Cerebras, SiliconFlow, Mistral, GitHub Models, custom, **Manus**
- **Streaming** replies (live Telegram message updates)
- **Daily token quota** per user
- **Multiple named conversations** (`/chats`)
- **Manus Agent mode** (`/manus`) — async tasks via official Manus API v2; text prompts + image/file edit
- **Admin stats** `/admin`
- Usage logged in Supabase

### Stack
| Piece | Choice |
|--------|--------|
| Bot | Python 3.10+, `pyTelegramBotAPI` |
| HTTP | `httpx` only (Deployka free ~128 MB friendly) |
| DB | Supabase PostgreSQL via PostgREST |
| Chat models | OpenAI-compatible `POST /v1/chat/completions` (+ stream) |
| Manus | REST `https://api.manus.ai/v2` (tasks, not chat completions) |

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
6. Run locally:

```bash
pip install -r requirements.txt
python main.py
```

7. **Deployka**: deploy `main.py` + set env (must include `ALLOW_SHARED_KEY` if the host requires it).

### Bot commands
| Command | Description |
|---------|-------------|
| `/start` | Main menu |
| `/settings` | Model, format, stream, history, chats |
| `/keys` | BYOK key management |
| `/manus` | Manus agent mode (images/edit/research) |
| `/manus <prompt>` | Run a Manus task immediately |
| `/chats` | List / switch / rename conversations |
| `/clear` `/newchat` | New conversation context |
| `/admin` | Admin dashboard |
| `/help` `/cancel` | Help / cancel pending input |

### Manus agent mode
Manus is **not** an OpenAI chat provider. The bot:
1. Calls `POST /v2/task.create` with `x-manus-api-key`
2. Polls `task.listMessages` until `stopped` / `waiting` / `error`
3. Sends result text + file/image URLs to Telegram

**Get a key:** [manus.im/app#settings/developers](https://manus.im/app#settings/developers) → Create API Key (shown once).

Optional env: `MANUS_API_KEY` (shared), `MANUS_LOCALE` (empty = omit; do not use invalid codes like `fa`).

Users can send **photo + caption** after “new task” for image edit prompts (inline base64 upload to Manus).

### Capacity (free tier rough estimate)

| Constraint | Typical free limit | Effect |
|------------|-------------------|--------|
| Deployka Free Nano | **~128 MB RAM**, 1 container | Python baseline ~30–40 MB; little headroom for concurrent jobs |
| Bot process | Single container, threaded telebot | OK for light chat; heavy Manus polls hold threads up to `MANUS_TIMEOUT_SEC` |
| Supabase Free | ~500 MB DB, shared compute, connection limits | PostgREST bursts are usually fine at low QPS; not the first bottleneck |
| Telegram | Flood limits on send/edit | Streaming edits every ~1.2 s are OK per chat; many chats → rate limits |
| Dahl / BYOK | Provider-specific | Shared key exhausts fast; **BYOK** spreads load |
| Manus API | **~10 `task.create`/min per user account** | Concurrent image jobs are the tightest limit |

**Rough concurrent users without crashing (this stack):**
- **Safe / sustained:** ~**5–10** simultaneous text chats  
- **Occasional spikes:** ~**15–25** if mostly short messages, streaming OK, few Manus jobs  
- **With Manus heavy use:** ~**2–5** Manus jobs at once on one key + a few text chats  
- **Comfortable daily actives:** ~**50–150** light users (not all chatting at the same second)

**Will struggle / risk OOM or queue collapse:** 50+ true simultaneous long streaming chats, or many parallel 90 s Manus tasks on the free Nano box.

**To scale later:** upgrade host RAM/CPU, move bot to a VPS, add a job queue for Manus, put Supabase pooler in front, require BYOK, disable streaming, lower `HISTORY_LIMIT`.

See capacity details (FA): [docs/SETUP_FA.md](docs/SETUP_FA.md)

### Environment
See [`.env.example`](.env.example). Highlights:
- `ALLOW_SHARED_KEY`, `STREAMING_ENABLED`, `DEFAULT_DAILY_TOKEN_QUOTA`
- `QUOTA_APPLIES_TO_OWN_KEYS`, `ADMIN_TELEGRAM_IDS`
- `MANUS_API_KEY`, `MANUS_LOCALE`, `MANUS_TIMEOUT_SEC`

### Database
`users`, `conversations`, `messages`, `usage`, `user_api_keys`, `user_quotas`  
SQL under [`sql/`](sql/).

### Security
- Never commit `.env`. Rotate leaked tokens.
- Keys stored for BYOK are **masked** in UI.
- Bot tables are backend-only (RLS disabled or no public policies) — protect `SUPABASE_KEY`.
- Multi-account farming of free provider quotas may violate provider ToS.

---

## Persian

### امکانات
- چت چندنوبتی + استریم پاسخ  
- تنظیمات: مدل/سرویس، فرمت، تاریخچه  
- **BYOK** برای Dahl و دیگر سرویس‌ها + **Manus**  
- **چند گفتگو** با نام (`/chats`)  
- **سهمیه روزانه** توکن  
- **Manus Agent** (`/manus`) — ساخت/ویرایش تصویر و تسک agent  
- **آمار ادمین** (`/admin`)

### راه‌اندازی
راهنمای قدم‌به‌قدم کامل (فارسی):

**[docs/SETUP_FA.md](docs/SETUP_FA.md)**

خلاصه:
1. BotFather + Supabase + (اختیاری) کلید Dahl/Manus  
2. اجرای `sql/01` تا `sql/03`  
3. `.env` / env های Deployka  
4. `pip install -r requirements.txt && python main.py` یا دیپلوی Deployka  

### ظرفیت تقریبی (رایگان)
| سناریو | تقریباً |
|--------|---------|
| چت متنی همزمان امن | **۵–۱۰ نفر** |
| اسپایک کوتاه | **۱۵–۲۵** (اگر Manus سنگین نباشد) |
| چند ویرایش تصویر Manus همزمان | **۲–۵** + چند چت ساده |
| کاربر فعال روزانه سبک | حدود **۵۰–۱۵۰** (نه همه در یک ثانیه) |

بالاتر از این روی Deployka Nano (۱۲۸MB) ریسک OOM/کندی/ریت‌لیمیت تلگرام بالا می‌رود.  
برای رشد: ارتقای سرور + صف (queue) برای Manus + BYOK اجباری.

### دستورات
`/start` `/settings` `/keys` `/manus` `/chats` `/clear` `/admin` `/help`

---

## Arabic

راجع التشغيل الكامل بالعربية: استخدم [docs/SETUP_FA.md](docs/SETUP_FA.md) (فارسی) أو اتبع English Quick start أعلاه.  
تقدير الاستخدام المتزامن على باقة مجانية: حوالي **5–10** محادثات نصية آمنة، و**2–5** مهام Manus متزامنة.

---

## Russian

Краткий старт — в разделе English Quick start.  
Подробная инструкция (перс.): [docs/SETUP_FA.md](docs/SETUP_FA.md).  
Оценка одновременных пользователей на free-tier: **5–10** текстовых чатов; Manus **2–5** задач параллельно.

---

## Repository layout

```
AIDahl/
├── main.py
├── requirements.txt
├── .env.example
├── .gitignore
├── docs/
│   └── SETUP_FA.md          # Full Persian implementation guide
├── sql/
│   ├── 01_user_api_keys.sql
│   ├── 02_user_settings.sql
│   └── 03_phase5.sql
└── README.md
```

## Links
- Dahl: https://inference.dahl.global · Docs: https://inference.dahl.global/docs/  
- Manus API: https://open.manus.ai/docs  
- Manus key: https://manus.im/app#settings/developers  
- Supabase: https://supabase.com  

## License
Use at your own risk. Respect each provider’s terms.
