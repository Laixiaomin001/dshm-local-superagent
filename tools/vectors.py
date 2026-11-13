#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vectors.py — 语义召回，直连 OpenAI 兼容的 embeddings 接口。

索引与查询同模型同维度，绝不混算。
数据：memory/向量.jsonl，一行一件 {file, sha1, model, dim, v}。
接口地址与 key 都从环境变量读：EMBED_BASE_URL、EMBED_API_KEY（或 EMBED_API_KEY_FILE 指向的文件）。

用法：
  vectors.py rebuild          # 全量重嵌（17 件也只要几秒）
  vectors.py query "词"       # 只看向量
  vectors.py hybrid "词"      # FTS + 向量，RRF(k=60)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import HOME, FLOW, DB, parse_flow, connect  # noqa: E402

DSH_HOME = Path(os.environ.get('DSH_HOME', str(Path.home() / '.dsh')))
KEY_FILE = Path(os.environ.get('EMBED_API_KEY_FILE', str(DSH_HOME / 'private' / 'embedding.key')))
URL = os.environ.get('EMBED_BASE_URL', 'https://api.example.com/v1/embeddings')
MODEL = os.environ.get('EMBED_MODEL', 'text-embedding-v3')
DIM = int(os.environ.get('EMBED_DIM', '1024'))
BATCH = 10           # 单批上限 10 条
TIMEOUT = 20         # 秒
VEC = HOME / 'memory' / '向量.jsonl'
K = 60


SCHEME = 'Bearer'  # HTTP 认证方案名，凭证本身从环境变量或文件读


def _read_key() -> str:
    key = os.environ.get('EMBED_API_KEY', '').strip()
    if key:
        return key
    try:
        key = KEY_FILE.read_text(encoding='utf-8').strip()
    except FileNotFoundError:
        raise RuntimeError(f'找不到 embedding key：{KEY_FILE}') from None
    except OSError as e:
        raise RuntimeError(f'读不到 embedding key：{KEY_FILE}（{e.strerror or e}）') from None
    if not key:
        raise RuntimeError(f'embedding key 是空的：{KEY_FILE}')
    return key


def _post(batch: list[str], key: str) -> list[list[float]]:
    body = json.dumps({'model': MODEL, 'input': batch, 'encoding_format': 'float'}).encode('utf-8')
    req = urllib.request.Request(URL, data=body, method='POST', headers={
        'Authorization': f'{SCHEME} {key}',
        'Content-Type': 'application/json',
    })
    try:
        raw = urllib.request.urlopen(req, timeout=TIMEOUT).read()
    except urllib.error.HTTPError as e:
        detail = ''
        try:
            detail = e.read().decode('utf-8', 'replace')[:200]
        except Exception:
            pass
        if e.code == 401:
            raise RuntimeError(f'embeddings 401：key 无效或无权限（{KEY_FILE}）{detail}') from None
        if e.code == 403:
            raise RuntimeError(f'embeddings 403：key 无该模型权限或服务未开通 {detail}') from None
        if e.code == 429:
            raise RuntimeError(f'embeddings 429：限流，稍后再试 {detail}') from None
        raise RuntimeError(f'embeddings HTTP {e.code}：{detail}') from None
    except (socket.timeout, TimeoutError):
        raise RuntimeError(f'embeddings 超时：{TIMEOUT} 秒没响应，查网络') from None
    except urllib.error.URLError as e:
        raise RuntimeError(f'embeddings 连不上：{e.reason}') from None
    except OSError as e:
        raise RuntimeError(f'embeddings 网络错误：{e}') from None

    try:
        d = json.loads(raw)
    except ValueError:
        raise RuntimeError(f'embeddings 返回不是 JSON：{raw[:120]!r}') from None
    if 'data' not in d:
        raise RuntimeError(f'embeddings 返回异常：{str(d)[:200]}')
    rows = sorted(d['data'], key=lambda x: x.get('index', 0))
    out = []
    for r in rows:
        v = r.get('embedding') or []
        if len(v) != DIM:
            raise RuntimeError(f'维度不对：{len(v)}（应为 {DIM}）')
        out.append([float(x) for x in v])
    return out


