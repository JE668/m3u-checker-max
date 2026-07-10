import os
import re
import time

import requests


# ============================
# NVIDIA NIM API — 免费 AI 推理
# ============================
# OpenAI 兼容端点（无需额外 SDK，改一行 base_url 即可切换）
# 免费层：40 RPM，无每日请求上限，中国大陆可直连
# 注册：build.nvidia.com → Get API Key → nvapi-xxxx

API_ENDPOINT = "https://integrate.api.nvidia.com/v1/chat/completions"
MODEL_PRIMARY = "stepfun-ai/step-3.5-flash"       # 主力：阶跃星辰 Step 3.5 Flash，响应极快、中文好
MODEL_FALLBACK = "google/gemma-4-31b-it"            # 备选：Google Gemma 4 31B，指令遵循强

# 请在 GitHub Secrets 以及本地环境变量中设置 NVIDIA_API_KEY
API_KEY = os.getenv("NVIDIA_API_KEY", "")

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


# ── AI 调用限流 + 退避重试 ──
# NVIDIA NIM 免费层限制：40 RPM（所有模型共享，step-3.5-flash 可达 60 RPM）
# 默认不再做客户端固定限速（_AI_MIN_INTERVAL=0）：AI 调用本就是偶发突发，
# 且 ai_cache.json 已跨 CI 持久化，绝大多数频道名直接命中缓存、不调 API；
# 真实节流交由服务端 429 + 指数退避（含 Retry-After）动态完成，避免无意义空等。
# 如需硬性客户端限速，设置环境变量 AI_MIN_INTERVAL（秒）即可恢复。
_AI_MIN_INTERVAL = float(os.getenv("AI_MIN_INTERVAL", "0"))
_last_ai_call_ts = 0.0
_current_model = MODEL_PRIMARY   # 当前使用的模型（主→备自动切换）
_fallback_triggered = False      # 是否已切换到备选模型

def _ai_rate_limit():
    """控制 AI API 调用频率：若距上次调用不足最小间隔则短暂休眠。"""
    global _last_ai_call_ts
    elapsed = time.time() - _last_ai_call_ts
    if elapsed < _AI_MIN_INTERVAL:
        time.sleep(_AI_MIN_INTERVAL - elapsed)
    _last_ai_call_ts = time.time()

def _post_with_retry(payload: dict, headers: dict, timeout: float, max_retries: int = 3):
    """带限流与指数退避重试的 API POST（主模型失败后自动切换备选模型）。

    两阶段策略：先用主模型重试 max_retries 次，若全部失败且未切换过，
    再用备选模型重试 max_retries 次。其他状态码（含 5xx）按原样返回，
    由调用方决定降级策略。耗尽全部重试后返回 None。
    """
    global _current_model, _fallback_triggered
    models_to_try = [_current_model]
    if _current_model == MODEL_PRIMARY and not _fallback_triggered:
        models_to_try.append(MODEL_FALLBACK)

    for model in models_to_try:
        payload["model"] = model
        _current_model = model
        if model == MODEL_FALLBACK:
            _fallback_triggered = True
        backoff = 1.0
        for attempt in range(max_retries):
            try:
                _ai_rate_limit()
                resp = requests.post(API_ENDPOINT, json=payload, headers=headers, timeout=timeout)
                if resp.status_code == 429:
                    # 尊重服务端 Retry-After；缺失时退化为指数退避
                    ra = resp.headers.get("Retry-After")
                    try:
                        wait = float(ra) if ra is not None else backoff
                    except (TypeError, ValueError):
                        wait = backoff
                    time.sleep(wait)
                    backoff = min(backoff * 2, 8.0)
                    continue
                return resp
            except Exception:
                time.sleep(backoff)
                backoff = min(backoff * 2, 8.0)
    return None


# ── 省级行政区 + 运营商后缀（用于清洗频道名中的地区运营商标记）──
# 单一数据源：新增省份只需在此处维护，避免散落在正则字面量中。
PROVINCE_NAMES = (
    "广东", "北京", "上海", "天津", "重庆", "浙江", "江苏", "湖北", "湖南", "四川",
    "山东", "河南", "河北", "福建", "安徽", "辽宁", "吉林", "黑龙江", "江西", "山西",
    "陕西", "云南", "贵州", "广西", "海南", "甘肃", "青海", "宁夏", "新疆", "内蒙古", "西藏",
)
_PROVINCE_RE = re.compile(
    r'\s*(?:' + '|'.join(PROVINCE_NAMES) + r')\s*(?:电信|联通|移动|有线|广电|IPTV|iptv)\s*',
    re.IGNORECASE,
)


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
    # 去除地区运营商后缀（如 "广东电信"、"北京联通"、"上海有线"）
    name = re.sub(_PROVINCE_RE, ' ', name)
    # 去除单独的运营商标记
    name = re.sub(r'\s*(?:电信|联通|移动|有线|广电)\s*', ' ', name)
    # 压缩多余空格
    name = re.sub(r'\s+', ' ', name).strip()

    return name if name else raw_name


