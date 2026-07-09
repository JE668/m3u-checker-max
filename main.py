"""
m3u-checker-max — IPTV 直播源检测与分类系统
=============================================
抓取 → 过滤 → 测速 → 分类 → 输出 M3U/TXT
支持分阶段 CI 执行，AI 辅助名称标准化与频道分类。

模块结构:
  utils/config.py      配置、会话、线程池、工具函数
  utils/loaders.py     别名/黑/白名单/频道模型加载
  utils/epg.py         EPG 下载、解析、合并
  utils/fetcher.py     直播源抓取、AI 别名收集
  utils/categorizer.py 分类引擎、demo.txt 自进化
  utils/speedtest.py   并发测速、分辨率检测、黑白过滤
  utils/output.py      成品输出 M3U/TXT/日志
"""

import concurrent.futures
import json
import os
import shutil
import time
from dataclasses import asdict, dataclass, field, fields
from typing import Optional

from utils.categorizer import (
    auto_update_demo,
    load_adult_sources,
    load_channel_model,
    load_source_cat,
)
from utils.config import (
    _AI_AVAILABLE,
    BLACKLIST_FILE,
    MAX_WORKERS,
    PROBE_RESOLUTION,
    PROBE_TIMEOUT,
    WHITELIST_FILE,
    _dedup_blacklist,
    _load_ai_cache,
    _save_ai_cache,
    _validate_configs,
    fmt_resolution,
    get_cache_stats,
    get_pool,
    live_print,
    write_summary,
    write_summary_table,
    flush_summary,
)
from utils.epg import (
    download_and_merge_epg,
)
from utils.fetcher import (
    fetch_and_parse_channels,
    fetch_source_meta,
    save_parse_results,
)
from utils.loaders import (
    load_aliases,
    load_demo_template,
    load_filter_lists,
)
from utils.output import (
    write_outputs,
)
from utils.speedtest import (
    append_auto_blacklist,
    apply_filter_lists,
    probe_resolution,
    run_speed_test,
)


# ── _AI_CACHE 持久化缓存（main 函数的局部变量） ──
_AI_CACHE = {}

@dataclass
class CIState:
    """CI 三阶段状态的单一字段来源与序列化载体。

    所有跨阶段传递的变量集中在此；_save_state/_load_state 通过 asdict
    自动完成序列化，避免手写字典键名带来的拼写/遗漏风险。
    """
    # 阶段1 派生状态
    url_to_source: dict = field(default_factory=dict)
    valid_results: dict = field(default_factory=dict)
    to_test: list = field(default_factory=list)
    logs_blacklist: list = field(default_factory=list)
    logs_whitelist: list = field(default_factory=list)
    adult_results: dict = field(default_factory=dict)
    adult_source_urls: set = field(default_factory=set)
    cat_order: list = field(default_factory=list)
    chan_to_cat: dict = field(default_factory=dict)
    chans_in_cat: dict = field(default_factory=dict)
    channel_to_station: dict = field(default_factory=dict)
    channel_model: dict = field(default_factory=dict)
    epg_report: object = None
    start_time: float = 0.0
    # 阶段2 派生状态
    resolution_map: dict = field(default_factory=dict)
    logs_success: list = field(default_factory=list)
    logs_fail: list = field(default_factory=list)
    fail_counts: dict = field(default_factory=dict)
    source_stats: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "CIState":
        """从磁盘 JSON 反序列化；自动将 adult_source_urls 还原为 set，
        忽略未知字段、缺失字段用默认值补齐，保证向前兼容。"""
        raw = {k: v for k, v in data.items() if k != "_version"}
        if raw.get("adult_source_urls") is not None:
            raw["adult_source_urls"] = set(raw["adult_source_urls"])
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})

