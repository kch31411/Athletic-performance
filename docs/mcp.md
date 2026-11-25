# MCP 연동

비서 에이전트가 이 시스템을 쓰는 통로. 구조는 세 겹이다.

```
에이전트(LLM)  ── 말·사진을 구조화된 입력으로 옮기는 일만 한다
     │  MCP (stdio 또는 HTTP)
도구 계층      coach/api.py · models.py   ── 입력 검증, 저장, 멱등성. 23개 도구 + 규칙 안내 1개
     │
엔진           belief · athlete · strength · metcon · feedback · replay ── 계산과 예측. LLM 없음
```

엔진과 도구 계층에는 언어 모델이 없다. **같은 입력 + 같은 저장 데이터 → 항상 같은 출력**이고,
해석이 필요한 부분(칠판 사진 읽기, "좀 힘들었어" 같은 말)은 전부 에이전트 쪽에 남는다.
그래서 에이전트마다 결과가 갈리지 않도록 입력 스키마를 좁게 잡았다.

| 모호해지기 쉬운 것 | 스키마가 강제하는 것 |
|---|---|
| 동작 이름 | 카탈로그 id 의 enum. 이름 → id 는 `find_movement` 가 결정적으로 찾아준다(없으면 빈 목록) |
| 무게 단위 | `{value, unit: kg\|lb}` 필수. 숫자만 보내면 거부. 변환은 서버가 한다 |
| 덤벨/바벨 기준 | 바벨 = 바 포함 총중량, 덤벨·케틀벨 = 한 개. 출력에도 `convention` 으로 명시 |
| 시간 | `"M:SS"` 문자열(정규식 검증) |
| 강도 | 숫자 척도 대신 사실로 받는다: 근력 `reps_left`(몇 개 더 가능했나), 메트콘 `effort: all_out\|hard\|controlled\|easy`, 수업 전체 `feel: light\|moderate\|hard\|max`. 1~10 숫자(`rpe`)는 본인이 말했을 때만. 둘 이상 주면 거부 |
| 메트콘 결과 | `type` 으로 구분: time / capped / rounds / rotation / none. 형식과 안 맞으면 거부 |
| 여/남 기준 | `{f, m}`. 프로필 성별에 맞는 값은 서버가 고른다 |
| 오타·없는 필드 | 모든 객체가 `additionalProperties: false` |
| 같은 기록 두 번 | 같은 날·세션·동작(또는 블록/섹션)은 덮어쓴다 → 재시도해도 이중 계산 없음 |
| 잘못된 기록 | 저장 후 전체 기록을 재생해 검증하고, 실패하면 되돌린다 |

거절할 때는 무엇이 틀렸고 무엇이 가능한지를 메시지에 담는다
(예: `session 'wieght' is not in the 2026-11-26 program. Sessions: ['hyfit', 'weight', 'cardio']`).

## 도구

읽기(R)는 상태를 바꾸지 않는 순수 예측·조회, 쓰기(W)는 `data/` 를 바꾼다.

