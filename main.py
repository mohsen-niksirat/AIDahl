import os
import re
import io
import json
import html
import uuid
import time
import base64
import logging
import threading
from datetime import datetime, timezone, date
from collections import defaultdict, deque
from typing import Optional, Dict, Any, List, Tuple

import httpx
from dotenv import load_dotenv
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DAHL_API_KEY = os.getenv("DAHL_API_KEY")
DAHL_BASE_URL = os.getenv("DAHL_BASE_URL", "https://inference.dahl.global/v1").rstrip("/")
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
TOKEN_QUOTA = int(os.getenv("TOKEN_QUOTA", "1000000"))
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "20"))
ALLOW_SHARED_KEY = os.getenv("ALLOW_SHARED_KEY", "true").lower() in ("1", "true", "yes")

# Manus agent mode (async tasks — not OpenAI chat completions)
MANUS_BASE_URL = os.getenv("MANUS_BASE_URL", "https://api.manus.ai").rstrip("/")
MANUS_POLL_INTERVAL = float(os.getenv("MANUS_POLL_INTERVAL", "4"))
MANUS_TIMEOUT_SEC = int(os.getenv("MANUS_TIMEOUT_SEC", "90"))
MANUS_DEFAULT_PROFILE = os.getenv("MANUS_DEFAULT_PROFILE", "standard")  # standard|lite|max
# Manus accepts documented locales like en, zh-CN, ja — omit if invalid
MANUS_LOCALE = os.getenv("MANUS_LOCALE", "").strip()  # empty = send no locale
# Optional shared Manus key if a user has no personal key
MANUS_SHARED_API_KEY = os.getenv("MANUS_API_KEY") or os.getenv("MANUS_SHARED_API_KEY")

# Concurrency queue — free-tier Nano (128MB) cannot handle many simultaneous agent jobs
MANUS_MAX_CONCURRENT = max(1, int(os.getenv("MANUS_MAX_CONCURRENT", "2")))
MANUS_QUEUE_MAX = max(1, int(os.getenv("MANUS_QUEUE_MAX", "15")))
MANUS_MAX_PER_USER = max(1, int(os.getenv("MANUS_MAX_PER_USER", "1")))

STREAMING_ENABLED = os.getenv("STREAMING_ENABLED", "true").lower() in ("1", "true", "yes")
STREAM_EDIT_INTERVAL = float(os.getenv("STREAM_EDIT_INTERVAL", "1.2"))
DEFAULT_DAILY_TOKEN_QUOTA = int(os.getenv("DEFAULT_DAILY_TOKEN_QUOTA", "50000"))
# Lifetime (overall) token budget per user — generous but bounded
DEFAULT_LIFETIME_TOKEN_QUOTA = int(os.getenv("DEFAULT_LIFETIME_TOKEN_QUOTA", "3000000"))
LIFETIME_WARN_PCT = float(os.getenv("LIFETIME_WARN_PCT", "75"))
# Keep only the active conversation in DB; purge others on new chat
KEEP_MESSAGES_IN_ACTIVE_CHAT = int(os.getenv("KEEP_MESSAGES_IN_ACTIVE_CHAT", "40"))
# Do not grow the usage table — counters live in user_quotas
SKIP_USAGE_TABLE = os.getenv("SKIP_USAGE_TABLE", "true").lower() in ("1", "true", "yes")
QUOTA_APPLIES_TO_OWN_KEYS = os.getenv("QUOTA_APPLIES_TO_OWN_KEYS", "false").lower() in (
    "1",
    "true",
    "yes",
)
ADMIN_TELEGRAM_IDS = set()
for _part in (os.getenv("ADMIN_TELEGRAM_IDS") or "").split(","):
    _part = _part.strip()
    if _part.isdigit():
        ADMIN_TELEGRAM_IDS.add(int(_part))

MAX_TG_MESSAGE = 4000
CHAT_LIST_LIMIT = 15
BOT_VERSION = os.getenv("BOT_VERSION", "1.0.0-final")
PROMPTOPIA_URL = os.getenv("PROMPTOPIA_URL", "https://mohsen-niksirat.github.io/promptopia/").strip()
PROMPTOPIA_LABEL = os.getenv("PROMPTOPIA_LABEL", "📚 پرامپت‌های آماده")

SYSTEM_PROMPT = (
    "You are a helpful AI assistant on Telegram. "
    "Answer clearly. Use short paragraphs, lists, and code blocks when useful."
)

logging.basicConfig(level=logging.INFO)
bot = telebot.TeleBot(BOT_TOKEN, num_threads=4)
# Avoid infinite hangs on Telegram get_file / send
try:
    bot.timeout = 30
except Exception:
    pass

PENDING: Dict[int, Dict[str, str]] = {}

# Manus job queue (in-memory; single Deployka container)
_MANUS_LOCK = threading.Lock()
_MANUS_ACTIVE = 0
_MANUS_QUEUE: deque = deque()  # jobs waiting
_MANUS_RUNNING_USERS: set = set()  # user_ids currently executing
_MANUS_QUEUED_USERS: set = set()  # user_ids waiting in queue
# user_id -> unix start time (for stale-lock recovery)
_MANUS_JOB_META: Dict[int, float] = {}
# Multi-turn Manus: user_id -> {task_id, task_url, updated_at, title}
_MANUS_SESSIONS: Dict[int, dict] = {}
MANUS_FOLLOWUP_TTL_SEC = int(os.getenv("MANUS_FOLLOWUP_TTL_SEC", "7200"))
# If a running job is silent longer than this, unlock the user
MANUS_STALE_LOCK_SEC = int(os.getenv("MANUS_STALE_LOCK_SEC", str(MANUS_TIMEOUT_SEC + 90)))

MODEL_SEP = "|"
LEGACY_PROVIDER = "dahl"

RESPONSE_STYLES = {
    "html": {
        "name_fa": "HTML (پیشنهادی)",
        "name_en": "HTML (recommended)",
        "desc": "پررنگ/کد خوانا؛ پایدارتر از Markdown تلگرام",
    },
    "markdown": {
        "name_fa": "Markdown تلگرام",
        "name_en": "Telegram Markdown",
        "desc": "در صورت خطا خودکار ساده می‌شود",
    },
    "plain": {
        "name_fa": "متن ساده",
        "name_en": "Plain text",
        "desc": "بدون فرمت",
    },
}

CONTEXT_CHOICES = [5, 10, 20, 40]

PROVIDERS: Dict[str, Dict[str, Any]] = {
    "dahl": {
        "name": "Dahl Inference",
        "name_fa": "دال (Dahl)",
        "base_url": "https://inference.dahl.global/v1",
        "get_key_url": "https://inference.dahl.global/account",
        "note_fa": "هدیه ثبت‌نام: ۱۰۰ میلیون توکن در Pool؛ سپس Allocate به کلید.",
        "models": {
            "glm": {"name": "GLM-5.3-Flash", "id": "zai-org/GLM-5.3-Flash"},
            "deepseek": {"name": "DeepSeek-V4-Flash", "id": "deepseek-ai/DeepSeek-V4-Flash-0731"},
            "minimax": {"name": "MiniMax-M2.7", "id": "MiniMaxAI/MiniMax-M2.7"},
        },
        "default_model_id": "zai-org/GLM-5.3-Flash",
    },
    "openrouter": {
        "name": "OpenRouter",
        "name_fa": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "get_key_url": "https://openrouter.ai/keys",
        "note_fa": "کلید رایگان بسازید؛ مدل‌های :free معمولاً بدون هزینه‌اند.",
        "models": {
            "llama_free": {
                "name": "Llama 3.3 70B (free)",
                "id": "meta-llama/llama-3.3-70b-instruct:free",
            },
            "gemini_free": {
                "name": "Gemini Flash (free)",
                "id": "google/gemini-2.0-flash-exp:free",
            },
        },
        "default_model_id": "meta-llama/llama-3.3-70b-instruct:free",
    },
    "groq": {
        "name": "Groq",
        "name_fa": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "get_key_url": "https://console.groq.com/keys",
        "note_fa": "کلید رایگان از Console Groq.",
        "models": {
            "llama": {"name": "Llama 3.3 70B Versatile", "id": "llama-3.3-70b-versatile"},
            "llama8b": {"name": "Llama 3.1 8B Instant", "id": "llama-3.1-8b-instant"},
        },
        "default_model_id": "llama-3.3-70b-versatile",
    },
    "cerebras": {
        "name": "Cerebras",
        "name_fa": "Cerebras",
        "base_url": "https://api.cerebras.ai/v1",
        "get_key_url": "https://cloud.cerebras.ai/",
        "note_fa": "کلید رایگان از Cloud Cerebras.",
        "models": {
            "llama": {"name": "Llama 3.3 70B", "id": "llama-3.3-70b"},
        },
        "default_model_id": "llama-3.3-70b",
    },
    "siliconflow": {
        "name": "SiliconFlow",
        "name_fa": "SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "get_key_url": "https://cloud.siliconflow.cn/account/ak",
        "note_fa": "کلید API از SiliconFlow.",
        "models": {
            "qwen": {"name": "Qwen2.5 7B", "id": "Qwen/Qwen2.5-7B-Instruct"},
        },
        "default_model_id": "Qwen/Qwen2.5-7B-Instruct",
    },
    "mistral": {
        "name": "Mistral",
        "name_fa": "Mistral",
        "base_url": "https://api.mistral.ai/v1",
        "get_key_url": "https://console.mistral.ai/api-keys",
        "note_fa": "کلید از Mistral Console.",
        "models": {
            "small": {"name": "Mistral Small", "id": "mistral-small-latest"},
        },
        "default_model_id": "mistral-small-latest",
    },
    "github": {
        "name": "GitHub Models",
        "name_fa": "GitHub Models",
        "base_url": "https://models.github.ai/inference",
        "get_key_url": "https://github.com/marketplace/models",
        "note_fa": "PAT / GitHub Models token.",
        "models": {
            "gpt4o": {"name": "GPT-4o (catalog)", "id": "openai/gpt-4o"},
        },
        "default_model_id": "openai/gpt-4o",
    },
    "manus": {
        "name": "Manus Agent",
        "name_fa": "Manus (دستیار/تصویر)",
        "base_url": "https://api.manus.ai",
        "get_key_url": "https://manus.im/app#settings/developers",
        "note_fa": (
            "API از Settings→Developers در manus.im بسازید (یک‌بار نمایش داده می‌شود). "
            "چت معمولی نیست؛ تسک agent اجرا می‌کند (متن + فایل/عکس). "
            "سهمیه credits روزانه/هفتگی دارد."
        ),
        "models": {},
        "default_model_id": None,
        "kind": "agent",
    },
    "custom": {
        "name": "Custom OpenAI-compatible",
        "name_fa": "سفارشی (سایت دیگر)",
        "base_url": None,
        "get_key_url": None,
        "note_fa": "هر سرویس OpenAI-compatible: Base URL + API Key + Model ID",
        "models": {},
        "default_model_id": None,
    },
}


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def today_date_str() -> str:
    return date.today().isoformat()


def is_admin(user_id: int) -> bool:
    if user_id in ADMIN_TELEGRAM_IDS:
        return True
    # Also honor users.is_admin column if set in Supabase
    try:
        res = sb_get(f"users?telegram_id=eq.{user_id}&select=is_admin")
        data = res.json() or []
        if data and bool(data[0].get("is_admin")):
            return True
    except Exception:
        pass
    return False


def send_admin_text(chat_id: int, text: str, reply_to_message=None, reply_markup=None):
    """Send admin panel; Markdown first, then plain fallback (avoid silent fail)."""
    try:
        if reply_to_message is not None:
            bot.reply_to(reply_to_message, text, reply_markup=reply_markup, parse_mode="Markdown")
        else:
            bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode="Markdown")
        return
    except Exception as e:
        logging.warning(f"admin Markdown send failed: {e}")
    plain = text.replace("**", "").replace("`", "")
    try:
        if reply_to_message is not None:
            bot.reply_to(reply_to_message, plain, reply_markup=reply_markup)
        else:
            bot.send_message(chat_id, plain, reply_markup=reply_markup)
    except Exception as e:
        logging.error(f"admin plain send failed: {e}")
        try:
            bot.send_message(chat_id, "❌ خطا در ارسال آمار ادمین. لاگ سرور را چک کنید.")
        except Exception:
            pass


def supabase_headers() -> dict:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def sb_get(path: str, timeout: float = 10.0):
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    with httpx.Client(timeout=timeout) as client:
        res = client.get(url, headers=supabase_headers())
        return res


def sb_post(path: str, payload: dict, prefer: str = "return=representation", timeout: float = 10.0):
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = supabase_headers()
    headers["Prefer"] = prefer
    with httpx.Client(timeout=timeout) as client:
        return client.post(url, headers=headers, json=payload)


def sb_patch(path: str, payload: dict, timeout: float = 10.0):
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    with httpx.Client(timeout=timeout) as client:
        return client.patch(url, headers=supabase_headers(), json=payload)


def sb_delete(path: str, timeout: float = 10.0):
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    with httpx.Client(timeout=timeout) as client:
        return client.delete(url, headers=supabase_headers())


def mask_key(key: str) -> str:
    key = key or ""
    if len(key) <= 8:
        return "*" * len(key)
    return key[:4] + "…" + key[-4:]


def parse_model_ref(raw: str) -> Tuple[str, str]:
    raw = (raw or "").strip()
    if not raw:
        return LEGACY_PROVIDER, PROVIDERS[LEGACY_PROVIDER]["default_model_id"]
    if MODEL_SEP in raw:
        provider, model_id = raw.split(MODEL_SEP, 1)
        provider = provider.strip()
        model_id = model_id.strip()
        if provider in PROVIDERS and model_id:
            return provider, model_id
    return LEGACY_PROVIDER, raw


def format_model_ref(provider: str, model_id: str) -> str:
    return f"{provider}{MODEL_SEP}{model_id}"


def truncate_message(text: str, limit: int = MAX_TG_MESSAGE) -> str:
    if not text:
        return "…"
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n\n… [truncated]"


def escape_telegram_markdown(text: str) -> str:
    out = []
    for ch in text:
        if ch in ("_", "*", "`", "["):
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def llm_text_to_html(text: str) -> str:
    body = html.escape(text, quote=False)
    body = re.sub(r"```(\w*)\n(.*?)```", lambda m: f"<pre>{m.group(2)}</pre>", body, flags=re.S)
    body = re.sub(r"```(.*?)```", lambda m: f"<pre>{m.group(1)}</pre>", body, flags=re.S)
    body = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", body, flags=re.S)
    body = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", body)
    body = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", body)
    body = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", body, flags=re.M)
    return body


def render_for_telegram(text: str, style: str) -> Tuple[str, Optional[str]]:
    """Return (payload_text, parse_mode)."""
    text = truncate_message(text)
    style = (style or "html").lower()
    if style == "plain":
        return text, None
    if style == "markdown":
        return text, "Markdown"
    return llm_text_to_html(text), "HTML"


def send_bot_reply(chat_id: int, message_id: int, text: str, style: str = "html"):
    payload, parse_mode = render_for_telegram(text, style)
    try:
        bot.edit_message_text(payload, chat_id=chat_id, message_id=message_id, parse_mode=parse_mode)
    except Exception as e:
        logging.warning(f"send_bot_reply primary failed ({style}): {e}")
        if style == "markdown":
            try:
                bot.edit_message_text(
                    escape_telegram_markdown(truncate_message(text)),
                    chat_id=chat_id,
                    message_id=message_id,
                    parse_mode="Markdown",
                )
                return
            except Exception as e2:
                logging.warning(f"markdown escape failed: {e2}")
        try:
            bot.edit_message_text(
                truncate_message(text),
                chat_id=chat_id,
                message_id=message_id,
                parse_mode=None,
            )
        except Exception as e3:
            logging.error(f"send_bot_reply plain failed: {e3}")


def safe_edit(chat_id: int, message_id: int, text: str, style: str = "html"):
    try:
        send_bot_reply(chat_id, message_id, text, style=style)
    except Exception as e:
        logging.error(f"safe_edit: {e}")


# ---------------------------------------------------------------------------
# User settings / keys / quota / conversations
# ---------------------------------------------------------------------------

def get_user_settings(user_id: int) -> dict:
    settings = {
        "response_style": "html",
        "context_messages": None,
        "selected_model": format_model_ref(
            LEGACY_PROVIDER, PROVIDERS[LEGACY_PROVIDER]["default_model_id"]
        ),
        "active_conversation_id": None,
        "columns_ok": True,
        "found": False,
    }
    try:
        res = sb_get(
            f"users?telegram_id=eq.{user_id}"
            f"&select=response_style,context_messages,selected_model,active_conversation_id,stream_enabled"
        )
        if res.status_code >= 400:
            settings["columns_ok"] = False
            try:
                res2 = sb_get(f"users?telegram_id=eq.{user_id}&select=selected_model")
                data = res2.json()
                if data:
                    settings["found"] = True
                    if data[0].get("selected_model"):
                        settings["selected_model"] = data[0]["selected_model"]
            except Exception:
                pass
            return settings
        data = res.json() or []
        if not data:
            return settings
        settings["found"] = True
        row = data[0]
        if "response_style" in row and row.get("response_style"):
            settings["response_style"] = row["response_style"]
        elif "response_style" not in row:
            settings["columns_ok"] = False
        if row.get("context_messages"):
            settings["context_messages"] = int(row["context_messages"])
        if row.get("selected_model"):
            settings["selected_model"] = row["selected_model"]
        if row.get("active_conversation_id"):
            settings["active_conversation_id"] = row["active_conversation_id"]
        if "stream_enabled" in row:
            settings["stream_enabled"] = row.get("stream_enabled")
    except Exception as e:
        logging.error(f"get_user_settings error: {e}")
        settings["columns_ok"] = False
    return settings


def set_user_settings(user_id: int, **fields) -> bool:
    if not fields:
        return False
    try:
        res = sb_patch(f"users?telegram_id=eq.{user_id}", fields)
        if res.status_code >= 400:
            logging.error(f"set_user_settings {res.status_code}: {res.text[:200]}")
            return False
        return True
    except Exception as e:
        logging.error(f"set_user_settings error: {e}")
        return False


def get_user_model(user_id: int) -> str:
    settings = get_user_settings(user_id)
    return settings.get("selected_model") or format_model_ref(
        LEGACY_PROVIDER, PROVIDERS[LEGACY_PROVIDER]["default_model_id"]
    )


def set_user_model(user_id: int, model_ref: str) -> None:
    set_user_settings(user_id, selected_model=model_ref)


def ensure_user(from_user) -> None:
    try:
        res = sb_get(f"users?telegram_id=eq.{from_user.id}&select=telegram_id")
        data = res.json()
        if not data:
            payload = {
                "telegram_id": from_user.id,
                "username": from_user.username,
                "first_name": from_user.first_name,
                "selected_model": format_model_ref(
                    LEGACY_PROVIDER, PROVIDERS[LEGACY_PROVIDER]["default_model_id"]
                ),
                "last_active_at": utcnow_iso(),
            }
            sb_post("users", payload)
        else:
            sb_patch(
                f"users?telegram_id=eq.{from_user.id}",
                {
                    "username": from_user.username,
                    "first_name": from_user.first_name,
                    "last_active_at": utcnow_iso(),
                },
            )
    except Exception as e:
        logging.error(f"ensure_user error: {e}")


def list_user_keys(user_id: int) -> Dict[str, dict]:
    result: Dict[str, dict] = {}
    try:
        res = sb_get(
            f"user_api_keys?user_id=eq.{user_id}"
            f"&select=provider,api_key,base_url,is_active,updated_at"
        )
        rows = res.json() or []
        if not isinstance(rows, list):
            return result
        for row in rows:
            if row.get("is_active", True):
                result[row["provider"]] = row
    except Exception as e:
        logging.error(f"list_user_keys error: {e}")
    return result


def upsert_user_key(user_id: int, provider: str, api_key: str, base_url: Optional[str] = None) -> bool:
    payload = {
        "user_id": user_id,
        "provider": provider,
        "api_key": api_key,
        "base_url": base_url,
        "is_active": True,
        "updated_at": utcnow_iso(),
        "created_at": utcnow_iso(),
    }
    try:
        url_path = f"user_api_keys?on_conflict=user_id,provider"
        url = f"{SUPABASE_URL}/rest/v1/{url_path}"
        headers = supabase_headers()
        headers["Prefer"] = "resolution=merge-duplicates,return=representation"
        with httpx.Client(timeout=10.0) as client:
            res = client.post(url, headers=headers, json=payload)
            if res.status_code >= 400:
                logging.error(f"upsert_user_key {res.status_code}: {res.text[:250]}")
                return False
            return True
    except Exception as e:
        logging.error(f"upsert_user_key error: {e}")
        return False


def delete_user_key(user_id: int, provider: str) -> bool:
    try:
        res = sb_delete(f"user_api_keys?user_id=eq.{user_id}&provider=eq.{provider}")
        return res.status_code < 400
    except Exception as e:
        logging.error(f"delete_user_key error: {e}")
        return False


# --- Quota (daily + lifetime) ---

def ensure_quota_row(user_id: int) -> dict:
    row = {
        "user_id": user_id,
        "daily_limit": DEFAULT_DAILY_TOKEN_QUOTA,
        "used_today": 0,
        "reset_at": today_date_str(),
        "lifetime_limit": DEFAULT_LIFETIME_TOKEN_QUOTA,
        "lifetime_used": 0,
    }
    try:
        res = sb_get(
            f"user_quotas?user_id=eq.{user_id}"
            f"&select=user_id,daily_limit,used_today,reset_at,lifetime_limit,lifetime_used"
        )
        if res.status_code >= 400:
            # columns missing (SQL 03/04 not run) — try legacy select
            res = sb_get(
                f"user_quotas?user_id=eq.{user_id}"
                f"&select=user_id,daily_limit,used_today,reset_at"
            )
            if res.status_code >= 400:
                return {"ok": False, **row, "table_missing": True}
        data = res.json() or []
        if data:
            row.update({k: v for k, v in data[0].items() if v is not None})
            row["table_missing"] = False
            row["ok"] = True
            if row.get("lifetime_limit") is None:
                row["lifetime_limit"] = DEFAULT_LIFETIME_TOKEN_QUOTA
            if row.get("lifetime_used") is None:
                row["lifetime_used"] = 0
        else:
            payload = {
                "user_id": user_id,
                "daily_limit": DEFAULT_DAILY_TOKEN_QUOTA,
                "used_today": 0,
                "reset_at": today_date_str(),
                "lifetime_limit": DEFAULT_LIFETIME_TOKEN_QUOTA,
                "lifetime_used": 0,
                "updated_at": utcnow_iso(),
            }
            r = sb_post("user_quotas", payload)
            if r.status_code >= 400:
                # retry without lifetime columns
                payload.pop("lifetime_limit", None)
                payload.pop("lifetime_used", None)
                sb_post("user_quotas", payload)
            row["table_missing"] = False
            row["ok"] = True
    except Exception as e:
        logging.error(f"ensure_quota_row error: {e}")
        row["ok"] = False
        row["table_missing"] = True
    return row


