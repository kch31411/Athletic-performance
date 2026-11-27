"""도구 계층: 엔진의 기능을 '입력이 자명한 함수'로 나눠 노출한다.

- 여기 함수들이 MCP 도구(coach/mcp_server.py)이자 CLI(`python -m coach call <도구> '<json>'`)다.
- 안에 LLM 은 없다. 같은 입력 + 같은 저장 데이터 → 항상 같은 출력.
- 입력은 models.py 의 타입으로 검증되고, 쓰기 도구는 저장 후 전체 기록을 재생해 보고
  실패하면 되돌린다(잘못된 기록이 남지 않는다). 같은 대상을 다시 기록하면 덮어쓴다(멱등).
"""
import functools
import math
import os
import re
import subprocess
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Literal, Optional

import yaml
from pydantic import Field, validate_call

from . import catalog as C
from . import hyrox, planjson, render
from .athlete import LB, normalize_profile
from .feedback import LogError, parse_time
from .metcon import evaluate
from .models import Strict  # noqa: E402  (StationSplit 에서 사용)
from .models import (EFFORT_RPE, SESSION_SRPE, Choice, Day, Effort, ErgEffort, Load, Machine, MetconBlock,
                     MetconScore, MovementId, RepsLeft, Rpe, Scale, Session, SessionFeel, SessionId, SetGroup,
                     Soreness, StrengthBlock, Superset, TimeStr, Unit, UsedLoad)
from .replay import build, load_profile
from .strength import plan_item
from .workout import SchemaError, _metcon, find_day, normalize_day

ROOT = Path(os.environ.get("COACH_ROOT") or Path(__file__).resolve().parent.parent)
TOOLS: dict[str, dict] = {}
LOADED = ("barbell", "dumbbell", "kettlebell", "machine")
SCORE_TYPES = {"for_time": ("time", "capped", "none"), "amrap": ("rounds", "none"),
               "rotation": ("rotation", "none"), "emom": ("none",), "interval": ("none",)}
MACHINE_KO = {"row": "row", "ski": "ski", "bike_erg": "bike", "assault_bike": "assault", "run": "run"}

OptDay = Annotated[Optional[date], Field(description=(
    "Evaluate the athlete's state as of this date (YYYY-MM-DD), including fatigue from logged sessions. "
    "Omit for today."))]


class ApiError(ValueError):
    """입력은 형식상 맞지만 의미상 받아들일 수 없을 때. 메시지에 고치는 방법을 담는다."""


def set_root(root: Path) -> None:
    global ROOT
    ROOT = Path(root)


def tool(writes: bool = False):
    def deco(fn):
        checked = validate_call(fn)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                out = checked(*args, **kwargs)
            except ApiError:
                raise
            except (SchemaError, LogError, KeyError, FileNotFoundError) as e:
                raise ApiError(str(e).strip("'\"")) from e
            if writes:
                out["storage"] = _sync(fn.__name__)
            return out

        TOOLS[fn.__name__] = {"fn": wrapper, "writes": writes}
        return wrapper
    return deco


# ── 저장소 ───────────────────────────────────────────────
def _sync(what: str) -> dict:
    """COACH_GIT_SYNC=commit|push 이면 data/ 변경을 git 에 남긴다 (기본 off)."""
    mode = os.environ.get("COACH_GIT_SYNC", "off")
    out = {"saved": True, "git": mode}
    if mode not in ("commit", "push"):
        return out
    try:
        subprocess.run(["git", "-C", str(ROOT), "add", "data"], check=True, capture_output=True)
        done = subprocess.run(["git", "-C", str(ROOT), "commit", "-q", "-m", f"coach: {what}"],
                              capture_output=True)
        out["committed"] = done.returncode == 0
        if mode == "push" and done.returncode == 0:
            pushed = subprocess.run(["git", "-C", str(ROOT), "push", "-q"], capture_output=True, timeout=60)
            out["pushed"] = pushed.returncode == 0
    except Exception as e:  # 저장은 이미 끝났다. git 실패는 알리기만 한다.
        out["git_error"] = str(e)
    return out


def _read(path: Path) -> dict:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}


def _write_checked(path: Path, data: dict, header: str = "") -> None:
    """쓰고 → 전체 재생으로 검증 → 실패하면 원상복구."""
    old = path.read_text(encoding="utf-8") if path.exists() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    try:
        build(ROOT)
    except Exception as e:
        if old is None:
            path.unlink()
        else:
            path.write_text(old, encoding="utf-8")
        raise ApiError(f"rejected, nothing was saved: {e}") from e


def _profile_path() -> Path:
    return ROOT / "data" / "athlete" / "profile.yaml"


def _workout_path(day: date) -> Path:
    return ROOT / "data" / "workouts" / f"{day.isoformat()}.yaml"


def _log_path(day: date) -> Path:
    return ROOT / "data" / "logs" / f"{day.isoformat()}.yaml"


def _sex() -> str:
    return normalize_profile(load_profile(ROOT))["sex"]


def _athlete(day: Optional[date]):
    athlete = build(ROOT, until=day or date.today())
    render.set_units(athlete)
    return athlete


def _program(day: date) -> dict:
    prog = find_day(ROOT, day, _sex())
    if not prog:
        raise ApiError(f"no program saved for {day}. Call save_program_day first.")
    return prog


def _jsonable(x: Any) -> Any:
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, date):
        return x.isoformat()
    return x


# ── 참조 ─────────────────────────────────────────────────
@tool()
def get_conventions() -> dict[str, Any]:
    """Rules for turning a gym whiteboard and a workout report into tool inputs (units, team formats,
    RPE, intensity keywords). Read once before save_program_day or any log_* call."""
    text = (Path(__file__).parent / "data" / "conventions.md").read_text(encoding="utf-8")
    return {"conventions": text}


