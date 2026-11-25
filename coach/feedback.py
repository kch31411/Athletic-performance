"""운동 결과(피드백) → 능력치 갱신 + 피로 누적.

기록 YAML 형식은 docs/schema.md 참고.
"""
from __future__ import annotations

import math
from datetime import date

from . import catalog as C
from .athlete import Athlete
from .metcon import evaluate
from .strength import set_fatigue

FEEL_RIR = {"easy": 4.0, "good": 2.0, "hard": 0.5, "fail": 0.0}
REGIONS = {
    "legs": ["squat", "hinge", "lunge"], "하체": ["squat", "hinge", "lunge"],
    "chest": ["push_h"], "가슴": ["push_h"],
    "shoulders": ["push_v"], "어깨": ["push_v"],
    "back": ["pull_h", "pull_v"], "등": ["pull_h", "pull_v"],
    "upper": ["push_h", "push_v", "pull_h", "pull_v"], "상체": ["push_h", "push_v", "pull_h", "pull_v"],
    "core": ["core"], "코어": ["core"],
}


class LogError(ValueError):
    pass


def parse_time(v) -> float:
    """'12:34' 또는 초 단위 숫자 → 초. (YAML 은 12:34 를 60진수 정수로 읽으므로 그대로 초가 된다)"""
    if isinstance(v, (int, float)):
        return float(v)
    parts = [float(x) for x in str(v).split(":")]
    return sum(x * 60 ** i for i, x in enumerate(reversed(parts)))


def _rir(entry: dict) -> tuple[float | None, bool]:
    """(RIR, 느낌에서 추정했는지)."""
    if entry.get("fail"):
        return 0.0, False
    if entry.get("rpe") is not None:
        return max(0.0, 10.0 - float(entry["rpe"])), False
    if entry.get("feel") is not None:
        if entry["feel"] not in FEEL_RIR:
            raise LogError(f"feel 은 {sorted(FEEL_RIR)} 중 하나: {entry['feel']}")
        return FEEL_RIR[entry["feel"]], True
    return None, False


def normalize_sets(res: dict) -> list[dict]:
    raw = res.get("sets")
    if isinstance(raw, list):
        sets = [dict(s) for s in raw]
    else:
        n = int(raw or 1)
        sets = [{"load": res.get("load", 0.0), "reps": res.get("reps")} for _ in range(n)]
    if not sets:
        raise LogError(f"세트 기록이 없습니다: {res}")
    last = sets[-1]
    for key in ("rpe", "feel", "fail"):               # 동작 전체에 적은 RPE 는 마지막 세트 것
        if res.get(key) is not None and last.get(key) is None:
            last[key] = res[key]
    out = []
    for s in sets:
        if s.get("reps") is None:
            raise LogError(f"reps 가 없습니다: {res}")
        rir, from_feel = _rir(s)
        out.append({"load": float(s.get("load", res.get("load", 0.0)) or 0.0),
                    "reps": float(s["reps"]), "rir": rir, "from_feel": from_feel})
    return out


def apply_strength(athlete: Athlete, res: dict, fatigue: bool = True, tag: str = "") -> dict | None:
    mid = res["movement"]
    m = C.movement(mid)
    sets = normalize_sets(res)
    event = None
    if athlete.has_strength(mid):
        k = athlete.k(mid)
        best = None
        for i, s in enumerate(sets):
            if s["rir"] is None:
                continue
            total = athlete.bw_load(mid) + s["load"]
            if total <= 0 or s["reps"] < 1:
                continue
            censored = s["rir"] > 4.0             # 너무 쉬웠던 세트는 '최소 이만큼'이라는 하한 정보
            r_eff = s["reps"] + min(s["rir"], 6.0) + set_fatigue(i)
            implied = math.log(total) + math.log(1.0 + (r_eff - 1.0) / k)
            if best is None or implied > best["implied"]:
                best = {"implied": implied, "r_eff": r_eff, "censored": censored, "set": s}
        if best:
            pred, _ = athlete.log1rm(mid, today=True)
            resid = best["implied"] - pred
            if not (best["censored"] and resid <= 0):
                a = ((best["r_eff"] - 1.0) / k) / (1.0 + (best["r_eff"] - 1.0) / k)
                h = dict(athlete.strength_h(mid))
                h["cond"] = 1.0
                for name in athlete.end_h(mid):
                    h[name] = a
                noise = 0.035 + 0.004 * max(0.0, best["r_eff"] - 5.0)
                noise += 0.03 if best["set"]["from_feel"] else 0.0
                noise += 0.02 if m["implement"] == "bodyweight" else 0.0
                noise += 0.03 if best["censored"] else 0.0
                before = athlete.one_rm(mid, today=False)
                athlete.b.observe(h, resid, noise, bound_only=best["censored"])
                athlete.count(f"str:{m['pattern']}")
                if mid not in athlete.observed:
                    athlete.observed.append(mid)
                event = {"kind": "strength", "movement": mid, "tag": tag,
                         "set": best["set"], "surprise": resid,
                         "e1rm_before": before, "e1rm_after": athlete.one_rm(mid, today=False)}
                athlete.events.append(event)
    if fatigue:
        stress = 0.0
        for s in sets:
            rpe = 10.0 - s["rir"] if s["rir"] is not None else 7.5
            stress += min(max((rpe - 5.0) / 4.0, 0.2), 1.25)
        athlete.add_fatigue({p: stress * w for p, w in m.get("fatigue", {}).items()})
    return event