def quota_status(user_id: int) -> dict:
    row = ensure_quota_row(user_id)
    daily_limit = int(row.get("daily_limit") or DEFAULT_DAILY_TOKEN_QUOTA)
    used = int(row.get("used_today") or 0)
    reset_at = str(row.get("reset_at") or today_date_str())
    today = today_date_str()
    if reset_at < today:
        used = 0
        try:
            sb_patch(
                f"user_quotas?user_id=eq.{user_id}",
                {"used_today": 0, "reset_at": today, "updated_at": utcnow_iso()},
            )
        except Exception:
            pass

    lifetime_limit = int(row.get("lifetime_limit") or DEFAULT_LIFETIME_TOKEN_QUOTA)
    lifetime_used = int(row.get("lifetime_used") or 0)
    lifetime_remaining = max(lifetime_limit - lifetime_used, 0) if lifetime_limit > 0 else None
    lifetime_pct = (
        (lifetime_used / lifetime_limit * 100.0) if lifetime_limit > 0 else 0.0
    )
    lifetime_exceeded = lifetime_limit > 0 and lifetime_used >= lifetime_limit
    lifetime_warn = (
        lifetime_limit > 0
        and not lifetime_exceeded
        and lifetime_pct >= LIFETIME_WARN_PCT
    )

    return {
        "ok": bool(row.get("ok")),
        "table_missing": bool(row.get("table_missing")),
        "limit": daily_limit,
        "used": used,
        "remaining": max(daily_limit - used, 0),
        "reset_at": today if reset_at < today else reset_at,
        "exceeded": used >= daily_limit and daily_limit > 0,
        "lifetime_limit": lifetime_limit,
        "lifetime_used": lifetime_used,
        "lifetime_remaining": lifetime_remaining,
        "lifetime_pct": lifetime_pct,
        "lifetime_exceeded": lifetime_exceeded,
        "lifetime_warn": lifetime_warn,
    }


def quota_add_usage(user_id: int, tokens: int) -> dict:
    """Increment daily + lifetime counters. Returns updated status."""
    if tokens <= 0:
        return quota_status(user_id)
    status = quota_status(user_id)
    if not status.get("ok"):
        return status
    new_daily = status["used"] + tokens
    new_life = status["lifetime_used"] + tokens
    try:
        sb_patch(
            f"user_quotas?user_id=eq.{user_id}",
            {
                "used_today": new_daily,
                "reset_at": today_date_str(),
                "lifetime_used": new_life,
                "updated_at": utcnow_iso(),
            },
        )
    except Exception as e:
        logging.error(f"quota_add_usage error: {e}")
        try:
            sb_patch(
                f"user_quotas?user_id=eq.{user_id}",
                {
                    "used_today": new_daily,
                    "reset_at": today_date_str(),
                    "updated_at": utcnow_iso(),
                },
            )
        except Exception as e2:
            logging.error(f"quota_add_usage daily-only: {e2}")
    return quota_status(user_id)


def quota_allowed(user_id: int, source: str) -> Tuple[bool, dict]:
    """
    source: 'user' | 'shared'
    Lifetime limit always applies (overall per-user cap).
    Daily limit applies to shared key; optional for personal keys.
    """
    status = quota_status(user_id)
    if status.get("table_missing"):
        return True, status
    if status.get("lifetime_exceeded"):
        return False, status
    if source == "user" and not QUOTA_APPLIES_TO_OWN_KEYS:
        return True, status
    return (not status["exceeded"]), status


def format_quota_block(status: dict, applies_note: str = "") -> str:
    if status.get("table_missing"):
        return (
            "سهمیه: جدول `user_quotas` ساخته نشده — sql/03 و sql/04 را اجرا کنید.\n"
        )
    life = status.get("lifetime_limit") or 0
    life_used = status.get("lifetime_used") or 0
    life_rem = status.get("lifetime_remaining")
    life_pct = status.get("lifetime_pct") or 0
    lines = [
        f"**سهمیه کلی (مادام‌العمر)**",
        f"مصرف: `{life_used:,}` / `{life:,}` (`{life_pct:.0f}%`)",
    ]
    if life_rem is not None:
        lines.append(f"باقیمانده کل: `{life_rem:,}`")
    if status.get("lifetime_exceeded"):
        lines.append("⛔️ **سهمیه کلی تمام شده است**")
    elif status.get("lifetime_warn"):
        lines.append("⚠️ **دارید به سقف کلی نزدیک می‌شوید**")
    lines.append("")
    lines.append("**سهمیه روزانه (کلید مشترک ربات)**")
    lines.append(f"امروز: `{status['used']:,}` / `{status['limit']:,}`")
    lines.append(f"باقیمانده امروز: `{status['remaining']:,}`")
    lines.append(f"ریست روزانه: `{status['reset_at']}`")
    if applies_note:
        lines.append(applies_note.strip())
    return "\n".join(lines) + "\n"


def quota_warning_text(status: dict) -> Optional[str]:
    if status.get("lifetime_exceeded"):
        return (
            "⛔️ **سهمیه کلی شما تمام شد.**\n"
            f"مصرف: `{status.get('lifetime_used', 0):,}` / "
            f"`{status.get('lifetime_limit', 0):,}`\n"
            "چت جدید تا افزایش سهمیه یا تمدید امکان‌پذیر نیست."
        )
    if status.get("lifetime_warn"):
        rem = status.get("lifetime_remaining")
        rem_s = f"`{rem:,}`" if rem is not None else "—"
        return (
            "⚠️ **هشدار سهمیه**\n"
            f"به **{status.get('lifetime_pct', 0):.0f}%** سقف کلی رسیده‌اید.\n"
            f"باقیمانده: {rem_s} توکن\n"
            "لطفاً مصرف را مدیریت کنید."
        )
    return None


# --- Storage hygiene: keep only active conversation ---

def purge_user_old_conversations(user_id: int, keep_conversation_id: Optional[str]) -> int:
    """
    Delete all conversations + messages for user except keep_conversation_id.
    Telegram chat history stays in Telegram; server keeps only current session.
    """
    deleted = 0
    try:
        res = sb_get(
            f"conversations?user_id=eq.{user_id}&select=id"
        )
        rows = res.json() or []
        if not isinstance(rows, list):
            return 0
        for row in rows:
            cid = str(row.get("id") or "")
            if not cid or (keep_conversation_id and cid == str(keep_conversation_id)):
                continue
            try:
                sb_delete(f"messages?conversation_id=eq.{cid}")
                sb_delete(f"conversations?id=eq.{cid}")
                deleted += 1
            except Exception as e:
                logging.warning(f"purge conv {cid}: {e}")
    except Exception as e:
        logging.error(f"purge_user_old_conversations: {e}")
    logging.info(f"purge conversations user={user_id} keep={keep_conversation_id} deleted={deleted}")
    return deleted


def trim_conversation_messages(conversation_id: str, keep_last: Optional[int] = None) -> int:
    """Keep only the last N messages of the active conversation in DB."""
    if not conversation_id:
        return 0
    keep = keep_last if keep_last is not None else KEEP_MESSAGES_IN_ACTIVE_CHAT
    if keep <= 0:
        return 0
    try:
        res = sb_get(
            f"messages?conversation_id=eq.{conversation_id}&order=id.desc&select=id"
        )
        rows = res.json() or []
        if not isinstance(rows, list) or len(rows) <= keep:
            return 0
        drop_ids = [r["id"] for r in rows[keep:] if r.get("id") is not None]
        if not drop_ids:
            return 0
        # PostgREST: delete by id in (...) — batch in chunks
        removed = 0
        for i in range(0, len(drop_ids), 20):
            chunk = drop_ids[i : i + 20]
            id_list = ",".join(str(x) for x in chunk)
            sb_delete(f"messages?id=in.({id_list})")
            removed += len(chunk)
        return removed
    except Exception as e:
        logging.error(f"trim_conversation_messages: {e}")
        return 0


# --- Conversations ---

def create_conversation(user_id: int, model: str, title: str = "گفتگوی جدید") -> dict:
    conversation_id = str(uuid.uuid4())
    payload = {
        "id": conversation_id,
        "user_id": user_id,
        "title": title[:80],
        "model": model,
        "created_at": utcnow_iso(),
        "updated_at": utcnow_iso(),
    }
    try:
        res = sb_post("conversations", payload)
        res.raise_for_status()
    except Exception as e:
        logging.error(f"create_conversation error: {e}")
        raise
    set_user_settings(user_id, active_conversation_id=conversation_id)
    # Storage policy: only the current conversation remains on our server
    purge_user_old_conversations(user_id, keep_conversation_id=conversation_id)
    return {"id": conversation_id, "title": title[:80], "model": model}


def list_conversations(user_id: int, limit: int = CHAT_LIST_LIMIT) -> List[dict]:
    try:
        res = sb_get(
            f"conversations?user_id=eq.{user_id}"
            f"&order=updated_at.desc&limit={limit}"
            f"&select=id,title,model,updated_at"
        )
        data = res.json() or []
        return data if isinstance(data, list) else []
    except Exception as e:
        logging.error(f"list_conversations error: {e}")
        return []


def get_conversation_by_id(conversation_id: str) -> Optional[dict]:
    try:
        res = sb_get(f"conversations?id=eq.{conversation_id}&select=id,title,model,user_id")
        data = res.json() or []
        return data[0] if data else None
    except Exception as e:
        logging.error(f"get_conversation_by_id error: {e}")
        return None


def get_active_conversation(user_id: int, model: str) -> dict:
    settings = get_user_settings(user_id)
    active_id = settings.get("active_conversation_id")
    if active_id:
        conv = get_conversation_by_id(str(active_id))
        if conv and conv.get("user_id") == user_id:
            return conv
    rows = list_conversations(user_id, limit=1)
    if rows:
        cid = rows[0]["id"]
        set_user_settings(user_id, active_conversation_id=cid)
        return rows[0]
    return create_conversation(user_id, model)


def set_active_conversation(user_id: int, conversation_id: str) -> bool:
    return set_user_settings(user_id, active_conversation_id=conversation_id)


def rename_conversation(conversation_id: str, title: str) -> bool:
    try:
        res = sb_patch(
            f"conversations?id=eq.{conversation_id}",
            {"title": title[:80], "updated_at": utcnow_iso()},
        )
        return res.status_code < 400
    except Exception as e:
        logging.error(f"rename_conversation error: {e}")
        return False


def touch_conversation(conversation_id: str, model: str, title: Optional[str] = None) -> None:
    payload = {"updated_at": utcnow_iso(), "model": model}
    if title:
        payload["title"] = title[:80]
    try:
        sb_patch(f"conversations?id=eq.{conversation_id}", payload)
    except Exception as e:
        logging.error(f"touch_conversation error: {e}")


