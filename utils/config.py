import atexit
import concurrent.futures
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from typing import Tuple

import requests


# 预编译正则：频道名排序用数字提取（categorizer.py 和 speedtest.py 共享）
_NUM_RE = re.compile(r'\d+')

try:
    from utils.ai_helper import get_cache_stats, standardize_channel_name  # noqa: F401
    _AI_AVAILABLE = True
except ImportError:
    def standardize_channel_name(x):
        return x
    def get_cache_stats():
        return {}
    _AI_AVAILABLE = False

def _dedup_blacklist():
    """启动时对 blacklist.txt 做一次性去重，防止每次 CI 追加导致的无限膨胀"""
    bl_path = BLACKLIST_FILE
    if not os.path.exists(bl_path):
        return
    with open(bl_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    seen = set()
    new_lines = []
    changed = False
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            new_lines.append(line)
        elif stripped not in seen:
            seen.add(stripped)
            new_lines.append(line)
        else:
            changed = True
    if changed:
        while new_lines and new_lines[-1].strip() == '':
            new_lines.pop()
        with open(bl_path, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)
        live_print("  🧹 blacklist.txt 去重完成")

# ===============================
# 1.2 AI 辅助标准化：持久化缓存
# ===============================
AI_CACHE_FILE = "output/ai_cache.json"

def _load_ai_cache():
    """从磁盘加载 AI 标准化缓存"""
    if not os.path.exists(AI_CACHE_FILE):
        return {}
    try:
        with open(AI_CACHE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

def _save_ai_cache(cache):
    """保存 AI 标准化缓存到磁盘"""
    try:
        os.makedirs(os.path.dirname(AI_CACHE_FILE), exist_ok=True)
        with open(AI_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except OSError:
        pass

def _ai_fallback(raw_name, ai_cache):
    """AI 兜底：当 alias.txt 无法匹配时，尝试 AI 标准化"""
    if not _AI_AVAILABLE or not raw_name:
        return raw_name, False
    # 先查持久化缓存
    if raw_name in ai_cache:
        return ai_cache[raw_name], False
    # 调用 AI
    result = standardize_channel_name(raw_name)
    if result and result != raw_name:
        ai_cache[raw_name] = result
        return result, True  # True = AI 做了修改
    ai_cache[raw_name] = raw_name
    return raw_name, False

# ===============================
# 1. 核心配置区 — 从 config/settings.py 加载
# ===============================
try:
    from config.settings import (  # noqa: F401, F811  (settings.py 为配置覆盖来源；部分名在下方有兜底默认值，属有意再导出/重定义)
        ADULT_KEYWORDS,
        ADULT_M3U,
        ADULT_SOURCES_FILE,
        ADULT_TXT,
        ALIAS_FILE,
        BLACKLIST_FILE,
        CDN_BASE,
        CHANNEL_MODEL_FILE,
        CHECK_CONNECT_TIMEOUT,
        CHECK_READ_TIMEOUT,
        CHECK_TOTAL_TIMEOUT,
        DEFAULT_HEADERS,
        DEMO_FILE,
        DOWNLOAD_TARGET_BYTES,
        EPG_BLACKLIST,
        EPG_FILE,
        EPG_KEEP_DAYS,
        EPG_MAX_WORKERS,
        ICON_DIR,
        ICONS_INDEX_FILE,
        INVALID_NAME_PATTERNS,
        LOG_FILE,
        MAX_WORKERS,
        MIN_BANDWIDTH_MBPS,
        MIN_RESOLUTION,
        NON_TV_LOG,
        NON_TV_PATTERNS,
        OUTPUT_EPG,
        OUTPUT_EPG_GZ,
        OUTPUT_M3U,
        OUTPUT_TXT,
        PROBE_RESOLUTION,
        PROBE_TIMEOUT,
        RETRY_BACKOFF,
        RETRY_MAX_ATTEMPTS,
        SAMPLE_PER_HOST,
        SOURCE_CAT_FILE,
        SOURCES_FILE,
        UNMATCHED_FILE,
        WHITELIST_FILE,
    )
except ImportError:
    pass  # 无 settings.py 时用下方默认值

# ── 环境变量覆盖（CI 场景，优先级最高） ──
_CFG_ENV = {  # (key, converter, default)
    "MAX_WORKERS": (int, 50),
    "SAMPLE_PER_HOST": (int, 2),
    "CHECK_CONNECT_TIMEOUT": (int, 5),
    "CHECK_READ_TIMEOUT": (int, 8),
    "CHECK_TOTAL_TIMEOUT": (int, 15),
    "MIN_BANDWIDTH_MBPS": (float, 2.0),
    "EPG_MAX_WORKERS": (int, 4),
    "EPG_KEEP_DAYS": (int, 3),
    "RETRY_MAX_ATTEMPTS": (int, 2),
    "RETRY_BACKOFF": (float, 1.0),
    "PROBE_TIMEOUT": (int, 4),
    "SUCCESS_LOG_SAMPLE_LIMIT": (int, 15),
    "CDN_BASE": (str, "https://gh.felicity.ac.cn"),
}
for _key, (_conv, _default) in _CFG_ENV.items():
    if _key in os.environ:
        globals()[_key] = _conv(os.environ[_key])
    elif _key not in globals():
        globals()[_key] = _default

# ── 布尔/env 特化覆盖 ──
if "PROBE_RESOLUTION" in os.environ:
    PROBE_RESOLUTION = os.environ["PROBE_RESOLUTION"].lower() in ("1", "true", "yes")
if "MIN_RESOLUTION" in os.environ:
    MIN_RESOLUTION = os.environ["MIN_RESOLUTION"]

# ── 派生值 ──
REPO_RAW = f"{CDN_BASE}/https://raw.githubusercontent.com/JE668/m3u-checker-max/main"
M3U_HEADER = f'#EXTM3U x-tvg-url="{REPO_RAW}/output/epg.xml.gz"\n'
SOURCE_META_URL = f"{CDN_BASE}/https://raw.githubusercontent.com/JE668/get-m3u/refs/heads/main/output/source-meta.json"
if 'DOWNLOAD_TARGET_BYTES' not in globals():
    DOWNLOAD_TARGET_BYTES = 1048576

# ── 分辨率辅助函数（纯逻辑，非配置） ──
def _parse_resolution(s: str) -> Tuple[int, int]:
    try:
        w, h = s.lower().split("x")
        return int(w.strip()), int(h.strip())
    except (ValueError, AttributeError):
        return 0, 0
MIN_RESOLUTION_WH = _parse_resolution(MIN_RESOLUTION)
MIN_RESOLUTION_PIXELS = MIN_RESOLUTION_WH[0] * MIN_RESOLUTION_WH[1]

def fmt_resolution(w: int, h: int) -> str:
    """格式化分辨率显示"""
    if w >= 3840 and h >= 2160:
        return "4K"
    elif w >= 1920 and h >= 1080:
        return "1080p"
    elif w >= 1280 and h >= 720:
        return "720p"
    elif w >= 720 and h >= 576:
        return "576p"
    elif w >= 640 and h >= 480:
        return "480p"
    elif w > 0:
        return f"{w}x{h}"
    return "未知"

# ── GitHub Actions Job Summary ──
SUMMARY_FILE = os.environ.get("GITHUB_STEP_SUMMARY", "")
_SUMMARY_BUFFER = []  # 缓冲，阶段结束时由 flush_summary() 一次性写出，减少 IO 次数

def write_summary(content):
    """追加到 Step Summary 缓冲（仅 GitHub Actions 环境生效）。
    累计到 _SUMMARY_BUFFER，由 flush_summary() 在阶段结束时一次性写出。"""
    if not SUMMARY_FILE:
        return
    _SUMMARY_BUFFER.append(content + "\n")

def write_summary_table(headers, rows):
    """追加 Markdown 表格到 Step Summary 缓冲"""
    if not SUMMARY_FILE:
        return
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(c) for c in row) + " |")
    write_summary("\n".join(lines))

def flush_summary():
    """将缓冲的 Step Summary 一次性写入文件（减少 IO；错误打到 stderr 可见，而非静默吞掉）"""
    if not SUMMARY_FILE or not _SUMMARY_BUFFER:
        return
    try:
        with open(SUMMARY_FILE, "a", encoding="utf-8") as f:
            f.write("".join(_SUMMARY_BUFFER))
    except OSError as e:
        print(f"⚠️ 写入 GITHUB_STEP_SUMMARY 失败: {e}", file=sys.stderr, flush=True)
    _SUMMARY_BUFFER.clear()

RULES_FILE = os.path.join("config", "rules.json")

def _load_category_rules():
    """从 config/rules.json 加载分类规则（单一可维护来源）。

    结构: [[keywords_list, category_name, priority], ...]
    加载失败（文件缺失/损坏/格式错误）时显式抛出 RuntimeError（fail-fast），
    避免静默回退到错误规则导致分类结果偏差。
    """
    if not os.path.exists(RULES_FILE):
        raise RuntimeError(f"分类规则文件缺失: {RULES_FILE}（请从仓库恢复该文件）")
    try:
        with open(RULES_FILE, encoding="utf-8") as _f:
            _data = json.load(_f)
    except (json.JSONDecodeError, OSError) as _e:
        raise RuntimeError(f"分类规则文件解析失败: {RULES_FILE} — {_e}") from _e
    _rules = []
    for _i, _item in enumerate(_data):
        if not (isinstance(_item, (list, tuple)) and len(_item) == 3):
            raise RuntimeError(f"分类规则格式错误 (第 {_i} 条): {_item!r}")
        _kw, _name, _pri = _item
        _rules.append((tuple(_kw), str(_name), int(_pri)))
    if not _rules:
        raise RuntimeError(f"分类规则文件为空: {RULES_FILE}")
    return _rules

CATEGORY_RULES = _load_category_rules()

DEFAULT_CATEGORY = ("📺其他频道", 8)

os.makedirs("output", exist_ok=True)
os.makedirs("config", exist_ok=True)
os.makedirs(ICON_DIR, exist_ok=True)

# P1-12: 全局 Session 复用（同一域名复用 TCP 连接 + SSL 会话）
_http_session = None

_SHARED_POOL = None

def get_pool() -> concurrent.futures.ThreadPoolExecutor:
    """全局共享线程池（测速 + 分辨率检测复用），减少线程反复创建销毁"""
    global _SHARED_POOL
    if _SHARED_POOL is None:
        _SHARED_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="m3u")
    return _SHARED_POOL

