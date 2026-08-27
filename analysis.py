"""Chess analysis core."""
import asyncio
import io
import json
import math
import os
import queue
import re
from contextlib import contextmanager

import chess
import chess.engine
import chess.pgn

from config import (
    STOCKFISH_PATH, ENGINE_DEPTH, ENGINE_THREADS, ENGINE_HASH,
)

PIECE_VALUES = {
    chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3,
    chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0,
}
MATE_THRESHOLD = 9000  # a score >= this means forced mate, not just an advantage
MIN_SACRIFICE = 2      # minimum NET sacrifice for !! (in pawns). 2 = "a piece for a pawn":
                       # a bishop en prise to a pawn, with a pawn won back, gives exactly 3-1=2.
                       # Equal trades give 0, a hanging pawn gives 1, so no false sacrifices.

# --- MOVE CLASSIFICATION THRESHOLDS ---
# chess.com rates a move not in "raw" centipawns but by the drop in win chance (win%):
# losing 200 cp in an equal position is a disaster; with an extra queen it's almost nothing.
WD_EXCELLENT = 2.0     # Excellent — "almost as good as the best move"
WD_GOOD = 5.0          # Good — "worse than the best, but doesn't spoil the position"
WD_INACCURACY = 10.0   # Inaccuracy — "the position is slightly worse"
WD_MISTAKE = 20.0      # Mistake — "the position is noticeably worse"; beyond that — Blunder
BEST_EQUAL_CP = 5      # move not from the PV but equal to the best by eval — still Best
GREAT_GAP = 10.0       # Great — the second-best move is this many win% worse than the best
ENDGAME_PIECES = 6     # endgame: <= this many pieces left on the board (excluding pawns and kings)
MIDGAME_PIECES = 10    # middlegame: pieces down to <= this many (trades have begun)
BACKRANK_SPARSE = 4    # ...or fewer than this many pieces left on the 1st/8th rank (development done)
OPENING_MAX_MOVES = 15 # ...or more than this many full moves played — the opening is definitely over
MISS_MIN_WIN = 60.0    # Miss — what win chance we had BEFORE the move
MISS_MIN_DROP = 15.0   # Miss — how much of that chance the move threw away

# --- OPENING THEORY DATABASE (ECO) ---
# openings.json: {position_EPD: "ECO Name"} — built by build_openings.py from the
# open lichess database (~3700 openings). EPD = the first 4 FEN fields, so it matches
# the key we compute from the moves.
OPENINGS = {}
OPENING_BOOK_DEPTH = 30  # opening theory reaches no further — don't scan the whole game

# Mini fallback if openings.json is unavailable (no names — just the "Book" label).
_FALLBACK_LINES = [
    ["e4", "e5", "Nf3", "Nc6", "Bb5"], ["e4", "c5", "Nf3"],
    ["e4", "c6", "d4", "d5"], ["e4", "e6", "d4", "d5"],
    ["d4", "d5", "c4"], ["d4", "Nf6", "c4", "g6"], ["c4", "e5"], ["Nf3", "d5"],
]


def _load_openings(path="openings.json"):
    global OPENINGS
    try:
        with open(path, encoding="utf-8") as f:
            OPENINGS = json.load(f)
        print(f"[openings] loaded openings: {len(OPENINGS)}")
        return
    except Exception as e:
        print(f"[openings] {path} unavailable ({e}) — minimal fallback without names")
    OPENINGS = {}
    for line in _FALLBACK_LINES:
        b = chess.Board()
        for san in line:
            try:
                b.push_san(san)
                OPENINGS.setdefault(b.epd(), "")
            except Exception:
                break


_load_openings()
BOOK_FENS = set(OPENINGS.keys())   # positions considered theory (verdict = Book)


def detect_opening(start_fen, moves):
    """Name of the opening played = the deepest known position along the game line.
    Returns 'ECO Name' or '' (non-standard start / no matches)."""
    try:
        board = chess.Board(start_fen)
    except Exception:
        return ""
    name = ""
    for mv in moves[:OPENING_BOOK_DEPTH]:
        if mv not in board.legal_moves:
            break
        board.push(mv)
        hit = OPENINGS.get(board.epd())
        if hit:
            name = hit
    return name


