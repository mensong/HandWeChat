# -*- coding: utf-8 -*-
"""发送限速：令牌桶 + 随机抖动。"""

from __future__ import annotations

import random
import threading
import time


class TokenBucketRateLimiter:
    """按分钟限制事件频率，并加入短随机延时。"""

    def __init__(
        self,
        max_per_minute: int = 20,
        *,
        jitter_range: tuple[float, float] = (0.4, 1.2),
    ) -> None:
        self._max_per_minute = max(1, int(max_per_minute))
        self._capacity = float(self._max_per_minute)
        self._tokens = self._capacity
        self._refill_rate = self._capacity / 60.0
        self._jitter_range = jitter_range
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    @property
    def max_per_minute(self) -> int:
        return self._max_per_minute

    def set_max_per_minute(self, value: int) -> None:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(
                self._capacity,
                self._tokens + elapsed * self._refill_rate,
            )
            self._max_per_minute = max(1, int(value))
            self._capacity = float(self._max_per_minute)
            self._refill_rate = self._capacity / 60.0
            self._tokens = min(self._capacity, self._tokens)
            self._last_refill = now

    def wait_one(self) -> float:
        """等待一次发送配额，返回实际等待秒数。"""

        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(
                self._capacity,
                self._tokens + elapsed * self._refill_rate,
            )
            self._last_refill = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                jitter = random.uniform(*self._jitter_range)
                time.sleep(jitter)
                return jitter

            missing = 1.0 - self._tokens
            sleep_time = missing / self._refill_rate
            self._tokens = 0.0

        time.sleep(sleep_time)
        with self._lock:
            self._tokens = max(0.0, self._tokens - 1.0)
            self._last_refill = time.monotonic()
        jitter = random.uniform(*self._jitter_range)
        time.sleep(jitter)
        return sleep_time + jitter