# 程序退出时自动清理全局线程池
def _cleanup_pool():
    global _SHARED_POOL
    if _SHARED_POOL:
        _SHARED_POOL.shutdown(wait=False)
        _SHARED_POOL = None
atexit.register(_cleanup_pool)

def get_session() -> requests.Session:
    global _http_session
    if _http_session is None:
        _http_session = requests.Session()
        _http_session.trust_env = False  # CI 环境 dotenv 代理干扰
        _http_session.headers.update(DEFAULT_HEADERS)
        # 连接池大小匹配并发度
        adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=MAX_WORKERS)
        _http_session.mount("http://", adapter)
        _http_session.mount("https://", adapter)
    return _http_session

def _validate_configs():
    """启动时验证所有必要配置文件的存在与基本完整性"""
    issues = []

    checks = [
        (SOURCES_FILE, True, "直播源"),
        (EPG_FILE, True, "EPG"),
        (ALIAS_FILE, True, "别名"),
        (DEMO_FILE, True, "分类模板"),
        (BLACKLIST_FILE, False, "黑名单"),
        (WHITELIST_FILE, False, "白名单"),
        (ADULT_SOURCES_FILE, False, "限制级来源"),
        (SOURCE_CAT_FILE, False, "来源分类映射"),
        (CHANNEL_MODEL_FILE, False, "频道模型"),
        (ICONS_INDEX_FILE, False, "图标索引"),
    ]

    for path, required, label in checks:
        exists = os.path.exists(path)
        if not exists and required:
            issues.append(f"❌ 缺失必要配置 [{label}]: {path}")
        elif exists:
            # 对必要配置做基本内容检查
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    first_line = f.readline().strip()
                if not first_line:
                    if required:
                        issues.append(f"⚠️ 配置文件为空 [{label}]: {path}")
                elif label == "别名":
                    # 检查别名文件是否至少有一行有效数据
                    has_data = False
                    with open(path, 'r', encoding='utf-8') as f:
                        for line in f:
                            s = line.strip()
                            if s and not s.startswith('#') and ',' in s:
                                has_data = True
                                break
                    if not has_data:
                        issues.append(f"⚠️ 别名文件无有效数据: {path}")
                elif label == "分类模板":
                    has_genre = False
                    with open(path, 'r', encoding='utf-8') as f:
                        for line in f:
                            if '#genre#' in line:
                                has_genre = True
                                break
                    if not has_genre:
                        issues.append(f"⚠️ 分类模板缺少 #genre# 分类行: {path}")
            except (OSError, UnicodeDecodeError) as e:
                issues.append(f"⚠️ 无法读取 [{label}]: {path} — {e}")
        elif not required and not exists:
            live_print(f"  ℹ️ 可选配置不存在 [{label}]: {path}（不影响运行）")

    if issues:
        live_print("\n━━━ ⚙️ 配置验证结果 ━━━━━━━━━━━━━━━━━━━")
        for issue in issues:
            live_print(f"  {issue}")
        live_print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n")
        if any(i.startswith("❌") for i in issues):
            live_print("🚨 必要配置文件缺失，请检查后重试")
            return False
    else:
        live_print("  ✅ 配置文件验证通过")
    return True

