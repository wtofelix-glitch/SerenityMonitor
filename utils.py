"""通用工具函数 — 各模块共享的辅助函数"""


def host_part(value: str) -> str:
    """从 host header 中提取纯域名/IP（去掉端口和逗号列表）"""
    value = (value or "").split(",", 1)[0].strip().lower()
    if value.startswith("[") and "]" in value:
        return value[1:value.index("]")]
    return value.rsplit(":", 1)[0] if ":" in value and value.count(":") == 1 else value


def is_mainboard(code: str) -> bool:
    """判断股票代码是否为主板标的（非创业板/科创板）"""
    if not code or len(code) != 6:
        return False
    # 主板代码前缀
    MAINBOARD_PREFIXES = ("000", "002", "600", "601", "603", "605")
    return any(code.startswith(p) for p in MAINBOARD_PREFIXES)
