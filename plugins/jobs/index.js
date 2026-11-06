/**
 * xiaomin-jobs —— 进程内调度器（运行层重写，2026-11-04）
 *
 * 设计见 文档/运行层重写-设计.md。要点：
 *  · apply() 只读任务表、注册两个工具、挂一个 60 秒定时器；不 spawn、不探端口
 *  · 每条任务：数组命令（不过 shell）、硬超时（到点 SIGKILL 整组）、失败指数退避
 *  · 补跑：错过的下一次若在 2×间隔内，开机后补跑一次，否则只顺延
 *  · 抖动：算下次时加 ±10%
 *  · 防重叠：同一条在跑就跳过本拍
 */
import { spawn } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, renameSync, appendFileSync, writeFileSync, statSync } from 'node:fs'
import { defineTool } from '@deepseek-ai/dsh-tools'

export const name = 'xiaomin-jobs'
export const inject = ['tools', 'timer']

const MEM = process.env.XIAOMIN_MEM || '/storage/Users/currentUser/Documents/小敏/记忆机'
const DIR = MEM + '/运行'
const JOBS = DIR + '/jobs.json'
const RUNS = DIR + '/runs.jsonl'
const TICK_MS = Number(process.env.XIAOMIN_TICK_MS || 60_000)

const DEFAULT_JOBS = {
  jobs: [
    {
      id: 'health', title: '体检', enabled: true, every: 3600, timeout: 90,
      cmd: ['python3', '/storage/Users/currentUser/Documents/小敏/进化/工具/体检.py'],
      next_run: 0, failures: 0, last_status: null, last_end: 0,
    },
    {
      id: 'backup', title: '日备份', enabled: true, at: '03:10', timeout: 600,
      cmd: ['python3', '/storage/Users/currentUser/Documents/小敏/记忆机/工具/jiyi.py', 'backup'],
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
    if (!d || !Array.isArray(d.jobs)) throw new Error('jobs 不是数组')
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
  } catch (e) { /* 写不进就只在内存里跑 */ }
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
        cwd: '/storage/Users/currentUser/Documents/小敏',
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
  // 任务表被手改过就重新读，免得为改一个超时又重启一次宿主
  try {
    const m = statSync(JOBS).mtimeMs
    if (m !== jobsMtime) { state = load(); jobsMtime = m }
  } catch (e) { /* 文件不在就按内存里的跑 */ }
  if (!state) state = load()
  const now = Date.now()
  for (const j of state.jobs) {
    if (!j.enabled) continue
    if (!Number(j.next_run)) {
      j.next_run = now + 45_000
    } else if (j.next_run < now - 2 * Math.max(60, Number(j.every) || 3600) * 1000) {
      // 错过太久只顺延，不补跑一串陈年任务
      j.next_run = nextRunFor(j, now)
    }
  }
  save()
  const due = state.jobs
    .filter((j) => j && j.enabled && !running.has(j.id) && (Number(j.next_run) || 0) <= now)
    .sort((a, b) => (a.next_run || 0) - (b.next_run || 0))
  if (!due.length) return []
  const job = due[0] // 一拍只跑一条
  await runJob(job, 'schedule')
  return [job.id]
}

export function statusText() {
  if (!state) state = load()
  const now = Date.now()
  const lines = state.jobs.map((j) => {
    const nxt = j.next_run ? new Date(j.next_run).toLocaleString('zh-CN', { hour12: false }) : '-'
    const last = j.last_end ? new Date(j.last_end).toLocaleString('zh-CN', { hour12: false }) : '-'
    const everySec = Number(j.every)
    const atJob = !everySec && typeof j.at === 'string' && /^\d{1,2}:\d{2}$/.test(j.at)
    const span = Math.max(60, everySec || 3600) * 1000
    // at 是每天跑一次，别拿 2×every（缺省 3600 秒）的 2 小时判超期，至少给 24 小时
    const staleAfter = atJob ? Math.max(2 * span, 24 * 3600 * 1000) : 2 * span
    const stale = j.enabled && j.last_end && now - j.last_end > staleAfter
    return `${j.enabled ? '●' : '○'} ${String(j.id).padEnd(8)} ${String(j.last_status || 'never').padEnd(6)} 上次 ${last}  下次 ${nxt}` +
      (j.failures ? `  连败 ${j.failures}` : '') + (stale ? '  ⚠ 超期未成功' : '')
  })
  return `共 ${state.jobs.length} 条，运行中 ${running.size} 条\n` + lines.join('\n')
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
  parameters: { id: { type: 'string', required: true, description: 'job id, e.g. health' } },
  output: textOut,
  async execute(args) {
    const st = state || load()
    const job = st.jobs.find((j) => j.id === args.id)
    if (!job) return { text: '没有这个任务：' + args.id }
    const r = await runJob(job, 'manual')
    return { text: `${args.id}: ${r.status || 'skipped:' + r.skipped}` }
  },
})

export function apply(ctx) {
  state = load()
  ctx.tools.register(statusTool)
  ctx.tools.register(runTool)
  // ctx.setInterval 来自 timer 服务，作用域跟随插件：卸载时自动清，不用手工 clear
  const setInt = typeof ctx.setInterval === 'function' ? ctx.setInterval.bind(ctx) : setInterval
  interval = setInt(() => { tick().catch(() => { /* 不把异常抛给宿主 */ }) }, TICK_MS)
}
