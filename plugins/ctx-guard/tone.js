/**
 * 口吻自检：纯本地字符串/正则判断，不调模型。
 * 看一眼刚说完的那段话，不合格就记一行日志，不插消息。
 */
import { appendFileSync, existsSync, mkdirSync, readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'

const DSH_HOME = process.env.DSH_HOME || join(homedir(), '.dsh')
const HOME = process.env.CORE_HOME || join(DSH_HOME, 'agent-core')
export const TONE_LOG = process.env.TONE_LOG || join(HOME, '运行', '口吻自检.jsonl')
export const DAILY_MAX = 500
export const AVG_SENT_MAX = 45

const BANNED = [
  '首先', '其次', '总之', '综上所述', '总的来说', '不难看出', '不可否认',
  '值得注意的是', '更重要的是', '换句话说', '某种程度上', '某种意义上',
  '其实', '说白了', '要知道', '而非',
]

const BANNED_PATTERNS = [
  { label: '不是…而是', re: /不是[^。！？!?\n]{0,40}而是/ },
  { label: '不仅…更是', re: /不仅[^。！？!?\n]{0,40}更是/ },
]

/** 本地时区的 ISO 时间，带 +08:00 这种偏移，方便按本地日期统计。 */
export function localIso(d = new Date()) {
  const pad = (n) => String(n).padStart(2, '0')
  const off = -d.getTimezoneOffset()
  const sign = off >= 0 ? '+' : '-'
  const abs = Math.abs(off)
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}` +
    `${sign}${pad(Math.floor(abs / 60))}:${pad(abs % 60)}`
}

/** 取最后一条 assistant/message 的正文；拿不到返回 null。 */
export function lastAssistantText(session) {
  if (!session) return null
  let events = null
  if (Array.isArray(session.events)) events = session.events
  else if (typeof session.snapshotEvents === 'function') events = session.snapshotEvents()
  else if (typeof session.ownEvents === 'function') events = session.ownEvents()
  if (!Array.isArray(events) || events.length === 0) return null

  for (let i = events.length - 1; i >= 0; i -= 1) {
    const e = events[i]
    if (!e || e.type !== 'assistant/message') continue
    const content = e.data && e.data.message && e.data.message.content
    if (!Array.isArray(content)) return null
    const text = content
      .filter((p) => p && p.type === 'text' && typeof p.text === 'string')
      .map((p) => p.text)
      .join('\n')
      .trim()
    return text || null
  }
  return null
}

/** 按中文句号/问号/感叹号切句，返回每句字数（不含空白）。 */
export function splitSentences(text) {
  return String(text)
    .split(/[。！？!?]+/)
    .map((s) => s.replace(/\s+/g, ''))
    .filter((s) => s.length > 0)
}

/** 三条规则：禁词、平均句长 > 45、完全空泛。返回 issues 数组，合格是 []。 */
export function checkTone(text) {
  const s = String(text || '')
  const issues = []

  for (const w of BANNED) {
    if (s.includes(w)) issues.push(`禁词:${w}`)
  }
  for (const p of BANNED_PATTERNS) {
    if (p.re.test(s)) issues.push(`禁词:${p.label}`)
  }

  const sentences = splitSentences(s)
  if (sentences.length > 0) {
    const total = sentences.reduce((n, x) => n + x.length, 0)
    const avg = Math.round(total / sentences.length)
    if (avg > AVG_SENT_MAX) issues.push(`长句:${avg}字`)
  }

  const hasDigit = /\d/.test(s)
  const hasPath = s.includes('/')
  const hasProper = /[A-Za-z]/.test(s) || /[「『“”"'《》]/.test(s)
  if (!hasDigit && !hasPath && !hasProper) issues.push('空泛')

  return issues
}

/** 今天已经写了几行；按文件+日期缓存，一天只读一次。 */
const counterCache = new Map()

function countToday(file, dateStr) {
  const key = `${file}|${dateStr}`
  if (counterCache.has(key)) return counterCache.get(key)
  let n = 0
  try {
    if (existsSync(file)) {
      for (const line of readFileSync(file, 'utf8').split('\n')) {
        if (line.includes(`"at":"${dateStr}`)) n += 1
      }
    }
  } catch {
    n = 0
  }
  counterCache.set(key, n)
  return n
}

/**
 * 追加一行日志。合格也写（issues:[]），方便算合格率。
 * 每天最多 DAILY_MAX 行，超了返回 false 不再写。
 */
export function recordTone(sessionId, turn, issues, text, opts = {}) {
  const file = opts.file || TONE_LOG
  const d = opts.now instanceof Date ? opts.now : new Date()
  const dateStr = localIso(d).slice(0, 10)
  const n = countToday(file, dateStr)
  if (n >= DAILY_MAX) return false

  const entry = {
    at: localIso(d),
    session: String(sessionId || 'unknown'),
    turn: turn == null ? null : Number(turn),
    issues: Array.isArray(issues) ? issues : [],
    chars: String(text || '').replace(/\s+/g, '').length,
  }
  try {
    mkdirSync(dirname(file), { recursive: true })
    appendFileSync(file, `${JSON.stringify(entry)}\n`, 'utf8')
  } catch {
    return false
  }
  counterCache.set(`${file}|${dateStr}`, n + 1)
  return true
}
