"""근력(웨이트) 블록 처방.

목표 무게 = 오늘의 1RM × %1RM(목표 반복수 + 남길 반복수(RIR) + 세트 누적 피로) → 장비 단위로 반올림
목표 RPE 는 '마지막 세트' 기준이다. 같은 무게로 여러 세트를 하면 뒤로 갈수록 힘들어지므로
세트당 0.3회분의 여유를 미리 깔아 둔다 (피드백 해석에도 같은 규칙을 쓴다).
"""
from __future__ import annotations

import math

from . import catalog as C
from .athlete import Athlete

RIR_TARGET = {"main": 1.5, "strength": 2.0, "volume": 2.0, "hyfit": 2.5}
DEFAULT_REPS = 12.0
LOADABLE = ("barbell", "dumbbell", "kettlebell", "machine")


def set_fatigue(set_index: int) -> float:
    """i번째 세트(0부터)에 누적된 피로를 '반복수'로 환산."""
    return min(0.3 * set_index, 1.2)


def confidence(sd: float) -> str:
    return "높음" if sd < 0.05 else ("보통" if sd < 0.10 else "낮음")


def plan_item(athlete: Athlete, item: dict, intent: str) -> dict:
    mid = item["movement"]
    m = C.movement(mid)
    lo, hi = item["reps_lo"], item["reps_hi"]
    assumed_reps = lo is None
    if assumed_reps:
        lo = hi = DEFAULT_REPS
    reps = hi if lo == hi else math.floor((lo + hi) / 2 + 0.5)
    main = item["main"] or (intent == "strength" and reps <= 5)
    if item["rpe"]:
        rir = max(0.0, 10.0 - item["rpe"])
    else:
        rir = 1.0 if item["drop"] else RIR_TARGET["main" if main else intent]
    sets = item["sets"] or (5 if main else 4)
    out = {"movement": mid, "reps_lo": lo, "reps_hi": hi, "reps": reps, "sets": sets,
           "rpe": 10.0 - rir, "main": main, "per_side": item["per_side"], "drop": item["drop"],
           "superset": item["superset"], "note": item.get("note"), "assumed_reps": assumed_reps,
           "implement": m["implement"], "hands": m["hands"]}
    if not athlete.has_strength(mid):
        out["mode"] = "conditioning"
        out["unbroken"] = athlete.unbroken(mid)
        return out

    r_eff = reps + rir + set_fatigue(sets - 1)
    ext = athlete.load_for(mid, r_eff)
    _, sd = athlete.log1rm(mid, today=False)
    out.update(e1rm=athlete.one_rm(mid, today=False), e1rm_today=athlete.one_rm(mid), sd=sd,
               confidence=confidence(sd), penalty=athlete.fatigue_penalty(mid))
    spread = max(sd, 0.025)

    if m["implement"] in LOADABLE:
        w = athlete.round_load(mid, ext)
        out.update(mode="load", load=w,
                   load_lo=athlete.round_load(mid, ext * math.exp(-spread)),
                   load_hi=athlete.round_load(mid, ext * math.exp(spread)))
        if main and m["implement"] == "barbell":
            ramp = [(athlete.round_load(mid, w * f), r) for f, r in ((0.5, 5), (0.7, 3), (0.85, 2))]
            out["warmup"] = [(x, r) for x, r in dict(ramp).items() if x < w]
        if item["drop"]:
            out["drops"] = [athlete.round_load(mid, w * 0.8), athlete.round_load(mid, w * 0.64)]
        return out

    # 맨몸 근력 동작: 중량 추가 / 맨몸 / 보조 중에서 고른다
    rmax = athlete.rmax(mid)
    out["rmax"] = rmax
    if ext >= 5.0 and m.get("weightable"):
        out.update(mode="weighted", load=athlete.round_load(mid, ext))
    elif rmax >= reps + 0.5 * rir:
        out["mode"] = "bodyweight"
    else:
        out.update(mode="assist", assist=max(5.0, round(-ext / 5.0) * 5.0),
                   chunk=max(1, math.floor(rmax * 0.7)))
    return out


def plan_block(athlete: Athlete, block: dict) -> list[dict]:
    return [plan_item(athlete, it, block["intent"]) for it in block["items"]]
