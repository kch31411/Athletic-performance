"""도구(API/MCP) 입력 타입. 여기 정의가 곧 에이전트가 보는 JSON 스키마다.

원칙: 해석의 여지를 스키마에서 없앤다.
  - 동작은 카탈로그 id 의 enum, 무게는 반드시 단위와 함께, 시간은 "M:SS" 문자열,
    결과는 type 으로 구분되는 합집합, 알 수 없는 키는 거부(extra=forbid).
"""
from datetime import date
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import catalog as C
from .athlete import LB

MovementId = Annotated[Literal[tuple(C.movements())], Field(description=(  # type: ignore[valid-type]
    "Movement id from the catalog. Resolve names with find_movement / list_movements; never invent ids."))]

Machine = Literal["row", "ski", "bike_erg", "assault_bike", "run"]
Unit = Literal["kg", "lb"]
Day = Annotated[date, Field(description="Calendar date, YYYY-MM-DD.")]
TimeStr = Annotated[str, Field(pattern=r"^(\d{1,2}:)?\d{1,3}:[0-5]\d$",
                               description='Elapsed time as "M:SS" or "H:MM:SS", e.g. "12:34".')]
SessionId = Annotated[str, Field(pattern=r"^[a-z0-9_]+$", max_length=40,
                                 description="Session id within the day, e.g. hyfit, weight, cardio, "
                                             "hyrox_engine. Use 'extra' for work outside the program.")]
Rpe = Annotated[float, Field(ge=1, le=10, description=(
    "Rate of perceived exertion: 10 = no more reps possible, 9 = 1 rep left, 8 = 2 left, 7 = 3 left. "
    "Only use when the athlete states a number themselves; otherwise use the concrete alternative."))]
RepsLeft = Annotated[int, Field(ge=0, le=9, description=(
    "How many MORE reps could have been done at the end of the set with good form. 0 = none (true max "
    "or failed the last rep). Ask exactly this: \"마지막 세트에서 몇 개 더 할 수 있었어요?\""))]
Effort = Annotated[Literal["all_out", "hard", "controlled", "easy"], Field(description=(
    "State at the end of a conditioning workout. all_out: could not have gone any faster, needed to stop "
    "and recover. hard: close to the limit, only a short final push was left. controlled: tough but steady, "
    "could have kept the same pace a few more minutes or gone clearly faster. easy: could talk in sentences."))]
EFFORT_RPE = {"all_out": 10.0, "hard": 9.0, "controlled": 8.0, "easy": 6.5}
SessionFeel = Annotated[Literal["light", "moderate", "hard", "max"], Field(description=(
    "How the whole class left the athlete. light: could do it again right away. moderate: worked, plenty "
    "left. hard: tired, fine after a normal night. max: drained for the rest of the day."))]
SESSION_SRPE = {"light": 4.0, "moderate": 6.0, "hard": 8.0, "max": 9.5}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Load(Strict):
    """A weight with its unit. Barbell: total including the bar. Dumbbell/kettlebell: ONE implement
    (for two-dumbbell movements give the weight of each). Never convert units yourself."""
    value: Annotated[float, Field(gt=0)]
    unit: Unit

    def kg(self) -> float:
        return self.value * (LB if self.unit == "lb" else 1.0)


class SexSplit(Strict):
    """A target that differs by sex, written like "12/15cal" (women/men) on the board."""
    f: Annotated[float, Field(gt=0)]
    m: Annotated[float, Field(gt=0)]


Amount = Union[Annotated[float, Field(gt=0)], SexSplit]


class ProgramLoad(Strict):
    """A weight prescribed by the gym (e.g. "3kg/6kg" → value {f: 3, m: 6}, unit kg)."""
    value: Amount
    unit: Unit


# ── 프로그램: 근력 ───────────────────────────────────────
class StrengthItem(Strict):
    movement: MovementId
    reps: Annotated[Optional[int], Field(ge=1, description="Reps per set. For a range like 3~5 give 3 here and 5 in reps_to. Omit if not written.")] = None
    reps_to: Annotated[Optional[int], Field(ge=1, description="Upper end of a rep range (3~5 → 5).")] = None
    per_side: Annotated[bool, Field(description='True for "8/8" style (reps per side).')] = False
    drop_set: Annotated[bool, Field(description='True when marked "(drop)".')] = False
    sets: Annotated[Optional[int], Field(ge=1, le=12, description="Only if the board states a set count.")] = None
    target_rpe: Optional[Rpe] = None
    main: Annotated[bool, Field(description="True if the coach marks this as the main lift of the session.")] = False
    note: Optional[str] = None


class Superset(Strict):
    """Movements joined by "&" and done back to back (e.g. "pull up 8 & push up 12")."""
    superset: Annotated[list[StrengthItem], Field(min_length=2)]


