# CLAUDE.md — SerenityMonitor

> 继承 `workspace/CLAUDE.md` 所有规范。本文档为项目级覆盖。

---

## Project Overview

**Project name:** SerenityMonitor

**Tech stack:**
- Language: Python 3.12+
- Web: Flask (dashboard, port 8400) + Plotly Dash (charts, port 8050)
- Database: SQLite (via `db.py`)
- Key libs: numpy, Sina Finance API (request)
- Test framework: **None (not yet implemented)**

**Important directories:**
- `cli.py` — CLI entry point (20+ commands)
- `app.py` — Flask dashboard (port 8400, inline HTML/CSS/JS)
- `dash_dashboard.py` — Dash panel (port 8050, candlestick/bar/scatter/radar charts)
- `db.py` — Database operations
- `scorer.py` — Multi-factor scoring engine (single file)
- `serenity_watcher.py` — Market data watchers

## Verification Commands

```bash
# CLI
python3 cli.py status
python3 cli.py rescore
python3 cli.py monitor

# Web
python3 app.py

# Dash
python3 dash_dashboard.py
```

> 运行 `ruff check .` 做代码检查（测试框架尚未落地）
