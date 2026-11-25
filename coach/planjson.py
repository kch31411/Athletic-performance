"""엔진 결과 → 기계가 읽는 JSON. (사람용 마크다운은 render.py)

모든 무게는 kg 과 lb 를 함께 싣고, 시간은 초와 "M:SS" 를 함께 싣는다.
"""
import math

from . import catalog as C
from . import render
from .athlete import LB, Athlete
from .metcon import evaluate
from .strength import plan_block

CONVENTION = {"barbell": "barbell_total_including_bar", "kettlebell": "one_kettlebell",
              "machine": "machine_stack", "bodyweight": "added_to_bodyweight", "other": "implement"}


def load_convention(mid: str) -> str | None:
    m = C.movement(mid)
    if m["implement"] == "dumbbell":
        return "each_of_two_dumbbells" if m["hands"] == 2 else "single_dumbbell"
    return CONVENTION.get(m["implement"])


def load_out(mid: str, w: float) -> dict:
    return {"kg": round(w, 1), "lb": round(w / LB), "display": render.load_text(mid, w),
            "convention": load_convention(mid)}


def t_out(s: float) -> dict:
    return {"seconds": int(round(s)), "text": render.mmss(s)}


def strength_item(p: dict) -> dict:
    mid = p["movement"]
    out = {"movement": mid, "name": C.ko(mid), "sets": p["sets"],
           "reps": {"min": int(p["reps_lo"]), "max": int(p["reps_hi"])}, "per_side": p["per_side"],
           "superset_group": p["superset"], "target_rpe_last_set": p["rpe"], "mode": p["mode"],
           "main": p["main"], "drop_set": p["drop"]}
    if p["assumed_reps"]:
        out["note"] = "reps not specified on the board; 12 assumed"
    if "e1rm" in p:
        out.update(estimated_1rm=load_out(mid, p["e1rm"]) if p["mode"] == "load"
                   else {"kg_including_bodyweight": round(p["e1rm"], 1)},
                   confidence={"높음": "high", "보통": "medium", "낮음": "low"}[p["confidence"]],
                   uncertainty_pct=round(p["sd"] * 100, 1),
                   fatigue_penalty_pct=round(p["penalty"] * 100, 1))
    if p["mode"] == "load":
        out.update(load=load_out(mid, p["load"]),
                   load_range={"low": load_out(mid, p["load_lo"]), "high": load_out(mid, p["load_hi"])})
        if p.get("warmup"):
            out["warmup"] = [{"load": load_out(mid, w), "reps": r} for w, r in p["warmup"]]
        if p.get("drops"):
            out["drop_loads"] = [load_out(mid, w) for w in p["drops"]]
    elif p["mode"] == "weighted":
        out.update(added_load=load_out(mid, p["load"]), max_unbroken_reps=round(p["rmax"], 1))
    elif p["mode"] == "bodyweight":
        out["max_unbroken_reps"] = round(p["rmax"], 1)
    elif p["mode"] == "assist":
        out.update(assistance_kg=p["assist"], or_bodyweight_chunks_of=p["chunk"],
                   max_unbroken_reps=round(p["rmax"], 1))
    return out


def _prescribed(it: dict) -> dict:
    out = {k: it[k] for k in ("reps", "cal", "distance_m", "seconds") if it.get(k) is not None}
    if it.get("max"):
        out["max_effort"] = True
    return out


def _view(v: dict) -> dict:
    mid = v["movement"]
    out = {"movement": mid, "name": C.ko(mid), "prescribed": _prescribed(v["item"])}
    if v.get("reps") is not None:
        out["my_reps_first_round"] = round(v["reps"], 1)
    if v.get("load") is not None and C.movement(mid)["implement"] != "bodyweight":
        out["load"] = load_out(mid, v["load"])
        out["load_is_prescribed_by_gym"] = v["item"].get("load") is not None
    if v.get("limited_by"):
        out["load_limited_by"] = v["limited_by"]
    if v.get("rmax") is not None:
        out["max_unbroken_reps"] = round(min(v["rmax"], 200.0), 1)
        out["suggested_split"] = v.get("split")
    if v.get("erg"):
        e = v["erg"]
        out["erg"] = {"machine": e["mod"], "cal_per_min": round(e["rate_cal_min"], 1),
                      "pace": render.pace_text(e["mod"], e["speed"]), "time": t_out(v["t"])}
    if v.get("alts"):
        out["alternatives"] = [a["movement"] for a in v["alts"]]
        out["auto_scaled"] = bool(v.get("scaled"))
    return out


def _band(center: float, sd: float) -> tuple[float, float]:
    return center * math.exp(-sd), center * math.exp(sd)


