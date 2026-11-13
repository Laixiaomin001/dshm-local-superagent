#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""style.py — 口吻基线：量「像不像我」。

量四样：每件字数、平均句长、短句率（≤12 字）、问句率。
基线存 `运行/口吻基线.json`，`check` 偏三成就报红。

用法：
  style.py baseline    # 重算并写基线
  style.py check       # 对比，偏了 exit 1
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import HOME, FLOW, parse_flow  # noqa: E402

BASE = HOME / '运行' / '口吻基线.json'


def sentences(text: str) -> list[str]:
    return [s for s in re.split(r'[。！？\n]+', text) if s.strip()]


def measure() -> dict:
    chars, sents, short, q = 0, 0, 0, 0
    n = 0
    for f in sorted(FLOW.glob('*.md')):
        p = parse_flow(f.read_text(encoding='utf-8'))
        body = p['body']
        heart = body.split('## 我心里', 1)[-1].strip() if '## 我心里' in body else body.split('## 判断', 1)[-1].strip() if '## 判断' in body else body
        if not heart:
            continue
        n += 1
        chars += len(heart)
        ss = sentences(heart)
        sents += len(ss)
        short += sum(1 for s in ss if len(s) <= 12)
        q += heart.count('？') + heart.count('?')
    if not n or not sents:
        return {'n': n, 'chars_avg': 0, 'sent_avg': 0, 'short_ratio': 0, 'question_ratio': 0}
    return {
        'n': n,
        'chars_avg': round(chars / n, 1),
        'sent_avg': round(chars / sents, 1),
        'short_ratio': round(short / sents, 3),
        'question_ratio': round(q / sents, 3),
    }


def save(m: dict) -> None:
    BASE.parent.mkdir(parents=True, exist_ok=True)
    m['at'] = datetime.now().astimezone().isoformat(timespec='seconds')
    BASE.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding='utf-8')


def main() -> int:
    ap = argparse.ArgumentParser(prog='style')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('baseline').set_defaults(fn='baseline')
    sub.add_parser('check').set_defaults(fn='check')
    a = ap.parse_args()
    cur = measure()
    if a.fn == 'baseline' or not BASE.exists():
        save(cur)
        print(f'基线已写：{json.dumps(cur, ensure_ascii=False)}')
        return 0
    old = json.loads(BASE.read_text(encoding='utf-8'))
    bad = []
    for k in ('chars_avg', 'sent_avg', 'short_ratio', 'question_ratio'):
        o = float(old.get(k) or 0)
        c = float(cur.get(k) or 0)
        if o > 0 and abs(c - o) / o > 0.3:
            bad.append(f'{k} {o}→{c}')
    print(f"样本 {cur['n']}｜" + '，'.join(f'{k}={cur[k]}' for k in ('chars_avg', 'sent_avg', 'short_ratio')))
    if bad:
        print('偏了：' + '；'.join(bad))
        return 1
    print('口吻在基线上')
    return 0


if __name__ == '__main__':
    sys.exit(main())
