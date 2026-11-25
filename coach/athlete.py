"""선수 모델: 프로필 + 능력치 믿음 + 피로 상태 → 오늘 낼 수 있는 퍼포먼스."""
from __future__ import annotations

import math
from datetime import date

from . import catalog as C
from .belief import Belief

K0 = 28.0            # 반복수-강도 곡선 기본 기울기: %1RM = 1 / (1 + (R-1)/k)
ANA_SEC = 30.0       # 무산소 여유(초): rate(T) = CP × (1 + A/T)
TAU_LOCAL = 1.6      # 국소 피로 감쇠 시간상수(일)
TAU_SYS = 1.2        # 전신 피로 감쇠 시간상수(일)
FATIGUE_C = 0.012    # 국소 피로 1단위당 근력 저하(로그)
SYS_C_STRENGTH = 0.008
SYS_C_ENGINE = 0.02
MAX_PENALTY = 0.12
LB = 0.45359237
LONG_T = 1200.0      # 이보다 긴 지속 운동은 T^-0.06 으로 출력이 떨어진다(리겔 공식 근사)

DEFAULT_EQUIPMENT = {
    "barbell_unit": "kg",          # kg | lb (lb 면 45lb 바, 5lb 단위로 처방)
    "barbell_kg": 20.0,
    "barbell_step_kg": 2.5,
    "dumbbells_kg": [2, 3, 4, 5, 6, 7, 8, 9, 10, 12.5, 15, 17.5, 20, 22.5, 25, 27.5, 30, 32.5, 35, 40],
    "kettlebells_kg": [4, 6, 8, 10, 12, 14, 16, 18, 20, 24, 28, 32],
}


def normalize_profile(raw: dict) -> dict:
    p = dict(raw or {})
    sex = str(p.get("sex", "m")).lower()[0]
    if sex not in ("m", "f"):
        raise ValueError("profile.sex 는 m 또는 f")
    p["sex"] = sex
    p["bodyweight_kg"] = float(p.get("bodyweight_kg") or (78.0 if sex == "m" else 60.0))
    lvl = p.get("level", "novice")
    p["level_num"] = float(C.LEVELS[lvl]) if isinstance(lvl, str) else float(lvl)
    p["preferred_machine"] = p.get("preferred_machine") or "row"
    eq = dict(DEFAULT_EQUIPMENT)
    if sex == "f":
        eq["barbell_kg"] = 15.0
    eq.update(p.get("equipment") or {})
    if eq["barbell_unit"] == "lb" and "barbell_kg" not in (p.get("equipment") or {}):
        eq["barbell_kg"] = (45.0 if sex == "m" else 35.0) * LB
    p["equipment"] = eq
    p["benchmarks"] = p.get("benchmarks") or []
    return p


