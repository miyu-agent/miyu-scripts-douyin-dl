# douyin_dl.py 的测试（审计 2026-11-18）。
#
# 跑法（在仓库根）：
#   python3 -I -B -m unittest discover -s .github/tests
# （不加 -t .：.github 不是合法的包名，加了 unittest 会报 Start directory is not importable。）
#
# 只用标准库；不联网：urlopen、tiktok_post 换成直接失败的桩，断网也能跑。HOME、XDG_*、MIYU_* 都指到临时目录。
# 放在 .github/tests/ 而不是 tests/：打包会跳过 .github，测试不会进包。

import importlib.util
import io
import json
import os
import pathlib
import struct
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "douyin_dl.py"

# SOF0 段：长度 11、8 位、高 200、宽 100、1 个分量
SOF0 = struct.pack(">HBHHB", 11, 8, 200, 100, 1) + b"\x01\x11\x00"


def load():
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("douyin_dl_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Sandbox(unittest.TestCase):
    """每条测试一个临时目录，环境里去掉所有 MIYU_*，网络函数先换成直接失败的桩。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("MIYU_", "XDG_"))}
        env.update(HOME=str(root / "home"), XDG_CONFIG_HOME=str(root / "cfg"),
                   XDG_CACHE_HOME=str(root / "xdg-cache"), XDG_STATE_HOME=str(root / "xdg-state"),
                   XDG_DATA_HOME=str(root / "xdg-data"))
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = root
        self.mod = load()
        self.calls = []

        def no_network(request, *a, **k):
            url = getattr(request, "full_url", request)
            self.calls.append(url)
            raise AssertionError(f"测试里不该联网：{url}")

        net = mock.patch.object(self.mod.urllib.request, "urlopen", no_network)
        net.start()
        self.addCleanup(net.stop)

        def no_tiktok(url, cookies_path):
            self.calls.append(url)
            raise AssertionError(f"不该把这个链接交给 yt-dlp：{url}")

        self.mod.tiktok_post = no_tiktok

    def call(self, args):
        """照 main() 跑一次：stdin 喂 JSON，返回 (退出码或 Failure, 输出)。"""
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(args))), redirect_stdout(out), \
                mock.patch.object(sys, "stderr", io.StringIO()):
            try:
                return self.mod.main(), out.getvalue()
            except self.mod.Failure as exc:
                return exc, out.getvalue()


class LookalikeHost(Sandbox):
    # 审计 #173-a：platform_of 用 host.endswith("tiktok.com")，evil-tiktok.com 也被当成 TikTok；
    # extract_url 的最后一条通配规则把它原样交回，于是交给 yt-dlp 去抓别的站。
    EVIL = "https://evil-tiktok.com/@a/video/1"

    def test_lookalike_host_is_not_tiktok(self):
        url = self.mod.extract_url(self.EVIL)
        if url is not None:  # extract_url 拒掉也算对
            self.assertIsNone(self.mod.platform_of(url))

    def test_lookalike_host_friendly_error_before_ytdlp(self):
        result, out = self.call({"action": "info", "url": self.EVIL})
        self.assertIsInstance(result, self.mod.Failure, f"应给友好报错，实际返回 {result!r}：{out}")
        self.assertEqual(self.calls, [], "不该联网、不该交给 yt-dlp")

    # 防改坏（现在就绿）：真的 TikTok 域名照旧认。
    def test_real_tiktok_hosts(self):
        for text in ("https://www.tiktok.com/@a/video/1", "https://vm.tiktok.com/ZMabc/"):
            with self.subTest(text=text):
                self.assertEqual(self.mod.platform_of(self.mod.extract_url(text)), "tiktok")


class SniffJpegFill(Sandbox):
    # 审计 #173-d：JPEG 的 marker 前可以有任意个 0xFF 填充字节（ITU T.81 B.1.1.2）。
    # sniff 在 0xFF 后直接把下一个字节当 marker，遇到 FF FF C0 就把 0xFF 当段、按后两字节当长度跳走，合法图判成 None。
    def test_fill_byte_before_sof0(self):
        data = b"\xff\xd8" + b"\xff\xff\xc0" + SOF0 + b"\xff\xd9"
        self.assertEqual(self.mod.sniff(data), ("image/jpeg", 100, 200))

    # 防改坏（现在就绿）：不带填充字节的照旧认得。
    def test_plain_sof0(self):
        data = b"\xff\xd8" + b"\xff\xc0" + SOF0 + b"\xff\xd9"
        self.assertEqual(self.mod.sniff(data), ("image/jpeg", 100, 200))


if __name__ == "__main__":
    unittest.main()
