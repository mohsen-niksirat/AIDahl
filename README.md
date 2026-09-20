# AIDahl — Telegram AI Bot (Dahl + multi-provider BYOK)

OpenAI-compatible Telegram assistant. Users can chat with models from **Dahl Inference**, **OpenRouter**, **Groq**, **Cerebras**, **SiliconFlow**, **Mistral**, **GitHub Models**, or any **custom** endpoint. **BYOK**: each user may register their own free API key so the bot scales beyond one shared quota.

> Docs: [English](#english) · [فارسی](#persian) · [العربية](#arabic) · [Русский](#russian)

---

## English

### Features
- Multi-turn chat history (configurable context length)
- **Settings**: pick provider/model, reply format (HTML / Markdown / plain), history size
- **BYOK**: `/keys` — store personal API keys (masked in UI)
- **Streaming** replies (live message updates while the model generates)
- **Daily token quota** per user (shared key and/or own key, via env)
- **Multiple named conversations** — switch / rename / new chat from the menu
- **Admin stats** `/admin` — users, tokens, top models (for `ADMIN_TELEGRAM_IDS`)
- Token usage logged in Supabase (`usage`, `messages`, `conversations`)

### Stack
| Piece | Choice |
|--------|--------|
| Bot | Python 3.10+, `pyTelegramBotAPI` |
| HTTP | `httpx` only (128 MB-friendly, no heavy SDKs) |
| DB | Supabase PostgreSQL via PostgREST |
| Models | OpenAI-compatible `POST /v1/chat/completions` (+ stream) |

### Quick start
1. Create a Telegram bot with [@BotFather](https://t.me/BotFather) → copy `BOT_TOKEN`
2. Create a [Supabase](https://supabase.com) project → copy URL + anon/service key
3. Get a Dahl key: [inference.dahl.global/account](https://inference.dahl.global/account) (allocate pool tokens to the key)
4. Clone this repo and configure env:

```bash
cp .env.example .env
# fill BOT_TOKEN, SUPABASE_URL, SUPABASE_KEY, DAHL_API_KEY, ADMIN_TELEGRAM_IDS
```

5. Run SQL migrations **in order** in Supabase SQL Editor:
   - `sql/01_user_api_keys.sql`
   - `sql/02_user_settings.sql`
   - `sql/03_phase5.sql`
6. Install & run:

```bash
pip install -r requirements.txt
python main.py
```

7. **Deployka / Docker free tier**: set the same variables in the platform env UI. `ALLOW_SHARED_KEY` must be present if the host requires it.

### Bot commands
| Command | Description |
|---------|-------------|
| `/start` | Main menu |
| `/settings` | Provider, model, reply format, history, chats |
| `/keys` | Manage API keys (BYOK) |
| `/chats` | List / switch / rename conversations |
| `/newchat` `/clear` | New conversation |
| `/admin` | Admin dashboard (if your ID is allowed) |
| `/help` | Help |
| `/cancel` | Cancel key-entry flow |

### Environment
See [`.env.example`](.env.example). Important flags:
- `STREAMING_ENABLED=true|false`
- `DEFAULT_DAILY_TOKEN_QUOTA` — default daily tokens per user
- `QUOTA_APPLIES_TO_OWN_KEYS` — apply quota to personal keys too
- `ADMIN_TELEGRAM_IDS` — comma-separated admin Telegram IDs
- `ALLOW_SHARED_KEY` — use bot’s Dahl key when user has none

### Database tables
`users`, `conversations`, `messages`, `usage`, `user_api_keys`, `user_quotas`  
Schema is created by the SQL files under [`sql/`](sql/).

### Security notes
- Never commit `.env` (gitignored). Rotate keys if they were shared.
- API keys in the bot are **masked** after save.
- RLS: bot tables are backend-only; disable RLS or use service key carefully.
- Multi-account abuse of free provider quotas may violate provider Terms of Service.

---

## Persian

### امکانات
- چت چندنوبتی با تاریخچه قابل تنظیم
- **تنظیمات**: انتخاب سرویس/مدل، فرمت پاسخ (HTML/Markdown/ساده)، اندازه تاریخچه
- **BYOK**: ثبت کلید API شخصی با `/keys`
- **استریم** پاسخ (به‌روزرسانی زنده پیام)
- **سهمیه روزانه توکن** برای هر کاربر
- **چند گفتگو با نام** — تعویض/تغییر نام/گفتگوی جدید
- **آمار ادمین** `/admin` — تعداد کاربر، توکن، مدل‌های پرتکرار

### راه‌اندازی سریع
1. ربات بات‌فادر + توکن  
2. پروژه Supabase + اجرای `sql/01` تا `sql/03` به ترتیب  
3. کلید Dahl و Allocate از Pool  
4. `cp .env.example .env` و پر کردن متغیرها  
5. `pip install -r requirements.txt && python main.py`  
6. دیپلوی روی Deployka با همان env ها (وجود `ALLOW_SHARED_KEY` الزامی)

### دستورات
`/start` `/settings` `/keys` `/chats` `/clear` `/admin` `/help` `/cancel`

---

## Arabic

### المميزات
- محادثة متعددة الأدوار مع سياق قابل للضبط
- **الإعدادات**: مزود/نموذج، تنسيق الرد، حجم السياق
- **مفاتيح API** الخاصة بالمستخدم (`/keys`)
- **بث مباشر** للرد أثناء التوليد
- **حصة يومية** من التوكنات لكل مستخدم
- **محادثات متعددة** بأسماء
- **إحصائيات المشرف** عبر `/admin`

### التشغيل
1. بوت تلغرام + Supabase + مفتاح Dahl  
2. نفّذ ملفات `sql/01` ثم `02` ثم `03`  
3. انسخ `.env.example` إلى `.env` واملأ القيم  
4. `pip install -r requirements.txt` ثم `python main.py`

---

## Russian

### Возможности
- Многораундовый чат с настраиваемой историей
- **Настройки**: провайдер/модель, формат ответа, размер контекста
- **Свои API-ключи** (`/keys`)
- **Стриминг** ответов в Telegram
- **Дневная квота** токенов на пользователя
- **Несколько именованных диалогов**
- **Админ-статистика** `/admin`

### Быстрый старт
1. Токен бота + Supabase + ключ Dahl  
2. SQL: `sql/01`, `02`, `03` по порядку  
3. `.env.example` → `.env`  
4. `pip install -r requirements.txt && python main.py`

---

## Repository layout

```
AIDahl/
├── main.py                 # Bot application
├── requirements.txt
├── .env.example
├── .gitignore
├── sql/
│   ├── 01_user_api_keys.sql
│   ├── 02_user_settings.sql
│   └── 03_phase5.sql
└── README.md
```

## License
Use at your own risk. Respect each inference provider’s terms.

---

## Links
- Dahl Inference: https://inference.dahl.global  
- Docs: https://inference.dahl.global/docs/  
- Supabase: https://supabase.com  
