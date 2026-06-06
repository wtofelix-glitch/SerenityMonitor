#!/bin/bash
# Serenity 每日 Rank IC 评估
# 由 cron 调度（16:20 周一至周五），在 fetch_history(16:00) + rescore(16:15) 之后执行
cd /Users/mac/workspace/SerenityMonitor && /Users/mac/.hermes/hermes-agent/.venv/bin/python3 factor_ic.py --json 2>&1
