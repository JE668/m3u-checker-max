import os, time, concurrent.futures, requests, gzip, io, re
import xml.etree.ElementTree as ET
from datetime import datetime

# ===============================
# 1. 核心配置区
# ===============================
SOURCES_FILE = "UPSTREAM_SOURCES.txt"
EPG_FILE = "UPSTREAM_EPG.txt"
ALIAS_FILE = "alias.txt"
DEMO_FILE = "demo.txt"

OUTPUT_TXT = "live.txt"
OUTPUT_M3U = "live.m3u"
OUTPUT_EPG = "epg.xml"
OUTPUT_EPG_GZ = "epg.xml.gz"
LOG_FILE = "log.txt"

M3U_HEADER = '#EXTM3U x-tvg-url="https://raw.githubusercontent.com/JE668/m3u-checker-max/refs/heads/main/epg.xml"\n'

def live_print(content):
    print(content, flush=True)

# ===============================
# 2. 核心字典：加载别名与分类
# ===============================
def load_aliases():
    """读取别名文件，生成精准匹配字典和正则匹配列表"""
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
    """根据别名规则获取标准名称"""
    if raw_name in aliases_exact: return aliases_exact[raw_name]
    if raw_name in aliases_exact.values(): return raw_name
    for reg, main_name in aliases_regex:
        if reg.match(raw_name): return main_name
    return raw_name

