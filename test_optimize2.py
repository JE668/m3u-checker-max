#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""m3u-checker-max 第二轮优化验证测试"""

# 1. _NUM_RE 统一从 config 导入
from utils.config import _NUM_RE as CFG_NUM_RE
from utils.categorizer import _NUM_RE as CAT_NUM_RE
from utils.speedtest import _NUM_RE as ST_NUM_RE
assert CFG_NUM_RE is CAT_NUM_RE, "categorizer._NUM_RE should be identical to config._NUM_RE"
assert CFG_NUM_RE is ST_NUM_RE, "speedtest._NUM_RE should be identical to config._NUM_RE"
print("✅ _NUM_RE 统一从 config 导入（3模块共享同一实例）")

# 2. _CATEGORY_RULES_SORTED 预排序
from utils.categorizer import _CATEGORY_RULES_SORTED
from utils.config import CATEGORY_RULES
assert len(_CATEGORY_RULES_SORTED) == len(CATEGORY_RULES)
assert _CATEGORY_RULES_SORTED[0][2] <= _CATEGORY_RULES_SORTED[-1][2], "Should be sorted by priority"
print("✅ _CATEGORY_RULES_SORTED 预排序正确")

# 3. _match_category 按优先级返回（找到即退出）
from utils.categorizer import _match_category
cat, pri = _match_category("CCTV-1")
assert cat.startswith("📺央视"), f"CCTV-1 should be 央视, got {cat}"
assert pri == 1, f"CCTV-1 priority should be 1, got {pri}"
cat, pri = _match_category("湖南卫视-4K")
assert cat.startswith("📡卫视"), f"湖南卫视-4K should be 卫视, got {cat}"
cat, pri = _match_category("纯4K频道")
assert cat.startswith("☘️4K"), f"纯4K should be 4K, got {cat}"
print("✅ _match_category 按优先级排序后找到即返回")

# 4. fetcher.py _resolve_name 提取公共函数
from utils.fetcher import fetch_and_parse_channels
import inspect
src = inspect.getsource(fetch_and_parse_channels)
assert "_resolve_name" in src, "_resolve_name should be extracted"
assert src.count("_ai_fallback") <= 2, "_ai_fallback should appear at most twice (_resolve_name + unmatched)"
print("✅ fetcher.py AI兜底逻辑提取为 _resolve_name 公共函数")

# 5. get_session trust_env=False
from utils.config import get_session
s = get_session()
assert s.trust_env == False, "Session should have trust_env=False"
print("✅ get_session trust_env=False (避免CI代理干扰)")

# 6. retry_request functools.wraps
from utils.config import retry_request
import functools

@retry_request(max_attempts=1)
def dummy_func():
    pass
assert dummy_func.__name__ == "dummy_func", f"Function name should be preserved, got {dummy_func.__name__}"
print("✅ retry_request 使用 functools.wraps 保留函数名")

# 7. speedtest TS 校验优化 — bytearray 预分配
from utils.speedtest import check_channel
src = inspect.getsource(check_channel)
assert "3760" in src, "TS check buffer should be 3760 bytes (20×188)"
assert "ts_offset" in src, "ts_offset tracking variable should exist"
assert "512 * 1024" not in src, "Old 512KB buffer should be removed"
print("✅ speedtest TS 校验优化为预分配 3760 字节")

# 8. epg.py 使用 fetch_url（带重试）
from utils.epg import _download_single_epg
src = inspect.getsource(_download_single_epg)
assert "fetch_url" in src, "EPG should use fetch_url (with retry)"
assert "get_session().get" not in src, "EPG should not use raw get_session().get"
print("✅ epg.py 使用 fetch_url（带重试）")

# 9. output.py 成人内容异常处理
from utils.output import write_outputs
src = inspect.getsource(write_outputs)
assert src.count("except OSError") >= 2, "write_outputs should have 2+ OSError handlers"
print("✅ output.py 成人内容写入异常处理已添加")

# 10. 性能测试：_match_category
import timeit
names = ['CCTV-1', 'CCTV-5+', 'CCTV-13 新闻', '湖南卫视', '湖南卫视-4K', '广东体育',
         '浙江卫视', '江苏卫视', 'CCTV-4K', '东方卫视 HD', '北京卫视', '深圳频道',
         '广州综合', '凤凰资讯', '翡翠台', 'MTV音乐', 'CHC家庭影院', 'NewTV超级电影',
         'SiTV欢笑剧场', 'ihot typen'] * 150
t = timeit.timeit(lambda: [_match_category(n) for n in names], number=5)
print(f"✅ _match_category 性能: {t/5*1000:.1f}ms per 3000 calls")

# 11. import main 无错误
import main
print("✅ import main 通过")

print()
print("━━━ 🎉 全部 11 项优化验证通过 ━━━")
