import os
import re
from typing import Dict, List, Optional, Set, Tuple

from utils.config import ALIAS_FILE, DEMO_FILE, ICON_DIR, ICONS_INDEX_FILE, REPO_RAW, live_print


def load_filter_lists(filepath: str) -> Tuple[Set[str], Set[str]]:
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

def load_aliases() -> Tuple[Dict[str, str], List[Tuple[re.Pattern, str]], Set[str]]:
    aliases_exact, aliases_regex = {}, []
    known_main_names = set()

    live_print("\n━━━ ⚙️ 加载系统配置文件 ━━━━━━━━━━━━━━━━━━━")
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

def get_main_name(raw_name: str, aliases_exact: Dict[str, str], aliases_regex: List[Tuple[re.Pattern, str]], known_main_names: Set[str], unmatched_set: Optional[Set[str]] = None) -> str:
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

# logo URL 指向 CDN 加速的 GitHub Raw（LFS 文件通过 Raw URL 正常返回图片内容）
_ICONS_BASE_URL = f"{REPO_RAW}/icons"

# 延迟构建：避免 import 模块时即扫描 5000+ 图标文件 / 读取索引（消除 import 副作用）
_LOGO_INDEX_CACHE = None

def get_logo_index() -> dict:
    """构建并返回 {clean_name: filename} 字典（首次调用时构建并缓存）。"""
    global _LOGO_INDEX_CACHE
    if _LOGO_INDEX_CACHE is None:
        _LOGO_INDEX_CACHE = _build_logo_index()
    return _LOGO_INDEX_CACHE

def get_local_logo_url(name: str) -> str:
    target = re.sub(r'[\s\-_]', '', name).lower()
    index = get_logo_index()
    if target in index:
        return f"{_ICONS_BASE_URL}/{index[target]}"
    return ""

def load_demo_template(aliases_exact: Dict[str, str], aliases_regex: List[Tuple[re.Pattern, str]], known_main_names: Set[str]) -> Tuple[List[str], Dict[str, str], Dict[str, List[str]]]:
    category_order = []
    channel_to_category = {}
    channels_in_category = {}

    if not os.path.exists(DEMO_FILE):
        live_print(f"⚠️ 未找到分类模板文件: {DEMO_FILE}")
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
    return category_order, channel_to_category, channels_in_category

