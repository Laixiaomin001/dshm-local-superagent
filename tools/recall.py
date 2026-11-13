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
import re
import sys
from pathlib import Path

from lib.io import atomic_write, read_text
from lib.paths import (FACTS, FLOW, LESSONS, OUT, PLANS, QUEUE, REVIEW,
                       STATE)
from lib.timeutil import now, parse_iso

FACT_CAND = REVIEW / '事实候选.md'
REVIEWED_MARK = '## 审核结论'  # 候选文末有这行，说明这轮已经判过，不再算欠账

LIMIT = 2000
MAX_PENDING = 5
MAX_USER = 5
MAX_LESSONS = 5
MAX_RESUME = 600


def read_state() -> dict:
    try:
        st = json.loads(STATE.read_text(encoding='utf-8'))
        return st if isinstance(st, dict) else {}
    except Exception:
        return {}


def slug(s: str, limit: int = 40) -> str:
    s = re.sub(r'[^\w\u4e00-\u9fff-]+', '-', s.strip())
    s = s.strip('-')[:limit]
    return s or 'untitled'


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
            dt = parse_iso(raw)
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


def resume_body() -> str:
    """从最近一条续点里取出「## 他说了啥」的正文，嵌进常驻页。

    resume 倒序存着每轮的记录，最新一条常是普通记录，不能直接取 [0]；
    从前往后找第一条标题含「续点」的，找不到就返回空串。
    续点文件名里带关键词（标题去掉「续点」和前面的冒号/顿号），
    在 流水 里按文件名倒序找第一份 stem 含关键词的 .md。
    找不到含「续点」的记录时（update_state 只留最近五条，续点容易被挤掉），
    兜底扫 流水 下文件名含「续点」的 .md，按文件名倒序取最新一份。
    任何一步失败都返回空串，不能影响常驻页其它部分。
    """
    try:
        target = None
        st = read_state()
        resume = st.get('resume') or []
        rec = next((r for r in resume
                    if isinstance(r, dict) and '续点' in str(r.get('title') or '')), None)
        if rec is not None:
            title = str(rec.get('title') or '').strip()
            key = re.sub(r'[\s：:·\-—、，,。]*续点[\s：:·\-—、，,。]*', '', title).strip()
            if key:
                files = sorted(FLOW.glob('*.md'), key=lambda p: p.stem, reverse=True)
                target = next((p for p in files if key in p.stem or slug(key) in p.stem), None)
        else:
            files = sorted(FLOW.glob('*.md'), key=lambda p: p.stem, reverse=True)
            target = next((p for p in files if '续点' in p.stem), None)
        if target is None:
            return ''
        text = read_text(target)
        if not text:
            return ''

        # 去掉开头的 ---...--- frontmatter
        text = re.sub(r'\A\ufeff?---\r?\n.*?\r?\n---\r?\n?', '', text, count=1, flags=re.S)

        # 取「## 他说了啥」到下一个 ## 之间的正文
        m = re.search(r'^##\s*他说了啥\s*\n(.*?)(?=^##\s|\Z)', text, flags=re.S | re.M)
        if not m:
            return ''
        body = m.group(1).strip()

        # 保留原有分段，压掉连续空行
        paras = [ln.strip() for ln in body.splitlines()]
        kept: list[str] = []
        for ln in paras:
            if ln:
                kept.append(ln)
            elif kept and kept[-1] != '':
                kept.append('')
        while kept and kept[-1] == '':
            kept.pop()
        body = '\n'.join(kept)
        if not body:
            return ''

        if len(body) > MAX_RESUME:
            body = body[:MAX_RESUME].rstrip() + '…'
        return body
    except Exception:
        return ''


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
        if domain != '用户' and subject != '用户' and '用户' not in subject:
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


