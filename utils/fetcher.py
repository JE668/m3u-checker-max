import collections
import json
import os
import re
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

import requests

from utils.config import (
    _AI_AVAILABLE,
    ALIAS_FILE,
    SOURCE_META_URL,
    SOURCES_FILE,
    UNMATCHED_FILE,
    _ai_fallback,
    fetch_url,
    get_session,
    live_print,
)
from utils.loaders import get_main_name


# ── EXTINF 属性提取正则 ──
def fetch_and_parse_channels(aliases_exact: Dict[str, str], aliases_regex: List[Tuple[re.Pattern, str]], known_main_names: Set[str], ai_cache: Optional[Dict[str, str]] = None) -> Tuple[list, Set[str], Dict[str, Set[str]]]:
    channels = []  # [(main_name, url, source_url), ...]
    unmatched_names = set()
    ai_pending_aliases = collections.defaultdict(set)  # {标准名: set(别名)} 批量收集，一次性写入

    if not os.path.exists(SOURCES_FILE):
        return channels, set(), {}
    with open(SOURCES_FILE, 'r', encoding='utf-8') as f:
        sources = [line.strip() for line in f if line.strip() and not line.startswith('#')]

    def _resolve_name(raw_name, aliases_exact, aliases_regex, known_main_names, unmatched_names, ai_cache, ai_pending_aliases, seen_source_renames):
        """名称解析+AI兜底+日志，返回 (main_name, is_new_alias)"""
        main_name = get_main_name(raw_name, aliases_exact, aliases_regex, known_main_names, unmatched_names)
        if main_name == raw_name and ai_cache is not None and _AI_AVAILABLE:
            ai_name, ai_changed = _ai_fallback(raw_name, ai_cache)
            if ai_changed and ai_name != raw_name:
                ai_pending_aliases[ai_name].add(raw_name)
                aliases_exact[raw_name] = ai_name
                known_main_names.add(ai_name)
                main_name = ai_name
                unmatched_names.discard(raw_name)
                live_print(f"  🤖 [AI兜底→alias] {raw_name} => {ai_name}")
        if raw_name != main_name and (raw_name, main_name) not in seen_source_renames:
            live_print(f"  📝 [名称修正] {raw_name} => {main_name}")
            seen_source_renames.add((raw_name, main_name))
        return main_name

    seen_urls = set()
    live_print("\n━━━ 📥 抓取直播源 ━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    for source_url in sources:
        try:
            r = fetch_url(source_url, timeout=10)  # P0-3: 使用 Session + UA + 重试
            r.encoding = 'utf-8'
            tmp_name = ""
            count = 0
            seen_source_renames = set()

            for line in r.iter_lines(decode_unicode=True):
                if not line:
                    continue
                line = line.strip()
                if not line:
                    continue
                if line.startswith("#EXTINF"):
                    # 提取频道名
                    tmp_name = line.split(",")[-1].strip()
                elif line.startswith("http"):
                    name = tmp_name if tmp_name else "未命名频道"
                    main_name = _resolve_name(name, aliases_exact, aliases_regex, known_main_names, unmatched_names, ai_cache, ai_pending_aliases, seen_source_renames)

                    if line not in seen_urls:
                        channels.append((main_name, line, source_url))
                        seen_urls.add(line)
                        count += 1
                    tmp_name = ""
                elif "," in line and "://" in line:
                    parts = line.split(",", 1)
                    raw_name = parts[0].strip()
                    main_name = _resolve_name(raw_name, aliases_exact, aliases_regex, known_main_names, unmatched_names, ai_cache, ai_pending_aliases, seen_source_renames)

                    if parts[1].strip() not in seen_urls:
                        channels.append((main_name, parts[1].strip(), source_url))
                        seen_urls.add(parts[1].strip())
                        count += 1
            label = "🔍待测"
            live_print(f"✅ {source_url} -> 提取 {count} 条 [{label}]")
        except Exception as e:  # P0-1: 精确捕获异常并输出详情
            live_print(f"❌ 连接失败: {source_url} — {type(e).__name__}: {e}")

    # ── AI 兜底处理未匹配频道（仅内存计算，不落盘）──
    if unmatched_names and ai_cache is not None and _AI_AVAILABLE:
        for name in list(unmatched_names):
            ai_name, ai_changed = _ai_fallback(name, ai_cache)
            if ai_changed and ai_name != name:
                ai_pending_aliases[ai_name].add(name)
                aliases_exact[name] = ai_name
                known_main_names.add(ai_name)
                unmatched_names.discard(name)
                live_print(f"  🤖 [AI兜底→alias] {name} => {ai_name}")

    return channels, unmatched_names, ai_pending_aliases


def save_parse_results(unmatched_names: Set[str], ai_pending_aliases: Dict[str, Set[str]]) -> None:
    """将解析阶段收集到的未匹配频道与 AI 别名落盘。

    与 fetch_and_parse_channels 解耦：解析函数只负责采集内存数据，
    由调用方在合适的时机显式触发持久化，避免「解析即写文件」的副作用。
    """
    # ── 未匹配频道清单 ──
    if unmatched_names:
        with open(UNMATCHED_FILE, "w", encoding="utf-8") as f:
            f.write("=============== 未匹配频道名单 ===============\n")
            f.write(f"时间: {datetime.now()}\n")
            f.write(f"说明: 以下 {len(unmatched_names)} 个频道在抓取时未能在 config/alias.txt 中找到匹配。\n")
            f.write("建议: 将它们复制到 alias.txt 中进行别名映射，以保持列表纯净。\n")
            f.write("==============================================\n\n")
            for name in sorted(unmatched_names):
                f.write(f"{name}\n")
        live_print(f"\n⚠️ 发现 {len(unmatched_names)} 个未匹配的频道！已输出待办清单至: {UNMATCHED_FILE}")
    else:
        live_print("\n✅ AI 辅助后全部未匹配频道已归入已知频道，无待办清单")
        if os.path.exists(UNMATCHED_FILE):
            os.remove(UNMATCHED_FILE)

    # ── 批量写入 AI 发现的别名到 alias.txt ──
    if ai_pending_aliases:
        alias_write_count = 0
        try:
            if os.path.exists(ALIAS_FILE):
                with open(ALIAS_FILE, 'r', encoding='utf-8') as f:
                    alias_lines = f.readlines()
            else:
                alias_lines = []
            # 读取现有主名集合，避免重复追加
            existing_entries = set()
            for line in alias_lines:
                s = line.strip()
                if s and not s.startswith('#'):
                    existing_entries.add(s.split(',')[0].strip())

            new_lines = []
            for main_name, aliases in sorted(ai_pending_aliases.items()):
                if main_name in existing_entries:
                    continue  # 主名已存在，跳过
                alias_str = ','.join(sorted(aliases, key=lambda x: -len(x)))
                new_lines.append(f"{main_name},{alias_str}\n")
                existing_entries.add(main_name)
                alias_write_count += 1

            if new_lines:
                # 在文件末尾追加
                if alias_lines and alias_lines[-1].strip() != '':
                    alias_lines.append('\n')
                alias_lines.append(f"# AI 自动添加 ({datetime.now().strftime('%Y-%m-%d %H:%M')})\n")
                alias_lines.extend(new_lines)
                with open(ALIAS_FILE, 'w', encoding='utf-8', newline='\n') as f:
                    f.writelines(alias_lines)
                live_print(f"  🤖 [AI→alias.txt] 写入 {alias_write_count} 条新别名映射")
            else:
                live_print("  🤖 [AI→alias.txt] 别名均已存在，无需写入")
        except Exception as e:
            live_print(f"  ⚠️ [AI→alias.txt] 写入失败: {e}")

def fetch_source_meta() -> Optional[dict]:
    """获取 get-m3u 探针元数据，返回 {host_port: {bandwidth_mbps: float}}"""
    try:
        r = get_session().get(SOURCE_META_URL, timeout=10)
        if r.status_code == 200:
            meta = json.loads(r.text)
            # host_port 统一为 lowercase（URL 解析可能大小写敏感）
            meta = {k.lower(): v for k, v in meta.items()}
            live_print(f"📡 已加载探针元数据: {len(meta)} 台服务器")
            return meta
    except requests.RequestException as e:
        live_print(f"⚠️ 探针元数据不可用: {e}")
    except (ValueError, json.JSONDecodeError) as e:
        live_print(f"⚠️ 探针元数据解析失败: {e}")
    return None

# ===============================
