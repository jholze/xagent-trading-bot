from core.test_symbols import is_phantom_test_symbol


def test_phantom_symbols_detected():
    assert is_phantom_test_symbol("SENSOR15/USDT")
    assert is_phantom_test_symbol("XENTRY15/USDT")
    assert is_phantom_test_symbol("TEST/PNL")
    assert not is_phantom_test_symbol("DOGE/USDT")
    assert not is_phantom_test_symbol("BTC/USDT")