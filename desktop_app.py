# -*- coding: utf-8 -*-
"""team-register 桌面应用入口。

双击图标 → 后台起 uvicorn → pywebview 弹出原生窗口加载控制面。
本地运行，依赖本地 AdsPower（指纹浏览器，端口 50325），与命令行 uvicorn 行为一致。

打包要点（PyInstaller）：
  - 打包后 ``__file__`` 在临时解压目录（sys._MEIPASS），模板/静态靠 spec 的 datas 打进 bundle。
  - SQLite / artifacts / .env 等**可写数据**必须落到稳定目录
    ``~/Library/Application Support/team-register/``，否则会写进只读 bundle / 临时目录导致丢数据。
  - 关键顺序：先 _bootstrap_env() 设好 DATABASE_URL/RUN_ARTIFACTS_DIR + load_dotenv，
    **再** import src.api.app（get_engine 惰性单例 + init_db 在 lifespan 调，不会提前触发）。
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path

APP_NAME = "Team Register 控制台"
HOST = "127.0.0.1"
PORT = 8080
ADS_PORT = 50325  # 本地 AdsPower API 端口


def _app_data_dir() -> Path:
    """稳定可写数据目录：~/Library/Application Support/team-register/（按平台兜底）。"""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "team-register"
    elif sys.platform.startswith("win"):
        base = Path(os.getenv("APPDATA", str(Path.home()))) / "team-register"
    else:
        base = Path(os.getenv("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "team-register"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _bundle_dir() -> Path:
    """打包资源根目录：PyInstaller 下是 sys._MEIPASS，开发时是脚本所在目录。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent


class _DesktopBridge:
    """pywebview JS API：前端通过 ``window.pywebview.api.save_file(...)`` 调用。

    存在原因：macOS WKWebView（pywebview 默认后端）不实现 HTML5 ``<a download>``
    与 blob URL 的下载行为——前端 ``downloadFile()`` 点下载会被 WebView 当成页面
    导航，把 CSV/JSON 响应体平铺渲染到窗口里（而非保存文件）。这里用 pywebview
    的原生 ``create_file_dialog(SAVE)`` 弹系统保存框，把内容写到用户选定的路径。

    Web 端（浏览器访问 uvicorn）不存在 ``window.pywebview``，前端自动回退到原 blob
    下载路径，本桥不参与，互不影响。
    """

    def __init__(self) -> None:
        self._window = None

    def bind_window(self, window) -> None:
        self._window = window

    def save_file(self, filename: str, content: str) -> dict:
        """弹原生保存对话框，把文本内容写到用户选定路径。

        Args:
            filename: 建议的文件名（如 ``accounts_registered.csv``）。
            content: 要保存的文本内容（导出均为 UTF-8 文本：CSV / JSON）。

        Returns:
            ``{"ok": True, "path": "/选定/路径"}`` 成功；
            ``{"ok": False, "cancelled": True}`` 用户取消；
            ``{"ok": False, "error": "..."}`` 写入失败。
        """
        try:
            import webview

            window = self._window or (webview.windows[0] if webview.windows else None)
            if window is None:
                return {"ok": False, "error": "no_active_window"}

            safe_name = str(filename or "download").strip() or "download"
            save_type = getattr(webview, "SAVE_DIALOG", 30)
            result = window.create_file_dialog(save_type, save_filename=safe_name)
            # 不同 pywebview 版本返回 str 或 Sequence[str]；统一取首个路径
            if not result:
                return {"ok": False, "cancelled": True}
            target = result[0] if isinstance(result, (list, tuple)) else str(result)
            if not target:
                return {"ok": False, "cancelled": True}

            with open(target, "w", encoding="utf-8", newline="") as fh:
                fh.write(content or "")
            return {"ok": True, "path": target}
        except Exception as exc:  # 桥接异常不能崩窗口，回报给前端提示
            return {"ok": False, "error": str(exc)}

    def copy_to_clipboard(self, text: str) -> dict:
        """把文本写入系统剪贴板（桌面端复制 token / 链接用）。

        存在原因：macOS WKWebView（pywebview 默认后端）在 http://localhost（非安全
        上下文）下，``navigator.clipboard`` 被拒、``execCommand('copy')`` 也被新版
        WebKit 弃用并返回 false——前端两层降级全失败，复制 token 报"复制失败，请
        手动选中"。这里下沉到 Python 用系统 ``pbcopy`` 直接写剪贴板，绕开浏览器沙箱
        （与 save_file 同思路：WKWebView 干不了的事交给原生）。

        Web 端（浏览器访问 uvicorn）无 ``window.pywebview``，前端走浏览器剪贴板 API，
        本桥不参与。

        Returns:
            ``{"ok": True}`` 成功；``{"ok": False, "error": "..."}`` 失败（前端回退提示）。
        """
        try:
            import subprocess

            payload = (text or "").encode("utf-8")
            if sys.platform == "darwin":
                cmd = ["pbcopy"]
            elif sys.platform.startswith("win"):
                cmd = ["clip"]
            else:  # Linux：优先 xclip，没有则 xsel
                cmd = ["xclip", "-selection", "clipboard"]
            proc = subprocess.run(cmd, input=payload, timeout=5)
            if proc.returncode != 0:
                return {"ok": False, "error": f"剪贴板命令退出码 {proc.returncode}"}
            return {"ok": True}
        except Exception as exc:  # 桥接异常不能崩窗口，回报给前端
            return {"ok": False, "error": str(exc)}


# 模块级单例：create_window 时绑定 window，供 save_file 调用对话框
_DESKTOP_BRIDGE = _DesktopBridge()


