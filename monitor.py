#!/usr/bin/env python3
"""MmxCode-monitor — MiniMax Code 本地 token 用量监控台。

零第三方依赖（仅 Python 标准库）。
数据源: <DATA_DIR>/v2/sqlite/runtime-state.sqlite
       （每次请求做只读快照，不写入、不锁库，不影响正在运行的 MiniMax Code）

用法:
    python monitor.py                     # 默认端口 7341, 自动打开浏览器
    python monitor.py --port 8000         # 指定端口
    python monitor.py --no-browser        # 不自动开浏览器
    python monitor.py --db D:/path/x.db   # 指定数据库文件
"""

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_PORT = 7341

# ---------------------------------------------------------------------------
# 数据库快照读取
# ---------------------------------------------------------------------------

def default_db_path() -> Path:
    data_dir = os.environ.get("MINIMAX_DATA_DIR") or (Path.home() / ".minimax")
    return Path(data_dir) / "v2" / "sqlite" / "runtime-state.sqlite"


def open_snapshot(db_path: Path) -> sqlite3.Connection:
    """安全获取一份一致性内存快照。

    优先: 只读 URI 打开源库 + sqlite3 backup API（官方推荐的在线备份方式，
    能正确处理 WAL，且不会阻塞写入方）。
    回退: 复制 db/-wal/-shm 三件套到临时目录再打开（极端锁场景）。
    """
    try:
        uri = f"file:{db_path.as_posix()}?mode=ro"
        src = sqlite3.connect(uri, uri=True, timeout=3)
        dst = sqlite3.connect(":memory:")
        with dst:
            src.backup(dst)
        src.close()
        return dst
    except sqlite3.Error:
        tmpdir = Path(tempfile.mkdtemp(prefix="MmxCode-monitor-"))
        try:
            base = tmpdir / "snap.db"
            shutil.copy2(db_path, base)
            for suffix in ("-wal", "-shm"):
                side = Path(str(db_path) + suffix)
                if side.exists():
                    shutil.copy2(side, Path(str(base) + suffix))
            con = sqlite3.connect(str(base))
            con.execute("PRAGMA journal_mode=DELETE")  # 触发 WAL 合并读取
            return con
        except Exception:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise


# 快照结果缓存: 避免并发请求重复 backup
_cache_lock = threading.Lock()
_cache = {"at": 0.0, "rows": None, "ledger": None, "error": None}
_CACHE_TTL = 2.0


MSG_SQL = """
SELECT m.created_at_ms AS ts,
       json_extract(m.data_json,'$.context_usage_telemetry.model') AS model,
       m.session_id,
       m.turn_id,
       json_extract(m.data_json,'$.usage.input_tokens')    AS inp,
       json_extract(m.data_json,'$.usage.output_tokens')   AS outp,
       json_extract(m.data_json,'$.usage.cache_read')      AS cr,
       json_extract(m.data_json,'$.usage.request_duration_ms') AS dur,
       s.title AS session_title
FROM local_runtime_message_rows m
LEFT JOIN local_runtime_sessions s ON s.session_id = m.session_id
WHERE json_extract(m.data_json,'$.usage') IS NOT NULL
ORDER BY m.created_at_ms
"""

LEDGER_SQL = """
SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens), SUM(reasoning_tokens),
       SUM(cache_read_tokens), SUM(cache_write_tokens), MIN(ts), MAX(ts),
       COUNT(DISTINCT session_id), COUNT(DISTINCT turn_id)
FROM local_runtime_token_usage
"""


def fetch_all(db_path: Path):
    """读取并缓存快照，返回 (message_rows, ledger)。"""
    with _cache_lock:
        now = time.time()
        if _cache["rows"] is not None and now - _cache["at"] < _CACHE_TTL:
            if _cache["error"]:
                raise RuntimeError(_cache["error"])
            return _cache["rows"], _cache["ledger"]
    try:
        con = open_snapshot(db_path)
        try:
            rows = con.execute(MSG_SQL).fetchall()
            try:
                ledger = con.execute(LEDGER_SQL).fetchone()
            except sqlite3.Error:
                ledger = None
        finally:
            con.close()
    except Exception as e:
        with _cache_lock:
            _cache.update(at=now, rows=None, ledger=None, error=str(e))
        raise
    with _cache_lock:
        _cache.update(at=now, rows=rows, ledger=ledger, error=None)
    return rows, ledger


# ---------------------------------------------------------------------------
# 聚合
# ---------------------------------------------------------------------------

RANGE_MS = {"1h": 3600_000, "24h": 86400_000, "7d": 7 * 86400_000,
            "30d": 30 * 86400_000, "all": None}


