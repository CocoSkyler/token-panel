# -*- coding: utf-8 -*-
"""
华为云公共能力：OAuth 令牌（client_credentials / authorization_code）
与 Push Kit 卡片推送（form 刷新消息）。

被 glm_panel_server.py（个人局域网版）与 tokenpanel_cloud.py（云托管多用户版）
共同引用。仅标准库。
"""

import json
import time
import urllib.parse
import urllib.request

OAUTH_TOKEN_URL = "https://oauth-login.cloud.huawei.com/oauth2/v3/token"
OAUTH_USERINFO_URL = "https://oauth-login.cloud.huawei.com/oauth2/v3/userinfo"
PUSH_SEND_URL = "https://push-api.cloud.huawei.com/v1/{client_id}/messages:send"


class HuaweiError(Exception):
    def __init__(self, code, msg):
        super().__init__(f"{code}: {msg}")
        self.code = code
        self.msg = msg


def _post_json(url, data_bytes, headers, timeout=15):
    req = urllib.request.Request(url, data=data_bytes, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _get_json(url, headers, timeout=15):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def client_credentials_token(client_id: str, client_secret: str) -> str:
    """服务级 access_token（推送调度用）"""
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode()
    j = _post_json(OAUTH_TOKEN_URL, data,
                   {"Content-Type": "application/x-www-form-urlencoded"})
    if not j.get("access_token"):
        raise HuaweiError(j.get("error", -1), j.get("error_description", "no token"))
    return j["access_token"]


def exchange_authorization_code(client_id: str, client_secret: str,
                                authorization_code: str) -> dict:
    """App 登录：授权码换 access_token/openID。
    返回 {openID, unionID, accessToken}；失败抛 HuaweiError。
    """
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "code": authorization_code,
    }).encode()
    j = _post_json(OAUTH_TOKEN_URL, data,
                   {"Content-Type": "application/x-www-form-urlencoded"})
    if not j.get("access_token"):
        raise HuaweiError(j.get("error", -1),
                          j.get("error_description", "exchange failed")[:200] or "exchange failed")
    out = {
        "openID": j.get("openID") or j.get("openId") or "",
        "unionID": j.get("unionID") or j.get("unionId") or "",
        "accessToken": j["access_token"],
    }
    if not out["openID"]:
        # 尝试 userinfo 端点
        try:
            u = _get_json(OAUTH_USERINFO_URL,
                          {"Authorization": f"Bearer {j['access_token']}"})
            out["openID"] = u.get("openID") or u.get("sub") or ""
            out["unionID"] = out["unionID"] or u.get("unionID") or ""
        except Exception:
            pass
    if not out["openID"]:
        # 兜底：解析 id_token 载荷（码是我们服务端刚换到的，信道可信）
        idt = j.get("id_token") or ""
        if idt.count(".") == 2:
            import base64
            payload = idt.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            try:
                p = json.loads(base64.urlsafe_b64decode(payload))
                out["openID"] = p.get("openID") or p.get("sub") or ""
                out["unionID"] = out["unionID"] or p.get("unionID") or ""
            except Exception:
                pass
    if not out["openID"]:
        raise HuaweiError(-1, "cannot resolve openID")
    return out


def push_card(access_token: str, client_id: str, push_token: str,
              form_id: str, form_name: str, form_data: dict) -> dict:
    """推送卡片刷新消息。form_data = 卡片绑定数据扁平 dict。
    返回华为响应 dict（code==80000000 为成功）。
    """
    inner = json.dumps(form_data, ensure_ascii=False, separators=(",", ":"))
    data = json.dumps({"formId": form_id, "formName": form_name,
                       "formData": inner}, separators=(",", ":"))
    body = {
        "validate_only": False,
        "message": {
            "data": data,
            "pushMsgType": 3,
            "token": [push_token],
            "target": {"type": 1},
        },
    }
    url = PUSH_SEND_URL.format(client_id=client_id)
    j = _post_json(url, json.dumps(body).encode("utf-8"), {
        "Content-Type": "application/json; charset=UTF-8",
        "Authorization": f"Bearer {access_token}",
    })
    return j


def compact_binding(binding: dict) -> dict:
    """裁剪聚合结果为卡片必需字段，控制推送体积（<4KB）。"""
    keep_top = ["ok", "err", "updatedText", "isLive", "todayTotal", "delta",
                "deltaBad", "yesterday", "proj", "prevAvg", "vsPct", "vsDelta",
                "vsBad", "ringValue", "yearTotal", "prevAvgRaw"]
    out = {k: binding.get(k) for k in keep_top}
    out["groups"] = [
        {k: g.get(k) for k in ("name", "color", "value", "share",
                                "shareNum", "hit", "hitNum", "spark")}
        for g in (binding.get("groups") or [])[:4]
    ]
    out["days"] = [
        {k: d.get(k) for k in ("v", "h", "today", "l")}
        for d in (binding.get("days") or [])
    ]
    return out


def now_ms() -> int:
    return int(time.time() * 1000)
