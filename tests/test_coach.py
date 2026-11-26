"""엔진의 핵심 성질 검증: 갱신 방향, 수렴, 재생 일관성, 스키마."""
import math
import shutil
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from coach import render
from coach.athlete import Athlete
from coach.belief import Belief
from coach.feedback import apply_erg, apply_strength, parse_time
from coach.metcon import evaluate, loadpref_h, suggest_split
from coach.replay import build
from coach.strength import plan_block, set_fatigue
from coach.workout import SchemaError, find_day, normalize_day

REPO = Path(__file__).resolve().parent.parent
DAYS = [date(2026, 11, d) for d in range(23, 29)]


def athlete(**kw) -> Athlete:
    prof = {"sex": "m", "bodyweight_kg": 80, "level": "novice"}
    prof.update(kw)
    a = Athlete(prof)
    a.advance_to(date(2026, 11, 23))
    return a


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "data" / "athlete").mkdir(parents=True)
    (tmp_path / "data" / "logs").mkdir()
    shutil.copytree(REPO / "data" / "workouts", tmp_path / "data" / "workouts")
    (tmp_path / "data" / "athlete" / "profile.yaml").write_text(
        yaml.safe_dump({"sex": "m", "bodyweight_kg": 80, "level": "novice"}))
    return tmp_path


def write_log(root: Path, day: str, body: dict) -> None:
    (root / "data" / "logs" / f"{day}.yaml").write_text(yaml.safe_dump({"date": day, **body}))


# ── 믿음 갱신 ────────────────────────────────────────────
def test_belief_converges_and_shrinks():
    b = Belief()
    h = {"str": 1.0, "str.pat:squat": 1.0, "str.mov:back_squat": 1.0}
    _, var0 = b.predict(h)
    for _ in range(6):
        mean, _ = b.predict(h)
        b.observe(h, 0.20 - mean, 0.04)
    mean, var = b.predict(h)
    assert abs(mean - 0.20) < 0.02
    assert var < var0 / 10


def test_observation_generalises_by_hierarchy():
    """스쿼트 기록은 프론트 스쿼트 > 벤치 순으로 다른 종목 추정에도 반영된다."""
    a = athlete()
    before = {m: a.one_rm(m, today=False) for m in ("back_squat", "front_squat", "bench_press")}
    target = before["back_squat"] * 1.25
    apply_strength(a, {"movement": "back_squat", "load": round(target * 0.9), "reps": 3, "rpe": 9.5})
    gain = {m: a.one_rm(m, today=False) / before[m] for m in before}
    assert gain["back_squat"] > gain["front_squat"] > gain["bench_press"] > 1.0


def test_easy_set_is_only_a_lower_bound():
    a = athlete()
    before = a.one_rm("bench_press", today=False)
    apply_strength(a, {"movement": "bench_press", "load": 20, "reps": 10, "rpe": 3}, fatigue=False)
    assert a.one_rm("bench_press", today=False) == pytest.approx(before)
    apply_strength(a, {"movement": "bench_press", "load": before, "reps": 5, "rpe": 4}, fatigue=False)
    assert a.one_rm("bench_press", today=False) > before


def test_high_rep_sets_inform_endurance_more_than_low_rep_sets():
    lo, hi = athlete(), athlete()
    w = lo.one_rm("bench_press")
    apply_strength(lo, {"movement": "bench_press", "load": w * 0.95, "reps": 3, "rpe": 10}, fatigue=False)
    apply_strength(hi, {"movement": "bench_press", "load": w * 0.70, "reps": 20, "rpe": 10}, fatigue=False)
    assert hi.b.value("end") > lo.b.value("end") > 0


def test_closed_loop_converges_to_true_strength():
    """실제 1RM 이 사전값보다 30% 높은 사람: 처방 → 수행 → RPE 보고를 반복하면 따라잡는다."""
    a = athlete()
    truth = a.one_rm("bench_press", today=False) * 1.30
    item = {"movement": "bench_press", "reps_lo": 8.0, "reps_hi": 8.0, "per_side": False, "drop": False,
            "sets": 4, "rpe": None, "main": False, "superset": None}
    rir = None
    for i in range(6):
        a.advance_to(date(2026, 11, 23) + timedelta(days=3 * i))
        plan = plan_block(a, {"intent": "volume", "items": [item]})[0]
        load = plan["load"]
        # 마지막 세트에서 실제로 남은 반복 수 → RPE
        true_rmax = 1 + 28 * (truth / load - 1)
        rir = max(0.0, true_rmax - 8 - set_fatigue(3))
        apply_strength(a, {"movement": "bench_press", "load": load, "reps": 8, "sets": 4,
                           "rpe": max(10 - rir, 1)}, fatigue=False)
    assert a.one_rm("bench_press", today=False) == pytest.approx(truth, rel=0.06)
    assert 1.0 <= rir <= 3.5        # 목표(마지막 세트 RIR 2)에 맞는 무게를 찾아냈다