def metcon(athlete: Athlete, ev: dict) -> dict:
    block, fmt = ev["block"], ev["format"]
    if fmt == "sections":
        return {"type": "metcon", "format": block["format"], "sections": [
            dict(metcon(athlete, part), name=part["block"].get("name") or f"section {i + 1}")
            for i, part in enumerate(ev["parts"])]}
    out: dict = {"type": "metcon", "format": block["format"], "intensity": block["intensity"],
                 "team": block["team"] if block["team"]["size"] > 1 else None,
                 "loads": [dict(movement=m, **load_out(m, w)) for m, w in ev["loads"].items()
                           if C.movement(m)["implement"] != "bodyweight"]}
    p = ev["pred"]
    if fmt in ("for_time", "amrap", "rotation"):
        out["items"] = [_view(v) for v in ev["views"]]
    if fmt == "for_time":
        lo, hi = _band(p["elapsed_s"], p["sd_log"])
        out["target"] = {"time": t_out(p["elapsed_s"]), "range": {"fast": t_out(lo), "slow": t_out(hi)},
                         "my_rounds": p["own_rounds"],
                         "my_time_per_round": t_out(p["own_s"] / max(p["own_rounds"], 1)),
                         "my_round_splits": [t_out(t) for t in p["round_s"]],
                         "time_cap": t_out(block["time_cap_s"]) if block.get("time_cap_s") else None,
                         "finishes_within_cap": not p["capped"],
                         "progress_at_cap_pct": round(p["cap_frac"] * 100)}
    elif fmt == "amrap":
        lo, hi = _band(p["team_rounds"], p["sd_log"])
        out["target"] = {"rounds": round(p["team_rounds"], 1),
                         "range": {"low": round(lo, 1), "high": round(hi, 1)},
                         "time_per_round": t_out(p["round_s"][0]) if p["round_s"] else None}
    elif fmt == "rotation":
        out["target"] = {"pacer_round_time": t_out(p["stint_s"]), "my_pacer_rounds": round(p["own_rounds"], 1),
                         "team_rotations": round(p["team_rounds"], 1),
                         "machines": [{"machine": m["mod"], "cal_per_stint": round(m["cal_per_stint"]),
                                       "cal_per_min": round(m["rate_cal_min"], 1),
                                       "pace": render.pace_text(m["mod"], m["speed"]),
                                       "my_total_cal": round(m["own_cal"])} for m in p["machines"]]}
    elif fmt == "emom":
        out["interval_s"] = block["interval_s"]
        out["total_min"] = p["total_min"]
        out["stations"] = []
        for st in ev["stations"]:
            first, last = st["first"], st["last"]
            unit = "cal" if first["kind"] == "erg" else "reps"
            row = {"movement": st["movement"], "name": C.ko(st["movement"]), "unit": unit,
                   "amount_first_round": first.get("reps", first.get("cal")),
                   "amount_last_round": last.get("reps", last.get("cal")),
                   "time_first_round": t_out(first["t_final"]), "time_last_round": t_out(last["t_final"]),
                   "fit_first_round": st["fit_first"], "fit_last_round": st["fit_last"]}
            if st.get("load") is not None and C.movement(st["movement"])["implement"] != "bodyweight":
                row["load"] = load_out(st["movement"], st["load"])
            if st.get("erg"):
                row["erg"] = {"machine": st["erg"]["mod"], "cal_per_min": round(st["erg"]["rate_cal_min"], 1),
                              "pace": render.pace_text(st["erg"]["mod"], st["erg"]["speed"])}
            if st.get("scale_to") is not None and st["scale_to"] < (row["amount_last_round"] or 0):
                row["scale_to_keep_rest"] = st["scale_to"]
            if st.get("alts"):
                row["alternatives"] = [a["movement"] for a in st["alts"]]
            out["stations"].append(row)
    elif fmt == "interval":
        out["work_s"], out["rest_s"], out["total_min"] = block["work_s"], block.get("rest_s"), p["total_min"]
        out["sections"] = []
        for i, sec in enumerate(ev["sections"]):
            lines = []
            for ln in sec["lines"]:
                mid, x = ln["movement"], ln.get("x")
                row = {"movement": mid, "name": C.ko(mid)}
                if ln.get("load") is not None and C.movement(mid)["implement"] != "bodyweight":
                    row["load"] = load_out(mid, ln["load"])
                if ln.get("max"):
                    row.update(kind="max_effort", target=round(ln["score"]),
                               unit="cal" if C.movement(mid)["implement"] == "erg" else "reps",
                               time_available=t_out(ln["remain_s"]))
                else:
                    row.update(kind="fixed", prescribed=_prescribed(ln["item"]))
                    if x:
                        row["time"] = t_out(x["t_final"])
                        if x["kind"] == "erg" and x.get("cal") is not None:
                            row["expected_cal"] = round(x["cal"], 1)
                lines.append(row)
            out["sections"].append({"name": sec.get("name") or f"section {i + 1}", "lines": lines})
    return out


def day_plan(athlete: Athlete, day: dict, only: list[str] | None = None) -> dict:
    render.set_units(athlete)
    sessions = []
    for s in day["sessions"]:
        if only and s["id"] not in only:
            continue
        blocks = []
        for i, b in enumerate(s["blocks"]):
            if b["type"] == "strength":
                blocks.append({"block": i + 1, "type": "strength", "intent": b["intent"],
                               "items": [strength_item(p) for p in plan_block(athlete, b)]})
            else:
                blocks.append(dict(metcon(athlete, evaluate(athlete, b)), block=i + 1))
        sessions.append({"id": s["id"], "title": s["title"], "focus": s.get("focus"),
                         "coach_notes": s["notes"], "assumptions": s["assumptions"], "blocks": blocks})
    return {"date": day["date"].isoformat(), "condition": condition(athlete),
            "low_data_warning": render.is_cold(athlete), "sessions": sessions,
            "markdown": render.plan_day(athlete, day, only)}


def condition(athlete: Athlete) -> dict:
    return {"checkin_adjust_pct": round((math.exp(athlete.b.value("cond")) - 1) * 100, 1),
            "local_fatigue_penalty_pct": {p: round(min(12.0, 1.2 * v), 1)
                                          for p, v in athlete.fatigue.items() if v >= 0.4},
            "systemic_fatigue": round(athlete.sys_fatigue, 2), "acute_chronic_load_ratio": athlete.acwr(),
            "summary": render.condition_lines(athlete)}