def get_conversation_history(conversation_id: str, limit: int = HISTORY_LIMIT) -> list:
    limit = max(0, int(limit or 0))
    if limit <= 0:
        return []
    try:
        res = sb_get(
            f"messages?conversation_id=eq.{conversation_id}"
            f"&order=id.desc&limit={limit}&select=role,content"
        )
        rows = res.json() or []
    except Exception as e:
        logging.error(f"get_conversation_history error: {e}")
        return []
    history = []
    for row in reversed(rows):
        role = row.get("role")
        content = (row.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            history.append({"role": role, "content": content})
    return history


def count_conversation_messages(conversation_id: str) -> int:
    try:
        res = sb_get(f"messages?conversation_id=eq.{conversation_id}&select=id")
        data = res.json() or []
        return len(data)
    except Exception:
        return 0


def save_message(
    conversation_id: str,
    role: str,
    content: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> None:
    payload = {
        "conversation_id": conversation_id,
        "role": role,
        "content": content,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "created_at": utcnow_iso(),
    }
    try:
        sb_post("messages", payload)
    except Exception as e:
        logging.error(f"save_message error: {e}")


def log_usage(
    user_id: int,
    model: str,
    in_tok: int,
    out_tok: int,
    total_tok: int,
    provider: str = LEGACY_PROVIDER,
) -> None:
    """Optional usage log. Disabled by default so the DB does not fill up."""
    if SKIP_USAGE_TABLE:
        return
    payload = {
        "user_id": user_id,
        "model": format_model_ref(provider, model) if provider else model,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "total_tokens": total_tok,
    }
    try:
        sb_post("usage", payload)
    except Exception as e:
        logging.error(f"log_usage error: {e}")


def get_usage_stats(user_id: Optional[int] = None) -> dict:
    path = "usage?select=model,input_tokens,output_tokens,total_tokens,user_id,created_at"
    if user_id is not None:
        path += f"&user_id=eq.{user_id}"
    stats = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "by_model": defaultdict(int),
        "calls": 0,
        "error": False,
    }
    try:
        res = sb_get(path)
        data = res.json() or []
        stats["calls"] = len(data)
        for item in data:
            inp = int(item.get("input_tokens") or 0)
            out = int(item.get("output_tokens") or 0)
            tot = int(item.get("total_tokens") or (inp + out))
            stats["input_tokens"] += inp
            stats["output_tokens"] += out
            stats["total_tokens"] += tot
            model = item.get("model") or "unknown"
            stats["by_model"][model] += tot
    except Exception as e:
        logging.error(f"get_usage_stats error: {e}")
        stats["error"] = True
    return stats


def resolve_inference_credentials(user_id: int) -> Optional[dict]:
    model_ref = get_user_model(user_id)
    provider, model_id = parse_model_ref(model_ref)
    keys = list_user_keys(user_id)
    row = keys.get(provider)
    if row and row.get("api_key"):
        base = row.get("base_url") or PROVIDERS.get(provider, {}).get("base_url")
        if not base:
            return None
        return {
            "provider": provider,
            "model_id": model_id,
            "base_url": base.rstrip("/"),
            "api_key": row["api_key"],
            "source": "user",
        }
    if ALLOW_SHARED_KEY and provider == LEGACY_PROVIDER and DAHL_API_KEY:
        base = DAHL_BASE_URL or PROVIDERS["dahl"]["base_url"]
        return {
            "provider": provider,
            "model_id": model_id,
            "base_url": base.rstrip("/"),
            "api_key": DAHL_API_KEY,
            "source": "shared",
        }
    return None


# ---------------------------------------------------------------------------
# LLM call: streaming + non-streaming
# ---------------------------------------------------------------------------

def call_llm(
    base_url: str,
    api_key: str,
    model_id: str,
    messages: list,
    stream: bool,
    on_partial=None,
) -> dict:
    """
    Returns {content, prompt_tokens, completion_tokens, total_tokens, streamed}
    on_partial(full_text_so_far) is called periodically when streaming.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    if not stream:
        payload = {"model": model_id, "messages": messages, "stream": False}
        with httpx.Client(timeout=90.0) as client:
            resp = client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        pt = usage.get("prompt_tokens", 0)
        ct = usage.get("completion_tokens", 0)
        tt = usage.get("total_tokens", pt + ct)
        return {
            "content": content,
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "total_tokens": tt,
            "streamed": False,
        }

    payload = {
        "model": model_id,
        "messages": messages,
        "stream": True,
    }
    # Optional OpenAI-compatible usage in stream (may be ignored by some providers)
    payload["stream_options"] = {"include_usage": True}

    chunks_text: List[str] = []
    usage = {}
    buffer = ""
    last_notify = 0.0

    with httpx.Client(timeout=120.0) as client:
        with client.stream(
            "POST", f"{base_url}/chat/completions", headers=headers, json=payload
        ) as resp:
            if resp.status_code >= 400:
                body = resp.read().decode("utf-8", errors="replace")
                raise httpx.HTTPStatusError(
                    f"{resp.status_code}: {body[:300]}",
                    request=resp.request,
                    response=resp,
                )
            for line in resp.iter_lines():
                if not line:
                    continue
                if line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                if chunk.get("usage"):
                    usage = chunk["usage"]
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                piece = delta.get("content") or ""
                if piece:
                    chunks_text.append(piece)
                    buffer += piece
                    now = time.time()
                    if on_partial and (now - last_notify) >= STREAM_EDIT_INTERVAL:
                        last_notify = now
                        try:
                            on_partial(buffer)
                        except Exception as e:
                            logging.warning(f"on_partial error: {e}")

    content = "".join(chunks_text)
    pt = int(usage.get("prompt_tokens") or 0)
    ct = int(usage.get("completion_tokens") or 0)
    if not usage:
        # rough estimate when provider omits usage on stream
        pt = sum(len(m.get("content") or "") for m in messages) // 4
        ct = max(len(content) // 4, 1)
    tt = int(usage.get("total_tokens") or (pt + ct))
    return {
        "content": content,
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "total_tokens": tt,
        "streamed": True,
    }


# ---------------------------------------------------------------------------
# Manus agent mode (API v2 — async tasks)
# Docs: https://open.manus.ai/docs
# ---------------------------------------------------------------------------

def manus_headers(api_key: str) -> dict:
    return {
        "x-manus-api-key": api_key,
        "Content-Type": "application/json",
    }


def get_manus_key(user_id: int) -> Optional[str]:
    keys = list_user_keys(user_id)
    row = keys.get("manus")
    if row and row.get("api_key"):
        return row["api_key"]
    if MANUS_SHARED_API_KEY:
        return MANUS_SHARED_API_KEY
    logging.warning(f"get_manus_key: no key for user={user_id} providers={list(keys.keys())}")
    return None


def manus_force_unlock(user_id: Optional[int] = None) -> int:
    """Clear stale queue locks so a stuck job cannot block the user forever."""
    global _MANUS_ACTIVE
    cleared = 0
    with _MANUS_LOCK:
        if user_id is None:
            _MANUS_RUNNING_USERS.clear()
            _MANUS_QUEUED_USERS.clear()
            _MANUS_JOB_META.clear()
            _MANUS_ACTIVE = 0
            cleared = 1
        else:
            if user_id in _MANUS_RUNNING_USERS or user_id in _MANUS_QUEUED_USERS:
                cleared = 1
            _MANUS_RUNNING_USERS.discard(user_id)
            _MANUS_QUEUED_USERS.discard(user_id)
            _MANUS_JOB_META.pop(user_id, None)
            if cleared:
                _MANUS_ACTIVE = max(0, _MANUS_ACTIVE - 1)
    return cleared


def manus_reap_stale_locks() -> int:
    """Unlock users whose jobs exceeded stale timeout (thread died / hang)."""
    now = time.time()
    stale = []
    with _MANUS_LOCK:
        for uid, started in list(_MANUS_JOB_META.items()):
            if now - started > MANUS_STALE_LOCK_SEC:
                stale.append(uid)
    n = 0
    for uid in stale:
        logging.warning(f"manus stale lock reaped user={uid}")
        manus_force_unlock(uid)
        n += 1
    return n


def manus_create_task(
    api_key: str,
    prompt: str,
    profile: Optional[str] = None,
    attachments: Optional[List[dict]] = None,
) -> dict:
    """
    attachments: list of Manus ContentPart file objects, e.g.
      {"type": "file", "file_data": "data:image/jpeg;base64,...", "filename": "photo.jpg"}
    """
    profile = (profile or MANUS_DEFAULT_PROFILE or "standard").lower()
    if profile not in ("standard", "lite", "max"):
        profile = "standard"

    text = (prompt or "").strip()[:4800]
    if not text:
        text = (
            "Edit/improve this image. Keep the main subject, "
            "return a polished result and briefly describe the changes."
        )

    content_parts: List[dict] = [{"type": "text", "text": text}]
    if attachments:
        for att in attachments:
            if att and att.get("file_data"):
                content_parts.append(att)

    payload = {
        "message": {"content": content_parts},
        "agent_profile": profile,
        "hide_in_task_list": False,
        "interactive_mode": False,
    }
    # Only send locale when explicitly configured — "fa" was rejected by Manus API
    if MANUS_LOCALE:
        payload["locale"] = MANUS_LOCALE
    with httpx.Client(timeout=60.0) as client:
        res = client.post(
            f"{MANUS_BASE_URL}/v2/task.create",
            headers=manus_headers(api_key),
            json=payload,
        )
    try:
        data = res.json()
    except Exception:
        data = {}
    if res.status_code >= 400 or (isinstance(data, dict) and data.get("ok") is False):
        err = (data or {}).get("error") or {}
        raise RuntimeError(
            f"Manus task.create {res.status_code}: {err.get('code') or ''} {err.get('message') or res.text[:200]}"
        )
    return {
        "task_id": data.get("task_id"),
        "task_url": data.get("task_url"),
        "task_title": data.get("task_title"),
        "share_url": data.get("share_url"),
    }


def telegram_file_url(file_path: str) -> str:
    return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"


def download_telegram_file(file_path: str, timeout: float = 30.0) -> bytes:
    url = telegram_file_url(file_path)
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        res = client.get(url)
        res.raise_for_status()
        return res.content


def extract_media_from_message(message) -> Tuple[Optional[dict], str]:
    """
    Build a Manus file ContentPart from a Telegram photo/document.
    Returns (attachment_or_None, error_message).
    """
    try:
        if getattr(message, "photo", None):
            file_id = message.photo[-1].file_id
            filename = "telegram_photo.jpg"
            mime = "image/jpeg"
        elif getattr(message, "document", None):
            file_id = message.document.file_id
            filename = message.document.file_name or "telegram_document"
            mime = message.document.mime_type or "application/octet-stream"
        else:
            return None, "پیام عکس/فایل ندارد"

        logging.info(f"extract_media: get_file file_id={file_id[:20]}…")
        try:
            file_info = bot.get_file(file_id)
        except Exception as e:
            logging.error(f"get_file failed: {e}")
            return None, f"خطا در get_file تلگرام: {e}"

        file_path = getattr(file_info, "file_path", None)
        if not file_path:
            return None, "file_path از تلگرام برنگشت"
        logging.info(f"extract_media: download path={file_path}")
        try:
            raw = download_telegram_file(file_path, timeout=20.0)
        except Exception as e:
            logging.error(f"download_telegram_file failed: {e}")
            return None, f"دانلود فایل تلگرام ناموفق: {e}"

        if not raw:
            return None, "فایل خالی دریافت شد"
        if len(raw) > 8 * 1024 * 1024:
            return None, f"حجم عکس/فایل زیاد است ({len(raw)//1024}KB) — حداکثر ~8MB"

        b64 = base64.b64encode(raw).decode("ascii")
        if message.photo:
            mime = "image/jpeg"
            filename = "telegram_photo.jpg"
        logging.info(f"extract_media: ok bytes={len(raw)} b64={len(b64)} name={filename}")
        return {
            "type": "file",
            "file_data": f"data:{mime};base64,{b64}",
            "filename": filename,
            "mime_type": mime,
        }, ""
    except Exception as e:
        logging.exception(f"extract_media_from_message: {e}")
        return None, f"خطای غیرمنتظره در آماده‌سازی فایل: {e}"


def manus_list_messages(api_key: str, task_id: str, limit: int = 50) -> list:
    url = (
        f"{MANUS_BASE_URL}/v2/task.listMessages"
        f"?task_id={task_id}&order=desc&limit={limit}"
    )
    try:
        with httpx.Client(timeout=20.0) as client:
            res = client.get(url, headers=manus_headers(api_key))
        data = res.json()
    except Exception as e:
        logging.error(f"manus_list_messages: {e}")
        return []
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in ("messages", "data", "events", "items", "result"):
        val = data.get(key)
        if isinstance(val, list):
            return val
        if isinstance(val, dict):
            for sub in ("messages", "data", "events", "items"):
                if isinstance(val.get(sub), list):
                    return val[sub]
    return []


def manus_task_detail(api_key: str, task_id: str) -> dict:
    try:
        with httpx.Client(timeout=20.0) as client:
            res = client.get(
                f"{MANUS_BASE_URL}/v2/task.detail?task_id={task_id}",
                headers=manus_headers(api_key),
            )
        data = res.json()
    except Exception as e:
        logging.error(f"manus_task_detail: {e}")
        return {}
    if not isinstance(data, dict):
        return {}
    return data.get("data") or data.get("task") or data


_IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff")
_FILE_HOST_HINTS = (
    "manuscdn.com",
    "files.manus",
    "manus.im",
    "amazonaws.com",
    "s3.",
    "blob.core.windows.net",
    "googleusercontent.com",
)


def _looks_like_media_url(url: str) -> bool:
    u = (url or "").lower()
    if not u.startswith("http"):
        return False
    if any(ext in u for ext in _IMAGE_EXT + (".pdf", ".md", ".zip", ".docx", ".mp4", ".webm")):
        return True
    if any(h in u for h in _FILE_HOST_HINTS) and any(
        k in u for k in ("/file", "/files", "/asset", "/media", "/upload", "/download", "/cdn")
    ):
        return True
    # signed / static image paths without extension
    if any(h in u for h in _FILE_HOST_HINTS) and any(k in u for k in ("image", "png", "jpg", "webp")):
        return True
    return False


def _filename_from_url(url: str, fallback: str = "manus-output") -> str:
    try:
        path = url.split("?")[0].rstrip("/")
        name = path.split("/")[-1] or fallback
        name = re.sub(r"[^\w.\-]+", "_", name)[:80]
        return name or fallback
    except Exception:
        return fallback


def _mime_from_name(name: str, url: str = "") -> str:
    blob = (name + " " + url).lower()
    if any(x in blob for x in (".png", "image/png")):
        return "image/png"
    if any(x in blob for x in (".jpg", ".jpeg", "image/jpeg")):
        return "image/jpeg"
    if any(x in blob for x in (".webp", "image/webp")):
        return "image/webp"
    if any(x in blob for x in (".gif", "image/gif")):
        return "image/gif"
    if ".pdf" in blob:
        return "application/pdf"
    if any(x in blob for x in (".md", "text/markdown")):
        return "text/markdown"
    if ".zip" in blob:
        return "application/zip"
    return ""


def _walk_manus_files(obj, files: List[dict], assistant_texts: List[str], depth: int = 0):
    """Deep-scan Manus JSON for any downloadable media/file URLs."""
    if depth > 8:
        return
    if isinstance(obj, dict):
        # collect text
        if obj.get("text") and isinstance(obj["text"], str):
            t = obj["text"].strip()
            if len(t) > 40 and not t.startswith("http"):
                assistant_texts.append(t)
        for key in (
            "fileUrl",
            "file_url",
            "url",
            "download_url",
            "downloadUrl",
            "src",
            "href",
            "image_url",
            "imageUrl",
            "file_url_signed",
            "public_url",
        ):
            val = obj.get(key)
            if isinstance(val, str) and val.startswith("http") and _looks_like_media_url(val):
                name = (
                    obj.get("fileName")
                    or obj.get("filename")
                    or obj.get("name")
                    or _filename_from_url(val)
                )
                mime = obj.get("mimeType") or obj.get("mime_type") or obj.get("type")
                if mime == "file" or mime == "output_file" or mime == "image":
                    mime = obj.get("mimeType") or obj.get("mime_type") or _mime_from_name(name, val)
                if not mime or mime in ("file", "output_file", "image"):
                    mime = _mime_from_name(name, val)
                # skip tiny avatars / icons
                if any(x in val.lower() for x in ("avatar", "favicon", "icon-32", "logo-light")):
                    continue
                files.append({"url": val, "name": str(name)[:80], "mime": mime or ""})
        for v in obj.values():
            _walk_manus_files(v, files, assistant_texts, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _walk_manus_files(item, files, assistant_texts, depth + 1)
    elif isinstance(obj, str):
        # markdown image links
        for m in re.findall(r"https?://[^\s\)\"']+", obj):
            if _looks_like_media_url(m):
                files.append({
                    "url": m,
                    "name": _filename_from_url(m),
                    "mime": _mime_from_name("", m),
                })


def manus_extract_from_messages(messages: List[dict]) -> dict:
    """Parse Manus task events — deep scan for assistant text + media URLs."""
    agent_status = None
    status_detail = {}
    assistant_texts: List[str] = []
    files: List[dict] = []
    error_text = None

    items = messages if isinstance(messages, list) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        etype = item.get("type") or item.get("event_type")

        if etype == "status_update" or "status_update" in item:
            su = item.get("status_update") or item
            if isinstance(su, dict):
                agent_status = su.get("agent_status") or agent_status
                if su.get("status_detail"):
                    status_detail = su["status_detail"]

        if etype == "error_message" or item.get("error_message"):
            err = item.get("error_message")
            if isinstance(err, dict):
                error_text = err.get("message") or err.get("content") or str(err)
            elif isinstance(err, str):
                error_text = err

        if etype == "assistant_message" or item.get("assistant_message"):
            am = item.get("assistant_message") or item
            if isinstance(am, dict):
                content = am.get("content")
                if isinstance(content, str) and content.strip() and not content.startswith("http"):
                    assistant_texts.append(content.strip())
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("text"):
                            t = str(part["text"]).strip()
                            if t and not t.startswith("http"):
                                assistant_texts.append(t)

        _walk_manus_files(item, files, assistant_texts)

    # unique by url
    seen = set()
    uniq_files = []
    for f in files:
        u = f.get("url")
        if not u or u in seen:
            continue
        seen.add(u)
        uniq_files.append(f)

    # Prefer images first for delivery
    uniq_files.sort(key=lambda f: (0 if str(f.get("mime", "")).startswith("image") or any(
        x in str(f.get("url", "")).lower() for x in _IMAGE_EXT
    ) else 1))

    # unique texts, keep last meaningful
    uniq_texts = []
    for t in assistant_texts:
        t = t.strip()
        if t and t not in uniq_texts:
            uniq_texts.append(t)
    text = "\n\n".join(uniq_texts[-3:]) if uniq_texts else ""

    return {
        "agent_status": agent_status,
        "status_detail": status_detail,
        "text": text,
        "files": uniq_files[:8],
        "error": error_text,
    }


def manus_collect_files(
    api_key: str,
    task_id: str,
    extra_limit: int = 50,
    exclude_urls: Optional[set] = None,
    newest_only: bool = False,
) -> List[dict]:
    """
    Harvest file URLs from listMessages + task.detail.
    exclude_urls: skip URLs already delivered (follow-up turns).
    newest_only: prefer files found in the newest messages first.
    """
    files: List[dict] = []
    texts: List[str] = []
    newest_files: List[dict] = []
    try:
        msgs = manus_list_messages(api_key, task_id, limit=extra_limit)
        # listMessages is usually newest-first
        if newest_only and msgs:
            for item in msgs[:12]:
                bucket: List[dict] = []
                _walk_manus_files(item, bucket, texts)
                newest_files.extend(bucket)
        ex = manus_extract_from_messages(msgs)
        files.extend(ex.get("files") or [])
        texts.extend([ex.get("text") or ""])
    except Exception as e:
        logging.error(f"collect from messages: {e}")
    try:
        detail = manus_task_detail(api_key, task_id)
        if detail:
            _walk_manus_files(detail, files, texts)
    except Exception as e:
        logging.error(f"collect from detail: {e}")

    if newest_only and newest_files:
        files = newest_files + files

    exclude = set(exclude_urls or ())
    seen = set()
    out = []
    for f in files:
        u = f.get("url")
        if not u or u in seen or u in exclude:
            continue
        seen.add(u)
        out.append(f)
    out.sort(key=lambda f: (
        0 if str(f.get("mime", "")).startswith("image") or any(
            x in str(f.get("url", "")).lower() for x in _IMAGE_EXT
        ) else 1
    ))
    return out


def manus_mark_delivered(user_id: int, urls: List[str]):
    sess = _MANUS_SESSIONS.get(user_id)
    if not sess:
        return
    delivered = list(sess.get("delivered_urls") or [])
    for u in urls:
        if u and u not in delivered:
            delivered.append(u)
    # cap memory
    sess["delivered_urls"] = delivered[-50:]
    sess["updated_at"] = time.time()


def manus_delivered_urls(user_id: int) -> set:
    sess = manus_get_session(user_id)
    if not sess:
        return set()
    return set(sess.get("delivered_urls") or ())


def manus_download_file(url: str, api_key: str) -> Optional[bytes]:
    """Download a Manus (or CDN) file; try with API key, then public."""
    headers_auth = {
        "x-manus-api-key": api_key,
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "AIDahlBot/1.0",
    }
    for headers in (headers_auth, {"User-Agent": "AIDahlBot/1.0"}):
        try:
            with httpx.Client(timeout=60.0, follow_redirects=True) as client:
                res = client.get(url, headers=headers)
            if res.status_code == 200 and res.content and len(res.content) > 100:
                ctype = res.headers.get("content-type", "")
                if "text/html" in ctype and len(res.content) < 5000:
                    continue
                return res.content
        except Exception as e:
            logging.warning(f"manus_download_file try failed: {e}")
    return None


def manus_wait_for_task(
    api_key: str,
    task_id: str,
    timeout_sec: Optional[int] = None,
    on_progress=None,
) -> dict:
    """Poll Manus until terminal status; keep hunting files after stop."""
    deadline = time.time() + (timeout_sec or MANUS_TIMEOUT_SEC)
    last = {
        "agent_status": "running",
        "status_detail": {},
        "text": "",
        "files": [],
        "error": None,
        "timed_out": False,
        "task_id": task_id,
    }
    extra_file_wait = 75  # seconds after stop to catch late-exported images
    extra_deadline = None

    while time.time() < deadline or (extra_deadline and time.time() < extra_deadline):
        try:
            messages = manus_list_messages(api_key, task_id)
            extracted = manus_extract_from_messages(messages)
        except Exception as e:
            logging.error(f"manus poll error: {e}")
            extracted = {}

        harvested = manus_collect_files(api_key, task_id)
        merged_files = harvested or (extracted.get("files") or [])
        # merge with existing
        have = {f.get("url") for f in last.get("files") or []}
        for f in merged_files:
            if f.get("url") not in have:
                last.setdefault("files", []).append(f)
                have.add(f.get("url"))

        last.update({
            "agent_status": extracted.get("agent_status") or last["agent_status"],
            "status_detail": extracted.get("status_detail") or last.get("status_detail") or {},
            "text": extracted.get("text") or last.get("text") or "",
            "error": extracted.get("error") or last.get("error"),
            "files": last.get("files") or merged_files,
        })

        status = last.get("agent_status")
        if on_progress:
            try:
                on_progress(last)
            except Exception as e:
                logging.warning(f"on_progress error: {e}")

        if status in ("stopped", "error", "waiting", "completed"):
            # images sometimes appear slightly after agent stops
            if last.get("files") or status in ("error", "waiting"):
                return last
            if extra_deadline is None:
                extra_deadline = time.time() + extra_file_wait
            time.sleep(MANUS_POLL_INTERVAL)
            continue

        time.sleep(MANUS_POLL_INTERVAL)

    if not last.get("timed_out"):
        # reached end without terminal clear
        if last.get("agent_status") not in ("stopped", "error", "waiting", "completed"):
            last["timed_out"] = True
    return last


def manus_credits_text(api_key: str) -> str:
    try:
        with httpx.Client(timeout=15.0) as client:
            res = client.get(
                f"{MANUS_BASE_URL}/v2/usage.availableCredits",
                headers=manus_headers(api_key),
            )
        data = res.json() or {}
        if not data.get("ok", True):
            return "دریافت credits ناموفق بود."
        d = data.get("data") or data
        total = d.get("total_credits")
        refresh = d.get("refresh_credits")
        interval = d.get("refresh_interval") or "—"
        nxt = d.get("next_refresh_time")
        lines = [
            "💳 **Manus credits**",
            f"قابل مصرف: `{total}`",
            f"refresh باقی‌مانده: `{refresh}` · دوره: `{interval}`",
        ]
        if nxt:
            lines.append(f"refresh بعدی: `{nxt}` (unix)")
        return "\n".join(lines)
    except Exception as e:
        logging.error(f"manus credits: {e}")
        return f"خطا در دریافت credits: {e}"


def _is_image_file(f: dict) -> bool:
    mime = str(f.get("mime") or "").lower()
    url = str(f.get("url") or "").lower()
    name = str(f.get("name") or "").lower()
    if mime.startswith("image/"):
        return True
    return any(x in url or x in name for x in _IMAGE_EXT)


def manus_send_file_pair(chat_id: int, content: bytes, name: str, f: dict):
    """Send image as chat photo + as original document (high quality)."""
    safe_name = (name or "manus-output").strip() or "manus-output"
    if _is_image_file(f):
        # ensure a proper extension for Telegram
        if not any(safe_name.lower().endswith(ext) for ext in _IMAGE_EXT):
            mime = str(f.get("mime") or "").lower()
            ext = ".png"
            if "jpeg" in mime or "jpg" in mime:
                ext = ".jpg"
            elif "webp" in mime:
                ext = ".webp"
            elif "gif" in mime:
                ext = ".gif"
            elif ".jpg" in safe_name.lower() or ".jpeg" in safe_name.lower():
                ext = ".jpg"
            safe_name = safe_name + ext

        bio_photo = io.BytesIO(content)
        bio_photo.name = safe_name
        try:
            bot.send_photo(
                chat_id,
                bio_photo,
                caption=f"🖼 نمایش در چت — {safe_name}",
            )
        except Exception as e:
            logging.warning(f"send_photo failed: {e}")
            try:
                bot.send_message(chat_id, f"⚠️ ارسال عکس preview ناموفق: {e}")
            except Exception:
                pass

        bio_doc = io.BytesIO(content)
        bio_doc.name = safe_name
        try:
            bot.send_document(
                chat_id,
                bio_doc,
                caption=f"📎 فایل باکیفیت (اصلی) — {safe_name}",
                visible_file_name=safe_name,
            )
        except Exception as e:
            logging.warning(f"send_document image failed: {e}")
            try:
                bot.send_document(
                    chat_id,
                    io.BytesIO(content),
                    caption=f"📎 فایل — {safe_name}",
                )
            except Exception as e2:
                logging.error(f"send_document retry failed: {e2}")
                bot.send_message(chat_id, f"⚠️ ارسال فایل ناموفق: {safe_name}\n{e2}")
    else:
        bio_doc = io.BytesIO(content)
        bio_doc.name = safe_name
        try:
            bot.send_document(
                chat_id,
                bio_doc,
                caption=f"📎 فایل Manus — {safe_name}",
                visible_file_name=safe_name,
            )
        except Exception as e:
            logging.error(f"send_document failed: {e}")
            bot.send_message(chat_id, f"📎 {safe_name}\n{f.get('url')}")


_INPUT_FILE_HINTS = (
    "telegram_photo",
    "telegram_document",
    "/input/",
    "input_image",
    "source_image",
    "original_image",
    "upload",
    "attachment",
)


def _looks_like_input_file(f: dict) -> bool:
    name = str(f.get("name") or "").lower()
    url = str(f.get("url") or "").lower()
    mime = str(f.get("mime") or "").lower()
    blob = name + " " + url + " " + mime
    if "output" in blob or "result" in blob or "edited" in blob or "export" in blob:
        return False
    return any(h in blob for h in _INPUT_FILE_HINTS)


def _filter_output_files(files: List[dict], had_attachment: bool = False) -> List[dict]:
    """Keep only likely Manus OUTPUT files; drop user input echoes."""
    out = []
    for f in files or []:
        if not f.get("url"):
            continue
        if _looks_like_input_file(f):
            continue
        out.append(f)
    if had_attachment:
        images = [f for f in out if _is_image_file(f)]
        others = [f for f in out if not _is_image_file(f)]
        out = images[:2] + others[:2]
    else:
        out = out[:3]
    return out


def _relax_output_files(files: List[dict], exclude: set) -> List[dict]:
    """If strict filter removed everything, fall back to newest non-delivered files."""
    cand = [f for f in files or [] if f.get("url") and f.get("url") not in exclude]
    if not cand:
        return []
    # Prefer anything that does not look like telegram input
    soft = [f for f in cand if not _looks_like_input_file(f)]
    if soft:
        return soft[:2]
    return cand[:2]


def manus_deliver_result(
    chat_id: int,
    status_msg,
    result: dict,
    task_url: Optional[str],
    api_key: Optional[str] = None,
    task_id: Optional[str] = None,
    exclude_urls: Optional[set] = None,
    followup: bool = False,
    user_id: Optional[int] = None,
    had_attachment: bool = False,
) -> List[str]:
    """Send final Manus OUTPUT only — never re-send the user's input image."""
    status = result.get("agent_status") or "?"
    text = (result.get("text") or "").strip()
    exclude = set(exclude_urls or ())
    raw_files = [f for f in (result.get("files") or []) if f.get("url") not in exclude]
    files = _filter_output_files(raw_files, had_attachment=had_attachment)
    error = result.get("error")

    # Harvest newest messages only — full task history includes the input image
    if api_key and task_id:
        try:
            more = manus_collect_files(
                api_key,
                task_id,
                exclude_urls=exclude,
                newest_only=True,
            )
            more = _filter_output_files(more, had_attachment=had_attachment)
            have = {f.get("url") for f in files}
            for f in more:
                u = f.get("url")
                if u and u not in have and u not in exclude and not _looks_like_input_file(f):
                    files.append(f)
                    have.add(u)
            files = _filter_output_files(files, had_attachment=had_attachment)
        except Exception as e:
            logging.warning(f"deliver harvest: {e}")

    sent_urls: List[str] = []

    if status == "error" or error:
        msg = f"❌ Manus agent خطا داد.\n`{error or status}`"
        if task_url:
            msg += f"\n{task_url}"
        try:
            bot.edit_message_text(
                msg,
                chat_id=status_msg.chat.id,
                message_id=status_msg.message_id,
                parse_mode="Markdown",
            )
        except Exception:
            bot.send_message(chat_id, msg)
    elif status == "waiting":
        detail = result.get("status_detail") or {}
        waiting_desc = detail.get("waiting_description") or detail.get("waiting_for_event_type") or ""
        msg = (
            "⏸ Manus در وضعیت **waiting** است.\n"
            f"{waiting_desc}\n\n"
            "برای ادامه در پنل Manus باز کنید."
        )
        if task_url:
            msg += f"\n{task_url}"
        try:
            bot.edit_message_text(
                msg,
                chat_id=status_msg.chat.id,
                message_id=status_msg.message_id,
                parse_mode="Markdown",
            )
        except Exception:
            bot.send_message(chat_id, msg)
        return sent_urls
    else:
        if files:
            final = (text or "✅ خروجی Manus آماده است.") + (
                f"\n\n📦 خروجی جدید: {len(files)} فایل (فقط نتیجه — عکس ورودی شما دوباره فرستاده نمی‌شود)"
            )
        else:
            final = text or "✅ Manus task تمام شد."
            final += "\n\nℹ️ فایل خروجی جدیدی در payload نبود (فقط متن)."
        if task_url:
            final += f"\n\n🔗 {task_url}"
        try:
            send_bot_reply(
                status_msg.chat.id, status_msg.message_id, final, style="html"
            )
        except Exception:
            try:
                bot.edit_message_text(
                    truncate_message(final),
                    chat_id=status_msg.chat.id,
                    message_id=status_msg.message_id,
                    parse_mode=None,
                )
            except Exception:
                bot.send_message(chat_id, truncate_message(final))

    if not files:
        # last chance: any newest file not already delivered
        if api_key and task_id:
            try:
                loose = manus_collect_files(api_key, task_id, exclude_urls=exclude, newest_only=True)
                loose = _relax_output_files(loose, exclude)
                if loose:
                    files = loose
            except Exception:
                pass
    if not files:
        return sent_urls

    delivered = 0
    for f in files[:4]:
        url = f.get("url")
        if not url or url in exclude or _looks_like_input_file(f):
            continue
        name = f.get("name") or _filename_from_url(url)
        content = None
        if api_key:
            content = manus_download_file(url, api_key)
        if not content:
            content = manus_download_file(url, "")
        if not content:
            logging.warning(f"download failed for {url}")
            continue
        try:
            manus_send_file_pair(chat_id, content, name, f)
            delivered += 1
            sent_urls.append(url)
        except Exception as e:
            logging.error(f"deliver file pair failed: {e}")

    if user_id and sent_urls:
        manus_mark_delivered(user_id, sent_urls)
    return sent_urls


def manus_queue_stats() -> dict:
    with _MANUS_LOCK:
        return {
            "active": _MANUS_ACTIVE,
            "queued": len(_MANUS_QUEUE),
            "max_concurrent": MANUS_MAX_CONCURRENT,
            "queue_max": MANUS_QUEUE_MAX,
        }


def manus_queue_status_text() -> dict:
    s = manus_queue_stats()
    return {
        "active": s["active"],
        "queued": s["queued"],
        "max_concurrent": s["max_concurrent"],
        "text": (
            f"📊 وضعیت صف Manus\n"
            f"در حال اجرا: `{s['active']}/{s['max_concurrent']}`\n"
            f"در صف: `{s['queued']}`\n"
            f"سقف صف: `{s['queue_max']}`"
        ),
    }


def manus_save_session(
    user_id: int,
    task_id: str,
    task_url: Optional[str] = None,
    title: str = "",
    keep_delivered: bool = False,
):
    prev = _MANUS_SESSIONS.get(user_id) or {}
    delivered = list(prev.get("delivered_urls") or []) if keep_delivered else []
    _MANUS_SESSIONS[user_id] = {
        "task_id": task_id,
        "task_url": task_url or (f"https://manus.im/app/{task_id}" if task_id else None),
        "updated_at": time.time(),
        "title": (title or "")[:60],
        "delivered_urls": delivered,
    }


def manus_get_session(user_id: int) -> Optional[dict]:
    sess = _MANUS_SESSIONS.get(user_id)
    if not sess or not sess.get("task_id"):
        return None
    age = time.time() - float(sess.get("updated_at") or 0)
    if age > MANUS_FOLLOWUP_TTL_SEC:
        _MANUS_SESSIONS.pop(user_id, None)
        return None
    return sess


def manus_clear_session(user_id: int):
    _MANUS_SESSIONS.pop(user_id, None)


def manus_session_label(user_id: int) -> str:
    sess = manus_get_session(user_id)
    if not sess:
        return "—"
    return f"`{str(sess.get('task_id'))[:10]}…`"


# ---------------------------------------------------------------------------
# Bot mode: exclusive chat (Dahl) vs manus
# ---------------------------------------------------------------------------

BOT_MODE_CHAT = "chat"
BOT_MODE_MANUS = "manus"
_USER_MODES: Dict[int, str] = {}
# user_id -> {name, prompt} selected for next Manus photo
_SAVED_PROMPT_PICK: Dict[int, dict] = {}
# Fallback if user_prompts table missing
_PROMPT_CACHE: Dict[int, Dict[str, str]] = {}


def get_bot_mode(user_id: int) -> str:
    mode = _USER_MODES.get(user_id)
    if mode in (BOT_MODE_CHAT, BOT_MODE_MANUS):
        return mode
    try:
        res = sb_get(f"users?telegram_id=eq.{user_id}&select=bot_mode")
        data = res.json() or []
        if data:
            m = (data[0].get("bot_mode") or "").strip().lower()
            if m in (BOT_MODE_CHAT, BOT_MODE_MANUS):
                _USER_MODES[user_id] = m
                return m
    except Exception:
        pass
    return BOT_MODE_CHAT


def set_bot_mode(user_id: int, mode: str) -> bool:
    if mode not in (BOT_MODE_CHAT, BOT_MODE_MANUS):
        return False
    _USER_MODES[user_id] = mode
    try:
        return set_user_settings(user_id, bot_mode=mode)
    except Exception as e:
        logging.warning(f"set_bot_mode persist: {e}")
        return True  # memory at least


def mode_fa(mode: str) -> str:
    return "💬 چت با دال" if mode == BOT_MODE_CHAT else "🎨 Manus"


# ---------------------------------------------------------------------------
# Saved prompts (long prompt + photo workaround)
# ---------------------------------------------------------------------------

def _prompt_cache_load(user_id: int) -> Dict[str, str]:
    if user_id not in _PROMPT_CACHE:
        _PROMPT_CACHE[user_id] = {}
    return _PROMPT_CACHE[user_id]


def list_user_prompts(user_id: int) -> List[dict]:
    rows: List[dict] = []
    try:
        res = sb_get(
            f"user_prompts?user_id=eq.{user_id}&order=updated_at.desc&select=id,name,prompt,updated_at&limit=50"
        )
        if res.status_code >= 400:
            cache = _prompt_cache_load(user_id)
            return [
                {"name": n, "prompt": p, "id": None, "updated_at": ""}
                for n, p in cache.items()
            ]
        data = res.json() or []
        if isinstance(data, list):
            rows = data
            cache = _prompt_cache_load(user_id)
            cache.clear()
            for r in rows:
                n = (r.get("name") or "").strip()
                if n:
                    cache[n] = r.get("prompt") or ""
    except Exception as e:
        logging.error(f"list_user_prompts: {e}")
        cache = _prompt_cache_load(user_id)
        return [
            {"name": n, "prompt": p, "id": None, "updated_at": ""}
            for n, p in cache.items()
        ]
    # de-dupe by name (keep first)
    seen = set()
    uniq = []
    for r in rows:
        n = (r.get("name") or "").strip()
        if not n or n in seen:
            continue
        seen.add(n)
        uniq.append({**r, "name": n})
    return uniq


def find_prompt_by_name(user_id: int, query: str) -> Optional[dict]:
    """Match saved prompt by exact name only (avoid long captions false-matching)."""
    q = (query or "").strip()
    if not q or len(q) > 64:
        return None
    rows = list_user_prompts(user_id)
    for r in rows:
        if (r.get("name") or "").strip() == q:
            return {"name": r["name"], "prompt": r.get("prompt") or ""}
    ql = q.lower()
    for r in rows:
        n = (r.get("name") or "").strip()
        if n.lower() == ql:
            p = r.get("prompt") or get_saved_prompt(user_id, n)
            if p:
                return {"name": n, "prompt": p}
    return None


def get_my_prompts_keyboard(user_id: int) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("➕ ثبت پرامپت جدید", callback_data="prompt_save_start"))
    rows = list_user_prompts(user_id)[:12]
    for r in rows:
        name = (r.get("name") or "")[:22]
        # one row per prompt: use | delete (name shown once)
        markup.row(
            InlineKeyboardButton(f"📝 {name}", callback_data=f"prompt_use:{(r.get('name') or '')[:40]}"),
            InlineKeyboardButton("🗑", callback_data=f"prompt_del:{(r.get('name') or '')[:40]}"),
        )
    markup.row(InlineKeyboardButton("🎨 Manus با پرامپت سیو شده", callback_data="manus_pick_prompt"))
    markup.row(InlineKeyboardButton("🔄 بروزرسانی", callback_data="menu_my_prompts"))
    markup.row(InlineKeyboardButton("🔙 منوی اصلی", callback_data="menu_main"))
    return markup


