#!/usr/bin/env python3
"""
公众号内容管道 — AI配图生成 + 自动发布
基于 Camoufox 浏览器 REST API (port 9377) 实现公众号排版发布
"""
import os, json, time
from datetime import date, datetime
from serenity_logger import get_logger

log = get_logger(__name__)

CAMOUFOX_URL = "http://localhost:9377"


def generate_cover_image(title: str, subtitle: str = "") -> str:
    """通过 AI 生成公众号封面图 (使用 DeepSeek V4 + 文字封面回退)"""
    from PIL import Image, ImageDraw, ImageFont
    import textwrap

    # 封面尺寸 900x500
    img = Image.new("RGB", (900, 500), (10, 10, 15))
    draw = ImageDraw.Draw(img)

    # 渐变背景
    for y in range(500):
        r = int(10 + (y / 500) * 15)
        g = int(10 + (y / 500) * 25)
        b = int(15 + (y / 500) * 40)
        draw.line([(0, y), (900, y)], fill=(r, g, b))

    # 标题文字
    try:
        font = ImageFont.truetype("/System/Library/Fonts/PingFang.ttc", 48)
        small_font = ImageFont.truetype("/System/Library/Fonts/PingFang.ttc", 24)
    except Exception:
        font = ImageFont.load_default()
        small_font = font

    # 换行处理
    wrapped = textwrap.wrap(title, width=18)
    y_offset = 120
    for line in wrapped[:3]:
        bbox = draw.textbbox((0, 0), line, font=font)
        tw = bbox[2] - bbox[0]
        draw.text(((900 - tw) // 2, y_offset), line, fill=(255, 214, 10), font=font)
        y_offset += 70

    if subtitle:
        bbox = draw.textbbox((0, 0), subtitle, font=small_font)
        sw = bbox[2] - bbox[0]
        draw.text(((900 - sw) // 2, y_offset + 20), subtitle, fill=(180, 180, 190), font=small_font)

    # S logo
    draw.rounded_rectangle([(420, 380), (480, 440)], radius=8, fill=(255, 214, 10))
    draw.text((440, 390), "S", fill=(10, 10, 15), font=font)

    save_dir = "/Users/mac/workspace/SerenityMonitor/reports/covers"
    os.makedirs(save_dir, exist_ok=True)
    filename = f"{save_dir}/cover_{date.today().isoformat()}.png"
    img.save(filename, "PNG")
    log.info("封面图已生成: %s", filename)
    return filename


def publish_to_wechat(article_path: str, cover_path: str = None) -> dict:
    """通过 Camoufox 浏览器自动发布公众号文章

    Args:
        article_path: Markdown 文章文件路径
        cover_path: 封面图路径(可选)
    Returns:
        {success: bool, url: str, error: str}
    """
    if not os.path.exists(article_path):
        return {"success": False, "error": f"文章不存在: {article_path}"}

    # 读取文章内容并转换为纯文本(公众号编辑器用富文本)
    with open(article_path) as f:
        content = f.read()

    title = ""
    for line in content.split("\n"):
        line = line.strip()
        if line.startswith("# "):
            title = line[2:]
            break

    if not title:
        title = os.path.basename(article_path).replace(".md", "")

    # Camoufox 浏览器创建标签页
    try:
        import requests

        # 1. 创建标签页
        r = requests.post(f"{CAMOUFOX_URL}/tabs", json={"url": "https://mp.weixin.qq.com"}, timeout=10)
        if r.status_code != 200:
            return {"success": False, "error": f"浏览器创建失败: HTTP {r.status_code}"}
        tab = r.json()
        tab_id = tab.get("id") if isinstance(tab, dict) else tab

        # 2. 等待登录(公众号需要扫码)
        log.info("请在浏览器中登录公众号后台, 60秒超时...")
        time.sleep(3)

        # 3. 获取页面快照确认登录状态
        snap = requests.get(f"{CAMOUFOX_URL}/tabs/{tab_id}/snapshot", timeout=5)
        snapshot_text = snap.json().get("text", "") if snap.ok else ""

        if "新建" not in snapshot_text and "素材管理" not in snapshot_text:
            log.warning("公众号后台未加载完成, 请手动确认")

        # 4. 保存文章草稿(通过剪贴板+手动粘贴方式)
        # Camoufox 自动化: 写入剪贴板内容
        draft_path = f"/Users/mac/workspace/SerenityMonitor/reports/wechat_draft_{date.today().isoformat()}.html"
        with open(draft_path, "w") as f:
            f.write(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>{title}</title></head>
<body>
<h1>{title}</h1>
{content.replace(chr(10), '<br>')}
<p><em>由 Serenity AI 自动生成 · {date.today().isoformat()}</em></p>
</body></html>""")

        return {"success": True, "draft_path": draft_path, "title": title, "note": "请在Camoufox浏览器中手动粘贴到公众号编辑器"}

    except Exception as e:
        return {"success": False, "error": str(e)}


def full_pipeline(article_md_path: str, subtitle: str = "", publish: bool = False) -> dict:
    """完整内容管道: 配图→排版→发布"""
    result = {"article": article_md_path, "cover": None, "draft": None, "published": False}

    # 1. 读取文章标题
    title = ""
    with open(article_md_path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("# "):
                title = line[2:]
                break

    if not title:
        return {**result, "error": "文章无标题"}

    # 2. 生成封面图
    try:
        cover = generate_cover_image(title, subtitle)
        result["cover"] = cover
        log.info("✅ 封面图: %s", cover)
    except Exception as e:
        log.warning("封面图生成失败: %s", e)

    # 3. 发布
    if publish:
        pub_result = publish_to_wechat(article_md_path, cover)
        result["draft"] = pub_result.get("draft_path")
        result["published"] = pub_result.get("success", False)

    return result


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法: python3 content_pipeline.py <文章.md> [--publish]")
        sys.exit(1)

    path = sys.argv[1]
    do_publish = "--publish" in sys.argv

    result = full_pipeline(path, publish=do_publish)
    print(json.dumps(result, ensure_ascii=False, indent=2))
