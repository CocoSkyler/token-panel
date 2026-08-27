# -*- coding: utf-8 -*-
"""
GLM Token 用量面板 - 本地采集服务

零依赖（仅 Python 标准库）。读取 ZCode 本地用量数据库（model_usage 表），
聚合后在局域网提供:
    GET /              -> 显示面板 (web/index.html)
    GET /api/usage     -> 聚合 JSON（供本页面 / 未来鸿蒙卡片等客户端使用）

用法:
    python glm_panel_server.py [--port 8787] [--db 路径]

扩展:
    多模型展示名与配色见下方 MODEL_LABELS / PALETTE。
    其他来源(Codex/Claude 等)可在 server/data/extras.json 中按 README 说明
    提供按天的补充数据，会被合并进 API 响应。
"""

import argparse
import datetime as dt
import io
import json
import os
import re
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import huawei_push as hp

# ---------------------------------------------------------------- 配置 ----

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.normpath(os.path.join(BASE_DIR, "..", "web"))
EXTRAS_PATH = os.path.join(BASE_DIR, "data", "extras.json")

DEFAULT_DB_CANDIDATES = [
    os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")),
                 ".zcode", "cli", "db", "db.sqlite"),
    os.path.expanduser("~/.zcode/cli/db/db.sqlite"),
]

HISTORY_DAYS = 30          # 返回最近多少天明细
LIVE_WINDOW_SEC = 5 * 60   # 距此窗口内有请求则视为 LIVE
CACHE_TTL_SEC = 5          # 聚合结果缓存秒数（降低对数据库的读频次）

# 展示名（key 为 model_usage.model_id；前缀 builtin:bigmodel-coding-plan/ 会被剥掉）
MODEL_LABELS = {
    "GLM-5.3": "GLM 5.3",
    "GLM-5.3-Flash": "GLM 5.3 Flash",
    "GLM-5.2": "GLM 5.2",
}

PALETTE = ["#a78bfa", "#fb923c", "#38bdf8", "#34d399", "#f472b6",
           "#facc15", "#60a5fa", "#c084fc"]

MODEL_RE = re.compile(r"[^/]+$")  # 取路径式模型名的最后一段


def label_of(model_id: str) -> str:
    short = MODEL_RE.search(model_id or "").group(0) or model_id or "unknown"
    return MODEL_LABELS.get(short, short)


# ---------------------------------------------------------------- 数据 ----

_cache_lock = threading.Lock()
_cache = {"at": 0.0, "payload": None}


def _resolve_db(arg_db: str | None) -> str | None:
    if arg_db:
        return arg_db if os.path.isfile(arg_db) else None
    for p in DEFAULT_DB_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


def _load_rows(db_path: str):
    """优先拷贝数据库再读（避开 ZCode 正在写入时的锁），失败则只读直连。"""
    tmp_dir = tempfile.mkdtemp(prefix="glm_panel_")
    try:
        for suffix in ("", "-wal", "-shm"):
            src = db_path + suffix
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(tmp_dir, "db.sqlite" + suffix))
        con = sqlite3.connect(os.path.join(tmp_dir, "db.sqlite"))
        try:
            cur = con.execute(
                """
                SELECT started_at, provider_id, model_id, status,
                       input_tokens, output_tokens, reasoning_tokens,
                       cache_creation_input_tokens, cache_read_input_tokens
                FROM model_usage
                WHERE status = 'completed'
                """
            )
            return cur.fetchall()
        finally:
            con.close()
    except Exception:
        con = sqlite3.connect(f"file:{db_path.replace(os.sep, '/')}?mode=ro", uri=True)
        try:
            cur = con.execute(
                """
                SELECT started_at, provider_id, model_id, status,
                       input_tokens, output_tokens, reasoning_tokens,
                       cache_creation_input_tokens, cache_read_input_tokens
                FROM model_usage
                WHERE status = 'completed'
                """
            )
            rows = cur.fetchall()
            return rows
        finally:
            con.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------- Push Kit 云端推送 ----

TOKENS_PATH = os.path.join(BASE_DIR, "data", "push_tokens.json")
PUSH_CFG_PATH = os.path.join(BASE_DIR, "data", "push_config.json")