def save_user_prompt(user_id: int, name: str, prompt: str) -> bool:
    name = (name or "").strip()[:64]
    prompt = (prompt or "").strip()
    if not name or not prompt:
        return False
    _prompt_cache_load(user_id)[name] = prompt
    payload = {
        "user_id": user_id,
        "name": name,
        "prompt": prompt[:8000],
        "updated_at": utcnow_iso(),
        "created_at": utcnow_iso(),
    }
    try:
        url = f"{SUPABASE_URL}/rest/v1/user_prompts?on_conflict=user_id,name"
        headers = supabase_headers()
        headers["Prefer"] = "resolution=merge-duplicates,return=representation"
        with httpx.Client(timeout=10.0) as client:
            res = client.post(url, headers=headers, json=payload)
            if res.status_code >= 400:
                logging.error(f"save_user_prompt {res.status_code}: {res.text[:200]}")
                return False
        return True
    except Exception as e:
        logging.error(f"save_user_prompt: {e}")
        return True  # cache-only fallback


def delete_user_prompt(user_id: int, name: str) -> bool:
    name = (name or "").strip()
    _prompt_cache_load(user_id).pop(name, None)
    if _SAVED_PROMPT_PICK.get(user_id, {}).get("name") == name:
        _SAVED_PROMPT_PICK.pop(user_id, None)
    try:
        res = sb_delete(f"user_prompts?user_id=eq.{user_id}&name=eq.{name}")
        return res.status_code < 400
    except Exception as e:
        logging.error(f"delete_user_prompt: {e}")
        return False


def get_saved_prompt(user_id: int, name: str) -> Optional[str]:
    name = (name or "").strip()
    cache = _prompt_cache_load(user_id)
    if name in cache:
        return cache[name]
    try:
        res = sb_get(
            f"user_prompts?user_id=eq.{user_id}&name=eq.{name}&select=prompt"
        )
        data = res.json() or []
        if data:
            p = data[0].get("prompt") or ""
            cache[name] = p
            return p
    except Exception as e:
        logging.error(f"get_saved_prompt: {e}")
    return None


def get_picked_prompt(user_id: int) -> Optional[dict]:
    pick = _SAVED_PROMPT_PICK.get(user_id)
    if not pick:
        return None
    # refresh from store
    stored = get_saved_prompt(user_id, pick.get("name") or "")
    if stored:
        pick["prompt"] = stored
    return pick


def pick_saved_prompt(user_id: int, name: str) -> Optional[dict]:
    prompt = get_saved_prompt(user_id, name)
    if not prompt:
        return None
    pick = {"name": name, "prompt": prompt, "picked_at": time.time()}
    _SAVED_PROMPT_PICK[user_id] = pick
    return pick


def clear_picked_prompt(user_id: int):
    _SAVED_PROMPT_PICK.pop(user_id, None)


def get_main_keyboard(mode: Optional[str] = None) -> InlineKeyboardMarkup:
    # mode unused for layout; labels stay fixed
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("⚙️ تنظیمات", callback_data="menu_settings"))
    markup.row(InlineKeyboardButton("🎨 Manus (تصویر/تحقیق)", callback_data="mode_manus"))
    markup.row(InlineKeyboardButton("💬 چت با دال", callback_data="mode_chat"))
    markup.row(InlineKeyboardButton("📝 پرامپت‌های من", callback_data="menu_my_prompts"))
    if PROMPTOPIA_URL:
        markup.row(InlineKeyboardButton(PROMPTOPIA_LABEL, url=PROMPTOPIA_URL))
    markup.row(
        InlineKeyboardButton("🔑 کلیدهای من", callback_data="keys_list"),
        InlineKeyboardButton("📊 آمار مصرف", callback_data="menu_usage"),
    )
    markup.row(InlineKeyboardButton("ℹ️ راهنما", callback_data="menu_help"))
    return markup


def get_manus_keyboard(user_id: Optional[int] = None) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("🚀 اجرای تسک جدید", callback_data="manus_run"))
    markup.row(InlineKeyboardButton("📝 استفاده از پرامپت سیو شده", callback_data="manus_pick_prompt"))
    markup.row(InlineKeyboardButton("🖼 مثال: ساخت تصویر", callback_data="manus_example_image"))
    markup.row(InlineKeyboardButton("📊 وضعیت صف", callback_data="manus_queue"))
    if user_id is not None:
        pick = _SAVED_PROMPT_PICK.get(user_id)
        if pick:
            markup.row(
                InlineKeyboardButton(
                    f"✔️ پرامپت فعال: {(pick.get('name') or '')[:24]}",
                    callback_data="manus_clear_pick",
                )
            )
    markup.row(InlineKeyboardButton("🔑 کلید Manus", callback_data="keys_provider:manus"))
    markup.row(InlineKeyboardButton("💳 credits من", callback_data="manus_credits"))
    markup.row(InlineKeyboardButton("💬 چت با دال", callback_data="mode_chat"))
    markup.row(InlineKeyboardButton("🔙 منوی اصلی", callback_data="menu_main"))
    return markup


def my_prompts_text(user_id: int) -> str:
    rows = list_user_prompts(user_id)
    pick = _SAVED_PROMPT_PICK.get(user_id)
    mode = get_bot_mode(user_id)
    lines = [
        "📝 **پرامپت‌های من**",
        "",
        f"حالت ربات: **{mode_fa(mode)}** (`{mode}`)",
        f"پرامپت انتخاب‌شده برای Manus: "
        + (f"**{(pick or {}).get('name')}**" if pick else "—"),
        "",
        "چرا؟ تلگرام روی کپشن عکس **طول متن محدود** دارد؛",
        "پرامپت بلند را ذخیره کن و در Manus فقط **عکس** بفرست.",
        "",
    ]
    if not rows:
        lines.append("هنوز پرامپتی ذخیره نکرده‌ای. «ثبت پرامپت جدید» را بزن.")
    else:
        for r in rows:
            name = r.get("name") or ""
            p = (r.get("prompt") or "").replace("\n", " ")
            lines.append(f"• **{name}** — `{p[:70]}…`")
    return "\n".join(lines)


def manus_send_task_message(
    api_key: str,
    task_id: str,
    prompt: str,
    attachments: Optional[List[dict]] = None,
) -> dict:
    """Continue an existing Manus task (multi-turn). POST /v2/task.sendMessage"""
    text = (prompt or "").strip()[:4800]
    if not text:
        text = (
            "Continue editing based on the previous result. "
            "Apply the requested change and briefly describe it."
        )
    content_parts: List[dict] = [{"type": "text", "text": text}]
    if attachments:
        for att in attachments:
            if att and att.get("file_data"):
                content_parts.append(att)
    payload = {
        "task_id": task_id,
        "message": {"content": content_parts},
    }
    if MANUS_LOCALE:
        payload["locale"] = MANUS_LOCALE
    with httpx.Client(timeout=60.0) as client:
        res = client.post(
            f"{MANUS_BASE_URL}/v2/task.sendMessage",
            headers=manus_headers(api_key),
            json=payload,
        )
    try:
        data = res.json()
    except Exception:
        data = {}
    if res.status_code >= 400 or (isinstance(data, dict) and data.get("ok") is False):
        err = (data or {}).get("error") or {}
        raise RuntimeError(
            f"Manus task.sendMessage {res.status_code}: {err.get('code') or ''} "
            f"{err.get('message') or res.text[:200]}"
        )
    return {"task_id": task_id, "raw": data}


def _manus_try_start_locked(job: dict) -> bool:
    """Call with lock held. Returns True if job started now."""
    global _MANUS_ACTIVE
    if _MANUS_ACTIVE >= MANUS_MAX_CONCURRENT:
        return False
    _MANUS_ACTIVE += 1
    _MANUS_RUNNING_USERS.add(job["user_id"])
    thread = threading.Thread(
        target=_manus_job_runner,
        args=(job,),
        daemon=True,
        name=f"manus-{job['user_id']}",
    )
    thread.start()
    return True


def _manus_pop_next_locked() -> Optional[dict]:
    """Call with lock held. Start next queued job if capacity allows."""
    global _MANUS_ACTIVE
    if _MANUS_ACTIVE >= MANUS_MAX_CONCURRENT:
        return None
    if not _MANUS_QUEUE:
        return None
    job = _MANUS_QUEUE.popleft()
    _MANUS_QUEUED_USERS.discard(job["user_id"])
    _MANUS_ACTIVE += 1
    _MANUS_RUNNING_USERS.add(job["user_id"])
    thread = threading.Thread(
        target=_manus_job_runner,
        args=(job,),
        daemon=True,
        name=f"manus-{job['user_id']}",
    )
    thread.start()
    return job


def _manus_job_runner(job: dict):
    global _MANUS_ACTIVE
    user_id = job["user_id"]
    chat_id = job["chat_id"]
    try:
        try:
            bot.send_message(
                chat_id,
                "🟢 **نوبت شما شد** — Manus Agent در حال اجرا…",
            )
        except Exception:
            pass
        _execute_manus_job(job)
    except Exception as e:
        logging.exception(f"manus job runner error: {e}")
        try:
            bot.send_message(chat_id, f"❌ خطای داخلی در صف Manus:\n`{str(e)[:200]}`")
        except Exception:
            pass
    finally:
        with _MANUS_LOCK:
            _MANUS_ACTIVE = max(0, _MANUS_ACTIVE - 1)
            _MANUS_RUNNING_USERS.discard(user_id)
            _MANUS_JOB_META.pop(user_id, None)
            # promote next waiting job
            _manus_pop_next_locked()


def submit_manus_job(
    message,
    prompt: str,
    user_id: Optional[int] = None,
    chat_id: Optional[int] = None,
    attachments: Optional[List[dict]] = None,
    followup: bool = False,
) -> bool:
    """
    Enqueue Manus work.
    followup=True → continue last task (same conversation) via task.sendMessage.
    followup=False → task.create (new Manus chat).
    """
    global _MANUS_ACTIVE
    if user_id is None:
        user_id = message.from_user.id if message is not None else None
    if chat_id is None:
        chat_id = message.chat.id if message is not None else user_id
    if user_id is None or chat_id is None:
        logging.error("submit_manus_job: missing user/chat")
        return False

    # Recover from zombie jobs (thread died / hang)
    try:
        manus_reap_stale_locks()
    except Exception as e:
        logging.warning(f"reap_stale: {e}")

    api_key = get_manus_key(user_id)
    if not api_key:
        try:
            bot.send_message(
                chat_id,
                "🔐 کلید Manus برای این حساب پیدا نشد.\n"
                "از `/keys` → Manus کلید را ثبت کنید یا `/manusdebug` را بزنید.",
            )
        except Exception:
            pass
        return False

    session = manus_get_session(user_id)
    mode = "create"
    task_id = None
    task_url = None
    if followup:
        if not session:
            try:
                bot.send_message(
                    chat_id,
                    "ℹ️ گفتگوی فعال Manus پیدا نشد (منقضی یا جدید).\n"
                    "از `/manus` → **اجرای تسک جدید** شروع کنید.",
                )
            except Exception:
                pass
            return False
        mode = "followup"
        task_id = session.get("task_id")
        task_url = session.get("task_url")
    else:
        manus_clear_session(user_id)

    job = {
        "user_id": user_id,
        "chat_id": chat_id,
        "prompt": prompt,
        "attachments": attachments,
        "queued_at": time.time(),
        "source_message": message,
        "mode": mode,
        "task_id": task_id,
        "task_url": task_url,
    }

    mode_fa = "ادامه همین گفتگوی Manus" if mode == "followup" else "گفتگوی جدید Manus"
    n_att = len(attachments or [])
    att_note = f" · پیوست: {n_att}" if n_att else ""

    with _MANUS_LOCK:
        if user_id in _MANUS_RUNNING_USERS or user_id in _MANUS_QUEUED_USERS:
            # stale check inside lock
            started = _MANUS_JOB_META.get(user_id, 0)
            if started and (time.time() - started) > MANUS_STALE_LOCK_SEC:
                logging.warning(f"force unlock stale user={user_id}")
                _MANUS_RUNNING_USERS.discard(user_id)
                _MANUS_QUEUED_USERS.discard(user_id)
                _MANUS_JOB_META.pop(user_id, None)
                _MANUS_ACTIVE = max(0, _MANUS_ACTIVE - 1)
            else:
                stats = manus_queue_stats()
                try:
                    bot.send_message(
                        chat_id,
                        "⏳ درخواست قبلی Manus هنوز در حال اجرا یا در صف است.\n"
                        f"{stats['active']}/{stats['max_concurrent']} اجرا · "
                        f"{stats['queued']} در صف\n"
                        "اگر مدت‌هاست گیر کرده: `/manusreset` را بزنید.",
                    )
                except Exception:
                    pass
                return False

        if len(_MANUS_QUEUE) >= MANUS_QUEUE_MAX:
            try:
                bot.send_message(
                    chat_id,
                    f"🚫 صف Manus پر است (`{len(_MANUS_QUEUE)}/{MANUS_QUEUE_MAX}`).\n"
                    "چند دقیقه دیگر دوباره تلاش کنید.",
                )
            except Exception:
                pass
            return False

        if _MANUS_ACTIVE < MANUS_MAX_CONCURRENT:
            _MANUS_ACTIVE += 1
            _MANUS_RUNNING_USERS.add(user_id)
            _MANUS_JOB_META[user_id] = time.time()
            logging.info(
                f"manus START user={user_id} mode={mode} att={n_att} "
                f"prompt_len={len(prompt or '')}"
            )
            threading.Thread(
                target=_manus_job_runner,
                args=(job,),
                daemon=True,
                name=f"manus-{user_id}",
            ).start()
            try:
                bot.send_message(
                    chat_id,
                    f"📨 درخواست Manus پذیرفته شد ({mode_fa}){att_note}\n"
                    "در حال ارسال به API…",
                )
            except Exception:
                pass
            return True

        _MANUS_QUEUE.append(job)
        _MANUS_QUEUED_USERS.add(user_id)
        position = len(_MANUS_QUEUE)
        active = _MANUS_ACTIVE
        logging.info(f"manus QUEUE user={user_id} pos={position}")
        try:
            bot.send_message(
                chat_id,
                f"📥 **درخواست Manus در صف** ({mode_fa}){att_note}\n\n"
                f"نوبت شما: **{position}**\n"
                f"در حال اجرا: `{active}/{MANUS_MAX_CONCURRENT}`\n"
                f"ظرفیت صف: `{position}/{MANUS_QUEUE_MAX}`\n\n"
                "به‌محض آزاد شدن نوبت، اجرا می‌شود.",
            )
        except Exception:
            pass
        return True


def run_manus_for_user(
    message,
    prompt: str,
    user_id: Optional[int] = None,
    chat_id: Optional[int] = None,
    attachments: Optional[List[dict]] = None,
    followup: bool = False,
):
    """Public entry — queue + create or follow-up."""
    return submit_manus_job(
        message,
        prompt,
        user_id=user_id,
        chat_id=chat_id,
        attachments=attachments,
        followup=followup,
    )


