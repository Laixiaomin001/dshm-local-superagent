#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ledger.py — 派单台账：派了什么、回来没有、验没验、遗留什么。

台账按天分文件：运行/派单台账/YYYYMMDD.jsonl，一天一个，一行一条 JSON：

  {"type":"dispatch","at":"...","tool":"subagent","summary":"...","session":"..."}
  {"type":"result","at":"...","ok":true,"verified":true,"summary":"...","left":""}

用法：
  ledger.py dispatch --tool subagent --summary "一句话" [--session ID]
  ledger.py result --summary "一句话" --ok true --verified true [--left "遗留"]
  ledger.py list [--day 20261111] [--pending]
  ledger.py summary [--day 20261111]

dispatch 和 result 按出现顺序配对，不靠 id：第 N 条 result 配第 N 条还没配上的 dispatch。
一天一个文件；不给 --day 就是今天。被 dream 挪进 归档/ 的日子照样能 --day 查。
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
from datetime import datetime
from pathlib import Path

HOME = Path(os.environ.get('CORE_HOME', str(Path.home() / '.dsh' / 'agent-core')))
LEDGER_DIR = HOME / '运行' / '派单台账'
ARCHIVE_DIR = LEDGER_DIR / '归档'
TYPES = ('dispatch', 'result')


def day_stamp(d: datetime | None = None) -> str:
    return (d or datetime.now()).strftime('%Y%m%d')


def ledger_path(day: str | None = None) -> Path:
    """按天取文件：运行/派单台账/YYYYMMDD.jsonl。目录不存在就建。"""
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    return LEDGER_DIR / ((day or day_stamp()) + '.jsonl')


def read_path(day: str | None = None) -> Path:
    """读的时候先看当天，没有再看归档，归档也是同一种文件名。"""
    p = LEDGER_DIR / ((day or day_stamp()) + '.jsonl')
    if p.exists():
        return p
    alt = ARCHIVE_DIR / p.name
    return alt if alt.exists() else p


def iso() -> str:
    return datetime.now().astimezone().isoformat(timespec='seconds')


def as_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    return str(v).strip().lower() in ('1', 'true', 'yes', 'y', 'on', '是', '有')


def parse_bool(v: str) -> bool:
    s = str(v).strip().lower()
    if s in ('1', 'true', 'yes', 'y', 'on'):
        return True
    if s in ('0', 'false', 'no', 'n', 'off'):
        return False
    raise argparse.ArgumentTypeError('要 true 或 false，收到 ' + str(v))


def append(rec: dict) -> None:
    """一行一次写，O_APPEND + 文件锁，插件和脚本同时写不会撕行。"""
    path = ledger_path(day_of(rec) or None)
    line = json.dumps(rec, ensure_ascii=False, separators=(',', ':'))
    with path.open('a', encoding='utf-8') as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(line + '\n')
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def load(day: str | None = None) -> tuple[list[dict], int]:
    path = read_path(day)
    if not path.exists():
        return [], 0
    recs: list[dict] = []
    bad = 0
    for raw in path.read_text(encoding='utf-8').splitlines():
        s = raw.strip()
        if not s:
            continue
        try:
            rec = json.loads(s)
            if not isinstance(rec, dict) or rec.get('type') not in TYPES:
                raise ValueError('not a ledger record')
            recs.append(rec)
        except (ValueError, TypeError):
            bad += 1
    return recs, bad


def pair(recs: list[dict]) -> tuple[dict[int, int], list[int]]:
    """第 N 条 result 配第 N 条还没配上的 dispatch。返回双向映射和没配上的 dispatch 下标。"""
    unpaired: list[int] = []
    paired: dict[int, int] = {}
    for i, rec in enumerate(recs):
        if rec.get('type') == 'dispatch':
            unpaired.append(i)
        elif unpaired:
            d = unpaired.pop(0)
            paired[i] = d
            paired[d] = i
    return paired, unpaired


def norm_day(raw: str | None) -> str | None:
    if not raw:
        return None
    digits = ''.join(ch for ch in str(raw) if ch.isdigit())
    if len(digits) != 8:
        raise SystemExit('--day 要 8 位日期，比如 20261111')
    return digits


def day_of(rec: dict) -> str:
    s = str(rec.get('at') or '')
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00')).astimezone().strftime('%Y%m%d')
    except (ValueError, TypeError):
        digits = ''.join(ch for ch in s if ch.isdigit())[:8]
        return digits if len(digits) == 8 else ''


def stamp(rec: dict) -> str:
    s = str(rec.get('at') or '')
    return s[:19].replace('T', ' ') or '-'