def load_demo_template():
    """读取 demo.txt，获取频道分类和排序骨架"""
    category_order =[]
    channel_to_category = {}
    channels_in_category = {}
    
    if not os.path.exists(DEMO_FILE): return category_order, channel_to_category, channels_in_category
    
    current_category = "未分类频道"
    with open(DEMO_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') and "#genre#" not in line: continue
            if "#genre#" in line:
                current_category = line.split(',')[0].strip()
                if current_category not in category_order:
                    category_order.append(current_category)
                    channels_in_category[current_category] =[]
            else:
                main_name = line
                channel_to_category[main_name] = current_category
                if main_name not in channels_in_category[current_category]:
                    channels_in_category[current_category].append(main_name)
    return category_order, channel_to_category, channels_in_category

# ===============================
# 3. 抓取与整合 EPG
# ===============================
def download_and_merge_epg():
    epg_urls =[]
    if os.path.exists(EPG_FILE):
        with open(EPG_FILE, 'r', encoding='utf-8') as f:
            epg_urls =[line.strip() for line in f if line.strip() and not line.startswith('#')]
            
    if not epg_urls: return
    live_print("::group::📅 开始下载并整合 EPG 节目单")
    merged_tv = ET.Element("tv")
    merged_tv.set("generator-info-name", "Merged EPG by GitHub Actions")
    seen_channels, seen_programmes = set(), set()
    
    for url in epg_urls:
        try:
            r = requests.get(url, timeout=20)
            content = gzip.decompress(r.content) if url.endswith('.gz') or r.headers.get('Content-Encoding') == 'gzip' else r.content
            root = ET.parse(io.BytesIO(content)).getroot()
            if root.tag != 'tv': continue
            
            for channel in root.findall('channel'):
                c_id = channel.get('id')
                if c_id not in seen_channels:
                    seen_channels.add(c_id); merged_tv.append(channel)
            for prog in root.findall('programme'):
                key = (prog.get('channel'), prog.get('start'), prog.get('stop'))
                if key not in seen_programmes:
                    seen_programmes.add(key); merged_tv.append(prog)
        except: pass

    # 写入 xml 并使用 gzip 压缩生成 .gz
    try:
        tree = ET.ElementTree(merged_tv)
        with open(OUTPUT_EPG, 'wb') as f:
            f.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
            tree.write(f, encoding='utf-8', xml_declaration=False)
        with open(OUTPUT_EPG, 'rb') as f_in, gzip.open(OUTPUT_EPG_GZ, 'wb') as f_out:
            f_out.writelines(f_in)
        live_print(f"🎉 EPG 整合完成，已生成 {OUTPUT_EPG} 与 {OUTPUT_EPG_GZ}")
    except Exception as e:
        live_print(f"❌ EPG 保存失败: {e}")
    live_print("::endgroup::")

# ===============================
# 4. 抓取直播源并进行别名映射
# ===============================
def fetch_and_parse_channels(aliases_exact, aliases_regex):
    channels =[]
    if not os.path.exists(SOURCES_FILE): return channels
    with open(SOURCES_FILE, 'r', encoding='utf-8') as f:
        sources =[line.strip() for line in f if line.strip() and not line.startswith('#')]
    
    seen_urls = set()
    for url in sources:
        try:
            r = requests.get(url, timeout=10)
            r.encoding = 'utf-8'
            tmp_name = ""
            for line in r.text.splitlines():
                line = line.strip()
                if not line: continue
                if line.startswith("#EXTINF"):
                    tmp_name = line.split(",")[-1].strip()
                elif line.startswith("http"):
                    name = tmp_name if tmp_name else "未命名频道"
                    # 关键：应用别名映射
                    main_name = get_main_name(name, aliases_exact, aliases_regex)
                    if line not in seen_urls:
                        channels.append((main_name, line))
                        seen_urls.add(line)
                    tmp_name = ""
                elif "," in line and "://" in line:
                    parts = line.split(",", 1)
                    main_name = get_main_name(parts[0].strip(), aliases_exact, aliases_regex)
                    if parts[1].strip() not in seen_urls:
                        channels.append((main_name, parts[1].strip()))
                        seen_urls.add(parts[1].strip())
        except: pass
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
                    return True, main_name, url, round(time.time() - start_time, 2)
                if time.time() - start_time > 5: break
    except: pass
    return False, main_name, url, 0

# ===============================
# 6. 主程序
# ===============================
if __name__ == "__main__":
    download_and_merge_epg()
    
    aliases_exact, aliases_regex = load_aliases()
    cat_order, chan_to_cat, chans_in_cat = load_demo_template()
    
    channels = fetch_and_parse_channels(aliases_exact, aliases_regex)
    if not channels: exit(0)

    live_print(f"::group::🎬 开始全量测速 (共 {len(channels)} 个独立链接)")
    valid_results = {}  # { main_name:[(url, elapsed)] }
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=100) as ex:
        futures =[ex.submit(check_channel, name, url) for name, url in channels]
        for future in concurrent.futures.as_completed(futures):
            is_valid, name, url, elapsed = future.result()
            if is_valid:
                if name not in valid_results: valid_results[name] = []
                valid_results[name].append((url, elapsed))
                live_print(f"🟢 [有效] {name:<15} | 耗时 {elapsed}s")
    live_print("::endgroup::")

    # ===============================
    # 7. 组装输出结构 (按照 demo.txt 排序)
    # ===============================
    live_print("::group::💾 正在写入最终文件")
    tvg_id = 1
    
    with open(OUTPUT_M3U, "w", encoding="utf-8") as fm3u, open(OUTPUT_TXT, "w", encoding="utf-8") as ftxt:
        fm3u.write(M3U_HEADER)
        
        # 处理在 demo.txt 中的频道
        for cat in cat_order:
            cat_written_in_txt = False
            for name in chans_in_cat[cat]:
                if name in valid_results:
                    if not cat_written_in_txt:
                        ftxt.write(f"\n{cat},#genre#\n")
                        cat_written_in_txt = True
                    
                    # 取出测速有效的链接，可以选择按耗时排序 (这里保留原始并发完成顺序或可改排序)
                    valid_urls = sorted(valid_results[name], key=lambda x: x[1]) 
                    for url, _ in valid_urls:
                        logo = f"https://gcore.jsdelivr.net/gh/taksssss/tv/icon/{name}.png"
                        fm3u.write(f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{name}" tvg-logo="{logo}" group-title="{cat}",{name}\n')
                        fm3u.write(f"{url}\n")
                        ftxt.write(f"{name},{url}\n")
                    tvg_id += 1
                    
        # 处理没有在 demo.txt 中定义，但存活的 "其他频道"
        other_channels =[n for n in valid_results.keys() if n not in chan_to_cat]
        if other_channels:
            ftxt.write(f"\n📺其他频道,#genre#\n")
            for name in other_channels:
                valid_urls = sorted(valid_results[name], key=lambda x: x[1])
                for url, _ in valid_urls:
                    logo = f"https://gcore.jsdelivr.net/gh/taksssss/tv/icon/{name}.png"
                    fm3u.write(f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{name}" tvg-logo="{logo}" group-title="📺其他频道",{name}\n')
                    fm3u.write(f"{url}\n")
                    ftxt.write(f"{name},{url}\n")
                tvg_id += 1

    live_print("✅ 文件写入完成！")
    live_print("::endgroup::")
