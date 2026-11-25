"""도구 계층(API/MCP): 스키마가 모호함을 막는지, 쓰기가 멱등·안전한지, MCP 로 같은 결과가 나오는지."""
import asyncio
import json
import shutil
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from coach import api

REPO = Path(__file__).resolve().parent.parent
LB = 0.45359237


@pytest.fixture
def root(tmp_path: Path, monkeypatch) -> Path:
    (tmp_path / "data" / "athlete").mkdir(parents=True)
    (tmp_path / "data" / "logs").mkdir()
    shutil.copytree(REPO / "data" / "workouts", tmp_path / "data" / "workouts")
    (tmp_path / "data" / "athlete" / "profile.yaml").write_text(
        yaml.safe_dump({"sex": "m", "bodyweight_kg": 70, "level": "intermediate"}))
    monkeypatch.setattr(api, "ROOT", tmp_path)
    monkeypatch.delenv("COACH_GIT_SYNC", raising=False)
    return tmp_path


def bench(load, reps=5, count=1, **kw):
    return {"date": "2026-11-26", "session_id": "weight", "movement": "bench_press",
            "sets": [{"reps": reps, "count": count, "load": load, **kw}]}


# ── 입력이 자명한가 ──────────────────────────────────────
def test_weights_need_an_explicit_unit(root):
    with pytest.raises(ValidationError):
        api.call("log_strength_result", bench(60))                      # 단위 없는 숫자는 거부
    with pytest.raises(ValidationError):
        api.call("log_strength_result", bench({"value": 60, "unit": "pound"}))
    api.call("log_strength_result", bench({"value": 155, "unit": "lb"}, reps=3, rpe=10))
    stored = api.call("get_log", {"date": "2026-11-26"})["log"]["sessions"][0]["results"][0]
    assert stored["sets"][0]["load"] == pytest.approx(155 * LB, abs=0.01)   # 변환은 서버가 한다


def test_unknown_fields_ids_and_sessions_are_rejected_with_guidance(root):
    with pytest.raises(ValidationError):
        api.call("log_strength_result", bench({"value": 60, "unit": "kg"}, weight=60))
    with pytest.raises(ValidationError):
        api.call("predict_max_reps", {"movement": "bench"})
    with pytest.raises(api.ApiError, match="Sessions: .*weight"):
        api.call("log_strength_result", dict(bench({"value": 60, "unit": "kg"}), session_id="wieght"))
    with pytest.raises(api.ApiError, match="needs `load`"):
        api.call("log_strength_result", dict(bench(None), sets=[{"reps": 5}]))
    with pytest.raises(ValidationError):                                 # rpe 와 feel 을 동시에
        api.call("log_strength_result", bench({"value": 60, "unit": "kg"}, rpe=8, feel="good"))
    assert not (root / "data" / "logs" / "2026-11-26.yaml").exists()     # 거절된 호출은 아무것도 남기지 않는다


def test_find_movement_is_deterministic_and_never_invents():
    assert api.call("find_movement", {"query": "BB row"})["matches"][0]["id"] == "barbell_row"
    assert api.call("find_movement", {"query": "씰로우"})["matches"][0]["id"] == "seal_row"
    assert api.call("find_movement", {"query": "HS push up"})["matches"][0]["id"] == "hspu"
    assert api.call("find_movement", {"query": "burpee over the bar"})["matches"][0]["id"] == "burpee_over_bar"
    assert api.call("find_movement", {"query": "underwater basket weaving"})["matches"] == []


def test_metcon_score_type_must_match_format(root):
    base = {"date": "2026-11-26", "session_id": "cardio"}
    with pytest.raises(api.ApiError, match="does not fit a for_time"):
        api.call("log_metcon_result", {**base, "result": {"type": "rounds", "rounds": 5}})
    with pytest.raises(api.ApiError, match="not in this block"):
        api.call("log_metcon_result", {**base, "result": {"type": "time", "time": "11:00"},
                                       "loads": [{"movement": "deadlift", "load": {"value": 60, "unit": "kg"}}]})
    with pytest.raises(ValidationError):
        api.call("log_metcon_result", {**base, "result": {"type": "time", "time": "11분"}})
    with pytest.raises(api.ApiError, match="2 sections"):
        api.call("log_metcon_result", {"date": "2026-11-28", "session_id": "cardio",
                                       "result": {"type": "time", "time": "11:00"}})


