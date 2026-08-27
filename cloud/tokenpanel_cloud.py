# -*- coding: utf-8 -*-
"""
Token 面板 · 云托管多用户后端

部署于 AGC 云托管（容器）。职责：
  - App 登录（华为账号授权码换 openID，签发会话）
  - 采集器上报按天×来源用量（密钥鉴权，幂等 upsert）
  - 聚合查询 /api/usage —— 输出与个人版 glm_panel_server 完全同构的 JSON
  - 设备(推送令牌+卡片ID)注册
  - 定时/触发式 Push Kit 卡片推送调度

仅 Python 标准库。环境变量：
  PORT=8080                监听端口（云托管注入）
  CLIENT_ID / CLIENT_SECRET  AGC 应用 OAuth 凭据（推送+登录码交换）
  DATA_PATH=./data/cloud.db SQLite 路径（挂持久盘）
  PUSH_INTERVAL_SEC=900    定时推送周期
  DEV_LOGIN=0              =1 时开放 /auth/dev 供本地联调（生产勿开）
"""

import datetime as dt
import io
import json
import os
import secrets
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import huawei_push as hp

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.environ.get("DATA_PATH",
                           os.path.join(BASE_DIR, "data", "cloud.db"))
CLIENT_ID = os.environ.get("CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET", "")
PORT = int(os.environ.get("PORT", "8080"))
PUSH_INTERVAL_SEC = int(os.environ.get("PUSH_INTERVAL_SEC", "900"))
DEV_LOGIN = os.environ.get("DEV_LOGIN", "0") == "1"
HISTORY_DAYS = 30
LIVE_WINDOW_MS = 30 * 60 * 1000      # 采集器 30 分钟内上报过 = LIVE
COLLECT_THROTTLE_SEC = 240           # 收到上报后最快 4 分钟才触发一次推送

_local_tz = dt.datetime.now().astimezone().tzinfo

_db_lock = threading.Lock()


# ---------------------------------------------------------------- 数据层 ----

