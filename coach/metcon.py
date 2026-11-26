"""메트콘(컨디셔닝) 모델: 무게 선택 · 기록 예측 · 페이싱.

시간 모델
    동작 시간 = 반복수 × 사이클시간 × 무게계수 × (1 + 휴식비율(d))
    d = 반복수 / 연속 가능 최대 반복수   (세트가 내 한계에 얼마나 가까운가)
    총 시간 = (웨이트·맨몸 구간 + 전환) × 길이계수 × exp(−복합수행능력) + 머신 구간

머신 구간은 심폐 엔진 요인이, 나머지는 복합 수행능력(met.*) 요인이 좌우한다.
"""
from __future__ import annotations

import math

from . import catalog as C
from .athlete import Athlete

INTENT_PCT = {"light": 0.47, "moderate": 0.57, "heavy": 0.67}
INTENT_CAP = {"light": 0.60, "moderate": 0.70, "heavy": 0.82}   # 볼륨이 아무리 적어도 넘지 않는 %1RM
LOADED = ("barbell", "dumbbell", "kettlebell")
PRED_NOISE = 0.10
STRESS_PER_MAXSET = 1.5      # 한계 세트 1개 분량의 볼륨 = 고강도 세트 1.5개 상당
ERG_STRESS_PER_MIN = 0.15


def loadpref_h(mid: str) -> dict[str, float]:
    """메트콘 무게 성향: 전체 + 기구별."""
    return {"loadpref": 1.0, f"loadpref.imp:{C.movement(mid)['implement']}": 1.0}


def rest_frac(d: float) -> float:
    """세트 난이도 d 에 따른 '작업 시간 대비 휴식 시간' 비율."""
    if d <= 0.2:
        return 0.0
    return min(4.0, 1.2 * (d - 0.2) ** 1.3)


def suggest_split(reps: float, d: float) -> list[int] | None:
    """큰 세트를 어떻게 끊을지 제안 (예: 21 → 8-7-6)."""
    reps = int(round(reps))
    if d <= 0.6 or reps <= 3:
        return None
    n = max(2, min(reps, math.ceil(d / 0.5), 5))
    base, rem = divmod(reps, n)
    parts = [base + (1 if i < rem else 0) for i in range(n)]
    if parts[-1] > 1 and parts[0] - parts[-1] < 2:
        parts[0] += 1
        parts[-1] -= 1
    return parts


def bucket_of(own_s: float) -> str:
    return "short" if own_s < 360 else ("medium" if own_s < 1080 else "long")


# ── 항목 해석 ────────────────────────────────────────────
def _auto_scale(athlete: Athlete, opts: list[dict]) -> dict:
    """쉬움→어려움 순으로 적힌 스케일 옵션 중, 무리 없이 가능한 가장 어려운 것."""
    for o in reversed(opts):
        mid = o["movement"]
        reps = o.get("reps") or 1
        cap = athlete.rmax(mid) if athlete.has_strength(mid) else athlete.unbroken(mid)
        if cap >= reps * 1.25:
            return o
    return opts[0]


def resolve_items(athlete: Athlete, items: list[dict], picks=None) -> list[dict]:
    picks = set(picks or [])
    out = []
    for it in items:
        if it.get("options"):
            opts = it["options"]
            chosen = next((o for o in opts if o["movement"] in picks), None)
            if chosen is None:
                chosen = _auto_scale(athlete, opts) if it.get("scale") else opts[0]
            c = dict(chosen)
            c["alts"] = [o for o in opts if o is not chosen]
            c["scaled"] = bool(it.get("scale"))
            out.append(c)
        else:
            out.append(dict(it))
    return out


def team_cfg(block: dict) -> dict:
    team = block["team"]
    size, style = team["size"], team["style"]
    scheme = block.get("rep_scheme")
    n = len(scheme) if scheme else (block.get("rounds") or 1)
    if size <= 1 or style == "solo":
        return {"own_rounds": n, "share": 1.0, "mult": 1.0, "rest": False}
    if style == "split":
        return {"own_rounds": n, "share": 1.0 / size, "mult": float(size), "rest": True}
    own = n if team["rounds_scope"] == "each" else max(1, round(n / size))
    return {"own_rounds": own, "share": 1.0, "mult": float(size), "rest": True}


