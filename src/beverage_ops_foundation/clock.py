"""提供可替换的 UTC 时钟。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    """定义服务所需的最小时钟接口。"""

    def now(self) -> datetime:
        """返回带时区的当前时间。"""


class SystemClock:
    """使用系统 UTC 时间。"""

    def now(self) -> datetime:
        """返回当前 UTC 时间。"""

        return datetime.now(timezone.utc)


class FixedClock:
    """为测试与离线验收提供固定时间。"""

    def __init__(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("固定时间必须包含时区")
        self._value = value.astimezone(timezone.utc)

    def now(self) -> datetime:
        """返回固定的 UTC 时间。"""

        return self._value


class ManualClock:
    """提供可手动推进的时间，用于跨班次与跨月窗口测试。"""

    def __init__(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("初始时间必须包含时区")
        self._value = value.astimezone(timezone.utc)

    def now(self) -> datetime:
        """返回当前设置的 UTC 时间。"""

        return self._value

    def set(self, value: datetime) -> None:
        """把时钟设置到新的时刻。"""

        if value.tzinfo is None:
            raise ValueError("目标时间必须包含时区")
        self._value = value.astimezone(timezone.utc)

    def advance(self, **delta) -> datetime:
        """按 timedelta 关键字参数推进时钟并返回新时刻。"""

        from datetime import timedelta

        self._value = self._value + timedelta(**delta)
        return self._value
