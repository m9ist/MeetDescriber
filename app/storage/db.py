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
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | processing | transcribed | analyzed | done | error
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
    """Создаёт таблицы если их нет. Сбрасывает зависшие 'processing' задания в 'pending'."""
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        conn.execute(
            "UPDATE jobs SET status='pending', updated_at=datetime('now') WHERE status='processing'"
        )

        # Миграции: добавляем новые колонки если их нет
        existing_jobs = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        for col, ddl in [
            ("transcribe_duration_sec", "ALTER TABLE jobs ADD COLUMN transcribe_duration_sec REAL"),
            ("diarize_duration_sec",    "ALTER TABLE jobs ADD COLUMN diarize_duration_sec REAL"),
            ("analyze_duration_sec",    "ALTER TABLE jobs ADD COLUMN analyze_duration_sec REAL"),
            ("followup_duration_sec",   "ALTER TABLE jobs ADD COLUMN followup_duration_sec REAL"),
            ("dismissed",               "ALTER TABLE jobs ADD COLUMN dismissed INTEGER DEFAULT 0"),
        ]:
            if col not in existing_jobs:
                conn.execute(ddl)

        existing_sessions = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        if "ended_at" not in existing_sessions:
            conn.execute("ALTER TABLE sessions ADD COLUMN ended_at TEXT")


def db_exists() -> bool:
    return Path(config.DB_PATH).exists()


def update_session(session_id: int, title: str, agenda: str) -> None:
    """Обновляет название и агенду сессии."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE sessions SET title=?, agenda=? WHERE id=?",
            (title, agenda, session_id),
        )


def update_job_paths(session_id: int, paths: dict) -> None:
    """Обновляет пути к файлам задания после переименования."""
    with get_conn() as conn:
        conn.execute(
            """UPDATE jobs SET
               transcription_path=COALESCE(?, transcription_path),
               analysis_path=COALESCE(?, analysis_path),
               followup_path=COALESCE(?, followup_path),
               updated_at=datetime('now')
               WHERE session_id=?""",
            (
                paths.get("transcription"),
                paths.get("analysis"),
                paths.get("followup"),
                session_id,
            ),
        )