def round_units(block: dict, items: list[dict], ri: int, share: float = 1.0) -> list[dict]:
    scheme = block.get("rep_scheme")
    prog = block.get("progression")
    units = []
    for idx, it in enumerate(items):
        u = {"movement": it["movement"], "idx": idx}
        has_amount = False
        for key in ("reps", "cal", "distance_m"):
            if it.get(key) is not None:
                val = it[key]
                if prog and key in ("reps", "cal"):
                    val += prog.get("add", 1) * (ri // prog.get("every_rounds", 1))
                u[key] = val * share
                has_amount = True
        if it.get("seconds") is not None:
            u["seconds"] = it["seconds"]
            has_amount = True
        if not has_amount and scheme:
            u["reps"] = scheme[min(ri, len(scheme) - 1)] * share
        if it.get("max"):
            u["max"] = True
        units.append(u)
    return units


# ── 단위 동작 시간 ───────────────────────────────────────
def _ctx(team_rest: bool) -> dict:
    return {
        "t_ref": 600.0,
        "erg_mix": 1.0 if team_rest else 0.92,
        "lam": 0.02 if team_rest else 0.06,
        "rest_scale": 0.6 if team_rest else 1.0,
    }


def unit_raw(athlete: Athlete, u: dict, ri: int, loads: dict, ctx: dict) -> dict:
    mid = u["movement"]
    m = C.movement(mid)
    res = {"movement": mid, "idx": u.get("idx"), "round": ri, "implement": m["implement"]}
    if m["implement"] == "erg":
        mod = athlete.erg_mod(mid)
        res.update(kind="erg", mod=mod)
        if "seconds" in u:
            rate = athlete.erg_rate(mod, max(u["seconds"] * 1.5, 20.0))
            res.update(t=u["seconds"], rate=rate, cal=rate * u["seconds"])
            return res
        rate = athlete.erg_rate(mod, ctx["t_ref"]) * ctx["erg_mix"]
        if "distance_m" in u:
            t = u["distance_m"] / athlete.erg_speed(mod, rate)
            res.update(t=t, rate=rate, cal=rate * t, distance_m=u["distance_m"])
        else:
            cal = u.get("cal", u.get("reps", 0.0))
            res.update(t=cal / rate, rate=rate, cal=cal)
        return res
    res["kind"] = m["cat"] if m["cat"] in ("W", "G") else "G"
    if "distance_m" in u:
        res.update(t=u["distance_m"] / m.get("speed_mps", 1.0), distance_m=u["distance_m"])
        return res
    if "seconds" in u and "reps" not in u:
        res.update(t=u["seconds"])
        return res
    reps = u.get("reps", 0.0)
    ext = loads.get(mid, 0.0)
    lf = 1.0
    if athlete.has_strength(mid):
        rmax = athlete.rmax(mid, ext)
        if m["implement"] in LOADED or m["implement"] == "machine":
            rel = ext / athlete.one_rm(mid)
            lf = 1.0 + 1.0 * max(0.0, rel - 0.4)
    else:
        rmax = athlete.unbroken(mid)
    d = reps / max(rmax, 0.5) * (1.0 + ctx["lam"] * ri)
    rest = rest_frac(d) * ctx["rest_scale"]
    res.update(t=reps * m["cycle_s"] * lf * (1.0 + rest), reps=reps, rmax=rmax, d=d,
               rest=rest, load=ext if (m["implement"] in LOADED or ext) else None)
    return res


def _transition(a: dict, b: dict) -> float:
    if a["movement"] == b["movement"]:
        return 0.0
    if a["kind"] == "erg" or b["kind"] == "erg":
        return 6.0
    if a["implement"] == b["implement"] and a["implement"] in LOADED:
        return 2.0
    return 3.0


def _round_trans(raws: list[dict], wrap: bool, team_rest: bool = False) -> float:
    """라운드 안의 동작 전환 시간. 팀 운동은 파트너가 하는 동안 미리 자리를 잡으므로 절반."""
    t = sum(_transition(a, b) for a, b in zip(raws, raws[1:]))
    if wrap and not team_rest and len(raws) > 1:
        t += _transition(raws[-1], raws[0])
    return t * (0.5 if team_rest else 1.0)


def _shares(flat: list[dict]) -> tuple[float, float, float, float, dict]:
    non = sum(x["t"] for x in flat if x["kind"] != "erg")
    w = sum(x["t"] for x in flat if x["kind"] == "W")
    g = sum(x["t"] for x in flat if x["kind"] == "G")
    erg: dict[str, float] = {}
    for x in flat:
        if x["kind"] == "erg":
            erg[x["mod"]] = erg.get(x["mod"], 0.0) + x["t"]
    sw = w / non if non > 0 else 0.0
    sg = g / non if non > 0 else 0.0
    return non, sw, sg, sum(erg.values()), erg


def _scale(athlete: Athlete, sw: float, sg: float, own_guess: float, team_rest: bool) -> tuple[float, str]:
    eff = own_guess * (0.6 if team_rest else 1.0)
    durf = 1.0 + 0.06 * math.log(1.0 + eff / 240.0)
    bucket = bucket_of(own_guess)
    return durf * math.exp(-athlete.metcon_factor(sw, sg, bucket)), bucket


def _time_h(athlete: Athlete, non_final: float, erg: dict, own: float, sw: float, sg: float,
            bucket: str) -> dict[str, float]:
    """d log(시간) / d 요인. 능력이 높을수록 시간이 줄어드므로 모두 음수."""
    h: dict[str, float] = {"cond": -1.0}
    if own <= 0:
        return h
    s_non = non_final / own
    for name, w in athlete.metcon_h(sw, sg, bucket).items():
        h[name] = h.get(name, 0.0) - s_non * w
    for mod, t in erg.items():
        for name, w in athlete.erg_h(mod).items():
            h[name] = h.get(name, 0.0) - (t / own) * w
    return h


def solve(athlete: Athlete, rounds: list[list[dict]], loads: dict, team_rest: bool) -> dict:
    """정해진 작업량을 끝내는 데 걸리는 본인 작업 시간."""
    ctx = _ctx(team_rest)
    own = 600.0
    out: dict = {}
    for _ in range(4):
        ctx["t_ref"] = max(own * (0.6 if team_rest else 1.0), 30.0)
        raws = [[unit_raw(athlete, u, ri, loads, ctx) for u in r] for ri, r in enumerate(rounds)]
        flat = [x for r in raws for x in r]
        trans = [_round_trans(r, ri < len(raws) - 1, team_rest) for ri, r in enumerate(raws)]
        non, sw, sg, ergt, erg = _shares(flat)
        scale, bucket = _scale(athlete, sw, sg, non + ergt + sum(trans), team_rest)
        non_final = (non + sum(trans)) * scale
        own = non_final + ergt
        round_s = []
        for r, tr in zip(raws, trans):
            for x in r:
                x["t_final"] = x["t"] * (scale if x["kind"] != "erg" else 1.0)
            round_s.append(sum(x["t_final"] for x in r) + tr * scale)
        out = {"own_s": own, "round_s": round_s, "raws": raws, "bucket": bucket,
               "h": _time_h(athlete, non_final, erg, own, sw, sg, bucket)}
    return out


def solve_amrap(athlete: Athlete, block: dict, items: list[dict], loads: dict, avail_s: float,
                cfg: dict) -> dict:
    """주어진 시간 동안 몇 라운드를 가는가."""
    ctx = _ctx(cfg["rest"])
    ctx["t_ref"] = max(avail_s * (0.6 if cfg["rest"] else 1.0), 30.0)
    first = [unit_raw(athlete, u, 0, loads, ctx) for u in round_units(block, items, 0, cfg["share"])]
    non, sw, sg, ergt, erg = _shares(first)
    scale, bucket = _scale(athlete, sw, sg, avail_s, cfg["rest"])
    cum, ri, raws_all, round_s = 0.0, 0, [], []
    frac, extra = 0.0, 0.0
    while ri < 400:
        raws = [unit_raw(athlete, u, ri, loads, ctx) for u in round_units(block, items, ri, cfg["share"])]
        for x in raws:
            x["t_final"] = x["t"] * (scale if x["kind"] != "erg" else 1.0)
        rt = sum(x["t_final"] for x in raws) + _round_trans(raws, True, cfg["rest"]) * scale
        if cum + rt > avail_s:
            left = avail_s - cum
            frac = left / rt
            for x in raws:                      # 마지막 라운드에서 몇 개 더 가는지
                amount = x.get("reps", x.get("cal", 0.0))
                if left >= x["t_final"]:
                    extra += amount
                    left -= x["t_final"] + 4.0 * scale
                else:
                    extra += amount * max(left, 0.0) / x["t_final"]
                    break
            raws_all.append(raws)
            break
        cum += rt
        round_s.append(rt)
        raws_all.append(raws)
        ri += 1
    non_final = (non + _round_trans(first, True, cfg["rest"])) * scale
    own_round = non_final + ergt
    return {"rounds": ri + frac, "full_rounds": ri, "extra": extra, "round_s": round_s,
            "raws": raws_all, "bucket": bucket,
            "h": _time_h(athlete, non_final, erg, own_round, sw, sg, bucket)}


# ── 무게 선택 ────────────────────────────────────────────
def choose_loads(athlete: Athlete, rounds: list[list[dict]], items: list[dict], intensity: str,
                 team_rest: bool) -> tuple[dict, dict]:
    """세트 크기·총 볼륨·의도 강도에 맞춰 무게를 고른다.

    같은 기구(바벨/덤벨/케틀벨)를 쓰는 동작들은 한 무게를 공유하고, 가장 약한 동작에 맞춘다.
    돌려주는 값: ({동작: kg}, {동작: 한계 동작 id})
    """
    fixed = {it["movement"]: it["load"] for it in items if it.get("load") is not None}
    total: dict[str, float] = {}
    set_eff: dict[str, float] = {}

    def loadable(mid: str) -> bool:
        return (athlete.has_strength(mid) and C.movement(mid)["implement"] in LOADED
                and mid not in fixed)

    for r in rounds:
        for i, u in enumerate(r):
            mid = u["movement"]
            if not loadable(mid):
                continue
            imp = C.movement(mid)["implement"]
            reps = u.get("reps") or 12.0
            others = 0.0
            for step in (-1, 1):                 # 같은 기구로 이어지는 컴플렉스
                j = i + step
                while 0 <= j < len(r) and C.movement(r[j]["movement"])["implement"] == imp:
                    if r[j]["movement"] != mid:
                        others += r[j].get("reps") or 12.0
                    j += step
            total[mid] = total.get(mid, 0.0) + reps + 0.25 * others
            set_eff[mid] = max(set_eff.get(mid, 0.0), reps + 0.5 * others)

    raw: dict[str, float] = {}
    for mid in total:
        pct = INTENT_PCT[intensity] + (0.03 if team_rest else 0.0)
        pct -= 0.08 * math.log2(max(total[mid], 1.0) / 30.0)
        pct -= 0.03 * math.log2(max(set_eff[mid], 1.0) / 9.0)
        pref, _ = athlete.b.predict(loadpref_h(mid))
        pct = min(max(pct * math.exp(pref), 0.25), INTENT_CAP[intensity])
        # 아직 기록이 없는 동작은 추정이 불확실한 만큼 보수적으로 잡는다
        raw[mid] = pct * athlete.one_rm(mid) * math.exp(-0.5 * athlete.log1rm(mid, today=False)[1])

    loads: dict[str, float] = dict(fixed)
    limiter: dict[str, str] = {}
    for imp in LOADED:
        group = [m for m in raw if C.movement(m)["implement"] == imp]
        if not group:
            continue
        lim = min(group, key=lambda m: raw[m])
        for m in group:
            loads[m] = athlete.round_load(m, raw[lim])
            limiter[m] = lim
    return loads, limiter


# ── 피로 기여 ────────────────────────────────────────────
def stress_of(athlete: Athlete, raws: list[list[dict]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for r in raws:
        for x in r:
            fmap = C.movement(x["movement"]).get("fatigue", {})
            if x["kind"] == "erg":
                amt = x["t"] / 60.0 * ERG_STRESS_PER_MIN
            elif "rmax" in x:
                amt = x["reps"] / max(x["rmax"], 1.0) * STRESS_PER_MAXSET
            else:
                amt = x["t"] / 60.0 * 0.5
            for p, w in fmap.items():
                out[p] = out.get(p, 0.0) + amt * w
    return out


# ── 항목별 안내 ──────────────────────────────────────────
def _item_views(athlete: Athlete, items: list[dict], raws: list[list[dict]], limiter: dict) -> list[dict]:
    views = []
    for idx, it in enumerate(items):
        xs = [x for r in raws for x in r if x.get("idx") == idx]
        v = {"movement": it["movement"], "item": it, "alts": it.get("alts", []),
             "scaled": it.get("scaled", False)}
        if xs:
            first, worst = xs[0], max(xs, key=lambda x: x.get("d", 0.0))
            v["t"] = first.get("t_final", first["t"])
            if first["kind"] == "erg":
                v["erg"] = {"mod": first["mod"], "rate_cal_min": first["rate"] * 60.0,
                            "speed": athlete.erg_speed(first["mod"], first["rate"]),
                            "cal": first.get("cal")}
            if "rmax" in first:
                v.update(rmax=first["rmax"], d=first["d"], d_worst=worst["d"], reps=first["reps"],
                         load=first.get("load"), split=suggest_split(first["reps"], first["d"]))
                lim = limiter.get(it["movement"])
                if lim and lim != it["movement"]:
                    v["limited_by"] = lim
        views.append(v)
    return views


def _sd(athlete: Athlete, h: dict, team: bool) -> float:
    _, var = athlete.b.predict(h)
    return math.sqrt(var + PRED_NOISE ** 2 + (0.10 ** 2 if team else 0.0))


# ── 형식별 평가 ──────────────────────────────────────────
def _eval_for_time(athlete: Athlete, block: dict, loads_in, picks) -> dict:
    cfg = team_cfg(block)
    items = resolve_items(athlete, block["items"], picks)
    rounds = [round_units(block, items, ri, cfg["share"]) for ri in range(cfg["own_rounds"])]
    loads, limiter = choose_loads(athlete, rounds, items, block["intensity"], cfg["rest"])
    loads.update(loads_in or {})
    sol = solve(athlete, rounds, loads, cfg["rest"])
    elapsed = sol["own_s"] * cfg["mult"]
    cap = block.get("time_cap_s")
    pred = {"own_s": sol["own_s"], "elapsed_s": elapsed, "round_s": sol["round_s"],
            "sd_log": _sd(athlete, sol["h"], cfg["rest"]), "own_rounds": cfg["own_rounds"],
            "capped": bool(cap and elapsed > cap), "cap_frac": min(1.0, cap / elapsed) if cap else 1.0}
    return {"format": "for_time", "block": block, "cfg": cfg, "loads": loads, "pred": pred,
            "h": sol["h"], "views": _item_views(athlete, items, sol["raws"], limiter),
            "stress": stress_of(athlete, sol["raws"]), "work_min": sol["own_s"] / 60.0}


def _eval_amrap(athlete: Athlete, block: dict, loads_in, picks) -> dict:
    cfg = team_cfg(block)
    items = resolve_items(athlete, block["items"], picks)
    avail = block["duration_s"] / cfg["mult"]
    loads: dict = {}
    limiter: dict = {}
    for _ in range(2):                           # 예상 라운드 수 → 볼륨 → 무게
        probe = solve_amrap(athlete, block, items, loads, avail, cfg)
        n = max(1, math.ceil(probe["rounds"]))
        rounds = [round_units(block, items, ri, cfg["share"]) for ri in range(n)]
        loads, limiter = choose_loads(athlete, rounds, items, block["intensity"], cfg["rest"])
    loads.update(loads_in or {})
    sol = solve_amrap(athlete, block, items, loads, avail, cfg)
    pred = {"rounds": sol["rounds"], "full_rounds": sol["full_rounds"], "extra": sol["extra"],
            "team_rounds": sol["rounds"] * (cfg["mult"] if block["team"]["style"] == "igyg" else 1.0),
            "round_s": sol["round_s"], "sd_log": _sd(athlete, sol["h"], cfg["rest"])}
    return {"format": "amrap", "block": block, "cfg": cfg, "loads": loads, "pred": pred,
            "h": sol["h"], "views": _item_views(athlete, items, sol["raws"], limiter),
            "stress": stress_of(athlete, sol["raws"]), "work_min": avail / 60.0}


def _eval_rotation(athlete: Athlete, block: dict, loads_in, picks) -> dict:
    """팀원이 스테이션을 돌아가며 맡는 AMRAP. pacer 스테이션이 끝나면 교대."""
    size = block["team"]["size"]
    dur = block["duration_s"]
    pacer = next(s for s in block["stations"] if s["pacer"])
    items = resolve_items(athlete, pacer["items"], picks)
    pblock = dict(block, rep_scheme=None, progression=None)
    n_own, loads, limiter, sol = 4, {}, {}, {}
    for _ in range(3):
        rounds = [round_units(pblock, items, ri) for ri in range(max(1, round(n_own)))]
        loads, limiter = choose_loads(athlete, rounds, items, block["intensity"], True)
        loads.update(loads_in or {})
        sol = solve(athlete, rounds, loads, True)
        t_w = sol["own_s"] / len(rounds)
        n_own = dur / (t_w * size)
    machines = []
    for st in block["stations"]:
        if st["pacer"]:
            continue
        for it in resolve_items(athlete, st["items"], picks):
            m = C.movement(it["movement"])
            if m["implement"] == "erg":
                mod = athlete.erg_mod(it["movement"])
                rate = athlete.erg_rate(mod, max(dur / size * 0.6, 60.0))
                machines.append({"movement": it["movement"], "mod": mod, "rate_cal_min": rate * 60.0,
                                 "cal_per_stint": rate * t_w, "own_cal": rate * dur / size,
                                 "speed": athlete.erg_speed(mod, rate)})
    stress = stress_of(athlete, sol["raws"])
    k = n_own / len(sol["raws"])
    stress = {p: v * k for p, v in stress.items()}
    for mc in machines:
        for p, w in C.movement(mc["movement"]).get("fatigue", {}).items():
            stress[p] = stress.get(p, 0.0) + dur / size / 60.0 * ERG_STRESS_PER_MIN * w
    pred = {"stint_s": t_w, "own_rounds": n_own, "team_rounds": dur / t_w,
            "sd_log": _sd(athlete, sol["h"], True), "machines": machines}
    return {"format": "rotation", "block": block, "loads": loads, "pred": pred, "h": sol["h"],
            "views": _item_views(athlete, items, sol["raws"], limiter), "stress": stress,
            "work_min": dur / size * (2.0 if machines else 1.0) / 60.0}


def _fit_label(ratio: float) -> str:
    return "여유" if ratio <= 0.6 else ("적정" if ratio <= 0.8 else ("빠듯" if ratio <= 0.95 else "초과"))


def _eval_emom(athlete: Athlete, block: dict, loads_in, picks) -> dict:
    items = resolve_items(athlete, block["items"], picks)
    n = block.get("rounds") or 1
    interval = block["interval_s"]
    rounds = [round_units(block, items, ri) for ri in range(n)]
    loads, limiter = choose_loads(athlete, rounds, items, block["intensity"], True)
    loads.update(loads_in or {})
    ctx = {"t_ref": 240.0, "erg_mix": 1.0, "lam": 0.03, "rest_scale": 0.6}
    raws_all, stations = [], []
    for ri in range(n):
        raws = [unit_raw(athlete, u, ri, loads, ctx) for u in rounds[ri]]
        for x in raws:
            sw, sg = (1.0, 0.0) if x["kind"] == "W" else (0.0, 1.0)
            scale = 1.0 if x["kind"] == "erg" else math.exp(-athlete.metcon_factor(sw, sg, "long"))
            x["t_final"] = x["t"] * scale
        raws_all.append(raws)
    for idx, it in enumerate(items):
        first, last = raws_all[0][idx], raws_all[-1][idx]
        st = {"movement": it["movement"], "item": it, "alts": it.get("alts", []),
              "first": first, "last": last, "load": loads.get(it["movement"]),
              "fit_first": _fit_label(first["t_final"] / interval),
              "fit_last": _fit_label(last["t_final"] / interval)}
        if first["kind"] == "erg":
            st["erg"] = {"mod": first["mod"], "rate_cal_min": first["rate"] * 60.0,
                         "speed": athlete.erg_speed(first["mod"], first["rate"])}
        if last["t_final"] > 0.8 * interval:      # 구간 안에 들어오는 최대량
            amount = last.get("reps", last.get("cal", 0.0))
            st["scale_to"] = max(1, math.floor(amount * 0.8 * interval / last["t_final"]))
        if "rmax" in first:
            st["limited_by"] = limiter.get(it["movement"]) if limiter.get(it["movement"]) != it["movement"] else None
        stations.append(st)
    work_s = sum(x["t_final"] for r in raws_all for x in r)
    return {"format": "emom", "block": block, "loads": loads, "stations": stations,
            "pred": {"total_min": n * len(items) * interval / 60.0, "work_s": work_s},
            "h": {}, "stress": stress_of(athlete, raws_all), "work_min": work_s / 60.0}


def _eval_interval(athlete: Athlete, block: dict, loads_in, picks) -> dict:
    """work_s 동안 정해진 일을 하고 남는 시간은 max 동작으로 채우는 인터벌."""
    work = block["work_s"]
    n = block.get("rounds") or 1
    secs = block["sections"] or [block]
    ctx = {"t_ref": work * 1.3, "erg_mix": 1.0, "lam": 0.03, "rest_scale": 0.6}
    all_loads: dict = {}
    out_secs, raws_all = [], []
    for sec in secs:
        items = resolve_items(athlete, sec["items"], picks)
        units = round_units(sec, items, 0)
        loads, _ = choose_loads(athlete, [units], items, sec["intensity"], True)
        loads.update(loads_in or {})
        all_loads.update(loads)
        used, lines, raws = 0.0, [], []
        for u, it in zip(units, items):
            if u.get("max"):
                continue
            x = unit_raw(athlete, u, 0, loads, ctx)
            sw, sg = (1.0, 0.0) if x["kind"] == "W" else (0.0, 1.0)
            x["t_final"] = x["t"] * (1.0 if x["kind"] == "erg" else math.exp(-athlete.metcon_factor(sw, sg, "short")))
            used += x["t_final"] + 5.0
            raws.append(x)
            lines.append({"movement": it["movement"], "item": it, "x": x, "load": loads.get(it["movement"])})
        remain = max(work - used, 0.0)
        for u, it in zip(units, items):
            if not u.get("max"):
                continue
            mid = it["movement"]
            if C.movement(mid)["implement"] == "erg":
                mod = athlete.erg_mod(mid)
                rate = athlete.erg_rate(mod, max(work * 1.3, 30.0))
                x = {"movement": mid, "kind": "erg", "mod": mod, "rate": rate, "t": remain,
                     "t_final": remain, "cal": rate * remain}
                score = rate * remain
            else:
                reps, x = 0, None
                while reps < 300:                 # 남은 시간에 들어가는 최대 반복 수
                    trial = unit_raw(athlete, {"movement": mid, "reps": reps + 1.0}, 0, loads, ctx)
                    sw, sg = (1.0, 0.0) if trial["kind"] == "W" else (0.0, 1.0)
                    trial["t_final"] = trial["t"] * math.exp(-athlete.metcon_factor(sw, sg, "short"))
                    if trial["t_final"] > remain:
                        break
                    reps, x = reps + 1, trial
                score = float(reps)
            if x:
                raws.append(x)
            lines.append({"movement": mid, "item": it, "x": x, "max": True, "score": score,
                          "remain_s": remain, "load": loads.get(mid)})
        raws_all += [raws] * n
        out_secs.append({"name": sec.get("name"), "lines": lines, "fixed_s": used})
    total = len(secs) * n * (work + (block.get("rest_s") or 0.0))
    return {"format": "interval", "block": block, "loads": all_loads, "sections": out_secs,
            "pred": {"total_min": total / 60.0}, "h": {},
            "stress": stress_of(athlete, raws_all), "work_min": len(secs) * n * work / 60.0}


def evaluate(athlete: Athlete, block: dict, loads: dict | None = None, picks=None) -> dict:
    """메트콘 블록 하나에 대한 처방 + 예측. loads 를 주면 그 무게로 예측한다."""
    fmt = block["format"]
    if fmt == "interval":
        return _eval_interval(athlete, block, loads, picks)
    if block.get("sections"):
        parts = [evaluate(athlete, s, loads, picks) for s in block["sections"]]
        stress: dict[str, float] = {}
        merged: dict = {}
        for p in parts:
            merged.update(p["loads"])
            for k, v in p["stress"].items():
                stress[k] = stress.get(k, 0.0) + v
        return {"format": "sections", "block": block, "parts": parts, "loads": merged, "h": {},
                "stress": stress, "work_min": sum(p["work_min"] for p in parts)}
    if fmt == "for_time":
        return _eval_for_time(athlete, block, loads, picks)
    if fmt == "amrap":
        return _eval_amrap(athlete, block, loads, picks)
    if fmt == "rotation":
        return _eval_rotation(athlete, block, loads, picks)
    if fmt == "emom":
        return _eval_emom(athlete, block, loads, picks)
    raise ValueError(f"지원하지 않는 형식: {fmt}")
