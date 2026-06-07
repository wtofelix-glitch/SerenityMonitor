#!/usr/bin/env python3
"""Serenity 每日历史数据拉取 — no_agent cron 入口脚本
内部调用 fetch_history.py，适配多标的场景
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fetch_history import main

if __name__ == "__main__":
    print(f"📥 Serenity 每日历史数据拉取 — {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')}")
    main()
