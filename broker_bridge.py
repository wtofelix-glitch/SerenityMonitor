"""
券商对接桥 — 同花顺/华泰等自动下单适配层
目前支持: 手动确认模式 (通过看板/API 执行, 在同花顺手动下单)
未来: THS API / XTP / QMT 直连
"""
import json, os, hmac, hashlib, time
from datetime import datetime
from serenity_logger import get_logger
log = get_logger(__name__)

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "broker_config.json")

def get_config():
    if not os.path.exists(CONFIG_PATH):
        return {"mode":"manual","broker":"ths","account":"","token":"","enabled":False}
    with open(CONFIG_PATH) as f: return json.load(f)

def save_config(cfg):
    with open(CONFIG_PATH,'w') as f: json.dump(cfg,f,ensure_ascii=False,indent=2)

def get_supported_brokers():
    """返回支持的券商列表"""
    return [
        {"id":"ths","name":"同花顺","mode":"manual","desc":"手动模式: API生成指令,手动在同花顺App执行"},
        {"id":"htsc","name":"华泰证券","mode":"qmt","desc":"QMT/Ptrader 直连(需API Key)"},
        {"id":"gf","name":"广发证券","mode":"xtp","desc":"XTP 直连(需API Key)"},
        {"id":"gj","name":"国金证券","mode":"qmt","desc":"QMT 直连(需API Key)"},
    ]

def generate_order(code, action, price, quantity, broker="ths"):
    """生成下单指令 (手动模式: 返回可复制到同花顺的文本)"""
    name = ""
    try:
        from config import STOCK_MAP
        name = STOCK_MAP.get(code,{}).get("name",code)
    except: name = code

    cfg = get_config()
    order = {
        "id": hashlib.md5(f"{code}{action}{price}{quantity}{time.time()}".encode()).hexdigest()[:12],
        "code": code, "name": name, "action": action, "price": price,
        "quantity": quantity, "amount": round(price*quantity, 2),
        "broker": broker, "mode": cfg["mode"],
        "created_at": datetime.now().isoformat(),
        "status": "pending"
    }

    if cfg["mode"] == "manual":
        order["instruction"] = (
            f"📱 同花顺下单指令\n"
            f"━━━━━━━━━━━━━━\n"
            f"标的: {name}({code})\n"
            f"方向: {'买入' if action=='buy' else '卖出'}\n"
            f"价格: ¥{price:.2f}\n"
            f"数量: {quantity}股\n"
            f"金额: ¥{order['amount']:,.0f}\n"
            f"━━━━━━━━━━━━━━\n"
            f"⚠️ 请在手机上确认后执行"
        )
    elif cfg["mode"] == "qmt":
        order["instruction"] = f"QMT_ORDER:{code},{action},{price},{quantity}"
    elif cfg["mode"] == "xtp":
        order["instruction"] = f"XTP_ORDER:{code},{action},{price},{quantity}"

    log.info(f"下单指令生成: {code} {action} {quantity}股@{price}")
    return order

def execute_plan(plan, broker="ths"):
    """批量生成执行计划的下单指令"""
    orders = []
    for s in plan.get("sells",[]):
        orders.append(generate_order(s["code"],"sell",s.get("estimated_proceeds",0)/max(s.get("shares",1),1),s.get("shares",0),broker))
    for b in plan.get("buys",[]):
        orders.append(generate_order(b["code"],"buy",b.get("price",0),b.get("shares",0),broker))
    return orders
