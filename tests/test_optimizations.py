"""m3u-checker-max 优化项回归测试。

覆盖本轮代码优化（消除副作用、单一数据源、性能与可读性的改进），
确保重构前后行为一致、不引入新 bug。

运行方式（无需第三方依赖，标准库 unittest）：
    python -m unittest tests.test_optimizations -v
"""
import io
import os
import sys
import json
import tempfile
import unittest
from unittest import mock
from dataclasses import asdict
from contextlib import redirect_stdout, redirect_stderr

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
            adult_results={"限制级台": [("http://x", 1.0)]},
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


class TestAutoUpdateDemo(unittest.TestCase):
    def test_match_category_called_once_per_channel(self):
        """auto_update_demo 对每个频道只调一次 _match_category（第二趟复用缓存）。

        回归：重构前第一趟(收集待分类) + 第二趟(算结果) 对同一频道调了两次 _match_category，
        属冗余计算。重构后第二趟读 rule_cat 缓存，每个新频道应恰好被匹配一次（无重复）。
        """
        import utils.categorizer as CAT
        chans_in_cat = {"📺央视频道,#genre#": ["CCTV-1"]}
        cat_order = ["📺央视频道,#genre#"]
        chan_to_cat = {"CCTV-1": "📺央视频道,#genre#"}
        valid_names = {"CCTV-1": 1.0, "湖南卫视": 1.0, "某乱码台": 1.0}

        tmp = tempfile.mkdtemp()
        CAT.DEMO_FILE = os.path.join(tmp, "demo.txt")
        CAT.NON_TV_LOG = os.path.join(tmp, "non-tv-filtered.txt")
        with open(CAT.DEMO_FILE, "w", encoding="utf-8") as f:
            f.write("📺央视频道,#genre#\nCCTV-1\n")

        real_match = CAT._match_category
        calls = []
        def spy(name, *a, **k):
            calls.append(name)
            return real_match(name, *a, **k)

        new_channels = [n for n in valid_names if n not in chan_to_cat]

        with mock.patch.object(CAT, "_match_category", spy), \
             mock.patch.object(CAT, "classify_channels_batch", return_value={}):
            CAT.auto_update_demo(valid_names, cat_order, chan_to_cat, chans_in_cat, channel_model={})

        # 调用构成：第一趟规则匹配(每新频道1次) + 末尾排序(channel_sort_key 需优先级,每新频道1次)。
        # 第二轮(原 line335)已改为复用 rule_cat 缓存，不再重复调 _match_category。
        # 因此总数须为 2×新频道数；若缓存修复被回退(第二轮再调)，则变 3× 而失败。
        self.assertEqual(
            len(calls), 2 * len(new_channels),
            f"_match_category 应恰好 2×新频道数(第一趟+排序)，实际调用序列 {calls}",
        )