class Athlete:
    def __init__(self, profile: dict) -> None:
        self.profile = normalize_profile(profile)
        self.b = Belief()
        self.fatigue: dict[str, float] = {p: 0.0 for p in C.PATTERNS}
        self.sys_fatigue = 0.0
        self.today: date | None = None
        self.daily_load: dict[date, float] = {}
        self.events: list[dict] = []      # 갱신 기록 (리뷰용)
        self.n_obs: dict[str, int] = {}   # 요인 그룹별 관측 수
        self.observed: list[str] = []     # 실제 기록이 들어온 동작

    # ── 기본 속성 ────────────────────────────────────────
    @property
    def sex(self) -> str:
        return self.profile["sex"]

    @property
    def bw(self) -> float:
        return self.profile["bodyweight_kg"]

    @property
    def level(self) -> float:
        return self.profile["level_num"]

    # ── 시간 경과 ────────────────────────────────────────
    def advance_to(self, day: date) -> None:
        if self.today is None:
            self.today = day
            self.b.reset("cond")
            return
        days = (day - self.today).days
        if days <= 0:
            return
        self.b.advance(days)
        for p in self.fatigue:
            self.fatigue[p] *= math.exp(-days / TAU_LOCAL)
        self.sys_fatigue *= math.exp(-days / TAU_SYS)
        self.b.reset("cond")
        self.today = day

    def add_fatigue(self, by_pattern: dict[str, float], systemic: float = 0.0) -> None:
        for p, v in by_pattern.items():
            self.fatigue[p] = self.fatigue.get(p, 0.0) + v
        self.sys_fatigue += systemic

    def acwr(self) -> float | None:
        """급성(7일):만성(28일) 부하 비. 데이터가 2주 미만이면 None."""
        if not self.daily_load or self.today is None:
            return None
        first = min(self.daily_load)
        if (self.today - first).days < 14:
            return None
        acute = sum(v for d, v in self.daily_load.items() if 0 <= (self.today - d).days < 7) / 7.0
        chronic = sum(v for d, v in self.daily_load.items() if 0 <= (self.today - d).days < 28) / 28.0
        return acute / chronic if chronic > 0 else None

    # ── 근력 ─────────────────────────────────────────────
    def has_strength(self, mid: str) -> bool:
        return "ref" in C.movement(mid)

    def anchor_1rm(self, ref: str) -> float:
        a = C.ANCHORS[ref]
        if self.sex == "f" and "f" in a:
            ratio = C.interp(a["f"], self.level)
        else:
            ratio = C.interp(a["m"], self.level) * (a.get("f_mult", 1.0) if self.sex == "f" else 1.0)
        return ratio * self.bw

    def strength_h(self, mid: str) -> dict[str, float]:
        m = C.movement(mid)
        return {"str": 1.0, f"str.pat:{m['pattern']}": 1.0, f"str.mov:{mid}": 1.0}

    def fatigue_penalty(self, mid: str) -> float:
        m = C.movement(mid)
        local = sum(w * self.fatigue.get(p, 0.0) for p, w in m.get("fatigue", {}).items())
        return min(MAX_PENALTY, FATIGUE_C * local + SYS_C_STRENGTH * self.sys_fatigue)

    def log1rm(self, mid: str, today: bool = True) -> tuple[float, float]:
        """(로그 1RM 평균, 표준편차). today=True 면 오늘 컨디션·피로 반영."""
        m = C.movement(mid)
        mean, var = self.b.predict(self.strength_h(mid))
        mean += math.log(self.anchor_1rm(m["ref"]) * m["ratio"])
        if today:
            mean += self.b.value("cond") - self.fatigue_penalty(mid)
        return mean, math.sqrt(max(var, 0.0))

    def one_rm(self, mid: str, today: bool = True) -> float:
        return math.exp(self.log1rm(mid, today)[0])

    def end_h(self, mid: str) -> dict[str, float]:
        return {"end": 1.0, f"end.pat:{C.movement(mid)['pattern']}": 1.0}

    def k(self, mid: str) -> float:
        mean, _ = self.b.predict(self.end_h(mid))
        return K0 * math.exp(mean)

    def bw_load(self, mid: str) -> float:
        return C.movement(mid)["bw_frac"] * self.bw

    def pct(self, mid: str, reps_max: float) -> float:
        """R회가 한계일 때 쓸 수 있는 %1RM."""
        return 1.0 / (1.0 + max(reps_max - 1.0, 0.0) / self.k(mid))

    def rmax(self, mid: str, ext: float = 0.0, today: bool = True) -> float:
        """외부 중량 ext 로 연속 가능한 최대 반복 수(예상)."""
        total = self.bw_load(mid) + ext
        if total <= 0:
            return 200.0
        r = 1.0 + self.k(mid) * (self.one_rm(mid, today) / total - 1.0)
        return min(max(r, 0.0), 200.0)

    def load_for(self, mid: str, reps_max: float, today: bool = True) -> float:
        """reps_max 회가 한계가 되는 외부 중량."""
        return self.one_rm(mid, today) * self.pct(mid, reps_max) - self.bw_load(mid)

    def unbroken(self, mid: str) -> float:
        m = C.movement(mid)
        return m.get("unbroken", 30) * C.interp(C.UNBROKEN_MULT, self.level)

    # ── 장비 반올림 ──────────────────────────────────────
    def round_load(self, mid: str, ext: float) -> float:
        m = C.movement(mid)
        eq = self.profile["equipment"]
        imp = m["implement"]
        if imp == "barbell":
            step = 5.0 * LB if eq["barbell_unit"] == "lb" else eq["barbell_step_kg"]
            return max(eq["barbell_kg"], round(ext / step) * step)
        if imp in ("dumbbell", "kettlebell"):
            opts = eq["dumbbells_kg" if imp == "dumbbell" else "kettlebells_kg"]
            return float(min(opts, key=lambda w: abs(w - ext)))
        if imp == "bodyweight":
            return round(ext / 2.5) * 2.5
        return max(0.0, round(ext / 2.5) * 2.5)

    # ── 심폐 엔진 ────────────────────────────────────────
    def erg_mod(self, mid: str) -> str:
        mod = C.movement(mid)["erg"]
        if mod == "preferred":
            pref = self.profile["preferred_machine"]
            mod = C.movement(pref)["erg"] if pref in C.movements() else pref
        return mod

    def erg_h(self, mod: str) -> dict[str, float]:
        return {"eng": 1.0, f"eng.mod:{mod}": 1.0}

    def erg_cp(self, mod: str, today: bool = True) -> float:
        """지속 가능 출력 (cal/초)."""
        base = C.interp(C.ENGINE_CP[self.sex], self.level)
        base *= (self.bw / C.ENGINE_REF_BW[self.sex]) ** 0.35 * C.ERG_RATIO[mod]
        mean, _ = self.b.predict(self.erg_h(mod))
        if today:
            mean += self.b.value("cond") - min(MAX_PENALTY, SYS_C_ENGINE * self.sys_fatigue)
        return base * math.exp(mean) / 60.0

    def ana_sec(self) -> float:
        return ANA_SEC * math.exp(self.b.value("ana"))

    def erg_rate(self, mod: str, duration_s: float, today: bool = True) -> float:
        """duration_s 동안 유지 가능한 최대 평균 출력 (cal/초)."""
        boost = 1.0 + self.ana_sec() / max(duration_s, 15.0)
        decay = (duration_s / LONG_T) ** -0.06 if duration_s > LONG_T else 1.0
        return self.erg_cp(mod, today) * min(boost, 2.2) * decay

    def run_speed(self, duration_s: float, today: bool = True) -> float:
        """duration_s 동안 유지 가능한 러닝 속도(m/s)."""
        return self.run_speed_from_rate(self.erg_rate("run", duration_s, today))

    def erg_speed(self, mod: str, rate_cal_s: float) -> float:
        """칼로리 적립 속도 → 이동 속도(m/s). Concept2 공식 근사."""
        if mod == "run":
            return self.run_speed_from_rate(rate_cal_s)
        watts = max((rate_cal_s / C.ERG_RATIO[mod] * 3600.0 - 300.0) / 3.44, 30.0)
        v = (watts / 2.8) ** (1.0 / 3.0)
        return v * 2.0 if mod in ("bike", "assault") else v

    def run_speed_from_rate(self, rate_cal_s: float) -> float:
        cp = self.erg_cp("run", today=False)
        base = C.interp(C.RUN_SPEED[self.sex], self.level)
        mean, _ = self.b.predict(self.erg_h("run"))
        return base * math.exp(mean) * (rate_cal_s / cp) ** 0.6

    # ── 복합 수행능력 ────────────────────────────────────
    def metcon_h(self, share_w: float, share_g: float, bucket: str) -> dict[str, float]:
        return {"met": 1.0, "met.cat:W": share_w, "met.cat:G": share_g, f"met.dur:{bucket}": 1.0}

    def metcon_factor(self, share_w: float, share_g: float, bucket: str, today: bool = True) -> float:
        mean, _ = self.b.predict(self.metcon_h(share_w, share_g, bucket))
        mean += C.interp(C.METCON_BASE, self.level)
        if today:
            mean += self.b.value("cond") - min(MAX_PENALTY, SYS_C_ENGINE * self.sys_fatigue)
        return mean

    def count(self, group: str) -> None:
        self.n_obs[group] = self.n_obs.get(group, 0) + 1
