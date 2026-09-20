import os
import re
import json
import html
import uuid
import time
import logging
from datetime import datetime, timezone, date
from collections import defaultdict
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

STREAMING_ENABLED = os.getenv("STREAMING_ENABLED", "true").lower() in ("1", "true", "yes")
STREAM_EDIT_INTERVAL = float(os.getenv("STREAM_EDIT_INTERVAL", "1.2"))
DEFAULT_DAILY_TOKEN_QUOTA = int(os.getenv("DEFAULT_DAILY_TOKEN_QUOTA", "50000"))
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

SYSTEM_PROMPT = (
    "You are a helpful AI assistant on Telegram. "
    "Answer clearly. Use short paragraphs, lists, and code blocks when useful."
)

logging.basicConfig(level=logging.INFO)
bot = telebot.TeleBot(BOT_TOKEN)

PENDING: Dict[int, Dict[str, str]] = {}

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
    return user_id in ADMIN_TELEGRAM_IDS


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


# --- Quota ---

def ensure_quota_row(user_id: int) -> dict:
    row = {
        "user_id": user_id,
        "daily_limit": DEFAULT_DAILY_TOKEN_QUOTA,
        "used_today": 0,
        "reset_at": today_date_str(),
    }
    try:
        res = sb_get(
            f"user_quotas?user_id=eq.{user_id}"
            f"&select=user_id,daily_limit,used_today,reset_at"
        )
        if res.status_code >= 400:
            # table missing — unlimited until SQL 03 runs
            return {"ok": False, **row, "table_missing": True}
        data = res.json() or []
        if data:
            row = data[0]
            row["table_missing"] = False
            row["ok"] = True
        else:
            sb_post("user_quotas", {
                "user_id": user_id,
                "daily_limit": DEFAULT_DAILY_TOKEN_QUOTA,
                "used_today": 0,
                "reset_at": today_date_str(),
                "updated_at": utcnow_iso(),
            })
            row["table_missing"] = False
            row["ok"] = True
    except Exception as e:
        logging.error(f"ensure_quota_row error: {e}")
        row["ok"] = False
        row["table_missing"] = True
    return row


def quota_status(user_id: int) -> dict:
    row = ensure_quota_row(user_id)
    limit = int(row.get("daily_limit") or DEFAULT_DAILY_TOKEN_QUOTA)
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
    remaining = max(limit - used, 0)
    return {
        "ok": bool(row.get("ok")),
        "table_missing": bool(row.get("table_missing")),
        "limit": limit,
        "used": used,
        "remaining": remaining,
        "reset_at": today if reset_at < today else reset_at,
        "exceeded": used >= limit and limit > 0,
    }


def quota_add_usage(user_id: int, tokens: int) -> None:
    if tokens <= 0:
        return
    status = quota_status(user_id)
    if not status.get("ok"):
        return
    try:
        sb_patch(
            f"user_quotas?user_id=eq.{user_id}",
            {
                "used_today": status["used"] + tokens,
                "reset_at": today_date_str(),
                "updated_at": utcnow_iso(),
            },
        )
    except Exception as e:
        logging.error(f"quota_add_usage error: {e}")


def quota_allowed(user_id: int, source: str) -> Tuple[bool, dict]:
    """source: 'user' | 'shared'"""
    if source == "user" and not QUOTA_APPLIES_TO_OWN_KEYS:
        return True, quota_status(user_id)
    status = quota_status(user_id)
    if status.get("table_missing"):
        return True, status
    return (not status["exceeded"]), status


def format_quota_block(status: dict, applies_note: str = "") -> str:
    if status.get("table_missing"):
        return "سهمیه روزانه: (جدول `user_quotas` هنوز ساخته نشده — sql/03)\n"
    return (
        f"سهمیه روزانه: `{status['used']:,}` / `{status['limit']:,}`\n"
        f"باقیمانده امروز: `{status['remaining']:,}`\n"
        f"ریست: `{status['reset_at']}`\n"
        f"{applies_note}"
    )


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
# Keyboards
# ---------------------------------------------------------------------------

def get_main_keyboard() -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("⚙️ تنظیمات", callback_data="menu_settings"))
    markup.row(
        InlineKeyboardButton("🤖 مدل فعال", callback_data="menu_models"),
        InlineKeyboardButton("💬 گفتگوها", callback_data="menu_chats"),
    )
    markup.row(
        InlineKeyboardButton("🔑 کلیدهای من", callback_data="keys_list"),
        InlineKeyboardButton("📊 آمار مصرف", callback_data="menu_usage"),
    )
    markup.row(InlineKeyboardButton("ℹ️ راهنما", callback_data="menu_help"))
    return markup


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
        if provider != "custom":
            markup.row(
                InlineKeyboardButton("🤖 استفاده از این سرویس", callback_data=f"set_model_provider:{provider}")
            )
    markup.row(InlineKeyboardButton("🔙 کلیدهای من", callback_data="keys_list"))
    markup.row(InlineKeyboardButton("⚙️ تنظیمات", callback_data="menu_settings"))
    return markup


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

