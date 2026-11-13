#!/usr/bin/env python3
"""账本检索评测：量 FTS / 向量 / 混合三种方式在 30 条查询上的 recall@5 与 MRR。

用法：
  eval.py                 # 三种方式各跑一遍
  eval.py --mode fts      # 只看 FTS（core.py search）
  eval.py --mode vec      # 只看向量（vectors.py query）
  eval.py --mode hybrid   # 只看混合（vectors.py hybrid，RRF k=60）
  eval.py --json          # 机器可读输出

只读：不动 core.py / vectors.py / 账本，只读查询集和检索输出。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

BIN = Path(__file__).resolve().parent
# 本目录有 queue.py，会挡住标准库 queue（ThreadPoolExecutor 依赖它），先从 sys.path 摘掉
sys.path[:] = [p for p in sys.path if Path(p or '.').resolve() != BIN]
from concurrent.futures import ThreadPoolExecutor  # noqa: E402
ROOT = BIN.parent
QUERIES = ROOT / '运行' / '评测' / '查询集.json'
K = 5
MODES = ('fts', 'vec', 'hybrid')
MODE_CN = {'fts': 'FTS', 'vec': '向量', 'hybrid': '混合'}

# 账本件名形如 20261111_1550_续点-今天做到哪
NAME_RE = re.compile(r'^(\d{8}_\d{4}_\S+)\.md$')


def _cmd(mode: str, q: str) -> list[str]:
    if mode == 'fts':
        return [sys.executable, str(BIN / 'core.py'), 'search', q, '--limit', str(K)]
    if mode == 'vec':
        return [sys.executable, str(BIN / 'vectors.py'), 'query', q, '--limit', str(K)]
    return [sys.executable, str(BIN / 'vectors.py'), 'hybrid', q, '--limit', str(K)]


def _names(out: str) -> list[str]:
    """从三种 CLI 的文本输出里按出现顺序抽出件名。"""
    got: list[str] = []
    for line in out.splitlines():
        for tok in line.split():
            m = NAME_RE.match(tok)
            if m and m.group(1) not in got:
                got.append(m.group(1))
    return got


def _run(mode: str, q: str) -> list[str]:
    p = subprocess.run(_cmd(mode, q), capture_output=True, text=True,
                       encoding='utf-8', errors='replace', cwd=str(BIN), timeout=180)
    return _names((p.stdout or '') + (p.stderr or ''))


def _norm(name: str) -> str:
    return name[:-3] if name.endswith('.md') else name


def _width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in s)


def _pad(s: str, n: int) -> str:
    return s + ' ' * max(0, n - _width(s))


def evaluate(mode: str, queries: list[dict], jobs: int = 1) -> dict:
    # 向量/混合现在各自独立请求、不再共用临时文件；
    # 仍串行跑，免得并发把 key 打成 429。FTS 全本地，可以并行。
    workers = max(1, jobs) if mode == 'fts' else 1
    with ThreadPoolExecutor(max_workers=workers) as ex:
        got_all = list(ex.map(lambda it: _run(mode, it['q']), queries))

    rows = []
    hits = 0
    mrr = 0.0
    for it, got in zip(queries, got_all):
        expect = {_norm(e) for e in it['expect']}
        rank = next((i + 1 for i, n in enumerate(got) if n in expect), 0)
        if rank:
            hits += 1
            mrr += 1.0 / rank
        rows.append({
            'kind': it.get('kind', ''),
            'q': it['q'],
            'expect': it['expect'],
            'top5': got,
            'rank': rank,
            'hit': bool(rank),
            'first_hit': got[rank - 1] if rank else '',
        })
    n = len(rows) or 1
    return {'mode': mode, 'n': len(rows), 'hits': hits,
            'recall': hits / n, 'mrr': mrr / n, 'rows': rows}


def print_table(res: dict) -> None:
    print(f"\n=== {MODE_CN[res['mode']]}（top{K}）" + '=' * 40)
    print(_pad('#', 4) + _pad('类型', 6) + _pad('查询', 34) + _pad('命中', 6)
          + _pad('位次', 6) + '首中件名')
    print('-' * 104)
    for i, r in enumerate(res['rows'], 1):
        print(_pad(str(i), 4) + _pad(r['kind'], 6) + _pad(r['q'], 34)
              + _pad('✓' if r['hit'] else '✗', 6)
              + _pad(str(r['rank']) if r['hit'] else '-', 6)
              + (r['first_hit'] if r['hit'] else '——'))


def main() -> int:
    ap = argparse.ArgumentParser(description='账本检索 recall@5 评测')
    ap.add_argument('--mode', choices=MODES + ('all',), default='all')
    ap.add_argument('--json', action='store_true', help='输出 JSON')
    ap.add_argument('--queries', default=str(QUERIES), help='查询集路径')
    ap.add_argument('--jobs', type=int, default=8,
                    help='FTS 并行数；向量/混合强制串行（SSH 共用临时文件）')
    a = ap.parse_args()

    qpath = Path(a.queries)
    if not qpath.exists():
        print(f'[error] 找不到查询集：{qpath}', file=sys.stderr)
        return 2
    queries = json.loads(qpath.read_text(encoding='utf-8'))
    if not queries:
        print('[error] 查询集为空', file=sys.stderr)
        return 2

    modes = MODES if a.mode == 'all' else (a.mode,)
    results = [evaluate(m, queries, a.jobs) for m in modes]

    if a.json:
        print(json.dumps({
            'k': K,
            'queries': len(queries),
            'results': {r['mode']: {
                'recall@5': round(r['recall'], 4),
                'mrr': round(r['mrr'], 4),
                'hits': r['hits'],
                'n': r['n'],
                'misses': [row['q'] for row in r['rows'] if not row['hit']],
                'rows': r['rows'],
            } for r in results},
        }, ensure_ascii=False, indent=2))
        return 0

    for r in results:
        print_table(r)

    print('\n' + '=' * 60)
    for r in results:
        print(f"{_pad(MODE_CN[r['mode']], 6)}recall@5 = {r['recall']:.2f} "
              f"({r['hits']}/{r['n']})   MRR = {r['mrr']:.3f}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
