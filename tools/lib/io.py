#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""io.py — 文件读写的小工具，全机只此一份。

atomic_write：临时文件写在同一目录 -> fsync -> chmod -> os.replace，
  断电或中途被杀只会留下垃圾临时文件，不会留下半截正文。
read_text / read_json：读不到就返回默认值，不抛 FileNotFoundError。
write_json：json.dumps(ensure_ascii=False) 的固定写法，缩进默认 1。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def atomic_write(path, text: str, mode: int = 0o600) -> None:
    """原子写文本：同目录临时文件 + os.replace，权限默认 600。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f'.{path.name}.tmp.', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_text(path, default: str = '') -> str:
    """读文本；文件不在或读不了就返回 default。"""
    try:
        return Path(path).read_text(encoding='utf-8')
    except OSError:
        return default


def read_json(path, default=None):
    """读 JSON；文件不在或解析失败就返回 default。"""
    try:
        text = Path(path).read_text(encoding='utf-8')
    except OSError:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def write_json(path, obj, indent: int = 1, mode: int = 0o600) -> None:
    """原子写 JSON，ensure_ascii=False；字节形态跟原来的 json.dumps 一致。"""
    atomic_write(path, json.dumps(obj, ensure_ascii=False, indent=indent), mode=mode)
