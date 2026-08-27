"""Telegram bot logic: commands, Telegram Stars payments, and auto-analysis
of recent Chess.com games with a per-player summary.

Updates arrive via webhook (see server.py) and land in handle_update().
The poll_all() scheduler is run on a timer from server.py."""
import asyncio
import hashlib
import html
import time

_started_at = time.time()  # process start time — for uptime in /admin

import httpx

import analysis
import db
import tg
from config import (
    ADMIN_ID, ADMIN_USERNAME, ANALYSIS_MAX_GAMES_PER_POLL, CHESSCOM_USER_AGENT,
    ENGINE_DEPTH, ENGINE_HASH, ENGINE_THREADS, MAX_PLIES, MINIAPP_DEEPLINK,
    POLL_INTERVAL_SECONDS, WEBHOOK_BASE_URL,
)

# ---------- bot texts (RU/EN) ----------
BOT_TEXT = {
    "en": {
        "welcome": (
            "♟ <b>Chess Analysis</b>\n\n"
            "I review your games with the Stockfish engine.\n\n"
            "• Open the Mini App to analyze any game or PGN.\n"
            "• <code>/subscribe chesscom_username</code> — and I'll automatically send you "
            "analysis of your new Chess.com games.\n\n"
            "Commands: /help"
        ),
        "help": (
            "<b>Commands</b>\n"
            "/subscribe &lt;username&gt; — subscribe to auto-analysis of Chess.com games\n"
            "/unsubscribe — stop\n"
            "/status — current subscription\n"
            "/support &lt;message&gt; — contact the author\n"
            "/donate — support the author ⭐"
        ),
        "need_username": "Provide your Chess.com username: <code>/subscribe magnuscarlsen</code>",
        "cc_not_found": "Player <b>{u}</b> not found on Chess.com. Check the username.",
        "subscribed": (
            "✅ Subscribed as <b>{u}</b>.\n"
            "I'll send analysis of your new finished games (checked every few minutes)."
        ),
        "unsubscribed": "🔕 Auto-analysis disabled. Re-enable: /subscribe username.",
        "status": "Username: <b>{u}</b>\nAuto-analysis: <b>{s}</b>",
        "on": "on", "off": "off",
        "donate_info": (
            "Thanks for wanting to support! ⭐\n"
            "Open the Mini App and tap “❤️ Support the author” — you can send any amount of Stars."
        ),
        "thanks": "💖 Thank you for the {stars} ⭐ support! It really helps the project grow.",
        "unknown": "Unknown command. See /help",
        # support / tickets
        "support_prompt": "✍️ Write your message and I'll forward it to the author.",
        "support_sent": "✅ Sent! The author will reply right here in this chat.",
        "support_unavailable": "Support is temporarily unavailable. Try again later.",
        "support_from_admin": "💬 <b>Support</b>\n\n{text}",
        "support_admin_new": (
            "📩 <b>New support ticket</b>\n"
            "From: {who} (id <code>{uid}</code>)\n\n"
            "{text}\n\n"
            "↩️ <i>Reply to this message to answer the user.</i>"
        ),
        "support_admin_delivered": "✅ Reply delivered to the user.",
        # announcement (broadcast)
        "announcement_prompt": (
            "📢 <b>Announcement</b>\n\n"
            "Reply to this message with what you want to send to <b>all users</b> "
            "— text, photo or video.\n\n"
            "<i>Formatting, links and emoji are kept. The reply is broadcast as-is, "
            "there is no confirmation step.</i>"
        ),
        "announcement_empty": "There are no users to send to yet.",
        "announcement_busy": "⏳ Another broadcast is still running — wait for it to finish.",
        "announcement_failed": "⚠️ Broadcast failed, see server logs.",
        "announcement_progress": "📤 Broadcasting… <b>{done}</b>/<b>{total}</b>",
        "announcement_done": (
            "✅ <b>Announcement sent</b>\n"
            "• Delivered: <b>{sent}</b>\n"
            "• Failed: <b>{failed}</b> <i>(blocked the bot / chat deleted)</i>"
        ),
        # report
        "you_played": "You played {color} — {outcome}",
        "white": "White", "black": "Black",
        "win": "win 🟢", "loss": "loss 🔴", "draw": "draw ⚪",
        "accuracy": "🎯 Accuracy: <b>{acc}%</b>",
        "worst": "Worst move: <b>{no}. {san}</b> (better was {best})",
        "clean": "No serious mistakes — clean game! 👏",
        "open_btn": "🔍 Open analysis",
    },
}

