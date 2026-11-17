#!/usr/bin/env python3
"""
Backrank - a local chess game-review, analysis and play server.

    python server.py            (then open http://127.0.0.1:8765)

Everything runs on this machine: the browser UI talks to this server, and this
server talks to Stockfish over UCI.

Only dependency: the `chess` package (python-chess).
"""
import argparse
import atexit
import datetime
import hashlib
import http.client
import io
import json
import math
import os
import queue
import random
import re
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import chess
    import chess.engine
    import chess.pgn
except ImportError:
    sys.exit("Missing dependency. Run:  pip install chess")

import subprocess

FROZEN = getattr(sys, "frozen", False)
# RES: bundled read-only files. ROOT: the folder the app lives in (engine/, data/).
RES = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
ROOT = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
STATIC = RES / "static"
DATA = Path(os.environ.get("BACKRANK_DATA") or ROOT / "data")
# Keep Stockfish from flashing a console window when we run as a windowed .exe.
POPEN_ARGS = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def say(*a):
    if sys.stdout:  # None in a windowed .exe
        print(*a, flush=True)
CPUS = os.cpu_count() or 2

# --------------------------------------------------------------------------
# Engine handling
# --------------------------------------------------------------------------


def find_engine(explicit=None):
    """Locate a Stockfish binary: --engine, $STOCKFISH_PATH, ./engine, ./, PATH."""
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    if os.environ.get("STOCKFISH_PATH"):
        candidates.append(Path(os.environ["STOCKFISH_PATH"]))
    for folder in (ROOT / "engine", ROOT):
        if folder.is_dir():
            for p in sorted(folder.rglob("*") if folder != ROOT else folder.glob("*")):
                if not p.is_file() or "stockfish" not in p.name.lower() or p == Path(sys.executable):
                    continue
                if os.name == "nt":
                    if p.suffix.lower() == ".exe":
                        candidates.append(p)
                elif p.suffix == "" and os.access(p, os.X_OK):
                    candidates.append(p)
    which = shutil.which("stockfish")
    if which:
        candidates.append(Path(which))
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


class Engine:
    """One UCI engine process, started lazily, used by one caller at a time."""

    def __init__(self, threads, hash_mb):
        self.threads, self.hash_mb = threads, hash_mb
        self.lock = threading.Lock()
        self.proc = None

    def _start(self):
        if not ENGINE_PATH:
            raise RuntimeError("No Stockfish binary found. Put stockfish in the 'engine' folder.")
        self.proc = chess.engine.SimpleEngine.popen_uci(ENGINE_PATH, **POPEN_ARGS)
        opts = {}
        if "Threads" in self.proc.options:
            opts["Threads"] = self.threads
        if "Hash" in self.proc.options:
            opts["Hash"] = self.hash_mb
        self.proc.configure(opts)

    def run(self, fn):
        """Call fn(engine) while holding the lock; restart once if the engine died."""
        with self.lock:
            for attempt in (0, 1):
                try:
                    if self.proc is None:
                        self._start()
                    return fn(self.proc)
                except (chess.engine.EngineTerminatedError, BrokenPipeError):
                    self.proc = None
                    if attempt:
                        raise

    def close(self):
        if self.proc:
            try:
                self.proc.quit()
            except Exception:
                pass
            self.proc = None


class EnginePool:
    """Single-threaded engines for game reviews, one position per engine at a time.

    A review is hundreds of small independent searches. Running them side by
    side on one core each is far faster than one search spread over every core.
    """

    def __init__(self, size, hash_mb):
        self.size = size
        self.engines = [Engine(threads=1, hash_mb=hash_mb) for _ in range(size)]
        self.free = queue.LifoQueue()
        for e in self.engines:
            self.free.put(e)

    def close(self):
        for e in self.engines:
            e.close()


