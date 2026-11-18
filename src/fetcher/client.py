# -*- coding: utf-8 -*-
"""官方名单接口的只读 HTTP 客户端：≥2s 限流、重试、风控立即中止。

硬约束（计划文档 §6.1「合规采集」/ §9 #8-#9）：
- 仅只读 GET、无并发、相邻请求间隔 ≥ ``Config.request_interval_s``（默认 2s）；
- 失败重试 ``Config.retry_times`` 次（指数退避），**风控状态码不重试不绕行**：
  ``Config.abort_status_codes``（403/412/429/503）出现即抛 ``RiskControlAbort``；
- 官方域名为国内直连，构造 opener 时清空系统代理（本机 shell 有 socks5h 代理变量，
  urllib 不支持 socks5h，绕开可避免误报网络错误）；
- ``sleep`` / ``clock`` / ``opener`` 可注入，便于单测限流与重试逻辑而不真正联网。
"""
from __future__ import annotations

import hashlib
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

try:
    from ..config import Config, load_config
except ImportError:  # 直接以脚本方式运行时的兜底
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from src.config import Config, load_config


class FetchError(RuntimeError):
    """网络/HTTP 异常（可重试后的最终失败）。"""


class RiskControlAbort(RuntimeError):
    """命中风控状态码：立即中止，不重试、不换代理（合规要求）。"""

    def __init__(self, status_code: int, url: str = "") -> None:
        super().__init__("命中风控状态码 %s：%s" % (status_code, url))
        self.status_code = int(status_code)
        self.url = url


def _build_opener():
    """无代理 opener（官方域名国内直连；避免 socks5h 代理变量干扰）。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


class RateLimitedClient:
    """限流 + 重试 + 风控中止的只读 GET 客户端。"""

    def __init__(self, cfg: Optional[Config] = None, interval: Optional[float] = None,
                 retries: Optional[int] = None, timeout: Optional[float] = None,
                 sleep=None, clock=None, opener=None) -> None:
        self.cfg = cfg or load_config()
        self.interval = float(self.cfg.request_interval_s if interval is None else interval)
        if self.interval < 2.0:
            self.interval = 2.0  # 合规下限，不允许更密
        self.retries = int(self.cfg.retry_times if retries is None else retries)
        self.timeout = float(self.cfg.http_timeout_s if timeout is None else timeout)
        self.abort_codes = set(self.cfg.abort_status_codes)
        self.sleep = sleep or time.sleep
        self.clock = clock or time.monotonic
        self.opener = opener or _build_opener()
        self._last_request_at: Optional[float] = None
        self.request_count = 0

    # ------------------------------------------------------------ 限流
    def _wait_turn(self) -> None:
        """保证相邻请求间隔 ≥ interval（首个请求不限）。"""
        if self._last_request_at is not None:
            wait = self.interval - (self.clock() - self._last_request_at)
            if wait > 0:
                self.sleep(wait)
        self._last_request_at = self.clock()

    # ------------------------------------------------------------ 请求
    def _open_once(self, url: str, params: Optional[dict]) -> bytes:
        """单次请求；风控码立即抛 RiskControlAbort，其余错误抛 FetchError。"""
        target = url
        if params:
            query = urllib.parse.urlencode(params)
            target = "%s%s%s" % (url, "&" if "?" in url else "?", query)
        request = urllib.request.Request(target, method="GET")
        for key, value in self.cfg.official_headers().items():
            request.add_header(key, value)
        self._wait_turn()
        self.request_count += 1
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                status = getattr(response, "status", 200) or 200
                if status in self.abort_codes:
                    raise RiskControlAbort(status, target)
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in self.abort_codes:
                raise RiskControlAbort(exc.code, target) from exc
            raise FetchError("HTTP %s：%s" % (exc.code, target)) from exc
        except urllib.error.URLError as exc:
            raise FetchError("网络错误：%s（%s）" % (getattr(exc, "reason", exc), target)) from exc
        except OSError as exc:
            raise FetchError("请求失败：%s（%s）" % (exc, target)) from exc

    def get_bytes(self, url: str, params: Optional[dict] = None) -> bytes:
        """GET 原始字节；按配置重试（风控码不重试）。"""
        last_error = None
        for attempt in range(1, self.retries + 1):
            try:
                return self._open_once(url, params)
            except RiskControlAbort:
                raise
            except FetchError as exc:
                last_error = exc
                if attempt < self.retries:
                    self.sleep(min(5 * attempt, 30))
        raise FetchError("请求失败（已重试 %d 次）：%s" % (self.retries, last_error))

    def get_text(self, url: str, params: Optional[dict] = None, encoding: str = "utf-8") -> str:
        """GET 文本（默认 UTF-8；JSON 接口无需 BOM 处理）。"""
        return self.get_bytes(url, params).decode(encoding, errors="replace").lstrip("\ufeff")


def sha256_hex(raw: bytes) -> str:
    """内容 sha256（存档 meta 复核用）。"""
    return hashlib.sha256(raw).hexdigest()


def main(argv: list) -> int:
    """自检：不联网，验证限流间隔与风控中止分支（独立运行入口）。"""
    import time

    start = time.time()
    cfg = load_config(argv[0] if argv else None)
    print("index_url = %s" % cfg.official_index_url)
    print("interval  = %.1fs / retries = %d / abort_codes = %s"
          % (cfg.request_interval_s, cfg.retry_times, list(cfg.abort_status_codes)))

    fake_sleeps = []
    fake_now = [0.0]

    def fake_sleep(seconds):
        fake_sleeps.append(round(seconds, 3))
        fake_now[0] += seconds

    client = RateLimitedClient(cfg, sleep=fake_sleep, clock=lambda: fake_now[0])
    client._last_request_at = fake_now[0]
    client._wait_turn()
    client._wait_turn()
    print("限流自检：sleep 调用 = %s" % fake_sleeps)

    class _FakeResponse:
        status = 403

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def read(self):
            return b""

    class _FakeOpener:
        def open(self, request, timeout=None):
            return _FakeResponse()

    abort_client = RateLimitedClient(cfg, sleep=fake_sleep, clock=lambda: fake_now[0], opener=_FakeOpener())
    try:
        abort_client.get_text("https://example.invalid/x")
        print("风控自检：未中止（不符合预期）")
        return 1
    except RiskControlAbort as exc:
        print("风控自检：已中止 status=%s" % exc.status_code)
    print("[done] 耗时 %.2f 秒" % (time.time() - start))
    return 0


if __name__ == "__main__":
    import sys

    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
