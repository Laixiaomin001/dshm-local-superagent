#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""queue.py — 耐久工单队列（目录即队列，认领靠 rename）。

  queue/inbox/*.json     待认领
  queue/running/*.json   已被某个 worker 认领（崩溃后会留在 running）
  queue/done/*.json      完成
  queue/stuck/*.json     失败或超时，等人看

用法：
  queue.py add   --title T --task "..." [--key K] [--cwd /path] [--session ID]
  queue.py list
  queue.py claim ID
  queue.py done  ID --result "..." [--out /path]
  queue.py fail  ID --reason "..."
  queue.py sweep [--older 1800]
  queue.py show  ID
  queue.py accept ID
  queue.py reject ID --reason "..." [--retry]
  queue.py resume ID [--task "新任务"] [--force]
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import string
import sys
import time
from datetime import datetime
from pathlib import Path

HOME = Path(os.environ.get('CORE_HOME', str(Path.home() / '.dsh' / 'agent-core')))
Q = HOME / 'queue'
INBOX, RUNNING, DONE, STUCK = Q / 'inbox', Q / 'running', Q / 'done', Q / 'stuck'


def iso() -> str:
    return datetime.now().astimezone().isoformat(timespec='seconds')


def ensure() -> None:
    for d in (INBOX, RUNNING, DONE, STUCK):
        d.mkdir(parents=True, exist_ok=True)


def new_id() -> str:
    ts = base36(int(time.time()))
    rnd = ''.join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f'q-{ts}-{rnd}'


def base36(n: int) -> str:
    digits = string.digits + string.ascii_lowercase
    if n <= 0:
        return '0'
    out = ''
    while n:
        n, r = divmod(n, 36)
        out = digits[r] + out
    return out


# 查单顺序：done 优先。running 里可能有已落 done 的幽灵副本（插件心跳残留），
# 以 done 的完成记录为准，否则 show/accept 会被幽灵挡住。
DIRS = ((INBOX, 'inbox'), (DONE, 'done'), (RUNNING, 'running'), (STUCK, 'stuck'))

# show 按这个顺序打；单里多出来的字段接在后面
SHOW_FIELDS = ('id', 'key', 'title', 'task', 'cwd', 'session', 'created',
               'status', 'duplicate_of', 'ok', 'accepted', 'accepted_at',
               'rejected', 'reject_reason', 'rejected_at',
               'result', 'finished', 'attempts')

# 一张单最多重试两次；到 2 次就不许再 --retry，转人工
MAX_ATTEMPTS = 2


def find_in(qid: str):
    """四个目录都找一遍，返回 (路径, 目录名)；找不到返回 (None, None)。"""
    for d, label in DIRS:
        p = d / f'{qid}.json'
        if p.exists():
            return p, label
    return None, None


def find(qid: str) -> Path | None:
    return find_in(qid)[0]


def _ts(rec: dict) -> float:
    """把 finished/created 解析成时间戳。两种格式都认：本地带时区、UTC 毫秒 Z。
    认不出的排最前，不参与「最近」的竞争。"""
    raw = str(rec.get('finished') or rec.get('created') or '').strip()
    if raw.endswith('Z'):
        raw = raw[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return float('-inf')
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.timestamp()


def latest_done_with_key(key: str) -> dict | None:
    """done 里同一 key 的最近一张单；没有返回 None。按时间点比，不按字符串。"""
    latest = None
    latest_ts = float('-inf')
    for p in DONE.glob('*.json'):
        try:
            r = json.loads(p.read_text(encoding='utf-8'))
        except Exception:
            continue
        if str(r.get('key') or '') != key:
            continue
        ts = _ts(r)
        if latest is None or ts >= latest_ts:
            latest, latest_ts = r, ts
    return latest


def cmd_add(a) -> int:
    ensure()
    key = (a.key or '').strip()
    done_before = None
    if key:
        # 同一把 key 只允许一张在跑：inbox/running 里有就拒绝
        for d in (INBOX, RUNNING):
            for p in sorted(d.glob('*.json')):
                try:
                    r = json.loads(p.read_text(encoding='utf-8'))
                except Exception:
                    continue
                if str(r.get('key') or '') != key:
                    continue
                print(f'[error] 同一件事已经有一单在跑：{r.get("id") or p.stem}')
                return 1
        # done 里的同 key 不拦：那是再来一次，排队前说一声
        done_before = latest_done_with_key(key)
    qid = new_id()
    rec = {'id': qid, 'title': a.title, 'task': a.task, 'cwd': a.cwd or '',
           'session': a.session or '', 'created': iso(), 'status': 'queued'}
    if key:
        rec['key'] = key
    (INBOX / f'{qid}.json').write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding='utf-8')
    print(f'queued {qid}  {a.title}')
    if done_before is not None:
        print(f'提示：上一单 {done_before.get("id")} 已完成，这次是新的一单')
    return 0


def cmd_list(a) -> int:
    ensure()
    for d, label in ((INBOX, '待认领'), (RUNNING, '进行中'), (STUCK, '卡住')):
        items = sorted(d.glob('*.json'))
        print(f'{label}：{len(items)}')
        for p in items[:10]:
            try:
                r = json.loads(p.read_text(encoding='utf-8'))
                print(f"  {r.get('id','?')}  {r.get('title','')}  · {r.get('created','')[:16]}")
            except Exception:
                print(f'  [?] {p.name}')
    done = sorted(DONE.glob('*.json'))
    print(f'已完成：{len(done)}')
    for p in done[-3:]:
        try:
            r = json.loads(p.read_text(encoding='utf-8'))
            print(f"  {r.get('id','?')}  {r.get('title','')}  · {r.get('finished','')[:16]}")
        except Exception:
            pass
    return 0


def cmd_claim(a) -> int:
    ensure()
    p = INBOX / f'{a.id}.json'
    if not p.exists():
        print(f'[error] inbox 里没有 {a.id}')
        return 1
    dst = RUNNING / p.name
    if dst.exists():
        print(f'[error] {a.id} 已在 running')
        return 1
    os.rename(p, dst)
    rec = json.loads(dst.read_text(encoding='utf-8'))
    rec['status'] = 'running'
    rec['started'] = iso()
    rec['heartbeat'] = rec['started']
    _write(dst, rec)
    print(dst.read_text(encoding='utf-8'))
    return 0


def finish(qid: str, src_dir: Path, extra: dict) -> int:
    p = src_dir / f'{qid}.json'
    if not p.exists():
        print(f'[error] {src_dir.name} 里没有 {qid}')
        return 1
    rec = json.loads(p.read_text(encoding='utf-8'))
    rec.update(extra)
    rec['finished'] = iso()
    (DONE / f'{qid}.json').write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding='utf-8')
    p.unlink()
    print(f'done {qid}')
    return 0


def cmd_done(a) -> int:
    return finish(a.id, RUNNING, {'ok': True, 'status': 'done',
                                  'result': (a.result or '')[:200000], 'out': a.out or ''})


def cmd_fail(a) -> int:
    p = RUNNING / f'{a.id}.json'
    if not p.exists():
        p = INBOX / f'{a.id}.json'
    if not p.exists():
        print(f'[error] 没有 {a.id}')
        return 1
    rec = json.loads(p.read_text(encoding='utf-8'))
    rec.update({'ok': False, 'status': 'failed',
                'reason': (a.reason or '')[:2000], 'finished': iso()})
    (STUCK / f'{a.id}.json').write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding='utf-8')
    p.unlink()
    print(f'stuck {a.id}')
    return 0


def cmd_sweep(a) -> int:
    ensure()
    now = time.time()
    moved = cleaned = 0
    for p in RUNNING.glob('*.json'):
        if (DONE / p.name).exists():
            # done 里已有同名完成记录：这是插件心跳残留，不是卡住，清掉别扫进 stuck
            try:
                p.unlink()
                cleaned += 1
            except Exception:
                pass
            continue
        rec = json.loads(p.read_text(encoding='utf-8'))
        # 按开始时间算超时；有心跳就用最新的心跳，没有 started 的老单退回 created
        ts = rec.get('heartbeat') or rec.get('started') or rec.get('created') or ''
        try:
            t = datetime.fromisoformat(ts).timestamp()
        except Exception:
            t = p.stat().st_mtime
        if now - t > a.older:
            rec.update({'ok': False, 'status': 'stale',
                        'reason': f'running 心跳停更超过 {a.older // 60} 分钟（宿主可能中途退出）',
                        'finished': iso()})
            (STUCK / p.name).write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding='utf-8')
            p.unlink()
            moved += 1
    print(f'swept {moved}')
    if cleaned:
        print(f'cleaned {cleaned}（running 里的已完成残留）')
    return 0


def cmd_show(a) -> int:
    ensure()
    p, where = find_in(a.id)
    if p is None:
        print(f'[error] 找不到这张单：{a.id}')
        return 1
    try:
        rec = json.loads(p.read_text(encoding='utf-8'))
    except Exception as e:
        print(f'[error] {p.name} 读不出来：{e}')
        return 1
    print(f'所在目录：{where}')
    shown = set()
    for k in SHOW_FIELDS:
        v = rec.get(k, '')
        if isinstance(v, bool):
            v = 'true' if v else 'false'
        print(f'{k}: {v}')
        shown.add(k)
    for k in rec:
        if k in shown:
            continue
        v = rec[k]
        if isinstance(v, bool):
            v = 'true' if v else 'false'
        print(f'{k}: {v}')
    return 0


def _attempts(rec: dict) -> int:
    try:
        return int(rec.get('attempts') or 0)
    except (TypeError, ValueError):
        return 0


def _write(p: Path, rec: dict) -> None:
    p.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding='utf-8')


def cmd_accept(a) -> int:
    """验收 done 里的单：打 accepted 标记。"""
    ensure()
    p, where = find_in(a.id)
    if p is None:
        print(f'[error] 找不到这张单：{a.id}')
        return 1
    if where != 'done':
        print(f'[error] {a.id} 在 {where}，只有 done 里的单能验收')
        return 1
    try:
        rec = json.loads(p.read_text(encoding='utf-8'))
    except Exception as e:
        print(f'[error] {p.name} 读不出来：{e}')
        return 1
    rec['accepted'] = True
    rec['accepted_at'] = iso()
    _write(p, rec)
    print(f'accepted {a.id}')
    return 0


def cmd_reject(a) -> int:
    """打回一张单。--retry 时挪回 inbox 重跑；attempts 已达上限就拒绝。"""
    ensure()
    p, where = find_in(a.id)
    if p is None:
        print(f'[error] 找不到这张单：{a.id}')
        return 1
    try:
        rec = json.loads(p.read_text(encoding='utf-8'))
    except Exception as e:
        print(f'[error] {p.name} 读不出来：{e}')
        return 1

    reason = (a.reason or '').strip()
    if not reason:
        print('[error] --reason 不能为空')
        return 1

    rec['rejected'] = True
    rec['reject_reason'] = reason[:2000]
    rec['rejected_at'] = iso()

    if not a.retry:
        # 不重试：单留在原地（正常是 done），只记打回
        _write(p, rec)
        print(f'rejected {a.id}  (留在 {where})')
        return 0

    attempts = _attempts(rec)
    if attempts >= MAX_ATTEMPTS:
        # 先判上限再改文件，拒绝时队列保持原样
        print(f'[error] {a.id} 已重试两次，转人工（attempts={attempts}，上限 {MAX_ATTEMPTS}）')
        return 1

    rec['attempts'] = attempts + 1
    rec['status'] = 'queued'
    for k in ('ok', 'result', 'out', 'reason', 'finished', 'started', 'heartbeat',
              'notified', 'childId', 'stopReason', 'pid', 'log', 'accepted',
              'accepted_at'):
        rec.pop(k, None)

    dst = INBOX / f'{a.id}.json'
    _write(dst, rec)
    if dst != p:
        p.unlink()
    print(f'rejected {a.id} -> inbox  (attempts {attempts} -> {rec["attempts"]})')
    return 0


def cmd_resume(a) -> int:
    ensure()
    p, where = find_in(a.id)
    if p is None:
        print(f'[error] 找不到这张单：{a.id}')
        return 1
    try:
        rec = json.loads(p.read_text(encoding='utf-8'))
    except Exception as e:
        print(f'[error] {p.name} 读不出来：{e}')
        return 1

    # 已完成的单重跑会白烧一遍，必须显式 --force
    if where == 'done' and rec.get('ok') is True and not a.force:
        print(f'[error] {a.id} 已完成（ok:true），重跑请加 --force')
        return 1

    attempts = _attempts(rec) + 1
    if a.task and a.task.strip():
        # 同一件事的后续：新单继承 cwd/session，记下 parent
        nid = new_id()
        new = {
            'id': nid,
            'title': rec.get('title', ''),
            'task': a.task,
            'cwd': rec.get('cwd', ''),
            'session': rec.get('session', ''),
            'created': iso(),
            'status': 'queued',
            'attempts': attempts,
            'parent': a.id,
        }
        (INBOX / f'{nid}.json').write_text(json.dumps(new, ensure_ascii=False, indent=1), encoding='utf-8')
        print(f'queued {nid}  (parent {a.id}, attempts {attempts})')
        return 0

    # 原单挪回 inbox：清掉上一轮的运行痕迹，attempts 累加
    rec['attempts'] = attempts
    rec['status'] = 'queued'
    for k in ('ok', 'result', 'reason', 'finished', 'started', 'heartbeat',
              'notified', 'childId', 'stopReason', 'pid', 'log'):
        rec.pop(k, None)
    dst = INBOX / f'{a.id}.json'
    dst.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding='utf-8')
    if dst != p:
        p.unlink()
    print(f'resumed {a.id}  ({where} -> inbox, attempts {attempts})')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog='queue')
    sub = ap.add_subparsers(dest='cmd', required=True)
    ad = sub.add_parser('add')
    ad.add_argument('--title', required=True)
    ad.add_argument('--task', required=True)
    ad.add_argument('--cwd', default='')
    ad.add_argument('--session', default='')
    ad.add_argument('--key', default='', help='幂等键：同一 key 在 inbox/running 里只能有一张')
    ad.set_defaults(fn=cmd_add)
    sub.add_parser('list').set_defaults(fn=cmd_list)
    cl = sub.add_parser('claim')
    cl.add_argument('id')
    cl.set_defaults(fn=cmd_claim)
    dn = sub.add_parser('done')
    dn.add_argument('id')
    dn.add_argument('--result', default='')
    dn.add_argument('--out', default='')
    dn.set_defaults(fn=cmd_done)
    fl = sub.add_parser('fail')
    fl.add_argument('id')
    fl.add_argument('--reason', default='')
    fl.set_defaults(fn=cmd_fail)
    sw = sub.add_parser('sweep')
    sw.add_argument('--older', type=int, default=1800)
    sw.set_defaults(fn=cmd_sweep)
    sh = sub.add_parser('show')
    sh.add_argument('id')
    sh.set_defaults(fn=cmd_show)
    rs = sub.add_parser('resume')
    rs.add_argument('id')
    rs.add_argument('--task', default='')
    rs.add_argument('--force', action='store_true')
    rs.set_defaults(fn=cmd_resume)
    ac = sub.add_parser('accept')
    ac.add_argument('id')
    ac.set_defaults(fn=cmd_accept)
    rj = sub.add_parser('reject')
    rj.add_argument('id')
    rj.add_argument('--reason', required=True)
    rj.add_argument('--retry', action='store_true')
    rj.set_defaults(fn=cmd_reject)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == '__main__':
    sys.exit(main())
