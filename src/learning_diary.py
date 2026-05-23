"""AI 학습 일지 — 매 학습 세션의 변경 사항을 구조화된 JSON으로 기록.

Market Learning이 일 3회(pre/post/post_us) 실행될 때마다
어떤 파라미터가 어떻게 변경되었는지, 어떤 판단이 내려졌는지를 기록한다.
journal.py에서 이 데이터를 읽어 일일 저널에 학습 상세 섹션을 생성한다.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

DIARY_PATH = Path("logs/learning_diary.json")
MAX_ENTRIES = 180  # ~60일 × 3회


def _load_diary() -> list[dict]:
    if DIARY_PATH.exists():
        try:
            with DIARY_PATH.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            return []
    return []


def _save_diary(entries: list[dict]) -> None:
    DIARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    entries = entries[-MAX_ENTRIES:]
    with DIARY_PATH.open("w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)


class LearningDiary:
    """하나의 학습 세션 동안 변경 사항을 수집하고 저장하는 컨텍스트 매니저."""

    def __init__(self, phase: str):
        self.phase = phase
        self.timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.date = datetime.now().strftime("%Y-%m-%d")
        self.changes: list[dict] = []
        self.metrics: dict = {}
        self.decisions: list[str] = []
        self.errors: list[str] = []

    def record_change(self, category: str, param: str, old_value, new_value, reason: str = "") -> None:
        if old_value == new_value:
            return
        self.changes.append({
            "category": category,
            "param": param,
            "old": _serialize(old_value),
            "new": _serialize(new_value),
            "reason": reason,
        })

    def record_metric(self, name: str, value) -> None:
        self.metrics[name] = _serialize(value)

    def record_decision(self, text: str) -> None:
        self.decisions.append(text)

    def record_error(self, text: str) -> None:
        self.errors.append(text)

    def save(self) -> dict:
        entry = {
            "date": self.date,
            "timestamp": self.timestamp,
            "phase": self.phase,
            "changes": self.changes,
            "metrics": self.metrics,
            "decisions": self.decisions,
            "errors": self.errors,
        }
        entries = _load_diary()
        entries.append(entry)
        _save_diary(entries)
        return entry

    def summary_lines(self) -> list[str]:
        lines = []
        phase_kr = {"pre": "장전", "post": "장후", "post_us": "미국장후"}.get(self.phase, self.phase)
        lines.append(f"**[{phase_kr} {self.timestamp[-8:]}]**")

        if self.changes:
            for c in self.changes:
                reason = f" ({c['reason']})" if c.get("reason") else ""
                lines.append(f"  - `{c['param']}`: {c['old']} → {c['new']}{reason}")

        if self.decisions:
            for d in self.decisions:
                lines.append(f"  - {d}")

        if self.errors:
            for e in self.errors:
                lines.append(f"  - ⚠ {e}")

        if self.metrics:
            metric_parts = [f"{k}={v}" for k, v in self.metrics.items()]
            lines.append(f"  - 지표: {', '.join(metric_parts)}")

        if not self.changes and not self.decisions and not self.errors:
            lines.append("  - 변경 사항 없음")

        return lines


def get_today_diary() -> list[dict]:
    today = datetime.now().strftime("%Y-%m-%d")
    entries = _load_diary()
    return [e for e in entries if e.get("date") == today]


def format_diary_for_journal() -> str:
    today_entries = get_today_diary()
    if not today_entries:
        return ""

    lines = ["## AI 학습 일지", ""]

    for entry in today_entries:
        diary = LearningDiary.__new__(LearningDiary)
        diary.phase = entry["phase"]
        diary.timestamp = entry["timestamp"]
        diary.changes = entry.get("changes", [])
        diary.metrics = entry.get("metrics", {})
        diary.decisions = entry.get("decisions", [])
        diary.errors = entry.get("errors", [])
        lines.extend(diary.summary_lines())
        lines.append("")

    total_changes = sum(len(e.get("changes", [])) for e in today_entries)
    total_errors = sum(len(e.get("errors", [])) for e in today_entries)
    lines.append(f"**요약**: {len(today_entries)}회 학습, "
                 f"{total_changes}건 파라미터 변경, "
                 f"{total_errors}건 오류")
    lines.append("")

    return "\n".join(lines)


def _serialize(v):
    if isinstance(v, float):
        return round(v, 6)
    if isinstance(v, (list, dict)):
        try:
            json.dumps(v)
            return v
        except (TypeError, ValueError):
            return str(v)
    return v
