from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from utils.config import (
    ADULT_M3U,
    ADULT_TXT,
    LOG_FILE,
    M3U_HEADER,
    MIN_RESOLUTION,
    MIN_RESOLUTION_PIXELS,
    OUTPUT_M3U,
    OUTPUT_TXT,
    fmt_resolution,
    live_print,
)
from utils.loaders import get_local_logo_url


def write_outputs(valid_results: Dict[str, List[Tuple[str, float]]], cat_order: List[str], chans_in_cat: Dict[str, List[str]], epg_report: list, logs_success: list, logs_fail: list, logs_whitelist: list, logs_blacklist: list, extra_stats: Optional[Dict[str, Any]] = None, adult_results: Optional[Dict[str, List[Tuple[str, float]]]] = None, channel_to_station: Optional[Dict[str, str]] = None, resolution_map: Optional[Dict[str, Tuple[int, int]]] = None) -> None:
    """写入 M3U/TXT 成品 + 日志文件"""
    if extra_stats is None:
        extra_stats = {}
    if resolution_map is None:
        resolution_map = {}
    live_print("\n━━━ 💾 写入结果文件 ━━━━━━━━━━━━━━━━━━━━━━━━━")

    # 外部 fallback logo 基础 URL
    fallback_logo_base = "https://gh.felicity.ac.cn/https://raw.githubusercontent.com/taksssss/tv/main/icon"

    # 分辨率过滤统计
    reso_filtered = 0
    reso_ok = 0

    try:
        with open(OUTPUT_M3U, "w", encoding="utf-8") as fm3u, open(OUTPUT_TXT, "w", encoding="utf-8") as ftxt:
            fm3u.write(M3U_HEADER)
            for cat in cat_order:
                cat_written_in_txt = False
                for name in chans_in_cat.get(cat, []):
                    if name in valid_results:
                        # elapsed 排最前（白名单免测），其余按速度升序
                        valid_urls = sorted(valid_results[name], key=lambda x: (0 if x[1] < 0 else 1, x[1]))
                        for url, elapsed in valid_urls:
                            # 分辨率过滤
                            res = resolution_map.get(url, (0, 0))
                            w, h = res
                            if MIN_RESOLUTION_PIXELS > 0 and w * h > 0 and w * h < MIN_RESOLUTION_PIXELS:
                                reso_filtered += 1
                                continue

                            if not cat_written_in_txt:
                                ftxt.write(f"\n{cat}\n")
                                cat_written_in_txt = True

                            logo = get_local_logo_url(name)
                            if not logo:
                                logo = f"{fallback_logo_base}/{name}.png"

                            cat_clean = cat.split(',')[0]
                            elapsed_display = "免测" if elapsed < 0 else f"{elapsed}s"
                            reso_tag = fmt_resolution(w, h)

                            # EXTINF 含分辨率属性
                            if w > 0 and h > 0:
                                fm3u.write(f'#EXTINF:-1 RESOLUTION={w}x{h} tvg-id="{name}" tvg-name="{name}" tvg-logo="{logo}" group-title="{cat_clean}",{name}\n')
                            else:
                                fm3u.write(f'#EXTINF:-1 tvg-id="{name}" tvg-name="{name}" tvg-logo="{logo}" group-title="{cat_clean}",{name}\n')
                            fm3u.write(f"{url}\n")
                            ftxt.write(f"{name},{url}\n")
                            reso_ok += 1
    except OSError as e:
        live_print(f"❌ 写入 M3U/TXT 失败: {e}")
        return

    # 写入成人内容（如果有）
    adult_written = 0
    if adult_results:
        try:
            with open(ADULT_M3U, "w", encoding="utf-8") as fam3u, open(ADULT_TXT, "w", encoding="utf-8") as fatxt:
                fam3u.write(M3U_HEADER)
                fatxt.write("📛成人内容,#genre#\n")
                for name in sorted(adult_results.keys()):
                    valid_urls = sorted(adult_results[name], key=lambda x: (0 if x[1] < 0 else 1, x[1]))
                    for url, elapsed in valid_urls:
                        logo = f"https://gh.felicity.ac.cn/https://raw.githubusercontent.com/taksssss/tv/main/icon/{name}.png"
                        fam3u.write(f'#EXTINF:-1 tvg-id="{name}" tvg-name="{name}" tvg-logo="{logo}" group-title="📛成人内容",{name}\n')
                        fam3u.write(f"{url}\n")
                        fatxt.write(f"{name},{url}\n")
                        adult_written += 1
        except OSError as e:
            live_print(f"❌ 写入成人内容失败: {e}")

    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write(f"任务时间: {datetime.now()}\n")
        f.write(f"白名单免测: {len(logs_whitelist)} | 黑名单拦截: {len(logs_blacklist)}\n")
        f.write(f"常规测速有效: {len(logs_success)} | 常规测速失效: {len(logs_fail)}\n\n")

        if epg_report:
            f.write("\n".join(epg_report) + "\n\n")

        if logs_whitelist:
            f.write("✅ 白名单免测:\n" + "\n".join(logs_whitelist) + "\n\n")

        # iptv-api免测日志块已移除

        if logs_blacklist:
            f.write("❌ 黑名单拦截:\n" + "\n".join(logs_blacklist) + "\n\n")

        f.write("🟢 测速有效源:\n" + "\n".join(logs_success) + "\n\n")
        f.write("🔴 测速失效源:\n" + "\n".join(logs_fail))

    # 附加数据：写入 log.txt 额外统计
    if extra_stats:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("\n" + "=" * 42 + "\n")
            f.write("📊 补充统计\n" + "=" * 42 + "\n\n")

            # 来源统计
            source_ok = extra_stats.get("source_ok", {})
            source_total = extra_stats.get("source_total", {})
            if source_total:
                f.write("各来源测速结果:\n")
                f.write(f"  {'来源':<50} {'成功':>6} {'总计':>6} {'成功率':>8}\n")
                f.write(f"  {'─'*74}\n")
                for src in sorted(source_total, key=lambda s: source_total[s], reverse=True):
                    ok = source_ok.get(src, 0)
                    total = source_total[src]
                    rate = f"{ok/total*100:.1f}%" if total > 0 else "-"
                    label = src.split("/")[-1][:48]  # 取文件名
                    f.write(f"  {label:<50} {ok:>6} {total:>6} {rate:>8}\n")
                f.write("\n")

            # 失败分类
            fail_counts = extra_stats.get("fail_counts", {})
            if fail_counts:
                f.write("失败原因统计:\n")
                for cat in sorted(fail_counts, key=fail_counts.get, reverse=True):
                    f.write(f"  {cat:<12} {fail_counts[cat]}\n")
                f.write("\n")

            # 频道分类落点统计
            valid_count = extra_stats.get("cat_live_counts", {})
            if valid_count:
                f.write("分类频道存活情况:\n")
                for cat in sorted(valid_count, key=valid_count.get, reverse=True):
                    f.write(f"  {cat:<40} {valid_count[cat]} 个频道\n")
                f.write("\n")

            # 运行时间
            elapsed = extra_stats.get("elapsed_seconds", 0)
            if elapsed:
                f.write(f"总运行时长: {elapsed:.0f} 秒 ({elapsed/60:.1f} 分钟)\n")

    # ── 分辨率过滤结果日志 ──
    if reso_filtered or reso_ok:
        live_print("\n🖥️ 分辨率筛选结果:")
        live_print(f"  ├ 通过 (≥{MIN_RESOLUTION}) .... {reso_ok}")
        live_print(f"  └ 过滤 (<{MIN_RESOLUTION}) .... {reso_filtered}")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"\n分辨率筛选: 通过={reso_ok}, 过滤={reso_filtered} (阈值={MIN_RESOLUTION})\n")

    live_print("✅ 所有结果文件已生成至 output/ 目录")

