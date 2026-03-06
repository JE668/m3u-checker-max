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

# 修改为经过 CDN 加速的 gz 格式 EPG 链接
M3U_HEADER = '#EXTM3U x-tvg-url="https://gh.llkk.cc/https://raw.githubusercontent.com/JE668/m3u-checker-max/refs/heads/main/epg.xml.gz"\n'

def live_print(content):
    print(content, flush=True)

# ===============================
# 2. 核心字典：加载别名与分类
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
    live_print(f"✅ 加载别名配置: 成功载入精确映射 {len(aliases_exact)} 个，正则映射 {len(aliases_regex)} 个。")
    return aliases_exact, aliases_regex

def get_main_name(raw_name, aliases_exact, aliases_regex):
    if raw_name in aliases_exact: return aliases_exact[raw_name]
    if raw_name in aliases_exact.values(): return raw_name
    for reg, main_name in aliases_regex:
        if reg.match(raw_name): return main_name
    return raw_name

def load_demo_template():
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
                    
    total_channels = sum(len(v) for v in channels_in_category.values())
    live_print(f"✅ 加载分类模板: 共识别 {len(category_order)} 个大类，包含 {total_channels} 个基础频道定义。")
    return category_order, channel_to_category, channels_in_category

# ===============================
# 3. 抓取与整合 EPG (彻底修复版本)
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
            live_print(f"📥 正在获取 EPG: {url}")
            # 加入假装浏览器的 Headers 避免被拦截返回 HTML
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36"}
            r = requests.get(url, headers=headers, timeout=20)
            
            content = r.content
            if not content:
                live_print("   -> ❌ 获取失败: 数据为空")
                continue
                
            # 🌟 智能解压核心：通过 gzip 的"魔法头部"(\x1f\x8b)判断是否为真压缩流
            if content.startswith(b'\x1f\x8b'):
                try:
                    content = gzip.decompress(content)
                except Exception as e:
                    live_print(f"   -> ❌ Gzip 解压失败: {e}")
                    continue

            # 开始尝试解析 XML
            try:
                root = ET.parse(io.BytesIO(content)).getroot()
                if root.tag != 'tv': 
                    live_print("   -> ❌ 内容不是标准 EPG 格式 (未发现 <tv> 标签)")
                    continue
            except ET.ParseError as e:
                # 截取前 30 个字符并打印，方便诊断到底是哪个网页返回了错误
                preview = content[:30].decode('utf-8', errors='ignore').replace('\n', ' ')
                live_print(f"   -> ❌ XML 解析失败: {e} (疑似非XML数据，文件头: {preview})")
                continue
            
            # 数据提取
            c_count, p_count = 0, 0
            for channel in root.findall('channel'):
                c_id = channel.get('id')
                if c_id not in seen_channels:
                    seen_channels.add(c_id); merged_tv.append(channel); c_count += 1
            for prog in root.findall('programme'):
                key = (prog.get('channel'), prog.get('start'), prog.get('stop'))
                if key not in seen_programmes:
                    seen_programmes.add(key); merged_tv.append(prog); p_count += 1
            live_print(f"   -> ✅ 成功提取: {c_count} 个频道，{p_count} 条节目单")
        except Exception as e: 
            live_print(f"   -> ❌ 获取异常: {type(e).__name__} ({e})")

    # 保存文件
    if len(seen_channels) > 0:
        try:
            tree = ET.ElementTree(merged_tv)
            with open(OUTPUT_EPG, 'wb') as f:
                f.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
                tree.write(f, encoding='utf-8', xml_declaration=False)
            with open(OUTPUT_EPG, 'rb') as f_in, gzip.open(OUTPUT_EPG_GZ, 'wb') as f_out:
                f_out.writelines(f_in)
            live_print(f"🎉 EPG 整合完成！共去重整合 {len(seen_channels)} 个频道，{len(seen_programmes)} 条节目。")
        except Exception as e:
            live_print(f"❌ EPG 保存失败: {e}")
    else:
        live_print("⚠️ 所有 EPG 获取均失败，本次未生成/更新 EPG 文件。")
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
    live_print("::group::📥 开始抓取并解析上游直播源")
    for url in sources:
        try:
            live_print(f"正在获取: {url}")
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
                        seen_urls.add(line)
                        count += 1
                    tmp_name = ""
                elif "," in line and "://" in line:
                    parts = line.split(",", 1)
                    main_name = get_main_name(parts[0].strip(), aliases_exact, aliases_regex)
                    if parts[1].strip() not in seen_urls:
                        channels.append((main_name, parts[1].strip()))
                        seen_urls.add(parts[1].strip())
                        count += 1
            live_print(f"   -> 成功获取且去重 {count} 条独立链接")
        except Exception as e: 
            live_print(f"   -> ❌ 获取失败: {e}")
    live_print(f"✅ 上游抓取完毕，共计排队待测频道: {len(channels)} 个")
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
                    return False, main_name, url, round(time.time() - start_time, 2), "下载超时(流无数据)"
        else:
            return False, main_name, url, round(time.time() - start_time, 2), f"状态码异常: {r.status_code}"
    except requests.exceptions.Timeout:
        return False, main_name, url, round(time.time() - start_time, 2), "请求超时(Timeout)"
    except requests.exceptions.ConnectionError:
        return False, main_name, url, round(time.time() - start_time, 2), "连接失败(ConnectionError)"
    except Exception as e:
        return False, main_name, url, round(time.time() - start_time, 2), f"其他错误: {type(e).__name__}"
    return False, main_name, url, round(time.time() - start_time, 2), "未知错误"

