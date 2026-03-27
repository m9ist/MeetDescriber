"""
SQLite схема и базовые операции.

Таблицы:
  sessions  — записанные встречи
  speakers  — сопоставление голосовых кластеров с именами
  jobs      — задания на post-processing
"""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import config


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT,                        -- название совещания
    agenda      TEXT,                        -- агенда, если задана
    started_at  TEXT NOT NULL,               -- ISO-8601
    ended_at    TEXT,
    audio_path  TEXT,                        -- путь к финальному аудиофайлу
    source      TEXT,                        -- 'meet' | 'manual'
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS speakers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    label       TEXT NOT NULL,               -- 'SPEAKER_00' и т.п. от pyannote
    name        TEXT,                        -- имя, если распознано
    session_id  INTEGER REFERENCES sessions(id),
    global_id   TEXT,                        -- для идентификации между сессиями (будущее)
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions(id),
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | processing | done | error
    transcription_path  TEXT,
    analysis_path       TEXT,
    followup_path       TEXT,
    error               TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


@contextmanager
def get_conn():
    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Создаёт таблицы если их нет."""
    with get_conn() as conn:
        conn.executescript(SCHEMA)


def db_exists() -> bool:
    return Path(config.DB_PATH).exists()