def _execute_manus_job(job: dict):
    """End-to-end Manus agent run (already scheduled by the queue)."""
    user_id = job["user_id"]
    chat_id = job["chat_id"]
    prompt = job.get("prompt") or ""
    attachments = job.get("attachments")
    mode = job.get("mode") or "create"
    existing_task_id = job.get("task_id")
    api_key = get_manus_key(user_id)
    if not api_key:
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton("🔑 ثبت کلید Manus", callback_data="keys_set:manus"))
        markup.row(InlineKeyboardButton("🌐 ساخت کلید", url="https://manus.im/app#settings/developers"))
        bot.send_message(
            chat_id,
            "🔐 **Manus Agent** به کلید API نیاز دارد.\n\n"
            "1. در manus.im ثبت‌نام کن\n"
            "2. Settings → Developers → Create API Key\n"
            "3. کلید را در ربات ثبت کن\n\n"
            "کلید فقط یک‌بار نمایش داده می‌شود.",
            reply_markup=markup,
        )
        return

    has_media = bool(attachments)
    media_note = " + پیوست عکس/فایل" if has_media else ""
    if mode == "followup" and existing_task_id:
        head = (
            "✏️ **Manus — ادیت/ادامه در همان گفتگو**\n"
            f"Task: `{str(existing_task_id)[:14]}…`{media_note}\n"
            f"پرامپت: `{prompt[:80]}`\n"
            f"timeout: `{MANUS_TIMEOUT_SEC}s`"
        )
    else:
        head = (
            "🎨 **Manus Agent — گفتگوی جدید**\n"
            f"پرامپت: `{prompt[:80]}`{media_note}\n"
            f"profile: `{MANUS_DEFAULT_PROFILE}` · timeout: `{MANUS_TIMEOUT_SEC}s`\n"
            "ممکن است چند دقیقه طول بکشد.\n"
            "در پایان: **متن + عکس preview + فایل باکیفیت** + امکان **ادامه ادیت**."
        )
    status_msg = bot.send_message(chat_id, head)

    created = None
    if mode == "followup" and existing_task_id:
        try:
            manus_send_task_message(api_key, existing_task_id, prompt, attachments=attachments)
            task_id = existing_task_id
            task_url = job.get("task_url") or f"https://manus.im/app/{task_id}"
        except Exception as e:
            logging.error(f"manus followup failed: {e}")
            err_s = str(e)
            hint = ""
            if "401" in err_s or "unauthenticated" in err_s:
                hint = "\nکلید نامعتبر است."
            elif "rate_limited" in err_s or "429" in err_s:
                hint = "\nRate limit — چند لحظه صبر کنید."
            elif "credit" in err_s.lower():
                hint = "\ncredits کافی نیست."
            elif "not_found" in err_s or "404" in err_s:
                hint = "\nTask قبلی پیدا نشد — تسک جدید بسازید (`/manus`)."
                manus_clear_session(user_id)
            bot.edit_message_text(
                f"❌ خطا در ادامه گفتگوی Manus.\n`{err_s[:300]}`{hint}",
                chat_id=status_msg.chat.id,
                message_id=status_msg.message_id,
                parse_mode="Markdown",
            )
            # fallback: start new if task vanished
            if "not_found" in err_s or "404" in err_s:
                try:
                    created = manus_create_task(api_key, prompt, attachments=attachments)
                    mode = "create"
                    task_id = created.get("task_id")
                    task_url = created.get("task_url") or f"https://manus.im/app/{task_id}"
                    if not task_id:
                        return
                    bot.send_message(chat_id, "🔄 تسک قبلی نبود — گفتگوی **جدید** Manus ساخته شد.")
                except Exception as e2:
                    logging.error(f"manus fallback create: {e2}")
                    return
            else:
                return
    else:
        try:
            created = manus_create_task(api_key, prompt, attachments=attachments)
        except Exception as e:
            logging.error(f"manus create failed: {e}")
            err_s = str(e)
            hint = ""
            if "401" in err_s or "unauthenticated" in err_s:
                hint = "\nکلید نامعتبر است — از «کلیدهای من» اصلاح کنید."
            elif "rate_limited" in err_s or "429" in err_s:
                hint = "\nRate limit — چند لحظه صبر کنید (حدود ۱۰ task/دقیقه)."
            elif "credit" in err_s.lower():
                hint = "\nسهمیه credits کافی نیست."
            elif "invalid_argument" in err_s:
                hint = "\nورودی نامعتبر — پرامپت/فایل را ساده‌تر کنید."
            bot.edit_message_text(
                f"❌ خطا در ساخت Manus task.\n`{err_s[:300]}`{hint}",
                chat_id=status_msg.chat.id,
                message_id=status_msg.message_id,
                parse_mode="Markdown",
            )
            return

        task_id = created.get("task_id")
        task_url = created.get("task_url") or (
            f"https://manus.im/app/{task_id}" if task_id else None
        )
        if not task_id:
            bot.edit_message_text(
                f"❌ task_id دریافت نشد.\n`{str(created)[:200]}`",
                chat_id=status_msg.chat.id,
                message_id=status_msg.message_id,
                parse_mode="Markdown",
            )
            return

    if not task_id:
        return

    # Remember this task for follow-up edits (keep delivered-URL memory on same task)
    manus_save_session(
        user_id,
        task_id,
        task_url,
        title=prompt[:60],
        keep_delivered=(mode == "followup"),
    )
    exclude = manus_delivered_urls(user_id) if mode == "followup" else set()

    try:
        active = get_active_conversation(user_id, get_user_model(user_id))
        tag = "[manus:edit]" if mode == "followup" else "[manus]"
        save_message(
            active.get("id", ""),
            "user",
            f"{tag}{'+media' if has_media else ''} {prompt}",
        )
    except Exception:
        pass

    def on_progress(state):
        st = state.get("agent_status") or "running"
        nfiles = len(state.get("files") or [])
        preview = (state.get("text") or "")[:100]
        files_note = f"\nفایل‌های پیدا شده: {nfiles}" if nfiles else ""
        mode_note = "ادیت" if mode == "followup" else "جدید"
        try:
            bot.edit_message_text(
                f"🎨 Manus ({mode_note}) `{st}`…\nTask: `{task_id}`\n{preview}{files_note}",
                chat_id=status_msg.chat.id,
                message_id=status_msg.message_id,
                parse_mode="Markdown",
            )
        except Exception:
            pass

    result = manus_wait_for_task(api_key, task_id, on_progress=on_progress)
    had_att = bool(attachments)
    exclude = manus_delivered_urls(user_id) if mode == "followup" else set()
    raw = [f for f in (result.get("files") or []) if f.get("url") not in exclude]
    filtered = _filter_output_files(raw, had_attachment=had_att)
    if not filtered and raw:
        # strict filter ate the output — deliver newest non-input candidates
        filtered = _relax_output_files(raw, exclude)
    result["files"] = filtered
    manus_deliver_result(
        chat_id,
        status_msg,
        result,
        task_url,
        api_key=api_key,
        task_id=task_id,
        exclude_urls=exclude,
        followup=(mode == "followup"),
        user_id=user_id,
        had_attachment=had_att,
    )

    # Stay in follow-up mode so the next photo/text continues this task
    if result.get("agent_status") not in ("error",):
        PENDING[user_id] = {
            "action": "manus_followup",
            "task_id": task_id,
            "task_url": task_url or "",
        }
        try:
            markup = InlineKeyboardMarkup()
            markup.row(InlineKeyboardButton("✏️ ادیت بعدی (همین گفتگو)", callback_data="manus_followup_hint"))
            markup.row(InlineKeyboardButton("🆕 گفتگوی جدید Manus", callback_data="manus_run"))
            markup.row(InlineKeyboardButton("⛔️ پایان حالت ادیت", callback_data="manus_end_followup"))
            bot.send_message(
                chat_id,
                f"🔗 **ادامه در همان گفتگوی Manus**\n"
                f"Task: `{str(task_id)[:14]}…`\n\n"
                "برای ادیت بعدی:\n"
                "• **عکس جدید + کپشن** بفرستید، یا\n"
                "• فقط **متن دستور** (مثلاً «پس‌زمینه را آبی کن»)\n\n"
                "این پیام‌ها روی **همین task** می‌روند، نه چت جدید.\n"
                f"مهلت: `{MANUS_FOLLOWUP_TTL_SEC//60}` دقیقه · `/cancel` برای خروج",
                reply_markup=markup,
            )
        except Exception:
            pass

    if result.get("text") or result.get("files"):
        try:
            active = get_active_conversation(user_id, get_user_model(user_id))
            save_message(
                active.get("id", ""),
                "assistant",
                (result.get("text") or "")[:4000]
                or f"[manus files] {len(result.get('files') or [])}",
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def get_settings_keyboard(style: str, context_n: Optional[int], model_ref: str, streaming: bool) -> InlineKeyboardMarkup:
    provider, model_id = parse_model_ref(model_ref)
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton(
            f"🤖 سرویس و مدل  ·  {provider} | {model_id[:24]}",
            callback_data="menu_models",
        )
    )
    style_name = RESPONSE_STYLES.get(style, RESPONSE_STYLES["html"])["name_fa"]
    markup.row(InlineKeyboardButton(f"📝 فرمت پاسخ  ·  {style_name}", callback_data="menu_style"))
    ctx_label = str(context_n) if context_n else f"پیش‌فرض {HISTORY_LIMIT}"
    markup.row(InlineKeyboardButton(f"📜 تاریخچه  ·  {ctx_label}", callback_data="menu_context"))
    stream_label = "روشن" if streaming else "خاموش"
    markup.row(InlineKeyboardButton(f"⚡ استریم پاسخ  ·  {stream_label}", callback_data="menu_stream"))
    markup.row(InlineKeyboardButton("💬 گفتگوها", callback_data="menu_chats"))
    markup.row(InlineKeyboardButton("🔑 کلیدهای API (BYOK)", callback_data="keys_list"))
    markup.row(InlineKeyboardButton("✍️ مدل سفارشی (ID دستی)", callback_data="set_model_custom_id"))
    markup.row(InlineKeyboardButton("🔄 بروزرسانی تنظیمات", callback_data="menu_settings"))
    markup.row(InlineKeyboardButton("🔙 منوی اصلی", callback_data="menu_main"))
    return markup


def get_style_keyboard(current: str) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup()
    for key, meta in RESPONSE_STYLES.items():
        mark = "✅" if current == key else "▫️"
        markup.row(InlineKeyboardButton(f"{mark} {meta['name_fa']}", callback_data=f"set_style:{key}"))
    markup.row(InlineKeyboardButton("🔙 تنظیمات", callback_data="menu_settings"))
    return markup


def get_context_keyboard(current: Optional[int]) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup()
    active = current if current else HISTORY_LIMIT
    for n in CONTEXT_CHOICES:
        mark = "✅" if n == active else "▫️"
        markup.row(InlineKeyboardButton(f"{mark} {n} پیام اخیر", callback_data=f"set_context:{n}"))
    if current:
        markup.row(InlineKeyboardButton(f"▫️ پیش‌فرض سرور ({HISTORY_LIMIT})", callback_data="set_context:0"))
    markup.row(InlineKeyboardButton("🔙 تنظیمات", callback_data="menu_settings"))
    return markup


def get_stream_keyboard(streaming: bool) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup()
    mark_on = "✅" if streaming else "▫️"
    mark_off = "✅" if not streaming else "▫️"
    markup.row(InlineKeyboardButton(f"{mark_on} استریم (تایپ زنده)", callback_data="set_stream:1"))
    markup.row(InlineKeyboardButton(f"{mark_off} غیرفعال (یکجا)", callback_data="set_stream:0"))
    markup.row(InlineKeyboardButton("🔙 تنظیمات", callback_data="menu_settings"))
    return markup


def get_providers_keyboard(selected_provider: Optional[str] = None) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("⭐️ انتخاب سریع مدل", callback_data="menu_models_quick"))
    for pkey, pdata in PROVIDERS.items():
        mark = "✅" if selected_provider == pkey else "▫️"
        markup.row(
            InlineKeyboardButton(f"{mark} {pdata['name_fa']}", callback_data=f"set_model_provider:{pkey}")
        )
    markup.row(InlineKeyboardButton("✍️ مدل دلخواه (ID دستی)", callback_data="set_model_custom_id"))
    markup.row(InlineKeyboardButton("⚙️ تنظیمات", callback_data="menu_settings"))
    markup.row(InlineKeyboardButton("🔙 منوی اصلی", callback_data="menu_main"))
    return markup


def get_quick_models_keyboard(selected_provider: str, selected_model_id: str) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup()
    for pkey, pdata in PROVIDERS.items():
        for mkey, mdata in (pdata.get("models") or {}).items():
            mid = mdata["id"]
            mark = "✅" if (pkey == selected_provider and mid == selected_model_id) else "▫️"
            label = f"{mark} {pdata['name_fa']} · {mdata['name']}"
            markup.row(InlineKeyboardButton(label, callback_data=f"set_model:{pkey}|{mkey}"))
    markup.row(InlineKeyboardButton("✍️ مدل دلخواه (ID دستی)", callback_data="set_model_custom_id"))
    markup.row(InlineKeyboardButton("⚙️ همه سرویس‌ها", callback_data="menu_models"))
    markup.row(InlineKeyboardButton("🔙 تنظیمات", callback_data="menu_settings"))
    return markup


def get_models_keyboard(provider: str, selected_model_id: Optional[str] = None) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup()
    pdata = PROVIDERS.get(provider, {})
    models = pdata.get("models") or {}
    if provider == "custom" or not models:
        markup.row(InlineKeyboardButton("✍️ Model ID سفارشی", callback_data="set_model_custom_id"))
    for mkey, mdata in models.items():
        mid = mdata["id"]
        mark = "✅" if selected_model_id == mid else "▫️"
        markup.row(InlineKeyboardButton(f"{mark} {mdata['name']}", callback_data=f"set_model:{provider}|{mkey}"))
    markup.row(InlineKeyboardButton("⭐️ همه مدل‌ها (سریع)", callback_data="menu_models_quick"))
    markup.row(InlineKeyboardButton("🔁 تغییر سرویس‌دهنده", callback_data="menu_models"))
    markup.row(InlineKeyboardButton("⚙️ تنظیمات", callback_data="menu_settings"))
    markup.row(InlineKeyboardButton("🔙 منوی اصلی", callback_data="menu_main"))
    return markup


def get_keys_keyboard(user_keys: Dict[str, dict]) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup()
    for pkey, pdata in PROVIDERS.items():
        if pkey == "custom":
            continue
        has = pkey in user_keys
        mark = "🟢" if has else "⚪️"
        markup.row(InlineKeyboardButton(f"{mark} {pdata['name_fa']}", callback_data=f"keys_provider:{pkey}"))
    markup.row(InlineKeyboardButton("🟣 سفارشی (سایت دیگر)", callback_data="keys_provider:custom"))
    markup.row(InlineKeyboardButton("🔄 بروزرسانی", callback_data="keys_list"))
    markup.row(InlineKeyboardButton("🎨 Manus Agent", callback_data="menu_manus"))
    markup.row(InlineKeyboardButton("⚙️ تنظیمات", callback_data="menu_settings"))
    markup.row(InlineKeyboardButton("🔙 منوی اصلی", callback_data="menu_main"))
    return markup


def get_key_provider_detail_keyboard(provider: str, has_key: bool) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup()
    pdata = PROVIDERS.get(provider, {})
    if pdata.get("get_key_url"):
        markup.row(InlineKeyboardButton("🌐 دریافت کلید", url=pdata["get_key_url"]))
    if provider == "custom":
        markup.row(InlineKeyboardButton("✍️ ثبت سرویس سفارشی", callback_data="keys_set:custom"))
    else:
        action = "✏️ تغییر کلید" if has_key else "➕ افزودن کلید"
        markup.row(InlineKeyboardButton(action, callback_data=f"keys_set:{provider}"))
    if has_key:
        markup.row(InlineKeyboardButton("🗑 حذف کلید", callback_data=f"keys_del:{provider}"))
        if provider == "manus":
            markup.row(InlineKeyboardButton("🎨 اجرای Manus Agent", callback_data="menu_manus"))
            markup.row(InlineKeyboardButton("💳 نمایش credits", callback_data="manus_credits"))
        elif provider != "custom":
            markup.row(
                InlineKeyboardButton("🤖 استفاده از این سرویس", callback_data=f"set_model_provider:{provider}")
            )
    markup.row(InlineKeyboardButton("🔙 کلیدهای من", callback_data="keys_list"))
    markup.row(InlineKeyboardButton("⚙️ تنظیمات", callback_data="menu_settings"))
    return markup


def manus_intro_text(user_id: int) -> str:
    keys = list_user_keys(user_id)
    has = "manus" in keys
    shared = bool(MANUS_SHARED_API_KEY)
    key_state = (
        f"🟢 کلید شخصی `{mask_key(keys['manus'].get('api_key',''))}`"
        if has
        else ("🟡 کلید مشترک ربات" if shared else "⚪️ کلید ثبت نشده")
    )
    return (
        "🎨 **Manus Agent Mode**\n\n"
        "این بخش با **API رسمی Manus** کار می‌کند (OpenAI chat نیست).\n"
        "یک **تسک agent** می‌سازد و نتیجه (متن / فایل / عکس) را در تلگرام می‌فرستد.\n\n"
        f"**کلید:** {key_state}\n"
        f"**profile:** `{MANUS_DEFAULT_PROFILE}`\n"
        f"**timeout:** `{MANUS_TIMEOUT_SEC}s`\n\n"
        "**نحوه دریافت کلید:**\n"
        "1. [manus.im](https://manus.im) ثبت‌نام\n"
        "2. [Settings → Developers](https://manus.im/app#settings/developers)\n"
        "3. Create API Key → کپی (فقط یک‌بار)\n"
        "4. در ربات: کلیدهای من → Manus → افزودن کلید\n\n"
        "**مثال پرامپت تصویر:**\n"
        "`Generate a flat illustration of a robot holding a Telegram logo, pastel colors`\n\n"
        "**ویرایش عکس:**\n"
        "«استفاده از پرامپت سیو شده» → نام پرامپت → فقط **عکس** بفرستید.\n"
        "یا «اجرای تسک جدید» → عکس + کپشن.\n\n"
        "**ادامه ادیت در همان گفتگو:**\n"
        "پس از هر نتیجه، عکس/متن بعدی را بفرستید — روی **همین task** می‌رود "
        "(`task.sendMessage`)، نه چت جدید.\n"
        "«🆕 گفتگوی جدید» فقط وقتی لازم است که موضوع عوض شود.\n\n"
        f"**صف همزمانی:** حداکثر **{MANUS_MAX_CONCURRENT}** اجرای همزمان؛ "
        f"بقیه در صف تا **{MANUS_QUEUE_MAX}** نفر.\n"
        "دلیل: محدودیت RAM سرور رایگان (جلوگیری از شات‌داون).\n"
        "هر کاربر فقط **۱** درخواست فعال/در صف.\n\n"
        "⚠️ محدودیت API Manus: ~۱۰ task/دقیقه به‌ازای هر کلید + credits.\n"
        "چت معمولی از Dahl/Groq/… استفاده می‌شود."
    )


def get_chats_keyboard(chats: List[dict], active_id: Optional[str]) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("🆕 گفتگوی جدید", callback_data="chat_new"))
    for chat in chats:
        cid = str(chat.get("id", ""))
        title = (chat.get("title") or "بدون عنوان")[:28]
        mark = "✅" if cid == str(active_id or "") else "▫️"
        markup.row(InlineKeyboardButton(f"{mark} {title}", callback_data=f"chat_open:{cid[:36]}"))
    markup.row(InlineKeyboardButton("✏️ تغییر نام گفتگوی فعال", callback_data="chat_rename"))
    markup.row(InlineKeyboardButton("🔄 بروزرسانی", callback_data="menu_chats"))
    markup.row(InlineKeyboardButton("⚙️ تنظیمات", callback_data="menu_settings"))
    markup.row(InlineKeyboardButton("🔙 منوی اصلی", callback_data="menu_main"))
    return markup


# ---------------------------------------------------------------------------
# UI texts
# ---------------------------------------------------------------------------

def settings_overview_text(
    settings: dict,
    keys: Dict[str, dict],
    streaming_flag: bool,
    user_id: Optional[int] = None,
) -> str:
    model_ref = settings.get("selected_model", "")
    provider, model_id = parse_model_ref(model_ref)
    style = settings.get("response_style", "html")
    ctx = settings.get("context_messages") or HISTORY_LIMIT
    style_meta = RESPONSE_STYLES.get(style, RESPONSE_STYLES["html"])
    has_key = provider in keys or (
        ALLOW_SHARED_KEY and provider == LEGACY_PROVIDER and DAHL_API_KEY
    )
    active = settings.get("active_conversation_id")
    mode = get_bot_mode(user_id) if user_id is not None else BOT_MODE_CHAT
    pick = _SAVED_PROMPT_PICK.get(user_id) if user_id is not None else None
    lines = [
        "⚙️ **تنظیمات ربات**",
        "",
        f"🎯 **حالت فعال:** {mode_fa(mode)} (`{mode}`)",
        f"🤖 **مدل ارسال/پاسخ:** `{provider}` / `{model_id}`",
        f"   کلید: `{'دارد' if has_key else 'ندارد'}`",
        f"📝 **فرمت پاسخ:** {style_meta['name_fa']}",
        f"📜 **تاریخچه:** {ctx} پیام",
        f"⚡ **استریم:** `{'روشن' if streaming_flag else 'خاموش'}`",
        f"💬 **گفتگوی فعال:** `{(str(active)[:8] + '…') if active else '—'}`",
        f"📝 **پرامپت Manus:** `{(pick or {}).get('name') or '—'}`",
        "",
        "حالت‌ها انحصاری‌اند: یا چت دال، یا Manus.",
        "از دکمه‌ها هر بخش را تغییر دهید.",
    ]
    if not settings.get("columns_ok", True):
        lines.append("")
        lines.append("⚠️ ستون‌های تنظیمات ناقص‌اند — `sql/02` و `sql/03` را اجرا کنید.")
    return "\n".join(lines)


def keys_overview_text(user_keys: Dict[str, dict]) -> str:
    lines = [
        "🔑 **کلیدهای API شما (BYOK)**",
        "",
        "کلید شخصی → سهمیه روی اکانت خود شما.",
        "",
    ]
    for pkey, pdata in PROVIDERS.items():
        row = user_keys.get(pkey)
        if row:
            base = row.get("base_url") or pdata.get("base_url") or "—"
            lines.append(f"🟢 **{pdata['name_fa']}** — `{mask_key(row.get('api_key',''))}`")
            lines.append(f"   Base: `{base}`")
        else:
            lines.append(f"⚪️ **{pdata['name_fa']}** — کلید ثبت نشده")
    lines.append("")
    lines.append(
        f"کلید مشترک ربات (Dahl): `{'فعال' if (ALLOW_SHARED_KEY and DAHL_API_KEY) else 'غیرفعال'}`"
    )
    return "\n".join(lines)


def keys_provider_detail_text(provider: str, row: Optional[dict]) -> str:
    pdata = PROVIDERS.get(provider, {})
    lines = [
        f"🔑 **{pdata.get('name_fa', provider)}**",
        "",
        pdata.get("note_fa") or "",
        "",
    ]
    if pdata.get("get_key_url"):
        lines.append(f"دریافت کلید: {pdata['get_key_url']}")
    lines.append("")
    if row:
        lines.append("وضعیت: 🟢 ثبت‌شده")
        lines.append(f"کلید: `{mask_key(row.get('api_key',''))}`")
        if row.get("base_url"):
            lines.append(f"Base URL: `{row.get('base_url')}`")
        lines.append("می‌توانید تغییر دهید یا حذف کنید.")
    else:
        lines.append("وضعیت: ⚪️ کلیدی ثبت نشده")
        if provider == "custom":
            lines.append("فرمت سفارشی: `BASE_URL ||| API_KEY ||| MODEL_ID`")
        else:
            lines.append("کلید را از سایت بالا بگیرید و «افزودن کلید» را بزنید.")
    return "\n".join(lines)


def chats_overview_text(chats: List[dict], active_id: Optional[str]) -> str:
    lines = [
        "💬 **گفتگوهای شما**",
        "",
        f"فعال: `{(str(active_id)[:8] + '…') if active_id else '—'}`",
        "",
    ]
    if not chats:
        lines.append("هنوز گفتگویی نیست. «گفتگوی جدید» را بزنید.")
    for i, chat in enumerate(chats, 1):
        cid = str(chat.get("id", ""))
        title = chat.get("title") or "بدون عنوان"
        mark = "✅" if cid == str(active_id or "") else "▫️"
        model = chat.get("model") or ""
        provider, mid = parse_model_ref(model)
        lines.append(f"{mark} `{i}` **{title}**")
        lines.append(f"   `{provider}` / `{mid[:40]}` · `{cid[:8]}…`")
    lines.append("")
    lines.append("🗄 فقط **گفتگوی فعلی** روی سرور نگه داشته می‌شود.")
    lines.append("با «گفتگوی جدید»، قبلی از دیتابیس پاک می‌شود (نه از تلگرام).")
    return "\n".join(lines)


