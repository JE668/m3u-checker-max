import os, time, concurrent.futures, requests, gzip, io, re
import xml.etree.ElementTree as ET
from datetime import datetime

# ===============================
# 1. 核心配置区
# ===============================
SOURCES_FILE = "config/sources.txt"
EPG_FILE = "config/epg.txt"
ALIAS_FILE = "config/alias.txt"
DEMO_FILE = "config/demo.txt"
BLACKLIST_FILE = "config/blacklist.txt"
WHITELIST_FILE = "config/whitelist.txt"
ICON_DIR = "icons"

OUTPUT_TXT = "output/live.txt"
OUTPUT_M3U = "output/live.m3u"
OUTPUT_EPG = "output/epg.xml"
OUTPUT_EPG_GZ = "output/epg.xml.gz"
LOG_FILE = "output/log.txt"
UNMATCHED_FILE = "output/unmatched.txt"

# CDN 基础域名（P2-17: 提取为配置，便于更换）
CDN_BASE = os.environ.get("CDN_BASE", "https://gh.felicity.ac.cn")
REPO_RAW = f"{CDN_BASE}/https://raw.githubusercontent.com/JE668/m3u-checker-max/main"

# M3U 头部
M3U_HEADER = f'#EXTM3U x-tvg-url="{REPO_RAW}/output/epg.xml.gz"\n'

# EPG 垃圾词汇过滤库
EPG_BLACKLIST = [
    "未能提供", "暂无节目", "精彩节目", "精彩節目",
    "没有节目", "未提供节目", "未提供節目",
    "no program", "no data", "精彩剧集", "暂未提供"
]

# HTTP 请求默认 Headers
DEFAULT_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# 测速并发线程数
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "50"))

# 测速参数
CHECK_CONNECT_TIMEOUT = int(os.environ.get("CHECK_CONNECT_TIMEOUT", "5"))
CHECK_READ_TIMEOUT = int(os.environ.get("CHECK_READ_TIMEOUT", "8"))
CHECK_TOTAL_TIMEOUT = int(os.environ.get("CHECK_TOTAL_TIMEOUT", "15"))
CHECK_DOWNLOAD_TARGET = 128 * 1024  # 128KB

# EPG 并发下载数
EPG_MAX_WORKERS = int(os.environ.get("EPG_MAX_WORKERS", "4"))

# 来源免测配置：iptv-api 已做过测速+分辨率过滤，跳过二次测速
IPTV_API_SOURCE_URL = "https://raw.githubusercontent.com/JE668/iptv-api/refs/heads/master/output/result.m3u"
IPTV_API_SKIP_TEST_TTL_HOURS = int(os.environ.get("IPTV_API_SKIP_TEST_TTL_HOURS", "24"))

# 重试配置
RETRY_MAX_ATTEMPTS = int(os.environ.get("RETRY_MAX_ATTEMPTS", "2"))
RETRY_BACKOFF = float(os.environ.get("RETRY_BACKOFF", "1.0"))

os.makedirs("output", exist_ok=True)
os.makedirs("config", exist_ok=True)
os.makedirs(ICON_DIR, exist_ok=True)

# P1-12: 全局 Session 复用（同一域名复用 TCP 连接 + SSL 会话）
_http_session = None

def get_session():
    global _http_session
    if _http_session is None:
        _http_session = requests.Session()
        _http_session.headers.update(DEFAULT_HEADERS)
        # 连接池大小匹配并发度
        adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=MAX_WORKERS)
        _http_session.mount("http://", adapter)
        _http_session.mount("https://", adapter)
    return _http_session

def live_print(content):
    print(content, flush=True)

# ===============================
# 1.5 网络工具：重试装饰器 (P1-6)
# ===============================
def retry_request(max_attempts=RETRY_MAX_ATTEMPTS, backoff=RETRY_BACKOFF):
    """对 requests 调用添加指数退避重试"""
    def decorator(func):
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

# ===============================
# 2. 核心字典：加载配置、黑白名单、别名与分类
# ===============================
def load_filter_lists(filepath):
    """通用黑/白名单加载器，自动区分频道名与具体链接"""
    names, urls = set(), set()
    if os.path.exists(filepath):
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'): continue
                if line.startswith('http'): urls.add(line)
                else: names.add(line)
    return names, urls

