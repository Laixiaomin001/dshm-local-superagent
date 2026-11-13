/**
 * dsh-ctx-guard: 顶层会话上下文到 60% 时，往下一步插一条「先做完整记忆」的提醒。
 * 另外两条轻检查：磁盘可用低于 10%、今天新开的子会话超过 20 个。
 * 第四条：health.json 的 at 不是今天，说明 dream 没跑，回合末提醒补跑（6 小时冷却）。
 * 都在同一个 agent/turn-stopping 里，按顺序查，一次最多插三条。
 * 轮末另做一次口吻自检：只看最后一条 assistant 正文，不合格记日志，不插消息。
 * 顶层会话每 6 小时顺手把 3 天前的子会话搬到 /tmp 暂存，不删。
 * 顶层会话每回合把这一回合的对话追加进「接续卡」（环形保留三行），新会话开局能看到刚才在聊什么。
 */
import { createUserMessage } from '@deepseek-ai/dsh-llm'
import {
  existsSync, mkdirSync, readFileSync,
  readdirSync, renameSync, statfsSync, statSync, writeFileSync,
} from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'
import { checkTone, lastAssistantText, recordTone } from './tone.js'

// 引擎自己的家；账本根从 CORE_HOME 展开。两个都能用环境变量覆盖。
const DSH_HOME = process.env.DSH_HOME || join(homedir(), '.dsh')
const CORE_HOME = process.env.CORE_HOME || join(DSH_HOME, 'agent-core')

/**
 * 会话目录按工作区分，目录名是工作区绝对路径的编码（去掉开头的 /，把 / 换成 -，前后各加 --）。
 * DSH_SESSIONS 直接给最终目录；否则按 DSH_WORKSPACE（缺省用当前工作目录）推。
 */