def apply_erg(athlete: Athlete, res: dict, tag: str = "") -> dict | None:
    mid = res.get("erg") or res.get("movement")
    mid = {"bike": "bike_erg", "assault": "assault_bike"}.get(mid, mid)
    mod = athlete.erg_mod(mid)
    t = parse_time(res["time"]) if res.get("time") is not None else float(res["seconds"])
    run_ratio = None
    if res.get("cal") is not None:
        rate = float(res["cal"]) / t
    elif res.get("distance_m") is not None and mod == "run":
        run_ratio = float(res["distance_m"]) / t / athlete.run_speed(t)
        rate = athlete.erg_rate("run", t) * run_ratio ** (1.0 / 0.6)
    elif res.get("distance_m") is not None and mod in ("row", "ski", "bike", "assault"):
        dist = float(res["distance_m"]) / (2.0 if mod in ("bike", "assault") else 1.0)
        watts = 2.8 / (t / dist) ** 3
        rate = (3.44 * watts + 300.0) / 3600.0 * C.ERG_RATIO[mod]
    else:
        raise LogError(f"머신 기록에는 cal 또는 distance_m 이 필요: {res}")
    effort = res.get("effort", "max")
    noise = 0.05
    if effort != "max":
        rate /= 0.93
        noise = 0.08
    pred = athlete.erg_rate(mod, t)
    resid = math.log(rate / pred)
    if run_ratio is not None:                      # 러닝은 속도 비 자체를 엔진 관측으로 쓴다
        resid = math.log(run_ratio / (0.93 if effort != "max" else 1.0))
        noise += 0.02
    a = athlete.ana_sec() / max(t, 15.0)
    h = dict(athlete.erg_h(mod))
    h["cond"] = 1.0
    h["ana"] = a / (1.0 + a)
    before = athlete.erg_cp(mod, today=False) * 60.0
    athlete.b.observe(h, resid, noise)
    athlete.count("eng")
    event = {"kind": "erg", "mod": mod, "tag": tag, "surprise": resid, "seconds": t,
             "cp_before": before, "cp_after": athlete.erg_cp(mod, today=False) * 60.0}
    athlete.events.append(event)
    return event


def _rpe_adjust(actual_s: float, rpe, noise: float) -> tuple[float, float]:
    """전력을 다하지 않은 기록은 '전력이었다면'으로 보정하고 불확실성을 키운다."""
    if rpe is None:
        return actual_s, noise + 0.03
    gap = max(0.0, 9.0 - max(float(rpe), 5.0))
    return actual_s * (1.0 - 0.04 * gap), noise + 0.03 * gap


def _observe_time(athlete: Athlete, ev: dict, resid: float, noise: float, tag: str, detail: dict) -> None:
    before = athlete.b.value("met")
    athlete.b.observe(ev["h"], resid, noise)
    athlete.count("met")
    athlete.events.append({"kind": "metcon", "tag": tag, "surprise": resid,
                           "met_before": before, "met_after": athlete.b.value("met"), **detail})