def _migrate_existing_db(data: Path) -> None:
    """首次启动数据迁移：新位置没有 DB、但项目根/bundle 旁有现成的 team_register.db
    时复制过去，避免双击 app 看到空库（现有账号池数据"消失"）。

    只在新位置不存在 DB 时执行一次；之后用新位置的库，互不干扰。
    """
    target = data / "team_register.db"
    if target.exists():
        return  # 已有库，不覆盖
    # 候选来源：开发目录（脚本旁）/ bundle 旁
    for src in (Path(__file__).resolve().parent / "team_register.db", _bundle_dir() / "team_register.db"):
        try:
            if src.exists() and src.resolve() != target.resolve():
                import shutil

                shutil.copy2(src, target)
                print(f"[desktop_app] 已迁移现有数据库 {src} → {target}", flush=True)
                return
        except Exception as exc:  # 迁移失败不致命，起空库即可
            print(f"[desktop_app] 数据库迁移跳过（{src}）: {exc}", flush=True)


def _bootstrap_env() -> Path:
    """在 import app 之前锚定可写路径 + 加载 .env。返回 data 目录。"""
    data = _app_data_dir()

    # 首次启动把现有 team_register.db 迁到可写目录（避免看到空库）
    _migrate_existing_db(data)

    # ① DB 重定向到可写目录（覆盖 src/db/engine.py 的相对默认 sqlite:///team_register.db）。
    #    用 setdefault：若用户已在环境/.env 显式设了 DATABASE_URL 则尊重之。
    os.environ.setdefault("DATABASE_URL", f"sqlite:///{data / 'team_register.db'}")

    # ② artifacts / 证据包目录（RUN_ARTIFACTS_DIR 是现有约定，见 CLAUDE.md 环境变量）。
    os.environ.setdefault("RUN_ARTIFACTS_DIR", str(data / "artifacts"))

    # ③ .env：优先 data 目录的 .env（用户配置）。首次启动若 data 目录没有、
    #    但开发目录旁有现成 .env（含 API key），复制过去——避免桌面 app 缺配置。
    from dotenv import load_dotenv

    user_env = data / ".env"
    if not user_env.exists():
        dev_env = Path(__file__).resolve().parent / ".env"
        try:
            if dev_env.exists() and dev_env.resolve() != user_env.resolve():
                import shutil

                shutil.copy2(dev_env, user_env)
                print(f"[desktop_app] 已迁移 .env → {user_env}", flush=True)
        except Exception as exc:
            print(f"[desktop_app] .env 迁移跳过: {exc}", flush=True)

    if user_env.exists():
        load_dotenv(user_env)
    # load_config() 内部还会再 load_dotenv() 自动查找，作为兜底

    return data


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    """探测 host:port 是否可连接（已被监听）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def _wait_port(host: str, port: int, timeout: float = 30.0) -> bool:
    """轮询等待端口就绪（服务起来）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_open(host, port):
            return True
        time.sleep(0.3)
    return False


def _run_server() -> None:
    """后台线程：起 uvicorn。import app 必须在 _bootstrap_env 之后发生。"""
    import logging

    import uvicorn

    from src.api.app import app
    from src.utils import setup_logger

    # 配置 root logger（含落盘 control-plane.log）+ 放开 INFO，
    # 否则桌面 app 下刷新 token / 核验 Plus 的步骤日志（logger.info）看不到、也排不了障。
    setup_logger(level=logging.INFO)

    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


def _adspower_hint() -> str:
    """AdsPower 未开时的提示（非阻塞，仅展示）。"""
    if _port_open(HOST, ADS_PORT):
        return ""
    return (
        "⚠️ 未检测到 AdsPower（端口 50325）。\n"
        "账号池的开通 Plus / 核验 / 刷新 token 需要 AdsPower 浏览器，"
        "请先启动 AdsPower 客户端后再使用这些功能。"
    )


def main() -> None:
    _bootstrap_env()

    # 单实例：8080 已被占用（已有一个在跑）→ 直接开窗口连上去，不再起第二个 uvicorn。
    already_running = _port_open(HOST, PORT)
    if not already_running:
        threading.Thread(target=_run_server, daemon=True).start()
        if not _wait_port(HOST, PORT, timeout=30.0):
            _fatal("服务启动超时，请检查端口 8080 是否被占用，或查看日志。")
            return

    # AdsPower 提示（不阻塞，仅在控制台/日志输出；窗口内功能失败时也会有明确报错）
    hint = _adspower_hint()
    if hint:
        print(hint, flush=True)

    import webview

    # pywebview JS↔Python 桥：暴露原生保存对话框给前端。
    # macOS WKWebView 不实现 <a download> / blob 下载（点了把响应当页面渲染），
    # 前端 downloadFile() 在桌面端改走 window.pywebview.api.save_file 弹原生保存框。
    window = webview.create_window(
        APP_NAME,
        f"http://{HOST}:{PORT}/",
        width=1440,
        height=900,
        min_size=(1100, 700),
        js_api=_DESKTOP_BRIDGE,
    )
    # 把 window 绑到 bridge，让 save_file 能调它的 create_file_dialog
    _DESKTOP_BRIDGE.bind_window(window)
    # 关窗口即退出；daemon 线程的 uvicorn 随主进程结束
    webview.start()


def _fatal(message: str) -> None:
    """启动失败兜底：尽量用原生弹窗提示，失败则打印。"""
    print(f"[desktop_app] FATAL: {message}", flush=True)
    try:
        import webview

        webview.create_window("启动失败", html=f"<h2 style='font-family:sans-serif'>启动失败</h2><p>{message}</p>")
        webview.start()
    except Exception:
        pass


if __name__ == "__main__":
    main()
