# 데이터 스키마

모든 데이터는 `data/` 아래 YAML 이다. 동작은 `coach/data/movements.yaml` 의 id 로 적는다.

## 1. 운동 프로그램 — `data/workouts/YYYY-MM-DD.yaml`

```yaml
date: 2026-11-26
gym: PERE
sessions:
  - id: weight            # 하루 안에서 유일. 기록(log)에서 이 id 로 참조
    title: WEIGHT
    focus: 상체 스트렝스
    notes: ["벤치가 메인이에요!"]          # 코치 노트 원문
    assumptions: ["..."]                   # 이미지 해석이 애매했던 부분 (처방서에 그대로 표시)
    blocks: [...]
```

### strength 블록

```yaml
- type: strength
  intent: strength        # strength(스트렝스) | volume(볼륨) | hyfit(HY-FIT 근력)
  items:
    - {movement: bench_press, reps: [3, 5], main: true}   # 범위는 [lo, hi]
    - {movement: split_squat, reps: 6, per_side: true}    # 6/6
    - {movement: bench_press, reps: 12, drop: true}       # 드랍세트
    - superset:                                           # A & B
        - {movement: pull_up, reps: 8}
        - {movement: push_up, reps: 12, rpe: 7}           # rpe: 목표 RPE 직접 지정
```

선택 키: `sets`(세트 수, 기본 메인 5·그 외 4), `rpe`, `main`, `note`. `reps` 생략 시 12회 기준.

### metcon 블록

공통 키: `format`, `rounds`, `rep_scheme`, `time_cap_min`, `duration_min`, `team`, `intensity`, `items`.

| format | 의미 | 필요한 키 |
|---|---|---|
| `for_time` | 정해진 일을 최대한 빨리 | `rounds` 또는 `rep_scheme: [21, 15, 9]` |
| `amrap` | 정해진 시간에 최대 라운드 | `duration_min` |
| `emom` | 매 `interval_s` 초마다 스테이션 1개 | `interval_s`, `rounds`, (선택) `progression: {every_rounds: 2, add: 1}` |
| `interval` | `work_s` 운동 / `rest_s` 휴식. 고정 동작 뒤 `max: true` 동작으로 남은 시간 채움 | `work_s`, `rest_s`, `rounds`, `sections` |
| `rotation` | 팀원이 스테이션을 돌며 맡는 AMRAP. `pacer: true` 스테이션이 끝나면 교대 | `duration_min`, `stations` |

- `team: {size: 2, style: igyg | split | rotation, rounds_scope: each | team}`
  - `igyg`: 한 명씩 번갈아 1라운드. `rounds_scope: each` 면 각자 `rounds` 만큼, `team` 이면 팀 합계.
  - `split`: 라운드마다 횟수를 나눠서 수행(각자 1/size).
- `intensity: light | moderate | heavy` — 메트콘 무게 기조. 관례: sweat → moderate, S.E → heavy,
  코치 노트("가볍게 하면 쉬워요", "무겁게 하면 재밌어요")가 있으면 그에 따른다.
- `sections`: 와드가 여러 개일 때. 각 섹션은 상위 블록의 설정을 물려받는다.

항목(item):

```yaml
- {movement: hang_clean, reps: 8}
- {movement: machine, cal: {f: 12, m: 15}}        # 여/남 기준이 다를 때
- {movement: row, distance_m: 100}
- {movement: bike_erg, seconds: 30}               # 30초 전력
- {movement: ski, max: true}                      # 남은 시간 최대
- {movement: db_thruster, max: true, load_kg: {f: 3, m: 6}}   # 지정 무게
- {movement: front_squat}                         # 횟수 생략 → rep_scheme 을 따름
- choice:                                         # A or B (본인 선택, 기본은 첫 번째)
    - {movement: deadlift, reps: 8}
    - {movement: goblet_squat, reps: 12}
- scale:                                          # 수준별 옵션 (쉬움 → 어려움 순). 엔진이 자동 선택
    - {movement: push_up, reps: 20}
    - {movement: pike_push_up, reps: 15}
    - {movement: hspu, reps: 10}
```

## 2. 운동 기록(피드백) — `data/logs/YYYY-MM-DD.yaml`

```yaml
date: 2026-11-26
checkin:                       # (선택) 운동 전 컨디션
  readiness: 3                 # 1(최악)~5(최상), 3이 평소
  sleep_h: 6.5
  soreness: {하체: 2}          # 0~3. 부위: 하체/상체/가슴/어깨/등/코어 또는 패턴 id
sessions:
  - id: weight                 # 프로그램의 세션 id
    srpe: 8                    # 수업 전체 강도 1~10
    duration_min: 50
    results:
      # 근력: 세트별 또는 요약형. rpe 는 마지막 세트 기준.
      - {movement: bench_press, load: 60, reps: 5, sets: 5, rpe: 8.5}
      - movement: back_squat
        sets:
          - {load: 80, reps: 3, rpe: 7}
          - {load: 90, reps: 3, rpe: 9}
      - {movement: pull_up, reps: 8, rpe: 9}              # 맨몸. load: +10(중량) / -15(밴드 보조)
      - {movement: db_row, load: 22.5, reps: 15, sets: 4, feel: good}   # rpe 대신 easy|good|hard|fail
  - id: cardio
    srpe: 9
    duration_min: 20
    results:
      - block: 1               # 세션 안의 몇 번째 블록인지 (1부터)
        time: "12:34"          # for_time 기록(팀이면 팀 기록). 타임캡에 걸렸으면 rounds_done: 2.5
        loads: {hang_clean_and_jerk: 35, front_squat: 35}   # 실제 사용 무게 (처방과 같으면 생략)
        picks: [goblet_squat]  # choice/scale 에서 고른 동작
        rpe: 9
        load_feel: good        # light | good | heavy → 다음 메트콘 무게 기조에 반영
      # amrap:    rounds: 6, reps: 12
      # rotation: my_rounds: 5 (내가 동작 스테이션을 돈 횟수) 또는 team_rounds: 15
      # 섹션이 여러 개면: sections: [{time: "11:20"}, {time: "12:05"}]
      # 머신 구간 기록:   ergs: [{erg: row, cal: 15, seconds: 50, effort: max}]
      - {erg: row, cal: 18, seconds: 60}                  # 단독 머신 테스트. effort: max(기본) | hard
      - {erg: run, distance_m: 5000, time: "24:30"}       # 러닝·로잉은 거리+시간도 가능
```

저장 파일의 `rpe` 는 내부 표현이다(10 − 남은 개수). 사용자에게는 숫자를 묻지 않고 도구 입력의
`reps_left`(몇 개 더 가능했나) · `effort`(전력/거의 전력/버틸 만함/편함) · `feel` 로 받아 변환한다.

## 3. 프로필 — `data/athlete/profile.yaml`

`sex`, `bodyweight_kg`, `level`(beginner|novice|intermediate|advanced 또는 0~3 숫자),
`preferred_machine`, `equipment`(`barbell_unit: kg|lb`, 덤벨·케틀벨 목록), `benchmarks`(알고 있는 기록 — 위 results 와 같은 형식 + `date`).