def _apply_one_metcon(athlete: Athlete, block: dict, res: dict, tag: str) -> dict:
    ev = evaluate(athlete, block, loads=res.get("loads"), picks=res.get("picks"))
    fmt = ev["format"]
    team = block["team"]["size"] > 1
    noise = 0.12 + (0.10 if team else 0.0)
    rpe = res.get("rpe")
    if fmt == "for_time" and (res.get("time") is not None or res.get("rounds_done") is not None):
        pred = ev["pred"]["elapsed_s"]
        if res.get("rounds_done") is not None:    # 타임캡에 걸림
            n = len(block["rep_scheme"]) if block.get("rep_scheme") else (block.get("rounds") or 1)
            cap = parse_time(res["time"]) if res.get("time") is not None else block["time_cap_s"]
            actual = cap * n / max(float(res["rounds_done"]), 0.25)
            noise += 0.05
        else:
            actual = parse_time(res["time"])
        actual, noise = _rpe_adjust(actual, rpe, noise)
        _observe_time(athlete, ev, math.log(actual / pred), noise, tag,
                      {"format": fmt, "pred_s": pred, "actual_s": actual})
    elif fmt == "amrap" and res.get("rounds") is not None:
        per_round = sum(v.get("reps", 0.0) or (v.get("erg") or {}).get("cal") or 0.0 for v in ev["views"])
        actual = float(res["rounds"]) + (float(res.get("reps", 0.0)) / per_round if per_round else 0.0)
        pred = ev["pred"]["team_rounds"]
        eq, noise = _rpe_adjust(1.0 / max(actual, 0.1), rpe, noise)
        _observe_time(athlete, ev, math.log(eq * pred), noise, tag,
                      {"format": fmt, "pred_rounds": pred, "actual_rounds": actual})
    elif fmt == "rotation" and (res.get("team_rounds") is not None or res.get("my_rounds") is not None):
        pred = ev["pred"]["team_rounds"]
        actual = (float(res["team_rounds"]) if res.get("team_rounds") is not None
                  else float(res["my_rounds"]) * block["team"]["size"])
        eq, noise = _rpe_adjust(1.0 / max(actual, 0.1), rpe, noise)
        _observe_time(athlete, ev, math.log(eq * pred), noise, tag,
                      {"format": fmt, "pred_rounds": pred, "actual_rounds": actual})
    for erg in res.get("ergs") or []:
        apply_erg(athlete, erg, tag)
    feel = res.get("load_feel")
    if feel and feel not in ("light", "good", "heavy"):
        raise LogError(f"load_feel 은 light/good/heavy: {feel}")
    # 무게 선호 학습: 처방보다 무겁게/가볍게 들었는지 + 그 무게가 어땠는지
    chosen = 0.0
    if res.get("loads"):
        planned = evaluate(athlete, block, picks=res.get("picks"))["loads"]
        ratios = [math.log(w / planned[m]) for m, w in res["loads"].items()
                  if planned.get(m) and w > 0 and C.movement(m)["implement"] != "bodyweight"]
        chosen = sum(ratios) / len(ratios) if ratios else 0.0
    if feel or chosen:
        shift = chosen + {"light": 0.06, "good": 0.0, "heavy": -0.06, None: 0.0}[feel]
        athlete.b.observe({"loadpref": 1.0}, shift, 0.10 if feel else 0.20)
    athlete.add_fatigue(ev["stress"])
    return ev


def apply_metcon(athlete: Athlete, block: dict, res: dict, tag: str = "") -> None:
    if block["type"] != "metcon":
        raise LogError(f"{tag}: block {res.get('block')} 은 메트콘이 아닙니다")
    if block.get("sections") and block["format"] != "interval":
        subs = res.get("sections")
        if subs:                                   # 섹션별 기록
            common = {k: res[k] for k in ("loads", "picks", "rpe", "load_feel") if k in res}
            for sec, sub in zip(block["sections"], subs):
                _apply_one_metcon(athlete, sec, {**common, **(sub or {})}, tag)
            return
        for sec in block["sections"]:
            _apply_one_metcon(athlete, sec, {k: v for k, v in res.items()
                                             if k in ("loads", "picks", "load_feel")}, tag)
        return
    _apply_one_metcon(athlete, block, res, tag)


def apply_checkin(athlete: Athlete, checkin: dict) -> None:
    """운동 전 컨디션 체크: readiness(1~5), sleep_h, soreness{부위: 0~3}."""
    adj = 0.0
    if checkin.get("readiness") is not None:
        adj += (float(checkin["readiness"]) - 3.0) * 0.015
    sleep = checkin.get("sleep_h")
    if sleep is not None:
        adj -= 0.01 * (float(sleep) < 6) + 0.01 * (float(sleep) < 5)
    athlete.b.reset("cond", mean=adj)
    for region, score in (checkin.get("soreness") or {}).items():
        pats = REGIONS.get(region, [region])
        for p in pats:
            if p not in athlete.fatigue:
                raise LogError(f"알 수 없는 부위: {region}")
            athlete.fatigue[p] = max(athlete.fatigue[p], float(score) * 1.5)


def apply_session(athlete: Athlete, day: date, workout: dict | None, slog: dict) -> None:
    sid = slog.get("id", "?")
    tag = f"{day.isoformat()}/{sid}"
    session = None
    if workout:
        session = next((s for s in workout["sessions"] if s["id"] == sid), None)
    for res in slog.get("results") or []:
        if "block" in res:
            if not session:
                raise LogError(f"{tag}: 메트콘 기록을 해석할 운동 프로그램이 없습니다")
            idx = int(res["block"]) - 1
            if not 0 <= idx < len(session["blocks"]):
                raise LogError(f"{tag}: block {res['block']} 이 없습니다")
            apply_metcon(athlete, session["blocks"][idx], res, tag)
        elif "erg" in res:
            apply_erg(athlete, res, tag)
        elif "movement" in res:
            if C.movement(res["movement"])["implement"] == "erg":
                apply_erg(athlete, res, tag)
            else:
                apply_strength(athlete, res, tag=tag)
        else:
            raise LogError(f"{tag}: 해석할 수 없는 기록: {res}")
    srpe = float(slog.get("srpe") or 7.0)
    minutes = float(slog.get("duration_min") or 50.0)
    athlete.add_fatigue({}, systemic=srpe * minutes / 300.0)
    athlete.daily_load[day] = athlete.daily_load.get(day, 0.0) + srpe * minutes
