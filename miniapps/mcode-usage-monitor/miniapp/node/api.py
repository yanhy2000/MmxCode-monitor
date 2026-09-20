#!/usr/bin/env python3
"""mcode-usage-monitor Mini App 的数据后端（一次性 CLI 模式）。

由 Node 入口(server.mjs)按需 spawn，查询结果以单个 JSON 写到 stdout 后退出。
不做 HTTP 监听：Mini App 运行时内 Node 到本机端口的直连不可用，子进程 stdout 是稳定通道。
数据逻辑与仓库根目录 monitor.py 保持一致:
  - SQLite 官方 backup API 只读快照(不锁库, 不影响运行中的 MiniMax Code)
  - 按 msg_id 跨会话去重(保留最早一条)
  - 时间分桶 / 模型聚合 / 最近调用

用法: python api.py [--range all|1h|24h|7d|30d] [--models a,b] [--db PATH]
"""

import argparse
import base64
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def default_db_path() -> Path:
    data_dir = os.environ.get("MINIMAX_DATA_DIR") or (Path.home() / ".minimax")
    return Path(data_dir) / "v2" / "sqlite" / "runtime-state.sqlite"


def open_snapshot(db_path: Path) -> sqlite3.Connection:
    """只读快照: 官方 backup API 优先, 极端锁场景回退到复制三件套。"""
    try:
        uri = f"file:{db_path.as_posix()}?mode=ro"
        src = sqlite3.connect(uri, uri=True, timeout=3)
        dst = sqlite3.connect(":memory:")
        with dst:
            src.backup(dst)
        src.close()
        return dst
    except sqlite3.Error:
        tmpdir = Path(tempfile.mkdtemp(prefix="mcode-usage-monitor-"))
        try:
            base = tmpdir / "snap.db"
            shutil.copy2(db_path, base)
            for suffix in ("-wal", "-shm"):
                side = Path(str(db_path) + suffix)
                if side.exists():
                    shutil.copy2(side, Path(str(base) + suffix))
            con = sqlite3.connect(str(base))
            con.execute("PRAGMA journal_mode=DELETE")
            return con
        except Exception:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise


MSG_SQL = """
SELECT m.created_at_ms AS ts,
       json_extract(m.data_json,'$.context_usage_telemetry.model') AS model,
       m.session_id,
       m.turn_id,
       json_extract(m.data_json,'$.usage.input_tokens')    AS inp,
       json_extract(m.data_json,'$.usage.output_tokens')   AS outp,
       json_extract(m.data_json,'$.usage.cache_read')      AS cr,
       json_extract(m.data_json,'$.usage.request_duration_ms') AS dur,
       s.title AS session_title,
       m.msg_id
FROM local_runtime_message_rows m
LEFT JOIN local_runtime_sessions s ON s.session_id = m.session_id
WHERE json_extract(m.data_json,'$.usage') IS NOT NULL
ORDER BY m.created_at_ms, m.id
"""

LEDGER_SQL = """
SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens), SUM(reasoning_tokens),
       SUM(cache_read_tokens), SUM(cache_write_tokens), MIN(ts), MAX(ts),
       COUNT(DISTINCT session_id), COUNT(DISTINCT turn_id)
FROM local_runtime_token_usage
"""


def dedup_by_msg_id(rows):
    """按 msg_id 跨会话去重(保留最早一条), 返回 (去重后的行, 丢弃行数)。"""
    seen = set()
    out = []
    dropped = 0
    for r in rows:
        mid = r[-1]
        if mid is None or mid not in seen:
            if mid is not None:
                seen.add(mid)
            out.append(r)
        else:
            dropped += 1
    return out, dropped


def fetch_all(db_path: Path):
    con = open_snapshot(db_path)
    try:
        rows, dropped = dedup_by_msg_id(con.execute(MSG_SQL).fetchall())
        try:
            ledger = con.execute(LEDGER_SQL).fetchone()
        except sqlite3.Error:
            ledger = None
    finally:
        con.close()
    return rows, ledger, dropped


RANGE_MS = {"1h": 3600_000, "24h": 86400_000, "7d": 7 * 86400_000,
            "30d": 30 * 86400_000, "all": None}


def pick_bucket_step(span_ms: int) -> int:
    for step in (600_000, 3_600_000, 3 * 3_600_000, 6 * 3_600_000,
                 86_400_000, 7 * 86_400_000):
        if span_ms <= step * 120:
            return step
    return 30 * 86_400_000


def fmt_bucket_label(ts_ms: int, step_ms: int) -> str:
    lt = time.localtime(ts_ms / 1000)
    if step_ms < 86_400_000:
        return time.strftime("%m-%d %H:%M", lt)
    if step_ms < 7 * 86_400_000:
        return time.strftime("%m-%d", lt)
    return time.strftime("%Y-%m-%d", lt)