# ===============================
# 6. 主程序
# ===============================
if __name__ == "__main__":
    download_and_merge_epg()
    
    aliases_exact, aliases_regex = load_aliases()
    cat_order, chan_to_cat, chans_in_cat = load_demo_template()
    
    channels = fetch_and_parse_channels(aliases_exact, aliases_regex)
    if not channels: 
        live_print("⚠️ 没有获取到任何待测频道，程序退出。")
        exit(0)

    live_print(f"::group::🎬 开始全量测速 (并发量: 100)")
    valid_results = {}  
    logs_success, logs_fail = [],[]
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=100) as ex:
        futures =[ex.submit(check_channel, name, url) for name, url in channels]
        for future in concurrent.futures.as_completed(futures):
            is_valid, name, url, elapsed, reason = future.result()
            if is_valid:
                if name not in valid_results: valid_results[name] = []
                valid_results[name].append((url, elapsed))
                msg = f"🟢 [有效] {name:<15} | 耗时 {elapsed}s | {url}"
                live_print(msg)
                logs_success.append(msg)
            else:
                msg = f"🔴 [失效] {name:<15} | 耗时 {elapsed}s | {reason:<15} | {url}"
                logs_fail.append(msg)
    live_print("::endgroup::")

    # ===============================
    # 7. 组装输出与文件写入
    # ===============================
    live_print("::group::💾 正在写入最终文件与详尽日志")
    tvg_id = 1
    
    with open(OUTPUT_M3U, "w", encoding="utf-8") as fm3u, open(OUTPUT_TXT, "w", encoding="utf-8") as ftxt:
        fm3u.write(M3U_HEADER)
        
        for cat in cat_order:
            cat_written_in_txt = False
            for name in chans_in_cat[cat]:
                if name in valid_results:
                    if not cat_written_in_txt:
                        ftxt.write(f"\n{cat},#genre#\n")
                        cat_written_in_txt = True
                    
                    # 按速度最快排序 (耗时短到长)
                    valid_urls = sorted(valid_results[name], key=lambda x: x[1]) 
                    for url, elapsed in valid_urls:
                        logo = f"https://gcore.jsdelivr.net/gh/taksssss/tv/icon/{name}.png"
                        fm3u.write(f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{name}" tvg-logo="{logo}" group-title="{cat}",{name}\n')
                        fm3u.write(f"{url}\n")
                        ftxt.write(f"{name},{url}\n")
                    tvg_id += 1
                    
        other_channels =[n for n in valid_results.keys() if n not in chan_to_cat]
        if other_channels:
            ftxt.write(f"\n📺其他频道,#genre#\n")
            for name in other_channels:
                valid_urls = sorted(valid_results[name], key=lambda x: x[1])
                for url, elapsed in valid_urls:
                    logo = f"https://gcore.jsdelivr.net/gh/taksssss/tv/icon/{name}.png"
                    fm3u.write(f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{name}" tvg-logo="{logo}" group-title="📺其他频道",{name}\n')
                    fm3u.write(f"{url}\n")
                    ftxt.write(f"{name},{url}\n")
                tvg_id += 1

    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write(f"=============== 频道检测详细报告 ===============\n")
        f.write(f"任务时间: {datetime.now()}\n")
        f.write(f"上游抓取总数: {len(channels)} 个链接\n")
        f.write(f"最终有效总数: {len(logs_success)} 个链接\n")
        f.write(f"过滤失效总数: {len(logs_fail)} 个链接\n")
        f.write(f"================================================\n\n")
        f.write("✅ 存活链接详情 (按处理顺序):\n")
        f.write("\n".join(logs_success) + "\n\n")
        f.write("❌ 失效链接详情 (含失败原因):\n")
        f.write("\n".join(logs_fail))

    live_print(f"✅ 文件写入完成！详细报告已生成至 {LOG_FILE}")
    live_print("::endgroup::")
