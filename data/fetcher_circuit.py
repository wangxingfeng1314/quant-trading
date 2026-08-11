"""熔断器 + 令牌桶 - 数据源保护机制

当连续失败达到阈值时，临时跳过该数据源一段时间，
防止 API 故障时无意义的重试浪费大量时间。
"""
import time
import threading
import logging

from core.config import FETCHER_CIRCUIT_THRESHOLD, FETCHER_CIRCUIT_COOLDOWN

logger = logging.getLogger(__name__)


class CircuitBreaker:
    """熔断器

    用法:
        breaker = CircuitBreaker("AKShare")
        if breaker.is_open():
            # 跳过此数据源
            return None
        try:
            data = fetch(...)
            breaker.success()
        except Exception:
            breaker.failure()
    """

    def __init__(self, name: str, threshold: int = None, cooldown: int = None):
        """
        Args:
            name: 数据源名称（用于日志）
            threshold: 连续失败次数阈值（默认用配置）
            cooldown: 熔断冷却时间秒数（默认用配置）
        """
        self.name = name
        self.threshold = threshold or FETCHER_CIRCUIT_THRESHOLD
        self.cooldown = cooldown or FETCHER_CIRCUIT_COOLDOWN
        self._failures = 0
        self._open_until = 0.0
        self._lock = threading.Lock()

    def is_open(self) -> bool:
        """检查熔断是否已打开（True=应跳过此数据源）"""
        with self._lock:
            if self._open_until > time.time():
                return True
            return False

    def success(self):
        """请求成功后调用：重置失败计数"""
        with self._lock:
            self._failures = 0

    def failure(self):
        """请求失败后调用：累计计数，达到阈值则打开熔断"""
        with self._lock:
            self._failures += 1
            if self._failures >= self.threshold:
                self._open_until = time.time() + self.cooldown
                logger.warning(
                    f"{self.name} 连续 {self._failures} 次失败，"
                    f"熔断 {self.cooldown}s"
                )

    def reset(self):
        """手动重置熔断器"""
        with self._lock:
            self._failures = 0
            self._open_until = 0.0


class TokenBucket:
    """令牌桶限频器

    用法:
        bucket = TokenBucket(rate=0.35)  # 每 0.35 秒一个令牌
        bucket.acquire()  # 等待直到拿到令牌
        data = api_call()
    """

    def __init__(self, rate: float):
        """
        Args:
            rate: 两次调用之间的最小间隔秒数
        """
        self.rate = rate
        self._last_time = 0.0
        self._lock = threading.Lock()

    def acquire(self):
        """获取一个令牌（如果间隔不足则等待）"""
        with self._lock:
            now = time.time()
            elapsed = now - self._last_time
            if elapsed < self.rate:
                time.sleep(self.rate - elapsed)
            self._last_time = time.time()
