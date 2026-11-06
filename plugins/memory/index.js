/**
 * xiaomin-memory —— 把小敏的账做成 4 个工具（mem_search / mem_get / mem_store / mem_status）。
 *
 * 铁律（见 文档/SUPERAGENT-合并架构-v1.md）：
 *  · apply() 只注册工具：不 spawn、不探端口、不写文件 —— 启动路径零副作用
 *  · 每次调用都带超时（15 秒），到点由 execFile 杀掉，不会“永远等”
 *  · 返回体积有上限：search 由 mem.py 封顶 2KB，get 由 --max 封顶 8KB
 *
 * 后端：$MEM/工具_v2/mem.py（纯标准库 python，无常驻进程）
 *
 * 另外挂一个全局 session/event 钩子（2026-11-06）：顶层会话每回合结束时，把本回合
 * 最后一条助手消息的尾巴（≤200 字）追进 状态.json 续点，按会话各一行
 * 「· 自动追加（未复核，HH:MM，<短id>）：…」，最多留 2 行。session.id 原样
 * 传给 mem.py 的 --session，由 mem.py 取前 8 位做短 id。
 * 子会话（subagent/delegate）不写；任何异常只记日志，不拖垮宿主。
 */
import { spawn } from 'node:child_process'
import { defineTool } from '@deepseek-ai/dsh-tools'

export const name = 'xiaomin-memory'
export const inject = ['tools']

const MEM_PY = '/storage/Users/currentUser/Documents/小敏/记忆机/工具/mem.py'
const PY = '/data/storage/el2/base/haps/entry/files/bin/python3'
const RESUME_TAIL = 200

function runOnce(args, timeoutMs) {
  return new Promise((resolve) => {
    let done = false
    const finish = (v) => { if (!done) { done = true; clearTimeout(timer); resolve(v) } }
    const child = spawn(PY, [MEM_PY, ...args], {
      stdio: ['ignore', 'pipe', 'pipe'],
      env: { ...process.env, MEM_TIMEOUT: '10' },
    })
    let out = ''
    let err = ''
    child.stdout.on('data', (d) => { if (out.length < 512 * 1024) out += String(d) })
    child.stderr.on('data', (d) => { if (err.length < 64 * 1024) err += String(d) })
    child.on('error', (e) => finish({ error: e }))
    child.on('close', (code) => finish({ code, out, err }))
    // 硬超时：到点 SIGKILL，绝不让一次调用挂住会话
    const timer = setTimeout(() => {
      try { child.kill('SIGKILL') } catch (e) { /* ignore */ }
      finish({ error: new Error('记忆服务超时（' + timeoutMs + 'ms）') })
    }, timeoutMs)
  })
}

async function callMem(args, timeoutMs = 15000) {
  const r = await runOnce(args, timeoutMs)
  if (r.error) throw new Error('记忆服务失败：' + String(r.error.message || r.error).slice(0, 600))
  if (typeof r.out !== 'string') throw new Error('记忆服务输出非字符串')
  if (r.code !== 0) throw new Error('记忆服务失败：' + String(r.err || ('exit ' + r.code)).trim().slice(0, 600))
  return r.out.trim()
}

const textOutput = {
  schema: {
    type: 'object',
    additionalProperties: false,
    properties: { text: { type: 'string', required: true, description: '记忆服务的文本输出。' } },
  },
  render: (_args, value) => [{ type: 'text', text: value.text }],
}

const searchTool = defineTool({
  name: 'mem_search',
  description: 'Search Xiaomin\'s long-term memory (flow ledger, FTS5). Returns at most 5 hits under 2 KB. Prefer it over reading the memory directory.',
  parameters: {
    query: { type: 'string', required: true, description: 'Keywords. For Chinese use 3+ contiguous characters; the trigram index does not span punctuation.' },
    limit: { type: 'number', description: 'Max hits, 1-10, default 5.' },
  },
  output: textOutput,
  async execute(args) {
    const argv = ['search', String(args.query)]
    if (args.limit !== undefined) argv.push('--limit', String(args.limit))
    return { text: await callMem(argv) }
  },
})

const getTool = defineTool({
  name: 'mem_get',
  description: 'Read one memory record by its file name (as returned by mem_search). At most 8 KB; longer records are truncated with a notice.',
  parameters: {
    name: { type: 'string', required: true, description: 'Record file name, e.g. 20261104_0518_继承开工.md' },
    max: { type: 'number', description: 'Byte cap, default 8000.' },
  },
  output: textOutput,
  async execute(args) {
    const argv = ['get', String(args.name)]
    if (args.max !== undefined) argv.push('--max', String(args.max))
    return { text: await callMem(argv) }
  },
})