_DRAW_RESULTS = {
    "agreed", "repetition", "stalemate", "insufficient",
    "50move", "timevsinsufficient",
}


def _t(lang, key):
    # The bot is English-only; the lang parameter is kept in the signature for call compatibility.
    return BOT_TEXT["en"].get(key, key)


def _esc(s):
    return html.escape(str(s))


def _hash(s):
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:16]


def _is_admin(frm):
    """Bot admin: matched by numeric ADMIN_ID (if set) or by ADMIN_USERNAME nickname."""
    if ADMIN_ID and frm.get("id") == ADMIN_ID:
        return True
    uname = (frm.get("username") or "").lower()
    return bool(ADMIN_USERNAME) and uname == ADMIN_USERNAME


# background tasks (broadcasts): keep references, or the garbage collector kills them midway
_background_tasks = set()


def _spawn(coro):
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


# tg_ids of users who entered /support and are now typing their ticket text
_awaiting_support = set()
# admin chat_id: taken from ADMIN_ID, otherwise remembered on the admin's first message
# (needed to proactively send tickets when only ADMIN_USERNAME is set)
_admin_chat_id = None


def _admin_chat():
    return ADMIN_ID or _admin_chat_id


# ====================== ADMIN PANEL ======================
_ADMIN_PAGES = ("overview", "users", "games", "stars", "server")
_ADMIN_TABS = (
    (("📊 Overview", "overview"), ("👥 Users", "users")),
    (("♟ Games", "games"), ("⭐ Stars", "stars")),
    (("⚙ Server", "server"),),
)
_SPARK_CHARS = "▁▂▃▄▅▆▇█"

# summary of the last scheduler run — filled in by poll_all()
_poll_stats = {"runs": 0, "last_at": 0, "last_secs": 0.0,
               "last_users": 0, "last_games": 0, "errors": 0}


def _fmt_langs(langs):
    return ", ".join(f"{_esc(l or '—')}: {n}" for l, n in langs) or "—"


def _uptime_str():
    secs = int(time.time() - _started_at)
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    parts.append(f"{s}s")  # always include seconds — so the text changes on "Refresh"
    return " ".join(parts)


def _pct(part, total):
    return (part * 100.0 / total) if total else 0.0


def _bar(part, total, width=10):
    """Funnel bar: ██████░░░░ 62% (74)."""
    filled = max(0, min(width, int(round(_pct(part, total) * width / 100.0))))
    return f'{"█" * filled}{"░" * (width - filled)} {_pct(part, total):.0f}% ({part})'


def _spark(values):
    """Mini per-day chart made of block characters (last bar is today)."""
    if not values:
        return "—"
    top = max(values)
    if top <= 0:
        return _SPARK_CHARS[0] * len(values)
    return "".join(
        _SPARK_CHARS[0] if v <= 0 else _SPARK_CHARS[min(7, 1 + int(v * 6.99 / top))]
        for v in values
    )


def _ago(ts):
    if not ts:
        return "—"
    d = max(0, int(time.time()) - int(ts))
    if d < 60:
        return f"{d}s ago"
    if d < 3600:
        return f"{d // 60}m ago"
    if d < 86400:
        return f"{d // 3600}h ago"
    return f"{d // 86400}d ago"


def _date(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(int(ts))) if ts else "—"