def live_print(content):
    """输出到 stderr（GitHub Actions 实时流式）+ 自动刷新"""
    print(content, flush=True, file=sys.stderr)

@contextmanager
def ci_group(title):
    """将一组 CI 日志折叠为可点击分组（GitHub Actions ::group:: 命令）。

    约束：GitHub Actions 不支持嵌套 group，因此阶段级与子任务级分组必须
    平级出现，绝不可在阶段 group 内再开启子 group（嵌套的 ::group:: 会被
    忽略，内层折叠失效）。本地运行（无 GITHUB_ACTIONS）退化为醒目的分隔行，
    保持日志可读性。
    """
    in_ci = os.environ.get("GITHUB_ACTIONS") == "true"
    if in_ci:
        # stdout：GitHub Actions 工作流命令在 stdout 识别最稳定
        print(f"::group::{title}", flush=True)
    else:
        live_print(f"\n▌ {title}")
    try:
        yield
    finally:
        if in_ci:
            print("::endgroup::", flush=True)
        else:
            live_print("")

# ===============================
# 1.5 网络工具：重试装饰器 (P1-6)
# ===============================
def retry_request(max_attempts: int = RETRY_MAX_ATTEMPTS, backoff: float = RETRY_BACKOFF):
    """对 requests 调用添加指数退避重试"""
    import functools
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except (requests.exceptions.Timeout,
                        requests.exceptions.ConnectionError,
                        requests.exceptions.ChunkedEncodingError) as e:
                    last_exc = e
                    if attempt < max_attempts:
                        wait = backoff * (2 ** (attempt - 1))
                        live_print(f"  ⏳ 重试 ({attempt}/{max_attempts})，{wait:.1f}s 后重试: {e}")
                        time.sleep(wait)
            raise last_exc
        return wrapper
    return decorator

def fetch_url(url: str, timeout: int = 10) -> requests.Response:
    """带重试的 URL 获取（封装 retry_request 提升可读性）"""
    return retry_request()(lambda u: get_session().get(u, timeout=timeout))(url)

# ===============================
