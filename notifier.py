"""
WeChat 推送模块 — 支持 WxPusher / 企业微信群机器人 / Server酱
韦布收消息用
"""
import os
import json
import logging
from datetime import date
from typing import Optional

# 尝试用 requests，没有就用 urllib
try:
    import requests as req_lib
    HAS_REQUESTS = True
except ImportError:
    import urllib.request as req_lib
    import urllib.error
    HAS_REQUESTS = False

logger = logging.getLogger("Serenity.Notifier")

# ============================================================
# 推送通道配置（从环境变量读取）
# 优先使用 WxPusher，其次是企业微信，最后是 Server酱
# ============================================================

# 在终端设置：
#   export WXPUSHER_TOKEN="AT_xxxx"         # WxPusher App Token
#   export WXPUSHER_UIDS="UID_xxxx"          # 接收者的 UID（多个逗号分隔）
#   export WECOM_WEBHOOK="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"
#   export SERVERCHAN_KEY="SCTxxxx"          # Server酱 SendKey


def _get_env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _post_json(url: str, data: dict) -> dict:
    """通用 POST JSON 并返回解析后的响应"""
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    if HAS_REQUESTS:
        try:
            resp = req_lib.post(url, json=data, timeout=10)
            return resp.json()
        except Exception as e:
            logger.warning(f"requests POST 失败: {e}")
            return {"code": -1, "msg": str(e)}
    else:
        try:
            from urllib.request import Request, urlopen
            req = Request(url, data=body, headers={"Content-Type": "application/json"})
            with urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            logger.warning(f"urllib POST 失败: {e}")
            return {"code": -1, "msg": str(e)}


# ============================================================
# WxPusher 通道
# ============================================================

WXPUSHER_API = "https://wxpusher.zjiecode.com/api/send/message"


def send_via_wxpusher(title: str, content: str,
                      content_type: int = 1,
                      summary: str = "") -> dict:
    """
    通过 WxPusher 推送消息到个人微信

    Parameters
    ----------
    title        : 标题
    content      : 内容（支持 HTML 或 Markdown）
    content_type : 1=文本, 2=HTML, 3=Markdown
    summary      : 摘要（公众号列表页显示）

    Returns
    -------
    dict
    """
    token = _get_env("WXPUSHER_TOKEN")
    uids_str = _get_env("WXPUSHER_UIDS")

    if not token or not uids_str:
        return {"code": -1, "msg": "WxPusher 未配置: 请设置 WXPUSHER_TOKEN 和 WXPUSHER_UIDS"}

    uids = [u.strip() for u in uids_str.split(",") if u.strip()]
    if not uids:
        return {"code": -1, "msg": "WxPusher 未配置接收者 UID"}

    data = {
        "appToken": token,
        "content": content,
        "summary": summary or title[:100],
        "contentType": content_type,
        "uids": uids,
        "url": "",
    }

    result = _post_json(WXPUSHER_API, data)
    if result.get("code") == 1000:
        logger.info(f"✅ WxPusher 推送成功: {title[:30]}")
    else:
        logger.warning(f"⚠️ WxPusher 推送失败: {result.get('msg', '未知错误')}")
    return result


# ============================================================
# 企业微信群机器人通道
# ============================================================

WECOM_API_BASE = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"


def send_via_wecom(content: str, msg_type: str = "markdown") -> dict:
    """
    通过企业微信群机器人推送消息

    Parameters
    ----------
    content  : 消息内容（type=text: 纯文本, type=markdown: Markdown 格式）
    msg_type : text / markdown

    Returns
    -------
    dict
    """
    webhook = _get_env("WECOM_WEBHOOK")
    if not webhook:
        return {"code": -1, "msg": "企业微信未配置: 请设置 WECOM_WEBHOOK"}

    data = {"msgtype": msg_type, msg_type: {"content": content}}
    result = _post_json(webhook, data)
    if result.get("errcode") == 0:
        logger.info(f"✅ 企业微信推送成功")
    else:
        logger.warning(f"⚠️ 企业微信推送失败: {result.get('errmsg', '未知错误')}")
    return result


# ============================================================
# Server酱 通道
# ============================================================

SERVERCHAN_API = "https://sctapi.ftqq.com"


def send_via_serverchan(title: str, content: str) -> dict:
    """
    通过 Server酱 推送消息到微信服务号

    Parameters
    ----------
    title   : 标题（必填）
    content : 内容（支持 Markdown）

    Returns
    -------
    dict
    """
    key = _get_env("SERVERCHAN_KEY")
    if not key:
        return {"code": -1, "msg": "Server酱未配置: 请设置 SERVERCHAN_KEY"}

    url = f"{SERVERCHAN_API}/{key}.send"
    data = {"title": title, "desp": content}

    result = _post_json(url, data)
    if result.get("code") == 0:
        logger.info(f"✅ Server酱推送成功: {title[:30]}")
    else:
        logger.warning(f"⚠️ Server酱推送失败: {result.get('message', '未知错误')}")
    return result


