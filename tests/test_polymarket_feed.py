from polypocket.feeds.polymarket import Window, parse_5min_btc_markets, parse_book_event


def test_parse_5min_btc_markets():
    """Should extract active 5-min BTC up/down markets with priceToBeat."""
    markets = [
        {
            "condition_id": "abc123",
            "question": "Bitcoin Up or Down - April 14, 4:00PM-4:05PM ET",
            "slug": "btc-updown-5m-1776196800",
            "tokens": [
                {"token_id": "tok_yes", "outcome": "Up"},
                {"token_id": "tok_no", "outcome": "Down"},
            ],
            "end_date_iso": "2026-04-14T20:05:00Z",
            "closed": False,
            "eventMetadata": {"priceToBeat": 84198.123456},
        },
        {
            "condition_id": "def456",
            "question": "Will ETH hit $5000?",
            "slug": "eth-5000",
            "tokens": [],
            "end_date_iso": "2026-05-01T00:00:00Z",
            "closed": False,
        },
    ]
    windows = parse_5min_btc_markets(markets)
    assert len(windows) == 1
    assert windows[0].condition_id == "abc123"
    assert windows[0].up_token_id == "tok_yes"
    assert windows[0].down_token_id == "tok_no"
    assert windows[0].price_to_beat == 84198.123456


def test_parse_book_event():
    """Should extract best ask price and size from a book event."""
    msg = {
        "event_type": "book",
        "asset_id": "tok_yes",
        "market": "abc123",
        "asks": [
            {"price": "0.58", "size": "100"},
            {"price": "0.60", "size": "50"},
        ],
        "bids": [
            {"price": "0.55", "size": "80"},
        ],
    }
    result = parse_book_event(msg)
    assert result["asset_id"] == "tok_yes"
    assert result["best_ask"] == 0.58
    assert result["best_ask_size"] == 100.0


def test_window_dataclass():
    window = Window(
        condition_id="abc",
        question="BTC Up or Down",
        up_token_id="tok_up",
        down_token_id="tok_down",
        end_time=1713100000.0,
        slug="btc-updown-5m-1776196800",
        price_to_beat=84198.123456,
    )
    assert window.start_time == 1713099700.0
    assert window.price_to_beat == 84198.123456


def test_parse_book_event_extracts_top_3_levels():
    msg = {
        "asset_id": "tok_up",
        "asks": [
            {"price": "0.60", "size": "50"},
            {"price": "0.55", "size": "120"},
            {"price": "0.57", "size": "80"},
            {"price": "0.65", "size": "30"},
            {"price": "0.56", "size": "90"},
        ],
    }
    result = parse_book_event(msg)
    assert result["best_ask"] == 0.55
    assert result["best_ask_size"] == 120.0
    assert len(result["top_asks"]) == 3
    assert result["top_asks"][0] == {"price": 0.55, "size": 120.0}
    assert result["top_asks"][1] == {"price": 0.56, "size": 90.0}
    assert result["top_asks"][2] == {"price": 0.57, "size": 80.0}


def test_parse_book_event_fewer_than_3_asks():
    msg = {
        "asset_id": "tok_up",
        "asks": [
            {"price": "0.55", "size": "120"},
        ],
    }
    result = parse_book_event(msg)
    assert len(result["top_asks"]) == 1
    assert result["top_asks"][0] == {"price": 0.55, "size": 120.0}


def test_parse_book_event_empty_asks():
    msg = {"asset_id": "tok_up", "asks": []}
    result = parse_book_event(msg)
    assert result["top_asks"] == []
    assert result["best_ask"] is None


def test_parse_book_event_extracts_top_3_bids_descending():
    """Bids must be returned highest-first; needed for pair-merge limit."""
    msg = {
        "asset_id": "tok_up",
        "asks": [],
        "bids": [
            {"price": "0.40", "size": "50"},
            {"price": "0.45", "size": "120"},
            {"price": "0.42", "size": "80"},
            {"price": "0.30", "size": "200"},
            {"price": "0.44", "size": "90"},
        ],
    }
    result = parse_book_event(msg)
    assert result["best_bid"] == 0.45
    assert result["best_bid_size"] == 120.0
    assert len(result["top_bids"]) == 3
    assert result["top_bids"][0] == {"price": 0.45, "size": 120.0}
    assert result["top_bids"][1] == {"price": 0.44, "size": 90.0}
    assert result["top_bids"][2] == {"price": 0.42, "size": 80.0}


def test_parse_book_event_empty_bids():
    msg = {"asset_id": "tok_up", "asks": [], "bids": []}
    result = parse_book_event(msg)
    assert result["top_bids"] == []
    assert result["best_bid"] is None


def test_parse_book_event_missing_bids_key():
    """Legacy events without a bids field must return empty bid state, not KeyError."""
    msg = {"asset_id": "tok_up", "asks": [{"price": "0.55", "size": "10"}]}
    result = parse_book_event(msg)
    assert result["top_bids"] == []
    assert result["best_bid"] is None