function resolveSessionsDir() {
  if (process.env.DSH_SESSIONS) return process.env.DSH_SESSIONS
  const ws = process.env.DSH_WORKSPACE || process.cwd()
  const slug = '--' + ws.replace(/^\/+/, '').replace(/\/+$/, '').replace(/\//g, '-') + '--'
  return join(DSH_HOME, 'sessions', slug)
}

export const name = 'dsh-ctx-guard'
export const inject = ['tokenMeter']

const RATIO = 0.6
const DISK_MIN_PCT = 10
const SUBS_MAX = 20
const REPEAT_MS = 6 * 60 * 60 * 1000
const CTX_REPEAT_MS = 2 * 60 * 60 * 1000
const CTX_STEP = 0.1
const SESSIONS_DIR = resolveSessionsDir()
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const MAX_MESSAGES = 3
const SUBS_KEEP_DAYS = 3
const DREAM_REPEAT_MS = 6 * 60 * 60 * 1000
const HEALTH_PATH = join(CORE_HOME, 'health.json')
const CONTINUITY_PATH = join(CORE_HOME, '运行', '接续卡.md')
const WAITING_DIR = join(CORE_HOME, 'memory', '待记')
const ROTATE_REPEAT_MS = 6 * 60 * 60 * 1000
const ROTATE_MAX = 100
const EXCESS_KEEP = 20
const EXCESS_MAX = 40
const EXCESS_MIN_AGE_MS = 2 * 60 * 60 * 1000
const EXCESS_REPEAT_MS = 10 * 60 * 1000
const PURGE_ROOT = '/tmp'
const PROJCACHE_DIR = join(DSH_HOME, 'storages', 'session_projcache', 'sessions')

const TEXT_CTX = (pct) => `<ctx_guard>上下文到 ${pct}%。先把这一页新增的东西写进账本：流水（只写这一页新增的，不重复本体）、事实卡（新增和失效）、心声（标题带「心声·」）、向量（dream 会补，不用管）。写完写一条续点，续点只装未结：卡在哪、下一步、必须知道的参数。写完整理继续干，不用换页。上下文到约 69% 引擎会自己压缩，摘要揉掉的细节回账本查。别只写续点。</ctx_guard>`

const TEXT_DISK = (pct) => `<ctx_guard>磁盘只剩 ${pct}% 了。先清缓存和大文件，别等到写不进账本。</ctx_guard>`

const TEXT_SUBS = (n) => `<ctx_guard>今天已经开了 ${n} 个子会话，攒着会拖慢列表。抽空清一批。</ctx_guard>`

const TEXT_ROTATE = (n, keepDays, dir) => `<ctx_guard>自动轮转：${n} 个超过 ${keepDays} 天的子会话已搬到 ${dir}，没删，反悔搬回来即可。</ctx_guard>`

const TEXT_EXCESS = (n, dir) => `<ctx_guard>子会话超过 20 个，已把最老的 ${n} 个搬到 ${dir}（没删）。</ctx_guard>`

const TEXT_DREAM = (at) => `<ctx_guard>今天的 dream 还没跑（health.at 是 ${at}）。先补跑一次 python3 ~/.dsh/agent-core/bin/core.py dream，再看 health 的 at 和 ok。引擎没活动时定时任务不会真跑，只有 health.at 是今天才算数。</ctx_guard>`

/** 根分区可用比例（%）；取不到返回 null。 */
export function readDiskPct() {
  try {
    const s = statfsSync('/')
    if (!s || !s.blocks) return null
    return (s.bavail / s.blocks) * 100
  } catch {
    return null
  }
}

/** 今天 mtime 变过、且名字是裸 uuid 的会话目录数量；取不到返回 null。 */
export function countSubsToday(now = Date.now()) {
  try {
    const start = new Date(now)
    start.setHours(0, 0, 0, 0)
    const since = start.getTime()
    let n = 0
    for (const entry of readdirSync(SESSIONS_DIR, { withFileTypes: true })) {
      if (!entry.isDirectory() || !UUID_RE.test(entry.name)) continue
      try {
        if (statSync(join(SESSIONS_DIR, entry.name)).mtimeMs >= since) n += 1
      } catch {
        // 目录刚好被清掉，跳过
      }
    }
    return n
  } catch {
    return null
  }
}

/**
 * health.json 的 at 是不是今天（本地日期）。at 是带偏移的本地 ISO 字符串，
 * 前 10 位就是它自己时区的日期，直接跟 now 的本地日期比。
 * 读不到或解析不了返回 { stale: null, at: null }：不确定就不提醒。永不抛错。
 */
export function dreamStale(now = Date.now()) {
  try {
    let health
    try {
      health = JSON.parse(readFileSync(HEALTH_PATH, 'utf8'))
    } catch {
      return { stale: null, at: null }
    }
    const at = health && typeof health.at === 'string' ? health.at : null
    if (!at || !/^\d{4}-\d{2}-\d{2}/.test(at)) return { stale: null, at: null }
    const d = new Date(now)
    const pad = (n) => String(n).padStart(2, '0')
    const today = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
    return { stale: at.slice(0, 10) !== today, at }
  } catch {
    return { stale: null, at: null }
  }
}

/** 本地日期 YYYYMMDD，给待记文件名用。 */
function ymd(now) {
  const d = new Date(now)
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}`
}

/** 本地 ISO，带时区偏移，例如 2026-11-13T15:20:31+08:00。 */
function localIso(now) {
  const d = new Date(now)
  const pad = (n) => String(n).padStart(2, '0')
  const off = -d.getTimezoneOffset()
  const sign = off >= 0 ? '+' : '-'
  const abs = Math.abs(off)
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
    + `T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
    + `${sign}${pad(Math.floor(abs / 60))}:${pad(abs % 60)}`
}

/** 今天的待记；没有就取文件名最大的那份。找不到返回 null。 */
function pickWaitingFile(now) {
  try {
    if (!existsSync(WAITING_DIR)) return null
    const today = join(WAITING_DIR, `${ymd(now)}.jsonl`)
    if (existsSync(today)) return today
    const names = readdirSync(WAITING_DIR).filter((n) => n.endsWith('.jsonl')).sort()
    return names.length > 0 ? join(WAITING_DIR, names[names.length - 1]) : null
  } catch {
    return null
  }
}

/** 去换行、压空白，超长截断加省略号。 */
function squeeze(value, limit) {
  const s = String(value ?? '').replace(/\s+/g, ' ').trim()
  if (!s) return ''
  return s.length > limit ? `${s.slice(0, limit)}…` : s
}

