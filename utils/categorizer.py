import os
from collections import Counter
from datetime import datetime
from typing import Dict, Optional, Tuple

from utils.ai_helper import classify_channel, classify_channels_batch
from utils.config import (
    _AI_AVAILABLE,
    _NUM_RE,
    ADULT_SOURCES_FILE,
    CATEGORY_RULES,
    CHANNEL_MODEL_FILE,
    DEFAULT_CATEGORY,
    DEMO_FILE,
    NON_TV_LOG,
    NON_TV_PATTERNS,
    SOURCE_CAT_FILE,
    live_print,
)
from utils.loaders import get_main_name, load_aliases


# 模块级缓存：避免同一 CI 周期内重复学习
_demo_rules_cache = None
_demo_rules_signature = None

# 预排序 CATEGORY_RULES（按优先级升序），避免每次 _match_category 重复排序
_CATEGORY_RULES_SORTED = sorted(CATEGORY_RULES, key=lambda x: x[2])

def _build_demo_rules(chans_in_cat):
    """
    从 demo.txt 已有分类结构中自动学习关键词匹配规则。
    
    对每个分类（"其他频道"除外），提取频道名的共同特征：
      - 中文2字前缀（如 "广东"→☘️广东频道）
      - 英文大写前缀（如 "CGTN"→📺中国国际电视台，标注长度≥3的英文前缀）
    
    返回 {关键词(大写): 分类名不含,#genre#} 字典
    """
    global _demo_rules_cache, _demo_rules_signature
    # 基于内容签名判断是否可复用缓存
    sig = tuple((cat, tuple(names)) for cat, names in sorted(chans_in_cat.items()))
    if _demo_rules_cache is not None and _demo_rules_signature == sig:
        return _demo_rules_cache
    _demo_rules_signature = sig
    demo_rules = {}
    for cat, names in chans_in_cat.items():
        if not names or "其他频道" in cat:
            continue

        prefix_score = {}  # {prefix: count}

        for name in names:
            if not name:
                continue
            # 提取中文前缀
            cz = ''
            for ch in name:
                if '\u4e00' <= ch <= '\u9fff':
                    cz += ch
                else:
                    break
            if len(cz) >= 2:
                # 尝试2字、3字前缀
                for length in [2, 3, 4]:
                    if len(cz) >= length:
                        p = cz[:length]
                        prefix_score[p] = prefix_score.get(p, 0) + 1

            # 提取英文前缀 → 转为大写后匹配
            eng = ''
            for ch in name:
                if ch.isascii() and ch.isalpha():
                    eng += ch
                else:
                    break
            if len(eng) >= 3:
                eng_upper = eng.upper()
                prefix_score[eng_upper] = prefix_score.get(eng_upper, 0) + 1

        # 选出现次数≥2 且最多的前缀作为该分类的规则
        best_prefix = None
        best_count = 0
        for prefix, count in prefix_score.items():
            if count >= 2 and count > best_count:
                best_count = count
                best_prefix = prefix

        if best_prefix and best_prefix not in demo_rules:
            demo_rules[best_prefix] = cat
            live_print(f"  📐 [自学习] 从 {cat} 的 {len(names)} 个频道中提取前缀 '{best_prefix}'")

    if demo_rules:
        live_print(f"  ✅ demo.txt 自学习: 成功提取 {len(demo_rules)} 条分类规则")

    _demo_rules_cache = demo_rules
    return demo_rules


def _match_category(name: str, demo_rules: Optional[Dict[str, str]] = None, channel_model: Optional[Dict[str, str]] = None, use_ai: bool = True) -> Tuple[str, int]:
    """根据频道名匹配分类
    
    匹配优先级：
    0. Channel_model.txt 精确匹配（最高优先级）
    1. demo.txt 自学习规则（前缀精确匹配）
    2. CATEGORY_RULES 硬编码规则（关键词包含）
    3. DEFAULT_CATEGORY 兜底
    """
    # 第 0 步：Channel_model 精确匹配
    if channel_model and name in channel_model:
        cat = channel_model[name]
        if not cat.endswith(",#genre#"):
            cat = f"{cat},#genre#"
        return cat, -2  # 数据库优先级最高

    # 第一步：demo.txt 自学习规则（前缀匹配，要求位置在开头）
    if demo_rules:
        for kw, cat in sorted(demo_rules.items(), key=lambda x: -len(x[0])):  # 长前缀优先
            if name.upper().startswith(kw) or name.startswith(kw):
                return f"{cat},#genre#", -1  # demo 规则优先级最高

    # 第二步：CATEGORY_RULES 硬编码规则（按优先级排序后遍历，找到即返回）
    name_upper = name.upper()
    # 快路径：先尝试精确匹配（对 CCTV-1, CCTV-5 等高频短名避免遍历全部规则）
    # 这里用 startswith 检查第一个匹配的规则即可，因为已按优先级排序
    for keywords, cat_name, priority in _CATEGORY_RULES_SORTED:
        if any(kw in name_upper for kw in keywords):
            return f"{cat_name},#genre#", priority

    # 第三步：AI 分类兜底（仅当其他规则都不匹配时；use_ai=False 时跳过，交由批量接口处理）
    if use_ai and _AI_AVAILABLE and name:
        ai_cat = classify_channel(name)
        if ai_cat and ai_cat != DEFAULT_CATEGORY[0]:
            return f"{ai_cat},#genre#", -1  # 给最高优先级，确保写入 demo.txt
    # 第四步：常规兜底
    return f"{DEFAULT_CATEGORY[0]},#genre#", DEFAULT_CATEGORY[1]


