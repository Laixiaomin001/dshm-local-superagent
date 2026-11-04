/**
 * dshm-local-delegate — a local delegate queue.
 *
 * A live session queues a task as one JSON file; this plugin polls the inbox
 * in-process, starts a child agent session as the worker, and reports the
 * result back to the session that queued it.
 *
 * No network, no repository, no resident process: the poller is a timer inside
 * the host, so it starts and stops with the host.
 */
import { randomUUID } from 'node:crypto'
import { mkdirSync, readdirSync, readFileSync, renameSync, unlinkSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'
import { defineTool } from '@deepseek-ai/dsh-tools'

export const name = 'dshm-local-delegate'
export const inject = ['tools', 'timer', 'agents', 'subagents']

const HOME = process.env.LOCAL_SUPERAGENT_HOME || join(homedir(), '.local-superagent')
const ROOT = process.env.LOCAL_SUPERAGENT_QUEUE || join(HOME, 'delegate')
const INBOX = join(ROOT, 'inbox')
const DONE = join(ROOT, 'done')
const DEFAULT_CWD = process.env.LOCAL_SUPERAGENT_WORKSPACE || homedir()
const POLL_MS = Number(process.env.LOCAL_SUPERAGENT_POLL_MS || 10 * 1000)
const WORKER_TIMEOUT_MS = Number(process.env.LOCAL_SUPERAGENT_WORKER_TIMEOUT_MS || 30 * 60 * 1000)
const KEEP_DONE = 200
const RESULT_TEXT_CAP = 400

const textOutput = {
  schema: {
    type: 'object',
    additionalProperties: false,
    properties: { text: { type: 'string', required: true, description: 'Result or a short explanation.' } },
  },
  render: (_args, value) => [{ type: 'text', text: String(value.text || '') }],
}

function ensureDirs() {
  mkdirSync(INBOX, { recursive: true })
  mkdirSync(DONE, { recursive: true })
}

function nowIso() {
  return new Date().toISOString()
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
        try { unlinkSync(join(DONE, f)) } catch (e) { /* ignore */ }
      }
    } catch (e) { /* ignore */ }
  }

  function writeDone(rec) {
    try {
      writeFileSync(join(DONE, rec.id + '.json'), JSON.stringify(rec, null, 2))
    } catch (e) { /* ignore */ }
    pruneDone()
  }

  async function runOne(rec) {
    const agent = liveAgent(rec.sessionId)
    if (!agent) {
      writeDone({ ...rec, ok: false, reason: 'session-not-live', finishedAt: nowIso() })
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
      writeDone({ ...rec, ok: false, reason: 'start-failed: ' + String((error && error.message) || error).slice(0, 400), finishedAt: nowIso() })
      return
    }
    let result
    try {
      result = await started.result
    } catch (error) {
      writeDone({ ...rec, ok: false, reason: 'run-failed: ' + String((error && error.message) || error).slice(0, 400), finishedAt: nowIso() })
      return
    }
    const output = extractText(result)
    writeDone({ ...rec, ok: true, childId: started.childId, stopReason: (result && result.stopReason) || '', output: String(output).slice(0, 200000), finishedAt: nowIso(), notified: false })
  }

  function formatNotice(rec) {
    const head = '[delegate] finished: ' + (rec.title || rec.id)
    const body = String(rec.output || rec.reason || '').slice(0, RESULT_TEXT_CAP)
    const tail = '\n\nFull record: ' + join(DONE, rec.id + '.json')
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
      try { rec = JSON.parse(readFileSync(p, 'utf8')) } catch (e) { continue }
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
      const files = readdirSync(INBOX).filter((f) => f.endsWith('.json'))
      for (const f of files) {
        const p = join(INBOX, f)
        let rec
        try {
          rec = JSON.parse(readFileSync(p, 'utf8'))
        } catch (e) {
          try { renameSync(p, join(DONE, f + '.bad')) } catch (err) { /* ignore */ }
          continue
        }
        const running = p + '.running'
        try {
          renameSync(p, running)
        } catch (e) {
          continue
        }
        runOne(rec)
          .catch(() => {})
          .then(() => {
            try { renameSync(running, join(DONE, rec.id + '.taken')) } catch (e) { /* ignore */ }
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
        sessionId,
        createdAt: nowIso(),
      }
      if (!rec.task.trim()) return { text: '[error: task must not be empty]' }
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
      let inbox = []
      let done = []
      try { inbox = readdirSync(INBOX).filter((f) => f.endsWith('.json')) } catch (e) { /* ignore */ }
      try { done = readdirSync(DONE).filter((f) => f.endsWith('.json')) } catch (e) { /* ignore */ }
      lines.push('queued: ' + inbox.length)
      for (const f of inbox.slice(0, 10)) {
        try {
          const rec = JSON.parse(readFileSync(join(INBOX, f), 'utf8'))
          lines.push('  ' + rec.id + '  ' + (rec.title || String(rec.task || '').slice(0, 40)))
        } catch (e) { /* ignore */ }
      }
      const recent = done
        .map((f) => { try { return JSON.parse(readFileSync(join(DONE, f), 'utf8')) } catch (e) { return undefined } })
        .filter(Boolean)
        .sort((a, b) => String(b.finishedAt || '').localeCompare(String(a.finishedAt || '')))
        .slice(0, 10)
      lines.push('recent:')
      for (const rec of recent) {
        const flag = rec.ok === false ? 'FAIL ' + (rec.reason || '') : 'OK'
        lines.push('  ' + (rec.id || '?') + '  ' + flag + '  ' + (rec.title || ''))
      }
      return { text: lines.join('\n') }
    },
  })

  ctx.tools.register(delegateTool)
  ctx.tools.register(statusTool)

  ensureDirs()
  const handle = ctx.setInterval(poll, POLL_MS)
  ctx.effect(() => () => {
    disposed = true
    try { clearInterval(handle) } catch (e) { /* ignore */ }
  })
}
