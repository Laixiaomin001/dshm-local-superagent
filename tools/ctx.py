#!/usr/bin/env python3
"""只读查询 DSH 会话的上下文已用百分比。

口径是 DSH Web UI 的「采样后」版本（不含 projectedTokens 修正）：
  pressureTokens = 最后一个 assistant/message 的
                   inputTokens + cacheReadTokens + cacheWriteTokens
  contextWindow  = 最后一个 request/context 的 data.contextWindow
  percent        = round(pressureTokens / contextWindow * 100)，封顶 100

日志只读，不写不删。解压调 node 内置 zlib.zstdDecompressSync，不装第三方包。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

DSH_HOME = Path(os.environ.get("DSH_HOME", str(Path.home() / ".dsh")))
SESSIONS_ROOT = DSH_HOME / "sessions"
LOG_NAME = "session.v4.jsonl.zstd"

# node 一次读多个日志，只回元数据，避免把整段对话灌进 python。
NODE_READER = r"""
const fs = require("fs"), zlib = require("zlib");
const out = [];
for (const p of process.argv.slice(1)) {
  const r = { path: p };
  try {
    const text = zlib.zstdDecompressSync(fs.readFileSync(p)).toString("utf8");
    let meta = null, ctx = null, asst = null;
    for (const line of text.split("\n")) {
      if (!line) continue;
      let e;
      try { e = JSON.parse(line); } catch { continue; }
      if (!e) continue;
      if (e.type === "session") meta = e;
      else if (e.type === "request/context") ctx = e;
      else if (e.type === "assistant/message" && e.data && e.data.usage) asst = e;
    }
    const u = asst && asst.data && asst.data.usage;
    r.session = meta ? {
      id: meta.id || null,
      origin: meta.origin || null,
      parentSession: meta.parentSession || null,
      createdAt: meta.createdAt || null
    } : null;
    r.contextWindow = ctx && ctx.data && typeof ctx.data.contextWindow === "number"
      ? ctx.data.contextWindow : null;
    r.usage = u ? {
      inputTokens: u.inputTokens || 0,
      cacheReadTokens: u.cacheReadTokens || 0,
      cacheWriteTokens: u.cacheWriteTokens || 0,
      outputTokens: u.outputTokens || 0
    } : null;
    r.turn = asst && asst.data ? (asst.data.turn ?? null) : null;
    r.usageTime = asst ? (asst.time ?? null) : null;
  } catch (err) {
    r.error = String((err && err.message) || err);
  }
  out.push(r);
}
process.stdout.write(JSON.stringify(out));
"""


@dataclass
class Session:
    path: Path
    mtime: float
    label: str
    uuid: str
    origin: str | None = None
    parent: str | None = None
    percent: int | None = None
    pressure: int | None = None
    window: int | None = None
    turn: int | None = None
    usage_time: int | None = None
    error: str | None = None
    raw: dict = field(default_factory=dict)


def die(msg: str, code: int = 2) -> "None":
    print(f"ctx.py: {msg}", file=sys.stderr)
    raise SystemExit(code)


def discover() -> list[Path]:
    if not SESSIONS_ROOT.is_dir():
        die(f"找不到会话目录 {SESSIONS_ROOT}")
    paths = list(SESSIONS_ROOT.glob(f"*/*/{LOG_NAME}"))
    if not paths:
        die(f"{SESSIONS_ROOT} 下没有 {LOG_NAME}")
    return paths


def read_meta(paths: list[Path]) -> dict[str, dict]:
    """返回 {绝对路径: node 解析出的元数据}。"""
    if not paths:
        return {}
    try:
        proc = subprocess.run(
            ["node", "-e", NODE_READER, *[str(p) for p in paths]],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except FileNotFoundError:
        die("需要 node 来解压 zstd，但 PATH 里没有 node")
    except subprocess.TimeoutExpired:
        die("解压会话日志超时")
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        die("解压失败：" + (detail[-1] if detail else f"node 退出码 {proc.returncode}"))
    try:
        rows = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        die(f"解压结果不是合法 JSON：{exc}")
    return {str(Path(row["path"])): row for row in rows}


def make_session(path: Path, row: dict) -> Session:
    meta = row.get("session") or {}
    dirname = path.parent.name
    raw_id = meta.get("id") or dirname
    uuid = raw_id[8:] if raw_id.startswith("session-") else raw_id
    sess = Session(
        path=path,
        mtime=path.stat().st_mtime,
        label="session-" + uuid[:8],
        uuid=uuid,
    )
    sess.raw = row
    sess.origin = meta.get("origin")
    sess.parent = meta.get("parentSession")
    sess.error = row.get("error")

    window = row.get("contextWindow")
    usage = row.get("usage")
    if row.get("error"):
        return sess
    if not isinstance(window, int) or window <= 0:
        sess.error = "最后一个 request/context 没有 contextWindow"
        return sess
    if not usage:
        sess.error = "没有带 usage 的 assistant/message"
        return sess
    pressure = (
        int(usage.get("inputTokens") or 0)
        + int(usage.get("cacheReadTokens") or 0)
        + int(usage.get("cacheWriteTokens") or 0)
    )
    sess.window = window
    sess.pressure = pressure
    sess.percent = min(100, int(pressure * 100.0 / window + 0.5))
    sess.turn = row.get("turn")
    sess.usage_time = row.get("usageTime")
    return sess


def load_one(path: Path) -> Session:
    return make_session(path, read_meta([path]).get(str(path), {"path": str(path), "error": "读不到"}))


def load_many(paths: list[Path]) -> list[Session]:
    meta = read_meta(paths)
    return [make_session(p, meta.get(str(p), {"path": str(p), "error": "读不到"})) for p in paths]


def is_subagent(sess: Session) -> bool:
    return sess.origin == "subagent" or bool(sess.parent)


def resolve_one(raw: str, paths: list[Path]) -> Path:
    given = raw[8:] if raw.startswith("session-") else raw
    if not given:
        die("--session 不能是空的会话 id")
    exact, prefix = [], []
    for p in paths:
        name = p.parent.name
        bare = name[8:] if name.startswith("session-") else name
        if bare == given or name == raw:
            exact.append(p)
        elif bare.startswith(given):
            prefix.append(p)
    hits = exact or prefix
    if not hits:
        die(f"找不到会话 {raw}")
    if len({str(p.parent) for p in hits}) > 1:
        names = ", ".join(sorted({p.parent.name for p in hits}))
        die(f"会话 id {raw} 不唯一，候选：{names}")
    return hits[0]


def latest_main(paths: list[Path]) -> tuple[Path, Session]:
    ordered = sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)
    last_sub: Session | None = None
    # 子会话也是会话，但默认「当前会话」应落在用户自己的对话上。
    for p in ordered:
        sess = load_one(p)
        if not is_subagent(sess):
            return p, sess
        if last_sub is None:
            last_sub = sess
    if last_sub is not None:
        return last_sub.path, last_sub
    die("没有可读的会话")


def fmt(sess: Session) -> str:
    if sess.error:
        return f"{sess.label}  n/a  ({sess.error})"
    return (
        f"{sess.label}  {sess.percent}%  "
        f"(pressure {sess.pressure} / window {sess.window})"
    )


def iso_time(ms: int | None) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def as_json(sess: Session) -> dict:
    row = {
        "session": sess.label,
        "id": sess.uuid,
        "percent": sess.percent,
        "pressureTokens": sess.pressure,
        "contextWindow": sess.window,
        "turn": sess.turn,
        "updatedAt": iso_time(sess.usage_time),
        "path": str(sess.path),
        "origin": sess.origin or "main",
        "parentSession": sess.parent,
    }
    if sess.error:
        row["error"] = sess.error
    return row


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="ctx.py",
        description="查 DSH 会话上下文已用百分比（口径同 Web UI 采样后版本）。",
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument("--session", metavar="ID", help="指定会话：裸 uuid 或带 session- 前缀")
    g.add_argument("--all", action="store_true", help="列出所有会话，按百分比降序")
    p.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    p.add_argument("--warn", type=int, metavar="PCT", help="有会话超过该百分比时退出码为 1")
    p.add_argument(
        "--main-only",
        action="store_true",
        help="配合 --all：只列主会话，不列子会话",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    paths = discover()

    if args.all:
        sessions = load_many(paths)
        if args.main_only:
            sessions = [s for s in sessions if not is_subagent(s)]
        if not sessions:
            die("没有可读的会话")
        sessions.sort(
            key=lambda s: (s.percent if s.percent is not None else -1, s.pressure or -1),
            reverse=True,
        )
    else:
        path = resolve_one(args.session, paths) if args.session else latest_main(paths)[0]
        sessions = [load_one(path)]

    failed = [s for s in sessions if s.error]
    for s in failed:
        print(f"ctx.py: {s.label}: {s.error}", file=sys.stderr)
    if not args.json:
        for s in sessions:
            print(fmt(s))
    else:
        if args.all:
            print(json.dumps({"sessions": [as_json(s) for s in sessions]}, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(as_json(sessions[0]), ensure_ascii=False, indent=2))

    if not args.all and sessions[0].error:
        return 2
    if args.warn is not None and any(
        s.percent is not None and s.percent > args.warn for s in sessions
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
