"""Small bounded ring buffers for chart-relevant telemetry only.

Appends never grow memory beyond ``maxlen``; history lives in RAM and is
never persisted.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from app.models.telemetry import HistoryPoint

GPU_METRICS = ("utilization", "vram", "temperature", "power")


class BoundedHistory:
    """A fixed-size series of (timestamp, value) points."""

    def __init__(self, maxlen: int) -> None:
        if not 1 <= maxlen <= 10_000:
            raise ValueError("history maxlen must be between 1 and 10000")
        self.maxlen = maxlen
        self._points: deque[HistoryPoint] = deque(maxlen=maxlen)

    def append(self, ts: datetime, value: float) -> None:
        self._points.append(HistoryPoint(t=ts, v=round(float(value), 2)))

    def as_list(self) -> list[HistoryPoint]:
        return list(self._points)

    def __len__(self) -> int:
        return len(self._points)


@dataclass
class GpuSeries:
    """The four bounded scalar series kept for one GPU index."""

    maxlen: int
    utilization: BoundedHistory = field(init=False)
    vram: BoundedHistory = field(init=False)
    temperature: BoundedHistory = field(init=False)
    power: BoundedHistory = field(init=False)

    def __post_init__(self) -> None:
        for metric in GPU_METRICS:
            setattr(self, metric, BoundedHistory(self.maxlen))

    def series(self, metric: str) -> BoundedHistory:
        if metric not in GPU_METRICS:
            raise ValueError(f"unknown gpu history metric: {metric!r}")
        history: BoundedHistory = getattr(self, metric)
        return history