def usage_text_for_user(user_id: int) -> str:
    settings = get_user_settings(user_id)
    model_ref = settings["selected_model"]
    provider, model_id = parse_model_ref(model_ref)
    keys = list_user_keys(user_id)
    q = quota_status(user_id)
    source = "shared"
    if provider in keys:
        source = "user"
    stats = {"total_tokens": q.get("lifetime_used") or 0, "error": False, "by_model": {}, "calls": 0,
             "input_tokens": 0, "output_tokens": 0}
    if not SKIP_USAGE_TABLE:
        live = get_usage_stats(user_id)
        if not live.get("error"):
            stats = live
            # lifetime counter is authoritative when usage table is pruned
            if q.get("ok"):
                stats["total_tokens"] = max(stats["total_tokens"], q.get("lifetime_used") or 0)
    applies = (
        "سهمیه روزانه نیز روی کلید شخصی اعمال می‌شود"
        if QUOTA_APPLIES_TO_OWN_KEYS
        else "سهمیه روزانه فقط برای کلید مشترک ربات — **سهمیه کلی برای همه**"
    )
    quota_note = format_quota_block(q, applies)
    storage_note = (
        "🗄 سیاست ذخیره‌سازی: فقط **گفتگوی فعلی** روی سرور می‌ماند؛ "
        "با چت جدید، تاریخچه قبلی از دیتابیس پاک می‌شود (چت تلگرام شما دست‌نخورده می‌ماند)."
    )
    return (
        "📊 **گزارش مصرف توکن**\n\n"
        f"{quota_note}\n"
        f"🤖 مدل: `{provider}` / `{model_id}`\n"
        f"🔐 منبع کلید فعلی: `{source}`\n"
        f"⚡ استریم: `{'روشن' if STREAMING_ENABLED else '—'}`\n"
        f"💾 نگهداری پیام در گفتگوی فعال: آخرین `{KEEP_MESSAGES_IN_ACTIVE_CHAT}` پیام\n\n"
        f"{storage_note}"
    )


def admin_stats_text() -> str:
    try:
        users_res = sb_get("users?select=telegram_id,last_active_at,is_admin")
        users = users_res.json() or []
        total_users = len(users) if isinstance(users, list) else 0
    except Exception as e:
        return f"خطا در آمار کاربران: {e}"

    today_prefix = today_date_str()
    active_today = 0
    if isinstance(users, list):
        for u in users:
            la = str(u.get("last_active_at") or "")
            if la.startswith(today_prefix):
                active_today += 1

    try:
        usage_res = sb_get("usage?select=model,total_tokens,input_tokens,output_tokens,created_at")
        usage_rows = usage_res.json() or []
    except Exception as e:
        usage_rows = []
        logging.error(f"admin usage: {e}")

    total_tokens = 0
    today_tokens = 0
    by_model = defaultdict(int)
    for row in usage_rows or []:
        tot = int(row.get("total_tokens") or 0)
        total_tokens += tot
        created = str(row.get("created_at") or "")
        if created.startswith(today_prefix):
            today_tokens += tot
        by_model[row.get("model") or "unknown"] += tot

    top_models = sorted(by_model.items(), key=lambda x: -x[1])[:8]

    try:
        keys_res = sb_get("user_api_keys?select=provider")
        key_rows = keys_res.json() or []
        by_provider = defaultdict(int)
        for r in key_rows or []:
            by_provider[r.get("provider") or "?"] += 1
        key_line = ", ".join(f"{p}:{c}" for p, c in sorted(by_provider.items(), key=lambda x: -x[1])) or "—"
    except Exception:
        key_line = "—"

    try:
        conv_res = sb_get("conversations?select=id")
        conv_rows = conv_res.json() or []
        total_chats = len(conv_rows) if isinstance(conv_rows, list) else 0
    except Exception:
        total_chats = 0

    try:
        msg_res = sb_get("messages?select=id&limit=1")
        # count not available cheaply; skip or use range
        msg_count_note = "—"
    except Exception:
        msg_count_note = "—"

    model_lines = "\n".join(f"• `{m}`: `{t:,}`" for m, t in top_models) or "—"
    if SKIP_USAGE_TABLE and not top_models:
        model_lines = "(usage table disabled — see user_quotas)"

    # Lifetime totals from quotas (always available)
    try:
        qres = sb_get("user_quotas?select=user_id,lifetime_used,lifetime_limit,used_today")
        qrows = qres.json() or []
        life_used = sum(int(r.get("lifetime_used") or 0) for r in qrows if isinstance(r, dict))
        life_lim = sum(int(r.get("lifetime_limit") or 0) for r in qrows if isinstance(r, dict))
        today_q = sum(int(r.get("used_today") or 0) for r in qrows if isinstance(r, dict))
        quota_users = len(qrows) if isinstance(qrows, list) else 0
    except Exception:
        life_used = life_lim = today_q = quota_users = 0

    return (
        "🛡 **آمار ادمین**\n\n"
        f"کاربران کل: `{total_users}`\n"
        f"فعال امروز: `{active_today}`\n"
        f"گفتگوها (فقط فعلی‌ها): `{total_chats}`\n"
        f"کاربران با ردیف سهمیه: `{quota_users}`\n\n"
        f"**سهمیه‌ها**\n"
        f"مصرف کلی همه: `{life_used:,}`\n"
        f"سقف کلی همه: `{life_lim:,}`\n"
        f"مصرف امروز (شمارنده): `{today_q:,}`\n"
        f"پیش‌فرض روزانه: `{DEFAULT_DAILY_TOKEN_QUOTA:,}`\n"
        f"پیش‌فرض مادام‌العمر: `{DEFAULT_LIFETIME_TOKEN_QUOTA:,}`\n"
        f"هشدار از: `{LIFETIME_WARN_PCT:.0f}%`\n\n"
        f"**لاگ usage:** `{'خاموش' if SKIP_USAGE_TABLE else 'روشن'}`\n"
        f"**مدل‌های پرتکرار:**\n{model_lines}\n\n"
        f"**کلیدهای BYOK:** {key_line}\n"
        f"سهمیه روزانه روی کلید شخصی: `{'بله' if QUOTA_APPLIES_TO_OWN_KEYS else 'خیر'}`\n"
        f"استریم: `{'روشن' if STREAMING_ENABLED else 'خاموش'}`\n"
        f"کلید مشترک Dahl: `{'ست‌شده' if DAHL_API_KEY else '—'}`\n"
        f"نسخه ربات: `{BOT_VERSION}`\n"
        f"شناسه‌های ادمین (env): `{','.join(str(x) for x in sorted(ADMIN_TELEGRAM_IDS)) or '—'}`"
    )


def get_admin_keyboard() -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("🔄 بروزرسانی آمار", callback_data="admin_refresh"))
    markup.row(InlineKeyboardButton("🔙 منوی اصلی", callback_data="menu_main"))
    return markup


# ---------------------------------------------------------------------------
# Message handlers
# ---------------------------------------------------------------------------

@bot.message_handler(commands=["start"])
def handle_start(message):
    ensure_user(message.from_user)
    user_id = message.from_user.id
    settings = get_user_settings(user_id)
    model_ref = settings["selected_model"]
    provider, model_id = parse_model_ref(model_ref)
    keys = list_user_keys(user_id)
    has_own = provider in keys
    conv = get_active_conversation(user_id, model_ref)
    cred_src = (
        "کلید شخصی"
        if has_own
        else ("کلید مشترک ربات" if ALLOW_SHARED_KEY and DAHL_API_KEY else "بدون کلید")
    )
    style = settings.get("response_style", "html")
    q = quota_status(user_id)
    admin_tag = " · 👑 ادمین" if is_admin(user_id) else ""
    text = (
        f"سلام {message.from_user.first_name}!{admin_tag}\n\n"
        f"🤖 **مدل پاسخ:** `{provider}` / `{model_id}`\n"
        f"🔐 **منبع کلید:** {cred_src}\n"
        f"📝 **فرمت:** `{style}`\n"
        f"⚡ **استریم:** `{'روشن' if STREAMING_ENABLED else 'خاموش'}`\n"
        f"💬 **گفتگو:** `{(str(conv.get('id',''))[:8] + '…')}`\n"
        f"📊 **سهمیه امروز:** `{q['used']:,}` / `{q['limit']:,}`\n"
        f"🧾 نسخه ربات: `{BOT_VERSION}`\n"
        f"🎯 **حالت:** {mode_fa(get_bot_mode(user_id))}\n\n"
        "یکی را انتخاب کن:\n"
        "• **⚙️ تنظیمات**\n"
        "• **🎨 Manus** — تصویر/تحقیق + پرامپت سیو شده\n"
        "• **💬 چت با دال** — فقط چت متنی\n"
        "• **📝 پرامپت‌های من** — ثبت/استفاده پرامپت بلند\n\n"
        f"دستورات: `/mode` `/saveprompt` `/myprompts` `/keys` `/manus` `/prompts` `/help`"
        + (" `/admin`" if is_admin(user_id) else "")
    )
    bot.reply_to(message, text, reply_markup=get_main_keyboard())


@bot.message_handler(commands=["help"])
def handle_help(message):
    admin_line = "\n• `/admin` — آمار ادمین" if is_admin(message.from_user.id) else ""
    text = (
        "ℹ️ **راهنما**\n\n"
        "• پیام متنی → پاسخ مدل با تاریخچه گفتگوی فعال\n"
        "• ⚙️ تنظیمات — مدل، فرمت، استریم، تاریخچه، سهمیه\n"
        "• 💬 گفتگوها — چند مکالمه با نام\n"
        "• 🔑 کلیدهای من — BYOK (Dahl, Groq, …)\n"
        "• 📚 پرامپت‌های آماده — `/prompts` (در مرورگر باز می‌شود)\n"
        "• `/clear` — گفتگوی تازه\n"
        "• `/chats` — لیست گفتگوها\n"
        "• `/settings` — تنظیمات\n"
        "• `/manus` — دستیار Manus (تصویر/agent)\n"
        "• `/manus <prompt>` — اجرای مستقیم"
        f"{admin_line}\n\n"
        "پاسخ خام؟ → تنظیمات → فرمت → **HTML**"
    )
    bot.reply_to(message, text, reply_markup=get_main_keyboard())


@bot.message_handler(commands=["prompts", "promptopia", "gallery"])
def handle_prompts(message):
    if not PROMPTOPIA_URL:
        bot.reply_to(message, "گالری پرامپت هنوز تنظیم نشده است.")
        return
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton(PROMPTOPIA_LABEL, url=PROMPTOPIA_URL))
    markup.row(InlineKeyboardButton("🔙 منوی اصلی", callback_data="menu_main"))
    bot.reply_to(
        message,
        "📚 **پرامپت‌های آماده**\n\n"
        "اگر ایده پرامپت نداری، از گالری آماده استفاده کن:\n"
        f"{PROMPTOPIA_URL}\n\n"
        "روی دکمه بزن تا در **مرورگر** باز شود.\n"
        "پرامپت را کپی کن → در ربات Paste کن (یا به Manus بده).",
        reply_markup=markup,
    )


@bot.message_handler(commands=["settings", "config"])
def handle_settings_cmd(message):
    ensure_user(message.from_user)
    user_id = message.from_user.id
    settings = get_user_settings(user_id)
    keys = list_user_keys(user_id)
    bot.reply_to(
        message,
        settings_overview_text(settings, keys, STREAMING_ENABLED, user_id=user_id),
        reply_markup=get_settings_keyboard(
            settings.get("response_style", "html"),
            settings.get("context_messages"),
            settings.get("selected_model"),
            STREAMING_ENABLED,
        ),
    )


@bot.message_handler(commands=["keys", "setkey", "byok"])
def handle_keys_cmd(message):
    ensure_user(message.from_user)
    user_id = message.from_user.id
    keys = list_user_keys(user_id)
    bot.reply_to(message, keys_overview_text(keys), reply_markup=get_keys_keyboard(keys))


@bot.message_handler(commands=["chats", "chatlist"])
def handle_chats_cmd(message):
    ensure_user(message.from_user)
    user_id = message.from_user.id
    settings = get_user_settings(user_id)
    chats = list_conversations(user_id)
    bot.reply_to(
        message,
        chats_overview_text(chats, settings.get("active_conversation_id")),
        reply_markup=get_chats_keyboard(chats, settings.get("active_conversation_id")),
    )


@bot.message_handler(commands=["clear", "newchat"])
def handle_clear(message):
    ensure_user(message.from_user)
    user_id = message.from_user.id
    model_ref = get_user_model(user_id)
    try:
        conv = create_conversation(user_id, model_ref, title="گفتگوی جدید")
        manus_clear_session(user_id)
        PENDING.pop(user_id, None)
        provider, model_id = parse_model_ref(model_ref)
        bot.reply_to(
            message,
            f"🧹 گفتگوی جدید ساخته و فعال شد.\n"
            f"شناسه: `{str(conv.get('id',''))[:8]}…`\n"
            f"مدل: `{provider}` / `{model_id}`\n\n"
            "🗄 **سیاست ذخیره:** فقط همین گفتگو روی سرور می‌ماند.\n"
            "🎨 سشن Manus هم ریست شد.\n"
            "گفتگوهای قبلی از دیتابیس پاک شدند؛ چت‌های تلگرام شما دست‌نخورده است.",
        )
    except Exception as e:
        logging.error(f"clear error: {e}")
        bot.reply_to(message, "خطا در ساخت گفتگوی جدید.")


@bot.message_handler(commands=["admin"])
def handle_admin(message):
    user_id = message.from_user.id
    try:
        if not is_admin(user_id):
            env_ids = ",".join(str(x) for x in sorted(ADMIN_TELEGRAM_IDS)) or "—"
            bot.reply_to(
                message,
                "⛔️ دسترسی ادمین ندارید.\n\n"
                f"آیدی شما: `{user_id}`\n"
                f"ADMIN_TELEGRAM_IDS روی سرور: `{env_ids}`\n\n"
                "در Deployka آیدی عددی خود را در env اضافه کنید:\n"
                "`ADMIN_TELEGRAM_IDS=YOUR_ID`\n"
                "سپس سرویس را ری‌استارت کنید.",
                parse_mode="Markdown",
            )
            return
        try:
            text = admin_stats_text()
        except Exception as e:
            logging.exception(f"admin_stats_text: {e}")
            text = f"❌ خطا در ساخت آمار ادمین:\n`{str(e)[:300]}`"
        send_admin_text(
            message.chat.id,
            text,
            reply_to_message=message,
            reply_markup=get_admin_keyboard(),
        )
    except Exception as e:
        logging.exception(f"handle_admin: {e}")
        try:
            bot.reply_to(message, f"❌ خطای /admin:\n`{str(e)[:250]}`")
        except Exception:
            pass


@bot.message_handler(commands=["mode"])
def handle_mode_cmd(message):
    ensure_user(message.from_user)
    user_id = message.from_user.id
    parts = (message.text or "").split()
    if len(parts) > 1:
        arg = parts[1].strip().lower()
        if arg in ("chat", "dahl", "چت"):
            set_bot_mode(user_id, BOT_MODE_CHAT)
            bot.reply_to(
                message,
                f"🎯 حالت: **{mode_fa(BOT_MODE_CHAT)}**\n"
                "فقط چت با مدل‌های دال/Groq/… فعال است.\n"
                "برای تصویر: منو → 🎨 Manus",
                reply_markup=get_main_keyboard(),
            )
            return
        if arg in ("manus", "agent", "تصویر"):
            set_bot_mode(user_id, BOT_MODE_MANUS)
            bot.reply_to(
                message,
                f"🎯 حالت: **{mode_fa(BOT_MODE_MANUS)}**\n"
                "فقط Manus (تصویر/agent) فعال است.\n"
                "برای چت عادی: منو → 💬 چت با دال",
                reply_markup=get_manus_keyboard(user_id),
            )
            return
    mode = get_bot_mode(user_id)
    bot.reply_to(
        message,
        f"🎯 حالت فعلی: **{mode_fa(mode)}**\n\n"
        "`/mode chat` — فقط چت دال\n"
        "`/mode manus` — فقط Manus",
        reply_markup=get_main_keyboard(),
    )


@bot.message_handler(commands=["myprompts", "savedprompts"])
def handle_my_prompts(message):
    ensure_user(message.from_user)
    user_id = message.from_user.id
    bot.reply_to(
        message,
        my_prompts_text(user_id),
        reply_markup=get_my_prompts_keyboard(user_id),
    )


@bot.message_handler(commands=["saveprompt"])
def handle_saveprompt_cmd(message):
    ensure_user(message.from_user)
    user_id = message.from_user.id
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip():
        PENDING[user_id] = {"action": "prompt_save_name", "name": parts[1].strip()[:64]}
        bot.reply_to(
            message,
            f"📝 نام: **{parts[1].strip()[:64]}**\n\n"
            "حالا **متن کامل پرامپت** را بفرستید (هرچقدر طولانی).",
        )
        return
    PENDING[user_id] = {"action": "prompt_save_name"}
    bot.reply_to(
        message,
        "📝 **ثبت پرامپت جدید**\n\n"
        "مرحله ۱ — **نام کوتاه** پرامپت را بفرستید:\n"
        "مثال: `edit_product_studio`\n\n"
        "لغو: `/cancel`",
    )


@bot.message_handler(commands=["manus", "agent"])
def handle_manus_cmd(message):
    ensure_user(message.from_user)
    user_id = message.from_user.id
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip():
        run_manus_for_user(
            message,
            parts[1].strip(),
            user_id=user_id,
            chat_id=message.chat.id,
        )
        return
    bot.reply_to(message, manus_intro_text(user_id), reply_markup=get_manus_keyboard(user_id))


@bot.message_handler(commands=["manusqueue", "queue"])
def handle_manus_queue_cmd(message):
    info = manus_queue_status_text()
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("🔄 بروزرسانی", callback_data="manus_queue"))
    markup.row(InlineKeyboardButton("🎨 Manus", callback_data="menu_manus"))
    bot.reply_to(message, info["text"], reply_markup=markup)


@bot.message_handler(commands=["manusreset"])
def handle_manus_reset(message):
    user_id = message.from_user.id
    n = manus_force_unlock(user_id)
    PENDING.pop(user_id, None)
    manus_clear_session(user_id)
    bot.reply_to(
        message,
        "🧹 قفل/صف Manus برای شما پاک شد.\n"
        f"وضعیت: `{'آزاد شد' if n else 'چیزی قفل نبود'}`\n"
        "دوباره `/manus` → اجرای تسک جدید.",
    )


@bot.message_handler(commands=["manusdebug"])
def handle_manus_debug(message):
    user_id = message.from_user.id
    keys = list_user_keys(user_id)
    sess = manus_get_session(user_id)
    pending = PENDING.get(user_id) or {}
    stats = manus_queue_stats()
    has_key = bool(keys.get("manus", {}).get("api_key")) or bool(MANUS_SHARED_API_KEY)
    ping = "—"
    try:
        key_for_ping = get_manus_key(user_id) or "invalid"
        with httpx.Client(timeout=8.0) as client:
            r = client.get(
                f"{MANUS_BASE_URL}/v2/usage.availableCredits",
                headers=manus_headers(key_for_ping),
            )
        ping = f"HTTP {r.status_code}"
        if r.status_code == 200:
            try:
                d = (r.json() or {}).get("data") or {}
                ping += f" · credits={d.get('total_credits', '?')}"
            except Exception:
                ping += " (key OK)"
        elif r.status_code in (401,):
            ping += " (key invalid)"
        elif r.status_code in (403,):
            ping += " (endpoint forbidden — key may still work for task.create)"
        elif r.status_code == 402:
            ping += " (no credits / not allocated)"
    except Exception as e:
        ping = f"error: {e}"
    text = (
        f"🔧 **Manus debug** · v`{BOT_VERSION}`\n\n"
        f"کلید Manus: `{'دارد' if keys.get('manus') else 'ندارد'}` · "
        f"مشترک: `{'ست' if MANUS_SHARED_API_KEY else '—'}` · "
        f"قابل استفاده: `{'بله' if has_key else 'خیر'}`\n"
        f"mask: `{mask_key(keys.get('manus', {}).get('api_key') or '')}`\n"
        f"session: `{(sess or {}).get('task_id') or '—'}`\n"
        f"pending: `{pending.get('action') or '—'}`\n"
        f"صف: `{stats['active']}/{stats['max_concurrent']}` · "
        f"queued `{stats['queued']}`\n"
        f"شما در running set: `{user_id in _MANUS_RUNNING_USERS}`\n"
        f"شما در queued set: `{user_id in _MANUS_QUEUED_USERS}`\n"
        f"API credits ping: {ping}\n"
        f"base: `{MANUS_BASE_URL}`\n"
        f"timeout: `{MANUS_TIMEOUT_SEC}s` · stale: `{MANUS_STALE_LOCK_SEC}s`\n"
        f"سهمیه کلی: `{quota_status(user_id).get('lifetime_used', 0):,}` / "
        f"`{quota_status(user_id).get('lifetime_limit', 0):,}`"
    )
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("🧹 پاک‌سازی قفل/صف", callback_data="manus_reset_btn"))
    markup.row(InlineKeyboardButton("🎨 Manus", callback_data="menu_manus"))
    bot.reply_to(message, text, reply_markup=markup)


def _dahl_fallback_reply(message, user_id: int, note: str = ""):
    """When Manus prompt is empty but Dahl key exists, answer via chat model."""
    ensure_user(message.from_user)
    settings = get_user_settings(user_id)
    cred = resolve_inference_credentials(user_id)
    if not cred:
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton("💬 چت با دال", callback_data="mode_chat"))
        markup.row(InlineKeyboardButton("🔑 کلیدها", callback_data="keys_list"))
        bot.reply_to(
            message,
            (note + "\n" if note else "")
            + "کلید دال/مدل چت هم در دسترس نیست.\n"
            "حالت را روی **چت با دال** بگذارید یا کلید ثبت کنید.",
            reply_markup=markup,
        )
        return
    try:
        model_ref = settings.get("selected_model") or get_user_model(user_id)
        conv = get_active_conversation(user_id, model_ref)
        history = get_conversation_history(
            conv.get("id"), limit=settings.get("context_messages") or HISTORY_LIMIT
        )
        user_text = (message.text or "").strip() or "hello"
        msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
        msgs.extend(history)
        msgs.append({"role": "user", "content": user_text})
        result = call_llm(
            cred["base_url"], cred["api_key"], cred["model_id"], msgs, stream=False
        )
        save_message(conv.get("id", ""), "user", user_text,
                     input_tokens=result.get("prompt_tokens", 0))
        save_message(conv.get("id", ""), "assistant", result.get("content", ""),
                     output_tokens=result.get("completion_tokens", 0))
        quota_add_usage(user_id, result.get("total_tokens", 0))
        prefix = f"{note}\n\n" if note else ""
        send_bot_reply(
            message.chat.id,
            None,
            prefix + (result.get("content") or "…"),
            style=settings.get("response_style", "html"),
        ) if False else bot.reply_to(
            message,
            truncate_message(prefix + (result.get("content") or "…")),
        )
    except Exception as e:
        logging.error(f"dahl fallback: {e}")
        bot.reply_to(message, f"خطا در چت دال:\n`{str(e)[:200]}`")


