/**
 * guard 回归测试：node test-guard.mjs
 * 两层：checkCommand 白名单；pre-execute 里 subagent 自动上账（写临时目录，不碰真台账）。
 */
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { apply, checkCommand, ledgerPath } from './index.js'

const CASES = [
  // 借道命令：必须 BLOCK
  ['python3 bin/core.py status; rm -rf /srv/data/memory', false, '分号借道'],
  ['python3 bin/core.py status && rm -rf /srv/data/memory', false, '&& 借道'],
  ['python3 bin/core.py status || rm -rf /srv/x', false, '|| 借道'],
  ['python3 bin/core.py status | tee /tmp/x', false, '管道'],
  ['python3 bin/core.py status > /srv/x', false, '重定向'],
  ['python3 bin/core.py status & rm -rf /srv/x', false, '后台符'],
  ['python3 bin/core.py status\nrm -rf /srv/x', false, '换行接命令'],
  ['python3 bin/core.py "$(rm -rf /srv/x)"', false, '双引号里的命令替换'],
  ['python3 bin/core.py `rm -rf /srv/x`', false, '反引号'],
  ['python3 bin/core.py status;', false, '结尾分号'],
  ['python3 -c "import os"', false, 'python3 -c'],
  ['cat bin/core.py', false, 'head 不是 python3/bin'],
  ['bash bin/core.py', false, 'bash 套壳'],
  ['bin/core.py status "unterminated', false, '引号没闭合'],
  ['bin/queue.py add --title "a;b" && rm -rf /srv/x', false, '引号后接借道'],

  // 正常命令：必须 PASS
  ['python3 ~/.dsh/agent-core/bin/core.py status', true, '正常记账'],
  ['bin/queue.py add --title "a;b"', true, '引号里的分号'],
  ['bin/core.py store --title a --said "他说了啥" --heart "我心里" --source s1', true, '带引号参数'],
  ["bin/core.py search '词; rm -rf /' --limit 5", true, '单引号里的分号'],
  ['python3 bin/core.py status --json', true, '-- 参数'],
  ['bin/fact.py list', true, 'fact.py'],
  ['python3 /opt/agent/bin/core.py status', true, '绝对路径'],
  ['bin/core.py get 20261111-210155', true, '参数带连字符'],
  ['python3 bin/ledger.py list --pending', true, 'ledger.py 台账'],
  ['bin/ledger.py summary', true, 'ledger.py 直接跑'],
]

let pass = 0
let fail = 0
for (const [cmd, want, note] of CASES) {
  const got = checkCommand(cmd)
  const ok = got.ok === want
  if (ok) pass += 1
  else fail += 1
  const label = want ? 'PASS' : 'BLOCK'
  const flag = ok ? 'ok  ' : 'FAIL'
  console.log(`${flag} 期望 ${label}  实际 ${got.ok ? 'PASS' : 'BLOCK'}  ${note}  <= ${JSON.stringify(cmd)}${ok ? '' : '   why=' + got.why}`)
}
console.log(`\n${pass} passed, ${fail} failed, ${CASES.length} cases`)

// —— 台账自动记账：模拟宿主 pre-execute，CORE_HOME 指到临时目录 ——
const ledgerFailStart = fail
const prevHome = process.env.CORE_HOME
const ledgerTmp = mkdtempSync(join(tmpdir(), 'guard-ledger-'))
process.env.CORE_HOME = ledgerTmp

function check(label, ok, detail = '') {
  if (ok) pass += 1
  else fail += 1
  console.log(`${ok ? 'ok  ' : 'FAIL'}  ${label}${ok || !detail ? '' : '   ' + detail}`)
}

try {
  const handlers = new Map()
  apply({ on: (name, fn) => handlers.set(name, fn) })
  const hook = handlers.get('tools/pre-execute')
  check('挂上 tools/pre-execute', typeof hook === 'function')

  let nextCalled = 0
  const stub = {
    name: 'subagent',
    arguments: { prompt: '把派单手册的回执纪律补一句台账\n再跑一遍测试' },
    agent: { session: { id: 'stub-session-001' } },
  }
  const gate = hook(stub, () => { nextCalled += 1; return 'NEXT' })
  check('subagent 放行、不改行为', gate === 'NEXT' && nextCalled === 1, `gate=${gate} next=${nextCalled}`)

  const file = ledgerPath()
  const lines = existsSync(file) ? readFileSync(file, 'utf8').trim().split('\n').filter(Boolean) : []
  check('台账多了一行', lines.length === 1, `实际 ${lines.length} 行`)
  const rec = lines.length ? JSON.parse(lines[lines.length - 1]) : {}
  check('type=dispatch', rec.type === 'dispatch', JSON.stringify(rec))
  check('tool=subagent', rec.tool === 'subagent')
  check('session=stub-session-001', rec.session === 'stub-session-001')
  check('summary 是 prompt 压成一行、≤120 字', typeof rec.summary === 'string'
    && rec.summary.length > 0 && rec.summary.length <= 120 && !rec.summary.includes('\n'),
    JSON.stringify(rec.summary))
  check('at 是本地时区 ISO 到秒', /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$/.test(rec.at || ''), rec.at)

  hook({ name: 'bash', arguments: { command: 'python3 bin/ledger.py list' }, agent: {} }, () => 'NEXT')
  const after = readFileSync(file, 'utf8').trim().split('\n').filter(Boolean)
  check('别的工具不记台账', after.length === 1, `实际 ${after.length} 行`)

  process.env.CORE_HOME = '/dev/null/ledger-home'
  const gate2 = hook({ name: 'subagent_fork', arguments: { prompt: 'x' }, agent: {} }, () => 'NEXT')
  check('写台账失败也不拦工具', gate2 === 'NEXT')
} finally {
  try { rmSync(ledgerTmp, { recursive: true, force: true }) } catch { /* 清不掉不算失败 */ }
  if (prevHome === undefined) delete process.env.CORE_HOME
  else process.env.CORE_HOME = prevHome
}

if (fail !== ledgerFailStart) console.log('台账测试有失败项')
console.log(`\n合计 ${pass} passed, ${fail} failed`)
process.exit(fail === 0 ? 0 : 1)
