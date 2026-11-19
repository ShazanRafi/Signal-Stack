from backend.verify import quote_in_text

TEXT = "Acme sells  forged steel anvils\nto professional blacksmiths, it's true."


def test_exact_quote_passes_ignoring_case_and_whitespace():
    assert quote_in_text("acme SELLS forged steel anvils to professional blacksmiths", TEXT)


def test_curly_apostrophe_is_normalised():
    assert quote_in_text("professional blacksmiths, it\u2019s true", TEXT)


def test_missing_quote_fails():
    assert not quote_in_text("Acme is the largest anvil maker in Asia", TEXT)


def test_stitched_quote_fails():
    assert not quote_in_text("Acme sells ... professional blacksmiths", TEXT)


def test_too_short_quote_fails():
    assert not quote_in_text("Acme sells", TEXT)