"""HTTP 会话与并发基础设施（从 utils/config.py 拆分）。

职责边界：
- 本模块只管「怎么发请求」（线程本地 session、共享线程池、重试策略）
- utils/config.py 管「配置与门面导出」（常量、文件路径、summary、校验）

常量直接取自 config/settings.py，不依赖 utils.config，避免循环导入。
外部调用方仍应通过 utils.config 导入（该模块做兼容再导出）。
"""
import atexit
import concurrent.futures
import threading
import time

import requests

from config.settings import (
    DEFAULT_HEADERS,
    MAX_WORKERS,
    RETRY_BACKOFF,
    RETRY_MAX_ATTEMPTS,
)

__all__ = ["get_session", "get_pool", "retry_request", "fetch_url"]


def _live_print(content):
    """惰性引用 config.live_print（模块级导入会造成循环依赖）"""
    try:
        from utils.config import live_print as _lp
        _lp(content)
    except ImportError:
        print(content, flush=True)


# ===============================
# 线程本地 Session（requests.Session 非线程安全）
# ===============================
_thread_local = threading.local()


def get_session() -> requests.Session:
    """返回当前线程专属的 requests.Session（线程安全）。

    requests.Session 不是线程安全的，每个工作线程持有独立 session，
    共享底层连接池配置但避免并发读写同一 Session 对象的竞态。
    """
    session = getattr(_thread_local, 'session', None)
    if session is None:
        session = requests.Session()
        session.trust_env = False  # CI 环境 dotenv 代理干扰
        session.headers.update(DEFAULT_HEADERS)
        # 连接池大小：session 已是 per-thread，单线程并发需求有限，
        # pool_maxsize=MAX_WORKERS 会浪费内存且制造大量半开连接
        adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=8)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        _thread_local.session = session
    return session


# ===============================
# 全局共享线程池（测速 + 分辨率检测复用）
# ===============================
_SHARED_POOL = None


def get_pool() -> concurrent.futures.ThreadPoolExecutor:
    """全局共享线程池，减少线程反复创建销毁"""
    global _SHARED_POOL
    if _SHARED_POOL is None:
        _SHARED_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="m3u")
    return _SHARED_POOL


def _cleanup_pool():
    global _SHARED_POOL
    if _SHARED_POOL:
        _SHARED_POOL.shutdown(wait=False)
        _SHARED_POOL = None


atexit.register(_cleanup_pool)


# ===============================
# 带指数退避的请求重试
# ===============================
def retry_request(max_attempts: int = RETRY_MAX_ATTEMPTS, backoff: float = RETRY_BACKOFF):
    """对 requests 调用添加指数退避重试"""
    import functools

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except (requests.exceptions.Timeout,
                        requests.exceptions.ConnectionError,
                        requests.exceptions.ChunkedEncodingError) as e:
                    last_exc = e
                    if attempt < max_attempts:
                        wait = backoff * (2 ** (attempt - 1))
                        _live_print(f"  ⏳ 重试 ({attempt}/{max_attempts})，{wait:.1f}s 后重试: {e}")
                        time.sleep(wait)
            raise last_exc
        return wrapper
    return decorator


def fetch_url(url: str, timeout: int = 10) -> requests.Response:
    """带重试的 URL 获取（封装 retry_request 提升可读性）"""
    return retry_request()(lambda u: get_session().get(u, timeout=timeout))(url)
