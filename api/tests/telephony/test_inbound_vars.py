from api.services.telephony.inbound_vars import normalize_passthrough_vars


def test_strips_prefix_variants():
    assert normalize_passthrough_vars(
        {"X-first_name": "Ada", "x-last": "L", "X_age": 36, "x_city": "London"}
    ) == {"first_name": "Ada", "last": "L", "age": 36, "city": "London"}


def test_keeps_plain_and_does_not_overstrip():
    assert normalize_passthrough_vars({"already": "ok", "xerox": "printer"}) == {
        "already": "ok",
        "xerox": "printer",
    }


def test_drops_reserved_after_strip():
    assert normalize_passthrough_vars(
        {"X-first_name": "Ada", "x-provider": "evil", "X-called_number": "9"}
    ) == {"first_name": "Ada"}


def test_drops_non_scalar():
    assert normalize_passthrough_vars(
        {"x-ok": "v", "x-bad": {"n": 1}, "x-list": [1, 2]}
    ) == {"ok": "v"}


def test_non_dict_returns_empty():
    assert normalize_passthrough_vars(None) == {}
    assert normalize_passthrough_vars("nope") == {}
    assert normalize_passthrough_vars([]) == {}


def test_scalar_types_preserved():
    out = normalize_passthrough_vars(
        {"x-s": "str", "x-i": 7, "x-f": 1.5, "x-b": True, "x-n": None}
    )
    assert out == {"s": "str", "i": 7, "f": 1.5, "b": True, "n": None}
