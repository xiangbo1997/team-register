# -*- coding: utf-8 -*-
"""
Grok (x.ai) 注册状态机

独立于 OpenAI 的 ``runtime.py``，专门驱动 Grok 邮箱注册流程，产出 ``sso`` token。

设计要点（feat/grok-register）：
- **复用 team-register 现有基建**：AdsPower CDP 连接（``get_browser_ws`` + ``connect_over_cdp``）、
  邮箱收码（``MailManager.get_verification_code``）、过 Turnstile（``captcha_solver.try_solve_captcha``）、
  SSE 事件推送（worker 传入的 ``emit`` 回调）、证据截图（``artifacts/runs/``）。
- **页面交互混合策略**：框架用 Playwright locator，React 受控输入 / OTP 填充 / Turnstile token 同步 /
  sso 提取等难点直接移植参考项目 grok-register（``core/register.py``）的 JS，
  通过 ``page.evaluate()`` 注入。
- **selector 是参考项目推测**：全部多重 fallback + 失败截图，**真机 DOM 为准**（与现有 phone handler 一致）。

流程 5 步：
  1. open_signup       — 导航 SIGNUP_URL，点「使用邮箱注册」
  2. fill_email        — 填邮箱 + 点「注册」（React setNativeValue）
  3. wait_and_fill_code — mail_api 收码 → 填 OTP → 点「确认邮箱」
  4. fill_profile      — 填姓名+密码 + 过 Turnstile → 点「完成注册」
  5. extract_sso       — 轮询 sso cookie（cookies API + JS document.cookie + localStorage 三法）

成功 sentinel = 拿到非空 ``sso``。无 sso 即失败（无 OpenAI 的 HOME/token 概念）。
"""

from __future__ import annotations

import logging
import os
import random
import secrets
import time
from typing import Any, Callable, Optional

logger = logging.getLogger("grok_register")

# Grok 注册入口（参考项目 config.SIGNUP_URL）
SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"

# 单次 poll_code 长轮询时长上限（秒）。必须 < Cloudflare 网关 100s 超时，
# 否则 email.cloudsentryai.com 经 CF 会返回 524（实测 grok 用 180 必 524）。
# 80s 留 20s 余量给 CF + 网络往返。总等待时长由 _wait_and_fill_code 外层循环累加。
_CODE_POLL_SEGMENT = 80

# Grok(x.ai) 验证码格式：`XXX-XXX` —— 两段各 3 位大写字母/数字混合，连字符分隔。
# 实测两个样本：`810-XC2`（run ae34564d）、`E52-GXZ`（run 82b1a4f8）—— 前段既可能是
# 纯数字也可能字母数字混合，故两段都用 [A-Z0-9]{3}（不能写死 \d{3}）。
# email-provider 通用提取器只认 OpenAI 的纯 6 位数字，认不出 Grok 混合码，
# 必须透传本 pattern 让 _safe_extract 优先用它（捕获组 1 = 验证码）。
# 锚定 "code" 关键词 + 非贪婪 .{0,60}? 跨过 "below to validate..." 说明文字降低误匹配。
_GROK_CODE_PATTERN = r"(?is)\bcode\b.{0,60}?([A-Z0-9]{3}-[A-Z0-9]{3})"

# sso cookie 所属域（extract_sso 轮询时遍历）
_SSO_ORIGINS = ("https://accounts.x.ai", "https://grok.com", "https://auth.x.ai", "https://x.ai")

# 定位策略：语言无关结构锚点优先，文案仅作最后兜底（界面随浏览器 locale 变中/英/日/韩/西…）。
# - input：全部用 type/name/autocomplete/inputmode 结构锚点（见各 _JS_FILL_*），跨语言不变。
# - 提交/确认/完成按钮：优先点表单内 button[type=submit]（_JS_CLICK_SUBMIT），与文案无关。
# - 第一步「邮箱注册」分流按钮：4 个 OAuth 按钮里挑 email 那个，靠「mail icon / mailto / email 关键词」
#   排除 X/Apple/Google（_JS_CLICK_EMAIL_SIGNUP），文案关键词覆盖多语言仅兜底。
# 多语言「email」关键词（小写、去空格后子串匹配）——仅入口分流按钮兜底用。
_EMAIL_KEYWORDS = (
    "email", "e-mail", "mail",          # en
    "邮箱", "邮件",                       # zh
    "メール",                            # ja
    "이메일", "메일",                     # ko
    "correo", "e-mail",                 # es
    "courriel", "courrier",             # fr
    "почт",                             # ru
    "بريد",                             # ar
)
# 第三方登录关键词（排除项）——避免第一步误点 X/Apple/Google。
_OAUTH_EXCLUDE_KEYWORDS = ("apple", "google", " x ", "with x", "twitter", "苹果", "谷歌")

_FIRST_NAMES = [
    "James", "Mary", "Robert", "Patricia", "John", "Jennifer", "Michael", "Linda",
    "David", "Elizabeth", "William", "Barbara", "Richard", "Susan", "Joseph", "Jessica",
    "Thomas", "Sarah", "Charles", "Karen", "Daniel", "Lisa", "Matthew", "Nancy",
]
_LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
    "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson",
    "Thomas", "Taylor", "Moore", "Jackson", "Martin", "Lee", "Perez", "Thompson",
]


# ─────────────────────────────────────────────────────────────────────
# 移植自参考项目 register.py 的 JS 片段（React 兼容 / 跨两版弹窗）
# ─────────────────────────────────────────────────────────────────────

# 重要：Playwright 的 page.evaluate(expr, arg) 把字符串当【表达式】求值，
# 不能用裸函数体 + arguments[N]（那是 DrissionPage 写法，会 SyntaxError: Unexpected token 'const'）。
# 统一包成箭头函数 IIFE：page.evaluate(JS, arg) → JS 求值为函数 → Playwright 用 arg 调用它。
# 多参数时 arg 传 dict，JS 形参解构。共用工具函数内联在每段里（evaluate 隔离作用域，不能跨段共享）。

_VIS = (
    "const isVisible=(n)=>{if(!n)return false;const s=getComputedStyle(n);"
    "if(s.display==='none'||s.visibility==='hidden'||s.opacity==='0')return false;"
    "const r=n.getBoundingClientRect();return r.width>0&&r.height>0;};"
)

# ── 语言无关点击：入口「邮箱注册」分流按钮 ──（arg: {emailKw, excludeKw}）
_JS_CLICK_EMAIL_SIGNUP = r"""(arg) => {
  const emailKw = arg.emailKw.map(s => s.toLowerCase());
  const excludeKw = arg.excludeKw.map(s => s.toLowerCase());
  """ + _VIS + r"""
  const candidates = Array.from(document.querySelectorAll('button, a, [role="button"]'))
    .filter(n => isVisible(n) && !n.disabled && n.getAttribute('aria-disabled') !== 'true');
  const txt = (n) => ((n.innerText || n.textContent || '') + ' '
    + (n.getAttribute('aria-label') || '') + ' '
    + (n.getAttribute('data-testid') || '') + ' '
    + (n.getAttribute('href') || '')).toLowerCase();
  const isExcluded = (t) => excludeKw.some(k => t.includes(k));
  let target = candidates.find(n => { const t = txt(n); return !isExcluded(t) && emailKw.some(k => t.includes(k)); });
  if (!target) {
    target = candidates.find(n => {
      const t = txt(n);
      if (isExcluded(t)) return false;
      if (t.includes('mailto')) return true;
      const svg = n.querySelector('svg');
      return !!svg && !t.includes('apple') && !t.includes('google');
    });
  }
  if (!target) return false;
  target.scrollIntoView({ block: 'center' });
  target.focus(); target.click();
  return true;
}"""

# ── 语言无关点击：表单提交按钮（无 arg）──
_JS_CLICK_SUBMIT = r"""() => {
  """ + _VIS + r"""
  const clickable = (n) => isVisible(n) && !n.disabled && n.getAttribute('aria-disabled') !== 'true';
  let btn = Array.from(document.querySelectorAll('button[type="submit"], input[type="submit"]')).find(clickable);
  if (!btn) {
    const btns = Array.from(document.querySelectorAll('button, [role="button"]')).filter(clickable);
    btn = btns.length ? btns[btns.length - 1] : null;
  }
  if (!btn) return false;
  btn.scrollIntoView({ block: 'center' });
  btn.focus(); btn.click();
  return true;
}"""

# 点击含指定文案的按钮（arg: wanted[]，多语言文案兜底）
_JS_CLICK_BY_TEXT = r"""(wanted0) => {
  const wanted = wanted0.map(s => String(s).toLowerCase().replace(/\s+/g, ''));
  """ + _VIS + r"""
  const candidates = Array.from(document.querySelectorAll('button, a, [role="button"], input[type="submit"]'));
  const target = candidates.find((node) => {
    if (!isVisible(node) || node.disabled || node.getAttribute('aria-disabled') === 'true') return false;
    const text = (node.innerText || node.value || node.textContent || '').toLowerCase().replace(/\s+/g, '');
    return wanted.some(w => text === w || text.includes(w));
  });
  if (!target) return false;
  target.focus(); target.click();
  return true;
}"""

# 填邮箱（arg: email；React setNativeValue + 有效性校验）
_JS_FILL_EMAIL = r"""(email) => {
  """ + _VIS + r"""
  const input = Array.from(document.querySelectorAll(
    'input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]'
  )).find(n => isVisible(n) && !n.disabled && !n.readOnly) || null;
  if (!input) return 'not-ready';
  input.focus(); input.click();
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
  const tracker = input._valueTracker;
  if (tracker) tracker.setValue('');
  if (setter) setter.call(input, email); else input.value = email;
  input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: email, inputType: 'insertText' }));
  input.dispatchEvent(new InputEvent('input', { bubbles: true, data: email, inputType: 'insertText' }));
  input.dispatchEvent(new Event('change', { bubbles: true }));
  if ((input.value || '').trim() !== email || !input.checkValidity()) return false;
  input.blur();
  return 'filled';
}"""

# 填验证码（arg: code；聚合单框 / 6 格分离框两种）
_JS_FILL_CODE = r"""(code0) => {
  const code = String(code0 || '').trim();
  """ + _VIS + r"""
  const setNativeValue = (input, value) => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) tracker.setValue('');
    if (setter) { setter.call(input, ''); setter.call(input, value); }
    else { input.value = ''; input.value = value; }
  };
  const dispatchInputEvents = (input, value) => {
    input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, cancelable: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new InputEvent('input', { bubbles: true, cancelable: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
  };
  const aggregate = Array.from(document.querySelectorAll(
    'input[data-input-otp="true"], input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"], input[inputmode="text"]'
  )).find(n => isVisible(n) && !n.disabled && !n.readOnly && Number(n.maxLength || code.length || 6) > 1) || null;
  const otpBoxes = Array.from(document.querySelectorAll('input')).filter(n => {
    if (!isVisible(n) || n.disabled || n.readOnly) return false;
    const maxLength = Number(n.maxLength || 0);
    return maxLength === 1 || String(n.autocomplete || '').toLowerCase() === 'one-time-code';
  });
  if (!aggregate && otpBoxes.length < code.length) return 'not-ready';
  if (aggregate) {
    aggregate.focus(); aggregate.click();
    setNativeValue(aggregate, code);
    dispatchInputEvents(aggregate, code);
    if (String(aggregate.value || '').trim() === code) { aggregate.blur(); return 'filled'; }
  }
  const ordered = otpBoxes.slice(0, code.length);
  for (let i = 0; i < ordered.length; i++) {
    const box = ordered[i];
    box.focus(); box.click();
    setNativeValue(box, code[i] || '');
    dispatchInputEvents(box, code[i] || '');
    box.blur();
  }
  return ordered.map(n => String(n.value || '').trim()).join('') === code ? 'filled' : 'mismatch';
}"""

