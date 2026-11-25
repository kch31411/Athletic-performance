"""처방서 · 능력치 현황 · 리뷰를 한국어 마크다운으로 출력."""
from __future__ import annotations

import math
from datetime import date

from . import catalog as C
from . import hyrox
from .athlete import LB, Athlete, MAX_PENALTY
from .metcon import evaluate
from .strength import confidence, plan_block

WEEKDAY = "월화수목금토일"
FORMAT_KO = {"for_time": "For Time", "amrap": "AMRAP", "emom": "EMOM", "interval": "인터벌",
             "rotation": "로테이션 AMRAP"}
INTENSITY_KO = {"light": "가볍게", "moderate": "중간", "heavy": "무겁게"}
KEY_LIFTS = ["back_squat", "deadlift", "bench_press", "strict_press", "barbell_row",
             "clean_and_jerk", "pull_up", "push_up"]


def kg(x: float) -> str:
    return f"{x:g}"


def mmss(s: float) -> str:
    s = int(round(s))
    return f"{s // 60}:{s % 60:02d}"


def num(x: float) -> str:
    return f"{x:.0f}" if abs(x - round(x)) < 0.05 else f"{x:.1f}"


def pct(x: float) -> str:
    return f"{(math.exp(x) - 1) * 100:+.0f}%"


_BAR_UNIT = "kg"


def set_units(athlete: Athlete) -> None:
    global _BAR_UNIT
    _BAR_UNIT = athlete.profile["equipment"]["barbell_unit"]


def is_lb(mid: str) -> bool:
    return _BAR_UNIT == "lb" and C.movement(mid)["implement"] == "barbell"


def wnum(mid: str, w: float) -> str:
    """표시 단위로 바꾼 숫자만."""
    return f"{round(w / LB / 5) * 5:g}" if is_lb(mid) else kg(w)


def wt(mid: str, w: float, both: bool = True) -> str:
    if is_lb(mid):
        return f"{wnum(mid, w)}lb" + (f"({w:.0f}kg)" if both else "")
    return f"{num(w)}kg"


def load_text(mid: str, w: float) -> str:
    m = C.movement(mid)
    if m["implement"] == "dumbbell":
        return f"덤벨 한 손 {kg(w)}kg" if m["hands"] == 2 else f"덤벨 {kg(w)}kg"
    if m["implement"] == "kettlebell":
        return f"케틀벨 {kg(w)}kg"
    return wt(mid, w)


def pace_text(mod: str, speed: float) -> str:
    if mod in ("row", "ski"):
        return f"{mmss(500 / speed)}/500m"
    if mod == "run":
        return f"{mmss(1000 / speed)}/km"
    return f"{mmss(1000 / speed)}/1000m"


def left_text(rir: float) -> str:
    """목표 강도를 '마지막 세트에서 몇 개 남기나'로 말한다."""
    lo, hi = math.floor(rir + 1e-9), math.ceil(rir - 1e-9)
    if hi <= 0:
        return "마지막 세트는 한계까지"
    span = f"{lo}개" if lo == hi else f"{lo}~{hi}개"
    return f"마지막 세트 끝에 {span} 더 할 수 있는 정도"


def is_cold(athlete: Athlete) -> bool:
    return sum(athlete.n_obs.values()) < 5


# ── 컨디션 요약 ──────────────────────────────────────────
def condition_lines(athlete: Athlete) -> list[str]:
    lines = []
    cond = athlete.b.value("cond")
    if abs(cond) >= 0.005:
        lines.append(f"체크인 반영 컨디션 {pct(cond)}")
    tired = []
    for p in C.PATTERNS:
        pen = min(MAX_PENALTY, 0.012 * athlete.fatigue.get(p, 0.0))
        if pen >= 0.01:
            tired.append(f"{C.PATTERN_KO[p]} −{pen * 100:.0f}%")
    if tired:
        lines.append("남은 피로: " + ", ".join(tired))
    if athlete.sys_fatigue >= 0.5:
        lines.append(f"전신 피로 지수 {athlete.sys_fatigue:.1f} (1.0 ≈ 어제 힘든 수업 1회)")
    acwr = athlete.acwr()
    if acwr is not None:
        flag = " ⚠️ 급격한 부하 증가 — 오늘은 세트마다 1개씩 더 남기세요" if acwr > 1.5 else ""
        lines.append(f"최근 7일/28일 부하 비 {acwr:.2f}{flag}")
    return lines or ["특이사항 없음 (누적 피로 미미)"]


