#!/bin/bash
# Запуск MeetDescriber на macOS
cd "$(dirname "$0")"
# Homebrew tools (ffmpeg и др.) могут не быть в PATH при запуске вне терминала
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
PYTHONUNBUFFERED=1 .venv/bin/python -m app.main
