#!/usr/bin/env bash
# team-register 分发镜像打包脚本（macOS / Apple Silicon）
#
# 把 dist/Team Register.app 打成可拖拽安装的 .dmg，附「打开说明」。
# 前置：先跑过 bash build_app.sh（产物 dist/Team Register.app 已存在）。
#
# 用法：bash build_dmg.sh
# 产物：dist/Team-Register-arm64.dmg
#
# 设计要点：
#   - 零第三方依赖，全用系统自带 hdiutil / ditto（保留 macOS 元数据与签名）。
#   - dmg 内放 /Applications 软链，用户挂载后把 app 拖过去即装。
#   - adhoc 签名 app 跨机分发会被 Gatekeeper 拦，附「打开说明.txt」教对方解除隔离。
set -euo pipefail

cd "$(dirname "$0")"

APP_NAME="Team Register"
APP_PATH="dist/${APP_NAME}.app"
VOL_NAME="Team Register"
DMG_OUT="dist/Team-Register-arm64.dmg"
STAGING="dist/dmg_staging"

if [[ ! -d "$APP_PATH" ]]; then
  echo "✗ 未找到 $APP_PATH，请先执行：bash build_app.sh" >&2
  exit 1
fi

echo "==> 清理旧 dmg / 暂存目录..."
rm -f "$DMG_OUT"
rm -rf "$STAGING"
mkdir -p "$STAGING"

echo "==> 拷贝 app 到暂存目录（ditto 保留签名与扩展属性）..."
ditto "$APP_PATH" "$STAGING/${APP_NAME}.app"

echo "==> 创建 /Applications 软链（拖拽安装用）..."
ln -s /Applications "$STAGING/Applications"

echo "==> 写入「打开说明.txt」..."
cat > "$STAGING/打开说明.txt" <<'TXT'
Team Register 控制台 — 安装与首次打开说明
============================================

【安装】
  把「Team Register.app」拖到旁边的「Applications」文件夹即可。

【首次打开（重要）】
  本应用未经 Apple 公证，首次打开可能提示「已损坏 / 无法验证开发者」。
  这是正常的（adhoc 签名），按任一方式打开：

  方式 A（推荐，终端执行一次）：
    把下面这行粘到「终端」回车，输入开机密码（不显示是正常的）：
      xattr -dr com.apple.quarantine "/Applications/Team Register.app"
    之后双击图标正常打开。

  方式 B（鼠标操作）：
    在「应用程序」里右键点 Team Register → 选「打开」→ 再点「打开」。

【使用前置】
  - 账号池的「开通 Plus / 核验 / 刷新 token」需要本机已启动 AdsPower
    指纹浏览器（API 端口 50325）。请先打开 AdsPower 客户端。
  - 首次启动是空配置。在应用内「配置」页填好 API key（接码 / 虚拟卡 /
    邮件 / LLM 等），数据保存在：
      ~/Library/Application Support/team-register/

【排障】
  - 看不到报错时，日志文件在：
      ~/Library/Application Support/team-register/artifacts/ 的父目录
    （control-plane.log）
TXT

echo "==> hdiutil 生成压缩 dmg..."
hdiutil create \
  -volname "$VOL_NAME" \
  -srcfolder "$STAGING" \
  -ov \
  -format UDZO \
  "$DMG_OUT"

echo "==> 清理暂存目录..."
rm -rf "$STAGING"

echo ""
echo "==> 完成！分发镜像：$DMG_OUT"
du -sh "$DMG_OUT"
echo ""
echo "    把这个 .dmg 发给对方（仅限 Apple Silicon Mac）。"
echo "    对方双击挂载 → 拖 app 到 Applications → 按「打开说明.txt」首次打开。"
