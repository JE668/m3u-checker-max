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
ICON_DIR = "icons"

OUTPUT_TXT = "output/live.txt"
OUTPUT_M3U = "output/live.m3u"
OUTPUT_EPG = "output/epg.xml"
OUTPUT_EPG_GZ = "output/epg.xml.gz"
LOG_FILE = "output/log.txt"

# M3U 头部 (CDN 加速)
M3U_HEADER = '#EXTM3U x-tvg-url="https://gh.llkk.cc/https://raw.githubusercontent.com/JE668/m3u-checker-max/main/output/epg.xml.gz"\n'

# EPG 垃圾词汇过滤库
EPG_BLACKLIST =[
    "未能提供", "暂无节目", "精彩节目", "精彩節目", 
    "没有节目", "未提供节目", "未提供節目", 
    "no program", "no data", "精彩剧集", "暂未提供"
]

os.makedirs("output", exist_ok=True)
os.makedirs("config", exist_ok=True)
os.makedirs(ICON_DIR, exist_ok=True)

def live_print(content):
    print(content, flush=True)

# ===============================
# 2. 核心字典：加载别名、图标与分类模板
# ===============================
def load_aliases():
    aliases_exact, aliases_regex = {},[]
    if not os.path.exists(ALIAS_FILE): return aliases_exact, aliases_regex
    with open(ALIAS_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'): continue
            parts = line.split(',')
            main_name = parts[0].strip()
            for alias in parts[1:]:
                alias = alias.strip()
                if alias.startswith("re:"):
                    try:
                        aliases_regex.append((re.compile(alias[3:]), main_name))
                    except: pass
                else:
                    aliases_exact[alias] = main_name
    return aliases_exact, aliases_regex

def get_main_name(raw_name, aliases_exact, aliases_regex):
    raw_name = raw_name.strip()
    if raw_name in aliases_exact: return aliases_exact[raw_name]
    if raw_name in aliases_exact.values(): return raw_name
    for reg, main_name in aliases_regex:
        if reg.match(raw_name): return main_name
    return raw_name

def get_local_logo_url(name):
    # 本地图标库最终在 GitHub Pages 的访问路径
    base_url = "https://gh.llkk.cc/https://raw.githubusercontent.com/JE668/m3u-checker-max/main/icons/"
    
    if not os.path.exists(ICON_DIR): return ""
    files = os.listdir(ICON_DIR)
    
    def clean(s): return re.sub(r'[^a-zA-Z0-9]', '', s).lower()
    
    target = clean(name)
    for f in files:
        if clean(os.path.splitext(f)[0]) == target:
            return base_url + f
            
    return "" 

def load_demo_template(aliases_exact, aliases_regex):
    category_order =[]
    channel_to_category = {}
    channels_in_category = {}
    
    if not os.path.exists(DEMO_FILE): return category_order, channel_to_category, channels_in_category
    
    current_category = None
    with open(DEMO_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') and "#genre#" not in line: continue
            
            if "#genre#" in line:
                current_category = line.split(',')[0].strip()
                if current_category not in category_order:
                    category_order.append(current_category)
                    channels_in_category[current_category] =[]
            elif current_category:
                raw_name = line
                # 记录的是标准名，保证 M3U 能够匹配到直播源
                main_name = get_main_name(raw_name, aliases_exact, aliases_regex)
                
                if current_category not in channels_in_category:
                    channels_in_category[current_category] =[]
                
                channel_to_category[main_name] = current_category
                if main_name not in channels_in_category[current_category]:
                    # 按原本的读取顺序追加，保证生成 m3u 时不乱序
                    channels_in_category[current_category].append(main_name)
                    
    return category_order, channel_to_category, channels_in_category

# ===============================
# 3. 抓取、清理与整合 EPG
# ===============================
def download_and_merge_epg(aliases_exact, aliases_regex):
    epg_urls =[]
    epg_report =[]
    if os.path.exists(EPG_FILE):
        with open(EPG_FILE, 'r', encoding='utf-8') as f:
            epg_urls =[line.strip() for line in f if line.strip() and not line.startswith('#')]
            
    if not epg_urls: return epg_report
    
    live_print("::group::📅 开始下载并整合 EPG 节目单")
    merged_tv = ET.Element("tv")
    merged_tv.set("generator-info-name", "Merged EPG by GitHub Actions")
    seen_channels, seen_programmes = set(), set()
    
    for url in epg_urls:
        if "gitee.com" in url and "/blob/" in url: url = url.replace("/blob/", "/raw/")
        elif "github.com" in url and "/blob/" in url: url = url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
            
        epg_report.append(f"▶ 来源: {url}")
        try:
            live_print(f"📥 正在获取: {url}")
            headers = {"User-Agent": "Mozilla/5.0"}
            r = requests.get(url, headers=headers, timeout=20)
            content = r.content
            if not content: continue
            if content.startswith(b'\x1f\x8b'):
                try: content = gzip.decompress(content)
                except: continue
            try:
                root = ET.parse(io.BytesIO(content)).getroot()
                if root.tag != 'tv': continue
            except: continue
            
            c_count, p_count, p_discard = 0, 0, 0
            rename_count = 0
            id_mapping = {}
            
            for channel in root.findall('channel'):
                orig_id = channel.get('id')
                display_name_elem = channel.find('display-name')
                if orig_id and display_name_elem is not None and display_name_elem.text:
                    orig_name = display_name_elem.text.strip()
                    main_name = get_main_name(orig_name, aliases_exact, aliases_regex)
                    
                    if orig_name != main_name: rename_count += 1
                    
                    id_mapping[orig_id] = main_name
                    channel.set('id', main_name)
                    display_name_elem.text = main_name
                    if main_name not in seen_channels:
                        seen_channels.add(main_name)
                        merged_tv.append(channel)
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
                        merged_tv.append(prog)
                        p_count += 1
            
            msg = f"   -> ✅ 提取频道: {c_count} | 节目: {p_count} | 🗑️ 过滤: {p_discard} | 🔧 修正名称: {rename_count}"
            live_print(msg); epg_report.append(msg)
        except Exception as e: 
            msg = f"   -> ❌ 异常: {e}"
            live_print(msg); epg_report.append(msg)

    if len(seen_channels) > 0:
        try:
            tree = ET.ElementTree(merged_tv)
            with open(OUTPUT_EPG, 'wb') as f:
                f.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
                tree.write(f, encoding='utf-8', xml_declaration=False)
            with open(OUTPUT_EPG, 'rb') as f_in, gzip.open(OUTPUT_EPG_GZ, 'wb') as f_out:
                f_out.writelines(f_in)
            final_msg = f"🎉 EPG 整合完成！规范频道数: {len(seen_channels)}"
            live_print(final_msg)
            epg_report.append("\n" + final_msg)
        except: pass
    live_print("::endgroup::")
    return epg_report

# ===============================
# 4. 抓取直播源
# ===============================
def fetch_and_parse_channels(aliases_exact, aliases_regex):
    channels =[]
    if not os.path.exists(SOURCES_FILE): return channels
    with open(SOURCES_FILE, 'r', encoding='utf-8') as f:
        sources =[line.strip() for line in f if line.strip() and not line.startswith('#')]
    
    seen_urls = set()
    live_print("::group::📥 开始抓取直播源")
    for url in sources:
        try:
            r = requests.get(url, timeout=10)
            r.encoding = 'utf-8'
            tmp_name = ""
            count = 0
            for line in r.text.splitlines():
                line = line.strip()
                if not line: continue
                if line.startswith("#EXTINF"):
                    tmp_name = line.split(",")[-1].strip()
                elif line.startswith("http"):
                    name = tmp_name if tmp_name else "未命名频道"
                    main_name = get_main_name(name, aliases_exact, aliases_regex)
                    if line not in seen_urls:
                        channels.append((main_name, line))
                        seen_urls.add(line); count += 1
                    tmp_name = ""
                elif "," in line and "://" in line:
                    parts = line.split(",", 1)
                    raw_name = parts[0].strip()
                    main_name = get_main_name(raw_name, aliases_exact, aliases_regex)
                    if parts[1].strip() not in seen_urls:
                        channels.append((main_name, parts[1].strip()))
                        seen_urls.add(parts[1].strip()); count += 1
            live_print(f"✅ {url} -> 提取 {count} 条")
        except: live_print(f"❌ 连接失败: {url}")
    live_print("::endgroup::")
    return channels

# ===============================
# 5. 并发测速
# ===============================
def check_channel(main_name, url):
    start_time = time.time()
    try:
        r = requests.get(url, stream=True, timeout=5)
        if r.status_code == 200:
            downloaded = 0
            for chunk in r.iter_content(chunk_size=1024 * 64):
                downloaded += len(chunk)
                if downloaded >= 1024 * 128:
                    return True, main_name, url, round(time.time() - start_time, 2), "成功"
                if time.time() - start_time > 5: 
                    return False, main_name, url, round(time.time() - start_time, 2), "超时无流"
        else: return False, main_name, url, round(time.time() - start_time, 2), f"Error {r.status_code}"
    except Exception: return False, main_name, url, round(time.time() - start_time, 2), "连接失败"
    return False, main_name, url, round(time.time() - start_time, 2), "未知"

# ===============================
# 6. 核心：无损追加模式进化 demo.txt
# ===============================
def channel_sort_key(name):
    nums = re.findall(r'\d+', name)
    val = int(nums[0]) if nums else 999
    name_upper = name.upper()
    if "4K" in name_upper and "CCTV" in name_upper: return (0, val, name)
    if "8K" in name_upper and "CCTV" in name_upper: return (1, val, name)
    if "CCTV" in name_upper: return (2, val, name)
    if "CETV" in name_upper: return (3, val, name)
    if "卫视" in name_upper: return (4, val, name)
    return (5, val, name)

def auto_update_demo(valid_names, cat_order, chan_to_cat, chans_in_cat):
    """采用绝对无损读取模式，只在对应分类底部追加新频道，绝不打乱原有排序和空行"""
    
    # 1. 识别真正的新频道 (不在现有分组里)
    new_channels = [n for n in valid_names if n not in chan_to_cat]
    if not new_channels:
        live_print("::group::🧠 自适应进化 demo.txt")
        live_print("✅ 未发现分类外的新频道，demo.txt 保持原样。")
        live_print("::endgroup::")
        return cat_order, chan_to_cat, chans_in_cat

    live_print("::group::🧠 自适应进化 demo.txt (无损追加模式)")
    
    # 2. 将新频道进行归类
    additions = {}
    for name in new_channels:
        name_upper = name.upper()
        if "4K" in name_upper or "8K" in name_upper: cat = "☘️4K/8K超高清频道,#genre#"
        elif "CCTV" in name_upper or "CETV" in name_upper: cat = "📺央视频道,#genre#"
        elif "卫视" in name_upper: cat = "📡卫视频道,#genre#"
        else: cat = "📺其他频道,#genre#"
        
        additions.setdefault(cat,[]).append(name)
        
        # 同步更新内存里的结构，以供后续输出 M3U 使用
        if cat not in cat_order:
            cat_order.append(cat)
            chans_in_cat[cat] =[]
        chans_in_cat[cat].append(name)
        chan_to_cat[name] = cat
        live_print(f"   -> 🆕 追加收录: [{name}] => [{cat.split(',')[0]}]")

    # 3. 读取原版 demo.txt，以纯文本行数组形式操作
    if os.path.exists(DEMO_FILE):
        with open(DEMO_FILE, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    else:
        lines =[]

    # 4. 精准定位并插入新频道
    for cat, names in additions.items():
        # 新增频道按规则简单排个序，避免插进去乱糟糟的
        sorted_names = sorted(names, key=channel_sort_key)
        
        # 寻找分类在文本中的位置
        cat_idx = -1
        for i, line in enumerate(lines):
            if line.strip() == cat:
                cat_idx = i
                break
                
        if cat_idx != -1:
            # 找到了该分类。往下找，直到遇到下一个分类 (#genre#) 或文本结尾
            insert_idx = cat_idx + 1
            while insert_idx < len(lines):
                if "#genre#" in lines[insert_idx]:
                    break
                insert_idx += 1
            
            # 为了美观，把插入点提起到换行符前面（如果有的话）
            while insert_idx > 0 and lines[insert_idx-1].strip() == "":
                insert_idx -= 1
                
            # 切片组合注入！
            insert_lines =[n + "\n" for n in sorted_names]
            lines = lines[:insert_idx] + insert_lines + lines[insert_idx:]
        else:
            # 文本里根本没有这个分类，那就全量加在最后面
            if lines and lines[-1].strip() != "":
                lines.append("\n")
            lines.append(cat + "\n")
            for n in sorted_names:
                lines.append(n + "\n")
            lines.append("\n")

    # 5. 回写覆盖 demo.txt
    try:
        with open(DEMO_FILE, 'w', encoding='utf-8') as f:
            f.writelines(lines)
        live_print(f"✅ demo.txt 无损更新完毕！原结构已完美保留。")
    except Exception as e:
        live_print(f"❌ demo.txt 更新失败: {e}")
        
    live_print("::endgroup::")
    return cat_order, chan_to_cat, chans_in_cat

# ===============================
# 7. 主程序
# ===============================
if __name__ == "__main__":
    aliases_exact, aliases_regex = load_aliases()
    epg_report = download_and_merge_epg(aliases_exact, aliases_regex)
    
    try:
        cat_order, chan_to_cat, chans_in_cat = load_demo_template(aliases_exact, aliases_regex)
    except Exception as e:
        live_print(f"❌ demo.txt 加载严重错误: {e}")
        exit(1)
        
    channels = fetch_and_parse_channels(aliases_exact, aliases_regex)
    
    if not channels: 
        live_print("⚠️ 未获取到任何有效直播源，退出。")
        exit(0)

    live_print(f"\n🚀 开始全量测速 (总数: {len(channels)} 个，并发: 100)...\n")
    
    valid_results = {}
    logs_success, logs_fail = [],[]
    total = len(channels)
    processed = 0
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=100) as ex:
        futures =[ex.submit(check_channel, name, url) for name, url in channels]
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
                live_print(msg)
                logs_fail.append(msg)

    live_print(f"\n🏁 测速结束: 有效 {len(logs_success)} / 失效 {len(logs_fail)}\n")

    # 🌟 调用全新的无损进化模块
    cat_order, chan_to_cat, chans_in_cat = auto_update_demo(valid_results.keys(), cat_order, chan_to_cat, chans_in_cat)

    live_print("::group::💾 写入结果文件")
    with open(OUTPUT_M3U, "w", encoding="utf-8") as fm3u, open(OUTPUT_TXT, "w", encoding="utf-8") as ftxt:
        fm3u.write(M3U_HEADER)
        # 保证按照 demo.txt 原有的分类顺序和频道顺序输出！
        for cat in cat_order:
            cat_written_in_txt = False
            for name in chans_in_cat.get(cat,[]):
                if name in valid_results:
                    if not cat_written_in_txt:
                        ftxt.write(f"\n{cat}\n")
                        cat_written_in_txt = True
                    
                    # 只有同名的不同测速链接才按速度排序
                    valid_urls = sorted(valid_results[name], key=lambda x: x[1]) 
                    for url, elapsed in valid_urls:
                        logo = get_local_logo_url(name)
                        if not logo:
                            logo = f"https://gh.llkk.cc/https://raw.githubusercontent.com/taksssss/tv/main/icon/{name}.png"
                            
                        cat_clean = cat.split(',')[0]
                        fm3u.write(f'#EXTINF:-1 tvg-id="{name}" tvg-name="{name}" tvg-logo="{logo}" group-title="{cat_clean}",{name}\n')
                        fm3u.write(f"{url}\n")
                        ftxt.write(f"{name},{url}\n")
    
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write(f"任务时间: {datetime.now()}\n")
        f.write(f"有效源: {len(logs_success)} | 失效源: {len(logs_fail)}\n\n")
        if epg_report:
            f.write("\n".join(epg_report) + "\n\n")
        f.write("✅ 有效源:\n" + "\n".join(logs_success) + "\n\n")
        f.write("❌ 失效源:\n" + "\n".join(logs_fail))
    
    live_print("✅ 所有文件已生成至 output/ 目录")
    live_print("::endgroup::")
