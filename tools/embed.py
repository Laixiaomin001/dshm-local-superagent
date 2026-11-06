#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""嵌入工具：只有百炼一条通道。

provider=bailian（唯一通道，也是默认）：阿里百炼 OpenAI 兼容端点，模型
text-embedding-v3，dimensions=1024。一次最多 10 条（保守），单条超时 10 秒，
失败重试 2 次（退避 0.5/1.5 秒）；批量那条路不稳时退回逐条，整批作废就抛错。
本机 ollama 与宿主桥那两条已经拆掉（2026-11-06），不再有本地向量。

纯标准库。用法：

    python3 embed.py --text "一句话"
    python3 embed.py --text "一句话" --full
    python3 embed.py --file 文本.txt
    python3 embed.py --check
    python3 embed.py --check --provider bailian
    python3 embed.py --text "一句话" --provider bailian|auto

环境变量（自测/换通道用，平时不用管）：
    DSHM_BAILIAN_KEY     百炼凭证文件，默认 凭证/百炼.key
    DSHM_BAILIAN_URL     百炼端点，默认 dashscope compatible-mode/v1/embeddings
    DSHM_BAILIAN_MODEL   百炼模型名，默认 text-embedding-v3
    DSHM_CA_FILE         TLS 根证书，默认 /etc/ssl/certs/cacert.pem
