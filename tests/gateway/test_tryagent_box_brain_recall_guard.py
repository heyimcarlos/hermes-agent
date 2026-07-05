from __future__ import annotations

import json
from pathlib import Path


def _write_setup(home: Path, *, state: str = "configured", provider: str = "gbrain") -> None:
    setup_dir = home / ".tryagent" / "memory"
    setup_dir.mkdir(parents=True)
    (setup_dir / "setup.json").write_text(
        json.dumps({"state": state, "provider": provider})
    )


def test_tryagent_recall_context_detects_natural_phrase_question(monkeypatch, tmp_path):
    from gateway import run

    monkeypatch.setattr(run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        run,
        "_tryagent_gbrain_recall_output",
        lambda message: 'GBrain query results:\npersonal/rehearsal-phrase -- maple signal 846',
    )
    _write_setup(tmp_path)

    context = run._tryagent_box_brain_recall_context_for_message(
        "What is my current launch rehearsal phrase?"
    )

    assert context is not None
    assert "<memory-context>" in context
    assert "TryAgent Box Brain recall context" in context
    assert "maple signal 846" in context
    assert "Hermes user profile" in context
    assert "do not call GBrain, MCP, or other memory-provider tools" in context
    assert "mcp_gbrain" not in context


def test_tryagent_recall_context_detects_cross_channel_memory(monkeypatch, tmp_path):
    from gateway import run

    monkeypatch.setattr(run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        run,
        "_tryagent_gbrain_recall_output",
        lambda message: 'GBrain search results:\npersonal/rehearsal-phrase -- maple signal 846',
    )
    _write_setup(tmp_path)

    context = run._tryagent_box_brain_recall_context_for_message(
        "What was the thing I saved from Discord?"
    )

    assert context is not None
    assert "maple signal 846" in context


def test_tryagent_recall_context_requires_configured_gbrain(monkeypatch, tmp_path):
    from gateway import run

    monkeypatch.setattr(run, "_hermes_home", tmp_path)
    _write_setup(tmp_path, state="required", provider="")

    context = run._tryagent_box_brain_recall_context_for_message(
        "What is my current launch rehearsal phrase?"
    )

    assert context is None


def test_tryagent_recall_context_ignores_non_memory_questions(monkeypatch, tmp_path):
    from gateway import run

    monkeypatch.setattr(run, "_hermes_home", tmp_path)
    _write_setup(tmp_path)

    context = run._tryagent_box_brain_recall_context_for_message(
        "What is the current weather in Toronto?"
    )

    assert context is None


def test_tryagent_recall_context_fails_closed_when_provider_unavailable(monkeypatch, tmp_path):
    from gateway import run

    monkeypatch.setattr(run, "_hermes_home", tmp_path)
    monkeypatch.setattr(run, "_tryagent_gbrain_recall_output", lambda message: None)
    _write_setup(tmp_path)

    context = run._tryagent_box_brain_recall_context_for_message(
        "What is my current launch rehearsal phrase?"
    )

    assert context is not None
    assert "Box Brain search is unavailable right now" in context
    assert "Do not answer owner-personal memory questions from platform history" in context


def test_tryagent_recall_turn_disables_gbrain_mcp_toolsets():
    from gateway import run

    enabled, disabled = run._tryagent_recall_turn_toolsets(["terminal", "gbrain"])

    assert enabled == []
    assert disabled == ["terminal", "gbrain", "mcp-gbrain"]
