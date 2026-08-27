# -*- coding: utf-8 -*-
"""
推送到手机桌面卡片（Push Kit 卡片刷新消息）

前置（一次性）:
1. AGC 控制台创建应用 com.skyler.glmtoken 并开通推送服务
2. 把 Client ID / Client Secret 填入 server/data/push_config.json
3. 手机上至少打开一次 Token面板 应用（会向电脑注册推送令牌+卡片ID）

用法:
    python push_phone.py            # 推送一次（把当前卡片数据刷新到手机）
    python push_phone.py --loop 15  # 每15分钟自动推一次（可挂后台/计划任务）
"""
import argparse
import importlib.util
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# formId -> formName 映射（卡片刷新消息需要路由到具体卡片样式）
FORM_NAMES = {
    "1692138936": "GlmToken4x4",
    "2108500568": "GlmToken4x4",
}

spec = importlib.util.spec_from_file_location(
    "panel_server", os.path.join(HERE, "glm_panel_server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)


def compact(binding: dict) -> dict:
    """裁剪成卡片需要的字段，控制推送体积"""
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


def push_now() -> None:
    binding = srv.build_payload(srv._resolve_db(None), force=True)
    if binding.get("error"):
        print("采集失败:", binding.get("message"))
        sys.exit(1)
    tokens = srv._load_push_tokens()
    targets = []
    for it in tokens:
        for fid in (it.get("formIds") or []):
            targets.append({"token": it["token"], "formId": fid,
                            "formName": FORM_NAMES.get(fid, "GlmToken4x4")})
    if not targets:
        print("还没有注册的推送目标。请先在手机上打开一次 Token面板 应用"
              "（添加过卡片更好），它会向本服务注册令牌。")
        sys.exit(1)
    binding["formTargets"] = targets
    data = compact(binding)
    data["formTargets"] = targets
    res = srv.push_card_to_all(data)
    print(json.dumps(res, ensure_ascii=False, indent=2)[:800])
    if not res.get("ok"):
        print("\n失败原因排查:")
        print("- 未配置 push_config.json -> 按模板填写 client_id/secret")
        print("- AGC 未开通推送/指纹不一致 -> 检查 AGC 项目设置")
        sys.exit(1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=0, help="循环间隔分钟数，0为单次")
    args = ap.parse_args()
    if args.loop > 0:
        while True:
            try:
                push_now()
            except Exception as e:
                print("push error:", e)
            time.sleep(args.loop * 60)
    else:
        push_now()