# ── 쓰기: 멱등 · 갱신 ────────────────────────────────────
def test_relogging_replaces_instead_of_double_counting(root):
    first = api.call("log_strength_result", bench({"value": 60, "unit": "kg"}, reps=5, count=5, rpe=9))
    again = api.call("log_strength_result", bench({"value": 60, "unit": "kg"}, reps=5, count=5, rpe=9))
    assert not first["replaced_existing"] and again["replaced_existing"]
    assert first["model_update"] == again["model_update"]
    results = api.call("get_log", {"date": "2026-11-26"})["log"]["sessions"][0]["results"]
    assert len(results) == 1 and len(results[0]["sets"]) == 5
    fixed = api.call("log_strength_result", bench({"value": 70, "unit": "kg"}, reps=5, count=5, rpe=9))
    assert fixed["model_update"]["estimated_1rm_kg"]["after"] > first["model_update"]["estimated_1rm_kg"]["after"]


def test_log_metcon_then_plan_changes_and_review_reports(root):
    plan = api.call("get_plan", {"date": "2026-11-26", "session_ids": ["cardio"]})
    block = plan["sessions"][0]["blocks"][0]
    assert block["format"] == "for_time" and block["items"][0]["load"]["convention"] == "barbell_total_including_bar"
    target = block["target"]["time"]["seconds"]
    fast = f"{int(target * 0.75) // 60}:{int(target * 0.75) % 60:02d}"
    out = api.call("log_metcon_result", {"date": "2026-11-26", "session_id": "cardio",
                                         "result": {"type": "time", "time": fast}, "rpe": 9.5, "load_feel": "light"})
    assert out["model_updates"][0]["performance_vs_expected_pct"] > 15
    api.call("log_session_summary", {"date": "2026-11-26", "session_id": "cardio", "feel": "hard", "duration_min": 25})
    review = api.call("get_review", {"date": "2026-11-26"})
    assert review["updates"][0]["capacity_pct"]["after"] > review["updates"][0]["capacity_pct"]["before"]
    sat = api.call("get_plan", {"date": "2026-11-28", "session_ids": ["cardio"]})
    api.call("clear_session_log", {"date": "2026-11-26", "session_id": "cardio"})
    sat0 = api.call("get_plan", {"date": "2026-11-28", "session_ids": ["cardio"]})
    t = lambda p: p["sessions"][0]["blocks"][0]["sections"][1]["target"]["time"]["seconds"]
    assert t(sat) < t(sat0)                                                # 빨랐던 기록 → 더 빠른 목표


def test_sectioned_block_logs_each_section(root):
    base = {"date": "2026-11-28", "session_id": "cardio"}
    api.call("log_metcon_result", {**base, "section": 2, "result": {"type": "time", "time": "12:00"}})
    api.call("log_metcon_result", {**base, "section": 1, "result": {"type": "capped", "rounds_done": 4.5},
                                   "loads": [{"movement": "clean_and_jerk", "load": {"value": 95, "unit": "lb"}}]})
    entry = api.call("get_log", {"date": "2026-11-28"})["log"]["sessions"][0]["results"]
    assert len(entry) == 1 and entry[0]["sections"][0]["rounds_done"] == 4.5 and entry[0]["sections"][1]["time"] == 720