# ── 근력 블록 ────────────────────────────────────────────
def _reps_text(p: dict) -> str:
    r = num(p["reps_lo"]) if p["reps_lo"] == p["reps_hi"] else f"{num(p['reps_lo'])}~{num(p['reps_hi'])}"
    if p["per_side"]:
        return f"{r}/{r}회"
    return f"{r}회"


def strength_lines(plans: list[dict]) -> list[str]:
    out = []
    n = 0
    prev_group = object()
    for p in plans:
        name = C.ko(p["movement"])
        in_superset = p["superset"] is not None
        if not in_superset or p["superset"] != prev_group:
            n += 1
            lead = f"{n}. "
        else:
            lead = "   ＋ "
        prev_group = p["superset"] if in_superset else object()
        tag = " (슈퍼세트)" if in_superset and lead.strip() != "＋" else ""
        head = f"{lead}**{name}**{tag} {p['sets']}세트 × {_reps_text(p)}"
        rpe = left_text(10 - p["rpe"])
        sub = []
        if p["mode"] == "load":
            head += f" — **{load_text(p['movement'], p['load'])}**"
            if p["load_lo"] != p["load_hi"]:
                head += f" (범위 {wnum(p['movement'], p['load_lo'])}~{wnum(p['movement'], p['load_hi'])})"
            head += f" · {rpe}"
            sub.append(f"추정 1RM {wt(p['movement'], p['e1rm'])} · 신뢰도 {p['confidence']}"
                       + (f" · 오늘 피로 −{p['penalty'] * 100:.0f}% 반영" if p["penalty"] >= 0.01 else ""))
            if p.get("warmup"):
                sub.append("워밍업: " + " → ".join(f"{wnum(p['movement'], w)}×{r}" for w, r in p["warmup"]))
            if p.get("drops"):
                sub.append("드랍: 마지막 세트 직후 " + " → ".join(wt(p["movement"], w, False) for w in p["drops"])
                           + " 로 내려 각각 한계 직전까지")
        elif p["mode"] == "weighted":
            head += f" — **중량 +{kg(p['load'])}kg** · {rpe}"
            sub.append(f"맨몸 연속 최대 약 {p['rmax']:.0f}회 예상 · 신뢰도 {p['confidence']}")
        elif p["mode"] == "bodyweight":
            head += f" — **맨몸** · {rpe}"
            sub.append(f"연속 최대 약 {p['rmax']:.0f}회 예상 · 신뢰도 {p['confidence']}")
        elif p["mode"] == "assist":
            head += f" — **보조 약 {kg(p['assist'])}kg(밴드)** 또는 맨몸 {p['chunk']}회씩 끊어서"
            sub.append(f"맨몸 연속 최대 약 {p['rmax']:.0f}회 예상 · 신뢰도 {p['confidence']}")
        else:
            head += " — 맨몸, 빠르고 정확하게"
        if p["assumed_reps"]:
            sub.append("횟수 미지정 → 12회 기준으로 산정")
        if p.get("note"):
            sub.append(str(p["note"]))
        out.append(head)
        out += [f"   - {s}" for s in sub]
    return out


# ── 메트콘 블록 ──────────────────────────────────────────
def _amount(it: dict, block: dict) -> str:
    if it.get("max"):
        return "최대"
    if it.get("reps") is not None:
        return f"{num(it['reps'])}회"
    if it.get("cal") is not None:
        return f"{num(it['cal'])}cal"
    if it.get("distance_m") is not None:
        return f"{num(it['distance_m'])}m"
    if it.get("seconds") is not None:
        return f"{num(it['seconds'])}초"
    if block.get("rep_scheme"):
        return "-".join(num(x) for x in block["rep_scheme"])
    return ""