| | 도구 | 하는 일 |
|---|---|---|
| R | `get_conventions` | 칠판·운동 보고를 입력으로 옮기는 규칙. 쓰기 전에 한 번 읽는다 |
| R | `list_movements` / `find_movement` | 동작 카탈로그, 이름 → id |
| R | `get_profile` | 프로필 |
| W | `update_profile` | 체중·수준·주 머신·바벨 단위 |
| W | `add_strength_benchmark` / `add_erg_benchmark` | 알고 있는 기록(PB, 하프마라톤 등) |
| W | `save_program_day` | 하루치 프로그램 저장(그 날짜를 통째로 교체) |
| R | `get_program_day` / `list_program_days` | 저장된 프로그램, 기록 여부 |
| R | `get_plan` | **그날 처방**: 무게·세트 끝에 남길 개수·세트 분할·목표 기록·머신 페이스 (+ 한국어 `markdown`) |
| R | `predict_strength_load` | 세트×횟수×남길 개수 → 권장 무게 |
| R | `predict_max_reps` | 무게 → 연속 최대 횟수, 추정 1RM |
| R | `predict_erg` | 칼로리/거리/시간 중 하나 → 나머지 |
| R | `predict_metcon` | 저장하지 않은 와드의 권장 무게·예상 기록 (what-if) |
| R | `get_status` | 능력치 추정과 불확실성, 피로 |
| W | `log_checkin` | 운동 전 컨디션(수면·근육통) |
| W | `log_strength_result` | 한 동작의 세트 기록 → 갱신된 1RM 추정 반환 |
| W | `log_metcon_result` | 와드(또는 섹션) 하나의 결과 → 예측 대비 차이 반환 |
| W | `log_erg_result` | 단독 머신·러닝 기록 |
| W | `log_session_summary` | 수업 전체 강도 × 시간 |
| R | `get_log` / `get_review` | 저장된 기록, 그날의 예측 vs 실제 |
| W | `clear_session_log` | 한 세션 기록 삭제(되돌리기) |

정확한 입력 형식은 각 도구의 JSON 스키마가 기준이다(`coach/models.py`).

## 설치와 실행

```bash
git clone https://github.com/kch31411/Athletic-performance && cd Athletic-performance
pip install numpy pyyaml pydantic mcp        # mcp 1.9+ 와 2.x 모두 동작
python -m coach.mcp_server                   # stdio
python -m coach.mcp_server --http --port 8765   # streamable HTTP → http://127.0.0.1:8765/mcp
```

MCP 클라이언트 설정 예 (Claude Desktop / Claude Code `.mcp.json` 등 stdio 를 쓰는 에이전트 공통):

```json
{
  "mcpServers": {
    "coach": {
      "command": "python",
      "args": ["-m", "coach.mcp_server"],
      "cwd": "/path/to/Athletic-performance",
      "env": {"COACH_GIT_SYNC": "push"}
    }
  }
}
```

- `COACH_ROOT`(또는 `--root`): `data/` 가 있는 폴더. 기본은 저장소 자체.
- `COACH_GIT_SYNC`: `off`(기본) / `commit` / `push`. 쓰기 도구가 끝날 때마다 `data/` 를 커밋(·푸시)한다.
  여러 곳(비서 에이전트, Claude Code 세션)에서 같은 기록을 보려면 `push` 로 두고 한 브랜치를 쓴다.
- HTTP 모드에는 인증이 없다. 기본은 `127.0.0.1` 에만 열린다. 외부에서 붙이려면 인증이 있는
  프록시/터널 뒤에 둬야 한다.

MCP 없이 같은 도구를 쓰려면:

```bash
python -m coach tools
python -m coach call get_plan '{"date": "2026-11-26", "session_ids": ["cardio"]}' -m
python -m coach call log_metcon_result '{"date": "2026-11-26", "session_id": "cardio",
  "result": {"type": "time", "time": "12:40"}, "effort": "hard", "load_feel": "good",
  "loads": [{"movement": "hang_clean_and_jerk", "load": {"value": 85, "unit": "lb"}},
            {"movement": "front_squat", "load": {"value": 85, "unit": "lb"}}]}'
```

## 호출 예

"목요일 웨이트 벤치 155파운드 5개 5세트 했고 마지막 세트는 한 개 더 할 수 있었어"

```json
{"tool": "log_strength_result", "arguments": {
  "date": "2026-11-26", "session_id": "weight", "movement": "bench_press",
  "sets": [{"reps": 5, "count": 5, "load": {"value": 155, "unit": "lb"}, "reps_left": 1}]}}
```

응답(요지): `{"replaced_existing": false, "model_update": {"performance_vs_expected_pct": 4.1,
"estimated_1rm_kg": {"before": 75.7, "after": 78.2}}}` — 에이전트는 이 숫자를 그대로 전하면 된다.
