# AGENTS.md — SerenityMonitor (Codex 规范)

> 继承 `workspace/AGENTS.md` 所有规范。本文档为项目级覆盖。

---

## Project: SerenityMonitor

A股市场多因子评分系统 — CPO对齐/瓶颈/AIcapex/护城河/动量五维评分。

**Tech stack:** Python 3.12+, Flask, SQLite, numpy, Sina Finance API
**Package manager:** pip / uv
**Key commands:** `python3 cli.py status`, `python3 cli.py rescore`, `python3 cli.py monitor`, `python3 app.py`
**Cron:** 每日评分 16:00, 盘中监控每 30 分, 每日收报告 15:30
**UI:** 毛玻璃移动端仪表盘，涨红跌绿
**Ports:** app.py → 8400, dash_dashboard.py → 8050

## Scope Constraints

- 仅限主板标的（000/002/600/601/603/605），无科创板(688)创业板(300/301)。
- 不修改交易/仓位计算逻辑。
- CLI 命令在 `cli.py` 的 commands dict 中注册。
- 新因子添加到 `scorer.py`。
- 数据库操作通过 `db.py` 封装的函数.
