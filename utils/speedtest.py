import concurrent.futures
import json
import os
import random
import re
import shutil
import subprocess
import threading
import time
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

import requests

from utils.config import (
    ADAPTIVE_CONCURRENCY,
    ADAPTIVE_SUCCESS_HIGH,
    ADAPTIVE_SUCCESS_LOW,
    BASE_WORKERS,
    BLACKLIST_FILE,
    CHECK_CONNECT_TIMEOUT,
    CHECK_READ_TIMEOUT,
    CHECK_TOTAL_TIMEOUT,
    DOWNLOAD_TARGET_BYTES,
    INCREMENTAL_SAMPLE_MIN,
    INCREMENTAL_SAMPLE_RATIO,
    INCREMENTAL_TESTING,
    INVALID_NAME_PATTERNS,
    MAX_WORKERS_CAP,
    MIN_BANDWIDTH_MBPS,
    MIN_WORKERS,
    PROBE_TIMEOUT,
    RESOLUTION_CACHE_ENABLED,
    RESOLUTION_CACHE_MAX_ENTRIES,
    RESOLUTION_CACHE_TTL,
    SAMPLE_PER_HOST,
    SUCCESS_LOG_SAMPLE_LIMIT,
    get_pool,
    get_session,
    live_print,
)

__all__ = [
    "check_channel", "probe_resolution", "probe_resolution_cached", "run_speed_test",
    "apply_filter_lists", "append_auto_blacklist",
    "get_initial_worker_count", "adjust_worker_count",
    "WHITELIST_HEAD_TIMEOUT",
]

# 白名单存活检测 HEAD 请求超时（秒）：HEAD 只探测存活，1.5s 足够，
# 3s 在全挂源上会放大为 50 线程 × 3s 的排队浪费
WHITELIST_HEAD_TIMEOUT = 1.5

# ── 分辨率缓存 ──
RESOLUTION_CACHE_FILE = "output/resolution_cache.json"

# ── 自适应并发状态 ──
ADAPTIVE_STATE_FILE = "output/.adaptive_state.json"

# ffprobe 可用性检查（首次调用时检测，结果缓存）
_ffprobe_checked = False
_ffprobe_available = False
_ffprobe_lock = threading.Lock()

def _check_ffprobe():
    global _ffprobe_checked, _ffprobe_available
    with _ffprobe_lock:
        if not _ffprobe_checked:
            _ffprobe_available = shutil.which("ffprobe") is not None
            _ffprobe_checked = True
            if not _ffprobe_available:
                live_print("⚠️ ffprobe 未安装，分辨率检测将跳过（所有频道返回 0x0）")
    return _ffprobe_available