# ====================== ENGINE POOL ======================
# Stockfish is a separate CPU-bound process. We keep a fixed pool of reusable
# engines and admit no more concurrent analyses than there are engines
# (semaphore); the rest wait in the queue. Both the web server and the bot use this.
POOL_SIZE = max(1, min(4, (os.cpu_count() or 2)))
_engine_pool: "queue.Queue" = queue.Queue()
_engine_sem = None          # asyncio.Semaphore, created at startup
_engines_ready = 0


def _make_engine():
    eng = chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH)
    eng.configure({"Threads": ENGINE_THREADS, "Hash": ENGINE_HASH})
    return eng


def _fill_pool():
    created = 0
    for _ in range(POOL_SIZE):
        try:
            _engine_pool.put(_make_engine())
            created += 1
        except Exception as e:
            print(f"[engine] Failed to start Stockfish: {e}")
    return created


async def startup_pool():
    """Starts the engines once at server startup."""
    global _engine_sem, _engines_ready
    loop = asyncio.get_running_loop()
    created = await loop.run_in_executor(None, _fill_pool)
    _engine_sem = asyncio.Semaphore(created or 1)
    _engines_ready = created
    print(f"[engine] Ready engines in the pool: {created}/{POOL_SIZE}")
    return created


async def shutdown_pool():
    while not _engine_pool.empty():
        try:
            _engine_pool.get_nowait().quit()
        except Exception:
            pass


def engines_ready():
    return _engines_ready


# ====================== SACRIFICE DETECTOR / !! ======================
def _win_pct(cp):
    """Converts an eval (centipawns, from the mover's point of view) into a win chance 0..100.
    Lichess formula. Needed both for accuracy and for move classification."""
    return 50 + 50 * (2 / (1 + math.exp(-0.00368208 * cp)) - 1)


def see_on_square(board, square):
    """Static exchange evaluation (SEE) on square `square`. The side to move starts
    a series of captures (each time capturing with the least valuable piece). Returns
    the net material gain of the initiating side (in pawns). A defended piece and
    an equal trade give 0; pins/x-rays are respected (only legal moves are considered)."""
    target = board.piece_at(square)
    if target is None:
        return 0
    legal_caps = [m for m in board.legal_moves
                  if m.to_square == square and board.is_capture(m)]
    if not legal_caps:
        return 0
    move = min(legal_caps,
               key=lambda m: PIECE_VALUES[board.piece_at(m.from_square).piece_type])
    captured_val = PIECE_VALUES[target.piece_type]
    board.push(move)
    gain = captured_val - see_on_square(board, square)
    board.pop()
    return max(0, gain)


def _least_valuable_capture(board, square):
    """Legal capture on `square` by the least valuable piece (or None)."""
    caps = [m for m in board.legal_moves
            if m.to_square == square and board.is_capture(m)]
    if not caps:
        return None
    return min(caps, key=lambda m: PIECE_VALUES[board.piece_at(m.from_square).piece_type])


def _best_see_grab(board, exclude_sq=None):
    """Best immediate material gain (by SEE) for the side to move, over all squares
    except exclude_sq. Needed to assess the COMPENSATION for a sacrifice."""
    stm = board.turn
    best = 0
    probe = board.copy()
    for sq in chess.SQUARES:
        if sq == exclude_sq:
            continue
        piece = board.piece_at(sq)
        if piece and piece.color != stm and board.is_attacked_by(stm, sq):
            best = max(best, see_on_square(probe, sq))
    return best


