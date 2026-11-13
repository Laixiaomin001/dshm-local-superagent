/** dsh-guard: 顶层会话不直接动手；子会话放行；记账脚本白名单；subagent 派单自动上账。 */
import { appendFileSync, mkdirSync, readFileSync, realpathSync } from 'node:fs'
import { homedir } from 'node:os'
import { basename, dirname, join, resolve } from 'node:path'

export const name = 'dsh-guard'
export const inject = ['tools']

// 引擎自己的家；账本根从 CORE_HOME 展开。两个都能用环境变量覆盖。
const DSH_HOME = process.env.DSH_HOME || join(homedir(), '.dsh')
const CORE_HOME = process.env.CORE_HOME || join(DSH_HOME, 'agent-core')

/**
 * 内置规则：policy.json 的 guard 节读不到、读坏了，就用这一份兜底。
 * 值跟搬进 policy 之前写死的一致；路径按 CORE_HOME 展开。
 */
const DEFAULT_GUARD = {
  block_tools: ['write', 'edit', 'move', 'remove', 'publish'],
  doc_tools: ['write', 'edit'],
  doc_dirs: [join(CORE_HOME, '运行'), '/tmp'],
  allow_scripts: ['core', 'fact', 'queue', 'dispatch', 'sentinel', 'style', 'review', 'vectors', 'voice', 'guard', 'ctx', 'usage', 'ledger', 'recall'],
  subagent_tools: ['subagent', 'subagent_fork'],
}

/** 取一列非空字符串；不是数组、或者一个合格的都没有，返回 null（当作没读到）。 */
function stringList(value) {
  if (!Array.isArray(value)) return null
  const out = []
  for (const item of value) {
    if (typeof item === 'string' && item.trim()) out.push(item.trim())
  }
  return out.length ? out : null
}

/**
 * 启动时读 $CORE_HOME/policy.json 的 guard 节，逐项合并：
 * 读到了（且是字符串数组）用读到的，读不到用 DEFAULT_GUARD 里同一项。
 * 文件不存在、JSON 解析失败、guard 不是对象，整份退回默认——配置坏了只会更保守，
 * 不会因为读不懂就把拦截放空。
 */
export function loadGuardPolicy(home = CORE_HOME) {
  const out = {
    block_tools: DEFAULT_GUARD.block_tools.slice(),
    doc_tools: DEFAULT_GUARD.doc_tools.slice(),
    doc_dirs: DEFAULT_GUARD.doc_dirs.slice(),
    allow_scripts: DEFAULT_GUARD.allow_scripts.slice(),
    subagent_tools: DEFAULT_GUARD.subagent_tools.slice(),
  }
  let guard
  try {
    const doc = JSON.parse(readFileSync(join(home, 'policy.json'), 'utf8'))
    guard = doc && typeof doc === 'object' ? doc.guard : null
  } catch {
    return out
  }
  if (!guard || typeof guard !== 'object' || Array.isArray(guard)) return out
  for (const key of Object.keys(out)) {
    const read = stringList(guard[key])
    if (read) out[key] = read
  }
  return out
}

/** 生效的规则（启动时那一份），给测试和排查看。 */
export const GUARD_POLICY = loadGuardPolicy()
for (const list of Object.values(GUARD_POLICY)) Object.freeze(list)
Object.freeze(GUARD_POLICY)

const BLOCK = new Set(GUARD_POLICY.block_tools)
// 顶层写文档的小口子：只有 doc_tools 里的工具、只有 doc_dirs 里的目录（含子目录）；删除类不放开
const DOC_TOOLS = new Set(GUARD_POLICY.doc_tools)
const DOC_DIRS = GUARD_POLICY.doc_dirs
const DOC_HOME = DOC_DIRS.map((d) => { try { return realpathSync(d) } catch { return resolve(d) } })
const SUBAGENT_TOOLS = new Set(GUARD_POLICY.subagent_tools)
const SUMMARY_MAX = 120
// 脚本名从 allow_scripts 拼出来；转义后再进正则，白名单里的名字只当字面量
const escapeRe = (s) => String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
const SCRIPTS = GUARD_POLICY.allow_scripts.map(escapeRe).join('|')
// 脚本必须是整条命令的 head（python3 后面那一个词，或命令本身就是 bin/xx.py），不能只是命令里出现过
const SCRIPT_PATH = new RegExp('(?:^|/)bin/(' + SCRIPTS + ')\\.py$')
const SCRIPT_REL = new RegExp('^bin/(' + SCRIPTS + ')\\.py$')
const REJECT = '顶层不直接动手：改文件、跑命令请派子会话；记账走 agent-core/bin/*.py'

