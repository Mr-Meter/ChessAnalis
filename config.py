"""Project configuration. All secrets and settings come from environment variables
(or from a .env file next to the project). See .env.example."""
import os


def _load_dotenv(path=".env"):
    """Minimal .env loader with no third-party dependencies."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key.strip(), value)


_load_dotenv()


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return int(default)


# --- Stockfish / analysis ---
STOCKFISH_PATH = os.environ.get("STOCKFISH_PATH", "./stockfish.exe")
ENGINE_DEPTH = _int("ENGINE_DEPTH", 13)
ENGINE_THREADS = _int("ENGINE_THREADS", 1)   # threads per SINGLE engine
ENGINE_HASH = _int("ENGINE_HASH", 64)        # MB of hash per engine
MAX_PLIES = _int("MAX_PLIES", 300)           # max game length 
MAX_PGN_CHARS = _int("MAX_PGN_CHARS", 100_000)

# --- Telegram bot ---
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
# Public HTTPS address of the server
WEBHOOK_BASE_URL = os.environ.get("WEBHOOK_BASE_URL", "").strip().rstrip("/")
# Arbitrary secret string
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "chess-analis-secret").strip()
WEBHOOK_PATH = "/telegram/webhook"
# Mini App link for the "Open analysis" deep link
MINIAPP_DEEPLINK = os.environ.get("MINIAPP_DEEPLINK", "").strip().rstrip("/")
# Bot admin
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "Mr_Meter").strip().lstrip("@").lower()
ADMIN_ID = _int("ADMIN_ID", 0)

# --- Storage and auto-analysis ---
DB_PATH = os.environ.get("DB_PATH", "chessanalis.db")
POLL_INTERVAL_SECONDS = _int("POLL_INTERVAL_SECONDS", 300)        # Chess.com polling (default 5 min)
ANALYSIS_MAX_GAMES_PER_POLL = _int("ANALYSIS_MAX_GAMES_PER_POLL", 3)  # at most N new games per pass
# User-Agent is required — chess.com blocks requests without it
CHESSCOM_USER_AGENT = os.environ.get(
    "CHESSCOM_USER_AGENT",
    "ChessAnalisBot/1.0 (Telegram Mini App; contact: petr.rakets@gmail.com)",
)

BOT_ENABLED = bool(BOT_TOKEN)
