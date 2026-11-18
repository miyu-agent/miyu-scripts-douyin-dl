# douyin_dl.py 读新环境变量名的测试（审计 2026-11-18 #168）。
#
# 跑法（在仓库根）：
#   python3 -I -B -m unittest discover -s .github/tests
# （不加 -t .：.github 不是合法的包名，加了 unittest 会报 Start directory is not importable。）
#
# 只用标准库；不联网：urllib.request.urlopen 换成直接失败的桩，断网也能跑。HOME、XDG_*、MIYU_* 都指到临时目录。
# 放在 .github/tests/ 而不是 tests/：打包会跳过 .github，测试不会进包。

import importlib.util
import json
import os
import pathlib
import struct
import sys
import tempfile
import time
import unittest
from unittest import mock

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "douyin_dl.py"

# 64×48 的 PNG 头：sniff 只看前 24 字节
PNG = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", 64, 48) + b"\x08\x06\x00\x00\x00"


def load():
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("douyin_dl_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Sandbox(unittest.TestCase):
    """每条测试一个临时目录，环境里去掉所有 MIYU_*，urlopen 先换成直接失败的桩。"""

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


class EnvNames(Sandbox):
    # 审计 #168：核心 FH-6 以后只给新名字 MIYU_CACHE_DIR、MIYU_IMAGES_DIR，不再给 MIYU_SCRIPT_*。
    # 脚本两处只读旧名：cache_dir()（抖音设备 cookie、yt-dlp 缓存）和 Attacher（附图给 AI 看）。

    def test_cache_dir_new_name(self):
        # 缓存里有一天内的 ttwid，mint_ttwid 应直接用它、不联网。现在读不到缓存目录，去联网，撞上桩。
        cache = self.root / "cache"
        cache.mkdir()
        os.environ["MIYU_CACHE_DIR"] = str(cache)
        self.assertNotIn("MIYU_SCRIPT_CACHE_DIR", os.environ)
        (cache / "ttwid.json").write_text(json.dumps({"value": "cached-ttwid", "at": time.time()}), encoding="utf-8")
        self.assertEqual(self.mod.cache_dir(), str(cache))
        self.assertEqual(self.mod.mint_ttwid(), "cached-ttwid")
        self.assertEqual(self.calls, [], "缓存在 MIYU_CACHE_DIR 里，不该联网")

    def test_images_dir_new_name(self):
        # 封面应复制进 MIYU_IMAGES_DIR 给 AI 看。现在读不到，add 返回 False，目录是空的。
        images = self.root / "attach"
        images.mkdir()
        os.environ["MIYU_IMAGES_DIR"] = str(images)
        self.assertNotIn("MIYU_SCRIPT_IMAGES_DIR", os.environ)
        attacher = self.mod.Attacher()
        self.assertTrue(attacher.add(PNG, "cover"))
        self.assertEqual(sorted(p.name for p in images.iterdir()), ["01-cover.png"])


if __name__ == "__main__":
    unittest.main()