def _view_line(v: dict, block: dict, share: float = 1.0) -> str:
    it = v["item"]
    mid = v["movement"]
    line = f"**{C.ko(mid)}** {_amount(it, block)}"
    bits = []
    if share < 1.0 and v.get("reps") is not None:
        bits.append(f"내 몫 {num(v['reps'])}회")
    if share < 1.0 and v.get("erg") and v["erg"].get("cal") and it.get("cal"):
        bits.append(f"내 몫 {num(v['erg']['cal'])}cal")
    if v.get("load") is not None and C.movement(mid)["implement"] != "bodyweight":
        tail = " (지정 무게)" if it.get("load") is not None else ""
        line += f" — **{load_text(mid, v['load'])}**{tail}"
        if v.get("limited_by"):
            bits.append(f"{C.ko(v['limited_by'])}에 맞춘 무게")
    if v.get("erg"):
        e = v["erg"]
        bits.append(f"약 {mmss(v['t'])} · {e['rate_cal_min']:.0f}cal/분 · {pace_text(e['mod'], e['speed'])}")
    if v.get("rmax") is not None:
        if v.get("split"):
            bits.append(f"연속 최대 ~{v['rmax']:.0f}회 → " + "-".join(str(x) for x in v["split"]) + " 로 끊기")
        elif v["rmax"] < 150 and v.get("d", 0) > 0.3:
            bits.append(f"연속 최대 ~{v['rmax']:.0f}회 → 언브로큰 목표")
        elif v.get("d", 1) <= 0.3:
            bits.append("언브로큰")
    if v.get("scaled"):
        bits.append("수준에 맞춰 고른 옵션")
    if bits:
        line += " · " + " · ".join(bits)
    return line


def _alt_lines(athlete: Athlete, block: dict, v: dict, share: float) -> list[str]:
    out = []
    for alt in v.get("alts") or []:
        ev = evaluate(athlete, block, picks=[alt["movement"]])
        av = next((x for x in ev.get("views", []) if x["movement"] == alt["movement"]), None)
        label = "더 어려운/쉬운 옵션" if v.get("scaled") else "선택지"
        out.append(f"   - {label}: " + (_view_line(av, block, share) if av else C.ko(alt["movement"])))
    return out


def _range(center: float, sd: float, fmt=mmss) -> str:
    return f"{fmt(center * math.exp(-sd))}~{fmt(center * math.exp(sd))}"


def _team_text(block: dict) -> str:
    team = block["team"]
    if team["size"] <= 1:
        return ""
    style = {"igyg": "번갈아(I go you go)", "split": "횟수 나눠서", "rotation": "로테이션"}[team["style"]]
    return f" · {team['size']}인 1조 {style}"


