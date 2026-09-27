"""冒烟测试：守护模块导入图与公开 API 面。

历史教训：本仓库 CI 曾因 `utils.py` 与 `utils/` 包冲突、缺 `requests` 依赖
在生产环境直接崩溃——本测试即在导入层面捕获这类回归。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class TestModuleImports(unittest.TestCase):
    def test_main_imports(self):
        import main as M
        for attr in ["main", "CIState", "write_feedback",
                     "compute_source_signature", "apply_incremental_testing"]:
            self.assertTrue(hasattr(M, attr), f"main 缺少公开符号: {attr}")

    def test_submodules_import(self):
        import utils.ai_helper
        import utils.categorizer
        import utils.config
        import utils.epg
        import utils.fetcher
        import utils.http
        import utils.loaders
        import utils.output
        import utils.speedtest

    def test_config_http_reexports(self):
        """http.py 拆分后，config.py 必须保持兼容再导出"""
        import utils.config as C
        import utils.http as H
        self.assertIs(C.get_session, H.get_session)
        self.assertIs(C.get_pool, H.get_pool)
        self.assertIs(C.fetch_url, H.fetch_url)
        self.assertIs(C.retry_request, H.retry_request)

    def test_feedback_contract_fields(self):
        """write_feedback 必须产出 server_scores/dead_ips（get-m3u 依赖契约）"""
        import json
        import tempfile
        import main as M
        with tempfile.TemporaryDirectory() as td:
            M.FEEDBACK_FILE = os.path.join(td, "feedback.json")
            M.write_feedback(
                {"CCTV-1": [("http://1.2.3.4:4000/rtp/x", 2.0)]},
                {}, {},
                to_test=[("CCTV-1", "http://1.2.3.4:4000/rtp/x"),
                         ("X", "http://9.9.9.9:8000/a"),
                         ("Y", "http://9.9.9.9:8000/b"),
                         ("Z", "http://9.9.9.9:8000/c")],
            )
            fb = json.load(open(M.FEEDBACK_FILE, encoding="utf-8"))
        self.assertEqual(fb["_version"], 1)
        self.assertIn("server_scores", fb)
        self.assertIn("dead_ips", fb)
        self.assertEqual(fb["dead_ips"], ["9.9.9.9:8000"])


if __name__ == "__main__":
    unittest.main()


class TestAliasRegexHealth(unittest.TestCase):
    """alias.txt 中所有 re: 正则必须可编译（防逗号拆分/管道符误改回归）"""
    def test_all_regex_aliases_compile(self):
        import re as _re
        path = os.path.join(ROOT, "config", "alias.txt")
        bad = []
        total = 0
        with open(path, encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith('#') or ',' not in line:
                    continue
                for part in line.split(',')[1:]:
                    part = part.strip()
                    if part.startswith('re:'):
                        total += 1
                        try:
                            _re.compile(part[3:])
                        except _re.error as e:
                            bad.append(f"行{i}: {e}")
        self.assertGreater(total, 0, "未能找到 re: 别名")
        self.assertEqual(bad, [], f"{len(bad)} 条坏正则: {bad[:3]}")