def load_aliases():
    aliases_exact, aliases_regex = {}, []
    known_main_names = set()

    live_print("::group::⚙️ 加载系统配置文件")
    if not os.path.exists(ALIAS_FILE):
        live_print(f"⚠️ 未找到别名配置文件: {ALIAS_FILE}")
        return aliases_exact, aliases_regex, known_main_names

    with open(ALIAS_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'): continue
            parts = line.split(',')
            main_name = parts[0].strip()
            known_main_names.add(main_name)

            for alias in parts[1:]:
                alias = alias.strip()
                if alias.startswith("re:"):
                    try:
                        aliases_regex.append((re.compile(alias[3:]), main_name))
                    except re.error as e:
                        live_print(f"⚠️ 正则编译失败 [{alias}]: {e}")
                else:
                    aliases_exact[alias] = main_name

    live_print(f"✅ {ALIAS_FILE} (只读): 成功载入精确映射 {len(aliases_exact)} 个，正则映射 {len(aliases_regex)} 个。")
    return aliases_exact, aliases_regex, known_main_names

def get_main_name(raw_name, aliases_exact, aliases_regex, known_main_names, unmatched_set=None):
    raw_name = raw_name.strip()
    if raw_name in known_main_names: return raw_name
    if raw_name in aliases_exact: return aliases_exact[raw_name]
    for reg, main_name in aliases_regex:
        if reg.match(raw_name): return main_name
    if unmatched_set is not None:
        unmatched_set.add(raw_name)
    return raw_name

# icons Release 配置（icons 以 LFS 管理，GH Actions 中不下载 LFS 文件，改用索引匹配）
ICONS_INDEX_FILE = "config/icons_index.txt"

def _build_logo_index():
    """构建 {clean_name: filename} 字典，O(1) 查找。
    优先扫描本地 icons/ 目录（开发环境），否则读取预生成索引文件（CI 环境）。"""
    index = {}
    # 1) 本地 icons 目录（LFS pull 后或开发环境）
    if os.path.exists(ICON_DIR) and os.path.isdir(ICON_DIR):
        files = os.listdir(ICON_DIR)
        if len(files) > 10:  # 目录非空且有一定数量
            for f in files:
                if f.startswith('.'): continue
                index[re.sub(r'[\s\-_]', '', os.path.splitext(f)[0]).lower()] = f
            return index
    # 2) 预生成索引文件（CI 环境，无需下载 321MB LFS 文件）
    if os.path.exists(ICONS_INDEX_FILE):
        with open(ICONS_INDEX_FILE, "r", encoding="utf-8") as fh:
            for line in fh:
                fname = line.strip()
                if fname and not fname.startswith('#'):
                    index[re.sub(r'[\s\-_]', '', os.path.splitext(fname)[0]).lower()] = fname
        live_print(f"📋 图标索引: 从 {ICONS_INDEX_FILE} 加载 {len(index)} 项")
        return index
    live_print(f"⚠️ 图标索引不可用: 本地 icons/ 和 {ICONS_INDEX_FILE} 均缺失")
    return index

LOGO_INDEX = _build_logo_index()

# logo URL 指向 CDN 加速的 GitHub Raw（LFS 文件通过 Raw URL 正常返回图片内容）
_ICONS_BASE_URL = f"{REPO_RAW}/icons"

def get_local_logo_url(name):
    target = re.sub(r'[\s\-_]', '', name).lower()
    if target in LOGO_INDEX:
        return f"{_ICONS_BASE_URL}/{LOGO_INDEX[target]}"
    return ""

def load_demo_template(aliases_exact, aliases_regex, known_main_names):
    category_order = []
    channel_to_category = {}
    channels_in_category = {}

    if not os.path.exists(DEMO_FILE):
        live_print(f"⚠️ 未找到分类模板文件: {DEMO_FILE}")
        live_print("::endgroup::")
        return category_order, channel_to_category, channels_in_category

    current_category = None
    with open(DEMO_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line: continue
            # P1-11: 修复运算符优先级 — 注释行但含 #genre# 的是分类行，应保留
            if line.startswith('#') and "#genre#" not in line: continue

            if "#genre#" in line:
                current_category = line.split(',')[0].strip()
                if current_category not in category_order:
                    category_order.append(current_category)
                    channels_in_category[current_category] = []
            elif current_category:
                raw_name = line
                main_name = get_main_name(raw_name, aliases_exact, aliases_regex, known_main_names)

                if current_category not in channels_in_category:
                    channels_in_category[current_category] = []

                channel_to_category[main_name] = current_category
                if main_name not in channels_in_category[current_category]:
                    channels_in_category[current_category].append(main_name)

    total_channels = sum(len(v) for v in channels_in_category.values())
    live_print(f"✅ {DEMO_FILE} (读写): 成功载入 {len(category_order)} 个大类，包含 {total_channels} 个已知频道。")
    live_print("::endgroup::")
    return category_order, channel_to_category, channels_in_category

# ===============================
# 3. 抓取、清理与整合 EPG
# ===============================
def _download_single_epg(url, aliases_exact, aliases_regex, known_main_names):
    """下载并解析单个 EPG 源（供并发调用）"""
    if "gitee.com" in url and "/blob/" in url:
        url = url.replace("/blob/", "/raw/")
    elif "github.com" in url and "/blob/" in url:
        url = url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")

    report_lines = [f"▶ 来源: {url}"]
    try:
        live_print(f"📥 正在获取: {url}")
        r = get_session().get(url, timeout=20)
        content = r.content
        if not content:
            report_lines.append(" -> ⚠️ 响应为空，跳过")
            return report_lines, [], []

        if content.startswith(b'\x1f\x8b'):
            try:
                content = gzip.decompress(content)
            except Exception as e:
                report_lines.append(f" -> ⚠️ gzip解压失败: {e}")
                return report_lines, [], []

        try:
            root = ET.parse(io.BytesIO(content)).getroot()
            if root.tag != 'tv':
                report_lines.append(" -> ⚠️ XML 根节点非 <tv>，跳过")
                return report_lines, [], []
        except ET.ParseError as e:  # P0-2: 精确捕获 XML 解析异常
            report_lines.append(f" -> ⚠️ XML 解析失败: {e}")
            return report_lines, [], []

        channels_out = []
        programmes_out = []
        seen_channels = set()
        seen_programmes = set()
        id_mapping = {}
        seen_epg_renames = set()
        c_count, p_count, p_discard, rename_count = 0, 0, 0, 0

        for channel in root.findall('channel'):
            orig_id = channel.get('id')
            display_name_elem = channel.find('display-name')
            if orig_id and display_name_elem is not None and display_name_elem.text:
                orig_name = display_name_elem.text.strip()
                main_name = get_main_name(orig_name, aliases_exact, aliases_regex, known_main_names)

                if orig_name != main_name:
                    rename_count += 1
                    if (orig_name, main_name) not in seen_epg_renames:
                        live_print(f"  📝 [EPG修正] {orig_name} => {main_name}")
                        seen_epg_renames.add((orig_name, main_name))

                id_mapping[orig_id] = main_name
                channel.set('id', main_name)
                display_name_elem.text = main_name
                if main_name not in seen_channels:
                    seen_channels.add(main_name)
                    channels_out.append(channel)
                    c_count += 1

        for prog in root.findall('programme'):
            title_node = prog.find('title')
            title_text = title_node.text.lower() if title_node is not None and title_node.text else ""
            if any(kw in title_text for kw in EPG_BLACKLIST):
                p_discard += 1
                continue
            orig_channel_id = prog.get('channel')
            if orig_channel_id in id_mapping:
                new_id = id_mapping[orig_channel_id]
                prog.set('channel', new_id)
                key = (new_id, prog.get('start'), prog.get('stop'))
                if key not in seen_programmes:
                    seen_programmes.add(key)
                    programmes_out.append(prog)
                    p_count += 1

        msg = f" -> ✅ 提取频道: {c_count} | 节目: {p_count} | 🗑️ 过滤: {p_discard} | 🔧 总修正: {rename_count}次"
        live_print(msg)
        report_lines.append(msg)
        return report_lines, channels_out, programmes_out

    except Exception as e:
        msg = f" -> ❌ 异常: {e}"
        live_print(msg)
        report_lines.append(msg)
        return report_lines, [], []


def download_and_merge_epg(aliases_exact, aliases_regex, known_main_names):
    epg_urls = []
    epg_report = []
    if os.path.exists(EPG_FILE):
        with open(EPG_FILE, 'r', encoding='utf-8') as f:
            epg_urls = [line.strip() for line in f if line.strip() and not line.startswith('#')]

    if not epg_urls: return epg_report

    live_print("::group::📅 开始下载并整合 EPG 节目单")

    # P1-8: EPG 并发下载
    merged_channels = []
    merged_programmes = []
    seen_channel_ids = set()
    seen_programme_keys = set()

    if len(epg_urls) > 1:
        live_print(f"🔄 使用 {EPG_MAX_WORKERS} 并发下载 {len(epg_urls)} 个 EPG 源")
        with concurrent.futures.ThreadPoolExecutor(max_workers=EPG_MAX_WORKERS) as ex:
            futures = {ex.submit(_download_single_epg, url, aliases_exact, aliases_regex, known_main_names): url
                       for url in epg_urls}
            for future in concurrent.futures.as_completed(futures):
                report, channels, programmes = future.result()
                epg_report.extend(report)
                for ch in channels:
                    ch_id = ch.get('id')
                    if ch_id not in seen_channel_ids:
                        seen_channel_ids.add(ch_id)
                        merged_channels.append(ch)
                for prog in programmes:
                    prog_key = (prog.get('channel'), prog.get('start'), prog.get('stop'))
                    if prog_key not in seen_programme_keys:
                        seen_programme_keys.add(prog_key)
                        merged_programmes.append(prog)
    else:
        # 单源直接串行
        report, channels, programmes = _download_single_epg(epg_urls[0], aliases_exact, aliases_regex, known_main_names)
        epg_report.extend(report)
        merged_channels = channels
        merged_programmes = programmes

    # 写入合并后的 EPG 文件
    if len(merged_channels) > 0:
        try:
            merged_tv = ET.Element("tv")
            merged_tv.set("generator-info-name", "Merged EPG by GitHub Actions")
            for ch in merged_channels:
                merged_tv.append(ch)
            for prog in merged_programmes:
                merged_tv.append(prog)

            tree = ET.ElementTree(merged_tv)
            with open(OUTPUT_EPG, 'wb') as f:
                f.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
                tree.write(f, encoding='utf-8', xml_declaration=False)
            with open(OUTPUT_EPG, 'rb') as f_in, gzip.open(OUTPUT_EPG_GZ, 'wb') as f_out:
                f_out.writelines(f_in)
            final_msg = f"🎉 EPG 整合完成！规范频道数: {len(merged_channels)}，节目数: {len(merged_programmes)}"
            live_print(final_msg)
            epg_report.append("\n" + final_msg)
        except Exception as e:
            live_print(f"❌ EPG写入失败: {e}")

    live_print("::endgroup::")
    return epg_report

# ===============================
# 4. 抓取直播源
# ===============================
# P2-14: EXTINF 属性提取正则
_RE_EXTINF_ATTRS = re.compile(r'tvg-logo="([^"]*)"')
_RE_EXTINF_GROUP = re.compile(r'group-title="([^"]*)"')

def fetch_and_parse_channels(aliases_exact, aliases_regex, known_main_names):
    channels = []  # [(main_name, url, source_url), ...]
    unmatched_names = set()

    if not os.path.exists(SOURCES_FILE): return channels
    with open(SOURCES_FILE, 'r', encoding='utf-8') as f:
        sources = [line.strip() for line in f if line.strip() and not line.startswith('#')]

    # 检查 iptv-api 数据新鲜度（TTL 保护）
    iptv_api_skip_test = False
    try:
        resp = get_session().head(IPTV_API_SOURCE_URL, timeout=10, allow_redirects=True)
        last_modified = resp.headers.get("Last-Modified")
        if last_modified:
            from email.utils import parsedate_to_datetime
            lm_dt = parsedate_to_datetime(last_modified)
            age_hours = (datetime.now(lm_dt.tzinfo) - lm_dt).total_seconds() / 3600
            if age_hours <= IPTV_API_SKIP_TEST_TTL_HOURS:
                iptv_api_skip_test = True
                live_print(f"✅ iptv-api 数据新鲜（{age_hours:.1f}h 前），其来源将跳过测速")
            else:
                live_print(f"⚠️ iptv-api 数据已过期（{age_hours:.1f}h > {IPTV_API_SKIP_TEST_TTL_HOURS}h TTL），降级为正常测速")
        else:
            live_print("⚠️ iptv-api 无 Last-Modified 头，降级为正常测速")
    except Exception as e:
        live_print(f"⚠️ iptv-api 新鲜度检查失败: {e}，降级为正常测速")

    seen_urls = set()
    live_print("::group::📥 开始抓取直播源")
    for source_url in sources:
        skip_this_source = (source_url == IPTV_API_SOURCE_URL and iptv_api_skip_test)
        try:
            r = retry_request()(lambda u: get_session().get(u, timeout=10))(source_url)  # P0-3: 使用 Session + UA
            r.encoding = 'utf-8'
            tmp_name = ""
            tmp_logo = ""  # P2-14: 提取 tvg-logo
            count = 0
            seen_source_renames = set()

            for line in r.text.splitlines():
                line = line.strip()
                if not line: continue
                if line.startswith("#EXTINF"):
                    # 提取频道名
                    tmp_name = line.split(",")[-1].strip()
                    # P2-14: 提取 tvg-logo 和 group-title
                    logo_match = _RE_EXTINF_ATTRS.search(line)
                    tmp_logo = logo_match.group(1) if logo_match else ""
                    group_match = _RE_EXTINF_GROUP.search(line)
                    # group-title 暂存，可用于后续分类优化
                    _ = group_match.group(1) if group_match else ""
                elif line.startswith("http"):
                    name = tmp_name if tmp_name else "未命名频道"
                    main_name = get_main_name(name, aliases_exact, aliases_regex, known_main_names, unmatched_names)

                    if name != main_name and (name, main_name) not in seen_source_renames:
                        live_print(f"  📝 [名称修正] {name} => {main_name}")
                        seen_source_renames.add((name, main_name))

                    if line not in seen_urls:
                        channels.append((main_name, line, source_url))
                        seen_urls.add(line); count += 1
                    tmp_name = ""
                    tmp_logo = ""
                elif "," in line and "://" in line:
                    parts = line.split(",", 1)
                    raw_name = parts[0].strip()
                    main_name = get_main_name(raw_name, aliases_exact, aliases_regex, known_main_names, unmatched_names)

                    if raw_name != main_name and (raw_name, main_name) not in seen_source_renames:
                        live_print(f"  📝 [名称修正] {raw_name} => {main_name}")
                        seen_source_renames.add((raw_name, main_name))

                    if parts[1].strip() not in seen_urls:
                        channels.append((main_name, parts[1].strip(), source_url))
                        seen_urls.add(parts[1].strip()); count += 1
            label = "🔄免测" if skip_this_source else "🔍待测"
            live_print(f"✅ {source_url} -> 提取 {count} 条 [{label}]")
        except Exception as e:  # P0-1: 精确捕获异常并输出详情
            live_print(f"❌ 连接失败: {source_url} — {type(e).__name__}: {e}")

    if unmatched_names:
        with open(UNMATCHED_FILE, "w", encoding="utf-8") as f:
            f.write(f"=============== 未匹配频道名单 ===============\n")
            f.write(f"时间: {datetime.now()}\n")
            f.write(f"说明: 以下 {len(unmatched_names)} 个频道在抓取时未能在 config/alias.txt 中找到匹配。\n")
            f.write(f"建议: 将它们复制到 alias.txt 中进行别名映射，以保持列表纯净。\n")
            f.write(f"==============================================\n\n")
            for name in sorted(unmatched_names):
                f.write(f"{name}\n")
        live_print(f"\n⚠️ 发现 {len(unmatched_names)} 个未匹配的频道！已输出待办清单至: {UNMATCHED_FILE}")
    else:
        if os.path.exists(UNMATCHED_FILE): os.remove(UNMATCHED_FILE)

    live_print("::endgroup::")
    return channels

# ===============================
# 5. 并发测速
# ===============================
def check_channel(main_name, url):
    """并发测速：下载 128KB 判定存活，总超时保护确保不会卡死"""
    start_time = time.time()
    try:
        with get_session().get(url, stream=True, timeout=(CHECK_CONNECT_TIMEOUT, CHECK_READ_TIMEOUT)) as r:
            if r.status_code != 200:
                return False, main_name, url, round(time.time() - start_time, 2), f"HTTP {r.status_code}"

            downloaded = 0
            last_chunk_time = time.time()

            for chunk in r.iter_content(chunk_size=1024 * 64):
                now = time.time()
                # P1-7: 总超时保护 — 无论 chunk 间隔多短，总耗时超限直接终止
                if now - start_time > CHECK_TOTAL_TIMEOUT:
                    return False, main_name, url, round(now - start_time, 2), "总超时"
                # 单 chunk 间隔超时（防止服务器极慢 drip 数据）
                if now - last_chunk_time > CHECK_READ_TIMEOUT:
                    return False, main_name, url, round(now - start_time, 2), "读取超时"

                downloaded += len(chunk)
                last_chunk_time = now
                if downloaded >= CHECK_DOWNLOAD_TARGET:
                    return True, main_name, url, round(now - start_time, 2), "成功"

            # 流结束但不足128KB
            return False, main_name, url, round(time.time() - start_time, 2), "流数据不足"

    except requests.exceptions.Timeout:
        return False, main_name, url, round(time.time() - start_time, 2), "连接超时"
    except requests.exceptions.ConnectionError as e:
        return False, main_name, url, round(time.time() - start_time, 2), f"连接失败: {e}"
    except Exception as e:
        return False, main_name, url, round(time.time() - start_time, 2), f"异常: {type(e).__name__}: {e}"

# ===============================
# 6. 核心：无损追加模式进化 demo.txt
# ===============================
# 频道分类规则：(匹配关键词列表, 分类显示名, 排序优先级)
CATEGORY_RULES = [
    (["4K", "8K"], "☘️4K/8K超高清频道", 0),
    (["CCTV"], "📺央视频道", 1),
    (["CETV"], "📺央视频道", 2),
    (["卫视"], "📡卫视频道", 3),
]

DEFAULT_CATEGORY = ("📺其他频道", 4)

# P1-9: 预编译排序用正则
_NUM_RE = re.compile(r'\d+')

def _match_category(name):
    """根据频道名匹配分类"""
    name_upper = name.upper()
    for keywords, cat_name, priority in CATEGORY_RULES:
        if any(kw in name_upper for kw in keywords):
            return f"{cat_name},#genre#", priority
    return f"{DEFAULT_CATEGORY[0]},#genre#", DEFAULT_CATEGORY[1]

def channel_sort_key(name):
    nums = _NUM_RE.findall(name)
    val = int(nums[0]) if nums else 999
    _, priority = _match_category(name)
    return (priority, val, name)

def auto_update_demo(valid_names, cat_order, chan_to_cat, chans_in_cat):
    live_print("\n::group::🧠 自适应进化 config/demo.txt (无损追加模式)")

    new_channels = [n for n in valid_names if n not in chan_to_cat]

    if not new_channels:
        live_print("ℹ️ 状态: 测速存活的频道均已存在于 config/demo.txt 当前分组中。")
        live_print("✅ 动作: 模板保持原样，无需写入更新。")
        live_print("::endgroup::")
        return cat_order, chan_to_cat, chans_in_cat

    live_print(f"ℹ️ 状态: 发现了 {len(new_channels)} 个全新的存活频道！准备自动归类并追加写入...")

    additions = {}
    for name in new_channels:
        cat, _ = _match_category(name)
        additions.setdefault(cat, []).append(name)
        if cat not in cat_order:
            cat_order.append(cat)
            chans_in_cat[cat] = []
        chans_in_cat[cat].append(name)
        chan_to_cat[name] = cat
        live_print(f" -> 🆕 自动追加: [{name}] 归入 [{cat.split(',')[0]}]")

    if os.path.exists(DEMO_FILE):
        with open(DEMO_FILE, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    else:
        lines = []

    # P1-10: 统一换行符处理 — 确保 \n 一致，去除 \r
    lines = [l.replace('\r\n', '\n').replace('\r', '\n') for l in lines]

    for cat, names in additions.items():
        sorted_names = sorted(names, key=channel_sort_key)
        cat_idx = -1
        for i, line in enumerate(lines):
            if line.strip() == cat:
                cat_idx = i
                break

        if cat_idx != -1:
            insert_idx = cat_idx + 1
            while insert_idx < len(lines):
                if "#genre#" in lines[insert_idx]:
                    break
                insert_idx += 1
            while insert_idx > 0 and lines[insert_idx - 1].strip() == "":
                insert_idx -= 1
            insert_lines = [n + "\n" for n in sorted_names]
            lines = lines[:insert_idx] + insert_lines + lines[insert_idx:]
        else:
            if lines and lines[-1].strip() != "":
                lines.append("\n")
            lines.append(cat + "\n")
            for n in sorted_names:
                lines.append(n + "\n")
            lines.append("\n")

    try:
        with open(DEMO_FILE, 'w', encoding='utf-8', newline='\n') as f:  # P1-10: 强制 LF
            f.writelines(lines)
        live_print(f"✅ 动作: config/demo.txt 已无损更新！原结构完美保留，底部已成功追加上述新频道。")
    except Exception as e:
        live_print(f"❌ 动作: config/demo.txt 更新失败: {e}")

    live_print("::endgroup::")
    return cat_order, chan_to_cat, chans_in_cat

# ===============================
# 7. 主程序
# ===============================

def apply_filter_lists(channels, blacklist_names, blacklist_urls, whitelist_names, whitelist_urls):
    """黑白名单 + 来源免测分流过滤
    
    channels: [(name, url, source_url), ...]
    - iptv-api 来源且数据新鲜 → 免测直入 valid_results（elapsed=-1）
    - 白名单 → 免测直入
    - 黑名单 → 拦截
    - 其余 → 进入 to_test 测速
    """
    to_test = []
    valid_results = {}
    logs_blacklist, logs_whitelist, logs_skip_test = [], [], []

    for name, url, source_url in channels:
        if name in blacklist_names or url in blacklist_urls:
            logs_blacklist.append(f"⚫ [黑名单屏蔽] {name:<12} | {url}")
            continue
        if name in whitelist_names or url in whitelist_urls:
            if name not in valid_results: valid_results[name] = []
            valid_results[name].append((url, -1.0))
            logs_whitelist.append(f"⚪ [白名单免测] {name:<12} | 免测 | {url}")
            continue
        # iptv-api 来源免测（数据已由 iptv-api 验证过分辨率+速率）
        if source_url == IPTV_API_SOURCE_URL:
            if name not in valid_results: valid_results[name] = []
            valid_results[name].append((url, -1.0))
            logs_skip_test.append(f"🔄 [iptv-api免测] {name:<12} | 免测 | {url}")
            continue
        to_test.append((name, url))

    return to_test, valid_results, logs_blacklist, logs_whitelist, logs_skip_test


def run_speed_test(to_test):
    """并发测速：返回 (valid_results, logs_success, logs_fail)"""
    valid_results = {}
    logs_success, logs_fail = [], []
    total = len(to_test)
    processed = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = [ex.submit(check_channel, name, url) for name, url in to_test]
        for future in concurrent.futures.as_completed(futures):
            processed += 1
            is_valid, name, url, elapsed, reason = future.result()
            progress = f"[{processed}/{total}]"
            if is_valid:
                if name not in valid_results: valid_results[name] = []
                valid_results[name].append((url, elapsed))
                msg = f"{progress} 🟢 {name:<12} | {elapsed:>4}s | {url}"
                live_print(msg)
                logs_success.append(msg)
            else:
                msg = f"{progress} 🔴 {name:<12} | {reason:<10} | {url}"
                logs_fail.append(msg)

    live_print(f"\n🏁 测速结束: 成功 {len(logs_success)} / 失败 {len(logs_fail)}\n")
    return valid_results, logs_success, logs_fail


def write_outputs(valid_results, cat_order, chans_in_cat, epg_report, logs_success, logs_fail, logs_whitelist, logs_skip_test, logs_blacklist):
    """写入 M3U/TXT 成品 + 日志文件"""
    live_print("::group::💾 写入结果文件")

    # 外部 fallback logo 基础 URL
    fallback_logo_base = "https://gh.felicity.ac.cn/https://raw.githubusercontent.com/taksssss/tv/main/icon"

    with open(OUTPUT_M3U, "w", encoding="utf-8") as fm3u, open(OUTPUT_TXT, "w", encoding="utf-8") as ftxt:
        fm3u.write(M3U_HEADER)
        for cat in cat_order:
            cat_written_in_txt = False
            for name in chans_in_cat.get(cat, []):
                if name in valid_results:
                    if not cat_written_in_txt:
                        ftxt.write(f"\n{cat}\n")
                        cat_written_in_txt = True

                    # elapsed=-1 排最前（白名单+iptv-api免测），其余按速度升序
                    valid_urls = sorted(valid_results[name], key=lambda x: (0 if x[1] < 0 else 1, x[1]))
                    for url, elapsed in valid_urls:
                        logo = get_local_logo_url(name)
                        if not logo:
                            logo = f"{fallback_logo_base}/{name}.png"

                        cat_clean = cat.split(',')[0]
                        elapsed_display = "免测" if elapsed < 0 else f"{elapsed}s"
                        fm3u.write(f'#EXTINF:-1 tvg-id="{name}" tvg-name="{name}" tvg-logo="{logo}" group-title="{cat_clean}",{name}\n')
                        fm3u.write(f"{url}\n")
                        ftxt.write(f"{name},{url}\n")

    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write(f"任务时间: {datetime.now()}\n")
        f.write(f"白名单免测: {len(logs_whitelist)} | iptv-api免测: {len(logs_skip_test)} | 黑名单拦截: {len(logs_blacklist)}\n")
        f.write(f"常规测速有效: {len(logs_success)} | 常规测速失效: {len(logs_fail)}\n\n")

        if epg_report:
            f.write("\n".join(epg_report) + "\n\n")

        if logs_whitelist:
            f.write("✅ 白名单免测:\n" + "\n".join(logs_whitelist) + "\n\n")

        if logs_skip_test:
            f.write("🔄 iptv-api免测:\n" + "\n".join(logs_skip_test) + "\n\n")

        if logs_blacklist:
            f.write("❌ 黑名单拦截:\n" + "\n".join(logs_blacklist) + "\n\n")

        f.write("🟢 测速有效源:\n" + "\n".join(logs_success) + "\n\n")
        f.write("🔴 测速失效源:\n" + "\n".join(logs_fail))

    live_print(f"✅ 所有结果文件已生成至 output/ 目录")
    live_print("::endgroup::")


if __name__ == "__main__":
    aliases_exact, aliases_regex, known_main_names = load_aliases()

    # 加载黑白名单
    blacklist_names, blacklist_urls = load_filter_lists(BLACKLIST_FILE)
    whitelist_names, whitelist_urls = load_filter_lists(WHITELIST_FILE)

    epg_report = download_and_merge_epg(aliases_exact, aliases_regex, known_main_names)

    try:
        cat_order, chan_to_cat, chans_in_cat = load_demo_template(aliases_exact, aliases_regex, known_main_names)
    except Exception as e:
        live_print(f"❌ config/demo.txt 加载严重错误: {e}")
        exit(1)

    channels = fetch_and_parse_channels(aliases_exact, aliases_regex, known_main_names)

    if not channels:
        live_print("⚠️ 未获取到任何有效直播源，退出。")
        exit(0)

    # 黑白名单 + 来源免测分流
    to_test, valid_results, logs_blacklist, logs_whitelist, logs_skip_test = apply_filter_lists(
        channels, blacklist_names, blacklist_urls, whitelist_names, whitelist_urls
    )
    skip_total = len(logs_whitelist) + len(logs_skip_test)
    live_print(f"\n🚀 开始测速 (待测: {len(to_test)} 条, 免测: 白名单{len(logs_whitelist)}+iptv-api{len(logs_skip_test)}={skip_total} 条, 拦截: {len(logs_blacklist)} 条)...\n")

    # 并发测速
    test_results, logs_success, logs_fail = run_speed_test(to_test)
    # 合并免测与测速结果（同名频道 URL 合并去重）
    for name, url_list in test_results.items():
        if name not in valid_results:
            valid_results[name] = url_list
        else:
            existing_urls = {u for u, _ in valid_results[name]}
            for url, elapsed in url_list:
                if url not in existing_urls:
                    valid_results[name].append((url, elapsed))
                    existing_urls.add(url)

    # 模板自进化
    cat_order, chan_to_cat, chans_in_cat = auto_update_demo(valid_results.keys(), cat_order, chan_to_cat, chans_in_cat)

    # 过滤空分类（测速后无任何存活频道的分类不写入输出）
    non_empty_cats = [cat for cat in cat_order if any(name in valid_results for name in chans_in_cat.get(cat, []))]
    if len(non_empty_cats) < len(cat_order):
        empty = len(cat_order) - len(non_empty_cats)
        live_print(f"🧹 过滤 {empty} 个空分类（无存活频道）")
        cat_order = non_empty_cats

    # 写入成品
    write_outputs(valid_results, cat_order, chans_in_cat, epg_report, logs_success, logs_fail, logs_whitelist, logs_skip_test, logs_blacklist)
