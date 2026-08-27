# Chess Analysis

A Telegram Mini App and bot that reviews chess games with the Stockfish engine.

You paste a PGN (or pick a game from a Chess.com archive) and get a full review:
a verdict for every move, accuracy for both players, an evaluation graph, and the
name of the opening. The bot can also watch your Chess.com account and send a short
report every time you finish a new game.

The backend is FastAPI, the frontend is one static `index.html` file, and the data
lives in SQLite.

## Features

**Game review**
- A verdict for each move, in Chess.com terms: `Brilliant`, `Great`, `Best`,
  `Excellent`, `Good`, `Book`, `Forced`, `Inaccuracy`, `Mistake`, `Miss`, `Blunder`.
- Accuracy per player, and separate accuracy for the opening, middlegame and endgame.
- Evaluation graph with phase bands — tap it to jump to any move.
- Opening name from the ECO database built out of the lichess tables.
- Clock times are read from the PGN when they are there.

**Interactive board**
- Drag the pieces to play your own line from any position in the game.
- Every move you try is evaluated by the engine and gets the same verdict as a game move.

**Telegram bot**
- `/subscribe <chesscom-username>` — the server checks your archive on a timer and
  reviews each new game automatically, then sends a summary with a button that opens
  the full review in the Mini App.
- Donations with Telegram Stars.
- Support tickets: the user writes to the bot, the admin answers in their own chat,
  and the answer goes back to the right user.
- Admin panel (`/admin`) with five tabs: overview, users, games, stars, server.

## Project layout

| File | What it does |
| --- | --- |
| `server.py` | FastAPI app: HTTP API, the Telegram webhook, and the auto-analysis timer. |
| `analysis.py` | The engine pool, move classification, accuracy, game phases, opening lookup. |
| `bot.py` | Bot commands, payments, support, admin panel, Chess.com polling. |
| `tg.py` | Thin async wrappers around the Telegram Bot API. |
| `db.py` | SQLite storage and the numbers shown in the admin panel. |
| `config.py` | Reads settings from environment variables or `.env`. |
| `build_openings.py` | Builds `openings.json`. Run it once; not needed at runtime. |
| `index.html` | The whole Mini App: board, review, graph, archive browser. |
| `img/` | Piece images for the board. |

## Requirements

- Python 3.10 or newer (the Docker image uses 3.12).
- A Stockfish binary.
- A Telegram bot token — only if you want the bot. Without a token the web part
  still works, and the bot and auto-analysis simply stay off.

## Run it locally

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Get Stockfish from https://stockfishchess.org/download/ and put it next to the
project, or point `STOCKFISH_PATH` at it.

```bash
cp .env.example .env            # then open .env and fill in what you need
uvicorn server:app --reload
```

Open http://127.0.0.1:8000 — the Mini App works in a normal browser too.

The bot needs a public HTTPS address, because Telegram delivers updates by webhook.
For local work, start a tunnel (`ngrok http 8000` or `cloudflared`) and put the
address it gives you into `WEBHOOK_BASE_URL`.

## Run it with Docker

```bash
cp .env.example .env
cp docker-compose.example.yml docker-compose.yml
cp Caddyfile.example Caddyfile          # put your domain in it
docker compose up -d --build
```

Two containers start: the app itself and Caddy in front of it. Caddy gets the HTTPS
certificate on its own, so the domain must already point at the server, and ports
80 and 443 must be free.

A few things worth knowing:

- Stockfish comes from the Debian package inside the image, so no `.exe` is needed.
- The database lives on the `dbdata` volume and survives a rebuild.
- The app runs with a single uvicorn worker on purpose. Several engines already run
  in parallel inside one process, and extra workers would only multiply them.
- Keep the `caddy_data` volume. It holds the certificates, and losing it means asking
  Let's Encrypt for new ones on every restart.

## Settings

All settings come from environment variables, or from a `.env` file next to the
project. `.env.example` lists every one of them.