def load_adult_sources(filename: str = ADULT_SOURCES_FILE) -> list:
    """加载限制级内容来源列表"""
    sources = []
    if not os.path.exists(filename):
        return sources
    with open(filename, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                sources.append(line)
    if sources:
        live_print(f"  [限制级来源] 从 {filename} 加载了 {len(sources)} 个限制级源")
    return sources


def load_source_cat(filename: str = SOURCE_CAT_FILE) -> list:
    """加载来源→分类映射

    格式 (config/source-cat.txt):
      # 注释
      文件名后缀 → ☘️综合频道
      URL关键词 → 📺央视频道

    返回: [(pattern, category), ...]，按行顺序优先匹配
    """
    patterns = []
    if not os.path.exists(filename):
        return patterns
    with open(filename, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '→' in line:
                parts = line.split('→', 1)
                pattern = parts[0].strip()
                category = parts[1].strip()
                if pattern and category:
                    patterns.append((pattern, category))
    if patterns:
        live_print(f"  📂 [来源映射] 从 {filename} 加载了 {len(patterns)} 条来源→分类规则")
    return patterns


def load_channel_model(filename: str = CHANNEL_MODEL_FILE) -> Tuple[Dict[str, str], Dict[str, str]]:
    """加载频道分类数据库

    格式 (config/Channel_model.txt):
      频道名|电视台|省份|地级市|频道分组

    返回: ({频道名: 分类}, {频道名: 电视台})
      如 ({"CCTV-1": "📺央视频道,#genre#"}, {"CCTV-1": "中央广播电视总台"})
    """
    channel_to_cat = {}
    channel_to_station = {}
    if not os.path.exists(filename):
        return channel_to_cat, channel_to_station
    with open(filename, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '|' not in line:
                continue
            parts = line.split('|')
            if len(parts) >= 5:
                name = parts[0].strip()
                station = parts[1].strip()
                category = parts[4].strip()
                if name and category:
                    channel_to_cat[name] = category
                if name and station:
                    channel_to_station[name] = station
            elif len(parts) >= 4:
                # 兼容旧4列格式
                name = parts[0].strip()
                category = parts[3].strip()
                if name and category:
                    channel_to_cat[name] = category
    if channel_to_cat:
        live_print(f"  📚 [分类数据库] 从 {filename} 加载了 {len(channel_to_cat)} 个频道分类")
    if channel_to_station:
        live_print(f"  🏢 [电视台库] 从 {filename} 加载了 {len(channel_to_station)} 个电视台归属")
    return channel_to_cat, channel_to_station


def _match_source_category(name, valid_results, url_to_source, source_cat_map):
    """当频道名无法匹配时，从来源URL推断分类"""
    if name not in valid_results:
        return None
    for url, _ in valid_results[name]:
        source_url = url_to_source.get(url, '')
        if not source_url:
            continue
        for pattern, cat in source_cat_map:
            if pattern in source_url:
                return cat
    return None


def channel_sort_key(name: str, demo_rules: Optional[Dict[str, str]] = None, channel_model: Optional[Dict[str, str]] = None, use_ai: bool = True) -> Tuple[int, int, str]:
    nums = _NUM_RE.findall(name)
    val = int(nums[0]) if nums else 999
    _, priority = _match_category(name, demo_rules, channel_model, use_ai=use_ai)
    return (priority if priority >= 0 else 0, val, name)

def is_non_tv_channel(name: str) -> bool:
    """检测是否为非电视台频道（直播平台/影视点播/广播等）"""
    return any(p in name for p in NON_TV_PATTERNS)

def auto_update_demo(valid_results: dict, cat_order: list, chan_to_cat: dict, chans_in_cat: dict, valid_results_opt: Optional[dict] = None, url_to_source: Optional[dict] = None, source_cat_map: Optional[list] = None, channel_model: Optional[dict] = None) -> Tuple[list, dict, dict]:
    live_print("\n━━━ 🧠 自适应进化 demo.txt ━━━━━━━━━━━━━━━━━━━━")

    if valid_results_opt is None:
        valid_results_opt = {}

    new_channels = [n for n in valid_results if n not in chan_to_cat]

    if not new_channels:
        live_print("ℹ️ 状态: 测速存活的频道均已存在于 config/demo.txt 当前分组中。")
        live_print("✅ 动作: 模板保持原样，无需写入更新。")
        return cat_order, chan_to_cat, chans_in_cat

    # ——————————————————————————————————————
    # P0 过滤：非电视台频道（直播平台/影视点播/广播等）
    # ——————————————————————————————————————
    tv_channels = []
    non_tv_channels = []
    for name in new_channels:
        if is_non_tv_channel(name):
            non_tv_channels.append(name)
        else:
            tv_channels.append(name)

    # 写入过滤日志（统计 + 明细）
    if non_tv_channels:
        with open(NON_TV_LOG, 'w', encoding='utf-8') as f:
            f.write("# 非 TV 频道过滤日志\n")
            f.write(f"# 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"# 总计: {len(non_tv_channels)} 个频道被过滤\n\n")
            # 按关键词分组统计
            kw_counts = Counter()
            for name in non_tv_channels:
                for p in NON_TV_PATTERNS:
                    if p in name:
                        kw_counts[p] += 1
                        break
            f.write("## 按关键词统计\n")
            for kw, cnt in kw_counts.most_common():
                f.write(f"{kw}: {cnt}\n")
            f.write("\n## 被过滤频道列表\n")
            for name in non_tv_channels:
                matched_kw = next((p for p in NON_TV_PATTERNS if p in name), "未知")
                f.write(f"[{matched_kw}] {name}\n")
        live_print(f"  🚫 [过滤] 跳过 {len(non_tv_channels)} 个非电视频道 → {NON_TV_LOG}")

    if not tv_channels:
        live_print("ℹ️ 状态: 测速存活的频道均已存在于 config/demo.txt 当前分组中（或全部被过滤）。")
        live_print("✅ 动作: 模板保持原样，无需写入更新。")
        return cat_order, chan_to_cat, chans_in_cat

    live_print(f"ℹ️ 状态: 发现了 {len(tv_channels)} 个全新的存活电视台频道！准备自动归类并追加写入...")

    # 别名归一化：将 tv_channels 中的频道名先走 alias.txt 映射
    try:
        a_exact, a_regex, a_known = load_aliases()
        normalized = []
        alias_mappings = []
        for name in tv_channels:
            main = get_main_name(name, a_exact, a_regex, a_known)
            if main != name:
                alias_mappings.append(f"    📝 [别名归一] {name} → {main}")
            normalized.append(main)
        tv_channels = normalized
        if alias_mappings:
            for m in alias_mappings:
                live_print(m)
    except Exception as e:
        live_print(f"  ⚠️ 别名归一化失败 (跳过): {e}")

    # 从 demo.txt 现有结构学习分类规则
    demo_rules = _build_demo_rules(chans_in_cat)

    # 第一趟：规则匹配（关闭 AI，避免逐条调用），收集需要 AI 兜底的频道
    # rule_cat 缓存第一趟结果，第二趟直接复用，避免对 tv_channels 重复计算 _match_category
    ai_pending = []
    rule_cat = {}
    for name in tv_channels:
        cat, _ = _match_category(name, demo_rules, channel_model, use_ai=False)
        rule_cat[name] = cat
        if cat.startswith(f"{DEFAULT_CATEGORY[0]},#genre#"):
            ai_pending.append(name)
    # 批量 AI 分类（一次或分片请求，结果写入缓存，避免 N 次逐条调用）
    ai_map = classify_channels_batch(ai_pending)
    if ai_pending:
        live_print(f"  🤖 [AI批量分类] 待分类 {len(ai_pending)} 个，成功归类 {len(ai_map)} 个")

    additions = {}
    for name in tv_channels:
        cat = rule_cat[name]
        # AI 批量结果兜底
        if cat.startswith(f"{DEFAULT_CATEGORY[0]},#genre#") and name in ai_map:
            cat = f"{ai_map[name]},#genre#"
        # 如果频道名匹配到兜底分类(📺其他频道)，尝试来源URL推断
        if cat == f"{DEFAULT_CATEGORY[0]},#genre#" and source_cat_map and valid_results_opt and url_to_source:
            source_cat = _match_source_category(name, valid_results_opt, url_to_source, source_cat_map)
            if source_cat and source_cat.strip():
                cat = f"{source_cat},#genre#"
                live_print(f"  🏷️ [来源推断] [{name}] → {source_cat} (基于来源URL)")
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
        sorted_names = sorted(names, key=lambda n: channel_sort_key(n, demo_rules, use_ai=False))
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
        live_print("✅ 动作: config/demo.txt 已无损更新！原结构完美保留，底部已成功追加上述新频道。")
    except Exception as e:
        live_print(f"❌ 动作: config/demo.txt 更新失败: {e}")
    return cat_order, chan_to_cat, chans_in_cat

# ===============================