# ============================================================
# 统一推送接口
# ============================================================

def send_message(title: str, content: str, content_type: str = "markdown",
                 summary: str = "") -> list[dict]:
    """
    统一推送接口 — 自动选择可用通道发送

    优先级: WxPusher > 企业微信 > Server酱

    Parameters
    ----------
    title        : 标题
    content      : 内容
    content_type : markdown / text / html
    summary      : 摘要

    Returns
    -------
    list[dict] — 各通道的发送结果
    """
    results = []

    # 1. WxPusher
    ct_map = {"text": 1, "html": 2, "markdown": 3}
    ct = ct_map.get(content_type, 1)
    r = send_via_wxpusher(title, content, content_type=ct, summary=summary)
    results.append(("wxpusher", r))
    if r.get("code") == 1000:
        return results  # WxPusher 成功，不再尝试其他通道

    # 2. 企业微信
    if content_type == "markdown":
        r = send_via_wecom(content, msg_type="markdown")
    else:
        r = send_via_wecom(content, msg_type="text")
    results.append(("wecom", r))
    if r.get("errcode") == 0:
        return results

    # 3. Server酱
    r = send_via_serverchan(title, content)
    results.append(("serverchan", r))

    return results


# ============================================================
# 预制推送模板
# ============================================================

def push_daily_report(report_str: str) -> list[dict]:
    """推送每日收盘简报"""
    today = date.today().isoformat()
    title = f"📊 Serenity 收盘简报 | {today}"
    summary = report_str.split("\n")[0][:100] if report_str else title
    return send_message(title, report_str, content_type="markdown", summary=summary)


def push_signal_summary(signals: list[dict]) -> list[dict]:
    """推送交易信号摘要"""
    today = date.today().isoformat()

    buy_signals = [s for s in signals if s.get("action") in ("STRONG_BUY", "BUY")]
    sell_signals = [s for s in signals if s.get("action") in ("SELL", "STOP_LOSS")]
    caution_signals = [s for s in signals if s.get("action") == "CAUTION_BUY"]

    title = f"📡 Serenity 交易信号 | {today}"
    lines = [f"# 📡 Serenity 交易信号 | {today}", ""]

    if buy_signals:
        lines.append(f"## 🟢 买入信号 ({len(buy_signals)})")
        for s in buy_signals:
            lines.append(f"- **{s['name']}** ({s['code']}) 评分 {s['total_score']}")
            lines.append(f"  - 现价 {s.get('price', 'N/A')} | 买入区 {s.get('buy_zone', 'N/A')}")
            if s.get("suggested_amount", 0) > 0:
                lines.append(f"  - 建议仓位 {s['suggested_amount']:.0f}元 ({s.get('suggested_shares', 0)}股)")
        lines.append("")

    if caution_signals:
        lines.append(f"## 🟡 关注信号 ({len(caution_signals)})")
        for s in caution_signals[:3]:
            lines.append(f"- {s['name']} ({s['code']}) 评分 {s['total_score']}")
        lines.append("")

    if sell_signals:
        lines.append(f"## 🔴 卖出信号 ({len(sell_signals)})")
        for s in sell_signals:
            lines.append(f"- **{s['name']}** ({s['code']}) — {s.get('reason', '信号触发')}")
        lines.append("")

    lines.append("---")
    lines.append(f"共监控 {len(signals)} 只标的")
    content = "\n".join(lines)

    return send_message(title, content, content_type="markdown", summary=f"买入{len(buy_signals)} 关注{len(caution_signals)} 卖出{len(sell_signals)}")


def push_alert(alert: dict) -> list[dict]:
    """推送单条预警（紧急）"""
    level = alert.get("level", "C")
    msg = alert.get("msg", "无内容")
    name = alert.get("name", alert.get("code", "未知"))
    code = alert.get("code", "")

    emoji_map = {"A": "🚨🔴", "B": "⚠️", "C": "💡"}
    emoji = emoji_map.get(level, "📌")
    title = f"{emoji} {name}({code}) 预警"

    content = f"# {title}\n\n{msg}\n\n---\n> SerenityMonitor 自动推送"

    return send_message(title, content, content_type="markdown", summary=f"[{level}] {msg[:50]}")


