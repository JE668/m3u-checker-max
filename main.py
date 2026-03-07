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
UNMATCHED_FILE = "output/unmatched.txt"

# 🌟 M3U 头部 (已更换为新 CDN 加速)
M3U_HEADER = '#EXTM3U x-tvg-url="https://gh.felicity.ac.cn/https://raw.githubusercontent.com/JE668/m3u-checker-max/main/output/epg.xml.gz"\n'

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
                    except: pass
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

def get_local_logo_url(name):
    """智能图标匹配引擎 (支持降级兜底)"""
    # 🌟 本地图标库地址 (已更换为新 CDN 加速)
    base_url = "https://gh.felicity.ac.cn/https://raw.githubusercontent.com/JE668/m3u-checker-max/main/icons/"
    if not os.path.exists(ICON_DIR): return ""
    files = os.listdir(ICON_DIR)
    
    def clean(s): return re.sub(r'[^a-zA-Z0-9\+]', '', s).lower()
    
    target = clean(name)
    
    # 第一层：精确匹配
    for f in files:
        if clean(os.path.splitext(f)[0]) == target:
            return base_url + f
            
    # 第二层：降级匹配
    base_name = re.sub(r'(?i)[\-\s\_]*(4k|8k|hd|fhd|超清|高清|标清|测试)$', '', name)
    if base_name != name:
        target_base = clean(base_name)
        for f in files:
            if clean(os.path.splitext(f)[0]) == target_base:
                return base_url + f
                
    return "" 