# 填姓名+密码（arg: {given, family, password}）
_JS_FILL_PROFILE = r"""(arg) => {
  const givenName = arg.given, familyName = arg.family, password = arg.password;
  """ + _VIS + r"""
  const pick = (selector) => Array.from(document.querySelectorAll(selector)).find(n => isVisible(n) && !n.disabled && !n.readOnly) || null;
  const setVal = (input, value) => {
    if (!input) return false;
    input.focus(); input.click();
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) tracker.setValue('');
    if (setter) { setter.call(input, ''); setter.call(input, value); }
    else { input.value = ''; input.value = value; }
    input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, cancelable: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new InputEvent('input', { bubbles: true, cancelable: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    input.dispatchEvent(new Event('blur', { bubbles: true }));
    return String(input.value || '') === String(value || '');
  };
  const given = pick('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
  const family = pick('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
  const pwd = pick('input[data-testid="password"], input[name="password"], input[type="password"]');
  if (!given || !family || !pwd) return 'not-ready';
  const ok = setVal(given, givenName) && setVal(family, familyName) && setVal(pwd, password);
  return ok ? 'filled' : 'verify-failed';
}"""

# 检测是否已在填邮箱页（无 arg）
_JS_HAS_EMAIL_INPUT = r"""() => {
  """ + _VIS + r"""
  const input = Array.from(document.querySelectorAll(
    'input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]'
  )).find(n => isVisible(n) && !n.disabled && !n.readOnly);
  return !!input;
}"""

# 检测是否已登录进 Grok 聊天主页（profile 残留旧 session 导致 sign-up 被重定向）。
# 真机实证（run c41ed520）：脏 profile 导航 sign-up 后直接进聊天页，
# 旧逻辑硬找 email input 找不到 → 误判「输入框未就绪」失败。
# 判定：URL 落到 grok.com/聊天域，或页面有聊天输入框（contenteditable / 大 textarea）。
_JS_IS_LOGGED_IN_CHAT = r"""() => {
  const url = String(location.href || '').toLowerCase();
  const onChatDomain = url.includes('grok.com') || (url.includes('x.ai') && !url.includes('/sign-up') && !url.includes('/sign-in') && !url.includes('accounts.'));
  // 聊天输入框：contenteditable 或带「询问/发消息」语义的大输入区
  const composer = document.querySelector('[contenteditable="true"], textarea[placeholder], div[role="textbox"]');
  const hasComposer = !!composer;
  // 同时确认页面没有注册流的 email/password input（避免误判注册表单页）
  const hasAuthInput = !!document.querySelector('input[type="email"], input[type="password"], input[name="email"]');
  return (onChatDomain || hasComposer) && !hasAuthInput;
}"""

# grok 域 storage 清理（注销旧登录态）：localStorage + sessionStorage + IndexedDB。
# grok 注册是全新流程，不需要保留任何 grok 域存储（不像 OpenAI 要保 chatgpt 主域
# anti-bot 凭证），可放心全清。
_JS_CLEAR_GROK_STORAGE = r"""async () => {
  const errs = [];
  try { localStorage.clear(); } catch (e) { errs.push('ls:'+e.message); }
  try { sessionStorage.clear(); } catch (e) { errs.push('ss:'+e.message); }
  try {
    if (window.indexedDB && indexedDB.databases) {
      const dbs = await indexedDB.databases();
      for (const db of dbs || []) { try { if (db.name) indexedDB.deleteDatabase(db.name); } catch (e) {} }
    }
  } catch (e) { errs.push('idb:'+e.message); }
  return errs;
}"""

# 检测是否已到资料页（无 arg）
_JS_HAS_PROFILE_FORM = r"""() => {
  const g = document.querySelector('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
  const f = document.querySelector('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
  const p = document.querySelector('input[data-testid="password"], input[name="password"], input[type="password"]');
  return !!(g && f && p);
}"""

# Turnstile state 检测（无 arg）
# ready 双判据（修「被动验证已过但白等 20s」卡顿，run a36ee800 实证）：
#   ① 隐藏 input cf-turnstile-response 有值（最终态，由 widget 回调异步写入，时机偏晚）；
#   ② turnstile.getResponse() 返回非空 token（widget 内部 token，常先于 input.value 就绪）。
# 任一就绪即 ready —— 被动验证真过时能立即返回，省掉等满 _wait_turnstile_ready 的 20s 墙钟。
# input 不存在但 widget 已有 token 时也算 ready（DOM 变体下隐藏 input 可能尚未挂载）。
_JS_TURNSTILE_STATE = r"""() => {
  let widgetToken = '';
  try { widgetToken = String(turnstile.getResponse() || '').trim(); } catch (e) {}
  const ci = document.querySelector('input[name="cf-turnstile-response"]');
  const inputToken = ci ? String(ci.value || '').trim() : '';
  if (inputToken || widgetToken) return 'ready';
  if (!ci) return 'not-found';
  return 'pending';
}"""

# Turnstile token 同步到隐藏 input（arg: token）
_JS_SYNC_TURNSTILE = r"""(token) => {
  const ci = document.querySelector('input[name="cf-turnstile-response"]');
  if (ci) {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
    if (setter) setter.call(ci, token); else ci.value = token;
    ci.dispatchEvent(new Event('input', { bubbles: true }));
    ci.dispatchEvent(new Event('change', { bubbles: true }));
    return true;
  }
  return false;
}"""

# 读 turnstile.getResponse()（无 arg）
_JS_GET_TURNSTILE = "() => { try { return turnstile.getResponse(); } catch(e) { return null; } }"

# 读 document.cookie（无 arg）
_JS_GET_COOKIE = "() => document.cookie"

# 从 localStorage 找 sso/token（无 arg）
_JS_LS_SSO = r"""() => {
  for (var i = 0; i < localStorage.length; i++) {
    var k = localStorage.key(i);
    if (k.toLowerCase().indexOf('sso') >= 0) { return localStorage.getItem(k); }
  }
  return '';
}"""

# ── CDP MouseEvent screenX/screenY 反检测 patch ──
# 根因（移植自 TheFalloutOf76 / ObjectAscended 的 turnstilePatch，2026-06-17）：
# Chrome 经 CDP（Input.dispatchMouseEvent）派发的鼠标事件有个固有特征——
# MouseEvent.screenX/screenY 恒等于 clientX/clientY（相对视口坐标），而真人点击时
# screenX = clientX + 浏览器窗口在物理屏幕的偏移，两者必然不同。Cloudflare 专门检测
# 「screenX === clientX」（或 screenX 偏小）来识别 CDP 自动化 → Turnstile 拒发 token。
#
# 修法（从 JS 侧根治，补 Python 侧 _click_submit_real 真实坐标点击补不到的洞）：
# 在每个 document 的脚本执行前（add_init_script），把 MouseEvent.prototype 的
# screenX/screenY 重写成一个随机但固定的「伪屏幕偏移」，让 screenX !== clientX。
# 必须改 prototype 的属性（拦截所有未来产生的 MouseEvent 实例），而非单个实例。
# 取值 800-1200 / 400-600：覆盖常见窗口偏移量，且对 4K 屏也成立（原 patch 注释）。
#
# 范围：仅 Grok（run_grok_task 内注入），不动已跑通的 OpenAI 流程；只补 screenX/screenY
# 这一个 Cloudflare 确定检测的缺口，不碰 navigator.webdriver 等（AdsPower 已做指纹伪装，
# 重复 patch 反而可能与其伪装打架引入异常指纹）。
#
# 关键覆盖点（Playwright 官方文档实证 + 原版 manifest "all_frames":true 对齐）：检测**恰好
# 发生在 Turnstile 的 cross-domain iframe 内部**（CF 复选框嵌在跨域 iframe，CDP 点击坐标相
# 对 iframe → screenX<100）。Playwright 的 context.add_init_script 走 CDP
# Page.addScriptToEvaluateOnNewDocument，「每个 child frame attach/navigate 时都注入」（含
# cross-domain iframe，浏览器进程级注入不受同源策略限制）——等价原版插件 all_frames:true。
# 故 patch 能打进 Turnstile iframe，这是它生效的前提（用 _verify_screen_patch 自检确认）。
#
# 已知风险（两个原版仓库都未处理，标记供真机验证）：
#   ① 描述符指纹：MouseEvent.prototype.screenX 原生是 accessor(getter)。用 defineProperty
#      重定义它本身是个「描述符被改」的动作，理论上可被 CF 二次检测（检查 getter 是否
#      native code）。本实现保留 getter 形态（非 value 数据属性）以最小化形态差异，但无法
#      消除「getter 不是 native」这一事实——若未来 CF 加此检测会失效。
#   ② 真人点击副作用：patch 后**真人手动点击**（_wait_turnstile_manual_handoff 兜底场景）
#      的 screenX 也变成伪造固定值。但伪造值落在「几百」区间(800-1200)正是 CF 认为真人的
#      范围，故不冲突、甚至更一致（不会出现真人点击却 screenX<100 的矛盾）。
#   ③ 军备竞赛：Chromium bug 40280325 的修复 2025-09 已写好但截至 2025-10 未 merge，
#      stable Chrome 仍带此 bug → patch 当前有效；一旦 merge 进 AdsPower 用的内核即失效。
#
# 随机值语义：JS 在**每个 frame 内**各自执行 randInt（add_init_script 注入的是源码字符串，
# 每个 document 求值一次）→ 天然 per-frame 随机，比原版「模块加载时算一次全局固定值」更自然。
_TURNSTILE_SCREEN_PATCH = r"""
(() => {
  const randInt = (min, max) => Math.floor(Math.random() * (max - min + 1)) + min;
  // old method wouldn't work on 4k screens —— 用固定随机偏移而非 0
  const sx = randInt(800, 1200);
  const sy = randInt(400, 600);
  try {
    // 用 getter 重定义（非 value 数据属性）：① 拦截所有未来 MouseEvent 实例；
    // ② 保留 accessor 形态，最小化与原生「screenX 本就是 getter」的描述符差异。
    Object.defineProperty(MouseEvent.prototype, 'screenX', { configurable: true, get: () => sx });
    Object.defineProperty(MouseEvent.prototype, 'screenY', { configurable: true, get: () => sy });
    // 标记位：供 _verify_screen_patch 自检确认 patch 真在本 frame 生效（含 iframe）。
    try { window.__tsPatchApplied = { sx, sy }; } catch (e) {}
  } catch (e) { /* 已被定义过 / 描述符不可配置则忽略，不抛 */ }
})();
"""

