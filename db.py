"""SQLite storage"""
import json
import os
import sqlite3
import threading
import time

from config import DB_PATH

_lock = threading.Lock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row


def init_db():
    with _lock:
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                tg_id         INTEGER PRIMARY KEY,
                chesscom      TEXT,
                lang          TEXT    DEFAULT 'en',
                subscribed    INTEGER DEFAULT 0,
                last_game_end INTEGER DEFAULT 0,   -- end_time of the last analyzed game
                archive_etag  TEXT,                -- ETag of the Chess.com monthly archive (conditional GET)
                username      TEXT,                -- Telegram @username (for admin-panel search)
                first_name    TEXT,
                last_seen     INTEGER,             -- last activity (for DAU/WAU/MAU)
                created_at    INTEGER
            );
            CREATE TABLE IF NOT EXISTS games (
                id            TEXT PRIMARY KEY,      -- game uuid from chess.com
                tg_id         INTEGER,
                analysis_json TEXT,
                meta_json     TEXT,
                created_at    INTEGER
            );
            CREATE TABLE IF NOT EXISTS donations (
                charge_id  TEXT PRIMARY KEY,
                tg_id      INTEGER,
                amount     INTEGER,
                created_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS support_tickets (
                admin_msg_id INTEGER PRIMARY KEY,  -- message id in the admin chat (the admin replies to it)
                user_tg_id   INTEGER,              -- ticket author who will receive the reply
                created_at   INTEGER
            );
            CREATE TABLE IF NOT EXISTS announcements (
                admin_msg_id INTEGER PRIMARY KEY,  -- the "reply to this message" prompt in the admin chat
                created_at   INTEGER
            );
            CREATE TABLE IF NOT EXISTS announcement_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                sent       INTEGER,
                failed     INTEGER,
                created_at INTEGER
            );
            """
        )
        # migrations: bring old databases up to the current users schema
        cols = [r["name"] for r in _conn.execute("PRAGMA table_info(users)").fetchall()]
        for name, ddl in (("archive_etag", "TEXT"), ("username", "TEXT"),
                          ("first_name", "TEXT"), ("last_seen", "INTEGER")):
            if name not in cols:
                _conn.execute(f"ALTER TABLE users ADD COLUMN {name} {ddl}")
        # indexes — only after the migration: an old DB doesn't have the last_seen column yet
        _conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_games_created ON games (created_at);
            CREATE INDEX IF NOT EXISTS idx_users_seen    ON users (last_seen);
            """
        )
        _conn.commit()


# ---------- users ----------
def get_user(tg_id):
    with _lock:
        row = _conn.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()
        return dict(row) if row else None


def ensure_user(tg_id, lang=None):
    """Creates the user if they don't exist. Returns the current record."""
    with _lock:
        row = _conn.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()
        if row is None:
            _conn.execute(
                "INSERT INTO users (tg_id, lang, created_at) VALUES (?, ?, ?)",
                (tg_id, lang or "en", int(time.time())),
            )
            _conn.commit()
            row = _conn.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()
        return dict(row)


def touch_user(tg_id, username=None, first_name=None):
    """Records activity (last_seen → DAU/WAU/MAU) and refreshes the profile from Telegram."""
    with _lock:
        _conn.execute(
            "UPDATE users SET last_seen=?, username=COALESCE(?, username), "
            "first_name=COALESCE(?, first_name) WHERE tg_id=?",
            (int(time.time()), username, first_name, tg_id),
        )
        _conn.commit()


def set_lang(tg_id, lang):
    with _lock:
        _conn.execute("UPDATE users SET lang=? WHERE tg_id=?", (lang, tg_id))
        _conn.commit()


def set_chesscom(tg_id, chesscom):
    with _lock:
        _conn.execute("UPDATE users SET chesscom=? WHERE tg_id=?", (chesscom, tg_id))
        _conn.commit()


