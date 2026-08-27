import asyncio
from contextlib import asynccontextmanager

import chess
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import analysis
import bot
import db
import tg
from config import (
    BOT_ENABLED, MAX_PGN_CHARS, MAX_PLIES, POLL_INTERVAL_SECONDS,
    WEBHOOK_BASE_URL, WEBHOOK_PATH, WEBHOOK_SECRET,
)


class AnalysisRequest(BaseModel):
    pgn: str


class PositionRequest(BaseModel):
    fen: str


class MoveRequest(BaseModel):
    fen: str                        # position BEFORE the move
    move: str                       # move played, in UCI (e2e4, e7e8q)
    prev_win_drop: float | None = None   # win% loss of the line's previous move (for Miss)


class InvoiceRequest(BaseModel):
    amount: int


async def _scheduler():
    """Background auto-analysis loop: polls Chess.com every POLL_INTERVAL_SECONDS."""
    await asyncio.sleep(10)  # let the server come up
    while True:
        try:
            await bot.poll_all()
        except Exception as e:
            print(f"[scheduler] error: {e}")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await analysis.startup_pool()
    db.init_db()
    app.state.scheduler_task = None

    if BOT_ENABLED:
        if WEBHOOK_BASE_URL:
            try:
                await tg.set_webhook(WEBHOOK_BASE_URL + WEBHOOK_PATH, WEBHOOK_SECRET)
                print(f"[bot] webhook installed: {WEBHOOK_BASE_URL + WEBHOOK_PATH}")
            except Exception as e:
                print(f"[bot] Failed to set up the webhook.: {e}")
        else:
            print("[bot] WEBHOOK_BASE_URL Not set — Telegram; I won't be able to send updates.")
        app.state.scheduler_task = asyncio.create_task(_scheduler())
    else:
        print("[bot] BOT_TOKEN not set — bot and auto-analysis disabled")

    try:
        yield
    finally:
        if app.state.scheduler_task:
            app.state.scheduler_task.cancel()
        await analysis.shutdown_pool()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/img", StaticFiles(directory="img"), name="img")


@app.get("/")
def get_index():
    return FileResponse("index.html")


@app.post("/analyze")
async def analyze_game(request: AnalysisRequest):
    if not analysis.engines_ready():
        raise HTTPException(status_code=503, detail="Stockfish engine unavailable")

    raw_pgn = request.pgn.strip()
    if len(raw_pgn) > MAX_PGN_CHARS:
        raise HTTPException(status_code=400, detail="PGN too large")

    parsed = analysis.parse_pgn(raw_pgn)
    if not parsed or not parsed["moves"]:
        # chess.pgn leniently parses any text into an "empty game" — treat that as an error
        raise HTTPException(status_code=400, detail="Invalid PGN format")
    if len(parsed["moves"]) > MAX_PLIES:
        raise HTTPException(status_code=400,
                            detail=f"Game too long (> {MAX_PLIES} plies)")

    try:
        results = await analysis.analyze_game_async(
            parsed["start_fen"], parsed["moves"], parsed["clocks"]
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Analysis error: {str(e)}")

    m = parsed["meta"]
    return {
        "meta": {
            "white": f'{m["white"]} ({m["white_elo"]})',
            "black": f'{m["black"]} ({m["black_elo"]})',
            "time_control": m["time_control"],
            "has_clocks": len(parsed["clocks"]) > 0,
            "opening": analysis.detect_opening(parsed["start_fen"], parsed["moves"]),
            "start_fen": parsed["start_fen"],
        },
        "analysis": results,
    }


@app.post("/analyze-position")
async def analyze_position(req: PositionRequest):
    """Evaluates a single position (FEN) — for the interactive board, where the user
    makes moves themselves and wants to see the evaluation and the engine's best reply."""
    if not analysis.engines_ready():
        raise HTTPException(status_code=503, detail="Stockfish engine unavailable")
    try:
        chess.Board(req.fen)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid FEN")
    try:
        return await analysis.analyze_position_async(req.fen)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Analysis error: {str(e)}")


@app.post("/analyze-move")
async def analyze_move(req: MoveRequest):
    """Evaluates one move made on the interactive board: a verdict (Brilliant…Blunder)
    using the same logic as full-game analysis, plus the evaluation of the position after the move."""
    if not analysis.engines_ready():
        raise HTTPException(status_code=503, detail="Stockfish engine unavailable")
    try:
        board = chess.Board(req.fen)
        move = chess.Move.from_uci(req.move)
        if move not in board.legal_moves:
            raise ValueError
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid FEN or move")
    try:
        return await analysis.analyze_move_async(req.fen, req.move, req.prev_win_drop)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Analysis error: {str(e)}")


@app.get("/game/{game_id}")
def get_cached_game(game_id: str):
    """Returns a previously analyzed game for the "Open analysis" deep link in the Mini App."""
    game = db.get_game(game_id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")
    return game


@app.post("/create-invoice")
async def create_invoice(req: InvoiceRequest):
    """Creates a Telegram Stars payment link (donation to the author of any amount)."""
    if not BOT_ENABLED:
        raise HTTPException(status_code=503, detail="Donations unavailable: bot is not configured")
    amount = max(1, min(10000, int(req.amount)))
    try:
        link = await tg.create_invoice_link(
            title="Support the author",
            description=f"Donation of {amount}⭐ — thank you for supporting the project!",
            payload=f"donation:{amount}",
            amount_stars=amount,
            label="Donation ⭐",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create invoice: {e}")
    return {"invoice_link": link, "amount": amount}


@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    if WEBHOOK_SECRET and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")
    update = await request.json()
    await bot.handle_update(update)
    return {"ok": True}
