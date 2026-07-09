"""m3u-checker-max 优化项回归测试。

覆盖本轮代码优化（消除副作用、单一数据源、性能与可读性的改进），
确保重构前后行为一致、不引入新 bug。

运行方式（无需第三方依赖，标准库 unittest）：
    python -m unittest tests.test_optimizations -v
"""
import os
import sys
import json
import tempfile
import unittest
from unittest import mock
from dataclasses import asdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import utils.ai_helper as A
import utils.config as C
import utils.loaders as L
import utils.fetcher as F
import utils.speedtest as S
import main as M


class TestAiHelper(unittest.TestCase):
    def test_province_regex_equivalence(self):
        """重构后的 _PROVINCE_RE 必须与原硬编码正则逐字等价。"""
        import re
        orig = re.compile(
            r'\s*(?:' + '|'.join(A.PROVINCE_NAMES) + r')\s*(?:电信|联通|移动|有线|广电|IPTV|iptv)\s*',
            re.IGNORECASE,
        )
        self.assertEqual(orig.pattern, A._PROVINCE_RE.pattern)
        self.assertEqual(orig.flags, A._PROVINCE_RE.flags)

    def test_batch_classify_equivalence_and_retry(self):
        """批量分类结果须与逐条调用一致；429 应触发重试。"""
        A.API_KEY = "dummy"
        A._CAT_CACHE.clear()

        def make_cat(name):
            if "CCTV" in name:
                return "📺央视频道"
            if "卫视" in name:
                return "📡卫视频道"
            return "📺其他频道"

        class Resp:
            def __init__(self, content, status=200):
                self.status_code = status
                self._c = content

            def json(self):
                return {"choices": [{"message": {"content": self._c}}]}

        def post(url, json=None, headers=None, timeout=5):
            p = json["messages"][-1]["content"]
            if "For each numbered channel" in p:
                block = p.split("Channels:\n", 1)[1]
                names = [l.split(". ", 1)[1] for l in block.splitlines() if ". " in l]
                return Resp("\n".join(f"{i + 1}. {make_cat(n)}" for i, n in enumerate(names)))
            name = p.split("Channel name: '", 1)[1].split("'", 1)[0]
            return Resp(make_cat(name))

        with mock.patch("utils.ai_helper.requests") as mreq:
            mreq.post = post
            single = {n: A.classify_channel(n) for n in ["CCTV-1", "湖南卫视", "某乱码台"]}
            batch = A.classify_channels_batch(["CCTV-1", "湖南卫视", "某乱码台"])
        self.assertEqual(single, batch)

        # 重试：连续 429 两次后成功
        A._CAT_CACHE.clear()
        calls = {"n": 0}

        def flaky(url, json=None, headers=None, timeout=5):
            calls["n"] += 1
            return Resp("x", status=429) if calls["n"] <= 2 else Resp(make_cat("CCTV-1"))

        with mock.patch("utils.ai_helper.requests") as mreq:
            mreq.post = flaky
            self.assertEqual(A.classify_channel("CCTV-1"), "📺央视频道")
            self.assertEqual(calls["n"], 3)


class TestLoaders(unittest.TestCase):
    def test_logo_index_lazy(self):
        """import 后不应存在模块级 LOGO_INDEX；首次调用才构建并缓存。"""
        self.assertFalse(hasattr(L, "LOGO_INDEX"))
        idx = L.get_logo_index()
        self.assertIsInstance(idx, dict)
        self.assertIs(L._LOGO_INDEX_CACHE, idx)