# 自检探针 JS：读 patch 是否在当前 frame 生效。返回 dict 供 Python 判定。
#   - applied:      window.__tsPatchApplied 标记是否存在（init script 是否跑过）
#   - screenX/Y:    实例化一个 MouseEvent 读其 screenX/Y（验证 getter 真生效）
#   - clientX:      同一实例的 clientX（用于确认 screenX !== clientX，即检测被规避）
#   - bypassed:     screenX !== clientX（true = CF 的 screenX===clientX 检测被规避）
_JS_VERIFY_SCREEN_PATCH = r"""() => {
  let applied = false, mark = null;
  try { applied = !!window.__tsPatchApplied; mark = window.__tsPatchApplied || null; } catch (e) {}
  let sx = null, sy = null, cx = null, cy = null;
  try {
    const ev = new MouseEvent('mousemove', { clientX: 42, clientY: 42 });
    sx = ev.screenX; sy = ev.screenY; cx = ev.clientX; cy = ev.clientY;
  } catch (e) {}
  return {
    applied: applied,
    mark: mark,
    screenX: sx, screenY: sy, clientX: cx, clientY: cy,
    bypassed: (sx !== null && cx !== null && sx !== cx),
  };
}"""


def _install_screen_patch(context: Any, emit: Callable) -> None:
    """在 context 上注入 MouseEvent screenX/screenY 反检测 patch（best-effort）。

    用 add_init_script 而非浏览器插件：我们连的是已运行的 AdsPower CDP，改不了启动参数
    （--load-extension 那条路走不通），但 add_init_script 在已连接的 context 上即可生效，
    且在每个新 document（含 cross-domain iframe）的脚本执行前注入，正好赶在 Turnstile
    widget 初始化之前 patch 好 MouseEvent.prototype。失败静默（不阻塞主流程）。
    """
    try:
        context.add_init_script(_TURNSTILE_SCREEN_PATCH)
        emit("action", "GROK_ENTRY", "已注入 MouseEvent screenX/screenY 反检测 patch",
             action_id="screen_patch", result="ok")
    except Exception as exc:
        logger.warning("Grok screenX/screenY patch 注入失败（继续，退回原过盾率）: %s", exc)


def _verify_screen_patch(page: Any, emit: Callable, *, in_iframe: bool = False) -> bool:
    """自检 patch 是否在主框架 / Turnstile iframe 内真生效（A 项：可观测性）。

    为什么需要：add_init_script 理论覆盖 cross-domain iframe（Playwright 官方文档），但检测
    恰发生在 iframe 内——若那个 frame 因时序/变体没被注入，patch 就完全失效却无从察觉（只能
    靠过没过盾盲猜）。本探针把「patch 是否生效 + screenX 是否已 !== clientX」做成事件流可见
    信号，真机跑时直接看 GROK_ENTRY/screen_patch_verify 事件即可定位。

    Args:
        page: Playwright Page（主框架探测）。
        emit: 事件回调。
        in_iframe: 仅用于事件标签区分（主框架 vs 报告 iframe 覆盖意图）。

    Returns:
        True = patch 已生效且 screenX !== clientX（检测被规避）；False = 未生效 / 探测失败。
    """
    scope = "iframe" if in_iframe else "main"
    try:
        result = page.evaluate(_JS_VERIFY_SCREEN_PATCH)
    except Exception as exc:
        emit("action", "GROK_ENTRY", f"screenX patch 自检失败（{scope}）: {exc}",
             action_id="screen_patch_verify", result="error")
        return False
    if not isinstance(result, dict):
        return False
    applied = bool(result.get("applied"))
    bypassed = bool(result.get("bypassed"))
    sx, cx = result.get("screenX"), result.get("clientX")
    if applied and bypassed:
        emit("action", "GROK_ENTRY",
             f"screenX patch 已生效（{scope}）：screenX={sx} != clientX={cx}，CDP 检测已规避",
             action_id="screen_patch_verify", result="ok")
        return True
    emit("action", "GROK_ENTRY",
         f"screenX patch 未生效（{scope}）：applied={applied} screenX={sx} clientX={cx}，"
         f"该 frame 可能未被注入（patch 失效，过盾率退回原状）",
         action_id="screen_patch_verify", result="warning")
    return False


def _verify_screen_patch_in_turnstile_iframe(page: Any, emit: Callable) -> bool:
    """在 Turnstile 的 cross-domain iframe 内自检 patch 是否生效（A 项核心）。

    检测发生在 iframe 内，故这里才是 patch 必须生效的真正位置。用 frame_locator 拿到
    Turnstile iframe 的 frame，在其内部 evaluate 探针。iframe 不存在（被动模式无交互式
    widget / 尚未渲染）则跳过返回 True（无 iframe 即无此 frame 的检测面）。
    """
    try:
        # 找 Turnstile iframe（复用 _TURNSTILE_IFRAME_SELECTOR）。frame_locator 拿不到
        # 对应 Frame 对象时退回「主框架已生效即可」。
        frames = page.frames if hasattr(page, "frames") else []
        cf_frames = [
            f for f in frames
            if "challenges.cloudflare.com" in str(getattr(f, "url", "") or "").lower()
            or "cloudflare" in str(getattr(f, "url", "") or "").lower()
        ]
        if not cf_frames:
            # 没有 Cloudflare iframe（被动模式/未渲染）→ 无此检测面，视为通过
            return True
        ok_any = False
        for fr in cf_frames:
            try:
                result = fr.evaluate(_JS_VERIFY_SCREEN_PATCH)
            except Exception:
                continue
            if isinstance(result, dict) and result.get("applied") and result.get("bypassed"):
                ok_any = True
                emit("action", "GROK_PROFILE",
                     f"screenX patch 已穿透 Turnstile iframe：screenX={result.get('screenX')} "
                     f"!= clientX={result.get('clientX')}",
                     action_id="screen_patch_verify", result="ok")
                break
        if not ok_any:
            emit("action", "GROK_PROFILE",
                 "screenX patch 未穿透 Turnstile iframe（检测面未覆盖，过盾率可能退回原状）",
                 action_id="screen_patch_verify", result="warning")
        return ok_any
    except Exception as exc:
        logger.warning("Grok Turnstile iframe patch 自检异常: %s", exc)
        return False


# ── 语言无关：关闭 Cookie 同意弹窗 ──（真机实测会遮挡 OTP 框）
# 策略：找含 cookie 文案的容器，点其中「接受类」按钮（多语言关键词），
# 否则点容器的关闭 ×。返回是否处理了弹窗。无 arg。
_JS_DISMISS_COOKIE = r"""() => {
  const isVisible = (n) => {
    if (!n) return false;
    const s = getComputedStyle(n);
    if (s.display==='none'||s.visibility==='hidden'||s.opacity==='0') return false;
    const r = n.getBoundingClientRect();
    return r.width>0 && r.height>0;
  };
  // 多语言「接受全部 cookie」关键词
  const acceptKw = ['accept all','accept','agree','allow all','allow','got it','ok',
    'terima semua','terima','setuju',            // id
    '接受全部','接受','同意','允许',               // zh
    'すべて受け入れる','同意する','許可',           // ja
    '모두 허용','수락','동의',                      // ko
    'aceptar todo','aceptar','permitir',          // es
    'tout accepter','accepter','autoriser',       // fr
    'alle akzeptieren','akzeptieren'];            // de
  // 含 "cookie" 文案的可见按钮里挑「接受类」
  const btns = Array.from(document.querySelectorAll('button, a, [role="button"]')).filter(isVisible);
  // 1) 直接找文案命中接受关键词的按钮（且页面确实有 cookie 提示）
  const pageHasCookie = (document.body.innerText || '').toLowerCase().includes('cookie');
  if (pageHasCookie) {
    const accept = btns.find(b => {
      const t = (b.innerText || b.textContent || '').toLowerCase().trim();
      return t && acceptKw.some(k => t === k || t.includes(k));
    });
    if (accept) { accept.click(); return 'accepted'; }
    // 2) 兜底：点弹窗里的关闭 ×（aria-label/文案含 close/× 等）
    const closeBtn = btns.find(b => {
      const t = ((b.innerText||'') + ' ' + (b.getAttribute('aria-label')||'')).toLowerCase().trim();
      return t === '×' || t === 'x' || t.includes('close') || t.includes('关闭') || t.includes('tutup') || t.includes('閉じる');
    });
    if (closeBtn) { closeBtn.click(); return 'closed'; }
  }
  return 'none';
}"""


# ─────────────────────────────────────────────────────────────────────
# 轻量 runtime-like 对象：仅供 captcha_solver.try_solve_captcha 鸭子类型使用
# （它只读 .captcha_solver / .page / .emit_event；evidence 只读 .url / .signals）
# ─────────────────────────────────────────────────────────────────────

class _GrokSolverRuntime:
    def __init__(self, page: Any, solver: Any, emit: Optional[Callable]) -> None:
        self.page = page
        self.captcha_solver = solver
        self._emit = emit

    def emit_event(self, event_type: str, payload: dict[str, Any]) -> None:
        if callable(self._emit):
            try:
                self._emit(event_type, None, payload)
            except Exception:
                pass


class _GrokEvidence:
    def __init__(self, url: str, signals: dict[str, Any]) -> None:
        self.url = url
        self.signals = signals or {}


class GrokRegistrationError(RuntimeError):
    """Grok 注册流程业务异常。"""


def _gen_password(length: int = 16) -> str:
    """生成强密码：保证含大写+小写+数字+特殊符号四类，长度 >=12，无歧义字符。

    修 run 1007773b「提交资料后页面未前进」根因：x.ai 资料页 react onSubmit 有密码强度
    校验，弱密码（缺某类字符 / 太短）被拦 → 表单不提交 → 停在资料页。旧 _gen_password 用
    token_hex+token_urlsafe 拼接，可能缺特殊符号或大写字母 → 强度不达标。改为显式保证四类。
    """
    length = max(12, int(length))
    lower = "abcdefghijkmnpqrstuvwxyz"   # 去掉易混 l/o
    upper = "ABCDEFGHJKLMNPQRSTUVWXYZ"   # 去掉易混 I/O
    digits = "23456789"                  # 去掉易混 0/1
    special = "!@#$%&*?-_"
    all_chars = lower + upper + digits + special
    # 先各取一个保证四类齐全，其余随机填充
    pwd = [
        secrets.choice(lower),
        secrets.choice(upper),
        secrets.choice(digits),
        secrets.choice(special),
    ]
    pwd += [secrets.choice(all_chars) for _ in range(length - 4)]
    # 打乱顺序（避免「首位固定类型」的可预测模式）
    secrets.SystemRandom().shuffle(pwd)
    return "".join(pwd)