def metcon_lines(athlete: Athlete, ev: dict) -> list[str]:
    block = ev["block"]
    fmt = ev["format"]
    out = []
    if fmt == "sections":
        for i, part in enumerate(ev["parts"]):
            out.append(f"**[{part['block'].get('name') or f'섹션 {i + 1}'}]**")
            out += metcon_lines(athlete, part)
        return out

    head = FORMAT_KO[block["format"]]
    if block.get("rep_scheme"):
        head += " " + "-".join(num(x) for x in block["rep_scheme"])
    elif block.get("rounds") and fmt in ("for_time", "emom", "interval"):
        head += f" {block['rounds']}라운드"
    if block.get("duration_s"):
        head += f" {num(block['duration_s'] / 60)}분"
    head += _team_text(block)
    if block.get("time_cap_s"):
        head += f" · 타임캡 {mmss(block['time_cap_s'])}"
    head += f" · 무게 기조: {INTENSITY_KO[block['intensity']]}"
    out.append(f"_{head}_")

    if fmt in ("for_time", "amrap", "rotation"):
        share = ev.get("cfg", {}).get("share", 1.0)
        for v in ev["views"]:
            out.append("- " + _view_line(v, block, share))
            out += _alt_lines(athlete, block, v, share)
    p = ev["pred"]
    if fmt == "for_time":
        own_round = p["own_s"] / max(p["own_rounds"], 1)
        target = f"🎯 **목표 기록 {mmss(p['elapsed_s'])}** (예상 범위 {_range(p['elapsed_s'], p['sd_log'])})"
        if block.get("rep_scheme"):
            target += " · 라운드별 " + " / ".join(mmss(t) for t in p["round_s"])
        elif block["team"]["size"] > 1:
            target += f" · 내 작업 라운드당 ~{mmss(own_round)}"
        else:
            target += f" · 라운드당 ~{mmss(own_round)}"
        out.append(target)
        if p["capped"]:
            out.append(f"⚠️ 이 무게면 타임캡 안에 약 {p['cap_frac'] * 100:.0f}% 지점 예상 — "
                       "완주가 목표면 무게를 한 단계 낮추세요")
    elif fmt == "amrap":
        score = p["team_rounds"]
        out.append(f"🎯 **목표 {math.floor(score)}라운드 + α** (예상 {score:.1f}라운드, "
                   f"범위 {_range(score, p['sd_log'], lambda x: f'{x:.1f}')}) · 라운드당 ~{mmss(p['round_s'][0]) if p['round_s'] else '-'}")
    elif fmt == "rotation":
        out.append(f"🎯 **동작 1라운드 ~{mmss(p['stint_s'])}** 안에 끊지 않고 → "
                   f"팀 로테이션 약 {p['team_rounds']:.0f}회 (내 동작 차례 ~{p['own_rounds']:.0f}회)")
        for mc in p["machines"]:
            out.append(f"- **머신({C.ERG_KO[mc['mod']]})** 차례마다 **~{mc['cal_per_stint']:.0f}cal** "
                       f"({mc['rate_cal_min']:.0f}cal/분, {pace_text(mc['mod'], mc['speed'])}) · "
                       f"30분 동안 내 누적 ~{mc['own_cal']:.0f}cal")
    elif fmt == "emom":
        interval = block["interval_s"]
        out.append(f"_{num(interval)}초마다 1개 스테이션 · 총 {num(p['total_min'])}분_")
        for i, st in enumerate(ev["stations"]):
            it, first, last = st["item"], st["first"], st["last"]
            a0 = first.get("reps", first.get("cal", 0.0))
            a1 = last.get("reps", last.get("cal", 0.0))
            unit = "cal" if first["kind"] == "erg" else "회"
            amount = f"{num(a0)}{unit}" if a0 == a1 else f"{num(a0)}→{num(a1)}{unit}"
            line = f"- M{i + 1} **{C.ko(st['movement'])}** {amount}"
            if st.get("load") is not None and C.movement(st["movement"])["implement"] != "bodyweight":
                tail = " (지정 무게)" if it.get("load") is not None else ""
                line += f" — **{load_text(st['movement'], st['load'])}**{tail}"
            if a0 == a1:
                line += f" · 예상 {mmss(first['t_final'])} ({st['fit_first']})"
            else:
                line += f" · 예상 {mmss(first['t_final'])}→{mmss(last['t_final'])} ({st['fit_first']}→{st['fit_last']})"
            if st.get("erg"):
                e = st["erg"]
                line += f" · {e['rate_cal_min']:.0f}cal/분 · {pace_text(e['mod'], e['speed'])}"
            if st.get("scale_to") is not None and st["scale_to"] < a1:
                line += f" · ⚠️ 휴식 확보하려면 {st['scale_to']}{unit}까지"
            out.append(line)
            for alt in st.get("alts") or []:
                out.append(f"   - 선택지: {C.ko(alt['movement'])} {_amount(alt, block)}")
    elif fmt == "interval":
        rest = block.get("rest_s") or 0
        out.append(f"_{mmss(block['work_s'])} 운동 / {mmss(rest)} 휴식 · 총 {num(p['total_min'])}분_")
        for i, sec in enumerate(ev["sections"]):
            parts = []
            for ln in sec["lines"]:
                name = C.ko(ln["movement"])
                x = ln.get("x")
                load = ""
                if ln.get("load") is not None and C.movement(ln["movement"])["implement"] != "bodyweight":
                    load = f" {load_text(ln['movement'], ln['load'])}"
                if ln.get("max"):
                    unit = "cal" if C.movement(ln["movement"])["implement"] == "erg" else "회"
                    parts.append(f"**{name}{load} 최대 → 목표 {ln['score']:.0f}{unit}** (남는 시간 {mmss(ln['remain_s'])})")
                elif x and x["kind"] == "erg" and "seconds" in ln["item"]:
                    parts.append(f"{name} {num(ln['item']['seconds'])}초 전력 → ~{x['cal']:.0f}cal")
                elif x:
                    parts.append(f"{name}{load} {_amount(ln['item'], block)} ~{mmss(x['t_final'])}")
            out.append(f"- [{sec.get('name') or f'섹션 {i + 1}'}] " + " → ".join(parts))
    return out