class StrengthBlock(Strict):
    type: Literal["strength"]
    intent: Annotated[Literal["strength", "volume", "hyfit"], Field(description=(
        'strength: heavy low-rep focus ("스트렝스"). volume: moderate load, more reps ("볼륨", 전신). '
        "hyfit: strength circuit inside a HY-FIT class."))]
    items: Annotated[list[Union[StrengthItem, Superset]], Field(min_length=1)]


# ── 프로그램: 메트콘 ─────────────────────────────────────
class MetconItem(Strict):
    """One line of a conditioning workout. Give exactly one of reps / cal / distance_m / seconds,
    or max_effort, or nothing when the block's rep_scheme (e.g. 21-15-9) applies."""
    movement: MovementId
    reps: Optional[Amount] = None
    cal: Optional[Amount] = None
    distance_m: Optional[Amount] = None
    seconds: Annotated[Optional[float], Field(gt=0, description="Fixed-duration effort, e.g. a 30-second sprint.")] = None
    max_effort: Annotated[bool, Field(description='True for "max" (as many as possible in the remaining time).')] = False
    load: Annotated[Optional[ProgramLoad], Field(description="Only when the board prescribes a weight.")] = None
    note: Optional[str] = None

    @model_validator(mode="after")
    def _one_amount(self):
        given = [k for k in ("reps", "cal", "distance_m", "seconds") if getattr(self, k) is not None]
        if len(given) > 1 or (given and self.max_effort):
            raise ValueError(f"give only one of reps/cal/distance_m/seconds/max_effort (got {given})")
        return self


class Choice(Strict):
    """"A or B": the athlete picks one. List in the order written."""
    choice: Annotated[list[MetconItem], Field(min_length=2)]


class Scale(Strict):
    """Level options for the same slot (e.g. "push up 20 / pike push up 15 / HSPU 10"),
    ordered EASIEST first. The engine picks the hardest feasible one."""
    scale: Annotated[list[MetconItem], Field(min_length=2)]


MetconEntry = Union[MetconItem, Choice, Scale]


class Team(Strict):
    size: Annotated[int, Field(ge=2, le=6)]
    style: Annotated[Literal["igyg", "split", "rotation"], Field(description=(
        "igyg: partners alternate whole rounds (I go, you go). "
        "split: partners share the reps of every round. "
        "rotation: members rotate through stations (use format=rotation)."))]
    rounds_scope: Annotated[Literal["each", "team"], Field(description=(
        "For igyg only: 'each' = every member does `rounds` rounds; 'team' = `rounds` is the team total."))] = "each"


class Progression(Strict):
    """EMOM where the amount grows, e.g. "+1 every 2 rounds"."""
    every_rounds: Annotated[int, Field(ge=1)]
    add: Annotated[int, Field(ge=1)] = 1


class Station(Strict):
    role: Literal["machine", "work", "rest"]
    pacer: Annotated[bool, Field(description="True for the station whose completion triggers the rotation.")] = False
    items: list[MetconEntry] = []


Intensity = Annotated[Literal["light", "moderate", "heavy"], Field(description=(
    "Loading intent. Convention: 'sweat' → moderate, 'S.E' → heavy; a coach note about going "
    "heavy/light overrides (e.g. \"가볍게 하면 쉬워요\" → heavy)."))]


class Section(Strict):
    """One of several workouts inside a block ("section 1", "section 2"). Unset fields inherit from the block."""
    name: Optional[str] = None
    items: Annotated[list[MetconEntry], Field(min_length=1)]
    rounds: Annotated[Optional[int], Field(ge=1)] = None
    rep_scheme: Optional[list[int]] = None
    time_cap_min: Annotated[Optional[float], Field(gt=0)] = None
    intensity: Optional[Intensity] = None


class MetconBlock(Strict):
    type: Literal["metcon"]
    format: Annotated[Literal["for_time", "amrap", "emom", "interval", "rotation"], Field(description=(
        "for_time: finish fixed work fast (needs rounds or rep_scheme). "
        "amrap: max rounds in duration_min. "
        "emom: one item every interval_s seconds, cycling through items for `rounds` cycles (E2MOM → interval_s 120). "
        "interval: work_s on / rest_s off, `rounds` times per section; fixed items then a max_effort item. "
        "rotation: team members rotate through `stations` for duration_min."))]
    rounds: Annotated[Optional[int], Field(ge=1)] = None
    rep_scheme: Annotated[Optional[list[int]], Field(description="e.g. [21, 15, 9]; applies to items without their own amount.")] = None
    time_cap_min: Annotated[Optional[float], Field(gt=0)] = None
    duration_min: Annotated[Optional[float], Field(gt=0)] = None
    interval_s: Annotated[Optional[float], Field(gt=0)] = None
    work_s: Annotated[Optional[float], Field(gt=0)] = None
    rest_s: Annotated[Optional[float], Field(ge=0)] = None
    team: Annotated[Optional[Team], Field(description="Omit for individual work.")] = None
    intensity: Intensity = "moderate"
    progression: Optional[Progression] = None
    items: list[MetconEntry] = []
    sections: list[Section] = []
    stations: list[Station] = []


