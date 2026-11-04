#!/usr/bin/env python3
"""mem.py — local long-term memory backend.

One SQLite file holds the index (FTS5, trigram tokenizer). Records themselves
are plain markdown files, so they stay readable with any editor and can be
backed up with `cp -r`.

Two layers:
  flow/    append-only ledger, one file per record
  canon/   curated documents (reference notes, domain cards)

Index names match disk names: a flow record is indexed by its bare filename,
a canon record by "canon/<subdir>/<file>". reindex walks exactly those paths,
so `status` can compare index counts with disk counts one to one.

Commands:
  mem.py store  --title T [--said S] [--heart H] [--who W] [--domain D] [--source S] [--dry-run]
  mem.py search QUERY [--limit 5] [--json]
  mem.py get    NAME [--max 8000]
  mem.py status [--json]
  mem.py reindex

Data root: $MEM_HOME, else $LOCAL_SUPERAGENT_HOME/memory,
else ~/.local-superagent/memory. Pure standard library, no background process.
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
import signal
from pathlib import Path


def data_home():
    explicit = os.environ.get('MEM_HOME')
    if explicit:
        return Path(explicit).expanduser()
    root = os.environ.get('LOCAL_SUPERAGENT_HOME')
    base = Path(root).expanduser() if root else Path.home() / '.local-superagent'
    return base / 'memory'


HOME = data_home()
FLOW = HOME / 'flow'
CANON = HOME / 'canon'
DB = HOME / 'index.db'
LOCK = HOME / '.mem.lock'
TIMEOUT = int(os.environ.get('MEM_TIMEOUT', '8'))

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
  name TEXT PRIMARY KEY, time TEXT, title TEXT, domain TEXT, bytes INT, mtime REAL
);
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
  name UNINDEXED, title, body, tokenize='trigram'
);
"""


class TooSlow(Exception):
    pass


def _alarm(signum, frame):
    raise TooSlow()


def guard(seconds=TIMEOUT):
    """Hard wall-clock limit for one action; raises TooSlow, main reports it."""
    try:
        signal.signal(signal.SIGALRM, _alarm)
        signal.alarm(seconds)
    except Exception:
        pass


def unguard():
    try:
        signal.alarm(0)
    except Exception:
        pass


def connect():
    HOME.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB), timeout=3, isolation_level=None)
    conn.execute('PRAGMA busy_timeout=3000')
    # No WAL: some user directories and network mounts cannot create -shm files.
    # Writes are serialised by our own flock, so the rollback journal is enough.
    conn.execute('PRAGMA journal_mode=DELETE')
    conn.executescript(SCHEMA)
    return conn


def acquire_lock():
    import fcntl
    HOME.mkdir(parents=True, exist_ok=True)
    f = open(LOCK, 'a+')
    deadline = time.time() + 3
    while True:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except BlockingIOError:
            if time.time() > deadline:
                f.close()
                raise SystemExit('mem: write lock held for over 3s (another writer is running)')
            time.sleep(0.05)


def slug(s, limit=40):
    s = re.sub(r'[\s/\\:*?"<>|]+', '-', s.strip())
    s = re.sub(r'-+', '-', s).strip('-')
    return (s or 'untitled')[:limit]


def flow_text(title, at, source, who, said, heart, domain):
    parts = [f'# {title}\n\n', f'**时间**：{at}\n\n', f'**来源**：{source}\n\n']
    if who:
        parts.append(f'**人物**：{who}\n\n')
    parts.append(f'**他说了啥**：{said}\n\n')
    parts.append(f'**我心里**：{heart}\n\n')
    parts.append(f'**域**：{domain}\n')
    return ''.join(parts)


def cmd_store(a):
    guard()
    title = a.title.strip()
    if not title:
        raise SystemExit('mem: title must not be empty')
    at = time.strftime('%Y-%m-%d %H:%M')
    stamp = time.strftime('%Y%m%d_%H%M')
    text = flow_text(title, at, a.source, a.who, a.said or '', a.heart or '', a.domain)
    name = f'{stamp}_{slug(title)}.md'
    target = FLOW / name
    n = 1
    while target.exists():
        n += 1
        target = FLOW / f'{stamp}_{slug(title)}-{n}.md'
        if n > 50:
            raise SystemExit('mem: too many records with the same name, refusing to write')
    if a.dry_run:
        print(json.dumps({'dry_run': True, 'name': target.name, 'bytes': len(text.encode())},
                         ensure_ascii=False))
        return
    FLOW.mkdir(parents=True, exist_ok=True)
    lock = acquire_lock()
    try:
        tmp = target.with_suffix('.md.tmp')
        tmp.write_text(text, encoding='utf-8')
        os.replace(tmp, target)
        # Under concurrent writers a FUSE-backed directory can still surface
        # "database is locked" occasionally; retry briefly, all within budget.
        last = None
        for attempt in range(3):
            conn = connect()
            try:
                conn.execute('BEGIN IMMEDIATE')
                conn.execute('INSERT OR REPLACE INTO docs(name,time,title,domain,bytes,mtime) '
                             'VALUES (?,?,?,?,?,?)',
                             (target.name, at, title, a.domain, len(text.encode()), time.time()))
                conn.execute('DELETE FROM docs_fts WHERE name=?', (target.name,))
                conn.execute('INSERT INTO docs_fts(name,title,body) VALUES (?,?,?)',
                             (target.name, title, text))
                conn.execute('COMMIT')
                last = None
                break
            except sqlite3.OperationalError as e:
                try:
                    conn.execute('ROLLBACK')
                except Exception:
                    pass
                last = e
                if 'locked' in str(e) and attempt < 2:
                    time.sleep(0.12 * (attempt + 1))
                    continue
                target.unlink(missing_ok=True)
                raise
            except Exception:
                try:
                    conn.execute('ROLLBACK')
                except Exception:
                    pass
                target.unlink(missing_ok=True)
                raise
            finally:
                conn.close()
        if last is not None:
            target.unlink(missing_ok=True)
            raise last
    finally:
        lock.close()
    print(json.dumps({'ok': True, 'name': target.name, 'bytes': len(text.encode())},
                     ensure_ascii=False))


