import os, gzip, io, concurrent.futures, xml.etree.ElementTree as ET
from typing import Tuple, Optional

from utils.config import (
    EPG_FILE, EPG_BLACKLIST, EPG_MAX_WORKERS, OUTPUT_EPG, OUTPUT_EPG_GZ,
    get_session, live_print, fetch_url
)
from utils.loaders import load_aliases, get_main_name

def _download_single_epg(url: str, aliases_exact: Dict[str, str], aliases_regex: List[Tuple[re.Pattern, str]], known_main_names: Set[str]) -> Tuple[list, list, list]:
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


def download_and_merge_epg(aliases_exact: Dict[str, str], aliases_regex: List[Tuple[re.Pattern, str]], known_main_names: Set[str]) -> list:
    epg_urls = []
    epg_report = []
    if os.path.exists(EPG_FILE):
        with open(EPG_FILE, 'r', encoding='utf-8') as f:
            epg_urls = [line.strip() for line in f if line.strip() and not line.startswith('#')]

    if not epg_urls: return epg_report

    live_print("\n━━━ 📅 下载并整合 EPG ━━━━━━━━━━━━━━━━━━━━━━━")

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
    return epg_report

# ===============================
# 4. 抓取直播源
# ===============================
