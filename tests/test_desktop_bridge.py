# -*- coding: utf-8 -*-
"""桌面端 pywebview 保存桥 ``_DesktopBridge.save_file`` 测试。

回归锁：多账号导出的 zip 是二进制，旧实现一律按 UTF-8 文本写
（``open(..., "w")`` + ``response.text()``），二进制字节被 UTF-8 解码替换成
U+FFFD 而损坏——症状是"导出选中账号全信息，grok 解压后为空"。修复后二进制走
``is_base64=True`` 路径：base64 解码 + 二进制写，无损保真。
"""

import base64
import importlib.util
import io
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

# desktop_app.py 在仓库根目录（非 src 包内），用 spec 直接加载
_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("desktop_app", _ROOT / "desktop_app.py")
desktop_app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(desktop_app)


class _FakeWindow:
    """伪 pywebview 窗口：create_file_dialog 直接返回预设保存路径（跳过 GUI）。"""

    def __init__(self, target_path: str):
        self._target = target_path

    def create_file_dialog(self, save_type, save_filename=None):
        return (self._target,)


class _FakeWindowCancelled:
    """模拟用户取消保存对话框：返回空。"""

    def create_file_dialog(self, save_type, save_filename=None):
        return None


def _make_zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("codex-a@x.com.json", '{"email":"a@x.com","sso_token":"tok-AAA"}')
        zf.writestr("codex-b@x.com.json", '{"email":"b@x.com","sso_token":"tok-BBB"}')
    return buf.getvalue()


class DesktopBridgeSaveFileTest(unittest.TestCase):
    def setUp(self):
        self.bridge = desktop_app._DesktopBridge()
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _bind_window_to(self, target: str):
        """让 bridge 用伪窗口，并把 webview.windows 也兜住（save_file 内部 fallback 用）。"""
        self.bridge.bind_window(_FakeWindow(target))

    def test_save_binary_zip_via_base64_roundtrips_losslessly(self):
        """is_base64=True：base64 解码 + 二进制写，zip 解压内容完整（核心回归锁）。"""
        zip_bytes = _make_zip_bytes()
        b64 = base64.b64encode(zip_bytes).decode("ascii")
        target = os.path.join(self.tmpdir, "export.zip")
        self._bind_window_to(target)

        res = self.bridge.save_file("export.zip", b64, True)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["path"], target)

        # 落盘字节与原始 zip 完全一致（无 U+FFFD 损坏）
        with open(target, "rb") as fh:
            written = fh.read()
        self.assertEqual(written, zip_bytes)

        # 解压可正常读到两条账号（用户报告的"解压后为空"反例）
        with zipfile.ZipFile(target) as zf:
            names = sorted(zf.namelist())
            self.assertEqual(names, ["codex-a@x.com.json", "codex-b@x.com.json"])
            self.assertIn("tok-AAA", zf.read("codex-a@x.com.json").decode())
            self.assertIn("tok-BBB", zf.read("codex-b@x.com.json").decode())

    def test_save_text_json_default_path_unchanged(self):
        """is_base64 缺省（False）：文本按 UTF-8 写，行为不变（单账号 JSON / SSO txt）。"""
        target = os.path.join(self.tmpdir, "single.json")
        self._bind_window_to(target)
        payload = '{"email":"solo@x.com","sso_token":"tok-SOLO"}'

        res = self.bridge.save_file("single.json", payload)
        self.assertTrue(res["ok"], res)
        with open(target, "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), payload)

    def test_save_text_with_unicode_preserved(self):
        """文本路径保留中文等非 ASCII（UTF-8）。"""
        target = os.path.join(self.tmpdir, "u.txt")
        self._bind_window_to(target)
        payload = "账号\nsso-令牌-中文"
        res = self.bridge.save_file("u.txt", payload, False)
        self.assertTrue(res["ok"], res)
        with open(target, "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), payload)

    def test_save_cancelled_when_dialog_returns_empty(self):
        """用户取消保存对话框 → {ok: False, cancelled: True}。"""
        self.bridge.bind_window(_FakeWindowCancelled())
        # webview.windows fallback 也得为空，避免 save_file 取到真实窗口
        with mock.patch.object(desktop_app, "_DESKTOP_BRIDGE", self.bridge):
            res = self.bridge.save_file("x.zip", "abc", True)
        self.assertFalse(res["ok"])
        self.assertTrue(res.get("cancelled"))


if __name__ == "__main__":
    unittest.main()