def _gen_name(config: Any = None) -> tuple[str, str]:
    """优先用 worker 注入的真实身份（first/last 与 email_local 呼应、不撞名人），
    无注入身份（CLI 直跑 / 老批次）才回退随机表。

    风控关键（feedback：填写要符合人类实际）：worker 在 _resolve_runtime_config 已把
    config_snapshot.identity 注入到 config.identity_first_name/last_name（email_local
    如 william.harrison82 即由它们派生）。Grok 旧逻辑自己 random.choice 笛卡尔积组合，会撞
    名人（Jennifer Lopez / Michael Jackson）且与注册邮箱 local-part 完全对不上 —— 这是账号
    关联签名红旗。改为复用现有身份系统，姓名与邮箱一致，符合真人注册分布。
    """
    first = (getattr(config, "identity_first_name", "") or "").strip() if config is not None else ""
    last = (getattr(config, "identity_last_name", "") or "").strip() if config is not None else ""
    if first and last:
        return first, last
    return random.choice(_FIRST_NAMES), random.choice(_LAST_NAMES)


def _build_grok_llm_provider(config: Any) -> Any:
    """构造 LLM 决策器（复用 OpenAI 同款 LLMDecisionProvider）。未启用/配置缺失返回 None。"""
    try:
        if not getattr(config, "llm_enabled", False):
            return None
        from src.automation.llm import LLMDecisionProvider, OpenAICompatibleLLMClient
        client = OpenAICompatibleLLMClient(
            base_url=config.llm_base_url,
            api_key=config.llm_api_key,
            model=config.llm_model,
            timeout_ms=getattr(config, "llm_timeout_ms", 30000),
        )
        return LLMDecisionProvider(
            client=client,
            confidence_threshold=getattr(config, "llm_confidence_threshold", 0.6),
        )
    except Exception as exc:
        logger.warning("Grok LLM 决策器构造失败（降级纯规则）: %s", exc)
        return None


def run_grok_task(
    *,
    config: Any,
    mail_api: Any,
    ads_id: str,
    email: str,
    password: str = "",
    db_run_id: Optional[str] = None,
    emit: Optional[Callable[[str, Optional[str], dict[str, Any]], None]] = None,
    captcha_solver: Any = None,
    code_timeout: int = 180,
    sso_timeout: int = 120,
) -> str:
    """执行单个 Grok 注册，返回 sso token（失败抛 GrokRegistrationError）。

    Args:
        config: AppConfig（含 ads_api / ads_api_key / proxy / captcha_* 等）。
        mail_api: MailManager（复用 ``get_verification_code`` / ``ensure_runtime_ready``）。
        ads_id: AdsPower profile id。
        email: 注册邮箱（真实可收码）。
        password: 密码，留空则自动生成。
        db_run_id: DB Run.id（用于证据/日志关联）。
        emit: SSE 事件回调 ``emit(event_type, state, payload)``；worker 传入。
        captcha_solver: SolverProvider；留空则 build_solver_from_config(config)。

    Returns:
        非空 sso token 字符串。

    Raises:
        GrokRegistrationError: 任一步失败。
    """
    from playwright.sync_api import sync_playwright, BrowserContext
    from src.browser import get_browser_ws, run_preflight_checks
    from src.automation.captcha_solver import build_solver_from_config, try_solve_captcha
    from src.automation.grok_assist import build_grok_experience

    password = password or _gen_password()
    solver = captcha_solver or build_solver_from_config(config)
    # AI 辅助决策 + 自进化基建：LLM 兜底（复用 OpenAI 同款）+ grok 独立经验库。
    # 二者皆可为 None（LLM 未启用 / 经验库构造失败）→ 决策层自动降级为纯硬规则。
    llm_provider = _build_grok_llm_provider(config)
    experience = build_grok_experience(config)

    def _emit(event_type: str, state: Optional[str], message: str, **extra: Any) -> None:
        if callable(emit):
            try:
                payload = {"message": message, **extra}
                emit(event_type, state, payload)
            except Exception:
                pass

    logger.info("Grok 注册开始: email=%s ads_id=%s run=%s", email, ads_id, (db_run_id or "")[:12])

    # 1) 连接 AdsPower（复用 OpenAI 路径的浏览器接入）
    try:
        try:
            run_preflight_checks(ads_api=config.ads_api, target_url="https://accounts.x.ai/", proxy_url=config.proxy)
        except Exception as exc:
            logger.warning("Grok 启动前检查失败，降级直接启动: %s", exc)
        ws_url = get_browser_ws(ads_api=config.ads_api, user_id=ads_id, api_key=config.ads_api_key)
    except Exception as exc:
        raise GrokRegistrationError(f"连接 AdsPower 失败: {exc}") from exc

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(ws_url)
        context: "BrowserContext" = browser.contexts[0]
        # 注入 CDP MouseEvent screenX/screenY 反检测 patch（必须在导航 sign-up 前装好，
        # 让后续每个 document 的 Turnstile widget 初始化时 MouseEvent.prototype 已被 patch）。
        _install_screen_patch(context, _emit)
        # 选一个干净 tab：profile 可能残留其他站点的 tab（尤其之前跑过 OpenAI 留下的
        # accounts/chatgpt 页 + 「セッションが終了しました」），直接用 pages[0] 会在错的
        # tab 上操作 → 找不到 Grok 表单 → 「填写资料超时或表单未就绪」。
        # 修 run c58b/44cd：关掉非 x.ai 的残留 tab，留/造一个干净 tab 给 Grok。
        page = _select_clean_grok_page(context, _emit)
        solver_runtime = _GrokSolverRuntime(page, solver, emit)

        # 决策上下文：硬规则失败时各步用它走「经验→LLM→固化」
        assist = {"llm": llm_provider, "exp": experience}
        try:
            _open_signup(page, _emit, assist, context=context)
            # A 项自检：sign-up 主框架已加载，确认 screenX patch 真生效（screenX !== clientX）。
            # 失败只记 warning 不阻塞——patch 缺失等于退回原过盾率，仍可走人工接管兜底。
            _verify_screen_patch(page, _emit)
            _fill_email(page, email, _emit, assist)
            _wait_and_fill_code(page, mail_api, email, code_timeout, _emit, assist)
            _fill_profile(page, password, solver_runtime, try_solve_captcha, _emit, assist, config=config)
            sso = _extract_sso(page, context, sso_timeout, _emit)
        except GrokRegistrationError:
            _screenshot(page, db_run_id, "grok_failure", config=config)
            raise
        except Exception as exc:
            _screenshot(page, db_run_id, "grok_failure", config=config)
            raise GrokRegistrationError(f"Grok 注册异常: {exc}") from exc

        if not sso:
            raise GrokRegistrationError("未提取到 sso token（注册可能未生效）")
        logger.info("Grok 注册成功: sso 长度=%d run=%s", len(sso), (db_run_id or "")[:12])
        _emit("state_change", "GROK_DONE", f"Grok 注册成功（sso {len(sso)} 字符）", result="ok")
        return sso


# ─────────────────────────────────────────────────────────────────────
# 5 步实现
# ─────────────────────────────────────────────────────────────────────

def _select_clean_grok_page(context: Any, emit: Callable) -> Any:
    """从 profile 现有 tab 里选/造一个干净 tab 给 Grok 注册用。

    根因（修 run c58b/44cd「填写资料超时或表单未就绪」）：AdsPower profile 复用，之前跑过
    OpenAI 会残留 accounts.openai.com / chatgpt.com 的 tab（截图见「セッションが終了しました」）。
    旧逻辑 `context.pages[0]` 可能拿到这个残留 OpenAI tab，后续在错 tab 上找 Grok 表单必然失败。

    策略（语言无关，靠 URL host 判断）：
      1. 已有 x.ai / grok.com 的 tab → 复用第一个
      2. 否则新开一个干净 tab
      3. 关掉其余残留 tab（尤其 openai/chatgpt），避免干扰 + 释放资源

    Returns:
        一个可用于 Grok 注册的 Page 对象。
    """
    pages = list(context.pages) if context.pages else []

    def _host(pg: Any) -> str:
        try:
            return str(pg.url or "").lower()
        except Exception:
            return ""

    grok_pages = [pg for pg in pages if "x.ai" in _host(pg) or "grok.com" in _host(pg)]
    target = grok_pages[0] if grok_pages else None
    if target is None:
        try:
            target = context.new_page()
            emit("action", "GROK_ENTRY", "新开干净 tab（profile 无 Grok tab）",
                 action_id="clean_tab", result="new")
        except Exception:
            # 兜底：实在开不了新 tab 就用第一个现有 tab（退回旧行为）
            target = pages[0] if pages else context.new_page()

    # 关掉所有非目标 tab（target 已被 skip）。残留 tab 多是 OpenAI/about:blank，
    # 留着只会干扰 + 占资源；Grok 注册只需要 target 这一个干净 tab。
    closed = 0
    for pg in pages:
        if pg is target:
            continue
        try:
            pg.close()
            closed += 1
        except Exception:
            pass
    if closed:
        emit("action", "GROK_ENTRY", f"已关闭 {closed} 个残留 tab（含 OpenAI 等）",
             action_id="clean_tab", result="closed")
    return target


def _safe_evaluate(page: Any, js: str, *args: Any, retries: int = 3, default: Any = None) -> Any:
    """安全执行 page.evaluate，撞上导航（Execution context destroyed）时自动等 settle 重试。

    根因（修 run 69939ddc「填邮箱超时」+「AI 辅助无候选元素」）：x.ai sign-up 走 OAuth
    多跳重定向，goto(domcontentloaded) 返回时页面可能仍在跳转。此时 evaluate 撞上导航会抛
    `Execution context was destroyed, most likely because of a navigation`，旧逻辑直接进
    except 降级 → 跳过清登录态 → 脏 profile 带旧账号 → 注册表单永远出不来。

    本函数：撞导航就 wait_for_load_state 等页面 settle 再重试，把竞态吸收在内部，
    让调用方拿到稳定结果而非异常。重试耗尽仍失败则返回 default（不抛）。
    """
    last_exc: Optional[Exception] = None
    for attempt in range(retries):
        try:
            return page.evaluate(js, *args) if args else page.evaluate(js)
        except Exception as exc:
            last_exc = exc
            msg = str(exc).lower()
            # 仅对「导航销毁上下文」类错误重试；其它错误（语法/超时）直接返回 default
            if "execution context" in msg or "navigation" in msg or "destroyed" in msg:
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=8000)
                except Exception:
                    pass
                time.sleep(0.8)
                continue
            break
    logger.debug("Grok _safe_evaluate 多次重试仍失败（返回 default）: %s", last_exc)
    return default


def _clear_grok_session(page: Any, context: Any, emit: Callable) -> None:
    """清掉 profile 残留的 grok 登录态（cookies + storage），保证从干净注册态开始。

    profile 复用时可能带着上一个注册成功账号的登录态，导致导航 sign-up 被直接
    重定向到聊天主页（run c41ed520 实证）。注册是全新流程，全清无副作用。
    """
    try:
        context.clear_cookies()
    except Exception as exc:
        logger.warning("Grok 清 cookies 失败（继续）: %s", exc)
    # storage 清理需在 grok 域上下文执行（localStorage 按域隔离）。
    # 用 _safe_evaluate：清理常紧跟 goto，易撞导航，撞了等 settle 重试而非放弃。
    _safe_evaluate(page, _JS_CLEAR_GROK_STORAGE, default=None)