def main(ci_phase: Optional[int] = None, ci_state_dir: str = "tmp") -> None:
    """主执行函数。ci_phase：None=完整运行，1/2/3=分阶段CI执行。"""
    global _AI_CACHE
    # 启动时去重 blacklist.txt
    _dedup_blacklist()
    # 配置验证
    _validate_configs()
    # 加载 AI 标准化缓存
    _AI_CACHE = _load_ai_cache()

    start_time = time.time()

    def _save_state(phase, st: CIState):
        "将 CIState 序列化为磁盘 JSON（asdict 自动处理 list/dict；set 需显式转 list）"
        os.makedirs(ci_state_dir, exist_ok=True)
        path = os.path.join(ci_state_dir, f"state{phase}.json")
        blob = asdict(st)
        blob["adult_source_urls"] = list(blob["adult_source_urls"])
        blob["_version"] = 1
        with open(path, "w") as f:
            json.dump(blob, f, ensure_ascii=False, default=str)
        live_print(f"  📦 状态已保存 → {path}")

    def _load_state(phase) -> Optional[CIState]:
        path = os.path.join(ci_state_dir, f"state{phase}.json")
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return CIState.from_dict(json.load(f))

    # ════════════════════════════════════════════
    # 阶段1：加载配置、抓取源、黑白名单过滤
    # ════════════════════════════════════════════
    if ci_phase is None or ci_phase >= 1:
        if ci_phase is not None and ci_phase >= 2:
            st = _load_state(1)
            if not st:
                live_print(f"  ❌ 未找到阶段1状态文件，无法继续阶段{ci_phase}")
                return
            url_to_source = st.url_to_source
            valid_results = st.valid_results
            to_test = st.to_test
            adult_results = st.adult_results
            adult_source_urls = st.adult_source_urls
            logs_blacklist = st.logs_blacklist
            logs_whitelist = st.logs_whitelist
            cat_order = st.cat_order
            chan_to_cat = st.chan_to_cat
            chans_in_cat = st.chans_in_cat
            channel_to_station = st.channel_to_station
            channel_model = st.channel_model
            epg_report = st.epg_report
            start_time = st.start_time
            live_print("  🔄 已从阶段1状态恢复")
        else:
            # ----- 阶段1：从头执行 -----
            aliases_exact, aliases_regex, known_main_names = load_aliases()

            # 加载黑白名单
            blacklist_names, blacklist_urls = load_filter_lists(BLACKLIST_FILE)
            whitelist_names, whitelist_urls = load_filter_lists(WHITELIST_FILE)

            epg_report = download_and_merge_epg(aliases_exact, aliases_regex, known_main_names)

            try:
                cat_order, chan_to_cat, chans_in_cat = load_demo_template(aliases_exact, aliases_regex, known_main_names)
            except Exception as e:
                live_print(f"❌ config/demo.txt 加载严重错误: {e}")
                return

            start_time = time.time()
            channels, url_to_group, unmatched_names, ai_pending_aliases = fetch_and_parse_channels(aliases_exact, aliases_regex, known_main_names, ai_cache=_AI_CACHE)
            # 解析阶段不再写文件；在此显式落盘，与抓取逻辑解耦
            save_parse_results(unmatched_names, ai_pending_aliases)

            if not channels:
                live_print("⚠️ 未获取到任何有效直播源，退出。")
                return

            # 建立 URL → 来源 映射
            url_to_source = {}
            for _, url, source_url in channels:
                url_to_source[url] = source_url

            source_channel_counts = {}
            for _, _, src in channels:
                source_channel_counts[src] = source_channel_counts.get(src, 0) + 1
            for src, cnt in source_channel_counts.items():
                live_print(f"  📡 {src.split('/')[-1]}: {cnt} 条")

            # 黑白名单过滤分流
            to_test, valid_results, logs_blacklist, logs_whitelist, auto_blacklist = apply_filter_lists(
                channels, blacklist_names, blacklist_urls, whitelist_names, whitelist_urls
            )
            # 过滤即返回内存结果；无效名落盘由调用方显式触发，避免副作用
            append_auto_blacklist(auto_blacklist)

            # 过滤 IPv6 地址
            enable_ipv6 = os.environ.get("ENABLE_IPV6", "").lower() == "true"
            ipv6_count = sum(1 for _, url in to_test if '[' in url)
            if ipv6_count and not enable_ipv6:
                to_test = [(n, u) for n, u in to_test if '[' not in u]
                live_print(f"🔇 过滤 {ipv6_count} 条 IPv6 链接 (GitHub Actions 无 IPv6 路由)")
            elif ipv6_count:
                live_print(f"🌐 保留 {ipv6_count} 条 IPv6 链接 (ENABLE_IPV6=true)")

            # 成人来源标记（仅URL模式匹配，后续阶段2会参加测速再归类）
            adult_sources = load_adult_sources()
            adult_results = {}
            adult_source_urls = set()  # 始终初始化，避免 adult-sources.txt 为空时 NameError
            if adult_sources:
                live_print(f"  🔞 成人源URL模式: {adult_sources}")
                for name, url in to_test:
                    src = url_to_source.get(url, '')
                    for a in adult_sources:
                        if a in src:
                            adult_source_urls.add(url)
                            break
                if adult_source_urls:
                    live_print(f"  🔞 标记 {len(adult_source_urls)} 个成人来源URL，将在阶段2参与测速后归类")

            channel_model, channel_to_station = load_channel_model()

        # 阶段1结束前保存 AI 缓存，避免跨阶段丢失
        if _AI_CACHE:
            _save_ai_cache(_AI_CACHE)
        if ci_phase == 1:
            _save_state(1, CIState(
                url_to_source=url_to_source,
                valid_results=valid_results,
                to_test=to_test,
                logs_blacklist=logs_blacklist,
                logs_whitelist=logs_whitelist,
                adult_results=adult_results,
                adult_source_urls=adult_source_urls,
                cat_order=cat_order,
                chan_to_cat=chan_to_cat,
                chans_in_cat=chans_in_cat,
                channel_to_station=channel_to_station,
                channel_model=channel_model,
                epg_report=epg_report,
                start_time=start_time,
            ))
            # 阶段1 Summary
            write_summary("## 🔍 阶段1 — 抓取与过滤\n")
            write_summary_table(
                ["指标", "数值"],
                [
                    ["📡 抓取频道", f"{sum(source_channel_counts.values())} 个"],
                    ["✅ 白名单免测", f"{len(logs_whitelist)} 个"],
                    ["🚫 黑名单拦截", f"{len(logs_blacklist)} 个"],
                    ["🔞 成人来源", f"{len(adult_source_urls)} 个"],
                    ["🎯 待测频道", f"{len(to_test)} 个"],
                    ["📂 分类模板", f"{len(cat_order)} 个大类"],
                ]
            )
            write_summary("")
            # 来源明细折叠
            if source_channel_counts:
                write_summary("<details><summary>📋 各来源抓取明细</summary>\n")
                write_summary_table(
                    ["来源", "抓取数"],
                    [[src.split('/')[-1][:50], cnt] for src, cnt in sorted(source_channel_counts.items(), key=lambda x: -x[1])]
                )
                write_summary("\n</details>\n")
            live_print("✅ 阶段1完成 (抓取源+过滤)")
            flush_summary()
            return

    # ════════════════════════════════════════════
    # 阶段2：并发测速
    # ════════════════════════════════════════════
    if ci_phase is None or ci_phase >= 2:
        if ci_phase is not None and ci_phase >= 3:
            st = _load_state(2)
            if not st:
                live_print("  ❌ 未找到阶段2状态文件")
                return
            valid_results = st.valid_results
            resolution_map = st.resolution_map
            adult_results = st.adult_results
            adult_source_urls = st.adult_source_urls
            to_test = st.to_test
            url_to_source = st.url_to_source
            cat_order = st.cat_order
            chan_to_cat = st.chan_to_cat
            chans_in_cat = st.chans_in_cat
            channel_to_station = st.channel_to_station
            channel_model = st.channel_model
            epg_report = st.epg_report
            logs_success = st.logs_success
            logs_fail = st.logs_fail
            logs_whitelist = st.logs_whitelist
            logs_blacklist = st.logs_blacklist
            fail_counts = st.fail_counts
            source_stats = st.source_stats
            start_time = st.start_time
            live_print("  🔄 已从阶段2状态恢复")
        else:
            live_print(f"\n🚀 开始测速 (待测: {len(to_test)} 条, 免测: 白名单{len(logs_whitelist)} 条, 拦截: {len(logs_blacklist)} 条)...\n")

            source_meta = fetch_source_meta()
            test_results, logs_success, logs_fail, fail_counts, source_stats = run_speed_test(
                to_test, source_meta=source_meta, source_urls=url_to_source, channel_to_station=channel_to_station
            )

            # 合并测速结果到 valid_results
            for name, url_list in test_results.items():
                if name not in valid_results:
                    valid_results[name] = url_list
                else:
                    existing_urls = {u for u, _ in valid_results[name]}
                    for url, elapsed in url_list:
                        if url not in existing_urls:
                            valid_results[name].append((url, elapsed))
                            existing_urls.add(url)

            # ═══ 成人来源分离：测速后将成人来源URL从 valid_results 移到 adult_results ═══
            if adult_source_urls:
                adult_results = {}
                for name in list(valid_results.keys()):
                    adult_urls = [(u, e) for u, e in valid_results[name] if u in adult_source_urls]
                    normal_urls = [(u, e) for u, e in valid_results[name] if u not in adult_source_urls]
                    if adult_urls:
                        adult_results[name] = adult_urls
                    if normal_urls:
                        valid_results[name] = normal_urls
                    elif name in valid_results:
                        del valid_results[name]
                live_print(f"  🔞 成人来源测速后分离: {len(adult_results)} 个频道 → output/adult.m3u")
            else:
                adult_results = {}

            # ═══ 分辨率检测 ═══
            resolution_map = {}
            if PROBE_RESOLUTION and valid_results:
                # 收集所有有效 URL
                probe_targets = []
                for name, urls in valid_results.items():
                    for url, elapsed in urls:
                        if url not in resolution_map:
                            probe_targets.append((name, url, elapsed))

                n_total = len(probe_targets)
                if n_total > 0:
                    live_print(f"\n🔍 分辨率检测: {n_total} 个频道 (并发 {MAX_WORKERS}, 超时 {PROBE_TIMEOUT}s)")
                    reso_probed = 0
                    reso_found = 0

                    def _probe_one(name, url, elapsed):
                        w, h = probe_resolution(url)
                        return url, w, h

                    pool = get_pool()
                    futures = {pool.submit(_probe_one, n, u, e): (n, u)
                               for n, u, e in probe_targets}
                    for future in concurrent.futures.as_completed(futures):
                        url, w, h = future.result()
                        resolution_map[url] = (w, h)
                        reso_probed += 1
                        if w > 0 and h > 0:
                            reso_found += 1
                        if reso_probed % 50 == 0 or reso_probed == n_total:
                            live_print(f"  🔍 分辨率检测: {reso_probed}/{n_total} | 已识别: {reso_found}")

                    live_print(f"✅ 分辨率检测完成: {reso_found}/{reso_probed} 识别成功")

                    # 分辨率分类统计
                    reso_stats = {}
                    for url, (w, h) in resolution_map.items():
                        if w > 0 and h > 0:
                            label = fmt_resolution(w, h)
                            reso_stats[label] = reso_stats.get(label, 0) + 1
                    if reso_stats:
                        live_print("📊 分辨率分布:")
                        for lbl in sorted(reso_stats, key=reso_stats.get, reverse=True):
                            cnt = reso_stats[lbl]
                            bar = '█' * min(cnt // 2 + 1, 15)
                            live_print(f"  {lbl:<10} {cnt:>4}  {bar}")
            else:
                resolution_map = {}

        if ci_phase == 2:
            _save_state(2, CIState(
                valid_results=valid_results,
                resolution_map=resolution_map,
                adult_results=adult_results,
                adult_source_urls=adult_source_urls,
                to_test=to_test,
                url_to_source=url_to_source,
                cat_order=cat_order,
                chan_to_cat=chan_to_cat,
                chans_in_cat=chans_in_cat,
                channel_to_station=channel_to_station,
                channel_model=channel_model,
                epg_report=epg_report,
                logs_success=logs_success,
                logs_fail=logs_fail,
                logs_whitelist=logs_whitelist,
                logs_blacklist=logs_blacklist,
                fail_counts=fail_counts,
                source_stats=source_stats,
                start_time=start_time,
            ))
            # 阶段2 Summary
            src_ok_dict = source_stats.get("ok", {})
            src_total_dict = source_stats.get("total", {})
            total_ok = sum(src_ok_dict.values())
            total_test = sum(src_total_dict.values())
            success_rate = f"{total_ok*100//total_test}%" if total_test else "N/A"
            write_summary("## 🚀 阶段2 — 测速与校验\n")
            write_summary_table(
                ["指标", "数值"],
                [
                    ["🎯 测速频道", f"{total_test} 个"],
                    ["✅ 成功", f"{total_ok} ({success_rate})"],
                    ["❌ 失败", f"{total_test - total_ok} 个"],
                    ["🔞 成人频道", f"{len(adult_results)} 个"],
                ]
            )
            write_summary("")
            # 失败原因折叠
            if fail_counts:
                total_fails = sum(fail_counts.values())
                write_summary("<details><summary>❌ 失败原因分布</summary>\n")
                rows = []
                for cat in sorted(fail_counts, key=fail_counts.get, reverse=True):
                    cnt = fail_counts[cat]
                    pct = f"{cnt/total_fails*100:.1f}%"
                    bar = '🟥' * min(cnt // 5 + 1, 10)
                    rows.append([cat, cnt, pct, bar])
                write_summary_table(["原因", "数量", "占比", ""], rows)
                write_summary("\n</details>\n")
            # 来源测速折叠
            if src_total_dict:
                write_summary("<details><summary>🔗 各来源测速结果</summary>\n")
                rows = []
                for src in sorted(src_total_dict, key=lambda s: src_total_dict[s], reverse=True):
                    ok = src_ok_dict.get(src, 0)
                    total = src_total_dict[src]
                    rate = f"{ok/total*100:.1f}%" if total > 0 else "-"
                    icon = "🟢" if ok/total >= 0.8 else "🟡" if ok/total >= 0.5 else "🔴"
                    rows.append([f"{icon} {src.split('/')[-1][:40]}", ok, total, rate])
                write_summary_table(["来源", "成功", "总计", "成功率"], rows)
                write_summary("\n</details>\n")
            # 分辨率折叠
            if reso_stats:
                write_summary("<details><summary>🖥️ 分辨率分布</summary>\n")
                rows = []
                for lbl in sorted(reso_stats, key=reso_stats.get, reverse=True):
                    cnt = reso_stats[lbl]
                    bar = '🟦' * min(cnt // 5 + 1, 15)
                    rows.append([lbl, cnt, bar])
                write_summary_table(["分辨率", "数量", ""], rows)
                write_summary("\n</details>\n")
            live_print("✅ 阶段2完成 (测速)")
            flush_summary()
            return

    # ════════════════════════════════════════════
    # 阶段3：分类模板进化 & 成品输出
    # ════════════════════════════════════════════
    if ci_phase is None or ci_phase >= 3:
        # 模板自进化
        source_cat_map = load_source_cat()
        cat_order, chan_to_cat, chans_in_cat = auto_update_demo(
            valid_results, cat_order, chan_to_cat, chans_in_cat,
            url_to_source=url_to_source, source_cat_map=source_cat_map, channel_model=channel_model
        )

        # 过滤空分类
        non_empty_cats = [cat for cat in cat_order if any(name in valid_results for name in chans_in_cat.get(cat, []))]
        if len(non_empty_cats) < len(cat_order):
            empty = len(cat_order) - len(non_empty_cats)
            live_print(f"🧹 过滤 {empty} 个空分类（无存活频道）")
            cat_order = non_empty_cats

        # 写入成品
        cat_live_counts = {}
        for cat in cat_order:
            cat_live_counts[cat] = sum(1 for name in chans_in_cat.get(cat, []) if name in valid_results)

        extra_stats = {
            "source_ok": source_stats["ok"],
            "source_total": source_stats["total"],
            "fail_counts": fail_counts,
            "cat_live_counts": cat_live_counts,
            "elapsed_seconds": time.time() - start_time,
        }
        write_outputs(valid_results, cat_order, chans_in_cat, epg_report, logs_success, logs_fail,
                       logs_whitelist, logs_blacklist, extra_stats,
                       adult_results=adult_results, channel_to_station=channel_to_station,
                       resolution_map=resolution_map)

        # CI最后阶段：清理临时状态
        if ci_phase == 3 and os.path.exists(ci_state_dir):
            shutil.rmtree(ci_state_dir)
            live_print(f"  🧹 已清理临时状态目录: {ci_state_dir}")

    # ── Phase 3 完成后的统一统计摘要 ──
    if ci_phase is None or ci_phase == 3:
        elapsed = round(time.time() - start_time, 2)
        total_channels = len(valid_results)
        adult_count = len(adult_results) if adult_results else 0

        # 来源统计
        src_ok_dict = source_stats.get("ok", {})
        src_total_dict = source_stats.get("total", {})
        source_ok = sum(src_ok_dict.values())
        source_total = sum(src_total_dict.values())

        # 失败分布
        top_fails = sorted(fail_counts.items(), key=lambda x: -x[1])[:5] if fail_counts else []

        # 分辨率分布
        reso_stats = {}
        for url, (w, h) in resolution_map.items():
            if w > 0 and h > 0:
                label = fmt_resolution(w, h)
                reso_stats[label] = reso_stats.get(label, 0) + 1

        # 非TV过滤（从 "## 被过滤频道列表" 之后统计实际频道数）
        non_tv_count = 0
        if os.path.exists("output/non-tv-filtered.txt"):
            with open("output/non-tv-filtered.txt", "r", encoding="utf-8") as f:
                content = f.read()
            if "## 被过滤频道列表" in content:
                after_list = content.split("## 被过滤频道列表")[1]
                non_tv_count = sum(1 for line in after_list.strip().split('\n') if line.strip())

        # 安全的变量获取
        to_test_count = len(to_test) if 'to_test' in locals() or 'to_test' in dir() else 0
        source_count = len(url_to_source) if 'url_to_source' in locals() or 'url_to_source' in dir() else len(src_total_dict)

        # ── 控制台管道视图 ──
        live_print("")
        live_print("━━━ 📊 直播源检测 — 阶段摘要 ━━━━━━━━━━━━━━━━━")
        live_print("  源获取 → 测速校验 → 分类输出")
        live_print("")
        live_print("  ┌─ 阶段1: 抓取与过滤")
        live_print(f"  │  ├ 总抓取频道 ........ {source_total:>4} 个")
        live_print(f"  │  ├ 白名单免测 ........ {len(logs_whitelist):>4} 个")
        live_print(f"  │  ├ 黑名单拦截 ........ {len(logs_blacklist):>4} 个")
        live_print(f"  │  ├ 非TV过滤 .......... {non_tv_count:>4} 个")
        live_print(f"  │  └ 待测频道 .......... {to_test_count:>4} 个")
        live_print("  │")
        live_print("  ├─ 阶段2: 测速与校验")
        live_print(f"  │  ├ 成功率 ............ {source_ok:>4}/{source_total} ({source_ok*100//source_total if source_total else 0}%)")
        live_print(f"  │  ├ 来源统计 .......... {source_count:>4} 个来源")
        live_print(f"  │  ├ 失败TOP ........... {top_fails[0][0]+': '+str(top_fails[0][1]) if top_fails else 'N/A'}")
        if reso_stats:
            live_print(f"  │  └ 分辨率分布 ........ {', '.join(f'{lbl}={cnt}' for lbl, cnt in sorted(reso_stats.items(), key=lambda x:-x[1])[:3])}")
        else:
            live_print("  │  └ 分辨率分布 ........ (无数据)")
        live_print("  │")
        live_print("  ├─ 阶段3: 模板进化与输出")
        live_print(f"  │  ├ 有效频道 .......... {total_channels:>4} 个 (→ output/live.m3u)")
        live_print(f"  │  ├ 输出分类数 ........ {len(cat_order):>4} 个")
        live_print(f"  │  ├ 成人频道 .......... {adult_count:>4} 个 (→ output/adult.m3u)")
        live_print(f"  │  └ EPG .............. {'✅' if epg_report else '❌'}")
        live_print("  │")
        live_print(f"  └─ 耗时: {elapsed:.2f}s")

        # ── 详细统计：写入 GITHUB_STEP_SUMMARY ──
        write_summary("## 🧠 阶段3 — 模板进化与输出\n")
        write_summary_table(
            ["指标", "数值"],
            [
                ["📺 有效频道", f"{total_channels} 个"],
                ["📂 输出分类", f"{len(cat_order)} 个"],
                ["🔞 成人频道", f"{adult_count} 个"],
                ["📅 EPG", "✅" if epg_report else "❌"],
                ["⏱️ 总耗时", f"{elapsed:.0f}s ({elapsed/60:.1f}min)"],
            ]
        )
        write_summary("")

        # 总体概览折叠
        write_summary("<details><summary>📈 总体概览</summary>\n")
        write_summary_table(
            ["指标", "数值"],
            [
                ["抓取频道总数", source_total],
                ["待测频道", to_test_count],
                ["测速成功", f"{source_ok} ({source_ok*100//source_total if source_total else 0}%)"],
                ["有效频道(去重)", total_channels],
                ["成人频道", adult_count],
                ["输出分类", len(cat_order)],
                ["白名单免测", len(logs_whitelist)],
                ["黑名单拦截", len(logs_blacklist)],
                ["非TV过滤", non_tv_count],
                ["EPG", "✅" if epg_report else "❌"],
                ["运行耗时", f"{elapsed:.0f}s ({elapsed/60:.1f}min)"],
            ]
        )
        write_summary("\n</details>\n")

        # ── 共享统计：仅计算一次，供下方 Markdown 与控制台双渲染（消除重复构建）──
        src_rows = []
        for src in sorted(src_total_dict, key=lambda s: src_total_dict[s], reverse=True):
            ok = src_ok_dict.get(src, 0)
            total = src_total_dict[src]
            rate = f"{ok/total*100:.1f}%" if total > 0 else "-"
            bar = "🟢" if ok/total >= 0.8 else "🟡" if ok/total >= 0.5 else "🔴"
            src_rows.append((src, ok, total, rate, bar))
        fail_rows = sorted(fail_counts.items(), key=lambda x: x[1], reverse=True) if fail_counts else []
        cat_rows = [(c, n) for c, n in sorted(cat_live_counts.items(), key=lambda x: x[1], reverse=True) if n > 0] if cat_live_counts else []
        reso_rows = sorted(reso_stats.items(), key=lambda x: x[1], reverse=True) if reso_stats else []

        # 各来源测速结果折叠
        if src_rows:
            write_summary("<details><summary>🔗 各来源测速结果</summary>\n")
            rows = [[f"{bar} {src.split('/')[-1][:50]}", ok, total, rate] for (src, ok, total, rate, bar) in src_rows]
            write_summary_table(["来源", "成功", "总计", "成功率"], rows)
            write_summary("\n</details>\n")

        # 失败原因折叠
        if fail_rows:
            total_fails = sum(c for _, c in fail_rows)
            write_summary("<details><summary>❌ 失败原因分布</summary>\n")
            rows = [[cat, cnt, f"{cnt/total_fails*100:.1f}%"] for cat, cnt in fail_rows]
            write_summary_table(["原因", "数量", "占比"], rows)
            write_summary("\n</details>\n")

        # 分类频道存活折叠
        if cat_rows:
            write_summary("<details><summary>📺 分类频道存活情况</summary>\n")
            rows = [[cat, cnt, '🟩' * min(cnt // 5 + 1, 15)] for cat, cnt in cat_rows]
            write_summary_table(["分类", "存活数", ""], rows)
            write_summary("\n</details>\n")

        # 分辨率折叠
        if reso_rows:
            write_summary("<details><summary>🖥️ 分辨率分布</summary>\n")
            rows = [[lbl, cnt, '🟦' * min(cnt // 5 + 1, 15)] for lbl, cnt in reso_rows]
            write_summary_table(["分辨率", "数量", ""], rows)
            write_summary("\n</details>\n")

        # 文件列表
        write_summary("<details><summary>💾 输出文件</summary>\n")
        file_rows = []
        for f_path in ["output/live.txt", "output/live.m3u", "output/adult.txt", "output/adult.m3u",
                       "output/epg.xml", "output/epg.xml.gz", "output/log.txt"]:
            if os.path.exists(f_path):
                size = os.path.getsize(f_path)
                size_str = f"{size/1024:.1f}KB" if size < 1024*1024 else f"{size/1024/1024:.1f}MB"
                file_rows.append([f"`{f_path}`", size_str])
        if file_rows:
            write_summary_table(["文件", "大小"], file_rows)
        write_summary("\n</details>\n")

        # 控制台也输出详细统计（复用上方共享统计，不再重复计算）
        live_print("")
        live_print("━━━ 📋 详细统计 ━━━━━━━━━━━━━━━━━━━━━━━━━")
        # 来源统计表
        if src_rows:
            live_print("\n🔗 各来源测速结果:")
            live_print(f"  {'来源':<50} {'成功':>6} {'总计':>6} {'成功率':>8}")
            live_print(f"  {'─'*74}")
            for (src, ok, total, rate, bar) in src_rows:
                label = src.split("/")[-1][:48]
                live_print(f"  {label:<50} {ok:>6} {total:>6} {rate:>8}")
        # 失败分布
        if fail_rows:
            live_print("\n❌ 失败原因分布:")
            for cat, cnt in fail_rows:
                live_print(f"  {cat:<20} {cnt}")
        # 分类存活
        if cat_rows:
            live_print("\n📺 分类频道存活情况:")
            for cat, cnt in cat_rows:
                live_print(f"  {cat:<40} {cnt} 个")
        # 分辨率
        if reso_rows:
            live_print("\n🖥️ 分辨率分布:")
            for lbl, cnt in reso_rows:
                bar = '█' * min(cnt // 2 + 1, 15)
                live_print(f"  {lbl:<10} {cnt:>4}  {bar}")

    # 保存 AI 标准化缓存
    if _AI_CACHE:
        _save_ai_cache(_AI_CACHE)
        if _AI_AVAILABLE:
            stats = get_cache_stats()
            live_print(f"  🤖 AI 缓存已保存: 运行时命中 {stats['hits']} / 未命中 {stats['misses']}")

    flush_summary()


if __name__ == "__main__":
    main()
