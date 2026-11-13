#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_recall.py — 常驻页生成回归测试。

在独立的临时 CORE_HOME 里造最小账本，调 recall.py build，检查不崩、结构完整。
  python3 tests/test_recall.py

定位 recall.py：默认取本文件同级的 ../bin/ 或 ../tools/，可用 RECALL_PY 覆盖。
全部通过时打印 PASS n/n 并退出 0；失败打印 FAIL 名称: 原因，退出 1。
"""
import json
import os
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 发布仓里脚本在 tools/；装到引擎后在同级的 bin/。两处都认，也能用 RECALL_PY 直接指。
_RECALL = HERE.parent / 'bin' / 'recall.py'
if not _RECALL.exists():
    _RECALL = HERE.parent / 'tools' / 'recall.py'
RECALL = Path(os.environ.get('RECALL_PY') or _RECALL)
TIMEOUT = 60

FLOW_NAME = '20261113-2100-续点-回归测试.md'
FLOW_TEXT = """---
title: 续点·回归测试
---

# 续点：回归测试

## 他说了啥

这一行应该出现在常驻页的续点正文里。

## 下一轮

接着做。
"""
STATE = {
    'count': 1,
    'resume': [{'title': '续点：回归测试', 'at': '2026-11-13T21:00:00+08:00'}],
}
SECTIONS = ['## 当前状态', '## 续点正文', '## 未结的事', '## 关于用户', '## 最近五条教训']
RESUME_MARK = '这一行应该出现在常驻页的续点正文里。'


def make_ledger(core):
    """最小账本：流水一份、状态.json 一份、运行/待审 空着。"""
    flow = core / 'memory' / '流水'
    flow.mkdir(parents=True)
    (flow / FLOW_NAME).write_text(FLOW_TEXT, encoding='utf-8')
    (core / 'memory' / '状态.json').write_text(
        json.dumps(STATE, ensure_ascii=False, indent=1), encoding='utf-8')
    (core / '运行' / '待审').mkdir(parents=True)


def run_build(core):
    env = os.environ.copy()
    env['CORE_HOME'] = str(core)
    return subprocess.run(
        [sys.executable, str(RECALL), 'build'],
        capture_output=True, text=True, encoding='utf-8', timeout=TIMEOUT, env=env)


def build_text(core):
    r = run_build(core)
    if r.returncode != 0:
        raise AssertionError(
            f'recall.py build 退出码 {r.returncode}：{r.stdout}{r.stderr}')
    p = core / 'memory' / '常驻.md'
    if not p.exists():
        raise AssertionError('build 后没有生成 memory/常驻.md')
    return p.read_text(encoding='utf-8')


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def eq(got, want, what):
    if got != want:
        raise AssertionError(f'{what}: 期望 {want!r}，实际 {got!r}')


def test_build_exit_ok(core):
    """build 退出码 0，生成 常驻.md。"""
    make_ledger(core)
    r = run_build(core)
    eq(r.returncode, 0, 'build 退出码')
    check((core / 'memory' / '常驻.md').exists(), 'build 后没有 memory/常驻.md')
    check('常驻 ' in r.stdout, f'build 没打印摘要：{r.stdout!r}')


def test_five_sections(core):
    """五个小节标题都在。"""
    make_ledger(core)
    text = build_text(core)
    missing = [h for h in SECTIONS if h not in text]
    check(not missing, f'缺小节：{missing}')


def test_length_limit(core):
    """总字数不超过 2000。"""
    make_ledger(core)
    text = build_text(core)
    check(len(text) > 0, '常驻页是空的')
    check(len(text) <= 2000, f'常驻页 {len(text)} 字，超过 2000')


def test_todo_line(core):
    """待办那行存在。"""
    make_ledger(core)
    text = build_text(core)
    check('- 待办：' in text, '常驻页里没有「- 待办：」这一行')


def test_resume_body(core):
    """续点正文从流水里取到了内容。"""
    make_ledger(core)
    text = build_text(core)
    check(RESUME_MARK in text, '续点正文没取到流水中「他说了啥」的正文')


CASES = [
    ('build 退出码 0 并生成常驻.md', test_build_exit_ok),
    ('五节标题齐全', test_five_sections),
    ('总字数不超过 2000', test_length_limit),
    ('待办行存在', test_todo_line),
    ('续点正文有内容', test_resume_body),
]


def main():
    if not RECALL.exists():
        print(f'FAIL 找不到 recall.py：{RECALL}（可用 RECALL_PY 指定）')
        return 2
    print(f'recall.py: {RECALL}')
    failed = []
    for name, fn in CASES:
        with tempfile.TemporaryDirectory(prefix='dsh-test-recall-') as tmp:
            core = Path(tmp)
            try:
                fn(core)
                print(f'PASS {name}')
            except AssertionError as e:
                failed.append(name)
                print(f'FAIL {name}: {e}')
            except Exception as e:
                failed.append(name)
                print(f'FAIL {name}: {type(e).__name__}: {e}')
                traceback.print_exc()
    n = len(CASES)
    if failed:
        print(f'FAIL {n - len(failed)}/{n}  （失败 {len(failed)} 条）')
        return 1
    print(f'PASS {n}/{n}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