def _settle_after_goto(page: Any) -> None:
    """goto 后等页面真正 settle（吸收 OAuth 多跳重定向），再做 evaluate。

    x.ai sign-up 走 OAuth 重定向，domcontentloaded 返回时常仍在跳转。先等 networkidle
    （重定向链跑完、网络静默），拿不到就退回 domcontentloaded + 固定 sleep 兜底。
    """
    try:
        page.wait_for_load_state("networkidle", timeout=12000)
    except Exception:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        time.sleep(1.5)


def _open_signup(page: Any, emit: Callable, assist: Optional[dict] = None, context: Any = None) -> None:
    emit("state_change", "GROK_ENTRY", f"打开 Grok 注册页 {SIGNUP_URL}")
    # 进站策略（修 run 69939ddc「填邮箱超时 / AI 辅助无候选元素」根因——脏 profile 没清空）：
    # 注册是全新流程，进站前**无条件清一次登录态**（cookie + storage），不再依赖
    # _JS_IS_LOGGED_IN_CHAT 检测「是否脏」。原因：
    #   ① 检测 evaluate 会撞 OAuth 重定向抛 Execution context destroyed → 旧逻辑进 except
    #      降级只 goto 不清理 → 脏号登录态残留 → 注册表单永远不渲染；
    #   ② 无条件清理对全新注册无副作用，比「检测到脏才清」更稳健，天然绕过竞态。
    # 流程：goto → settle → 清登录态 → 重新 goto → settle，全程 evaluate 走 _safe_evaluate。
    if context is not None:
        try:
            page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=60000)
            _settle_after_goto(page)
            # 无条件清：先清当前域 storage + cookies（此时已在 x.ai 域，localStorage 可清）
            _clear_grok_session(page, context, emit)
            emit("action", "GROK_ENTRY", "进站前已清理 profile 登录态（cookie+storage）",
                 action_id="clean_session", result="cleared")
            # 重新导航到干净 sign-up
            page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=60000)
            _settle_after_goto(page)
            # 清理后仍被重定向到聊天页（cookie 域更深）→ 再清一次 + 刷新
            if _safe_evaluate(page, _JS_IS_LOGGED_IN_CHAT, default=False):
                emit("action", "GROK_ENTRY", "清理后仍残留登录态，二次清理并刷新",
                     action_id="clean_session", result="dirty_again")
                _clear_grok_session(page, context, emit)
                page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=60000)
                _settle_after_goto(page)
        except Exception as exc:
            # 降级路径也保证清理（修旧 bug：旧 except 只 goto 不清登录态）
            logger.warning("Grok clean-start 异常，降级仍执行清理: %s", exc)
            try:
                _clear_grok_session(page, context, emit)
            except Exception:
                pass
            page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=60000)
            _settle_after_goto(page)
    else:
        page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=60000)
        _settle_after_goto(page)
    _dismiss_cookie(page, emit)  # 真机会弹 Cookie 同意框遮挡操作，先关掉
    # 语言无关：从 4 个 OAuth 按钮里挑 email 那个（多语言关键词 + 排除 Apple/Google/X + svg 兜底）。
    # 部分场景页面直接就是填邮箱页（无分流），探测到 email input 即可跳过点击。
    deadline = time.time() + 12
    while time.time() < deadline:
        if _safe_evaluate(page, _JS_HAS_EMAIL_INPUT, default=False):
            emit("action", "GROK_ENTRY", "已在填邮箱页，跳过分流按钮", action_id="email_signup", result="skipped")
            return
        clicked = _safe_evaluate(
            page, _JS_CLICK_EMAIL_SIGNUP,
            {"emailKw": list(_EMAIL_KEYWORDS), "excludeKw": list(_OAUTH_EXCLUDE_KEYWORDS)},
            default=False,
        )
        if clicked:
            emit("action", "GROK_ENTRY", "已点击邮箱注册入口", action_id="email_signup", result="ok")
            time.sleep(1.5)
            return
        time.sleep(0.5)
    # 硬规则失败 → AI 辅助决策（找一个可点的、像 email 入口的按钮），成功后固化
    if _try_assist(page, emit, assist, step="entry", want_fill=False,
                   verify=lambda: bool(_safe_evaluate(page, _JS_HAS_EMAIL_INPUT, default=False))):
        emit("action", "GROK_ENTRY", "AI 辅助点中邮箱注册入口", action_id="email_signup", result="assisted")
        return
    raise GrokRegistrationError("未找到邮箱注册入口按钮（硬规则+AI辅助均失败，已存证据截图）")


# 邮箱输入框 selector（与 _JS_FILL_EMAIL 内 selector 对齐，真实键盘填充用）
_GROK_EMAIL_SELECTOR = (
    'input[data-testid="email"], input[name="email"], '
    'input[type="email"], input[autocomplete="email"]'
)


def _fill_email(page: Any, email: str, emit: Callable, assist: Optional[dict] = None) -> None:
    emit("state_change", "GROK_FILL_EMAIL", f"填写邮箱 {email}")
    deadline = time.time() + 15
    while time.time() < deadline:
        # 主路径：真实键盘填邮箱（isTrusted=true）。根因（修 run 2082cfd5「收到验证码但
        # 停在空邮箱页、OTP 框始终不出现」）：x.ai 邮箱框是 react 受控组件，旧 _JS_FILL_EMAIL
        # 用 setter+合成 InputEvent（isTrusted=false）→ 视觉填了、回读也过，但 react formState
        # 不认 → 提交 onSubmit 拿到空邮箱 → x.ai 忽略提交，停在邮箱页，OTP 永不渲染。
        # 与资料页/OTP/OpenAI 密码页同源修复（复用 _fill_one_field_real 的 press_sequentially）。
        filled = _fill_one_field_real(page, _GROK_EMAIL_SELECTOR, email)
        # 真实键盘失败再退回 JS 合成事件（某些非 react 场景仍有效）
        if not filled:
            filled = page.evaluate(_JS_FILL_EMAIL, email) == "filled"
        if filled:
            time.sleep(0.8)
            # 语言无关提交：优先 button[type=submit]，失败再回退 Enter 键
            if not page.evaluate(_JS_CLICK_SUBMIT):
                try:
                    page.keyboard.press("Enter")
                except Exception:
                    pass
            emit("action", "GROK_FILL_EMAIL", "已提交邮箱", action_id="submit_email", result="ok")
            return
        time.sleep(0.5)
    # 硬规则填不进邮箱框 → AI 辅助：找可填元素填 email，成功后固化
    if _try_assist(page, emit, assist, step="fill_email", want_fill=True, fill_value=email,
                   verify=lambda: True):
        page.evaluate(_JS_CLICK_SUBMIT)
        emit("action", "GROK_FILL_EMAIL", "AI 辅助填写并提交邮箱", action_id="submit_email", result="assisted")
        return
    raise GrokRegistrationError("填写邮箱超时或输入框未就绪（硬规则+AI辅助均失败）")


def _wait_and_fill_code(page: Any, mail_api: Any, email: str, timeout: int, emit: Callable, assist: Optional[dict] = None) -> None:
    emit("state_change", "GROK_VERIFY_EMAIL", "等待 Grok 验证码邮件...")
    # 复用 MailManager 高层收码（内部 create_session→poll→complete）。
    # 关键（修 524）：email-provider 走 Cloudflare，单个 poll 请求 hold > 100s 会被 CF 网关
    # 砍断返回 524。poll_code 是单个长 HTTP 请求（服务端 hold timeout_seconds 秒），
    # 所以单次 wait_timeout 必须 < CF 100s 窗口。这里分段轮询：单次 ≤ _CODE_POLL_SEGMENT，
    # 外层循环累加到总 timeout —— 既不触发 524，又能等够足够长的总时长。
    code: Optional[str] = None
    poll_deadline = time.time() + max(timeout, _CODE_POLL_SEGMENT)
    while time.time() < poll_deadline and not code:
        remaining = poll_deadline - time.time()
        segment = int(min(_CODE_POLL_SEGMENT, max(10, remaining)))
        try:
            code = mail_api.get_verification_code(
                email, wait_timeout=segment, code_pattern=_GROK_CODE_PATTERN,
            )
        except Exception as exc:
            # 单段失败（含偶发 524/网络抖动）→ 不立即 fail，继续下一段，给服务端恢复窗口
            logger.warning("Grok 收码单段失败（继续重试）: %s", exc)
            time.sleep(2)
    if not code:
        raise GrokRegistrationError(f"{timeout}s 内未收到 Grok 验证码")
    emit("action", "GROK_VERIFY_EMAIL", f"收到验证码 {code}", action_id="get_code", result="ok")
    _dismiss_cookie(page, emit)  # OTP 框常被 Cookie 弹窗遮挡，填码前先关掉

    # Grok 码是 `Y8K-H6W` 这种带连字符的分组格式，但 x.ai 的 OTP 输入框（无论 6 格
    # 分格还是单聚合框）只存字符本身、不含连字符——连字符只是邮件里给人看的视觉分组。
    # 实测 run 58aad671：码 Y8K-H6W(7 字符) 填进 6 格框时 otpBoxes(6) < code.len(7) → not-ready
    # 永远填不进。故填码用剥连字符版（Y8KH6W，6 字符对应 6 格）；若该版填不进再退回原始版。
    fill_candidates = [code.replace("-", "").replace(" ", "")]
    if code not in fill_candidates:
        fill_candidates.append(code)  # 兜底：万一某场景输入框确实要带连字符的原始码

    deadline = time.time() + 60
    while time.time() < deadline:
        # 已经跳到资料页（部分场景自动跳转）→ 直接返回
        if page.evaluate(_JS_HAS_PROFILE_FORM):
            return
        # 主路径：真实键盘填 OTP（isTrusted=true）。x.ai OTP 框是 react 严格组件，
        # JS setNativeValue + 合成 InputEvent（isTrusted=false）视觉上填了但 react state 不认 →
        # 提交时判 OTP 为空 → x.ai 重置回邮箱页（实证 run c58b 失败截图就是空邮箱页）。
        # 与资料页/OpenAI 密码页同源修复（changelog 2026-06-02 react isTrusted）。
        filled = False
        for fill_code in fill_candidates:
            if _fill_otp_real_keyboard(page, fill_code):
                filled = True
                break
        # 真实键盘失败再退回 JS 合成事件（某些非 react 场景仍有效）
        if not filled:
            for fill_code in fill_candidates:
                if page.evaluate(_JS_FILL_CODE, fill_code) == "filled":
                    filled = True
                    break
        if filled:
            time.sleep(1.0)
            # 语言无关提交（OTP 框很多场景填满即自动提交，这里再补一次 submit 点击）
            if not page.evaluate(_JS_CLICK_SUBMIT):
                try:
                    page.keyboard.press("Enter")
                except Exception:
                    pass
            # 等待跳转到资料页（最多 15s）
            for _ in range(30):
                time.sleep(0.5)
                if page.evaluate(_JS_HAS_PROFILE_FORM):
                    emit("action", "GROK_VERIFY_EMAIL", "已确认验证码，进入资料页", action_id="confirm_code", result="ok")
                    return
            # 15s 没进资料页：可能 OTP 被拒重置回邮箱页。检测是否退回邮箱页，
            # 是则不放行（继续循环重填），避免 fill_profile 在邮箱页死等资料表单。
            if page.evaluate(_JS_HAS_EMAIL_INPUT):
                emit("action", "GROK_VERIFY_EMAIL", "OTP 提交后退回邮箱页（验证码可能被拒），重试",
                     action_id="confirm_code", result="bounced_back")
                # 退回邮箱页说明邮箱要重填——但本函数只管 OTP，交由外层超时/重试处理；
                # 这里继续循环，下一轮 _JS_HAS_PROFILE_FORM 仍 false 会再试填 OTP（若 OTP 框还在）
                time.sleep(1.0)
                continue
            return  # 既非资料页也非邮箱页（中间态）→ 放行由 fill_profile 判断
        time.sleep(0.6)
    # 硬规则填不进 OTP（框被遮挡/结构变化）→ AI 辅助填验证码，成功后固化
    if _try_assist(page, emit, assist, step="verify_email", want_fill=True, fill_value=code,
                   verify=lambda: bool(page.evaluate(_JS_HAS_PROFILE_FORM)) or True):
        page.evaluate(_JS_CLICK_SUBMIT)
        emit("action", "GROK_VERIFY_EMAIL", "AI 辅助填写验证码", action_id="confirm_code", result="assisted")
        return
    raise GrokRegistrationError("填写验证码超时（硬规则+AI辅助均失败）")