def probe_resolution(url: str, timeout: Optional[float] = None) -> Tuple[int, int]:
    """使用 ffprobe 探测直播流的视频分辨率

    采用「单次下载 + 管道」：通过共享 session 拉流，直接管道喂给 ffprobe，
    避免 ffprobe 自己再发起一次网络下载（原先的冗余请求），
    同时复用与 check_channel 一致的 session/UA/代理/重试策略。

    返回: (width, height) 或 (0, 0)
    """
    if not _check_ffprobe():
        return 0, 0
    if timeout is None:
        timeout = PROBE_TIMEOUT
    try:
        with get_session().get(url, stream=True, timeout=(CHECK_CONNECT_TIMEOUT, CHECK_READ_TIMEOUT)) as resp:
            if resp.status_code != 200:
                return 0, 0
            proc = subprocess.Popen(
                ["ffprobe", "-v", "quiet", "-print_format", "json",
                 "-show_streams", "-select_streams", "v:0",
                 "-rw_timeout", str(int(timeout * 1000000)),
                 "-analyzeduration", "1500000",
                 "-probesize", "5000000",
                 "-i", "pipe:0"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            downloaded = 0
            try:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    try:
                        proc.stdin.write(chunk)
                    except (BrokenPipeError, ValueError):
                        break
                    downloaded += len(chunk)
                    if downloaded >= 5000000:  # 达到 probesize 上限即停止喂数据
                        break
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
            try:
                out, _ = proc.communicate(timeout=timeout + 2)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, _ = proc.communicate()
            if out and out.strip():
                data = json.loads(out)
                for stream in data.get("streams", []):
                    w = stream.get("width", 0)
                    h = stream.get("height", 0)
                    if w > 0 and h > 0:
                        return w, h
    except requests.exceptions.RequestException:
        return 0, 0
    except subprocess.SubprocessError:
        return 0, 0
    except json.JSONDecodeError:
        return 0, 0
    except Exception:
        return 0, 0
    return 0, 0


# ── 分辨率缓存并发写锁（50 线程池并发调用时的 read-modify-write 竞态防护）──
_RESO_CACHE_LOCK = threading.Lock()

# 失败探测（0x0）使用更短的缓存 TTL：流恢复后能被更快重新探测
RESOLUTION_FAIL_TTL = int(os.environ.get("RESOLUTION_FAIL_TTL", str(24 * 3600)))  # 默认 1 天


def probe_resolution_cached(url: str) -> Tuple[int, int]:
    """带缓存的分辨率探测：URL hash 命中且未过期则直接返回，节省 ffprobe 调用

    - 成功结果缓存 RESOLUTION_CACHE_TTL（默认 7 天）
    - 失败结果（0,0）只缓存 RESOLUTION_FAIL_TTL（默认 1 天），流恢复后可更快重探
    - 50 线程并发下用锁保护缓存文件的读-改-写
    """
    import hashlib, json, os, time as _time

    cache_key = hashlib.sha256(url.encode()).hexdigest()[:16]

    # 尝试读取缓存（成功与失败条目使用不同 TTL）
    if os.path.exists(RESOLUTION_CACHE_FILE):
        try:
            with open(RESOLUTION_CACHE_FILE, 'r', encoding='utf-8') as f:
                cache = json.load(f)
            entry = cache.get(cache_key)
            if entry:
                ttl = RESOLUTION_CACHE_TTL if (entry.get("w", 0) > 0) else RESOLUTION_FAIL_TTL
                if _time.time() - entry.get("ts", 0) < ttl:
                    return (entry["w"], entry["h"])
        except (json.JSONDecodeError, OSError, KeyError):
            pass

    # 缓存未命中，实际探测
    wh = probe_resolution(url)

    # 更新缓存（加锁：50 线程并发 read-modify-write 防竞态）
    try:
        with _RESO_CACHE_LOCK:
            cache = {}
            if os.path.exists(RESOLUTION_CACHE_FILE):
                try:
                    with open(RESOLUTION_CACHE_FILE, 'r', encoding='utf-8') as f:
                        cache = json.load(f)
                except (json.JSONDecodeError, OSError):
                    cache = {}
            cache[cache_key] = {"w": wh[0], "h": wh[1], "ts": _time.time()}

            # 保留最近 N 条，防止无限膨胀
            if len(cache) > RESOLUTION_CACHE_MAX_ENTRIES:
                sorted_entries = sorted(cache.items(), key=lambda x: x[1].get("ts", 0))
                cache = dict(sorted_entries[-RESOLUTION_CACHE_MAX_ENTRIES:])

            os.makedirs(os.path.dirname(RESOLUTION_CACHE_FILE), exist_ok=True)
            with open(RESOLUTION_CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump(cache, f, ensure_ascii=False)
    except Exception:
        pass  # 缓存写入失败不影响主流程

    return wh


# ===============================
# 4a. 自适应并发
# ===============================
def load_adaptive_state() -> dict:
    """加载上次的自适应并发状态"""
    if os.path.exists(ADAPTIVE_STATE_FILE):
        try:
            with open(ADAPTIVE_STATE_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_adaptive_state(success_rate: float, worker_count: int):
    """保存自适应并发状态供下次运行使用"""
    try:
        os.makedirs(os.path.dirname(ADAPTIVE_STATE_FILE), exist_ok=True)
        with open(ADAPTIVE_STATE_FILE, 'w') as f:
            json.dump({
                "last_success_rate": success_rate,
                "last_workers": worker_count,
                "timestamp": time.time()
            }, f)
    except Exception:
        pass


def get_initial_worker_count() -> int:
    """根据上次运行的成功率计算本次初始并发数"""
    state = load_adaptive_state()
    last_rate = state.get("last_success_rate")
    if last_rate is None:
        return BASE_WORKERS

    # 上次成功率低 → 减少并发；高 → 增加
    if last_rate < ADAPTIVE_SUCCESS_LOW:
        return max(MIN_WORKERS, int(BASE_WORKERS * last_rate / ADAPTIVE_SUCCESS_LOW))
    elif last_rate > ADAPTIVE_SUCCESS_HIGH:
        return min(MAX_WORKERS_CAP, int(BASE_WORKERS * 1.5))
    return BASE_WORKERS


def adjust_worker_count(current_workers: int, success_count: int, total_count: int) -> int:
    """根据成功率动态调整并发数"""
    if not ADAPTIVE_CONCURRENCY or total_count < 10:
        return current_workers

    success_rate = success_count / total_count
    if success_rate < ADAPTIVE_SUCCESS_LOW:
        new_workers = max(MIN_WORKERS, current_workers // 2)
        live_print(f"  ⚡ 成功率偏低 ({success_rate:.0%})，并发 {current_workers} → {new_workers}")
    elif success_rate > ADAPTIVE_SUCCESS_HIGH:
        new_workers = min(MAX_WORKERS_CAP, current_workers + 10)
        live_print(f"  ⚡ 成功率良好 ({success_rate:.0%})，并发 {current_workers} → {new_workers}")
    else:
        return current_workers

    return new_workers


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
            ts_check_data = bytearray(3760)  # 只需前 20 个 TS 包 (20×188=3760)
            ts_offset = 0

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

                # 收集前 3760 字节用于 TS 格式校验
                if ts_offset < 3760:
                    remaining = 3760 - ts_offset
                    chunk_to_copy = chunk[:remaining]
                    ts_check_data[ts_offset:ts_offset+len(chunk_to_copy)] = chunk_to_copy
                    ts_offset += len(chunk_to_copy)

                downloaded += len(chunk)
                last_chunk_time = now
                
                # 早期带宽评估：已下载 200KB 且带宽远低于阈值时提前终止
                if 200_000 <= downloaded < DOWNLOAD_TARGET_BYTES:
                    early_bw = downloaded * 8 / (now - start_time) / 1_000_000
                    if early_bw < MIN_BANDWIDTH_MBPS * 0.3:
                        return False, main_name, url, round(now - start_time, 2), f"带宽不足(早期 {early_bw:.1f}Mbps)"

                if downloaded >= DOWNLOAD_TARGET_BYTES:
                    elapsed = time.time() - start_time
                    bandwidth = downloaded * 8 / elapsed / 1_000_000

                    # 带宽阈值过滤
                    if bandwidth < MIN_BANDWIDTH_MBPS:
                        return False, main_name, url, round(elapsed, 2), f"带宽不足({bandwidth:.1f}Mbps < {MIN_BANDWIDTH_MBPS})"

                    # MPEG-TS 同步字节(0x47)校验 — 只检查前20个TS包即可判断
                    check_count = min(ts_offset // 188, 20)
                    if check_count > 0:
                        syncs = sum(1 for i in range(0, check_count * 188, 188) if ts_check_data[i] == 0x47)
                        ts_score = syncs / check_count
                    else:
                        ts_score = 0.0

                    if ts_score >= 0.8:
                        return True, main_name, url, round(elapsed, 2), f"TS流({bandwidth:.1f}Mbps)"
                    else:
                        # 非TS（HLS/FLV等），走带宽判断
                        return True, main_name, url, round(elapsed, 2), f"非TS({bandwidth:.1f}Mbps)"

            # 流结束但不足下载目标
            elapsed = time.time() - start_time
            bw = downloaded * 8 / elapsed / 1_000_000 if elapsed > 0 else 0
            if ts_check_data[:8].startswith(b'#EXTM3U'):
                return _probe_hls_segment(main_name, url, bytes(ts_check_data[:ts_offset]))
            return False, main_name, url, round(elapsed, 2), f"流数据不足({bw:.1f}Mbps)"

    except requests.exceptions.Timeout:
        return False, main_name, url, round(time.time() - start_time, 2), "连接超时"
    except requests.exceptions.ConnectionError as e:
        return False, main_name, url, round(time.time() - start_time, 2), f"连接失败: {e}"
    except Exception as e:
        return False, main_name, url, round(time.time() - start_time, 2), f"异常: {type(e).__name__}: {e}"


def _probe_hls_segment(main_name: str, url: str, manifest: bytes) -> Tuple[bool, str, str, float, str]:
    """HLS（.m3u8 播放列表）探测：解析首个媒体分片并实测带宽。

    大量 IPTV 源（尤其成人/HLS 聚合源）只提供 playlist，直接测速永远「流数据不足」。
    """
    try:
        from urllib.parse import urljoin
        lines = [ln.strip() for ln in manifest.decode('utf-8', 'ignore').splitlines()
                 if ln.strip() and not ln.startswith('#')]
        seg_url = None
        for ln in lines:
            target = urljoin(url, ln)
            if ln.lower().endswith('.m3u8'):
                # 主清单 → 子清单（带宽变体，取第一个）
                with get_session().get(target, timeout=(CHECK_CONNECT_TIMEOUT, CHECK_READ_TIMEOUT)) as vr:
                    if vr.status_code == 200:
                        sub = [s.strip() for s in vr.text.splitlines() if s.strip() and not s.startswith('#')]
                        if sub:
                            seg_url = urljoin(target, sub[0])
                break
            else:
                seg_url = target
                break
        if not seg_url:
            return False, main_name, url, 0.0, "HLS清单无分片"

        start = time.time()
        with get_session().get(seg_url, stream=True, timeout=(CHECK_CONNECT_TIMEOUT, CHECK_READ_TIMEOUT)) as r:
            if r.status_code != 200:
                return False, main_name, url, 0.0, f"HLS分片 HTTP {r.status_code}"
            downloaded = 0
            for chunk in r.iter_content(chunk_size=64 * 1024):
                downloaded += len(chunk)
                if downloaded >= 200 * 1024:
                    break
            elapsed = time.time() - start
            bw = downloaded * 8 / elapsed / 1_000_000 if elapsed > 0 else 0
            if downloaded < 8 * 1024:
                return False, main_name, url, round(elapsed, 2), f"HLS分片过小({downloaded}B)"
            if bw < MIN_BANDWIDTH_MBPS * 0.3:
                return False, main_name, url, round(elapsed, 2), f"HLS带宽不足({bw:.1f}Mbps)"
            return True, main_name, url, round(elapsed, 2), f"HLS分片({bw:.1f}Mbps)"
    except Exception as e:
        return False, main_name, url, 0.0, f"HLS探测失败: {type(e).__name__}"



def apply_filter_lists(channels: list, blacklist_names: Set[str], blacklist_urls: Set[str], whitelist_names: Set[str], whitelist_urls: Set[str]) -> Tuple[list, Dict[str, list], list, list, list]:
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

    # 检测无效频道名（仅在此处记录一次 [无效名]，避免后续重复记录）
    for name, url, source_url in channels:
        for pattern in INVALID_NAME_PATTERNS:
            if re.match(pattern, name):
                auto_blacklist.append(name)
                logs_blacklist.append(f"⚫ [无效名] {name}")
                break

    # 第一趟：分离白名单条目，并发做存活检测
    whitelist_candidates = []  # [(name, url), ...]
    for name, url, source_url in channels:
        if name in blacklist_names or url in blacklist_urls:
            logs_blacklist.append(f"⚫ [黑名单屏蔽] {name:<12} | {url}")
        elif name in auto_blacklist:
            # 已在上方 [无效名] 检测中记录，此处不再重复记录，亦不进入测速/白名单
            pass
        elif name in whitelist_names or url in whitelist_urls:
            whitelist_candidates.append((name, url))
        else:
            to_test.append((name, url))

    if whitelist_candidates:
        whitelist_alive = set()  # 存储 (name, url) of alive entries

        def _check_head(name, url):
            try:
                hr = get_session().head(url, timeout=WHITELIST_HEAD_TIMEOUT)
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
                    if name not in valid_results:
                        valid_results[name] = []
                    valid_results[name].append((url, -1.0))
                    logs_whitelist.append(f"⚪ [白名单免测] {name:<12} | 免测 | {url}")
                else:
                    to_test.append((name, url))
                    logs_whitelist.append(f"⚪→🔍 [白名单离线] {name:<12} | 降级测速 | {url}")

    return to_test, valid_results, logs_blacklist, logs_whitelist, auto_blacklist


def append_auto_blacklist(auto_blacklist: List[str]) -> None:
    """将无效频道名自动追加到黑名单文件（与过滤分流逻辑解耦）。

    在 apply_filter_lists 检测出 auto_blacklist 后由调用方显式调用，
    避免「过滤即写文件」的副作用；写入前做去重，防止文件无限膨胀。
    """
    if not auto_blacklist:
        return
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
        live_print("  ℹ️ [自动黑名单] 本次无新无效频道名，跳过追加")

    _cap_auto_blacklist()


# 自动追加区块的最大条数：超出后丢弃最旧的，防止 git 仓库无限膨胀
AUTO_BLACKLIST_CAP = int(os.environ.get("AUTO_BLACKLIST_CAP", "5000"))


def _cap_auto_blacklist(cap: int = AUTO_BLACKLIST_CAP) -> None:
    """裁剪自动追加区块，仅保留最新的 cap 条（手动维护段不受影响）"""
    if cap <= 0 or not os.path.exists(BLACKLIST_FILE):
        return
    try:
        with open(BLACKLIST_FILE, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except OSError:
        return

    # 定位自动区块（从标记行到文件末尾）
    auto_start = -1
    for i, line in enumerate(lines):
        if '# 自动追加的无效频道名' in line:
            auto_start = i
            break
    if auto_start < 0:
        return

    auto_entries = [ln for ln in lines[auto_start + 1:] if ln.strip() and not ln.startswith('#')]
    if len(auto_entries) <= cap:
        return

    # 新条目在 append 时插到区块头部，故保留头部即为保留最新
    kept = auto_entries[:cap]
    new_lines = lines[:auto_start + 1] + kept
    try:
        with open(BLACKLIST_FILE, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)
        live_print(f"  🧹 [自动黑名单] 裁剪 {len(auto_entries)} → {cap} 条（保留最新）")
    except OSError:
        pass


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

    # ── 自适应并发 ──
    workers = get_initial_worker_count() if ADAPTIVE_CONCURRENCY else None
    local_executor = None
    if workers is not None:
        local_executor = concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="m3u-adaptive")
        live_print(f"⚡ 自适应并发: {workers} workers (基于上次成功率)")
    pool_fn = (lambda: local_executor) if workers is not None else get_pool

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
    success_printed = 0  # 控制台成功日志采样计数

    # 合并样本任务一起并发测
    all_samples = sample_tasks + small_host_tasks

    def _process_result(name, url, host, is_valid, elapsed, reason):
        nonlocal sample_processed, success_printed
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
            logs_success.append(msg)  # 完整日志仍写入文件
            if success_printed < SUCCESS_LOG_SAMPLE_LIMIT:
                live_print(msg)  # 控制台仅采样显示前 N 条成功
                success_printed += 1
        else:
            msg = f"🎯 [{sample_processed}/{total_samples}] 🔴 {_fmt_name(name):<24} | {reason:<15} | {url}"
            logs_fail.append(msg)
            live_print(msg)  # 失败日志全部打印，便于排查

    if all_samples:
        live_print(f"🎯 预筛阶段: {len(all_samples)} 个样本")
        pool = pool_fn()
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
                reason = f"⏭️ [服务器死亡] {_fmt_name(name):<24} | {url}"
                logs_fail.append(reason)
                cat = _classify_failure(reason)
                fail_counts[cat] = fail_counts.get(cat, 0) + 1

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

        pool = pool_fn()
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
                logs_success.append(msg)  # 完整日志仍写入文件
                if success_printed < SUCCESS_LOG_SAMPLE_LIMIT:
                    live_print(msg)  # 控制台仅采样显示前 N 条成功
                    success_printed += 1
            else:
                cat = _classify_failure(reason)
                fail_counts[cat] = fail_counts.get(cat, 0) + 1
                if source_urls:
                    src = source_urls.get(url, "未知")
                    source_total[src] = source_total.get(src, 0) + 1
                msg = f"[{full_processed}/{total}] 🔴 {_fmt_name(name):<24} | {reason:<15} | {url}"
                logs_fail.append(msg)
                live_print(msg)  # 失败日志全部打印，便于排查

    live_print(f"\n🏁 测速结束: 成功 {len(logs_success)} / 失败 {len(logs_fail)}\n")
    if success_printed >= SUCCESS_LOG_SAMPLE_LIMIT:
        live_print(f"  ℹ️ 成功日志已采样（仅显示前 {SUCCESS_LOG_SAMPLE_LIMIT} 条），共 {len(logs_success)} 条成功详情见 output/log.txt")

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

    # ── 清理本地执行器并保存自适应状态 ──
    if local_executor:
        local_executor.shutdown(wait=False)
        total_count = len(to_test)
        ok_count = len(logs_success)
        success_rate = ok_count / max(total_count, 1)
        save_adaptive_state(success_rate, workers)
        live_print(f"  ⚡ 本次成功率 {success_rate:.0%}，并发 {workers}，已保存自适应状态")

    return valid_results, logs_success, logs_fail, fail_counts, {"ok": source_ok, "total": source_total}

