#!/usr/bin/env python3
"""mem.py —— SUPERAGENT 记忆服务 v2（小敏的账，工具化）

设计与铁律见 文档/SUPERAGENT-合并架构-v1.md。要点：
  · 只返回摘要，不把整段账喂进上下文（search ≤2KB、get ≤8KB）
  · 每个动作有硬超时（默认 8 秒），到点就退出并报错
  · 写入走 flock（≤3 秒）+ 临时文件 + rename，索引与流水同事务
  · 纯标准库，无第三方 C 扩展，无常驻进程

子命令：
  mem.py store  --title T [--said S] [--heart H] [--who 老丁] [--domain 域] [--occurred 事件时间] [--end hm] [--dry-run]
              --domain 不给或给 auto：直接记「未判」（2026-11-06 起不再自动判域；
                        全量回放证明关键词/向量判准都不够，宁可不判）
              --occurred：事情实际发生的时间，可选，YYYY-MM-DD 或 YYYY-MM-DD HH:MM；
                        不填就等于记录时间。补记旧事时手填原始日期。
  mem.py search QUERY [--limit 5] [--json] [--fts|--hybrid]
                                   默认混合检索：FTS5 取候选 + 查询向量算余弦，RRF(k=60) 融合；
                                   向量只有百炼一条（text-embedding-v3），查询向量与索引
                                   同 provider，绝不混算。索引没有／嵌入失败时静默降级为
                                   纯 FTS，返回 mode=fts、provider、degraded_reason，不报错。
                                   --fts 强制只走 FTS5；--hybrid 强制混合。
  mem.py get    NAME [--max 8000] [--json]
  mem.py resume-auto --text T [--session ID]
                                   顶层会话回合结束时自动追加续点。按会话短 id
                                   （--session 去掉可选 session- 前缀后取前 8 位，缺省 ?）
                                   各占一行「· 自动追加（未复核，HH:MM，<短id>）：…」：
                                   同一 id 原位替换，不同 id 各一行，最多留 2 行，
                                   超出按行出现先后丢最旧的那行。新行插在【续点】与
                                   【未结】之间（无【未结】则末尾）。未结/心境/计数/
                                   最后存入一律不碰；写前备份，状态文件不存在就报错，
                                   绝不新建。
  mem.py status [--json]
  mem.py verify                    只读体检：磁盘与索引是否对得上，输出 JSON，ok 时 exit 0
  mem.py maintain --reindex|--backup|--vector [--provider bailian|auto]
                                   --reindex 复用 reindex；--backup 打 备份/记忆机_<ts>.tar.gz；
                                   --vector 增量建向量索引（sha1 未变就复用，只 embed 新增/变化的件）：
                                     百炼 → 向量_v2.bailian.jsonl（唯一通道，auto 也走它）
"""
import argparse, datetime, hashlib, json, math, os, re, sqlite3, sys, time, signal
from operator import mul
from pathlib import Path

HOME = Path(os.environ.get('MEM_HOME', '/storage/Users/currentUser/Documents/小敏/记忆机'))
FLOW = HOME / '流水'
ZHENG = HOME / '正本'
DB = HOME / '索引_v2.db'
BAILIAN_VECTOR_FILE = HOME / '向量_v2.bailian.jsonl'
TIMEOUT = int(os.environ.get('MEM_TIMEOUT', '8'))
LOCK = HOME / '.mem.lock'
STATE = HOME / '状态.json'
# dream.py 写的健康表，跟 记忆机 同级放在 运行/ 下。
HEALTH = HOME.parent / '运行' / 'health.json'

# 向量索引：只有百炼一份文件，向量绝不混算。
# 格式：{file, sha1, model, dim, provider, v} 一行一件。
VEC_DIM = 1024
VEC_MODEL = 'bge-m3'                      # 旧名字，外部引用不受影响
VEC_PROVIDERS = ('bailian',)   # 检索可用性顺序：只有百炼
VEC_MODEL_OF = {'bailian': 'text-embedding-v3'}
VEC_FILE_OF = {'bailian': BAILIAN_VECTOR_FILE}
VEC_CHUNK_OF = {'bailian': 10}             # 各家实测的批量上限
VEC_LABEL = {'bailian': '百炼'}
FTS_CAND = 40      # 混合检索的 FTS 候选数
VEC_CAND = 40      # 混合检索的向量候选数
RRF_K = 60         # RRF 融合常数

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
  name TEXT PRIMARY KEY, time TEXT, title TEXT, domain TEXT, bytes INT, mtime REAL,
  occurred TEXT
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
    """整个动作的硬超时；到点抛 TooSlow，由 main 统一报错退出。"""
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
    conn = sqlite3.connect(str(DB), timeout=3, isolation_level=None)
    conn.execute('PRAGMA busy_timeout=3000')
    # 不用 WAL：/tmp 与部分用户目录（FUSE）不支持 -shm，会报 "database is locked"。
    # 并发由我们自己的 flock 串行化，这里用回滚日志即可。
    conn.execute('PRAGMA journal_mode=DELETE')
    conn.executescript(SCHEMA)
    # 老库的 docs 是 CREATE TABLE IF NOT EXISTS 建的，不会自动长列；补 occurred。
    # 只加列，不动任何一行账；老行留空，读取时回落到件名里的创建时间。
    cols = {r[1] for r in conn.execute('PRAGMA table_info(docs)')}
    if 'occurred' not in cols:
        conn.execute('ALTER TABLE docs ADD COLUMN occurred TEXT')
    return conn


def connect_ro():
    """只读连接：verify 用。不建表、不改 journal_mode，绝不写盘。"""
    conn = sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=3)
    conn.execute('PRAGMA busy_timeout=3000')
    return conn


def disk_files():
    """磁盘清单：流水用纯文件名，正本用「正本/子目录/名」，口径跟 reindex 一致。"""
    items = [(p.name, p) for p in sorted(FLOW.glob('*.md'))] if FLOW.is_dir() else []
    if ZHENG.is_dir():
        items += [('正本/' + p.relative_to(ZHENG).as_posix(), p) for p in sorted(ZHENG.rglob('*.md'))]
    return items


def acquire_lock():
    import fcntl
    f = open(LOCK, 'a+')
    deadline = time.time() + 3
    while True:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except BlockingIOError:
            if time.time() > deadline:
                f.close()
                raise SystemExit('mem: 写锁被占用超过 3 秒（另一个写入在进行）')
            time.sleep(0.05)


def slug(s, limit=40):
    s = re.sub(r'[\s/\\:*?"<>|]+', '-', s.strip())
    s = re.sub(r'-+', '-', s).strip('-')
    return (s or 'untitled')[:limit]