# ── 하루 처방서 ──────────────────────────────────────────
def plan_day(athlete: Athlete, day: dict, only: list[str] | None = None) -> str:
    set_units(athlete)
    d: date = day["date"]
    prof = athlete.profile
    lvl = next((C.LEVEL_KO[k] for k, v in C.LEVELS.items() if v == prof["level_num"]), f"수준 {prof['level_num']:g}")
    out = [f"# {d.month}.{d.day} ({WEEKDAY[d.weekday()]}) 훈련 처방", ""]
    out.append(f"- 프로필: {'남' if athlete.sex == 'm' else '여'} · {kg(athlete.bw)}kg · {lvl} "
               f"· 반영된 기록 {sum(athlete.n_obs.values())}건")
    out += [f"- 오늘 상태: {x}" for x in condition_lines(athlete)]
    if is_cold(athlete):
        out.append("- ⚠️ 아직 기록이 거의 없어 **수준 평균값으로 추정** 중입니다. 범위의 아래쪽에서 시작해 "
                   "남는 개수로 맞추고, 결과를 알려주면 다음 처방부터 빠르게 정확해집니다.")
    for s in day["sessions"]:
        if only and s["id"] not in only:
            continue
        title = s["title"] + (f" — {s['focus']}" if s.get("focus") else "")
        out += ["", f"## {title}  `{s['id']}`"]
        if s["notes"]:
            out.append("> 코치 노트: " + " / ".join(s["notes"]))
        for b in s["blocks"]:
            out.append("")
            if b["type"] == "strength":
                out += strength_lines(plan_block(athlete, b))
                out.append("")
                step = "+5~10lb" if _BAR_UNIT == "lb" else "+2.5~5kg"
                out.append(f"조절 규칙: 첫 세트 끝에 4개 이상 더 할 수 있겠으면 {step}, "
                           "목표보다 덜 남을 것 같으면 −5%.")
            else:
                out += metcon_lines(athlete, evaluate(athlete, b))
        if s["assumptions"]:
            out.append("")
            out.append("해석/가정: " + " · ".join(s["assumptions"]))
    out += ["", "---", "끝나고 알려줄 것",
            "- 근력: 종목별 실제 무게×횟수×세트 + **마지막 세트에서 몇 개 더 할 수 있었는지**",
            "- 메트콘: 기록(시간·라운드), 쓴 무게, 무게가 가벼웠는지·무거웠는지, 그리고 끝났을 때 상태 —",
            "  ① 전력(더 빨리는 불가능) ② 거의 전력(막판 스퍼트만 남음) "
            "③ 버틸 만함(같은 페이스로 몇 분 더 가능) ④ 편했음(대화 가능)"]
    return "\n".join(out) + "\n"


