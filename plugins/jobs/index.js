/**
 * dshm-local-jobs — an in-process job scheduler.
 *
 * Design:
 *  · apply() reads the job table, registers two tools, and arms one timer;
 *    it does not spawn a child or probe a port at startup
 *  · each job runs as an argv array (no shell), with a hard timeout that
 *    SIGKILLs the whole process group
 *  · missed runs are caught up once if within 2x the interval, otherwise skipped
 *  · next run gets +/-10% jitter
 *  · a job is skipped while the same id is still running
 *
 * Table: $LOCAL_SUPERAGENT_HOME/jobs/jobs.json, editable by hand.
 * Runs:  $LOCAL_SUPERAGENT_HOME/jobs/runs.jsonl, append-only.
 */
import { spawn } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, renameSync, appendFileSync, writeFileSync, statSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'
import { defineTool } from '@deepseek-ai/dsh-tools'

export const name = 'dshm-local-jobs'
export const inject = ['tools', 'timer']

const HOME = process.env.LOCAL_SUPERAGENT_HOME || join(homedir(), '.local-superagent')
const DIR = process.env.LOCAL_SUPERAGENT_JOBS_DIR || join(HOME, 'jobs')
const JOBS = join(DIR, 'jobs.json')
const RUNS = join(DIR, 'runs.jsonl')
const TICK_MS = Number(process.env.LOCAL_SUPERAGENT_TICK_MS || 60_000)
const DEFAULT_CWD = process.env.LOCAL_SUPERAGENT_WORKSPACE || homedir()

// Written once when no table exists. Disabled on purpose: registering a job
// must not start running anything by surprise.
const DEFAULT_JOBS = {
  jobs: [
    {
      id: 'example', title: 'Example job', enabled: false, every: 3600, timeout: 60,
      cmd: ['python3', '-c', 'print("hello from the local scheduler")'],
      next_run: 0, failures: 0, last_status: null, last_end: 0,
    },
  ],
}

const running = new Set()
let state = null
let interval = null
let jobsMtime = 0

const textOut = {
  schema: { type: 'object', additionalProperties: false, properties: { text: { type: 'string', required: true } } },
  render: (_a, v) => [{ type: 'text', text: v.text }],
}

function ensureDir() {
  if (!existsSync(DIR)) mkdirSync(DIR, { recursive: true })
}

export function load() {
  ensureDir()
  if (!existsSync(JOBS)) {
    writeFileSync(JOBS, JSON.stringify(DEFAULT_JOBS, null, 1), 'utf8')
    return JSON.parse(JSON.stringify(DEFAULT_JOBS))
  }
  try {
    const d = JSON.parse(readFileSync(JOBS, 'utf8'))
    if (!d || !Array.isArray(d.jobs)) throw new Error('jobs is not an array')
    return d
  } catch (e) {
    try { renameSync(JOBS, JOBS + '.broken-' + Date.now()) } catch { /* ignore */ }
    return JSON.parse(JSON.stringify(DEFAULT_JOBS))
  }
}

function save() {
  try {
    const tmp = JOBS + '.tmp'
    writeFileSync(tmp, JSON.stringify(state, null, 1), 'utf8')
    renameSync(tmp, JOBS)
  } catch (e) { /* if the disk refuses, keep running from memory */ }
}

function jitter(ms) {
  return Math.round(ms * (0.9 + Math.random() * 0.2))
}

function nextRunFor(job, from) {
  if (typeof job.at === 'string' && /^\d{1,2}:\d{2}$/.test(job.at)) {
    const [h, m] = job.at.split(':').map(Number)
    const t = new Date(from)
    t.setHours(h, m, 0, 0)
    if (t.getTime() <= from) t.setDate(t.getDate() + 1)
    return t.getTime()
  }
  const every = Math.max(60, Number(job.every) || 3600)
  return from + jitter(every * 1000)
}

function logRun(rec) {
  try {
    ensureDir()
    appendFileSync(RUNS, JSON.stringify(rec) + '\n', 'utf8')
  } catch (e) { /* ignore */ }
}

