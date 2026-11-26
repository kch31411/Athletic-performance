# Athletic Performance — 개인 맞춤 훈련 코치

체육관(PERE)의 주간 프로그램에 대해 **세션별 목표(무게·횟수·기록)를 처방**하고, 운동 후 피드백으로
**능력치 모델과 컨디션을 갱신**하는 시스템. 사용자는 한국어로 대화하며 주로 휴대폰으로 본다. 항상 한국어로 답한다.

- 엔진: `coach/` (순수 Python + numpy/pyyaml). 모델 설명은 `README.md`, 데이터 형식은 `docs/schema.md`.
- 데이터: `data/athlete/profile.yaml`, `data/workouts/DATE.yaml`, `data/logs/DATE.yaml`, `data/plans/DATE.md`.
- 상태는 저장하지 않는다. 매번 프로필 + 기록 전체를 재생(`coach/replay.py`)해서 만든다.
  → 지난 기록을 고쳐도 되고, `data/athlete/state.json`·`status.md` 는 보기용 산출물일 뿐 직접 고치지 않는다.
- 컨테이너는 휘발성이다. **data/ 를 바꿨으면 그 턴 안에 커밋·푸시**해야 다음 세션에 남는다.

## 사용자 메모
- **목표: HYROX 오픈 싱글 출전, 2027년 5월 중순** (정확한 날짜 미정 — 정해지면 여기 적는다). 준비 계획과
  기준 측정 체크리스트는 `data/athlete/hyrox_plan.md` — 측정값이 들어오면 체크하고 우선순위를 다시 본다.
  체육관에 슬레드 있음. 월볼은 30개 정도씩 끊어서 함(100개 연속 시도는 아직).
  `python -m coach hyrox` 로 예상 기록·약점 구간을 본다. 스테이션을 시간 재고 했다는 말이 나오면
  `log_hyrox_station`(쌩쌩할 때였는지 `fresh` / 지친 상태였는지 `race` 확인), 시뮬레이션·대회는 `log_hyrox_race`.
- 주로 **CARDIO / HYROX** 수업을 듣는다 → 처방을 전달할 때 그 세션을 먼저, 자세히. 나머지는 요청하면.
- 바벨은 파운드, 덤벨·케틀벨은 kg 을 쓴다 (2026-11-25 본인 확인) → `equipment.barbell_unit: lb`.
  수업에서 쓰는 원판이 25lb 라 85lb 를 못 만들고 95lb 로 했다고 함(2026-11-26). 쓸 수 있는 원판 종류를 확인해
  `update_profile` 의 `barbell_plates` 에 넣는다(넣으면 처방이 만들 수 있는 무게로만 나온다).
- **1~10 강도 숫자는 모호하다고 함 (2026-11-25).** 숫자를 묻거나 처방에 RPE 를 쓰지 않는다.
  근력은 "마지막 세트에서 몇 개 더 할 수 있었어요?"(`reps_left`), 메트콘은 끝났을 때 상태 4지선다(`effort`),
  수업 전체는 `feel: light|moderate|hard|max` 로 받는다. 본인이 숫자를 말하면 그때만 `rpe`.
- `preferred_machine` 은 아직 미확인(기본 row). 머신 종목 피드백을 받을 때 어떤 머신이었는지 확인한다.

## 명령

```bash
python -m coach check              # 프로그램·기록 YAML 검증
python -m coach plan 2026-11-25    # 처방서 → data/plans/ 저장 (+ -s weight cardio 로 세션만 출력)
python -m coach review 2026-11-25  # 그날 기록: 예측 vs 실제, 무엇이 갱신됐는지
python -m coach status             # 능력치 현황 → data/athlete/status.md, state.json
python -m pytest -q                # 엔진을 고쳤으면 반드시
python -m coach hyrox              # HYROX 오픈 싱글 예상 기록(구간별, 약점)
python -m coach tools              # 도구(API/MCP) 목록
python -m coach call <도구> '<json>'   # MCP 와 같은 도구를 직접 호출 (-m: markdown 만 출력)
```

