# Treadmill Logger - Windows EXE Build

This project can be built into a single Windows 11 executable using PyInstaller.

## Prerequisites
- Windows 11
- Python 3.9+ installed and on PATH

## Build
From this folder:

```bat
build_exe.bat
```

This will:
- create a local `.venv`
- install dependencies
- generate `dist\TreadmillLogger.exe`

## Run
Double-click:
```
dist\TreadmillLogger.exe
```

## Notes
- If Windows SmartScreen blocks the app, click **More info** → **Run anyway**.
- If you rebuild, delete `dist`/`build` if you want a clean output.
