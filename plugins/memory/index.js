/**
 * dshm-local-memory — four agent tools over a local memory store
 * (mem_search / mem_get / mem_store / mem_status).
 *
 * Rules that keep the host responsive:
 *  · apply() only registers tools: no spawn, no port probing, no file writes
 *  · every call has a hard timeout; the child is SIGKILLed when it expires
 *  · output is capped by the backend (search 2 KB, get 8 KB)
 *
 * Backend: a short-lived, standard-library-only python script
 * (LOCAL_MEMORY_PY, default $LOCAL_SUPERAGENT_HOME/memory/mem.py).
 */
import { spawn } from 'node:child_process'
import { homedir } from 'node:os'
import { join } from 'node:path'
import { defineTool } from '@deepseek-ai/dsh-tools'

export const name = 'dshm-local-memory'
export const inject = ['tools']

const HOME = process.env.LOCAL_SUPERAGENT_HOME || join(homedir(), '.local-superagent')
const MEM_PY = process.env.LOCAL_MEMORY_PY || join(HOME, 'memory', 'mem.py')
const PY = process.env.LOCAL_SUPERAGENT_PYTHON || 'python3'

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
    // Hard timeout: SIGKILL, never let one call hang the session.
    const timer = setTimeout(() => {
      try { child.kill('SIGKILL') } catch (e) { /* ignore */ }
      finish({ error: new Error('memory backend timed out after ' + timeoutMs + 'ms') })
    }, timeoutMs)
  })
}

async function callMem(args, timeoutMs = 15000) {
  const r = await runOnce(args, timeoutMs)
  if (r.error) throw new Error('memory backend failed: ' + String(r.error.message || r.error).slice(0, 600))
  if (typeof r.out !== 'string') throw new Error('memory backend returned a non-string payload')
  if (r.code !== 0) throw new Error('memory backend failed: ' + String(r.err || ('exit ' + r.code)).trim().slice(0, 600))
  return r.out.trim()
}

const textOutput = {
  schema: {
    type: 'object',
    additionalProperties: false,
    properties: { text: { type: 'string', required: true, description: 'Text output from the memory backend.' } },
  },
  render: (_args, value) => [{ type: 'text', text: value.text }],
}

const searchTool = defineTool({
  name: 'mem_search',
  description: 'Search the local long-term memory (flow ledger, FTS5). Returns at most 5 hits under 2 KB. Prefer it over reading the memory directory.',
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
    name: { type: 'string', required: true, description: 'Record file name, e.g. 20260101_1200_example.md' },
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
  description: 'Append one record to the local memory, with four fields: time, who, what was said, and a personal note. Atomic write; the index is updated in the same transaction.',
  parameters: {
    title: { type: 'string', required: true, description: 'Short title of the record.' },
    said: { type: 'string', description: 'What the person said (verbatim or near).' },
    heart: { type: 'string', description: 'A personal note about the record.' },
    who: { type: 'string', description: 'Person, optional.' },
    domain: { type: 'string', description: 'Domain tag, optional.' },
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

export function apply(ctx) {
  ctx.tools.register(searchTool)
  ctx.tools.register(getTool)
  ctx.tools.register(storeTool)
  ctx.tools.register(statusTool)
}