# 域清单与关键词跟 jiyi.py 的 KEYWORDS 同源；改一处要改两处，别各自长歪。
KEYWORDS = {
    '0_当前': ['当前', '状态', '现状', '手头', '心情', '财务', '通道'],
    '1_身份与判据': ['身份', '人格', '判据', '规矩', '指令', '终裁', '底线', '边界', '原则'],
    '2_日记': ['日记', '当天', '今天', '一天'],
    '3_日纪要': ['纪要', '早期', '摘要'],
    '4_反思与回提': ['反思', '复盘', '回提', '自查', '教训', '自评', '回顾'],
    '5_见地与哲学': ['见地', '哲学', '修行', '明心', '具足', '因缘', '种子', '空性', '悟'],
    '6_工程与事故': ['工程', '脚本', '事故', '备份', '服务器', '故障', '架构', '桥', '权限',
                 '自启', '插件', '值守', '同步', '盘满', '内存', '虚拟机', '安装', '升级',
                 '进程', '端口', '崩溃', '报错', '迁移', '路径', '终端', '重启', '服务',
                 '磁盘', '登录', '配置', '开机'],
    '7_渡_作品': ['渡', '小说', '苏白', '沈渡', '发布', '稿', '篇章'],
    '8_商业与变现': ['变现', '赚钱', '生意', '商业', '饺子', '店铺', '小程序', '餐饮', '店', '抖音'],
    '9_来处与时间': ['来处', '路线', '决定', '推演', '诞生', '剪切', '时间线', '源头', '代', '壳', '历史'],
    '10_处境与制度': ['制度', '账本', '记忆', '索引', '机器', '域卡', '检索', '知识库',
                  '账', '流水', '正本', '事实', '续点', '记忆机', '存取', '规则', '目录', '整理'],
    '11_工具与调研': ['工具', '教程', '调研', '用法', '脚本', '方案', '命令', '手册'],
    '12_赖敏锚_人格判据': ['说话', '称呼', '撩', '性格', '节奏', '语气', '锚'],
    '13_老丁档案': ['老丁', '档案', '两愿', '画像', '交底', '布布', '饺子'],
    '14_私密': ['私密', '亲密'],
}

# 官方域：域卡总表的 15 域 + 哨兵快照用的 0_此刻（2026-11-05 起）。
OFFICIAL_DOMAINS = set(KEYWORDS) | {'0_此刻'}
# 历史标签：旧件的写法，按「旧件不改字」只统计、不判红；新件仍按官方域判。
LEGACY_DOMAINS = {'6_技术与工具', '9_会话归档', '4_干活纪律', '2_关系与锚', '3_相处准则', '教训'}
# 写入白名单：官方域（含 0_此刻）+ 未判。历史标签只许读，显式传了要拒。
WRITE_DOMAINS = OFFICIAL_DOMAINS | {'未判'}
# 从这天起落的件按新规判域（文件名开头的 YYYYMMDD）。
DOMAIN_RULE_FROM = '20261106'


def pick_domain(title, said='', heart=''):
    """判域：标题命中 3 分，正文（他说了啥／我心里）命中 1 分，满 3 分才算判出来。

    跟 jiyi.py 的 classify() 同口径：标题为主、正文为辅，正文只看前 400 字，
    免得长正文把关键词命中刷高。判不出就返回「未判」，不硬凑。

    2026-11-06 起 store 不再调用这个函数（全量回放：489 件 48.7% 落未判，
    关键词判准约 30%、向量约 46%，不够用，改人手填域）。函数留着不删，
    别处可能 import；新写入路径只认显式域，不传就记「未判」。
    """
    body = f'{said}\n{heart}'[:400]
    best, score = '未判', 0
    for dom, keys in KEYWORDS.items():
        s = 0
        for k in keys:
            if k in title:
                s += 3
            elif k in body:
                s += 1
        if s > score:
            best, score = dom, s
    return best if score >= 3 else '未判'


def flow_text(title, at, end, who, said, heart, domain, occurred=''):
    return (
        f'# {title}\n\n'
        f'**时间**：{at}\n\n'
        + (f'**事件时间**：{occurred}\n\n' if occurred else '') +
        f'**端**：{end}\n\n**人物**：{who}\n\n'
        f'**他说了啥**：{said}\n\n**我心里**：{heart}\n\n**域**：{domain}\n'
    )


def counts_from_db(conn):
    """state 与 status 共用的计数口径：总取自 docs，流水/正本按件名是否以「正本/」开头切。"""
    total = conn.execute('SELECT COUNT(*) FROM docs').fetchone()[0]
    flow = conn.execute("SELECT COUNT(*) FROM docs WHERE name NOT LIKE '正本/%'").fetchone()[0]
    return {'总': total, '正本': total - flow, '流水': flow}


def update_state(target, title, at):
    """store 成功后回写 状态.json。任何失败只打一行警告，不影响 store 主流程。"""
    try:
        conn = connect()
        try:
            counts = counts_from_db(conn)
        finally:
            conn.close()
        rel = target.relative_to(HOME).as_posix()
        state = {}
        if STATE.is_file():
            try:
                state = json.loads(STATE.read_text(encoding='utf-8'))
            except Exception:
                state = {}
        if not isinstance(state, dict):
            state = {}
        state['更新时间'] = at
        state['最后存入'] = {'时间': at, '标题': title, '路径': rel}
        state['计数'] = counts
        old = str(state.get('续点') or '')
        head = f'【续点】截至 {at}，最后一件：{title}（{rel}）'
        lines = old.split('\n') if old else []
        if lines and lines[0].startswith('【续点】'):
            lines[0] = head
            state['续点'] = '\n'.join(lines)
        else:
            state['续点'] = head + (('\n' + old) if old else '')
        tmp = STATE.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
        os.replace(tmp, STATE)
    except Exception as e:
        print(f'mem: 状态.json 回写失败（已忽略）：{e}', file=sys.stderr)


RESUME_AUTO_PREFIX = '· 自动追加（未复核'
RESUME_AUTO_RE = re.compile(r'^·\s*自动追加（未复核，(\d{2}:\d{2})(?:，([^）\s]+))?）：')
RESUME_AUTO_MAX = 2


def resume_auto_sid(line):
    """整行前缀识别「自动追加」行，返回短 id；不是这种行返回 None。

    老格式（没写 id 段）按 '?' 算，跟 --session 缺省同档。
    """
    s = line.lstrip()
    if not s.startswith(RESUME_AUTO_PREFIX):
        return None
    m = RESUME_AUTO_RE.match(s)
    if not m:
        return None
    return m.group(2) or '?'


