#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""guard.py — 工具边界守卫（第二道墙，软 Enforcement）。

用途：在真正执行写/删/危险命令之前，先问一句「这条路径、这条命令，允许吗」。
它不替代宿主的 hook，只保证：**没有经过它的改动，就是违规改动**（由宪章约束）。

用法：
  guard.py path "$DSH_HOME/agent-core/memory/x.md"
  guard.py cmd "rm -rf $DSH_HOME/agent-core"
  guard.py cmd "python3 bin/core.py store --title x"

退出码：0 允许，1 拒绝，2 用法错误。每次判定都记进 运行/guard.jsonl。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

DSH_HOME = Path(os.environ.get('DSH_HOME', str(Path.home() / '.dsh')))
HOME = Path(os.environ.get('CORE_HOME', str(DSH_HOME / 'agent-core')))
WORKSPACE = Path(os.environ.get('DSH_WORKSPACE', str(Path.home() / 'workspace')))
EXPORT = Path(os.environ.get('CORE_EXPORT', str(DSH_HOME / 'exports')))
POLICY = HOME / 'policy.json'
LOG = HOME / '运行' / 'guard.jsonl'

# 默认策略里的路径按环境变量展开；resolve 过再比，符号链接也能对上
def _r(p) -> str:
    try:
        return str(Path(p).expanduser().resolve())
    except Exception:
        return str(p)


DEFAULT_POLICY = {
    'protect': [
        _r(DSH_HOME / '.credentials.yaml'),
        _r(DSH_HOME / 'profiles'),
        _r(HOME / 'memory' / '索引.db'),
        _r(DSH_HOME / 'sessions'),
    ],
    'allow_write_roots': [
        _r(HOME),
        _r(WORKSPACE),
        _r(EXPORT),
        '/tmp',
    ],
    'deny_cmd': [
        r'\brm\s+-[a-zA-Z]*r[a-zA-Z]*f?\s+/\s*$',
        r'\bmkfs\b',
        r'\bdd\s+if=',
        r'>\s*/dev/(sd|block)',
        r'\bshutdown\b',
        r'\breboot\b',
        r':\(\)\s*\{',
        r'chmod\s+-R\s+777\s+/',
        r'\bcurl\b[^\n]*\|\s*(ba|z)?sh\b',
    ],
    'destructive': r'\b(rm|mv|chmod|chown|truncate|shred)\b|>\s*\S|tee\s+\S',
    # 这些不是「文件」，是账本本身：任何破坏性动作都拒，哪怕父目录在允许根里
    'protect_fragments': [
        _r(HOME / 'memory'),
        _r(HOME / 'charter.md'),
        _r(HOME / 'identity.md'),
        _r(HOME / 'health.json'),
    ],
}


def load_policy() -> dict:
    p = dict(DEFAULT_POLICY)
    try:
        p.update(json.loads(POLICY.read_text(encoding='utf-8')))
    except Exception:
        pass
    return p


def audit(tool: str, target: str, ok: bool, reason: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        rec = {'at': datetime.now().astimezone().isoformat(timespec='seconds'),
               'tool': tool, 'target': target[:500], 'ok': ok, 'reason': reason}
        with open(LOG, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except Exception:
        pass


def check_path(raw: str, policy: dict) -> tuple[bool, str]:
    try:
        p = Path(raw).expanduser().resolve()
    except Exception as e:
        return False, f'路径无法解析：{e}'
    s = str(p)
    for x in policy['protect']:
        if s == x or s.startswith(x.rstrip('/') + '/'):
            return False, f'受保护路径：{x}'
    for x in policy['allow_write_roots']:
        if s == x or s.startswith(x.rstrip('/') + '/'):
            return True, f'允许写入：{x}'
    return False, '不在允许写入的根目录内（宪章第 4 条）'


def check_cmd(cmd: str, policy: dict) -> tuple[bool, str]:
    c = cmd.strip()
    if not c:
        return False, '空命令'
    for pat in policy['deny_cmd']:
        if re.search(pat, c):
            return False, f'命中禁止规则：{pat}'
    # 受保护路径 + 破坏性动作 = 拒绝
    if re.search(policy['destructive'], c):
        for x in policy['protect']:
            if x in c:
                return False, f'对受保护路径执行破坏性操作：{x}'
        for x in policy.get('protect_fragments', []):
            if x in c:
                return False, f'对账本本体执行破坏性操作：{x}'
    return True, '允许执行'


def main() -> int:
    ap = argparse.ArgumentParser(prog='guard')
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('path')
    p.add_argument('path')
    c = sub.add_parser('cmd')
    c.add_argument('command')
    a = ap.parse_args()
    policy = load_policy()
    if a.cmd == 'path':
        ok, reason = check_path(a.path, policy)
        target = a.path
    else:
        ok, reason = check_cmd(a.command, policy)
        target = a.command
    audit(a.cmd, target, ok, reason)
    print(('✓ ' if ok else '✗ ') + reason)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