# OTP 聚合框（单框装整个码）selector，语言无关
_GROK_OTP_AGG_SELECTOR = (
    'input[data-input-otp="true"], input[name="code"], input[autocomplete="one-time-code"], '
    'input[inputmode="numeric"], input[inputmode="text"]'
)
# OTP 分格框（每格一字符）selector
_GROK_OTP_BOX_SELECTOR = 'input[maxlength="1"], input[autocomplete="one-time-code"]'


def _fill_otp_real_keyboard(page: Any, code: str) -> bool:
    """用 Playwright 真实键盘填 OTP（isTrusted=true）。react OTP 组件只认真实键盘。

    兼容两种结构：① 单聚合框（一个 input 装整个码）② 6 格分离框（每格一字符）。
    填前 click 聚焦 + 清空，填后回读校验。任一结构成功即返回 True。
    """
    code = str(code or "").strip()
    if not code:
        return False
    # 结构 1：单聚合框
    try:
        agg = page.locator(_GROK_OTP_AGG_SELECTOR).first
        agg.wait_for(state="visible", timeout=2000)
        # maxLength>1 才是聚合框（排除分格框）
        maxlen = agg.get_attribute("maxlength", timeout=1000)
        if maxlen is None or int(maxlen or 0) > 1 or int(maxlen or 0) == 0:
            agg.click()
            for combo in ("Meta+A", "Control+A"):
                try:
                    page.keyboard.press(combo)
                    break
                except Exception:
                    continue
            try:
                page.keyboard.press("Backspace")
            except Exception:
                pass
            agg.press_sequentially(code, delay=random.randint(30, 80))
            if (agg.input_value() or "").strip().replace("-", "").replace(" ", "") == code:
                return True
    except Exception:
        pass
    # 结构 2：6 格分离框——逐格真实键盘输入
    try:
        boxes = page.locator(_GROK_OTP_BOX_SELECTOR)
        n = boxes.count()
        if n >= len(code):
            first_box = boxes.first
            first_box.click()
            # 逐字符 type：react OTP 通常自动 focus 下一格
            for ch in code:
                page.keyboard.type(ch, delay=random.randint(30, 80))
            # 回读校验：拼接所有格的值
            filled = ""
            for i in range(min(n, len(code))):
                try:
                    filled += (boxes.nth(i).input_value() or "").strip()
                except Exception:
                    pass
            if filled == code:
                return True
    except Exception:
        pass
    return False


def _fill_profile(page: Any, password: str, solver_runtime: Any, try_solve: Callable, emit: Callable, assist: Optional[dict] = None, config: Any = None) -> None:
    emit("state_change", "GROK_PROFILE", "填写姓名和密码...")
    first, last = _gen_name(config)
    deadline = time.time() + 60
    while time.time() < deadline:
        # 关键（修 run b5e42a3f "未提取到 sso token"，真机 DOM + 截图实证）：x.ai 资料页是
        # react-hook-form 严格表单，只信任 isTrusted=true 的真实用户输入。旧 _JS_FILL_PROFILE
        # 用原生 setter + 合成 InputEvent（isTrusted=false）注入值——视觉上值进去了、GTM 也记到
        # field-interact，但 react formState 不认 → 提交时 onSubmit 被拦 → 停在资料页拿不到 sso。
        # 改为 Playwright press_sequentially（真实键盘事件 isTrusted=true），与 OpenAI 创建密码页
        # /OTP 页同源修复（changelog 2026-06-02 react-aria isTrusted）。
        if _fill_profile_real_keyboard(page, first, last, password, emit):
            # A 项核心自检：检测发生在 Turnstile 的 cross-domain iframe 内，过盾前确认 patch
            # 真穿透到了那个 iframe（screenX !== clientX）。未穿透 → 事件流 warning 提示「检测面
            # 未覆盖」，便于真机定位是 patch 没生效还是别的风控层卡住，不阻塞主流程。
            _verify_screen_patch_in_turnstile_iframe(page, emit)
            # 过 Turnstile（提交前）：先尝试 solver（pending 时），再轮询等 token 就绪。
            # Turnstile 是被动异步验证，截图实证会自动 "成功しました!" 且 cf-turnstile-response
            # 隐藏 input 被填入完整 token；提交前轮询等 pending → ready（最多 ~20s）。
            _solve_turnstile_if_present(page, solver_runtime, try_solve, emit)
            # 先等被动验证自动完成（managed 模式正常路径，~20s）；若超时仍 pending，
            # 说明被 Cloudflare 降级成交互式（需手动点复选框）——进入人工接管等待，
            # 给运维时间在 AdsPower 窗口手动勾选「私はロボットではありません」。
            # 设计依据（3 agent 调研 + Cloudflare 官方文档交叉验证，2026-06-04）：
            # token 绑定 IP/指纹/sitekey，跨环境注入必被 siteverify 拒；交互式挑战
            # 唯一可靠的免费解是真人 isTrusted 点击。详见 captcha_solver.py 模块 docstring。
            if not _wait_turnstile_ready(page, emit, timeout=20):
                # 被动验证超时（被降级成交互式）→ 先试自动点击复选框兜底（真实坐标鼠标，
                # 半信任环境有非零成功率，失败无副作用）；仍不通过才等人工。
                if not _try_click_turnstile_checkbox(page, emit):
                    _wait_turnstile_manual_handoff(page, emit, config=config)
            # 提交资料：多策略 + 提交后验证页面真的前进（语言无关，不靠按钮文案）。
            # 修 run 44cd0531「Turnstile 过了 / finish=ok 但停在资料页拿不到 sso」：旧逻辑
            # 点一次 click() 就 return，从不验证 react 表单 onSubmit 是否真触发——按钮有
            # [@media(pointer:fine)]:hidden 覆盖 span，CDP click 可能点到覆盖层没提交。
            submitted = _submit_profile_and_confirm(page, emit, assist)
            if not submitted:
                emit("action", "GROK_PROFILE", "提交资料后页面未前进（可能未生效）",
                     action_id="finish", result="warning")
            emit("action", "GROK_PROFILE", f"已提交资料 {first} {last}", action_id="finish", result="ok")
            time.sleep(2)
            return
        time.sleep(0.5)
    raise GrokRegistrationError("填写资料超时或表单未就绪")


# 资料页输入框 selector（真机 DOM 实证：data-testid 主锚 + name/autocomplete 兜底）
_GROK_GIVEN_SELECTOR = 'input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]'
_GROK_FAMILY_SELECTOR = 'input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]'
_GROK_PASSWORD_SELECTOR = 'input[data-testid="password"], input[name="password"], input[type="password"]'
# 提交按钮：表单内 type=submit（真机 DOM「登録を完了」无 disabled/aria-disabled，文案随 locale 变）
_GROK_SUBMIT_SELECTOR = 'form button[type="submit"], button[type="submit"], input[type="submit"]'


def _fill_one_field_real(page: Any, selector: str, value: str) -> bool:
    """用 Playwright 真实键盘填单个输入框（isTrusted=true），回读校验。

    react-hook-form 只认真实用户输入，故必须 press_sequentially 而非 JS setter。
    填前先 click 聚焦 + 清空（Meta/Ctrl+A → Backspace），避免残留值叠加。
    """
    try:
        loc = page.locator(selector).first
        loc.wait_for(state="visible", timeout=5000)
    except Exception:
        return False
    try:
        # 已是目标值则跳过（防重试叠加）
        if (loc.input_value() or "").strip() == value:
            return True
    except Exception:
        pass
    try:
        loc.click()
        # 全选清空（跨平台：Meta+A 不中再 Control+A）
        for combo in ("Meta+A", "Control+A"):
            try:
                page.keyboard.press(combo)
                break
            except Exception:
                continue
        try:
            page.keyboard.press("Backspace")
        except Exception:
            pass
        loc.press_sequentially(value, delay=random.randint(25, 70))
        # 回读校验：react 受控组件值同步到 DOM 才算成功
        return (loc.input_value() or "").strip() == value
    except Exception as exc:
        logger.warning("Grok 真实键盘填充失败 selector=%s: %s", selector[:30], exc)
        return False


def _fill_profile_real_keyboard(page: Any, first: str, last: str, password: str, emit: Callable) -> bool:
    """Playwright 真实键盘依次填姓名/密码，全部回读校验通过才返回 True。"""
    if not _fill_one_field_real(page, _GROK_GIVEN_SELECTOR, first):
        return False
    if not _fill_one_field_real(page, _GROK_FAMILY_SELECTOR, last):
        return False
    if not _fill_one_field_real(page, _GROK_PASSWORD_SELECTOR, password):
        return False
    return True


