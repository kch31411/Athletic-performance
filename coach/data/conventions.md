# Conventions for agents using the coach tools

The engine is deterministic: no language model runs inside it. Your job is to transcribe what the
gym board or the athlete says into tool inputs exactly; the engine does all arithmetic and prediction.

## General
- Dates: `YYYY-MM-DD`. Resolve "today/yesterday/Thursday" yourself before calling.
- Movements: always a catalog id. Resolve names with `find_movement`. If nothing matches, tell the
  athlete the movement is not in the catalog; do not substitute a different one silently.
- Weights: always `{value, unit}` with the unit the person used ("225 pounds" → `{value: 225, unit: "lb"}`).
  Never convert units yourself. Barbell = total including the bar. Dumbbell/kettlebell = ONE implement
  ("20kg dumbbells x2" → `{value: 20, unit: "kg"}`).
- Times: `"M:SS"` strings ("12분 34초" → `"12:34"`).
- Effort is asked as concrete facts, not a 1-10 score (the athlete finds the number scale vague):
  - Strength: "마지막 세트에서 몇 개 더 할 수 있었어요?" → `reps_left` (0 = none). Vague words only →
    `feel`: easy / good / hard / fail.
  - Conditioning: how it ended → `effort`: all_out (could not go faster) / hard (only a final push left) /
    controlled (could hold the pace a few more minutes) / easy (could talk).
  - Whole class → `feel`: light / moderate / hard / max.
  - Use the numeric `rpe` / `srpe` fields only when the athlete volunteers a number.
  - Never invent any of these. If not given, log without it (the work still counts toward fatigue) and ask once.
- Never estimate or adjust numbers yourself. To answer "what should I lift / how fast", call a
  prediction tool and relay its numbers (the `markdown` field is ready to show).

## Reading a program board (save_program_day)
- One session per class on the board (HY-FIT, WEIGHT, CARDIO, HYROX ...). Session ids: lower-case
  title (`hyfit`, `weight`, `cardio`); if a title repeats, use the sub-heading (`hyrox_strength`, `hyrox_engine`).
- `A & B` on one line → superset (strength) or two consecutive items (metcon).
- `12/15cal`, `3kg/6kg` → women/men: `{f: 12, m: 15}`.
- `8/8` → reps 8, per_side true. `3~5` → reps 3, reps_to 5. `(drop)` → drop_set true.
- `21/15/9` → rep_scheme [21, 15, 9]; items carry no reps of their own.
- `A or B` → choice. `push up 20 / pike push up 15 / HSPU 10` (level options) → scale, easiest first.
- `machine` with no type → movement `machine`. `BB` = barbell, `DB` = dumbbell, `KB` = kettlebell,
  `HLR` = hanging leg raise, `ab` = assault bike, `HS push up` = hspu.
- Strength intent: "스트렝스" → strength (mark the stated main lift `main: true`); "볼륨"/"전신" → volume;
  numbered strength lists inside HY-FIT → hyfit.
- Metcon intensity: `sweat` → moderate; `S.E` → heavy; a coach note about weight overrides
  ("가볍게 하면 쉬워요" / "무겁게 하면 재밌어요" → heavy).
- Formats: "for time" / "N rounds" → for_time; "AMRAP 30'" → amrap; "EMOM", "M1/M2/M3", "e2mom"
  → emom (interval_s 60 or 120; "36' e2mom" with 6 stations → rounds 3); "2:30 on/off" → interval;
  "p1 / p2 / p3" roles that rotate → rotation with stations.
- Teams: "i go you go" → igyg; shared reps → split. If the board does not say whether rounds are per
  person or team total, choose the reading that gives a plausible class length and say so in `assumptions`.
- Anything you had to guess goes into `assumptions` verbatim; do not hide it.

## Typical flows
1. New board → `save_program_day` per date → `get_plan(date)`.
2. Before class ("slept 5h, legs sore") → `log_checkin` → `get_plan(date)` again.
3. After class → one `log_strength_result` per movement, one `log_metcon_result` per workout/section,
   then `log_session_summary` → relay `model_update(s)`; `get_review(date)` for the whole day.
4. Known bests ("bench 155lb x3", "half marathon 1:52") → `add_strength_benchmark` / `add_erg_benchmark`.
5. Questions ("how many pull-ups can I do", "what should I squat for 5") → `predict_max_reps`,
   `predict_strength_load`, `predict_erg`, `predict_metcon`, `get_status`.
6. HYROX: "what would my race time be" → `predict_hyrox`. A timed station (practice or simulation) →
   `log_hyrox_station`; ask whether it was done fresh or already fatigued (`effort`), and give `load` /
   `amount` only when they differ from the race standard. A race or full simulation → `log_hyrox_race`
   (finish time, plus splits from the result sheet if available).

Re-logging the same movement / block replaces the earlier entry, so corrections are safe.
