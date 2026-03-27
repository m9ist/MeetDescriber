"""
Именование файлов и генерация Markdown-документов.

Формат имени: YYYY-MM-DD_[название]_[тип].md
"""
from __future__ import annotations

import re
import wave
from datetime import datetime
from pathlib import Path
from typing import Optional

import config


# ── Пути ─────────────────────────────────────────────────────────────────────

def _safe_name(title: str) -> str:
    """Превращает произвольный заголовок в безопасное имя файла."""
    s = re.sub(r'[\\/:*?"<>|]', "_", title or "без_названия")
    s = re.sub(r"\s+", "_", s.strip())
    return s[:60]


def get_doc_paths(title: str, started_at: str) -> dict[str, Path]:
    """
    Возвращает пути для трёх документов сессии.

    Returns:
        {"transcription": Path, "analysis": Path, "followup": Path}
    """
    config.ensure_dirs()
    date = (started_at or "")[:10] or datetime.now().strftime("%Y-%m-%d")
    name = _safe_name(title)
    base = config.DOCUMENTS_DIR / f"{date}_{name}"
    return {
        "transcription": Path(f"{base}_transcription.md"),
        "analysis":      Path(f"{base}_analysis.md"),
        "followup":      Path(f"{base}_followup.md"),
    }


# ── Конкатенация чанков ───────────────────────────────────────────────────────

def merge_chunks(session_dir: Path) -> Optional[Path]:
    """
    Объединяет все chunk_NNNN.wav из session_dir в один merged.wav.
    Возвращает путь к merged.wav или None если чанков нет.
    """
    chunks = sorted(session_dir.glob("chunk_*.wav"))
    if not chunks:
        return None

    merged_path = session_dir / "merged.wav"
    if merged_path.exists():
        return merged_path

    # Читаем параметры из первого чанка
    with wave.open(str(chunks[0]), "rb") as wf:
        params = wf.getparams()

    with wave.open(str(merged_path), "wb") as out:
        out.setparams(params)
        for chunk in chunks:
            with wave.open(str(chunk), "rb") as wf:
                out.writeframes(wf.readframes(wf.getnframes()))

    return merged_path


# ── Генерация Markdown ────────────────────────────────────────────────────────

def _fmt_time(seconds: float) -> str:
    """00:01:23"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _fmt_duration(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h:
        return f"{h}ч {m}мин"
    return f"{m}мин {s}сек"


def write_transcription_md(
    path: Path,
    title: str,
    started_at: str,
    agenda: str,
    duration: float,
    speaker_map: dict[str, str],   # SPEAKER_00 → "Иван" или "Спикер 1"
    segments: list[dict],           # [{"start", "end", "text", "speaker", "confidence"}]
) -> None:
    """
    Генерирует _transcription.md.

    segments — уже выровненные (транскрипция + диаризация).
    """
    date = (started_at or "")[:10]
    participants = sorted(set(speaker_map.values()))

    lines = [
        f"# {title or 'Встреча'}",
        "",
        f"**Дата:** {date}",
        f"**Длительность:** {_fmt_duration(duration)}",
        f"**Участники:** {', '.join(participants) if participants else 'не определены'}",
        "",
    ]

    if agenda and agenda.strip():
        lines += [
            "## Агенда",
            "",
            agenda.strip(),
            "",
        ]

    lines += ["---", "", "## Расшифровка", ""]

    prev_speaker = None
    for seg in segments:
        speaker_label = seg.get("speaker", "")
        speaker_name = speaker_map.get(speaker_label, speaker_label or "?")
        ts = _fmt_time(seg["start"])
        text = seg["text"].strip()
        conf = seg.get("confidence", 1.0)

        if not text:
            continue

        # Разделитель при смене спикера
        if speaker_name != prev_speaker and prev_speaker is not None:
            lines.append("")

        # Метка качества если низкое
        quality_mark = " ⚠" if conf < config.QUALITY_THRESHOLD else ""

        lines.append(f"`[{ts}]` **{speaker_name}:**{quality_mark} {text}")
        prev_speaker = speaker_name

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
