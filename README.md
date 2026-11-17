# Backrank

Local chess game review, analysis board, and play. Everything runs on your machine.

## Setup (Windows) - desktop app

1. Install Python 3.9+ from python.org if you don't have it (only needed to build).
2. Double-click `build.bat`. It installs the build tools and produces
   `Backrank.exe` in this folder (takes a minute or two).
3. Download Stockfish from https://stockfishchess.org/download/ (the AVX2
   Windows build), unzip it, and put it in the `engine` folder.
4. Run `Backrank.exe`. It opens in its own window; closing the window quits it.

Keep `Backrank.exe` next to the `engine` folder. You can move the pair anywhere
and pin the exe to the taskbar. Run `build.bat` again after changing the code.

Without building: `run.bat` runs the same app in your browser instead.
Linux/macOS: `./run.sh`, or `pip install pywebview && python app.py` for a window.

## What's in it

- **Profile** is the first screen. See below.
- **Review** - import recent games by Chess.com or Lichess username, or paste a
  PGN. Every move is graded (Brilliant to Blunder), with accuracy, average
  centipawn loss, an eval graph, key moments, a coach note per move, opening
  names, and a "show best move" toggle. Make a move on the board during a
  review to explore your own line with a live engine eval.
- **Profile** - set your name and Chess.com / Lichess usernames, then fetch
  your games (or import a PGN export). The dashboard shows win rate overall and
  by colour, rating over time, results by opening, how games end, and - for
  games you have had analyzed - average accuracy, move-quality breakdown, your
  most common kinds of mistake with clickable examples, and which game phase
  and piece they happen with. **Analyze all games** runs the engine over every
  game that has not been reviewed yet; it saves as it goes, so you can stop and
  resume. Games played in the app are tracked too.
- **Puzzles** - every puzzle is a position from one of your analyzed games
  where you made a mistake, miss or blunder. You are shown what you played and
  have to find something better. The engine's best move is accepted, and so is
  any move within 4 points of win chance of it. New puzzles come first, then
  ones you got wrong, and kinds of mistake you make most often come up most.
  Pick a single kind from the list, or use **Practice these** on a row of the
  Profile's mistakes card. Progress is kept in `data/puzzles.json`.
- **Play** - against Stockfish at a chosen strength, or against a friend on the
  same screen.
- **Play on Lichess** - choose Lichess as the opponent on the Play tab to play
  your own Lichess games on this board: find an opponent (rapid or slower; a
  Lichess rule for other apps), play the Lichess computer, or challenge a
  player by username (blitz or slower). You connect once with a personal access
  token that has only the "Play games with the board API" permission, created
  at lichess.org under Preferences, API access tokens. The token is stored in
  `data/lichess.json` and sent only to lichess.org; Disconnect deletes it from
  this computer (delete it on Lichess too if you want it revoked). Lichess
  forbids engine help during a game, so analysis and review are switched off
  while a Lichess game is in progress. Finished games go into your library.
  Not supported: take-backs, chat, incoming challenges, correspondence games.
- **Analysis** - free board with up to 5 engine lines; load any FEN or PGN.

Your profile, game library and saved reviews live in the `data` folder next to
the app; delete that folder to start fresh. Games played in the app are also
saved as PGN in `data/games`.

## How the numbers are computed

- Reviews run one single-threaded Stockfish per CPU core (minus two), each
  taking one position at a time, so a game is analyzed many positions at once.
  Positions already seen at the same depth (openings, mostly) are reused.
- Win chance from centipawns, per-move accuracy, and game accuracy use
  Lichess' published formulas.
- Classifications use win-chance loss: <=2 Excellent, <=5 Good, <=10 Inaccuracy,
  <=20 Mistake, above that Blunder. Best = the engine's first choice. Great =
  the only move that holds. Brilliant = a sound piece sacrifice. Miss = failing
  to punish a mistake or missing a forced mate.
- Mistake categories on the Profile page are heuristics (for example "left a
  piece hanging" means the engine's reply wins material by capture). A move can
  fall into more than one category.
- "Est. rating" is a rough curve fitted to average centipawn loss. Treat it as
  a mood indicator, not a measurement.
- Strength settings below the engine's minimum Elo (about 1320 in current
  Stockfish) are approximated by mixing in random moves.

## Files

    app.py             desktop window around the server (what the exe runs); on
                       Windows it is frameless and the page draws the title bar
    icon.ico           app icon (the rook from the in-app logo)
    build.bat          builds Backrank.exe
    server.py          the whole backend (python-chess + Stockfish over UCI)
    static/index.html  the whole frontend (no build step, no CDN)
    openings.json      opening names (from lichess-org/chess-openings, CC0)
