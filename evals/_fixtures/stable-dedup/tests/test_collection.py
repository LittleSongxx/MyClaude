from collection import unique_items


def test_preserves_first_seen_order():
    assert unique_items(["b", "a", "b", "c", "a"]) == ["b", "a", "c"]


def test_supports_unhashable_values():
    assert unique_items([{"id": 1}, {"id": 1}, {"id": 2}]) == [{"id": 1}, {"id": 2}]