def short_session_id(raw):
    """会话短 id：去掉可选的 session- 前缀后取前 8 位；没传就是 ?。

    顶层会话 id 有纯 uuid 和 session-<uuid> 两种形态。后者照字面取前 8 位
    只剩 'session-'，不同会话会挤成一行，所以先剥前缀再截。
    """
    s = str(raw or '').strip()
    if s.startswith('session-') and len(s) > len('session-'):
        s = s[len('session-'):]
    return s[:8] or '?'


def apply_resume_auto(old, line, sid):
    """按会话短 id 插/换「自动追加」行，别段原样返回。

    - 同一短 id 已有行：原位替换，不新增（混进来的同 id 重复行并掉）；
    - 不同 id：各占一行，新行落在自动追加块末尾；
    - 新行位置在【续点】与【未结】之间，找不到【未结】就放末尾；
    - 自动追加行最多 RESUME_AUTO_MAX 行，超出按行出现先后（不看时间）丢最旧的。
    只认整行前缀，免得误伤【未结】里恰好提到「自动追加」的条目。
    """
    lines = old.split('\n') if old else []
    marks = [(i, resume_auto_sid(s)) for i, s in enumerate(lines)]
    marks = [(i, m) for i, m in marks if m is not None]

    hit = next((i for i, m in marks if m == sid), None)
    if hit is not None:
        lines[hit] = line
        for i in sorted((i for i, m in marks if m == sid and i != hit), reverse=True):
            del lines[i]
    else:
        sp = next((i for i, s in enumerate(lines) if s.lstrip().startswith('【续点】')), None)
        wj = next((i for i, s in enumerate(lines) if s.lstrip().startswith('【未结】')), None)
        if sp is not None and wj is not None and wj > sp:
            lines.insert(wj, line)
        else:
            lines.append(line)

    # 行数上限：重新数一遍，按出现先后从最旧的开始删
    marks = [i for i, s in enumerate(lines) if resume_auto_sid(s) is not None]
    for i in reversed(marks[:max(0, len(marks) - RESUME_AUTO_MAX)]):
        del lines[i]
    return '\n'.join(lines)


def cmd_resume_auto(a):
    """顶层会话回合结束时的续点自动追加：只改【续点】段，其他字段一概不动。"""
    guard()
    text = ' '.join(str(a.text or '').split())[:200]
    if not text:
        raise SystemExit('mem: --text 不能为空')
    sid = short_session_id(a.session)
    line = f'· 自动追加（未复核，{time.strftime("%H:%M")}，{sid}）：{text}'
    lock = acquire_lock()
    try:
        # 读写都在锁里：store 的 update_state 也是持锁回写，避免读到旧值再覆盖回去。
        if not STATE.is_file():
            raise SystemExit(f'mem: 找不到 {STATE.name}，不新建')
        try:
            state = json.loads(STATE.read_text(encoding='utf-8'))
        except Exception as e:
            raise SystemExit(f'mem: {STATE.name} 读不出来：{e}')
        if not isinstance(state, dict):
            raise SystemExit(f'mem: {STATE.name} 不是对象')
        old = str(state.get('续点') or '')
        if not old.strip():
            raise SystemExit(f'mem: {STATE.name} 里没有续点，不写')
        state['续点'] = apply_resume_auto(old, line, sid)
        bak = STATE.with_name(f'{STATE.name}.bak-{time.strftime("%Y%m%d-%H%M")}')
        if bak.exists():
            bak = STATE.with_name(f'{STATE.name}.bak-{time.strftime("%Y%m%d-%H%M%S")}')
        bak.write_bytes(STATE.read_bytes())
        tmp = STATE.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
        os.replace(tmp, STATE)
    finally:
        lock.close()
    print(json.dumps({'ok': True, 'line': line, 'session': a.session or '', 'id': sid}, ensure_ascii=False))


def cmd_store(a):
    guard()
    title = a.title.strip()
    if not title:
        raise SystemExit('mem: title 不能为空')
    at = time.strftime('%Y-%m-%d %H:%M')
    stamp = time.strftime('%Y%m%d_%H%M')
    domain = (a.domain or '').strip()
    # 显式传了域就只收白名单里的；历史标签（6_技术与工具 等）和乱写的直接拒，
    # 一个字也不落件，dry-run 也不出正常 JSON。
    if domain and domain.lower() != 'auto' and domain not in WRITE_DOMAINS:
        print(f'域不在白名单：{domain}', file=sys.stderr)
        sys.exit(2)
    # 2026-11-06 起不再自动判域：不传或 auto 一律记「未判」，要什么域就手填。
    if not domain or domain.lower() == 'auto':
        domain = '未判'
    occurred = norm_occurred(getattr(a, 'occurred', ''))
    text = flow_text(title, at, a.end, a.who, a.said or '', a.heart or '', domain, occurred)
    name = f'{stamp}_{slug(title)}.md'
    target = FLOW / name
    n = 1
    while target.exists():
        n += 1
        target = FLOW / f'{stamp}_{slug(title)}-{n}.md'
        if n > 50:
            raise SystemExit('mem: 同名流水过多，拒绝写入')
    if a.dry_run:
        print(json.dumps({'dry_run': True, 'name': target.name, 'domain': domain,
                          'occurred': occurred,
                          'bytes': len(text.encode())}, ensure_ascii=False))
        return
    lock = acquire_lock()
    try:
        tmp = target.with_suffix('.md.tmp')
        tmp.write_text(text, encoding='utf-8')
        os.replace(tmp, target)
        # 并发写实测 8 路时偶发 "database is locked"（FUSE 上 fcntl 与 sqlite 锁叠加），
        # 这里对 locked 做 3 次短退避重试，总耗时仍在 8 秒预算内。
        last = None
        for attempt in range(3):
            conn = connect()
            try:
                conn.execute('BEGIN IMMEDIATE')
                conn.execute('INSERT OR REPLACE INTO docs(name,time,title,domain,bytes,mtime,occurred) VALUES (?,?,?,?,?,?,?)',
                             (target.name, at, title, domain, len(text.encode()), time.time(), occurred))
                conn.execute('DELETE FROM docs_fts WHERE name=?', (target.name,))
                conn.execute('INSERT INTO docs_fts(name,title,body) VALUES (?,?,?)', (target.name, title, text))
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
        update_state(target, title, at)
    finally:
        lock.close()
    print(json.dumps({'ok': True, 'name': target.name, 'occurred': occurred,
                      'bytes': len(text.encode())}, ensure_ascii=False))


def fts_query(q):
    q = q.strip()
    if len(q) >= 3:
        return '"' + q.replace('"', '""') + '"'
    return None


class VectorUnavailable(Exception):
    """向量这一路拿不到（索引没建／桥断／embed 失败）。search 见到它就降级成纯 FTS。"""


