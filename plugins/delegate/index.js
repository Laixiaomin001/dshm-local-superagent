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
// 回执正文上限：DELEGATE_RESULT_CAP（老名字 DELEGATE_TEXT_CAP 仍认），默认 400 字。
// 全文不受影响，done/<id>.json 里最多 200000 字，回执末尾给路径。
const RESULT_TEXT_CAP = (() => {
  const n = Number(process.env.DELEGATE_RESULT_CAP || process.env.DELEGATE_TEXT_CAP || 400)
  return Number.isFinite(n) && n > 0 ? n : 400
})()
const QUEUE_PY = join(DSH_HOME, 'agent-core', 'bin', 'queue.py')
const CORE_HOME = join(DSH_HOME, 'agent-core')
const PYTHON = process.env.DELEGATE_PYTHON || 'python3'

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

// dispatch.py 投给远端的单：target=tiangong，或带 cmd。delegate 不接，留在 inbox 给 dispatch.py。
function shouldSkip(rec) {
  if (!rec || typeof rec !== 'object') return false
  if (rec.target === 'tiangong') return true
  if (Object.prototype.hasOwnProperty.call(rec, 'cmd')) return true
  return false
}

// 前面这里有过两份认领逻辑：runningKeyMap 自己扫 running 判同 key，
// parkDuplicate 自己把同批重复单挪 stuck。queue.py claim 已经在做同一件事
// （判 key、判 running 同名单、rename、写 started/status），这里再判一次就是
// 第二份实现，两边一旦改法不同就会出现「插件认了、队列说没认」。
// 删掉它们，认领胜负只看 claim 的退出码；重复单留在 inbox，claim 非 0 就不跑。

