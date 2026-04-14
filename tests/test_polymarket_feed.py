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