def push_portfolio_summary(pv: dict, target: dict, trailing: list = None) -> list[dict]:
    """推送投资组合状态（含移动止盈 + 操作建议）"""
    today = date.today().isoformat()
    pnl = pv.get("total_profit_pct", 0)

    lines = [f"# 📊 投资组合 | {today}", ""]
    lines.append(f"**总资产:** {pv['total_value']:.2f} 元")
    lines.append(f"**可用现金:** {pv['cash']:.2f} 元")
    lines.append(f"**总盈亏:** {pnl:+.2f}% ({pv.get('total_profit_amount', 0):+.2f} 元)")
    lines.append(f"**目标进度:** {target.get('progress_pct', 0):.1f}% | "
                 f"时间 {target.get('time_pct', 0):.1f}%")

    if pv.get("positions"):
        lines.append("")
        lines.append("**持仓明细:**")
        for pos in pv["positions"]:
            emoji = "🟢" if pos["profit_pct"] >= 0 else "🔴"
            lines.append(f"- {emoji} {pos['name']}({pos['code']}) "
                         f"盈亏 {pos['profit_pct']:+.2f}% | 仓位 {pos['weight']:.0f}%")
            
            # 移动止盈信息
            if trailing:
                for t in trailing:
                    if t["code"] == pos["code"] and t["profit_pct"] > 3:
                        lines.append(f"  └ 最高浮盈 +{t['peak_profit_pct']:.1f}% | "
                                     f"当前回撤 {abs(t['drawdown_from_peak']):.1f}%")
                        if t["trailing_triggered"]:
                            lines.append(f"  └ 🔴 移动止盈已触发！建议卖出")
                        elif t["exceeds_profit_take1"]:
                            lines.append(f"  └ 🟢 已达一档止盈(+10%)，建议减半（整百股）")

        # 止盈止损汇总
        lines.append("")
        lines.append("**操作建议:**")
        from portfolio import get_portfolio
        pm = get_portfolio()
        advice = pm.get_position_advice([])
        for a in advice:
            icon_map = {"STRONG_HOLD": "🟢", "WEAK_HOLD": "🟡", "SELL_PARTIAL": "🟢",
                        "SELL_TRAILING": "🔴", "STOP_LOSS": "🔴🔴", "CONSIDER_ADD": "🟢+"}
            icon = icon_map.get(a["action"], "⚪")
            if a["reasons"]:
                lines.append(f"- {icon} {a['name']}: {a['reasons'][0]}")

    # 现金效率
    cash_pct = pv['cash'] / pv['total_value'] * 100 if pv['total_value'] > 0 else 0
    if cash_pct > 20:
        extra = int(pv['cash'] - pv['total_value'] * 0.10)
        lines.append(f"")
        lines.append(f"💡 现金比例 {cash_pct:.0f}%，可将 {extra} 元用于加仓或开新仓")

    lines.append("")
    lines.append("---")
    lines.append("> SerenityMonitor")

    content_text = "\n".join(lines)
    title = f"📊 Serenity 组合 {today}"
    return send_message(title, content_text, content_type="markdown", summary=f"盈亏 {pnl:+.1f}% | {len(pv.get('positions', []))}只持仓")


# ============================================================
# 配置指引
# ============================================================

def print_setup_guide():
    """打印推送配置指引"""
    guide = """
📡 Serenity 微信推送配置
═══════════════════════════════════════

推荐使用 WxPusher（个人微信直接收消息）

【方式一：WxPusher（推荐）】
1. 打开 https://wxpusher.zjiecode.com/ 注册登录
2. 创建应用 → 获取 App Token（以 AT_ 开头）
3. 扫描应用二维码关注，获得你的 UID（以 UID_ 开头）
4. 在终端执行:
   export WXPUSHER_TOKEN="你的AppToken"
   export WXPUSHER_UIDS="你的UID"

【方式二：企业微信群机器人】
1. 在企业微信建一个群（个人也可以注册企业微信）
2. 群设置 → 群机器人 → 添加机器人
3. 复制 Webhook URL
4. 在终端执行:
   export WECOM_WEBHOOK="你的WebhookURL"

【方式三：Server酱】
1. 打开 https://sct.ftqq.com/ 用 GitHub 登录
2. 获取 SendKey
3. 在终端执行:
   export SERVERCHAN_KEY="你的SendKey"

设置好后运行 python3 cli.py test-push 测试推送
═══════════════════════════════════════
"""
    print(guide)


# ============================================================
# 测试
# ============================================================

def test_push() -> bool:
    """测试推送是否正常工作"""
    content = (
        "# ✅ Serenity 微信推送测试\n\n"
        "如果你收到这条消息，说明微信推送配置成功了！\n\n"
        "**测试信息:**\n"
        f"- 时间: {date.today().isoformat()}\n"
        "- 通道: 自动选择\n\n"
        "> SerenityMonitor 自动推送"
    )

    results = send_message("✅ Serenity 推送测试", content, content_type="markdown",
                           summary="微信推送测试消息")

    success = False
    for channel, r in results:
        if channel == "wxpusher" and r.get("code") == 1000:
            success = True
            print(f"  ✅ WxPusher 推送成功！请查看微信")
        elif channel == "wecom" and r.get("errcode") == 0:
            success = True
            print(f"  ✅ 企业微信推送成功！请查看企业微信群")
        elif channel == "serverchan" and r.get("code") == 0:
            success = True
            print(f"  ✅ Server酱推送成功！请查看微信服务号")
        else:
            print(f"  ❌ {channel} 推送失败: {r.get('msg', r.get('errmsg', '未知'))}")

    if not success:
        print("\n❌ 所有推送通道均失败。请先配置推送通道:")
        print_setup_guide()

    return success


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "guide":
        print_setup_guide()
    else:
        test_push()
