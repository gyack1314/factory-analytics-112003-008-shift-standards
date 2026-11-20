"""时间区间工具与经验分布统计。

区间统一为 ``[start, end)`` 半开整数分钟区间，右端不包含。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

Interval = tuple[int, int]


def overlap(a: Interval, b: Interval) -> int:
    """两个区间的重叠分钟数。"""
    lo = max(a[0], b[0])
    hi = min(a[1], b[1])
    return max(0, hi - lo)


def merge(intervals: list[Interval]) -> list[Interval]:
    """合并互相接触或重叠的区间，返回有序不相交区间列表。"""
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged: list[Interval] = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def union_len(intervals: list[Interval]) -> int:
    """若干区间的并集总长度。"""
    return sum(end - start for start, end in merge(intervals))


def clip(interval: Interval, window: Interval) -> Interval | None:
    """把区间裁剪到窗口内；完全不相交返回 None。"""
    lo = max(interval[0], window[0])
    hi = min(interval[1], window[1])
    if lo < hi:
        return lo, hi
    return None


def shift_containing(t: int, boundaries: tuple[int, ...]) -> int:
    """返回时刻 t 所属班次的序号（按班次边界划分）。

    boundaries 为有序边界点，班次 k 覆盖 [boundaries[k], boundaries[k+1])；
    早于第一个边界归入班次 0。
    """
    for k in range(len(boundaries) - 1):
        if boundaries[k] <= t < boundaries[k + 1]:
            return k
    if t < boundaries[0]:
        return -1
    return len(boundaries) - 1


def split_by_shifts(interval: Interval,
                    boundaries: tuple[int, ...]) -> list[tuple[int, Interval]]:
    """把一个区间按班次边界切开，返回 (班次序号, 子区间) 列表。"""
    start, end = interval
    out: list[tuple[int, Interval]] = []
    cursor = start
    k = shift_containing(cursor, boundaries)
    while cursor < end:
        if k < 0:  # 早于首条边界
            nxt = boundaries[0]
            out.append((k, (cursor, min(end, nxt))))
            cursor = min(end, nxt)
            k = 0
            continue
        upper = boundaries[k + 1] if k + 1 < len(boundaries) else math.inf
        nxt = min(end, upper)
        out.append((k, (cursor, int(nxt))))
        cursor = int(nxt)
        k += 1
    return out


@dataclass(frozen=True)
class EmpiricalDistribution:
    """经验分布：样本、分位数与稳健离散度。

    样本量不足时不给分位数（None），由调用方决定降级口径。
    """
    samples: tuple[float, ...]
    p10: float | None
    p25: float | None
    median: float | None
    p75: float | None
    p90: float | None
    mean: float | None
    std: float | None

    @property
    def n(self) -> int:
        return len(self.samples)

    def as_dict(self) -> dict:
        return {
            "n": self.n,
            "p10": self.p10, "p25": self.p25, "median": self.median,
            "p75": self.p75, "p90": self.p90,
            "mean": round(self.mean, 2) if self.mean is not None else None,
            "std": round(self.std, 2) if self.std is not None else None,
        }


def quantile(sorted_samples: list[float], q: float) -> float:
    """线性插值分位数（与 numpy 默认 'linear' 口径一致）。"""
    if not sorted_samples:
        raise ValueError("空样本无法计算分位数")
    if len(sorted_samples) == 1:
        return float(sorted_samples[0])
    pos = q * (len(sorted_samples) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return float(sorted_samples[lo])
    frac = pos - lo
    return (sorted_samples[lo] * (1 - frac)
            + sorted_samples[hi] * frac)


def distribution(samples: list[float], min_n: int = 4) -> EmpiricalDistribution:
    """构造经验分布；样本少于 min_n 时只保留均值，不给分位数。"""
    n = len(samples)
    mean = sum(samples) / n if n else None
    std = None
    p10 = p25 = med = p75 = p90 = None
    if n:
        ordered = sorted(samples)
        if n >= min_n:
            p10 = round(quantile(ordered, 0.10), 2)
            p25 = round(quantile(ordered, 0.25), 2)
            med = round(quantile(ordered, 0.50), 2)
            p75 = round(quantile(ordered, 0.75), 2)
            p90 = round(quantile(ordered, 0.90), 2)
        if n >= 2:
            var = sum((x - mean) ** 2 for x in samples) / (n - 1)
            std = math.sqrt(var)
    return EmpiricalDistribution(
        samples=tuple(round(x, 2) for x in samples),
        p10=p10, p25=p25, median=med, p75=p75, p90=p90,
        mean=mean, std=std,
    )
