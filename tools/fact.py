#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fact.py — 事实层：一句「到现在还成立的话」。

动作收成 ADD-only 的三个走向（不做 UPDATE / DELETE，作废交 retire）：
  ADD        没有同键的有效卡 → 直接写新卡
  NOOP       同键有效卡的 hash 相同 → 已存在，不写
  SUPERSEDE  同键有效卡的 hash 不同 → 旧卡标失效并指回新卡，再写新卡

字段（老行缺字段按默认值读）：
  confidence     0~1，默认按 --source-type 给：extract 0.4 / tool 0.5 / human 0.8
  superseded_by  被哪张卡取代（兼容老字段「取代」）
  last_used      最后一次被 cite 或召回的时间（老行回落到「时间」「生效」）
  hash           内容指纹（主体+谓词+值），去重用；老行现算
  decay_applied  已扣过几个 30 天（内部记账，防重复 decay 反复打折）
  cite 一次 confidence +0.02（上限 1.0）；decay 按满 30 天乘 0.9 降。

存 `memory/事实.jsonl`，一件一行。
用法：
  fact.py add --subject 这台机器 --predicate 型号 --value "示例值" \\
      [--source 件名] [--verify "怎么验的"] [--source-type extract|tool|human]
  fact.py list [--all]
  fact.py check
  fact.py retire F0002 [--reason "被新卡取代"]
  fact.py cite F0001
  fact.py decay
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import MEM, iso  # noqa: E402

FACTS = MEM / '事实.jsonl'

SOURCE_CONFIDENCE = {'extract': 0.4, 'tool': 0.5, 'human': 0.8}
DEFAULT_SOURCE_TYPE = 'extract'
CITE_BOOST = 0.02
DECAY_PERIOD_DAYS = 30
DECAY_FACTOR = 0.9


def load() -> list[dict]:
    if not FACTS.exists():
        return []
    out = []
    for line in FACTS.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def save(cards: list[dict]) -> None:
    FACTS.parent.mkdir(parents=True, exist_ok=True)
    tmp = FACTS.with_suffix(f'.tmp.{os.getpid()}')
    tmp.write_text('\n'.join(json.dumps(c, ensure_ascii=False) for c in cards) + '\n', encoding='utf-8')
    os.replace(tmp, FACTS)


def next_id(cards: list[dict]) -> str:
    n = 0
    for c in cards:
        try:
            n = max(n, int(str(c.get('卡', 'F0'))[1:]))
        except Exception:
            pass
    return f'F{n + 1:04d}'


def active(cards: list[dict]) -> list[dict]:
    return [c for c in cards if not c.get('失效')]


# ---------------------------------------------------------------- 新字段的读写兼容

def fingerprint(subject: str, predicate: str, value: str) -> str:
    """内容指纹：主体+谓词+值，sha256 前 16 位。"""
    raw = '\x1f'.join([subject or '', predicate or '', value or ''])
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]


def card_hash(card: dict) -> str:
    """老行没存 hash，按内容现算。"""
    return card.get('hash') or fingerprint(card.get('主体'), card.get('谓词'), card.get('值'))


def card_confidence(card: dict) -> float:
    """老行没存 confidence，按抽取默认值 0.4 读。"""
    raw = card.get('confidence')
    try:
        return min(1.0, max(0.0, float(raw)))
    except (TypeError, ValueError):
        return SOURCE_CONFIDENCE[DEFAULT_SOURCE_TYPE]


def parse_when(text) -> datetime | None:
    if not text:
        return None
    try:
        d = datetime.fromisoformat(str(text).strip().replace('Z', '+00:00'))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.astimezone()
    return d


def card_last_used(card: dict) -> datetime | None:
    """last_used 缺失时回落到创建时间，老卡才能参与 decay。"""
    return parse_when(card.get('last_used')) or parse_when(card.get('时间')) or parse_when(card.get('生效'))


def card_superseded_by(card: dict) -> str | None:
    return card.get('superseded_by') or card.get('取代')


# ---------------------------------------------------------------- 子命令

def cmd_add(a) -> int:
    cards = load()
    digest = fingerprint(a.subject, a.predicate, a.value)
    same = [c for c in active(cards) if c.get('主体') == a.subject and c.get('谓词') == a.predicate]

    # NOOP：同键同内容，已经有这张卡了
    for c in same:
        if card_hash(c) == digest:
            print(f"NOOP {c['卡']} 已存在：{a.subject}·{a.predicate} = {a.value}")
            return 0

    today = datetime.now().astimezone().date().isoformat()
    new_id = next_id(cards)
    now = iso()

    # SUPERSEDE：同键不同内容，旧卡失效并指回新卡（仍是写新卡，不改旧卡的值）
    for c in same:
        c['失效'] = today
        c['取代'] = new_id
        c['superseded_by'] = new_id
        c['失效原因'] = '被新卡取代'

    card = {
        '卡': new_id,
        '主体': a.subject,
        '谓词': a.predicate,
        '值': a.value,
        '时间': now,
        '来源': a.source or '',
        '来源类型': a.source_type,
        '验证': a.verify or '未验证',
        'confidence': SOURCE_CONFIDENCE[a.source_type],
        'hash': digest,
        'last_used': now,
        'decay_applied': 0,
        '生效': today,
        '失效': None,
        '取代': None,
        'superseded_by': None,
        '链接': [],
        '热度': 0,
    }
    cards.append(card)
    save(cards)

    if same:
        old = '、'.join(c['卡'] for c in same)
        print(f"SUPERSEDE {old} → {new_id}  {a.subject}·{a.predicate} = {a.value}"
              f"（旧卡失效并标 superseded_by，新卡 ADD）")
    else:
        print(f"ADD {new_id}  {a.subject}·{a.predicate} = {a.value}")
    print(f"      置信 {card['confidence']:.2f}｜来源类型 {a.source_type}｜hash {digest}")
    return 0


