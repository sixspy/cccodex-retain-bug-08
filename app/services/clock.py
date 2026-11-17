"""统一的基准日来源：页面/接口/测试都从同一处取「今天」。

设置 RETAIN_REFERENCE_DATE=YYYY-MM-DD 可冻结基准日，便于本机复现宽限判定。
"""
from __future__ import annotations

import os
from datetime import date, datetime


DATE_FMT = "%Y-%m-%d"


def parse_day(value: str) -> date:
    return datetime.strptime(value, DATE_FMT).date()


def today() -> date:
    raw = (os.environ.get("RETAIN_REFERENCE_DATE") or "").strip()
    if raw:
        return parse_day(raw)
    return date.today()


def today_str() -> str:
    return today().isoformat()


def days_between(later: date, earlier: date) -> int:
    return (later - earlier).days
