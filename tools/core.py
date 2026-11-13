#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core.py — 可迁移的 Agent 核心（与宿主无关）。

架构：
  账本   memory/流水/*.md      唯一真相，纯文本，人可读
  索引   memory/索引.db        SQLite FTS5(trigram)，派生物，可重建
  状态   memory/状态.json      件数 / 最后写入 / 续点
  待记   memory/待记/*.jsonl   每回合原料，只增不删
  日报   memory/日报/*.md      整理产物，人读
  健康   health.json           后台是否正常的唯一凭据

命令：
  init / store / search / get / log / dream / reindex / verify / status / export / resume

设计约束：
  · 纯标准库，无第三方依赖
  · 写入 = 临时文件 + rename；索引与正本同一事务
  · 检索返回有字节上限，绝不把整本账灌进上下文
  · 任何一步失败都记进 steps，不裸崩
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
from collections import Counter
from datetime import date, datetime
from pathlib import Path

HOME = Path(os.environ.get('CORE_HOME', str(Path.home() / '.dsh' / 'agent-core')))
DSH_HOME = Path(os.environ.get('DSH_HOME', str(Path.home() / '.dsh')))
PLUGINS_DIR = Path(os.environ.get('DSH_PLUGINS', str(DSH_HOME / 'plugins')))
MEM = HOME / 'memory'
FLOW = MEM / '流水'
PENDING = MEM / '待记'
DIGEST = MEM / '日报'
RUN = HOME / '运行'
REVIEW = RUN / '待审'
LEDGER = RUN / '派单台账'
LEDGER_ARCHIVE = LEDGER / '归档'
LESSONS = RUN / '教训.md'
DB = MEM / '索引.db'
STATE = MEM / '状态.json'
HEALTH = HOME / 'health.json'
LOCK = MEM / '.core.lock'
CONSUMED = PENDING / '.consumed.json'
EXPORT_ROOT = Path(os.environ.get('EXPORT_ROOT') or os.environ.get('CORE_EXPORT')
                   or str(Path.home() / '.dsh' / 'exports'))
MAX_PENDING_BYTES = 512 * 1024
SEARCH_BYTES = 2000
GET_BYTES = 8000


# ---------------------------------------------------------------- 基础工具

def now() -> datetime:
    return datetime.now().astimezone()


def iso(d: datetime | None = None) -> str:
    return (d or now()).isoformat(timespec='seconds')


def slug(s: str, limit: int = 40) -> str:
    s = re.sub(r'[^\w\u4e00-\u9fff-]+', '-', s.strip())
    s = s.strip('-')[:limit]
    return s or 'untitled'


def one_line(s: str, limit: int = 200) -> str:
    return re.sub(r'\s+', ' ', str(s or '')).strip()[:limit]


def ensure_dirs() -> None:
    for p in (HOME, MEM, FLOW, PENDING, DIGEST):
        p.mkdir(parents=True, exist_ok=True)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB, timeout=10)
    conn.execute('PRAGMA journal_mode=DELETE')
    conn.execute('PRAGMA synchronous=NORMAL')
    return conn


def init_db() -> None:
    conn = connect()
    with conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS docs(
            name TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            at TEXT NOT NULL,
            occurred TEXT,
            domain TEXT,
            salience INTEGER DEFAULT 3,
            who TEXT,
            tags TEXT,
            source TEXT,
            sha1 TEXT,
            bytes INTEGER,
            body TEXT
        )''')
        conn.execute('''CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
            name UNINDEXED, title, body, tokenize='trigram'
        )''')
    conn.close()


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f'.tmp.{os.getpid()}.{int(time.time()*1000)%100000}')
    tmp.write_text(text, encoding='utf-8')
    os.replace(tmp, path)


def acquire_lock():
    MEM.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK, 'a+')
    deadline = time.time() + 3
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except BlockingIOError:
            if time.time() > deadline:
                fh.close()
                raise SystemExit('[error] 账本被占用超过 3 秒')
            time.sleep(0.05)


# ---------------------------------------------------------------- 正本与索引

def parse_flow(text: str) -> dict:
    """解析 frontmatter 与正文。坏件返回 title 为空，由调用方兜底。"""
    meta: dict[str, str] = {}
    body = text
    if text.startswith('---'):
        end = text.find('\n---', 3)
        if end != -1:
            head = text[3:end]
            body = text[end + 4:]
            for line in head.splitlines():
                if ':' in line:
                    k, v = line.split(':', 1)
                    meta[k.strip()] = v.strip()
    return {'meta': meta, 'body': body.strip()}


def record_text(title, at, occurred, domain, salience, who, tags, source, said, heart) -> str:
    return (
        '---\n'
        f'title: {title}\n'
        f'at: {at}\n'
        f'occurred: {occurred or at}\n'
        f'domain: {domain}\n'
        f'salience: {salience}\n'
        f'who: {who}\n'
        f'tags: {tags}\n'
        f'source: {source}\n'
        '---\n\n'
        f'## 他说了啥\n\n{said or "（无）"}\n\n'
        f'## 我心里\n\n{heart or "（无）"}\n'
    )


def index_one(conn: sqlite3.Connection, name: str, text: str) -> None:
    p = parse_flow(text)
    m = p['meta']
    # 索引用正文，去掉小标题，免得多条结果里全是「## 他说了啥」
    body = '\n'.join(l for l in p['body'].splitlines() if not l.strip().startswith('## '))
    conn.execute(
        'INSERT OR REPLACE INTO docs(name,title,at,occurred,domain,salience,who,tags,source,sha1,bytes,body) '
        'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
        (name, m.get('title', name), m.get('at', ''), m.get('occurred', ''),
         m.get('domain', '未判'), int(m.get('salience', 3) or 3), m.get('who', ''),
         m.get('tags', ''), m.get('source', ''),
         hashlib.sha1(text.encode('utf-8')).hexdigest(), len(text.encode('utf-8')), body),
    )
    conn.execute('DELETE FROM docs_fts WHERE name=?', (name,))
    conn.execute('INSERT INTO docs_fts(name,title,body) VALUES(?,?,?)',
                 (name, m.get('title', name), body))


def reindex() -> int:
    init_db()
    conn = connect()
    names = set()
    with conn:
        for f in sorted(FLOW.glob('*.md')):
            index_one(conn, f.name, f.read_text(encoding='utf-8'))
            names.add(f.name)
        for row in conn.execute('SELECT name FROM docs').fetchall():
            if row[0] not in names:
                conn.execute('DELETE FROM docs WHERE name=?', (row[0],))
                conn.execute('DELETE FROM docs_fts WHERE name=?', (row[0],))
    conn.close()
    return len(names)


def disk_names() -> set[str]:
    return {f.name for f in FLOW.glob('*.md')}


def db_names() -> set[str]:
    if not DB.exists():
        return set()
    conn = connect()
    try:
        return {r[0] for r in conn.execute('SELECT name FROM docs').fetchall()}
    finally:
        conn.close()


# ---------------------------------------------------------------- 命令

def cmd_init(_a) -> int:
    ensure_dirs()
    init_db()
    if not STATE.exists():
        atomic_write(STATE, json.dumps({'count': 0, 'last': None, 'resume': []},
                                       ensure_ascii=False, indent=1))
    print(f'core 就绪：{HOME}')
    return 0


def cmd_store(a) -> int:
    ensure_dirs()
    init_db()
    t = now()
    title = one_line(a.title, 120)
    at = iso(t)
    occurred = a.occurred or at
    domain = a.domain or '未判'
    salience = max(1, min(5, int(a.salience)))
    who = a.who or '用户'
    tags = a.tags or ''
    source = a.source or ''
    said = a.said or ''
    heart = a.heart or ''
    if a.dry_run:
        print(record_text(title, at, occurred, domain, salience, who, tags, source, said, heart))
        return 0
    name = f'{t:%Y%m%d_%H%M}_{slug(title)}.md'
    i = 2
    while (FLOW / name).exists():
        name = f'{t:%Y%m%d_%H%M}_{slug(title)}-{i}.md'
        i += 1
    text = record_text(title, at, occurred, domain, salience, who, tags, source, said, heart)
    fh = acquire_lock()
    try:
        atomic_write(FLOW / name, text)
        conn = connect()
        try:
            with conn:
                index_one(conn, name, text)
        finally:
            conn.close()
        update_state(title, at)
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()
    print(f'已记：{name}')
    return 0


def update_state(title: str, at: str) -> None:
    try:
        st = json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else {}
    except Exception:
        st = {}
    resume = [r for r in st.get('resume', []) if r.get('title') != title]
    resume.insert(0, {'title': title, 'at': at})
    st = {'count': len(disk_names()), 'last': at, 'resume': resume[:5]}
    atomic_write(STATE, json.dumps(st, ensure_ascii=False, indent=1))


def _like_search(conn, q, limit):
    like = f'%{q}%'
    return conn.execute(
        'SELECT name,title,at,domain,salience,body,0.0 FROM docs '
        'WHERE title LIKE ? OR body LIKE ? ORDER BY at DESC LIMIT ?',
        (like, like, limit)).fetchall()


def cmd_search(a) -> int:
    ensure_dirs()
    init_db()
    q = (getattr(a, 'query_opt', '') or a.query or '').strip()
    if not q:
        print('[error] query 不能为空')
        return 2
    limit = max(1, min(10, int(a.limit)))
    if getattr(a, 'hybrid', False):
        import vectors
        for name, score in vectors.fuse(q, limit):
            try:
                meta = parse_flow((FLOW / name).read_text(encoding='utf-8'))['meta']
            except Exception:
                meta = {}
            print(f"{score:.4f}  {name}\n  {meta.get('title','')} · {meta.get('at','')[:10]} · {meta.get('domain','')}")
        return 0
    conn = connect()
    try:
        if len(q) < 3:
            rows = _like_search(conn, q, limit)
        else:
            phrase = '"' + q.replace('"', '""') + '"'
            rows = conn.execute(
                'SELECT d.name,d.title,d.at,d.domain,d.salience,'
                "snippet(docs_fts,2,'[',']','…',16) AS snip, bm25(docs_fts) AS r "
                'FROM docs_fts JOIN docs d ON d.name=docs_fts.name '
                'WHERE docs_fts MATCH ? ORDER BY r LIMIT ?',
                (phrase, limit)).fetchall()
            rows = [(n, t, at, dom, sal, snip, -r) for (n, t, at, dom, sal, snip, r) in rows]
            if not rows:
                rows = _like_search(conn, q, limit)
                rows = [(n, t, at, dom, sal, body[:160], 0.0) for (n, t, at, dom, sal, body, _r) in rows]
    finally:
        conn.close()
    if not rows:
        print(f'没有命中：{q}')
        return 0
    today = now()
    out = []
    used = 0
    for n, t, at, dom, sal, snip, score in rows:
        try:
            days = max(0.0, (today - datetime.fromisoformat(at)).total_seconds() / 86400)
        except Exception:
            days = 999
        recency = max(0.0, 1.0 - min(days, 365) / 365)
        total = float(score) + int(sal or 3) * 0.4 + recency
        line = f'{n}\n  {t} · {at[:10]} · {dom} · s{sal} · {total:.2f}\n  {one_line(snip, 160)}'
        if used + len(line.encode('utf-8')) > SEARCH_BYTES:
            break
        out.append(line)
        used += len(line.encode('utf-8'))
    print('\n'.join(out))
    return 0


def cmd_get(a) -> int:
    name = a.name
    p = FLOW / name
    if not p.exists():
        print(f'[error] 没有这件：{name}')
        return 1
    text = p.read_text(encoding='utf-8')
    cap = max(200, int(a.max))
    b = text.encode('utf-8')
    if len(b) > cap:
        text = b[:cap].decode('utf-8', 'ignore') + f'\n\n…（截断，全文 {len(b)} 字节）'
    print(text)
    return 0


def cmd_log(a) -> int:
    ensure_dirs()
    t = now()
    rec = {
        't': int(t.timestamp() * 1000),
        'at': iso(t),
        'session': a.session or '',
        'turn': int(a.turn or 0),
        'user': one_line(a.user, 200),
        'assistant': one_line(a.assistant, 200),
        'tools': int(a.tools or 0),
        'files': [x for x in (a.files or '').split(',') if x][:10],
    }
    path = PENDING / f'{t:%Y%m%d}.jsonl'
    for i in range(2, 10):
        if not path.exists() or path.stat().st_size < MAX_PENDING_BYTES:
            break
        path = PENDING / f'{t:%Y%m%d}-{i}.jsonl'
    with open(path, 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
    print(f'待记 +1 → {path.name}')
    return 0


def load_consumed() -> dict:
    try:
        return json.loads(CONSUMED.read_text(encoding='utf-8'))
    except Exception:
        return {}


def consume() -> tuple[int, list[str]]:
    """把待记的新行按天补进日报；返回 (新行数, 触及的日期)。"""
    seen = load_consumed()
    total = 0
    days = []
    for path in sorted(PENDING.glob('*.jsonl')):
        offset = int(seen.get(path.name, 0))
        lines = path.read_text(encoding='utf-8').splitlines()
        new = lines[offset:]
        if not new:
            continue
        day = re.sub(r'\D', '', path.name)[:8]
        digest = DIGEST / f'{day}.md'
        if not digest.exists():
            atomic_write(digest, f'# {day[:4]}-{day[4:6]}-{day[6:8]} 整理\n\n')
        with open(digest, 'a', encoding='utf-8') as fh:
            for line in new:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                hm = str(r.get('at', ''))[11:16]
                files = '、'.join(r.get('files') or []) or '—'
                fh.write(f"- {hm} `{r.get('session','')[:12]}` 回合{r.get('turn',0)} · "
                         f"{one_line(r.get('user',''), 60)} · 工具{r.get('tools',0)} · {files}\n")
                total += 1
        seen[path.name] = len(lines)
        if day not in days:
            days.append(day)
    atomic_write(CONSUMED, json.dumps(seen, ensure_ascii=False, indent=1))
    return total, days


def read_health() -> dict | None:
    try:
        return json.loads(HEALTH.read_text(encoding='utf-8'))
    except Exception:
        return None


def cmd_verify(a) -> int:
    ensure_dirs()
    t0 = time.time()
    disk = disk_names()
    db = db_names()
    missing = sorted(disk - db)
    extra = sorted(db - disk)
    fts = 0
    bad = []
    if DB.exists():
        conn = connect()
        try:
            fts = conn.execute('SELECT count(*) FROM docs_fts').fetchone()[0]
            for name, dom in conn.execute('SELECT name,domain FROM docs').fetchall():
                if not dom or dom == '未判':
                    bad.append(name)
        finally:
            conn.close()
    ok = not missing and not extra and fts == len(db) == len(disk)
    result = {
        'ok': ok,
        'at': iso(),
        'ms': int((time.time() - t0) * 1000),
        'disk': len(disk), 'db': len(db), 'fts': fts,
        'missing': missing[:5], 'extra': extra[:5],
        'undecided_domain': len(bad),
    }
    if a.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(('✓ ok' if ok else '✗ 不一致') +
              f" · 磁盘 {len(disk)} · 索引 {len(db)} · FTS {fts}" +
              (f" · missing {len(missing)}" if missing else '') +
              (f" · extra {len(extra)}" if extra else '') +
              f" · 未判域 {len(bad)}")
    return 0 if ok else 1


def cmd_reindex(_a) -> int:
    n = reindex()
    print(f'重建完成：{n} 件')
    return 0


def cmd_status(a) -> int:
    ensure_dirs()
    st = {}
    try:
        st = json.loads(STATE.read_text(encoding='utf-8'))
    except Exception:
        pass
    h = read_health()
    size = DB.stat().st_size if DB.exists() else 0
    info = {
        'count': len(disk_names()),
        'db_bytes': size,
        'last': st.get('last'),
        'health': None if not h else {'ok': h.get('ok'), 'at': h.get('at')},
        'resume': st.get('resume', []),
    }
    if a.json:
        print(json.dumps(info, ensure_ascii=False))
        return 0
    print(f"件数 {info['count']} · 索引 {size//1024} KB · 最后写入 {(info['last'] or '—')[:16]}")
    if h:
        print(f"健康 {'✓' if h.get('ok') else '✗'} · {h.get('at','')[:16]}")
    else:
        print('健康 未生成（跑一次 dream）')
    ag = DSH_HOME / 'AGENTS.md'
    try:
        hooked = ag.exists() and '## 记忆与账本' in ag.read_text(encoding='utf-8')
    except Exception:
        hooked = False
    print('宪章挂钩 ' + ('✓' if hooked else '✗ 需重新追加到 AGENTS.md'))
    if info['resume']:
        print('续点：')
        for r in info['resume'][:3]:
            print(f"  · {r.get('at','')[:16]} {r.get('title','')}")
    return 0


def snapshot() -> str:
    """把整本账导到 $EXPORT_ROOT/agent-core-latest，并留当天 tar.gz（最多 7 份）。"""
    EXPORT_ROOT.mkdir(parents=True, exist_ok=True)
    latest = EXPORT_ROOT / 'agent-core-latest'
    r = subprocess.run([sys.executable, str(Path(__file__).resolve()), 'export', '--out', str(latest)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:300])
    tar = EXPORT_ROOT / f'agent-core-{now():%Y%m%d}.tar.gz'
    with tarfile.open(tar, 'w:gz') as tf:
        tf.add(latest, arcname='agent-core')
    olds = sorted(EXPORT_ROOT.glob('agent-core-*.tar.gz'))[:-7]
    for o in olds:
        try:
            o.unlink()
        except Exception:
            pass
    return tar.name


def cmd_doctor(a) -> int:
    """运维体检：把「行为是否还在正轨」变成可判定的检查项。"""
    checks = []

    def chk(name, ok, detail='', warn=False):
        # warn=True 的项只亮黄，不算不通过，不阻断
        checks.append((name, bool(ok), detail, warn))

    chk('宪章/身份/规则', all((HOME / f).exists() for f in ('charter.md', 'identity.md', 'policy.json')))
    ag = DSH_HOME / 'AGENTS.md'
    try:
        chk('AGENTS 挂钩', ag.exists() and '## 记忆与账本' in ag.read_text(encoding='utf-8'))
    except Exception:
        chk('AGENTS 挂钩', False, '读不到')
    h = read_health()
    if h:
        try:
            age = (now() - datetime.fromisoformat(h['at'])).total_seconds() / 3600
        except Exception:
            age = 999
        chk('健康表 <36h', age < 36, f'{age:.1f}h 前')
    else:
        chk('健康表 <36h', False, '从未生成')
    try:
        consumed = load_consumed()
        backlog = sum(max(0, len(p.read_text(encoding='utf-8').splitlines()) - consumed.get(p.name, 0))
                      for p in PENDING.glob('*.jsonl'))
    except Exception:
        backlog = 0
    chk('待记积压 <200', backlog < 200, f'{backlog} 条')
    stuck = len(list((HOME / 'queue' / 'stuck').glob('*.json')))
    chk('卡住工单 =0', stuck == 0, f'{stuck} 张')
    chk('账本对账', command_verify())
    tars = sorted(EXPORT_ROOT.glob('agent-core-*.tar.gz'), key=lambda x: x.stat().st_mtime)
    # 只查本机导出目录的 tar，远端没有副本，不是异地备份
    snap_ok = bool(tars) and (time.time() - tars[-1].stat().st_mtime) / 86400 < 8
    chk('本机快照', snap_ok,
        f'{tars[-1].name} · 8 天内；远端无副本' if snap_ok
        else f"{tars[-1].name if tars else '没有'}（异地副本不存在）")
    # 心声断档：只认标题带「心声·」的件，最新一件超 72h 亮黄，不阻断
    last_heart = None
    for f in FLOW.glob('*.md'):
        try:
            m = parse_flow(f.read_text(encoding='utf-8'))['meta']
        except Exception:
            continue
        if '心声·' not in m.get('title', ''):
            continue
        try:
            at = datetime.fromisoformat(m.get('at', ''))
        except Exception:
            continue
        if last_heart is None or at > last_heart:
            last_heart = at
    if last_heart is None:
        chk('心声断档', False, '本机没有心声件', warn=True)
    else:
        days = (now() - last_heart).total_seconds() / 86400
        ago = '<3d' if days <= 3 else f'{days:.0f}d'
        chk('心声断档', days <= 3, f'{ago} · 最近 {last_heart:%Y-%m-%d}', warn=True)
    # 图谱一致性：只查结构断言，不查天天会变的数字
    gbad = []
    try:
        gp = RUN / '图谱.md'
        if not gp.exists():
            gbad.append('缺 运行/图谱.md')
        elif gp.stat().st_size < 4096:
            gbad.append(f'图谱.md {gp.stat().st_size} 字节 < 4096')
    except Exception:
        gbad.append('读不到 运行/图谱.md')
    try:
        md = (RUN / '手册' / '整理.md').read_text(encoding='utf-8')
        parts = md.split('## dream 十三步', 1)
        if len(parts) < 2:
            gbad.append('整理.md 缺「## dream 十三步」')
        else:
            body = parts[1].split('\n## ', 1)[0]
            steps = [ln for ln in body.splitlines() if re.match(r'\s*\d+\.', ln)]
            if len(steps) < 13:
                gbad.append(f'dream 步骤 {len(steps)} 条 < 13')
    except Exception:
        gbad.append('读不到 手册/整理.md')
    try:
        sch = json.loads((DSH_HOME / 'storages' / 'schedule.json')
                         .read_text(encoding='utf-8'))
        tasks = ((sch.get('tables') or {}).get('tasks') or {})
        act = sum(1 for v in tasks.values() if isinstance(v, dict) and v.get('status') == 'active')
        if act < 2:
            gbad.append(f'schedule active {act} 条 < 2')
    except Exception:
        gbad.append('读不到 storages/schedule.json')
    for name in ('guard', 'ctx-guard', 'delegate'):
        if not (PLUGINS_DIR / name).is_dir():
            gbad.append(f'缺 {PLUGINS_DIR / name}')
    if not (PLUGINS_DIR / 'ctx-guard' / 'tone.js').exists():
        gbad.append(f'缺 {PLUGINS_DIR / "ctx-guard" / "tone.js"}')
    chk('图谱一致性', not gbad, '；'.join(gbad) if gbad else '图谱一致')

    bad = [c for c in checks if not c[1] and not c[3]]
    warn = [c for c in checks if not c[1] and c[3]]
    for name, ok, detail, w in checks:
        mark = '✓' if ok else ('△' if w else '✗')
        print(mark + f' {name}' + (f' · {detail}' if detail else ''))
    if bad:
        print(f'{len(bad)} 项不通过')
    elif warn:
        print(f'{len(checks) - len(warn)} 项通过，{len(warn)} 项提醒')
    else:
        print('全部通过')
    return 0 if not bad else 1


# ---------------------------------------------------------------- 教训候选
#
# 只筛候选，不下结论。判断由人/主会话第二天做，结果写进 运行/教训.md。

# 用户这轮像在纠正上一条回答
LESSON_CORRECTIONS = ('不对', '不是', '错了', '搞错', '重新', '别',
                      '怎么又', '我说的是', '你理解错', '不行', '怎么')
# 我这边可能自己暴露了没做成
LESSON_SELF = ('失败', '报错', '没成', '不行', '卡住')
# 待记里混着的系统噪声，不算用户说的话
_NOISE_PREFIX = ('Time sampled while preparing', '<system-reminder>',
                 'Current runtime context', 'The user changed the approval policy',
                 'Browser time zone for this request', 'Updated instructions from:')

LESSON_SEED = (
    '# 教训台账\n\n'
    '只写真的被现实推翻过的判断。格式：一条一段，写三样——'
    '当时怎么判断的、错在哪、下次怎么拦。\n\n'
    '- （示例）2026-11-11 以为删会话只删目录即可，实际还有子代理 parent 引用和调度引用两层；'
    '下次删会话前先查引用。\n'
)


def _user_parts(v) -> list[str]:
    """user 有时是数组、有时是字符串；去掉系统噪声，保留真正说的话。"""
    items = v if isinstance(v, (list, tuple)) else [v]
    keep = []
    for x in items:
        s = str(x or '').strip()
        if not s or s.startswith(_NOISE_PREFIX):
            continue
        keep.append(s)
    if not keep:
        raw = ' '.join(str(x or '') for x in items).strip()
        if raw:
            keep.append(raw)
    return keep


def _clip(s: str, limit: int) -> str:
    s = one_line(s, limit)
    return s + '…' if len(s) >= limit else s


def scan_lessons(day: str) -> tuple[int, str]:
    """扫当天待记，挑判断可能出错的轮次，写 运行/待审/<day>.md。返回 (候选数, 备注)。"""
    day = re.sub(r'\D', '', str(day or ''))[:8]
    if len(day) != 8:
        return 0, '日期格式不对'
    stamp = f'{day[:4]}-{day[4:6]}-{day[6:8]}'
    if not LESSONS.exists():
        atomic_write(LESSONS, LESSON_SEED)

    rows = []
    for path in sorted(PENDING.glob(f'{day}*.jsonl')):
        try:
            lines = path.read_text(encoding='utf-8').splitlines()
        except Exception:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue

            parts = _user_parts(r.get('user'))
            user = one_line(' / '.join(parts), 4000)
            tail = one_line(r.get('assistant_tail') or r.get('assistant') or '', 4000)
            tools = int(r.get('tools') or 0)
            files = [str(f) for f in (r.get('files') or []) if str(f).strip()]
            names = [os.path.basename(f.rstrip('/')) for f in files]

            counts: dict[str, int] = {}
            for n in names:
                counts[n] = counts.get(n, 0) + 1
            top = max(counts.items(), key=lambda kv: kv[1]) if counts else ('', 0)

            hits = []
            for w in LESSON_CORRECTIONS:
                if w in user:
                    hits.append(f'纠正词「{w}」')
                    break
            if tools >= 30:
                hits.append(f'工具打转（{tools} 次）')
            if top[1] >= 3:
                hits.append(f'反复改「{top[0]}」×{top[1]}')
            for w in LESSON_SELF:
                if w in tail:
                    hits.append(f'自述「{w}」')
                    break
            if not hits:
                continue

            if top[1] >= 2:
                files_disp = f'{top[0]}×{top[1]}'
            elif names:
                files_disp = '、'.join(names[:3])
            else:
                files_disp = '—'
            rows.append({
                'turn': int(r.get('turn') or 0),
                'hm': str(r.get('at', ''))[11:16],
                'at': str(r.get('at', '')),
                'user': _clip(user, 200) or '（空）',
                'tail': _clip(tail, 200) or '（无）',
                'hits': hits,
                'tools': tools,
                'files': files_disp,
            })

    # 命中越多越像出了事，先占名额；同分按时间读
    rows.sort(key=lambda x: (-len(x['hits']), x['at'], x['turn']))
    rows = rows[:8]
    rows.sort(key=lambda x: (x['at'], x['turn']))
    out = REVIEW / f'{day}.md'

    if not rows:
        atomic_write(out, f'# {stamp} 没有触发词\n')
        return 0, '无候选'

    md = [f'# {stamp} 教训候选（{len(rows)} 条）', '']
    for i, c in enumerate(rows, 1):
        md.append(f"## {i} · 回合 {c['turn']} · {c['hm']}")
        md.append(f"用户说：{c['user']}")
        md.append(f"我回：{c['tail']}")
        md.append(f"命中：{'、'.join(c['hits'])}")
        md.append(f"工具：{c['tools']} 次 · 文件：{c['files']}")
        md.append('')
    md.append('判完了把该留的写进 运行/教训.md，其余不管。')
    atomic_write(out, '\n'.join(md) + '\n')
    return len(rows), f'{len(rows)} 条候选'


# ---------------------------------------------------------------- 判域与事实候选
#
# 两步都只写候选清单到 运行/待审/，不碰账本、不自动判、不自动写事实卡。

UNDECIDED_DOMAINS = ('', '未判')

FACT_CONNECTORS = ('叫', '是', '在', '用', '有')
FACT_SUBJ_SUFFIX = ('机器', '手机', '平板', '电脑', '引擎', '系统', '服务器', '远端',
                    '账本', '索引', '端口', '模型', '软件', '数据库', '账号', '备份',
                    '目录', '文件', '脚本', '命令', '插件', '工具', '配置', '版本',
                    '地址', '密码', '密钥', '设备', '进程', '服务', '路径', '任务',
                    '接口', '带宽', '尺寸', '电池', '屏幕', '内核', '容器', '会话',
                    '记忆', '流水', '待记', '日报', '向量', '事实', '卡', '人', '名')
FACT_DISCARD_PREFIX = ('所以', '因为', '但是', '而且', '然后', '就是', '看来', '记得',
                       '我觉得', '其实', '如果', '不过', '同时', '现在', '刚才',
                       '还有', '只是', '再往后')
FACT_QUESTION = ('吗', '呢', '？', '?', '怎么', '为什么', '哪', '谁', '多少',
                 '是否', '还是')
FACT_MODAL = ('都', '也', '还', '只', '就', '才', '又', '并', '大概率', '可能',
              '应该', '也许', '大概')


def _fact_subject_ok(s: str) -> bool:
    """主体得是人名、设备名、路径或软件名，太长的从句一律不要。"""
    s = s.strip(' 　,，。:：')
    if not s or len(s) > 14:
        return False
    if re.fullmatch(r'(~?/)[\w./~\-]+', s):
        return True
    if re.fullmatch(r'[A-Za-z][\w.+\-]{1,30}', s):
        return True
    if s in ('我', '你', '他', '她', '用户'):
        return True
    if re.search(r'[\u4e00-\u9fff]', s) and s.endswith(FACT_SUBJ_SUFFIX):
        return True
    return False


def _fact_location_like(v: str) -> bool:
    return bool(re.search(r'/|里|上|中|下|盘|云|远端|手机|本机|机器|目录|服务器', v))


def _fact_triple(seg: str):
    """把一句短句拆成 (主体, 谓词, 值)；拆不出像样的就返回 None。"""
    text = re.sub(r'`+|\*\*', '', seg)
    text = re.sub(r'^\s*[-*]?\s*\d+[.、]\s*', '', text)
    if any(w in text for w in FACT_QUESTION):
        return None
    for c in FACT_CONNECTORS:
        i = text.find(c, 1)
        if i <= 0:
            continue
        raw = re.split(r'[，,：:、；;—\-]', text[:i])[-1]
        for d in FACT_DISCARD_PREFIX:
            if raw.startswith(d):
                raw = raw[len(d):]
        subj = raw.strip(' 　,，。:：`*')
        val = text[i + 1:].strip()
        val = re.sub(r'^(了|的|着|过|个)+', '', val)
        val = re.split(r'[，,。；;！!？?]', val)[0].strip(' 　`*')
        if not _fact_subject_ok(subj) or len(val) < 2 or len(val) > 40:
            continue
        if any(w in subj for w in FACT_MODAL):
            continue
        if subj.startswith(('个', '是', '可以', '能', '要', '请', '把', '被', '给', '让', '跟', '和')):
            continue
        if subj.startswith('我') and len(subj) > 1 and subj[1] in '记知想看听':
            continue
        if c == '在' and not _fact_location_like(val):
            continue
        if '没有' in val or val.endswith(('吗', '呢')):
            continue
        return subj, c, val
    return None


def scan_undecided() -> tuple[int, str]:
    """扫流水件的 domain，空或「未判」的写进 运行/待审/未判域.md。返回 (件数, 备注)。"""
    stamp = f'{now():%Y-%m-%d}'
    rows = []
    for path in sorted(FLOW.glob('*.md')):
        try:
            data = parse_flow(path.read_text(encoding='utf-8'))
        except Exception:
            continue
        meta = data['meta']
        if str(meta.get('domain') or '').strip() not in UNDECIDED_DOMAINS:
            continue
        title = one_line(meta.get('title') or '', 60) or path.stem
        body = _clip(data['body'], 60) or '（空）'
        rows.append((path.stem, title, body))

    out = REVIEW / '未判域.md'
    if not rows:
        atomic_write(out, f'# 未判域 · {stamp}\n\n全部已判\n')
        return 0, '0 件未判'
    md = [f'# 未判域 · {stamp}（{len(rows)} 件）', '']
    for name, title, body in rows:
        md.append(f'{name} · {title} · {body}')
    md.append('')
    md.append('判完把 domain 补进对应件的 frontmatter。')
    atomic_write(out, '\n'.join(md) + '\n')
    return len(rows), f'{len(rows)} 件未判'


def scan_factcand(day: str) -> tuple[int, str]:
    """扫当天待记，抽「主体 + 谓词 + 值」候选，写 运行/待审/事实候选.md。只提候选。"""
    day = re.sub(r'\D', '', str(day or ''))[:8]
    if len(day) != 8:
        return 0, '日期格式不对'
    stamp = f'{day[:4]}-{day[4:6]}-{day[6:8]}'

    rows = []
    for path in sorted(PENDING.glob(f'{day}*.jsonl')):
        try:
            lines = path.read_text(encoding='utf-8').splitlines()
        except Exception:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            rows.append((path, r))
    rows.sort(key=lambda pr: str(pr[1].get('at') or ''))

    seen: set[tuple[str, str]] = set()
    cands = []
    for path, r in rows:
        parts = _user_parts(r.get('user'))
        chunks = [' / '.join(parts), one_line(r.get('assistant_tail') or r.get('assistant') or '', 4000)]
        for chunk in chunks:
            for seg in re.split(r'[。！？；\n]', str(chunk)):
                seg = seg.strip()
                if not seg or len(seg) > 60:
                    continue
                hit = _fact_triple(seg)
                if not hit:
                    continue
                key = (hit[0], hit[1])
                if key in seen:
                    continue
                seen.add(key)
                cands.append({'subj': hit[0], 'pred': hit[1], 'val': hit[2],
                              'src': f"{path.name} · 回合{int(r.get('turn') or 0)}"})

    out = REVIEW / '事实候选.md'
    if not cands:
        atomic_write(out, f'# 事实候选 · {stamp}\n\n今天没有候选\n')
        return 0, '0 条候选'
    md = [f'# 事实候选 · {stamp}（{len(cands)} 条）', '']
    for c in cands:
        md.append(f"{c['subj']} | {c['pred']} | {c['val']} | {c['src']}")
    md.append('')
    md.append('挑真的写进 记忆/事实.jsonl；一条只留一个有效卡。')
    atomic_write(out, '\n'.join(md) + '\n')
    return len(cands), f'{len(cands)} 条候选'


def tone_report(day: datetime | None = None) -> str:
    """读 运行/口吻自检.jsonl，统计当天条数、不合格率和最常犯的三条，写报告。

    合规率本身也要能看见，所以合格行（issues 为空）也计入分母。
    """
    log = RUN / '口吻自检.jsonl'
    if not log.exists():
        return '无记录'
    d = day or now()
    prefix = d.strftime('%Y-%m-%d')
    recs = []
    try:
        for line in log.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if str(r.get('at') or '').startswith(prefix):
                recs.append(r)
    except Exception as e:
        return '读日志失败: ' + str(e)[:60]
    if not recs:
        return '无记录'

    n = len(recs)
    bad = sum(1 for r in recs if r.get('issues'))
    rate = round(bad * 100 / n, 1)
    counts = Counter()
    for r in recs:
        for issue in r.get('issues') or []:
            counts[str(issue)] += 1
    top = counts.most_common(3)

    md = [f'# 口吻自检 · {d:%Y-%m-%d}', '',
          f'- 记录：{n} 条',
          f'- 不合格：{bad} 条（{rate}%）',
          f'- 合格：{n - bad} 条（{round((n - bad) * 100 / n, 1)}%）', '']
    md.append('## 最常犯')
    md.append('')
    if top:
        for issue, cnt in top:
            md.append(f'- {issue}（{cnt} 次）')
    else:
        md.append('- 没有不合格记录')
    md.append('')
    atomic_write(RUN / '报告' / f'口吻-{d:%Y%m%d}.md', '\n'.join(md) + '\n')
    return f'{n} 条，不合格 {rate}%'


def sweep_ledger() -> str:
    """台账按天分文件：30 天前的挪进 归档/，归档里 180 天前的删掉。返回清了几份。"""
    if not LEDGER.exists():
        return '清 0 份'
    today = now().date()
    n = 0
    moved: set[str] = set()

    def day_of_name(p: Path) -> date | None:
        s = p.stem
        if len(s) != 8 or not s.isdigit():
            return None
        try:
            return date(int(s[:4]), int(s[4:6]), int(s[6:]))
        except ValueError:
            return None

    for f in sorted(LEDGER.glob('*.jsonl')):
        d = day_of_name(f)
        if d is None or (today - d).days <= 30:
            continue
        LEDGER_ARCHIVE.mkdir(parents=True, exist_ok=True)
        shutil.move(str(f), str(LEDGER_ARCHIVE / f.name))
        moved.add(f.name)
        n += 1

    if LEDGER_ARCHIVE.exists():
        for f in sorted(LEDGER_ARCHIVE.glob('*.jsonl')):
            d = day_of_name(f)
            if d is not None and (today - d).days > 180:
                f.unlink()
                if f.name not in moved:  # 刚挪进来的同一份不重复数
                    n += 1
    return f'清 {n} 份'


def cmd_dream(a) -> int:
    # dream 自己的日志：起步和收尾各留一行，写不进去不影响主流程
    def _dream_log(line: str) -> None:
        try:
            (RUN / 'dream.log').parent.mkdir(parents=True, exist_ok=True)
            with (RUN / 'dream.log').open('a', encoding='utf-8') as f:
                f.write(line + '\n')
        except Exception:
            pass

    _dream_log(f'started {iso()}')
    ensure_dirs()
    init_db()
    steps = []
    t0 = time.time()

    def step(name, fn):
        s = time.time()
        try:
            note = fn()
            steps.append({'name': name, 'ok': True, 'ms': int((time.time() - s) * 1000), 'note': note})
            return note
        except Exception as e:  # 不裸崩
            steps.append({'name': name, 'ok': False, 'ms': int((time.time() - s) * 1000),
                          'note': str(e)[:300]})
            return None

    def _child(argv, timeout, last_line=False):
        # 子进程非 0 退出必须记失败；只看 stdout 会把体检红当成绿
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or '').strip()
        if r.returncode != 0:
            lines = (r.stderr or out).strip().splitlines()
            why = lines[-1] if lines else '无输出'
            raise RuntimeError(f'exit {r.returncode}: {why[:240]}')
        if last_line:
            return (out.splitlines() or ['ok'])[-1]
        return out or 'ok'

    step('reindex', lambda: reindex())
    step('sentinel', lambda: _child(
        [sys.executable, str(Path(__file__).with_name('sentinel.py'))], 180))
    consumed = step('consume', lambda: consume()[0])
    step('digest', lambda: f"{len(list(DIGEST.glob('*.md')))} 份")
    step('lessons', lambda: (lambda n: f'{n} 条候选')(scan_lessons(now().strftime('%Y%m%d'))[0]))
    step('undecided', lambda: (lambda n: f'{n} 件未判')(scan_undecided()[0]))
    step('factcand', lambda: (lambda n: f'{n} 条候选')(scan_factcand(now().strftime('%Y%m%d'))[0]))
    step('tone', tone_report)
    def _vectors_step():
        # 向量是锦上添花：嵌入接口不通或报错只降级，不拖红整轮
        try:
            r = subprocess.run(
                [sys.executable, str(Path(__file__).with_name('vectors.py')), 'rebuild'],
                capture_output=True, text=True, timeout=600)
            if r.returncode != 0:
                lines = (r.stderr or r.stdout or '').strip().splitlines()
                return 'degraded: ' + (lines[-1] if lines else f'退出码 {r.returncode}')[:80]
            return (r.stdout or '').strip() or 'ok'
        except Exception as e:
            return 'degraded: ' + (str(e).strip() or '向量重建失败')[:80]

    step('vectors', _vectors_step)

    def _recall_step():
        # 常驻层：把续点/待办/用户事实/教训压成一页，每轮开局先读
        r = subprocess.run(
            [sys.executable, str(Path(__file__).with_name('recall.py')), 'build'],
            capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            lines = (r.stderr or r.stdout or '').strip().splitlines()
            raise RuntimeError((lines[-1] if lines else f'退出码 {r.returncode}')[:200])
        return (r.stdout or '').strip() or 'ok'

    step('recall', _recall_step)
    step('doctor', lambda: _child(
        [sys.executable, str(Path(__file__).resolve()), 'doctor'], 180, last_line=True))
    step('ledger', sweep_ledger)
    step('backup', lambda: snapshot())
    ok = all(s['ok'] for s in steps)
    health = {
        'ok': ok,
        'at': iso(),
        'ms': int((time.time() - t0) * 1000),
        'steps': steps,
        'counts': {'disk': len(disk_names()), 'db': len(db_names())},
        'consumed': consumed,
    }
    atomic_write(HEALTH, json.dumps(health, ensure_ascii=False, indent=1))
    _dream_log(f"done ok={'true' if ok else 'false'} {iso()}")
    if a.json:
        print(json.dumps(health, ensure_ascii=False))
    else:
        for s in steps:
            print(('✓' if s['ok'] else '✗') + f" {s['name']} {s['ms']}ms {s['note']}")
        print(f"健康 {'✓' if ok else '✗'} → {HEALTH}")
    return 0 if ok else 1


def command_verify() -> bool:
    disk = disk_names()
    db = db_names()
    if disk != db or not DB.exists():
        return False
    conn = connect()
    try:
        fts = conn.execute('SELECT count(*) FROM docs_fts').fetchone()[0]
    finally:
        conn.close()
    return fts == len(db)


def cmd_export(a) -> int:
    ensure_dirs()
    stamp = f'{now():%Y%m%d-%H%M}'
    out = Path(a.out) if a.out else (EXPORT_ROOT / f'agent-core-{stamp}')
    out = out.resolve()
    root = EXPORT_ROOT.resolve()
    if out == root or root not in out.parents:
        print(f'[error] 拒绝导出到 {out}：只允许 {root} 下的子目录')
        return 2
    if out.exists():
        shutil.rmtree(out)
    (out / '流水').mkdir(parents=True)
    (out / '日报').mkdir(parents=True)
    for f in FLOW.glob('*.md'):
        shutil.copy2(f, out / '流水' / f.name)
    for f in DIGEST.glob('*.md'):
        shutil.copy2(f, out / '日报' / f.name)
    for f in ('charter.md', 'identity.md', 'README.md', 'ARCHITECTURE.md', 'policy.json', 'health.json'):
        if (HOME / f).exists():
            shutil.copy2(HOME / f, out / f)
    if STATE.exists():
        shutil.copy2(STATE, out / '状态.json')
    index = ['# Agent 核心导出\n', f'时间：{iso()}\n', f'件数：{len(list(FLOW.glob("*.md")))}\n\n',
             '这份目录不依赖任何程序，直接读就行。\n\n']
    atomic_write(out / '索引.md', ''.join(index))
    tar = out.with_suffix('.tar.gz')
    with tarfile.open(tar, 'w:gz') as tf:
        tf.add(out, arcname=out.name)
    print(f'导出：{out}\n打包：{tar}')
    return 0


def cmd_resume(_a) -> int:
    try:
        st = json.loads(STATE.read_text(encoding='utf-8'))
    except Exception:
        print('没有状态文件')
        return 1
    for r in st.get('resume', [])[:5]:
        print(f"- {r.get('at','')[:16]} {r.get('title','')}")
    return 0


# ---------------------------------------------------------------- 入口

def main() -> int:
    ap = argparse.ArgumentParser(prog='core')
    sub = ap.add_subparsers(dest='cmd', required=True)

    sub.add_parser('init').set_defaults(fn=cmd_init)
    sub.add_parser('reindex').set_defaults(fn=cmd_reindex)
    sub.add_parser('resume').set_defaults(fn=cmd_resume)

    s = sub.add_parser('store')
    s.add_argument('--title', required=True)
    s.add_argument('--said', default='')
    s.add_argument('--heart', default='')
    s.add_argument('--who', default='用户')
    s.add_argument('--domain', default='未判')
    s.add_argument('--salience', type=int, default=3)
    s.add_argument('--tags', default='')
    s.add_argument('--occurred', default='')
    s.add_argument('--source', default='')
    s.add_argument('--dry-run', action='store_true')
    s.set_defaults(fn=cmd_store)

    q = sub.add_parser('search')
    q.add_argument('query', nargs='?', default='')
    q.add_argument('--query', dest='query_opt', default='')
    q.add_argument('--limit', type=int, default=5)
    q.add_argument('--json', action='store_true')
    q.add_argument('--hybrid', action='store_true')
    q.set_defaults(fn=cmd_search)

    g = sub.add_parser('get')
    g.add_argument('name')
    g.add_argument('--max', type=int, default=GET_BYTES)
    g.set_defaults(fn=cmd_get)

    lg = sub.add_parser('log')
    lg.add_argument('--session', default='')
    lg.add_argument('--turn', type=int, default=0)
    lg.add_argument('--user', default='')
    lg.add_argument('--assistant', default='')
    lg.add_argument('--tools', type=int, default=0)
    lg.add_argument('--files', default='')
    lg.set_defaults(fn=cmd_log)

    doc = sub.add_parser('doctor')
    doc.set_defaults(fn=cmd_doctor)

    d = sub.add_parser('dream')
    d.add_argument('--json', action='store_true')
    d.set_defaults(fn=cmd_dream)

    v = sub.add_parser('verify')
    v.add_argument('--json', action='store_true')
    v.set_defaults(fn=cmd_verify)

    stt = sub.add_parser('status')
    stt.add_argument('--json', action='store_true')
    stt.set_defaults(fn=cmd_status)

    ex = sub.add_parser('export')
    ex.add_argument('--out', default='')
    ex.set_defaults(fn=cmd_export)

    a = ap.parse_args()
    return a.fn(a)


if __name__ == '__main__':
    sys.exit(main())
