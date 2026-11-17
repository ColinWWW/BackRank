@echo off
rem Builds Backrank.exe (a single-file desktop app) into this folder.
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py) || (set PY=python)
%PY% -m pip install --upgrade chess pywebview pyinstaller || goto :fail
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed --name Backrank --icon icon.ico ^
  --add-data "static;static" --add-data "openings.json;." --add-data "icon.ico;." app.py || goto :fail
move /y dist\Backrank.exe . >nul
rmdir /s /q build dist 2>nul
del Backrank.spec 2>nul
echo.
echo Done. Backrank.exe is in this folder. Keep it next to the "engine" folder.
pause
exit /b 0
:fail
echo.
echo Build failed. Copy the error above and send it to Claude.
pause
exit /b 1
