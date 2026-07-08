import os, time, concurrent.futures, requests, re, json, sys, shutil, atexit
from collections import Counter, defaultdict
from typing import Optional, Tuple, List, Dict, Set, Any
from datetime import datetime

# 预编译正则：频道名排序用数字提取（categorizer.py 和 speedtest.py 共享）
_NUM_RE = re.compile(r'\d+')

try:
    from utils.ai_helper import standardize_channel_name, clear_cache, get_cache_stats, classify_channel
    _AI_AVAILABLE = True
except ImportError:
    standardize_channel_name = lambda x: x
    classify_channel = lambda x: ''
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
        live_print(f"  🧹 blacklist.txt 去重完成")

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
    from config.settings import *
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
    "RETRY_MAX_ATTEMPTS": (int, 2),
    "RETRY_BACKOFF": (float, 1.0),
    "PROBE_TIMEOUT": (int, 4),
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

def write_summary(content):
    """写入 GITHUB_STEP_SUMMARY（仅 GitHub Actions 环境生效）"""
    if SUMMARY_FILE:
        try:
            with open(SUMMARY_FILE, "a", encoding="utf-8") as f:
                f.write(content + "\n")
        except OSError:
            pass

CATEGORY_RULES = [
    # === 优先级 0：4K/8K 超高清 ===
    # 注意：4K 优先级设为 3.5（在卫视之后），避免"湖南卫视-4K"被归到4K而非卫视
    (["4K", "8K"], "☘️4K/8K超高清频道", 4),

    # === 优先级 1：国家级广播 ===
    (["CCTV"], "📺央视频道", 1),
    (["CETV"], "📺中国教育电视台", 1),
    (["CGTN"], "📺中国国际电视台", 1),
    (["春晚"], "🎉春晚频道", 1),

    # === 优先级 2：品牌/服务系列（字母均转大写匹配，解决大小写问题） ===
    (["IHOT"], "📺iHOT系列", 2),
    (["IPTV"], "📺IPTV系列", 2),
    (["NEWTV"], "📺NewTV系列", 2),
    (["CHC"], "📺CHC系列", 2),
    (["BESTV"], "📺BesTV系列", 2),
    (["SITV"], "📺SiTV系列", 2),

    # === 优先级 3：卫星频道 ===
    (["卫视"], "📡卫视频道", 3),

    # === 优先级 4：港·澳·台（J2, TVBS 等含字母的关键词需大写） ===
    (["凤凰", "翡翠", "明珠", "靖天", "东森", "三立", "TVBS", "港台",
      "无线新闻", "VIUTV", "纬来", "J2"], "🌊港·澳·台", 4),

    # === 优先级 5：省份频道（放在功能分类之前，确保"广东体育"归广东频道） ===
    (["广东", "广州", "深圳", "东莞", "中山", "佛山", "珠海", "汕头", "揭阳", "梅州",
      "惠州", "江门", "肇庆", "韶关", "河源", "清远", "湛江", "茂名", "阳江", "云浮",
      "潮州", "汕尾", "南国都市", "大湾区", "嘉佳卡通", "岭南"], "☘️广东频道", 5),
    (["湖南", "金鹰", "快乐垂钓", "长沙", "湘潭"], "☘️湖南频道", 5),
    (["浙江", "HZTV", "中国蓝", "杭州", "宁波", "温州", "嘉兴", "绍兴", "湖州",
      "金华", "台州", "舟山", "衢州", "丽水"], "☘️浙江频道", 5),
    (["湖北", "武汉", "荆门", "宜昌", "襄阳", "荆州", "黄石", "十堰", "孝感", "黄冈"], "☘️湖北频道", 5),
    (["河南", "郑州", "开封", "洛阳", "新乡", "安阳", "许昌", "平顶山", "南阳",
      "信阳", "驻马店", "商丘", "周口", "焦作"], "☘️河南频道", 5),
    (["河北", "石家庄", "邯郸", "衡水", "邢台", "秦皇岛", "沧州", "保定", "张家口",
      "承德", "廊坊", "唐山"], "☘️河北频道", 5),
    (["福建", "厦门", "福州", "泉州", "漳州", "莆田", "龙岩", "三明", "南平", "宁德"], "☘️福建频道", 5),
    (["安徽", "合肥", "芜湖", "蚌埠", "铜陵", "亳州", "六安", "滁州", "黄山"], "☘️安徽频道", 5),
    (["辽宁", "沈阳", "大连", "鞍山"], "☘️辽宁频道", 5),
    (["黑龙江", "哈尔滨", "齐齐哈尔", "佳木斯", "大庆", "鹤岗"], "☘️黑龙江频道", 5),
    (["吉林", "延边", "长春"], "☘️吉林频道", 5),
    (["陕西", "西安", "咸阳", "宝鸡"], "☘️陕西频道", 5),
    (["云南", "昆明"], "☘️云南频道", 5),
    (["贵州", "贵阳", "遵义"], "☘️贵州频道", 5),
    (["广西", "桂林", "南宁", "柳州", "梧州", "北海", "百色"], "☘️广西频道", 5),
    (["甘肃", "兰州"], "☘️甘肃频道", 5),
    (["内蒙古", "内蒙"], "☘️内蒙古频道", 5),
    (["海南", "三沙", "海口"], "☘️海南频道", 5),
    (["江苏", "南京", "苏州", "无锡", "常州", "镇江", "南通", "扬州", "徐州",
      "淮安", "盐城", "连云港", "泰州", "宿迁"], "☘️江苏频道", 5),
    (["江西", "南昌", "赣州", "九江"], "☘️江西频道", 5),

    # === 优先级 6：功能分类 ===
    # 体育类
    (["体育", "竞技", "围棋", "篮球", "乒羽", "足球", "电竞", "劲爆", "五星体育",
      "先锋乒羽", "魅力足球", "天元围棋", "睛彩", "健身"], "🏀体育频道", 6),
    # 电影类
    (["电影", "淘电影", "龙祥电影"], "🎥电影频道", 6),
    # 动画类
    (["动画", "动漫", "卡通", "少儿"], "🪁动画频道", 6),
    # 教育类
    (["教育", "早期教育", "卫生健康", "现代教育"], "📚教育频道", 6),

    # === 优先级 7：剧场与数字频道 ===
    (["剧场", "戏剧", "话剧"], "🎬剧场频道", 7),
    (["数字", "数码"], "📺数字频道", 7),

    # === 优先级 8：兜底专业频道 ===
    (["专业", "天气", "指南", "纪录", "纪实", "时尚", "文物", "武术", "生态环境",
      "环球", "全纪实", "梨园", "国学", "游戏风云", "茶频道"], "📺专业频道", 7),

    # === 没有独立频道分类的省份/城市（暂归其他，以防关键词过于宽泛） ===
    (["上海", "SHANGHAI"], "☘️上海频道", 5),
    (["北京", "BEIJING"], "☘️北京频道", 5),
    (["山东", "SHANDONG", "济南", "青岛", "潍坊"], "☘️山东频道", 5),
    (["四川", "SICHUAN", "成都", "绵阳"], "☘️四川频道", 5),
    (["山西", "SHANXI", "太原"], "☘️山西频道", 5),
    (["西藏", "拉萨"], "☘️西藏频道", 5),
    (["宁夏", "银川"], "☘️宁夏频道", 5),
    (["青海", "西宁"], "☘️青海频道", 5),
    (["新疆", "乌鲁木齐"], "☘️新疆频道", 5),
]

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
        (ADULT_SOURCES_FILE, False, "成人来源"),
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
