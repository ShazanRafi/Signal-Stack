from signalstack.domain.brief_check import check_text, unsupported_numbers, valid_ids

CORPUS = "Fees rise 12% in 2026 for 3,500 students"


def test_numbers_supported_or_flagged():
    assert unsupported_numbers("Fees rise 12% [S1]", CORPUS) == []
    assert unsupported_numbers("Fees rise 45% [S1]", CORPUS) == ["45%"]
    assert unsupported_numbers("about 3,500 students", CORPUS) == []
    assert unsupported_numbers("in 2031 and 3 firms", CORPUS) == []  
    assert unsupported_numbers("the [S45] item", CORPUS) == []        


def test_check_text_rules():
    assert check_text("Fine claim [S1].", {1}, CORPUS) is None
    assert "no source markers" in check_text("Unsupported claim.", {1}, CORPUS)
    assert "not in this section" in check_text("Claim [S2].", {1}, CORPUS)
    assert "unsupported number" in check_text("Fees rise 99% [S1].", {1}, CORPUS)


def test_valid_ids():
    assert valid_ids([1, 1, 2], {1, 2}) == [1, 2]
    assert valid_ids([], {1}) is None
    assert valid_ids([1, 9], {1}) is None