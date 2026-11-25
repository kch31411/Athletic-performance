"""MCP 서버: coach.api 의 도구를 그대로 노출하는 얇은 껍데기.

    python -m coach.mcp_server                      # stdio (로컬 에이전트)
    python -m coach.mcp_server --http --port 8765   # streamable HTTP

환경변수
    COACH_ROOT       데이터가 있는 저장소 경로 (기본: 이 패키지의 상위 폴더)
    COACH_GIT_SYNC   off(기본) | commit | push — 쓰기 도구가 끝날 때마다 data/ 를 git 에 반영
"""
import argparse
from pathlib import Path

from . import api

import functools

from pydantic import ValidationError

try:                                    # mcp 2.x
    from mcp.server.mcpserver import MCPServer as _Server
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:                     # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server
    from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

INSTRUCTIONS = """Deterministic training-prescription engine for one athlete (no LLM inside).
You transcribe; the engine computes. Call get_conventions once before writing anything.
Flow: save_program_day → get_plan → (after training) log_strength_result / log_metcon_result /
log_session_summary → get_review. Weights are {value, unit}; movements are catalog ids (find_movement);
times are "M:SS"; never convert units or invent RPE values. Relay numbers from tool output unchanged."""


def _annotations(writes: bool, name: str) -> ToolAnnotations:
    hints = {"readOnlyHint": not writes, "destructiveHint": name == "clear_session_log",
             "idempotentHint": True, "openWorldHint": False}
    try:
        return ToolAnnotations(**hints)
    except Exception:
        snake = {"readOnlyHint": "read_only_hint", "destructiveHint": "destructive_hint",
                 "idempotentHint": "idempotent_hint", "openWorldHint": "open_world_hint"}
        return ToolAnnotations(**{snake[k]: v for k, v in hints.items()})


def _reporting(fn):
    """거절 사유(무엇이 틀렸고 어떻게 고치는지)를 에이전트가 그대로 받도록 ToolError 로 바꾼다."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (api.ApiError, ValidationError) as e:
            raise ToolError(str(e)) from e
    return wrapper


def create_server(**kwargs) -> "_Server":
    server = _Server("athletic-performance-coach", instructions=INSTRUCTIONS, **kwargs)
    for name, spec in api.TOOLS.items():
        fn = _reporting(spec["fn"])
        server.add_tool(fn, name=name, description=" ".join((fn.__doc__ or "").split()),
                        annotations=_annotations(spec["writes"], name))
    return server


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="coach.mcp_server", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, help="데이터 저장소 경로 (COACH_ROOT 보다 우선)")
    ap.add_argument("--http", action="store_true", help="stdio 대신 streamable HTTP 로 띄운다")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args(argv)
    if args.root:
        api.set_root(args.root)
    if not args.http:
        create_server().run("stdio")
        return
    try:                                 # mcp 2.x: 실행 시점에 host/port
        create_server().run("streamable-http", host=args.host, port=args.port)
    except TypeError:                    # mcp 1.x: 생성 시점에 host/port
        create_server(host=args.host, port=args.port).run("streamable-http")


if __name__ == "__main__":
    main()