def set_subscribed(tg_id, subscribed):
    with _lock:
        _conn.execute("UPDATE users SET subscribed=? WHERE tg_id=?", (1 if subscribed else 0, tg_id))
        _conn.commit()


def set_last_game_end(tg_id, end_time):
    with _lock:
        _conn.execute("UPDATE users SET last_game_end=? WHERE tg_id=?", (int(end_time), tg_id))
        _conn.commit()


def set_archive_etag(tg_id, etag):
    with _lock:
        _conn.execute("UPDATE users SET archive_etag=? WHERE tg_id=?", (etag, tg_id))
        _conn.commit()


def list_all_user_ids():
    """All tg_ids for broadcasts (/announcement)."""
    with _lock:
        rows = _conn.execute("SELECT tg_id FROM users ORDER BY tg_id").fetchall()
        return [r["tg_id"] for r in rows]


def list_subscribers():
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM users WHERE subscribed=1 AND chesscom IS NOT NULL AND chesscom<>''"
        ).fetchall()
        return [dict(r) for r in rows]


# ---------- analyzed games cache ----------
def save_game(game_id, tg_id, analysis, meta):
    with _lock:
        _conn.execute(
            "INSERT OR REPLACE INTO games (id, tg_id, analysis_json, meta_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (game_id, tg_id, json.dumps(analysis), json.dumps(meta), int(time.time())),
        )
        _conn.commit()


def get_game(game_id):
    with _lock:
        row = _conn.execute("SELECT * FROM games WHERE id=?", (game_id,)).fetchone()
    if not row:
        return None
    return {"meta": json.loads(row["meta_json"]), "analysis": json.loads(row["analysis_json"])}


# ---------- statistics (admin panel) ----------
DAY = 86400

# Helper readers. IMPORTANT: _lock is not reentrant, so the statistics functions
# access the database only through these and never take the lock from outside.


def _rows(sql, params=()):
    with _lock:
        return [dict(r) for r in _conn.execute(sql, params).fetchall()]


def _one(sql, params=()):
    with _lock:
        row = _conn.execute(sql, params).fetchone()
    return dict(row) if row else {}


def _n(sql, params=()):
    return _one(sql, params).get("n", 0) or 0


