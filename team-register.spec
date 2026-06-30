# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：team-register 桌面应用。

构建：pyinstaller team-register.spec --clean
产物：dist/Team Register.app（macOS）

设计要点：
  - collect_submodules('src')：整个 src 包全收，避免逐个补 hiddenimports
    （worker 动态 import main、providers 装饰器注册、services lazy import 等
    PyInstaller 静态分析跟不到的动态引用一次性覆盖）。
  - 模板/静态/locales 作为 datas 打进 bundle，配合 src/api/app.py 的 Path(__file__)
    定位（PyInstaller 下 __file__ 指向 sys._MEIPASS，相对结构保持即可）。
  - **收 Playwright 的 Python 客户端栈（含 _impl._driver），但排除 Chromium 二进制**：
    浏览器走 AdsPower（connect_over_cdp），Playwright 只做 CDP 客户端；但 sync_playwright()
    的启动链路仍要 import _driver 模块、读取 driver 脚本与 package.json，故这些必须打进来。
    只在 collect_data_files 里 excludes 掉 .local-browsers 下的 Chromium，省 ~150MB。
"""

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

block_cipher = None

# 整个 src 包 + 根目录 main（worker 动态 import）+ 第三方动态模块
hiddenimports = (
    collect_submodules("src")
    + ["main"]
    + collect_submodules("uvicorn")
    + collect_submodules("playwright")  # _impl._driver 等动态子模块必须收全
    + ["sqlmodel", "sqlalchemy.dialects.sqlite", "passlib.handlers.bcrypt"]
)

# 数据文件：模板 + 静态 + locales（保持 src/ 下的相对结构）
datas = [
    ("src/templates", "src/templates"),
    ("src/static", "src/static"),
    # 帮助页手册（knowledge_service 用 _PROJECT_ROOT/docs/usage-manual.md 定位；
    # _PROJECT_ROOT = deps.py parents[2] = bundle 的 Frameworks 目录，故落到 docs/ 同级）
    ("docs/usage-manual.md", "docs"),
]
# email-validator / pydantic 等可能带数据文件，按需 collect（保守起见收 pydantic）
datas += collect_data_files("pydantic", include_py_files=False)
# Playwright 的 driver 脚本 + package.json（_driver 模块运行时要读取这些定位 node driver）；
# 仅排除已下载的 Chromium 二进制（走 AdsPower，不需要本地浏览器，省 ~150MB）
datas += collect_data_files(
    "playwright",
    include_py_files=False,
    excludes=["**/.local-browsers/**", "**/node_modules/**"],
)

a = Analysis(
    ["desktop_app.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 测试框架不进产物。注意：不要 exclude playwright._impl._driver——
    # 即使走 AdsPower（connect_over_cdp），sync_playwright() 启动链路仍依赖该模块；
    # 浏览器二进制的瘦身已在上方 collect_data_files 的 excludes 处理。
    excludes=["pytest"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Team Register",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # --windowed：无终端黑窗
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/icon.icns" if __import__("os").path.exists("assets/icon.icns") else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Team Register",
)

app = BUNDLE(
    coll,
    name="Team Register.app",
    icon="assets/icon.icns" if __import__("os").path.exists("assets/icon.icns") else None,
    bundle_identifier="com.teamregister.console",
    info_plist={
        "CFBundleName": "Team Register",
        "CFBundleDisplayName": "Team Register 控制台",
        "NSHighResolutionCapable": True,
        # 允许连本地 http（127.0.0.1）—— App Transport Security 例外
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
    },
)