def test_erg_feedback_moves_engine():
    a = athlete()
    cp = a.erg_cp("row", today=False)
    apply_erg(a, {"erg": "row", "cal": a.erg_rate("row", 240) * 240 * 1.2, "seconds": 240})
    assert a.erg_cp("row", today=False) > cp * 1.08
    assert a.erg_cp("ski", today=False) > cp * 0.95 * 1.04     # 다른 머신에도 일부 전이


# ── 피로 / 컨디션 ────────────────────────────────────────
def test_fatigue_lowers_next_day_prescription_then_recovers(root):
    write_log(root, "2026-11-24", {"sessions": [{"id": "weight", "srpe": 9, "duration_min": 60, "results": [
        {"movement": "back_squat", "sets": [{"load": 70, "reps": 5, "rpe": 9}] * 6}]}]})
    fresh = build(root, until=date(2026, 11, 23))
    next_day = build(root, until=date(2026, 11, 25))
    week_later = build(root, until=date(2026, 12, 2))
    assert next_day.fatigue_penalty("back_squat") > 0.03
    assert next_day.fatigue_penalty("back_squat") > next_day.fatigue_penalty("bench_press")
    assert week_later.fatigue_penalty("back_squat") < 0.005
    assert fresh.fatigue_penalty("back_squat") == 0


def test_checkin_shifts_todays_condition_only(root):
    write_log(root, "2026-11-25", {"checkin": {"readiness": 1, "sleep_h": 4}})
    today = build(root, until=date(2026, 11, 25))
    tomorrow = build(root, until=date(2026, 11, 26))
    base = Athlete({"sex": "m", "bodyweight_kg": 80, "level": "novice"})
    assert today.one_rm("deadlift") < base.one_rm("deadlift") * 0.96
    assert tomorrow.one_rm("deadlift") == pytest.approx(base.one_rm("deadlift"))


# ── 메트콘 ───────────────────────────────────────────────
def test_metcon_scales_sensibly_with_level_and_load():
    day = find_day(REPO, date(2026, 11, 26), "m")
    block = day["sessions"][2]["blocks"][0]                    # 21-15-9
    nov, adv = athlete(level="novice"), athlete(level="advanced")
    e_nov, e_adv = evaluate(nov, block), evaluate(adv, block)
    assert e_adv["pred"]["elapsed_s"] < e_nov["pred"]["elapsed_s"]
    assert e_adv["loads"]["hang_clean_and_jerk"] > e_nov["loads"]["hang_clean_and_jerk"]
    assert e_nov["loads"]["front_squat"] == e_nov["loads"]["hang_clean_and_jerk"]   # 바벨 공유
    heavier = evaluate(nov, block, loads={"hang_clean_and_jerk": e_nov["loads"]["hang_clean_and_jerk"] + 15,
                                           "front_squat": e_nov["loads"]["front_squat"] + 15})
    assert heavier["pred"]["elapsed_s"] > e_nov["pred"]["elapsed_s"]
    assert e_nov["pred"]["round_s"][0] > e_nov["pred"]["round_s"][1] > e_nov["pred"]["round_s"][2]


def test_metcon_result_updates_capacity_and_next_prediction(root):
    before = build(root, until=date(2026, 11, 27))
    block = find_day(root, date(2026, 11, 26), "m")["sessions"][2]["blocks"][0]
    pred = evaluate(build(root, until=date(2026, 11, 26)), block)["pred"]["elapsed_s"]
    write_log(root, "2026-11-26", {"sessions": [{"id": "cardio", "results": [
        {"block": 1, "time": parse_time("10:00") if pred > 700 else pred * 0.8, "rpe": 9.5, "load_feel": "light"}]}]})
    after = build(root, until=date(2026, 11, 27))
    assert after.b.value("met") > before.b.value("met")
    assert after.b.value("loadpref") > 0
    sat = find_day(root, date(2026, 11, 28), "m")["sessions"][1]["blocks"][0]
    t0 = evaluate(build(root, until=date(2026, 11, 28), skip_sessions_on=date(2026, 11, 26)), sat)
    t1 = evaluate(build(root, until=date(2026, 11, 28)), sat)
    assert t1["parts"][1]["pred"]["own_s"] < t0["parts"][1]["pred"]["own_s"]