# ── 능력치 현황 ──────────────────────────────────────────
def status_data(athlete: Athlete) -> dict:
    b = athlete.b
    lifts = {}
    for mid in dict.fromkeys(KEY_LIFTS + athlete.observed):
        mean, sd = athlete.log1rm(mid, today=False)
        row = {"e1rm_kg": round(math.exp(mean), 1), "sd_pct": round(sd * 100, 1)}
        if C.movement(mid)["bw_frac"] > 0:
            row["max_reps"] = round(athlete.rmax(mid, today=False), 1)
        lifts[mid] = row
    engine = {}
    for mod in C.ERG_RATIO:
        cp = athlete.erg_cp(mod, today=False) * 60
        _, var = b.predict(athlete.erg_h(mod))
        engine[mod] = {"cal_per_min": round(cp, 1), "sd_pct": round(math.sqrt(var) * 100, 1),
                       "cal_1min": round(athlete.erg_rate(mod, 60, today=False) * 60, 1)}
    factors = {n: {"value": round(float(b.mean[i]), 4), "sd": round(b.sd(n), 4)}
               for n, i in sorted(b.idx.items()) if n != "cond"}
    return {
        "as_of": athlete.today.isoformat() if athlete.today else None,
        "observations": dict(sorted(athlete.n_obs.items())),
        "strength_1rm": lifts, "engine": engine, "factors": factors,
        "fatigue": {p: round(v, 2) for p, v in athlete.fatigue.items() if v >= 0.05},
        "systemic_fatigue": round(athlete.sys_fatigue, 2), "acwr": athlete.acwr(),
    }


def status_md(athlete: Athlete) -> str:
    set_units(athlete)
    b = athlete.b
    as_of = athlete.today.isoformat() if athlete.today else "-"
    out = [f"# 능력치 현황 ({as_of} 기준)", "",
           f"반영된 기록 {sum(athlete.n_obs.values())}건. 값은 '프로필 수준의 평균적인 사람' 대비 편차이며, "
           "±는 모델이 아직 확신하지 못하는 폭입니다.", ""]

    def axis(label: str, h: dict[str, float]) -> str:
        mean, var = b.predict(h)
        return f"| {label} | {pct(mean)} | ±{math.sqrt(var) * 100:.0f}% |"

    out += ["## 큰 그림", "", "| 축 | 수준 평균 대비 | 불확실성 |", "|---|---|---|",
            axis("근력", {"str": 1.0}),
            axis("근지구력(고반복)", {"end": 1.0}),
            axis("파워(올림픽 리프팅)", {"str": 1.0, "str.pat:olympic": 1.0}),
            axis("심폐 엔진(지속 출력)", {"eng": 1.0}),
            axis("무산소 여유(단거리 전력)", {"ana": 1.0}),
            axis("복합 수행능력(메트콘 페이스)", {"met": 1.0}), ""]

    out += ["## 근력 — 추정 1RM", "", "| 종목 | 1RM | 범위 | 체중비 | 신뢰도 |", "|---|---|---|---|---|"]
    for mid, row in status_data(athlete)["strength_1rm"].items():
        mean, sd = athlete.log1rm(mid, today=False)
        one = math.exp(mean)
        bwf = C.movement(mid)["bw_frac"]
        if bwf > 0:
            name = f"{C.ko(mid)} (연속 최대 ~{athlete.rmax(mid, today=False):.0f}회)"
            val = f"체중 포함 {one:.0f}kg"
        else:
            name, val = C.ko(mid), f"{one:.0f}kg" + (f" ({one / LB:.0f}lb)" if is_lb(mid) else "")
        out.append(f"| {name} | {val} | {one * math.exp(-sd):.0f}~{one * math.exp(sd):.0f} "
                   f"| {one / athlete.bw:.2f} | {confidence(sd)} |")

    out += ["", "## 패턴별 근력 · 근지구력", "", "| 패턴 | 근력 편차 | 근지구력 편차 | 관측 |", "|---|---|---|---|"]
    for p in C.PATTERNS:
        if p in ("core", "plyo"):
            continue
        s, _ = b.predict({"str": 1.0, f"str.pat:{p}": 1.0})
        e, _ = b.predict({"end": 1.0, f"end.pat:{p}": 1.0})
        out.append(f"| {C.PATTERN_KO[p]} | {pct(s)} | {pct(e)} | {athlete.n_obs.get(f'str:{p}', 0)}건 |")

    out += ["", "## 심폐 엔진", "", "| 머신 | 지속 가능(10분+) | 1분 전력 | 불확실성 |", "|---|---|---|---|"]
    for mod, row in status_data(athlete)["engine"].items():
        out.append(f"| {C.ERG_KO[mod]} | {row['cal_per_min']:.1f}cal/분 | {row['cal_1min']:.0f}cal | ±{row['sd_pct']:.0f}% |")

    out += ["", "## 복합 수행능력(메트콘)", ""]
    for label, name in (("전체", "met"), ("웨이트 위주", "met.cat:W"), ("맨몸 위주", "met.cat:G"),
                        ("짧은(6분↓)", "met.dur:short"), ("중간(6~18분)", "met.dur:medium"),
                        ("긴(18분↑)", "met.dur:long")):
        out.append(f"- {label}: {pct(b.value(name))} (±{b.sd(name) * 100:.0f}%)")
    out.append(f"- 메트콘 무게 선호 보정: {pct(b.value('loadpref'))}")

    race = hyrox.predict(athlete, "open")
    out += ["", "## HYROX 오픈 싱글", "",
            f"- 예상 {hms(race['total_s'])} (범위 {hms(race['total_s'] * math.exp(-race['sd_log']))}"
            f"~{hms(race['total_s'] * math.exp(race['sd_log']))}) · HYROX 기록 {athlete.n_obs.get('hyx', 0)}건 반영"]
    out += ["", "## 컨디션 · 피로", ""] + [f"- {x}" for x in condition_lines(athlete)]
    return "\n".join(out) + "\n"


