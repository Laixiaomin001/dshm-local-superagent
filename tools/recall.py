#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""recall.py — 常驻记忆层：每轮开局先读的那一页。

分两层：
  常驻  memory/常驻.md     小、短、每轮先读；只由本脚本写
  归档  memory/流水/ 等    长的都留在那，按需 core.py search

用法：
  recall.py build   从续点、待办、事实卡、教训重建常驻.md
  recall.py show    打印常驻.md

只读别的文件，只写 memory/常驻.md；总字数硬顶 2000。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

HOME = Path(os.environ.get('CORE_HOME', str(Path.home() / '.dsh' / 'agent-core')))
MEM = HOME / 'memory'
RUN = HOME / '运行'
QUEUE = HOME / 'queue'
PLANS = RUN / '计划'
STATE = MEM / '状态.json'
FACTS = MEM / '事实.jsonl'
FLOW = MEM / '流水'
LESSONS = RUN / '教训.md'
OUT = MEM / '常驻.md'

LIMIT = 2000
MAX_PENDING = 5
MAX_USER = 5
MAX_LESSONS = 5


def now() -> datetime:
    return datetime.now().astimezone()


def read_text(p: Path) -> str:
    try:
        return p.read_text(encoding='utf-8')
    except Exception:
        return ''


def read_state() -> dict:
    try:
        st = json.loads(STATE.read_text(encoding='utf-8'))
        return st if isinstance(st, dict) else {}
    except Exception:
        return {}


def one_line(s, limit: int) -> str:
    s = re.sub(r'\s+', ' ', str(s or '')).strip()
    if len(s) <= limit:
        return s
    return s[:max(1, limit - 1)].rstrip() + '…'


def age_hours(rec: dict) -> float:
    for key in ('heartbeat', 'started', 'createdAt', 'created'):
        raw = rec.get(key)
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
            if dt.tzinfo is None:
                dt = dt.astimezone()
            return max(0.0, (now() - dt).total_seconds() / 3600)
        except Exception:
            continue
    return 0.0


def current_line(st: dict) -> str:
    resume = st.get('resume') or []
    if resume and isinstance(resume[0], dict):
        r = resume[0]
        title = one_line(r.get('title', ''), 30)
        at = str(r.get('at', ''))[:16].replace('T', ' ')
        if title.startswith('续点'):
            title = re.sub(r'^续点[：:·\-—\s]*', '', title)
            return f'上一轮停在「{title}」（{at}），按续点接着做。'
        if title:
            return f'这一阵接着「{title}」往下做（最近 {at}）。'
    return '还没有续点，先看流水和账本再定。'


def pending_lines() -> list[str]:
    """队列 + 计划里还挂着的事，最多五条。"""
    out: list[str] = []
    for sub, label in (('running', 'running'), ('inbox', 'queued')):
        for f in sorted(QUEUE.glob(f'{sub}/*.json')):
            try:
                rec = json.loads(f.read_text(encoding='utf-8'))
            except Exception:
                rec = {}
            title = one_line(rec.get('title') or f.stem, 40)
            tag = label
            if sub == 'running':
                h = age_hours(rec)
                if h >= 3:
                    tag = f'running {h:.0f}h 未收'
            out.append(f'[{tag}] {title}')

    for f in sorted(PLANS.glob('*.md')):
        text = read_text(f)
        if not text:
            continue
        head = next((ln.strip() for ln in text.splitlines() if ln.strip()), '')
        if '已执行' in head or '## 执行记录' in text:
            continue
        title = re.sub(r'^#+\s*', '', head)
        title = one_line(title.split('·')[0], 34) or f.stem
        suffix = '（未执行）' if '未执行' in head else ''
        out.append(f'[计划] {title}{suffix}')
    return out[:MAX_PENDING]


def user_lines() -> list[str]:
    """事实卡里 domain=用户 的条目；没有就明说没有。"""
    out: list[str] = []
    for line in read_text(FACTS).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            card = json.loads(line)
        except Exception:
            continue
        if card.get('失效'):
            continue
        subject = str(card.get('主体', '')).strip()
        predicate = str(card.get('谓词', '')).strip()
        value = str(card.get('值', '')).strip()
        domain = str(card.get('领域') or card.get('domain') or '').strip()
        if domain != '用户' and subject not in ('用户',) and '用户' not in subject:
            continue
        if not value:
            continue
        label = f'{subject}·{predicate}' if predicate else subject
        out.append(f'{one_line(label, 20)}：{one_line(value, 40)}')
    return out[-MAX_USER:]


def lesson_titles() -> list[str]:
    """教训台账最近五条，只取标题（第一句），正文留在教训.md。"""
    out: list[str] = []
    for line in read_text(LESSONS).splitlines():
        s = line.strip()
        if not s.startswith('- '):
            continue
        body = s[2:].strip()
        if '示例' in body:
            continue
        body = re.sub(r'^\d{4}-\d{2}-\d{2}\s*', '', body)
        m = re.match(r'^(.{2,40}?)[。；;]', body)
        title = m.group(1).strip() if m else one_line(body, 36)
        if title:
            out.append(title)
    return out[-MAX_LESSONS:]


def compose() -> tuple[str, dict]:
    st = read_state()
    count = st.get('count')
    if not isinstance(count, int):
        count = len(list(FLOW.glob('*.md')))

    pending = pending_lines() or ['没有挂着的活。']
    user = user_lines() or ['暂无，事实卡里还没有 domain=用户 的条目。']
    lessons = lesson_titles() or ['台账里还没有条目。']

    lines = [
        '<!-- 这个文件由 recall.py build 生成，别手改。 -->',
        '',
        '# 常驻记忆',
        '',
        f"生成：{now():%Y-%m-%d %H:%M} · 账本 {count} 件",
        '',
        '## 当前状态',
        current_line(st),
        '',
        '## 未结的事',
        *[f'- {x}' for x in pending],
        '',
        '## 关于用户',
        *[f'- {x}' for x in user],
        '',
        '## 最近五条教训（正文在 运行/教训.md）',
        *[f'- {x}' for x in lessons],
        '',
    ]
    text = '\n'.join(lines)
    if len(text) > LIMIT:  # 兜底：正常单行都截过，不该走到这里
        text = text[:LIMIT].rsplit('\n', 1)[0] + '\n'
    stats = {'pending': len(pending_lines()), 'lessons': len(lesson_titles())}
    return text, stats


def write_out(text: str) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_name(f'.{OUT.name}.tmp.{os.getpid()}')
    tmp.write_text(text, encoding='utf-8')
    os.chmod(tmp, 0o600)
    os.replace(tmp, OUT)


def cmd_build(_a) -> int:
    text, stats = compose()
    write_out(text)
    print(f"常驻 {len(text)} 字 · 未结 {stats['pending']} · 教训 {stats['lessons']}")
    return 0


def cmd_show(_a) -> int:
    if not OUT.exists():
        print(f'还没有 {OUT}，先跑 recall.py build', file=sys.stderr)
        return 1
    print(OUT.read_text(encoding='utf-8').rstrip())
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='常驻记忆层：生成或查看 memory/常驻.md')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('build', help='从续点/待办/事实卡/教训重建常驻.md').set_defaults(fn=cmd_build)
    sub.add_parser('show', help='打印常驻.md').set_defaults(fn=cmd_show)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == '__main__':
    sys.exit(main())