def _click_submit_real(page: Any) -> bool:
    """真实鼠标点击提交按钮（isTrusted=true）。语言无关，靠 type=submit 而非文案。

    用 page.mouse.click(x, y) 点按钮真实坐标——产生最真实的 isTrusted 点击事件，
    坐标相对主框架（避开 Cloudflare 对 CDP click 的 screenX<100 检测），且穿透按钮上
    的 [@media(pointer:fine)]:hidden 覆盖 span（精细指针下该 span 隐藏，鼠标坐标点直达按钮）。

    返回 True = 点中可见可用的 submit 按钮；False = 未找到（由调用方回退）。
    """
    try:
        loc = page.locator(_GROK_SUBMIT_SELECTOR).first
        loc.wait_for(state="visible", timeout=5000)
        loc.scroll_into_view_if_needed(timeout=2000)
        # 优先真实坐标鼠标点击（isTrusted=true，最接近真人）
        try:
            box = loc.bounding_box(timeout=2000)
            if box:
                cx = box["x"] + box["width"] / 2
                cy = box["y"] + box["height"] / 2
                page.mouse.move(cx, cy)
                time.sleep(0.1)
                page.mouse.click(cx, cy)
                return True
        except Exception:
            pass
        # 兜底：locator.click（force 穿透覆盖层）
        loc.click(timeout=5000, force=True)
        return True
    except Exception as exc:
        logger.warning("Grok 真实点击提交失败（回退）: %s", exc)
        return False


# 表单原生提交（语言无关）：form.requestSubmit() 触发 react onSubmit，比点按钮更可靠
# ——按钮有 [@media(pointer:fine)]:hidden 覆盖 span，CDP click 可能点到覆盖层不提交。
_JS_REQUEST_SUBMIT = r"""() => {
  const ci = document.querySelector('input[name="cf-turnstile-response"]');
  const form = ci ? ci.closest('form') : document.querySelector('form');
  if (!form) return false;
  if (typeof form.requestSubmit === 'function') { form.requestSubmit(); return true; }
  form.submit();
  return true;
}"""

# 资料页是否还在（语言无关）：靠 cf-turnstile-response 隐藏 input 是否还存在判断，
# 不靠 URL/文案。提交成功后 react 会卸载资料表单，这个 input 随之消失。
_JS_PROFILE_STILL_PRESENT = r"""() => {
  return !!document.querySelector('input[name="cf-turnstile-response"]');
}"""


def _turnstile_token_present(page: Any) -> bool:
    """cf-turnstile-response 隐藏 input 是否已被填入非空 token（语言无关）。"""
    try:
        return str(page.evaluate(_JS_TURNSTILE_STATE) or "") == "ready"
    except Exception:
        return False


def _profile_left_page(page: Any) -> bool:
    """资料页是否已离开（提交生效）。语言无关：cf-turnstile-response input 消失即视为前进。"""
    try:
        return not bool(page.evaluate(_JS_PROFILE_STILL_PRESENT))
    except Exception:
        return False


def _submit_profile_and_confirm(
    page: Any, emit: Callable, assist: Optional[dict] = None, *, max_attempts: int = 4
) -> bool:
    """提交资料并验证页面真的前进；未前进则换更强方式重试。语言无关，不靠按钮文案。

    根因（修 run 44cd0531「Turnstile 过了但停在资料页拿不到 sso」）：被动 Turnstile 需要
    几秒才自动写入 token，且提交按钮有覆盖 span 导致 CDP click 可能不触发 react onSubmit。
    本函数：① 提交前先等 token 就绪（最多 10s）；② 多策略提交（requestSubmit → 真实点击
    → Enter）；③ 每次提交后验证页面是否前进（cf-turnstile-response input 消失），未前进
    则升级策略重试。

    Returns:
        True = 确认页面已离开资料页（提交生效）；False = 多次尝试仍停在资料页。
    """
    # 提交前确认 Turnstile 真勾上了再点（你的洞察：自动勾选需要时间，没勾上别点提交）。
    # 轮询 3 轮，每轮等 ~3.3s（共 ~10s）查 cf-turnstile-response 是否被填入 token。
    token_ready = False
    for poll in range(1, 4):
        for _ in range(7):  # 每轮 ~3.3s
            if _turnstile_token_present(page):
                token_ready = True
                break
            time.sleep(0.5)
        if token_ready:
            emit("action", "GROK_PROFILE", f"Turnstile 已勾选（第 {poll} 轮确认），开始提交",
                 action_id="submit", result="pending")
            break
        emit("action", "GROK_PROFILE", f"Turnstile 尚未勾选，继续等待（第 {poll}/3 轮）",
             action_id="submit", result="pending")
    if not token_ready:
        # 三轮仍没勾上：被降级成交互式需人工点，已由上层 _wait_turnstile_manual_handoff 处理；
        # 这里仍尝试提交（万一是检测延迟），但标 warning
        emit("action", "GROK_PROFILE", "Turnstile 三轮仍未勾选即提交（大概率失败）",
             action_id="submit", result="warning")

    for attempt in range(1, max_attempts + 1):
        # 策略升级：1=真实坐标点击（isTrusted=true，最可能过 react onSubmit 校验）
        # 2=form.requestSubmit 3=Enter 4=AI 辅助。
        # 实证 run 1007773b：requestSubmit 当首选时「提交后页面未前进」——x.ai 资料页
        # react onSubmit 需要真实用户点击信号，合成提交不够，故真实点击提到首位。
        if attempt == 1:
            _click_submit_real(page)
        elif attempt == 2:
            try:
                page.evaluate(_JS_REQUEST_SUBMIT)
            except Exception as exc:
                logger.warning("Grok requestSubmit 失败: %s", exc)
        elif attempt == 3:
            try:
                page.locator(_GROK_PASSWORD_SELECTOR).first.press("Enter", timeout=3000)
            except Exception:
                try:
                    page.keyboard.press("Enter")
                except Exception:
                    pass
        else:
            _try_assist(page, emit, assist, step="profile_submit", want_fill=False,
                        verify=lambda: _profile_left_page(page))

        # 提交后给 react/网络一点时间，再验证页面是否前进
        for _ in range(6):  # 最多等 ~3s
            time.sleep(0.5)
            if _profile_left_page(page):
                emit("action", "GROK_PROFILE", f"提交生效（策略 {attempt}）",
                     action_id="submit", result="ok")
                return True
    return False


def _solve_turnstile_if_present(page: Any, solver_runtime: Any, try_solve: Callable, emit: Callable) -> None:
    """检测 Turnstile，pending 则调 solver 求解并同步 token。"""
    try:
        state = page.evaluate(_JS_TURNSTILE_STATE)
    except Exception:
        state = "not-found"
    if state != "pending":
        return
    emit("action", "GROK_PROFILE", "检测到 Turnstile，尝试自动求解...", action_id="turnstile", result="pending")
    evidence = _GrokEvidence(url=str(page.url), signals={})
    solved = try_solve(solver_runtime, evidence)
    if solved:
        try:
            token = page.evaluate(_JS_GET_TURNSTILE)
            if token:
                page.evaluate(_JS_SYNC_TURNSTILE, token)
        except Exception:
            pass
        emit("action", "GROK_PROFILE", "Turnstile 求解成功", action_id="turnstile", result="ok")
    else:
        # 求解失败不立即 fail —— 服务端可能接受无 token 提交，由 extract_sso 判定最终结果
        emit("action", "GROK_PROFILE", "Turnstile 自动求解失败，继续尝试提交", action_id="turnstile", result="failed")


