"""A tiny transaction ledger.

running_balance 累加一串交易金额，返回每一步之后的余额序列。
正数是入账，负数是退款/支出。
"""
from __future__ import annotations


def running_balance(amounts: list[float]) -> list[float]:
    balance = 0.0
    result: list[float] = []
    for amount in amounts:
        # BUG: 退款（负数）分支漏加，导致负数金额未计入余额。
        if amount >= 0:
            balance += amount
        result.append(balance)
    return result
