"""Builds the openings database: takes lichess's open ECO TSV tables
(github.com/lichess-org/chess-openings) and turns them into openings.json —
a map of {position_EPD: "ECO Name"}. Local a.tsv..e.tsv in the project folder
take priority; if absent, the files are downloaded. Run once:

    python build_openings.py

Important: the database stores not only the final position of each line but all
intermediate ones too (any prefix of a theoretical line is also theory). Named
positions get "ECO Name", intermediate ones get an empty string (unnamed book).
The result is read by analysis.py at startup; the script is not needed at runtime."""
import json
import os

import chess
import chess.pgn

FILES = ["a.tsv", "b.tsv", "c.tsv", "d.tsv", "e.tsv"]
BASE = "https://raw.githubusercontent.com/lichess-org/chess-openings/master/"
OUT = "openings.json"


def _read_tsv(fname):
    """Local file if it's next to the script; otherwise download from github."""
    if os.path.exists(fname):
        with open(fname, encoding="utf-8") as f:
            return f.read()
    import httpx
    with httpx.Client(timeout=60, follow_redirects=True) as c:
        r = c.get(BASE + fname)
        r.raise_for_status()
        return r.text


def epds_for(pgn_moves):
    """Plays through a SAN sequence and returns the keys of all positions in the
    line (EPD = first 4 FEN fields). The last element is the final position."""
    board = chess.Board()
    epds = []
    for token in pgn_moves.split():
        if token.endswith(".") or token[0].isdigit() and "." in token:
            continue  # move number like "1." / "12."
        board.push_san(token)
        epds.append(board.epd())
    return epds


def main():
    lines = []          # [(eco, name, [epd, ...])]
    for fname in FILES:
        text = _read_tsv(fname)
        for line in text.splitlines()[1:]:  # skip the eco	name	pgn header
            parts = line.split("	")
            if len(parts) < 3:
                continue
            eco, name, pgn = parts[0].strip(), parts[1].strip(), parts[2].strip()
            try:
                epds = epds_for(pgn)
            except Exception:
                continue
            if epds:
                lines.append((eco, name, epds))
        print(f"  {fname}: lines collected {len(lines)}")

    openings = {}
    # named final positions first — their names must not be overwritten by prefixes
    for eco, name, epds in lines:
        openings[epds[-1]] = f"{eco} {name}"
    named = len(openings)
    # then intermediate positions of all lines — unnamed theory
    for _, _, epds in lines:
        for key in epds[:-1]:
            openings.setdefault(key, "")

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(openings, f, ensure_ascii=False)
    print(f"Finished: {len(openings)} positions ({named} named) -> {OUT}")


if __name__ == "__main__":
    main()