def test_choosing_heavier_loads_than_prescribed_raises_future_prescriptions(root):
    block = find_day(root, date(2026, 11, 25), "m")["sessions"][2]["blocks"][0]
    planned = evaluate(build(root, until=date(2026, 11, 25)), block)["loads"]["db_push_press"]
    write_log(root, "2026-11-25", {"sessions": [{"id": "cardio", "results": [
        {"block": 1, "my_rounds": 5, "loads": {"db_push_press": planned * 1.4, "db_front_squat": planned * 1.4},
         "load_feel": "good"}]}]})
    after = build(root, until=date(2026, 11, 28))
    pref = lambda m: after.b.predict(loadpref_h(m))[0]
    assert pref("db_push_press") > 0.1                       # 덤벨 성향은 크게 올라가고
    assert 0 < pref("clean_and_jerk") < pref("db_push_press") / 2   # 바벨로는 조금만 옮겨간다
    sat = find_day(root, date(2026, 11, 28), "m")["sessions"][1]["blocks"][0]
    before = build(root, until=date(2026, 11, 28), skip_sessions_on=date(2026, 11, 25))
    assert (evaluate(after, sat)["loads"]["devil_press"] >= evaluate(before, sat)["loads"]["devil_press"])


def test_split_suggestion():
    assert suggest_split(21, 0.3) is None
    parts = suggest_split(21, 1.2)
    assert sum(parts) == 21 and parts == sorted(parts, reverse=True) and len(parts) == 3


# ── 재생 / 스키마 / 출력 ─────────────────────────────────
def test_replay_is_deterministic(root):
    write_log(root, "2026-11-24", {"sessions": [{"id": "weight", "results": [
        {"movement": "back_squat", "load": 80, "reps": 3, "sets": 5, "rpe": 8}]}]})
    a, b = build(root, until=date(2026, 11, 28)), build(root, until=date(2026, 11, 28))
    assert render.status_data(a) == render.status_data(b)


def test_benchmarks_in_profile_seed_the_model(root):
    prof = {"sex": "f", "bodyweight_kg": 58, "level": "novice",
            "benchmarks": [{"movement": "deadlift", "load": 100, "reps": 1, "rpe": 10},
                           {"erg": "row", "distance_m": 2000, "time": "8:20"}]}
    (root / "data" / "athlete" / "profile.yaml").write_text(yaml.safe_dump(prof))
    a = build(root, until=date(2026, 11, 23))
    assert a.one_rm("deadlift", today=False) == pytest.approx(100, rel=0.06)
    assert a.b.value("eng") > 0


@pytest.mark.parametrize("sex", ["m", "f"])
@pytest.mark.parametrize("level", ["beginner", "advanced"])
def test_every_programmed_day_renders(sex, level):
    for d in DAYS:
        a = Athlete({"sex": sex, "bodyweight_kg": 62 if sex == "f" else 82, "level": level})
        a.advance_to(d)
        text = render.plan_day(a, find_day(REPO, d, sex))
        assert "훈련 처방" in text and "nan" not in text and "inf" not in text
    assert "능력치 현황" in render.status_md(a)


def test_sex_specific_targets():
    f = find_day(REPO, date(2026, 11, 28), "f")["sessions"][1]["blocks"][0]["sections"][0]["items"][1]
    m = find_day(REPO, date(2026, 11, 28), "m")["sessions"][1]["blocks"][0]["sections"][0]["items"][1]
    assert (f["cal"], m["cal"]) == (12, 15)


def test_schema_rejects_unknown_movement_and_keys():
    base = {"date": "2026-12-01", "sessions": [{"id": "x", "blocks": [
        {"type": "strength", "items": [{"movement": "no_such_move", "reps": 5}]}]}]}
    with pytest.raises(KeyError):
        normalize_day(base, "m")
    base["sessions"][0]["blocks"][0]["items"][0] = {"movement": "deadlift", "rep": 5}
    with pytest.raises(SchemaError):
        normalize_day(base, "m")


def test_parse_time():
    assert parse_time("12:34") == 754 and parse_time(754) == 754 and parse_time("1:02:03") == 3723
    assert math.isclose(parse_time("0:45.5"), 45.5)