@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    user_id = call.from_user.id
    data = call.data or ""
    ensure_user(call.from_user)

    def answer(text=None, alert=False):
        try:
            bot.answer_callback_query(call.id, text or "", show_alert=alert)
        except Exception:
            pass

    # ----- Bot modes (exclusive) -----
    if data == "mode_chat":
        set_bot_mode(user_id, BOT_MODE_CHAT)
        answer("حالت: چت با دال")
        bot.edit_message_text(
            f"🎯 **حالت فعال:** {mode_fa(BOT_MODE_CHAT)}\n\n"
            "فقط چت با مدل‌های دال/Groq/… کار می‌کند.\n"
            "برای تصویر/Manus از منوی اصلی 🎨 را بزنید.",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=get_main_keyboard(),
        )
        return

    if data == "mode_manus":
        set_bot_mode(user_id, BOT_MODE_MANUS)
        # Entering Manus does not force follow-up; new photo = new task
        PENDING.pop(user_id, None)
        answer("حالت: Manus — عکس+کپشن = تسک جدید")
        bot.edit_message_text(
            manus_intro_text(user_id) + f"\n\n🎯 حالت: **{mode_fa(BOT_MODE_MANUS)}**",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=get_manus_keyboard(user_id),
        )
        return

    # ----- Saved prompts -----
    if data == "menu_my_prompts":
        answer()
        bot.edit_message_text(
            my_prompts_text(user_id),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=get_my_prompts_keyboard(user_id),
        )
        return

    if data == "prompt_save_start":
        PENDING[user_id] = {"action": "prompt_save_name"}
        answer()
        bot.send_message(
            call.message.chat.id,
            "📝 نام کوتاه پرامپت را بفرستید (مثلاً `product_edit`):",
        )
        return

    if data == "manus_pick_prompt":
        rows = list_user_prompts(user_id)
        if not rows:
            PENDING[user_id] = {"action": "prompt_save_name"}
            answer("اول پرامپت ثبت کنید", True)
            bot.send_message(
                call.message.chat.id,
                "پرامپت سیو شده‌ای ندارید.\nنام پرامپت را بفرستید تا ثبت شود:",
            )
            return
        PENDING[user_id] = {"action": "prompt_pick_name"}
        answer()
        names = "\n".join(f"• `{r.get('name')}`" for r in rows[:15])
        bot.send_message(
            call.message.chat.id,
            "📝 **استفاده از پرامپت سیو شده**\n\n"
            f"پرامپت‌های شما:\n{names}\n\n"
            "نام پرامپت را بفرستید، سپس **فقط عکس** بفرستید.",
        )
        return

    if data == "manus_clear_pick":
        clear_picked_prompt(user_id)
        answer("پرامپت انتخاب‌شده پاک شد", True)
        bot.edit_message_text(
            manus_intro_text(user_id),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=get_manus_keyboard(user_id),
        )
        return

    if data.startswith("prompt_use:"):
        name = data.split(":", 1)[1]
        pick = pick_saved_prompt(user_id, name)
        if not pick:
            answer("پیدا نشد", True)
            return
        set_bot_mode(user_id, BOT_MODE_MANUS)
        answer(f"فعال: {name}", True)
        bot.edit_message_text(
            f"✅ پرامپت **{name}** فعال شد.\n🎯 حالت: Manus\n\n"
            "حالا **فقط عکس** بفرستید.",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=get_manus_keyboard(user_id),
        )
        return

    if data.startswith("prompt_del:"):
        name = data.split(":", 1)[1]
        delete_user_prompt(user_id, name)
        answer("حذف شد", True)
        bot.edit_message_text(
            my_prompts_text(user_id),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=get_my_prompts_keyboard(user_id),
        )
        return

    # ----- Manus agent mode -----
    if data == "menu_manus":
        answer()
        bot.edit_message_text(
            manus_intro_text(user_id),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=get_manus_keyboard(user_id),
        )
        return

    if data == "manus_run":
        set_bot_mode(user_id, BOT_MODE_MANUS)
        manus_clear_session(user_id)
        # keep saved prompt pick unless cleared
        PENDING[user_id] = {"action": "manus_prompt"}
        answer()
        bot.send_message(
            call.message.chat.id,
            "🆕 **گفتگوی جدید Manus**\n\n"
            "پرامپت را بفرستید:\n"
            "• **فقط متن:** `Generate an image of a cat astronaut`\n"
            "• **عکس + کپشن:** برای ویرایش عکس\n\n"
            "اگر می‌خواهید روی **همان نتیجه قبلی** ادیت کنید، این دکمه را نزنید — "
            "کافی است عکس/متن بعدی را بفرستید (حالت ادیت پس از هر نتیجه فعال می‌ماند).\n"
            "لغو: `/cancel`",
        )
        return

    if data == "manus_followup_hint":
        answer()
        sess = manus_get_session(user_id)
        if not sess:
            bot.send_message(call.message.chat.id, "گفتگوی فعال Manus نیست — `/manus` جدید بزنید.")
            return
        PENDING[user_id] = {
            "action": "manus_followup",
            "task_id": sess.get("task_id"),
            "task_url": sess.get("task_url") or "",
        }
        bot.send_message(
            call.message.chat.id,
            "✏️ حالت **ادیت در همان گفتگو** فعال است.\n"
            f"Task: `{str(sess.get('task_id'))[:14]}…`\n\n"
            "عکس + کپشن یا فقط متن دستور بفرستید.",
        )
        return

    if data == "manus_end_followup":
        manus_clear_session(user_id)
        PENDING.pop(user_id, None)
        answer("حالت ادیت Manus بسته شد", True)
        bot.edit_message_text(
            "⛔️ حالت ادیت Manus پایان یافت.\nبرای شروع: `/manus`",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=get_manus_keyboard(user_id),
        )
        return

    if data == "manus_example_image":
        answer()
        run_manus_for_user(
            message=call.message,
            prompt=(
                "Generate a high-quality flat illustration for a Telegram AI bot: "
                "friendly robot, pastel blue background, no text in the image."
            ),
            user_id=user_id,
            chat_id=call.message.chat.id,
        )
        return

    if data == "manus_reset_btn":
        n = manus_force_unlock(user_id)
        PENDING.pop(user_id, None)
        manus_clear_session(user_id)
        answer("پاک شد" if n else "چیزی قفل نبود", True)
        bot.edit_message_text(
            "🧹 قفل/صف Manus پاک شد. دوباره `/manus` را بزنید.",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=get_manus_keyboard(user_id),
        )
        return

    if data == "manus_queue":
        info = manus_queue_status_text()
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton("🔄 بروزرسانی", callback_data="manus_queue"))
        markup.row(InlineKeyboardButton("🔙 Manus", callback_data="menu_manus"))
        answer()
        bot.edit_message_text(
            info["text"],
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode="Markdown",
        )
        return

    if data == "manus_credits":
        api_key = get_manus_key(user_id)
        answer()
        if not api_key:
            bot.edit_message_text(
                manus_intro_text(user_id),
                call.message.chat.id,
                call.message.message_id,
                reply_markup=get_manus_keyboard(user_id),
            )
            return
        text = manus_credits_text(api_key)
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton("🔙 Manus", callback_data="menu_manus"))
        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode="Markdown",
        )
        return

    if data == "set_model_provider:manus":
        answer("Manus حالت Agent است — از دکمه 🎨 استفاده کنید", True)
        bot.edit_message_text(
            manus_intro_text(user_id),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=get_manus_keyboard(user_id),
        )
        return

    if data == "menu_main":
        settings = get_user_settings(user_id)
        provider, model_id = parse_model_ref(settings["selected_model"])
        keys = list_user_keys(user_id)
        src = "شخصی" if provider in keys else "مشترک/—"
        q = quota_status(user_id)
        text = (
            f"منوی اصلی\n\n"
            f"🤖 مدل: `{provider}` / `{model_id}`\n"
            f"🔐 کلید: {src} · 📝 `{settings.get('response_style','html')}`\n"
            f"📊 سهمیه امروز: `{q['used']:,}/{q['limit']:,}`"
        )
        bot.edit_message_text(
            text, call.message.chat.id, call.message.message_id, reply_markup=get_main_keyboard()
        )
        answer()
        return

    if data == "admin_refresh":
        if not is_admin(user_id):
            answer("دسترسی ندارید", True)
            return
        try:
            text = admin_stats_text()
        except Exception as e:
            text = f"❌ خطا در آمار:\n`{str(e)[:250]}`"
        try:
            bot.edit_message_text(
                text,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=get_admin_keyboard(),
                parse_mode="Markdown",
            )
        except Exception as e:
            logging.warning(f"admin_refresh markdown: {e}")
            try:
                bot.edit_message_text(
                    text.replace("**", "").replace("`", ""),
                    call.message.chat.id,
                    call.message.message_id,
                    reply_markup=get_admin_keyboard(),
                )
            except Exception as e2:
                logging.error(f"admin_refresh plain: {e2}")
        answer()
        return

    if data == "menu_settings":
        settings = get_user_settings(user_id)
        keys = list_user_keys(user_id)
        bot.edit_message_text(
            settings_overview_text(settings, keys, STREAMING_ENABLED, user_id=user_id),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=get_settings_keyboard(
                settings.get("response_style", "html"),
                settings.get("context_messages"),
                settings.get("selected_model"),
                STREAMING_ENABLED,
            ),
        )
        answer()
        return

    if data == "menu_stream":
        settings = get_user_settings(user_id)
        user_stream = settings.get("stream_enabled")
        effective = STREAMING_ENABLED if user_stream is None else bool(user_stream)
        bot.edit_message_text(
            f"⚡ **استریم پاسخ**\n\n"
            f"پیش‌فرض سرور: `{'روشن' if STREAMING_ENABLED else 'خاموش'}`\n"
            f"شما: `{'روشن' if effective else 'خاموش'}`\n"
            f"فاصله آپدیت: `{STREAM_EDIT_INTERVAL}s`\n\n"
            "در حالت استریم، پیام ربات هنگام تولید به‌روز می‌شود (تایپ زنده).",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=get_stream_keyboard(effective),
        )
        answer()
        return

    if data.startswith("set_stream:"):
        val = data.split(":", 1)[1] == "1"
        ok = set_user_settings(user_id, stream_enabled=val)
        if ok:
            answer("ذخیره شد")
        else:
            answer(
                "ستون stream_enabled ذخیره نشد — sql/03 را اجرا کنید. از پیش‌فرض سرور استفاده می‌شود.",
                True,
            )
        settings = get_user_settings(user_id)
        effective = val if ok else STREAMING_ENABLED
        if settings.get("stream_enabled") is not None:
            effective = bool(settings.get("stream_enabled"))
        bot.edit_message_text(
            settings_overview_text(settings, list_user_keys(user_id), effective, user_id=user_id),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=get_settings_keyboard(
                settings.get("response_style", "html"),
                settings.get("context_messages"),
                settings.get("selected_model"),
                effective,
            ),
        )
        return

    if data == "menu_style":
        settings = get_user_settings(user_id)
        current = settings.get("response_style", "html")
        rows = "\n".join(
            f"{'✅' if k==current else '▫️'} **{v['name_fa']}** — {v['desc']}"
            for k, v in RESPONSE_STYLES.items()
        )
        bot.edit_message_text(
            f"📝 **فرمت ارسال پاسخ**\n\n{rows}",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=get_style_keyboard(current),
        )
        answer()
        return

    if data.startswith("set_style:"):
        style = data.split(":", 1)[1]
        if style not in RESPONSE_STYLES:
            answer("فرمت نامعتبر", True)
            return
        ok = set_user_settings(user_id, response_style=style)
        answer(
            f"فرمت: {RESPONSE_STYLES[style]['name_fa']}" if ok else "ذخیره نشد — sql/02",
            not ok,
        )
        settings = get_user_settings(user_id)
        bot.edit_message_text(
            settings_overview_text(settings, list_user_keys(user_id), STREAMING_ENABLED, user_id=user_id),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=get_settings_keyboard(
                settings.get("response_style", "html"),
                settings.get("context_messages"),
                settings.get("selected_model"),
                STREAMING_ENABLED,
            ),
        )
        return

    if data == "menu_context":
        settings = get_user_settings(user_id)
        current = settings.get("context_messages")
        bot.edit_message_text(
            f"📜 **تعداد پیام‌های تاریخچه**\n\nالان: `{current or HISTORY_LIMIT}`",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=get_context_keyboard(current),
        )
        answer()
        return

    if data.startswith("set_context:"):
        n = int(data.split(":", 1)[1])
        ok = set_user_settings(user_id, context_messages=n if n > 0 else None)
        answer("ذخیره شد" if ok else "ذخیره نشد — sql/02", not ok)
        settings = get_user_settings(user_id)
        bot.edit_message_text(
            settings_overview_text(settings, list_user_keys(user_id), STREAMING_ENABLED, user_id=user_id),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=get_settings_keyboard(
                settings.get("response_style", "html"),
                settings.get("context_messages"),
                settings.get("selected_model"),
                STREAMING_ENABLED,
            ),
        )
        return

    if data == "menu_chats":
        settings = get_user_settings(user_id)
        chats = list_conversations(user_id)
        bot.edit_message_text(
            chats_overview_text(chats, settings.get("active_conversation_id")),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=get_chats_keyboard(chats, settings.get("active_conversation_id")),
        )
        answer()
        return

    if data == "chat_new":
        model_ref = get_user_model(user_id)
        conv = create_conversation(user_id, model_ref, title="گفتگوی جدید")
        # New Telegram chat session → also reset Manus follow-up context
        manus_clear_session(user_id)
        PENDING.pop(user_id, None)
        chats = list_conversations(user_id)
        answer("گفتگوی جدید فعال شد", True)
        bot.edit_message_text(
            chats_overview_text(chats, conv["id"])
            + "\n\n🎨 سشن Manus هم ریست شد — عکس بعدی = **تسک جدید**.",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=get_chats_keyboard(chats, conv["id"]),
        )
        return

    if data.startswith("chat_open:"):
        cid = data.split(":", 1)[1]
        conv = get_conversation_by_id(cid)
        if not conv or conv.get("user_id") != user_id:
            answer("گفتگو پیدا نشد", True)
            return
        set_active_conversation(user_id, cid)
        chats = list_conversations(user_id)
        answer(f"فعال شد: {conv.get('title')}", True)
        bot.edit_message_text(
            chats_overview_text(chats, cid),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=get_chats_keyboard(chats, cid),
        )
        return

    if data == "chat_rename":
        settings = get_user_settings(user_id)
        active = settings.get("active_conversation_id")
        if not active:
            answer("گفتگوی فعالی نیست", True)
            return
        PENDING[user_id] = {"action": "rename_chat", "conversation_id": str(active)}
        answer()
        bot.send_message(
            call.message.chat.id,
            "عنوان جدید گفتگوی فعال را بفرستید (لغو: `/cancel`):",
        )
        return

    if data == "menu_models":
        settings = get_user_settings(user_id)
        provider, model_id = parse_model_ref(settings["selected_model"])
        bot.edit_message_text(
            f"**انتخاب سرویس‌دهنده**\n\nالان: `{provider}` / `{model_id}`\n\n"
            "⭐️ انتخاب سریع · یا سرویس را جدا بزنید · یا ID دستی",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=get_providers_keyboard(provider),
        )
        answer()
        return

    if data == "menu_models_quick":
        settings = get_user_settings(user_id)
        provider, model_id = parse_model_ref(settings["selected_model"])
        bot.edit_message_text(
            f"⭐️ **انتخاب سریع مدل**\n\nالان: `{provider}` / `{model_id}`",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=get_quick_models_keyboard(provider, model_id),
        )
        answer()
        return

    if data.startswith("set_model_provider:"):
        provider = data.split(":", 1)[1]
        if provider not in PROVIDERS:
            answer("سرویس نامعتبر", True)
            return
        _, model_id = parse_model_ref(get_user_model(user_id))
        pdata = PROVIDERS[provider]
        model_ids = {m["id"] for m in (pdata.get("models") or {}).values()}
        if provider == "custom":
            prev_provider, prev_model = parse_model_ref(get_user_model(user_id))
            model_id = prev_model if prev_provider == "custom" else (
                pdata.get("default_model_id") or "custom-model"
            )
        elif model_id not in model_ids:
            model_id = pdata.get("default_model_id") or next(iter(model_ids), "unknown")
        set_user_model(user_id, format_model_ref(provider, model_id))
        keys = list_user_keys(user_id)
        if provider not in keys and not (
            ALLOW_SHARED_KEY and provider == LEGACY_PROVIDER and DAHL_API_KEY
        ):
            answer("کلید این سرویس ثبت نشده", True)
        else:
            answer("به‌روز شد")
        bot.edit_message_text(
            f"مدل‌های **{pdata['name_fa']}**\n\nفعلی: `{model_id}`",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=get_models_keyboard(provider, model_id),
        )
        return

    if data.startswith("set_model:"):
        body = data.split(":", 1)[1]
        if MODEL_SEP not in body:
            answer("فرمت نامعتبر", True)
            return
        provider, mkey = body.split(MODEL_SEP, 1)
        pdata = PROVIDERS.get(provider)
        if not pdata:
            answer("سرویس نامعتبر", True)
            return
        models = pdata.get("models") or {}
        model_id = models[mkey]["id"] if mkey in models else mkey
        set_user_model(user_id, format_model_ref(provider, model_id))
        answer(f"{provider} · {model_id}", True)
        settings = get_user_settings(user_id)
        bot.edit_message_text(
            settings_overview_text(settings, list_user_keys(user_id), STREAMING_ENABLED, user_id=user_id),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=get_settings_keyboard(
                settings.get("response_style", "html"),
                settings.get("context_messages"),
                settings.get("selected_model"),
                STREAMING_ENABLED,
            ),
        )
        return

    if data == "set_model_custom_id":
        PENDING[user_id] = {"action": "set_custom_model"}
        answer()
        bot.send_message(
            call.message.chat.id,
            "Model ID را بفرستید:\n"
            "• `zai-org/GLM-5.3-Flash`\n"
            "• `groq|llama-3.3-70b-versatile`\n"
            "• یا `BASE_URL ||| API_KEY ||| MODEL_ID` برای سرویس سفارشی",
        )
        return

    if data == "keys_list":
        keys = list_user_keys(user_id)
        bot.edit_message_text(
            keys_overview_text(keys),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=get_keys_keyboard(keys),
        )
        answer()
        return

    if data.startswith("keys_provider:"):
        provider = data.split(":", 1)[1]
        keys = list_user_keys(user_id)
        row = keys.get(provider)
        bot.edit_message_text(
            keys_provider_detail_text(provider, row),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=get_key_provider_detail_keyboard(provider, row is not None),
        )
        answer()
        return

    if data.startswith("keys_set:"):
        provider = data.split(":", 1)[1]
        if provider == "custom":
            PENDING[user_id] = {"action": "set_custom_base"}
            answer()
            bot.send_message(call.message.chat.id, "مرحله ۱/۳ — Base URL سفارشی:")
            return
        PENDING[user_id] = {"action": "set_key", "provider": provider}
        pdata = PROVIDERS.get(provider, {})
        answer()
        url_line = f"\n{pdata.get('get_key_url')}" if pdata.get("get_key_url") else ""
        bot.send_message(
            call.message.chat.id,
            f"کلید **{pdata.get('name_fa', provider)}** را بفرستید.{url_line}\nلغو: `/cancel`",
        )
        return

    if data.startswith("keys_del:"):
        provider = data.split(":", 1)[1]
        ok = delete_user_key(user_id, provider)
        answer("حذف شد" if ok else "خطا در حذف", True)
        keys = list_user_keys(user_id)
        bot.edit_message_text(
            keys_overview_text(keys),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=get_keys_keyboard(keys),
        )
        return

    if data == "menu_usage":
        text = usage_text_for_user(user_id)
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton("⚙️ تنظیمات", callback_data="menu_settings"))
        markup.row(InlineKeyboardButton("💬 گفتگوها", callback_data="menu_chats"))
        markup.row(InlineKeyboardButton("🔙 بازگشت", callback_data="menu_main"))
        bot.edit_message_text(
            text, call.message.chat.id, call.message.message_id, reply_markup=markup
        )
        answer()
        return

    if data == "menu_help":
        text = (
            "ℹ️ **راهنما**\n\n"
            "• چت عادی: Dahl / Groq / OpenRouter / … (BYOK)\n"
            "• **🎨 Manus Agent** — تصویر/تحقیق با API رسمی Manus\n"
            "• `/manus` یا `/manus <prompt>`\n"
            "• 📚 `/prompts` — پرامپت‌های آماده (مرورگر)\n"
            "• `/settings` `/keys` `/chats` `/clear` `/admin`\n\n"
            "پاسخ خام؟ → تنظیمات → فرمت → **HTML**"
        )
        markup = InlineKeyboardMarkup()
        if PROMPTOPIA_URL:
            markup.row(InlineKeyboardButton(PROMPTOPIA_LABEL, url=PROMPTOPIA_URL))
        markup.row(InlineKeyboardButton("⚙️ تنظیمات", callback_data="menu_settings"))
        markup.row(InlineKeyboardButton("🔙 بازگشت", callback_data="menu_main"))
        bot.edit_message_text(
            text, call.message.chat.id, call.message.message_id, reply_markup=markup
        )
        answer()
        return

    answer()


@bot.message_handler(commands=["cancel"])
def handle_cancel(message):
    user_id = message.from_user.id
    PENDING.pop(user_id, None)
    # keep manus session unless user ends follow-up explicitly
    bot.reply_to(
        message,
        "عملیات لغو شد.\n"
        "اگر می‌خواهید حالت ادیت Manus هم بسته شود: دکمه «⛔️ پایان حالت ادیت» یا دوباره `/cancel` بعد از نتیجه.",
    )


