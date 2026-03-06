import os, time, concurrent.futures, requests, gzip, io
import xml.etree.ElementTree as ET
from datetime import datetime

# ===============================
# 1. 核心配置区
# ===============================
SOURCES_FILE = "UPSTREAM_SOURCES.txt"
EPG_FILE = "UPSTREAM_EPG.txt"

OUTPUT_TXT = "live.txt"
OUTPUT_M3U = "live.m3u"
OUTPUT_EPG = "epg.xml"
LOG_FILE = "log.txt"

def live_print(content):
    print(content, flush=True)

def load_urls_from_file(filename):
    """从本地 txt 文件读取 URL 列表，忽略空行和注释"""
    if not os.path.exists(filename):
        live_print(f"⚠️ 未找到配置文件: {filename}")
        return[]
    with open(filename, 'r', encoding='utf-8') as f:
        return [line.strip() for line in f if line.strip() and not line.startswith('#')]

# ===============================
# 2. 抓取与整合 EPG (支持 .gz 解压与去重)
# ===============================
def download_and_merge_epg(epg_urls):
    if not epg_urls:
        return None
        
    live_print("::group::📅 开始下载并整合 EPG 节目单")
    merged_tv = ET.Element("tv")
    merged_tv.set("generator-info-name", "Merged EPG by GitHub Actions")
    
    seen_channels = set()
    seen_programmes = set()
    
    for url in epg_urls:
        live_print(f"📥 正在获取 EPG: {url}")
        try:
            r = requests.get(url, timeout=20)
            if r.status_code != 200:
                live_print(f"⚠️ EPG 获取失败，状态码 {r.status_code}")
                continue
            
            content = r.content
            # 判断是否为 gz 压缩格式
            if url.endswith('.gz') or r.headers.get('Content-Encoding') == 'gzip':
                try:
                    content = gzip.decompress(content)
                except Exception as e:
                    live_print(f"⚠️ Gzip 解压失败: {e}")
                    continue
            
            # 解析 XML
            try:
                tree = ET.parse(io.BytesIO(content))
                root = tree.getroot()
            except ET.ParseError as e:
                live_print(f"⚠️ XML 解析失败: {e}")
                continue
                
            if root.tag != 'tv':
                live_print("⚠️ 根节点不是 <tv>，跳过。")
                continue
                
            # 处理频道信息去重 (<channel> 标签基于 id 去重)
            new_channels = 0
            for channel in root.findall('channel'):
                c_id = channel.get('id')
                if c_id not in seen_channels:
                    seen_channels.add(c_id)
                    merged_tv.append(channel)
                    new_channels += 1
                    
            # 处理节目单信息去重 (<programme> 标签基于 频道+开始+结束时间 去重)
            new_progs = 0
            for prog in root.findall('programme'):
                key = (prog.get('channel'), prog.get('start'), prog.get('stop'))
                if key not in seen_programmes:
                    seen_programmes.add(key)
                    merged_tv.append(prog)
                    new_progs += 1
                    
            live_print(f"✅ 成功整合 (新增频道: {new_channels}, 新增节目: {new_progs})")
        except Exception as e:
            live_print(f"❌ 处理 EPG 异常 {url}: {e}")
            
    # 保存合并后的 EPG
    try:
        tree = ET.ElementTree(merged_tv)
        with open(OUTPUT_EPG, 'wb') as f:
            f.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
            tree.write(f, encoding='utf-8', xml_declaration=False)
        live_print(f"🎉 EPG 整合完成，已保存至 {OUTPUT_EPG} (总频道数: {len(seen_channels)}, 总节目数: {len(seen_programmes)})")
        live_print("::endgroup::")
        return OUTPUT_EPG
    except Exception as e:
        live_print(f"❌ 保存合并 EPG 失败: {e}")
        live_print("::endgroup::")
        return None