def test_checkin_and_benchmark_and_profile(root):
    before = api.call("predict_strength_load", {"movement": "deadlift", "reps": 5, "date": "2026-11-26"})
    api.call("log_checkin", {"date": "2026-11-26", "readiness": 1, "sleep_h": 4,
                             "soreness": [{"region": "legs", "level": 3}]})
    tired = api.call("predict_strength_load", {"movement": "deadlift", "reps": 5, "date": "2026-11-26"})
    assert tired["load"]["kg"] < before["load"]["kg"]
    api.call("add_strength_benchmark", {"movement": "deadlift", "load": {"value": 405, "unit": "lb"}, "reps": 1})
    assert api.call("predict_max_reps", {"movement": "deadlift", "load": {"value": 100, "unit": "kg"},
                                         "date": "2026-11-20"})["estimated_1rm_kg"] > 170
    dup = api.call("add_strength_benchmark", {"movement": "deadlift", "load": {"value": 405, "unit": "lb"}, "reps": 1})
    assert dup["added"] is False
    out = api.call("update_profile", {"barbell_unit": "lb", "bodyweight_kg": 71})
    assert out["profile"]["barbell_unit"] == "lb" and out["profile"]["bodyweight_kg"] == 71
    assert "lb" in api.call("predict_strength_load", {"movement": "bench_press", "reps": 5})["load"]["display"]


def test_save_program_day_roundtrip_and_validation(root):
    day = {"date": "2026-12-01", "gym": "PERE", "sessions": [
        {"id": "weight", "title": "WEIGHT", "focus": "상체 스트렝스", "blocks": [
            {"type": "strength", "intent": "strength", "items": [
                {"movement": "bench_press", "reps": 3, "reps_to": 5, "main": True},
                {"superset": [{"movement": "pull_up", "reps": 8}, {"movement": "push_up", "reps": 12}]}]}]},
        {"id": "cardio", "title": "CARDIO", "focus": "sweat", "assumptions": ["team total 6 rounds"], "blocks": [
            {"type": "metcon", "format": "for_time", "rounds": 6, "time_cap_min": 15,
             "team": {"size": 2, "style": "igyg", "rounds_scope": "team"},
             "items": [{"movement": "machine", "cal": {"f": 12, "m": 15}},
                       {"choice": [{"movement": "deadlift", "reps": 8}, {"movement": "goblet_squat", "reps": 12}]},
                       {"movement": "db_thruster", "reps": 10, "load": {"value": {"f": 10, "m": 15}, "unit": "lb"}}]}]}]}
    out = api.call("save_program_day", day)
    assert out["replaced_existing"] is False
    plan = api.call("get_plan", {"date": "2026-12-01"})
    strength = plan["sessions"][0]["blocks"][0]["items"]
    assert strength[0]["reps"] == {"min": 3, "max": 5} and strength[1]["superset_group"] == strength[2]["superset_group"]
    metcon = plan["sessions"][1]["blocks"][0]
    assert metcon["items"][0]["prescribed"] == {"cal": 15}                 # 남성 기준이 적용됨
    assert metcon["items"][2]["load"]["kg"] == pytest.approx(15 * LB, abs=0.1) and metcon["items"][2]["load_is_prescribed_by_gym"]
    assert metcon["items"][1]["alternatives"] == ["goblet_squat"]
    assert "team total 6 rounds" in plan["markdown"]
    assert api.call("save_program_day", day)["replaced_existing"] is True

    bad = json.loads(json.dumps(day))
    bad["sessions"][1]["blocks"][0]["items"][0] = {"movement": "machine", "cal": 15, "reps": 10}
    with pytest.raises(ValidationError):
        api.call("save_program_day", bad)
    bad["sessions"][1]["blocks"][0] = {"type": "metcon", "format": "amrap", "items": [{"movement": "burpee", "reps": 10}]}
    with pytest.raises(api.ApiError, match="duration_min"):
        api.call("save_program_day", bad)


def test_predictions_are_pure_and_repeatable(root):
    args = {"block": {"type": "metcon", "format": "amrap", "duration_min": 12, "items": [
        {"movement": "thruster", "reps": 9}, {"movement": "pull_up", "reps": 9}, {"movement": "row", "cal": 12}]}}
    a, b = api.call("predict_metcon", args), api.call("predict_metcon", args)
    assert a == b and a["target"]["range"]["low"] < a["target"]["rounds"] < a["target"]["range"]["high"]
    heavy = api.call("predict_metcon", {**args, "loads": [
        {"movement": "thruster", "load": {"value": a["loads"][0]["kg"] + 20, "unit": "kg"}}]})
    assert heavy["target"]["rounds"] < a["target"]["rounds"]
    erg = api.call("predict_erg", {"machine": "row", "cal": 30})
    back = api.call("predict_erg", {"machine": "row", "time": erg["time"]["text"]})
    assert back["cal"] == pytest.approx(30, abs=0.6)
    assert not list((root / "data" / "logs").iterdir())


