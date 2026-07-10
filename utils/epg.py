import concurrent.futures
import gzip
import io
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Set, Tuple

from utils.config import (
    EPG_BLACKLIST,
    EPG_FILE,
    EPG_KEEP_DAYS,
    EPG_MAX_WORKERS,
    OUTPUT_EPG,
    OUTPUT_EPG_GZ,
    fetch_url,
    live_print,
)
from utils.loaders import get_main_name


# ── 北京时间 UTC+8 ──
_BJT = timezone(timedelta(hours=8))


def _parse_epg_time(ts: str) -> datetime:
    """解析 EPG 时间戳格式 'YYYYMMDDHHmmSS +XXXX' 或 'YYYYMMDDHHmmSS'"""
    # 去掉时区后缀（如 '+0800'），只取前14位
    core = ts[:14]
    tz_sign = ts[15] if len(ts) > 15 else '+'
    tz_h = int(ts[16:18]) if len(ts) > 17 else 8
    tz_m = int(ts[19:21]) if len(ts) > 20 else 0
    tz_offset = timedelta(hours=tz_h if tz_sign == '+' else -tz_h, minutes=tz_m if tz_sign == '+' else -tz_m)
    dt = datetime.strptime(core, "%Y%m%d%H%M%S").replace(tzinfo=timezone(tz_offset))
    return dt.astimezone(_BJT)


def _filter_programmes_by_days(programmes: list, keep_days: int) -> list:
    """按天数过滤节目数据，保留北京时间前一天+当天+后一天。
    keep_days=0 时不过滤（保留全部）；keep_days<0 视为0。
    返回过滤后的 programmes 列表。
    """
    if keep_days <= 0:
        return programmes

    now_bjt = datetime.now(_BJT)
    today = now_bjt.date()
    # 计算保留窗口：前1天到后(keep_days-2)天 → 默认 keep_days=3 即 [-1, 0, +1]
    day_before = today - timedelta(days=1)
    day_after = today + timedelta(days=keep_days - 2)
    # 窗口起始时刻和结束时刻（用 UTC+8 整日边界）
    window_start = datetime(day_before.year, day_before.month, day_before.day, 0, 0, 0, tzinfo=_BJT)
    window_end = datetime(day_after.year, day_after.month, day_after.day, 23, 59, 59, tzinfo=_BJT)

    kept = []
    for prog in programmes:
        start_ts = prog.get('start')
        if not start_ts:
            continue
        try:
            dt = _parse_epg_time(start_ts)
            if window_start <= dt <= window_end:
                kept.append(prog)
        except (ValueError, IndexError):
            # 解析失败的保留（不丢弃）
            kept.append(prog)
    return kept


def _download_single_epg(url: str, aliases_exact: Dict[str, str], aliases_regex: List[Tuple[re.Pattern, str]], known_main_names: Set[str]) -> Tuple[list, list, list]:
    """下载并解析单个 EPG 源（供并发调用）"""
    if "gitee.com" in url and "/blob/" in url:
        url = url.replace("/blob/", "/raw/")
    elif "github.com" in url and "/blob/" in url:
        url = url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")

    report_lines = [f"▶ 来源: {url}"]
    try:
        live_print(f"📥 正在获取: {url}")
        r = fetch_url(url, timeout=20)
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

    if not epg_urls:
        return epg_report

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
        # ── 按天数过滤节目 ──
        orig_prog_count = len(merged_programmes)
        if EPG_KEEP_DAYS > 0:
            merged_programmes = _filter_programmes_by_days(merged_programmes, EPG_KEEP_DAYS)
            kept_prog_count = len(merged_programmes)
            dropped = orig_prog_count - kept_prog_count
            live_print(f"📅 EPG 按天数过滤（保留{EPG_KEEP_DAYS}天）：{orig_prog_count} → {kept_prog_count} 条节目（丢弃 {dropped} 条过期数据）")
            # 修剪没有节目的频道
            orig_ch_count = len(merged_channels)
            surviving_channels = set(prog.get('channel') for prog in merged_programmes)
            merged_channels = [ch for ch in merged_channels if ch.get('id') in surviving_channels]
            live_print(f"📺 频道数同步修剪：{orig_ch_count} → {len(merged_channels)}（去除无节目频道）")

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