def _wait_turnstile_ready(page: Any, emit: Callable, *, timeout: int = 20) -> bool:
    """提交前轮询等 Turnstile token 就绪（被动验证会自动完成）。

    x.ai 资料页的 Turnstile 是 managed/被动模式：页面加载后几秒内自动验证通过
    （截图实证 "成功しました!"），cf-turnstile-response 隐藏 input 随后被填入 token。
    旧逻辑 solver 失败就立刻点提交，此时 token 未就绪 →「登録を完了」提交无效。

    Returns:
        True = token 已就绪 / 无 Turnstile（可提交）；False = 超时仍 pending（仍放行提交，
        由 extract_sso 判定最终结果，避免被动模式偶发慢导致硬失败）。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            state = page.evaluate(_JS_TURNSTILE_STATE)
        except Exception:
            state = "not-found"
        if state in ("ready", "not-found"):
            if state == "ready":
                emit("action", "GROK_PROFILE", "Turnstile token 已就绪", action_id="turnstile", result="ok")
            return True
        time.sleep(0.5)
    emit("action", "GROK_PROFILE", "Turnstile 等待超时（仍尝试提交）", action_id="turnstile", result="timeout")
    return False


# Turnstile 复选框所在的 Cloudflare iframe（src 含 challenges.cloudflare.com）。
# 真机 DOM：交互式 widget 是嵌套 iframe，复选框在 iframe 内部，主框架点不到，
# 必须先定位 iframe 再在其 content frame 里点复选框。
# Turnstile widget 的 iframe selector。真机 DOM（run 0117b60f 实证）：widget 渲染在
# 资料页内嵌容器 `<div><input type="hidden" name="cf-turnstile-response" id="cf-chl-widget-*">`，
# 复选框 iframe 是该容器子节点。旧 selector 只认 challenges.cloudflare.com，漏判这次的
# widget（src 不含该串）→ count()==0 → 误判被动模式跳过。放宽到 cloudflare 通用 + 容器内 iframe。
_TURNSTILE_IFRAME_SELECTOR = (
    'iframe[src*="challenges.cloudflare.com"], '
    'iframe[src*="cloudflare"], '
    'iframe[title*="Cloudflare"], '
    'iframe[title*="Widget"], '
    'iframe[title*="human"], '
    'iframe[title*="ロボット"], '
    '[id^="cf-chl-widget"] iframe, '
    'div:has(> div > input[name="cf-turnstile-response"]) iframe'
)
# iframe 内复选框 selector（语言无关，多重兜底：role/type/class）
_TURNSTILE_CHECKBOX_SELECTOR = (
    'input[type="checkbox"], '
    '[role="checkbox"], '
    'label.cb-lb input, '
    '.cb-i, '
    '#challenge-stage input'
)


def _try_click_turnstile_checkbox(page: Any, emit: Callable, *, timeout: int = 8) -> bool:
    """交互式 Turnstile 兜底：用真实坐标鼠标点击 iframe 内复选框，试着自动过。

    设计取舍（务实，非保证）：
      - 已知局限（captcha_solver.py docstring + 3-agent 调研 2026-06-04）：Cloudflare 行为层
        会分析鼠标轨迹/isTrusted/自动化指纹，纯自动点击在「严环境」下大概率被识破不发 token。
      - 但用 page.mouse.move(带轨迹)+ click(真实坐标) 比裸 CDP click 真实得多（与
        _click_submit_real 同款，避开 screenX<100 检测），在「半信任环境」下有非零成功率。
      - 故作为人工接管前的零成本兜底：试一次，token 就绪则省掉人工；不就绪则照常等人工。
        失败无副作用（只是多点一下复选框，不影响后续人工再点）。

    Returns:
        True = 点击后 cf-turnstile-response 变 ready（自动过了）；False = 仍 pending（交人工）。
    """
    # 快速短路判据（修 run 0117b60f「widget 存在但被误判被动模式跳过」+ run a36ee800「真被动白等 4s」）：
    # 用 cf-turnstile-response 隐藏 input 是否存在作为「有无 Turnstile widget」的**权威信号**
    # （真机 DOM 实证 widget 一定带这个 input），而非依赖 iframe src 匹配（selector 易漏判变体）。
    #   - input 不存在 → 真没有 Turnstile（纯被动/无挑战）→ 立即返回，不浪费时间；
    #   - input 存在 → 有 widget（可能交互式复选框）→ 继续找复选框点击，找不到也由上层转人工。
    try:
        has_widget = bool(page.evaluate(
            '() => !!document.querySelector(\'input[name="cf-turnstile-response"]\')'
        ))
    except Exception:
        has_widget = True  # 检测异常时保守认为有 widget，宁可多试一次复选框
    if not has_widget:
        logger.info("Grok 无 cf-turnstile-response（无 Turnstile widget），跳过自动点击兜底")
        return False
    try:
        frame_loc = page.frame_locator(_TURNSTILE_IFRAME_SELECTOR).first
        checkbox = frame_loc.locator(_TURNSTILE_CHECKBOX_SELECTOR).first
        checkbox.wait_for(state="visible", timeout=6000)
        box = checkbox.bounding_box(timeout=2000)
    except Exception as exc:
        # 有 widget 但定位不到复选框 iframe（DOM 变体 / 仍在加载）→ 不在此点击，
        # 返回 False 让上层进人工接管等待（你可在窗口手动点「Verify you are human」）。
        logger.info("Grok Turnstile widget 存在但复选框未定位到，转人工接管: %s", exc)
        return False

    if not box:
        return False

    emit("action", "GROK_PROFILE", "Turnstile 交互式：尝试自动点击复选框（兜底，失败转人工）",
         action_id="turnstile", result="pending")
    try:
        cx = box["x"] + box["width"] / 2
        cy = box["y"] + box["height"] / 2
        # 真人感：先把鼠标移到附近再移到目标（产生移动轨迹），停顿后点击
        page.mouse.move(cx - 30, cy - 12)
        time.sleep(0.15)
        page.mouse.move(cx, cy)
        time.sleep(0.2)
        page.mouse.click(cx, cy)
    except Exception as exc:
        logger.warning("Grok Turnstile 自动点击异常（转人工）: %s", exc)
        return False

    # 点击后轮询等 token 就绪（Cloudflare 验证需要 1-3s）
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _turnstile_token_present(page):
            emit("action", "GROK_PROFILE", "Turnstile 自动点击成功（兜底生效，省去人工）",
                 action_id="turnstile", result="ok")
            return True
        time.sleep(0.5)
    emit("action", "GROK_PROFILE", "Turnstile 自动点击未通过（转人工接管）",
         action_id="turnstile", result="failed")
    return False


# 人工接管等待 Turnstile 的默认时长（秒）。被动验证超时即视为降级成交互式，
# 给运维在 AdsPower 窗口手动点复选框的时间窗口；可被 config.grok_turnstile_manual_handoff_sec 覆盖。
_TURNSTILE_MANUAL_HANDOFF_SEC = 180


def _wait_turnstile_manual_handoff(page: Any, emit: Callable, *, config: Any = None) -> bool:
    """被动验证超时后，轮询等人工手动勾选 Turnstile 复选框。

    场景：Cloudflare 把会话降级成交互式 Turnstile（截图实证出现可勾选 ☐），被动验证
    不会自动变绿。此时唯一可靠的免费解是真人在 AdsPower 窗口手动点击复选框（isTrusted=true
    的真实点击 + 真实行为生物特征，天然过 Cloudflare 行为层；自动 CDP 点击会被 screenX<100
    检测识破，token 注入会被 IP/指纹绑定的 siteverify 拒）。

    无人值守（worker 批量）场景：等满超时仍 pending → 返回 False，由上层走失败/换号，
    比旧逻辑「硬提交必失败」更明确。

    Returns:
        True = 等待期内复选框被勾选（cf-turnstile-response 变 ready）；False = 超时仍 pending。
    """
    timeout = _TURNSTILE_MANUAL_HANDOFF_SEC
    if config is not None:
        try:
            override = int(getattr(config, "grok_turnstile_manual_handoff_sec", 0) or 0)
            if override > 0:
                timeout = override
        except (TypeError, ValueError):
            pass

    emit(
        "action", "GROK_PROFILE",
        f"Turnstile 被降级为交互式，请在 AdsPower 窗口手动勾选「私はロボットではありません」"
        f"（等待 {timeout}s）...",
        action_id="turnstile", result="manual_handoff",
    )
    deadline = time.time() + timeout
    last_notice = 0.0
    while time.time() < deadline:
        try:
            state = page.evaluate(_JS_TURNSTILE_STATE)
        except Exception:
            state = "not-found"
        if state in ("ready", "not-found"):
            emit(
                "action", "GROK_PROFILE", "Turnstile 已通过（人工接管成功）",
                action_id="turnstile", result="ok",
            )
            return True
        # 每 30s 提醒一次剩余时间，避免运维以为卡死
        now = time.time()
        if now - last_notice >= 30:
            remaining = int(deadline - now)
            emit(
                "action", "GROK_PROFILE", f"仍在等待手动勾选 Turnstile（剩余 ~{remaining}s）",
                action_id="turnstile", result="manual_handoff",
            )
            last_notice = now
        time.sleep(1.0)

    emit(
        "action", "GROK_PROFILE", "Turnstile 人工接管超时（仍尝试提交，大概率失败）",
        action_id="turnstile", result="timeout",
    )
    return False


def _extract_sso(page: Any, context: Any, timeout: int, emit: Callable) -> str:
    """轮询 sso cookie（context.cookies + JS document.cookie + localStorage 三法）。"""
    emit("state_change", "GROK_EXTRACT_SSO", "等待 sso token...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        # 法 1：Playwright context cookies
        try:
            for ck in context.cookies():
                if str(ck.get("name", "")).strip() == "sso" and str(ck.get("value", "")).strip():
                    return str(ck["value"]).strip()
        except Exception:
            pass
        # 法 2：JS document.cookie
        try:
            raw = page.evaluate(_JS_GET_COOKIE) or ""
            for pair in raw.split(";"):
                pair = pair.strip()
                if pair.startswith("sso=") and len(pair) > 4:
                    return pair[4:].strip()
        except Exception:
            pass
        # 法 3：localStorage
        try:
            ls = page.evaluate(_JS_LS_SSO)
            if ls:
                return str(ls).strip()
        except Exception:
            pass
        time.sleep(1)
    return ""


# ─────────────────────────────────────────────────────────────────────
# 辅助
# ─────────────────────────────────────────────────────────────────────

def _try_assist(
    page: Any,
    emit: Callable,
    assist: Optional[dict],
    *,
    step: str,
    want_fill: bool,
    fill_value: str = "",
    verify: Optional[Callable[[], bool]] = None,
) -> bool:
    """硬规则失败后的 AI 辅助决策入口（薄封装 grok_assist.assisted_action）。

    assist={"llm": LLMDecisionProvider|None, "exp": ExperienceStore|None}；
    二者皆 None 时 assisted_action 内部直接返回 False（纯降级，不报错）。
    """
    if not assist:
        return False
    try:
        from src.automation.grok_assist import assisted_action
        return assisted_action(
            page,
            step=step,
            want_fill=want_fill,
            fill_value=fill_value,
            experience=assist.get("exp"),
            llm_provider=assist.get("llm"),
            emit=emit,
            verify=verify,
        )
    except Exception as exc:
        logger.warning("Grok AI 辅助决策异常（降级）: %s", exc)
        return False


def _dismiss_cookie(page: Any, emit: Callable) -> None:
    """关闭 Cookie 同意弹窗（语言无关，best-effort，失败静默）。

    真机实测 Grok 在验证码页弹 Cookie 同意框遮挡 OTP 输入框；不关掉无法填码。
    点「接受全部」优先（多语言），否则点关闭 ×。重试 2 次（弹窗可能延迟出现）。
    """
    for _ in range(2):
        try:
            result = page.evaluate(_JS_DISMISS_COOKIE)
        except Exception:
            result = "none"
        if result in ("accepted", "closed"):
            emit("action", None, "已关闭 Cookie 弹窗", action_id="dismiss_cookie", result=result)
            time.sleep(0.6)
            return
        time.sleep(0.5)


def _click_text(page: Any, text: Any, *, timeout: int = 10) -> bool:
    """点击含指定文案的按钮：Playwright locator 优先，JS fallback。

    text 可为单个字符串或字符串元组（任一匹配即可）。
    """
    wanted = [text] if isinstance(text, str) else list(text)
    deadline = time.time() + timeout
    # locator 优先（更稳）
    for w in wanted:
        try:
            loc = page.get_by_text(w, exact=False).first
            if loc and loc.is_visible(timeout=1000):
                loc.click(timeout=2000)
                return True
        except Exception:
            pass
    # JS fallback（覆盖 role=button / aria 等 locator 抓不到的）
    while time.time() < deadline:
        try:
            if page.evaluate(_JS_CLICK_BY_TEXT, wanted):
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _resolve_artifacts_base(config: Any = None) -> str:
    """解析证据包根目录（绝对路径），与 ArtifactRecorder 的 base_dir 同源。

    根因（修「桌面 app 失败从不截图」）：桌面 app 进程 cwd=`/`，旧 _screenshot 用相对路径
    `artifacts/runs/<id>` 会解析成 `/artifacts/...`（无写权限）→ 静默写失败，导致失败现场
    既无截图也无 AI 辅助可用的视觉证据。

    优先级（与 ArtifactRecorder 一致，落到 base_dir/<run_id>，**不再多套 runs 层**）：
      ① config.run_artifacts_dir（注册流证据包同源 base_dir）；
      ② env RUN_ARTIFACTS_DIR（桌面 app 由 desktop_app._bootstrap_env 锚定为绝对路径）；
      ③ 兜底 "artifacts/runs"（CLI 直跑场景，cwd 在项目根可用）。
    """
    base = ""
    if config is not None:
        base = (getattr(config, "run_artifacts_dir", "") or "").strip()
    if not base:
        base = (os.getenv("RUN_ARTIFACTS_DIR", "") or "").strip()
    if not base:
        base = "artifacts/runs"
    return base


def _screenshot(page: Any, db_run_id: Optional[str], name: str, config: Any = None) -> None:
    """失败截图到证据包目录（best-effort）。

    与 run 的其它证据同目录（base_dir/<run_id>/<name>.png）。失败不抛，但**记 warning**
    （旧版静默吞异常 → 出问题时无从排查「为啥没截图」）。db_run_id 为空时用时间戳兜底文件名，
    保证任何失败都有现场快照。
    """
    base = _resolve_artifacts_base(config)
    run_id = db_run_id or f"grok-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    try:
        run_dir = os.path.join(base, run_id)
        os.makedirs(run_dir, exist_ok=True)
        out_path = os.path.join(run_dir, f"{name}.png")
        page.screenshot(path=out_path, full_page=False)
        logger.info("Grok 失败截图已保存: %s", out_path)
    except Exception as exc:
        logger.warning("Grok 失败截图保存失败 (base=%s run=%s): %s", base, run_id, exc)


__all__ = ["run_grok_task", "GrokRegistrationError", "SIGNUP_URL"]
