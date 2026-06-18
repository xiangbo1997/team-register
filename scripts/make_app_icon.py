# -*- coding: utf-8 -*-
"""生成 Team Register 桌面应用图标（assets/icon.icns）。

设计语义：OpenAI 账号「批量注册」自动化控制台。
  - 圆角方形底（macOS Big Sur+ 风格）+ Indigo 主色渐变（#4F46E5，与控制台 UI 一致）
  - 三个层叠的用户头像卡片 = 「批量账号 / 号池」
  - 右下角绿色对勾徽标 = 「注册成功」

用法：python scripts/make_app_icon.py
产物：
  - assets/icon_1024.png   （主图，留底备查）
  - assets/icon.icns       （PyInstaller spec 直接引用）

依赖：Pillow（图形绘制）+ macOS 自带 iconutil（PNG → icns 打包）。
"""

from __future__ import annotations

import math
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

# 项目设计系统主色（见 reference_stitch_accounts_pool_screen 记忆）
INDIGO = (79, 70, 229)        # #4F46E5 主色
INDIGO_DEEP = (55, 48, 163)   # #3730A3 渐变深端
CARD_WHITE = (255, 255, 255)
CARD_FACE = (224, 231, 255)   # #E0E7FF 头像占位浅靛
SUCCESS = (34, 197, 94)       # #22C55E 成功绿
SUCCESS_RING = (255, 255, 255)

S = 1024  # 主画布边长（icns 最大尺寸）
ASSETS = Path(__file__).resolve().parents[1] / "assets"


def _rounded_rect_mask(size: int, radius: int) -> Image.Image:
    """生成圆角方形的 L 模式遮罩（用于 macOS 图标的 squircle 近似）。"""
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return mask


def _vertical_gradient(size: int, top: tuple, bottom: tuple) -> Image.Image:
    """竖直线性渐变背景。"""
    base = Image.new("RGB", (size, size), top)
    px = base.load()
    for y in range(size):
        t = y / (size - 1)
        r = int(top[0] + (bottom[0] - top[0]) * t)
        g = int(top[1] + (bottom[1] - top[1]) * t)
        b = int(top[2] + (bottom[2] - top[2]) * t)
        for x in range(size):
            px[x, y] = (r, g, b)
    return base