def embed_texts(texts: list[str]) -> list[list[float]]:
    """批量嵌入，10 条一批，失败重试一次；返回与输入同序。"""
    if not texts:
        return []
    key = _read_key()
    # 件已按「标题 + 正文 800 字」拼好，这里只做兜底上限，免得多吞掉正文
    clean = [t.replace('\n', ' ')[:2000] for t in texts]
    out: list[list[float]] = []
    for i in range(0, len(clean), BATCH):
        batch = clean[i:i + BATCH]
        try:
            out.extend(_post(batch, key))
        except RuntimeError as e:
            msg = str(e)
            if ' 401：' in msg or ' 403：' in msg:
                raise                      # key 的事，重试没用
            time.sleep(1.0)
            try:
                out.extend(_post(batch, key))
            except RuntimeError as e2:
                raise RuntimeError(f'{e2}（已重试 1 次）') from None
    if len(out) != len(clean):
        raise RuntimeError(f'数量不对：{len(out)}/{len(clean)}')
    return out


def cmd_rebuild(a) -> int:
    docs = []
    for f in sorted(FLOW.glob('*.md')):
        text = f.read_text(encoding='utf-8')
        p = parse_flow(text)
        body = p['body'] or text
        title = p['meta'].get('title') or f.stem
        docs.append((f.name, hashlib.sha1(text.encode()).hexdigest(), title + '\n' + body[:800]))
    vecs = embed_texts([d[2] for d in docs])
    if len(vecs) != len(docs):
        print(f'数量不对：{len(vecs)}/{len(docs)}')
        return 1
    with open(VEC, 'w', encoding='utf-8') as fh:
        for (name, sha, _), v in zip(docs, vecs):
            fh.write(json.dumps({'file': name, 'sha1': sha, 'model': MODEL, 'dim': DIM, 'v': v}) + '\n')
    print(f'{VEC}  {len(docs)} 件  {MODEL}')
    return 0


def load_vecs() -> dict[str, list[float]]:
    out = {}
    if not VEC.exists():
        return out
    for line in VEC.read_text(encoding='utf-8').splitlines():
        try:
            d = json.loads(line)
            out[d['file']] = d['v']
        except Exception:
            pass
    return out


def cos(a, b) -> float:
    s = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return s / (na * nb) if na and nb else 0.0


def fts_rank(q: str, n: int = 20) -> list[str]:
    conn = connect()
    try:
        if len(q) < 3:
            rows = conn.execute('SELECT name FROM docs WHERE title LIKE ? OR body LIKE ? LIMIT ?',
                                (f'%{q}%', f'%{q}%', n)).fetchall()
        else:
            phrase = '"' + q.replace('"', '""') + '"'
            rows = conn.execute('SELECT name FROM docs_fts WHERE docs_fts MATCH ? ORDER BY rank LIMIT ?',
                                (phrase, n)).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def fuse(q: str, limit: int) -> list[tuple[str, float]]:
    fts = fts_rank(q)
    vec = load_vecs()
    qv = embed_texts([q])[0]
    sims = sorted(((cos(qv, v), name) for name, v in vec.items()), reverse=True)[:20]
    scores: dict[str, float] = {}
    for i, name in enumerate(fts):
        scores[name] = scores.get(name, 0) + 1 / (K + i + 1)
    for i, (_, name) in enumerate(sims):
        scores[name] = scores.get(name, 0) + 1 / (K + i + 1)
    return sorted(scores.items(), key=lambda x: -x[1])[:limit]


def cmd_query(a) -> int:
    qv = embed_texts([a.text])[0]
    sims = sorted(((cos(qv, v), n) for n, v in load_vecs().items()), reverse=True)[:a.limit]
    for s, n in sims:
        print(f'{s:.3f}  {n}')
    return 0


def cmd_hybrid(a) -> int:
    for name, score in fuse(a.text, a.limit):
        meta = {}
        try:
            meta = parse_flow((FLOW / name).read_text(encoding='utf-8'))['meta']
        except Exception:
            pass
        print(f'{score:.4f}  {name}')
        print(f"  {meta.get('title', '')} · {meta.get('at', '')[:10]} · {meta.get('domain', '')}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog='vectors')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('rebuild').set_defaults(fn=cmd_rebuild)
    q = sub.add_parser('query'); q.add_argument('text'); q.add_argument('--limit', type=int, default=5); q.set_defaults(fn=cmd_query)
    h = sub.add_parser('hybrid'); h.add_argument('text'); h.add_argument('--limit', type=int, default=5); h.set_defaults(fn=cmd_hybrid)
    a = ap.parse_args()
    try:
        return a.fn(a)
    except RuntimeError as e:
        print(f'出错：{e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
