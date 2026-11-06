/** xiaomin-sentinel —— 每回合攒一行素材，落 记忆机/运行/待记/。规格见知识库 23 篇第五节。 */
import { appendFileSync, mkdirSync, statSync } from 'node:fs'

export const name = 'xiaomin-sentinel'
export const inject = []

const DIR = '/storage/Users/currentUser/Documents/小敏/运行/待记'
const MAX_BYTES = 512 * 1024
const FILE_RE = /[\w\-\/.（）()\u4e00-\u9fa5]+\.(?:md|js|py|json)\b/g
const buffers = new Map()

function stamp(d = new Date()) {
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}`
}

function textOf(event) {
  const d = event && event.data; const msg = d && (d.message || (d.role === "user" ? d : null))
  if (!msg || !Array.isArray(msg.content)) return ''
  return msg.content.map((c) => (c && typeof c.text === 'string' ? c.text : '')).join(' ').replace(/\s+/g, ' ').trim()
}

function bucket(id) {
  let b = buffers.get(id)
  if (!b) { b = { turn: 0, user: [], assistant: '', tools: [], files: [] }; buffers.set(id, b) }
  return b
}

function appendLine(line) {
  mkdirSync(DIR, { recursive: true })
  const day = stamp()
  let path = `${DIR}/${day}.jsonl`
  for (let i = 2; ; i += 1) {
    try {
      if (statSync(path).size < MAX_BYTES) break
    } catch (e) { break }
    path = `${DIR}/${day}-${i}.jsonl`
  }
  appendFileSync(path, JSON.stringify(line) + '\n', 'utf8')
}

function flush(id, b, event) {
  const userText = (b.user[0] || '').length
  if (b.tools.length === 0 && userText < 20) return
  const files = []
  for (const t of b.tools) for (const m of String(t.args).match(FILE_RE) || []) if (!files.includes(m)) files.push(m)
  appendLine({
    t: Date.now(),
    session: id,
    turn: b.turn,
    user: b.user.slice(0, 3),
    assistant_tail: String(b.assistant || '').slice(-200),
    tools: b.tools.slice(0, 20),
    files: files.slice(0, 10),
  })
}

export function apply(ctx) {
  try { mkdirSync(DIR, { recursive: true }) } catch (e) { /* 目录建不了也不能拖垮宿主 */ }
  ctx.on('session/event', (session, event) => {
    try {
      if (!session || !event) return
      const id = String(session.id || '')
      if (!id) return
      const b = bucket(id)
      if (event.type === 'turn/start') {
        b.turn = (event.data && event.data.turn) || 0
        b.user = []; b.assistant = ''; b.tools = []
      } else if (event.type === 'user/message') {
        const t = textOf(event)
        if (t) b.user.push(t.slice(0, 200))
      } else if (event.type === 'assistant/message') {
        const t = textOf(event)
        if (t) b.assistant = t
      } else if (event.type === 'tool/call') {
        const d = event.data || {}
        if (d.name) b.tools.push({ name: d.name, args: String(d.arguments == null ? '' : d.arguments).slice(0, 80) })
      } else if (event.type === 'turn/end') {
        flush(id, b, event)
        buffers.delete(id)
      }
    } catch (e) { /* 单条事件出错不拖垮宿主 */ }
  }, { global: true })
}
