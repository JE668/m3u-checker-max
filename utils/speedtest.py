import concurrent.futures, random, time, subprocess, json, re, os
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

from utils.config import (
    CHECK_CONNECT_TIMEOUT, CHECK_READ_TIMEOUT, CHECK_TOTAL_TIMEOUT,
    DOWNLOAD_TARGET_BYTES, MIN_BANDWIDTH_MBPS, SAMPLE_PER_HOST,
    MAX_WORKERS, DEFAULT_HEADERS, INVALID_NAME_PATTERNS, BLACKLIST_FILE,
    get_session, get_pool, live_print, _AI_AVAILABLE
)

def probe_resolution(url: str, timeout: Optional[float] = None) -> Tuple[int, int]:
    """使用 ffprobe 探测直播流的视频分辨率
    
    返回: (width, height) 或 (0, 0)
    """
    if timeout is None:
        timeout = PROBE_TIMEOUT
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-select_streams", "v:0",
             "-rw_timeout", str(int(timeout * 1000000)),
             "-analyzeduration", "1500000",
             "-probesize", "5000000",
             url],
            capture_output=True, text=True, timeout=timeout + 2
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            for stream in data.get("streams", []):
                w = stream.get("width", 0)
                h = stream.get("height", 0)
                if w > 0 and h > 0:
                    return w, h
    except subprocess.TimeoutExpired:
        pass
    except json.JSONDecodeError:
        pass
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return 0, 0


# ===============================
# 5. 并发测速
# ===============================
def check_channel(main_name: str, url: str) -> Tuple[bool, str, str, float, str]:
    """并发测速：下载 1MB 验证 + 带宽测量 + TS格式校验"""
    start_time = time.time()
    try:
        with get_session().get(url, stream=True, timeout=(CHECK_CONNECT_TIMEOUT, CHECK_READ_TIMEOUT)) as r:
            if r.status_code != 200:
                return False, main_name, url, round(time.time() - start_time, 2), f"HTTP {r.status_code}"

            downloaded = 0
            last_chunk_time = time.time()
            ts_check_data = bytearray()  # 收集用于TS同步字节校验

            for chunk in r.iter_content(chunk_size=1024 * 64):
                now = time.time()
                # 总超时保护
                if now - start_time > CHECK_TOTAL_TIMEOUT:
                    bw = downloaded * 8 / (now - start_time) / 1_000_000 if now > start_time else 0
                    return False, main_name, url, round(now - start_time, 2), f"总超时({bw:.1f}Mbps)"
                # 单 chunk 间隔超时
                if now - last_chunk_time > CHECK_READ_TIMEOUT:
                    bw = downloaded * 8 / (now - start_time) / 1_000_000 if now > start_time else 0
                    return False, main_name, url, round(now - start_time, 2), f"读取超时({bw:.1f}Mbps)"

                # 收集前 512KB 用于格式校验
                if len(ts_check_data) < 512 * 1024:
                    ts_check_data.extend(chunk)

                downloaded += len(chunk)
                last_chunk_time = now

                if downloaded >= DOWNLOAD_TARGET_BYTES:
                    elapsed = time.time() - start_time
                    bandwidth = downloaded * 8 / elapsed / 1_000_000

                    # 带宽阈值过滤
                    if bandwidth < MIN_BANDWIDTH_MBPS:
                        return False, main_name, url, round(elapsed, 2), f"带宽不足({bandwidth:.1f}Mbps < {MIN_BANDWIDTH_MBPS})"

                    # MPEG-TS 同步字节(0x47)校验
                    ts_sample = memoryview(ts_check_data)[:512 * 1024]
                    ts_score = 0.0
                    if len(ts_sample) >= 188:
                        expected = len(ts_sample) // 188
                        syncs = sum(1 for i in range(0, expected * 188, 188) if ts_sample[i] == 0x47)
                        ts_score = syncs / expected if expected > 0 else 0

                    if ts_score >= 0.8:
                        return True, main_name, url, round(elapsed, 2), f"TS流({bandwidth:.1f}Mbps)"
                    else:
                        # 非TS（HLS/FLV等），走带宽判断
                        return True, main_name, url, round(elapsed, 2), f"非TS({bandwidth:.1f}Mbps)"

            # 流结束但不足下载目标
            elapsed = time.time() - start_time
            bw = downloaded * 8 / elapsed / 1_000_000 if elapsed > 0 else 0
            return False, main_name, url, round(elapsed, 2), f"流数据不足({bw:.1f}Mbps)"

    except requests.exceptions.Timeout:
        return False, main_name, url, round(time.time() - start_time, 2), "连接超时"
    except requests.exceptions.ConnectionError as e:
        return False, main_name, url, round(time.time() - start_time, 2), f"连接失败: {e}"
    except Exception as e:
        return False, main_name, url, round(time.time() - start_time, 2), f"异常: {type(e).__name__}: {e}"