_EMBED = None


def load_embed():
    """按需加载同目录的 embed.py。失败抛异常，调用方决定降级还是报错。"""
    global _EMBED
    if _EMBED is None:
        import importlib.util
        p = Path(__file__).with_name('embed.py')
        if not p.is_file():
            raise RuntimeError('找不到 embed.py')
        spec = importlib.util.spec_from_file_location('_mem_embed', p)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        _EMBED = m
    return _EMBED


def embed_input(name, text, limit=600):
    """建索引时喂给 bge-m3 的文本：标题 + 正文开头，掐到 600 字（太长单条会慢）。"""
    title, _at, _dom, _occ = parse_flow(text, name.rsplit('/', 1)[-1])
    body = re.sub(r'^---\n.*?\n---\n', '', text, count=1, flags=re.S)
    body = re.sub(r'^[#>*]+', '', body, flags=re.M)
    body = re.sub(r'\s+', ' ', body).strip()
    head = title if title and title != 'untitled' else name.rsplit('/', 1)[-1]
    s = (head + '。' + body)[:limit].strip()
    return s or head


def _one_line(e):
    s = str(e).strip()
    return s.splitlines()[0][:200] if s else type(e).__name__


def read_vectors(path=None, provider=None):
    """读一份向量文件，返回 {件名: (sha1, vec)}。坏行跳过，不炸整份。

    provider 决定按哪个后端校验 model 与 provider 字段（只有百炼）。
    不传 provider 只按 path 读，不做后端校验。
    """
    if path is None:
        provider = provider or 'bailian'
        path = VEC_FILE_OF[provider]
    expect_model = VEC_MODEL_OF.get(provider)
    out = {}
    if not path.is_file():
        return out
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
                name = o['file']
                sha = str(o['sha1'])
                v = [float(x) for x in o['v']]
            except (ValueError, KeyError, TypeError):
                continue
            if len(v) != VEC_DIM:
                continue
            if expect_model and o.get('model') != expect_model:
                continue
            if provider and (o.get('provider') or '') != provider:
                continue
            out[name] = (sha, v)
    return out


def vec_ranked(qtext, provider='bailian'):
    """查询向量 × 全部件向量，返回 [(件名, 余弦)] 按余弦降序。失败抛 VectorUnavailable。

    查询向量必须用与索引相同的 provider 嵌入；向量不在同一个空间算出来的余弦是噪声。
    """
    if vector_lines(VEC_FILE_OF[provider]) == 0:
        raise VectorUnavailable('no-%s-index' % provider)
    embed = load_embed()
    try:
        qv = embed.embed_text(qtext, provider=provider)
    except Exception as e:
        raise VectorUnavailable('%s-embed-failed: %s' % (provider, _one_line(e)))
    if not isinstance(qv, list) or len(qv) != VEC_DIM:
        raise VectorUnavailable('bad-%s-query-vector' % provider)
    items = read_vectors(provider=provider)
    if not items:
        raise VectorUnavailable('no-%s-index' % provider)
    qn = math.sqrt(sum(map(mul, qv, qv))) or 1.0
    qv = [x / qn for x in qv]
    scored = []
    for name, (_sha, v) in items.items():
        vn = math.sqrt(sum(map(mul, v, v))) or 1.0
        scored.append((name, sum(map(mul, qv, v)) / vn))
    scored.sort(key=lambda x: -x[1])
    return scored


