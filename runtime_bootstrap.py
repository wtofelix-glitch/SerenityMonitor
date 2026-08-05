"""Keep direct Serenity entrypoints on the project-managed Python runtime."""

from __future__ import annotations

import os
import sys
from typing import Optional


BOOTSTRAP_ENV = "SERENITY_RUNTIME_BOOTSTRAPPED"
PYTHON_ENV = "SERENITY_PYTHON"


def find_project_python(project_dir: Optional[str] = None) -> Optional[str]:
    """Return the configured/project virtualenv interpreter when available."""
    override = os.environ.get(PYTHON_ENV, "").strip()
    candidates = [override] if override else []
    root = project_dir or os.path.dirname(os.path.abspath(__file__))
    candidates.extend([
        os.path.join(root, ".venv", "bin", "python3"),
        os.path.join(root, ".venv", "bin", "python"),
    ])
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return os.path.abspath(candidate)
    return None


def ensure_project_runtime(entry_file: str) -> None:
    """Re-exec a directly-invoked script under Serenity's virtualenv once."""
    if os.environ.get(BOOTSTRAP_ENV) == "1":
        return
    project_dir = os.path.dirname(os.path.abspath(entry_file))
    target = find_project_python(project_dir)
    if not target:
        return
    try:
        if os.path.samefile(sys.executable, target):
            return
    except OSError:
        if os.path.realpath(sys.executable) == os.path.realpath(target):
            return

    env = os.environ.copy()
    env[BOOTSTRAP_ENV] = "1"
    os.execve(target, [target, os.path.abspath(entry_file), *sys.argv[1:]], env)