def find_sacrificed_material(board_after):
    """Maximum NET sacrifice: a piece of the side that just moved which the opponent
    can profitably take (by SEE), MINUS our immediate counter-gain on another
    square after that capture. Mutually hanging pieces (desperado trades like
    Bxb7/Bxb2 with a pair of hanging rooks) don't count as a sacrifice: whatever
    gets taken is immediately won back. 0 — no real sacrifice."""
    opponent = board_after.turn
    our_color = not opponent
    best_sac = 0
    probe = board_after.copy()
    for sq in chess.SQUARES:
        piece = board_after.piece_at(sq)
        if not (piece and piece.color == our_color and board_after.is_attacked_by(opponent, sq)):
            continue
        gross = see_on_square(probe, sq)   # what the opponent wins on this square
        if gross <= best_sac:
            continue                        # the net sacrifice can't exceed the one already found
        cap = _least_valuable_capture(board_after, sq)
        if cap is None:
            continue
        # the opponent takes the piece -> compute our best immediate counter-gain
        after_cap = board_after.copy()
        after_cap.push(cap)
        compensation = _best_see_grab(after_cap, exclude_sq=sq)  # the trade on sq is already covered by SEE
        best_sac = max(best_sac, gross - compensation)
    return best_sac


def is_brilliant(board_after, move, best_score, actual_score):
    """Brilliant move (!!) — logic close to chess.com's.
      1. the move is nearly the best; 2. after the move we are not losing (the sacrifice is sound);
      3. we weren't already completely winning (forced mate is allowed);
      4. the move does NOT capture a piece (capturing a piece is a trade, not a sacrifice; a pawn at most);
      5. a real NET sacrifice >= "a piece for a pawn" remains on the board (SEE
         minus the immediate counter-gain)."""
    if best_score - actual_score > 50:
        return False
    if actual_score < -50:
        return False
    if best_score > 600 and actual_score < MATE_THRESHOLD:
        return False
    board_before = board_after.copy()
    board_before.pop()
    if board_before.is_en_passant(move):
        captured_val = 1
    else:
        captured_piece = board_before.piece_at(move.to_square)
        captured_val = PIECE_VALUES[captured_piece.piece_type] if captured_piece else 0
    if captured_val > 1:
        return False
    return find_sacrificed_material(board_after) >= MIN_SACRIFICE


def is_great(board_before, move, played_is_best, best_score, second_score,
             only_one_move, opponent_last_to):
    """Great (!) — "the only good move": the best move was played, and any other attempt
    would noticeably spoil the position (the second-best move is GREAT_GAP win% worse).
    The gap is measured in win%, so in an already winning position, where almost everything
    leads to a win, Great is not awarded. We also filter out "self-evident" finds: the
    obligatory recapture of a piece that just moved, and positions with virtually no choice."""
    if only_one_move or second_score is None or not played_is_best:
        return False
    if board_before.legal_moves.count() < 3:
        return False   # hardly anything to choose from — that's necessity, not a find
    if move.to_square == opponent_last_to and board_before.is_capture(move):
        return False   # recapturing/taking a piece that just arrived here — obvious
    return (_win_pct(best_score) - _win_pct(second_score)) >= GREAT_GAP


def is_miss(win_drop, best_score):
    """Miss — a missed chance to punish the opponent: before the move we had a solid
    advantage (win chance >= MISS_MIN_WIN, often an outright mate), and the move gave up
    a noticeable part of it. Call only when the opponent's previous move was itself a
    mistake — otherwise it's not a "missed opportunity" but an ordinary mistake."""
    return _win_pct(best_score) >= MISS_MIN_WIN and win_drop >= MISS_MIN_DROP


def verdict_for(win_drop, is_mate, played_is_best, brilliant, great, miss,
                is_book, only_one_move):
    """Verdict for the move in chess.com terms. The order of checks matters: special labels
    (mate / sacrifice / only move / theory) override the regular quality scale."""
    if is_mate:
        return "Best"          # mate delivered — the game is over, the move is by definition best
    if brilliant:
        return "Brilliant"     # a strong move with a sound material sacrifice
    if great:
        return "Great"         # the only good move in the position
    if is_book:
        return "Book"          # opening theory
    if only_one_move:
        return "Forced"        # there was no choice
    if miss:
        return "Miss"          # failed to punish the opponent's mistake
    if played_is_best:
        return "Best"          # exactly the move the engine plays
    if win_drop < WD_EXCELLENT:
        return "Excellent"
    if win_drop < WD_GOOD:
        return "Good"
    if win_drop < WD_INACCURACY:
        return "Inaccuracy"
    if win_drop < WD_MISTAKE:
        return "Mistake"
    return "Blunder"


