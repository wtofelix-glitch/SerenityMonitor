"""Tests for stable interpreter selection across Serenity entrypoints."""

import os
import sys

import runtime_bootstrap


def test_find_project_python_prefers_project_virtualenv(tmp_path, monkeypatch):
    monkeypatch.delenv(runtime_bootstrap.PYTHON_ENV, raising=False)
    interpreter = tmp_path / ".venv" / "bin" / "python3"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("#!/bin/sh\n", encoding="ascii")
    interpreter.chmod(0o755)

    assert runtime_bootstrap.find_project_python(str(tmp_path)) == str(interpreter)


def test_find_project_python_honours_explicit_override(tmp_path, monkeypatch):
    interpreter = tmp_path / "serenity-python"
    interpreter.write_text("#!/bin/sh\n", encoding="ascii")
    interpreter.chmod(0o755)
    monkeypatch.setenv(runtime_bootstrap.PYTHON_ENV, str(interpreter))

    assert runtime_bootstrap.find_project_python("/missing") == str(interpreter)


def test_bridge_tasks_inherit_bridge_interpreter():
    import serenity_bridge_server

    assert all(command[0] == sys.executable for command in serenity_bridge_server.TASKS.values())
    assert all(os.path.isabs(command[0]) for command in serenity_bridge_server.TASKS.values())
