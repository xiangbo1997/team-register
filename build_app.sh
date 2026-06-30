#!/usr/bin/env bash
# team-register 桌面应用一键构建脚本（macOS）
#
# 用法：bash build_app.sh
# 产物：dist/Team Register.app
#
# 前置：已 pip install -r requirements.txt（运行时依赖）
set -euo pipefail

cd "$(dirname "$0")"

echo "==> 安装打包依赖（pywebview + pyinstaller）..."
pip install -r requirements-build.txt

echo "==> 清理旧产物..."
rm -rf build dist

echo "==> PyInstaller 打包..."
pyinstaller team-register.spec --clean --noconfirm

echo ""
echo "==> 完成！产物：dist/Team Register.app"
echo "    首次打开若被 Gatekeeper 拦截（未签名），执行："
echo "    xattr -dr com.apple.quarantine 'dist/Team Register.app'"
echo "    或右键 → 打开。"