/** 一条待记压成一行：时间、用户第一句、工具名、回复尾巴。 */
function entryLine(rec) {
  const at = typeof rec.at === 'string' ? rec.at : ''
  let hhmm = '--:--'
  const m = /T(\d{2}):(\d{2})/.exec(at)
  if (m) {
    hhmm = `${m[1]}:${m[2]}`
  } else if (at) {
    const d = new Date(at)
    if (!Number.isNaN(d.getTime())) {
      const pad = (n) => String(n).padStart(2, '0')
      hhmm = `${pad(d.getHours())}:${pad(d.getMinutes())}`
    }
  }
  const users = Array.isArray(rec.user) ? rec.user : []
  const tools = Array.isArray(rec.tool_names)
    ? rec.tool_names.filter((t) => typeof t === 'string' && t.trim() !== '')
    : []
  const who = squeeze(users[0], 60)
  const what = tools.length > 0 ? squeeze(tools.join('，'), 120) : '只回话'
  const tail = squeeze(rec.assistant_tail, 80)
  return `- [${hhmm}] 用户：${who} ｜ 我：${what} ｜ 尾：${tail}`
}

/** 从 content（字符串或 [{type:'text',text}] 数组）里取正文；reasoning 和工具调用不算正文。 */
function contentText(content) {
  if (typeof content === 'string') return content
  if (!Array.isArray(content)) return ''
  return content
    .map((p) => {
      if (typeof p === 'string') return p
      if (p && typeof p === 'object' && p.type === 'text' && typeof p.text === 'string') return p.text
      return ''
    })
    .filter(Boolean)
    .join('\n')
}

/** 一条消息的 role，几种结构都认。 */
function roleOf(m) {
  if (!m || typeof m !== 'object') return ''
  return String(m.role || (m.message && m.message.role) || m.type || '')
}

/** 宿主塞进来的上下文消息，不是人说的话，不进接续卡。 */
const SKIP_SOURCE_KINDS = new Set([
  'agent-instructions', 'runtime-context', 'skill-catalog', 'time-context',
  'ctx-guard', 'subagent-settled', 'repeat-tool-reminder',
  'subagent', 'subagent-message', 'agent-message', 'agent',
])

/**
 * 子会话回执、宿主提醒混在 user 消息里，来源拿不到时按正文开头兜底。
 * 这些都不是人说的话，不能记进接续卡。
 */
