from services.desk.hud import desk_enabled


def test_desk_disabled_by_default_without_flag():
    assert desk_enabled({}) is False


def test_desk_enabled_flag():
    assert desk_enabled({"desk": {"enabled": True}}) is True
    assert desk_enabled({"desk": {"enabled": False}}) is False
