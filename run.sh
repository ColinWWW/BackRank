#!/bin/sh
cd "$(dirname "$0")"
python3 -c "import chess" 2>/dev/null || python3 -m pip install --user chess
exec python3 server.py "$@"