# ====================== GAME ANALYSIS ======================
def parse_pgn(pgn_text):
    """Extracts metadata, clocks, and the move list from raw PGN.
    Returns a dict, or None if the PGN could not be parsed."""
    raw_pgn = (pgn_text or "").strip()

    def find_header(tag, default):
        match = re.search(r'\[{}\s+"([^"]+)"\]'.format(tag), raw_pgn)
        return match.group(1) if match else default

    meta = {
        "white": find_header("White", "White Player"),
        "black": find_header("Black", "Black Player"),
        "white_elo": find_header("WhiteElo", "?"),
        "black_elo": find_header("BlackElo", "?"),
        "time_control": find_header("TimeControl", "-"),
    }
    clocks = re.findall(r'%clk\s+([0-9:.]+)', raw_pgn)

    clean_pgn = re.sub(r'\{[^}]*\}', '', raw_pgn)
    clean_pgn = re.sub(r'\[%clk[^\]]*\]', '', clean_pgn)

    game = chess.pgn.read_game(io.StringIO(clean_pgn))
    if not game:
        return None

    start_fen = game.board().fen()
    moves = list(game.mainline_moves())
    return {
        "meta": meta,
        "clocks": clocks,
        "start_fen": start_fen,
        "moves": moves,
    }


def _majors_minors(board):
    """Number of major and minor pieces on the board (both sides, excluding pawns and kings)."""
    return sum(len(board.pieces(pt, color))
               for pt in (chess.QUEEN, chess.ROOK, chess.BISHOP, chess.KNIGHT)
               for color in (chess.WHITE, chess.BLACK))


def _backrank_sparse(board):
    """Development complete: one side has fewer than BACKRANK_SPARSE pieces left on its
    home rank (king included) — a sign of transition into the middlegame (lichess heuristic)."""
    for color, rank_bb in ((chess.WHITE, chess.BB_RANK_1), (chess.BLACK, chess.BB_RANK_8)):
        cnt = sum(1 for sq in chess.SquareSet(rank_bb)
                  if (pc := board.piece_at(sq)) and pc.color == color)
        if cnt < BACKRANK_SPARSE:
            return True
    return False


def _phase_of(board, was_in_book, midgame_started, endgame_started):
    """Game phase by rules close to chess.com's.
    Opening — while the game follows theory. Our database is smaller than chess.com's,
    so leaving the book doesn't by itself end the opening: the middlegame begins when
    out of theory AND development is effectively complete (trades: pieces <= MIDGAME_PIECES,
    or a home rank has emptied, or OPENING_MAX_MOVES full moves have passed).
    Endgame — pieces remaining <= ENDGAME_PIECES (roughly 2-3 per side).
    Phases never roll back. Returns (phase, midgame_started, endgame_started)."""
    if not endgame_started:
        endgame_started = _majors_minors(board) <= ENDGAME_PIECES
    if endgame_started:
        return "endgame", True, True
    if not midgame_started and not was_in_book:
        midgame_started = (
            _majors_minors(board) <= MIDGAME_PIECES
            or _backrank_sparse(board)
            or board.fullmove_number > OPENING_MAX_MOVES
        )
    return ("middlegame" if midgame_started else "opening"), midgame_started, False


def _read_multipv(infos):
    """Extracts from the engine response (MultiPV=2) the best move's score, the second-best
    score (None if there are no alternatives), and the principal variation. Both scores are
    from the mover's point of view."""
    best_score = infos[0]["score"].relative.score(mate_score=10000)
    pv = list(infos[0].get("pv") or [])
    second = infos[1]["score"].relative.score(mate_score=10000) if len(infos) > 1 else None
    return best_score, second, pv