def settings_overview_text(settings: dict, keys: Dict[str, dict], streaming_flag: bool) -> str:
    model_ref = settings.get("selected_model", "")
    provider, model_id = parse_model_ref(model_ref)
    style = settings.get("response_style", "html")
    ctx = settings.get("context_messages") or HISTORY_LIMIT
    style_meta = RESPONSE_STYLES.get(style, RESPONSE_STYLES["html"])
    has_key = provider in keys or (
        ALLOW_SHARED_KEY and provider == LEGACY_PROVIDER and DAHL_API_KEY
    )
    active = settings.get("active_conversation_id")
    lines = [
        "⚙️ **تنظیمات ربات**",
        "",
        f"🤖 **مدل ارسال/پاسخ:** `{provider}` / `{model_id}`",
        f"   کلید: `{'دارد' if has_key else 'ندارد'}`",
        f"📝 **فرمت پاسخ:** {style_meta['name_fa']}",
        f"📜 **تاریخچه:** {ctx} پیام",
        f"⚡ **استریم:** `{'روشن' if streaming_flag else 'خاموش'}`",
        f"💬 **گفتگوی فعال:** `{(str(active)[:8] + '…') if active else '—'}`",
        "",
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
    lines.append("روی هر گفتگو بزنید تا فعال شود؛ بعد پیام بدهید.")
    return "\n".join(lines)


def usage_text_for_user(user_id: int) -> str:
    settings = get_user_settings(user_id)
    model_ref = settings["selected_model"]
    provider, model_id = parse_model_ref(model_ref)
    keys = list_user_keys(user_id)
    stats = get_usage_stats(user_id)
    q = quota_status(user_id)
    source = "shared"
    if provider in keys:
        source = "user"
    if stats["error"]:
        return "خطا در دریافت آمار مصرف."
    model_lines = ""
    if stats["by_model"]:
        model_lines = "\n**تفکیک مصرف:**\n"
        for mid, tot in sorted(stats["by_model"].items(), key=lambda x: -x[1])[:8]:
            model_lines += f"• `{mid}`: `{tot:,}`\n"
    applies = (
        "روی کلید شخصی هم اعمال می‌شود"
        if QUOTA_APPLIES_TO_OWN_KEYS
        else "فقط برای کلید مشترک ربات"
    )
    quota_note = format_quota_block(q, applies + "\n")
    return (
        "📊 **گزارش مصرف توکن**\n\n"
        f"کل مصرف ثبت‌شده: `{stats['total_tokens']:,}`\n"
        f"• ورودی: `{stats['input_tokens']:,}`\n"
        f"• خروجی: `{stats['output_tokens']:,}`\n"
        f"• درخواست‌ها: `{stats['calls']:,}`\n\n"
        f"**سهمیه روزانه**\n{quota_note}\n"
        f"🤖 مدل: `{provider}` / `{model_id}`\n"
        f"🔐 منبع کلید فعلی: `{source}`\n"
        f"⚡ استریم: `{'روشن' if STREAMING_ENABLED else '—'}`\n"
        f"{model_lines}"
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

    return (
        "🛡 **آمار ادمین**\n\n"
        f"کاربران کل: `{total_users}`\n"
        f"فعال امروز: `{active_today}`\n"
        f"گفتگوها: `{total_chats}`\n"
        f"کل توکن مصرفی: `{total_tokens:,}`\n"
        f"توکن امروز: `{today_tokens:,}`\n\n"
        f"**مدل‌های پرتکرار (توکن):**\n{model_lines}\n\n"
        f"**کلیدهای BYOK ثبت‌شده:** {key_line}\n"
        f"سهمیه پیش‌فرض روزانه: `{DEFAULT_DAILY_TOKEN_QUOTA:,}`\n"
        f"اعمال سهمیه روی کلید شخصی: `{'بله' if QUOTA_APPLIES_TO_OWN_KEYS else 'خیر'}`\n"
        f"استریم: `{'روشن' if STREAMING_ENABLED else 'خاموش'}`\n"
        f"کلید مشترک Dahl: `{'ست‌شده' if DAHL_API_KEY else '—'}`"
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
        f"📊 **سهمیه امروز:** `{q['used']:,}` / `{q['limit']:,}`\n\n"
        "مدل، فرمت و گفتگوها از **⚙️ تنظیمات**.\n"
        "دستورات: `/settings` `/keys` `/chats` `/clear` `/help`"
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
        "• `/clear` — گفتگوی تازه\n"
        "• `/chats` — لیست گفتگوها\n"
        "• `/settings` — تنظیمات"
        f"{admin_line}\n\n"
        "پاسخ خام؟ → تنظیمات → فرمت → **HTML**"
    )
    bot.reply_to(message, text, reply_markup=get_main_keyboard())


@bot.message_handler(commands=["settings", "config"])
def handle_settings_cmd(message):
    ensure_user(message.from_user)
    user_id = message.from_user.id
    settings = get_user_settings(user_id)
    keys = list_user_keys(user_id)
    bot.reply_to(
        message,
        settings_overview_text(settings, keys, STREAMING_ENABLED),
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
        provider, model_id = parse_model_ref(model_ref)
        bot.reply_to(
            message,
            f"🧹 گفتگوی جدید ساخته و فعال شد.\n"
            f"شناسه: `{str(conv.get('id',''))[:8]}…`\n"
            f"مدل: `{provider}` / `{model_id}`\n\n"
            "رکوردهای قبلی در دیتابیس می‌مانند.",
        )
    except Exception as e:
        logging.error(f"clear error: {e}")
        bot.reply_to(message, "خطا در ساخت گفتگوی جدید.")


@bot.message_handler(commands=["admin"])
def handle_admin(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        bot.reply_to(message, "⛔️ دسترسی ادمین ندارید.\n`ADMIN_TELEGRAM_IDS` را در env تنظیم کنید.")
        return
    bot.reply_to(message, admin_stats_text(), reply_markup=get_admin_keyboard(), parse_mode="Markdown")


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
        bot.edit_message_text(
            admin_stats_text(),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=get_admin_keyboard(),
            parse_mode="Markdown",
        )
        answer()
        return

    if data == "menu_settings":
        settings = get_user_settings(user_id)
        keys = list_user_keys(user_id)
        bot.edit_message_text(
            settings_overview_text(settings, keys, STREAMING_ENABLED),
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
            settings_overview_text(settings, list_user_keys(user_id), effective),
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
            settings_overview_text(settings, list_user_keys(user_id), STREAMING_ENABLED),
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
            settings_overview_text(settings, list_user_keys(user_id), STREAMING_ENABLED),
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
        chats = list_conversations(user_id)
        answer("گفتگوی جدید فعال شد", True)
        bot.edit_message_text(
            chats_overview_text(chats, conv["id"]),
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
            settings_overview_text(settings, list_user_keys(user_id), STREAMING_ENABLED),
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
            "چت · تنظیمات · BYOK · استریم · سهمیه روزانه · گفتگوهای چندگانه\n"
            "`/settings` `/keys` `/chats` `/clear` `/help`"
        )
        markup = InlineKeyboardMarkup()
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
    PENDING.pop(message.from_user.id, None)
    bot.reply_to(message, "عملیات لغو شد.")


def _handle_pending_input(message) -> bool:
    """Return True if pending flow consumed the message."""
    user_id = message.from_user.id
    pending = PENDING.get(user_id)
    if not pending:
        return False
    action = pending.get("action")
    raw = (message.text or "").strip()

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


@bot.message_handler(func=lambda msg: msg.content_type == "text", content_types=["text"])
def handle_chat(message):
    user_id = message.from_user.id
    text = message.text or ""
    if text.startswith("/"):
        return
    if _handle_pending_input(message):
        return

    ensure_user(message.from_user)
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
        bot.reply_to(
            message,
            f"⛔️ **سهمیه روزانه تمام شد.**\n\n"
            f"{format_quota_block(qstat)}\n"
            f"فردا ریست می‌شود. یا کلید شخصی ثبت کنید (`/keys`).",
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
    quota_add_usage(user_id, tt)
    save_message(conversation_id, "user", text, input_tokens=pt, output_tokens=0)
    save_message(conversation_id, "assistant", reply_content, input_tokens=0, output_tokens=ct)

    title = None
    if not history:
        title = text.strip().replace("\n", " ")[:80] or "گفتگوی جدید"
    touch_conversation(conversation_id, model_ref, title=title)

    final = reply_content
    if result.get("streamed"):
        final += f"\n\n—\n`{tt:,}` توکن · استریم"
    else:
        final += f"\n\n—\n`{tt:,}` توکن"
    send_bot_reply(status_msg.chat.id, status_msg.message_id, final, style=response_style)


if __name__ == "__main__":
    bot.remove_webhook()
    bot.infinity_polling(skip_pending=True)