# ── HYROX 레이스 예측 ────────────────────────────────────
def hms(s: float) -> str:
    s = int(round(s))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else mmss(s)


def hyrox_md(athlete: Athlete, pred: dict) -> str:
    div = {"open": "오픈", "pro": "프로"}[pred["division"]]
    lo, hi = pred["total_s"] * math.exp(-pred["sd_log"]), pred["total_s"] * math.exp(pred["sd_log"])
    out = [f"# HYROX {div} 싱글 예상 기록", "",
           f"**{hms(pred['total_s'])}** (예상 범위 {hms(lo)}~{hms(hi)}) · "
           f"같은 조건의 중급 평균 선수 {hms(pred['reference']['total_s'])}", "",
           f"- 러닝 8km 합계 {hms(pred['run_total_s'])} · 스테이션 합계 {hms(pred['station_total_s'])} "
           f"· 이동 구간 {mmss(pred['roxzone_s'])}",
           f"- HYROX 기록 반영 {athlete.n_obs.get('hyx', 0)}건"
           + (" — 아직 없어서 다른 능력치에서 간접 추정한 값입니다." if not athlete.n_obs.get("hyx") else ""),
           "", "| 구간 | 예상 | 평균 대비 |", "|---|---|---|"]
    for leg in pred["legs"]:
        if leg["kind"] == "run":
            out.append(f"| 러닝 {leg['index']} (1km) | {mmss(leg['seconds'])} | |")
        else:
            st = leg["station"]
            load = f" · {kg(leg['load_kg'])}kg" if leg.get("load_kg") else ""
            diff = leg["vs_reference_s"]
            mark = "비슷" if abs(diff) < 5 else (f"+{diff:.0f}초 느림" if diff > 0 else f"{-diff:.0f}초 빠름")
            out.append(f"| **{hyrox.STATION_KO[st]}**{load} | **{mmss(leg['seconds'])}** | {mark} |")
    rd = pred["run_vs_reference_s"]
    out += ["", f"러닝 8km 는 평균 대비 " + ("비슷합니다." if abs(rd) < 20 else
            (f"{rd:.0f}초 느립니다." if rd > 0 else f"{-rd:.0f}초 빠릅니다."))]
    slow = sorted((l for l in pred["legs"] if l["kind"] == "station" and l["vs_reference_s"] >= 5),
                  key=lambda l: -l["vs_reference_s"])[:3]
    if slow:
        out.append("시간을 가장 많이 잃는 구간: " + ", ".join(
            f"{hyrox.STATION_KO[l['station']]}(+{l['vs_reference_s']:.0f}초)" for l in slow))
    return "\n".join(out) + "\n"