def _run_analysis(start_fen, moves, clocks):
    """Synchronous game analysis on one engine from the pool (runs in a thread).
    Optimization: each position is evaluated once — a move's score = minus the score
    of the position after it (which is also the best-move score for the next half-move).
    We run with MultiPV=2: the second-best move is needed for the Great verdict."""
    engine = _engine_pool.get()
    healthy = True
    try:
        board = chess.Board(start_fen)
        limit = chess.engine.Limit(depth=ENGINE_DEPTH)

        prev_best, prev_second, prev_pv = _read_multipv(engine.analyse(board, limit, multipv=2))
        opp_prev_drop = None   # how much the opponent's win chance dropped on their last move
        in_theory = True       # the game is still following the opening book (Opening phase)
        midgame_started = False
        endgame_started = False

        results = []
        for idx, move in enumerate(moves):
            if move not in board.legal_moves:
                break

            mover_is_white = board.turn == chess.WHITE
            only_one_move = board.legal_moves.count() == 1

            best_score = prev_best
            second_score = prev_second
            best_move_obj = prev_pv[0] if prev_pv else move
            played_is_best = bool(prev_pv) and prev_pv[0] == move
            best_move_san = board.san(best_move_obj)
            best_move_from = chess.square_name(best_move_obj.from_square)
            best_move_to = chess.square_name(best_move_obj.to_square)

            actual_move_san = board.san(move)
            target_square = chess.square_name(move.to_square)

            board_before = board.copy(stack=False)
            board.push(move)
            is_mate = board.is_checkmate()

            if board.legal_moves.count() == 0:
                next_best = -10000 if is_mate else 0   # mate by the mover / stalemate
                next_second, next_pv = None, []
                game_over = True
                mate_rel = 0 if is_mate else None      # mate already on the board / stalemate
            else:
                nxt = engine.analyse(board, limit, multipv=2)
                next_best, next_second, next_pv = _read_multipv(nxt)
                game_over = False
                mate_rel = nxt[0]["score"].relative.mate()  # moves to mate from the mover's side

            # moves to forced mate from WHITE's point of view (sign = who mates),
            # None — no mate within the search depth. board.turn — who moves AFTER the move.
            if mate_rel is None or mate_rel == 0:
                mate_white = mate_rel
            else:
                mate_white = mate_rel if board.turn == chess.WHITE else -mate_rel

            actual_score = -next_best       # from the mover's point of view
            loss = max(0, best_score - actual_score)
            win_drop = max(0.0, _win_pct(best_score) - _win_pct(actual_score))

            if loss <= BEST_EQUAL_CP:
                played_is_best = True   # not from the PV, but just as strong by the engine's eval

            brilliant = great = miss = False
            if not is_mate:
                if win_drop < WD_EXCELLENT:
                    # the move is strong — check whether it's brilliant or the only one
                    brilliant = is_brilliant(board, move, best_score, actual_score)
                    great = not brilliant and is_great(
                        board_before, move, played_is_best, best_score, second_score,
                        only_one_move, moves[idx - 1].to_square if idx else None)
                elif opp_prev_drop is not None and opp_prev_drop >= WD_INACCURACY:
                    # the opponent just made a mistake — check whether we missed the chance to punish it
                    miss = is_miss(win_drop, best_score)

            fen_after_key = " ".join(board.fen().split()[:4])
            is_book = fen_after_key in BOOK_FENS

            in_theory = in_theory and is_book   # a move outside the book — theory is over (for good)
            phase, midgame_started, endgame_started = _phase_of(
                board, in_theory, midgame_started, endgame_started)

            verdict = verdict_for(win_drop, is_mate, played_is_best, brilliant, great,
                                  miss, is_book, only_one_move)
            score_white = actual_score if mover_is_white else -actual_score

            results.append({
                "move": actual_move_san,
                "best_move": best_move_san,
                "best_move_from": best_move_from,
                "best_move_to": best_move_to,
                "score": round(score_white / 100, 2),
                "mate": mate_white,           # moves to mate (from White's side) or None
                "verdict": verdict,
                "fen": board.fen(),
                "square": target_square,
                "clk": clocks[idx] if idx < len(clocks) else None,
                "loss": int(loss),            # loss in centipawns (for accuracy/summary)
                "win_drop": round(win_drop, 2),
                "phase": phase,               # opening / middlegame / endgame
            })

            prev_best, prev_second, prev_pv = next_best, next_second, next_pv
            opp_prev_drop = win_drop
            if game_over:
                break
        return results
    except Exception:
        healthy = False
        raise
    finally:
        if healthy:
            _engine_pool.put(engine)
        else:
            try:
                engine.quit()
            except Exception:
                pass
            try:
                _engine_pool.put(_make_engine())
            except Exception:
                pass


