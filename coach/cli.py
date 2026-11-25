"""명령줄 인터페이스.

    python -m coach check                  운동 프로그램·기록 YAML 검증
    python -m coach plan 2026-11-25        그날 처방서 생성 (data/plans/ 에 저장)
    python -m coach plan 2026-11-25 -s weight cardio
    python -m coach review 2026-11-25      그날 기록의 예측 vs 실제
    python -m coach status                 능력치 현황 (data/athlete/status.md, state.json 갱신)
    python -m coach tools                  도구(API/MCP) 목록
    python -m coach call <도구> '<json>'   MCP 와 같은 도구를 직접 호출 (검증·멱등 동일)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from . import render
from .replay import build, load_logs, load_profile
from .athlete import normalize_profile
from .workout import find_day, load_day

ROOT = Path(__file__).resolve().parent.parent


def cmd_check(root: Path, _args) -> int:
    sex = normalize_profile(load_profile(root))["sex"]
    files = sorted((root / "data" / "workouts").glob("*.yaml"))
    for f in files:
        day = load_day(f, sex)
        print(f"OK  {f.name}: " + ", ".join(s["id"] for s in day["sessions"]))
    build(root)
    print(f"OK  기록 {len(load_logs(root))}일치 재생 완료")
    return 0


def cmd_plan(root: Path, args) -> int:
    d = date.fromisoformat(args.date)
    athlete = build(root, until=d)
    day = find_day(root, d, athlete.sex)
    if not day:
        print(f"data/workouts/{d.isoformat()}.yaml 이 없습니다", file=sys.stderr)
        return 1
    text = render.plan_day(athlete, day, args.session)
    if not args.session:
        out = root / "data" / "plans" / f"{d.isoformat()}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    print(text)
    return 0


def cmd_review(root: Path, args) -> int:
    d = date.fromisoformat(args.date)
    print(render.review_md(build(root, until=d), d))
    return 0


def cmd_status(root: Path, args) -> int:
    until = date.fromisoformat(args.date) if args.date else date.today()
    athlete = build(root, until=until)
    text = render.status_md(athlete)
    adir = root / "data" / "athlete"
    (adir / "status.md").write_text(text, encoding="utf-8")
    (adir / "state.json").write_text(
        json.dumps(render.status_data(athlete), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(text)
    return 0


def cmd_tools(root: Path, _args) -> int:
    from . import api
    for name, spec in api.TOOLS.items():
        doc = " ".join((spec["fn"].__doc__ or "").split())
        print(f"{'W' if spec['writes'] else 'R'}  {name:24s} {doc[:110]}")
    return 0


def cmd_call(root: Path, args) -> int:
    from pydantic import ValidationError

    from . import api
    api.set_root(root)
    try:
        out = api.call(args.tool, json.loads(args.json or "{}"))
    except (api.ApiError, ValidationError) as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False, indent=2))
        return 1
    if args.markdown and "markdown" in out:
        print(out["markdown"])
    else:
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="coach", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=ROOT)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    p = sub.add_parser("plan")
    p.add_argument("date")
    p.add_argument("-s", "--session", nargs="*")
    p.set_defaults(fn=cmd_plan)
    p = sub.add_parser("review")
    p.add_argument("date")
    p.set_defaults(fn=cmd_review)
    p = sub.add_parser("status")
    p.add_argument("date", nargs="?")
    p.set_defaults(fn=cmd_status)
    sub.add_parser("tools").set_defaults(fn=cmd_tools)
    p = sub.add_parser("call")
    p.add_argument("tool")
    p.add_argument("json", nargs="?")
    p.add_argument("-m", "--markdown", action="store_true", help="결과의 markdown 필드만 출력")
    p.set_defaults(fn=cmd_call)
    args = ap.parse_args(argv)
    return args.fn(args.root, args)


if __name__ == "__main__":
    raise SystemExit(main())