class TestFetcher(unittest.TestCase):
    def test_save_parse_results(self):
        """save_parse_results 须复现原内联写文件行为，且对已有条目去重。"""
        tmp = tempfile.mkdtemp()
        unm = os.path.join(tmp, "unmatched.txt")
        ali = os.path.join(tmp, "alias.txt")
        F.UNMATCHED_FILE = unm
        F.ALIAS_FILE = ali

        F.save_parse_results({"芒果TV", "CCTV-9"}, {"CCTV-1": {"CCTV1", "ChinaTV"}})
        self.assertTrue(os.path.exists(unm))
        content = open(unm, encoding="utf-8").read()
        self.assertIn("芒果TV", content)
        self.assertIn("2 个频道", content)
        with open(ali, encoding="utf-8") as fh:
            self.assertIn("CCTV-1,ChinaTV,CCTV1", fh.read())

        if os.path.exists(unm):
            os.remove(unm)
        F.save_parse_results(set(), {})
        self.assertFalse(os.path.exists(unm))

        with open(ali, "w", encoding="utf-8") as fh:
            fh.write("CCTV-1,CCTV1\n")
        F.save_parse_results(set(), {"CCTV-1": {"CCTV1"}})
        with open(ali, encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), "CCTV-1,CCTV1")


class TestSpeedtest(unittest.TestCase):
    def test_append_auto_blacklist_dedup(self):
        tmp = tempfile.mkdtemp()
        bl = os.path.join(tmp, "bl.txt")
        S.BLACKLIST_FILE = bl
        S.append_auto_blacklist(["X", "Y"])
        S.append_auto_blacklist(["Y", "Z"])  # Y 重复
        with open(bl, encoding="utf-8") as fh:
            content = fh.read()
        self.assertEqual(content.count("Y"), 1)
        self.assertIn("X", content)
        self.assertIn("Z", content)

    def test_log_sampling(self):
        """成功日志采样（文件仍完整），失败日志全部打印。"""
        events = []

        def fake_live(msg):
            if "🟢" in msg:
                events.append("ok")
            elif "🔴" in msg:
                events.append("fail")

        S.live_print = fake_live
        S.check_channel = (
            lambda n, u: (False, n, u, 1.0, "连接超时") if n == "BAD"
            else (True, n, u, 1.0, "TS流(10.0Mbps)")
        )
        to_test = [(f"CH{i}", f"http://h/{i}") for i in range(20)] + [("BAD", "http://h/b")]
        _, ls, lf, _, _ = S.run_speed_test(to_test)
        self.assertEqual(len(ls), 20)
        self.assertEqual(len(lf), 1)
        self.assertLessEqual(events.count("ok"), S.SUCCESS_LOG_SAMPLE_LIMIT)
        self.assertEqual(events.count("fail"), 1)

    def test_probe_resolution_pipe(self):
        """单次下载管道模式：ffprobe 不可用时 (0,0)；可用时解析管道字节。"""
        S._ffprobe_checked = True
        S._ffprobe_available = False
        self.assertEqual(S.probe_resolution("x"), (0, 0))
        S._ffprobe_available = True

        captured = []

        class FakeResp:
            def __init__(self, chunks):
                self._chunks = chunks
                self.status_code = 200
                self.closed = False

            def __enter__(self):
                return self

            def __exit__(self, *a):
                self.closed = True

            def iter_content(self, chunk_size=65536):
                for c in self._chunks:
                    yield c

        class FakeStdin:
            def write(self, c):
                captured.append(c)

            def close(self):
                pass

        class FakeProc:
            def __init__(self, *a, **k):
                self.stdin = FakeStdin()

            def communicate(self, timeout=None):
                return ('{"streams":[{"width":1920,"height":1080}]}', "")

            def kill(self):
                pass

        S.get_session = lambda: type("S", (), {"get": lambda self, u, **k: FakeResp([b"x" * 65536, b"y" * 65536])})()
        captured_kwargs = {}
        def fake_popen(*a, **k):
            captured_kwargs.update(k)
            return FakeProc()
        S.subprocess.Popen = fake_popen
        self.assertEqual(S.probe_resolution("x"), (1920, 1080))
        self.assertEqual(sum(len(c) for c in captured), 128 * 1024)
        # 回归 #1：stdin 必须以二进制模式打开（不能 text=True），否则往 stdin 写 bytes 会抛 TypeError
        self.assertNotIn("text", captured_kwargs)
        self.assertNotEqual(captured_kwargs.get("text"), True)


