#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dream.py —— 每日整理：体检 → 重建索引 → 消费待记 → 补向量 → 写健康表。

一次跑完预算 55 秒（硬上限 60 秒），纯标准库。

规矩：
  · 只调 mem.py 的现成入口，不改 mem.py；
  · 流水、正本只读，一个字不写；
  · 待记的原始 jsonl 只读不删，读到第几行记在 待记/.consumed.json；
  · 向量还没接，--vector 的 ok:false 照实记进健康表，不影响退出码；
  · 任何一步抛异常都记进 steps 和 health.json，不裸崩。

退出码：verify.ok 且 reindex.ok → 0，否则 1。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path('/storage/Users/currentUser/Documents/小敏')
MEM = ROOT / '记忆机'
TOOLS = MEM / '工具'
MEMPY = TOOLS / 'mem.py'
RUNTIME = ROOT / '运行'
PENDING = RUNTIME / '待记'
CONSUMED = PENDING / '.consumed.json'
HEALTH = RUNTIME / 'health.json'

BUDGET_SEC = 55.0          # 整轮预算，离调度器 120 秒超时留足余量
VERIFY_CAP = 30.0
REINDEX_CAP = 25.0
VECTOR_CAP = 8.0


# ---------------------------------------------------------------- 小工具

def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec='seconds')


def last_json(text: str):
    """从子命令输出里取最后一行能解析的 JSON 对象。"""
    for line in reversed((text or '').splitlines()):
        line = line.strip()
        if line.startswith('{') and line.endswith('}'):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


def atomic_write(path: Path, text: str) -> None:
    tmp = Path(str(path) + '.tmp')
    tmp.write_text(text, encoding='utf-8')
    os.replace(tmp, path)


def atomic_write_json(path: Path, obj) -> None:
    atomic_write(path, json.dumps(obj, ensure_ascii=False, indent=1) + '\n')


def call_mem(args, cap: float, deadline: float):
    """跑一条 mem.py 子命令。返回 (rc, stdout, stderr, ms, err)。

    rc 为 None 表示超时/起不来，err 里写原因；mem.py 正常返回非零时 rc 是那个码。
    """
    t0 = time.time()
    limit = min(cap, max(1.0, deadline - t0))
    cmd = [sys.executable, str(MEMPY), *args]
    try:
        p = subprocess.run(cmd, cwd=str(TOOLS), capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=limit)
        return p.returncode, p.stdout, p.stderr, int((time.time() - t0) * 1000), None
    except subprocess.TimeoutExpired:
        return None, '', '', int((time.time() - t0) * 1000), '超时 %.0fs' % limit
    except OSError as e:
        return None, '', '', int((time.time() - t0) * 1000), '起不来：%s' % e


# ---------------------------------------------------------------- 一、体检

def step_verify(deadline: float) -> dict:
    """跑 mem.py verify，拿对账数字。

    这版 mem.py 的 verify 直接打 JSON、不认 --json；新版本认。先试 --json，
    报了 unrecognized arguments 就退回不带参的写法，两种都能接。
    """
    rc, out, err, ms, why = call_mem(['verify', '--json'], VERIFY_CAP, deadline)
    if rc == 2 and 'unrecognized arguments' in (err or ''):
        rc, out, err, ms2, why = call_mem(['verify'], VERIFY_CAP, deadline)
        ms += ms2
    data = last_json(out) or {}
    ok = bool(data.get('ok'))
    if why:
        ok = False
        detail = 'verify 没跑成：%s' % why
    else:
        legacy = data.get('legacy_domain')
        nlegacy = len(legacy) if isinstance(legacy, list) else (legacy or 0)
        detail = 'flow=%s canon=%s docs=%s 缺=%s 多=%s 坏域=%s 旧域=%s 空域=%s' % (
            data.get('flow'), data.get('canon'), data.get('index_docs'),
            len(data.get('missing') or []), len(data.get('extra') or []),
            len(data.get('bad_domain') or []), nlegacy, data.get('empty_domain', 0))
        if not data:
            detail = (detail + '；输出解析不出 JSON：' + (out or err or '')[:200]).strip()
    return {
        'ok': ok,
        'flow': data.get('flow'),
        'canon': data.get('canon'),
        'index_docs': data.get('index_docs'),
        'missing': data.get('missing') or [],
        'extra': data.get('extra') or [],
        'bad_domain': data.get('bad_domain') or [],
        # mem.py 现在两个口径都给：bad_domain 是认不出的域（清单，正常为空），
        # legacy_domain 是历史域名。健康表里只存历史域名的件数，不把上百个文件名抄进来；
        # 要清单跑 工具/mem.py verify。
        'legacy_domain': nlegacy,
        'ms': ms,
        'detail': detail,
    }