# ===============================
# 6. 核心：无损追加模式进化 demo.txt
# ===============================
# ===============================
# 6a. 频道分类引擎
# ===============================
# 频道分类规则：(匹配关键词列表, 分类显示名, 排序优先级)
# 优先级编号越小越优先匹配


# P1-9: 预编译排序用正则
_NUM_RE = re.compile(r'\d+')

# --- demo.txt 自学习分类规则 ---


# 预编译排序用正则
_NUM_RE = re.compile(r'\d+')



def apply_filter_lists(channels: list, blacklist_names: Set[str], blacklist_urls: Set[str], whitelist_names: Set[str], whitelist_urls: Set[str]) -> Tuple[list, Dict[str, list], list, list]:
    """黑白名单过滤分流
    
    channels: [(name, url, source_url), ...]
    - 白名单 → 并发 HEAD 存活检测，在线免测，离级降级
    - 黑名单 → 拦截
    - 无效频道名 → 自动追加黑名单
    - 其余 → 进入 to_test 测速
    """
    to_test = []
    valid_results = {}
    logs_blacklist, logs_whitelist = [], []
    auto_blacklist = []  # 自动发现的无效频道名
    
    # 检测无效频道名
    for name, url, source_url in channels:
        for pattern in INVALID_NAME_PATTERNS:
            if re.match(pattern, name):
                auto_blacklist.append(name)
                logs_blacklist.append(f"⚫ [无效名] {name}")
                break

    # 第一趟：分离白名单条目，并发做存活检测
    whitelist_candidates = []  # [(name, url), ...]
    for name, url, source_url in channels:
        if name in blacklist_names or url in blacklist_urls or name in auto_blacklist:
            logs_blacklist.append(f"⚫ [黑名单屏蔽] {name:<12} | {url}")
        elif name in whitelist_names or url in whitelist_urls:
            whitelist_candidates.append((name, url))
        else:
            to_test.append((name, url))

    if whitelist_candidates:
        whitelist_alive = set()  # 存储 (name, url) of alive entries
        dead_entries = []

        def _check_head(name, url):
            try:
                hr = get_session().head(url, timeout=3)
                if hr.status_code == 200:
                    return (name, url, True)
            except requests.RequestException:
                pass
            return (name, url, False)

        live_print(f"🔍 白名单存活检测: {len(whitelist_candidates)} 条 (并发 HEAD)")
        pool = get_pool()
        futs = {pool.submit(_check_head, n, u): (n, u) for n, u in whitelist_candidates}
        for f in concurrent.futures.as_completed(futs):
                name, url, alive = f.result()
                if alive:
                    whitelist_alive.add((name, url))
                    if name not in valid_results: valid_results[name] = []
                    valid_results[name].append((url, -1.0))
                    logs_whitelist.append(f"⚪ [白名单免测] {name:<12} | 免测 | {url}")
                else:
                    to_test.append((name, url))
                    logs_whitelist.append(f"⚪→🔍 [白名单离线] {name:<12} | 降级测速 | {url}")

    # 自动追加无效频道名到黑名单文件（检查去重，防无限膨胀）
    if auto_blacklist:
        existing = set()
        appended_already = False
        auto_section_line = -1
        if os.path.exists(BLACKLIST_FILE):
            with open(BLACKLIST_FILE, 'r', encoding='utf-8') as f:
                bl_lines = f.readlines()
            for i, line in enumerate(bl_lines):
                s = line.strip()
                if s and not s.startswith('#'):
                    existing.add(s)
                if '# 自动追加的无效频道名' in line:
                    appended_already = True
                    if auto_section_line < 0:
                        auto_section_line = i
        new_entries = set(auto_blacklist) - existing
        if new_entries:
            if appended_already and auto_section_line >= 0:
                # 插入到已有 auto 区块之后（跳过连续注释行和空行）
                insert_pos = auto_section_line + 1
                while insert_pos < len(bl_lines):
                    s = bl_lines[insert_pos].strip()
                    if s and not s.startswith('#'):
                        break
                    insert_pos += 1
                # 在首个非注释/非空行之前插入新条目
                for name in sorted(new_entries):
                    bl_lines.insert(insert_pos, f"{name}\n")
                    insert_pos += 1
                with open(BLACKLIST_FILE, 'w', encoding='utf-8') as f:
                    f.writelines(bl_lines)
            else:
                with open(BLACKLIST_FILE, 'a', encoding='utf-8') as f:
                    f.write("\n# 自动追加的无效频道名\n")
                    for name in sorted(new_entries):
                        f.write(f"{name}\n")
            live_print(f"  📛 [自动黑名单] 发现 {len(new_entries)} 个新无效频道名，已追加到 {BLACKLIST_FILE}")
        else:
            live_print(f"  ℹ️ [自动黑名单] 本次无新无效频道名，跳过追加")
    
    return to_test, valid_results, logs_blacklist, logs_whitelist


