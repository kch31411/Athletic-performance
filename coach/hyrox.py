"""HYROX 싱글 레이스 예측.

레이스 = 러닝 1km × 8 + 스테이션 8개 + 록스존(이동 구간).

각 구간 시간은 이미 있는 능력치에서 나온다.
    러닝      : 레이스 길이만큼 유지 가능한 속도(심폐 엔진) × 구간별 '다리 무거움' 계수
    스키·로잉 : 지속 가능 출력(CP)의 일정 비율로 탄다고 본다
    그 외     : 기준 선수(중급·평균 체중)의 기록 × (내 근력 대비 부하)^지수 × 복합 수행능력
거기에 구간별 개인 편차 요인(hyx.sta:<구간>, hyx.run)이 곱해지고,
스테이션 연습·시뮬레이션·실제 대회 기록이 이 요인들을 갱신한다.

기준 기록(T_REF)과 지수는 경험칙에서 출발한 사전값이다. 개인차는 기록으로 학습된다.
"""
import math

from . import catalog as C
from .athlete import Athlete

STATIONS = ["ski", "sled_push", "sled_pull", "burpee_broad_jump", "row", "farmers_carry",
            "sandbag_lunge", "wall_ball"]
STATION_KO = {"ski": "스키 1000m", "sled_push": "슬레드 푸쉬 50m", "sled_pull": "슬레드 풀 50m",
              "burpee_broad_jump": "버피 브로드 점프 80m", "row": "로잉 1000m",
              "farmers_carry": "파머스 캐리 200m", "sandbag_lunge": "샌드백 런지 100m",
              "wall_ball": "월볼 100개"}
AMOUNT = {"ski": 1000.0, "sled_push": 50.0, "sled_pull": 50.0, "burpee_broad_jump": 80.0, "row": 1000.0,
          "farmers_carry": 200.0, "sandbag_lunge": 100.0, "wall_ball": 100.0}
UNIT = {s: "reps" if s == "wall_ball" else "m" for s in STATIONS}
# 부하(kg): 슬레드는 썰매 포함 총중량, 파머스 캐리는 한 손, 샌드백·월볼은 그 무게
LOADS = {
    ("open", "m"): {"sled_push": 152, "sled_pull": 103, "farmers_carry": 24, "sandbag_lunge": 20, "wall_ball": 6},
    ("open", "f"): {"sled_push": 102, "sled_pull": 78, "farmers_carry": 16, "sandbag_lunge": 10, "wall_ball": 4},
    ("pro", "m"): {"sled_push": 202, "sled_pull": 153, "farmers_carry": 32, "sandbag_lunge": 30, "wall_ball": 9},
    ("pro", "f"): {"sled_push": 152, "sled_pull": 103, "farmers_carry": 24, "sandbag_lunge": 20, "wall_ball": 6},
}
# 기준 선수(중급, 남 80kg / 여 62kg)가 오픈 부하로 레이스 중에 내는 기록(초)
T_REF = {"sled_push": 195.0, "sled_pull": 285.0, "burpee_broad_jump": 315.0, "farmers_carry": 130.0,
         "sandbag_lunge": 285.0, "wall_ball": 390.0}
MET_W = {"sled_push": 0.5, "sled_pull": 0.5, "burpee_broad_jump": 1.0, "farmers_carry": 0.5,
         "sandbag_lunge": 0.8, "wall_ball": 1.0}
ERG_FRAC = {"ski": 0.78, "row": 0.72}            # 레이스에서 쓰는 CP 대비 파워 비율
RUN_MULT = [0.97, 1.03, 1.08, 1.09, 1.07, 1.05, 1.08, 1.10]   # 구간별 러닝 페이스 계수(직전 스테이션의 여파)
FRESH = {"sled_push": 1.05, "sled_pull": 1.08, "burpee_broad_jump": 1.10, "farmers_carry": 1.08,
         "sandbag_lunge": 1.12, "wall_ball": 1.12}   # 레이스 중 기록 / 쌩쌩할 때 단독 기록
ROXZONE_M = 1100.0                                # 이동 구간을 조깅 거리로 환산
NOISE = {"run": 0.03, "station": 0.12, "roxzone": 0.15}


def station_load(division: str, sex: str, station: str) -> float | None:
    return LOADS[(division, sex)].get(station)


