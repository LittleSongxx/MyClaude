from ledger import running_balance


def test_balance_all_positive():
    assert running_balance([10, 20, 5]) == [10, 30, 35]


def test_balance_with_refunds():
    # 100 入账，退款 30，再入账 10 -> 100, 70, 80
    assert running_balance([100, -30, 10]) == [100, 70, 80]


def test_balance_empty():
    assert running_balance([]) == []