# ===============================
# 3. 抓取与解析上游直播源
# ===============================
def fetch_and_parse_channels(sources):
    channels =[]
    live_print("::group::📥 开始抓取并解析上游直播源")
    
    for url in sources:
        try:
            live_print(f"正在获取: {url}")
            r = requests.get(url, timeout=10)
            r.encoding = 'utf-8'
            lines = r.text.splitlines()
            
            tmp_name = ""
            for line in lines:
                line = line.strip()
                if not line: continue
                if line.startswith("#EXTINF"):
                    tmp_name = line.split(",")[-1].strip()
                elif line.startswith("http"):
                    name = tmp_name if tmp_name else "未命名频道"
                    channels.append((name, line))
                    tmp_name = ""
                elif "," in line and "://" in line:
                    parts = line.split(",", 1)
                    channels.append((parts[0].strip(), parts[1].strip()))
        except Exception as e:
            live_print(f"❌ 抓取失败 {url}: {e}")
            
    # 保持原有顺序去重
    seen_urls = set()
    unique_channels =[]
    for name, url in channels:
        if url not in seen_urls:
            unique_channels.append((name, url))
            seen_urls.add(url)
            
    live_print(f"✅ 解析完成，去重后共计获取频道数: {len(unique_channels)}")
    live_print("::endgroup::")
    return unique_channels

# ===============================
# 4. 全量测速逻辑
# ===============================
def check_channel(index, name, url):
    start_time = time.time()
    try:
        r = requests.get(url, stream=True, timeout=5)
        if r.status_code == 200:
            downloaded = 0
            for chunk in r.iter_content(chunk_size=1024 * 64):
                downloaded += len(chunk)
                if downloaded >= 1024 * 128:
                    elapsed = round(time.time() - start_time, 2)
                    return True, index, name, url, elapsed
                if time.time() - start_time > 5:
                    break
    except:
        pass
    return False, index, name, url, 0

# ===============================
# 5. 主运行逻辑
# ===============================
if __name__ == "__main__":
    # 1. 整合 EPG
    epg_urls = load_urls_from_file(EPG_FILE)
    merged_epg_file = download_and_merge_epg(epg_urls)

    # 2. 获取直播源
    sources = load_urls_from_file(SOURCES_FILE)
    channels = fetch_and_parse_channels(sources)
    if not channels:
        live_print("⚠️ 没有获取到任何频道，程序退出。")
        exit(0)

    # 3. 测速
    live_print(f"::group::🎬 开始全量连通性检测 (并发量:100)")
    valid_results, logs = [],[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=100) as ex:
        futures = {ex.submit(check_channel, idx, name, url): idx for idx, (name, url) in enumerate(channels)}
        for future in concurrent.futures.as_completed(futures):
            is_valid, idx, name, url, elapsed = future.result()
            if is_valid:
                valid_results.append((idx, name, url))
                msg = f"🟢 [有效] {name:<15} | 耗时: {elapsed}s"
                live_print(msg)
                logs.append(msg)
            else:
                logs.append(f"🔴 [失效] {name:<15} | 无法获取视频流")
    live_print("::endgroup::")

    # 4. 排序与写入
    live_print("::group::💾 正在写入文件 (保持原有排序)")
    valid_results.sort(key=lambda x: x[0])
    
    with open(OUTPUT_TXT, "w", encoding="utf-8") as f:
        for _, name, url in valid_results:
            f.write(f"{name},{url}\n")
            
    with open(OUTPUT_M3U, "w", encoding="utf-8") as f:
        # 如果成功合并了 EPG，将其相对路径写入 M3U 的 x-tvg-url 中
        tvg_url = f' x-tvg-url="{OUTPUT_EPG}"' if merged_epg_file else ""
        f.write(f'#EXTM3U{tvg_url}\n')
        for _, name, url in valid_results:
            f.write(f'#EXTINF:-1,{name}\n{url}\n')
            
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write(f"频道检测报告 | 时间: {datetime.now()}\n")
        f.write(f"总计检测: {len(channels)} | 存活: {len(valid_results)} | 失效: {len(channels) - len(valid_results)}\n\n")
        f.write("\n".join(logs))

    live_print(f"✅ 处理完成！存活率: {len(valid_results)} / {len(channels)}")
    live_print("::endgroup::")
