"""동작 카탈로그와 수준별 기준치(사전 지식)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

LEVELS = {"beginner": 0.0, "novice": 1.0, "intermediate": 2.0, "advanced": 3.0}
LEVEL_KO = {"beginner": "입문", "novice": "초급", "intermediate": "중급", "advanced": "상급"}

# 기준 리프트 1RM / 체중 (남성, 수준 0~3). 여성은 f_mult 를 곱한다.
ANCHORS: dict[str, dict] = {
    "back_squat": {"m": [0.80, 1.10, 1.45, 1.85], "f_mult": 0.78},
    "deadlift": {"m": [1.00, 1.35, 1.80, 2.20], "f_mult": 0.80},
    "bench_press": {"m": [0.60, 0.85, 1.15, 1.45], "f_mult": 0.62},
    "strict_press": {"m": [0.40, 0.55, 0.70, 0.88], "f_mult": 0.62},
    "barbell_row": {"m": [0.55, 0.75, 0.95, 1.20], "f_mult": 0.68},
    "clean_and_jerk": {"m": [0.45, 0.70, 0.95, 1.25], "f_mult": 0.75},
    # 풀업은 (체중 + 추가중량) 기준 1RM / 체중
    "pull_up": {"m": [0.95, 1.15, 1.40, 1.65], "f": [0.80, 0.98, 1.20, 1.42]},
}

# 로잉 기준 지속 가능 출력(cal/min) — 체중 보정 전
ENGINE_CP = {"m": [10.0, 12.5, 15.5, 18.5], "f": [7.5, 9.5, 11.5, 14.0]}
ENGINE_REF_BW = {"m": 80.0, "f": 62.0}
# 로잉 대비 머신별 칼로리 적립 속도
ERG_RATIO = {"row": 1.0, "ski": 0.95, "bike": 1.0, "assault": 1.05, "run": 0.80}
ERG_KO = {"row": "로잉", "ski": "스키", "bike": "바이크", "assault": "어썰트 바이크", "run": "러닝"}
# 러닝 임계 속도(m/s)
RUN_SPEED = {"m": [2.5, 2.9, 3.4, 4.0], "f": [2.3, 2.7, 3.1, 3.6]}
# 메트콘 페이스의 수준별 기본값(로그): 음수 = 느림
METCON_BASE = [-0.18, -0.02, 0.13, 0.25]
# 컨디셔닝 동작 연속 반복 능력의 수준별 배수
UNBROKEN_MULT = [0.5, 0.75, 1.0, 1.4]

PATTERN_KO = {
    "squat": "스쿼트", "hinge": "힌지(후면사슬)", "lunge": "런지/편측", "push_h": "수평 밀기(가슴)",
    "push_v": "수직 밀기(어깨)", "pull_h": "수평 당기기(등)", "pull_v": "수직 당기기(등)",
    "olympic": "올림픽/파워", "core": "코어", "plyo": "점프/버피",
}
PATTERNS = list(PATTERN_KO)


def interp(table: list[float], level: float) -> float:
    level = min(max(level, 0.0), len(table) - 1.0)
    lo = int(level)
    hi = min(lo + 1, len(table) - 1)
    t = level - lo
    return table[lo] * (1 - t) + table[hi] * t


@lru_cache(maxsize=1)
def movements() -> dict[str, dict]:
    path = Path(__file__).parent / "data" / "movements.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    for mid, m in data.items():
        m["id"] = mid
        m.setdefault("hands", 2 if m["implement"] == "dumbbell" else 1)
        m.setdefault("bw_frac", 0.0)
        if "fatigue" not in m and "pattern" in m:
            m["fatigue"] = {m["pattern"]: 1.0}
        if "ref" in m and m["ref"] not in ANCHORS:
            raise ValueError(f"{mid}: 기준 리프트 {m['ref']} 가 없습니다")
    return data


def movement(mid: str) -> dict:
    cat = movements()
    if mid not in cat:
        raise KeyError(f"카탈로그에 없는 동작: {mid} (coach/data/movements.yaml 에 추가하세요)")
    return cat[mid]


def ko(mid: str) -> str:
    return movement(mid).get("ko", mid)
