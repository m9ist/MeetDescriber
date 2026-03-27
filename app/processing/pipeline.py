"""
Post-processing пайплайн.

Этап 4:
  1. Слить чанки в merged.wav
  2. Транскрипция (faster-whisper / mlx-whisper)
  3. Диаризация (pyannote.audio)
  4. Выравнивание по временным меткам
  5. Детекция имён спикеров
  6. Сохранение спикеров в БД
  7. Генерация _transcription.md

Этап 5 (LLM) — вызывается следующим шагом после Этапа 4.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Optional

import config
from app.storage.db import get_conn
from app.storage import file_manager
from app.transcription.backend import get_backend, TranscriptionResult
from app.diarization.pyannote_diarizer import PyannoteDiarizer, DiarizationSegment


# ── Выравнивание транскрипции и диаризации ────────────────────────────────────

def _assign_speakers(
    transcription: TranscriptionResult,
    diarization: list[DiarizationSegment],
) -> list[dict]:
    """
    Для каждого транскрипционного сегмента находит спикера по максимальному перекрытию.

    Returns:
        list[dict] с ключами: start, end, text, confidence, speaker
    """
    result = []
    for seg in transcription.segments:
        best_speaker = ""
        best_overlap = 0.0
        for d in diarization:
            overlap = min(seg.end, d.end) - max(seg.start, d.start)
            if overlap > best_overlap:
                best_overlap = overlap
                best_speaker = d.speaker
        result.append({
            "start":      seg.start,
            "end":        seg.end,
            "text":       seg.text,
            "confidence": seg.confidence,
            "speaker":    best_speaker,
        })
    return result


# ── Детекция имён спикеров ────────────────────────────────────────────────────

_NAME_PATTERNS = [
    r"меня зовут\s+([А-ЯЁа-яёA-Za-z][а-яёa-z]+)",
    r"я\s+([А-ЯЁA-Z][а-яёa-z]+)[,\s]",
    r"это\s+([А-ЯЁA-Z][а-яёa-z]+)[,\s]",
    r"говорит\s+([А-ЯЁA-Z][а-яёa-z]+)[,\s]",
    r"([А-ЯЁA-Z][а-яёa-z]+)\s+здесь",
]


def _detect_names(aligned: list[dict]) -> dict[str, str]:
    """
    Сканирует текст сегментов и пытается сопоставить спикеров с именами.

    Returns:
        dict speaker_label → name (только найденные)
    """
    found: dict[str, str] = {}
    for seg in aligned:
        speaker = seg.get("speaker")
        if not speaker or speaker in found:
            continue
        text = seg.get("text", "")
        for pattern in _NAME_PATTERNS:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                found[speaker] = m.group(1).capitalize()
                break
    return found


def _build_speaker_map(
    aligned: list[dict],
    detected_names: dict[str, str],
    saved_names: dict[str, str],
) -> dict[str, str]:
    """
    Строит итоговый словарь speaker_label → отображаемое имя.
    Приоритет: сохранённые в БД > только что обнаруженные > Спикер N.
    """
    all_labels = sorted({seg["speaker"] for seg in aligned if seg.get("speaker")})
    speaker_map: dict[str, str] = {}
    for i, label in enumerate(all_labels, start=1):
        if label in saved_names:
            speaker_map[label] = saved_names[label]
        elif label in detected_names:
            speaker_map[label] = detected_names[label]
        else:
            speaker_map[label] = f"Спикер {i}"
    return speaker_map


# ── БД: спикеры ───────────────────────────────────────────────────────────────

def _load_saved_speakers(session_id: int) -> dict[str, str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT label, name FROM speakers WHERE session_id=? AND name IS NOT NULL",
            (session_id,),
        ).fetchall()
    return {r["label"]: r["name"] for r in rows}


def _save_speakers(session_id: int, speaker_map: dict[str, str]) -> None:
    with get_conn() as conn:
        for label, name in speaker_map.items():
            existing = conn.execute(
                "SELECT id FROM speakers WHERE session_id=? AND label=?",
                (session_id, label),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE speakers SET name=? WHERE id=?",
                    (name, existing["id"]),
                )
            else:
                conn.execute(
                    "INSERT INTO speakers (label, name, session_id) VALUES (?,?,?)",
                    (label, name, session_id),
                )


# ── Главная функция ───────────────────────────────────────────────────────────

def run_transcription(
    job_id: int,
    on_progress: Optional[Callable[[str, str], None]] = None,
) -> Optional[Path]:
    """
    Запускает полный пайплайн Этапа 4 для задания job_id.

    Returns:
        Path к transcription.md или None при ошибке.
    """
    # Загружаем задание и сессию
    with get_conn() as conn:
        job = conn.execute(
            "SELECT * FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        if not job:
            raise ValueError(f"Job {job_id} not found")

        session = conn.execute(
            "SELECT * FROM sessions WHERE id=?", (job["session_id"],)
        ).fetchone()
        if not session:
            raise ValueError(f"Session {job['session_id']} not found")

    session_id = session["id"]
    title = session["title"] or "Встреча"
    agenda = session["agenda"] or ""
    started_at = session["started_at"] or ""

    # Путь к записям
    session_dir = config.RECORDINGS_DIR / f"session_{session_id}"
    doc_paths = file_manager.get_doc_paths(title, started_at)

    def _progress(stage: str, detail: str = "") -> None:
        if on_progress:
            on_progress(stage, detail)

    # ── Этап 2: транскрипция + диаризация ────────────────────────────────────
    job = dict(job)  # sqlite3.Row → dict для .get()

    if job.get("transcription_path") and Path(job["transcription_path"]).exists():
        # Уже сделано — пропускаем
        pass
    else:
        _set_job_status(job_id, "processing")
        merged = file_manager.merge_chunks(session_dir)
        if not merged:
            _set_job_status(job_id, "error", "Нет аудиофайлов в сессии")
            _progress("error", "Нет аудиофайлов")
            return None

        _progress("transcribing")
        backend = get_backend()
        transcription = backend.transcribe(merged)

        _progress("diarizing")
        diarizer = PyannoteDiarizer()
        diarization = diarizer.diarize(merged)

        _progress("aligning")
        aligned = _assign_speakers(transcription, diarization)

        saved_names = _load_saved_speakers(session_id)
        detected_names = _detect_names(aligned)
        speaker_map = _build_speaker_map(aligned, detected_names, saved_names)
        _save_speakers(session_id, speaker_map)

        file_manager.write_transcription_md(
            path=doc_paths["transcription"],
            title=title,
            started_at=started_at,
            agenda=agenda,
            duration=transcription.duration,
            speaker_map=speaker_map,
            segments=aligned,
        )
        with get_conn() as conn:
            conn.execute(
                "UPDATE jobs SET transcription_path=?, status='transcribed', "
                "updated_at=datetime('now') WHERE id=?",
                (str(doc_paths["transcription"]), job_id),
            )

    # ── Этап 5а: анализ ───────────────────────────────────────────────────────
    if job.get("analysis_path") and Path(job["analysis_path"]).exists():
        pass
    else:
        _progress("analysis")
        from app.processing.analysis import write_analysis_md
        write_analysis_md(
            path=doc_paths["analysis"],
            title=title,
            started_at=started_at,
            agenda=agenda,
            transcription_path=doc_paths["transcription"],
            prompt_path=doc_paths["analysis_prompt"],
        )
        with get_conn() as conn:
            conn.execute(
                "UPDATE jobs SET analysis_path=?, updated_at=datetime('now') WHERE id=?",
                (str(doc_paths["analysis"]), job_id),
            )

    # ── Этап 5б: follow-up ────────────────────────────────────────────────────
    if job.get("followup_path") and Path(job["followup_path"]).exists():
        pass
    else:
        _progress("followup")
        from app.processing.followup import write_followup_md
        write_followup_md(
            path=doc_paths["followup"],
            title=title,
            started_at=started_at,
            analysis_path=doc_paths["analysis"],
            prompt_path=doc_paths["followup_prompt"],
        )
        with get_conn() as conn:
            conn.execute(
                "UPDATE jobs SET followup_path=?, updated_at=datetime('now') WHERE id=?",
                (str(doc_paths["followup"]), job_id),
            )

    _set_job_status(job_id, "done")
    _progress("done")
    return doc_paths["transcription"]


def _set_job_status(job_id: int, status: str, error: str = "") -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET status=?, error=?, updated_at=datetime('now') WHERE id=?",
            (status, error or None, job_id),
        )
