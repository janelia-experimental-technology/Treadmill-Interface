@echo off
setlocal

REM Create a local virtual environment for building
if not exist .venv (
  python -m venv .venv
)

call .venv\Scripts\activate

REM Install build dependencies
python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller

REM Build a single-file Windows executable (no console)
pyinstaller --noconfirm --clean --onefile --windowed --name TreadmillLogger treadmill_logger.py

REM Output will be in dist\TreadmillLogger.exe
endlocal
