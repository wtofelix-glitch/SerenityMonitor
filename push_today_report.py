#!/usr/bin/env python3
"""推送 Serenity Monitor 每日多因子评分日报到微信"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from notifier import test_push

# ── 拼接日报正文 ──

msg = """📊 Serenity 多因子评分日报 | 6/9 (周二)

━━━ 评分 TOP3 ━━━
🥇 光迅科技  74.5分  🟢🟢 BUY  买入区
🥈 华工科技  68.0分  🟢 CAUTION_BUY  买入区
🥉 海螺水泥  64.6分  🟢 CAUTION_BUY  深度折扣14%

━━━ 持仓信号 ━━━
BUY     光迅科技｜华工科技｜海螺水泥
CAUTION_BUY  剑桥科技｜大秦铁路｜长江电力｜招商银行
HOLD    兆易创新｜中国巨石｜云南锗业｜兴发集团｜工商银行｜亨通光电
WATCH   士兰微

━━━ 权重调整 ━━━
base +1.7%｜factor +1.5%｜momentum -3.3%
信号强化→基本面/因子端，削弱→动量端

━━━ 因子归因关键发现 ━━━
📈 有效：A15波幅(+0.78)｜OBV(+0.53)｜A1日内(+0.40)
📉 衰减预警：残差｜A15波幅｜MACD｜A19动量｜K线形态｜A1日内
→ 短期IC全面低于长期，建议降低权重/暂停

💰 总资金监测中，风控正常。"""

# 用 notifier 推送
test_push()
print("推送完成")