def _daily_series(table, days=14):
    """Buckets created_at over the last `days` days — for the mini chart.
    The last bar is the current day."""
    start = int(time.time()) - (days - 1) * DAY
    rows = _rows("SELECT created_at FROM " + table + " WHERE created_at >= ?", (start,))
    buckets = [0] * days
    for r in rows:
        i = int(((r.get("created_at") or 0) - start) // DAY)
        if 0 <= i < days:
            buckets[i] += 1
    return buckets


def get_stats():
    """Key numbers for the main /admin page."""
    now = int(time.time())
    day, week = now - DAY, now - 7 * DAY
    return {
        "users": _n("SELECT COUNT(*) AS n FROM users"),
        "users_day": _n("SELECT COUNT(*) AS n FROM users WHERE created_at >= ?", (day,)),
        "users_week": _n("SELECT COUNT(*) AS n FROM users WHERE created_at >= ?", (week,)),
        "dau": _n("SELECT COUNT(*) AS n FROM users WHERE last_seen >= ?", (day,)),
        "wau": _n("SELECT COUNT(*) AS n FROM users WHERE last_seen >= ?", (week,)),
        "linked": _n("SELECT COUNT(*) AS n FROM users WHERE chesscom IS NOT NULL AND chesscom<>''"),
        "subscribers": _n(
            "SELECT COUNT(*) AS n FROM users "
            "WHERE subscribed=1 AND chesscom IS NOT NULL AND chesscom<>''"
        ),
        "games": _n("SELECT COUNT(*) AS n FROM games"),
        "games_day": _n("SELECT COUNT(*) AS n FROM games WHERE created_at >= ?", (day,)),
        "donations": _n("SELECT COUNT(*) AS n FROM donations"),
        "stars": _n("SELECT COALESCE(SUM(amount), 0) AS n FROM donations"),
        "stars_day": _n(
            "SELECT COALESCE(SUM(amount), 0) AS n FROM donations WHERE created_at >= ?", (day,)
        ),
        "tickets_day": _n(
            "SELECT COUNT(*) AS n FROM support_tickets WHERE created_at >= ?", (day,)
        ),
        "langs": [(r["lang"], r["n"]) for r in _rows(
            "SELECT lang, COUNT(*) AS n FROM users GROUP BY lang ORDER BY n DESC"
        )],
    }


def get_user_stats():
    """"Users" page: growth, activity (DAU/WAU/MAU), and the subscription funnel."""
    now = int(time.time())
    day, week, month = now - DAY, now - 7 * DAY, now - 30 * DAY
    return {
        "total": _n("SELECT COUNT(*) AS n FROM users"),
        "new_day": _n("SELECT COUNT(*) AS n FROM users WHERE created_at >= ?", (day,)),
        "new_week": _n("SELECT COUNT(*) AS n FROM users WHERE created_at >= ?", (week,)),
        "new_month": _n("SELECT COUNT(*) AS n FROM users WHERE created_at >= ?", (month,)),
        "dau": _n("SELECT COUNT(*) AS n FROM users WHERE last_seen >= ?", (day,)),
        "wau": _n("SELECT COUNT(*) AS n FROM users WHERE last_seen >= ?", (week,)),
        "mau": _n("SELECT COUNT(*) AS n FROM users WHERE last_seen >= ?", (month,)),
        "silent": _n(
            "SELECT COUNT(*) AS n FROM users WHERE last_seen IS NULL OR last_seen < ?", (month,)
        ),
        "linked": _n("SELECT COUNT(*) AS n FROM users WHERE chesscom IS NOT NULL AND chesscom<>''"),
        "subscribed": _n(
            "SELECT COUNT(*) AS n FROM users "
            "WHERE subscribed=1 AND chesscom IS NOT NULL AND chesscom<>''"
        ),
        "churned": _n(
            "SELECT COUNT(*) AS n FROM users "
            "WHERE subscribed=0 AND chesscom IS NOT NULL AND chesscom<>''"
        ),
        "daily": _daily_series("users"),
        "langs": [(r["lang"], r["n"]) for r in _rows(
            "SELECT lang, COUNT(*) AS n FROM users GROUP BY lang ORDER BY n DESC LIMIT 5"
        )],
        "recent": _rows(
            "SELECT tg_id, username, first_name, chesscom, created_at FROM users "
            "ORDER BY created_at DESC LIMIT 5"
        ),
    }


def get_game_stats():
    """"Games" page: volume, play quality, and what exactly is being analyzed."""
    now = int(time.time())
    day, week, month = now - DAY, now - 7 * DAY, now - 30 * DAY
    acc = _one(
        "SELECT COUNT(*) AS n, ROUND(AVG(json_extract(meta_json, '$.accuracy')), 1) AS avg_acc, "
        "ROUND(AVG(json_extract(meta_json, '$.blunders')), 2) AS avg_blunders "
        "FROM games WHERE json_extract(meta_json, '$.accuracy') IS NOT NULL"
    )
    res = {r["res"]: r["n"] for r in _rows(
        "SELECT json_extract(meta_json, '$.result') AS res, COUNT(*) AS n FROM games "
        "WHERE json_extract(meta_json, '$.result') IS NOT NULL GROUP BY res"
    )}
    return {
        "total": _n("SELECT COUNT(*) AS n FROM games"),
        "day": _n("SELECT COUNT(*) AS n FROM games WHERE created_at >= ?", (day,)),
        "week": _n("SELECT COUNT(*) AS n FROM games WHERE created_at >= ?", (week,)),
        "month": _n("SELECT COUNT(*) AS n FROM games WHERE created_at >= ?", (month,)),
        "players": _n("SELECT COUNT(DISTINCT tg_id) AS n FROM games"),
        "rated": acc.get("n", 0),
        "avg_accuracy": acc.get("avg_acc"),
        "avg_blunders": acc.get("avg_blunders"),
        "wins": res.get("win", 0),
        "losses": res.get("loss", 0),
        "draws": res.get("draw", 0),
        "daily": _daily_series("games"),
        "openings": [(r["op"], r["n"]) for r in _rows(
            "SELECT json_extract(meta_json, '$.opening') AS op, COUNT(*) AS n FROM games "
            "WHERE op IS NOT NULL AND op <> '' GROUP BY op ORDER BY n DESC LIMIT 5"
        )],
        "time_classes": [(r["tc"], r["n"]) for r in _rows(
            "SELECT json_extract(meta_json, '$.time_class') AS tc, COUNT(*) AS n FROM games "
            "WHERE tc IS NOT NULL AND tc <> '' GROUP BY tc ORDER BY n DESC LIMIT 5"
        )],
        "top_players": _rows(
            "SELECT g.tg_id, u.username, u.chesscom, COUNT(*) AS n FROM games g "
            "LEFT JOIN users u ON u.tg_id = g.tg_id "
            "GROUP BY g.tg_id ORDER BY n DESC LIMIT 5"
        ),
    }


def get_money_stats():
    """"Stars" page: how much is donated, by whom, and when."""
    now = int(time.time())
    day, week, month = now - DAY, now - 7 * DAY, now - 30 * DAY

    def total_since(ts):
        return _n(
            "SELECT COALESCE(SUM(amount), 0) AS n FROM donations WHERE created_at >= ?", (ts,)
        )

    agg = _one(
        "SELECT COUNT(*) AS cnt, COALESCE(SUM(amount), 0) AS total, "
        "COUNT(DISTINCT tg_id) AS donors, COALESCE(MAX(amount), 0) AS best, "
        "COALESCE(MAX(created_at), 0) AS last_at FROM donations"
    )
    cnt = agg.get("cnt", 0)
    return {
        "count": cnt,
        "total": agg.get("total", 0),
        "donors": agg.get("donors", 0),
        "avg": round(agg.get("total", 0) / cnt, 1) if cnt else 0,
        "best": agg.get("best", 0),
        "last_at": agg.get("last_at", 0),
        "day": total_since(day),
        "week": total_since(week),
        "month": total_since(month),
        "daily": _daily_series("donations"),
        "top": _rows(
            "SELECT d.tg_id, u.username, u.first_name, SUM(d.amount) AS amount, COUNT(*) AS n "
            "FROM donations d LEFT JOIN users u ON u.tg_id = d.tg_id "
            "GROUP BY d.tg_id ORDER BY amount DESC LIMIT 5"
        ),
    }


def get_support_stats():
    """Support tickets and the broadcast log."""
    now = int(time.time())
    day, week = now - DAY, now - 7 * DAY
    last = _one(
        "SELECT sent, failed, created_at FROM announcement_log ORDER BY created_at DESC LIMIT 1"
    )
    return {
        "tickets": _n("SELECT COUNT(*) AS n FROM support_tickets"),
        "tickets_day": _n(
            "SELECT COUNT(*) AS n FROM support_tickets WHERE created_at >= ?", (day,)
        ),
        "tickets_week": _n(
            "SELECT COUNT(*) AS n FROM support_tickets WHERE created_at >= ?", (week,)
        ),
        "ticket_users": _n("SELECT COUNT(DISTINCT user_tg_id) AS n FROM support_tickets"),
        "announcements": _n("SELECT COUNT(*) AS n FROM announcement_log"),
        "last_announcement": last or None,
    }


def get_db_info():
    """DB file size and table row counts"""
    try:
        size = os.path.getsize(DB_PATH)
    except OSError:
        size = 0
    tables = {}
    for t in ("users", "games", "donations", "support_tickets", "announcement_log"):
        tables[t] = _n("SELECT COUNT(*) AS n FROM " + t)
    return {"size": size, "tables": tables, "path": DB_PATH}


# ---------- user card (/user in the admin panel) ----------
def find_user(query):
    """Finds a user by tg_id, Telegram @username, or Chess.com username."""
    q = str(query).strip().lstrip("@")
    if not q:
        return None
    if q.lstrip("-").isdigit():
        row = _one("SELECT * FROM users WHERE tg_id=?", (int(q),))
        if row:
            return row
    return _one(
        "SELECT * FROM users WHERE LOWER(username)=LOWER(?) OR LOWER(chesscom)=LOWER(?) LIMIT 1",
        (q, q),
    ) or None


def get_user_card(tg_id):
    """One user's profile plus their activity (for /user in the admin panel)."""
    user = _one("SELECT * FROM users WHERE tg_id=?", (tg_id,))
    if not user:
        return None
    games = _one(
        "SELECT COUNT(*) AS n, COALESCE(MAX(created_at), 0) AS last_at, "
        "ROUND(AVG(json_extract(meta_json, '$.accuracy')), 1) AS avg_acc "
        "FROM games WHERE tg_id=?", (tg_id,)
    )
    money = _one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS total FROM donations WHERE tg_id=?",
        (tg_id,),
    )
    return {
        "user": user,
        "games": games.get("n", 0),
        "games_last_at": games.get("last_at", 0),
        "avg_accuracy": games.get("avg_acc"),
        "donations": money.get("n", 0),
        "stars": money.get("total", 0),
        "tickets": _n("SELECT COUNT(*) AS n FROM support_tickets WHERE user_tg_id=?", (tg_id,)),
    }


