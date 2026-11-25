"""상태 재구성: 프로필(사전 지식) + 기록 전체를 날짜순으로 다시 재생한다.

상태를 따로 저장해 두고 덧칠하는 대신 매번 처음부터 재생하므로,
지난 기록을 고치거나 모델을 바꿔도 항상 일관된 상태가 나온다.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import yaml

from .athlete import Athlete
from .feedback import apply_checkin, apply_erg, apply_session, apply_strength
from .workout import find_day


def _as_date(v) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v))


def load_profile(root: Path) -> dict:
    path = Path(root) / "data" / "athlete" / "profile.yaml"
    if not path.exists():
        raise FileNotFoundError(f"프로필이 없습니다: {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_logs(root: Path) -> dict[date, dict]:
    logs = {}
    for path in sorted((Path(root) / "data" / "logs").glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        logs[_as_date(raw.get("date", path.stem))] = raw
    return logs


def build(root: Path, until: date | None = None, skip_sessions_on: date | None = None) -> Athlete:
    """until 날짜(포함)까지의 기록을 반영한 선수 상태.

    skip_sessions_on: 그 날짜의 세션 기록만 빼고 본다 (리뷰에서 '운동 전' 상태를 만들 때).
    """
    root = Path(root)
    athlete = Athlete(load_profile(root))
    logs = load_logs(root)
    marks = list(logs)
    dated = [_as_date(b["date"]) for b in athlete.profile["benchmarks"] if b.get("date")]
    first = min(marks + dated + [until or date.today()])
    benches: dict[date, list[dict]] = {}
    for b in athlete.profile["benchmarks"]:
        d = _as_date(b["date"]) if b.get("date") else first - timedelta(days=7)
        benches.setdefault(d, []).append(b)

    for day in sorted(set(marks) | set(benches)):
        if until and day > until:
            break
        athlete.advance_to(day)
        log = logs.get(day) or {}
        if log.get("checkin"):
            apply_checkin(athlete, log["checkin"])
        for b in benches.get(day, []):
            if "erg" in b:
                apply_erg(athlete, b, tag="benchmark")
            else:
                apply_strength(athlete, b, fatigue=False, tag="benchmark")
        if day == skip_sessions_on:
            continue
        workout = find_day(root, day, athlete.sex)
        for slog in log.get("sessions") or []:
            apply_session(athlete, day, workout, slog)
    if until:
        athlete.advance_to(until)
    return athlete
