"""
Функции для окна управления совещаниями.

Все операции работают напрямую с БД и файловой системой.
Не зависит от UI.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Literal

import config
from app.storage.db import get_conn
from app.storage.file_manager import get_doc_paths


# ── Чтение ────────────────────────────────────────────────────────────────────

def list_all_meetings(
    search: str = "",
    status_filter: str = "all",
) -> list[dict]:
    """
    Возвращает список всех совещаний, отсортированных по дате убывания.

    search        — подстрока в названии (case-insensitive), пустая строка = без фильтра
    status_filter — 'all' или конкретный статус ('pending', 'transcribed', ...)
    """
    query = """
        SELECT
            s.id            AS session_id,
            s.title,
            s.agenda,
            s.started_at,
            s.ended_at,
            s.source,
            j.id            AS job_id,
            j.status,
            j.error,
            j.dismissed,
            j.transcription_path,
            j.analysis_path,
            j.followup_path,
            j.transcribe_duration_sec,
            j.diarize_duration_sec,
            j.analyze_duration_sec,
            j.followup_duration_sec
        FROM sessions s
        LEFT JOIN jobs j ON j.session_id = s.id
        WHERE 1=1
    """
    params: list = []

    if search:
        query += " AND LOWER(s.title) LIKE LOWER(?)"
        params.append(f"%{search}%")

    if status_filter != "all":
        query += " AND j.status = ?"
        params.append(status_filter)

    query += " ORDER BY s.started_at DESC"

    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()

    result = []
    for row in rows:
        d = dict(row)
        # Вычисляем длительность в секундах
        started = d.get("started_at") or ""
        ended = d.get("ended_at") or ""
        duration_sec = None
        if started and ended:
            try:
                from datetime import datetime
                fmt = "%Y-%m-%dT%H:%M:%S"
                # Обрезаем часовой пояс если есть (+00:00 / Z)
                s = started[:19]
                e = ended[:19]
                duration_sec = (
                    datetime.fromisoformat(e) - datetime.fromisoformat(s)
                ).total_seconds()
                if duration_sec < 0:
                    duration_sec = None
            except Exception:
                pass
        d["duration_sec"] = duration_sec
        result.append(d)

    return result


def get_stats() -> dict:
    """
    Возвращает агрегированную статистику:
      meetings_count, recordings_size_bytes, documents_size_bytes
    """
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
    meetings_count = row[0] if row else 0

    recordings_size = _dir_size(config.RECORDINGS_DIR)
    documents_size = _dir_size(config.DOCUMENTS_DIR)

    return {
        "meetings_count": meetings_count,
        "recordings_size_bytes": recordings_size,
        "documents_size_bytes": documents_size,
    }


# ── Удаление ──────────────────────────────────────────────────────────────────

def delete_meeting(session_id: int) -> None:
    """
    Удаляет совещание полностью:
    - записи в sessions, jobs, speakers
    - все документы (transcription, analysis, followup, prompt-файлы)
    - папку data/recordings/session_X/
    """
    with get_conn() as conn:
        # Получаем данные для удаления файлов
        session = conn.execute(
            "SELECT title, started_at FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        job = conn.execute(
            """SELECT transcription_path, analysis_path, followup_path
               FROM jobs WHERE session_id=?""",
            (session_id,),
        ).fetchone()

        if session:
            session = dict(session)
        if job:
            job = dict(job)

        # Удаляем записи БД
        conn.execute("DELETE FROM speakers WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM jobs WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))

    # Удаляем файлы документов
    if session:
        doc_paths = get_doc_paths(session["title"] or "", session["started_at"] or "")
        for key in ("transcription", "analysis", "analysis_prompt", "followup", "followup_prompt"):
            _unlink(doc_paths.get(key))

    # На случай если пути в БД расходятся с get_doc_paths (переименование и т.п.)
    if job:
        for col in ("transcription_path", "analysis_path", "followup_path"):
            p = job.get(col)
            if p:
                _unlink(Path(p))

    # Удаляем папку с аудиочанками
    session_dir = config.RECORDINGS_DIR / f"session_{session_id}"
    _rmdir(session_dir)


def delete_audio(session_id: int) -> None:
    """Удаляет только папку data/recordings/session_X/ (документы остаются)."""
    session_dir = config.RECORDINGS_DIR / f"session_{session_id}"
    _rmdir(session_dir)


def delete_old_audio(days: int) -> int:
    """
    Удаляет папки с аудио для сессий старше `days` дней
    у которых status IN ('transcribed', 'analyzed', 'done').

    Возвращает количество удалённых папок.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT s.id FROM sessions s
               JOIN jobs j ON j.session_id = s.id
               WHERE j.status IN ('transcribed', 'analyzed', 'done')
                 AND s.started_at < datetime('now', ? || ' days')""",
            (f"-{days}",),
        ).fetchall()

    deleted = 0
    for row in rows:
        session_dir = config.RECORDINGS_DIR / f"session_{row[0]}"
        if session_dir.exists():
            _rmdir(session_dir)
            deleted += 1
    return deleted


# ── Сброс этапа ───────────────────────────────────────────────────────────────

def reset_to_stage(
    job_id: int,
    stage: Literal["transcription", "analysis", "followup"],
) -> None:
    """
    Сбрасывает задание к указанному этапу:
    - удаляет файлы этого и последующих этапов
    - выставляет соответствующий status
    - снимает dismissed (dismissed=0)

    stage='transcription' → удалить transcription+analysis+followup, status='pending'
    stage='analysis'      → удалить analysis+followup, status='transcribed'
    stage='followup'      → удалить followup, status='analyzed'
    """
    with get_conn() as conn:
        row = conn.execute(
            """SELECT j.transcription_path, j.analysis_path, j.followup_path,
                      s.title, s.started_at
               FROM jobs j JOIN sessions s ON s.id = j.session_id
               WHERE j.id=?""",
            (job_id,),
        ).fetchone()
        if not row:
            return
        row = dict(row)

    doc_paths = get_doc_paths(row["title"] or "", row["started_at"] or "")

    if stage == "transcription":
        _unlink_stage_file(row.get("transcription_path"), doc_paths.get("transcription"))
        _unlink(doc_paths.get("analysis_prompt"))
        _unlink_stage_file(row.get("analysis_path"), doc_paths.get("analysis"))
        _unlink(doc_paths.get("followup_prompt"))
        _unlink_stage_file(row.get("followup_path"), doc_paths.get("followup"))
        new_status = "pending"
        clear_cols = "transcription_path=NULL, analysis_path=NULL, followup_path=NULL, " \
                     "transcribe_duration_sec=NULL, analyze_duration_sec=NULL, followup_duration_sec=NULL"

    elif stage == "analysis":
        _unlink(doc_paths.get("analysis_prompt"))
        _unlink_stage_file(row.get("analysis_path"), doc_paths.get("analysis"))
        _unlink(doc_paths.get("followup_prompt"))
        _unlink_stage_file(row.get("followup_path"), doc_paths.get("followup"))
        new_status = "transcribed"
        clear_cols = "analysis_path=NULL, followup_path=NULL, " \
                     "analyze_duration_sec=NULL, followup_duration_sec=NULL"

    else:  # followup
        _unlink(doc_paths.get("followup_prompt"))
        _unlink_stage_file(row.get("followup_path"), doc_paths.get("followup"))
        new_status = "analyzed"
        clear_cols = "followup_path=NULL, followup_duration_sec=NULL"

    with get_conn() as conn:
        conn.execute(
            f"UPDATE jobs SET status=?, dismissed=0, {clear_cols}, "
            f"error=NULL, updated_at=datetime('now') WHERE id=?",
            (new_status, job_id),
        )


# ── Скрытие ───────────────────────────────────────────────────────────────────

def set_dismissed(job_id: int, dismissed: bool) -> None:
    """Устанавливает флаг dismissed для задания."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET dismissed=?, updated_at=datetime('now') WHERE id=?",
            (1 if dismissed else 0, job_id),
        )


# ── Вспомогательные ───────────────────────────────────────────────────────────

def _unlink(path) -> None:
    """Удаляет файл если существует. Принимает Path или str или None."""
    if path is None:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def _unlink_stage_file(db_path, computed_path) -> None:
    """Удаляет файл по пути из БД (если задан), затем по вычисленному пути."""
    if db_path:
        _unlink(Path(db_path))
    if computed_path and computed_path != db_path:
        _unlink(computed_path)


def _rmdir(path: Path) -> None:
    """Удаляет директорию рекурсивно если существует."""
    if path and path.exists():
        try:
            shutil.rmtree(path)
        except OSError:
            pass


def _dir_size(path: Path) -> int:
    """Рекурсивно считает суммарный размер файлов в директории."""
    if not path or not path.exists():
        return 0
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total