class TestConfig(unittest.TestCase):
    def test_category_rules_match_json(self):
        """CATEGORY_RULES 须与 config/rules.json 逐条一致（外置单一来源）。"""
        data = json.load(open(os.path.join(ROOT, "config", "rules.json"), encoding="utf-8"))
        expected = [(tuple(i[0]), str(i[1]), int(i[2])) for i in data]
        self.assertEqual(C.CATEGORY_RULES, expected)


class TestMainCIState(unittest.TestCase):
    def _sample_state(self) -> "M.CIState":
        return M.CIState(
            url_to_source={"http://a/1": "http://src/a", "http://b/2": "http://src/b"},
            valid_results={"CCTV-1": [("http://a/1", 0.5)], "湖南卫视": [("http://b/2", 0.7)]},
            to_test=[("CCTV-1", "http://a/1"), ("湖南卫视", "http://b/2")],
            logs_blacklist=["bad"],
            logs_whitelist=["ok"],
            adult_results={"成人台": [("http://x", 1.0)]},
            adult_source_urls={"http://x", "http://y"},
            cat_order=["央视频道", "卫视频道"],
            chan_to_cat={"CCTV-1": "央视频道"},
            chans_in_cat={"央视频道": ["CCTV-1"]},
            channel_to_station={"CCTV-1": "CCTV"},
            channel_model={"CCTV-1": "model1"},
            epg_report={"channels": 5},
            start_time=1700000000.0,
            resolution_map={"http://a/1": (1920, 1080)},
            logs_success=[("CCTV-1", "http://a/1")],
            logs_fail=[("BAD", "http://bad", "timeout")],
            fail_counts={"timeout": 1},
            source_stats={"ok": {"src/a": 1}, "total": {"src/a": 1}},
        )

    def _old_ser(self, obj):
        "复刻重构前 main.py 的 _ser，用于与新的 asdict 序列化做等价对比。"
        if isinstance(obj, (set, tuple)):
            return list(obj)
        if isinstance(obj, dict):
            return {k: self._old_ser(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._old_ser(v) for v in obj]
        return obj

    def test_serialization_matches_old(self):
        """新 _save_state（asdict）的序列化结果须与旧手写字典逐字段等价。

        关键：旧 _ser 会把 tuple→list、set→list；JSON 往返后同理。
        """
        st = self._sample_state()
        new_blob = asdict(st)
        new_blob["adult_source_urls"] = list(new_blob["adult_source_urls"])
        new_blob["_version"] = 1
        old = {
            "url_to_source": st.url_to_source, "valid_results": st.valid_results,
            "to_test": st.to_test, "logs_blacklist": st.logs_blacklist,
            "logs_whitelist": st.logs_whitelist, "adult_results": st.adult_results,
            "adult_source_urls": list(st.adult_source_urls), "cat_order": st.cat_order,
            "chan_to_cat": st.chan_to_cat, "chans_in_cat": st.chans_in_cat,
            "channel_to_station": st.channel_to_station, "channel_model": st.channel_model,
            "epg_report": st.epg_report, "start_time": st.start_time,
            "resolution_map": st.resolution_map, "logs_success": st.logs_success,
            "logs_fail": st.logs_fail, "fail_counts": st.fail_counts,
            "source_stats": st.source_stats, "_version": 1,
        }
        # 经 _ser 归一化后两份结构必须完全一致（证明序列化等价，无遗漏/多余）
        self.assertEqual(self._old_ser(old), self._old_ser(new_blob))

        # 反序列化：set 字段须还原为 set，且其余字段逻辑等价（tuple→list 同旧行为）
        restored = M.CIState.from_dict(json.loads(json.dumps(new_blob, ensure_ascii=False)))
        self.assertIsInstance(restored.adult_source_urls, set)
        self.assertEqual(restored.adult_source_urls, {"http://x", "http://y"})
        self.assertEqual(
            restored.valid_results,
            {"CCTV-1": [["http://a/1", 0.5]], "湖南卫视": [["http://b/2", 0.7]]},
        )

    def test_asdict_matches_old_format(self):
        """asdict 生成的字段键集合须与原手写字典一致（无遗漏/无多余）。"""
        st = self._sample_state()
        keys = set(asdict(st).keys())
        self.assertEqual(
            keys,
            {
                "url_to_source", "valid_results", "to_test", "logs_blacklist",
                "logs_whitelist", "adult_results", "adult_source_urls", "cat_order",
                "chan_to_cat", "chans_in_cat", "channel_to_station", "channel_model",
                "epg_report", "start_time", "resolution_map", "logs_success",
                "logs_fail", "fail_counts", "source_stats",
            },
        )

    def test_from_dict_ignores_unknown_and_fills_missing(self):
        """from_dict 忽略未知字段、缺失字段用默认值补齐（向前兼容旧状态）。"""
        raw = {
            "valid_results": {"CCTV-1": []},
            "adult_source_urls": ["http://x", "http://x"],  # 列表须还原为 set
            "mystery_field": "drop me",
        }
        st = M.CIState.from_dict(raw)
        self.assertEqual(st.valid_results, {"CCTV-1": []})
        self.assertEqual(st.adult_source_urls, {"http://x"})
        self.assertEqual(st.to_test, [])          # 缺失 → 默认工厂
        self.assertEqual(st.resolution_map, {})    # 阶段1 状态不含阶段2 字段
        self.assertIsNone(st.epg_report)


class TestAiStandardize(unittest.TestCase):
    def setUp(self):
        A.API_KEY = "dummy"
        A._CACHE.clear()

    def test_standardize_uses_retry(self):
        """standardize_channel_name 须走 _post_with_retry（限流+退避），而非裸 requests.post。"""
        class Resp:
            status_code = 200
            def json(self):
                return {"choices": [{"message": {"content": "CCTV-1"}}]}

        captured = {}
        def fake_retry(payload, headers, timeout, max_retries=3):
            captured["called"] = True
            captured["timeout"] = timeout
            return Resp()
        with mock.patch.object(A, "_post_with_retry", fake_retry):
            out = A.standardize_channel_name("CCTV-1 超清 广东电信")
        self.assertTrue(captured.get("called"))
        self.assertEqual(captured.get("timeout"), 5)
        self.assertEqual(out, "CCTV-1")

    def test_standardize_degrade_on_retry_exhausted(self):
        """_post_with_retry 返回 None（限流耗尽/异常）时须降级到规则预处理结果。"""
        with mock.patch.object(A, "_post_with_retry", lambda *a, **k: None):
            out = A.standardize_channel_name("CCTV-1 超清 广东电信")
        # 规则预处理会去掉 超清/广东电信 → 'CCTV-1'
        self.assertEqual(out, "CCTV-1")


class TestCategorizerSort(unittest.TestCase):
    def test_sort_key_disables_ai(self):
        """排序用 channel_sort_key 必须 use_ai=False，避免冗余单条 AI 调用。"""
        import utils.categorizer as CAT
        A.API_KEY = ""  # 即便误触发 AI 也直接降级，测试不依赖网络
        seen = {}
        orig = CAT._match_category
        def spy(name, demo_rules=None, channel_model=None, use_ai=True):
            seen["use_ai"] = use_ai
            return orig(name, demo_rules, channel_model, use_ai=use_ai)
        with mock.patch.object(CAT, "_match_category", spy):
            CAT.channel_sort_key("CCTV-1", None, use_ai=False)
        self.assertIs(seen.get("use_ai"), False)

    def test_sort_key_default_uses_ai(self):
        """默认仍走 AI（保留向后兼容语义），仅排序调用显式关闭。"""
        import utils.categorizer as CAT
        A.API_KEY = ""
        seen = {}
        orig = CAT._match_category
        def spy(name, demo_rules=None, channel_model=None, use_ai=True):
            seen["use_ai"] = use_ai
            return orig(name, demo_rules, channel_model, use_ai=use_ai)
        with mock.patch.object(CAT, "_match_category", spy):
            CAT.channel_sort_key("CCTV-1", None)
        self.assertIs(seen.get("use_ai"), True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