class TestSummaryBuffer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "summary.md")
        C._SUMMARY_BUFFER.clear()
        self._patch = mock.patch.object(C, "SUMMARY_FILE", self.path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        C._SUMMARY_BUFFER.clear()

    def test_write_summary_buffers_until_flush(self):
        """write_summary 须缓冲（不立即落盘），flush_summary 才一次性写出。"""
        C.write_summary("hello")
        self.assertFalse(os.path.exists(self.path), "flush 前应仍未落盘")
        C.flush_summary()
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "hello\n")

    def test_buffer_cleared_after_flush(self):
        """flush 后缓冲清空，二次 flush 不应重复写出。"""
        C.write_summary("a")
        C.write_summary("b")
        C.flush_summary()
        C.flush_summary()
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "a\nb\n")

    def test_summary_table_renders_markdown(self):
        C.write_summary_table(["A", "B"], [["1", "2"]])
        C.flush_summary()
        with open(self.path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("| A | B |", content)
        self.assertIn("| --- | --- |", content)
        self.assertIn("| 1 | 2 |", content)

    def test_empty_buffer_flush_is_noop(self):
        """无内容时 flush 不得创建文件。"""
        C.flush_summary()
        self.assertFalse(os.path.exists(self.path))


class TestLogTxtSampling(unittest.TestCase):
    def _call_write_outputs(self, success_logs, n_channels):
        import utils.output as O
        tmp = tempfile.mkdtemp()
        names = [f"CH{i}" for i in range(n_channels)]
        valid_results = {n: [("http://example.com/stream", 1.0)] for n in names}
        cat_order = ["新闻,#genre#"]
        chans_in_cat = {"新闻,#genre#": names}
        log = os.path.join(tmp, "log.txt")
        patches = (
            mock.patch.object(O, "OUTPUT_M3U", os.path.join(tmp, "live.m3u")),
            mock.patch.object(O, "OUTPUT_TXT", os.path.join(tmp, "live.txt")),
            mock.patch.object(O, "LOG_FILE", log),
            mock.patch.object(O, "ADULT_M3U", os.path.join(tmp, "a.m3u")),
            mock.patch.object(O, "ADULT_TXT", os.path.join(tmp, "a.txt")),
            mock.patch.object(O, "M3U_HEADER", "#EXTM3U\n"),
            mock.patch.object(O, "MIN_RESOLUTION", "1920x1080"),
            mock.patch.object(O, "MIN_RESOLUTION_PIXELS", 1920 * 1080),
            mock.patch.object(O, "get_local_logo_url", lambda n: ""),
        )
        for p in patches:
            p.start()
        try:
            O.write_outputs(valid_results, cat_order, chans_in_cat, [], success_logs, [], [], [])
        finally:
            for p in patches:
                p.stop()
        with open(log, encoding="utf-8") as f:
            return f.read()

    def test_success_log_sampled_when_over_limit(self):
        """成功日志超过上限时须采样（仅前 N 条）+ 省略提示，避免每次提交膨胀仓库。"""
        content = self._call_write_outputs([f"🟢 成功 {i}" for i in range(20)], 20)
        self.assertIn("采样前 15/20 条", content)
        self.assertIn("🟢 成功 0", content)
        self.assertIn("🟢 成功 14", content)
        self.assertNotIn("🟢 成功 15", content)  # 第16条起被省略
        self.assertIn("其余 5 条已省略", content)

    def test_success_log_full_when_under_limit(self):
        """未超上限时成功日志全量写入（行为不变）。"""
        content = self._call_write_outputs([f"🟢 成功 {i}" for i in range(3)], 3)
        self.assertIn("🟢 成功 0", content)
        self.assertIn("🟢 成功 2", content)
        self.assertNotIn("采样前", content)


class TestAdultRewrite(unittest.TestCase):
    """方案B：只要配置了成人来源就强制重写 adult 文件，避免源挂掉时陈旧内容残留。"""

    def _call(self, adult_source_urls=None, adult_results=None):
        import utils.output as O
        tmp = tempfile.mkdtemp()
        valid_results = {"新闻": [("http://example.com/s", 1.0)]}
        cat_order = ["新闻"]
        chans_in_cat = {"新闻": ["新闻"]}
        patches = (
            mock.patch.object(O, "OUTPUT_M3U", os.path.join(tmp, "live.m3u")),
            mock.patch.object(O, "OUTPUT_TXT", os.path.join(tmp, "live.txt")),
            mock.patch.object(O, "LOG_FILE", os.path.join(tmp, "log.txt")),
            mock.patch.object(O, "ADULT_M3U", os.path.join(tmp, "a.m3u")),
            mock.patch.object(O, "ADULT_TXT", os.path.join(tmp, "a.txt")),
            mock.patch.object(O, "M3U_HEADER", "#EXTM3U\n"),
            mock.patch.object(O, "MIN_RESOLUTION", "1920x1080"),
            mock.patch.object(O, "MIN_RESOLUTION_PIXELS", 1920 * 1080),
            mock.patch.object(O, "get_local_logo_url", lambda n: ""),
        )
        for p in patches:
            p.start()
        try:
            O.write_outputs(valid_results, cat_order, chans_in_cat, [], [], [], [], [],
                            adult_results=adult_results, adult_source_urls=adult_source_urls)
        finally:
            for p in patches:
                p.stop()
        return os.path.join(tmp, "a.m3u"), os.path.join(tmp, "a.txt")

    def test_configured_but_no_live_rewrites_empty(self):
        """配置了成人来源但本次无存活频道 → adult 文件被重写为空列表（含表头），不残留陈旧内容。"""
        m3u, txt = self._call(adult_source_urls={"http://x/adult.m3u8"}, adult_results={})
        self.assertTrue(os.path.exists(m3u))
        self.assertTrue(os.path.exists(txt))
        with open(m3u, encoding="utf-8") as f:
            self.assertEqual(f.read(), "#EXTM3U\n")
        with open(txt, encoding="utf-8") as f:
            self.assertEqual(f.read(), "📛限制级内容,#genre#\n")

    def test_configured_with_live_writes_channels(self):
        """配置了成人来源且有存活频道 → 正常写入频道。"""
        m3u, txt = self._call(
            adult_source_urls={"http://x/adult.m3u8"},
            adult_results={"限制级台": [("http://x/adult.m3u8", 1.0)]},
        )
        with open(m3u, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("#EXTM3U\n", content)
        self.assertIn("http://x/adult.m3u8", content)
        self.assertIn("限制级台", content)

    def test_not_configured_leaves_files_untouched(self):
        """未配置成人来源(adult_source_urls 为空) → 不创建/不触碰 adult 文件。"""
        m3u, txt = self._call(adult_source_urls=set(), adult_results={})
        self.assertFalse(os.path.exists(m3u))
        self.assertFalse(os.path.exists(txt))


class TestConfigConstants(unittest.TestCase):
    def test_success_log_sample_limit_single_source(self):
        """SUCCESS_LOG_SAMPLE_LIMIT 现由 config 单一来源定义（默认 15，支持 env 覆盖）。"""
        self.assertEqual(C.SUCCESS_LOG_SAMPLE_LIMIT, 15)


class TestCIGroup(unittest.TestCase):
    def test_emits_group_commands_in_ci(self):
        """CI 环境(GITHUB_ACTIONS=true)下须输出 ::group::/::endgroup:: 到 stdout。"""
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            with redirect_stdout(buf):
                with C.ci_group("抓取直播源"):
                    pass
        out = buf.getvalue()
        self.assertIn("::group::抓取直播源", out)
        self.assertIn("::endgroup::", out)

    def test_local_fallback_prints_banner(self):
        """本地环境(无 GITHUB_ACTIONS)下退化为 stderr 分隔行。"""
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True):
            with redirect_stderr(buf):
                with C.ci_group("抓取直播源"):
                    pass
        err = buf.getvalue()
        self.assertIn("抓取直播源", err)

    def test_closes_group_on_exception(self):
        """分组内异常时仍须输出 ::endgroup::，保证分组正确闭合。"""
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            with redirect_stdout(buf):
                with self.assertRaises(ValueError):
                    with C.ci_group("会失败的分组"):
                        raise ValueError("boom")
        out = buf.getvalue()
        self.assertIn("::group::会失败的分组", out)
        self.assertIn("::endgroup::", out)


class TestRefactorCleanup(unittest.TestCase):
    """锁定 #6/#7 重构形态，防止回归。"""

    def test_fetch_returns_3_tuple_no_url_to_group(self):
        """#6: fetch_and_parse_channels 不再返回 url_to_group（死返回值已移除）"""
        import utils.fetcher as F
        import inspect
        src = inspect.getsource(F)
        self.assertNotIn("url_to_group", src)
        ann = F.fetch_and_parse_channels.__annotations__.get("return")
        self.assertIsNotNone(ann)
        # Tuple[list, Set[str], Dict[str, Set[str]]] 应含 3 个类型参数
        self.assertEqual(len(getattr(ann, "__args__", ())), 3)

    def test_auto_update_demo_first_param_renamed(self):
        """#7: auto_update_demo 第一个参数已从严误导的 valid_names 重命名为 valid_results"""
        import utils.categorizer as CAT
        import inspect
        params = list(inspect.signature(CAT.auto_update_demo).parameters)
        self.assertIn("valid_results", params)
        self.assertNotIn("valid_names", params)
        # 第五个可选参数已避开命名冲突，改名为 valid_results_opt
        self.assertIn("valid_results_opt", params)


if __name__ == "__main__":
    unittest.main(verbosity=2)