export function runJob(job, reason) {
  if (!state) state = load()
  return new Promise((resolve) => {
    if (running.has(job.id)) return resolve({ skipped: 'running' })
    running.add(job.id)
    const started = Date.now()
    const timeoutMs = Math.max(1, Number(job.timeout) || 60) * 1000
    let out = ''
    let err = ''
    let child = null
    let killTimer = null
    let done = false

    const finish = (status, extra) => {
      if (done) return
      done = true
      if (killTimer) clearTimeout(killTimer)
      running.delete(job.id)
      const ended = Date.now()
      job.last_status = status
      job.last_end = ended
      job.failures = status === 'ok' ? 0 : (job.failures || 0) + 1
      let next
      if (job.failures >= 3 && !job.at) {
        const base = Math.max(60, Number(job.every) || 3600) * 1000
        next = ended + Math.min(base * Math.pow(2, job.failures - 2), 6 * 3600 * 1000)
      } else {
        next = nextRunFor(job, ended)
      }
      job.next_run = next
      save()
      logRun({ at: new Date(started).toISOString(), id: job.id, reason, status, ms: ended - started,
        rc: extra && extra.rc !== undefined ? extra.rc : null,
        tail: (err || out).trim().split('\n').slice(-3).join(' | ').slice(0, 300) })
      resolve({ status })
    }

    killTimer = setTimeout(() => {
      try { if (child && child.pid) process.kill(-child.pid, 'SIGKILL') } catch (e) { /* ignore */ }
      try { if (child) child.kill('SIGKILL') } catch (e) { /* ignore */ }
      finish('timeout')
    }, timeoutMs)

    try {
      const [bin, ...args] = job.cmd
      child = spawn(bin, args, {
        detached: true,
        stdio: ['ignore', 'pipe', 'pipe'],
        cwd: job.cwd || DEFAULT_CWD,
      })
      child.stdout.on('data', (d) => { if (out.length < 64 * 1024) out += String(d) })
      child.stderr.on('data', (d) => { if (err.length < 64 * 1024) err += String(d) })
      child.on('error', (e) => { err += String(e && e.message); finish('spawn-error') })
      child.on('close', (rc) => {
        const okCodes = Array.isArray(job.okExit) ? job.okExit : [0]
        finish(okCodes.includes(rc) ? 'ok' : 'failed', { rc })
      })
    } catch (e) {
      err += String(e && e.message)
      finish('spawn-error')
    }
  })
}

export async function tick() {
  // Re-read the table when the file is edited, so changing a timeout does not
  // require a host restart.
  try {
    const m = statSync(JOBS).mtimeMs
    if (m !== jobsMtime) { state = load(); jobsMtime = m }
  } catch (e) { /* no file yet: use what is in memory */ }
  if (!state) state = load()
  const now = Date.now()
  for (const j of state.jobs) {
    if (!j.enabled) continue
    if (!Number(j.next_run)) {
      j.next_run = now + 45_000
    } else if (j.next_run < now - 2 * Math.max(60, Number(j.every) || 3600) * 1000) {
      // Missed for too long: skip ahead instead of running a backlog.
      j.next_run = nextRunFor(j, now)
    }
  }
  save()
  const due = state.jobs
    .filter((j) => j && j.enabled && !running.has(j.id) && (Number(j.next_run) || 0) <= now)
    .sort((a, b) => (a.next_run || 0) - (b.next_run || 0))
  if (!due.length) return []
  const job = due[0] // one job per tick
  await runJob(job, 'schedule')
  return [job.id]
}

export function statusText() {
  if (!state) state = load()
  const now = Date.now()
  const lines = state.jobs.map((j) => {
    const nxt = j.next_run ? new Date(j.next_run).toLocaleString(undefined, { hour12: false }) : '-'
    const last = j.last_end ? new Date(j.last_end).toLocaleString(undefined, { hour12: false }) : '-'
    const everySec = Number(j.every)
    const atJob = !everySec && typeof j.at === 'string' && /^\d{1,2}:\d{2}$/.test(j.at)
    const span = Math.max(60, everySec || 3600) * 1000
    // at means once a day; do not judge staleness by 2x the default hourly span.
    const staleAfter = atJob ? Math.max(2 * span, 24 * 3600 * 1000) : 2 * span
    const stale = j.enabled && j.last_end && now - j.last_end > staleAfter
    return `${j.enabled ? '●' : '○'} ${String(j.id).padEnd(8)} ${String(j.last_status || 'never').padEnd(6)} last ${last}  next ${nxt}` +
      (j.failures ? `  fails ${j.failures}` : '') + (stale ? '  WARN: overdue' : '')
  })
  return `${state.jobs.length} jobs, ${running.size} running\n` + lines.join('\n')
}

const statusTool = defineTool({
  name: 'jobs_status',
  description: 'Show the in-process job scheduler: each job\'s last status/time, next run, failure streak, and a stale warning when a job has not succeeded for two intervals.',
  parameters: {},
  output: textOut,
  async execute() { return { text: statusText() } },
})

const runTool = defineTool({
  name: 'jobs_run',
  description: 'Run one job immediately by id (for testing). Returns its terminal status.',
  parameters: { id: { type: 'string', required: true, description: 'job id, e.g. example' } },
  output: textOut,
  async execute(args) {
    const st = state || load()
    const job = st.jobs.find((j) => j.id === args.id)
    if (!job) return { text: 'no such job: ' + args.id }
    const r = await runJob(job, 'manual')
    return { text: `${args.id}: ${r.status || 'skipped:' + r.skipped}` }
  },
})

export function apply(ctx) {
  state = load()
  ctx.tools.register(statusTool)
  ctx.tools.register(runTool)
  // ctx.setInterval follows the plugin scope and is cleared on unload.
  const setInt = typeof ctx.setInterval === 'function' ? ctx.setInterval.bind(ctx) : setInterval
  interval = setInt(() => { tick().catch(() => { /* never throw into the host */ }) }, TICK_MS)
}
