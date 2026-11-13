#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""review.py — 日结：把今天落下的账、料、失败拼成一张自评草稿。

只出草稿，不写结论。三句话留给人填：学到什么、哪步手搓了、哪句话被纠正。
产物：`运行/自评/YYYYMMDD.md`
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import HOME, FLOW, PENDING, DIGEST, read_health  # noqa: E402

OUT = HOME / '运行' / '自评'


def main() -> int:
    today = datetime.now().strftime('%Y%m%d')
    OUT.mkdir(parents=True, exist_ok=True)
    flow = [f.name for f in FLOW.glob(f'{today}_*.md')]
    pending = 0
    p = PENDING / f'{today}.jsonl'
    if p.exists():
        pending = len(p.read_text(encoding='utf-8').splitlines())
    stuck = list((HOME / 'queue' / 'stuck').glob('*.json'))
    digest = DIGEST / f'{today}.md'
    h = read_health() or {}

    lines = [
        f'# {today[:4]}-{today[4:6]}-{today[6:]} 自评',
        '',
        f"- 落账：{len(flow)} 件",
        f"- 待记：{pending} 行",
        f"- 卡住工单：{len(stuck)} 张",
        f"- 日报：{'有' if digest.exists() else '没有'}",
        f"- 健康：{'✓' if h.get('ok') else '✗ 或无'}（{h.get('at', '—')[:16]}）",
        '',
        '## 今天落下的',
    ]
    lines += [f'- {n}' for n in flow[-10:]] or ['- （无）']
    lines += [
        '',
        '## 三句',
        '',
        '学到什么：',
        '',
        '哪步手搓了：',
        '',
        '哪句话被纠正：',
        '',
    ]
    out = OUT / f'{today}.md'
    out.write_text('\n'.join(lines), encoding='utf-8')
    print(f'{out}｜账 {len(flow)}｜料 {pending}｜卡 {len(stuck)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