const storeTool = defineTool({
  name: 'mem_store',
  description: 'Append one record to Xiaomin\'s memory, with the same four fields as the old ledger (time, who, what was said, what I felt). Atomic write; index updated in the same transaction.',
  parameters: {
    title: { type: 'string', required: true, description: 'Short title of the record.' },
    said: { type: 'string', description: 'What the person said (verbatim or near).' },
    heart: { type: 'string', description: 'What I felt / my own note.' },
    who: { type: 'string', description: 'Person, default 老丁.' },
    domain: { type: 'string', description: 'Domain tag, default 6_工程与事故.' },
    dryRun: { type: 'boolean', description: 'Preview only, write nothing.' },
  },
  output: textOutput,
  async execute(args) {
    const argv = ['store', '--title', String(args.title)]
    if (args.said !== undefined) argv.push('--said', String(args.said))
    if (args.heart !== undefined) argv.push('--heart', String(args.heart))
    if (args.who !== undefined) argv.push('--who', String(args.who))
    if (args.domain !== undefined) argv.push('--domain', String(args.domain))
    if (args.dryRun === true) argv.push('--dry-run')
    return { text: await callMem(argv) }
  },
})

const statusTool = defineTool({
  name: 'mem_status',
  description: 'Health of the memory index: records on disk vs indexed, drift samples, last write, database size.',
  parameters: {},
  output: textOutput,
  async execute() {
    return { text: await callMem(['status']) }
  },
})

/**
 * 取事件里助手的回复正文：只认 content 里 type === 'text' 的段落。
 * 同一份 content 里 reasoning（模型思考）也带 text 字段，必须跳过；
 * tool-call / image / file 段没有可当正文的文本，同样跳过。
 * 只剩思考没有正文时返回 ''，由调用方按「没文本就跳过」处理。
 * 口径跟 xiaomin-sentinel 的 textOf 不同（那边收料另算），别合并。
 */
function textOf(event) {
  const d = event && event.data; const msg = d && (d.message || (d.role === 'user' ? d : null))
  if (!msg || !Array.isArray(msg.content)) return ''
  const parts = []
  for (const c of msg.content) {
    if (c && c.type === 'text' && typeof c.text === 'string') parts.push(c.text)
  }
  return parts.join(' ').replace(/\s+/g, ' ').trim()
}

/**
 * 顶层会话判定，字段来自 dsh-session 的 SessionHeader
 * （cores/.../node_modules/@deepseek-ai/dsh-session/lib/index.js 的 validateSessionHeader）：
 * 子会话由 dsh-subagent 的 childSessionMeta() 建，一定带 parentSession + origin:'subagent'
 * + 正数 delegationDepth；顶层会话这三样都没有。
 */
export function isTopLevelSession(session) {
  const h = (session && session.header) || {}
  const parent = h.parentSession || session?.parentSession || session?.parentSessionId
  if (parent) return false
  if (h.origin === 'subagent') return false
  if (typeof h.delegationDepth === 'number' && h.delegationDepth > 0) return false
  return true
}

// 每个会话本回合最后一条 assistant/message 的文本；turn/start 清，turn/end 用。
const lastAssistant = new Map()

function logHookError(e) {
  try { console.error('xiaomin-memory: resume-auto：' + String((e && e.message) || e).slice(0, 300)) } catch (_) { /* 日志本身也不能炸 */ }
}

/**
 * 全局 session/event 钩子主体。只处理 turn/end：把本回合最后一条助手消息的尾巴
 * 连同 session.id 交给 mem.py resume-auto。导出是为了宿主外能喂假事件做 stub 测试。
 */
export async function onSessionEvent(session, event) {
  try {
    if (!session || !event) return
    const id = String(session.id || '')
    if (!id) return
    if (event.type === 'turn/start') {
      lastAssistant.delete(id)
      return
    }
    if (event.type === 'assistant/message') {
      const t = textOf(event)
      if (t) lastAssistant.set(id, t)
      return
    }
    if (event.type !== 'turn/end') return
    const tail = lastAssistant.get(id)
    lastAssistant.delete(id)
    if (!tail) return
    if (!isTopLevelSession(session)) return
    await callMem(['resume-auto', '--text=' + tail.slice(-RESUME_TAIL), '--session=' + id])
  } catch (e) {
    logHookError(e)
  }
}

export function apply(ctx) {
  ctx.tools.register(searchTool)
  ctx.tools.register(getTool)
  ctx.tools.register(storeTool)
  ctx.tools.register(statusTool)
  ctx.on('session/event', (session, event) => { onSessionEvent(session, event) }, { global: true })
}

