import requests
import os
import re
import json
import time

# ============================
# Google Gemini API — 官方端点
# ============================
# OpenAI 兼容模式（无需额外 SDK）
# 官方文档: https://ai.google.dev/gemini-api/docs/openai

API_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
MODEL = "gemma-4-26b-a4b-it"

# 请在 GitHub Secrets 以及本地环境变量中设置 GEMINI_API_KEY
API_KEY = os.getenv("GEMINI_API_KEY", "")

# ── AI 标准化缓存（避免重复 API 调用，每次运行有效） ──
_CACHE = {}  # {raw_name: standardized_name}
_CACHE_HITS = 0
_CACHE_MISSES = 0

def clear_cache():
    """清空运行时缓存（CI 步骤间可调用）"""
    global _CACHE, _CACHE_HITS, _CACHE_MISSES
    _CACHE = {}
    _CACHE_HITS = 0
    _CACHE_MISSES = 0

def get_cache_stats():
    return {"hits": _CACHE_HITS, "misses": _CACHE_MISSES, "size": len(_CACHE)}


def _simple_standardize(raw_name: str) -> str:
    """纯规则预处理：去除括号、方括号、连字符后的杂项，减少 AI Token 消耗"""
    if not raw_name:
        return raw_name

    name = raw_name.strip()
    # 去除括号内容
    name = re.sub(r'\s*\(.*?\)\s*', ' ', name)
    name = re.sub(r'\s*\[.*?\]\s*', ' ', name)
    # 去除尾部连字符及之后的内容（如 "CCTV-1 高清-广东" → "CCTV-1 高清"）
    # 去除尾部连字符后跟质量标记或中文的情况，保留 数字后缀（如 "CCTV-1"）
    name = re.sub(r'\s*[-—]\s*(?:4K|8K|HD|UHD|高清|超清|标清).*?$', '', name, flags=re.IGNORECASE)
    # 去除常见的质量标记
    name = re.sub(r'\s*(超清|高清|标清|HD|4K|8K|UHD)\s*', ' ', name, flags=re.IGNORECASE)
    # 压缩多余空格
    name = re.sub(r'\s+', ' ', name).strip()

    return name if name else raw_name


def standardize_channel_name(raw_name: str) -> str:
    """
    调用 Google Gemini API（Gemma 4）将混乱的频道名称标准化。
    两次缓存：运行时字典缓存 + 去重过滤（同名请求只调用一次 API）。

    返回: 标准化后的名称。API 不可用时静默返回原名。
    """
    if not raw_name or not raw_name.strip():
        return raw_name

    global _CACHE, _CACHE_HITS, _CACHE_MISSES

    # ── 检查缓存 ──
    if raw_name in _CACHE:
        _CACHE_HITS += 1
        return _CACHE[raw_name]
    _CACHE_MISSES += 1

    # ── 如果没 API Key，就直接走规则预处理然后返回 ──
    if not API_KEY:
        result = _simple_standardize(raw_name)
        _CACHE[raw_name] = result
        return result

    # ── 规则预处理（轻度清洗后传给 AI） ──
    pre_cleaned = _simple_standardize(raw_name)

    # ── 构造 OpenAI 兼容的 API 请求 ──
    prompt = (
        f"You are an IPTV channel naming expert. "
        f"Standardize the following channel name into its most concise, official version. "
        f"Remove quality markers (4K, HD, 超清, 高清), region markers (广东, 电信, 联通), "
        f"and redundant descriptions. If already clean or unidentifiable, return as-is.\n\n"
        f"Examples:\n"
        f"- 'CCTV-1 超清 广东电信' -> 'CCTV-1'\n"
        f"- '湖南卫视 (HD)' -> '湖南卫视'\n"
        f"- 'CCTV-5 体育' -> 'CCTV-5'\n"
        f"- '广东卫视-4K' -> '广东卫视'\n"
        f"- ' CCTV-13 新闻 ' -> 'CCTV-13'\n"
        f"- '浙江卫视 [高清]' -> '浙江卫视'\n\n"
        f"Input: '{pre_cleaned}'\n"
        f"Output: (Return ONLY the standardized name, no explanation, no quotes)"
    )

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}"
    }

    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "You are a precise data cleaning tool. Output only the final result."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.0
    }

    try:
        response = requests.post(API_ENDPOINT, json=payload, headers=headers, timeout=5)
        if response.status_code == 200:
            result = response.json()
            standardized = result['choices'][0]['message']['content'].strip()
            standardized = standardized.replace('"', '').replace("'", "")
            final = standardized if standardized else raw_name
        else:
            # API 报错时降级到规则预处理结果
            final = pre_cleaned
    except Exception:
        # 网络异常时降级到规则预处理结果
        final = pre_cleaned

    _CACHE[raw_name] = final
    return final


# ── AI 分类缓存 ──
_CAT_CACHE = {}