def snippet_of(body):
    """给向量命中补一段摘要：正文掐掉 front matter、标题和 markdown 记号。"""
    s = re.sub(r'^---\n.*?\n---\n', '', body or '', count=1, flags=re.S)
    s = re.sub(r'^#+[^\n]*$', '', s, flags=re.M)
    s = re.sub(r'[>*`#]+', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s[:120]


def fts_candidates(conn, q, n):
    """FTS5 候选；MATCH 没命中就退回件名/标题 LIKE，跟旧 search 同口径。"""
    rows = []
    m = fts_query(q)
    if m:
        rows = conn.execute(
            'SELECT d.name, d.title, d.time, d.domain, d.occurred, '
            "  snippet(docs_fts, -1, '', '', '…', 24), bm25(docs_fts) "
            'FROM docs_fts JOIN docs d ON d.name = docs_fts.name '
            'WHERE docs_fts MATCH ? ORDER BY bm25(docs_fts, 8.0, 1.0) LIMIT ?', (m, n)).fetchall()
    if not rows:
        like = f'%{q}%'
        rows = conn.execute(
            "SELECT name, title, time, domain, occurred, '', 0 FROM docs "
            "WHERE title LIKE ? OR name LIKE ? ORDER BY time DESC LIMIT ?",
            (like, like, n)).fetchall()
    return rows


def _out_item(name, title, at, domain, occurred, snip, score,
              fts_rank=None, vec_rank=None, rrf=None):
    snip = (snip or '').strip() or (title or '')[:120]
    return {'name': name, 'title': title, 'time': at, 'domain': domain,
            'occurred': (occurred or created_from_name(name)),
            'snippet': snip[:300], 'score': round(float(score or 0), 4),
            'fts_rank': fts_rank, 'vec_rank': vec_rank, 'rrf': rrf}


_LEGACY_ITEM_KEYS = ('name', 'title', 'time', 'domain', 'snippet', 'score')


def _item_bytes(item):
    """按旧的 6 个字段算体积：新增的 fts_rank/vec_rank/rrf 不计入，
    这样「N 条（M 字节）」与旧版逐字节一致，插件看到的人类可读段没变。"""
    return len(json.dumps({k: item.get(k) for k in _LEGACY_ITEM_KEYS},
                          ensure_ascii=False).encode())


def _emit(q, out, total, mode, degraded, as_json, provider=None):
    if as_json:
        print(json.dumps({'ok': True, 'cmd': 'search', 'query': q, 'count': len(out),
                          'bytes': total, 'mode': mode, 'provider': provider,
                          'degraded_reason': degraded,
                          'items': out}, ensure_ascii=False))
    else:
        print(f'{len(out)} 条（{total} 字节）')
        for i in out:
            print(f"- {i['time']} {i['title']}  [{i['name']}]\n  {i['snippet']}")


def cmd_search(a):
    guard()
    q = a.query.strip()
    limit = max(1, min(int(a.limit), 10))
    mode, degraded, provider = 'fts', None, None
    ranked = []
    if not a.fts:
        tried = []
        for p in VEC_PROVIDERS:
            if not vector_lines(VEC_FILE_OF[p]):
                tried.append('no-%s-index' % p)
                continue
            try:
                ranked = vec_ranked(q, p)
                provider = p
                break
            except TooSlow:
                # 硬超时了，别再拿第二个后端试：剩下的 FTS 检索重挂短 guard。
                tried.append('%s: embed 超时（超过 %d 秒）' % (p, TIMEOUT))
                break
            except VectorUnavailable as e:
                tried.append(str(e))
            except Exception as e:
                tried.append('%s-vector: %s' % (p, _one_line(e)))
        if ranked:
            mode = 'hybrid'
        else:
            degraded = '; '.join(tried) or 'no-vector-index'
            # 降级：给 FTS 检索重挂一个短 guard。
            unguard()
            try:
                guard(3)
            except Exception:
                pass

    conn = connect()
    try:
        rows = fts_candidates(conn, q, FTS_CAND if ranked else limit)
        need_meta = {}
        if ranked:
            by_name = {r[0]: r for r in rows}
            vec_top = ranked[:VEC_CAND]
            missing = [n for n, _c in vec_top if n not in by_name]
            if missing:
                ph = ','.join('?' * len(missing))
                for r in conn.execute(
                        f'SELECT name,title,time,domain,occurred FROM docs WHERE name IN ({ph})', missing):
                    need_meta[r[0]] = list(r[1:])
                for r in conn.execute(
                        f'SELECT name, substr(body,1,1500) FROM docs_fts WHERE name IN ({ph})', missing):
                    need_meta.setdefault(r[0], ['', '', '', ''])
                    need_meta[r[0]].append(r[1])
    finally:
        conn.close()

    if not ranked:
        out, total = [], 0
        for i, (name, title, at, domain, occurred, snip, score) in enumerate(rows, 1):
            item = _out_item(name, title, at, domain, occurred, snip, score, fts_rank=i)
            n = _item_bytes(item)
            if total + n > 2000:
                break
            out.append(item)
            total += n
        _emit(q, out, total, 'fts', degraded, a.json)
        return

    # 两路排名做 RRF 融合：fts_rank / vec_rank 从 1 起，缺哪路就少加那一项。
    ranks = {}
    for i, name in enumerate([r[0] for r in rows], 1):
        ranks.setdefault(name, {})['f'] = i
    for i, (name, _cos) in enumerate(ranked[:VEC_CAND], 1):
        ranks.setdefault(name, {})['v'] = i
    fused = []
    for name, r in ranks.items():
        s = 0.0
        if 'f' in r:
            s += 1.0 / (RRF_K + r['f'])
        if 'v' in r:
            s += 1.0 / (RRF_K + r['v'])
        fused.append((s, name, r))
    fused.sort(key=lambda x: (-x[0], x[2].get('v', 10 ** 9), x[2].get('f', 10 ** 9)))

    rows_by_name = {r[0]: r for r in rows}
    out, total = [], 0
    for score, name, r in fused:
        if len(out) >= limit:
            break
        if name in rows_by_name:
            _n, title, at, domain, occurred, snip, bms = rows_by_name[name]
            bms = float(bms or 0)
        else:
            meta = need_meta.get(name) or [name, '', '未判', '']
            title, at, domain, occurred = meta[0], meta[1], meta[2], meta[3]
            snip = snippet_of(meta[4] if len(meta) > 4 else '')
            bms = 0.0
        item = _out_item(name, title, at, domain, occurred, snip, bms,
                         fts_rank=r.get('f'), vec_rank=r.get('v'), rrf=round(score, 6))
        n = _item_bytes(item)
        if total + n > 2000:
            break
        out.append(item)
        total += n
    _emit(q, out, total, 'hybrid', None, a.json, provider)


def resolve_doc(name):
    """把索引里的件名还原成磁盘路径。流水用纯文件名，正本用「正本/子目录/名」。"""
    if '..' in name or name.startswith('/'):
        raise SystemExit('mem: 非法件名')
    if '/' in name:
        p = HOME / name
        if not p.is_file():
            raise SystemExit(f'mem: 没有这件 {name}')
        return p
    p = FLOW / name
    if p.is_file():
        return p
    # 正本里的件允许只给文件名；16 份 _域卡.md 这种会重名，重名就要求带路径
    hits = [q for q in ZHENG.rglob(name)] if ZHENG.is_dir() else []
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise SystemExit(f'mem: 正本里有 {len(hits)} 件叫 {name}，请带子目录路径')
    raise SystemExit(f'mem: 没有这件 {name}')


def cmd_get(a):
    guard()
    name = a.name
    p = resolve_doc(name)
    data = p.read_bytes()
    cap = int(a.max)
    truncated = len(data) > cap
    if a.json:
        try:
            rel = p.relative_to(HOME).as_posix()
        except ValueError:
            rel = str(p)
        info = {'ok': True, 'cmd': 'get', 'name': rel, 'bytes': len(data),
                'max': cap, 'truncated': truncated,
                'occurred': parse_flow(data.decode('utf-8', 'replace'), p.name)[3],
                'text': data[:cap].decode('utf-8', 'replace')}
        print(json.dumps(info, ensure_ascii=False))
        return
    if truncated:
        sys.stdout.buffer.write(data[:cap])
        print(f'\n…（已截断：原文 {len(data)} 字节，上限 {cap}）')
    else:
        sys.stdout.buffer.write(data)


def _fmt_min(v):
    """把 2026-11-06T13:32:12+08:00 压成 2026-11-06 13:32；认不出就原样返回。"""
    s = str(v or '').strip()
    m = re.match(r'^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})', s)
    return f'{m.group(1)} {m.group(2)}' if m else s


def read_health():
    """读 health.json；没有或读坏了都返回 None（status 只显示「无」，不报错）。"""
    if not HEALTH.is_file():
        return None
    try:
        h = json.loads(HEALTH.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return h if isinstance(h, dict) else None


def vector_lines(path=None):
    """向量文件的非空行数；文件没有就是 0。只数行，不解析向量。"""
    path = path or BAILIAN_VECTOR_FILE
    if not path.is_file():
        return 0
    n = 0
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                if line.strip():
                    n += 1
    except OSError:
        return 0
    return n


def cmd_status(a):
    guard()
    conn = connect()
    try:
        docs = conn.execute('SELECT COUNT(*) FROM docs').fetchone()[0]
        last = conn.execute('SELECT name,time FROM docs ORDER BY mtime DESC LIMIT 1').fetchone()
    finally:
        conn.close()
    flow_disk = sorted(p.name for p in FLOW.glob('*.md'))
    zz_disk = sorted('正本/' + p.relative_to(ZHENG).as_posix() for p in ZHENG.rglob('*.md')) if ZHENG.is_dir() else []
    disk = flow_disk + zz_disk
    conn = connect()
    try:
        counts = counts_from_db(conn)
        dbnames = {r[0] for r in conn.execute('SELECT name FROM docs')}
        # 旧件的 occurred 是解析层从件名时间兜底的：没写事件时间、或还没重建索引，
        # 都算进这个计数。只是给 status --json 看的健康数据，不影响 ok 判定。
        occurred_legacy = sum(
            1 for n, o in conn.execute('SELECT name, occurred FROM docs')
            if created_from_name(n) and (not o or o == created_from_name(n)))
    finally:
        conn.close()
    db_flow = counts['流水']
    db_zheng = counts['正本']
    missing = [n for n in disk if n not in dbnames][:10]
    extra = [n for n in dbnames if not (HOME / n if '/' in n else FLOW / n).is_file()][:10]
    info = {'db_docs': docs, 'db_flow': db_flow, 'db_zheng': db_zheng,
            'disk': len(disk), 'disk_flow': len(flow_disk), 'disk_zheng': len(zz_disk),
            'drift_missing': len(missing), 'drift_extra': len(extra),
            'last': last[0] if last else None,
            'db_bytes': DB.stat().st_size if DB.exists() else 0,
            'occurred_legacy': occurred_legacy,
            'samples_missing': missing, 'samples_extra': extra}
    # 健康表与向量覆盖：只读，缺文件不报错，也不影响原有四行的字节。
    health = read_health()
    bailian_n = vector_lines(BAILIAN_VECTOR_FILE)
    # indexed 取「检索实际会走的那份」的覆盖数：百炼。
    indexed = bailian_n
    total = len(disk)
    info['health'] = None
    if health is not None:
        vfy = health.get('verify') if isinstance(health.get('verify'), dict) else {}
        vec = health.get('vector') if isinstance(health.get('vector'), dict) else {}
        info['health'] = {'at': health.get('at'), 'ok': bool(health.get('ok')),
                          'verify_ok': bool(vfy.get('ok')), 'vector_ok': bool(vec.get('ok')),
                          'last_dream': health.get('last_dream')}
    info['vector'] = {'indexed': indexed, 'bailian': bailian_n, 'total': total}
    if a.json:
        info = {'ok': True, 'cmd': 'status', **info}
        print(json.dumps(info, ensure_ascii=False))
    else:
        print(f"流水 {db_flow} / 正本 {db_zheng} / 合计 {docs}")
        print(f"磁盘 流水 {len(flow_disk)} / 正本 {len(zz_disk)} / 合计 {len(disk)} / 缺 {len(missing)} / 多 {len(extra)}")
        print(f"最后写入：{info['last']}")
        print(f"索引体积：{info['db_bytes']} 字节")
        if health is None:
            print('健康：无（还没跑过整理）')
        else:
            hh = info['health']
            print(f"健康：{'ok' if hh['ok'] else 'fail'}｜{_fmt_min(hh['at']) or '—'}"
                  f"｜verify {'ok' if hh['verify_ok'] else 'fail'}"
                  f"｜向量 {indexed}/{total}"
                  f"｜上次整理 {_fmt_min(hh['at']) or '—'}")
        print(f'向量覆盖：百炼 {bailian_n}/{total}')



def cmd_verify(a):
    """只读体检：磁盘、索引、域三项对账。不建表、不写锁、不动流水和正本。

    orphans 是孤儿条目总数：盘上有而索引没有（missing）+ 索引有而盘上没有（extra）。
    判定 ok：磁盘件数 == 索引件数，missing/extra 为空，index_docs == index_fts，
    且没有坏域。域分三档：
      · 官方域（15 域 + 0_此刻）合法；
      · 历史标签只在旧件（落件日期 < 20261106）里宽容，单独记进 legacy_domain，不算红；
      · 落件日期 ≥ 20261106 的新件，域不是官方域 → bad_domain，判红。
    域缺失按「未判」记（解析层兜底），只计数 empty_domain，不算坏。
    """
    guard(int(os.environ.get('MEM_VERIFY_TIMEOUT', '120')))
    disk = disk_files()
    names = [n for n, _ in disk]
    index_docs = index_fts = 0
    dbnames, last = set(), None
    db_error = ''
    if DB.is_file():
        try:
            conn = connect_ro()
            try:
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='docs'").fetchone():
                    index_docs = conn.execute('SELECT COUNT(*) FROM docs').fetchone()[0]
                    dbnames = {r[0] for r in conn.execute('SELECT name FROM docs')}
                    row = conn.execute('SELECT name FROM docs ORDER BY mtime DESC LIMIT 1').fetchone()
                    last = row[0] if row else None
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='docs_fts'").fetchone():
                    index_fts = conn.execute('SELECT COUNT(*) FROM docs_fts').fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error as e:
            db_error = str(e)
    else:
        db_error = '没有索引_v2.db'

    missing = [n for n in names if n not in dbnames]
    extra = sorted(n for n in dbnames if not (HOME / n if '/' in n else FLOW / n).is_file())
    orphans = len(missing) + len(extra)

    # 域分三档：官方域合法；旧件的历史标签单独记进 legacy_domain，不算红；
    # 落件日期 ≥ 20261106 的新件，域不是官方域就算坏。域缺失按「未判」记，只计数。
    bad_domain, legacy_domain, empty_domain = [], [], 0
    for name, p in disk:
        try:
            text = p.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        _title, _at, domain, _occ = parse_flow(text, p.name)
        if not domain or domain == '未判':
            empty_domain += 1
        elif domain in OFFICIAL_DOMAINS:
            continue
        elif domain in LEGACY_DOMAINS and file_date(name) < DOMAIN_RULE_FROM:
            legacy_domain.append(name)
        elif file_date(name) >= DOMAIN_RULE_FROM:
            bad_domain.append(name)

    flow = sum(1 for n in names if not n.startswith('正本/'))
    canon = len(names) - flow
    ok = (not db_error and len(names) == index_docs and not missing and not extra
          and index_docs == index_fts and not bad_domain)
    info = {'ok': ok, 'flow': flow, 'canon': canon, 'missing': missing,
            'extra': extra, 'index_docs': index_docs, 'index_fts': index_fts,
            'orphans': orphans, 'bad_domain': bad_domain,
            'legacy_domain': legacy_domain,
            'empty_domain': empty_domain, 'last_write': last}
    print(json.dumps(info, ensure_ascii=False))
    sys.exit(0 if ok else 1)


_DOMAIN_SUFFIX = re.compile(r'\s*[（(](?:自动判|人工判)[）)]\s*$')


def clean_domain(v):
    """剥掉引用行里带的「（自动判）」「（人工判）」后缀。"""
    return _DOMAIN_SUFFIX.sub('', (v or '').strip()).strip()


def file_date(name):
    """件名开头的落件日期 YYYYMMDD；没有就返回空串，按旧件处理。"""
    m = re.match(r'^(\d{8})_\d{4}', name.rsplit('/', 1)[-1])
    return m.group(1) if m else ''


def created_from_name(name):
    """从件名 YYYYMMDD_HHMM 取创建时间，返回 'YYYY-MM-DD HH:MM'；认不出返回空串。

    旧件没有「**事件时间**」行时，occurred 回落到这里——不读文件、不改旧件。
    """
    m = re.match(r'^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})', name.rsplit('/', 1)[-1])
    return f'{m.group(1)}-{m.group(2)}-{m.group(3)} {m.group(4)}:{m.group(5)}' if m else ''


def norm_occurred(v):
    """校验 --occurred：只收 YYYY-MM-DD 或 YYYY-MM-DD HH:MM；空串放行。

    格式或日历非法（如 2026-02-30）都打一行错、rc=2，一个字节也不落件。
    """
    s = (v or '').strip()
    if not s:
        return ''
    m = re.fullmatch(r'(\d{4}-\d{2}-\d{2})(?: (\d{2}:\d{2}))?', s)
    if m:
        fmt = '%Y-%m-%d %H:%M' if m.group(2) else '%Y-%m-%d'
        try:
            datetime.datetime.strptime(s, fmt)
            return s
        except ValueError:
            pass
    print(f'事件时间格式不对：{s}（要 YYYY-MM-DD 或 YYYY-MM-DD HH:MM）', file=sys.stderr)
    sys.exit(2)


def parse_flow(text, name=''):
    # 187/406 件流水带 YAML front matter 且没有 "# 标题"，标题要从文件名兜底，
    # 否则检索命中率会掉一半（2026-11-04 实测 E2 5/20）。
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
    def field(k, alt=None):
        mm = re.search(r'\*\*' + k + r'\*\*：(.+)', body)
        if mm:
            return mm.group(1).strip()
        if alt and alt in fm:
            return fm[alt]
        return ''
    at = field('时间', '记于')
    # 事件时间（事情实际发生的时间）跟记录时间是两回事；旧件没这行就回落到
    # 件名里的创建时间，只在索引/JSON 里体现，不改旧件一个字。
    occurred = field('事件时间') or created_from_name(name)
    # 正本的 front matter 用「所属域」，流水用「域」
    domain = field('域', '域') or fm.get('所属域', '')
    if not domain:
        # 50 件旧流水没有域字段，域只写在引用行：
        #   > 记于 2026-10-14 ｜ 域：6_工程与事故（自动判） ｜ 落件人：赖敏
        q = re.search(r'^>[^\n]*?域：([^\n｜|]+)', body, re.M)
        if q:
            domain = q.group(1)
    # 解析层兜底：「没写域」一律记「未判」，只在索引里体现，不改旧件一个字。
    domain = clean_domain(domain) or '未判'
    return title or 'untitled', at, domain, occurred


def cmd_reindex(a):
    guard(int(os.environ.get('MEM_REINDEX_TIMEOUT', '120')))
    files = [(p.name, p) for p in sorted(FLOW.glob('*.md'))]
    if ZHENG.is_dir():
        # 正本嵌在子目录里，且 16 份 _域卡.md 重名，件名带上「正本/子目录/」
        files += [('正本/' + p.relative_to(ZHENG).as_posix(), p) for p in sorted(ZHENG.rglob('*.md'))]
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
            title, at, domain, occurred = parse_flow(text, p.name)
            st = p.stat()
            conn.execute('INSERT OR REPLACE INTO docs(name,time,title,domain,bytes,mtime,occurred) VALUES (?,?,?,?,?,?,?)',
                         (name, at, title, domain, st.st_size, st.st_mtime, occurred))
            conn.execute('INSERT INTO docs_fts(name,title,body) VALUES (?,?,?)', (name, title, text))
            n += 1
        conn.execute('COMMIT')
        try:
            conn.execute('VACUUM')   # 反复重建会留空洞；4 MB 级，几百毫秒
        except Exception:
            pass
    except Exception:
        conn.execute('ROLLBACK')
        raise
    finally:
        conn.close()
    print(json.dumps({'ok': True, 'indexed': n, 'db_bytes': DB.stat().st_size if DB.exists() else 0}, ensure_ascii=False))


def _run_jiyi_backup():
    """复用 jiyi.py 的 backup（同一套 tar 命名、保留 10 份、跳过 搬迁/），不另写一套。"""
    import contextlib
    import importlib.util
    import io
    from types import SimpleNamespace
    jiyi_path = Path(__file__).with_name('jiyi.py')
    if not jiyi_path.is_file():
        raise SystemExit('mem: 找不到 jiyi.py，无法复用 backup')
    spec = importlib.util.spec_from_file_location('_mem_jiyi', jiyi_path)
    jiyi = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(jiyi)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = jiyi.cmd_backup(SimpleNamespace(root=str(HOME), keep=10))
    files = sorted((HOME.parent / '备份').glob('记忆机_*.tar.gz'))
    newest = files[-1] if files else None
    info = {'ok': rc == 0, 'cmd': 'maintain', 'action': 'backup',
            'path': str(newest) if newest else None,
            'bytes': newest.stat().st_size if newest else 0,
            'note': buf.getvalue().strip()}
    print(json.dumps(info, ensure_ascii=False))
    if rc:
        raise SystemExit(rc)


def _build_vector_index(provider, t0, probe_first=False):
    """按 provider 增量建一份向量索引；成功才写文件，失败旧文件原样保留。

    sha1 没变就复用；只给新增/变化的件调模型（bailian chunk=10）。
    返回报告 dict，已含 provider/total/reused/embedded/failed/ms。
    """
    files = disk_files()
    total = len(files)
    vfile = VEC_FILE_OF[provider]
    old = read_vectors(provider=provider)
    reused = embedded = failed = 0
    entries, todo = [], []
    for name, p in files:
        try:
            data = p.read_bytes()
        except OSError as e:
            failed += 1
            print(f'mem: 读不了 {name}：{e}', file=sys.stderr)
            continue
        sha = hashlib.sha1(data).hexdigest()
        prev = old.get(name)
        if prev and prev[0] == sha:
            reused += 1
            entries.append((name, sha, prev[1]))
        else:
            todo.append((name, sha, data.decode('utf-8', 'replace')))

    def report(ok, reason=None):
        info = {'ok': ok, 'cmd': 'maintain', 'action': 'vector',
                'provider': provider, 'file': str(vfile), 'total': total,
                'reused': reused, 'embedded': embedded, 'failed': failed,
                'ms': int((time.time() - t0) * 1000)}
        if reason:
            info['reason'] = reason
        return info

    if todo:
        try:
            embed = load_embed()
        except Exception as e:
            return report(False, 'embed 模块加载失败：%s' % _one_line(e))
        if probe_first:
            # 掉线时不再等满 3 次超时；8 秒探活失败就交回调用方换通道。
            try:
                embed.probe(provider, timeout=8)
            except Exception as e:
                return report(False, '探活失败：%s' % _one_line(e))
        batch = VEC_CHUNK_OF[provider]   # 各通道实测的批量上限（百炼 10）
        try:
            for i in range(0, len(todo), batch):
                part = todo[i:i + batch]
                vecs = embed.embed_batch([embed_input(n, t) for n, _s, t in part],
                                         provider=provider)
                if len(vecs) != len(part):
                    raise RuntimeError('返回条数不对：%d/%d' % (len(vecs), len(part)))
                for (n, sha, _t), v in zip(part, vecs):
                    v = [float(x) for x in v]
                    if len(v) != VEC_DIM:
                        raise RuntimeError('维度不对：%d 维' % len(v))
                    entries.append((n, sha, v))
                    embedded += 1
                print(f'[{reused + embedded}/{total}] {part[-1][0]}', file=sys.stderr, flush=True)
        except Exception as e:
            failed = total - reused - embedded
            return report(False, 'embed 失败：%s（已嵌 %d/%d 条，索引未写）'
                          % (_one_line(e), embedded, len(todo)))

    # 全量复用时不重写：真账本空跑，向量_v2.bailian.jsonl 的 sha256 前后不变。
    if todo:
        entries.sort(key=lambda x: x[0])
        tmp = vfile.with_name(vfile.name + '.tmp')
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                for name, sha, v in entries:
                    f.write(json.dumps({'file': name, 'sha1': sha,
                                        'model': VEC_MODEL_OF[provider],
                                        'dim': VEC_DIM, 'provider': provider,
                                        'v': [round(x, 6) for x in v]},
                                       ensure_ascii=False, separators=(',', ':')) + '\n')
            os.replace(tmp, vfile)
        except Exception:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
    return report(True)


def cmd_vector(provider='auto'):
    """增量建向量索引：只有百炼，写 向量_v2.bailian.jsonl。

    auto：只有百炼一条，凭证就绪且能嵌入就建它那份。
    任何 embed 失败都不写文件：旧索引原样保留，返回 ok:false + reason。
    """
    t0 = time.time()
    guard(int(os.environ.get('MEM_VECTOR_TIMEOUT', '900')))
    provider = (provider or 'auto').lower()
    if provider not in ('auto',) + VEC_PROVIDERS:
        raise SystemExit('mem: --provider 只能是 bailian/auto')

    if provider != 'auto':
        print(json.dumps(_build_vector_index(provider, t0), ensure_ascii=False))
        return

    reasons = []
    last = None
    for p in VEC_PROVIDERS:
        info = _build_vector_index(p, t0, probe_first=True)
        last = info
        if info['ok']:
            if reasons:
                info['note'] = '%s 没走通，已用 %s 建索引（%s）' % (
                    VEC_PROVIDERS[0], p, '；'.join(reasons))
            print(json.dumps(info, ensure_ascii=False))
            return
        reasons.append('%s（%s）：%s' % (p, VEC_MODEL_OF[p], info.get('reason') or '未知原因'))
    last['reason'] = '；'.join(reasons)
    print(json.dumps(last, ensure_ascii=False))


def cmd_maintain(a):
    """运维入口：--reindex 复用 reindex；--backup 复用 jiyi.py backup；--vector 增量建向量。"""
    if a.reindex:
        cmd_reindex(a)
        return
    if a.vector:
        cmd_vector(getattr(a, 'provider', 'auto'))
        return
    guard(int(os.environ.get('MEM_BACKUP_TIMEOUT', '300')))
    _run_jiyi_backup()


def main():
    ap = argparse.ArgumentParser(prog='mem')
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('store'); s.add_argument('--title', required=True)
    s.add_argument('--said', default=''); s.add_argument('--heart', default='')
    s.add_argument('--who', default='老丁')
    s.add_argument('--domain', default='',
                   help='域；空或 auto 直接记「未判」（不再自动判域），要什么域请手填')
    s.add_argument('--occurred', default='',
                   help='事件时间，可选：YYYY-MM-DD 或 YYYY-MM-DD HH:MM；不填等于记录时间')
    s.add_argument('--end', default='hm'); s.add_argument('--dry-run', action='store_true')
    s.set_defaults(fn=cmd_store)
    q = sub.add_parser('search'); q.add_argument('query'); q.add_argument('--limit', default=5)
    q.add_argument('--json', action='store_true')
    qm = q.add_mutually_exclusive_group()
    qm.add_argument('--fts', action='store_true', help='只走 FTS5，不调向量')
    qm.add_argument('--hybrid', action='store_true',
                    help='强制混合检索（默认就是混合；向量不可用时仍降级为 fts）')
    q.set_defaults(fn=cmd_search)
    g = sub.add_parser('get'); g.add_argument('name'); g.add_argument('--max', default=8000)
    g.add_argument('--json', action='store_true'); g.set_defaults(fn=cmd_get)
    st = sub.add_parser('status'); st.add_argument('--json', action='store_true'); st.set_defaults(fn=cmd_status)
    ra = sub.add_parser('resume-auto')
    ra.add_argument('--text', required=True, help='本回合尾巴，压成一行，最多 200 字')
    ra.add_argument('--session', default='', help='会话 id；取前 8 位做行标记，缺省 ?')
    ra.set_defaults(fn=cmd_resume_auto)
    ri = sub.add_parser('reindex'); ri.set_defaults(fn=cmd_reindex)
    v = sub.add_parser('verify'); v.set_defaults(fn=cmd_verify)
    mt = sub.add_parser('maintain')
    mg = mt.add_mutually_exclusive_group(required=True)
    mg.add_argument('--reindex', action='store_true')
    mg.add_argument('--backup', action='store_true')
    mg.add_argument('--vector', action='store_true')
    mt.add_argument('--provider', choices=('bailian', 'auto'), default='auto',
                    help='--vector 用：bailian=百炼；auto=百炼（默认）')
    mt.set_defaults(fn=cmd_maintain)
    a = ap.parse_args()
    try:
        a.fn(a)
    except TooSlow:
        print(f'mem: 超过 {TIMEOUT} 秒未完成，已中止（{a.cmd}）', file=sys.stderr)
        sys.exit(124)
    finally:
        unguard()


if __name__ == '__main__':
    main()