async def analyze_game_async(start_fen, moves, clocks):
    """Async wrapper: limits concurrency with a semaphore and offloads the heavy
    computation to a thread so as not to block the event loop."""
    if _engine_sem is None:
        raise RuntimeError("Engine is not initialized")
    async with _engine_sem:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _run_analysis, start_fen, moves, clocks)


# ====================== SINGLE-POSITION ANALYSIS (interactive board) ======================
@contextmanager
def _borrow_engine():
    """Borrows an engine from the pool for a single operation and always returns it.
    If the engine "crashed" (exception) — closes it and spins up a new one instead."""
    engine = _engine_pool.get()
    healthy = True
    try:
        yield engine
    except Exception:
        healthy = False
        raise
    finally:
        if healthy:
            _engine_pool.put(engine)
        else:
            try:
                engine.quit()
            except Exception:
                pass
            try:
                _engine_pool.put(_make_engine())
            except Exception:
                pass


def _run_position(fen, depth):
    """Evaluates a single position: returns the score (from White's side, in pawns),
    the best move, and the start of the best line. Used for interactive analysis,
    when the user makes moves on the board themselves."""
    board = chess.Board(fen)
    if board.is_game_over():
        return {
            "fen": fen,
            "score": 0.0,
            "cp": 0,
            "mate": None,
            "best_move": None,
            "best_move_from": None,
            "best_move_to": None,
            "pv": [],
            "game_over": True,
            "checkmate": board.is_checkmate(),
        }

    with _borrow_engine() as engine:
        info = engine.analyse(board, chess.engine.Limit(depth=depth or ENGINE_DEPTH))

    rel = info["score"].relative
    cp = rel.score(mate_score=10000)
    white_cp = cp if board.turn == chess.WHITE else -cp
    mate_rel = rel.mate()  # moves to mate from the mover's side (None — no mate)
    mate_white = mate_rel if (mate_rel is None or board.turn == chess.WHITE) else -mate_rel
    pv = list(info.get("pv") or [])
    best = pv[0] if pv else None

    pv_san, probe = [], board.copy()
    for mv in pv[:6]:
        if mv not in probe.legal_moves:
            break
        pv_san.append(probe.san(mv))
        probe.push(mv)

    return {
        "fen": fen,
        "score": round(white_cp / 100, 2),
        "cp": int(white_cp),  # the same score in centipawns (for manual analysis)
        "mate": mate_white,  # None or a signed number of moves to mate (from White's side)
        "best_move": board.san(best) if best else None,
        "best_move_from": chess.square_name(best.from_square) if best else None,
        "best_move_to": chess.square_name(best.to_square) if best else None,
        "pv": pv_san,
        "game_over": False,
        "checkmate": False,
    }


