"""Thin async wrappers around the Telegram Bot API (via httpx)."""
import httpx

from config import BOT_TOKEN

_API = f"https://api.telegram.org/bot{BOT_TOKEN}"


class TelegramError(RuntimeError):
    """Bot API error with parsed fields (error_code/retry_after are needed for broadcasts)."""

    def __init__(self, method, data):
        self.method = method
        self.description = data.get("description") or ""
        self.error_code = data.get("error_code")
        # on 429, Telegram sends how many seconds to wait before retrying
        self.retry_after = (data.get("parameters") or {}).get("retry_after")
        super().__init__(f"Telegram API {method}: {self.description}")


async def _call(method, **params):
    """Calls a Bot API method. Returns the result field or raises TelegramError."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{_API}/{method}", json=params)
        data = resp.json()
    if not data.get("ok"):
        raise TelegramError(method, data)
    return data.get("result")


async def send_message(chat_id, text, reply_markup=None, parse_mode="HTML"):
    params = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        params["reply_markup"] = reply_markup
    return await _call("sendMessage", **params)


async def copy_message(chat_id, from_chat_id, message_id, reply_markup=None):
    """Copies a message to another chat "as new" — keeps the formatting the admin
    typed in Telegram (bold, links, emoji), with no link back to the original."""
    params = {"chat_id": chat_id, "from_chat_id": from_chat_id, "message_id": message_id}
    if reply_markup is not None:
        params["reply_markup"] = reply_markup
    return await _call("copyMessage", **params)


async def answer_pre_checkout_query(query_id, ok=True, error_message=None):
    params = {"pre_checkout_query_id": query_id, "ok": ok}
    if error_message:
        params["error_message"] = error_message
    return await _call("answerPreCheckoutQuery", **params)


async def answer_callback_query(query_id, text=None):
    params = {"callback_query_id": query_id}
    if text:
        params["text"] = text
    return await _call("answerCallbackQuery", **params)


async def edit_message_text(chat_id, message_id, text, reply_markup=None, parse_mode="HTML"):
    params = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        params["reply_markup"] = reply_markup
    return await _call("editMessageText", **params)


async def create_invoice_link(title, description, payload, amount_stars, label):
    """Creates a payment link for Telegram Stars (XTR currency).
    amount_stars is the number of stars (for XTR it is given directly, no ×100)."""
    return await _call(
        "createInvoiceLink",
        title=title,
        description=description,
        payload=payload,
        currency="XTR",
        prices=[{"label": label, "amount": int(amount_stars)}],
    )


async def set_webhook(url, secret_token):
    return await _call(
        "setWebhook",
        url=url,
        secret_token=secret_token,
        allowed_updates=["message", "pre_checkout_query", "callback_query"],
        drop_pending_updates=True,
    )


async def delete_webhook():
    return await _call("deleteWebhook", drop_pending_updates=False)


async def get_me():
    return await _call("getMe")


async def get_my_star_balance():
    """Current bot balance in stars (the getMyStarBalance method returns {amount, ...})."""
    res = await _call("getMyStarBalance")
    if isinstance(res, dict):
        return res.get("amount", 0)
    return res


def inline_button_url(text, url):
    """Inline keyboard with a single URL button."""
    return {"inline_keyboard": [[{"text": text, "url": url}]]}


def inline_keyboard(rows):
    """Keyboard built from rows of [(text, callback_data), ...] — for admin panel tabs."""
    return {"inline_keyboard": [
        [{"text": text, "callback_data": data} for text, data in row] for row in rows
    ]}


def inline_button_callback(text, callback_data):
    """Inline keyboard with a single callback button (pressed inside the chat)."""
    return {"inline_keyboard": [[{"text": text, "callback_data": callback_data}]]}