# ── 리뷰 (예측 vs 실제) ──────────────────────────────────
def review_md(athlete: Athlete, day: date) -> str:
    set_units(athlete)
    prefix = day.isoformat()
    events = [e for e in athlete.events if e.get("tag", "").startswith(prefix)]
    out = [f"# {day.month}.{day.day} ({WEEKDAY[day.weekday()]}) 리뷰 — 예측 vs 실제", ""]
    if not events:
        out.append("이 날짜에 능력치 갱신으로 이어진 기록이 없습니다 (남은 개수·기록 시간이 빠졌는지 확인).")
        return "\n".join(out) + "\n"
    for e in events:
        sid = e["tag"].split("/", 1)[1]
        s = e["surprise"]
        if e["kind"] == "strength":
            st = e["set"]
            rpe = f"({st['rir']:g}개 남김)" if st["rir"] is not None else ""
            load = f"{wt(e['movement'], st['load'])} × " if st["load"] else ""
            verdict = "예상대로" if abs(s) < 0.02 else (f"예상보다 {abs(s) * 100:.0f}% {'강함' if s > 0 else '약함'}")
            out.append(f"- `{sid}` **{C.ko(e['movement'])}** {load}{num(st['reps'])}회 {rpe} → {verdict} · "
                       f"추정 1RM {e['e1rm_before']:.1f} → **{e['e1rm_after']:.1f}kg**")
        elif e["kind"] == "metcon":
            if "pred_s" in e:
                body = f"예측 {mmss(e['pred_s'])} / 실제(전력 환산) {mmss(e['actual_s'])}"
            else:
                body = f"예측 {e['pred_rounds']:.1f} / 실제 {e['actual_rounds']:.1f} 라운드"
            verdict = "예상대로" if abs(s) < 0.03 else (f"예상보다 {abs(s) * 100:.0f}% {'느림' if s > 0 else '빠름'}")
            out.append(f"- `{sid}` **메트콘** {body} → {verdict} · "
                       f"복합 수행능력 {pct(e['met_before'])} → **{pct(e['met_after'])}**")
        elif e["kind"] == "hyrox":
            name = {"run": "HYROX 러닝", "total": "HYROX 총 기록"}.get(e["what"]) or hyrox.STATION_KO[e["what"]]
            verdict = "예상대로" if abs(s) < 0.03 else (f"예상보다 {abs(s) * 100:.0f}% {'느림' if s > 0 else '빠름'}")
            out.append(f"- `{sid}` **{name}** 예측 {hms(e['pred_s'])} / 실제 {hms(e['actual_s'])} → {verdict}")
        elif e["kind"] == "erg":
            verdict = "예상대로" if abs(s) < 0.03 else (f"예상보다 {abs(s) * 100:.0f}% {'높음' if s > 0 else '낮음'}")
            out.append(f"- `{sid}` **{C.ERG_KO[e['mod']]}** {mmss(e['seconds'])} 구간 출력 → {verdict} · "
                       f"지속 가능 출력 {e['cp_before']:.1f} → **{e['cp_after']:.1f}cal/분**")
    out += ["", "이 변화는 다음 처방부터 자동 반영됩니다."]
    return "\n".join(out) + "\n"