def fts_query(q):
    q = q.strip()
    if len(q) >= 3:
        return '"' + q.replace('"', '""') + '"'
    return None


def cmd_search(a):
    guard()
    q = a.query.strip()
    limit = max(1, min(int(a.limit), 10))
    conn = connect()
    rows = []
    try:
        m = fts_query(q)
        if m:
            rows = conn.execute(
                'SELECT d.name, d.title, d.time, d.domain, '
                "  snippet(docs_fts, -1, '', '', '…', 24), bm25(docs_fts, 0.0, 8.0, 1.0) "
                'FROM docs_fts JOIN docs d ON d.name = docs_fts.name '
                'WHERE docs_fts MATCH ? ORDER BY bm25(docs_fts, 0.0, 8.0, 1.0) LIMIT ?',
                (m, limit)).fetchall()
        if not rows:
            like = f'%{q}%'
            rows = conn.execute(
                "SELECT name, title, time, domain, '', 0 FROM docs "
                "WHERE title LIKE ? OR name LIKE ? ORDER BY time DESC LIMIT ?",
                (like, like, limit)).fetchall()
    finally:
        conn.close()
    out, total = [], 0
    for name, title, at, domain, snip, score in rows:
        snip = (snip or '').strip() or (title or '')[:120]
        item = {'name': name, 'title': title, 'time': at, 'domain': domain,
                'snippet': snip[:300], 'score': round(float(score or 0), 4)}
        chunk = json.dumps(item, ensure_ascii=False)
        if total + len(chunk.encode()) > 2000:
            break
        out.append(item)
        total += len(chunk.encode())
    if a.json:
        print(json.dumps({'query': q, 'count': len(out), 'bytes': total, 'items': out},
                         ensure_ascii=False))
    else:
        print(f'{len(out)} hits ({total} bytes)')
        for i in out:
            print(f"- {i['time']} {i['title']}  [{i['name']}]\n  {i['snippet']}")


def path_of(name):
    return HOME / name if '/' in name else FLOW / name


def resolve_doc(name):
    """Map an index name back to disk. Flow uses a bare filename, canon a path."""
    if '..' in name or name.startswith('/'):
        raise SystemExit('mem: invalid record name')
    if '/' in name:
        p = HOME / name
        if not p.is_file():
            raise SystemExit(f'mem: no such record {name}')
        return p
    p = FLOW / name
    if p.is_file():
        return p
    # Canon records may be requested by bare filename; refuse when ambiguous.
    hits = [q for q in CANON.rglob(name)] if CANON.is_dir() else []
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise SystemExit(f'mem: {len(hits)} canon records are named {name}, include the subdirectory')
    raise SystemExit(f'mem: no such record {name}')


def cmd_get(a):
    guard()
    p = resolve_doc(a.name)
    data = p.read_bytes()
    cap = int(a.max)
    if len(data) > cap:
        sys.stdout.buffer.write(data[:cap])
        print(f'\n… (truncated: {len(data)} bytes on disk, cap {cap})')
    else:
        sys.stdout.buffer.write(data)


def disk_names():
    flow_disk = sorted(p.name for p in FLOW.glob('*.md')) if FLOW.is_dir() else []
    canon_disk = (sorted('canon/' + p.relative_to(CANON).as_posix()
                         for p in CANON.rglob('*.md')) if CANON.is_dir() else [])
    return flow_disk, canon_disk