def _load_push_tokens() -> list:
    try:
        with io.open(TOKENS_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except Exception:
        return []


def _save_push_tokens(items: list) -> None:
    os.makedirs(os.path.dirname(TOKENS_PATH), exist_ok=True)
    with io.open(TOKENS_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def _register_push(body: dict) -> None:
    token = str(body.get("token") or "").strip()
    if not token:
        return
    items = _load_push_tokens()
    now = int(time.time() * 1000)
    form_ids = [str(x) for x in (body.get("formIds") or []) if str(x).strip()]
    for it in items:
        if it.get("token") == token or (form_ids and set(form_ids) & set(it.get("formIds") or [])):
            if it.get("token") != token:
                # 令牌被系统重新签发：替换并清理旧条目
                it["token"] = token
            if form_ids:
                merged = list(dict.fromkeys((it.get("formIds") or []) + form_ids))
                it["formIds"] = merged
            it["updatedAt"] = now
            break
    else:
        items.append({"token": token, "formIds": form_ids,
                      "device": str(body.get("device") or "device"),
                      "updatedAt": now})
    _save_push_tokens(items)


def _push_cfg() -> dict:
    try:
        with io.open(PUSH_CFG_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        if d.get("client_id") and d.get("client_secret"):
            return d
    except Exception:
        pass
    return {}


def push_card_to_all(binding: dict) -> dict:
    """把卡片绑定数据经华为云推给所有注册设备（目标在 binding.formTargets）。"""
    import urllib.request
    cfg = _push_cfg()
    if not cfg:
        return {"ok": False, "reason": "未配置 push_config.json（client_id/client_secret）"}
    targets = binding.get("formTargets") or []
    if not targets:
        return {"ok": False, "reason": "没有已注册的推送目标（先打开一次应用）"}
    try:
        access = hp.client_credentials_token(cfg["client_id"], cfg["client_secret"])
    except Exception as e:
        return {"ok": False, "reason": f"获取 access_token 失败: {e}"}

    ok_n = fail_n = 0
    errs = []
    for t in targets:
        try:
            j = hp.push_card(access, cfg["client_id"], t.get("token"),
                             str(t.get("formId")), t.get("formName") or "GlmToken2x4",
                             binding)
            if str(j.get("code")) == "80000000":
                ok_n += 1
            else:
                fail_n += 1
                errs.append(f"{str(t.get('formId'))[:8]}…: {j}")
        except Exception as e:
            fail_n += 1
            errs.append(f"{str(t.get('formId'))[:8]}…: {e}")
    return {"ok": ok_n > 0, "sent": ok_n, "failed": fail_n, "errors": errs[:5]}


def _load_extras() -> dict:
    """可选的外部数据源: data/extras.json
    格式: {"显示名": {"color": "#...", "hitRate": 0.8,
                      "days": {"YYYY-MM-DD": tokens, ...}}, ...}
    """
    try:
        with io.open(EXTRAS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# ---------------------------------------------- 其他工具本地用量自动发现 ----

_LOCAL_TZ = dt.datetime.now().astimezone().tzinfo
_CLAUDE_DIR = os.path.join(
    os.environ.get("USERPROFILE", os.path.expanduser("~")), ".claude", "projects")
_CODEX_DIR = os.path.join(
    os.environ.get("USERPROFILE", os.path.expanduser("~")), ".codex", "sessions")
ADAPTER_TTL_SEC = 90
_adapter_cache = {"at": 0.0, "data": {}}


def _utc_to_local_date(iso_ts: str):
    """'2026-08-26T11:54:41.054Z' -> 本地日期字符串"""
    try:
        dtv = dt.datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return dtv.astimezone(_LOCAL_TZ).strftime("%Y-%m-%d")
    except Exception:
        return None


def _scan_claude_days():
    """扫描 Claude Code 会话转录 (~/.claude/projects/**/*.jsonl)
    每行 assistant 记录带 timestamp 与 message.usage 字段。
    返回 (days{name:tokens}, hit_rate)
    """
    days = {}
    inp_sum = cr_sum = cw_sum = 0
    if not os.path.isdir(_CLAUDE_DIR):
        return days, 0
    for dirpath, _dirs, files in os.walk(_CLAUDE_DIR):
        for fn in files:
            if not fn.endswith(".jsonl"):
                continue
            fp = os.path.join(dirpath, fn)
            try:
                with io.open(fp, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if '"usage"' not in line:
                            continue
                        mts = re.search(r'"timestamp":"([^"]+)"', line)
                        mu = re.search(r'"usage":\{([^}]*)\}', line)
                        if not mu:
                            continue
                        fl = dict((k, int(v)) for k, v in
                                  re.findall(r'"(\w+)"\s*:\s*(\d+)', mu.group(1)))
                        inp = fl.get("input_tokens", 0)
                        outp = fl.get("output_tokens", 0)
                        cr = fl.get("cache_read_input_tokens", 0)
                        cw = fl.get("cache_creation_input_tokens", 0)
                        total = inp + outp + cr + cw
                        if total <= 0:
                            continue
                        inp_sum += inp
                        cr_sum += cr
                        cw_sum += cw
                        ds = (_utc_to_local_date(mts.group(1))
                              if mts else time.strftime("%Y-%m-%d",
                                                        time.localtime(os.path.getmtime(fp))))
                        if ds:
                            days[ds] = days.get(ds, 0) + total
            except Exception:
                continue
    denom = inp_sum + cr_sum + cw_sum
    hit = round(cr_sum / denom, 4) if denom > 0 else 0
    return days, hit


def _scan_codex_days():
    """扫描 Codex CLI 会话 (~/.codex/sessions/*/*/*/*.jsonl)
    token_count 事件里的 total_token_usage 是会话累计值，
    因此每个文件只取最后一条，归属到最后记录的本地日期。
    """
    days = {}
    inp_sum = cach_sum = 0
    if not os.path.isdir(_CODEX_DIR):
        return days, 0
    for dirpath, _dirs, files in os.walk(_CODEX_DIR):
        for fn in files:
            if not fn.endswith(".jsonl"):
                continue
            fp = os.path.join(dirpath, fn)
            last = None
            last_ds = None
            try:
                with io.open(fp, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if '"token_count"' not in line:
                            continue
                        try:
                            o = json.loads(line)
                        except Exception:
                            continue
                        info = ((o.get("payload") or {}).get("info") or {})
                        tot = info.get("total_token_usage") or {}
                        if not tot:
                            continue
                        ts = o.get("timestamp")
                        last_ds = (_utc_to_local_date(ts) if isinstance(ts, str) else None) \
                            or last_ds \
                            or time.strftime("%Y-%m-%d",
                                             time.localtime(os.path.getmtime(fp)))
                        last = tot
            except Exception:
                continue
            if last:
                tokens = int(last.get("total_tokens") or 0)
                if tokens > 0:
                    ds = last_ds or time.strftime("%Y-%m-%d")
                    days[ds] = days.get(ds, 0) + tokens
                inp_sum += int(last.get("input_tokens") or 0)
                cach_sum += int(last.get("cached_input_tokens") or 0)
    denom = inp_sum + cach_sum
    hit = round(cach_sum / denom, 4) if denom > 0 else 0
    return days, hit


def _auto_providers() -> dict:
    """带缓存的自动来源扫描。返回 {显示名: {"color":..., "hitRate":..., "days":{...}}}"""
    now = time.time()
    if now - _adapter_cache["at"] < ADAPTER_TTL_SEC:
        return _adapter_cache["data"]
    data = {}
    try:
        d, h = _scan_claude_days()
        if d:
            data["Claude Code"] = {"color": "#D97757", "hitRate": h, "days": d}
    except Exception:
        pass
    try:
        d, h = _scan_codex_days()
        if d:
            data["Codex"] = {"color": "#10A37F", "hitRate": h, "days": d}
    except Exception:
        pass
    _adapter_cache["at"] = now
    _adapter_cache["data"] = data
    return data


def _aggregate(rows):
    tz = dt.datetime.now().astimezone().tzinfo
    now = dt.datetime.now(tz)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def to_local(ms: int) -> dt.datetime:
        return dt.datetime.fromtimestamp(ms / 1000, tz)

    def date_str(d: dt.datetime) -> str:
        return d.strftime("%Y-%m-%d")

    # 按天 / 按 (provider|model) 归集
    by_day = {}      # 'YYYY-MM-DD' -> dict(tokens)
    by_group = {}    # key -> dict(days={date:usage}, today=..)
    last_activity_at = 0

    for (started_ms, provider_id, model_id, _status,
         inp, outp, _reason, cwrite, cread) in rows:
        ts = started_ms or 0
        day = to_local(ts)
        ds = date_str(day)
        # 与 ZCode 自身统计一致: input_tokens 已包含缓存读取, 总量 = input + output
        total = (inp or 0) + (outp or 0)
        d = by_day.setdefault(ds, {"tokens": 0})
        d["tokens"] += total

        gkey = f"{label_of(model_id)}"
        g = by_group.setdefault(gkey, {"days": {}, "input": 0, "cache_read": 0,
                                       "cache_write": 0, "output": 0, "tokens": 0,
                                       "last_at": 0})
        gd = g["days"].setdefault(ds, 0)
        g["days"][ds] = gd + total
        g["input"] += inp or 0
        g["output"] += outp or 0
        g["cache_read"] += cread or 0
        g["cache_write"] += cwrite or 0
        g["tokens"] += total
        if ts > g["last_at"]:
            g["last_at"] = ts
        if ts > last_activity_at:
            last_activity_at = ts

    # ---- 外部来源: Codex / Claude 自动发现 + extras.json 手工配置 ----
    external = {}
    for name, info in _auto_providers().items():
        external[name] = {"color": info.get("color"),
                          "hitRate": float(info.get("hitRate") or 0),
                          "days": {str(k): int(v or 0)
                                   for k, v in (info.get("days") or {}).items()}}
    for name, cfg in _load_extras().items():
        dm = {str(k): int(v or 0) for k, v in (cfg.get("days") or {}).items()}
        if not dm and cfg.get("today") is not None:
            dm = {date_str(now): int(cfg.get("today") or 0)}
        if name in external:
            # 手工配置覆盖同名自动来源的对应字段
            if cfg.get("color"):
                external[name]["color"] = cfg.get("color")
            if dm:
                external[name]["days"] = dm
            if cfg.get("hitRate") is not None:
                external[name]["hitRate"] = float(cfg.get("hitRate") or 0)
        else:
            external[name] = {"color": cfg.get("color"),
                              "hitRate": float(cfg.get("hitRate") or 0),
                              "days": dm}
    # 先并入按天总量，使今日/均值/年累计等所有指标为"全部工具"口径
    for _name, _spec in external.items():
        for ds, v in _spec["days"].items():
            rec = by_day.setdefault(ds, {"tokens": 0})
            rec["tokens"] += v

    def stats(inp, outp, cwrite, cread):
        return {"total": inp + outp, "input": inp, "output": outp,
                "cacheRead": cread, "cacheWrite": cwrite,
                "hitRate": round(cread / inp, 4) if inp > 0 else 0}

    # 连续日期序列（含今天，补零）
    days_list = []
    for i in range(HISTORY_DAYS - 1, -1, -1):
        d = (midnight - dt.timedelta(days=i))
        ds = date_str(d)
        rec = by_day.get(ds)
        days_list.append({"date": ds, "label": d.strftime("%m-%d"),
                          "total": rec["tokens"] if rec else 0})

    today_s, yest_s = date_str(now), date_str(midnight - dt.timedelta(days=1))

    def day_total(ds):
        rec = by_day.get(ds)
        return rec["tokens"] if rec else 0

    today_total = day_total(today_s)
    yesterday_total = day_total(yest_s)
    prev7 = [day_total(date_str(midnight - dt.timedelta(days=k)))
             for k in range(1, 8)]
    prev7_avg = sum(prev7) / 7.0

    year_start = date_str(now.replace(month=1, day=1))
    year_total = sum(r["tokens"] for ds, r in by_day.items() if ds >= year_start)

    elapsed_frac = ((now - midnight).total_seconds()) / 86400.0
    projection = today_total / elapsed_frac if elapsed_frac > 0.02 else today_total

    # 分组排序：今日用量降序，未上榜的按近7日总量排
    def group_key(g):
        return g[1]["days"].get(today_s, 0)

    ranked = sorted(by_group.items(),
                    key=lambda kv: (kv[1].get("days", {}).get(today_s, 0),
                                    sum(kv[1]["days"].values())),
                    reverse=True)

    groups = []
    for idx, (name, g) in enumerate(ranked):
        tot = sum(g["days"].values())
        stat = stats(g["input"], g["output"], g["cache_write"], g["cache_read"])
        series = [(date_str(midnight - dt.timedelta(days=k)),
                   g["days"].get(date_str(midnight - dt.timedelta(days=k)), 0))
                  for k in range(HISTORY_DAYS - 1, -1, -1)]
        groups.append({
            "name": name,
            "color": PALETTE[idx % len(PALETTE)],
            "today": g["days"].get(today_s, 0),
            "todayShare": (g["days"].get(today_s, 0) / today_total) if today_total else 0,
            "hitRate": stat["hitRate"],
            "total": tot,
            "lastAt": g["last_at"],
            "days": [{"date": ds, "total": t} for ds, t in series],
        })

    # 外部来源分组追加（其日用量已在上方并入总口径）
    next_palette = len(groups)
    for name, spec in sorted(external.items()):
        days_map = spec["days"]
        series = []
        tot = 0
        ex_today = 0
        for k in range(HISTORY_DAYS - 1, -1, -1):
            ds = date_str(midnight - dt.timedelta(days=k))
            t = int(days_map.get(ds) or 0)
            series.append({"date": ds, "total": t})
            tot += t
            if ds == today_s:
                ex_today = t
        groups.append({
            "name": name,
            "color": spec.get("color") or PALETTE[next_palette % len(PALETTE)],
            "today": ex_today,
            "todayShare": (ex_today / today_total) if today_total else 0,
            "hitRate": float(spec.get("hitRate") or 0),
            "total": tot,
            "lastAt": 0,
            "days": series,
        })
        next_palette += 1

    # 统一按今日用量降序（趋势堆叠与图例顺序随之一致）
    groups.sort(key=lambda g: (g["today"], g["total"]), reverse=True)

    payload = {
        "generatedAt": int(time.time() * 1000),
        "live": {
            "lastActivityAt": last_activity_at,
            "active": bool(last_activity_at and
                           time.time() * 1000 - last_activity_at <= LIVE_WINDOW_SEC * 1000),
        },
        "today": {"total": today_total, "yesterdayTotal": yesterday_total,
                  "projection": round(projection)},
        "prev7Avg": round(prev7_avg),
        "yearTotal": year_total,
        "days": days_list,
        "groups": groups,
        "formTargets": [],
        "definition": {
            "note": ("总消耗=input+output（ZCode 口径，输入侧含缓存命中）；"
                     "Claude/Codex 来自本机会话日志自动发现；其他来源经 extras.json 配置"),
            "modelSource": "ZCode model_usage 表 (status='completed') "
                           "+ Claude Code 转录 + Codex 会话 + extras.json",
        },
    }
    return payload


def build_payload(db_path: str | None, force: bool = False) -> dict:
    if db_path is None:
        return {"error": "no_database",
                "message": "未找到 ZCode 用量数据库，请用 --db 指定 model_usage 所在的 db.sqlite",
                "generatedAt": int(time.time() * 1000),
                "live": {"lastActivityAt": 0, "active": False},
                "today": {"total": 0, "yesterdayTotal": 0, "projection": 0},
                "prev7Avg": 0, "yearTotal": 0, "days": [], "groups": []}
    with _cache_lock:
        now = time.time()
        if not force and _cache["payload"] and now - _cache["at"] < CACHE_TTL_SEC:
            return _cache["payload"]
        rows = _load_rows(db_path)
        payload = _aggregate(rows)
        _cache.update(at=now, payload=payload)
        return payload


# ---------------------------------------------------------------- HTTP ----

class Handler(BaseHTTPRequestHandler):
    server_version = "GlmTokenPanel/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 安静一些
        pass

    def _send(self, code, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        print(f"[REQ] POST {self.path}", flush=True)
        if self.path.split("?")[0] != "/api/push/register":
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode("utf-8")
            body = json.loads(raw) if raw else {}
        except Exception as e:
            self._send(400, json.dumps({"ok": False, "error": str(e)}).encode(),
                       "application/json; charset=utf-8")
            return
        _register_push(body)
        self._send(200, json.dumps({"ok": True}).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/usage":
            body = json.dumps(self.server.get_usage(), ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
            return
        if path == "/healthz":
            self._send(200, b"ok", "text/plain; charset=utf-8")
            return
        # 静态文件（限定 web 目录内）
        rel = "index.html" if path == "/" else path.lstrip("/")
        fs_path = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not fs_path.startswith(WEB_DIR) or not os.path.isfile(fs_path):
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        ctype = ("text/html; charset=utf-8" if fs_path.endswith(".html")
                 else "application/javascript" if fs_path.endswith(".js")
                 else "image/svg+xml" if fs_path.endswith(".svg")
                 else "application/octet-stream")
        with io.open(fs_path, "rb") as f:
            self._send(200, f.read(), ctype)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, db_path):
        super().__init__(addr, Handler)
        self._db_path = db_path
        self.usage_lock = threading.Lock()

    def get_usage(self):
        with self.usage_lock:
            try:
                return build_payload(self._db_path)
            except Exception as e:  # 读库失败也尽量返回可用响应
                import traceback
                traceback.print_exc()
                return {"error": "aggregate_failed", "message": str(e),
                        "generatedAt": int(time.time() * 1000),
                        "live": {"lastActivityAt": 0, "active": False},
                        "today": {"total": 0, "yesterdayTotal": 0, "projection": 0},
                        "prev7Avg": 0, "yearTotal": 0, "days": [], "groups": []}


CLOUD_SYNC_INTERVAL_SEC = 1800


def cloud_sync_loop(cloud_url: str, cloud_key: str, db_path_getter):
    """采集器云上报：周期性把本地聚合出的 按天×来源 用量推给云端（幂等覆盖）。"""
    import urllib.request
    while True:
        try:
            payload = build_payload(db_path_getter(), force=True)
            sources = []
            for g in payload.get("groups", []):
                days = {d["date"]: d["total"] for d in g.get("days", [])}
                sources.append({"name": g["name"], "color": g["color"],
                                "hitRate": g.get("hitRate", 0), "days": days})
            body = json.dumps({"sources": sources}, ensure_ascii=False).encode()
            req = urllib.request.Request(
                cloud_url.rstrip("/") + "/api/collect", data=body,
                headers={"Content-Type": "application/json; charset=UTF-8",
                         "X-Collector-Key": cloud_key})
            with urllib.request.urlopen(req, timeout=20) as r:
                r.read()
            print(f"[CLOUD-SYNC] 上报 {len(sources)} 个来源成功", flush=True)
        except Exception as e:
            print(f"[CLOUD-SYNC] 失败: {e}", flush=True)
        time.sleep(CLOUD_SYNC_INTERVAL_SEC)


def lan_addresses(port: int):
    ips = ["127.0.0.1"]
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.insert(0, s.getsockname()[0])
        s.close()
    except Exception:
        pass
    return [f"http://{ip}:{port}" for ip in dict.fromkeys(ips)]


def main():
    ap = argparse.ArgumentParser(description="GLM token usage panel server")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--db", default=None, help="ZCode db.sqlite 路径")
    ap.add_argument("--cloud-url", default=None,
                    help="云端地址(如 https://xxx.agconnect.link)：开启云上报")
    ap.add_argument("--cloud-key", default=None, help="云端采集器密钥(tp_开头)")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    db = _resolve_db(args.db)
    if db is None:
        print("[警告] 未找到 ZCode 用量数据库（~/.zcode/cli/db/db.sqlite），"
              "API 将返回错误提示。可用 --db 指定路径。")

    if args.cloud_url and args.cloud_key:
        threading.Thread(target=cloud_sync_loop,
                         args=(args.cloud_url, args.cloud_key,
                               lambda: db), daemon=True).start()
        print(f"   云上报 : {args.cloud_url} (每{CLOUD_SYNC_INTERVAL_SEC // 60}分钟)")

    srv = Server(("0.0.0.0", args.port), db)
    print("=" * 56)
    print(" GLM Token 用量面板已启动")
    print(f"   数据库 : {db}")
    for u in lan_addresses(args.port):
        print(f"   面板   : {u}   <- 手机浏览器打开这个地址")
    print("   Ctrl+C 停止")
    print("=" * 56)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