def test_effort_is_reported_as_concrete_facts_not_a_score(root):
    by_left = api.call("log_strength_result", bench({"value": 60, "unit": "kg"}, reps=5, count=5, reps_left=1))
    by_rpe = api.call("log_strength_result", bench({"value": 60, "unit": "kg"}, reps=5, count=5, rpe=9))
    assert by_left["model_update"] == by_rpe["model_update"]              # 1개 남음 == RPE 9
    assert by_left["model_update"]["best_set"]["reps_left"] == 1
    with pytest.raises(ValidationError):
        api.call("log_strength_result", bench({"value": 60, "unit": "kg"}, reps_left=1, rpe=9))
    base = {"date": "2026-11-26", "session_id": "cardio", "result": {"type": "time", "time": "12:00"}}
    with pytest.raises(api.ApiError, match="effort or rpe"):
        api.call("log_metcon_result", {**base, "effort": "hard", "rpe": 9})
    with pytest.raises(ValidationError):
        api.call("log_metcon_result", {**base, "effort": "kind of hard"})
    hard = api.call("log_metcon_result", {**base, "effort": "all_out"})["model_updates"][0]
    eased = api.call("log_metcon_result", {**base, "effort": "controlled"})["model_updates"][0]
    assert eased["performance_vs_expected_pct"] > hard["performance_vs_expected_pct"]   # 여유 있게 낸 같은 기록이 더 좋은 신호
    with pytest.raises(api.ApiError, match="exactly one"):
        api.call("log_session_summary", {"date": "2026-11-26", "session_id": "cardio"})
    plan = api.call("get_plan", {"date": "2026-11-26", "session_ids": ["weight"]})
    assert plan["sessions"][0]["blocks"][0]["items"][1]["target_reps_left_last_set"] == 1.5
    assert "1~2개 더 할 수 있는 정도" in plan["markdown"] and "RPE" not in plan["markdown"]
    assert api.call("predict_strength_load", {"movement": "bench_press", "reps": 5, "target_reps_left": 0})["load"]["kg"] > \
        api.call("predict_strength_load", {"movement": "bench_press", "reps": 5, "target_reps_left": 4})["load"]["kg"]


# ── MCP 로 노출된 모습 ───────────────────────────────────
def test_mcp_server_exposes_the_same_tools(root):
    from coach.mcp_server import create_server
    server = create_server()

    async def go():
        tools = {t.name: t for t in await server.list_tools()}
        assert set(tools) == set(api.TOOLS)
        schema = getattr(tools["log_strength_result"], "input_schema", None) or tools["log_strength_result"].inputSchema
        assert set(schema["required"]) == {"date", "session_id", "movement", "sets"}
        assert "back_squat" in schema["properties"]["movement"]["enum"]
        assert schema["$defs"]["Load"]["required"] == ["value", "unit"] and schema["$defs"]["Load"]["additionalProperties"] is False
        for t in tools.values():                       # 모든 도구·매개변수에 설명이 있다
            assert t.description
        ok = await server.call_tool("predict_erg", {"machine": "ski", "cal": 30})
        text = ok.content[0].text if hasattr(ok, "content") else ok[0][0].text
        assert json.loads(text) == api.call("predict_erg", {"machine": "ski", "cal": 30})
        try:
            bad = await server.call_tool("log_strength_result", dict(bench({"value": 60, "unit": "kg"}), session_id="nope"))
            message = bad.content[0].text
        except Exception as e:                          # SDK 버전에 따라 예외 또는 isError 결과
            message = str(e)
        assert "Sessions:" in message and "weight" in message
    asyncio.run(go())