def _reference(sex: str) -> Athlete:
    return Athlete({"sex": sex, "bodyweight_kg": C.ENGINE_REF_BW[sex], "level": "intermediate"})


def _demand(a: Athlete, station: str, load: float) -> tuple[float, float]:
    """(내 근력 대비 부하, 민감도 지수). 값이 클수록 느려진다."""
    one = lambda m: a.one_rm(m, today=False)
    if station == "sled_push":
        return load / (one("back_squat") + 0.5 * a.bw), 2.0
    if station == "sled_pull":
        return load / (one("barbell_row") + 0.5 * a.bw), 1.3
    if station == "farmers_carry":
        return load / one("deadlift"), 0.5
    if station == "sandbag_lunge":
        return (a.bw + load) / (one("back_squat") + a.bw), 0.7
    if station == "wall_ball":
        return load / one("thruster"), 0.6
    return a.bw, 0.3                               # 버피 브로드 점프: 가벼울수록 유리


def _erg_watts(a: Athlete, mod: str) -> tuple[float, float]:
    """(CP 파워[W], d log W / d log 칼로리출력)."""
    per_hr = a.erg_cp(mod, today=False) / C.ERG_RATIO[mod] * 3600.0
    return max((per_hr - 300.0) / 3.44, 30.0), per_hr / max(per_hr - 300.0, 1.0)


def station_time(a: Athlete, station: str, division: str = "open", load: float | None = None,
                 amount: float | None = None, effort: str = "race") -> tuple[float, dict[str, float]]:
    """스테이션 예상 시간(초)과 d log(시간)/d 요인.

    load   : 표준과 다른 무게로 연습했을 때. amount: 표준보다 짧게 했을 때(미터 또는 개수).
    effort : race = 레이스·시뮬레이션 중(피로한 상태), fresh = 쌩쌩할 때 단독 전력.
    """
    full = AMOUNT[station]
    share = (amount or full) / full
    scale = share ** 1.08 if share < 1 else share
    name = f"hyx.sta:{station}"
    own = a.b.value(name)
    if station in ERG_FRAC:
        watts, sens = _erg_watts(a, station)
        speed = (ERG_FRAC[station] * watts / 2.8) ** (1.0 / 3.0)
        h = {k: -w * sens / 3.0 for k, w in a.erg_h(station).items()}
        h[name] = -1.0
        return full / speed * scale * math.exp(-own), h
    std = station_load(division, a.sex, station)
    ref = _reference(a.sex)
    mine, power = _demand(a, station, load if load is not None else (std or 0.0))
    base, _ = _demand(ref, station, LOADS[("open", a.sex)].get(station, 0.0))
    cat = (0.0, 1.0) if station == "burpee_broad_jump" else (1.0, 0.0)
    m_rel = a.metcon_factor(cat[0], cat[1], "long", today=False) - C.METCON_BASE[2]
    w = MET_W[station]
    t = T_REF[station] * (mine / base) ** power * math.exp(-w * m_rel - own) * scale
    if station == "burpee_broad_jump" and a.sex == "f":
        t *= 1.10
    if effort == "fresh":
        t /= FRESH[station]
    h = {k: -w * v for k, v in a.metcon_h(cat[0], cat[1], "long").items()}
    h[name] = -1.0
    return t, h