def classify_channel(name: str) -> str:
    """
    调用 AI 判断频道归属分类。

    返回格式: 分类名（如 "📺央视频道"、"☘️广东频道"、"📡卫视频道"）
    API 不可用时返回空字符串。
    """
    if not name or not name.strip():
        return ""

    if name in _CAT_CACHE:
        return _CAT_CACHE[name]

    if not API_KEY:
        return ""

    categories_list = [
        "📺央视频道 — 频道名包含 CCTV、CETV、CGTN 的",
        "📡卫视频道 — 省级卫视（如 湖南卫视、浙江卫视、江苏卫视、东方卫视、北京卫视、广东卫视 等）",
        "☘️广东频道 — 广东/广州/深圳/东莞/佛山/中山/珠海/汕头/惠州/江门/肇庆/韶关/河源/清远/湛江/茂名/阳江/云浮/潮州/揭阳/汕尾/梅州 等地方频道",
        "☘️北京频道 — 北京地方频道",
        "☘️天津频道 — 天津地方频道",
        "☘️上海频道 — 上海/东方 等地方频道",
        "☘️重庆频道 — 重庆地方频道",
        "☘️河北频道 — 河北/石家庄/邯郸/衡水/邢台/秦皇岛/沧州/保定/张家口/承德/廊坊/唐山 等地方频道",
        "☘️山西频道 — 山西/太原 等地方频道",
        "☘️内蒙古频道 — 内蒙古/内蒙 等地方频道",
        "☘️辽宁频道 — 辽宁/沈阳/大连/鞍山 等地方频道",
        "☘️吉林频道 — 吉林/延边/长春 等地方频道",
        "☘️黑龙江频道 — 黑龙江/哈尔滨/齐齐哈尔/佳木斯/大庆 等地方频道",
        "☘️江苏频道 — 江苏/南京/苏州/无锡/常州/镇江/南通/扬州/徐州/淮安/盐城/连云港/泰州/宿迁 等地方频道",
        "☘️浙江频道 — 浙江/杭州/宁波/温州/嘉兴/绍兴/湖州/金华/台州/舟山/衢州/丽水 等地方频道",
        "☘️安徽频道 — 安徽/合肥/芜湖/蚌埠/铜陵/亳州/六安/黄山 等地方频道",
        "☘️福建频道 — 福建/厦门/福州/泉州/漳州/莆田/龙岩/三明/南平/宁德 等地方频道",
        "☘️江西频道 — 江西/南昌/赣州/九江 等地方频道",
        "☘️山东频道 — 山东/济南/青岛/潍坊 等地方频道",
        "☘️河南频道 — 河南/郑州/开封/洛阳/新乡/安阳/南阳/信阳/商丘/周口 等地方频道",
        "☘️湖北频道 — 湖北/武汉/荆门/宜昌/襄阳/荆州/黄石/十堰/孝感/黄冈 等地方频道",
        "☘️湖南频道 — 湖南/长沙/湘潭 等地方频道",
        "☘️广东频道 — 广东/广州/深圳/东莞/佛山/中山/珠海/汕头/惠州/江门/肇庆/韶关/河源/清远/湛江/茂名/阳江/云浮/潮州/揭阳/汕尾/梅州 等地方频道",
        "☘️广西频道 — 广西/桂林/南宁/柳州/梧州/北海 等地方频道",
        "☘️海南频道 — 海南/三沙/海口 等地方频道",
        "☘️四川频道 — 四川/成都/绵阳 等地方频道",
        "☘️贵州频道 — 贵州/贵阳/遵义 等地方频道",
        "☘️云南频道 — 云南/昆明 等地方频道",
        "☘️西藏频道 — 西藏/拉萨 等地方频道",
        "☘️陕西频道 — 陕西/西安/咸阳/宝鸡 等地方频道",
        "☘️甘肃频道 — 甘肃/兰州 等地方频道",
        "☘️青海频道 — 青海/西宁 等地方频道",
        "☘️宁夏频道 — 宁夏/银川 等地方频道",
        "☘️新疆频道 — 新疆/乌鲁木齐 等地方频道",
        "🌊港·澳·台 — 凤凰/翡翠/明珠/东森/三立/TVBS/中天/纬来/靖天/J2/VIUTV 等港澳台频道",
        "🏀体育频道 — 体育/竞技/篮球/足球/电竞/健身 等频道",
        "🎥电影频道 — 电影/CHC电影/龙祥电影 等频道",
        "🎬剧场频道 — 剧场/戏剧 等频道",
        "🪁动画频道 — 动画/动漫/卡通/少儿 等频道",
        "📚教育频道 — 教育/卫生健康 等频道",
        "🎉春晚频道 — 春晚相关频道",
        "📺中国教育电视台 — CETV/教育等频道",
        "📺中国国际电视台 — CGTN/英语/纪录/国际新闻等频道",
        "📺CHC系列 — CHC系列电影频道",
        "📺BesTV系列 — BesTV百视通系列频道",
        "📺NewTV系列 — NewTV系列频道",
        "📺SiTV系列 — SiTV系列频道",
        "📺iHOT系列 — iHOT系列频道",
        "📺IPTV系列 — IPTV专区频道",
        "📺数字频道 — 数字电视频道",
        "☘️4K/8K超高清频道 — 含 4K/8K 的超高清频道",
        "📺其他频道 — 以上都不匹配的",
    ]
    cats_text = "\n".join(f"- {c}" for c in categories_list)
    prompt = (
        f"You are an IPTV channel categorization expert. "
        f"Given a channel name, determine its most appropriate category from the list below. "
        f"Return ONLY the category name, no explanation.\n\n"
        f"Available categories:\n"
        f"{cats_text}\n\n"
        f"Channel name: '{name}'\n"
        f"Category:"
    )

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}"
    }

    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "You are a precise IPTV channel categorizer. Output only the category name."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.0
    }

    try:
        response = requests.post(API_ENDPOINT, json=payload, headers=headers, timeout=5)
        if response.status_code == 200:
            result = response.json()
            cat = result['choices'][0]['message']['content'].strip()
            # Validate: ensure it's one of our known categories
            known_cats = [c.split(' —')[0] for c in categories_list] + ['📺其他频道']
            if cat in known_cats:
                _CAT_CACHE[name] = cat
                return cat
    except Exception:
        pass

    return ""