ENGINE_PATH = None
POOL = EnginePool(size=max(1, min(32, CPUS - 2)), hash_mb=32)  # game reviews
LIVE = Engine(threads=max(1, min(4, CPUS // 2)), hash_mb=128)  # analysis board
PLAY = Engine(threads=1, hash_mb=64)                     # computer opponent


def close_engines():
    for e in (POOL, LIVE, PLAY):
        e.close()


atexit.register(close_engines)


def engine_name():
    if not ENGINE_PATH:
        return None
    try:
        return LIVE.run(lambda e: e.id.get("name", "UCI engine"))
    except Exception as ex:  # binary exists but will not run
        return None if isinstance(ex, RuntimeError) else f"(failed to start: {ex})"


# --------------------------------------------------------------------------
# Evaluation helpers
# --------------------------------------------------------------------------


def win_pct(cp):
    """Centipawns -> White's winning chances in percent (Lichess' published curve)."""
    return 50 + 50 * (2 / (1 + math.exp(-0.00368208 * cp)) - 1)


def ev_from_score(pov_score):
    s = pov_score.white()
    m = s.mate()
    if m is not None:
        up = m > 0
        return {"cp": None, "mate": m, "win": 100.0 if up else 0.0, "num": 1000 if up else -1000}
    cp = s.score()
    return {"cp": cp, "mate": None, "win": round(win_pct(cp), 2), "num": max(-1000, min(1000, cp))}


def terminal_eval(board):
    if board.is_checkmate():
        white_won = board.turn == chess.BLACK
        return {"cp": None, "mate": 0, "win": 100.0 if white_won else 0.0,
                "num": 1000 if white_won else -1000}
    return {"cp": 0, "mate": None, "win": 50.0, "num": 0}


def fmt_eval(e):
    if e["mate"] is not None:
        if e["mate"] == 0:
            return "checkmate"
        return ("M%d" % e["mate"]) if e["mate"] > 0 else ("-M%d" % -e["mate"])
    return "%+.2f" % (e["cp"] / 100)


def pv_to_san(board, pv, limit=8):
    b, out = board.copy(stack=False), []
    for mv in pv[:limit]:
        if mv not in b.legal_moves:
            break
        out.append(b.san(mv))
        b.push(mv)
    return out


def analyse(engine, board, limit, multipv=1):
    """Return a list of lines: [{eval, pv (uci), pv_san, depth}], best first."""
    if board.is_game_over():
        return [{"eval": terminal_eval(board), "pv": [], "pv_san": [], "depth": 0}]
    multipv = max(1, min(multipv, board.legal_moves.count()))
    infos = engine.run(lambda e: e.analyse(board, limit, multipv=multipv))
    if isinstance(infos, dict):
        infos = [infos]
    lines = []
    for info in infos:
        if "score" not in info:
            continue
        pv = info.get("pv", [])
        lines.append({"eval": ev_from_score(info["score"]),
                      "pv": [m.uci() for m in pv[:12]],
                      "pv_san": pv_to_san(board, pv),
                      "depth": info.get("depth", 0)})
    return lines


# --------------------------------------------------------------------------
# Game review
# --------------------------------------------------------------------------

try:
    OPENINGS = json.loads((RES / "openings.json").read_text(encoding="utf-8"))
except Exception:
    OPENINGS = {}

VAL = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 100}
PIECE_NAME = {chess.PAWN: "pawn", chess.KNIGHT: "knight", chess.BISHOP: "bishop",
              chess.ROOK: "rook", chess.QUEEN: "queen", chess.KING: "king"}
DEPTH_TIME_CAP = [(10, 1.0), (14, 2.5), (18, 6.0), (99, 15.0)]  # seconds per position


def see_capture(board, sq):
    """Material the side to move wins by starting an exchange on `sq` (0 if none)."""
    caps = [m for m in board.legal_moves if m.to_square == sq and board.piece_type_at(sq)]
    if not caps:
        return 0
    mv = min(caps, key=lambda m: VAL[board.piece_type_at(m.from_square)])
    victim = VAL[board.piece_type_at(sq)]
    board.push(mv)
    gain = victim - see_capture(board, sq)
    board.pop()
    return max(0, gain)


def move_accuracy(before, after):
    """Lichess' accuracy formula. Inputs: mover's win% before and after the move."""
    if after >= before:
        return 100.0
    raw = 103.1668100711649 * math.exp(-0.04354415386753951 * (before - after)) - 3.166924740191411
    return max(0.0, min(100.0, raw + 1))


def game_accuracy(wins, start_white):
    """Lichess' game accuracy: mean of a volatility-weighted mean and a harmonic mean."""
    n = len(wins) - 1
    if n <= 0:
        return {"white": None, "black": None}
    size = max(2, min(8, n // 10))
    windows = [wins[:size]] * max(0, min(size, len(wins)) - 2)
    windows += [wins[i:i + size] for i in range(0, len(wins) - size + 1)]

    def stdev(xs):
        mean = sum(xs) / len(xs)
        return math.sqrt(sum((x - mean) ** 2 for x in xs) / len(xs))

    weights = [max(0.5, min(12, stdev(w))) for w in windows]
    per = {True: [], False: []}
    for i in range(n):
        white = (i % 2 == 0) == start_white
        prev, nxt = wins[i], wins[i + 1]
        acc = move_accuracy(prev, nxt) if white else move_accuracy(100 - prev, 100 - nxt)
        per[white].append((acc, weights[i] if i < len(weights) else 1.0))
    out = {}
    for white, key in ((True, "white"), (False, "black")):
        rows = per[white]
        if not rows:
            out[key] = None
            continue
        weighted = sum(a * w for a, w in rows) / sum(w for _, w in rows)
        harmonic = len(rows) / sum(1 / max(a, 1.0) for a, _ in rows)
        out[key] = round((weighted + harmonic) / 2, 1)
    return out


def estimate_rating(acpl):
    """Rough rating guess from average centipawn loss. A heuristic, not a measurement."""
    if acpl is None:
        return None
    return int(round(max(100, min(3200, 3100 * math.exp(-0.0105 * acpl))) / 50) * 50)


def describe(cls, m, mover_before, mover_after, second, hung, opening):
    """One plain-English coach note for a move."""
    san, best = m["san"], m["best_san"]
    eb, ea = fmt_eval(m["eval_before"]), fmt_eval(m["eval_after"])
    notes = []
    if cls == "book":
        return f"Book move. {opening[1]} ({opening[0]})." if opening else "Book move."
    if cls == "brilliant":
        notes.append(f"Brilliant! {san} offers material and it is still the strongest idea in the position ({ea}).")
    elif cls == "great":
        keeps = "keeps the advantage" if mover_before >= 65 else "holds the position together"
        txt = f"Great find. {san} was the only move that {keeps}"
        if second:
            txt += f"; the next-best try, {second['pv_san'][0]}, drops to {fmt_eval(second['eval'])}"
        notes.append(txt + ".")
    elif cls == "best":
        notes.append(f"{san} is the engine's top choice ({ea}).")
    elif cls == "excellent":
        notes.append(f"Excellent. Practically as good as the engine's pick, {best}.")
    elif cls == "good":
        notes.append(f"A solid move. {best} was a touch more precise.")
    else:
        head = {"inaccuracy": "Inaccuracy.", "mistake": "Mistake.", "miss": "Missed chance.",
                "blunder": "Blunder."}[cls]
        notes.append(f"{head} {best} was stronger; the evaluation goes from {eb} to {ea}.")
        if cls == "inaccuracy":
            pass
        elif mover_before >= 65 and mover_after < 35:
            notes.append("That turns a winning position into a losing one.")
        elif mover_before >= 65 and mover_after < 65:
            notes.append("That lets most of the advantage slip.")
        elif mover_before >= 35 and mover_after < 35:
            notes.append("The position is now difficult to hold.")
        mb, ma = m["eval_before"]["mate"], m["eval_after"]["mate"]
        white = m["color"] == "white"
        if mb and ((mb > 0) == white) and not (ma is not None and (ma > 0) == white and ma != 0):
            notes.append(f"There was a forced mate in {abs(mb)} starting with {best}.")
        if ma and ((ma > 0) != white):
            notes.append(f"It allows a forced mate in {abs(ma)}.")
        elif hung:
            notes.append(f"It leaves the {hung} hanging.")
        if m["refutation"]:
            notes.append(f"The punishment starts with {m['refutation'][0]}.")
    return " ".join(notes)


def phase_of(board):
    if board.fullmove_number <= 10:
        return "opening"
    pieces = [p.piece_type for p in board.piece_map().values() if p.piece_type not in (chess.PAWN, chess.KING)]
    total = sum(VAL[p] for p in pieces)
    return "endgame" if total <= 20 or (chess.QUEEN not in pieces and total <= 26) else "middlegame"


def capture_gain(board, mv):
    """Material won by playing capture `mv`, after the best recapture sequence."""
    if not board.is_capture(mv):
        return 0
    victim = 1 if board.is_en_passant(mv) else VAL[board.piece_type_at(mv.to_square)]
    board.push(mv)
    gain = victim - see_capture(board, mv.to_square)
    board.pop()
    return gain


def mistake_tags(b, after, mv, m, white, wb, wa, given_up, reply_pv, clock):
    """Label what kind of error a move was. A move can carry several tags."""
    tags = []
    mb, ma = m["eval_before"]["mate"], m["eval_after"]["mate"]
    had_mate = bool(mb) and (mb > 0) == white
    still_mate = ma is not None and (ma == 0 or (ma > 0) == white)
    best = chess.Move.from_uci(m["best_uci"])
    if had_mate and not still_mate:
        tags.append("missed_mate")
    if ma and (ma > 0) != white:
        tags.append("allowed_mate")
    reply = chess.Move.from_uci(reply_pv[0]) if reply_pv else None
    if given_up >= 2:
        tags.append("hung_moved")
    elif reply and reply in after.legal_moves and capture_gain(after, reply) >= 2:
        tags.append("left_hanging")
    if "missed_mate" not in tags and best != mv and best in b.legal_moves:
        if capture_gain(b, best) >= 2:
            tags.append("missed_capture")
        elif b.gives_check(best) and not tags:
            tags.append("missed_check")
    if b.is_capture(mv) and not tags:
        tags.append("bad_capture")
    if not tags:  # nothing tactical: describe the quiet error by what was played instead
        piece = b.piece_type_at(mv.from_square)
        if best != mv and best in b.legal_moves and b.is_castling(best):
            tags.append("delayed_castling")
        elif best != mv and best in b.legal_moves and b.is_capture(best):
            tags.append("skipped_capture")
        elif piece == chess.KING and not b.is_castling(mv) and m["phase"] != "endgame":
            tags.append("king_move")
        elif piece == chess.QUEEN and m["phase"] == "opening":
            tags.append("early_queen")
        elif piece == chess.PAWN:
            tags.append("pawn_push")
        else:
            tags.append("positional")
    if wb >= 70 and wa < 50:
        tags.append("threw_win")
    if clock is not None and clock < 30:
        tags.append("low_time")
    return tags


EVAL_CACHE = {}   # (position, depth) -> lines; openings repeat from game to game


def evaluate_positions(boards, depth, limit, job):
    """Engine lines (multipv 2) for every board, spread across the engine pool."""
    out, todo, lock, errors = [None] * len(boards), iter(range(len(boards))), threading.Lock(), []

    def worker():
        engine = None
        try:
            while not errors and not job.get("cancel"):
                with lock:
                    i = next(todo, None)
                if i is None:
                    break
                b = boards[i]
                key = (b.epd(), depth)
                lines = EVAL_CACHE.get(key)
                if lines is None:
                    engine = engine or POOL.free.get()
                    lines = analyse(engine, b, limit, multipv=2)
                    if lines and lines[0]["depth"] >= depth and len(EVAL_CACHE) < 200_000:
                        EVAL_CACHE[key] = lines
                out[i] = lines
                with lock:
                    job["done"] += 1
        except Exception as ex:
            errors.append(ex)
        finally:
            if engine:
                POOL.free.put(engine)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(min(POOL.size, len(boards)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if errors:
        raise errors[0]
    if job.get("cancel"):
        raise InterruptedError("cancelled")
    return out


def review_game(pgn_text, depth, job):
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        raise ValueError("Could not read that PGN.")
    nodes = list(game.mainline())
    moves = [n.move for n in nodes]
    if not moves:
        raise ValueError("That PGN has no moves.")
    clocks = [n.clock() for n in nodes]
    board = game.board()
    start_fen, start_white = board.fen(), board.turn == chess.WHITE
    cap = next(t for d, t in DEPTH_TIME_CAP if depth <= d)
    limit = chess.engine.Limit(depth=depth, time=cap)

    # Pass 1: evaluate every position (before each move, plus the final one).
    boards = [board.copy()]
    for mv in moves:
        board.push(mv)
        boards.append(board.copy())
    job["done"], job["total"] = 0, len(boards)
    lines = evaluate_positions(boards, depth, limit, job)
    if any(not ln for ln in lines):
        raise RuntimeError("The engine returned no evaluation for a position.")

    # Opening: last position found in the book; every move up to it counts as book.
    opening, book_ply = None, 0
    for i, b in enumerate(boards[1:], start=1):
        hit = OPENINGS.get(b.epd())
        if hit:
            opening, book_ply = hit, i

    # Pass 2: grade each move.
    out, prev_cls = [], None
    for i, mv in enumerate(moves):
        b, after = boards[i], boards[i + 1]
        white = b.turn == chess.WHITE
        best_line = lines[i][0]
        second = lines[i][1] if len(lines[i]) > 1 else None
        e_before, e_after = best_line["eval"], lines[i + 1][0]["eval"]
        best_uci = best_line["pv"][0] if best_line["pv"] else mv.uci()
        played_best = mv.uci() == best_uci
        if played_best:  # same move: trust the deeper look from the next position
            e_before = dict(e_before)
        mover = lambda e: e["win"] if white else 100 - e["win"]
        wb, wa = mover(e_before), mover(e_after)
        loss = 0.0 if played_best else max(0.0, wb - wa)
        cp_loss = 0 if played_best else max(0, (e_before["num"] - e_after["num"]) * (1 if white else -1))

        piece = b.piece_type_at(mv.from_square)
        captured = VAL.get(b.piece_type_at(mv.to_square), 0)
        given_up = see_capture(after, mv.to_square) - captured if piece != chess.PAWN else 0
        recapture = i > 0 and b.is_capture(mv) and moves[i - 1].to_square == mv.to_square

        if i < book_ply:
            cls = "book"
        elif played_best or loss <= 2:
            cls = "best" if played_best else "excellent"
            second_win = mover(second["eval"]) if second else None
            mates = e_after["mate"] == 0
            if (given_up >= 2 and wa >= 45 and not mv.promotion
                    and (second_win is None or second_win < 97)):
                cls = "brilliant"
            elif (played_best and second_win is not None and wb - second_win >= 15
                  and wa >= 25 and second_win < 90 and not recapture and not mates):
                cls = "great"
        elif loss <= 5:
            cls = "good"
        elif loss <= 10:
            cls = "inaccuracy"
        else:
            mate_before = e_before["mate"]
            had_mate = bool(mate_before) and (mate_before > 0) == white
            gifted = prev_cls in ("mistake", "blunder", "miss") and wb >= 60
            if (gifted or had_mate) and wa >= 35:
                cls = "miss"
            else:
                cls = "mistake" if loss <= 20 else "blunder"

        m = {
            "ply": i + 1, "color": "white" if white else "black",
            "number": b.fullmove_number, "san": b.san(mv), "uci": mv.uci(),
            "fen_before": b.fen(), "fen_after": after.fen(),
            "eval_before": e_before, "eval_after": e_after,
            "best_uci": best_uci,
            "best_san": best_line["pv_san"][0] if best_line["pv_san"] else b.san(mv),
            "best_line": best_line["pv_san"],
            "refutation": lines[i + 1][0]["pv_san"][:6],
            "loss": round(loss, 1), "cp_loss": cp_loss,
            "accuracy": round(move_accuracy(wb, wa) if not played_best else 100.0, 1),
            "class": cls, "piece": PIECE_NAME[piece], "phase": phase_of(b), "clock": clocks[i],
        }
        m["tags"] = mistake_tags(b, after, mv, m, white, wb, wa, given_up,
                                 lines[i + 1][0]["pv"], clocks[i]) if cls in ERROR_CLASSES + ("inaccuracy",) else []
        hung = PIECE_NAME[piece] if (given_up >= 2 and cls in ("mistake", "blunder")) else None
        m["comment"] = describe(cls, m, wb, wa, second, hung, opening)
        out.append(m)
        prev_cls = cls

    wins = [lines[0][0]["eval"]["win"]] + [m["eval_after"]["win"] for m in out]
    acc = game_accuracy(wins, start_white)
    players = {}
    for color in ("white", "black"):
        mine = [m for m in out if m["color"] == color]
        graded = [m for m in mine if m["class"] != "book"] or mine
        acpl = round(sum(m["cp_loss"] for m in graded) / len(graded)) if graded else None
        counts = {}
        for m in mine:
            counts[m["class"]] = counts.get(m["class"], 0) + 1
        players[color] = {
            "name": game.headers.get(color.capitalize(), color.capitalize()),
            "rating": game.headers.get(color.capitalize() + "Elo", ""),
            "accuracy": acc[color], "acpl": acpl,
            "estimated_rating": estimate_rating(acpl), "counts": counts,
        }
    return {
        "headers": dict(game.headers), "start_fen": start_fen,
        "start_eval": lines[0][0]["eval"], "moves": out, "players": players,
        "opening": {"eco": opening[0], "name": opening[1]} if opening else None,
        "result": game.headers.get("Result", "*"), "depth": depth,
        "engine": engine_name(), "pgn": pgn_text,
    }


JOBS = {}


def start_review(pgn, depth):
    job_id = uuid.uuid4().hex[:10]
    job = {"id": job_id, "status": "running", "done": 0, "total": 0}
    JOBS[job_id] = job

    def work():
        try:
            game = chess.pgn.read_game(io.StringIO(pgn))
            cached = stored_review(game_key(game), depth) if game else None
            if cached:
                job["result"] = cached
            else:
                job["result"] = review_game(pgn, depth, job)
                store_review(pgn, job["result"])
            job["status"] = "done"
        except InterruptedError:
            job["status"] = "cancelled"
        except Exception as ex:
            job["status"], job["error"] = "error", str(ex)

    threading.Thread(target=work, daemon=True).start()
    return job


# --------------------------------------------------------------------------
# Games played in the app
# --------------------------------------------------------------------------

GAMES = {}
SEAT_TYPES = ("human", "engine")


def normalise_seat(spec, color):
    if isinstance(spec, str):
        spec = {"type": spec}
    spec = dict(spec or {})
    kind = spec.get("type", "human")
    if kind not in SEAT_TYPES:
        raise ValueError(f"seat type must be one of {SEAT_TYPES}")
    seat = {"type": kind}
    if kind == "engine":
        elo = spec.get("elo")
        seat["elo"] = None if elo in (None, "", "max") else max(200, min(3600, int(elo)))
        seat["movetime"] = max(0.05, min(30.0, float(spec.get("movetime", 0.5))))
        default = "Stockfish" + (f" ({seat['elo']})" if seat["elo"] else "")
    else:
        default = "Player"
    seat["name"] = str(spec.get("name") or default)[:40]
    return seat


def pick_engine_move(board, seat):
    """Choose a move for an engine seat, weakened to roughly seat['elo'] if set."""
    elo, movetime = seat.get("elo"), seat.get("movetime", 0.5)

    def go(e):
        opts, limit = {}, chess.engine.Limit(time=movetime)
        has = "UCI_Elo" in e.options and "UCI_LimitStrength" in e.options
        lo = e.options["UCI_Elo"].min if has else 1350
        hi = e.options["UCI_Elo"].max if has else 2850
        if elo is None or elo >= hi:
            if has:
                opts["UCI_LimitStrength"] = False
        elif elo >= lo:
            if has:
                opts.update(UCI_LimitStrength=True, UCI_Elo=elo)
        else:
            # Below the engine's floor: weakest setting, shallow search, plus
            # a share of random moves that grows as the target rating drops.
            if has:
                opts.update(UCI_LimitStrength=True, UCI_Elo=lo)
            limit = chess.engine.Limit(depth=1, time=0.05)
            if random.random() < 0.6 * (lo - elo) / max(1, lo - 200):
                return random.choice(list(board.legal_moves))
        return e.play(board, limit, options=opts).move

    return PLAY.run(go)


class Game:
    def __init__(self, white, black, fen=None):
        self.id = uuid.uuid4().hex[:8]
        self.board = chess.Board(fen) if fen else chess.Board()
        if not self.board.is_valid():
            raise ValueError("That FEN is not a legal position.")
        self.start_fen = self.board.fen()
        self.seats = {"white": white, "black": black}
        self.created = time.time()
        self.version = 0
        self.resigned = None       # colour that resigned
        self.error = None
        self.thinking = False
        self.saved = False
        self.cond = threading.Condition()

    # -- status ----------------------------------------------------------
    def outcome(self):
        if self.resigned:
            return ("0-1" if self.resigned == "white" else "1-0"), "resignation"
        oc = self.board.outcome(claim_draw=True)
        if oc is None:
            return None, None
        return oc.result(), oc.termination.name.lower().replace("_", " ")

    def turn(self):
        return "white" if self.board.turn == chess.WHITE else "black"

    def pgn(self):
        g = chess.pgn.Game.from_board(self.board)
        result, _ = self.outcome()
        g.headers.update({
            "Event": "Backrank local game", "Site": "localhost",
            "Date": datetime.datetime.fromtimestamp(self.created).strftime("%Y.%m.%d"),
            "White": self.seats["white"]["name"], "Black": self.seats["black"]["name"],
            "Result": result or "*",
        })
        g.headers.pop("Round", None)
        return str(g)

    def state(self, brief=False):
        result, termination = self.outcome()
        st = {
            "id": self.id, "version": self.version, "fen": self.board.fen(),
            "turn": self.turn(), "ply": len(self.board.move_stack),
            "status": "finished" if result else "active",
            "result": result, "termination": termination,
            "players": self.seats, "thinking": self.thinking, "error": self.error,
            "last_move": self.board.peek().uci() if self.board.move_stack else None,
            "check": self.board.is_check(), "created": self.created,
        }
        if brief:
            return st
        replay, sans = chess.Board(self.start_fen), []
        for mv in self.board.move_stack:
            sans.append(replay.san(mv))
            replay.push(mv)
        st.update({
            "start_fen": self.start_fen,
            "moves": [m.uci() for m in self.board.move_stack], "san": sans,
            "legal": [] if result else [{"uci": m.uci(), "san": self.board.san(m)}
                                        for m in self.board.legal_moves],
            "pgn": self.pgn(),
        })
        return st

    # -- actions (call with self.cond held) -------------------------------
    def _changed(self):
        self.version += 1
        if self.outcome()[0] and not self.saved:
            self.saved = True
            try:
                self._save()
            except OSError:
                pass
        self.cond.notify_all()

    def _write_pgn(self):
        folder = DATA / "games"
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        (folder / f"{stamp}_{self.id}.pgn").write_text(self.pgn() + "\n", encoding="utf-8")

    def _save(self):
        self._write_pgn()
        humans = [c for c in ("white", "black") if self.seats[c]["type"] == "human"]
        if len(humans) == 1 and len(self.board.move_stack) >= 2:
            add_game(self.pgn(), {"source": "local", "speed": "local", "ts": time.time(),
                                  "date": datetime.date.today().isoformat()}, humans[0])
            save_library()

    def _parse(self, text):
        text = str(text).strip()
        mv = None
        if re.fullmatch(r"[a-h][1-8][a-h][1-8][qrbnQRBN]?", text):
            mv = chess.Move.from_uci(text.lower())
            if mv not in self.board.legal_moves:
                mv = None
        if mv is None:
            try:
                mv = self.board.parse_san(text)
            except ValueError:
                raise ValueError(f"Illegal or unreadable move: {text!r}")
        return mv

    def play(self, text, color=None):
        with self.cond:
            if self.outcome()[0]:
                raise ValueError("The game is over.")
            if color and color != self.turn():
                raise ValueError(f"It is {self.turn()}'s turn, not {color}'s.")
            self.board.push(self._parse(text))
            self._changed()
        self.wake_engine()

    def resign(self, color):
        with self.cond:
            if self.outcome()[0]:
                raise ValueError("The game is over.")
            if color not in ("white", "black"):
                raise ValueError("color must be 'white' or 'black'")
            self.resigned = color
            self._changed()

    def takeback(self):
        with self.cond:
            if self.thinking or not self.board.move_stack:
                raise ValueError("Nothing to take back right now.")
            self.resigned = None
            self.board.pop()
            # Against the computer, also undo its reply so it is the human's turn.
            if self.seats[self.turn()]["type"] == "engine" and self.board.move_stack:
                self.board.pop()
            self._changed()
        self.wake_engine()

    def wake_engine(self):
        """If an engine seat is to move, think on a background thread."""
        with self.cond:
            seat = self.seats[self.turn()]
            if seat["type"] != "engine" or self.thinking or self.outcome()[0]:
                return
            self.thinking = True
            board, version = self.board.copy(), self.version

        def think():
            mv, err = None, None
            try:
                mv = pick_engine_move(board, seat)
            except Exception as ex:
                err = str(ex)
            with self.cond:
                self.thinking = False
                if err:
                    self.error = err
                    self.version += 1
                    self.cond.notify_all()
                    return
                if self.version == version and mv in self.board.legal_moves:
                    self.board.push(mv)
                    self._changed()
            self.wake_engine()

        threading.Thread(target=think, daemon=True).start()

    def wait(self, since, seconds):
        with self.cond:
            self.cond.wait_for(lambda: self.version != since, timeout=seconds)


# --------------------------------------------------------------------------
# Importing games from Chess.com / Lichess (read-only public APIs)
# --------------------------------------------------------------------------

USERNAME = re.compile(r"[A-Za-z0-9_-]{2,30}")


def http_get(url, accept="application/json"):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Backrank/1.0 (local personal game-review tool)", "Accept": accept})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "replace")


def import_chesscom(user, limit=40):
    base = f"https://api.chess.com/pub/player/{user.lower()}/games/archives"
    archives = json.loads(http_get(base)).get("archives", [])
    games = []
    for url in reversed(archives[-(3 if limit <= 40 else 12):]):
        if not url.startswith("https://api.chess.com/pub/player/"):
            continue
        for g in reversed(json.loads(http_get(url)).get("games", [])):
            if g.get("rules", "chess") != "chess" or not g.get("pgn"):
                continue
            w, b = g["white"], g["black"]
            result = "1-0" if w.get("result") == "win" else "0-1" if b.get("result") == "win" else "1/2-1/2"
            games.append({
                "white": w.get("username"), "black": b.get("username"),
                "white_elo": w.get("rating"), "black_elo": b.get("rating"),
                "result": result, "speed": g.get("time_class", ""),
                "date": datetime.datetime.fromtimestamp(g.get("end_time", 0)).strftime("%Y-%m-%d"),
                "ts": g.get("end_time", 0), "url": g.get("url"), "pgn": g["pgn"],
            })
        if len(games) >= limit:
            break
    return games[:limit]


def import_lichess(user, limit=30):
    url = f"https://lichess.org/api/games/user/{user}?max={limit}&pgnInJson=true&clocks=true"
    games = []
    for line in http_get(url, accept="application/x-ndjson").splitlines():
        if not line.strip():
            continue
        g = json.loads(line)
        if g.get("variant", "standard") != "standard" or not g.get("pgn"):
            continue
        pl = g.get("players", {})
        name = lambda c: (pl.get(c, {}).get("user") or {}).get("name") or "Anonymous"
        winner = g.get("winner")
        games.append({
            "white": name("white"), "black": name("black"),
            "white_elo": pl.get("white", {}).get("rating"), "black_elo": pl.get("black", {}).get("rating"),
            "result": "1-0" if winner == "white" else "0-1" if winner == "black" else "1/2-1/2",
            "speed": g.get("speed", ""),
            "date": datetime.datetime.fromtimestamp(g.get("createdAt", 0) / 1000).strftime("%Y-%m-%d"),
            "ts": g.get("createdAt", 0) / 1000,
            "url": "https://lichess.org/" + g.get("id", ""), "pgn": g["pgn"],
        })
    return games


# --------------------------------------------------------------------------
# Small position helpers used by the UI
# --------------------------------------------------------------------------


def position_info(fen, move=None):
    board = chess.Board(fen) if fen and fen != "startpos" else chess.Board()
    played = None
    if move:
        try:
            mv = chess.Move.from_uci(move)
            if mv not in board.legal_moves:
                raise ValueError
        except ValueError:
            mv = board.parse_san(move)
        played = {"uci": mv.uci(), "san": board.san(mv)}
        board.push(mv)
    oc = board.outcome()
    return {
        "fen": board.fen(), "turn": "white" if board.turn else "black",
        "played": played, "check": board.is_check(),
        "legal": [{"uci": m.uci(), "san": board.san(m)} for m in board.legal_moves],
        "over": oc is not None, "result": oc.result() if oc else None,
        "termination": oc.termination.name.lower().replace("_", " ") if oc else None,
    }


def parse_pgn(pgn):
    game = chess.pgn.read_game(io.StringIO(pgn))
    if game is None:
        raise ValueError("Could not read that PGN.")
    board = game.board()
    nodes = [{"fen": board.fen(), "san": None, "uci": None}]
    for mv in game.mainline_moves():
        san = board.san(mv)
        board.push(mv)
        nodes.append({"fen": board.fen(), "san": san, "uci": mv.uci()})
    return {"headers": dict(game.headers), "nodes": nodes}


# --------------------------------------------------------------------------
# Player profile, game library and dashboard statistics
# --------------------------------------------------------------------------

LIB_LOCK = threading.RLock()
ERROR_CLASSES = ("mistake", "miss", "blunder")
TAG_LABELS = {
    "hung_moved": "Moved a piece to a square where it could be taken",
    "left_hanging": "Left a piece hanging or missed a threat",
    "missed_capture": "Missed a winning capture",
    "missed_check": "Missed a strong check",
    "missed_mate": "Missed a forced mate",
    "allowed_mate": "Walked into a forced mate",
    "bad_capture": "Made a capture that backfired",
    "threw_win": "Threw away a winning position",
    "low_time": "Erred with under 30 seconds on the clock",
    "delayed_castling": "Put off castling when it was the best move",
    "skipped_capture": "Passed up a capture or recapture that was best",
    "king_move": "Moved the king into trouble before the endgame",
    "early_queen": "Brought the queen out to a bad square in the opening",
    "pawn_push": "Weakening pawn move",
    "positional": "Put a piece on the wrong square",
}
TAG_SHORT = {
    "hung_moved": "Moved into a capture", "left_hanging": "Left a piece hanging",
    "missed_capture": "Missed a winning capture", "missed_check": "Missed a strong check",
    "missed_mate": "Missed a mate", "allowed_mate": "Walked into mate",
    "bad_capture": "Capture that backfired", "threw_win": "threw away a winning position",
    "low_time": "came with under 30 seconds left", "delayed_castling": "Delayed castling",
    "skipped_capture": "Skipped the best capture", "king_move": "Risky king move",
    "early_queen": "Early queen move", "pawn_push": "Weakening pawn move", "positional": "Misplaced piece",
}
CONTEXT_TAGS = ("threw_win", "low_time")   # circumstances of a mistake rather than a kind of mistake


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj), encoding="utf-8")
    os.replace(tmp, path)


PROFILE = load_json(DATA / "profile.json", {})
LIB = load_json(DATA / "library.json", {})


def identities():
    return {str(PROFILE.get(k, "")).strip().lower() for k in ("name", "chesscom", "lichess")} - {""}


def game_key(game):
    h = game.headers
    moves = " ".join(m.uci() for m in game.mainline_moves())
    raw = "|".join([h.get("White", ""), h.get("Black", ""), h.get("UTCDate") or h.get("Date", ""), moves])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def speed_from_tc(tc):
    if not tc or tc in ("-", "?"):
        return "other"
    if "/" in tc:
        return "daily"
    try:
        base, _, inc = tc.partition("+")
        est = int(base) + 40 * int(inc or 0)
    except ValueError:
        return "other"
    return "bullet" if est < 180 else "blitz" if est < 600 else "rapid" if est < 1800 else "classical"


def how_it_ended(game, board):
    text = game.headers.get("Termination", "").lower()
    if board.is_checkmate():
        return "checkmate"
    if game.headers.get("Result") == "1/2-1/2":
        return "draw"
    if "time" in text:
        return "on time"
    if "abandon" in text:
        return "abandoned"
    return "resignation"


def add_game(pgn_text, meta=None, my_color=None):
    """Add one game to the library (or return the existing record)."""
    meta = meta or {}
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        return None
    board, opening, plies = game.board(), None, 0
    if board.fen() != chess.STARTING_FEN:
        return None
    for mv in game.mainline_moves():
        board.push(mv)
        plies += 1
        if plies <= 40:
            opening = OPENINGS.get(board.epd()) or opening
    if plies < 2:
        return None
    h, key = game.headers, game_key(game)
    with LIB_LOCK:
        if key in LIB:
            return LIB[key]
        white, black = meta.get("white") or h.get("White", "?"), meta.get("black") or h.get("Black", "?")
        if my_color is None:
            ids = identities()
            my_color = "white" if white.lower() in ids else "black" if black.lower() in ids else None
        date = meta.get("date") or (h.get("UTCDate") or h.get("Date", "")).replace(".", "-")
        if not re.fullmatch(r"\d{4}-\d\d-\d\d", date):
            date = ""
        ts = meta.get("ts")
        if not ts:
            try:
                ts = datetime.datetime.strptime(date, "%Y-%m-%d").timestamp()
            except ValueError:
                ts = 0
        elo = lambda c: meta.get(c + "_elo") or (int(h[c.capitalize() + "Elo"])
                                                 if h.get(c.capitalize() + "Elo", "").isdigit() else None)
        rec = {
            "id": key, "source": meta.get("source", "pgn"), "url": meta.get("url"),
            "white": white, "black": black, "white_elo": elo("white"), "black_elo": elo("black"),
            "result": meta.get("result") or h.get("Result", "*"),
            "speed": meta.get("speed") or speed_from_tc(h.get("TimeControl")),
            "date": date, "ts": ts, "my_color": my_color,
            "opening": {"eco": opening[0], "name": opening[1]} if opening else None,
            "plies": plies, "end": how_it_ended(game, board), "pgn": pgn_text, "review": None,
        }
        LIB[key] = rec
        return rec


def save_library():
    with LIB_LOCK:
        save_json(DATA / "library.json", LIB)


def stored_review(key, min_depth=0):
    rec = LIB.get(key)
    if not rec or not rec.get("review") or rec["review"]["depth"] < min_depth:
        return None
    return load_json(DATA / "reviews" / f"{key}.json", None)


def store_review(pgn_text, result):
    """Keep a finished review on disk and fold a digest of my moves into the library."""
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        return None
    key = game_key(game)
    rec = LIB.get(key) or add_game(pgn_text)
    if rec is None:
        return None
    result["game_id"] = key
    with LIB_LOCK:
        save_json(DATA / "reviews" / f"{key}.json", result)
        digest = {"depth": result["depth"]}
        my = rec.get("my_color")
        if my:
            opp = "black" if my == "white" else "white"
            mine = [m for m in result["moves"] if m["color"] == my]
            phases = {}
            for m in mine:
                phases[m["phase"]] = phases.get(m["phase"], 0) + 1
            digest.update({
                "accuracy": result["players"][my]["accuracy"],
                "opp_accuracy": result["players"][opp]["accuracy"],
                "acpl": result["players"][my]["acpl"],
                "counts": result["players"][my]["counts"], "phase_moves": phases,
                "errors": [{k: m[k] for k in ("ply", "number", "san", "class", "phase", "tags", "piece", "loss")}
                           for m in mine if m["class"] in ERROR_CLASSES],
            })
        rec["review"] = digest
        save_library()
    return key


def sync_profile():
    """Pull recent games for the profile's usernames into the library."""
    added, errors = 0, []
    for site, fn in (("chesscom", import_chesscom), ("lichess", import_lichess)):
        user = str(PROFILE.get(site, "")).strip()
        if not user:
            continue
        if not USERNAME.fullmatch(user):
            errors.append(f"{site}: '{user}' is not a valid username")
            continue
        try:
            games = fn(user, limit=300)
        except urllib.error.HTTPError as ex:
            errors.append(f"{site}: " + ("no such player" if ex.code == 404 else f"HTTP {ex.code}"))
            continue
        except (urllib.error.URLError, ValueError, OSError) as ex:
            errors.append(f"{site}: could not reach the site ({getattr(ex, 'reason', ex)})")
            continue
        for g in games:
            before = len(LIB)
            color = "white" if str(g["white"]).lower() == user.lower() else "black"
            add_game(g["pgn"], dict(g, source=site), color)
            added += len(LIB) - before
    save_library()
    return {"added": added, "errors": errors}


def import_pgn_text(text):
    """Add every game in a (multi-game) PGN export to the library."""
    stream, added, seen = io.StringIO(text), 0, 0
    while True:
        game = chess.pgn.read_game(stream)
        if game is None:
            break
        seen += 1
        before = len(LIB)
        add_game(str(game))
        added += len(LIB) - before
    save_library()
    return {"found": seen, "added": added}


BATCH = {"status": "idle", "done": 0, "total": 0, "current": "", "failed": 0, "job": {},
         "plies": 0, "plies_done": 0, "started": 0}


def pending_games():
    """My games that have no engine review yet, newest first."""
    with LIB_LOCK:
        return sorted((g for g in LIB.values() if g.get("my_color") and not g.get("review")),
                      key=lambda g: -g["ts"])


def start_batch(depth, count=None):
    """Analyze every unreviewed game of mine (or just the newest `count`)."""
    if BATCH["status"] != "idle":
        raise ValueError("A batch analysis is already running.")
    todo = pending_games()[:count]
    if not todo:
        raise ValueError("Every game in your library is already analyzed.")
    BATCH.update(status="running", done=0, total=len(todo), failed=0, current="", job={},
                 plies=sum(g["plies"] + 1 for g in todo), plies_done=0, started=time.time())

    def work():
        for g in todo:
            job = BATCH["job"] = {"done": 0, "total": 0}
            if BATCH["status"] != "running":
                break
            BATCH["current"] = f"{g['white']} vs {g['black']}"
            try:
                store_review(g["pgn"], review_game(g["pgn"], depth, job))
            except InterruptedError:
                break
            except Exception:
                BATCH["failed"] += 1
            BATCH["done"] += 1
            BATCH["plies_done"] += g["plies"] + 1
        BATCH["status"] = "idle"

    threading.Thread(target=work, daemon=True).start()


def batch_state():
    # Progress counts positions rather than games, so long games do not stall the bar.
    running = BATCH["status"] != "idle"
    done = BATCH["plies_done"] + (BATCH["job"].get("done", 0) if running else 0)
    progress = min(1.0, done / BATCH["plies"]) if BATCH["plies"] else 0
    elapsed = time.time() - BATCH["started"] if running else 0
    return {"status": BATCH["status"], "done": BATCH["done"], "total": BATCH["total"],
            "failed": BATCH["failed"], "current": BATCH["current"], "progress": progress,
            "eta": round(elapsed * (1 - progress) / progress) if running and progress > 0.02 else None,
            "pending": len(pending_games())}


def my_outcome(g):
    if g["result"] == "1/2-1/2":
        return "draw"
    if g["result"] not in ("1-0", "0-1"):
        return None
    return "win" if (g["result"] == "1-0") == (g["my_color"] == "white") else "loss"


def wdl(games):
    out = {"games": 0, "win": 0, "draw": 0, "loss": 0}
    for g in games:
        o = my_outcome(g)
        if o:
            out[o] += 1
            out["games"] += 1
    out["win_pct"] = round(100 * out["win"] / out["games"], 1) if out["games"] else None
    return out


def compute_stats(speed=None, days=None):
    with LIB_LOCK:
        mine = sorted((g for g in LIB.values() if g.get("my_color") and my_outcome(g)), key=lambda g: g["ts"])
    speeds = {}
    for g in mine:
        speeds[g["speed"]] = speeds.get(g["speed"], 0) + 1
    if days:
        cutoff = time.time() - days * 86400
        mine = [g for g in mine if g["ts"] >= cutoff]
    games = [g for g in mine if not speed or g["speed"] == speed]
    opp = lambda g: g["black"] if g["my_color"] == "white" else g["white"]

    # rating over time, for the chosen speed or the most played rated one
    rated = [g for g in games if g.get(g["my_color"] + "_elo")]
    rating_speed = speed
    if not rating_speed and rated:
        tally = {}
        for g in rated:
            tally[g["speed"]] = tally.get(g["speed"], 0) + 1
        rating_speed = max(tally, key=tally.get)
    rating = [{"date": g["date"], "elo": g[g["my_color"] + "_elo"]} for g in rated if g["speed"] == rating_speed]

    # openings, grouped by family and the colour I had
    fams = {}
    for g in games:
        name = g["opening"]["name"].split(":")[0] if g.get("opening") else "Unknown opening"
        fams.setdefault((name, g["my_color"]), []).append(g)
    openings = []
    for (name, color), gs in fams.items():
        accs = [g["review"]["accuracy"] for g in gs if g.get("review") and g["review"].get("accuracy") is not None]
        openings.append(dict(wdl(gs), name=name, color=color,
                             accuracy=round(sum(accs) / len(accs), 1) if accs else None))
    openings.sort(key=lambda o: -o["games"])

    endings = {"win": {}, "loss": {}}
    for g in games:
        o = my_outcome(g)
        if o in endings:
            endings[o][g["end"]] = endings[o].get(g["end"], 0) + 1

    # everything below needs engine reviews
    reviewed = [g for g in games if g.get("review") and g["review"].get("accuracy") is not None]
    counts, phase_moves, phase_errors, tags, pieces = {}, {}, {}, {}, {}
    for g in reviewed:
        r = g["review"]
        for k, v in r["counts"].items():
            counts[k] = counts.get(k, 0) + v
        for k, v in r["phase_moves"].items():
            phase_moves[k] = phase_moves.get(k, 0) + v
        for e in r["errors"]:
            phase_errors[e["phase"]] = phase_errors.get(e["phase"], 0) + 1
            pieces[e["piece"]] = pieces.get(e["piece"], 0) + 1
            for t in e["tags"]:
                slot = tags.setdefault(t, {"tag": t, "label": TAG_LABELS.get(t, t), "short": TAG_SHORT.get(t, t),
                                           "context": t in CONTEXT_TAGS, "count": 0, "examples": []})
                slot["count"] += 1
                slot["examples"].append({
                    "game": g["id"], "ply": e["ply"], "class": e["class"], "date": g["date"], "opponent": opp(g),
                    "move": f"{e['number']}{'.' if g['my_color'] == 'white' else '…'} {e['san']}"})
    total_errors = sum(phase_errors.values())
    tag_list = sorted(tags.values(), key=lambda t: -t["count"])
    for t in tag_list:
        t["examples"] = t["examples"][-4:][::-1]
        t["share"] = round(100 * t["count"] / total_errors) if total_errors else 0
    phases = [{"phase": p, "moves": phase_moves.get(p, 0), "errors": phase_errors.get(p, 0),
               "per100": round(100 * phase_errors.get(p, 0) / phase_moves[p], 1) if phase_moves.get(p) else None}
              for p in ("opening", "middlegame", "endgame")]
    avg = lambda key: round(sum(g["review"][key] for g in reviewed) / len(reviewed), 1) if reviewed else None

    return {
        "overall": wdl(games),
        "white": wdl([g for g in games if g["my_color"] == "white"]),
        "black": wdl([g for g in games if g["my_color"] == "black"]),
        "speeds": speeds, "form": [my_outcome(g) for g in games[-12:]],
        "rating": rating, "rating_speed": rating_speed,
        "openings": openings[:12], "endings": endings,
        "reviewed": len(reviewed), "unreviewed": len(games) - len(reviewed),
        "library": sum(1 for g in list(LIB.values()) if g.get("my_color")), "pending": len(pending_games()),
        "accuracy": avg("accuracy"), "opp_accuracy": avg("opp_accuracy"), "acpl": avg("acpl"),
        "accuracy_trend": [{"date": g["date"], "accuracy": g["review"]["accuracy"]} for g in reviewed],
        "counts": counts, "errors_per_game": round(total_errors / len(reviewed), 1) if reviewed else None,
        "total_errors": total_errors, "phases": phases, "tags": tag_list, "pieces": pieces,
        "games": [{"id": g["id"], "date": g["date"], "opponent": opp(g),
                   "opp_elo": g.get(("black" if g["my_color"] == "white" else "white") + "_elo"),
                   "color": g["my_color"], "outcome": my_outcome(g), "speed": g["speed"],
                   "opening": g["opening"]["name"] if g.get("opening") else "",
                   "accuracy": g["review"].get("accuracy") if g.get("review") else None}
                  for g in games[::-1][:80]],
    }


# --------------------------------------------------------------------------
# Puzzles: positions from my own games where I went wrong
# --------------------------------------------------------------------------

PUZZLE_LOG = load_json(DATA / "puzzles.json", {})
PUZZLE_CACHE = {"stamp": None, "items": {}}
PUZZLE_SLACK = 4.0   # an answer this close to the best move (in win%) also counts


def puzzle_pool():
    """Every mistake, miss and blunder of mine in the analyzed games, keyed by puzzle id."""
    with LIB_LOCK:
        games = [g for g in LIB.values() if g.get("my_color") and g.get("review")]
    stamp = (len(games), sum(g["review"]["depth"] for g in games))
    if PUZZLE_CACHE["stamp"] == stamp:
        return PUZZLE_CACHE["items"]
    items = {}
    for g in games:
        review = load_json(DATA / "reviews" / f"{g['id']}.json", None)
        if not review:
            continue
        my = g["my_color"]
        for m in review["moves"]:
            if m["color"] != my or m["class"] not in ERROR_CLASSES or m["best_uci"] == m["uci"]:
                continue
            win = m["eval_before"]["win"] if my == "white" else 100 - m["eval_before"]["win"]
            if win < 8:      # already lost: nothing worth finding
                continue
            tags = m.get("tags") or []
            kinds = [t for t in tags if t not in CONTEXT_TAGS] or ["positional"]
            pid = f"{g['id']}-{m['ply']}"
            items[pid] = {
                "id": pid, "game": g["id"], "ply": m["ply"], "fen": m["fen_before"], "color": my,
                "number": m["number"], "played_uci": m["uci"], "played_san": m["san"], "class": m["class"],
                "best_uci": m["best_uci"], "best_san": m["best_san"], "best_line": m["best_line"],
                "comment": m["comment"], "eval_best": m["eval_before"], "eval_played": m["eval_after"],
                "win": win, "kinds": kinds, "context": [t for t in tags if t in CONTEXT_TAGS],
                "opponent": g["black"] if my == "white" else g["white"], "date": g["date"],
            }
    PUZZLE_CACHE.update(stamp=stamp, items=items)
    return items


def puzzle_summary():
    items = puzzle_pool()
    done = lambda p: bool(PUZZLE_LOG.get(p["id"], {}).get("solved"))
    kinds = {}
    for pz in items.values():
        for t in pz["kinds"]:
            slot = kinds.setdefault(t, {"tag": t, "label": TAG_SHORT.get(t, t), "count": 0, "solved": 0})
            slot["count"] += 1
            slot["solved"] += done(pz)
    return {"total": len(items), "solved": sum(done(pz) for pz in items.values()),
            "first_try": sum(1 for pz in items.values() if PUZZLE_LOG.get(pz["id"], {}).get("first")),
            "kinds": sorted(kinds.values(), key=lambda k: -k["count"])}


def puzzle_view(pz):
    """What the solver gets to see: the position, not the answer."""
    board = chess.Board(pz["fen"])
    return {k: pz[k] for k in ("id", "game", "ply", "fen", "color", "number", "played_san", "played_uci",
                               "class", "opponent", "date")} | {
        "legal": [m.uci() for m in board.legal_moves], "check": board.is_check(),
        "kinds": [TAG_SHORT.get(t, t) for t in pz["kinds"]],
        "context": [TAG_SHORT.get(t, t) for t in pz["context"]],
        "log": PUZZLE_LOG.get(pz["id"])}


def puzzle_solution(pz):
    return {k: pz[k] for k in ("best_uci", "best_san", "best_line", "played_uci", "played_san", "comment",
                               "eval_best", "eval_played")}


def puzzle_next(tag=None, skip=None):
    """Pick the next puzzle: new ones first, then ones I got wrong, weighted to my commonest mistakes."""
    items = puzzle_pool()
    pool = [pz for pz in items.values() if not tag or tag in pz["kinds"]]
    if not pool:
        return None
    fresh = [pz for pz in pool if pz["id"] not in PUZZLE_LOG]
    again = [pz for pz in pool if pz["id"] in PUZZLE_LOG and not PUZZLE_LOG[pz["id"]].get("solved")]
    group = fresh or again or pool
    if len(group) > 1:
        group = [pz for pz in group if pz["id"] != skip]
    freq = {}
    for pz in items.values():
        freq[pz["kinds"][0]] = freq.get(pz["kinds"][0], 0) + 1
    pick = random.choices(group, weights=[freq[pz["kinds"][0]] for pz in group])[0]
    return dict(puzzle_view(pick), left=len(fresh) + len(again), of=len(pool), repeat=not (fresh or again))


def puzzle_get(pid):
    pz = puzzle_pool().get(pid)
    if not pz:
        raise ApiError(404, "No such puzzle.")
    return pz


def puzzle_try(pid, move):
    pz = puzzle_get(pid)
    board = chess.Board(pz["fen"])
    try:
        mv = chess.Move.from_uci(str(move))
    except ValueError:
        mv = None
    if mv not in board.legal_moves:
        raise ValueError("That move is not legal here.")
    loss = 0.0
    if mv.uci() == pz["best_uci"]:
        verdict = "best"
    elif mv.uci() == pz["played_uci"]:
        verdict, loss = "played", None
    else:
        after = board.copy()
        after.push(mv)
        ev = analyse(LIVE, after, chess.engine.Limit(depth=14, time=1.5))[0]["eval"]
        loss = round(max(0.0, pz["win"] - (ev["win"] if pz["color"] == "white" else 100 - ev["win"])), 1)
        verdict = "good" if loss <= PUZZLE_SLACK else "wrong"
    solved = verdict in ("best", "good")
    with LIB_LOCK:
        entry = PUZZLE_LOG.setdefault(pid, {"tries": 0, "solved": False, "first": False})
        if not entry["solved"]:
            entry["tries"] += 1
            if solved:
                entry.update(solved=True, first=entry["tries"] == 1 and not entry.get("revealed"), ts=int(time.time()))
            save_json(DATA / "puzzles.json", PUZZLE_LOG)
    out = {"verdict": verdict, "loss": loss, "san": board.san(mv), "log": entry, "summary": puzzle_summary()}
    if solved:
        board.push(mv)
        out.update(solution=puzzle_solution(pz), fen=board.fen())
    return out


def puzzle_reveal(pid):
    pz = puzzle_get(pid)
    with LIB_LOCK:
        entry = PUZZLE_LOG.setdefault(pid, {"tries": 0, "solved": False, "first": False})
        if not entry["solved"]:
            entry["revealed"] = True
            save_json(DATA / "puzzles.json", PUZZLE_LOG)
    board = chess.Board(pz["fen"])
    board.push_uci(pz["best_uci"])
    return {"solution": puzzle_solution(pz), "fen": board.fen(), "log": entry}


# --------------------------------------------------------------------------
# Lichess: play your own Lichess games on this board (Board API)
# --------------------------------------------------------------------------

LICHESS_URL = "https://lichess.org"
LICHESS_AGENT = "Backrank (local chess app)"
LI_TOKEN = re.compile(r"[A-Za-z0-9_]{8,512}")
LI_ENDINGS = {"mate": "checkmate", "resign": "resignation", "outoftime": "time ran out",
              "timeout": "a player left", "noStart": "never started", "aborted": "aborted",
              "insufficientMaterialClaim": "insufficient material", "unknownFinish": "ended",
              "draw": "agreed or claimed"}


class LichessAuthError(ValueError):
    pass


def li_request(path, token, form=None, method=None, stream=False, timeout=20):
    """One call to Lichess. Returns parsed JSON, or the open response for ndjson streams."""
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    method = method or ("POST" if data is not None else "GET")
    if method == "POST" and data is None:
        data = b""
    req = urllib.request.Request(LICHESS_URL + path, data=data, method=method, headers={
        "Authorization": "Bearer " + token, "User-Agent": LICHESS_AGENT,
        "Accept": "application/x-ndjson" if stream else "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as ex:
        try:
            detail = json.loads(ex.read().decode("utf-8", "replace")).get("error")
        except Exception:
            detail = None
        if ex.code == 401:
            raise LichessAuthError("Lichess did not accept the access token.")
        if ex.code == 429:
            raise ValueError("Lichess asked us to slow down. Wait a minute and try again.")
        raise ValueError("Lichess: " + (detail if isinstance(detail, str) else json.dumps(detail) if detail
                                        else f"HTTP {ex.code}"))
    except (urllib.error.URLError, OSError) as ex:
        raise ValueError(f"Could not reach Lichess ({getattr(ex, 'reason', ex)}).")
    if stream:
        return resp
    with resp:
        body = resp.read().decode("utf-8", "replace")
    return json.loads(body) if body.strip() else {}


def kill_stream(resp):
    """Drop an ndjson stream that another thread may be blocked reading."""
    try:
        resp.fp.raw._sock.shutdown(socket.SHUT_RDWR)
    except Exception:
        pass
    threading.Thread(target=resp.close, daemon=True).start()


class LichessGame(Game):
    """A game on Lichess, mirrored here from its Board API stream."""

    def __init__(self, gid, my_color, token, epoch):
        mine = my_color if my_color in ("white", "black") else "white"
        seat = lambda c: {"type": "human" if c == mine else "lichess", "name": "You" if c == mine else "Opponent"}
        super().__init__(seat("white"), seat("black"))
        self.id, self.my_color, self.token, self.epoch = gid, mine, token, epoch
        self.li_status, self.winner = "created", None
        self.clock, self.clock_at, self.draw_offer, self.meta = None, 0, None, {}

    # -- status ----------------------------------------------------------
    def outcome(self):
        if self.li_status in ("created", "started"):
            return None, None
        if self.winner in ("white", "black"):
            result = "1-0" if self.winner == "white" else "0-1"
        else:
            result = "*" if self.li_status in ("aborted", "noStart", "unknownFinish") else "1/2-1/2"
        return result, LI_ENDINGS.get(self.li_status, self.li_status)

    def pgn(self):
        g = chess.pgn.Game.from_board(self.board)
        result, _ = self.outcome()
        when = datetime.datetime.fromtimestamp((self.meta.get("createdAt") or self.created * 1000) / 1000,
                                               datetime.timezone.utc)
        g.headers.update({
            "Event": f"{'Rated' if self.meta.get('rated') else 'Casual'} {self.meta.get('speed', '')} game".replace("  ", " "),
            "Site": f"{LICHESS_URL}/{self.id}", "Date": when.strftime("%Y.%m.%d"),
            "White": self.seats["white"]["name"], "Black": self.seats["black"]["name"],
            "Result": result or "*", "UTCDate": when.strftime("%Y.%m.%d"), "UTCTime": when.strftime("%H:%M:%S"),
        })
        for c in ("white", "black"):
            if self.seats[c].get("rating"):
                g.headers[c.capitalize() + "Elo"] = str(self.seats[c]["rating"])
        clock = self.meta.get("clock")
        if clock:
            g.headers["TimeControl"] = f"{clock.get('initial', 0) // 1000}+{clock.get('increment', 0) // 1000}"
        g.headers.pop("Round", None)
        return str(g)

    def state(self, brief=False):
        st = super().state(brief)
        clock = dict(self.clock, age=round(time.time() - self.clock_at, 2)) if self.clock else None
        st.update(source="lichess", my_color=self.my_color, clock=clock, draw_offer=self.draw_offer,
                  url=f"{LICHESS_URL}/{self.id}", rated=bool(self.meta.get("rated")),
                  speed=self.meta.get("speed", ""))
        return st

    def _save(self):
        if len(self.board.move_stack) < 2:
            return
        self._write_pgn()
        ts = (self.meta.get("createdAt") or self.created * 1000) / 1000
        add_game(self.pgn(), {"source": "lichess", "url": f"{LICHESS_URL}/{self.id}", "ts": ts,
                              "speed": self.meta.get("speed") or None,
                              "date": datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")}, self.my_color)
        save_library()

    # -- updates from the Lichess stream ----------------------------------
    def _full(self, ev):
        with self.cond:
            for c in ("white", "black"):
                pl = ev.get(c) or {}
                name = pl.get("name") or (f"lichess AI level {pl['aiLevel']}" if pl.get("aiLevel") else "Anonymous")
                self.seats[c].update(name=str(name)[:40], rating=pl.get("rating"))
            me = (LI.username or "").lower()
            if me and me in ((ev.get("white") or {}).get("id"), (ev.get("black") or {}).get("id")):
                self.my_color = "white" if (ev.get("white") or {}).get("id") == me else "black"
                for c in ("white", "black"):
                    self.seats[c]["type"] = "human" if c == self.my_color else "lichess"
            self.meta = {k: ev.get(k) for k in ("rated", "speed", "createdAt", "clock")}
            fen = ev.get("initialFen") or "startpos"
            self.start_fen = chess.STARTING_FEN if fen == "startpos" else fen
            self._state(ev.get("state") or {})

    def _state(self, st):
        board = chess.Board(self.start_fen)
        for uci in (st.get("moves") or "").split():
            board.push_uci(uci)          # accepts both e1g1 and king-takes-rook castling
        self.board = board
        self.li_status, self.winner = st.get("status", "started"), st.get("winner")
        if "wtime" in st:
            self.clock = {"white": st["wtime"], "black": st.get("btime", 0)}
            self.clock_at = time.time()
        self.draw_offer = "white" if st.get("wdraw") else "black" if st.get("bdraw") else None
        self.error = None
        self._changed()

    def run(self):
        """Follow the game's stream until it ends, reconnecting if the line drops."""
        failures = 0
        while not self.outcome()[0] and LI.epoch == self.epoch:
            try:
                with li_request(f"/api/board/game/stream/{self.id}", self.token, stream=True, timeout=90) as resp:
                    for raw in resp:
                        line = raw.strip()
                        if not line:
                            continue
                        ev = json.loads(line)
                        if ev.get("type") == "gameFull":
                            self._full(ev)
                        elif ev.get("type") == "gameState":
                            with self.cond:
                                self._state(ev)
                        failures = 0
            except (ValueError, OSError, http.client.HTTPException) as ex:
                failures += 1
                with self.cond:
                    if failures >= 6 and self.li_status == "created":   # never managed to load it
                        self.li_status = "unknownFinish"
                    self.error = str(ex) or "lost the connection to Lichess"
                    self.version += 1
                    self.cond.notify_all()
            if not self.outcome()[0]:
                time.sleep(min(15, 1 + 2 * failures))

    # -- actions ----------------------------------------------------------
    def _post(self, action):
        li_request(f"/api/board/game/{self.id}/{action}", self.token, method="POST")

    def play(self, text, color=None):
        with self.cond:
            if self.outcome()[0]:
                raise ValueError("The game is over.")
            if self.turn() != self.my_color:
                raise ValueError("It is not your turn.")
            mv = self._parse(text)
            ply = len(self.board.move_stack)
            self.board.push(mv)          # show it at once; the stream confirms it
            self._changed()
        try:
            self._post("move/" + mv.uci())
        except ValueError:
            with self.cond:
                if len(self.board.move_stack) == ply + 1 and self.board.peek() == mv:
                    self.board.pop()
                    self._changed()
            raise

    def resign(self, color=None):
        if self.outcome()[0]:
            raise ValueError("The game is over.")
        self._post("abort" if len(self.board.move_stack) < 2 else "resign")

    def draw(self, accept):
        if self.outcome()[0]:
            raise ValueError("The game is over.")
        self._post("draw/" + ("yes" if accept else "no"))

    def takeback(self):
        raise ValueError("Take-backs are not available in Lichess games.")

    def wake_engine(self):
        pass


class Lichess:
    """The link to one Lichess account: its token, event stream and pending seek."""

    def __init__(self):
        self.lock = threading.RLock()
        self.token = self.username = self.error = self.pending = self.notice = None
        self.epoch = 0

    def start(self):
        token = load_json(DATA / "lichess.json", {}).get("token")
        if token and LI_TOKEN.fullmatch(str(token)):
            self._open(token, None)

    def _open(self, token, username):
        with self.lock:
            self.epoch += 1
            self.token, self.username, self.error, self.notice = token, username, None, None
            threading.Thread(target=self._events, args=(self.epoch, token), daemon=True).start()

    def connect(self, token):
        token = str(token or "").strip()
        if not LI_TOKEN.fullmatch(token):
            raise ValueError("That does not look like a Lichess access token.")
        username = li_request("/api/account", token).get("username")
        if not username:
            raise ValueError("Lichess did not say whose token that is.")
        save_json(DATA / "lichess.json", {"token": token})
        self._open(token, username)

    def disconnect(self):
        with self.lock:
            self.cancel()
            self.epoch += 1
            self.token = self.username = self.error = self.notice = None
        try:
            (DATA / "lichess.json").unlink()
        except OSError:
            pass

    def live_games(self):
        return [g for g in list(GAMES.values()) if isinstance(g, LichessGame) and not g.outcome()[0]]

    def status(self):
        p = self.pending
        return {"connected": bool(self.token and self.username), "username": self.username,
                "has_token": bool(self.token), "error": self.error, "notice": self.notice,
                "pending": {"kind": p["kind"], "label": p["label"]} if p else None,
                "live": [g.id for g in sorted(self.live_games(), key=lambda g: -g.created)]}

    def _need(self):
        if not self.token:
            raise ValueError("Connect your Lichess account first.")
        if self.pending:
            raise ValueError("Already waiting for a game. Cancel that first.")
        return self.token

    # -- the account's event stream: tells us when a game starts ----------
    def _events(self, epoch, token):
        delay = 2
        while self.epoch == epoch:
            try:
                if not self.username:
                    self.username = li_request("/api/account", token).get("username")
                with li_request("/api/stream/event", token, stream=True, timeout=40) as resp:
                    self.error, delay = None, 2
                    for raw in resp:
                        if self.epoch != epoch:
                            return
                        line = raw.strip()
                        if not line:
                            continue
                        ev = json.loads(line)
                        if ev.get("type") == "gameStart":
                            self._game_started(ev.get("game") or {}, epoch, token)
                        elif ev.get("type") == "challengeDeclined":
                            self.notice = "Your challenge was declined."
            except LichessAuthError as ex:
                if self.epoch == epoch:
                    self.error, self.username = str(ex) + " Disconnect and connect again with a new token.", None
                return
            except (ValueError, OSError, http.client.HTTPException) as ex:
                if self.epoch == epoch:
                    self.error = "Lost the connection to Lichess; retrying."
            time.sleep(delay)
            delay = min(60, delay * 2)

    def _game_started(self, info, epoch, token):
        gid = info.get("gameId") or info.get("id")
        variant = info.get("variant") or {}
        standard = (variant.get("key", "standard") if isinstance(variant, dict) else variant) == "standard"
        playable = (info.get("compat") or {}).get("board", True) and info.get("speed") != "correspondence"
        if not gid or not re.fullmatch(r"\w{4,16}", gid) or not standard or not playable:
            return
        with self.lock:
            if gid in GAMES:
                return
            game = GAMES[gid] = LichessGame(gid, info.get("color"), token, epoch)
        threading.Thread(target=game.run, daemon=True).start()

    # -- starting games ----------------------------------------------------
    @staticmethod
    def _clock(minutes, increment, floor, what):
        minutes, increment = float(minutes), int(increment)
        if not (0 < minutes <= 180 and 0 <= increment <= 60):
            raise ValueError("Pick a time control between 1 and 180 minutes.")
        if minutes * 60 + 40 * increment < floor:
            raise ValueError(what)
        return minutes, increment

    def _wait(self, kind, label, resp, on_line=None):
        """Hold a seek/challenge stream open on a background thread until it resolves."""
        pending = self.pending = {"kind": kind, "label": label, "resp": resp, "id": None}
        self.notice = None

        def watch():
            try:
                with resp:
                    for raw in resp:
                        line = raw.strip()
                        if line and on_line:
                            on_line(pending, json.loads(line))
            except (ValueError, OSError, http.client.HTTPException):
                pass
            if self.pending is pending:
                self.pending = None

        threading.Thread(target=watch, daemon=True).start()

    def seek(self, minutes, increment, rated):
        token = self._need()
        minutes, increment = self._clock(minutes, increment, 480,
                                         "Lichess only lets other apps find opponents at rapid or slower, "
                                         "for example 10+0 or 5+5.")
        resp = li_request("/api/board/seek", token, stream=True, timeout=90, form={
            "rated": "true" if rated else "false", "time": f"{minutes:g}", "increment": increment})
        self._wait("seek", f"Looking for an opponent at {minutes:g}+{increment}", resp)

    def challenge_ai(self, level, minutes, increment, color):
        token = self._need()
        minutes, increment = self._clock(minutes, increment, 180, "Lichess needs blitz or slower here, for example 3+2.")
        game = li_request("/api/challenge/ai", token, form={
            "level": max(1, min(8, int(level))), "clock.limit": int(minutes * 60), "clock.increment": increment,
            "color": color if color in ("white", "black") else "random"})
        if game.get("id"):   # do not wait for the event stream to announce it
            self._game_started({"gameId": game["id"], "color": (game.get("player") if isinstance(
                game.get("player"), str) else None) or color}, self.epoch, token)
        return game.get("id")

    def challenge_user(self, username, minutes, increment, rated, color):
        token = self._need()
        username = str(username or "").strip()
        if not USERNAME.fullmatch(username):
            raise ValueError("That does not look like a Lichess username.")
        minutes, increment = self._clock(minutes, increment, 180, "Lichess needs blitz or slower here, for example 3+2.")
        resp = li_request(f"/api/challenge/{username}", token, stream=True, timeout=90, form={
            "rated": "true" if rated else "false", "clock.limit": int(minutes * 60), "clock.increment": increment,
            "color": color if color in ("white", "black") else "random", "keepAliveStream": "true"})

        def on_line(pending, ev):
            pending["id"] = pending["id"] or ev.get("id") or (ev.get("challenge") or {}).get("id")
            if ev.get("done") == "declined":
                self.notice = f"{username} declined the challenge."

        self._wait("challenge", f"Waiting for {username} to accept ({minutes:g}+{increment})", resp, on_line)

    def cancel(self):
        pending, self.pending = self.pending, None
        if not pending:
            return
        kill_stream(pending["resp"])     # closing a seek's connection withdraws it
        if pending["kind"] == "challenge" and pending["id"] and self.token:
            try:
                li_request(f"/api/challenge/{pending['id']}/cancel", self.token, method="POST")
            except ValueError:
                pass


LI = Lichess()


def fair_play_check():
    """Lichess forbids engine help during a game, so the engine stays off while one is live."""
    if LI.live_games():
        raise ApiError(409, "The engine is switched off while a Lichess game is in progress.")


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Backrank/1.0"

    def log_message(self, *a):  # keep the console quiet
        pass

    # -- plumbing ---------------------------------------------------------
    def send(self, status, body, ctype="application/json; charset=utf-8"):
        if not isinstance(body, (bytes, bytearray)):
            body = (json.dumps(body) if not isinstance(body, str) else body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        raw = self._raw
        if len(raw) > 20_000_000:
            raise ApiError(413, "Request too large.")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            raise ApiError(400, "Body must be JSON.")
        if not isinstance(data, dict):
            raise ApiError(400, "Body must be a JSON object.")
        return data

    def handle_any(self, method):
        # Only answer requests addressed to this machine (blocks DNS-rebinding pages).
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if host not in ("127.0.0.1", "localhost", "::1"):
            return self.send(403, {"error": "This server only answers on localhost."})
        # Always drain the request body so a route that ignores it cannot corrupt keep-alive.
        n = int(self.headers.get("Content-Length") or 0)
        self._raw = self.rfile.read(n) if n else b""
        path, _, query = self.path.partition("?")
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        try:
            self.route(method, path.rstrip("/") or "/", params)
        except ApiError as ex:
            self.send(ex.status, {"error": str(ex)})
        except (ValueError, KeyError) as ex:
            self.send(400, {"error": str(ex) or ex.__class__.__name__})
        except RuntimeError as ex:
            self.send(503, {"error": str(ex)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as ex:
            self.send(500, {"error": f"{ex.__class__.__name__}: {ex}"})

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")

    def do_DELETE(self):
        self.handle_any("DELETE")

    # -- routes -----------------------------------------------------------
    def route(self, method, path, q):
        if method == "GET" and path in ("/", "/index.html"):
            return self.send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")

        if path == "/api/status":
            return self.send(200, {"engine": engine_name(), "engine_path": ENGINE_PATH,
                                   "cpus": CPUS, "openings": len(OPENINGS)})

        if path == "/api/position" and method == "POST":
            d = self.body()
            return self.send(200, position_info(d.get("fen"), d.get("move")))

        if path == "/api/pgn" and method == "POST":
            return self.send(200, parse_pgn(self.body().get("pgn", "")))

        if path == "/api/analyze" and method == "POST":
            fair_play_check()
            d = self.body()
            board = chess.Board(d["fen"]) if d.get("fen") else chess.Board()
            for mv in d.get("moves", []):
                board.push_uci(mv)
            limit = chess.engine.Limit(depth=max(1, min(40, int(d.get("depth", 16)))),
                                       time=max(0.05, min(30.0, float(d.get("time", 1.5)))))
            lines = analyse(LIVE, board, limit, multipv=max(1, min(5, int(d.get("multipv", 1)))))
            return self.send(200, {"fen": board.fen(), "lines": lines})

        # reviews
        if path == "/api/review" and method == "POST":
            fair_play_check()
            d = self.body()
            if not ENGINE_PATH:
                raise RuntimeError("No Stockfish binary found. Put stockfish in the 'engine' folder.")
            if not str(d.get("pgn", "")).strip():
                raise ValueError("Paste a PGN first.")
            job = start_review(d["pgn"], max(6, min(26, int(d.get("depth", 14)))))
            return self.send(200, {"id": job["id"]})
        m = re.fullmatch(r"/api/review/(\w+)", path)
        if m:
            job = JOBS.get(m.group(1))
            if not job:
                raise ApiError(404, "No such review.")
            if method == "DELETE":
                job["cancel"] = True
            return self.send(200, {k: v for k, v in job.items() if k != "cancel"})

        # imports
        m = re.fullmatch(r"/api/import/(chesscom|lichess)", path)
        if m and method == "GET":
            user = q.get("user", "")
            if not USERNAME.fullmatch(user):
                raise ValueError("That does not look like a username.")
            try:
                games = import_chesscom(user) if m.group(1) == "chesscom" else import_lichess(user)
            except urllib.error.HTTPError as ex:
                raise ApiError(404 if ex.code == 404 else 502,
                               "No such player." if ex.code == 404 else f"Site answered HTTP {ex.code}.")
            except urllib.error.URLError as ex:
                raise ApiError(502, f"Could not reach the site: {ex.reason}")
            return self.send(200, {"games": games})

        # profile, library, dashboard
        if path == "/api/profile":
            if method == "POST":
                d = self.body()
                for k in ("name", "chesscom", "lichess"):
                    PROFILE[k] = str(d.get(k, "")).strip()[:40]
                save_json(DATA / "profile.json", PROFILE)
                ids = identities()
                with LIB_LOCK:  # re-check which side is "me" for games added by PGN
                    for g in LIB.values():
                        if g["source"] == "pgn":
                            g["my_color"] = ("white" if g["white"].lower() in ids
                                             else "black" if g["black"].lower() in ids else None)
                save_library()
            with LIB_LOCK:
                mine = [g for g in LIB.values() if g.get("my_color")]
            return self.send(200, {"profile": PROFILE, "games": len(mine),
                                   "reviewed": sum(1 for g in mine if g.get("review")), "batch": batch_state()})
        if path == "/api/profile/sync" and method == "POST":
            return self.send(200, sync_profile())
        if path == "/api/library/import" and method == "POST":
            return self.send(200, import_pgn_text(str(self.body().get("pgn", ""))))
        if path == "/api/library/analyze":
            if method == "POST":
                d = self.body()
                if not ENGINE_PATH:
                    raise RuntimeError("No Stockfish binary found. Put stockfish in the 'engine' folder.")
                start_batch(max(6, min(22, int(d.get("depth", 12)))),
                            max(1, int(d["count"])) if d.get("count") else None)
            elif method == "DELETE":
                BATCH["status"] = "stopping" if BATCH["status"] == "running" else BATCH["status"]
                BATCH["job"]["cancel"] = True
            return self.send(200, batch_state())
        m = re.fullmatch(r"/api/library/(\w+)", path)
        if m and method == "GET":
            rec = LIB.get(m.group(1))
            if not rec:
                raise ApiError(404, "No such game in the library.")
            return self.send(200, {"game": rec, "review": stored_review(rec["id"])})
        if path == "/api/stats":
            return self.send(200, compute_stats(q.get("speed") or None, int(q["days"]) if q.get("days") else None))

        # puzzles
        if path == "/api/puzzles":
            return self.send(200, puzzle_summary())
        if path == "/api/puzzles/next":
            return self.send(200, {"puzzle": puzzle_next(q.get("tag") or None, q.get("skip")),
                                   "summary": puzzle_summary()})
        m = re.fullmatch(r"/api/puzzles/([\w-]+)/(try|reveal)", path)
        if m and method == "POST":
            if m.group(2) == "try":
                return self.send(200, puzzle_try(m.group(1), self.body().get("move")))
            return self.send(200, puzzle_reveal(m.group(1)))

        # lichess
        if path == "/api/lichess":
            return self.send(200, LI.status())
        m = re.fullmatch(r"/api/lichess/(connect|disconnect|seek|ai|challenge|cancel)", path)
        if m and method == "POST":
            d, what = self.body(), m.group(1)
            clock = (d.get("minutes", 10), d.get("increment", 0))
            if what == "connect":
                LI.connect(d.get("token"))
            elif what == "disconnect":
                LI.disconnect()
            elif what == "seek":
                LI.seek(*clock, bool(d.get("rated")))
            elif what == "ai":
                LI.challenge_ai(d.get("level", 3), *clock, d.get("color"))
            elif what == "challenge":
                LI.challenge_user(d.get("username"), *clock, bool(d.get("rated")), d.get("color"))
            else:
                LI.cancel()
            return self.send(200, LI.status())

        # games
        if path == "/api/games":
            if method == "POST":
                d = self.body()
                game = Game(normalise_seat(d.get("white", "human"), "white"),
                            normalise_seat(d.get("black", "human"), "black"), d.get("fen"))
                if not ENGINE_PATH and "engine" in (game.seats["white"]["type"], game.seats["black"]["type"]):
                    raise RuntimeError("No Stockfish binary found, so the computer cannot play.")
                GAMES[game.id] = game
                game.wake_engine()
                return self.send(200, game.state())
            games = sorted(GAMES.values(), key=lambda g: -g.created)
            return self.send(200, {"games": [g.state(brief=True) for g in games]})
        m = re.fullmatch(r"/api/games/(\w+)(?:/(move|resign|takeback|draw|pgn))?", path)
        if m:
            game = GAMES.get(m.group(1))
            if not game:
                raise ApiError(404, "No such game.")
            action = m.group(2)
            if action is None and method == "GET":
                if "since" in q:
                    game.wait(int(q["since"]), max(0.0, min(60.0, float(q.get("wait", 25)))))
                return self.send(200, game.state())
            if action == "pgn":
                return self.send(200, game.pgn() + "\n", "text/plain; charset=utf-8")
            if method != "POST":
                raise ApiError(405, "Use POST.")
            d = self.body()
            if action == "move":
                game.play(d["move"], d.get("color"))
            elif action == "resign":
                game.resign(d.get("color") or game.turn())
            elif action == "takeback":
                game.takeback()
            elif action == "draw":
                if not isinstance(game, LichessGame):
                    raise ValueError("Draw offers are only for Lichess games.")
                game.draw(d.get("accept", True))
            return self.send(200, game.state())

        raise ApiError(404, "Not found.")


def serve(port=8765, engine=None):
    """Start the HTTP server on a background thread and return it."""
    global ENGINE_PATH
    ENGINE_PATH = find_engine(engine)
    LI.start()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def main():
    ap = argparse.ArgumentParser(description="Backrank local chess server (browser mode)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--engine", help="path to a Stockfish (or any UCI) binary")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    httpd = serve(args.port, args.engine)
    url = f"http://127.0.0.1:{args.port}"
    say(f"Backrank is running at {url}   (Ctrl+C to stop)")
    if ENGINE_PATH:
        say(f"Engine: {ENGINE_PATH}")
    else:
        say("No Stockfish found. Download it from https://stockfishchess.org/download/")
        say(f"and put the stockfish executable in: {ROOT / 'engine'}")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        say("\nStopped.")
        httpd.shutdown()


if __name__ == "__main__":
    main()