def predict(a: Athlete, division: str = "open") -> dict:
    """레이스 전체 예측. 컨디션은 '잘 쉬고 나온 대회 날' 기준(누적 피로 제외)."""
    total = 5400.0
    out: dict = {}
    for _ in range(4):
        v = a.run_speed(total, today=False) * math.exp(a.b.value("hyx.run"))
        run_h = {**{k: -1.0 for k in a.erg_h("run")}, "hyx.run": -1.0}
        legs, parts = [], []                      # parts: (초, h, 잡음)
        for i, st in enumerate(STATIONS):
            rt = 1000.0 / v * RUN_MULT[i]
            legs.append({"kind": "run", "index": i + 1, "seconds": rt, "pace_s_per_km": rt})
            parts.append((rt, run_h, NOISE["run"]))
            t, h = station_time(a, st, division)
            legs.append({"kind": "station", "station": st, "seconds": t,
                         "load_kg": station_load(division, a.sex, st)})
            parts.append((t, h, NOISE["station"]))
        rox = ROXZONE_M / (0.8 * v)
        parts.append((rox, {k: -1.0 for k in a.erg_h("run")}, NOISE["roxzone"]))
        total = sum(p[0] for p in parts)
        h_total: dict[str, float] = {}
        for t, h, _ in parts:
            for k, w in h.items():
                h_total[k] = h_total.get(k, 0.0) + w * t / total
        _, var = a.b.predict(h_total)
        sd = math.sqrt(var + sum((t / total * n) ** 2 for t, _, n in parts))
        out = {"division": division, "sex": a.sex, "total_s": total, "sd_log": sd, "legs": legs,
               "roxzone_s": rox, "run_total_s": sum(l["seconds"] for l in legs if l["kind"] == "run"),
               "station_total_s": sum(l["seconds"] for l in legs if l["kind"] == "station"),
               "h": h_total}
    # 같은 종목·같은 부하에서 기준 선수(중급 평균)와 비교 → 어디서 시간을 잃고 버는지
    ref = _reference(a.sex)
    ref_total = 5400.0
    for _ in range(3):
        rv = ref.run_speed(ref_total, today=False)
        ref_station = {st: station_time(ref, st, division)[0] for st in STATIONS}
        ref_run = sum(1000.0 / rv * m for m in RUN_MULT)
        ref_total = ref_run + sum(ref_station.values()) + ROXZONE_M / (0.8 * rv)
    for leg in out["legs"]:
        if leg["kind"] == "station":
            leg["vs_reference_s"] = leg["seconds"] - ref_station[leg["station"]]
    out["reference"] = {"total_s": ref_total, "run_total_s": ref_run, "station_s": ref_station}
    out["run_vs_reference_s"] = out["run_total_s"] - ref_run
    return out


def observe_station(a: Athlete, station: str, seconds: float, division: str = "open",
                    load: float | None = None, amount: float | None = None, effort: str = "race",
                    tag: str = "") -> dict:
    """스테이션 기록 하나로 그 구간 편차(+ 관련 능력치)를 갱신."""
    pred, h = station_time(a, station, division, load, amount, effort)
    # 스테이션 하나의 기록은 그 구간의 요령·적성에 대한 증거다. 전반적인 메트콘 능력까지
    # 끌어올리지 않도록 복합 수행능력 요인은 갱신 대상에서 뺀다(머신 구간의 엔진 요인은 유지).
    h = {k: w for k, w in h.items() if not k.startswith("met")}
    h["cond"] = -1.0
    resid = math.log(seconds / pred)
    partial = amount is not None and amount < AMOUNT[station]
    a.b.observe(h, resid, 0.08 + (0.04 if partial else 0.0) + (0.03 if effort == "fresh" else 0.0))
    a.count("hyx")
    event = {"kind": "hyrox", "tag": tag, "what": station, "surprise": resid, "pred_s": pred,
             "actual_s": seconds}
    a.events.append(event)
    return event


def observe_race(a: Athlete, division: str, total_s: float | None = None,
                 run_splits: list[float] | None = None, station_splits: dict[str, float] | None = None,
                 tag: str = "") -> list[dict]:
    """대회·풀 시뮬레이션 결과. 구간 기록이 있으면 구간별로, 없으면 총 기록으로 갱신."""
    events = []
    before = predict(a, division)
    if run_splits:
        pred = before["run_total_s"] * len(run_splits) / 8.0 if len(run_splits) != 8 else before["run_total_s"]
        actual = sum(run_splits)
        resid = math.log(actual / pred)
        h = {**{k: -1.0 for k in a.erg_h("run")}, "hyx.run": -1.0, "cond": -1.0}
        a.b.observe(h, resid, 0.03)
        a.count("hyx")
        events.append({"kind": "hyrox", "tag": tag, "what": "run", "surprise": resid, "pred_s": pred,
                       "actual_s": actual})
        a.events.append(events[-1])
    for st, sec in (station_splits or {}).items():
        events.append(observe_station(a, st, sec, division, effort="race", tag=tag))
    if total_s is not None and not (run_splits or station_splits):
        resid = math.log(total_s / before["total_s"])
        a.b.observe(dict(before["h"], cond=-1.0), resid, 0.03)
        a.count("hyx")
    if total_s is not None:
        events.append({"kind": "hyrox", "tag": tag, "what": "total",
                       "surprise": math.log(total_s / before["total_s"]),
                       "pred_s": before["total_s"], "actual_s": total_s})
        a.events.append(events[-1])
    return events