# ---------------------------------------------------------------- 二、重索引

def step_reindex(deadline: float) -> dict:
    rc, out, err, ms, why = call_mem(['maintain', '--reindex'], REINDEX_CAP, deadline)
    data = last_json(out) or {}
    ok = (rc == 0) and bool(data.get('ok')) and not why
    if why:
        detail = 'reindex 没跑成：%s' % why
    elif not ok:
        detail = 'rc=%s %s' % (rc, (err or out or '').strip()[:200])
    else:
        detail = 'indexed=%s db=%s' % (data.get('indexed'), data.get('db_bytes'))
    return {'ok': ok, 'ms': ms, 'detail': detail}


# ---------------------------------------------------------------- 三、消费待记

def load_consumed() -> dict:
    if not CONSUMED.is_file():
        return {'version': 1, 'updated': None, 'files': {}}
    try:
        d = json.loads(CONSUMED.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {'version': 1, 'updated': None, 'files': {}}
    if not isinstance(d, dict):
        return {'version': 1, 'updated': None, 'files': {}}
    if not isinstance(d.get('files'), dict):
        d['files'] = {}
    return d


def fmt_row(rec: dict) -> str:
    """一行素材：时间 会话 轮次 | 用户首句（40 字） | 工具数 | 涉及文件（3 个）。"""
    t = rec.get('t')
    try:
        dt = datetime.fromtimestamp(int(t) / 1000)
    except (TypeError, ValueError, OSError):
        dt = datetime.now()
    hhmm = dt.strftime('%H:%M')

    sess = str(rec.get('session') or '?')
    sess = sess[8:] if sess.startswith('session-') else sess
    sess = sess[:8] or '?'
    turn = rec.get('turn')
    turn = '?' if turn is None else str(turn)

    users = rec.get('user') or []
    if isinstance(users, str):
        users = [users]
    first = ''
    for u in users:
        if isinstance(u, str) and u.strip():
            first = u.strip()
            break
    first = re.sub(r'\s+', ' ', first).replace('|', '／')[:40] or '（无）'

    tools = rec.get('tools')
    ntool = len(tools) if isinstance(tools, list) else 0

    files = rec.get('files') or []
    names = []
    for f in (files if isinstance(files, list) else []):
        b = Path(str(f)).name
        if b and b not in names:
            names.append(b)
    files_txt = ', '.join(names[:3]) if names else '—'

    return '%s %s 轮%s | %s | %d | %s' % (hhmm, sess, turn, first, ntool, files_txt)


DAY_HEAD = '# 待记日报 %s\n\n> 列：时间 会话 轮次 | 用户首句（40 字） | 工具数 | 涉及文件（3 个）\n'


def day_title(day: str) -> str:
    return '%s-%s-%s' % (day[:4], day[4:6], day[6:8]) if len(day) == 8 else day


def append_day_report(day: str, rows: list) -> None:
    path = PENDING / ('日报-%s.md' % day)
    stamp = datetime.now().strftime('%H:%M')
    body = '\n'.join(rows) + '\n'
    if path.is_file():
        # 同一天重复跑：追加到末尾，标清本次新增几条
        with open(path, 'a', encoding='utf-8') as f:
            f.write('\n## 追加 %s · 新增 %d 条\n\n' % (stamp, len(rows)))
            f.write(body)
    else:
        atomic_write(path, (DAY_HEAD % day_title(day)) + '\n**首次生成 %s · %d 条**\n\n' % (stamp, len(rows)) + body)


def step_consume() -> dict:
    """读 待记/*.jsonl 的新行，按天写日报。原始 jsonl 只读，不改不删。"""
    t0 = time.time()
    consumed = load_consumed()
    seen = consumed['files']

    files_scanned = 0
    new_lines = 0
    skipped = 0
    by_day = {}
    max_t = {}

    paths = sorted(PENDING.glob('*.jsonl')) if PENDING.is_dir() else []
    for p in paths:
        files_scanned += 1
        try:
            raw = p.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        lines = raw.splitlines()
        done = seen.get(p.name, 0)
        try:
            done = int(done)
        except (TypeError, ValueError):
            done = 0
        if done < 0 or done > len(lines):
            done = 0                     # 文件被截短或换过，从头再读
        for line in lines[done:]:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if not isinstance(rec, dict):
                skipped += 1
                continue
            try:
                dt = datetime.fromtimestamp(int(rec.get('t')) / 1000)
            except (TypeError, ValueError, OSError):
                dt = datetime.now()
            day = dt.strftime('%Y%m%d')
            by_day.setdefault(day, []).append((int(rec.get('t') or 0), fmt_row(rec)))
            max_t[day] = max(max_t.get(day, 0), int(rec.get('t') or 0))
            new_lines += 1
        seen[p.name] = len(lines)

    for day in sorted(by_day):
        rows = [row for _t, row in sorted(by_day[day], key=lambda x: x[0])]
        append_day_report(day, rows)

    consumed['files'] = seen
    consumed['updated'] = now_iso()
    atomic_write_json(CONSUMED, consumed)

    days = sorted(by_day)
    detail = '扫 %d 个文件，新增 %d 行' % (files_scanned, new_lines)
    if days:
        detail += '（' + '、'.join('%s:%d' % (d, len(by_day[d])) for d in days) + '）'
    if skipped:
        detail += '，坏行 %d' % skipped
    return {
        'ok': True,
        'files': files_scanned,
        'lines': new_lines,
        'days': len(days),
        'skipped': skipped,
        'ms': int((time.time() - t0) * 1000),
        'detail': detail,
    }


# ---------------------------------------------------------------- 四、向量（占位）

def step_vector(deadline: float) -> dict:
    rc, out, err, ms, why = call_mem(['maintain', '--vector'], VECTOR_CAP, deadline)
    data = last_json(out) or {}
    if why:
        return {'ok': False, 'reason': why, 'ms': ms, 'detail': 'vector 没跑成：%s' % why}
    ok = bool(data.get('ok'))
    reason = data.get('reason') or ('' if ok else (err or out or '').strip()[:80] or 'unknown')
    detail = 'ok' if ok else ('not-built' if reason == 'not-built' else str(reason))
    # 向量还没接：照实记 ok:false，但不算异常，打印时标 skip
    return {'ok': ok, 'reason': reason, 'ms': ms, 'detail': 'vector %s' % detail,
            'skip': (not ok and reason == 'not-built')}


# ---------------------------------------------------------------- 五、健康表

def previous_at():
    try:
        if HEALTH.is_file():
            return json.loads(HEALTH.read_text(encoding='utf-8')).get('at')
    except (OSError, json.JSONDecodeError):
        pass
    return None


def main() -> int:
    deadline = time.time() + BUDGET_SEC
    steps = []
    print('dream %s' % now_iso())

    def record(name, fn):
        t0 = time.time()
        try:
            info = fn()
        except Exception as e:                      # 不裸崩：记进 steps 和 health
            info = {'ok': False, 'ms': int((time.time() - t0) * 1000),
                    'detail': '%s: %s' % (type(e).__name__, e)}
        ok = bool(info.get('ok'))
        label = 'ok  ' if ok else ('skip' if info.get('skip') else 'FAIL')
        print('[%s] %-8s %5sms  %s' % (label, name,
                                       info.get('ms', 0), info.get('detail', '')))
        steps.append({'name': name, 'ok': ok,
                      'ms': info.get('ms', 0), 'detail': info.get('detail', '')})
        return info

    verify = record('verify', lambda: step_verify(deadline))
    reindex = record('reindex', lambda: step_reindex(deadline))
    consumed = record('consume', step_consume)
    vector = record('vector', lambda: step_vector(deadline))

    overall = bool(verify.get('ok')) and bool(reindex.get('ok'))
    health = {
        'at': now_iso(),
        'ok': overall,
        'verify': {
            'ok': bool(verify.get('ok')),
            'flow': verify.get('flow'),
            'canon': verify.get('canon'),
            'index_docs': verify.get('index_docs'),
            'missing': verify.get('missing') or [],
            'extra': verify.get('extra') or [],
            'bad_domain': verify.get('bad_domain') or [],
            'legacy_domain': verify.get('legacy_domain', 0),
        },
        'reindex': {'ok': bool(reindex.get('ok')), 'ms': reindex.get('ms', 0)},
        'consumed': {
            'files': consumed.get('files', 0),
            'lines': consumed.get('lines', 0),
            'days': consumed.get('days', 0),
        },
        'vector': {
            'ok': bool(vector.get('ok')),
            'reason': vector.get('reason') or '',
            'ms': vector.get('ms', 0),
        },
        'steps': steps,
        'last_dream': previous_at(),
    }

    try:
        atomic_write_json(HEALTH, health)
        print('health → %s' % HEALTH)
    except OSError as e:
        print('health 写不进：%s' % e)

    print('结果：%s（verify.ok=%s reindex.ok=%s）' % (
        'OK' if overall else 'FAIL', health['verify']['ok'], health['reindex']['ok']))
    return 0 if overall else 1


if __name__ == '__main__':
    sys.exit(main())