def standardize_channel_name(raw_name: str) -> str:
    """
    调用 NVIDIA NIM API（Step 3.5 Flash / Gemma 4 31B）将混乱的频道名称标准化。
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
        "model": _current_model,
        "messages": [
            {"role": "system", "content": "You are a precise data cleaning tool. Output only the final result."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.0
    }

    try:
        response = _post_with_retry(payload, headers, timeout=5)
        if response is not None and response.status_code == 200:
            result = response.json()
            standardized = result['choices'][0]['message']['content'].strip()
            standardized = standardized.replace('"', '').replace("'", "")
            final = standardized if standardized else raw_name
        else:
            # API 报错/限流耗尽时降级到规则预处理结果
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

    # 从 CATEGORY_RULES 自动生成分类列表，避免重复维护
    from utils.config import CATEGORY_RULES
    categories_list = []
    for keywords, cat_name, _ in CATEGORY_RULES:
        # 取关键词作为描述
        desc = "/".join(keywords[:3]) if keywords else cat_name
        categories_list.append(f"{cat_name} — 含 {desc} 等")
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
        "model": _current_model,
        "messages": [
            {"role": "system", "content": "You are a precise IPTV channel categorizer. Output only the category name."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.0
    }

    try:
        response = _post_with_retry(payload, headers, 5)
        if response is not None and response.status_code == 200:
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


def classify_channels_batch(names: list, batch_size: int = 50) -> dict:
    """批量分类：将多个频道名合并为一次（或分片）API 调用，返回 {name: cat}。

    相比逐条调用 classify_channel，可大幅减少 API 请求数（N 个频道 → ⌈N/batch_size⌉ 次）。
    结果同样写入 _CAT_CACHE，因此后续 classify_channel 命中缓存、零额外请求。
    API 不可用时返回空 dict，调用方应回退到默认分类。

    注意：返回的 cat 为纯分类名（不含 ",#genre#"），调用方需自行拼接。
    """
    result = {}
    if not names or not API_KEY:
        return result

    # 构建分类清单（与 classify_channel 保持一致）
    from utils.config import CATEGORY_RULES
    categories_list = []
    for keywords, cat_name, _ in CATEGORY_RULES:
        desc = "/".join(keywords[:3]) if keywords else cat_name
        categories_list.append(f"{cat_name} — 含 {desc} 等")
    cats_text = "\n".join(f"- {c}" for c in categories_list)
    known_cats = [c.split(' —')[0] for c in categories_list] + ['📺其他频道']

    # 去重 + 跳过已缓存
    pending = []
    for n in names:
        if not n or not n.strip():
            continue
        if n in _CAT_CACHE:
            result[n] = _CAT_CACHE[n]
        elif n not in pending:
            pending.append(n)

    if not pending:
        return result

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}"
    }

    for i in range(0, len(pending), batch_size):
        chunk = pending[i:i + batch_size]
        numbered = "\n".join(f"{idx + 1}. {n}" for idx, n in enumerate(chunk))
        prompt = (
            f"You are an IPTV channel categorization expert. "
            f"For each numbered channel name below, determine its most appropriate category "
            f"from the list. Reply with the SAME numbering, each line exactly: "
            f"'<number>. <category name>'. No explanation, no extra text.\n\n"
            f"Available categories:\n{cats_text}\n\n"
            f"Channels:\n{numbered}\n"
        )
        payload = {
            "model": _current_model,
            "messages": [
                {"role": "system", "content": "You are a precise IPTV channel categorizer. Output only numbered category names."},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.0
        }
        response = _post_with_retry(payload, headers, 15)
        if response is None or response.status_code != 200:
            continue
        try:
            text = response.json()['choices'][0]['message']['content']
        except Exception:
            continue
        # 按行号映射回原频道名并校验
        for line in text.splitlines():
            m = re.match(r'^\s*(\d+)\.\s*(.+?)\s*$', line)
            if not m:
                continue
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(chunk):
                cat = m.group(2).strip()
                if cat in known_cats:
                    _CAT_CACHE[chunk[idx]] = cat
                    result[chunk[idx]] = cat

    return result