@bot.message_handler(content_types=["photo", "document"], func=lambda m: True)
def handle_manus_media(message):
    """
    Photo/document routing:
    - Explicit follow-up pending → same Manus task
    - Caption == saved prompt name → NEW Manus task with that stored text
    - Caption (free text) in Manus mode → NEW Manus task (task.create)
    - Photo only + picked prompt → NEW Manus task
    - Otherwise → short guide
    """
    user_id = message.from_user.id
    pending = PENDING.get(user_id) or {}
    caption = (message.caption or "").strip()
    action = pending.get("action")
    try:
        session = manus_get_session(user_id)
    except Exception:
        session = None
    manus_reap_stale_locks()
    ensure_user(message.from_user)
    mode = get_bot_mode(user_id)
    pick = get_picked_prompt(user_id)
    status = None

    try:
        # 1) Explicit follow-up only
        if action == "manus_followup" and session:
            status = bot.reply_to(message, "⏬ ادیت در همان گفتگوی Manus…")
            attachment, att_err = extract_media_from_message(message)
            prompt = caption or (pick.get("prompt") if pick else "") or (
                "Continue from the previous result and briefly describe the change."
            )
            if not attachment and not prompt:
                bot.edit_message_text(
                    "کپشن/پرامپت ندارید.",
                    chat_id=status.chat.id,
                    message_id=status.message_id,
                )
                return
            ok = run_manus_for_user(
                message,
                prompt,
                user_id=user_id,
                chat_id=message.chat.id,
                attachments=[attachment] if attachment else None,
                followup=True,
            )
            if not ok:
                bot.edit_message_text(
                    "❌ ثبت ادیت Manus ناموفق. `/manusdebug`",
                    chat_id=status.chat.id,
                    message_id=status.message_id,
                )
            return

        # Caption exactly matches a saved prompt name → NEW task
        named = find_prompt_by_name(user_id, caption) if caption else None
        if named and named.get("prompt"):
            status = bot.reply_to(
                message,
                f"⏬ عکس + پرامپت **{named.get('name')}**\n"
                f"({len(named['prompt'])} کاراکتر) → **تسک جدید Manus**…",
            )
            attachment, att_err = extract_media_from_message(message)
            if not attachment:
                bot.edit_message_text(
                    f"❌ دریافت عکس ناموفق.\n{att_err}",
                    chat_id=status.chat.id,
                    message_id=status.message_id,
                )
                return
            pick_saved_prompt(user_id, named["name"])
            set_bot_mode(user_id, BOT_MODE_MANUS)
            PENDING.pop(user_id, None)
            ok = run_manus_for_user(
                message,
                named["prompt"],
                user_id=user_id,
                chat_id=message.chat.id,
                attachments=[attachment],
                followup=False,
            )
            if not ok:
                bot.edit_message_text(
                    "❌ ثبت Manus ناموفق. `/manusdebug`",
                    chat_id=status.chat.id,
                    message_id=status.message_id,
                )
            return

        # Chat mode (no saved-name caption): do not run Manus
        if mode == BOT_MODE_CHAT and action != "manus_prompt":
            markup = InlineKeyboardMarkup()
            markup.row(InlineKeyboardButton("🎨 Manus (تصویر + متن)", callback_data="mode_manus"))
            markup.row(InlineKeyboardButton("💬 چت با دال", callback_data="mode_chat"))
            bot.reply_to(
                message,
                "🎯 حالت فعلی **چت با دال** است.\n\n"
                "برای عکس + متن:\n"
                "**🎨 Manus** را بزنید، بعد عکس + کپشن بفرستید\n"
                "یا کپشن = نام پرامپت سیوشده باشد.",
                reply_markup=markup,
            )
            return

        # 2) Manus mode / manus_prompt / photo+caption → NEW task
        # (NOT auto-followup just because an old session exists)
        set_bot_mode(user_id, BOT_MODE_MANUS)
        PENDING.pop(user_id, None)
        if action == "manus_prompt" or mode == BOT_MODE_MANUS or caption or pick:
            manus_clear_session(user_id)  # new task by default
            status = bot.reply_to(
                message,
                "⏬ دریافت عکس + متن → **تسک جدید Manus**…",
            )
            attachment, att_err = extract_media_from_message(message)
            if not attachment:
                bot.edit_message_text(
                    f"❌ دریافت عکس ناموفق.\n{att_err}\nعکس کوچک‌تر بفرستید.",
                    chat_id=status.chat.id,
                    message_id=status.message_id,
                )
                return

            prompt = caption or (pick.get("prompt") if pick else "") or ""
            if not prompt:
                bot.edit_message_text(
                    "کپشن ندارید و پرامپت سیو شده هم انتخاب نشده.\n"
                    "کپشن بنویسید یا نام پرامپت سیوشده را در کپشن بگذارید.",
                    chat_id=status.chat.id,
                    message_id=status.message_id,
                )
                return

            try:
                bot.edit_message_text(
                    f"✅ عکس آماده · پرامپت ({len(prompt)} کاراکتر)\n"
                    f"ارسال به Manus (task جدید)…\n`{prompt[:60]}`",
                    chat_id=status.chat.id,
                    message_id=status.message_id,
                )
            except Exception:
                pass

            ok = run_manus_for_user(
                message,
                prompt,
                user_id=user_id,
                chat_id=message.chat.id,
                attachments=[attachment],
                followup=False,
            )
            if not ok:
                bot.edit_message_text(
                    "❌ ثبت درخواست Manus ناموفق. `/manusdebug`",
                    chat_id=status.chat.id,
                    message_id=status.message_id,
                )
            return

        # Fallback guide
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton("🎨 حالت Manus", callback_data="mode_manus"))
        markup.row(InlineKeyboardButton("🚀 اجرای تسک جدید", callback_data="manus_run"))
        markup.row(InlineKeyboardButton("📝 پرامپت سیو شده", callback_data="manus_pick_prompt"))
        bot.reply_to(
            message,
            "برای Manus:\n"
            "1) **🎨 Manus** را بزنید\n"
            "2) **عکس + کپشن** بفرستید (متن آزاد یا نام پرامپت سیوشده)\n"
            "ادامه ادیت فقط پس از پیام نتیجه و با دکمه/پیام بعدی است.",
            reply_markup=markup,
        )
    except Exception as e:
        logging.exception(f"handle_manus_media: {e}")
        try:
            if status:
                bot.edit_message_text(
                    f"❌ خطای داخلی Manus:\n`{str(e)[:250]}`",
                    chat_id=status.chat.id,
                    message_id=status.message_id,
                )
            else:
                bot.reply_to(message, f"❌ خطای داخلی:\n`{str(e)[:250]}`")
        except Exception:
            pass


def _handle_pending_input(message) -> bool:
    """Return True if pending flow consumed the message."""
    user_id = message.from_user.id
    pending = PENDING.get(user_id)
    if not pending:
        return False
    action = pending.get("action")
    raw = (message.text or "").strip()

    if action == "manus_followup":
        task_id = pending.get("task_id")
        prompt = raw
        if not prompt:
            bot.reply_to(
                message,
                "دستور ادیت خالی است.\n"
                "متن بنویسید (مثلاً «پس‌زمینه را آبی کن») یا عکس+کپشن بفرستید.",
            )
            return True
        # keep follow-up mode until job finishes (job re-arms it)
        run_manus_for_user(
            message,
            prompt.strip(),
            user_id=user_id,
            chat_id=message.chat.id,
            followup=True,
        )
        return True

    if action == "prompt_save_name":
        name = (pending.get("name") or raw or "").strip()[:64]
        if not name:
            bot.reply_to(message, "نام خالی است. دوباره `/saveprompt`.")
            return True
        if not raw or (pending.get("name") and len(raw) > 2 and pending.get("awaiting_prompt") != True and pending.get("name") == raw.strip() and False):
            pass
        # If name came from command and this message is the prompt text
        if pending.get("name") and pending.get("await_text"):
            prompt = raw
            if len(prompt) < 5:
                bot.reply_to(message, "متن پرامپت خیلی کوتاه است.")
                return True
            PENDING.pop(user_id, None)
            ok = save_user_prompt(user_id, pending["name"], prompt)
            bot.reply_to(
                message,
                f"{'✅' if ok else '⚠️'} پرامپت **{pending['name']}** ذخیره شد "
                f"({len(prompt)} کاراکتر).\n\n"
                "برای استفاده: 🎨 Manus → **استفاده از پرامپت سیو شده** → فقط عکس بفرست.",
                reply_markup=get_my_prompts_keyboard(user_id),
            )
            return True
        # This message is the name → ask for text
        if not pending.get("name"):
            name = raw.strip()[:64]
            if not name:
                bot.reply_to(message, "نام خالی است.")
                return True
            PENDING[user_id] = {"action": "prompt_save_name", "name": name, "await_text": True}
            bot.reply_to(message, f"📝 نام: **{name}**\n\nمتن کامل پرامپت را بفرستید:")
            return True
        # name already set from /saveprompt name → this is prompt text
        prompt = raw
        if len(prompt) < 5:
            bot.reply_to(message, "متن پرامپت خیلی کوتاه است. متن بلندتر بفرستید.")
            return True
        pname = pending.get("name")
        PENDING.pop(user_id, None)
        ok = save_user_prompt(user_id, pname, prompt)
        bot.reply_to(
            message,
            f"{'✅' if ok else '⚠️'} پرامپت **{pname}** ذخیره شد ({len(prompt)} کاراکتر).\n\n"
            "🎨 Manus → استفاده از پرامپت سیو شده → عکس",
            reply_markup=get_my_prompts_keyboard(user_id),
        )
        return True

    if action == "prompt_pick_name":
        name = raw.strip()
        PENDING.pop(user_id, None)
        pick = pick_saved_prompt(user_id, name)
        if not pick:
            bot.reply_to(
                message,
                "پرامپتی با این نام پیدا نشد.\n`/myprompts` را ببینید.",
                reply_markup=get_my_prompts_keyboard(user_id),
            )
            return True
        set_bot_mode(user_id, BOT_MODE_MANUS)
        bot.reply_to(
            message,
            f"✅ پرامپت **{pick['name']}** انتخاب شد ({len(pick['prompt'])} کاراکتر).\n"
            f"🎯 حالت: **Manus**\n\n"
            "حالا فقط **عکس** بفرستید (کپشن لازم نیست).\n"
            "اگر کپشن بدهید، همان جایگزین پرامپت سیو شده می‌شود.",
            reply_markup=get_manus_keyboard(user_id),
        )
        return True

    if action == "manus_prompt":
        prompt = pending.get("preset") or raw
        PENDING.pop(user_id, None)
        manus_clear_session(user_id)
        if not prompt or not prompt.strip():
            # empty → try Dahl if available
            cred = resolve_inference_credentials(user_id)
            if cred:
                bot.reply_to(
                    message,
                    "ℹ️ پرامپت Manus خالی بود — تلاش با **چت دال**…",
                )
                # fall through by setting a synthetic request? send directly:
                _dahl_fallback_reply(
                    message,
                    user_id,
                    note="ℹ️ پرامپت Manus خالی بود — پاسخ از **چت دال**:",
                )
                return True
            bot.reply_to(
                message,
                "پرامپت خالی است. `/manus` یا پرامپت سیو شده انتخاب کنید.",
            )
            return True
        run_manus_for_user(
            message,
            prompt.strip(),
            user_id=user_id,
            chat_id=message.chat.id,
            followup=False,
        )
        return True

    if action == "rename_chat":
        cid = pending.get("conversation_id")
        PENDING.pop(user_id, None)
        if not cid or not raw:
            bot.reply_to(message, "عنوان نامعتبر.")
            return True
        ok = rename_conversation(cid, raw)
        chats = list_conversations(user_id)
        bot.reply_to(
            message,
            f"{'✅' if ok else '❌'} نام گفتگو: **{raw[:60]}**",
            reply_markup=get_chats_keyboard(chats, cid),
        )
        return True

    if action == "set_key":
        provider = pending.get("provider")
        if not provider:
            PENDING.pop(user_id, None)
            return True
        if len(raw) < 8:
            bot.reply_to(message, "کلید نامعتبر. دوباره یا `/cancel`.")
            return True
        if upsert_user_key(user_id, provider, raw, None):
            PENDING.pop(user_id, None)
            pdata = PROVIDERS[provider]
            if provider == "manus":
                bot.reply_to(
                    message,
                    f"✅ کلید **Manus** ذخیره شد: `{mask_key(raw)}`\n\n"
                    "از منوی اصلی **🎨 Manus Agent** یا `/manus` استفاده کنید.",
                    reply_markup=get_main_keyboard(),
                )
                return True
            model_id = pdata.get("default_model_id") or ""
            if model_id:
                set_user_model(user_id, format_model_ref(provider, model_id))
            bot.reply_to(
                message,
                f"✅ کلید **{pdata['name_fa']}**: `{mask_key(raw)}`\nمدل: `{provider}` / `{model_id}`",
                reply_markup=get_main_keyboard(),
            )
        else:
            bot.reply_to(message, "خطا در ذخیره کلید (RLS / جدول user_api_keys؟).")
        return True

    if action == "set_custom_base":
        if not raw.startswith("http"):
            bot.reply_to(message, "Base URL باید http/https باشد.")
            return True
        PENDING[user_id] = {"action": "set_custom_key", "base_url": raw.rstrip("/")}
        bot.reply_to(message, "مرحله ۲/۳ — API Key:")
        return True

    if action == "set_custom_key":
        base_url = pending.get("base_url")
        if not base_url or len(raw) < 4:
            bot.reply_to(message, "کلید نامعتبر.")
            return True
        PENDING[user_id] = {"action": "set_custom_model", "base_url": base_url, "api_key": raw}
        bot.reply_to(message, "مرحله ۳/۳ — Model ID (یا `provider|model`):")
        return True

    if action == "set_custom_model":
        base_url = pending.get("base_url")
        api_key = pending.get("api_key")
        model_id = raw.strip()
        if not model_id:
            bot.reply_to(message, "Model ID خالی.")
            return True
        if base_url and api_key:
            if upsert_user_key(user_id, "custom", api_key, base_url=base_url):
                PENDING.pop(user_id, None)
                if "|" in model_id:
                    p, mid = model_id.split("|", 1)
                    set_user_model(user_id, format_model_ref(p.strip(), mid.strip()))
                else:
                    set_user_model(user_id, format_model_ref("custom", model_id))
                bot.reply_to(
                    message,
                    f"✅ سفارشی ذخیره شد.\n`{base_url}` · `{mask_key(api_key)}`\nمدل: `{model_id}`",
                    reply_markup=get_main_keyboard(),
                )
            else:
                bot.reply_to(message, "خطا در ذخیره کلید سفارشی.")
            return True
        # only model id
        PENDING.pop(user_id, None)
        if "|" in model_id:
            p, mid = model_id.split("|", 1)
            set_user_model(user_id, format_model_ref(p.strip(), mid.strip()))
        else:
            cur, _ = parse_model_ref(get_user_model(user_id))
            set_user_model(user_id, format_model_ref(cur, model_id))
        bot.reply_to(message, f"✅ مدل تنظیم شد: `{model_id}`", reply_markup=get_main_keyboard())
        return True

    PENDING.pop(user_id, None)
    return False


def _looks_like_manus_followup(text: str) -> bool:
    t = (text or "").lower()
    if not t or len(t) > 800:
        return False
    keys = (
        "ادیت", "ویرایش", "تغییر", "پس‌زمینه", "رنگ", "کیفیت", "حذف", "اضافه",
        "انجام بده", "بساز", "عکس", "تصویر",
        "edit", "change", "remove", "background", "color", "quality",
        "make the", "make it", "enhance", "retouch", "improve", "add ", "replace",
        "crop", "resize", "blur", "sharpen", "translate the image",
    )
    return any(k in t for k in keys)


@bot.message_handler(func=lambda msg: msg.content_type == "text", content_types=["text"])
def handle_chat(message):
    user_id = message.from_user.id
    text = message.text or ""
    if text.startswith("/"):
        return
    if _handle_pending_input(message):
        return

    ensure_user(message.from_user)
    mode = get_bot_mode(user_id)

    # Exclusive Manus mode: text goes to Manus (or Dahl fallback if empty)
    if mode == BOT_MODE_MANUS:
        pick = get_picked_prompt(user_id)
        prompt = text.strip()
        if not prompt and pick:
            prompt = pick.get("prompt") or ""
        if not prompt:
            _dahl_fallback_reply(
                message,
                user_id,
                note="🎯 حالت Manus بود ولی متن خالی بود — تلاش با **دال**:",
            )
            return
        run_manus_for_user(
            message,
            prompt,
            user_id=user_id,
            chat_id=message.chat.id,
            followup=False,
        )
        return

    # Chat mode: Manus-like text is ignored; Dahl/OpenAI path only
    sess = manus_get_session(user_id)
    if sess and _looks_like_manus_followup(text):
        bot.reply_to(
            message,
            "🎯 حالت فعلی **چت با دال** است.\n"
            "برای ادیت Manus از منوی اصلی **🎨 Manus** را بزنید.\n"
            "`/mode manus`",
            reply_markup=get_main_keyboard(),
        )
        return

    settings = get_user_settings(user_id)
    model_ref = settings["selected_model"]
    response_style = settings.get("response_style", "html")
    history_limit = settings.get("context_messages") or HISTORY_LIMIT
    # per-user stream flag if column exists
    stream_flag = STREAMING_ENABLED
    if settings.get("stream_enabled") is not None:
        stream_flag = bool(settings.get("stream_enabled"))

    cred = resolve_inference_credentials(user_id)
    if not cred:
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton("🔑 افزودن کلید من", callback_data="keys_list"))
        markup.row(InlineKeyboardButton("⚙️ تنظیمات مدل", callback_data="menu_settings"))
        bot.reply_to(
            message,
            "🔐 برای این مدل کلید API شخصی لازم است.\nکلید ثبت کنید یا مدل را عوض کنید.",
            reply_markup=markup,
        )
        return

    provider = cred["provider"]
    model_id = cred["model_id"]
    source = cred["source"]

    allowed, qstat = quota_allowed(user_id, source)
    if not allowed:
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton("📊 آمار مصرف", callback_data="menu_usage"))
        if qstat.get("lifetime_exceeded"):
            head = "⛔️ **سهمیه کلی شما تمام شد.**"
            tail = "برای ادامه باید سهمیه افزایش یابد (ادمین) یا طرح جدید فعال شود."
        else:
            head = "⛔️ **سهمیه روزانه (کلید مشترک) تمام شد.**"
            tail = "فردا ریست می‌شود. یا کلید شخصی ثبت کنید (`/keys`) تا از سهمیه خودتان استفاده شود."
        bot.reply_to(
            message,
            f"{head}\n\n{format_quota_block(qstat)}\n{tail}",
            reply_markup=markup,
        )
        return

    try:
        conversation = get_active_conversation(user_id, model_ref)
        conversation_id = conversation["id"]
    except Exception as e:
        logging.error(f"conversation error: {e}")
        bot.reply_to(message, "خطا در آماده‌سازی گفتگو.")
        return

    history = get_conversation_history(conversation_id, limit=history_limit)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": text})

    src_label = "کلید شما" if source == "user" else "کلید مشترک"
    mode_label = "استریم" if stream_flag else "یکجا"
    chat_title = (conversation.get("title") or "گفتگو")[:24]
    status_msg = bot.reply_to(
        message,
        f"⏳ تولید پاسخ…\n"
        f"💬 {chat_title}\n"
        f"🤖 `{model_id}` · {provider} · {src_label}\n"
        f"⚡ {mode_label} · 📝 `{response_style}`",
    )

    def on_partial(full_text: str):
        safe_edit(status_msg.chat.id, status_msg.message_id, full_text, style=response_style)

    try:
        result = call_llm(
            cred["base_url"],
            cred["api_key"],
            model_id,
            messages,
            stream=stream_flag,
            on_partial=on_partial if stream_flag else None,
        )
    except httpx.HTTPStatusError as e:
        status_code = getattr(e.response, "status_code", None)
        save_message(conversation_id, "user", text)
        if status_code == 402:
            safe_edit(
                status_msg.chat.id,
                status_msg.message_id,
                "توکن این کلید تمام شده (402).\nشارژ/Allocate یا کلید دیگر در `/keys`.",
                style=response_style,
            )
            return
        if status_code == 401:
            safe_edit(
                status_msg.chat.id,
                status_msg.message_id,
                "کلید API نامعتبر (401). از `/keys` اصلاح کنید.",
                style=response_style,
            )
            return
        logging.error(f"LLM HTTP error: {e}")
        # provider may reject stream_options — retry non-stream
        if stream_flag:
            try:
                result = call_llm(
                    cred["base_url"], cred["api_key"], model_id, messages, stream=False
                )
            except Exception as e2:
                logging.error(f"LLM retry failed: {e2}")
                safe_edit(
                    status_msg.chat.id,
                    status_msg.message_id,
                    f"خطا در ارتباط با مدل (`{provider}`).",
                    style="plain",
                )
                save_message(conversation_id, "user", text)
                return
        else:
            safe_edit(
                status_msg.chat.id,
                status_msg.message_id,
                f"خطا در ارتباط با مدل (`{provider}`).",
                style="plain",
            )
            save_message(conversation_id, "user", text)
            return
    except Exception as e:
        logging.error(f"LLM error: {e}")
        save_message(conversation_id, "user", text)
        safe_edit(
            status_msg.chat.id,
            status_msg.message_id,
            f"خطا در ارتباط با مدل (`{provider}` / `{model_id}`).",
            style="plain",
        )
        return

    reply_content = result["content"]
    pt = result["prompt_tokens"]
    ct = result["completion_tokens"]
    tt = result["total_tokens"]

    log_usage(user_id, model_id, pt, ct, tt, provider=provider)
    qstat = quota_add_usage(user_id, tt)
    save_message(conversation_id, "user", text, input_tokens=pt, output_tokens=0)
    save_message(conversation_id, "assistant", reply_content, input_tokens=0, output_tokens=ct)
    # Keep only recent messages of the active chat on the server
    trim_conversation_messages(conversation_id)

    title = None
    if not history:
        title = text.strip().replace("\n", " ")[:80] or "گفتگوی جدید"
    touch_conversation(conversation_id, model_ref, title=title)

    final = reply_content
    if result.get("streamed"):
        final += f"\n\n—\n`{tt:,}` توکن · استریم"
    else:
        final += f"\n\n—\n`{tt:,}` توکن"
    warn = quota_warning_text(qstat) if isinstance(qstat, dict) else None
    if warn:
        final += f"\n\n{warn}"
    send_bot_reply(status_msg.chat.id, status_msg.message_id, final, style=response_style)


if __name__ == "__main__":
    manus_force_unlock(None)
    bot.remove_webhook()
    bot.infinity_polling(skip_pending=True)