// 认领和心跳都只走 queue.py，插件不再碰 running 文件。
// 0 才算认领到；非 0 表示同 key 已在跑或已被别的进程抢走，下一轮再看。
function queueCli(args) {
  try {
    execFileSync(PYTHON, [QUEUE_PY, ...args], {
      encoding: 'utf8', timeout: 20000, stdio: ['ignore', 'pipe', 'pipe'],
      maxBuffer: 4 * 1024 * 1024,
      env: { ...process.env, CORE_HOME },
    })
    return true
  } catch (e) {
    return false
  }
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

  // 老的 sessionId/createdAt/finishedAt/output 不再写，只留 queue.py 那套字段；
  // 已经写进 payload 的旧字段照原样留着（读的时候 normalize 两套都认）。
  function stripLegacy(out) {
    for (const k of ['sessionId', 'createdAt', 'finishedAt', 'output']) delete out[k]
    return out
  }

  function finishBase(rec, extra) {
    const out = { ...rec, ...extra, finished: nowIso(), attempts: (Number(rec.attempts) || 0) + 1 }
    return stripLegacy(out)
  }

  function writeDone(rec, extra) {
    const out = finishBase(rec, extra)
    out.ok = true
    out.status = 'done'
    // 执行者说完成不算数，等主会话验收；打回重跑的新一轮也没验过
    out.accepted = false
    delete out.accepted_at
    // 上一轮的打回标记不代表这一轮的结果
    delete out.rejected
    delete out.reject_reason
    delete out.rejected_at
    if (out.result == null) out.result = ''
    try {
      writeFileSync(join(DONE, rec.id + '.json'), JSON.stringify(out, null, 2))
    } catch (e) { /* ignore */ }
    pruneDone()
  }

  // 失败不再混进 done：queue.py / doctor / sweep 都按 stuck 认失败单。
  function writeStuck(rec, extra) {
    const out = finishBase(rec, extra)
    out.ok = false
    out.status = 'failed'
    out.reason = String(out.reason == null ? '' : out.reason).slice(0, 2000)
    try {
      writeFileSync(join(STUCK, rec.id + '.json'), JSON.stringify(out, null, 2))
    } catch (e) { /* ignore */ }
  }

  async function runOne(rec, runningPath, queueId) {
    // 认领、started、status:running 都由 queue.py claim 写好；这里只起会话。
    // 心跳不再改 running 文件（那会跟 queue.py 抢同一张单），改成每 60 秒调 heartbeat 子命令。
    let finished = false
    const beat = () => {
      if (finished) return
      queueCli(['heartbeat', queueId])
    }
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
        writeStuck(rec, { reason: 'session-not-live' })
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
            // 不继承父会话预设：主会话可能是 readonly，worker 继承过去就只会查不会写。
            // 跟 readonly-preset 里的 tool-subagent 配置对齐，一律用 standard。
            agentPreset: 'standard',
          },
          signal: AbortSignal.timeout(WORKER_TIMEOUT_MS),
        })
      } catch (error) {
        writeStuck(rec, { reason: 'start-failed: ' + String((error && error.message) || error).slice(0, 400) })
        return
      }
      let result
      try {
        result = await started.result
      } catch (error) {
        writeStuck(rec, { reason: 'run-failed: ' + String((error && error.message) || error).slice(0, 400) })
        return
      }
      const output = extractText(result)
      writeDone(rec, { childId: started.childId, stopReason: (result && result.stopReason) || '', result: String(output).slice(0, 200000), notified: false })
    } finally {
      stopBeat()
      // 完成/失败都在这里收掉 running，不再等外层 .then()；.then() 里那次保留也无害
      try { unlinkSync(runningPath) } catch (e) { /* ignore */ }
    }
  }

  function formatNotice(rec, where) {
    const dir = where === 'stuck' ? STUCK : DONE
    const head = (rec.ok === false ? '【派单未完成】' : '【派单完成】') + (rec.title || rec.id)
    const body = String(rec.result || rec.output || rec.reason || '').slice(0, RESULT_TEXT_CAP)
    const tail = '\n\n全文在 ' + join(dir, rec.id + '.json')
    return head + '\n' + body + tail
  }

  function tryNotify(rec, where) {
    const agent = liveAgent(rec.sessionId)
    if (!agent) return 'no-agent'
    if (agent.status !== 'idle') return 'busy'
    const message = {
      id: randomUUID(),
      role: 'user',
      content: [{ type: 'text', text: formatNotice(rec, where) }],
      source: { kind: 'user' },
    }
    try {
      agent.followup(message)
      return 'ok'
    } catch (e) {
      return 'failed'
    }
  }

  // 失败单也要回执：失败写在 stuck，扫 done 的写法会把失败静默吞掉。
  function notifyPending() {
    for (const [where, dir] of [['done', DONE], ['stuck', STUCK]]) {
      let files = []
      try { files = readdirSync(dir).filter((f) => f.endsWith('.json')) } catch (e) { continue }
      for (const f of files) {
        const p = join(dir, f)
        let raw
        try { raw = JSON.parse(readFileSync(p, 'utf8')) } catch (e) { continue }
        const rec = normalize(raw)
        if (!rec || rec.notified === true) continue
        // 判重作废的单带了 notify:false，不用打扰父会话
        if (rec.notify === false) continue
        const state = tryNotify(rec, where)
        if (state === 'ok' || state === 'failed') {
          raw.notified = true
          try { writeFileSync(p, JSON.stringify(raw, null, 2)) } catch (e) { /* ignore */ }
        }
      }
    }
  }

  function poll() {
    if (disposed) return
    try {
      ensureDirs()
      const files = readdirSync(INBOX).filter((f) => f.endsWith('.json')).sort()
      for (const f of files) {
        const p = join(INBOX, f)
        const queueId = f.replace(/\.json$/, '')
        let raw
        try {
          raw = JSON.parse(readFileSync(p, 'utf8'))
        } catch (e) {
          try { renameSync(p, join(STUCK, f + '.bad')) } catch (err) { /* ignore */ }
          continue
        }
        const rec = normalize(raw)
        if (!rec.id) rec.id = queueId
        // 先判归属再认领：远端的单原样留在 inbox，dispatch.py 还能捡
        if (shouldSkip(rec)) continue
        // 没有 task 的单认不了：认了只会起一个空活，挪进 stuck 等人看
        if (!String(rec.task || '').trim()) {
          writeStuck(rec, { reason: 'no-task: 单里没有 task，格式不认识' })
          if (existsSync(join(STUCK, rec.id + '.json'))) {
            try { unlinkSync(p) } catch (e) { /* ignore */ }
          }
          continue
        }
        // 认领只认 queue.py 的退出码：0 才是这张单的，非 0（同 key 在跑、已被别人抢）
        // 就跳过，单留在 inbox，下一轮再看。
        if (!queueCli(['claim', queueId, '--worker', 'delegate'])) continue
        const runningPath = join(RUNNING, f)
        let claimed
        try {
          claimed = normalize(JSON.parse(readFileSync(runningPath, 'utf8')))
        } catch (e) {
          // claim 已成功但读不回 running：拿 inbox 的单先跑，started 用当前时间兜底
          claimed = { ...rec, status: 'running', started: nowIso() }
        }
        if (!claimed.id) claimed.id = queueId
        runOne(claimed, runningPath, queueId)
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
      try {
        const out = execFileSync(PYTHON, [QUEUE_PY, 'accept', id], {
          encoding: 'utf8', timeout: 20000, stdio: ['ignore', 'pipe', 'pipe'],
          env: { ...process.env, CORE_HOME },
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