def discover_session_files(db_path: Path) -> dict:
    """扫描 <data root>/v2/sessions 下真实存在的会话文件。

    只有目录里带 messages.jsonl 的才算真正的会话文件；目录名形如
    01-26-32-226-session_<base64(session_id)>，由此还原会话 id。
    返回 {session_id: {"mtime": ms, "size": bytes}}。
    """
    root = db_path.parent.parent / "sessions"
    found = {}
    if not root.is_dir():
        return found
    for f in root.rglob("messages.jsonl"):
        name = f.parent.name
        if "-session_" not in name:
            continue
        b64 = name.split("-session_", 1)[1]
        try:
            sid = base64.b64decode(b64 + "=" * (-len(b64) % 4)).decode("utf-8")
        except Exception:
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        found[sid] = {"mtime": int(st.st_mtime * 1000), "size": st.st_size}
    return found


def build_payload(db_path: Path, range_key: str, models_filter=None,
                  sessions_filter=None) -> dict:
    rows_all, ledger, dedup_dropped = fetch_all(db_path)
    now_ms = int(time.time() * 1000)
    span = RANGE_MS.get(range_key)
    if span is not None:
        cutoff = now_ms - span
        rows_all = [r for r in rows_all if r[0] >= cutoff]

    avail = {}
    for r in rows_all:
        key = r[1] or "unknown"
        m = avail.setdefault(key, {"model": key, "calls": 0, "output": 0})
        m["calls"] += 1
        m["output"] += r[5] or 0
    available_models = sorted(avail.values(), key=lambda x: -x["output"])

    # 会话清单以磁盘上的真实会话文件为准（与产品内展示口径一致）
    stats = {}
    for r in rows_all:
        d = stats.setdefault(r[2], {"title": r[8] or "", "calls": 0, "tokens": 0, "last_ts": r[0]})
        d["calls"] += 1
        d["tokens"] += (r[4] or 0) + (r[5] or 0) + (r[6] or 0)
        d["last_ts"] = max(d["last_ts"], r[0])

    files = discover_session_files(db_path)
    available_sessions = []
    if files:
        session_source = "files"
        for sid, meta in files.items():
            st = stats.get(sid)
            available_sessions.append({
                "session_id": sid,
                "title": (st["title"] if st and st["title"] else sid),
                "calls": st["calls"] if st else 0,
                "tokens": st["tokens"] if st else 0,
                "last_ts": st["last_ts"] if st else 0,
                "file_mtime": meta["mtime"],
            })
        available_sessions.sort(key=lambda x: -x["file_mtime"])
    else:  # 兜底：一个会话文件都没有时，退回按库内会话列举
        session_source = "db"
        for sid, st in stats.items():
            available_sessions.append({
                "session_id": sid,
                "title": st["title"] or sid,
                "calls": st["calls"], "tokens": st["tokens"], "last_ts": st["last_ts"],
            })
        available_sessions.sort(key=lambda x: -x["last_ts"])

    rows = rows_all
    if models_filter:
        wanted = set(models_filter)
        rows = [r for r in rows if (r[1] or "unknown") in wanted]
    if sessions_filter:
        wanted_s = set(sessions_filter)
        rows = [r for r in rows if r[2] in wanted_s]

    n = len(rows)
    sum_in = sum((r[4] or 0) for r in rows)
    sum_out = sum((r[5] or 0) for r in rows)
    sum_cr = sum((r[6] or 0) for r in rows)
    sum_dur = sum((r[7] or 0) for r in rows)
    hit_rate = (sum_cr / (sum_cr + sum_in) * 100) if (sum_cr + sum_in) else 0.0
    tok_s = (sum_out / (sum_dur / 1000)) if sum_dur else 0.0
    sessions = {r[2] for r in rows}

    series = []
    split = {"labels": [], "models": []}
    if n:
        lo, hi = rows[0][0], rows[-1][0]
        span_ms = max(hi - lo, 1)
        step = pick_bucket_step(span_ms if span is None else min(span, span_ms))
        buckets = {}
        per_model = {}
        for r in rows:
            b = r[0] // step * step
            d = buckets.setdefault(b, [0, 0, 0, 0, 0])
            d[0] += r[4] or 0
            d[1] += r[5] or 0
            d[2] += r[6] or 0
            d[3] += 1
            d[4] += r[7] or 0
            mk = r[1] or "unknown"
            pm = per_model.setdefault(mk, {})
            pm[b] = pm.get(b, 0) + (r[5] or 0)
        keys = sorted(buckets)
        for b in keys:
            d = buckets[b]
            series.append({
                "label": fmt_bucket_label(b, step),
                "input": d[0], "output": d[1], "cache_read": d[2],
                "calls": d[3],
                "tok_s": round(d[1] / (d[4] / 1000), 1) if d[4] else 0,
            })
        models_sorted = sorted(per_model, key=lambda m: -sum(per_model[m].values()))
        split = {
            "labels": [fmt_bucket_label(b, step) for b in keys],
            "models": [{"model": m, "output": [per_model[m].get(b, 0) for b in keys]}
                       for m in models_sorted],
        }

    models = {}
    for r in rows:
        m = models.setdefault(r[1] or "unknown",
                              {"model": r[1] or "unknown", "calls": 0,
                               "input": 0, "output": 0, "cache_read": 0, "dur": 0})
        m["calls"] += 1
        m["input"] += r[4] or 0
        m["output"] += r[5] or 0
        m["cache_read"] += r[6] or 0
        m["dur"] += r[7] or 0
    model_list = []
    for m in sorted(models.values(), key=lambda x: -(x["output"] + x["input"] + x["cache_read"])):
        tot_in = m["cache_read"] + m["input"]
        model_list.append({
            "model": m["model"], "calls": m["calls"],
            "input": m["input"], "output": m["output"],
            "cache_read": m["cache_read"],
            "hit_rate": round(m["cache_read"] / tot_in * 100, 1) if tot_in else 0,
            "avg_dur_ms": round(m["dur"] / m["calls"]) if m["calls"] else 0,
            "tok_s": round(m["output"] / (m["dur"] / 1000), 1) if m["dur"] else 0,
        })

    recent = []
    for r in reversed(rows[-60:]):
        dur = r[7] or 0
        title = r[8] or r[2] or ""
        recent.append({
            "ts": r[0],
            "time": time.strftime("%m-%d %H:%M:%S", time.localtime(r[0] / 1000)),
            "model": r[1] or "unknown",
            "session": (title[:24] + "…") if len(title) > 25 else title,
            "session_id": r[2], "turn_id": (r[3] or "")[:8],
            "input": r[4] or 0, "output": r[5] or 0, "cache_read": r[6] or 0,
            "dur_ms": dur,
            "tok_s": round((r[5] or 0) / (dur / 1000), 1) if dur else 0,
        })

    ledger_obj = None
    if ledger:
        ledger_obj = {
            "rows": ledger[0], "input": ledger[1], "output": ledger[2],
            "reasoning": ledger[3], "cache_read": ledger[4],
            "cache_write": ledger[5],
            "sessions": ledger[8], "turns": ledger[9],
        }
        if ledger[6]:
            ledger_obj["first_ts"] = ledger[6]
            ledger_obj["last_ts"] = ledger[7]

    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "range": range_key,
        "available_models": available_models,
        "selected_models": sorted(models_filter) if models_filter else [],
        "dedup_dropped": dedup_dropped,
        "overview": {
            "calls": n,
            "sessions": len(sessions),
            "input_tokens": sum_in,
            "output_tokens": sum_out,
            "cache_read_tokens": sum_cr,
            "total_tokens": sum_in + sum_cr + sum_out,
            "hit_rate_pct": round(hit_rate, 2),
            "avg_tok_s": round(tok_s, 1),
            "sum_dur_s": round(sum_dur / 1000, 1),
        },
        "series": series,
        "split": split,
        "models": model_list,
        "available_sessions": available_sessions,
        "selected_sessions": sorted(sessions_filter) if sessions_filter else [],
        "session_files": len(available_sessions),
        "session_source": session_source,
        "recent": recent,
        "ledger": ledger_obj,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--range", default="all", dest="range_key",
                    help="all | 1h | 24h | 7d | 30d")
    ap.add_argument("--models", default="", help="逗号分隔的模型列表, 留空为全部")
    ap.add_argument("--sessions", default="", help="逗号分隔的会话 id 列表, 留空为全部")
    ap.add_argument("--db", default=str(default_db_path()))
    args = ap.parse_args()

    def emit(obj, code):
        sys.stdout.write(json.dumps(obj, ensure_ascii=False))
        sys.exit(code)

    rng = args.range_key if args.range_key in RANGE_MS else "all"
    models = [p.strip() for p in args.models.split(",") if p.strip()] or None
    sessions = [p.strip() for p in args.sessions.split(",") if p.strip()] or None

    db = Path(args.db)
    if not db.exists():
        emit({"error": f"database not found: {db}"}, 1)
    try:
        payload = build_payload(db, rng, models, sessions)
    except Exception as e:  # 结构化错误交给 Node, 不打 traceback
        emit({"error": str(e)}, 1)
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
