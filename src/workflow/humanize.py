# -*- coding: utf-8 -*-
"""
拟人化交互模块。

提供贝塞尔曲线鼠标移动、非匀速键入等替代 Playwright 原生 `page.click` /
`page.fill` 的方法，用于绕过基于行为指纹的自动化检测。

设计要点：
- 所有随机性通过注入 `rng: random.Random` 控制，便于测试复现；
- `sleep_fn` 可注入以便在单元测试中跳过真实等待；
- 仅依赖 Playwright 的 Page / Mouse / Keyboard 接口，测试使用 Mock 隔离。
"""

from __future__ import annotations

import math
import random
import time
from typing import Any, Callable, List, Optional, Tuple

# 模块级常量：默认的 sleep 函数，方便测试整体替换
_SLEEP = time.sleep

# 按键间隔裁剪区间（毫秒）：避免超常值导致等待时间过长或过短
_MIN_KEY_DELAY_MS = 30.0
_MAX_KEY_DELAY_MS = 500.0

# 一个字“单词”按 5 个字符计（英文打字约定）
_CHARS_PER_WORD = 5

# 邻近按键映射（模拟打字错位）：覆盖常见字母即可
_NEIGHBOR_KEYS = {
    "a": "sqwz",
    "b": "vghn",
    "c": "xdfv",
    "d": "serfcx",
    "e": "wsdr",
    "f": "drtgvc",
    "g": "ftyhbv",
    "h": "gyujnb",
    "i": "ujko",
    "j": "huikmn",
    "k": "jiolm",
    "l": "kop",
    "m": "njk",
    "n": "bhjm",
    "o": "iklp",
    "p": "ol",
    "q": "wa",
    "r": "edft",
    "s": "awedxz",
    "t": "rfgy",
    "u": "yhji",
    "v": "cfgb",
    "w": "qase",
    "x": "zsdc",
    "y": "tghu",
    "z": "asx",
}


def _validate_wpm(wpm_mean: float, wpm_std: float) -> None:
    """WPM 参数合法性校验（必须为正）。"""
    if wpm_mean <= 0:
        raise ValueError("wpm_mean 必须为正数")
    if wpm_std < 0:
        raise ValueError("wpm_std 不能为负数")


def _wpm_to_ms_per_char(wpm: float) -> float:
    """WPM -> 每字符毫秒。"""
    cps = wpm * _CHARS_PER_WORD / 60.0
    return 1000.0 / cps


