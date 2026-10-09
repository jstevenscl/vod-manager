"""A failing AI provider (bad/retired model, quota, rejected key) used to look
exactly like "nothing fit the category": evaluate_candidates_for_category logged
and skipped every failed batch and returned []. The interactive route can now
ask for the real error; the scheduler keeps the old, non-raising behaviour so a
persistent failure isn't retried every poll tick."""

import asyncio

import pytest

import ai_assist

CANDIDATES = [{"id": i, "name": f"Movie {i}", "year": 2000 + i} for i in range(1, 6)]


def _run(monkeypatch, behaviours, **kwargs):
    calls = []

    async def fake_call_ai(*args, **kw):
        calls.append(1)
        outcome = behaviours[min(len(calls) - 1, len(behaviours) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(ai_assist, "_call_ai", fake_call_ai)
    monkeypatch.setattr(ai_assist, "_AI_EVAL_BATCH_SIZE", 2)  # 5 candidates -> 3 batches
    return asyncio.run(ai_assist.evaluate_candidates_for_category("animated films", "movie", CANDIDATES, **kwargs))


def test_default_keeps_the_non_raising_behaviour(monkeypatch):
    assert _run(monkeypatch, [ValueError("gemini request failed (404): model retired")]) == []


def test_interactive_call_raises_the_real_error_when_every_batch_fails(monkeypatch):
    with pytest.raises(ValueError, match="model retired"):
        _run(monkeypatch, [ValueError("gemini request failed (404): model retired")], raise_if_all_failed=True)


def test_partial_failure_returns_what_succeeded_without_raising(monkeypatch):
    ok = {"matches": [0]}
    result = _run(monkeypatch, [ok, ValueError("blip"), ok], raise_if_all_failed=True)
    assert result == [1, 5]  # first item of batch 1 and of batch 3


def test_a_legitimate_empty_answer_is_not_an_error(monkeypatch):
    assert _run(monkeypatch, [{"matches": []}], raise_if_all_failed=True) == []
