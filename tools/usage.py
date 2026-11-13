#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DSH 会话 token 用量统计：主会话 / 子会话分开算，按天汇总，可选估钱。

数据来源（只读，不写不删任何会话文件）：
  ~/.dsh/sessions/<workspace>/<session-id>/session.v4.jsonl.zstd   (或 .jsonl 未压缩)

计数规则（照抄引擎 token-meter 的投影，见 @deepseek-ai/dsh-token-meter）：
  - 用量在 `assistant/message` 事件的 data.usage，形如
    {inputTokens, outputTokens, cacheReadTokens, cacheWriteTokens, totalTokens}
  - 同一 turn/step 出现多条 assistant/message 时，后一条替换前一条（不是相加）
  - `llm/retry-started` 会把替换槽清空，所以重试丢掉的请求也照实各算一次
  - usage 在 data.stream 里还会有一份副本，不重复计
  - 同一步的 usage 若完全一样（引擎重放），只算一次
  - 按天分桶用事件自己的 `time` 毫秒时间戳（本机时区），不看文件 mtime

解压：优先 python zstandard；其次 $DSH_ZSTD_BIN；其次 PATH 里的 zstd -dc；
最后用 node 现写一段 zlib 解压（不依赖 unzstd.mjs 是否存在）。

用法：
  usage.py                    # 今天
  usage.py --day 20261111     # 指定某天
  usage.py --all              # 全部日子
  usage.py --json             # 机器可读（可与上面任意一个组合）
  usage.py --prices           # 只打印价格表并退出
  usage.py --dir <sessions根>  # 换数据根目录（默认 ~/.dsh/sessions）