def _size(nbytes):
    size = float(nbytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0


def _num(value, suffix=""):
    """Metrics from meta_json may be NULL (old games) — show a dash."""
    return f"{value}{suffix}" if value is not None else "—"


def _user_label(row):
    if row.get("username"):
        return "@" + _esc(row["username"])
    name = row.get("first_name") or row.get("chesscom")
    return _esc(name) if name else f'<code>{row.get("tg_id")}</code>'


async def _star_balance():
    try:
        return await tg.get_my_star_balance()
    except Exception as e:
        print(f"[admin] balance error: {e}")
        return None


async def _page_overview():
    st = db.get_stats()
    bal = await _star_balance()
    return [
        "🛠 <b>Admin panel</b>",
        "",
        f'👥 Users: <b>{st["users"]}</b>  (+{st["users_day"]} today, +{st["users_week"]} week)',
        f'   Active: <b>{st["dau"]}</b> today · <b>{st["wau"]}</b> week',
        f'   Linked: <b>{st["linked"]}</b> · auto-analysis: <b>{st["subscribers"]}</b>',
        "",
        f'♟ Games analyzed: <b>{st["games"]}</b>  (+{st["games_day"]} today)',
        f'⭐ Stars: <b>{st["stars"]}</b> from {st["donations"]} donations '
        f'(+{st["stars_day"]} today)',
        f'   Bot balance: <b>{bal if bal is not None else "—"}</b> ⭐',
        f'📩 Tickets today: <b>{st["tickets_day"]}</b>',
        "",
        f'⚙ Uptime <b>{_uptime_str()}</b> · engines <b>{analysis.engines_ready()}</b>',
    ]


def _page_users():
    st = db.get_user_stats()
    lines = [
        "👥 <b>Users</b>",
        "",
        f'Total: <b>{st["total"]}</b>',
        f'New: <b>+{st["new_day"]}</b> today · +{st["new_week"]} week · +{st["new_month"]} month',
        f'Active: DAU <b>{st["dau"]}</b> · WAU <b>{st["wau"]}</b> · MAU <b>{st["mau"]}</b>',
        f'Dormant (30d+): <b>{st["silent"]}</b>',
        "",
        "<b>Funnel</b>",
        f'Linked Chess.com <code>{_bar(st["linked"], st["total"])}</code>',
        f'Auto-analysis on <code>{_bar(st["subscribed"], st["total"])}</code>',
        f'Linked but turned off: <b>{st["churned"]}</b>',
        "",
        f'<b>New users, 14 days</b> (peak {max(st["daily"]) if st["daily"] else 0}/day)',
        f'<code>{_spark(st["daily"])}</code>',
        "",
        f'Languages: {_fmt_langs(st["langs"])}',
    ]
    if st["recent"]:
        lines += ["", "<b>Newest</b>"]
        for r in st["recent"]:
            cc = f' · {_esc(r["chesscom"])}' if r.get("chesscom") else ""
            lines.append(f'• {_user_label(r)}{cc} — {_ago(r.get("created_at"))}')
    return lines


def _page_games():
    st = db.get_game_stats()
    decided = st["wins"] + st["losses"] + st["draws"]
    lines = [
        "♟ <b>Games</b>",
        "",
        f'Analyzed: <b>{st["total"]}</b> for <b>{st["players"]}</b> players',
        f'Period: <b>+{st["day"]}</b> today · +{st["week"]} week · +{st["month"]} month',
        "",
        f'<b>Quality</b> <i>(by {st["rated"]} games with stats)</i>',
        f'Avg accuracy: <b>{_num(st["avg_accuracy"], "%")}</b>',
        f'Avg blunders per game: <b>{_num(st["avg_blunders"])}</b>',
    ]
    if decided:
        lines.append(
            f'Results: 🟢 {st["wins"]} · ⚪ {st["draws"]} · 🔴 {st["losses"]}  '
            f'(win rate {_pct(st["wins"], decided):.0f}%)'
        )
    lines += [
        "",
        f'<b>Analyzed, 14 days</b> (peak {max(st["daily"]) if st["daily"] else 0}/day)',
        f'<code>{_spark(st["daily"])}</code>',
    ]
    if st["openings"]:
        lines += ["", "<b>Top openings</b>"]
        lines += [f'{i}. {_esc(op)} — <b>{n}</b>' for i, (op, n) in enumerate(st["openings"], 1)]
    if st["time_classes"]:
        lines += ["", "Time controls: " + " · ".join(
            f'{_esc(tc)} <b>{n}</b>' for tc, n in st["time_classes"]
        )]
    if st["top_players"]:
        lines += ["", "<b>Most active</b>"]
        lines += [f'• {_user_label(r)} — <b>{r["n"]}</b>' for r in st["top_players"]]
    return lines


async def _page_stars():
    st = db.get_money_stats()
    bal = await _star_balance()
    lines = [
        "⭐ <b>Stars</b>",
        "",
        f'Bot balance: <b>{bal if bal is not None else "—"}</b> ⭐',
        f'Collected: <b>{st["total"]}</b> ⭐ from <b>{st["count"]}</b> donations '
        f'· <b>{st["donors"]}</b> donors',
        f'Average: <b>{st["avg"]}</b> ⭐ · biggest: <b>{st["best"]}</b> ⭐',
        f'Period: <b>+{st["day"]}</b> today · +{st["week"]} week · +{st["month"]} month',
        f'Last donation: <b>{_ago(st["last_at"])}</b>',
        "",
        "<b>Donations, 14 days</b>",
        f'<code>{_spark(st["daily"])}</code>',
    ]
    if st["top"]:
        lines += ["", "<b>Top donors</b>"]
        for i, r in enumerate(st["top"], 1):
            lines.append(f'{i}. {_user_label(r)} — <b>{r["amount"]}</b> ⭐ ({r["n"]}×)')
    return lines


def _page_server():
    info = db.get_db_info()
    sup = db.get_support_stats()
    t = info["tables"]
    lines = [
        "⚙ <b>Server</b>",
        "",
        f'Uptime: <b>{_uptime_str()}</b>',
        f'Stockfish engines: <b>{analysis.engines_ready()}</b>',
        f'Engine: depth <b>{ENGINE_DEPTH}</b> · {ENGINE_THREADS} thread(s) · {ENGINE_HASH} MB hash',
        "",
        "<b>Chess.com polling</b>",
        f'Interval: <b>{POLL_INTERVAL_SECONDS}s</b> · runs: <b>{_poll_stats["runs"]}</b> '
        f'· errors: <b>{_poll_stats["errors"]}</b>',
    ]
    if _poll_stats["runs"]:
        lines.append(
            f'Last run: <b>{_ago(_poll_stats["last_at"])}</b> — {_poll_stats["last_users"]} '
            f'subscribers, {_poll_stats["last_games"]} new games, {_poll_stats["last_secs"]:.1f}s'
        )
    lines += [
        "",
        "<b>Database</b>",
        f'{_esc(info["path"])} — <b>{_size(info["size"])}</b>',
        f'users {t["users"]} · games {t["games"]} · donations {t["donations"]} '
        f'· tickets {t["support_tickets"]}',
        "",
        "<b>Support</b>",
        f'Tickets: <b>{sup["tickets"]}</b> from {sup["ticket_users"]} users '
        f'(+{sup["tickets_day"]} today, +{sup["tickets_week"]} week)',
    ]
    last = sup["last_announcement"]
    if last:
        lines.append(
            f'Announcements: <b>{sup["announcements"]}</b> · last {_ago(last["created_at"])} '
            f'— {last["sent"]} delivered, {last["failed"]} failed'
        )
    lines += ["", "<i>/user &lt;id|@nick&gt; — user card · /announcement — broadcast</i>"]
    return lines


def _admin_markup(page):
    rows = [
        [(f"• {title} •" if key == page else title, f"admin:{key}") for title, key in row]
        for row in _ADMIN_TABS
    ]
    rows.append([("🔄 Refresh", f"admin:{page}")])
    return tg.inline_keyboard(rows)


async def _build_admin_panel(page="overview"):
    """Text and keyboard for a single admin panel tab."""
    if page == "users":
        lines = _page_users()
    elif page == "games":
        lines = _page_games()
    elif page == "stars":
        lines = await _page_stars()
    elif page == "server":
        lines = _page_server()
    else:
        page = "overview"
        lines = await _page_overview()
    # the timestamp also guarantees the text changes so Telegram accepts the edit
    lines += ["", f'<i>⏱ {time.strftime("%H:%M:%S", time.gmtime())} UTC</i>']
    return "\n".join(lines), _admin_markup(page)


async def _send_admin_panel(chat_id, page="overview"):
    try:
        text, markup = await _build_admin_panel(page)
    except Exception as e:
        print(f"[admin] build error: {e}")
        await tg.send_message(chat_id, f"⚠️ Admin panel error: {_esc(e)}")
        return
    await tg.send_message(chat_id, text, reply_markup=markup)


async def _send_user_card(chat_id, query):
    """/user <id|@nick|chesscom> — card for a single user."""
    if not query:
        await tg.send_message(
            chat_id, "Usage: <code>/user 12345</code>, <code>/user @nick</code> "
                     "or <code>/user chesscom_name</code>"
        )
        return
    found = db.find_user(query)
    if not found:
        await tg.send_message(chat_id, f"User <b>{_esc(query)}</b> not found.")
        return
    card = db.get_user_card(found["tg_id"])
    if not card:
        await tg.send_message(chat_id, f"User <b>{_esc(query)}</b> not found.")
        return
    u = card["user"]
    uname = f' (@{_esc(u["username"])})' if u.get("username") else ""
    lines = [
        f'👤 <b>{_esc(u.get("first_name") or "user")}</b>{uname}',
        f'ID: <code>{u["tg_id"]}</code> · lang: {_esc(u.get("lang") or "—")}',
        f'Chess.com: <b>{_esc(u.get("chesscom") or "—")}</b> · auto-analysis: '
        f'<b>{"on" if u.get("subscribed") else "off"}</b>',
        f'Joined: {_date(u.get("created_at"))} ({_ago(u.get("created_at"))})',
        f'Last seen: <b>{_ago(u.get("last_seen"))}</b>',
        "",
        f'Games analyzed: <b>{card["games"]}</b> · avg accuracy '
        f'<b>{_num(card["avg_accuracy"], "%")}</b> · last {_ago(card["games_last_at"])}',
        f'Donations: <b>{card["donations"]}</b> for <b>{card["stars"]}</b> ⭐',
        f'Support tickets: <b>{card["tickets"]}</b>',
    ]
    await tg.send_message(chat_id, "\n".join(lines))


# ====================== INCOMING UPDATES ======================
async def handle_update(update):
    try:
        if "pre_checkout_query" in update:
            await tg.answer_pre_checkout_query(update["pre_checkout_query"]["id"], ok=True)
            return
        if "callback_query" in update:
            await _on_callback(update["callback_query"])
            return
        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return
        if "successful_payment" in msg:
            await _on_successful_payment(msg)
            return
        if "text" in msg:
            await _on_text(msg)
        elif msg.get("reply_to_message"):
            await _on_media_reply(msg)
    except Exception as e:
        print(f"[bot] handle_update error: {e}")


async def _on_media_reply(msg):
    """Admin replying with a photo/video/file to the /announcement prompt.
    copyMessage carries media with its caption as-is, so anything can be broadcast."""
    frm = msg.get("from", {})
    reply = msg.get("reply_to_message") or {}
    if not _is_admin(frm):
        return
    if db.pop_announcement_prompt(reply.get("message_id")):
        _spawn(_broadcast_announcement(msg["chat"]["id"], msg["message_id"]))


async def _on_callback(cq):
    """Inline button presses: switching admin panel tabs."""
    data = cq.get("data") or ""
    frm = cq.get("from", {})
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")

    if data.startswith("admin:") and _is_admin(frm) and chat_id and message_id:
        page = data.split(":", 1)[1]
        if page not in _ADMIN_PAGES:
            page = "overview"  # including the legacy admin:refresh button
        try:
            text, markup = await _build_admin_panel(page)
            await tg.edit_message_text(chat_id, message_id, text, reply_markup=markup)
            await tg.answer_callback_query(cq["id"], text="Updated")
            return
        except Exception as e:
            print(f"[admin] page {page} error: {e}")
            await tg.answer_callback_query(cq["id"], text=f"Error: {e}"[:190])
            return

    await tg.answer_callback_query(cq["id"])


async def _on_text(msg):
    global _admin_chat_id
    chat_id = msg["chat"]["id"]
    text = msg["text"].strip()
    frm = msg.get("from", {})
    db.ensure_user(chat_id, "en")
    db.touch_user(chat_id, frm.get("username"), frm.get("first_name"))
    lang = "en"

    # remember the admin's chat so we can send tickets there (when only ADMIN_USERNAME is set)
    if _is_admin(frm):
        _admin_chat_id = frm.get("id")

    # 1) Admin replies to a ticket → forward the answer to the ticket author
    reply = msg.get("reply_to_message")
    if reply and _is_admin(frm):
        # 1a) reply to the /announcement prompt → broadcast to everyone
        if db.pop_announcement_prompt(reply.get("message_id")):
            # run in background: a broadcast takes minutes, and the webhook must answer
            # Telegram right away, or it deems the update undelivered and resends it
            _spawn(_broadcast_announcement(chat_id, msg["message_id"]))
            return
        user_tg = db.get_ticket_user(reply.get("message_id"))
        if user_tg:
            await tg.send_message(user_tg, _t(lang, "support_from_admin").format(text=_esc(text)))
            await tg.send_message(chat_id, _t(lang, "support_admin_delivered"))
            return

    # 2) The user previously entered /support — this text is the ticket itself
    if chat_id in _awaiting_support and not text.startswith("/"):
        _awaiting_support.discard(chat_id)
        await _open_ticket(msg, text)
        return

    if not text.startswith("/"):
        await tg.send_message(chat_id, _t(lang, "unknown"))
        return

    parts = text.split()
    cmd = parts[0].split("@")[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if cmd == "/start":
        await tg.send_message(chat_id, _t(lang, "welcome"))
    elif cmd == "/help":
        await tg.send_message(chat_id, _t(lang, "help"))
    elif cmd in ("/subscribe", "/link"):
        if not arg:
            await tg.send_message(chat_id, _t(lang, "need_username"))
            return
        username = arg.strip().lstrip("@")
        if not await _chesscom_exists(username):
            await tg.send_message(chat_id, _t(lang, "cc_not_found").format(u=_esc(username)))
            return
        db.set_chesscom(chat_id, username)
        db.set_subscribed(chat_id, True)
        db.set_last_game_end(chat_id, int(time.time()))  # only analyze future games
        await tg.send_message(chat_id, _t(lang, "subscribed").format(u=_esc(username)))
    elif cmd == "/unsubscribe":
        db.set_subscribed(chat_id, False)
        await tg.send_message(chat_id, _t(lang, "unsubscribed"))
    elif cmd == "/status":
        u = db.get_user(chat_id) or {}
        await tg.send_message(chat_id, _t(lang, "status").format(
            u=_esc(u.get("chesscom") or "—"),
            s=_t(lang, "on") if u.get("subscribed") else _t(lang, "off"),
        ))
    elif cmd == "/support":
        body = text[len(parts[0]):].strip()  # everything after /support
        if body:
            await _open_ticket(msg, body)
        else:
            _awaiting_support.add(chat_id)
            await tg.send_message(chat_id, _t(lang, "support_prompt"))
    elif cmd == "/donate":
        await tg.send_message(chat_id, _t(lang, "donate_info"))
    elif cmd == "/admin":
        if _is_admin(frm):
            await _send_admin_panel(chat_id)
        else:
            await tg.send_message(chat_id, _t(lang, "unknown"))
    elif cmd == "/user":
        if _is_admin(frm):
            await _send_user_card(chat_id, text[len(parts[0]):].strip())
        else:
            await tg.send_message(chat_id, _t(lang, "unknown"))
    elif cmd == "/announcement":
        if _is_admin(frm):
            await _send_announcement(chat_id)
        else:
            await tg.send_message(chat_id, _t(lang, "unknown"))
    else:
        await tg.send_message(chat_id, _t(lang, "unknown"))


async def _open_ticket(msg, body):
    """Forwards a user's ticket to the admin and remembers the message↔author link,
    so the admin's reply comes back to exactly that user."""
    chat_id = msg["chat"]["id"]
    frm = msg.get("from", {})
    admin_chat = _admin_chat()
    if not admin_chat:
        await tg.send_message(chat_id, _t("en", "support_unavailable"))
        print("[support] Admin chat_id missing: set ADMIN_ID or message the bot as the admin.")
        return

    name = frm.get("first_name") or "user"
    uname = frm.get("username")
    who = f"{_esc(name)} (@{_esc(uname)})" if uname else _esc(name)
    sent = await tg.send_message(admin_chat, _t("en", "support_admin_new").format(
        who=who, uid=chat_id, text=_esc(body),
    ))
    if sent and sent.get("message_id"):
        db.save_ticket(sent["message_id"], chat_id)
    await tg.send_message(chat_id, _t("en", "support_sent"))


# ====================== BROADCAST (/announcement) ======================
# ~20 messages per second: Telegram caps mass broadcasts at about 30/s
_BROADCAST_DELAY = 0.05
_BROADCAST_PROGRESS_EVERY = 25
_broadcast_running = False


async def _send_announcement(chat_id):
    """/announcement: ask the admin to reply — that reply is what gets sent to everyone."""
    sent = await tg.send_message(chat_id, _t("en", "announcement_prompt"))
    if sent and sent.get("message_id"):
        db.save_announcement_prompt(sent["message_id"])


async def _copy_to_user(tg_id, from_chat_id, message_id):
    """Copy the message to a single user. True — delivered."""
    for attempt in range(2):
        try:
            await tg.copy_message(tg_id, from_chat_id, message_id)
            return True
        except tg.TelegramError as e:
            # 429: Telegram itself says how long to wait — wait it out and try again
            if e.retry_after and attempt == 0:
                await asyncio.sleep(e.retry_after + 1)
                continue
            # 403 — bot blocked, 400 — chat deleted: normal for a broadcast
            if e.error_code not in (400, 403):
                print(f"[announcement] {tg_id}: {e}")
            return False
        except Exception as e:
            print(f"[announcement] {tg_id}: {e}")
            return False
    return False


async def _broadcast_announcement(admin_chat, message_id):
    """Background task wrapper: nobody awaits the broadcast, so errors are caught here
    rather than in handle_update, and two broadcasts are never allowed to run at once."""
    global _broadcast_running
    if _broadcast_running:
        await tg.send_message(admin_chat, _t("en", "announcement_busy"))
        return
    _broadcast_running = True
    try:
        await _run_broadcast(admin_chat, message_id)
    except Exception as e:
        print(f"[announcement] broadcast failed: {e}")
        try:
            await tg.send_message(admin_chat, _t("en", "announcement_failed"))
        except Exception:
            pass
    finally:
        _broadcast_running = False


async def _run_broadcast(admin_chat, message_id):
    """Copies the admin's message to all users, showing progress in their chat."""
    user_ids = db.list_all_user_ids()
    total = len(user_ids)
    if not total:
        await tg.send_message(admin_chat, _t("en", "announcement_empty"))
        return

    status = await tg.send_message(
        admin_chat, _t("en", "announcement_progress").format(done=0, total=total)
    )
    status_id = (status or {}).get("message_id")

    sent = failed = 0
    for i, tg_id in enumerate(user_ids, 1):
        if await _copy_to_user(tg_id, admin_chat, message_id):
            sent += 1
        else:
            failed += 1
        if status_id and i % _BROADCAST_PROGRESS_EVERY == 0 and i < total:
            try:
                await tg.edit_message_text(
                    admin_chat, status_id,
                    _t("en", "announcement_progress").format(done=i, total=total),
                )
            except Exception:
                pass
        await asyncio.sleep(_BROADCAST_DELAY)

    db.log_announcement(sent, failed)
    text = _t("en", "announcement_done").format(sent=sent, failed=failed)
    try:
        if status_id:
            await tg.edit_message_text(admin_chat, status_id, text)
        else:
            await tg.send_message(admin_chat, text)
    except Exception:
        await tg.send_message(admin_chat, text)


async def _on_successful_payment(msg):
    chat_id = msg["chat"]["id"]
    sp = msg["successful_payment"]
    user = db.get_user(chat_id) or db.ensure_user(chat_id)
    lang = user.get("lang", "en")
    db.record_donation(
        sp.get("telegram_payment_charge_id", f"unknown-{int(time.time())}"),
        chat_id, sp.get("total_amount", 0),
    )
    await tg.send_message(chat_id, _t(lang, "thanks").format(stars=sp.get("total_amount", 0)))


# ====================== CHESS.COM + AUTO-ANALYSIS ======================
async def _chesscom_exists(username):
    url = f"https://api.chess.com/pub/player/{username.lower()}"
    try:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": CHESSCOM_USER_AGENT}) as c:
            r = await c.get(url)
            return r.status_code == 200
    except Exception:
        return False


async def _fetch_month_games(username, etag=None):
    """Conditional GET of the Chess.com monthly archive. If an etag is passed and the
    archive hasn't changed, Chess.com returns 304 with no body — saving traffic and
    staying clear of API rate limits.
    Returns (not_modified, games, new_etag)."""
    now = time.gmtime()
    url = (f"https://api.chess.com/pub/player/{username.lower()}"
           f"/games/{now.tm_year}/{now.tm_mon:02d}")
    headers = {"User-Agent": CHESSCOM_USER_AGENT}
    if etag:
        headers["If-None-Match"] = etag
    try:
        async with httpx.AsyncClient(timeout=30, headers=headers) as c:
            r = await c.get(url)
            if r.status_code == 304:
                return True, [], None
            if r.status_code != 200:
                return False, [], None
            return False, r.json().get("games", []), r.headers.get("ETag")
    except Exception as e:
        print(f"[poll] fetch error for {username}: {e}")
        return False, [], None


def _deeplink(game_id):
    # A t.me link opens as a Mini App inside Telegram → startapp parameter (start_param).
    # A plain https address opens as a web page → the frontend reads ?game= (index.html).
    if MINIAPP_DEEPLINK:
        if "t.me/" in MINIAPP_DEEPLINK:
            return f"{MINIAPP_DEEPLINK}?startapp={game_id}"
        return f"{MINIAPP_DEEPLINK}/?game={game_id}"
    if WEBHOOK_BASE_URL:
        return f"{WEBHOOK_BASE_URL}/?game={game_id}"
    return None


def _raw_outcome(game, hero_is_white):
    """win / draw / loss — the outcome goes into meta in this form for statistics."""
    res = (game["white"] if hero_is_white else game["black"]).get("result", "")
    if res == "win":
        return "win"
    if res in _DRAW_RESULTS:
        return "draw"
    return "loss"


def _hero_outcome(game, hero_is_white, lang):
    return _t(lang, _raw_outcome(game, hero_is_white))


def _format_report(lang, game, hero_is_white, summary, game_id, opening=""):
    white = f'{game["white"]["username"]} ({game["white"].get("rating", "?")})'
    black = f'{game["black"]["username"]} ({game["black"].get("rating", "?")})'
    color_word = _t(lang, "white") if hero_is_white else _t(lang, "black")
    outcome = _hero_outcome(game, hero_is_white, lang)
    c = summary["counts"]

    def g(k):
        return c.get(k, 0)

    stats = (f'✨{g("Brilliant")}  ❗{g("Great")}  ⭐{g("Best")}  ✓{g("Excellent")}  '
             f'👍{g("Good")}  ?!{g("Inaccuracy")}  ✗{g("Miss")}  ?{g("Mistake")}  '
             f'??{g("Blunder")}')

    if summary["worst"]:
        _, no, san, best = summary["worst"]
        worst_line = _t(lang, "worst").format(no=no, san=_esc(san), best=_esc(best))
    else:
        worst_line = _t(lang, "clean")

    lines = [
        f'🏁 <b>{_esc(white)} vs {_esc(black)}</b> · {_esc(game.get("time_class", ""))}',
    ]
    if opening:
        lines.append(f'📖 {_esc(opening)}')
    lines += [
        _t(lang, "you_played").format(color=color_word, outcome=outcome),
        _t(lang, "accuracy").format(acc=summary["accuracy"]),
        stats,
        worst_line,
    ]
    link = _deeplink(game_id)
    markup = tg.inline_button_url(_t(lang, "open_btn"), link) if link else None
    return "\n".join(lines), markup


async def _analyze_and_report(tg_id, username, lang, game):
    parsed = analysis.parse_pgn(game.get("pgn", ""))
    if not parsed or not parsed["moves"] or len(parsed["moves"]) > MAX_PLIES:
        return
    results = await analysis.analyze_game_async(
        parsed["start_fen"], parsed["moves"], parsed["clocks"]
    )
    hero_is_white = game["white"]["username"].lower() == username.lower()
    summary = analysis.summarize(results, hero_is_white)

    opening = analysis.detect_opening(parsed["start_fen"], parsed["moves"])
    game_id = game.get("uuid") or _hash(game.get("url", str(game.get("end_time", ""))))
    meta = {
        "white": f'{game["white"]["username"]} ({game["white"].get("rating", "?")})',
        "black": f'{game["black"]["username"]} ({game["black"].get("rating", "?")})',
        "time_control": game.get("time_control", "-"),
        "has_clocks": bool(parsed["clocks"]),
        "opening": opening,
        "start_fen": parsed["start_fen"],
        # the fields below are read by the admin panel (db.get_game_stats)
        "time_class": game.get("time_class", ""),
        "accuracy": summary["accuracy"],
        "blunders": summary["counts"].get("Blunder", 0),
        "result": _raw_outcome(game, hero_is_white),
    }
    db.save_game(game_id, tg_id, results, meta)

    text, markup = _format_report(lang, game, hero_is_white, summary, game_id, opening)
    await tg.send_message(tg_id, text, reply_markup=markup)


async def _poll_user(user):
    tg_id = user["tg_id"]
    username = user["chesscom"]
    lang = user.get("lang", "en")
    last_end = user.get("last_game_end") or 0
    etag = user.get("archive_etag") or None

    not_modified, games, new_etag = await _fetch_month_games(username, etag)
    if not_modified:
        return 0  # archive unchanged (304) — no new games, nothing to download
    if new_etag and new_etag != etag:
        db.set_archive_etag(tg_id, new_etag)
    new_games = sorted(
        [g for g in games if g.get("end_time", 0) > last_end and g.get("pgn")],
        key=lambda g: g.get("end_time", 0),
    )
    if not new_games:
        return 0
    # avoid churning through a big backlog at once — take only the freshest N
    new_games = new_games[-ANALYSIS_MAX_GAMES_PER_POLL:]
    for g in new_games:
        try:
            await _analyze_and_report(tg_id, username, lang, g)
        except Exception as e:
            print(f"[poll] analyze error for {username}: {e}")
        db.set_last_game_end(tg_id, g.get("end_time", 0))
    return len(new_games)


async def poll_all():
    """One scheduler pass: check all subscribers for new games.
    Pass results are stored in _poll_stats — shown by the Server tab."""
    started = time.time()
    users = db.list_subscribers()
    reported = 0
    for user in users:
        try:
            reported += await _poll_user(user) or 0
        except Exception as e:
            _poll_stats["errors"] += 1
            print(f"[poll] user {user.get('tg_id')} error: {e}")
    _poll_stats.update({
        "runs": _poll_stats["runs"] + 1,
        "last_at": int(time.time()),
        "last_secs": time.time() - started,
        "last_users": len(users),
        "last_games": reported,
    })
