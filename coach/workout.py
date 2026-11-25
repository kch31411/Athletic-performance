"""운동 프로그램 YAML 스키마: 읽기 · 검증 · 정규화.

스키마 설명은 docs/schema.md 참고.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml

from . import catalog as C

FORMATS = {"for_time", "amrap", "emom", "interval", "rotation"}
INTENSITIES = {"light", "moderate", "heavy"}
STRENGTH_INTENTS = {"strength", "volume", "hyfit"}
AMOUNT_KEYS = ("reps", "cal", "distance_m", "seconds")


class SchemaError(ValueError):
    pass


def _sexed(value, sex: str):
    """{f: 12, m: 15} 꼴이면 성별에 맞는 값을 고른다."""
    if isinstance(value, dict) and set(value) <= {"f", "m"}:
        return value[sex]
    return value


def _metcon_item(raw: dict, sex: str, where: str) -> dict:
    if "choice" in raw or "scale" in raw:
        key = "choice" if "choice" in raw else "scale"
        opts = [_metcon_item(o, sex, where) for o in raw[key]]
        return {"options": opts, "scale": key == "scale"}
    if "movement" not in raw:
        raise SchemaError(f"{where}: movement 가 없습니다: {raw}")
    C.movement(raw["movement"])
    item = {"movement": raw["movement"]}
    for k in AMOUNT_KEYS:
        if raw.get(k) is not None:
            item[k] = float(_sexed(raw[k], sex))
    if raw.get("load_kg") is not None:
        item["load"] = float(_sexed(raw["load_kg"], sex))
    if raw.get("max"):
        item["max"] = True
    if raw.get("note"):
        item["note"] = str(raw["note"])
    unknown = set(raw) - {"movement", "load_kg", "max", "note", *AMOUNT_KEYS}
    if unknown:
        raise SchemaError(f"{where}: 알 수 없는 키 {sorted(unknown)}")
    return item


def _metcon(raw: dict, sex: str, where: str, parent: dict | None = None) -> dict:
    b: dict = {"type": "metcon"}
    inherit = ("format", "rounds", "time_cap_min", "duration_min", "interval_s", "work_s",
               "rest_s", "team", "intensity")
    src = dict(raw)
    for k in inherit:
        if k not in src and parent and k in parent:
            src[k] = parent[k]
    fmt = src.get("format")
    if fmt not in FORMATS:
        raise SchemaError(f"{where}: format 은 {sorted(FORMATS)} 중 하나 (지금: {fmt})")
    b["format"] = fmt
    b["name"] = src.get("name")
    b["rounds"] = int(src["rounds"]) if src.get("rounds") else None
    b["rep_scheme"] = [float(x) for x in src["rep_scheme"]] if src.get("rep_scheme") else None
    b["time_cap_s"] = float(src["time_cap_min"]) * 60 if src.get("time_cap_min") else None
    b["duration_s"] = float(src["duration_min"]) * 60 if src.get("duration_min") else None
    b["interval_s"] = float(src.get("interval_s") or 60)
    b["work_s"] = float(src["work_s"]) if src.get("work_s") else None
    b["rest_s"] = float(src["rest_s"]) if src.get("rest_s") else None
    b["intensity"] = src.get("intensity") or "moderate"
    if b["intensity"] not in INTENSITIES:
        raise SchemaError(f"{where}: intensity 는 {sorted(INTENSITIES)} 중 하나")
    team = dict(src.get("team") or {})
    team.setdefault("size", 1)
    team.setdefault("style", "solo" if team["size"] == 1 else "igyg")
    team.setdefault("rounds_scope", "each")
    if team["style"] not in ("solo", "igyg", "split", "rotation"):
        raise SchemaError(f"{where}: team.style 오류: {team['style']}")
    b["team"] = team
    b["progression"] = src.get("progression")
    b["items"] = [_metcon_item(i, sex, where) for i in src.get("items") or []]
    b["sections"] = [
        _metcon(s, sex, f"{where}/section{n + 1}", parent=src)
        for n, s in enumerate(src.get("sections") or [])
    ]
    b["stations"] = []
    for st in src.get("stations") or []:
        b["stations"].append({
            "role": st.get("role", "work"),
            "pacer": bool(st.get("pacer")),
            "items": [_metcon_item(i, sex, where) for i in st.get("items") or []],
        })
    if fmt == "amrap" and not b["duration_s"]:
        raise SchemaError(f"{where}: amrap 에는 duration_min 이 필요")
    if fmt == "rotation" and not (b["duration_s"] and any(s["pacer"] for s in b["stations"])):
        raise SchemaError(f"{where}: rotation 에는 duration_min 과 pacer 스테이션이 필요")
    if fmt == "interval" and not b["work_s"]:
        raise SchemaError(f"{where}: interval 에는 work_s 가 필요")
    if not (b["items"] or b["sections"] or b["stations"]):
        raise SchemaError(f"{where}: items / sections / stations 중 하나는 있어야 합니다")
    return b


def _strength_item(raw: dict, where: str, group: int | None) -> dict:
    if "movement" not in raw:
        raise SchemaError(f"{where}: movement 가 없습니다: {raw}")
    C.movement(raw["movement"])
    reps = raw.get("reps")
    if reps is None:
        lo = hi = None
    elif isinstance(reps, (list, tuple)):
        lo, hi = float(reps[0]), float(reps[-1])
    else:
        lo = hi = float(reps)
    unknown = set(raw) - {"movement", "reps", "per_side", "drop", "sets", "rpe", "main", "note"}
    if unknown:
        raise SchemaError(f"{where}: 알 수 없는 키 {sorted(unknown)}")
    return {
        "movement": raw["movement"], "reps_lo": lo, "reps_hi": hi,
        "per_side": bool(raw.get("per_side")), "drop": bool(raw.get("drop")),
        "sets": int(raw["sets"]) if raw.get("sets") else None,
        "rpe": float(raw["rpe"]) if raw.get("rpe") else None,
        "main": bool(raw.get("main")), "superset": group, "note": raw.get("note"),
    }


def _strength(raw: dict, where: str) -> dict:
    intent = raw.get("intent", "volume")
    if intent not in STRENGTH_INTENTS:
        raise SchemaError(f"{where}: intent 는 {sorted(STRENGTH_INTENTS)} 중 하나")
    items = []
    for n, it in enumerate(raw.get("items") or []):
        if "superset" in it:
            items += [_strength_item(s, where, n) for s in it["superset"]]
        else:
            items.append(_strength_item(it, where, None))
    if not items:
        raise SchemaError(f"{where}: items 가 비었습니다")
    return {"type": "strength", "intent": intent, "items": items}


def normalize_day(raw: dict, sex: str) -> dict:
    if "date" not in raw:
        raise SchemaError("date 가 없습니다")
    d = raw["date"] if isinstance(raw["date"], date) else date.fromisoformat(str(raw["date"]))
    sessions = []
    seen = set()
    for s in raw.get("sessions") or []:
        sid = s.get("id")
        if not sid or sid in seen:
            raise SchemaError(f"{d}: 세션 id 가 없거나 중복: {sid}")
        seen.add(sid)
        blocks = []
        for n, b in enumerate(s.get("blocks") or []):
            where = f"{d}/{sid}/block{n + 1}"
            if b.get("type") == "strength":
                blocks.append(_strength(b, where))
            elif b.get("type") == "metcon":
                blocks.append(_metcon(b, sex, where))
            else:
                raise SchemaError(f"{where}: type 은 strength 또는 metcon")
        sessions.append({
            "id": sid, "title": s.get("title", sid), "focus": s.get("focus"),
            "notes": list(s.get("notes") or []), "assumptions": list(s.get("assumptions") or []),
            "blocks": blocks,
        })
    return {"date": d, "gym": raw.get("gym"), "sessions": sessions}


def load_day(path: Path, sex: str) -> dict:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return normalize_day(raw, sex)


def find_day(root: Path, d: date, sex: str) -> dict | None:
    path = Path(root) / "data" / "workouts" / f"{d.isoformat()}.yaml"
    return load_day(path, sex) if path.exists() else None