"""

import argparse
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request

BAILIAN_KEY_FILE = os.environ.get(
    "DSHM_BAILIAN_KEY",
    "/storage/Users/currentUser/Documents/小敏/凭证/百炼.key",
)
BAILIAN_URL = os.environ.get(
    "DSHM_BAILIAN_URL",
    "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings",
).rstrip("/")
BAILIAN_MODEL = os.environ.get("DSHM_BAILIAN_MODEL", "text-embedding-v3")
# 本机内嵌 CPython 没有默认 CA 路径（ssl.get_default_verify_paths 的 capath
# 指向 /etc/ssl/certs，但那里只有 cacert.pem，没有按 hash 命名的链接），
# 不显式指文件就会 CERTIFICATE_VERIFY_FAILED。2026-11-06 实测。
CA_FILE = os.environ.get("DSHM_CA_FILE", "/etc/ssl/certs/cacert.pem")

EXPECT_DIM = 1024          # 向量总宽 1024 维
BAILIAN_DIM = 1024         # text-embedding-v3 指了 dimensions=1024
BAILIAN_TIMEOUT = 10       # 百炼单次 HTTP 超时（秒）
PROBE_TIMEOUT = 8          # 探活（maintain 选路用）短一点
RETRY_DELAYS = ()  # 失败重试退避，共重试 2 次
CHUNK = {"bailian": 10}              # 各通道的批量上限
PROVIDERS = ("bailian",)             # 全部通道
AUTO_ORDER = ("bailian",)            # auto 选路顺序
DEFAULT_CHUNK = CHUNK["bailian"]     # 保持旧名字，外部引用不受影响

PROBE_TEXT = "探活：embed.py provider probe"
DEGRADE_PROVIDER = "bailian"

_USAGE = {"prompt_tokens": 0, "total_tokens": 0, "calls": 0}
LAST_PROVIDER = None   # 最近一次公开调用实际走的通道
_SSL_CTX = None


class EmbedError(Exception):
    """嵌入调用失败。kind 标清是哪一类，消息里带清因。"""

    def __init__(self, message, kind="error", retryable=False):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable


def _one_line(e):
    return str(e).strip().splitlines()[0][:220] if str(e).strip() else type(e).__name__


def _reset_usage():
    _USAGE["prompt_tokens"] = 0
    _USAGE["total_tokens"] = 0
    _USAGE["calls"] = 0


def last_usage():
    """最近一次公开调用累计的 usage（百炼响应里的真实数字）。"""
    return dict(_USAGE)


# ---------------------------------------------------------------- 凭证

def read_api_key():
    """读百炼凭证：裸 key 或 JSON 都认。任何错误消息里都不带 key 内容。"""
    path = BAILIAN_KEY_FILE
    if not os.path.isfile(path):
        raise EmbedError("百炼凭证不存在：%s" % path, kind="key")
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
    except OSError as e:
        raise EmbedError("百炼凭证读不了：%s（%s）" % (path, e), kind="key")
    except UnicodeDecodeError as e:
        raise EmbedError("百炼凭证不是 UTF-8：%s（%s）" % (path, e), kind="key")
    if not raw:
        raise EmbedError("百炼凭证是空文件：%s" % path, kind="key")
    key = raw
    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
        except ValueError as e:
            raise EmbedError("百炼凭证像 JSON 但解析不了：%s（%s）" % (path, e), kind="key")
        if not isinstance(obj, dict):
            raise EmbedError("百炼凭证 JSON 不是对象：%s" % path, kind="key")
        for k in ("key", "api_key", "apiKey", "token", "access_key", "DASHSCOPE_API_KEY"):
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                key = v.strip()
                break
        else:
            raise EmbedError(
                "百炼凭证 JSON 里没有可用的 key 字段（有：%s）"
                % ",".join(sorted(str(x) for x in obj)),
                kind="key",
            )
    key = key.strip().strip('"').strip("'").strip()
    if not key:
        raise EmbedError("百炼凭证内容为空：%s" % path, kind="key")
    return key


def _mask_key(key):
    # 只露前缀和长度，够判断「是不是同一个 key」，不够被抄走。
    return (key[:3] + "…") if len(key) > 3 else "…"


# ---------------------------------------------------------------- HTTP

def _ssl_context():
    global _SSL_CTX
    if _SSL_CTX is None:
        cafile = CA_FILE if os.path.isfile(CA_FILE) else None
        try:
            if cafile:
                _SSL_CTX = ssl.create_default_context(cafile=cafile)
            else:
                _SSL_CTX = ssl.create_default_context()
        except Exception as e:
            raise EmbedError(
                "建 TLS 上下文失败：%s\n  可把 DSHM_CA_FILE 指到一份 cacert.pem" % e,
                kind="tls",
            )
    return _SSL_CTX


def _tls_error(stage, reason):
    return EmbedError(
        "%s TLS 失败：%s\n"
        "  本机内嵌 Python 没有默认 CA，已尝试 %s；可用 DSHM_CA_FILE 换一份根证书。"
        % (stage, reason, CA_FILE),
        kind="tls",
    )


def _post_json(url, payload, headers, timeout, stage):
    """POST 一个 JSON，返回解析后的对象。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        if e.code in (401, 403):
            raise EmbedError(
                "%s 拒绝凭证（HTTP %d）%s"
                % (stage, e.code, ("：" + body) if body else ""),
                kind="denied",
            )
        raise EmbedError(
            "%s 返回 HTTP %d%s" % (stage, e.code, ("：" + body) if body else ""),
            kind="http",
            retryable=(e.code == 429 or e.code >= 500),
        )
    except (socket.timeout, TimeoutError):
        raise EmbedError(
            "%s 超时（%d 秒没回）" % (stage, timeout), kind="timeout", retryable=True
        )
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, ssl.SSLError) or "CERTIFICATE_VERIFY_FAILED" in str(reason):
            raise _tls_error(stage, reason)
        raise EmbedError(
            "%s 网络失败：%s" % (stage, reason), kind="network", retryable=True
        )
    except ssl.SSLError as e:
        raise _tls_error(stage, e)

    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except ValueError as e:
        raise EmbedError(
            "%s 返回的不是合法 JSON（%s），原文前 300 字：%s" % (stage, e, text[:300]),
            kind="parse",
        )


# ---------------------------------------------------------------- 百炼通道