def load_demo_template(aliases_exact, aliases_regex, known_main_names):
    category_order =[]
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
            if not line or line.startswith('#') and "#genre#" not in line: continue
            
            if "#genre#" in line:
                current_category = line.split(',')[0].strip()
                if current_category not in category_order:
                    category_order.append(current_category)
                    channels_in_category[current_category] =[]
            elif current_category:
                raw_name = line
                main_name = get_main_name(raw_name, aliases_exact, aliases_regex, known_main_names)
                
                if current_category not in channels_in_category:
                    channels_in_category[current_category] =[]
                
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
def download_and_merge_epg(aliases_exact, aliases_regex, known_main_names):
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
            seen_epg_renames = set()
            
            for channel in root.findall('channel'):
                orig_id = channel.get('id')
                display_name_elem = channel.find('display-name')
                if orig_id and display_name_elem is not None and display_name_elem.text:
                    orig_name = display_name_elem.text.strip()
                    main_name = get_main_name(orig_name, aliases_exact, aliases_regex, known_main_names)
                    
                    if orig_name != main_name: 
                        rename_count += 1
                        if (orig_name, main_name) not in seen_epg_renames:
                            live_print(f"   📝 [EPG修正] {orig_name} => {main_name}")
                            seen_epg_renames.add((orig_name, main_name))
                    
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
            
            msg = f"   -> ✅ 提取频道: {c_count} | 节目: {p_count} | 🗑️ 过滤: {p_discard} | 🔧 总修正: {rename_count}次"
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
def fetch_and_parse_channels(aliases_exact, aliases_regex, known_main_names):
    channels =[]
    unmatched_names = set() 
    
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
            seen_source_renames = set()
            
            for line in r.text.splitlines():
                line = line.strip()
                if not line: continue
                if line.startswith("#EXTINF"):
                    tmp_name = line.split(",")[-1].strip()
                elif line.startswith("http"):
                    name = tmp_name if tmp_name else "未命名频道"
                    main_name = get_main_name(name, aliases_exact, aliases_regex, known_main_names, unmatched_names)
                    
                    if name != main_name and (name, main_name) not in seen_source_renames:
                        live_print(f"   📝 [名称修正] {name} => {main_name}")
                        seen_source_renames.add((name, main_name))
                        
                    if line not in seen_urls:
                        channels.append((main_name, line))
                        seen_urls.add(line); count += 1
                    tmp_name = ""
                elif "," in line and "://" in line:
                    parts = line.split(",", 1)
                    raw_name = parts[0].strip()
                    main_name = get_main_name(raw_name, aliases_exact, aliases_regex, known_main_names, unmatched_names)
                    
                    if raw_name != main_name and (raw_name, main_name) not in seen_source_renames:
                        live_print(f"   📝 [名称修正] {raw_name} => {main_name}")
                        seen_source_renames.add((raw_name, main_name))
                        
                    if parts[1].strip() not in seen_urls:
                        channels.append((main_name, parts[1].strip()))
                        seen_urls.add(parts[1].strip()); count += 1
            live_print(f"✅ {url} -> 提取 {count} 条")
        except: live_print(f"❌ 连接失败: {url}")
        
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
    live_print("\n::group::🧠 自适应进化 config/demo.txt (无损追加模式)")
    
    new_channels =[n for n in valid_names if n not in chan_to_cat]
    
    if not new_channels:
        live_print("ℹ️ 状态: 测速存活的频道均已存在于 config/demo.txt 当前分组中。")
        live_print("✅ 动作: 模板保持原样，无需写入更新。")
        live_print("::endgroup::")
        return cat_order, chan_to_cat, chans_in_cat

    live_print(f"ℹ️ 状态: 发现了 {len(new_channels)} 个全新的存活频道！准备自动归类并追加写入...")
    
    additions = {}
    for name in new_channels:
        name_upper = name.upper()
        if "4K" in name_upper or "8K" in name_upper: cat = "☘️4K/8K超高清频道,#genre#"
        elif "CCTV" in name_upper or "CETV" in name_upper: cat = "📺央视频道,#genre#"
        elif "卫视" in name_upper: cat = "📡卫视频道,#genre#"
        else: cat = "📺其他频道,#genre#"
        
        additions.setdefault(cat,[]).append(name)
        
        if cat not in cat_order:
            cat_order.append(cat)
            chans_in_cat[cat] =[]
        chans_in_cat[cat].append(name)
        chan_to_cat[name] = cat
        live_print(f"   -> 🆕 自动追加: [{name}] 归入 [{cat.split(',')[0]}]")

    if os.path.exists(DEMO_FILE):
        with open(DEMO_FILE, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    else:
        lines =[]

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
            while insert_idx > 0 and lines[insert_idx-1].strip() == "":
                insert_idx -= 1
            insert_lines =[n + "\n" for n in sorted_names]
            lines = lines[:insert_idx] + insert_lines + lines[insert_idx:]
        else:
            if lines and lines[-1].strip() != "":
                lines.append("\n")
            lines.append(cat + "\n")
            for n in sorted_names:
                lines.append(n + "\n")
            lines.append("\n")

    try:
        with open(DEMO_FILE, 'w', encoding='utf-8') as f:
            f.writelines(lines)
        live_print(f"✅ 动作: config/demo.txt 已无损更新！原结构完美保留，底部已成功追加上述新频道。")
    except Exception as e:
        live_print(f"❌ 动作: config/demo.txt 更新失败: {e}")
        
    live_print("::endgroup::")
    return cat_order, chan_to_cat, chans_in_cat

# ===============================
# 7. 主程序
# ===============================
if __name__ == "__main__":
    aliases_exact, aliases_regex, known_main_names = load_aliases()
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
                logs_fail.append(msg)

    live_print(f"\n🏁 测速结束: 有效 {len(logs_success)} / 失效 {len(logs_fail)}\n")

    cat_order, chan_to_cat, chans_in_cat = auto_update_demo(valid_results.keys(), cat_order, chan_to_cat, chans_in_cat)

    live_print("::group::💾 写入结果文件")
    with open(OUTPUT_M3U, "w", encoding="utf-8") as fm3u, open(OUTPUT_TXT, "w", encoding="utf-8") as ftxt:
        fm3u.write(M3U_HEADER)
        for cat in cat_order:
            cat_written_in_txt = False
            for name in chans_in_cat.get(cat,[]):
                if name in valid_results:
                    if not cat_written_in_txt:
                        ftxt.write(f"\n{cat}\n")
                        cat_written_in_txt = True
                    
                    valid_urls = sorted(valid_results[name], key=lambda x: x[1]) 
                    for url, elapsed in valid_urls:
                        logo = get_local_logo_url(name)
                        if not logo:
                            # 🌟 远程图标兜底 (已更换为新 CDN 加速)
                            logo = f"https://gh.felicity.ac.cn/https://raw.githubusercontent.com/taksssss/tv/main/icon/{name}.png"
                            
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
    
    live_print(f"✅ 所有结果文件已生成至 output/ 目录")
    live_print("::endgroup::")
