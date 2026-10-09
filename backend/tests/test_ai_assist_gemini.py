"""GH#51: Gemini disambiguation failed for two reasons -- a list-valued JSON
Schema `type` (["integer","null"]) that Gemini rejects with 'Schema type must
not be a list', and thinking tokens eating the fixed 512 maxOutputTokens so the
function call was cut off (finishReason MALFORMED_FUNCTION_CALL)."""

import asyncio
import copy

import pytest

import ai_assist


class _Resp:
    status_code = 200

    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


def _fake_client(responses, sent):
    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, params=None, json=None):
            sent.append(json)
            return _Resp(responses[min(len(sent) - 1, len(responses) - 1)])

    return FakeClient


def _call(monkeypatch, responses, model="gemini-2.5-flash", schema=None, max_tokens=512):
    sent = []
    monkeypatch.setattr(ai_assist, "get_gemini_api_key", lambda: "synthetic-key")
    monkeypatch.setattr(ai_assist.httpx, "AsyncClient", _fake_client(responses, sent))
    schema = schema if schema is not None else ai_assist._YEAR_MATCH_SCHEMA
    result = asyncio.run(ai_assist._call_gemini(model, "sys", "msg", "report_match", "desc", schema, max_tokens))
    return result, sent


def _ok(args):
    return {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"functionCall": {"name": "report_match", "args": args}}]}}]}


def _no_list_types(node):
    if isinstance(node, dict):
        assert not isinstance(node.get("type"), list), node
        assert "additionalProperties" not in node
        return all(_no_list_types(v) for v in node.values()) if node else True
    if isinstance(node, list):
        return all(_no_list_types(v) for v in node)
    return True


def test_year_match_schema_becomes_nullable_not_a_type_list(monkeypatch):
    original = copy.deepcopy(ai_assist._YEAR_MATCH_SCHEMA)
    _, sent = _call(monkeypatch, [_ok({"best_match_index": None, "reasoning": "r", "confidence": "low"})])
    params = sent[0]["tools"][0]["function_declarations"][0]["parameters"]
    assert params["properties"]["best_match_index"] == {
        "type": "integer", "nullable": True,
        "description": "Index into the candidate list of the best match, or null if none are a confident match.",
    }
    assert ai_assist._YEAR_MATCH_SCHEMA == original  # the shared schema (Anthropic/OpenAI) is untouched


def test_every_schema_is_gemini_safe():
    for schema in (ai_assist._RULE_SCHEMA, ai_assist._BATCH_SCHEMA, ai_assist._YEAR_MATCH_SCHEMA, ai_assist._DUPLICATE_VERIFY_SCHEMA):
        assert _no_list_types(ai_assist._gemini_schema(schema))


def test_a_null_answer_is_returned_as_is(monkeypatch):
    result, _ = _call(monkeypatch, [_ok({"best_match_index": None, "reasoning": "none fit", "confidence": "low"})])
    assert result["best_match_index"] is None


def test_flash_disables_thinking_and_has_headroom(monkeypatch):
    _, sent = _call(monkeypatch, [_ok({"best_match_index": 1, "reasoning": "r", "confidence": "high"})], model="gemini-2.5-flash")
    cfg = sent[0]["generationConfig"]
    assert cfg["thinkingConfig"] == {"thinkingBudget": 0}
    assert cfg["maxOutputTokens"] >= 2048  # was 512


def test_pro_keeps_the_minimum_thinking_budget(monkeypatch):
    # 2.5 Pro cannot turn thinking off: a budget of 0 is rejected
    _, sent = _call(monkeypatch, [_ok({"best_match_index": 1, "reasoning": "r", "confidence": "high"})], model="gemini-2.5-pro")
    assert sent[0]["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 128}


def test_unknown_model_gets_no_thinking_config_but_a_bigger_cap(monkeypatch):
    _, sent = _call(monkeypatch, [_ok({"best_match_index": 1, "reasoning": "r", "confidence": "high"})], model="gemini-3-future")
    cfg = sent[0]["generationConfig"]
    assert "thinkingConfig" not in cfg and cfg["maxOutputTokens"] >= 4096


def test_truncated_call_is_retried_once_with_more_room(monkeypatch):
    truncated = {"candidates": [{"finishReason": "MALFORMED_FUNCTION_CALL", "finishMessage": "Malformed function call: print(default_api.report_match(best_match_"}]}
    result, sent = _call(monkeypatch, [truncated, _ok({"best_match_index": 2, "reasoning": "r", "confidence": "high"})])
    assert result["best_match_index"] == 2 and len(sent) == 2
    assert sent[1]["generationConfig"]["maxOutputTokens"] > sent[0]["generationConfig"]["maxOutputTokens"]


def test_persistent_truncation_raises_a_specific_error(monkeypatch):
    truncated = {"candidates": [{"finishReason": "MAX_TOKENS"}]}
    with pytest.raises(ValueError, match="finishReason: MAX_TOKENS"):
        _call(monkeypatch, [truncated, truncated])


def test_a_safety_stop_is_not_retried(monkeypatch):
    blocked = {"candidates": [{"finishReason": "SAFETY"}]}
    sent = []
    monkeypatch.setattr(ai_assist, "get_gemini_api_key", lambda: "synthetic-key")
    monkeypatch.setattr(ai_assist.httpx, "AsyncClient", _fake_client([blocked], sent))
    with pytest.raises(ValueError, match="SAFETY"):
        asyncio.run(ai_assist._call_gemini("gemini-2.5-flash", "s", "m", "report_match", "d", ai_assist._YEAR_MATCH_SCHEMA, 512))
    assert len(sent) == 1


def test_gemini_503_is_retried_then_succeeds(monkeypatch):
    sleeps = []

    async def no_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(ai_assist.asyncio, "sleep", no_sleep)
    sent = []

    class Busy:
        status_code = 503

        def raise_for_status(self):
            raise AssertionError("a retried 503 must not surface")

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, params=None, json=None):
            sent.append(json)
            if len(sent) <= 2:
                return Busy()
            resp = _Resp(_ok({"best_match_index": 0, "reasoning": "r", "confidence": "high"}))
            resp.status_code = 200
            return resp

    monkeypatch.setattr(ai_assist, "get_gemini_api_key", lambda: "synthetic-key")
    monkeypatch.setattr(ai_assist.httpx, "AsyncClient", FakeClient)
    result = asyncio.run(ai_assist._call_gemini("gemini-flash-latest", "s", "m", "report_match", "d", ai_assist._YEAR_MATCH_SCHEMA, 512))
    assert result["best_match_index"] == 0 and len(sent) == 3 and sleeps == [2.0, 5.0]


def test_gemini_quota_429_is_not_retried(monkeypatch):
    import httpx
    sent = []

    class Quota:
        status_code = 429
        text = "quota"

        def raise_for_status(self):
            raise httpx.HTTPStatusError("429", request=None, response=self)

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, params=None, json=None):
            sent.append(json)
            return Quota()

    monkeypatch.setattr(ai_assist, "get_gemini_api_key", lambda: "synthetic-key")
    monkeypatch.setattr(ai_assist.httpx, "AsyncClient", FakeClient)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(ai_assist._call_gemini("gemini-flash-latest", "s", "m", "report_match", "d", ai_assist._YEAR_MATCH_SCHEMA, 512))
    assert len(sent) == 1


def test_default_gemini_model_is_the_latest_alias():
    import config
    assert config._AI_DEFAULT_MODELS["gemini"] == "gemini-flash-latest"