def bezier_path(
    start: Tuple[float, float],
    end: Tuple[float, float],
    steps: int = 30,
    rng: Optional[random.Random] = None,
) -> List[Tuple[float, float]]:
    """
    生成从 start 到 end 的 3 阶贝塞尔曲线采样点。

    两个控制点围绕起止线段中点，沿法向进行随机扰动，扰动幅度与起止距离相关。

    Args:
        start: 起点 (x, y)
        end:   终点 (x, y)
        steps: 采样数量（含起止点）；<2 时强制 2。
        rng:   可注入随机源；None 时使用默认随机源。

    Returns:
        长度为 steps 的 (x, y) 元组列表，第一项为 start，最后一项为 end。
    """
    if rng is None:
        rng = random.Random()
    if steps < 2:
        steps = 2

    sx, sy = start
    ex, ey = end
    dx = ex - sx
    dy = ey - sy
    distance = math.hypot(dx, dy)

    # 控制点偏移幅度：距离越长、偏移越大；最小保留一个基数避免直线
    jitter_scale = max(distance * 0.25, 10.0)

    # 法向单位向量（距离为 0 时退化为 (0, 0)，后续用 rng 生成独立扰动）
    if distance > 0:
        nx = -dy / distance
        ny = dx / distance
    else:
        nx, ny = 0.0, 0.0

    # 两个控制点：位于起止线段 1/3、2/3 处，并沿法向施加随机偏移
    def _ctrl_point(t: float) -> Tuple[float, float]:
        base_x = sx + dx * t
        base_y = sy + dy * t
        offset = rng.uniform(-jitter_scale, jitter_scale)
        # 再加一点切向轻微扰动，避免曲线过于对称
        tangent_offset = rng.uniform(-jitter_scale * 0.2, jitter_scale * 0.2)
        cx = base_x + nx * offset + (dx / distance * tangent_offset if distance > 0 else 0.0)
        cy = base_y + ny * offset + (dy / distance * tangent_offset if distance > 0 else 0.0)
        return cx, cy

    c1 = _ctrl_point(1.0 / 3.0)
    c2 = _ctrl_point(2.0 / 3.0)

    points: List[Tuple[float, float]] = []
    for i in range(steps):
        t = i / (steps - 1)
        mt = 1.0 - t
        # B(t) = (1-t)^3 P0 + 3(1-t)^2 t C1 + 3(1-t) t^2 C2 + t^3 P1
        x = (
            mt ** 3 * sx
            + 3 * mt ** 2 * t * c1[0]
            + 3 * mt * t ** 2 * c2[0]
            + t ** 3 * ex
        )
        y = (
            mt ** 3 * sy
            + 3 * mt ** 2 * t * c1[1]
            + 3 * mt * t ** 2 * c2[1]
            + t ** 3 * ey
        )
        points.append((x, y))

    # 强制首尾精确对齐，规避浮点误差
    points[0] = (float(sx), float(sy))
    points[-1] = (float(ex), float(ey))
    return points


def sample_keystroke_delays(
    length: int,
    *,
    wpm_mean: float = 180,
    wpm_std: float = 40,
    rng: Optional[random.Random] = None,
) -> List[float]:
    """
    按正态分布采样每个按键的间隔（毫秒），并裁剪到 [30, 500]。

    将 WPM 转为每字符毫秒均值，将 WPM 标准差按相同比例换算为毫秒标准差。

    Args:
        length:   采样个数；<=0 时返回空列表。
        wpm_mean: 目标 WPM 均值。
        wpm_std:  WPM 标准差。
        rng:      可注入随机源。
    """
    _validate_wpm(wpm_mean, wpm_std)
    if length <= 0:
        return []
    if rng is None:
        rng = random.Random()

    mean_ms = _wpm_to_ms_per_char(wpm_mean)

    # std 换算：ms/char 对 WPM 导数，一阶近似取 mean_ms * (wpm_std / wpm_mean)
    if wpm_std == 0:
        std_ms = 0.0
    else:
        std_ms = mean_ms * (wpm_std / wpm_mean)

    delays: List[float] = []
    for _ in range(length):
        if std_ms == 0:
            raw = mean_ms
        else:
            raw = rng.gauss(mean_ms, std_ms)
        clamped = max(_MIN_KEY_DELAY_MS, min(_MAX_KEY_DELAY_MS, raw))
        delays.append(clamped)
    return delays


def _resolve_center(element: Any) -> Tuple[float, float]:
    """从 Playwright Locator 的 bounding_box() 结果解析中心坐标。"""
    box = element.bounding_box()
    if not box:
        raise RuntimeError("元素不可见或未附着：bounding_box() 返回空")
    cx = float(box["x"]) + float(box["width"]) / 2.0
    cy = float(box["y"]) + float(box["height"]) / 2.0
    return cx, cy


