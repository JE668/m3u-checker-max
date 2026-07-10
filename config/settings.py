# ═══════════════════════════════════════════════
# m3u-checker-max 统一配置文件
# ═══════════════════════════════════════════════
# 修改此文件后无需修改 main.py
# 环境变量优先级更高（用于 CI 动态覆盖）
# ═══════════════════════════════════════════════

# ── 测速并发 ──
MAX_WORKERS = 50                     # 并发线程数
EPG_MAX_WORKERS = 4                  # EPG 下载并发数

# ── 服务器级预筛 ──
SAMPLE_PER_HOST = 2                  # 每台服务器预抽检频道数（判断死活）

# ── 测速超时参数（秒） ──
CHECK_CONNECT_TIMEOUT = 5            # TCP 连接超时
CHECK_READ_TIMEOUT = 8               # 单次读取超时
CHECK_TOTAL_TIMEOUT = 15             # 总超时保护
DOWNLOAD_TARGET_BYTES = 1048576      # 下载大小（1MB）
MIN_BANDWIDTH_MBPS = 2.0             # 最低带宽阈值

# ── 重试 ──
RETRY_MAX_ATTEMPTS = 2               # 最大重试次数
RETRY_BACKOFF = 1.0                  # 重试退避系数（秒）

# ── 分辨率检测 ──
PROBE_RESOLUTION = True              # 是否检测分辨率
PROBE_TIMEOUT = 4                    # ffprobe 超时（秒）
MIN_RESOLUTION = "1920x1080"          # 最低分辨率过滤（0x0=不过滤）

# ── CDN 与数据源 ──
CDN_BASE = "https://gh.felicity.ac.cn"
# get-m3u 探针元数据（测速优先级排序用）：由 utils/config.py 依据 CDN_BASE 派生为单一来源

# ── 文件路径 ──
SOURCES_FILE = "config/sources.txt"
EPG_FILE = "config/epg.txt"
ALIAS_FILE = "config/alias.txt"
DEMO_FILE = "config/demo.txt"
BLACKLIST_FILE = "config/blacklist.txt"
CHANNEL_MODEL_FILE = "config/Channel_model.txt"
WHITELIST_FILE = "config/whitelist.txt"
ADULT_SOURCES_FILE = "config/adult-sources.txt"
SOURCE_CAT_FILE = "config/source-cat.txt"
ICON_DIR = "icons"
ICONS_INDEX_FILE = "config/icons_index.txt"

OUTPUT_TXT = "output/live.txt"
OUTPUT_M3U = "output/live.m3u"
OUTPUT_EPG = "output/epg.xml"
OUTPUT_EPG_GZ = "output/epg.xml.gz"
LOG_FILE = "output/log.txt"
UNMATCHED_FILE = "output/unmatched.txt"
ADULT_TXT = "output/adult.txt"
ADULT_M3U = "output/adult.m3u"
NON_TV_LOG = "output/non-tv-filtered.txt"

# ── HTTP 请求 ──
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

# ── 非电视台频道过滤词 ──
NON_TV_PATTERNS = (
    "斗鱼", "虎牙", "B站", "哔哩", "Bilibili", "bilibili", "抖音", "快手",
    "Astream", "映客", "花椒", "一直播", "来疯", "陌陌", "YY",
    "挨饿德", "大司马", "PDD", "旭旭宝宝", "卢本伟", "冯提莫",
    "YouTube", "PlutoTV", "Pluto", "Tubi", "Netflix", "奈飞", "Disney+",
    "点播", "影视", "电影", "电视剧", "综艺", "MV", "Video",
    "广播", "Radio", "电台", "Music", "音乐频道",
    "非合规", "阿塔",
    "赌场", "赌波",
)

# ── 限制级频道匹配（已弃用关键词免测，仅URL来源匹配，见 config/adult-sources.txt）──
ADULT_KEYWORDS = ()

# ── 频道名清洗规则 ──
INVALID_NAME_PATTERNS = [
    r'^\d{4}-\d{1,2}-\d{1,2}#',
    r'^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}$',
    r'^免费订阅',
    r'^请勿贩卖',
    r'^维护时间',
    r'^佛系维护',
]

# ── EPG ──
EPG_BLACKLIST = [
    "未能提供", "暂无节目", "精彩节目", "精彩節目",
    "没有节目", "未提供节目", "未提供節目",
    "no program", "no data", "精彩剧集", "暂未提供"
]
EPG_KEEP_DAYS = 2                    # EPG 保留天数（前一天+当天），0=保留全部