"""

import argparse
import collections
import datetime as _dt
import glob
import json
import os
import shutil
import subprocess
import sys

# 数据根目录：路径常量走 lib/paths，DSH_HOME 默认为 ~/.dsh
from lib.paths import DSH_HOME  # noqa: E402

DEFAULT_ROOT = str(DSH_HOME / "sessions")

# ---------------------------------------------------------------------------
# 价格表（只给估算用；来源见 PRICE_SOURCE，不确定就如实标）
# 单位：人民币元 / 百万 token。input = 未命中缓存的输入；cache_read = 缓存命中输入；
# output = 输出（含推理）；cache_write = 写缓存。
# 注意：DeepSeek 官方价通常分「缓存命中/未命中」两档输入价，一般没有单独的
# 「写缓存」收费档（写入按未命中输入价算）。这里如实标 None，估算时按
# 「未命中输入价」并写出假设。
# ---------------------------------------------------------------------------
PRICE_SOURCE = {
    "写入日期": "2026-11-11（用户手工写进脚本，脚本不联网，未核对过任何页面）",
    "参考页面": "DeepSeek 官方定价页 https://api-docs.deepseek.com/zh-cn/quick_start/pricing",
    "可信度": "不可信：数字是凭记忆填的占位值，只用来演示算法，不是官方价",
    "怎么改": "翻官方页核对后改下面 PRICING；改完跑 usage.py --prices 看一眼",
    "默认行为": "默认只给 token、不算钱；要算加 --money",
}

# 结构：provider -> {model -> {input, cache_read, output, cache_write}}，单位 元/百万 token
# None 表示「不知道/不用单收」，估算时按键 SKIPPED_PRICE_ASSUMPTION 处理
PRICING = {
    "deepseek": {
        # DeepSeek-V3.2 / deepseek-chat 档（缓存未命中 / 命中 / 输出）
        "deepseek-flash": {"input": 2.0, "cache_read": 0.2, "output": 3.0, "cache_write": None},
        "deepseek-chat": {"input": 2.0, "cache_read": 0.2, "output": 3.0, "cache_write": None},
        "deepseek-reasoner": {"input": 2.0, "cache_read": 0.2, "output": 3.0, "cache_write": None},
        "deepseek-v3.2": {"input": 2.0, "cache_read": 0.2, "output": 3.0, "cache_write": None},
        "deepseek-v3": {"input": 2.0, "cache_read": 0.5, "output": 8.0, "cache_write": None},
        "deepseek-v3.1": {"input": 4.0, "cache_read": 1.0, "output": 16.0, "cache_write": None},
        "deepseek-r1": {"input": 4.0, "cache_read": 1.0, "output": 16.0, "cache_write": None},
    },
}
CACHE_WRITE_ASSUMPTION = "写缓存按未命中输入价计（DeepSeek 不单收写缓存费）"

BUCKETS = ("input", "output", "cache_read", "cache_write")
BUCKET_LABEL = {
    "input": "未命中输入",
    "output": "输出",
    "cache_read": "缓存命中",
    "cache_write": "写缓存",
}
USAGE_FIELD = {
    "inputTokens": "input",
    "outputTokens": "output",
    "cacheReadTokens": "cache_read",
    "cacheWriteTokens": "cache_write",
}


# ---------------------------------------------------------------------------
# 解压
# ---------------------------------------------------------------------------
_NODE_DECOMPRESS = r"""
const { zstdDecompressSync } = require('node:zlib');
const { readFileSync } = require('node:fs');
// node -e 时 argv = [node, <路径>]
process.stdout.write(zstdDecompressSync(readFileSync(process.argv[1])));
"""


class Unzipper:
    """找一个可用的 zstd 解压方式，进程内缓存选好的策略。"""

    def __init__(self):
        self.mode = None
        self.why = {}

    def _pick(self):
        if self.mode:
            return self.mode
        try:
            import zstandard  # noqa: F401

            self.mode = "zstandard"
            return self.mode
        except Exception as exc:  # noqa: BLE001
            self.why["zstandard"] = str(exc)
        env_bin = os.environ.get("DSH_ZSTD_BIN")
        candidates = [env_bin] if env_bin else []
        candidates.append(shutil.which("zstd"))
        for cand in candidates:
            if cand and os.path.exists(cand):
                self.mode = "cli:" + cand
                return self.mode
        node_bin = shutil.which("node")
        if node_bin:
            self.mode = "node:" + node_bin
            return self.mode
        raise SystemExit(
            "解压失败：没有 python zstandard、没有 zstd 命令、也没有 node。"
            " 装 python-zstandard 或设 DSH_ZSTD_BIN 指向 zstd 可执行文件。"
        )

    def read(self, path):
        """返回文件的 bytes 内容（未压缩文件直接读）。"""
        if not path.endswith(".zstd"):
            with open(path, "rb") as fh:
                return fh.read()
        mode = self._pick()
        if mode == "zstandard":
            import zstandard

            with open(path, "rb") as fh:
                return zstandard.ZstdDecompressor().stream_reader(fh).read()
        if mode.startswith("cli:"):
            exe = mode[4:]
            proc = subprocess.run([exe, "-dc", "--", path], capture_output=True)
            if proc.returncode != 0:
                raise RuntimeError(
                    "%s 解压失败: %s" % (path, proc.stderr.decode("utf-8", "replace")[:300])
                )
            return proc.stdout
        node_bin = mode[5:]
        proc = subprocess.run(
            [node_bin, "-e", _NODE_DECOMPRESS, "--", path], capture_output=True
        )
        out = proc.stdout
        if proc.returncode != 0:
            raise RuntimeError(
                "%s 解压失败(node): %s" % (path, proc.stderr.decode("utf-8", "replace")[:300])
            )
        return out


# ---------------------------------------------------------------------------
# 扫日志
# ---------------------------------------------------------------------------
def find_logs(root):
    """扫 <root>/*/<session-id>/session.v4.jsonl[.zstd]。

    返回 [(路径, 目录名)]。同一个会话目录里 .zstd 和 .jsonl 都在时优先 .zstd。
    """
    out = []
    pattern = os.path.join(root, "*", "*", "session.v4.jsonl*")
    seen = set()
    for path in sorted(glob.glob(pattern)):
        base = os.path.basename(path)
        if base not in ("session.v4.jsonl.zstd", "session.v4.jsonl"):
            continue
        session_dir = os.path.dirname(path)
        key = session_dir
        # 同目录如果压缩版存在，跳过未压缩的
        if base == "session.v4.jsonl" and os.path.exists(path + ".zstd"):
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append((path, os.path.basename(session_dir)))
    return out


def zero():
    return {k: 0 for k in BUCKETS}


def add_into(dst, src):
    for k in BUCKETS:
        dst[k] += src.get(k, 0)


def sub_into(dst, src):
    for k in BUCKETS:
        dst[k] -= src.get(k, 0)


def buckets_of(usage):
    b = zero()
    for field, key in USAGE_FIELD.items():
        v = usage.get(field)
        if isinstance(v, int):
            b[key] = v
    return b


def bucket_total(b):
    return sum(b.get(k, 0) for k in BUCKETS)


class SessionStats:
    def __init__(self, sid, title=None):
        self.id = sid
        self.dir = None
        self.title = title
        self.origin = None
        self.parent = None
        self.created_at = None
        self.models = collections.Counter()
        self.by_day = collections.defaultdict(zero)
        self.total = zero()
        self.events = 0
        self.usage_events = 0
        self.replaced = 0
        self.retry_reset = 0
        self.first_time = None
        self.last_time = None
        self.file = None

    @property
    def kind(self):
        return "subagent" if self.origin == "subagent" else "main"

    def note_model(self, name):
        if name:
            self.models[name] += 1


def local_day(ms):
    return _dt.datetime.fromtimestamp(ms / 1000.0).strftime("%Y%m%d")


def scan_file(path, unz, stats_by_id):
    raw = unz.read(path)
    header = None
    sid = None
    lines = raw.split(b"\n")
    # 第一行是 header（type=session）
    parsed = []
    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line.decode("utf-8", "replace") if isinstance(line, bytes) else line)
        except Exception:  # noqa: BLE001
            continue  # 坏行跳过，最后报计数
        if i == 0 and ev.get("type") == "session":
            header = ev
            continue
        parsed.append(ev)
    if header is not None:
        sid = header.get("id")
    if sid is None:
        sid = os.path.basename(os.path.dirname(path))

    st = stats_by_id.get(sid)
    created = header.get("createdAt") if header else None
    if st is None:
        st = SessionStats(sid)
        stats_by_id[sid] = st
    st.file = path
    st.dir = os.path.dirname(path)
    if header is not None:
        st.origin = header.get("origin")
        st.parent = header.get("parentSession")
        st.created_at = created

    # 按引擎 token-meter 的规则折叠 usage：last(turn,step) 槽位替换
    last = None  # (turn, step, buckets)
    for ev in parsed:
        t = ev.get("type")
        data = ev.get("data") or {}
        ts = ev.get("time")
        if isinstance(ts, int):
            if st.first_time is None or ts < st.first_time:
                st.first_time = ts
            if st.last_time is None or ts > st.last_time:
                st.last_time = ts
        if t == "session/title":
            title = data.get("title") if isinstance(data, dict) else None
            if title:
                st.title = title
            continue
        if t == "assistant/message":
            st.events += 1
            msg = data.get("message") or {}
            src = msg.get("source") or {}
            st.note_model(src.get("model"))
        if not isinstance(ts, int):
            continue
        if t == "llm/retry-started":
            if last is not None and last[0] == data.get("turn") and last[1] == data.get("step"):
                last = None
                st.retry_reset += 1
            continue
        if t not in ("assistant/message", "assistant/attempt"):
            continue
        usage = data.get("usage")
        if usage is None:
            usage = _last_stream_usage(data.get("stream"))
        if usage is None:
            continue
        b = buckets_of(usage)
        turn, step = data.get("turn"), data.get("step")
        prev = None
        if last is not None and last[0] == turn and last[1] == step:
            prev = last[2]
            if prev == b:
                continue  # 重放，同样的数不重复加
        day = local_day(ts)
        if prev is not None:
            sub_into(st.by_day[day], prev)
            sub_into(st.total, prev)
            st.replaced += 1
        add_into(st.by_day[day], b)
        add_into(st.total, b)
        st.usage_events += 1
        last = (turn, step, b)
    return


def _last_stream_usage(stream):
    if not isinstance(stream, list):
        return None
    for item in reversed(stream):
        if not isinstance(item, dict):
            continue
        chunk = item.get("chunk")
        if isinstance(chunk, dict) and isinstance(chunk.get("usage"), dict):
            return chunk["usage"]
    return None


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
def merge(dicts):
    out = zero()
    for d in dicts:
        add_into(out, d)
    return out


def pct(part, whole):
    if not whole:
        return 0.0
    return 100.0 * part / whole


def price_for(model):
    """按模型名找价；找不到返回 None。"""
    if not model:
        return None
    name = model.strip().lower()
    for table in PRICING.values():
        if name in table:
            return table[name]
    # 前缀匹配：deepseek-flash-xxx 之类
    for table in PRICING.values():
        for key, price in table.items():
            if name.startswith(key):
                return price
    return None


def estimate_cny(model, b):
    """返回 (人民币, 是否按假设算的)。找不到价返回 (None, False)。"""
    price = price_for(model)
    if not price:
        return None, False
    assumed = False
    cost = 0.0
    unit = 1_000_000.0
    cost += (b.get("input", 0) / unit) * price.get("input", 0)
    cost += (b.get("cache_read", 0) / unit) * price.get("cache_read", 0)
    cost += (b.get("output", 0) / unit) * price.get("output", 0)
    cw = price.get("cache_write")
    if cw is None:
        cw = price.get("input", 0)
        assumed = True
    cost += (b.get("cache_write", 0) / unit) * cw
    return cost, assumed


def fmt_int(n):
    return "{:,}".format(int(n))


def fmt_pct(x):
    return "%.1f%%" % x


def main_rank(stats, key):
    return sorted(stats, key=lambda s: bucket_total(s[key]), reverse=True)


def build_report(root, day, show_all, money=False):
    unz = Unzipper()
    logs = find_logs(root)
    stats_by_id = {}
    errors = []
    for path, dirname in logs:
        try:
            scan_file(path, unz, stats_by_id)
        except Exception as exc:  # noqa: BLE001
            errors.append({"file": path, "error": str(exc)})

    all_stats = list(stats_by_id.values())
    # 过滤出窗口内的天
    def in_window(d):
        if show_all or day is None:
            return True
        return d == day

    def windowed(st):
        if show_all or day is None:
            return dict(st.total)
        return dict(st.by_day.get(day) or zero())

    out = {
        "root": root,
        "window": "all" if (show_all or day is None) else day,
        "generated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "decompress": unz.mode or "unused",
        "files": len(logs),
        "errors": errors,
        "price_source": PRICE_SOURCE,
        "cache_write_assumption": CACHE_WRITE_ASSUMPTION,
    }

    # 按天
    by_day = collections.defaultdict(lambda: {"main": zero(), "subagent": zero(), "total": zero()})
    for st in all_stats:
        for d, b in st.by_day.items():
            kind = st.kind
            add_into(by_day[d][kind], b)
            add_into(by_day[d]["total"], b)
    out["days"] = {}
    for d in sorted(by_day):
        if not in_window(d):
            continue
        row = by_day[d]
        out["days"][d] = {
            "main": row["main"],
            "subagent": row["subagent"],
            "total": row["total"],
            "main_pct": pct(bucket_total(row["main"]), bucket_total(row["total"])),
            "subagent_pct": pct(bucket_total(row["subagent"]), bucket_total(row["total"])),
        }

    # 窗口内总量
    main_t = zero()
    sub_t = zero()
    for st in all_stats:
        w = windowed(st)
        if st.kind == "subagent":
            add_into(sub_t, w)
        else:
            add_into(main_t, w)
    total_t = merge([main_t, sub_t])
    out["totals"] = {
        "main": main_t,
        "subagent": sub_t,
        "total": total_t,
        "main_pct": pct(bucket_total(main_t), bucket_total(total_t)),
        "subagent_pct": pct(bucket_total(sub_t), bucket_total(total_t)),
    }

    # 会话排行（窗口内有数的才算）
    sess_rows = []
    for st in all_stats:
        w = windowed(st)
        if bucket_total(w) == 0:
            continue
        models = sorted(st.models, key=lambda m: -st.models[m]) if st.models else []
        model = models[0] if models else None
        cny, assumed = estimate_cny(model, w) if money else (None, False)
        sess_rows.append(
            {
                "id": st.id,
                "kind": st.kind,
                "origin": st.origin,
                "parent": st.parent,
                "title": st.title,
                "models": models,
                "buckets": w,
                "total": bucket_total(w),
                "share_pct": 0.0,
                "estimate_cny": None if cny is None else round(cny, 4),
                "price_assumed": assumed,
                "usage_events": st.usage_events,
                "retries": st.retry_reset,
                "file": st.file,
            }
        )
    grand = sum(r["total"] for r in sess_rows)
    for r in sess_rows:
        r["share_pct"] = pct(r["total"], grand)
    sess_rows.sort(key=lambda r: -r["total"])
    out["sessions"] = sess_rows
    out["session_count"] = {
        "main": sum(1 for s in all_stats if s.kind == "main"),
        "subagent": sum(1 for s in all_stats if s.kind == "subagent"),
        "with_usage": len(sess_rows),
    }
    return out


def print_text(rep, money=False):
    w = rep["window"]
    label = "全部" if w == "all" else "%s-%s-%s" % (w[0:4], w[4:6], w[6:8])
    tot = rep["totals"]
    print("DSH token 用量  ·  %s  ·  生成于 %s" % (label, rep["generated_at"]))
    print("数据根目录：%s   日志文件 %d 个   解压方式 %s" % (rep["root"], rep["files"], rep["decompress"]))
    print("")

    def block(name, b):
        t = bucket_total(b)
        print(
            "  %-8s 合计 %14s  未命中输入 %12s  输出 %10s  缓存命中 %14s  写缓存 %10s"
            % (
                name,
                fmt_int(t),
                fmt_int(b.get("input", 0)),
                fmt_int(b.get("output", 0)),
                fmt_int(b.get("cache_read", 0)),
                fmt_int(b.get("cache_write", 0)),
            )
        )

    print("总 token（四项分开）")
    block("总计", tot["total"])
    block("主会话", tot["main"])
    block("子会话", tot["subagent"])
    print(
        "  占比：主会话 %s   子会话 %s"
        % (fmt_pct(tot["main_pct"]), fmt_pct(tot["subagent_pct"]))
    )
    print(
        "  会话数：主会话 %d，子会话 %d，其中有 usage 的 %d"
        % (
            rep["session_count"]["main"],
            rep["session_count"]["subagent"],
            rep["session_count"]["with_usage"],
        )
    )
    print("")

    days = rep["days"]
    if len(days) > 1:
        print("按天（前 10）")
        for d, row in sorted(days.items(), key=lambda kv: -bucket_total(kv[1]["total"]))[:10]:
            print(
                "  %s-%s-%s  合计 %12s  主 %12s (%s)  子 %12s (%s)"
                % (
                    d[0:4],
                    d[4:6],
                    d[6:8],
                    fmt_int(bucket_total(row["total"])),
                    fmt_int(bucket_total(row["main"])),
                    fmt_pct(row["main_pct"]),
                    fmt_int(bucket_total(row["subagent"])),
                    fmt_pct(row["subagent_pct"]),
                )
            )
        print("")

    print("最贵的会话（前 10）")
    for i, r in enumerate(rep["sessions"][:10], 1):
        title = (r["title"] or "").replace("\n", " ")[:28]
        parent = r["parent"] or "-"
        money = ""
        if r["estimate_cny"] is not None:
            money = "  ≈¥%.2f%s" % (r["estimate_cny"], "*" if r["price_assumed"] else "")
        print(
            "  %2d. %-8s %-36s %14s  %5s   父 %s  %s%s"
            % (
                i,
                "子会话" if r["kind"] == "subagent" else "主会话",
                r["id"],
                fmt_int(r["total"]),
                fmt_pct(r["share_pct"]),
                parent[:20],
                title,
                money,
            )
        )
    print("")

    if not money:
        print("估算费用：未算（默认只给 token；加 --money 才按脚本里的价格表估，价格表未核对）")
        if rep["errors"]:
            print("")
            print("读不了的文件 %d 个：" % len(rep["errors"]))
            for e in rep["errors"]:
                print("  %s  %s" % (e["file"], e["error"][:120]))
        return

    money_total = 0.0
    money_ok = 0
    assumed_any = False
    for r in rep["sessions"]:
        if r["estimate_cny"] is not None:
            money_total += r["estimate_cny"]
            money_ok += 1
            assumed_any = assumed_any or r["price_assumed"]
    unpriced = [r for r in rep["sessions"] if r["estimate_cny"] is None]
    print("估算费用")
    if money_ok:
        print(
            "  已计价 %d 个会话  ≈¥%.2f%s"
            % (money_ok, money_total, "（* 含写缓存按未命中输入价估的假设）" if assumed_any else "")
        )
    if unpriced:
        models = sorted({m for r in unpriced for m in (r["models"] or [None])})
        print("  %d 个会话的模型不在价目表里，未计价：%s" % (len(unpriced), ", ".join(m or "未知" for m in models)))
        print("  要加价：改本脚本 PRICING 表")
    else:
        print("  全部会话都已计价")
    print("  价格来源：" + PRICE_SOURCE["写入日期"] + "；" + PRICE_SOURCE["可信度"])
    print("  写缓存假设：" + CACHE_WRITE_ASSUMPTION)
    if rep["errors"]:
        print("")
        print("读不了的文件 %d 个：" % len(rep["errors"]))
        for e in rep["errors"]:
            print("  %s  %s" % (e["file"], e["error"][:120]))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="统计 DSH 会话 token 用量（主会话/子会话分开）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="例：usage.py / usage.py --day 20261111 / usage.py --all / usage.py --json",
    )
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--day", metavar="YYYYMMDD", help="统计某一天（按事件时间戳，本机时区）")
    g.add_argument("--all", action="store_true", help="统计全部日子")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    ap.add_argument("--prices", action="store_true", help="只打印价格表")
    ap.add_argument("--money", action="store_true", help="按脚本里的价格表估算人民币（价格未核对，谨慎）")
    ap.add_argument("--dir", default=DEFAULT_ROOT, help="sessions 根目录（默认 %(default)s）")
    args = ap.parse_args(argv)

    if args.prices:
        print(json.dumps({"source": PRICE_SOURCE, "pricing": PRICING,
                          "cache_write_assumption": CACHE_WRITE_ASSUMPTION},
                         ensure_ascii=False, indent=2))
        return 0

    day = None
    if args.day:
        try:
            _dt.datetime.strptime(args.day, "%Y%m%d")
        except ValueError:
            ap.error("--day 要写成 YYYYMMDD，比如 20261111")
        day = args.day
    if not args.day and not args.all:
        day = _dt.datetime.now().strftime("%Y%m%d")

    if not os.path.isdir(args.dir):
        print("找不到目录：%s" % args.dir, file=sys.stderr)
        return 2

    rep = build_report(args.dir, day, args.all, money=args.money)
    if args.json:
        json.dump(rep, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        print_text(rep, money=args.money)
    return 0


if __name__ == "__main__":
    sys.exit(main())
