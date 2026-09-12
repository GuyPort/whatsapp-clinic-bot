"""Deterministic test doubles shared by conversation-state tests."""

from datetime import datetime, timedelta


class ManualClock:
    def __init__(self, value: datetime):
        self.set(value)

    def now(self) -> datetime:
        return self._value

    def set(self, value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("ManualClock requires an aware datetime")
        self._value = value

    def advance(self, delta: timedelta) -> datetime:
        if delta < timedelta(0):
            raise ValueError("ManualClock cannot move backwards")
        self._value += delta
        return self._value
