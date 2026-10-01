"""核验服务测试用的可推进时钟。"""

from __future__ import annotations

from datetime import datetime, timedelta

from beverage_ops_foundation.clock import FixedClock


class MutableClock(FixedClock):
    """在测试中模拟跨班次、跨月的时间推进。"""

    def advance(self, delta: timedelta) -> datetime:
        self._value = self._value + delta
        return self._value

    def set_to(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("时间必须包含时区")
        self._value = value.astimezone(self._value.tzinfo)
