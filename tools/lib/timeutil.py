#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""timeutil.py — 时间格式，全机只此一份。

三种写法各管一件事，别混：
  now()        当前时间，带本机时区的 datetime
  local_iso()  给机器读的本地时间串，精确到秒，带 +08:00
  parse_iso()  读回时间串，统一成带时区的 datetime（不带时区的按本机时区算）

day_stamp() 是账本文件名和按天分桶用的 YYYYMMDD。
"""
from __future__ import annotations

from datetime import datetime


def now() -> datetime:
    """当前时间，带本机时区。"""
    return datetime.now().astimezone()


def local_iso(dt: datetime | None = None) -> str:
    """datetime -> 本地时间串，精确到秒。不传 dt 就是现在。"""
    if dt is None:
        return now().isoformat(timespec='seconds')
    if isinstance(dt, str):
        dt = parse_iso(dt)
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone().isoformat(timespec='seconds')


def iso(dt: datetime | None = None) -> str:
    """local_iso 的旧名字，给还没迁完的脚本用。"""
    return local_iso(dt)


def now_iso() -> str:
    """当前时间的本地时间串。"""
    return local_iso(now())


def parse_iso(s: str | datetime) -> datetime:
    """时间串 -> 带时区的 datetime。

    认 ISO 8601；结尾 Z 当 UTC；不带时区的按本机时区算。
    解析不了就抛 ValueError，不吞错。
    """
    if isinstance(s, datetime):
        dt = s
    else:
        text = str(s).strip()
        if text.endswith(('Z', 'z')):
            text = text[:-1] + '+00:00'
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


def day_stamp(dt: datetime | None = None) -> str:
    """YYYYMMDD，按本机时区。"""
    return (dt or now()).strftime('%Y%m%d')