Block = Annotated[Union[StrengthBlock, MetconBlock], Field(discriminator="type")]


class Session(Strict):
    """One class on the board (e.g. HY-FIT, WEIGHT, CARDIO, HYROX)."""
    id: SessionId
    title: str
    focus: Annotated[Optional[str], Field(description='Sub-heading on the board, e.g. "하체 스트렝스", "S.E".')] = None
    notes: Annotated[list[str], Field(description="Coach notes, verbatim.")] = []
    assumptions: Annotated[list[str], Field(description=(
        "Every interpretation you had to guess (team style, team-total vs each, implement...). Shown to the athlete."))] = []
    blocks: Annotated[list[Block], Field(min_length=1)]


# ── 기록 ─────────────────────────────────────────────────
class SetGroup(Strict):
    """`count` consecutive identical sets. reps_left / rpe / feel describe the LAST set of the group;
    give at most one of them (reps_left preferred)."""
    reps: Annotated[int, Field(ge=1, description="Reps actually completed per set (per side if the movement is per-side).")]
    count: Annotated[int, Field(ge=1, le=20)] = 1
    load: Annotated[Optional[Load], Field(description=(
        "External weight. Required for barbell/dumbbell/kettlebell/machine movements. "
        "For bodyweight movements: weight ADDED (omit for plain bodyweight)."))] = None
    assistance: Annotated[Optional[Load], Field(description="Bodyweight movements only: band/machine assistance.")] = None
    reps_left: Optional[RepsLeft] = None
    rpe: Optional[Rpe] = None
    feel: Annotated[Optional[Literal["easy", "good", "hard", "fail"]], Field(description=(
        "Only when the athlete cannot say how many reps were left. easy ≈ 4+ left, good ≈ 2 left, "
        "hard ≈ 0-1 left, fail = missed reps."))] = None

    @model_validator(mode="after")
    def _exclusive(self):
        if sum(x is not None for x in (self.reps_left, self.rpe, self.feel)) > 1:
            raise ValueError("give only one of reps_left / rpe / feel")
        if self.load is not None and self.assistance is not None:
            raise ValueError("give load or assistance, not both")
        return self


class TimeResult(Strict):
    """Finished a for_time workout."""
    type: Literal["time"]
    time: TimeStr


class CappedResult(Strict):
    """Hit the time cap before finishing a for_time workout."""
    type: Literal["capped"]
    rounds_done: Annotated[float, Field(gt=0, description="Rounds completed, counted the same way as the block's rounds (2.5 = two and a half).")]


class RoundsResult(Strict):
    """AMRAP score (team score for team AMRAPs)."""
    type: Literal["rounds"]
    rounds: Annotated[int, Field(ge=0)]
    extra_reps: Annotated[int, Field(ge=0)] = 0


class RotationResult(Strict):
    """Rotation workout: give my_rounds (times I completed the pacer station) OR team_rounds (total rotations)."""
    type: Literal["rotation"]
    my_rounds: Annotated[Optional[float], Field(gt=0)] = None
    team_rounds: Annotated[Optional[float], Field(gt=0)] = None

    @model_validator(mode="after")
    def _one(self):
        if (self.my_rounds is None) == (self.team_rounds is None):
            raise ValueError("give exactly one of my_rounds / team_rounds")
        return self


class NoScore(Strict):
    """No score to report (EMOM / interval, or score unknown). The work still counts toward fatigue."""
    type: Literal["none"]


MetconScore = Annotated[Union[TimeResult, CappedResult, RoundsResult, RotationResult, NoScore],
                        Field(discriminator="type")]


class UsedLoad(Strict):
    movement: MovementId
    load: Load


class ErgEffort(Strict):
    """A measured machine piece: calories or distance covered in a known time."""
    machine: Machine
    time: TimeStr
    cal: Annotated[Optional[float], Field(gt=0)] = None
    distance_m: Annotated[Optional[float], Field(gt=0)] = None
    effort: Annotated[Literal["max", "hard"], Field(description="max = all-out for that duration; hard = strong but not all-out.")] = "max"

    @model_validator(mode="after")
    def _one(self):
        if (self.cal is None) == (self.distance_m is None):
            raise ValueError("give exactly one of cal / distance_m")
        if self.distance_m is not None and self.machine == "assault_bike":
            raise ValueError("assault_bike: report cal")
        return self


class Soreness(Strict):
    region: Literal["legs", "chest", "shoulders", "back", "core", "upper"]
    level: Annotated[int, Field(ge=0, le=3, description="0 none, 1 mild, 2 clearly sore, 3 severe.")]