def _classify_failure(reason: str) -> str:
    """对失败原因分类"""
    if reason.startswith("HTTP "):
        code = reason.split()[1]
        if code.startswith("4"):
            return "HTTP 4xx"
        elif code.startswith("5"):
            return "HTTP 5xx"
        else:
            return f"HTTP {code}"
    if reason.startswith("连接超时"):
        return "连接超时"
    if reason.startswith("连接失败"):
        return "连接失败"
    if reason.startswith("读取超时"):
        return "读取超时"
    if reason.startswith("总超时"):
        return "总超时"
    if reason.startswith("带宽不足"):
        return "带宽不足"
    if reason.startswith("流数据不足"):
        return "流数据不足"
    if reason.startswith("异常"):
        return "其他异常"
    if reason.startswith("⏭️") or "服务器死亡" in reason:
        return "服务器死亡"
    return "其他"


def run_speed_test(to_test: list, source_meta: Optional[dict] = None, source_urls: Optional[Dict[str, str]] = None, channel_to_station: Optional[Dict[str, str]] = None) -> Tuple[Dict[str, list], list, list, Dict[str, int], Dict[str, dict]]:
    """并发测速：服务器级预筛 + 全量测速
    
    source_urls: {url: source_url} — 来源统计用
    返回: (valid_results, logs_success, logs_fail, fail_counts, source_stats)
    
    改进点：
    1. 按 host (ip:port) 分组
    2. 每组先抽 SAMPLE_PER_HOST 个频道预检
    3. 预检全部失败 → 标记服务器死亡，跳过该组其余频道
    4. 预检至少通过一个 → 全量测试该组其余频道
    5. 若提供 source_meta，低带宽服务器减少预检样本，全量按带宽降序优先
    """
    valid_results = {}
    logs_success, logs_fail = [], []
    fail_counts = {}  # 失败原因分类统计
    source_ok, source_total = {}, {}  # 来源统计

    # 频道名+电视台归属显示
    def _fmt_name(name):
        s = (channel_to_station or {}).get(name, '')
        return f"{name}（{s}）" if s else name

    if not to_test:
        return valid_results, logs_success, logs_fail, fail_counts, {"ok": {}, "total": {}}

    # Phase 1: 按 host 分组
    host_groups = {}
    for name, url in to_test:
        parsed = urlparse(url)
        host = parsed.hostname
        if host is None:
            host = url
        elif ':' in host:
            host = f"[{host}]"
        try:
            port = parsed.port
        except ValueError:
            port = None
        if port and host != url:
            host = f"{host}:{port}"
        host_groups.setdefault(host.lower(), []).append((name, url))

    live_print(f"\n📊 测速分组: {len(host_groups)} 台服务器, {len(to_test)} 个频道")

    # Phase 2: 自适应样本数 — 从 meta 读取带宽，低带宽机器少抽
    # 优先级规则：带宽越高越优先全量测
    host_priority = {}  # host -> sort_key (数字越大越优先)
    for host in host_groups:
        bw = (source_meta or {}).get(host, {}).get("bandwidth_mbps", 0) or 0
        if bw >= 5.0:
            samples = SAMPLE_PER_HOST
            priority = 4
        elif bw >= 2.0:
            samples = SAMPLE_PER_HOST
            priority = 3
        elif bw >= 1.0:
            samples = max(1, SAMPLE_PER_HOST - 1)  # 减一半样本
            priority = 2
        elif bw >= 0.5:
            samples = 1  # 只测 1 个
            priority = 1
        else:
            samples = 1  # 无 meta 或极低带宽，也测 1 个碰运气
            priority = 0
        host_priority[host] = (priority, bw, host)
        if source_meta and bw > 0:
            live_print(f"  ⚡ {host:<21} | {bw:>5.1f}Mbps | 样本数: {samples}")

    sample_tasks = []            # (name, url, host)
    remaining_by_host = {}       # host -> [(name, url), ...]
    small_host_tasks = []        # (name, url, host) — 不足样本数的直接全量

    for host, entries in host_groups.items():
        bl, bw, _ = host_priority[host]
        sample_count = min(max(1, SAMPLE_PER_HOST if bl >= 2 else (1 if bw >= 0.5 else 1)), len(entries))
        entries_list = list(entries)
        if len(entries_list) <= sample_count:
            small_host_tasks.extend((n, u, host) for n, u in entries_list)
        else:
            sampled = random.sample(entries_list, sample_count)
            sampled_set = set(sampled)
            remaining = [e for e in entries_list if e not in sampled_set]
            sample_tasks.extend((n, u, host) for n, u in sampled)
            remaining_by_host[host] = remaining

    total_samples = len(sample_tasks) + len(small_host_tasks)
    sample_results = {}  # host -> [True/False, ...]
    sample_processed = 0

    # 合并样本任务一起并发测
    all_samples = sample_tasks + small_host_tasks

    def _process_result(name, url, host, is_valid, elapsed, reason):
        nonlocal sample_processed
        sample_processed += 1
        sample_results.setdefault(host, []).append(is_valid)
        if not is_valid:
            cat = _classify_failure(reason)
            fail_counts[cat] = fail_counts.get(cat, 0) + 1
            if source_urls:
                src = source_urls.get(url, "未知")
                source_total[src] = source_total.get(src, 0) + 1
        if is_valid:
            if source_urls:
                src = source_urls.get(url, "未知")
                source_ok[src] = source_ok.get(src, 0) + 1
                source_total[src] = source_total.get(src, 0) + 1
            valid_results.setdefault(name, []).append((url, elapsed))
            msg = f"🎯 [{sample_processed}/{total_samples}] 🟢 {_fmt_name(name):<24} | {elapsed:>4}s | {reason:<15} | {url}"
            live_print(msg)
            logs_success.append(msg)
        else:
            msg = f"🎯 [{sample_processed}/{total_samples}] 🔴 {_fmt_name(name):<24} | {reason:<15} | {url}"
            logs_fail.append(msg)

    if all_samples:
        live_print(f"🎯 预筛阶段: {len(all_samples)} 个样本")
        pool = get_pool()
        futures = {pool.submit(check_channel, name, url): (name, url, host)
                   for name, url, host in all_samples}
        for future in concurrent.futures.as_completed(futures):
            name, url, host = futures[future]
            is_valid, _, _, elapsed, reason = future.result()
            _process_result(name, url, host, is_valid, elapsed, reason)

    # 判断服务器死活
    alive_hosts = set(h for h, rs in sample_results.items() if any(rs))
    dead_hosts = set(remaining_by_host.keys()) - alive_hosts

    if dead_hosts:
        skipped = sum(len(remaining_by_host[h]) for h in dead_hosts)
        live_print(f"\n💀 淘汰 {len(dead_hosts)} 台死服务器, 跳过 {skipped} 个频道")
        for host in dead_hosts:
            for name, url in remaining_by_host[host]:
                logs_fail.append(f"⏭️ [服务器死亡] {_fmt_name(name):<24} | {url}")

    # Phase 3: 全量测速存活服务器的剩余频道，按 meta 带宽降序排序
    full_test = []
    alive_host_list = sorted(alive_hosts, key=lambda h: host_priority.get(h, (0, 0, h))[0], reverse=True)
    for host in alive_host_list:
        if host in remaining_by_host:
            # 同 host 内按频道名排序保持一致
            entries = sorted(remaining_by_host[host])
            full_test.extend([(n, u) for n, u in entries])

    if full_test:
        total = len(full_test)
        full_processed = 0
        live_print(f"🚀 全量测速: {total} 个频道 ({len(alive_hosts)} 台服务器, 优先高带宽)")

        pool = get_pool()
        futures = {pool.submit(check_channel, name, url): (name, url)
                   for name, url in full_test}
        for future in concurrent.futures.as_completed(futures):
            full_processed += 1
            name, url = futures[future]
            is_valid, _, _, elapsed, reason = future.result()
            if is_valid:
                if source_urls:
                    src = source_urls.get(url, "未知")
                    source_ok[src] = source_ok.get(src, 0) + 1
                    source_total[src] = source_total.get(src, 0) + 1
                valid_results.setdefault(name, []).append((url, elapsed))
                msg = f"[{full_processed}/{total}] 🟢 {_fmt_name(name):<24} | {elapsed:>4}s | {reason:<15} | {url}"
                live_print(msg)
                logs_success.append(msg)
            else:
                cat = _classify_failure(reason)
                fail_counts[cat] = fail_counts.get(cat, 0) + 1
                if source_urls:
                    src = source_urls.get(url, "未知")
                    source_total[src] = source_total.get(src, 0) + 1
                msg = f"[{full_processed}/{total}] 🔴 {_fmt_name(name):<24} | {reason:<15} | {url}"
                logs_fail.append(msg)

    live_print(f"\n🏁 测速结束: 成功 {len(logs_success)} / 失败 {len(logs_fail)}\n")

    # 失败原因分类统计
    if fail_counts:
        live_print("📊 失败原因分布:")
        live_print(f"  {'类别':<12} {'数量':>5}")
        live_print(f"  {'─'*18}")
        for cat in sorted(fail_counts, key=fail_counts.get, reverse=True):
            count = fail_counts[cat]
            bar = '█' * min(count // 5 + 1, 15)
            live_print(f"  {cat:<12} {count:>5}  {bar}")
        live_print("")

    # 来源统计
    if source_total:
        live_print("📊 各来源测速结果:")
        live_print(f"  {'来源':<30} {'成功':>5} {'总计':>5} {'成功率':>8}")
        live_print(f"  {'─'*52}")
        for src in sorted(source_total, key=lambda s: source_total[s], reverse=True):
            ok = source_ok.get(src, 0)
            total = source_total[src]
            rate = f"{ok/total*100:.0f}%" if total > 0 else "-"
            bar = '█' * max(1, min(ok * 15 // max(total, 1), 15))
            dim = '░' * (15 - len(bar))
            live_print(f"  {src[-30:]:>30} {ok:>5} {total:>5} {rate:>8}  {bar}{dim}")
        live_print("")

    return valid_results, logs_success, logs_fail, fail_counts, {"ok": source_ok, "total": source_total}

