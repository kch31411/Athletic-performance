"""잠재 능력치에 대한 가우시안 믿음(평균 + 공분산)과 칼만 갱신.

모든 능력치는 로그 스케일의 '요인(factor)'이다. 값 0 은 "프로필에 적은 수준의
평균적인 사람"을 뜻하고, +0.10 은 그보다 약 10% 높다는 뜻이다.

요인 이름 규칙
    str                 전신 근력
    str.pat:<pattern>   패턴별 근력 (squat, hinge, push_h ...)
    str.mov:<movement>  동작별 근력 편차
    end                 근지구력 (반복수-강도 곡선의 기울기)
    end.pat:<pattern>   패턴별 근지구력
    eng                 심폐 엔진 (지속 가능한 출력)
    eng.mod:<modality>  머신별 편차 (row, ski, bike, assault, run)
    ana                 무산소 용량 (짧은 전력 구간의 여유)
    met                 복합 수행능력 (메트콘 페이스)
    met.cat:<W|G>       웨이트 위주 / 맨몸 위주 메트콘 편차
    met.dur:<bucket>    길이별 편차 (short, medium, long)
    hyx.sta:<station>   HYROX 스테이션별 편차 (기술·요령 등 다른 능력치로 설명 안 되는 부분)
    hyx.run             HYROX 스테이션 사이 러닝(다리가 무거운 상태)의 편차
    cond                오늘의 컨디션 (매일 초기화)
    loadpref            메트콘 무게 선호 (가볍게/무겁게 피드백으로 학습)
"""
from __future__ import annotations

import math

import numpy as np

# 접두사 → (사전 표준편차, 하루당 표류 표준편차)
PRIORS: dict[str, tuple[float, float]] = {
    "str.pat": (0.10, 0.0030),
    "str.mov": (0.10, 0.0030),
    "str": (0.20, 0.0040),
    "end.pat": (0.15, 0.0040),
    "end": (0.20, 0.0040),
    "eng.mod": (0.10, 0.0030),
    "eng": (0.18, 0.0050),
    "ana": (0.30, 0.0050),
    "met.cat": (0.10, 0.0030),
    "met.dur": (0.08, 0.0030),
    "met": (0.20, 0.0050),
    "hyx.sta": (0.15, 0.0030),
    "hyx.run": (0.06, 0.0030),
    "cond": (0.03, 0.0),
    "loadpref": (0.15, 0.0),
}


def prior_for(name: str) -> tuple[float, float]:
    head = name.split(":")[0]
    if head not in PRIORS:
        raise KeyError(f"알 수 없는 요인: {name}")
    return PRIORS[head]


class Belief:
    def __init__(self) -> None:
        self.names: list[str] = []
        self.idx: dict[str, int] = {}
        self.mean = np.zeros(0)
        self.cov = np.zeros((0, 0))

    # ── 요인 관리 ────────────────────────────────────────
    def ensure(self, name: str) -> int:
        if name in self.idx:
            return self.idx[name]
        sd, _ = prior_for(name)
        n = len(self.names)
        cov = np.zeros((n + 1, n + 1))
        cov[:n, :n] = self.cov
        cov[n, n] = sd * sd
        self.cov = cov
        self.mean = np.append(self.mean, 0.0)
        self.names.append(name)
        self.idx[name] = n
        return n

    def _vec(self, h: dict[str, float]) -> np.ndarray:
        for name in h:
            self.ensure(name)
        v = np.zeros(len(self.names))
        for name, w in h.items():
            v[self.idx[name]] += w
        return v

    def value(self, name: str) -> float:
        i = self.ensure(name)
        return float(self.mean[i])

    def sd(self, name: str) -> float:
        i = self.ensure(name)
        return math.sqrt(max(self.cov[i, i], 0.0))

    # ── 예측 / 갱신 ──────────────────────────────────────
    def predict(self, h: dict[str, float]) -> tuple[float, float]:
        """선형 결합 h·x 의 (평균, 분산)."""
        v = self._vec(h)
        return float(v @ self.mean), float(v @ self.cov @ v)

    def observe(self, h: dict[str, float], residual: float, noise_sd: float,
                bound_only: bool = False) -> float:
        """관측 = h·x + 잡음. residual = 관측값 − 현재 예측값. 칼만 이득의 합을 돌려준다.

        bound_only: '최소 이만큼'이라는 하한 정보일 때. 평균은 옮기되 확신(분산)은 그대로 둔다.
        """
        v = self._vec(h)
        pv = self.cov @ v
        s = float(v @ pv) + noise_sd * noise_sd
        gain = pv / s
        self.mean = self.mean + gain * residual
        if bound_only:
            return float(v @ gain)
        self.cov = self.cov - np.outer(gain, pv)
        self.cov = (self.cov + self.cov.T) / 2.0
        return float(v @ gain)

    def advance(self, days: float) -> None:
        """시간이 지나면 능력치에 대한 확신이 조금씩 흐려진다(랜덤워크)."""
        days = min(max(days, 0.0), 90.0)
        if days <= 0:
            return
        for name, i in self.idx.items():
            _, drift = prior_for(name)
            self.cov[i, i] += drift * drift * days

    def reset(self, name: str, mean: float = 0.0, sd: float | None = None) -> None:
        i = self.ensure(name)
        if sd is None:
            sd, _ = prior_for(name)
        self.cov[i, :] = 0.0
        self.cov[:, i] = 0.0
        self.cov[i, i] = sd * sd
        self.mean[i] = mean
