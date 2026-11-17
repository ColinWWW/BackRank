@echo off
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py) || (set PY=python)
%PY% -c "import chess" 2>nul || %PY% -m pip install chess
%PY% server.py
pause