| Variable | Default | What it is |
| --- | --- | --- |
| `BOT_TOKEN` | empty | Token from @BotFather. Empty means no bot and no auto-analysis. |
| `WEBHOOK_BASE_URL` | empty | Public HTTPS address of the server, no trailing slash. |
| `WEBHOOK_SECRET` | `chess-analis-secret` | Any random string. Telegram sends it back in a header and the server checks it. |
| `MINIAPP_DEEPLINK` | empty | Mini App link for the button that opens a review. |
| `ADMIN_USERNAME` | — | Telegram username of the admin, without `@`. |
| `ADMIN_ID` | `0` | Numeric Telegram id of the admin. Stricter than the username. |
| `STOCKFISH_PATH` | `./stockfish.exe` | Path to the engine. Docker overrides it to `/usr/games/stockfish`. |
| `ENGINE_DEPTH` | `13` | Search depth. Higher is better and slower. |
| `ENGINE_THREADS` | `1` | Threads for a single engine. |
| `ENGINE_HASH` | `64` | Hash table size in MB, per engine. |
| `MAX_PLIES` | `300` | Longest game the server accepts, in half-moves. |
| `MAX_PGN_CHARS` | `100000` | Largest PGN the server accepts. |
| `DB_PATH` | `chessanalis.db` | SQLite file. |
| `POLL_INTERVAL_SECONDS` | `300` | How often the archives of subscribers are checked. |
| `ANALYSIS_MAX_GAMES_PER_POLL` | `3` | Most games analyzed for one user in one pass. |
| `CHESSCOM_USER_AGENT` | see `.env.example` | Chess.com blocks requests without a User-Agent. Put your own contact in it. |

The engine pool size is not a setting. It is the number of CPUs, up to four, decided
at startup.

## HTTP API

| Method | Path | What it does |
| --- | --- | --- |
| `GET` | `/` | Serves the Mini App. |
| `POST` | `/analyze` | Full game review. Body: `{"pgn": "..."}`. |
| `POST` | `/analyze-position` | Evaluates one position. Body: `{"fen": "..."}`. |
| `POST` | `/analyze-move` | Evaluates one move and gives it a verdict. Body: `{"fen": "...", "move": "e2e4"}`. |
| `GET` | `/game/{id}` | A review that was saved earlier, for the deep link. |
| `POST` | `/create-invoice` | Creates a Telegram Stars payment link. |
| `POST` | `/telegram/webhook` | Where Telegram sends updates. Not for manual use. |

The Mini App asks Chess.com for game archives straight from the browser, so that
traffic never touches the server.

## Bot commands

| Command | What it does |
| --- | --- |
| `/start` | Short intro. |
| `/help` | The list of commands. |
| `/subscribe <username>` | Links a Chess.com account and turns auto-analysis on. `/link` does the same. |
| `/unsubscribe` | Turns auto-analysis off. The account stays linked. |
| `/status` | Shows the linked account and whether auto-analysis is on. |
| `/support <text>` | Sends a message to the admin. Without text, the bot asks for it. |
| `/donate` | How to support the author with Stars. |

Admin only:

| Command | What it does |
| --- | --- |
| `/admin` | The admin panel. |
| `/user <id or @nick or chesscom>` | Card for one user. |
| `/announcement` | Asks you to reply with a message, then sends that message to everyone. |

Only games played after `/subscribe` are analyzed, so subscribing does not start a
review of your whole history.

## How moves are graded

Every position is evaluated twice: before the move and after it. Both evaluations are
turned into a win chance from 0 to 100 percent with the lichess formula, and the
difference between them — the win chance the move threw away — decides the verdict.

The scale is: under 2 percent is `Excellent`, under 5 is `Good`, under 10 is
`Inaccuracy`, under 20 is `Mistake`, and anything above that is a `Blunder`. Playing
the engine's own move gives `Best`.

Five labels ignore that scale:

- `Book` — the position is still in opening theory.
- `Forced` — there was only one legal move.
- `Brilliant` — a strong move that gives away real material and still works. The
  sacrifice is measured with static exchange evaluation, so a piece that is simply
  won back on the next move does not count.
- `Great` — the only good move in the position: every other try is clearly worse.
- `Miss` — you were winning, the opponent slipped, and the move let them off.

Phases are decided by how much material is left and how empty the back rank is,
close to the way Chess.com does it. A phase never goes backwards.

All the thresholds sit at the top of `analysis.py` as named constants, so they are
easy to change.

## The opening database

`openings.json` ships with the project, so you only need this if you want to rebuild it:

```bash
python build_openings.py
```

The script takes the open ECO tables from
[lichess-org/chess-openings](https://github.com/lichess-org/chess-openings) and turns
them into a map of position to name. It keeps every position along a line, not only
the last one, because a prefix of a theoretical line is theory too. `analysis.py`
loads the file at startup. If the file is missing, the server still runs, but it falls
back to a handful of common lines without names.

## Limits

- A game is analyzed by one engine, and there are at most four of them. Long games at
  a high depth take a while.
- A review is stored only when it comes from auto-analysis. A PGN you paste by hand is
  analyzed and shown, but not saved.
- The Mini App needs a network connection: jQuery, chessboard.js and chess.js come
  from a CDN.
