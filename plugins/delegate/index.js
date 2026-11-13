/** dsh-delegate: local delegate queue -> child agent worker -> report back. */
import { execFileSync } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { existsSync, mkdirSync, readdirSync, readFileSync, renameSync, unlinkSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'
import { defineTool } from '@deepseek-ai/dsh-tools'

export const name = 'dsh-delegate'
export const inject = ['tools', 'timer', 'agents', 'subagents']

const DSH_HOME = process.env.DSH_HOME || join(homedir(), '.dsh')
// 和 queue.py / dispatch.py 共用一套队列目录
const ROOT = join(DSH_HOME, 'agent-core', 'queue')
const INBOX = join(ROOT, 'inbox')
const RUNNING = join(ROOT, 'running')
const DONE = join(ROOT, 'done')
const STUCK = join(ROOT, 'stuck')
const DEFAULT_CWD = process.env.DSH_WORKSPACE || process.cwd()
const POLL_MS = 10 * 1000
const HEARTBEAT_MS = 60 * 1000
const WORKER_TIMEOUT_MS = Number(process.env.DELEGATE_TIMEOUT_MS || 30 * 60 * 1000)
const KEEP_DONE = 200
const RESULT_TEXT_CAP = Number(process.env.DELEGATE_TEXT_CAP || 400)
const QUEUE_PY = join(DSH_HOME, 'agent-core', 'bin', 'queue.py')

const textOutput = {
  schema: {
    type: 'object',
    additionalProperties: false,
    properties: { text: { type: 'string', required: true, description: '结果或说明。' } },
  },
  render: (_args, value) => [{ type: 'text', text: String(value.text || '') }],
}

function ensureDirs() {
  mkdirSync(INBOX, { recursive: true })
  mkdirSync(RUNNING, { recursive: true })
  mkdirSync(DONE, { recursive: true })
  mkdirSync(STUCK, { recursive: true })
}

function nowIso() {
  return new Date().toISOString()
}

// queue.py 的单用 session/created/finished/result，老的单用 sessionId/createdAt/output。
// 读的时候两种都认，写的时候只写 queue.py 的字段。
function normalize(rec) {
  const out = { ...rec }
  if (!out.sessionId && out.session) out.sessionId = out.session
  if (!out.session && out.sessionId) out.session = out.sessionId
  if (!out.createdAt && out.created) out.createdAt = out.created
  if (!out.created && out.createdAt) out.created = out.createdAt
  if (out.output == null && out.result != null) out.output = out.result
  if (out.result == null && out.output != null) out.result = out.output
  if (!out.finishedAt && out.finished) out.finishedAt = out.finished
  if (!out.finished && out.finishedAt) out.finished = out.finishedAt
  out.attempts = Number.isFinite(Number(out.attempts)) ? Number(out.attempts) : 0
  return out
}

// 四个队列目录，按这个顺序查
const DIRS = () => [['inbox', INBOX], ['running', RUNNING], ['done', DONE], ['stuck', STUCK]]

function detail(rec) {
  let s = ''
  if (rec.attempts) s += '  attempts=' + rec.attempts
  if (rec.started) s += '  started=' + String(rec.started).slice(0, 19)
  return s
}

// 跨目录按 id 找单；命中返回 {rec, dir}，否则 undefined
function findRecord(id) {
  const name = String(id || '').trim().replace(/\.json$/, '')
  if (!name) return undefined
  for (const [dir, path] of DIRS()) {
    try {
      const rec = normalize(JSON.parse(readFileSync(join(path, name + '.json'), 'utf8')))
      return { rec, dir }
    } catch (e) { /* 换下一个目录 */ }
  }
  return undefined
}

function statusOf(rec, dir) {
  if (rec.status) return rec.status
  if (dir === 'done') return rec.ok === false ? 'failed' : 'done'
  if (dir === 'running') return 'running'
  if (dir === 'stuck') return 'failed'
  return 'queued'
}

// dispatch.py 投给远端的单：target=remote，或带 cmd。delegate 不接，留在 inbox 给 dispatch.py。
function shouldSkip(rec) {
  if (!rec || typeof rec !== 'object') return false
  if (rec.target === 'remote') return true
  if (Object.prototype.hasOwnProperty.call(rec, 'cmd')) return true
  return false
}

// 正在跑的 key -> id。同键的另一张只认已经在跑的那张，不再抢。
function runningKeyMap() {
  const out = new Map()
  let files = []
  try { files = readdirSync(RUNNING).filter((f) => f.endsWith('.json')) } catch (e) { return out }
  for (const f of files) {
    try {
      const rec = JSON.parse(readFileSync(join(RUNNING, f), 'utf8'))
      const key = String((rec && rec.key) || '').trim()
      if (key) out.set(key, rec.id || f.replace(/\.json$/, ''))
    } catch (e) { /* 心跳正写到一半就跳过，下一轮再看 */ }
  }
  return out
}

// 同一批 inbox 里同 key 的第二张：作废，挪到 stuck 并记下它重复的是哪张。
function parkDuplicate(fileName, rec, firstId) {
  const out = {
    ...rec,
    ok: false,
    status: 'failed',
    reason: 'duplicate_of ' + firstId,
    duplicate_of: firstId,
    finished: nowIso(),
  }
  try {
    writeFileSync(join(STUCK, fileName), JSON.stringify(out, null, 2))
  } catch (e) { return }  // 写不进去就留在 inbox，下一轮还会再判
  try { unlinkSync(join(INBOX, fileName)) } catch (e) { /* ignore */ }
}

function workerPrompt(rec) {
  return [
    'You are an executor worker doing one job independently. You cannot see the main session context; the task below is everything.',
    '',
    'Title: ' + (rec.title || '(none)'),
    'Task:',
    rec.task,
    '',
    'Rules:',
    '1. Work in the given directory with real tools; do the job, do not just plan.',
    '2. When done, reply in the language of the task, short and factual: what you did, which files changed, how you verified, what is left.',
    '3. Your reply goes straight back to the main session, so no pleasantries.',
  ].join('\n')
}


function tryJson(s) {
  try { return JSON.parse(s) } catch (e) { return undefined }
}

function contentToText(v) {
  if (typeof v === 'string') return v
  if (Array.isArray(v)) {
    return v.map((x) => {
      if (typeof x === 'string') return x
      if (x && typeof x.text === 'string') return x.text
      return ''
    }).filter(Boolean).join('\n').trim()
  }
  if (v && typeof v.text === 'string') return v.text.trim()
  if (v && typeof v === 'object') return JSON.stringify(v, null, 2)
  return ''
}

function extractText(result) {
  if (!result) return ''
  let raw = result.output
  if (raw === undefined || raw === null) raw = result.structured
  if (typeof raw === 'string') {
    const parsed = tryJson(raw)
    if (parsed !== undefined) return contentToText(parsed)
    return raw
  }
  return contentToText(raw)
}

export function apply(ctx) {
  let disposed = false

  function pickProvider() {
    try {
      const listed = typeof ctx.subagents.list === 'function' ? ctx.subagents.list() : []
      const arr = Array.isArray(listed) ? listed : []
      const spawn = arr.find((p) => p && p.name === 'spawn')
      if (spawn) return spawn.name
      if (arr.length > 0 && arr[0] && arr[0].name) return arr[0].name
    } catch (e) { /* fall back */ }
    return 'spawn'
  }

  function liveAgent(sessionId) {
    try {
      const agents = ctx.get('agents')
      if (!agents || typeof agents.get !== 'function') return undefined
      return agents.get(sessionId)
    } catch (e) {
      return undefined
    }
  }

  function pruneDone() {
    try {
      const files = readdirSync(DONE).filter((f) => f.endsWith('.json'))
      if (files.length <= KEEP_DONE) return
      files.sort()
      for (const f of files.slice(0, files.length - KEEP_DONE)) {
        try { writeFileSync(join(DONE, f), '') } catch (e) { /* ignore */ }
      }
    } catch (e) { /* ignore */ }
  }

  function writeDone(rec, extra) {
    const out = { ...rec, ...extra, finished: nowIso(), attempts: (Number(rec.attempts) || 0) + 1 }
    out.status = out.ok === false ? 'failed' : 'done'
    // 执行者说完成不算数，等主会话验收；打回重跑的新一轮也没验过
    out.accepted = false
    delete out.accepted_at
    // 上一轮的打回标记不代表这一轮的结果
    delete out.rejected
    delete out.reject_reason
    delete out.rejected_at
    for (const k of ['sessionId', 'createdAt', 'finishedAt', 'output']) delete out[k]
    if (out.result == null) out.result = ''
    try {
      writeFileSync(join(DONE, rec.id + '.json'), JSON.stringify(out, null, 2))
    } catch (e) { /* ignore */ }
    pruneDone()
  }

  async function runOne(rec, runningPath) {
    // 认领时落 started，之后每 60 秒更新 heartbeat；都写在 running 文件里，完成时进 done。
    const startedIso = nowIso()
    const live = { ...rec, started: startedIso, heartbeat: startedIso, status: 'running' }
    let finished = false
    const beat = () => {
      if (finished) return
      try {
        live.heartbeat = nowIso()
        writeFileSync(runningPath, JSON.stringify(live, null, 2))
      } catch (e) { /* ignore */ }
    }
    try { writeFileSync(runningPath, JSON.stringify(live, null, 2)) } catch (e) { /* ignore */ }
    let tick
    try { tick = ctx.setInterval(beat, HEARTBEAT_MS) } catch (e) { tick = undefined }
    const stopBeat = () => {
      finished = true
      try {
        // ctx.setInterval 返回的是 ctx.effect 的 dispose 函数，不是 Node 的 Timer。
        // 交给 clearInterval 清不掉，会一直往 running 里写心跳，done 之后变幽灵单。
        if (typeof tick === 'function') tick()
        else if (tick !== undefined) clearInterval(tick)
      } catch (e) { /* ignore */ }
    }

    try {
      const agent = liveAgent(rec.sessionId)
      if (!agent) {
        writeDone(live, { ok: false, reason: 'session-not-live' })
        return
      }
      let started
      try {
        started = await ctx.subagents.startActivation({
          delivery: 'parent',
          provider: pickProvider(),
          label: (rec.title || rec.task || rec.id).slice(0, 60),
          request: {
            prompt: [{ type: 'text', text: workerPrompt(rec) }],
            parent: agent,
            cwd: rec.cwd || DEFAULT_CWD,
          },
          signal: AbortSignal.timeout(WORKER_TIMEOUT_MS),
        })
      } catch (error) {
        writeDone(live, { ok: false, reason: 'start-failed: ' + String((error && error.message) || error).slice(0, 400) })
        return
      }
      let result
      try {
        result = await started.result
      } catch (error) {
        writeDone(live, { ok: false, reason: 'run-failed: ' + String((error && error.message) || error).slice(0, 400) })
        return
      }
      const output = extractText(result)
      writeDone(live, { ok: true, childId: started.childId, stopReason: (result && result.stopReason) || '', result: String(output).slice(0, 200000), notified: false })
    } finally {
      stopBeat()
      // 完成/失败都在这里收掉 running，不再等外层 .then()；.then() 里那次保留也无害
      try { unlinkSync(runningPath) } catch (e) { /* ignore */ }
    }
  }

  function formatNotice(rec) {
    const head = '【派单完成】' + (rec.title || rec.id)
    const body = String(rec.result || rec.output || rec.reason || '').slice(0, RESULT_TEXT_CAP)
    const tail = '\n\n全文在 ' + join(DONE, rec.id + '.json')
    return head + '\n' + body + tail
  }

  function tryNotify(rec) {
    const agent = liveAgent(rec.sessionId)
    if (!agent) return 'no-agent'
    if (agent.status !== 'idle') return 'busy'
    const message = {
      id: randomUUID(),
      role: 'user',
      content: [{ type: 'text', text: formatNotice(rec) }],
      source: { kind: 'user' },
    }
    try {
      agent.followup(message)
      return 'ok'
    } catch (e) {
      return 'failed'
    }
  }

  function notifyPending() {
    let files = []
    try { files = readdirSync(DONE).filter((f) => f.endsWith('.json')) } catch (e) { return }
    for (const f of files) {
      const p = join(DONE, f)
      let rec
      try { rec = normalize(JSON.parse(readFileSync(p, 'utf8'))) } catch (e) { continue }
      if (!rec || rec.notified === true) continue
      const state = tryNotify(rec)
      if (state === 'ok' || state === 'failed') {
        rec.notified = true
        try { writeFileSync(p, JSON.stringify(rec, null, 2)) } catch (e) { /* ignore */ }
      }
    }
  }

  function poll() {
    if (disposed) return
    try {
      ensureDirs()
      // 这一轮开始时已经在跑的 key；本轮新认领的另记，用于分辨 inbox 里的重复投递
      const runningKeys = runningKeyMap()
      const claimedKeys = new Map()
      const files = readdirSync(INBOX).filter((f) => f.endsWith('.json')).sort()
      for (const f of files) {
        const p = join(INBOX, f)
        let raw
        try {
          raw = JSON.parse(readFileSync(p, 'utf8'))
        } catch (e) {
          try { renameSync(p, join(STUCK, f + '.bad')) } catch (err) { /* ignore */ }
          continue
        }
        const rec = normalize(raw)
        // 先判归属，再 rename：远端的单原样留在 inbox，dispatch.py 还能捡
        if (shouldSkip(rec)) continue
        if (!rec.id) rec.id = f.replace(/\.json$/, '')
        const key = String(rec.key || '').trim()
        // 同一批 inbox 里同 key 的第二张：只认第一张，这张作废
        if (key && claimedKeys.has(key)) {
          parkDuplicate(f, rec, claimedKeys.get(key))
          continue
        }
        // 同键的另一张已经在跑：不抢，留在 inbox 等它结束
        if (key && runningKeys.has(key)) continue
        const runningPath = join(RUNNING, f)
        // running 里已有同名单（崩溃残留）就不抢，跟 queue.py claim 的检查一致
        if (existsSync(runningPath)) continue
        try {
          renameSync(p, runningPath)
        } catch (e) {
          continue
        }
        if (key) claimedKeys.set(key, rec.id)
        runOne(rec, runningPath)
          .catch(() => {})
          .then(() => {
            try { unlinkSync(runningPath) } catch (e) { /* ignore */ }
          })
      }
      notifyPending()
    } catch (e) { /* never break the host */ }
  }

  const delegateTool = defineTool({
    name: 'delegate',
    description: 'Queue a task for a local background worker. The plugin polls the local queue, starts a child agent session to do it, and reports the result back to this session. No network, no repository. The worker cannot see this session; write the task fully.',
    parameters: {
      task: { type: 'string', required: true, description: 'Full task: background, goal, acceptance, working directory.' },
      title: { type: 'string', description: 'Short title for list and report.' },
      cwd: { type: 'string', description: 'Worker working directory, default ' + DEFAULT_CWD },
      key: { type: 'string', description: 'Optional idempotency key: orders with the same key will not be picked up a second time while one is running.' },
    },
    output: textOutput,
    async execute(args) {
      ensureDirs()
      const agents = ctx.get('agents')
      const agent = agents && typeof agents.currentInitiator === 'function' ? agents.currentInitiator() : undefined
      const sessionId = agent && agent.id
      if (!sessionId) return { text: '[error: no live session; call delegate from a session turn]' }
      const rec = {
        id: 'd-' + Date.now().toString(36) + '-' + randomUUID().slice(0, 8),
        title: String(args.title || '').slice(0, 120),
        task: String(args.task || ''),
        cwd: args.cwd ? String(args.cwd) : '',
        session: sessionId,
        created: nowIso(),
        status: 'queued',
      }
      if (!rec.task.trim()) return { text: '[error: task must not be empty]' }
      const key = String(args.key || '').trim()
      if (key) rec.key = key
      writeFileSync(join(INBOX, rec.id + '.json'), JSON.stringify(rec, null, 2))
      return { text: 'Queued ' + rec.id + ' (' + (rec.title || 'untitled') + '). Polls every ' + POLL_MS / 1000 + 's; the result will come back to this session.' }
    },
  })

  const statusTool = defineTool({
    name: 'delegate_status',
    description: 'Show the local delegate queue: queued, running, and the latest finished tasks with result summaries.',
    parameters: {},
    output: textOutput,
    async execute() {
      ensureDirs()
      const lines = []
      const readDir = (dir) => {
        let names = []
        try { names = readdirSync(dir).filter((f) => f.endsWith('.json')) } catch (e) { /* ignore */ }
        const out = []
        for (const f of names) {
          try { out.push(normalize(JSON.parse(readFileSync(join(dir, f), 'utf8')))) } catch (e) { /* ignore */ }
        }
        return out
      }
      const queued = readDir(INBOX).filter((rec) => !shouldSkip(rec))
      lines.push('queued: ' + queued.length)
      for (const rec of queued.slice(0, 10)) {
        lines.push('  ' + rec.id + '  ' + (rec.title || String(rec.task || '').slice(0, 40)) + detail(rec))
      }
      const running = readDir(RUNNING)
      lines.push('running: ' + running.length)
      for (const rec of running.slice(0, 10)) {
        lines.push('  ' + (rec.id || '?') + '  ' + (rec.title || '') + detail(rec))
      }
      const recent = readDir(DONE)
        .sort((a, b) => String(b.finishedAt || '').localeCompare(String(a.finishedAt || '')))
        .slice(0, 10)
      lines.push('recent:')
      for (const rec of recent) {
        let flag
        if (rec.ok === false) {
          flag = 'FAIL ' + (rec.reason || '')
        } else if (rec.accepted === true) {
          flag = 'OK 已验收'
        } else if (rec.accepted === false) {
          flag = 'OK 待验收'
        } else {
          flag = 'OK'
        }
        if (rec.rejected === true) flag += ' 已打回'
        lines.push('  ' + (rec.id || '?') + '  ' + flag + '  ' + (rec.title || '') + detail(rec))
      }
      const stuck = readDir(STUCK)
        .sort((a, b) => String(b.finishedAt || '').localeCompare(String(a.finishedAt || '')))
        .slice(0, 5)
      if (stuck.length) {
        lines.push('stuck:')
        for (const rec of stuck) {
          lines.push('  ' + (rec.id || '?') + '  ' + (rec.reason || rec.title || '') + detail(rec))
        }
      }
      return { text: lines.join('\n') }
    },
  })

  const showTool = defineTool({
    name: 'delegate_show',
    description: 'Show one delegate queue order by id: title, status, ok, attempts, first 200 chars of result, and which queue directory it is in.',
    parameters: {
      id: { type: 'string', required: true, description: 'Order id, e.g. d-mwc1g0uc-f0fbd428' },
    },
    output: textOutput,
    async execute(args) {
      ensureDirs()
      const hit = findRecord(args && args.id)
      if (!hit) return { text: '找不到这张单' }
      const { rec, dir } = hit
      const head = 'id: ' + rec.id + '\ntitle: ' + (rec.title || '') + '\nstatus: ' + statusOf(rec, dir)
      const facts = []
      if (rec.ok !== undefined) facts.push('ok: ' + String(rec.ok))
      if (rec.accepted !== undefined) facts.push('accepted: ' + String(rec.accepted))
      if (rec.accepted_at) facts.push('accepted_at: ' + rec.accepted_at)
      if (rec.rejected === true) facts.push('rejected: true  ' + String(rec.reject_reason || ''))
      if (rec.attempts) facts.push('attempts: ' + rec.attempts)
      if (rec.started) facts.push('started: ' + rec.started)
      if (rec.finished) facts.push('finished: ' + rec.finished)
      facts.push('dir: ' + dir)
      const body = String(rec.result || rec.reason || '').slice(0, 200)
      return { text: head + (facts.length ? '\n' + facts.join('\n') : '') + '\nresult: ' + body }
    },
  })

  const acceptTool = defineTool({
    name: 'delegate_accept',
    description: '验收一张已完成的派单：调用 queue.py accept，把 done 里的单标记 accepted=true。',
    parameters: {
      id: { type: 'string', required: true, description: '派单 id' },
    },
    output: textOutput,
    async execute(args) {
      const id = String((args && args.id) || '').trim()
      if (!id) return { text: '[error] id 不能为空' }
      const py = process.env.DELEGATE_PYTHON || 'python3'
      try {
        const out = execFileSync(py, [QUEUE_PY, 'accept', id], {
          encoding: 'utf8', timeout: 20000, stdio: ['ignore', 'pipe', 'pipe'],
          env: { ...process.env, CORE_HOME: join(DSH_HOME, 'agent-core') },
        })
        return { text: String(out || '').trim() || ('accepted ' + id) }
      } catch (error) {
        const msg = [error && error.stdout, error && error.stderr]
          .filter(Boolean).join('\n').trim() || String((error && error.message) || error)
        return { text: '[error] 验收失败：' + msg }
      }
    },
  })

  ctx.tools.register(delegateTool)
  ctx.tools.register(statusTool)
  ctx.tools.register(showTool)
  ctx.tools.register(acceptTool)

  ensureDirs()
  const handle = ctx.setInterval(poll, POLL_MS)
  ctx.effect(() => () => {
    disposed = true
    try { clearInterval(handle) } catch (e) { /* ignore */ }
  })
}
