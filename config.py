"""
Загрузка конфигурации из .env файла.
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).parent
DATA_DIR = ROOT_DIR / "data"
RECORDINGS_DIR = DATA_DIR / "recordings"
DOCUMENTS_DIR = DATA_DIR / "documents"
DB_PATH = DATA_DIR / "meets.db"

load_dotenv(ROOT_DIR / ".env")

HUGGINGFACE_TOKEN = os.getenv("HUGGINGFACE_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Платформа
IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

# Настройки захвата
CHUNK_DURATION_SEC = 30          # длина одного чанка
SILENCE_THRESHOLD_RMS = 100      # ниже — считается тишиной
QUALITY_THRESHOLD = 0.70         # confidence ниже этого → toast-предупреждение

# Whisper
WHISPER_MODEL = "large-v3"
WHISPER_LANGUAGE = "ru"


def ensure_dirs() -> None:
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)
