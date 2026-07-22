from slug import slugify


def test_chinese_name_is_preserved():
    assert slugify("数据 平台") == "数据-平台"


def test_mixed_language_and_ascii_behavior():
    assert slugify("My 数据 API") == "my-数据-api"
    assert slugify("Hello, World!") == "hello-world"
