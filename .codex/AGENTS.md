# .codex/AGENTS.md — SerenityMonitor

> Project-specific execution guide for Codex. See also: `AGENTS.md` (root).

## Stack & Commands

- **Python 3.12+**, Flask, SQLite, numpy, Sina Finance API
- Package manager: `pip` / `uv`
- CLI: `python3 cli.py status | rescore | monitor`
- Web server: `python3 app.py` (port **8400**), dashboard: `python3 dash_dashboard.py` (port **8050**)

## Execution Rules

- Only main-board tickers (000/002/600/601/603/605); no STAR (688) or ChiNext (300/301).
- New scoring factors → `scorer.py`. DB operations → `db.py`.
- CLI commands registered in `cli.py`'s `commands` dict.
- Do not touch trade/position calculation logic.