def click_humanized(
    page: Any,
    selector: str,
    *,
    steps: int = 30,
    duration_ms: int = 400,
    rng: Optional[random.Random] = None,
    sleep_fn: Optional[Callable[[float], None]] = None,
    start_pos: Optional[Tuple[float, float]] = None,
) -> None:
    """
    沿贝塞尔曲线把鼠标移到元素中心后点击。

    Args:
        page:        Playwright Page 对象（Mock 亦可）
        selector:    CSS 选择器
        steps:       曲线采样数
        duration_ms: 总移动耗时（ms），实际会加 ±25% 抖动
        rng:         可注入随机源
        sleep_fn:    可注入 sleep；默认使用 time.sleep
        start_pos:   起始坐标；None 时默认 (0, 0)
    """
    if rng is None:
        rng = random.Random()
    if sleep_fn is None:
        sleep_fn = _SLEEP

    element = page.locator(selector).first
    end_pos = _resolve_center(element)
    begin_pos = start_pos if start_pos is not None else (0.0, 0.0)

    # 抖动总耗时（±25%）并换算每步间隔
    jitter = rng.uniform(-0.25, 0.25)
    effective_ms = max(0.0, float(duration_ms) * (1.0 + jitter))
    path = bezier_path(begin_pos, end_pos, steps=steps, rng=rng)
    per_step_ms = effective_ms / max(len(path) - 1, 1)
    per_step_sec = per_step_ms / 1000.0

    mouse = page.mouse
    # 先瞬移到起点以锚定鼠标位置（Playwright 无法查询当前坐标）
    mouse.move(begin_pos[0], begin_pos[1])
    for x, y in path[1:]:
        mouse.move(x, y)
        if per_step_sec > 0:
            sleep_fn(per_step_sec)

    mouse.down()
    # 按下与抬起之间保留一个短促停顿，模拟真人点击时序
    hold_sec = max(0.01, rng.uniform(0.02, 0.08))
    sleep_fn(hold_sec)
    mouse.up()


def _pick_typo_char(char: str, rng: random.Random) -> Optional[str]:
    """挑选一个邻近按键用于模拟打错；无邻居时返回 None。"""
    key = char.lower()
    neighbors = _NEIGHBOR_KEYS.get(key)
    if not neighbors:
        return None
    pick = rng.choice(neighbors)
    # 保留原始大小写风格
    return pick.upper() if char.isupper() else pick


def type_humanized(
    page: Any,
    selector: str,
    text: str,
    *,
    wpm_mean: float = 180,
    wpm_std: float = 40,
    typo_rate: float = 0.02,
    rng: Optional[random.Random] = None,
    sleep_fn: Optional[Callable[[float], None]] = None,
) -> None:
    """
    在目标输入框中非匀速键入文本，并按概率模拟“打错立即退格纠正”。

    Args:
        page:      Playwright Page
        selector:  目标输入框的 CSS 选择器
        text:      待输入文本
        wpm_mean:  打字速度均值（WPM）
        wpm_std:   WPM 标准差
        typo_rate: 每个字符触发打错纠正的概率（[0, 1]）
        rng:       可注入随机源
        sleep_fn:  可注入 sleep
    """
    _validate_wpm(wpm_mean, wpm_std)
    if not 0.0 <= typo_rate <= 1.0:
        raise ValueError("typo_rate 必须在 [0, 1] 区间内")
    if rng is None:
        rng = random.Random()
    if sleep_fn is None:
        sleep_fn = _SLEEP

    # 先聚焦输入框
    page.locator(selector).click()

    delays = sample_keystroke_delays(
        len(text), wpm_mean=wpm_mean, wpm_std=wpm_std, rng=rng
    )

    keyboard = page.keyboard
    for idx, ch in enumerate(text):
        # 概率性打错：先打一个邻近键，再退格擦除
        if typo_rate > 0 and rng.random() < typo_rate:
            typo = _pick_typo_char(ch, rng)
            if typo is not None:
                keyboard.type(typo)
                # 打错后的停顿（发现错误的反应时间）
                sleep_fn(max(0.05, rng.uniform(0.08, 0.2)))
                keyboard.press("Backspace")
                sleep_fn(max(0.02, rng.uniform(0.05, 0.12)))

        keyboard.type(ch)
        delay_sec = delays[idx] / 1000.0
        if delay_sec > 0:
            sleep_fn(delay_sec)