기록·프로필·프로그램을 **쓸 때는 YAML 을 손으로 고치지 말고 `python -m coach call ...` 도구를 쓴다**
(단위 변환·검증·덮어쓰기 규칙이 MCP 로 붙는 다른 에이전트와 똑같아진다). 입력 규칙은
`coach/data/conventions.md`, 스키마는 `coach/models.py`. 카탈로그에 없는 동작 추가 같은 구조 변경만 파일을 직접 고친다.

## 작업 흐름

### A. 프로그램 이미지를 받았을 때
1. 날짜별로 `data/workouts/YYYY-MM-DD.yaml` 작성 (`docs/schema.md`). 연도는 요일로 확인한다.
2. 동작은 `coach/data/movements.yaml` 의 id 로. 없는 동작이면 카탈로그에 추가한다
   (비슷한 동작을 참고해 `ref`/`ratio`/`cycle_s`/`fatigue` 를 정하고, 근력 모델이 어색한 맨몸 컨디셔닝은 `unbroken`).
3. 해석이 애매한 부분(팀 방식, 라운드가 팀 합계인지 각자인지, 기구 등)은 가장 그럴듯하게 정하고
   `assumptions` 에 적는다. 처방서에 그대로 나오므로 사용자가 바로잡을 수 있다. 사용자가 바로잡으면 YAML 을 고친다.
4. 관례: `sweat` → `intensity: moderate`, `S.E` → `heavy`, 코치 노트가 무게를 언급하면 그에 따름.
   WEIGHT 의 "볼륨" → `intent: volume`, "스트렝스" → `strength`(메인 종목에 `main: true`), HY-FIT 근력 → `hyfit`.
   `12/15cal` 같은 표기는 여/남 → `{f: 12, m: 15}`.
5. `python -m coach check` → `python -m coach plan DATE` → 처방을 채팅으로 전달(사용자가 가는 수업 위주로 간결하게).

### B. 운동 후 피드백을 받았을 때
1. 말로 준 내용을 `data/logs/YYYY-MM-DD.yaml` 로 옮긴다(같은 날 파일이 있으면 세션을 추가).
   근력은 남은 개수(`reps_left`), 못 들었으면 "쉬웠다/적당/힘들었다/실패" → `feel`. 그것도 없으면 한 번만 물어본다.
   메트콘은 기록·실제 사용 무게·끝났을 때 상태(`effort`)·`load_feel` 을 챙긴다. 처방과 다른 무게를 썼으면 반드시 `loads` 에 적는다.
2. `python -m coach review DATE` 로 예측과 실제의 차이를 확인하고, `python -m coach status` 로 산출물 갱신.
3. 무엇이 예상과 달랐고 그래서 추정이 어떻게 바뀌었는지 2~4줄로 알려준다. 남은 주간 처방이 달라졌으면 다시 뽑는다.
4. 커밋·푸시.

### C. 운동 전 컨디션(수면·근육통·피곤함)을 말했을 때
그날 로그에 `checkin` 을 쓰고 `plan` 을 다시 돌린다.

### D. 프로필·기록(1RM, 최대 풀업, 로잉 기록 등)을 알려줬을 때
`profile.yaml` 의 값 또는 `benchmarks` 에 날짜와 함께 추가한다. 체중이 바뀌면 `bodyweight_kg` 갱신.

## 엔진을 고칠 때
- 계층: 엔진(계산) ← `api.py`(도구: 검증·저장) ← `mcp_server.py`/`cli.py`(껍데기). 엔진과 도구 계층에 LLM 호출을 넣지 않는다.
  도구를 추가·변경하면 `docs/mcp.md` 표와 `conventions.md`, `tests/test_api.py` 를 같이 고친다.
- 사전값(수준별 기준, 동작 비율, 사이클 시간)은 `coach/catalog.py`, `coach/data/movements.yaml`.
  개인 편차는 사전값을 고치지 말고 기록으로 학습되게 둔다. 사전값은 "누구에게나" 틀렸을 때만 고친다.
- 처방(`strength.py`, `metcon.py`)과 피드백 해석(`feedback.py`)은 같은 식을 써야 한다
  (예: 세트 누적 피로 `set_fatigue`). 한쪽만 바꾸면 폐루프가 편향된다.
- 예측이 체계적으로 빗나가는 패턴(특정 형식·동작에서 계속 느림/빠름)이 보이면 `review` 결과를 근거로 모델 구조를 손본다.