def _run_move_eval(prev_fen, move_uci, prev_win_drop=None, depth=None):
    """Evaluates ONE played move (interactive board): the same classification as in
    full-game analysis — Brilliant/Great/Best/…/Miss. Analyses the position before
    and after the move (both MultiPV=2; the engine hash is warm, so re-evaluating is cheap).
    prev_win_drop — the win% loss of the line's previous move (for Miss detection)."""
    board = chess.Board(prev_fen)
    move = chess.Move.from_uci(move_uci)
    if move not in board.legal_moves:
        raise ValueError("illegal move")

    limit = chess.engine.Limit(depth=depth or ENGINE_DEPTH)
    only_one_move = board.legal_moves.count() == 1
    mover_is_white = board.turn == chess.WHITE

    with _borrow_engine() as engine:
        best_score, second_score, pv = _read_multipv(engine.analyse(board, limit, multipv=2))
        played_is_best = bool(pv) and pv[0] == move

        board_before = board.copy(stack=False)
        board.push(move)
        is_mate = board.is_checkmate()

        if board.legal_moves.count() == 0:
            next_best, next_pv = (-10000 if is_mate else 0), []
            game_over = True
            mate_rel = 0 if is_mate else None
        else:
            nxt = engine.analyse(board, limit, multipv=2)
            next_best, _, next_pv = _read_multipv(nxt)
            game_over = False
            mate_rel = nxt[0]["score"].relative.mate()

    actual_score = -next_best
    loss = max(0, best_score - actual_score)
    win_drop = max(0.0, _win_pct(best_score) - _win_pct(actual_score))
    if loss <= BEST_EQUAL_CP:
        played_is_best = True

    brilliant = great = miss = False
    if not is_mate:
        if win_drop < WD_EXCELLENT:
            brilliant = is_brilliant(board, move, best_score, actual_score)
            great = not brilliant and is_great(
                board_before, move, played_is_best, best_score, second_score,
                only_one_move, None)
        elif prev_win_drop is not None and prev_win_drop >= WD_INACCURACY:
            miss = is_miss(win_drop, best_score)

    is_book = " ".join(board.fen().split()[:4]) in BOOK_FENS
    verdict = verdict_for(win_drop, is_mate, played_is_best, brilliant, great,
                          miss, is_book, only_one_move)

    # score and best reply in the position AFTER the move (for the arrow and continuing the line)
    white_cp = actual_score if mover_is_white else -actual_score
    if mate_rel is None or mate_rel == 0:
        mate_white = mate_rel
    else:
        mate_white = mate_rel if board.turn == chess.WHITE else -mate_rel
    best_reply = next_pv[0] if next_pv else None

    pv_san, probe = [], board.copy(stack=False)
    for mv in next_pv[:6]:
        if mv not in probe.legal_moves:
            break
        pv_san.append(probe.san(mv))
        probe.push(mv)

    return {
        "fen": board.fen(),
        "score": round(white_cp / 100, 2),
        "cp": int(white_cp),
        "mate": mate_white,
        "best_move": board.san(best_reply) if best_reply else None,
        "best_move_from": chess.square_name(best_reply.from_square) if best_reply else None,
        "best_move_to": chess.square_name(best_reply.to_square) if best_reply else None,
        "pv": pv_san,
        "game_over": game_over,
        "checkmate": is_mate,
        "verdict": verdict,
        "win_drop": round(win_drop, 2),
    }


async def analyze_move_async(prev_fen, move_uci, prev_win_drop=None, depth=None):
    """Async wrapper for evaluating one played move (interactive board)."""
    if _engine_sem is None:
        raise RuntimeError("Engine is not initialized")
    async with _engine_sem:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, _run_move_eval, prev_fen, move_uci, prev_win_drop, depth)


async def analyze_position_async(fen, depth=None):
    """Async wrapper for analyzing a single position (interactive board)."""
    if _engine_sem is None:
        raise RuntimeError("Engine is not initialized")
    async with _engine_sem:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _run_position, fen, depth)


# ====================== PLAYER SUMMARY ======================
def _accuracy_from_drop(mean_win_drop):
    """Accuracy 0..100 from the mean drop in win chance (Lichess formula)."""
    acc = 103.1668 * math.exp(-0.04354 * mean_win_drop) - 3.1669
    return max(0.0, min(100.0, acc))


def summarize(analysis, hero_is_white):
    """Builds a summary over ONLY the moves of the player of the given color:
    accuracy %, verdict breakdown, and the worst move."""
    drops = []
    counts = {}
    total_moves = 0
    worst = None  # (loss, move_number, san, best_move)
    for i, item in enumerate(analysis):
        move_is_white = (i % 2 == 0)
        if move_is_white != hero_is_white:
            continue
        total_moves += 1
        counts[item["verdict"]] = counts.get(item["verdict"], 0) + 1
        # a book move is exact adherence to theory, count it as perfect (drop 0):
        # a player who played the opening by the book shouldn't lose accuracy
        drops.append(0.0 if item["verdict"] == "Book" else item.get("win_drop", 0.0))
        move_no = i // 2 + 1
        if item["verdict"] in ("Blunder", "Mistake", "Miss"):
            cand = (item.get("loss", 0), move_no, item["move"], item["best_move"])
            if worst is None or cand[0] > worst[0]:
                worst = cand

    accuracy = _accuracy_from_drop(sum(drops) / len(drops)) if drops else 100.0
    return {
        "accuracy": round(accuracy, 1),
        "moves": total_moves,
        "counts": counts,
        "worst": worst,  # None or (loss, move_no, san, best_move)
    }