def pick_bucket_step(span_ms: int) -> int:
    """目标 24~120 个桶，从小到大选步长。"""
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


def build_payload(db_path: Path, range_key: str, models_filter=None) -> dict:
    rows_all, ledger = fetch_all(db_path)
    now_ms = int(time.time() * 1000)
    span = RANGE_MS.get(range_key)
    if span is not None:
        cutoff = now_ms - span
        rows_all = [r for r in rows_all if r[0] >= cutoff]

    # 模型清单：不受模型筛选影响，供筛选项展示
    avail = {}
    for r in rows_all:
        key = r[1] or "unknown"
        m = avail.setdefault(key, {"model": key, "calls": 0, "output": 0})
        m["calls"] += 1
        m["output"] += r[5] or 0
    available_models = sorted(avail.values(), key=lambda x: -x["output"])

    # 应用模型筛选
    rows = rows_all
    if models_filter:
        wanted = set(models_filter)
        rows = [r for r in rows_all if (r[1] or "unknown") in wanted]

    n = len(rows)
    sum_in = sum((r[4] or 0) for r in rows)
    sum_out = sum((r[5] or 0) for r in rows)
    sum_cr = sum((r[6] or 0) for r in rows)
    sum_dur = sum((r[7] or 0) for r in rows)
    hit_rate = (sum_cr / (sum_cr + sum_in) * 100) if (sum_cr + sum_in) else 0.0
    tok_s = (sum_out / (sum_dur / 1000)) if sum_dur else 0.0
    sessions = {r[2] for r in rows}

    # --- 时间序列（动态分桶） ---
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
            d = buckets.setdefault(b, [0, 0, 0, 0, 0])  # in,out,cr,calls,dur
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

    # --- 按模型 ---
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

    # --- 最近调用 ---
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

    # --- 账本表（token_usage 交叉核对） ---
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
        "overview": {
            "calls": n,
            "sessions": len(sessions),
            "input_tokens": sum_in,
            "output_tokens": sum_out,
            "cache_read_tokens": sum_cr,
            "hit_rate_pct": round(hit_rate, 2),
            "avg_tok_s": round(tok_s, 1),
            "sum_dur_s": round(sum_dur / 1000, 1),
        },
        "series": series,
        "split": split,
        "models": model_list,
        "recent": recent,
        "ledger": ledger_obj,
    }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

MIME = {".html": "text/html; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon"}


class Handler(BaseHTTPRequestHandler):
    db_path: Path = None  # 类属性注入
    server_version = "MmxCode-monitor/0.1"

    def log_message(self, fmt, *args):  # 安静些
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except ConnectionAbortedError:
            pass

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            f = STATIC_DIR / "index.html"
            if f.exists():
                self._send(200, f.read_bytes(), MIME[".html"])
            else:
                self._send(404, "index.html missing".encode(), "text/plain")
        elif path == "/echarts.min.js":
            f = STATIC_DIR / "echarts.min.js"
            if f.exists():
                self._send(200, f.read_bytes(), MIME[".js"])
            else:
                self._send(404, b"echarts.min.js missing", "text/plain")
        elif path == "/api/data":
            rng = "all"
            models = None
            qs = self.path.split("?", 1)
            if len(qs) == 2:
                for kv in qs[1].split("&"):
                    if kv.startswith("range="):
                        rng = kv[6:] or "all"
                    elif kv.startswith("models="):
                        raw = unquote(kv[7:])
                        models = [p.strip() for p in raw.split(",") if p.strip()] or None
            if rng not in RANGE_MS:
                rng = "all"
            try:
                payload = build_payload(self.db_path, rng, models)
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            except Exception as e:
                obj = {"error": str(e)}
                self._send(500, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
        elif path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        else:
            self._send(404, b"not found", "text/plain")


def main():
    ap = argparse.ArgumentParser(description="MiniMax Code token monitor")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--db", default=str(default_db_path()))
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"[error] database not found: {db}")
        sys.exit(1)

    # 启动自检: 确认快照可读
    try:
        rows, ledger = fetch_all(db)
        print(f"[ok] snapshot readable: {len(rows)} usage rows, "
              f"ledger rows={ledger[0] if ledger else 'n/a'}")
    except Exception as e:
        print(f"[warn] first snapshot failed (service will keep retrying): {e}")

    port = args.port
    httpd = None
    for _ in range(20):
        try:
            httpd = ThreadingHTTPServer((args.host, port), Handler)
            break
        except OSError:
            port += 1
    if httpd is None:
        print("[error] no free port found")
        sys.exit(1)
    Handler.db_path = db

    url = f"http://{args.host}:{port}/"
    print(f"MmxCode-monitor serving on {url}  (db: {db})")
    print("press Ctrl+C to stop")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