/** 台账按天分文件：运行/派单台账/YYYYMMDD.jsonl。按调用时读 CORE_HOME，测试可以指到临时目录，不碰真台账。 */
export function ledgerPath(d = new Date()) {
  // 按调用时读环境变量，测试才能指到临时目录
  const home = process.env.CORE_HOME || CORE_HOME
  const p = (n) => String(n).padStart(2, '0')
  const day = `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}`
  return join(home, '运行', '派单台账', `${day}.jsonl`)
}

/** 本地时区 ISO，到秒，和 ledger.py 的 at 同一个格式。 */
export function localIso(d = new Date()) {
  const p = (n) => String(n).padStart(2, '0')
  const off = -d.getTimezoneOffset()
  const sign = off >= 0 ? '+' : '-'
  const abs = Math.abs(off)
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
    + `T${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
    + `${sign}${p(Math.floor(abs / 60))}:${p(abs % 60)}`
}

function brief(exec) {
  const a = exec && exec.arguments
  const raw = a && typeof a === 'object' ? (a.prompt ?? a.description ?? '') : ''
  const text = String(raw).replace(/\s+/g, ' ').trim()
  return text.length > SUMMARY_MAX ? text.slice(0, SUMMARY_MAX) : text
}

function sessionOf(exec) {
  const agent = exec && exec.agent
  const s = agent && agent.session
  if (s && typeof s.id === 'string') return s.id
  if (agent && typeof agent.id === 'string') return agent.id
  return ''
}

/**
 * subagent / subagent_fork 派出去时往台账追一行 dispatch。
 * 只记不拦；任何错都吞掉，不能因为写台账失败拖住工具。
 * 返回 true 表示写进去了，只给测试看。
 */
export function recordDispatch(exec) {
  try {
    if (!exec || !SUBAGENT_TOOLS.has(exec.name)) return false
    const at = new Date()
    const rec = {
      type: 'dispatch',
      at: localIso(at),
      tool: exec.name,
      summary: brief(exec),
      session: sessionOf(exec),
    }
    const file = ledgerPath(at)
    mkdirSync(dirname(file), { recursive: true })
    appendFileSync(file, JSON.stringify(rec) + '\n', 'utf8')
    return true
  } catch {
    return false
  }
}

function depthOf(exec) {
  const s = exec && exec.agent && exec.agent.session
  const h = s && s.header && typeof s.header === 'object' ? s.header : s
  const d = h && h.delegationDepth
  return typeof d === 'number' ? d : 0
}

/**
 * 每次拒绝往 运行/guard.jsonl 追一行。写盘失败只吞掉，拒绝照常返回。
 * 返回 true 表示写进去了，只给测试看。
 */
export function recordReject(exec, why) {
  try {
    const at = new Date()
    const home = CORE_HOME
    const file = join(home, '运行', 'guard.jsonl')
    const rec = {
      at: localIso(at),
      tool: String((exec && exec.name) || ''),
      cmd: String(cmdOf(exec)).slice(0, 200),
      why: String(why),
      depth: depthOf(exec),
    }
    mkdirSync(dirname(file), { recursive: true })
    appendFileSync(file, JSON.stringify(rec) + '\n', 'utf8')
    return true
  } catch {
    return false
  }
}

function isChild(agent) {
  const s = agent && agent.session
  if (!s) return false
  const h = s.header && typeof s.header === 'object' ? s.header : s
  if (h.parentSession !== undefined && h.parentSession !== null) return true
  if (h.origin === 'subagent') return true
  if (typeof h.delegationDepth === 'number' && h.delegationDepth > 0) return true
  return false
}

function cmdOf(exec) {
  const a = exec && exec.arguments
  if (typeof a === 'string') return a
  if (a && typeof a === 'object') return String(a.command ?? a.cmd ?? '')
  return ''
}

/** write/edit 的目标路径；两种工具都是 file_path，另两个写法兜底。 */
function targetPathOf(exec) {
  const a = exec && exec.arguments
  if (typeof a === 'string') return a.trim()
  if (a && typeof a === 'object') return String(a.file_path ?? a.filePath ?? a.path ?? '').trim()
  return ''
}

/**
 * 真实路径：存在就 realpath；文件还没建就 realpath 父目录再拼回来（挡住父目录是软链的绕道）；
 * 父目录也没有就退回 resolve 的结果。
 */
export function realTarget(p) {
  const abs = resolve(p)
  try { return realpathSync(abs) } catch { /* 文件还不存在 */ }
  try { return join(realpathSync(dirname(abs)), basename(abs)) } catch { /* 父目录也不存在 */ }
  return abs
}

/**
 * 顶层写文档放行判定：write/edit 且真实路径落在 DOC_HOME 下（含子目录）。
 * 命中返回真实路径，否则返回空串。move/remove/publish 永远返回空串。
 */
export function docWriteTarget(exec) {
  if (!exec || !DOC_TOOLS.has(exec.name)) return ''
  const raw = targetPathOf(exec)
  if (!raw) return ''
  const t = realTarget(raw)
  const hit = DOC_HOME.find((d) => t === d || t.startsWith(d + '/'))
  return hit ? t : ''
}

/**
 * 判一条命令能不能放行。旧版只看命令里有没有 `bin/xx.py` 子串，
 * `python3 bin/core.py status; rm -rf …` 这种借道命令能整个混过去。
 *
 * 现在要求：
 * - 整条命令只有一个命令：引号外出现 ; & | < > 换行、$(、反引号、反斜杠，直接拒；
 * - 以 `python3 ` 或 `bin/` 开头，且脚本名在白名单里（脚本必须是 head，不是路过的子串）；
 * - 引号只压住 ; & | < >（bash 在引号里不拆命令），`--` 后的参数和引号里的内容照常给；
 * - 双引号里的 $( 和反引号照样会被 bash 执行，所以不因为加引号就放行；
 * - 引号没闭合直接拒（否则后半条命令会被吞进引号里）。
 *
 * 返回 { ok, why }，why 只给测试和排查看。
 */
export function checkCommand(raw) {
  const cmd = String(raw ?? '').trim()
  if (!cmd) return { ok: false, why: '空命令' }

  const tokens = []
  let cur = ''
  let started = false
  let quote = ''
  let danger = ''
  const push = () => {
    if (!started) return
    tokens.push(cur)
    cur = ''
    started = false
  }

  for (let i = 0; i < cmd.length; i += 1) {
    const c = cmd[i]
    if (quote === "'") {
      if (c === "'") quote = ''
      else cur += c
      continue
    }
    if (quote === '"') {
      // 双引号里只有 \" \\ \$ \` 是转义，别的反斜杠留着给 bash
      if (c === '\\' && '"\\$`'.includes(cmd[i + 1] ?? '')) {
        cur += cmd[i + 1]
        i += 1
        continue
      }
      if (c === '"') {
        quote = ''
        continue
      }
      if (c === '`') {
        danger = '反引号'
        continue
      }
      if (c === '$' && cmd[i + 1] === '(') {
        danger = '$('
        continue
      }
      cur += c
      continue
    }
    if (c === "'" || c === '"') {
      quote = c
      started = true
      continue
    }
    if (c === '\n' || c === '\r') {
      danger = '换行'
      continue
    }
    if (';&|<>'.includes(c)) {
      danger = c
      continue
    }
    if (c === '`') {
      danger = '反引号'
      continue
    }
    if (c === '$' && cmd[i + 1] === '(') {
      danger = '$('
      continue
    }
    if (c === '\\') {
      danger = '反斜杠'
      i += 1
      continue
    }
    if (/\s/.test(c)) {
      push()
      continue
    }
    cur += c
    started = true
  }

  if (quote) return { ok: false, why: '引号没闭合' }
  push()
  if (danger) return { ok: false, why: '含 ' + danger }
  if (tokens.length === 0) return { ok: false, why: '空命令' }

  const [first, second] = tokens
  if (first === 'python3') {
    if (!second) return { ok: false, why: 'python3 后面没有脚本' }
    if (!SCRIPT_PATH.test(second)) return { ok: false, why: '脚本不在白名单：' + second }
    return { ok: true }
  }
  if (SCRIPT_REL.test(first)) return { ok: true }
  return { ok: false, why: '只允许 python3 或 bin/ 开头的单条记账命令' }
}

export function allowedCommand(raw) {
  return checkCommand(raw).ok
}

export function apply(ctx) {
  ctx.on('tools/pre-execute', (exec, next) => {
    if (!exec) return next()
    recordDispatch(exec)
    const n = exec.name
    if (!BLOCK.has(n) && n !== 'bash') return next()
    if (isChild(exec.agent)) return next()
    if (docWriteTarget(exec)) return next()
    if (n === 'bash') {
      const verdict = checkCommand(cmdOf(exec))
      if (verdict.ok) return next()
      recordReject(exec, verdict.why)
      return { kind: 'deny', reason: REJECT }
    }
    recordReject(exec, REJECT)
    return { kind: 'deny', reason: REJECT }
  }, { prepend: true })
}