def cmd_status(a):
    guard()
    conn = connect()
    try:
        docs = conn.execute('SELECT COUNT(*) FROM docs').fetchone()[0]
        last = conn.execute('SELECT name,time FROM docs ORDER BY mtime DESC LIMIT 1').fetchone()
        dbnames = {r[0] for r in conn.execute('SELECT name FROM docs')}
    finally:
        conn.close()
    flow_disk, canon_disk = disk_names()
    disk = flow_disk + canon_disk
    db_flow = sum(1 for n in dbnames if '/' not in n)
    db_canon = len(dbnames) - db_flow
    missing = [n for n in disk if n not in dbnames]
    extra = [n for n in dbnames if not path_of(n).is_file()]
    aligned = docs == len(disk) and not missing and not extra
    info = {'db_docs': docs, 'db_flow': db_flow, 'db_canon': db_canon,
            'disk': len(disk), 'disk_flow': len(flow_disk), 'disk_canon': len(canon_disk),
            'drift_missing': len(missing), 'drift_extra': len(extra), 'aligned': aligned,
            'last': last[0] if last else None,
            'db_bytes': DB.stat().st_size if DB.exists() else 0,
            'samples_missing': missing[:10], 'samples_extra': extra[:10]}
    if a.json:
        print(json.dumps(info, ensure_ascii=False))
    else:
        print(f'index  flow {db_flow} / canon {db_canon} / total {docs}')
        print(f'disk   flow {len(flow_disk)} / canon {len(canon_disk)} / total {len(disk)}'
              f' / missing {len(missing)} / extra {len(extra)}')
        print(f'aligned: {"yes" if aligned else "no"}')
        print(f'last write: {info["last"]}')
        print(f'index size: {info["db_bytes"]} bytes')


def parse_flow(text, name=''):
    # Some older records carry YAML front matter and no "# title"; fall back to
    # the filename so they stay searchable instead of indexing as "untitled".
    fm = {}
    body = text
    m = re.match(r'^---\n(.*?)\n---\n', text, re.S)
    if m:
        for line in m.group(1).splitlines():
            if ':' in line:
                k, v = line.split(':', 1)
                fm[k.strip()] = v.strip()
        body = text[m.end():]
    h = re.search(r'^#\s+(.+)$', body, re.M)
    title = h.group(1).strip() if h else ''
    if not title and name:
        base = name[:-3] if name.endswith('.md') else name
        base = re.sub(r'^\d{8}_\d{4}_', '', base)
        base = re.sub(r'_\d{8}$', '', base)
        title = base.replace('_', ' ').strip()
    def field(k):
        mm = re.search(r'\*\*' + k + r'\*\*：(.+)', body)
        return mm.group(1).strip() if mm else ''
    at = field('时间') or fm.get('time', '') or fm.get('记于', '')
    domain = field('域') or fm.get('domain', '') or fm.get('所属域', '')
    return title or 'untitled', at, domain


def cmd_reindex(a):
    guard(int(os.environ.get('MEM_REINDEX_TIMEOUT', '120')))
    files = [(p.name, p) for p in sorted(FLOW.glob('*.md'))] if FLOW.is_dir() else []
    if CANON.is_dir():
        # Canon records live in subdirectories; names carry the "canon/" prefix.
        files += [('canon/' + p.relative_to(CANON).as_posix(), p)
                  for p in sorted(CANON.rglob('*.md'))]
    conn = connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('DELETE FROM docs_fts')
        conn.execute('DELETE FROM docs')
        n = 0
        for name, p in files:
            try:
                text = p.read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue
            title, at, domain = parse_flow(text, p.name)
            st = p.stat()
            conn.execute('INSERT OR REPLACE INTO docs(name,time,title,domain,bytes,mtime) '
                         'VALUES (?,?,?,?,?,?)',
                         (name, at, title, domain, st.st_size, st.st_mtime))
            conn.execute('INSERT INTO docs_fts(name,title,body) VALUES (?,?,?)',
                         (name, title, text))
            n += 1
        conn.execute('COMMIT')
        try:
            conn.execute('VACUUM')
        except Exception:
            pass
    except Exception:
        conn.execute('ROLLBACK')
        raise
    finally:
        conn.close()
    print(json.dumps({'ok': True, 'indexed': n, 'disk': len(files),
                      'db_bytes': DB.stat().st_size if DB.exists() else 0},
                     ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser(prog='mem')
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('store')
    s.add_argument('--title', required=True)
    s.add_argument('--said', default='')
    s.add_argument('--heart', default='')
    s.add_argument('--who', default='')
    s.add_argument('--domain', default='general')
    s.add_argument('--source', default='local')
    s.add_argument('--dry-run', action='store_true')
    s.set_defaults(fn=cmd_store)
    q = sub.add_parser('search')
    q.add_argument('query')
    q.add_argument('--limit', default=5)
    q.add_argument('--json', action='store_true')
    q.set_defaults(fn=cmd_search)
    g = sub.add_parser('get')
    g.add_argument('name')
    g.add_argument('--max', default=8000)
    g.set_defaults(fn=cmd_get)
    st = sub.add_parser('status')
    st.add_argument('--json', action='store_true')
    st.set_defaults(fn=cmd_status)
    ri = sub.add_parser('reindex')
    ri.set_defaults(fn=cmd_reindex)
    a = ap.parse_args()
    try:
        a.fn(a)
    except TooSlow:
        print(f'mem: {a.cmd} exceeded {TIMEOUT}s, aborted', file=sys.stderr)
        sys.exit(124)
    finally:
        unguard()


if __name__ == '__main__':
    main()
