# Athletic Performance — 개인 맞춤 훈련 코치

체육관(PERE)의 주간 프로그램에 대해 **세션별 목표(무게·횟수·기록)를 처방**하고, 운동 후 피드백으로
**능력치 모델과 컨디션을 갱신**하는 시스템. 사용자는 한국어로 대화하며 주로 휴대폰으로 본다. 항상 한국어로 답한다.

- 엔진: `coach/` (순수 Python + numpy/pyyaml). 모델 설명은 `README.md`, 데이터 형식은 `docs/schema.md`.
- 데이터: `data/athlete/profile.yaml`, `data/workouts/DATE.yaml`, `data/logs/DATE.yaml`, `data/plans/DATE.md`.
- 상태는 저장하지 않는다. 매번 프로필 + 기록 전체를 재생(`coach/replay.py`)해서 만든다.
  → 지난 기록을 고쳐도 되고, `data/athlete/state.json`·`status.md` 는 보기용 산출물일 뿐 직접 고치지 않는다.
- 컨테이너는 휘발성이다. **data/ 를 바꿨으면 그 턴 안에 커밋·푸시**해야 다음 세션에 남는다.

## 사용자 메모
- 주로 **CARDIO / HYROX** 수업을 듣는다 → 처방을 전달할 때 그 세션을 먼저, 자세히. 나머지는 요청하면.
- PB 를 파운드로 말한다 → `equipment.barbell_unit: lb` (바벨은 lb, 덤벨·케틀벨은 kg). 사용자가 kg 이 편하다고 하면 바꾼다.
- `preferred_machine` 은 아직 미확인(기본 row). 머신 종목 피드백을 받을 때 어떤 머신이었는지 확인한다.

## 명령

```bash
python -m coach check              # 프로그램·기록 YAML 검증
python -m coach plan 2026-11-25    # 처방서 → data/plans/ 저장 (+ -s weight cardio 로 세션만 출력)
python -m coach review 2026-11-25  # 그날 기록: 예측 vs 실제, 무엇이 갱신됐는지
python -m coach status             # 능력치 현황 → data/athlete/status.md, state.json
python -m pytest -q                # 엔진을 고쳤으면 반드시
```

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
   RPE 를 안 줬으면 "쉬웠다/적당/힘들었다/실패" → `feel: easy|good|hard|fail`. 그것도 없으면 한 번만 물어본다.
   메트콘은 기록·실제 사용 무게·RPE·`load_feel` 을 챙긴다. 처방과 다른 무게를 썼으면 반드시 `loads` 에 적는다.
2. `python -m coach review DATE` 로 예측과 실제의 차이를 확인하고, `python -m coach status` 로 산출물 갱신.
3. 무엇이 예상과 달랐고 그래서 추정이 어떻게 바뀌었는지 2~4줄로 알려준다. 남은 주간 처방이 달라졌으면 다시 뽑는다.
4. 커밋·푸시.

### C. 운동 전 컨디션(수면·근육통·피곤함)을 말했을 때
그날 로그에 `checkin` 을 쓰고 `plan` 을 다시 돌린다.

### D. 프로필·기록(1RM, 최대 풀업, 로잉 기록 등)을 알려줬을 때
`profile.yaml` 의 값 또는 `benchmarks` 에 날짜와 함께 추가한다. 체중이 바뀌면 `bodyweight_kg` 갱신.

## 엔진을 고칠 때
- 사전값(수준별 기준, 동작 비율, 사이클 시간)은 `coach/catalog.py`, `coach/data/movements.yaml`.
  개인 편차는 사전값을 고치지 말고 기록으로 학습되게 둔다. 사전값은 "누구에게나" 틀렸을 때만 고친다.
- 처방(`strength.py`, `metcon.py`)과 피드백 해석(`feedback.py`)은 같은 식을 써야 한다
  (예: 세트 누적 피로 `set_fatigue`). 한쪽만 바꾸면 폐루프가 편향된다.
- 예측이 체계적으로 빗나가는 패턴(특정 형식·동작에서 계속 느림/빠름)이 보이면 `review` 결과를 근거로 모델 구조를 손본다.
