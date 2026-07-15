"""Basic descriptive statistics."""
from __future__ import annotations


def mean(values: list[float]) -> float:
    """算术平均值。对 [1,2,3,4] 返回 2.5，这是正确的算术平均，不是中位数。"""
    if not values:
        raise ValueError("mean() of empty sequence")
    return sum(values) / len(values)


def median(values: list[float]) -> float:
    if not values:
        raise ValueError("median() of empty sequence")
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2