def cmd_list(a) -> int:
    cards = load() if a.all else active(load())
    if not cards:
        print('（没有卡）')
        return 0
    for c in cards:
        mark = '' if not c.get('失效') else f"  [失效 {c['失效']}]"
        succ = card_superseded_by(c)
        if succ:
            mark += f" → {succ}"
        print(f"{c['卡']}  {c['主体']}·{c['谓词']} = {c['值']}  置信 {card_confidence(c):.2f}{mark}")
        if c.get('来源'):
            print(f"      来源：{c['来源']}｜验证：{c.get('验证', '未验证')}")
    print(f'共 {len(cards)} 张')
    return 0


def cmd_check(a) -> int:
    cards = load()
    act = active(cards)
    unverified = [c for c in act if c.get('验证') in ('', '未验证', None)]
    no_source = [c for c in act if not c.get('来源')]
    conflicts = []
    seen = {}
    for c in act:
        k = (c.get('主体'), c.get('谓词'))
        if k in seen:
            conflicts.append((k, seen[k]['卡'], c['卡']))
        seen[k] = c
    hashes = {}
    dup_hash = []
    for c in act:
        h = card_hash(c)
        if h in hashes:
            dup_hash.append((h, hashes[h], c['卡']))
        hashes[h] = c['卡']
    print(f'总卡 {len(cards)}｜有效 {len(act)}｜未验证 {len(unverified)}｜无来源 {len(no_source)}'
          f'｜同键冲突 {len(conflicts)}｜内容重复 {len(dup_hash)}')
    for k, x, y in conflicts[:5]:
        print(f'  冲突：{k[0]}·{k[1]} → {x} / {y}')
    for h, x, y in dup_hash[:5]:
        print(f'  重复：hash {h} → {x} / {y}')
    return 0


def cmd_retire(a) -> int:
    cards = load()
    for c in cards:
        if c.get('卡') == a.id:
            if c.get('失效'):
                print(f"{a.id} 已经失效")
                return 0
            c['失效'] = datetime.now().astimezone().date().isoformat()
            c['失效原因'] = a.reason or '手动作废'
            save(cards)
            print(f'RETIRE {a.id}')
            return 0
    print(f'没有这张卡：{a.id}')
    return 1


def cmd_cite(a) -> int:
    cards = load()
    for c in cards:
        if c.get('卡') == a.id:
            old = card_confidence(c)
            new = round(min(1.0, old + CITE_BOOST), 4)
            c['confidence'] = new
            c['last_used'] = iso()
            c['decay_applied'] = 0
            c['热度'] = int(c.get('热度') or 0) + 1
            save(cards)
            print(json.dumps(c, ensure_ascii=False, indent=1))
            print(f"CITE {a.id} 置信 {old:.2f} → {new:.2f}｜热度 {c['热度']}｜last_used {c['last_used']}")
            return 0
    print(f'没有这张卡：{a.id}')
    return 1


def cmd_decay(a) -> int:
    cards = load()
    now = datetime.now().astimezone()
    changed = []
    for c in cards:
        if c.get('失效'):
            continue
        last = card_last_used(c)
        if not last:
            continue
        days = (now - last.astimezone()).total_seconds() / 86400
        periods = int(days // DECAY_PERIOD_DAYS)
        applied = int(c.get('decay_applied') or 0)
        pending = periods - applied
        if pending < 1:
            continue
        old = card_confidence(c)
        new = round(max(0.0, old * (DECAY_FACTOR ** pending)), 4)
        if new >= old:
            c['decay_applied'] = periods
            continue
        c['confidence'] = new
        c['decay_applied'] = periods
        changed.append((c['卡'], c.get('主体'), c.get('谓词'), old, new, days, pending))
    if not changed:
        print('没有满 30 天未用的有效卡，未改动')
        return 0
    save(cards)
    for cid, subject, predicate, old, new, days, periods in changed:
        print(f'DECAY {cid}  {subject}·{predicate}  置信 {old:.4f} → {new:.4f}'
              f'（{days:.1f} 天未用，{periods}×0.9）')
    print(f'共降 {len(changed)} 张')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog='fact')
    sub = ap.add_subparsers(dest='cmd', required=True)
    ad = sub.add_parser('add')
    ad.add_argument('--subject', required=True)
    ad.add_argument('--predicate', required=True)
    ad.add_argument('--value', required=True)
    ad.add_argument('--source', default='')
    ad.add_argument('--verify', default='')
    ad.add_argument('--source-type', default=DEFAULT_SOURCE_TYPE,
                    choices=list(SOURCE_CONFIDENCE), help='extract 0.4 / tool 0.5 / human 0.8')
    ad.set_defaults(fn=cmd_add)
    ls = sub.add_parser('list')
    ls.add_argument('--all', action='store_true')
    ls.set_defaults(fn=cmd_list)
    sub.add_parser('check').set_defaults(fn=cmd_check)
    rt = sub.add_parser('retire')
    rt.add_argument('id')
    rt.add_argument('--reason', default='')
    rt.set_defaults(fn=cmd_retire)
    ct = sub.add_parser('cite')
    ct.add_argument('id')
    ct.set_defaults(fn=cmd_cite)
    sub.add_parser('decay').set_defaults(fn=cmd_decay)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == '__main__':
    sys.exit(main())
