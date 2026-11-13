#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sentinel.py — 从会话原文里补记「待记」，不依赖模型自觉。

会话存在 $DSH_HOME/sessions/<workspace>/<session>/session.v4.jsonl.zstd。
这里解压、按 turn 聚合，只收完整回合（见到 turn/end 才落），
偏移记在 运行/sentinel.json，重复跑不会重复记。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HOME = Path(os.environ.get('CORE_HOME', str(Path.home() / '.dsh' / 'agent-core')))
SESSIONS = Path(os.environ.get('DSH_SESSIONS', str(Path.home() / '.dsh' / 'sessions')))
PENDING = HOME / 'memory' / '待记'
STATE = HOME / '运行' / 'sentinel.json'
UNZSTD = Path(__file__).with_name('unzstd.mjs')
FILE_RE = re.compile(r'[\w\-/.（）()\u4e00-\u9fa5]+\.(?:md|js|py|json|jsonl|yml|yaml|sh|mjs)\b')
MAX_BYTES = 512 * 1024


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding='utf-8'))
    except Exception:
        return {}


def text_of(content) -> str:
    if not isinstance(content, list):
        return ''
    return ' '.join(c.get('text', '') for c in content
                    if isinstance(c, dict) and c.get('type') == 'text').strip()


def append(rec: dict) -> None:
    PENDING.mkdir(parents=True, exist_ok=True)
    d = datetime.fromtimestamp(rec['t'] / 1000).strftime('%Y%m%d')
    path = PENDING / f'{d}.jsonl'
    for i in range(2, 10):
        if not path.exists() or path.stat().st_size < MAX_BYTES:
            break
        path = PENDING / f'{d}-{i}.jsonl'
    with open(path, 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + '\n')


def scan(path: Path, rel: str, state: dict) -> int:
    try:
        raw = subprocess.run(['node', str(UNZSTD), str(path)],
                             capture_output=True, timeout=60)
        if raw.returncode != 0:
            return 0
        lines = raw.stdout.decode('utf-8', 'ignore').splitlines()
    except Exception:
        return 0
    if not lines:
        return 0
    try:
        header = json.loads(lines[0])
    except Exception:
        return 0
    if header.get('type') != 'session' or int(header.get('delegationDepth') or 0) > 0:
        return 0
    offset = int(state.get(rel, {}).get('offset', 0))
    if offset >= len(lines):
        return 0
    buf = None
    last_complete = offset
    written = 0
    for i in range(offset, len(lines)):
        try:
            e = json.loads(lines[i])
        except Exception:
            continue
        t = e.get('type')
        if t == 'turn/start':
            buf = {'turn': (e.get('data') or {}).get('turn', 0), 'user': [],
                   'assistant': '', 'tools': [], 'files': [], 't': e.get('time') or 0}
        elif buf is None:
            continue
        elif t == 'user/message':
            u = text_of((e.get('data') or {}).get('content'))
            if u:
                buf['user'].append(u[:200])
        elif t == 'assistant/message':
            a = text_of(((e.get('data') or {}).get('message') or {}).get('content'))
            if a:
                buf['assistant'] = a
        elif t == 'tool/call':
            d = e.get('data') or {}
            if d.get('name'):
                buf['tools'].append(d['name'])
                for m in FILE_RE.findall(str(d.get('arguments', ''))):
                    if m not in buf['files']:
                        buf['files'].append(m)
        elif t == 'turn/end':
            user_text = ''.join(buf['user'])
            if buf and (buf['tools'] or user_text):
                append({'t': buf['t'] or int(datetime.now().timestamp() * 1000),
                        'at': datetime.fromtimestamp((buf['t'] or 0) / 1000).astimezone().isoformat(timespec='seconds'),
                        'session': header.get('id', ''),
                        'turn': buf['turn'],
                        'user': buf['user'][:3],
                        'assistant_tail': buf['assistant'][-200:],
                        'tools': len(buf['tools']),
                        'tool_names': buf['tools'][:20],
                        'files': buf['files'][:10],
                        'short': (not buf['tools']) and len(user_text) < 20,
                        'from': 'sentinel'})
                written += 1
                last_complete = i + 1
            buf = None
    state[rel] = {'offset': last_complete}
    return written


def main() -> int:
    state = load_state()
    total = 0
    sessions = 0
    for p in sorted(SESSIONS.glob('*/session-*/session.v4.jsonl.zstd')):
        rel = str(p.relative_to(SESSIONS))
        prev = state.get(rel, {}).get('offset')
        n = scan(p, rel, state)
        if n:
            total += n
        sessions += 1
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding='utf-8')
    print(f'扫描 {sessions} 个会话，补记 {total} 条')
    return 0


if __name__ == '__main__':
    sys.exit(main())