def _draw_account_card(draw: ImageDraw.ImageDraw, cx: int, cy: int, w: int, h: int,
                       radius: int, fill: tuple, alpha: int = 255) -> None:
    """画一张「账号卡片」：白底圆角矩形 + 头像圆 + 两条信息线。"""
    x0, y0 = cx - w // 2, cy - h // 2
    x1, y1 = cx + w // 2, cy + h // 2
    draw.rounded_rectangle([x0, y0, x1, y1], radius=radius,
                           fill=fill + (alpha,))
    # 头像圆（左侧）
    av_r = h // 3
    av_cx = x0 + int(w * 0.26)
    av_cy = cy
    draw.ellipse([av_cx - av_r, av_cy - av_r, av_cx + av_r, av_cy + av_r],
                 fill=CARD_FACE + (alpha,))
    # 信息线（右侧两条）
    line_x0 = av_cx + av_r + int(w * 0.06)
    line_x1 = x1 - int(w * 0.12)
    lh = max(6, h // 12)
    draw.rounded_rectangle([line_x0, cy - lh - lh, line_x1, cy - lh], radius=lh // 2,
                           fill=CARD_FACE + (alpha,))
    draw.rounded_rectangle([line_x0, cy + lh // 2, line_x1 - int(w * 0.12), cy + lh + lh // 2],
                           radius=lh // 2, fill=CARD_FACE + (alpha,))


def build_icon() -> Image.Image:
    """组装 1024×1024 主图。"""
    # 1) 渐变底 + squircle 遮罩
    bg = _vertical_gradient(S, INDIGO, INDIGO_DEEP).convert("RGBA")
    mask = _rounded_rect_mask(S, radius=int(S * 0.225))  # macOS 标准圆角比例
    canvas = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    canvas.paste(bg, (0, 0), mask)

    draw = ImageDraw.Draw(canvas)

    # 2) 三张层叠账号卡片（从后到前，营造「批量」纵深）
    card_w, card_h = int(S * 0.52), int(S * 0.165)
    card_r = int(card_h * 0.28)
    center_x = int(S * 0.46)
    # 后卡（最上、最淡、略小）
    _draw_account_card(draw, center_x + 40, int(S * 0.34), int(card_w * 0.9),
                       int(card_h * 0.9), card_r, CARD_WHITE, alpha=120)
    # 中卡
    _draw_account_card(draw, center_x + 20, int(S * 0.47), int(card_w * 0.96),
                       int(card_h * 0.96), card_r, CARD_WHITE, alpha=185)
    # 前卡（最下、最实、最大）
    _draw_account_card(draw, center_x, int(S * 0.60), card_w, card_h,
                       card_r, CARD_WHITE, alpha=255)

    # 3) 右下角成功对勾徽标（绿底白勾 + 白描边）
    badge_r = int(S * 0.135)
    bx, by = int(S * 0.74), int(S * 0.72)
    ring = badge_r + int(S * 0.018)
    draw.ellipse([bx - ring, by - ring, bx + ring, by + ring], fill=SUCCESS_RING + (255,))
    draw.ellipse([bx - badge_r, by - badge_r, bx + badge_r, by + badge_r], fill=SUCCESS + (255,))
    # 对勾（粗折线）
    cw = int(badge_r * 0.42)
    pts = [
        (bx - int(badge_r * 0.45), by + int(badge_r * 0.02)),
        (bx - int(badge_r * 0.08), by + int(badge_r * 0.38)),
        (bx + int(badge_r * 0.52), by - int(badge_r * 0.40)),
    ]
    draw.line(pts, fill=CARD_WHITE + (255,), width=cw, joint="curve")
    # 折线端点补圆，避免毛刺
    for (px_, py_) in pts:
        draw.ellipse([px_ - cw // 2, py_ - cw // 2, px_ + cw // 2, py_ + cw // 2],
                     fill=CARD_WHITE + (255,))

    return canvas


def export_icns(master: Image.Image) -> Path:
    """用 macOS iconutil 把主图打成 .icns（多分辨率）。"""
    ASSETS.mkdir(parents=True, exist_ok=True)
    png_path = ASSETS / "icon_1024.png"
    master.save(png_path)
    print(f"[icon] 主图已保存 {png_path}")

    if sys.platform != "darwin":
        print("[icon] 非 macOS，跳过 .icns 生成（spec 会回退到无图标）")
        return png_path

    iconset = ASSETS / "icon.iconset"
    if iconset.exists():
        for f in iconset.iterdir():
            f.unlink()
    else:
        iconset.mkdir()

    # macOS iconset 标准命名规格：尺寸 + @1x/@2x
    specs = [
        (16, "icon_16x16.png"), (32, "icon_16x16@2x.png"),
        (32, "icon_32x32.png"), (64, "icon_32x32@2x.png"),
        (128, "icon_128x128.png"), (256, "icon_128x128@2x.png"),
        (256, "icon_256x256.png"), (512, "icon_256x256@2x.png"),
        (512, "icon_512x512.png"), (1024, "icon_512x512@2x.png"),
    ]
    for size, name in specs:
        master.resize((size, size), Image.LANCZOS).save(iconset / name)

    icns_path = ASSETS / "icon.icns"
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns_path)],
                   check=True)
    print(f"[icon] .icns 已生成 {icns_path}")

    # 清理临时 iconset
    for f in iconset.iterdir():
        f.unlink()
    iconset.rmdir()
    return icns_path


if __name__ == "__main__":
    icon = build_icon()
    export_icns(icon)
    print("[icon] 完成。")
