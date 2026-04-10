# -*- coding: utf-8 -*-
"""诊断站内 checkout 页面结构"""

from playwright.sync_api import sync_playwright
from src.config import load_config
from src.browser import get_browser_ws
from src.utils import setup_logger, human_delay

logger = setup_logger()


def main():
    config = load_config()
    ws_url = get_browser_ws(
        ads_api=config.ads_api,
        user_id=config.task_ads_id,
        api_key=config.ads_api_key,
    )

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(ws_url)
        context = browser.contexts[0]

        # 找到 chatgpt.com/checkout 页面
        page = None
        for pg in context.pages:
            if "checkout" in (pg.url or ""):
                page = pg
                break

        if not page:
            logger.error("未找到 checkout 页面")
            for pg in context.pages:
                logger.info("  - %s", pg.url)
            return

        logger.info("页面 URL: %s", page.url)

        # 等待页面完全加载
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass

        # 打印页面 HTML 概要
        html = page.content()
        logger.info("页面 HTML 长度: %d", len(html))

        # 查找所有 iframe
        frames = page.frames
        logger.info("frames 数量: %d", len(frames))
        for i, f in enumerate(frames):
            logger.info("frame[%d] name=%r url=%s", i, f.name, (f.url or "")[:100])
            inputs = f.query_selector_all("input")
            logger.info("  inputs: %d", len(inputs))
            for inp in inputs[:15]:
                logger.info("    name=%s type=%s placeholder=%s id=%s",
                            inp.get_attribute("name"),
                            inp.get_attribute("type"),
                            inp.get_attribute("placeholder"),
                            inp.get_attribute("id"))
            # 查找 select
            selects = f.query_selector_all("select")
            for sel in selects[:5]:
                logger.info("    select name=%s", sel.get_attribute("name"))
            # 查找 button
            buttons = f.query_selector_all("button")
            for btn in buttons[:5]:
                logger.info("    button type=%s text=%s", btn.get_attribute("type"), btn.text_content()[:50])

        # 查看页面是否有错误或加载提示
        body_text = page.locator("body").text_content()
        if body_text:
            logger.info("页面文本 (前500字): %s", body_text[:500])


if __name__ == "__main__":
    main()