def count_items(text: str) -> int:
    """数一份候选里的条目：按实际格式认，认不出来算 0。

    三种格式依次认：小节标题（`## 1 · …`）、`- ` 行、表格行。
    表格行指带竖线的非标题行，排除 `|---|` 这种分隔行。
    """
    lines = [ln.strip() for ln in text.splitlines()]
    heads = [ln for ln in lines if re.match(r'^##\s*\d+\s*[·.、|]', ln)]
    if heads:
        return len(heads)
    bullets = [ln for ln in lines if ln.startswith('- ')]
    if bullets:
        return len(bullets)
    rows = [ln for ln in lines
            if ln and not ln.startswith('#') and '|' in ln
            and re.sub(r'[-:|\s]', '', ln)]
    return len(rows)


def latest_lesson_cand() -> Path | None:
    """待审 下日期最新的 YYYYMMDD.md；没有就 None。"""
    try:
        cands = [p for p in REVIEW.glob('*.md') if re.fullmatch(r'\d{8}', p.stem)]
    except Exception:
        return None
    return max(cands, key=lambda p: p.stem, default=None)


def reviewed(p: Path) -> bool:
    """候选是否已判过：正文里出现「## 审核结论」就算。读不到按未判处理。"""
    try:
        return REVIEWED_MARK in p.read_text(encoding='utf-8')
    except Exception:
        return False


def lesson_items(p: Path) -> int:
    """教训候选条目数；已判过的那份算 0。"""
    if reviewed(p):
        return 0
    return count_items(read_text(p))


def pending_counts() -> tuple[int, int]:
    """待审欠账：(教训候选条数, 事实候选条数)。文件不在或读失败都算 0，不抛错。"""
    lesson = fact = 0
    try:
        p = latest_lesson_cand()
        if p is not None:
            lesson = lesson_items(p)
    except Exception:
        lesson = 0
    try:
        fact = count_items(read_text(FACT_CAND))
    except Exception:
        fact = 0
    return lesson, fact


def compose() -> tuple[str, dict]:
    st = read_state()
    count = st.get('count')
    if not isinstance(count, int):
        count = len(list(FLOW.glob('*.md')))

    pending = pending_lines() or ['没有挂着的活。']
    user = user_lines() or ['暂无，事实卡里还没有 domain=用户 的条目。']
    lessons = lesson_titles() or ['台账里还没有条目。']
    resume = resume_body()

    n_lesson, n_fact = pending_counts()
    backlog = ('- 待办：清空' if not n_lesson and not n_fact
               else f'- 待办：教训候选 {n_lesson} 条 · 事实候选 {n_fact} 条')

    lines = [
        '<!-- recall.py 生成，勿手改 -->',
        '',
        '# 常驻记忆',
        '',
        f"生成：{now():%Y-%m-%d %H:%M} · 账本 {count} 件",
        '',
        '## 当前状态',
        current_line(st),
        '',
        '## 续点正文',
        *(resume.splitlines() if resume else ['（没有续点正文，去 memory/流水/ 里找最新一份）']),
        '',
        '## 未结的事',
        *[f'- {x}' for x in pending],
        backlog,
        '- 找东西：路径和工具选择先看 运行/路径速查.md',
        '',
        '## 关于用户',
        *[f'- {x}' for x in user],
        '',
        '## 最近五条教训',
        *[f'- {x}' for x in lessons],
        '',
    ]
    text = '\n'.join(lines)
    if len(text) > LIMIT:  # 兜底：正常单行都截过，不该走到这里
        text = text[:LIMIT].rsplit('\n', 1)[0] + '\n'
    stats = {'pending': len(pending_lines()), 'lessons': len(lesson_titles()),
             'cand_lesson': n_lesson, 'cand_fact': n_fact}
    return text, stats


def cmd_build(_a) -> int:
    text, stats = compose()
    atomic_write(OUT, text)
    print(f"常驻 {len(text)} 字 · 未结 {stats['pending']} · 教训 {stats['lessons']}"
          f" · 候选 教训{stats['cand_lesson']}/事实{stats['cand_fact']}")
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