# ---------- support tickets ----------
def save_ticket(admin_msg_id, user_tg_id):
    """Remember which message in the admin chat must be replied to
    so that the reply goes to this user."""
    with _lock:
        _conn.execute(
            "INSERT OR REPLACE INTO support_tickets (admin_msg_id, user_tg_id, created_at) "
            "VALUES (?, ?, ?)",
            (int(admin_msg_id), int(user_tg_id), int(time.time())),
        )
        _conn.commit()


def get_ticket_user(admin_msg_id):
    """Given the id of the message the admin replied to, return the ticket author (or None)."""
    if admin_msg_id is None:
        return None
    with _lock:
        row = _conn.execute(
            "SELECT user_tg_id FROM support_tickets WHERE admin_msg_id=?", (int(admin_msg_id),)
        ).fetchone()
        return row["user_tg_id"] if row else None


# ---------- broadcasts ----------
def save_announcement_prompt(admin_msg_id):
    with _lock:
        _conn.execute(
            "INSERT OR REPLACE INTO announcements (admin_msg_id, created_at) VALUES (?, ?)",
            (int(admin_msg_id), int(time.time())),
        )
        _conn.commit()


def pop_announcement_prompt(admin_msg_id):
    if admin_msg_id is None:
        return False
    with _lock:
        cur = _conn.execute(
            "DELETE FROM announcements WHERE admin_msg_id=?", (int(admin_msg_id),)
        )
        _conn.commit()
        return cur.rowcount > 0


def log_announcement(sent, failed):
    """Record the broadcast outcome — the admin panel shows when the last one went out and to how many."""
    with _lock:
        _conn.execute(
            "INSERT INTO announcement_log (sent, failed, created_at) VALUES (?, ?, ?)",
            (int(sent), int(failed), int(time.time())),
        )
        _conn.commit()


# ---------- donations ----------
def record_donation(charge_id, tg_id, amount):
    with _lock:
        _conn.execute(
            "INSERT OR IGNORE INTO donations (charge_id, tg_id, amount, created_at) VALUES (?, ?, ?, ?)",
            (charge_id, tg_id, int(amount), int(time.time())),
        )
        _conn.commit()