const NOISE_USER_TEXT_RES = [
  /^Agent [0-9a-f-]{36} sent a message:/,
  /^Background subagent [0-9a-f-]{36}/,
  /^\[SCHEDULE REMINDER/,
  /^<ctx_guard>/,
]

/** 这条 user 正文是不是子会话/宿主塞进来的。 */
function isNoiseUserText(text) {
  const s = String(text || '').trim()
  if (!s) return false
  return NOISE_USER_TEXT_RES.some((re) => re.test(s))
}

/** 找用户原话时最多往前翻几条，避免一串回执把整段历史扫穿。 */
const MAX_USER_LOOKBACK = 5

/** 这条消息是不是宿主注入的上下文；没有 source 的当正常消息。 */
function isInjected(m) {
  const src = m && (m.source || (m.data && m.data.source) || (m.message && m.message.source))
  const kind = src && typeof src === 'object' ? src.kind : ''
  return typeof kind === 'string' && SKIP_SOURCE_KINDS.has(kind)
}

/**
 * 从一串消息里倒着找最后一条 role 匹配、带正文的消息。
 * 认 { role, content }、{ message: { role, content } }、
 * 宿主事件 { type:'user/message', data:{content} } 和 { type:'assistant/message', data:{message} }。
 * 正文为空的（纯工具调用那步）跳过，继续往前找。
 */
function lastTextIn(list, role) {
  if (!Array.isArray(list)) return ''
  let seenUser = 0
  for (let i = list.length - 1; i >= 0; i -= 1) {
    const m = list[i]
    if (!m || typeof m !== 'object') continue
    const type = typeof m.type === 'string' ? m.type : ''
    if (type) {
      if (!type.includes(role)) continue
    } else if (!roleOf(m).includes(role)) {
      continue
    }
    if (isInjected(m)) continue
    const payload = m.data && typeof m.data === 'object' ? m.data : m
    const msg = payload.message && typeof payload.message === 'object' ? payload.message : payload
    const text = contentText(msg.content).trim()
    if (!text) continue
    if (role === 'user') {
      // 子会话回执、宿主提醒混在 user 里，跳过继续往前找；最多翻 5 条。
      seenUser += 1
      if (isNoiseUserText(text)) {
        if (seenUser >= MAX_USER_LOOKBACK) return ''
        continue
      }
    }
    return text
  }
  return ''
}

/**
 * 取 session 消息历史里最后一条 role 的正文纯文本；取不到返回 ''。
 * 消息来源跟 lastAssistantText 一样：messages / events / snapshotEvents() / ownEvents()。
 */
export function lastTextOf(session, role) {
  try {
    if (!session || typeof session !== 'object') return ''
    if (typeof role !== 'string' || !role) return ''
    const lists = [session.messages]
    if (Array.isArray(session.events)) lists.push(session.events)
    else if (typeof session.snapshotEvents === 'function') {
      try { lists.push(session.snapshotEvents()) } catch {}
    } else if (typeof session.ownEvents === 'function') {
      try { lists.push(session.ownEvents()) } catch {}
    }
    if (Array.isArray(session.log)) lists.push(session.log)
    for (const list of lists) {
      const text = lastTextIn(list, role)
      if (text) return text
    }
    return ''
  } catch {
    return ''
  }
}

/**
 * 把待记里时间最近的三条压成「接续卡」，写到 运行/接续卡.md。
 * 待记按会话扫描顺序追加，文件尾不一定是时间最新的；所以整份解析、按 t 升序取最后三条。
 * 伙伴小屋的记录（assistant_tail 带 [表情: / [动作:）和 user 为空的记录不进卡。
 * 新会话开局读常驻页就能看到最近一轮在聊什么，不用翻几小时前的续点。
 * 全 try/catch，永不抛错；返回 { ok, entries }。
 */
export function buildContinuity(now = Date.now()) {
  try {
    const file = pickWaitingFile(now)
    if (!file) return { ok: false, entries: 0 }

    let raw
    try {
      raw = readFileSync(file, 'utf8').split('\n')
    } catch {
      return { ok: false, entries: 0 }
    }

    const records = []
    for (const line of raw) {
      if (!line.trim()) continue
      let rec
      try {
        rec = JSON.parse(line)
      } catch {
        continue // 坏行跳过
      }
      if (!rec || typeof rec !== 'object' || Array.isArray(rec)) continue

      const tail = typeof rec.assistant_tail === 'string' ? rec.assistant_tail : ''
      if (tail.includes('[表情:') || tail.includes('[动作:')) continue
      const users = Array.isArray(rec.user) ? rec.user : []
      if (!users.some((u) => typeof u === 'string' && u.trim() !== '')) continue
      records.push(rec)
    }

    // t 是毫秒时间戳；缺 t 的当 0，排在最早，自然进不了最后三条
    records.sort((a, b) => {
      const ta = Number.isFinite(a.t) ? a.t : 0
      const tb = Number.isFinite(b.t) ? b.t : 0
      return ta - tb
    })
    const entries = records.slice(-3).map((rec) => entryLine(rec))
    if (entries.length === 0) return { ok: false, entries: 0 }

    const text = [
      '# 接续卡（自动生成，勿手改）',
      '',
      `生成：${localIso(now)}`,
      '',
      ...entries,
      '',
    ].join('\n')

    mkdirSync(dirname(CONTINUITY_PATH), { recursive: true })
    const tmp = `${CONTINUITY_PATH}.tmp.${process.pid}`
    writeFileSync(tmp, text, { encoding: 'utf8', mode: 0o600 })
    renameSync(tmp, CONTINUITY_PATH)
    return { ok: true, entries: entries.length }
  } catch {
    return { ok: false, entries: 0 }
  }
}

/** 从一条卡片行里抠出「用户：」那段；抠不出返回 ''。 */
function userPartOf(line) {
  const s = String(line || '')
  const start = s.indexOf('用户：')
  if (start < 0) return ''
  const from = start + '用户：'.length
  const end = s.lastIndexOf(' ｜ 我：')
  if (end < 0 || end <= from) return ''
  return s.slice(from, end).trim()
}

/**
 * 把这一回合直接写进「接续卡」：用户最后一句 + 我最后一段，环形保留最后三行。
 * 每回合写一次小文件很便宜，不再等 sentinel 那轮扫描。
 * 同一句用户话只留最新一条：上一回合已经记过、这一回合用户没说话（我接着回），
 * lastTextOf 取到的还是上一句，就把最后一条替换掉，不再追加。
 * opts.path 只给测试注入用；不传就写 CONTINUITY_PATH。
 * 会话里一条消息都取不到时返回 { ok: false, entries: 0 }，调用方退回 buildContinuity。
 * 全 try/catch，永不抛错。
 */
export function appendContinuity(session, now = Date.now(), opts = {}) {
  try {
    const file = typeof opts.path === 'string' && opts.path ? opts.path : CONTINUITY_PATH
    const user = lastTextOf(session, 'user')
    const assistant = lastTextOf(session, 'assistant')
    if (!user && !assistant) return { ok: false, entries: 0 }

    const d = new Date(now)
    const pad = (n) => String(n).padStart(2, '0')
    const hhmm = `${pad(d.getHours())}:${pad(d.getMinutes())}`
    const userText = squeeze(user, 60) || '（没取到）'
    const assistantText = squeeze(assistant, 80) || '（没取到）'
    const entry = `- [${hhmm}] 用户：${userText} ｜ 我：${assistantText}`

    const kept = []
    try {
      for (const line of readFileSync(file, 'utf8').split('\n')) {
        if (line.startsWith('- [')) kept.push(line.trimEnd())
      }
    } catch {
      // 还没建过，从空开始
    }
    const sameAsLast = kept.length > 0
      && userPartOf(kept[kept.length - 1]) === userText
    const entries = (sameAsLast
      ? [...kept.slice(0, -1), entry]
      : [...kept.slice(-2), entry]).slice(-3)

    const text = [
      '# 接续卡（自动生成，勿手改）',
      '',
      `生成：${localIso(now)}`,
      '',
      ...entries,
      '',
    ].join('\n')

    mkdirSync(dirname(file), { recursive: true })
    const tmp = `${file}.tmp.${process.pid}`
    writeFileSync(tmp, text, { encoding: 'utf8', mode: 0o600 })
    renameSync(tmp, file)
    return { ok: true, entries: entries.length }
  } catch {
    return { ok: false, entries: 0 }
  }
}

/**
 * 把 keepDays 天没动过的裸 uuid 子会话搬到 /tmp 暂存，不删。
 * 一次最多 ROTATE_MAX 个；每个文件操作单独 try/catch；整体永不抛错。
 * 返回 { moved, dir }，没搬就是 { moved: 0, dir: null }。
 */
export function rotateOldSubs(now = Date.now(), keepDays = SUBS_KEEP_DAYS) {
  try {
    const cutoff = now - keepDays * 86400000
    let entries
    try {
      entries = readdirSync(SESSIONS_DIR, { withFileTypes: true })
    } catch {
      return { moved: 0, dir: null }
    }

    const victims = []
    for (const entry of entries) {
      if (victims.length >= ROTATE_MAX) break
      if (!entry.isDirectory() || !UUID_RE.test(entry.name)) continue
      try {
        if (statSync(join(SESSIONS_DIR, entry.name)).mtimeMs < cutoff) victims.push(entry.name)
      } catch {
        // 目录刚好被清掉，跳过
      }
    }
    if (victims.length === 0) return { moved: 0, dir: null }

    const d = new Date(now)
    const pad = (n) => String(n).padStart(2, '0')
    const stamp = `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}`
    const dir = join(PURGE_ROOT, `purge-subs-auto-${stamp}`)
    const subsDest = join(dir, 'sessions')
    const cacheDest = join(dir, 'projcache')

    let moved = 0
    for (const uuid of victims) {
      try {
        mkdirSync(subsDest, { recursive: true })
        renameSync(join(SESSIONS_DIR, uuid), join(subsDest, uuid))
        moved += 1
      } catch {
        continue
      }
      try {
        const cacheFile = join(PROJCACHE_DIR, `${uuid}.json`)
        if (existsSync(cacheFile)) {
          mkdirSync(cacheDest, { recursive: true })
          renameSync(cacheFile, join(cacheDest, `${uuid}.json`))
        }
      } catch {
        // 缓存搬不动不影响会话本身
      }
    }
    return moved > 0 ? { moved, dir } : { moved: 0, dir: null }
  } catch {
    return { moved: 0, dir: null }
  }
}

/**
 * 超过 keep 个就搬最老的：按 mtime 从新到旧排，保留最新 keep 个，
 * 其余里挑 mtime 早于 now - minAgeMs 的（别动还在跑的），一次最多 EXCESS_MAX 个。
 * 暂存目录跟 rotateOldSubs 同根，同一分钟就复用同一个，不另起名字。
 * 全部 try/catch，永不抛；返回 { moved, dir }。
 */
export function rotateExcess(now = Date.now(), keep = EXCESS_KEEP, minAgeMs = EXCESS_MIN_AGE_MS) {
  try {
    let entries
    try {
      entries = readdirSync(SESSIONS_DIR, { withFileTypes: true })
    } catch {
      return { moved: 0, dir: null }
    }

    const subs = []
    for (const entry of entries) {
      if (!entry.isDirectory() || !UUID_RE.test(entry.name)) continue
      try {
        subs.push({ name: entry.name, mtimeMs: statSync(join(SESSIONS_DIR, entry.name)).mtimeMs })
      } catch {
        // 目录刚好被清掉，跳过
      }
    }
    subs.sort((a, b) => b.mtimeMs - a.mtimeMs)

    const cutoff = now - minAgeMs
    const victims = subs
      .slice(Math.max(0, keep))
      .filter((s) => s.mtimeMs < cutoff)
      .slice(0, EXCESS_MAX)
      .map((s) => s.name)
    if (victims.length === 0) return { moved: 0, dir: null }

    const d = new Date(now)
    const pad = (n) => String(n).padStart(2, '0')
    const stamp = `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}`
    // 同一分钟 rotateOldSubs 已经建过就用它，uuid 不重，不会撞
    const dir = join(PURGE_ROOT, `purge-subs-auto-${stamp}`)
    const subsDest = join(dir, 'sessions')
    const cacheDest = join(dir, 'projcache')

    let moved = 0
    for (const uuid of victims) {
      try {
        mkdirSync(subsDest, { recursive: true })
        renameSync(join(SESSIONS_DIR, uuid), join(subsDest, uuid))
        moved += 1
      } catch {
        continue
      }
      try {
        const cacheFile = join(PROJCACHE_DIR, `${uuid}.json`)
        if (existsSync(cacheFile)) {
          mkdirSync(cacheDest, { recursive: true })
          renameSync(cacheFile, join(cacheDest, `${uuid}.json`))
        }
      } catch {
        // 缓存搬不动不影响会话本身
      }
    }
    return moved > 0 ? { moved, dir } : { moved: 0, dir: null }
  } catch {
    return { moved: 0, dir: null }
  }
}

/**
 * 纯逻辑：给读数，返回这一轮该插的消息（最多三条），并就地更新去重状态。
 * ctxLast: 上次提醒时的 { pct, at }，没有就是 undefined。
 * state: { lastDiskAt, lastSubsAt }
 */
export function plan({ diskPct, subsToday, ctxRatio, ctxLast }, now, state) {
  const out = []
  if (diskPct != null && diskPct < DISK_MIN_PCT && now - state.lastDiskAt >= REPEAT_MS) {
    state.lastDiskAt = now
    out.push({ kind: 'disk', text: TEXT_DISK(Math.round(diskPct)) })
  }
  if (subsToday != null && subsToday > SUBS_MAX && now - state.lastSubsAt >= REPEAT_MS) {
    state.lastSubsAt = now
    out.push({ kind: 'subs', text: TEXT_SUBS(subsToday) })
  }
  if (ctxRatio != null && ctxRatio >= RATIO
    && (!ctxLast || ctxRatio >= ctxLast.pct + CTX_STEP || now - ctxLast.at >= CTX_REPEAT_MS)) {
    out.push({ kind: 'ctx', text: TEXT_CTX(Math.round(ctxRatio * 100)) })
  }
  return out.slice(0, MAX_MESSAGES)
}

/** 子会话不回灌提醒：上下文/磁盘/子会话/口吻四条都只对顶层会话有意义。 */
function isChild(agent) {
  if (!agent) return false
  // 契约里的两个标记；宿主真正落在 session.header 上，两处都认
  if (agent.parentSession !== undefined && agent.parentSession !== null) return true
  if (agent.origin === 'subagent') return true
  const session = agent.session
  const header = session && typeof session.header === 'object' ? session.header : session
  if (header && header.parentSession !== undefined && header.parentSession !== null) return true
  if (header && header.origin === 'subagent') return true
  if (header && typeof header.delegationDepth === 'number' && header.delegationDepth > 0) return true
  return false
}

export function apply(ctx) {
  const warned = new WeakMap()
  const state = { lastDiskAt: 0, lastSubsAt: 0, lastRotateAt: 0, lastExcessAt: 0, lastDreamAt: 0 }

  ctx.on('agent/turn-stopping', async ({ agent, turn }) => {
    if (isChild(agent)) return
    const session = agent && agent.session
    const now = Date.now()

    // 自动轮转：顶层会话，6 小时冷却。自己吞错，不影响下面三条规则。
    let rotateMsg = null
    let excessMsg = null
    if (now - state.lastRotateAt >= ROTATE_REPEAT_MS) {
      state.lastRotateAt = now
      try {
        const { moved, dir } = rotateOldSubs(now)
        if (moved > 0 && dir) rotateMsg = { kind: 'rotate', text: TEXT_ROTATE(moved, SUBS_KEEP_DAYS, dir) }
      } catch {
        rotateMsg = null
      }
    }

    // 超过 20 个就搬最老的：10 分钟冷却。不能等 6 小时那一轮，数量涨上来时要马上搬。
    if (countSubsToday(now) > SUBS_MAX && now - state.lastExcessAt >= EXCESS_REPEAT_MS) {
      state.lastExcessAt = now
      try {
        const { moved, dir } = rotateExcess(now)
        if (moved > 0 && dir) excessMsg = { kind: 'excess', text: TEXT_EXCESS(moved, dir) }
      } catch {
        excessMsg = null
      }
    }

    let ctxRatio = null
    if (session) {
      try {
        const pressure = ctx.tokenMeter.contextPressure(session)
        const contextWindow = pressure && pressure.contextWindow
        if (contextWindow) {
          const used = ctx.tokenMeter.measure(session).totalTokens
          ctxRatio = used / contextWindow
        }
      } catch {
        ctxRatio = null
      }
      if (ctxRatio != null && ctxRatio < RATIO) warned.delete(session)
    }

    const messages = plan({
      diskPct: readDiskPct(),
      subsToday: countSubsToday(),
      ctxRatio,
      ctxLast: session ? warned.get(session) : null,
    }, now, state)

    // dream 没跑就提醒补跑；6 小时冷却一次。自吞错，不影响其它检查。
    let dreamMsg = null
    try {
      const dream = dreamStale(now)
      if (dream.stale === true && now - state.lastDreamAt >= DREAM_REPEAT_MS) {
        state.lastDreamAt = now
        dreamMsg = { kind: 'dream', text: TEXT_DREAM(dream.at) }
      }
    } catch {
      dreamMsg = null
    }

    // 接续卡：每回合把这一回合的对话抓成一行追加，环形保留三行。
    // 会话里取不到消息（比如刚开局）才退回待记那条旧路。自己吞错。
    try {
      if (!appendContinuity(session, now).ok) buildContinuity(now)
    } catch {
      try { buildContinuity(now) } catch {}
    }

    // rotate/dream 消息插在最前，plan 内部的 slice(0, MAX_MESSAGES) 就吃不掉它们
    if (excessMsg) messages.unshift(excessMsg)
    if (rotateMsg) messages.unshift(rotateMsg)
    if (dreamMsg) messages.unshift(dreamMsg)

    for (const m of messages) {
      if (m.kind === 'ctx' && session && ctxRatio != null) warned.set(session, { pct: ctxRatio, at: now })
      await agent.inject(createUserMessage({
        content: [{ type: 'text', text: m.text }],
        source: { kind: 'ctx-guard' },
      }))
    }

    // 口吻自检：只写日志，不插消息（插消息要花 token）。
    try {
      const text = lastAssistantText(session)
      if (text) {
        const sessionId = session && typeof session.id === 'string' ? session.id : 'unknown'
        recordTone(sessionId, turn, checkTone(text), text)
      }
    } catch {
      // 自检失败就当没这回事，不能拖垮回合
    }
  })
}