def _movement_row(mid: str) -> dict:
    m = C.movement(mid)
    return {"id": mid, "name": m.get("ko"), "aliases": m.get("aliases", []), "implement": m["implement"],
            "pattern": m.get("pattern"), "load_convention": planjson.load_convention(mid),
            "has_strength_model": "ref" in m}


@tool()
def list_movements(
    implement: Annotated[Optional[Literal["barbell", "dumbbell", "kettlebell", "bodyweight", "machine",
                                          "erg", "other"]], Field(description="Filter by implement.")] = None,
    pattern: Annotated[Optional[str], Field(description="Filter by movement pattern, e.g. squat, hinge, push_h, pull_v, olympic.")] = None,
) -> dict[str, Any]:
    """List the movement catalog. Every `movement` field in other tools must be one of these ids."""
    rows = [_movement_row(m) for m in C.movements()]
    if implement:
        rows = [r for r in rows if r["implement"] == implement]
    if pattern:
        rows = [r for r in rows if r["pattern"] == pattern]
    return {"movements": rows}


def _norm(s: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", s.lower())


@tool()
def find_movement(
    query: Annotated[str, Field(min_length=1, description='A movement name as written or spoken, e.g. "BB row", "씰로우", "burpee over the bar".')],
) -> dict[str, Any]:
    """Resolve a movement name to catalog ids by exact/substring match on ids, names and aliases
    (deterministic, no guessing). Returns best matches first; an empty list means it is not in the catalog."""
    q = _norm(query)
    scored = []
    for mid, m in C.movements().items():
        names = [_norm(x) for x in [mid, m.get("ko", ""), *m.get("aliases", [])] if x]
        if q in names:
            score = 1.0
        elif any(q in n or n in q for n in names if len(n) >= 3):
            best = max((len(n) for n in names if len(n) >= 3 and (q in n or n in q)), default=0)
            score = round(0.5 + 0.4 * min(best, len(q)) / max(best, len(q)), 3)
        else:
            continue
        scored.append((score, mid))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return {"query": query, "matches": [dict(_movement_row(mid), score=s) for s, mid in scored[:6]]}


# ── 프로필 ───────────────────────────────────────────────
@tool()
def get_profile() -> dict[str, Any]:
    """The athlete profile the model starts from (sex, bodyweight, level, units, known benchmarks)."""
    raw = load_profile(ROOT)
    p = normalize_profile(raw)
    return {"sex": p["sex"], "bodyweight_kg": p["bodyweight_kg"], "level": raw.get("level", "novice"),
            "preferred_machine": p["preferred_machine"], "barbell_unit": p["equipment"]["barbell_unit"],
            "barbell_plates": p["equipment"]["barbell_plates"],
            "benchmarks": _jsonable(p["benchmarks"])}


@tool(writes=True)
def update_profile(
    sex: Optional[Literal["m", "f"]] = None,
    bodyweight_kg: Annotated[Optional[float], Field(gt=30, lt=250)] = None,
    level: Annotated[Optional[Literal["beginner", "novice", "intermediate", "advanced"]], Field(description=(
        "Starting point only; logged results override it. Rough guide (men / women, bench 1RM ÷ bodyweight): "
        "beginner 0.6/0.4, novice 0.85/0.5, intermediate 1.15/0.7, advanced 1.45/0.9."))] = None,
    preferred_machine: Annotated[Optional[Machine], Field(description='Machine used when the board just says "machine".')] = None,
    barbell_unit: Annotated[Optional[Unit], Field(description="Unit the athlete loads barbells in; prescriptions are rounded and displayed in it.")] = None,
    barbell_plates: Annotated[Optional[list[Annotated[float, Field(gt=0)]]], Field(description=(
        "Plate sizes actually available, weight of ONE plate in barbell_unit (e.g. [10, 25, 45]). Barbell "
        "prescriptions are then limited to loads that can really be built (bar + the same plates on both "
        "sides). Pass an empty list to remove the restriction."))] = None,
) -> dict[str, Any]:
    """Change profile fields. Only the fields you pass are changed."""
    raw = _read(_profile_path())
    changed = {}
    for key, val in (("sex", sex), ("bodyweight_kg", bodyweight_kg), ("level", level),
                     ("preferred_machine", preferred_machine)):
        if val is not None:
            raw[key] = changed[key] = val
    if barbell_unit is not None:
        raw.setdefault("equipment", {})["barbell_unit"] = changed["barbell_unit"] = barbell_unit
    if barbell_plates is not None:
        plates = sorted(set(barbell_plates)) or None
        raw.setdefault("equipment", {})["barbell_plates"] = changed["barbell_plates"] = plates
    if not changed:
        raise ApiError("nothing to change: pass at least one field")
    _write_checked(_profile_path(), raw, "# 선수 프로필 — 모델의 출발점. 기록이 쌓이면 기록이 우선한다.\n")
    return {"changed": changed, "profile": get_profile.__wrapped__()}


def _signed_load(mid: str, load: Optional[Load], assistance: Optional[Load]) -> float:
    m = C.movement(mid)
    if m["implement"] == "erg":
        raise ApiError(f"{mid} is a machine: use an erg tool, not a strength tool")
    if m["implement"] in LOADED:
        if load is None or assistance is not None:
            raise ApiError(f"{mid} ({m['implement']}) needs `load` ({planjson.load_convention(mid)})")
        return load.kg()
    if load is not None and m["implement"] == "other":
        return load.kg()
    return load.kg() if load else (-assistance.kg() if assistance else 0.0)


def _add_benchmark(entry: dict) -> dict:
    raw = _read(_profile_path())
    benches = raw.get("benchmarks") or []
    if entry in benches:
        return {"added": False, "reason": "identical benchmark already present", "benchmark": _jsonable(entry)}
    raw["benchmarks"] = benches + [entry]
    _write_checked(_profile_path(), raw, "# 선수 프로필 — 모델의 출발점. 기록이 쌓이면 기록이 우선한다.\n")
    return {"added": True, "benchmark": _jsonable(entry)}


@tool(writes=True)
def add_strength_benchmark(
    movement: MovementId,
    reps: Annotated[int, Field(ge=1, le=50)],
    load: Annotated[Optional[Load], Field(description="Weight lifted (see Load). For bodyweight movements: weight added, omit if none.")] = None,
    assistance: Annotated[Optional[Load], Field(description="Bodyweight movements only.")] = None,
    reps_left: Annotated[RepsLeft, Field(description="Reps left in the tank on that set. A personal best / max set is 0.")] = 0,
    date: Annotated[Optional[date], Field(description="When it was done (YYYY-MM-DD). Omit if unknown.")] = None,
    note: Optional[str] = None,
) -> dict[str, Any]:
    """Record a known best or test set outside a logged session (e.g. "squat 225lb x3", "max 12 pull-ups").
    It seeds the strength model. For sets done in a class, use log_strength_result instead."""
    entry: dict = {"movement": movement, "load": round(_signed_load(movement, load, assistance), 2),
                   "reps": reps, "rpe": 10 - reps_left}
    if date:
        entry["date"] = date
    if note:
        entry["note"] = note
    return _add_benchmark(entry)


def _erg_entry(e: ErgEffort) -> dict:
    out: dict = {"erg": e.machine, "time": parse_time(e.time), "effort": e.effort}
    if e.cal is not None:
        out["cal"] = e.cal
    elif e.cal_per_hour is not None:
        out["cal"] = round(e.cal_per_hour * out["time"] / 3600.0, 2)
    else:
        out["distance_m"] = e.distance_m
    return out


@tool(writes=True)
def add_erg_benchmark(
    effort: ErgEffort,
    date: Annotated[Optional[date], Field(description="When it was done (YYYY-MM-DD). Omit if unknown.")] = None,
    note: Optional[str] = None,
) -> dict[str, Any]:
    """Record a known machine/running result outside a logged session (e.g. 2k row 7:40, half marathon
    1:52:00 → machine run, distance_m 21097.5). It seeds the engine model."""
    entry = _erg_entry(effort)
    if date:
        entry["date"] = date
    if note:
        entry["note"] = note
    return _add_benchmark(entry)


# ── 프로그램 ─────────────────────────────────────────────
def _amount(a):
    return a if isinstance(a, (int, float)) or a is None else {"f": a.f, "m": a.m}


def _raw_metcon_item(e) -> dict:
    if isinstance(e, Choice):
        return {"choice": [_raw_metcon_item(i) for i in e.choice]}
    if isinstance(e, Scale):
        return {"scale": [_raw_metcon_item(i) for i in e.scale]}
    out: dict = {"movement": e.movement}
    for key in ("reps", "cal", "distance_m"):
        if getattr(e, key) is not None:
            out[key] = _amount(getattr(e, key))
    if e.seconds is not None:
        out["seconds"] = e.seconds
    if e.max_effort:
        out["max"] = True
    if e.load is not None:
        k = LB if e.load.unit == "lb" else 1.0
        v = _amount(e.load.value)
        out["load_kg"] = {s: round(x * k, 2) for s, x in v.items()} if isinstance(v, dict) else round(v * k, 2)
    if e.note:
        out["note"] = e.note
    return out


def _raw_strength_item(i) -> dict:
    if i.reps_to is not None and (i.reps is None or i.reps_to < i.reps):
        raise ApiError(f"{i.movement}: reps_to needs reps and must be >= reps")
    out: dict = {"movement": i.movement}
    if i.reps is not None:
        out["reps"] = [i.reps, i.reps_to] if i.reps_to and i.reps_to != i.reps else i.reps
    for src, dst in (("per_side", "per_side"), ("drop_set", "drop"), ("sets", "sets"),
                     ("target_rpe", "rpe"), ("main", "main"), ("note", "note")):
        if getattr(i, src):
            out[dst] = getattr(i, src)
    return out


def _raw_block(b) -> dict:
    if isinstance(b, StrengthBlock):
        return {"type": "strength", "intent": b.intent, "items": [
            {"superset": [_raw_strength_item(s) for s in i.superset]} if isinstance(i, Superset)
            else _raw_strength_item(i) for i in b.items]}
    out: dict = {"type": "metcon", "format": b.format, "intensity": b.intensity}
    for key in ("rounds", "rep_scheme", "time_cap_min", "duration_min", "interval_s", "work_s", "rest_s"):
        if getattr(b, key) is not None:
            out[key] = getattr(b, key)
    if b.team:
        out["team"] = b.team.model_dump()
    if b.progression:
        out["progression"] = b.progression.model_dump()
    if b.items:
        out["items"] = [_raw_metcon_item(i) for i in b.items]
    if b.sections:
        out["sections"] = []
        for s in b.sections:
            sec = {k: getattr(s, k) for k in ("name", "rounds", "rep_scheme", "time_cap_min", "intensity")
                   if getattr(s, k) is not None}
            sec["items"] = [_raw_metcon_item(i) for i in s.items]
            out["sections"].append(sec)
    if b.stations:
        out["stations"] = [{"role": s.role, "pacer": s.pacer, "items": [_raw_metcon_item(i) for i in s.items]}
                           for s in b.stations]
    return out


@tool(writes=True)
def save_program_day(
    date: Day,
    sessions: Annotated[list[Session], Field(min_length=1, description="Every class on the board for that day.")],
    gym: Optional[str] = None,
) -> dict[str, Any]:
    """Store the gym's program for one day (replaces any program already saved for that date).
    Transcribe the board literally into this structure; put every guess in `assumptions`.
    Afterwards call get_plan(date) for the prescription."""
    raw = {"date": date, "gym": gym, "sessions": [
        {"id": s.id, "title": s.title, "focus": s.focus, "notes": s.notes, "assumptions": s.assumptions,
         "blocks": [_raw_block(b) for b in s.blocks]} for s in sessions]}
    normalize_day(raw, _sex())
    path = _workout_path(date)
    replaced = path.exists()
    _write_checked(path, raw)
    return {"date": date.isoformat(), "replaced_existing": replaced,
            "sessions": [{"id": s.id, "title": s.title, "blocks": len(s.blocks)} for s in sessions]}


@tool()
def get_program_day(date: Day) -> dict[str, Any]:
    """The stored program for a date, exactly as saved (internal YAML form: loads in kg)."""
    path = _workout_path(date)
    if not path.exists():
        raise ApiError(f"no program saved for {date}")
    return {"program": _jsonable(_read(path))}


@tool()
def list_program_days() -> dict[str, Any]:
    """Dates that have a stored program, with their session ids and which sessions already have a log."""
    days = []
    for path in sorted((ROOT / "data" / "workouts").glob("*.yaml")):
        raw = _read(path)
        log = _read(ROOT / "data" / "logs" / path.name)
        days.append({"date": path.stem, "sessions": [s["id"] for s in raw.get("sessions") or []],
                     "logged_sessions": [s.get("id") for s in log.get("sessions") or []],
                     "has_checkin": bool(log.get("checkin"))})
    return {"days": days}


# ── 예측 (읽기 전용) ─────────────────────────────────────
@tool()
def get_plan(
    date: Day,
    session_ids: Annotated[Optional[list[SessionId]], Field(description="Limit to these sessions. Omit for all.")] = None,
) -> dict[str, Any]:
    """The prescription for a stored program day: loads, target RPE, set splits, target times/rounds,
    machine paces. Uses everything logged up to and including that date (check-in, earlier sessions).
    `markdown` is a ready-to-show Korean version of the same numbers."""
    prog = _program(date)
    known = [s["id"] for s in prog["sessions"]]
    for sid in session_ids or []:
        if sid not in known:
            raise ApiError(f"session '{sid}' is not in the {date} program. Sessions: {known}")
    return planjson.day_plan(_athlete(date), prog, session_ids)


@tool()
def predict_strength_load(
    movement: MovementId,
    reps: Annotated[int, Field(ge=1, le=50, description="Target reps per set.")],
    sets: Annotated[int, Field(ge=1, le=12, description="Number of sets at the same load.")] = 4,
    target_reps_left: Annotated[int, Field(ge=0, le=5, description=(
        "How many reps should still be possible at the end of the LAST set. 2 = solid training set, "
        "1 = heavy, 0 = to the limit, 3-4 = easy/technique."))] = 2,
    date: OptDay = None,
) -> dict[str, Any]:
    """Recommended load for sets × reps leaving a chosen number of reps in reserve, without needing a
    stored program. Bodyweight movements return whether to add weight, go unweighted, or use assistance."""
    item = {"movement": movement, "reps_lo": float(reps), "reps_hi": float(reps), "per_side": False,
            "drop": False, "sets": sets, "rpe": 10.0 - target_reps_left, "main": False, "superset": None}
    return planjson.strength_item(plan_item(_athlete(date), item, "volume"))


@tool()
def predict_max_reps(
    movement: MovementId,
    load: Annotated[Optional[Load], Field(description="External weight (see Load). Omit for bodyweight.")] = None,
    assistance: Annotated[Optional[Load], Field(description="Bodyweight movements only.")] = None,
    date: OptDay = None,
) -> dict[str, Any]:
    """Estimated maximum unbroken reps at a given load, plus the estimated 1RM it is derived from."""
    athlete = _athlete(date)
    if not athlete.has_strength(movement):
        return {"movement": movement, "has_strength_model": False,
                "reference_unbroken_reps": round(athlete.unbroken(movement))}
    ext = _signed_load(movement, load, assistance)
    mean, sd = athlete.log1rm(movement, today=False)
    one_today = athlete.one_rm(movement)
    total = athlete.bw_load(movement) + ext
    return {"movement": movement, "has_strength_model": True,
            "max_reps": round(athlete.rmax(movement, ext), 1), "can_lift_once": total <= one_today,
            "estimated_1rm_kg": round(math.exp(mean), 1), "estimated_1rm_today_kg": round(one_today, 1),
            "includes_bodyweight_kg": round(athlete.bw_load(movement), 1),
            "uncertainty_pct": round(sd * 100, 1)}


@tool()
def predict_erg(
    machine: Machine,
    cal: Annotated[Optional[float], Field(gt=0, description="Target calories → predicted time.")] = None,
    distance_m: Annotated[Optional[float], Field(gt=0, description="Target distance → predicted time.")] = None,
    time: Annotated[Optional[TimeStr], Field(description="Fixed duration → predicted calories/distance.")] = None,
    date: OptDay = None,
) -> dict[str, Any]:
    """Best all-out machine performance for ONE of: a calorie target, a distance target, or a fixed time.
    (Inside a mixed workout the sustainable pace is lower; get_plan accounts for that.)"""
    if sum(x is not None for x in (cal, distance_m, time)) != 1:
        raise ApiError("give exactly one of cal / distance_m / time")
    if distance_m is not None and machine == "assault_bike":
        raise ApiError("assault_bike: use cal or time")
    athlete = _athlete(date)
    mod = athlete.erg_mod(machine)
    t = parse_time(time) if time else 120.0
    for _ in range(30):                     # 목표량 ↔ 시간의 고정점
        rate = athlete.erg_rate(mod, t)
        speed = athlete.erg_speed(mod, rate)
        if cal is not None:
            t = cal / rate
        elif distance_m is not None:
            t = distance_m / speed
    return {"machine": machine, "time": planjson.t_out(t), "cal": round(rate * t, 1),
            "cal_per_min": round(rate * 60, 1), "distance_m": round(speed * t),
            "pace": render.pace_text(mod, speed)}


def _block_movements(block: dict) -> set[str]:
    out: set[str] = set()

    def walk(items):
        for it in items:
            if it.get("options"):
                walk(it["options"])
            else:
                out.add(it["movement"])
    walk(block.get("items") or [])
    for sec in block.get("sections") or []:
        out |= _block_movements(sec)
    for st in block.get("stations") or []:
        walk(st["items"])
    return out


def _loads_picks(block: dict, loads: list[UsedLoad], picks: list[str]) -> tuple[dict, list[str]]:
    allowed = _block_movements(block)
    out = {}
    for u in loads:
        m = C.movement(u.movement)
        if u.movement not in allowed:
            raise ApiError(f"loads: {u.movement} is not in this block. Movements: {sorted(allowed)}")
        if m["implement"] not in LOADED:
            raise ApiError(f"loads: {u.movement} is not a loaded movement")
        out[u.movement] = round(u.load.kg(), 2)
    for p in picks:
        if p not in allowed:
            raise ApiError(f"picks: {p} is not an option in this block. Movements: {sorted(allowed)}")
    return out, list(picks)


@tool()
def predict_metcon(
    block: MetconBlock,
    loads: Annotated[list[UsedLoad], Field(description="Force these loads instead of the recommended ones.")] = [],
    picks: Annotated[list[MovementId], Field(description="For choice/scale items: the option to use.")] = [],
    date: OptDay = None,
) -> dict[str, Any]:
    """Recommended loads and predicted result for one conditioning workout without storing it
    (what-if: different loads, options, or a workout that is not on the program)."""
    athlete = _athlete(date)
    norm = _metcon(_raw_block(block), athlete.sex, "predict_metcon")
    kg, chosen = _loads_picks(norm, loads, picks)
    ev = evaluate(athlete, norm, loads=kg or None, picks=chosen or None)
    out = planjson.metcon(athlete, ev)
    out["markdown"] = "\n".join(render.metcon_lines(athlete, ev))
    return out


@tool()
def get_status(date: OptDay = None) -> dict[str, Any]:
    """Current ability estimates with uncertainty: 1RMs, muscular endurance, engine per machine,
    mixed-modal capacity, fatigue. `markdown` is a Korean summary."""
    athlete = _athlete(date)
    out = render.status_data(athlete)
    out["condition"] = planjson.condition(athlete)
    out["markdown"] = render.status_md(athlete)
    return out


# ── 기록 (쓰기) ──────────────────────────────────────────
def _session(day: date, session_id: str, need_block: bool = False) -> Optional[dict]:
    prog = find_day(ROOT, day, _sex())
    if prog is None:
        if need_block:
            raise ApiError(f"no program saved for {day}: save_program_day first, then log the workout result")
        return None
    session = next((s for s in prog["sessions"] if s["id"] == session_id), None)
    if session is None and (need_block or session_id != "extra"):
        raise ApiError(f"session '{session_id}' is not in the {day} program. "
                       f"Sessions: {[s['id'] for s in prog['sessions']]} (or 'extra' for other work)")
    return session


def _upsert(day: date, session_id: str, match, entry: Optional[dict], summary: Optional[dict] = None) -> dict:
    """세션 기록에서 match 에 해당하는 항목을 entry 로 바꾸거나(없으면 추가) 지운다."""
    path = _log_path(day)
    log = _read(path)
    log["date"] = day
    sessions = log.setdefault("sessions", [])
    slog = next((s for s in sessions if s.get("id") == session_id), None)
    if slog is None:
        slog = {"id": session_id, "results": []}
        sessions.append(slog)
    results = slog.setdefault("results", [])
    replaced = [r for r in results if match(r)]
    results[:] = [r for r in results if not match(r)]
    if entry is not None:
        results.append(entry)
    if summary:
        slog.update(summary)
    _write_checked(path, log)
    return {"replaced_existing": bool(replaced)}


def _events(day: date, session_id: Optional[str] = None) -> list[dict]:
    athlete = build(ROOT, until=day)
    prefix = day.isoformat() + "/" + (session_id or "")
    out = []
    for e in athlete.events:
        if not e.get("tag", "").startswith(prefix):
            continue
        row = {"session": e["tag"].split("/", 1)[1], "kind": e["kind"]}
        s = e["surprise"]
        if e["kind"] == "strength":
            st = e["set"]
            row.update(movement=e["movement"], performance_vs_expected_pct=round(s * 100, 1),
                       best_set={"load_kg": st["load"], "reps": st["reps"],
                                 "reps_left": st["rir"]},
                       estimated_1rm_kg={"before": round(e["e1rm_before"], 1), "after": round(e["e1rm_after"], 1)})
        elif e["kind"] == "metcon":
            row.update(format=e["format"], performance_vs_expected_pct=round(-s * 100, 1),
                       capacity_pct={"before": round(e["met_before"] * 100, 1), "after": round(e["met_after"] * 100, 1)})
            if "pred_s" in e:
                row.update(predicted_time=planjson.t_out(e["pred_s"]), actual_time=planjson.t_out(e["actual_s"]))
            else:
                row.update(predicted_rounds=round(e["pred_rounds"], 1), actual_rounds=round(e["actual_rounds"], 1))
        elif e["kind"] == "hyrox":
            row.update(what=e["what"], performance_vs_expected_pct=round(-s * 100, 1),
                       predicted_time=planjson.t_out(e["pred_s"]), actual_time=planjson.t_out(e["actual_s"]))
        else:
            row.update(machine=e["mod"], performance_vs_expected_pct=round(s * 100, 1),
                       sustainable_cal_per_min={"before": round(e["cp_before"], 1), "after": round(e["cp_after"], 1)})
        out.append(row)
    return out


@tool(writes=True)
def log_checkin(
    date: Day,
    readiness: Annotated[Optional[int], Field(ge=1, le=5, description="Overall feeling before training: 1 worst, 3 normal, 5 best.")] = None,
    sleep_h: Annotated[Optional[float], Field(ge=0, le=16, description="Hours slept last night.")] = None,
    soreness: Annotated[list[Soreness], Field(description="Sore body regions, if any.")] = [],
) -> dict[str, Any]:
    """Pre-workout condition for a date (replaces that date's previous check-in). It adjusts that day's
    prescription only; call get_plan again afterwards."""
    if readiness is None and sleep_h is None and not soreness:
        raise ApiError("give at least one of readiness / sleep_h / soreness")
    checkin: dict = {}
    if readiness is not None:
        checkin["readiness"] = readiness
    if sleep_h is not None:
        checkin["sleep_h"] = sleep_h
    if soreness:
        checkin["soreness"] = {s.region: s.level for s in soreness}
    path = _log_path(date)
    log = _read(path)
    log["date"] = date
    log["checkin"] = checkin
    log.setdefault("sessions", [])
    _write_checked(path, log)
    return {"date": date.isoformat(), "checkin": checkin,
            "condition": planjson.condition(_athlete(date))}


@tool(writes=True)
def log_strength_result(
    date: Day,
    session_id: SessionId,
    movement: MovementId,
    sets: Annotated[list[SetGroup], Field(min_length=1, description=(
        "What was actually done, in order. \"60kg 10 reps x 4 sets, last set RPE 8\" → "
        "[{reps: 10, count: 4, load: {value: 60, unit: kg}, reps_left: 2}]."))],
) -> dict[str, Any]:
    """Record the sets done for ONE strength movement in a session. Logging the same movement in the
    same session again replaces the earlier entry. Returns how the result compared with the model's
    expectation and the updated 1RM estimate."""
    _session(date, session_id)
    expanded = []
    for g in sets:
        kg = round(_signed_load(movement, g.load, g.assistance), 2)
        for i in range(g.count):
            s: dict = {"load": kg, "reps": g.reps}
            if i == g.count - 1:
                if g.reps_left is not None:
                    s["rpe"] = 10 - g.reps_left
                if g.rpe is not None:
                    s["rpe"] = g.rpe
                if g.feel is not None:
                    s["feel"] = g.feel
            expanded.append(s)
    entry = {"movement": movement, "sets": expanded}
    out = _upsert(date, session_id, lambda r: r.get("movement") == movement and "block" not in r, entry)
    updates = [e for e in _events(date, session_id) if e.get("movement") == movement]
    out.update(date=date.isoformat(), session_id=session_id, movement=movement, sets_recorded=len(expanded),
               model_update=updates[-1] if updates else None)
    if not updates:
        out["note"] = ("stored and counted toward fatigue, but reps_left was not given (or the movement has no "
                       "strength model), so ability estimates did not change")
    return out


@tool(writes=True)
def log_metcon_result(
    date: Day,
    session_id: SessionId,
    result: Annotated[MetconScore, Field(description=(
        "The score. for_time → time or capped; amrap → rounds; rotation → rotation; emom/interval → none."))],
    block: Annotated[int, Field(ge=1, description="Which block of the session (1-based, see get_plan).")] = 1,
    section: Annotated[Optional[int], Field(ge=1, description="Required when the block has sections: which one (1-based).")] = None,
    loads: Annotated[list[UsedLoad], Field(description="Weights actually used. Omit a movement only if the recommended load was used.")] = [],
    picks: Annotated[list[MovementId], Field(description="For choice/scale items: the option actually done.")] = [],
    effort: Optional[Effort] = None,
    rpe: Annotated[Optional[Rpe], Field(description="Alternative to `effort`, only if the athlete gives a 1-10 number.")] = None,
    load_feel: Annotated[Optional[Literal["light", "good", "heavy"]], Field(description="How the weights felt for this workout.")] = None,
    erg_efforts: Annotated[list[ErgEffort], Field(description="Optional measured machine pieces inside the workout.")] = [],
) -> dict[str, Any]:
    """Record the result of ONE conditioning workout (a metcon block, or one section of it) from the
    stored program. Logging the same block/section again replaces the earlier entry. Returns predicted
    vs actual and the updated capacity estimate."""
    session = _session(date, session_id, need_block=True)
    if not 1 <= block <= len(session["blocks"]) or session["blocks"][block - 1]["type"] != "metcon":
        kinds = [b["type"] for b in session["blocks"]]
        raise ApiError(f"block {block} of '{session_id}' is not a metcon. Blocks: {kinds}")
    target = session["blocks"][block - 1]
    sectioned = bool(target.get("sections")) and target["format"] != "interval"
    if sectioned and section is None:
        raise ApiError(f"this block has {len(target['sections'])} sections: pass `section` and log each one")
    if sectioned and section > len(target["sections"]):
        raise ApiError(f"section {section} does not exist (block has {len(target['sections'])})")
    if not sectioned and section is not None:
        raise ApiError("this block has no separately scored sections: omit `section`")
    scored = target["sections"][section - 1] if sectioned else target
    allowed = SCORE_TYPES[scored["format"]]
    if result.type not in allowed:
        raise ApiError(f"result.type '{result.type}' does not fit a {scored['format']} workout; use one of {list(allowed)}")

    if effort is not None and rpe is not None:
        raise ApiError("give effort or rpe, not both")
    if effort is not None:
        rpe = EFFORT_RPE[effort]
    kg, chosen = _loads_picks(scored, loads, picks)
    res: dict = {}
    if result.type == "time":
        res["time"] = parse_time(result.time)
    elif result.type == "capped":
        res["dnf"] = True
        if result.rounds_done is not None:
            res["rounds_done"] = result.rounds_done
    elif result.type == "rounds":
        res.update(rounds=result.rounds, reps=result.extra_reps)
    elif result.type == "rotation":
        res.update({k: v for k, v in (("my_rounds", result.my_rounds), ("team_rounds", result.team_rounds))
                    if v is not None})
    if kg:
        res["loads"] = kg
    if chosen:
        res["picks"] = chosen
    if rpe is not None:
        res["rpe"] = rpe
    if load_feel:
        res["load_feel"] = load_feel
    if erg_efforts:
        res["ergs"] = [_erg_entry(e) for e in erg_efforts]

    def same(r):
        return r.get("block") == block

    if sectioned:
        slog = next((s for s in _read(_log_path(date)).get("sessions") or [] if s.get("id") == session_id), {})
        old = next((r for r in slog.get("results") or [] if same(r)), {})
        parts = list(old.get("sections") or [])
        parts += [{}] * (len(target["sections"]) - len(parts))
        replaced_section = bool(parts[section - 1])
        parts[section - 1] = res
        entry = {"block": block, "sections": parts}
    else:
        entry = {"block": block, **res}
    out = _upsert(date, session_id, same, entry)
    if sectioned:
        out["replaced_existing"] = replaced_section
    updates = [e for e in _events(date, session_id) if e["kind"] in ("metcon", "erg")]
    out.update(date=date.isoformat(), session_id=session_id, block=block, section=section,
               model_updates=updates)
    return out


@tool(writes=True)
def log_erg_result(
    date: Day,
    effort: ErgEffort,
    session_id: SessionId = "extra",
) -> dict[str, Any]:
    """Record a stand-alone measured machine or running effort (a test piece, a run). For machine work
    inside a programmed workout use log_metcon_result.erg_efforts instead."""
    _session(date, session_id)
    entry = _erg_entry(effort)
    out = _upsert(date, session_id, lambda r: r == entry, entry)
    updates = [e for e in _events(date, session_id) if e["kind"] == "erg"]
    out.update(date=date.isoformat(), session_id=session_id, model_update=updates[-1] if updates else None)
    return out


@tool(writes=True)
def log_session_summary(
    date: Day,
    session_id: SessionId,
    feel: Optional[SessionFeel] = None,
    srpe: Annotated[Optional[int], Field(ge=1, le=10, description="Alternative to `feel`, only if the athlete gives a 1-10 number.")] = None,
    duration_min: Annotated[float, Field(gt=0, le=300, description="Total minutes of the class including rests.")] = 50,
) -> dict[str, Any]:
    """Record the overall load of a session (how hard × how long). Drives systemic fatigue and the
    weekly load ratio. Replaces the earlier summary for that session."""
    if (feel is None) == (srpe is None):
        raise ApiError("give exactly one of feel / srpe")
    srpe = srpe if srpe is not None else SESSION_SRPE[feel]
    _session(date, session_id)
    out = _upsert(date, session_id, lambda r: False, None, {"srpe": srpe, "duration_min": duration_min})
    out.update(date=date.isoformat(), session_id=session_id, srpe=srpe, duration_min=duration_min)
    return out


@tool()
def get_log(date: Day) -> dict[str, Any]:
    """Everything logged for a date, as stored (loads in kg; negative load = assistance; times in seconds)."""
    return {"date": date.isoformat(), "log": _jsonable(_read(_log_path(date))) or None}


@tool(writes=True)
def clear_session_log(date: Day, session_id: SessionId) -> dict[str, Any]:
    """Delete everything logged for one session on a date (results and summary). Use to undo a wrong
    log before re-entering it. The check-in is kept."""
    path = _log_path(date)
    log = _read(path)
    before = len(log.get("sessions") or [])
    log["sessions"] = [s for s in log.get("sessions") or [] if s.get("id") != session_id]
    if len(log["sessions"]) == before:
        raise ApiError(f"nothing logged for session '{session_id}' on {date}")
    _write_checked(path, log)
    return {"date": date.isoformat(), "session_id": session_id, "deleted": True}


@tool()
def get_review(date: Day) -> dict[str, Any]:
    """Predicted vs actual for everything logged on a date, and how each result moved the estimates.
    performance_vs_expected_pct > 0 means better than the model expected."""
    athlete = build(ROOT, until=date)
    return {"date": date.isoformat(), "updates": _events(date), "markdown": render.review_md(athlete, date)}


# ── HYROX ────────────────────────────────────────────────
Division = Annotated[Literal["open", "pro"], Field(description="HYROX singles division (sex comes from the profile).")]
HyroxStation = Literal["ski", "sled_push", "sled_pull", "burpee_broad_jump", "row", "farmers_carry",
                       "sandbag_lunge", "wall_ball"]


def _hyrox_out(athlete, division: str) -> dict:
    p = hyrox.predict(athlete, division)
    lo, hi = p["total_s"] * math.exp(-p["sd_log"]), p["total_s"] * math.exp(p["sd_log"])
    legs = []
    for leg in p["legs"]:
        row = {"kind": leg["kind"], "time": planjson.t_out(leg["seconds"])}
        if leg["kind"] == "run":
            row["run_number"] = leg["index"]
        else:
            row.update(station=leg["station"], name=hyrox.STATION_KO[leg["station"]], load_kg=leg["load_kg"],
                       seconds_vs_average_athlete=round(leg["vs_reference_s"]))
        legs.append(row)
    return {"division": division, "sex": p["sex"],
            "total": {"seconds": round(p["total_s"]), "text": render.hms(p["total_s"])},
            "range": {"fast": render.hms(lo), "slow": render.hms(hi)},
            "run_total": planjson.t_out(p["run_total_s"]), "station_total": planjson.t_out(p["station_total_s"]),
            "roxzone": planjson.t_out(p["roxzone_s"]),
            "average_athlete_total": render.hms(p["reference"]["total_s"]),
            "run_seconds_vs_average_athlete": round(p["run_vs_reference_s"]),
            "hyrox_results_used": athlete.n_obs.get("hyx", 0), "legs": legs,
            "markdown": render.hyrox_md(athlete, p)}


@tool()
def predict_hyrox(division: Division = "open", date: OptDay = None) -> dict[str, Any]:
    """Predicted HYROX singles race: total time with range, every run and station split, and where time is
    lost or gained versus an average intermediate athlete. Assumes a rested race day."""
    return _hyrox_out(_athlete(date), division)


@tool(writes=True)
def log_hyrox_station(
    date: Day,
    station: HyroxStation,
    time: TimeStr,
    effort: Annotated[Literal["race", "fresh"], Field(description=(
        "race: done inside a race or full/partial simulation, already fatigued. "
        "fresh: a stand-alone all-out attempt when rested."))],
    session_id: SessionId = "extra",
    division: Division = "open",
    load: Annotated[Optional[Load], Field(description=(
        "Only if different from the division standard. Sleds: total including the sled. "
        "Farmers carry: ONE hand. Sandbag / wall ball: that implement."))] = None,
    amount: Annotated[Optional[float], Field(gt=0, description=(
        "Only if shorter than the race standard: meters (wall_ball: reps). "
        "Standards: ski/row 1000 m, sleds 50 m, burpee broad jump 80 m, farmers 200 m, lunges 100 m, wall balls 100."))] = None,
) -> dict[str, Any]:
    """Record a timed HYROX station (practice, simulation piece). Updates that station's estimate and the
    race prediction. For a fresh all-out ski/row test use log_erg_result instead."""
    if station in hyrox.ERG_FRAC and effort == "fresh":
        raise ApiError("a fresh all-out ski/row piece is a machine test: use log_erg_result")
    if load is not None and hyrox.station_load(division, _sex(), station) is None:
        raise ApiError(f"{station} has no load")
    if amount is not None and amount > hyrox.AMOUNT[station]:
        raise ApiError(f"amount exceeds the race standard ({hyrox.AMOUNT[station]:g} {hyrox.UNIT[station]})")
    _session(date, session_id)
    before = _hyrox_out(_athlete(date), division)["total"]
    entry: dict = {"hyrox_station": station, "time": parse_time(time), "division": division, "effort": effort}
    if load is not None:
        entry["load_kg"] = round(load.kg(), 2)
    if amount is not None and amount < hyrox.AMOUNT[station]:
        entry["amount"] = amount
    key = {k: v for k, v in entry.items() if k != "time"}
    out = _upsert(date, session_id, lambda r: {k: v for k, v in r.items() if k != "time"} == key, entry)
    updates = [e for e in _events(date, session_id) if e["kind"] == "hyrox" and e.get("what") == station]
    out.update(date=date.isoformat(), station=station, model_update=updates[-1] if updates else None,
               race_prediction={"before": before["text"], "after": _hyrox_out(_athlete(date), division)["total"]["text"]})
    return out


class StationSplit(Strict):
    station: HyroxStation
    time: TimeStr


@tool(writes=True)
def log_hyrox_race(
    date: Day,
    total_time: Annotated[Optional[TimeStr], Field(description='Finish time, e.g. "1:28:45".')] = None,
    run_splits: Annotated[list[TimeStr], Field(description="The eight 1 km run splits in order (all eight or none).")] = [],
    station_splits: Annotated[list[StationSplit], Field(description="Station times from the result sheet (any subset).")] = [],
    division: Division = "open",
) -> dict[str, Any]:
    """Record a HYROX race or full simulation. With splits, each segment updates its own estimate; with
    only a total, everything is scaled together. Logging again for the same date replaces the earlier race."""
    if total_time is None and not run_splits and not station_splits:
        raise ApiError("give total_time and/or splits")
    if run_splits and len(run_splits) != 8:
        raise ApiError("run_splits must contain all eight 1 km splits (or be omitted)")
    stations = {s.station: parse_time(s.time) for s in station_splits}
    if len(stations) != len(station_splits):
        raise ApiError("station_splits lists a station twice")
    before = _hyrox_out(_athlete(date), division)["total"]
    race: dict = {"division": division}
    if total_time is not None:
        race["total"] = parse_time(total_time)
    if run_splits:
        race["run_splits"] = [parse_time(t) for t in run_splits]
    if stations:
        race["stations"] = stations
    minutes = (race.get("total") or sum(race.get("run_splits", [])) + sum(stations.values())) / 60.0
    out = _upsert(date, "race", lambda r: "hyrox_race" in r, {"hyrox_race": race},
                  {"srpe": 10, "duration_min": round(minutes, 1)})
    out.update(date=date.isoformat(), model_updates=[e for e in _events(date, "race") if e["kind"] == "hyrox"],
               race_prediction={"before": before["text"], "after": _hyrox_out(_athlete(date), division)["total"]["text"]})
    return out


def call(name: str, args: dict) -> dict:
    """이름으로 도구 호출 (CLI·테스트용)."""
    if name not in TOOLS:
        raise ApiError(f"unknown tool '{name}'. Tools: {sorted(TOOLS)}")
    return TOOLS[name]["fn"](**args)