def _connect():
    con = sqlite3.connect(DATA_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


_DB = None


def db() -> sqlite3.Connection:
    global _DB
    if _DB is None:
        os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
        _DB = _connect()
        _DB.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            uid TEXT PRIMARY KEY,
            union_id TEXT DEFAULT '',
            display_name TEXT DEFAULT '',
            session TEXT DEFAULT '',
            collector_key TEXT UNIQUE,
            created_at INTEGER,
            last_login INTEGER DEFAULT 0,
            last_collect INTEGER DEFAULT 0,
            last_push INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS usage(
            uid TEXT, date TEXT, source TEXT,
            tokens INTEGER DEFAULT 0,
            color TEXT DEFAULT '',
            hit_rate REAL DEFAULT 0,
            updated_at INTEGER,
            PRIMARY KEY(uid, date, source)
        );
        CREATE TABLE IF NOT EXISTS devices(
            uid TEXT, push_token TEXT,
            form_ids TEXT DEFAULT '[]',
            updated_at INTEGER,
            PRIMARY KEY(uid, push_token)
        );
        """)
        _DB.commit()
    return _DB


def user_by_session(session: str):
    with _db_lock:
        return db().execute(
            "SELECT * FROM users WHERE session=?", (session,)).fetchone()


def user_by_key(key: str):
    with _db_lock:
        return db().execute(
            "SELECT * FROM users WHERE collector_key=?", (key,)).fetchone()


def issue_session(uid: str, union_id: str = "") -> dict:
    session = secrets.token_hex(24)
    with _db_lock:
        con = db()
        row = con.execute("SELECT collector_key FROM users WHERE uid=?",
                          (uid,)).fetchone()
        if row is None:
            ckey = "tp_" + secrets.token_hex(16)
            con.execute(
                "INSERT INTO users(uid, union_id, session, collector_key,"
                " created_at, last_login) VALUES(?,?,?,?,?,?)",
                (uid, union_id, session, ckey, hp.now_ms(), hp.now_ms()))
        else:
            con.execute("UPDATE users SET session=?, last_login=?, union_id=?"
                        " WHERE uid=?", (session, hp.now_ms(), union_id, uid))
            ckey = row["collector_key"]
        con.commit()
    return {"uid": uid, "session": session, "collectorKey": ckey}


def rotate_key(uid: str) -> str:
    ckey = "tp_" + secrets.token_hex(16)
    with _db_lock:
        con = db()
        con.execute("UPDATE users SET collector_key=? WHERE uid=?", (ckey, uid))
        con.commit()
    return ckey


def upsert_usage(uid: str, sources: list) -> int:
    now = hp.now_ms()
    n = 0
    with _db_lock:
        con = db()
        for s in sources:
            name = str(s.get("name") or "")[:64]
            if not name:
                continue
            color = str(s.get("color") or "")[:16]
            hit = float(s.get("hitRate") or 0)
            for d, v in (s.get("days") or {}).items():
                if not isinstance(v, (int, float)) or v < 0:
                    continue
                date = str(d)[:10]
                if not date[:4].isdigit():
                    continue
                con.execute(
                    "INSERT INTO usage(uid,date,source,tokens,color,hit_rate,"
                    "updated_at) VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(uid,date,source) DO UPDATE SET "
                    "tokens=excluded.tokens, color=excluded.color,"
                    " hit_rate=excluded.hit_rate, updated_at=excluded.updated_at",
                    (uid, date, name, int(v), color, hit, now))
                n += 1
        con.execute("UPDATE users SET last_collect=? WHERE uid=?", (now, uid))
        con.commit()
    return n


def register_device(uid: str, push_token: str, form_ids: list) -> None:
    if not push_token:
        return
    with _db_lock:
        con = db()
        row = con.execute("SELECT form_ids FROM devices WHERE uid=? AND"
                          " push_token=?", (uid, push_token)).fetchone()
        merged = list(form_ids or [])
        if row:
            try:
                merged = list(dict.fromkeys(json.loads(row["form_ids"]) + merged))
            except Exception:
                pass
        con.execute(
            "INSERT INTO devices(uid,push_token,form_ids,updated_at)"
            " VALUES(?,?,?,?) ON CONFLICT(uid,push_token) DO UPDATE SET"
            " form_ids=excluded.form_ids, updated_at=excluded.updated_at",
            (uid, push_token, json.dumps(merged), hp.now_ms()))
        con.commit()


def all_device_targets(uid: str) -> list:
    with _db_lock:
        rows = db().execute("SELECT * FROM devices WHERE uid=?",
                            (uid,)).fetchall()
    targets = []
    for r in rows:
        try:
            fids = json.loads(r["form_ids"])
        except Exception:
            fids = []
        for fid in fids:
            targets.append({"token": r["push_token"], "formId": str(fid)})
    return targets


def users_with_devices() -> list:
    with _db_lock:
        rows = db().execute(
            "SELECT u.uid FROM users u JOIN devices d ON d.uid=u.uid"
            " GROUP BY u.uid").fetchall()
    return [r["uid"] for r in rows]


# ---------------------------------------------------------------- 聚合 ----

def _dstr(d: dt.datetime) -> str:
    return d.strftime("%Y-%m-%d")


def aggregate(uid: str) -> dict:
    now = dt.datetime.now(_local_tz)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    with _db_lock:
        rows = db().execute(
            "SELECT date, source, tokens, color, hit_rate FROM usage"
            " WHERE uid=? AND date>=?",
            (uid, _dstr(midnight - dt.timedelta(days=HISTORY_DAYS - 1)))).fetchall()
        urow = db().execute("SELECT last_collect FROM users WHERE uid=?",
                            (uid,)).fetchone()
    last_collect = (urow or {"last_collect": 0})["last_collect"] or 0

    by_day = {}
    sources = {}
    for r in rows:
        by_day[r["date"]] = by_day.get(r["date"], 0) + r["tokens"]
        g = sources.setdefault(r["source"], {"color": r["color"],
                                             "hit": r["hit_rate"] or 0.0,
                                             "days": {}})
        g["days"][r["date"]] = g["days"].get(r["date"], 0) + r["tokens"]

    days_list = []
    for i in range(HISTORY_DAYS - 1, -1, -1):
        d = midnight - dt.timedelta(days=i)
        ds = _dstr(d)
        days_list.append({"date": ds, "label": d.strftime("%m-%d"),
                          "total": by_day.get(ds, 0)})

    today_s = _dstr(now)
    yest_s = _dstr(midnight - dt.timedelta(days=1))

    def day_total(ds):
        return by_day.get(ds, 0)

    today_total = day_total(today_s)
    prev7 = [day_total(_dstr(midnight - dt.timedelta(days=k)))
             for k in range(1, 8)]
    prev7_avg = sum(prev7) / 7.0
    elapsed = (now - midnight).total_seconds() / 86400.0
    year_start = _dstr(now.replace(month=1, day=1))
    year_total = sum(v for k, v in by_day.items() if k >= year_start)

    groups = []
    ranked = sorted(sources.items(),
                    key=lambda kv: (kv[1]["days"].get(today_s, 0),
                                    sum(kv[1]["days"].values())), reverse=True)
    for name, g in ranked:
        tot = sum(g["days"].values())
        series = [{"date": ds, "total": g["days"].get(ds, 0)}
                  for ds in [d["date"] for d in days_list]]
        groups.append({
            "name": name, "color": g["color"] or "#A78BFA",
            "today": g["days"].get(today_s, 0),
            "todayShare": (g["days"].get(today_s, 0) / today_total)
                          if today_total else 0,
            "hitRate": round(g["hit"], 4),
            "total": tot, "lastAt": last_collect,
            "days": series,
        })

    return {
        "generatedAt": hp.now_ms(),
        "live": {"lastActivityAt": last_collect,
                 "active": bool(last_collect and
                                hp.now_ms() - last_collect <= LIVE_WINDOW_MS)},
        "today": {"total": today_total,
                  "yesterdayTotal": day_total(yest_s),
                  "projection": round(today_total / elapsed)
                                if elapsed > 0.02 else today_total},
        "prev7Avg": round(prev7_avg),
        "yearTotal": year_total,
        "days": days_list,
        "groups": groups,
        "definition": {"note": "云端多用户聚合，口径与个人版一致"},
    }


# ------------------------------------------------- 卡片扁平化（与 App 端一致）----

def _fmt(n: float) -> str:
    n = max(0.0, float(n or 0))

    def seg(v: float, d: int) -> str:
        s = f"{v:.{d}f}"
        return s.rstrip("0").rstrip(".") if "." in s else s

    def dec(v: float) -> int:
        return 0 if v >= 100 else (1 if v >= 10 else 2)

    if n >= 1e9:
        return seg(n / 1e9, dec(n / 1e9)) + "B"
    if n >= 1e6:
        return seg(n / 1e6, dec(n / 1e6)) + "M"
    if n >= 1e3:
        v = n / 1e3
        return seg(v, 1 if v < 10 else 0) + "K"
    return str(int(round(n)))


def _ago(ms: int) -> str:
    if not ms:
        return ""
    s = max(0, int(hp.now_ms() / 1000 - ms / 1000))
    if s < 60:
        return "刚刚"
    if s < 3600:
        return f"{s // 60} 分钟前"
    if s < 86400:
        return f"{s // 3600} 小时前"
    return f"{s // 86400} 天前"


def flatten_carddata(p: dict) -> dict:
    """把聚合 JSON 转成卡片 LocalStorage 直接可用的扁平结构。"""
    t = p.get("today", {})
    total = t.get("total", 0)
    yest = t.get("yesterdayTotal", 0)
    diff = total - yest
    avg = p.get("prev7Avg", 0)
    ratio = (total / avg) if avg else 0
    vpct = round((ratio - 1) * 100) if avg else 0
    days = p.get("days", [])[-7:]
    maxv = max([d.get("total", 0) for d in days] + [1])
    return {
        "ok": True, "err": "", "isLive": p.get("live", {}).get("active", False),
        "updatedText": _ago(p.get("live", {}).get("lastActivityAt", 0)),
        "todayTotal": _fmt(total), "delta": ("+" if diff > 0 else "") + _fmt(diff),
        "deltaBad": diff > 0, "yesterday": _fmt(yest),
        "proj": _fmt(t.get("projection", 0)),
        "prevAvg": _fmt(avg), "prevAvgRaw": int(avg),
        "vsPct": f"{round(ratio * 100)}%", "ringValue": min(100, round(ratio * 100)),
        "vsDelta": f"较均值 {'+' if vpct > 0 else ''}{vpct}%", "vsBad": vpct > 0,
        "yearTotal": _fmt(p.get("yearTotal", 0)),
        "groups": [{
            "name": g["name"], "color": g["color"], "value": _fmt(g["today"]),
            "share": f"{round(g['todayShare'] * 100)}%",
            "shareNum": max(4, round(g["todayShare"] * 100)),
            "hit": f"{round(g['hitRate'] * 100)}%",
            "hitNum": min(100, round(g["hitRate"] * 100)),
            "spark": [d["total"] for d in g["days"][-7:]],
        } for g in p.get("groups", [])[:4]],
        "days": [{
            "v": d["total"], "h": 6 + round(d["total"] / maxv * 38),
            "today": i == len(days) - 1, "l": d["label"],
        } for i, d in enumerate(days)],
    }


# ---------------------------------------------------------------- 推送 ----

_access_cache = {"token": "", "at": 0.0}


def _cached_access() -> str:
    if not CLIENT_ID or not CLIENT_SECRET:
        return ""
    if time.time() - _access_cache["at"] < 3000 and _access_cache["token"]:
        return _access_cache["token"]
    tok = hp.client_credentials_token(CLIENT_ID, CLIENT_SECRET)
    _access_cache.update(token=tok, at=time.time())
    return tok


def push_user(uid: str, reason: str = "") -> dict:
    targets = all_device_targets(uid)
    if not targets:
        return {"ok": False, "reason": "no devices"}
    try:
        access = _cached_access()
    except Exception as e:
        return {"ok": False, "reason": f"access_token: {e}"}
    data = flatten_carddata(aggregate(uid))
    ok_n = fail_n = 0
    errs = []
    for t in targets:
        try:
            j = hp.push_card(access, CLIENT_ID, t["token"], t["formId"],
                             "GlmToken4x4", data)
            if str(j.get("code")) == "80000000":
                ok_n += 1
            else:
                fail_n += 1
                errs.append(str(j)[:150])
        except Exception as e:
            fail_n += 1
            errs.append(str(e)[:150])
    with _db_lock:
        con = db()
        con.execute("UPDATE users SET last_push=? WHERE uid=?",
                    (hp.now_ms(), uid))
        con.commit()
    print(f"[PUSH] {uid} {reason} ok={ok_n} fail={fail_n} {errs[:1]}", flush=True)
    return {"ok": ok_n > 0, "sent": ok_n, "failed": fail_n}


def push_dispatcher():
    while True:
        time.sleep(PUSH_INTERVAL_SEC)
        try:
            for uid in users_with_devices():
                with _db_lock:
                    row = db().execute("SELECT last_push FROM users WHERE uid=?",
                                       (uid,)).fetchone()
                if row and hp.now_ms() - (row["last_push"] or 0) < PUSH_INTERVAL_SEC * 1000 * 0.9:
                    continue
                push_user(uid, "timer")
        except Exception as e:
            print("[PUSH] dispatcher error:", e, flush=True)


# ---------------------------------------------------------------- HTTP ----

PRIVACY_HTML = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>Token面板 隐私政策</title></head><body style="max-width:720px;margin:24px auto;font-family:sans-serif;line-height:1.7">
<h1>Token面板 隐私政策</h1>
<p>本应用用于展示您本人 AI 编程工具的 Token 用量统计。我们高度重视您的个人信息保护。</p>
<h2>我们收集哪些信息</h2>
<ul>
<li>华为账号开放标识（openID）：用于识别您的账号并隔离您的数据，不获取您的手机号、姓名等敏感信息；</li>
<li>AI 工具用量数据：由您自行部署在个人电脑上的采集程序上报，仅包含按日统计的 Token 数量，不含对话内容与代码内容；</li>
<li>推送令牌与卡片标识：用于向您本人的设备刷新桌面卡片数据。</li>
</ul>
<h2>信息如何使用与存储</h2>
<p>上述信息仅用于向您本人提供用量展示与卡片刷新服务，存储于境内云服务器并进行账号隔离。未经您的同意，我们不会向任何第三方提供、公开您的个人信息。</p>
<h2>您的权利</h2>
<p>您可以随时删除应用或联系我们清除您的全部数据。若您卸载并停止使用采集程序，将不再产生新的数据。</p>
<h2>联系方式</h2>
<p>开发者邮箱：support@example.com（正式上架前替换）</p>
<p>更新日期：2026-08-27</p>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "TokenPanelCloud/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # -- helpers --
    def _send(self, code, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 512 * 1024:
            raise ValueError("body too large")
        raw = self.rfile.read(n) if n else b""
        return json.loads(raw.decode("utf-8")) if raw else {}

    # -- routes --
    def do_OPTIONS(self):
        self._send(204, b"", "text/plain")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/healthz":
            self._send(200, b"ok", "text/plain; charset=utf-8")
            return
        if path == "/privacy.html":
            self._send(200, PRIVACY_HTML.encode("utf-8"),
                       "text/html; charset=utf-8")
            return
        if path == "/api/usage":
            u = user_by_session(
                self.headers.get("X-Session")
                or (self.headers.get("Authorization") or "").replace(
                    "Bearer ", "").strip())
            if not u:
                self._json(401, {"error": "unauthorized"})
                return
            try:
                self._json(200, aggregate(u["uid"]))
            except Exception as e:
                self._json(500, {"error": str(e)})
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            if path == "/auth/huawei":
                return self._auth_huawei()
            if path == "/auth/dev" and DEV_LOGIN:
                return self._auth_dev()
            if path == "/api/key":
                return self._api_key()
            if path == "/api/collect":
                return self._api_collect()
            if path == "/api/devices":
                return self._api_devices()
        except hp.HuaweiError as e:
            self._json(400, {"error": f"huawei: {e}"})
            return
        except Exception as e:
            self._json(500, {"error": str(e)})
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def _session_user(self):
        return user_by_session(
            self.headers.get("X-Session")
            or (self.headers.get("Authorization") or "").replace(
                "Bearer ", "").strip())

    def _auth_huawei(self):
        body = self._body()
        code = str(body.get("authorizationCode") or "").strip()
        if not code:
            self._json(400, {"error": "authorizationCode required"})
            return
        if not CLIENT_ID or not CLIENT_SECRET:
            self._json(500, {"error": "server not configured"})
            return
        info = hp.exchange_authorization_code(CLIENT_ID, CLIENT_SECRET, code)
        out = issue_session(info["openID"], info.get("unionID", ""))
        out["serverTime"] = hp.now_ms()
        self._json(200, out)

    def _auth_dev(self):
        body = self._body()
        uid = str(body.get("uid") or "dev-user")[:64]
        out = issue_session(uid)
        out["serverTime"] = hp.now_ms()
        self._json(200, out)

    def _api_key(self):
        u = self._session_user()
        if not u:
            self._json(401, {"error": "unauthorized"})
            return
        key = rotate_key(u["uid"])
        self._json(200, {"collectorKey": key})

    def _api_collect(self):
        key = self.headers.get("X-Collector-Key", "")
        u = user_by_key(key)
        if not u:
            self._json(401, {"error": "bad collector key"})
            return
        body = self._body()
        n = upsert_usage(u["uid"], body.get("sources") or [])
        # 节流触发推送
        if hp.now_ms() - (u["last_push"] or 0) > COLLECT_THROTTLE_SEC * 1000:
            threading.Thread(target=push_user, args=(u["uid"], "collect"),
                             daemon=True).start()
        self._json(200, {"ok": True, "rows": n})

    def _api_devices(self):
        u = self._session_user()
        if not u:
            self._json(401, {"error": "unauthorized"})
            return
        body = self._body()
        register_device(u["uid"], str(body.get("token") or ""),
                        [str(x) for x in (body.get("formIds") or [])])
        self._json(200, {"ok": True})


def main():
    try:
        import sys
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(f"[CLOUD] port={PORT} data={DATA_PATH} dev_login={DEV_LOGIN} "
          f"client_id={'set' if CLIENT_ID else 'MISSING'}", flush=True)
    threading.Thread(target=push_dispatcher, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    srv.serve_forever()


if __name__ == "__main__":
    main()