def cmd_dispatch(args: argparse.Namespace) -> int:
    rec = {
        'type': 'dispatch',
        'at': iso(),
        'tool': args.tool,
        'summary': args.summary.strip(),
        'session': args.session or '',
    }
    append(rec)
    print(f'已记 dispatch：{rec["tool"]}  {rec["summary"]}  [session {rec["session"] or "-"}]')
    return 0


def cmd_result(args: argparse.Namespace) -> int:
    rec = {
        'type': 'result',
        'at': iso(),
        'ok': args.ok,
        'verified': args.verified,
        'summary': args.summary.strip(),
        'left': (args.left or '').strip(),
    }
    append(rec)
    print(f'已记 result：{"成" if rec["ok"] else "败"}  {"已验" if rec["verified"] else "未验"}  '
          f'{rec["summary"]}  遗留：{rec["left"] or "无"}')
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    want = norm_day(args.day) or day_stamp()
    recs, bad = load(want)
    if not recs:
        print('台账还是空的：' + str(read_path(want)))
        return 0
    paired, unpaired = pair(recs)
    pending = set(unpaired)

    seq: dict[int, int] = {}
    n = 0
    for i, rec in enumerate(recs):
        if rec.get('type') == 'dispatch':
            n += 1
            seq[i] = n

    shots = 0
    for i, rec in enumerate(recs):
        if args.pending:
            if rec.get('type') != 'dispatch' or i not in pending:
                continue
            print(f'{stamp(rec)}  派#{seq[i]}  {rec.get("tool") or "?":<13} '
                  f'{rec.get("summary") or ""}   [session {rec.get("session") or "-"}]')
            shots += 1
            continue

        if rec.get('type') == 'dispatch':
            print(f'{stamp(rec)}  派#{seq[i]}  {rec.get("tool") or "?":<13} '
                  f'{rec.get("summary") or ""}   [session {rec.get("session") or "-"}]')
        else:
            d = paired.get(i)
            tag = f'回#{seq[d]}' if d is not None and d in seq else '回- '
            ok = '成' if as_bool(rec.get('ok')) else '败'
            ver = '已验' if as_bool(rec.get('verified')) else '未验'
            left = str(rec.get('left') or '').strip()
            print(f'{stamp(rec)}  {tag}  {ok} {ver}  {rec.get("summary") or ""}   '
                  f'遗留：{left or "无"}')
        shots += 1

    if bad:
        print(f'（跳过 {bad} 行不是台账格式的坏行）')
    if args.pending:
        print(f'\n待回：{shots} 条派单还没有结果' if shots else '\n没有待回的派单')
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    want = norm_day(args.day) or day_stamp()
    recs, bad = load(want)
    paired, unpaired = pair(recs)

    dispatched = [r for r in recs if r.get('type') == 'dispatch']
    results = [r for r in recs if r.get('type') == 'result']
    verified = sum(1 for r in results if as_bool(r.get('verified')))
    left = sum(1 for r in results if str(r.get('left') or '').strip())
    pending = sum(1 for i in unpaired if recs[i].get('type') == 'dispatch')

    print(f'日期  {want}')
    print(f'派单  {len(dispatched)}')
    print(f'回来  {len(results)}')
    print(f'验过  {verified}')
    print(f'遗留  {left}')
    print(f'待回  {pending}')
    if bad:
        print(f'（跳过 {bad} 行坏行）')
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog='ledger.py', description='派单台账')
    sub = p.add_subparsers(dest='cmd', required=True)

    d = sub.add_parser('dispatch', help='记一条派单')
    d.add_argument('--tool', required=True, help='subagent / subagent_fork')
    d.add_argument('--summary', required=True, help='一句话说派了什么')
    d.add_argument('--session', default='', help='派单的会话号')
    d.set_defaults(func=cmd_dispatch)

    r = sub.add_parser('result', help='记一条结果')
    r.add_argument('--summary', required=True, help='一句话说结果')
    r.add_argument('--ok', required=True, type=parse_bool, help='true / false')
    r.add_argument('--verified', required=True, type=parse_bool, help='验收过没有，不能省')
    r.add_argument('--left', default='', help='遗留，没有就空着')
    r.set_defaults(func=cmd_result)

    ls = sub.add_parser('list', help='列台账')
    ls.add_argument('--day', default=None, help='20261111')
    ls.add_argument('--pending', action='store_true', help='只看没有结果的派单')
    ls.set_defaults(func=cmd_list)

    s = sub.add_parser('summary', help='当天汇总')
    s.add_argument('--day', default=None, help='20261111，默认今天')
    s.set_defaults(func=cmd_summary)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
