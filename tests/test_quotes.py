from polypocket.quotes import QuoteSnapshot, validate_quote


def test_validate_quote_missing_side():
    snapshot = QuoteSnapshot(up_ask=0.48, down_ask=None)

    result = validate_quote(snapshot)

    assert result.valid is False
    assert result.reason == "missing-side"


def test_validate_quote_rejects_out_of_range_ask():
    snapshot = QuoteSnapshot(up_ask=1.01, down_ask=0.01)

    result = validate_quote(snapshot)

    assert result.valid is False
    assert result.reason == "ask-out-of-range"


def test_validate_quote_rejects_overround():
    snapshot = QuoteSnapshot(up_ask=0.52, down_ask=0.51)

    result = validate_quote(snapshot)

    assert result.valid is False
    assert result.reason == "overround"


def test_validate_quote_accepts_sane_two_sided_book():
    snapshot = QuoteSnapshot(up_ask=0.49, down_ask=0.50)

    result = validate_quote(snapshot)

    assert result.valid is True
    assert result.reason is None
