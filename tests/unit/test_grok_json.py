import pytest

from intelligence.grok_json import GrokError, clean_grok_json, parse_grok_json


def test_clean_grok_json_strips_markdown():
    raw = '```json\n{"variable": "rsi_buy_low", "new_value": 26}\n```'
    assert parse_grok_json(raw)["variable"] == "rsi_buy_low"


def test_parse_grok_json_required_keys():
    with pytest.raises(GrokError):
        parse_grok_json('{"variable": "x"}', required_keys=["variable", "new_value"])


def test_parse_grok_json_api_error():
    with pytest.raises(GrokError):
        parse_grok_json("API-Fehler: timeout")


def test_clean_grok_json_plain():
    assert clean_grok_json('  {"a": 1}  ') == '{"a": 1}'