def _invoke_bailian(texts, timeout=BAILIAN_TIMEOUT):
    """调阿里百炼 OpenAI 兼容端点，返回等长向量列表（顺序与输入一致）。"""
    key = read_api_key()
    payload = {
        "model": BAILIAN_MODEL,
        "input": texts,
        "dimensions": BAILIAN_DIM,
        "encoding_format": "float",
    }
    d = _post_json(
        BAILIAN_URL, payload, {"Authorization": "Bearer " + key},
        timeout, "调百炼 embedding",
    )
    usage = d.get("usage") if isinstance(d, dict) else None
    if isinstance(usage, dict):
        _USAGE["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        _USAGE["total_tokens"] += int(usage.get("total_tokens") or 0)
        _USAGE["calls"] += 1

    data = d.get("data") if isinstance(d, dict) else None
    if not isinstance(data, list) or not data:
        raise EmbedError(
            "百炼返回里没有非空 data 数组：%s"
            % json.dumps(d, ensure_ascii=False)[:300],
            kind="parse",
        )
    # 返回顺序要和输入一致：带 index 就按 index 排，并核对是否 0..n-1。
    if all(isinstance(x, dict) and isinstance(x.get("index"), int) for x in data):
        idx = [x["index"] for x in data]
        if sorted(idx) != list(range(len(texts))):
            raise EmbedError(
                "百炼返回的 index 对不上：%s（发了 %d 条）" % (idx, len(texts)),
                kind="parse",
            )
        data = sorted(data, key=lambda x: x["index"])
    elif len(data) != len(texts):
        raise EmbedError(
            "百炼返回条数不对：发了 %d 条，回了 %d 条" % (len(texts), len(data)),
            kind="parse",
        )

    vectors = []
    for pos, item in enumerate(data):
        emb = item.get("embedding") if isinstance(item, dict) else None
        if not isinstance(emb, list):
            raise EmbedError(
                "百炼第 %d 条结果没有 embedding 数组：%s"
                % (pos, json.dumps(item, ensure_ascii=False)[:200]),
                kind="parse",
            )
        vec = [float(x) for x in emb]
        if len(vec) != BAILIAN_DIM:
            raise EmbedError(
                "百炼维度不对：第 %d 条是 %d 维，%s（dimensions=%d）应该是 %d 维"
                % (pos, len(vec), BAILIAN_MODEL, BAILIAN_DIM, BAILIAN_DIM),
                kind="dim",
            )
        vectors.append(vec)
    if len(vectors) != len(texts):
        raise EmbedError(
            "百炼返回条数不对：发了 %d 条，回了 %d 条" % (len(texts), len(vectors)),
            kind="parse",
        )
    return vectors


def _invoke(provider, texts, timeout=None):
    return _invoke_bailian(texts, BAILIAN_TIMEOUT if timeout is None else timeout)


def _with_retry(provider, texts, timeout=None):
    last = None
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            return _invoke(provider, texts, timeout)
        except EmbedError as e:
            last = e
            if not e.retryable or attempt >= len(RETRY_DELAYS):
                raise
            time.sleep(RETRY_DELAYS[attempt])
    raise last


def _embed_chunks(provider, texts, chunk):
    out = []
    for start in range(0, len(texts), chunk):
        part = texts[start:start + chunk]
        try:
            out.extend(_with_retry(provider, part))
        except EmbedError as e:
            # 批量那条路不稳时，退回逐条，别整批作废
            if len(part) > 1 and e.retryable:
                for t in part:
                    out.append(_with_retry(provider, [t])[0])
            else:
                raise
    return out


# ---------------------------------------------------------------- 公开接口

def _check_texts(texts):
    if not texts:
        return
    for i, t in enumerate(texts):
        if not isinstance(t, str) or not t.strip():
            raise ValueError("第 %d 条不是非空字符串：%r" % (i, t))


def _channel_label(provider):
    return "百炼（%s）" % BAILIAN_MODEL


def _all_failed(errs):
    lines = "\n".join("  %s：%s" % (_channel_label(p), msg) for p, msg in errs)
    return EmbedError("通道不可用：\n" + lines, kind="all-failed")


def probe(provider=DEGRADE_PROVIDER, timeout=PROBE_TIMEOUT):
    """真调一次单条嵌入，判断通道通不通（不做重试，快失败）。返回实际 provider。"""
    provider = (provider or DEGRADE_PROVIDER).lower()
    if provider not in ("auto",) + PROVIDERS:
        raise ValueError("provider 只能是 bailian/auto：%r" % provider)
    global LAST_PROVIDER
    _reset_usage()
    if provider == "auto":
        errs = []
        for p in AUTO_ORDER:
            try:
                _invoke(p, [PROBE_TEXT], timeout)
                LAST_PROVIDER = p
                return p
            except EmbedError as e:
                errs.append((p, _one_line(e)))
        raise _all_failed(errs)
    try:
        _invoke(provider, [PROBE_TEXT], timeout)
    except EmbedError as e:
        raise EmbedError(
            "%s 通道探活失败：%s" % (provider, _one_line(e)), kind=e.kind, retryable=False
        )
    LAST_PROVIDER = provider
    return provider


def embed_text(text, provider=DEGRADE_PROVIDER):
    """单条文本 -> 1024 维向量。provider=auto 时按通道自动选。"""
    _check_texts([text])
    return embed_batch([text], provider=provider)[0]


def embed_batch(texts, chunk=None, provider=DEGRADE_PROVIDER):
    """多条文本 -> 等长向量列表。百炼 chunk=10。

    provider=auto：只有百炼一条，探第一条通不通，通了整批用到底；中途失败整批失败，
    由调用方决定重跑。
    """
    texts = list(texts)
    if not texts:
        return []
    provider = (provider or DEGRADE_PROVIDER).lower()
    if provider not in ("auto",) + PROVIDERS:
        raise ValueError("provider 只能是 bailian/auto：%r" % provider)
    if chunk is not None and chunk < 1:
        raise ValueError("chunk 至少是 1")
    _check_texts(texts)

    global LAST_PROVIDER
    _reset_usage()

    if provider != "auto":
        vecs = _embed_chunks(provider, texts, chunk or CHUNK[provider])
        LAST_PROVIDER = provider
        return vecs

    # auto：只有百炼，第一条单独探，成功就全程用这条通道。
    errs = []
    first, chosen = None, None
    for p in AUTO_ORDER:
        # 百炼要先有凭证，没凭证就别去撞网络
        try:
            read_api_key()
        except EmbedError as e_key:
            errs.append((p, _one_line(e_key)))
            continue
        try:
            first = _with_retry(p, [texts[0]])
            chosen = p
            break
        except EmbedError as e:
            errs.append((p, _one_line(e)))
    if chosen is None:
        raise _all_failed(errs)

    if len(texts) > 1:
        try:
            rest = _embed_chunks(chosen, texts[1:], chunk or CHUNK[chosen])
        except EmbedError as e:
            raise EmbedError(
                "%s 通道中途失败：%s\n"
                "  已嵌入 1/%d 条；为避免不同模型的向量混用，剩余没有嵌入。"
                "整批重跑，或显式 --provider bailian。"
                % (chosen, _one_line(e), len(texts)),
                kind="partial",
            )
        vecs = first + rest
    else:
        vecs = first
    LAST_PROVIDER = chosen
    return vecs


# ---------------------------------------------------------------- CLI

def _vec_out(vec, full):
    if full:
        return vec
    return [round(x, 6) for x in vec[:5]]


def cmd_text(text, full, provider=DEGRADE_PROVIDER):
    vec = embed_text(text, provider=provider)
    print(json.dumps({"dim": len(vec), "vector": _vec_out(vec, full)}, ensure_ascii=False))
    return 0


def cmd_file(path, full, chunk=None, provider=DEGRADE_PROVIDER):
    if not os.path.isfile(path):
        raise EmbedError("文件不存在：%s" % path, kind="input")
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = [ln.rstrip("\r\n") for ln in f]
    except OSError as e:
        raise EmbedError("文件读不了：%s（%s）" % (path, e), kind="input")
    except UnicodeDecodeError as e:
        raise EmbedError("文件不是 UTF-8：%s（%s）" % (path, e), kind="input")

    items = [(i + 1, ln) for i, ln in enumerate(lines) if ln.strip()]
    if not items:
        raise EmbedError("文件里没有非空行：%s" % path, kind="input")

    # 整份文件用同一条通道，别逐批探活（auto 时先定一次）。
    p = provider if provider != "auto" else probe("auto")
    step = chunk or CHUNK[p]
    for start in range(0, len(items), step):
        part = items[start:start + step]
        vecs = embed_batch([t for _, t in part], chunk=step, provider=p)
        for (lineno, text), vec in zip(part, vecs):
            print(json.dumps(
                {"line": lineno, "text": text, "dim": len(vec),
                 "vector": _vec_out(vec, full)},
                ensure_ascii=False,
            ))
        sys.stdout.flush()
    return 0


def _step(n, total, title):
    print("[%d/%d] %s ... " % (n, total, title), end="", flush=True)


def _check_bailian():
    """百炼通道三步自检；返回是否全过。"""
    ok_all = True
    vec = None
    _step(1, 3, "读百炼凭证")
    try:
        key = read_api_key()
        print("OK（%s，%d 字符）" % (_mask_key(key), len(key)))
    except EmbedError as e:
        ok_all = False
        print("失败")
        print("      %s" % str(e).replace("\n", "\n      "))

    _step(2, 3, "调 %s（dim=%d）" % (BAILIAN_MODEL, BAILIAN_DIM))
    if not ok_all:
        print("跳过")
    else:
        t0 = time.time()
        try:
            _reset_usage()
            vecs = _invoke_bailian(["自检：embed.py 百炼连通性测试"])
            vec = vecs[0]
            dt = time.time() - t0
            u = last_usage()
            print("OK（%.2f 秒，dim=%d，tokens=%d）"
                  % (dt, len(vec), int(u.get("total_tokens") or 0)))
        except EmbedError as e:
            ok_all = False
            print("失败")
            print("      %s" % str(e).replace("\n", "\n      "))

    _step(3, 3, "校验返回")
    if vec is None:
        print("跳过")
    elif len(vec) != BAILIAN_DIM:
        ok_all = False
        print("失败（dim=%d，应为 %d）" % (len(vec), BAILIAN_DIM))
    else:
        print("OK（dim=%d，前 5 个 %s）" % (len(vec), [round(x, 6) for x in vec[:5]]))
    return ok_all


def cmd_check(provider=DEGRADE_PROVIDER):
    provider = (provider or DEGRADE_PROVIDER).lower()

    if provider == "bailian":
        ok = _check_bailian()
        print("")
        print("实际走：%s" % ("bailian（阿里百炼 %s）" % BAILIAN_MODEL if ok else "无（百炼不可用）"))
        print("自检%s" % ("全过" if ok else "没过，看上面的失败项"))
        return 0 if ok else 1

    # auto：只有百炼，显示实际走的那条。
    ok = _check_bailian()
    print("")
    if ok:
        print("实际走：bailian（阿里百炼 %s）" % BAILIAN_MODEL)
        print("自检全过")
        return 0
    print("实际走：无（百炼不可用）")
    print("自检没过，看上面的失败项")
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="嵌入工具：阿里百炼 text-embedding-v3（唯一通道）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--text", help="单条文本，输出 {\"dim\":1024,\"vector\":[...]}")
    group.add_argument("--file", help="按行读文件，一行一条，输出 JSONL")
    group.add_argument("--check", action="store_true", help="自检：凭证/embedding，显示实际走的通道")
    parser.add_argument("--full", action="store_true", help="输出完整 1024 维（默认只打前 5 个）")
    parser.add_argument("--chunk", type=int, default=None,
                        help="批量每批条数；默认按通道 bailian 10")
    parser.add_argument("--provider", choices=("bailian", "auto"), default="auto",
                        help="bailian=百炼；auto=百炼（默认）")
    args = parser.parse_args(argv)

    if args.chunk is not None and args.chunk < 1:
        print("错误：--chunk 至少是 1", file=sys.stderr)
        return 2

    try:
        if args.check:
            return cmd_check(args.provider)
        if args.text is not None:
            return cmd_text(args.text, args.full, args.provider)
        return cmd_file(args.file, args.full, args.chunk, args.provider)
    except EmbedError as e:
        print("错误：%s" % str(e).replace("\n", "\n  "), file=sys.stderr)
        return 2
    except ValueError as e:
        print("参数错误：%s" % e, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
