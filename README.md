# Token 面板

在鸿蒙手机桌面实时查看你的 AI 编程工具 Token 用量（GLM/ZCode、Claude Code、Codex CLI，以及可配置的千问/豆包/Kimi 等），支持桌面服务卡片、任意网络云端刷新。

```
┌────────────┐   授权码登录   ┌─────────────┐   定时上报   ┌──────────────┐
│ 鸿蒙手机 App │ ───────────→ │  AGC 云托管   │ ←────────── │  电脑采集器    │
│  + 桌面卡片  │ ←─────────── │  (本仓库 cloud)│ ──────────→ │ (本仓库 collector)│
└────────────┘   卡片推送/拉取 └─────────────┘  华为推送直达  └──────────────┘
```

## 用户安装（2 分钟）

1. 手机安装 Token 面板 App，用**华为账号一键登录**
2. 在 App 里复制你的**采集器密钥**（`tp_` 开头）
3. 电脑装 Python 3.10+，下载本仓库 `collector/` 目录
   `python glm_panel_server.py --cloud-url <采集上报触发URL> --cloud-key 你的密钥`
4. 完成。桌面卡片每 30 分钟自动刷新，云端每 15 分钟推送刷新，人在任何网络都能看到最新用量

## 仓库结构

| 目录 | 说明 |
|---|---|
| `cloudfunctions/` | AGC 云函数/云对象后端（登录/上报/聚合/推送，免费额度，[部署见 docs/deploy.md](docs/deploy.md)）|
| `collector/` | 电脑端采集器（自动发现 ZCode/Claude/Codex 本地用量 + 本地网页面板 + 云上报） |
| `docs/` | 文档与隐私政策 |

## 数据与隐私

- 用量数据由**你自己电脑上的采集器**统计上报，仅含按日 Token 数量，**不含任何对话内容或代码**
- 登录仅使用华为账号开放标识（openID）做数据隔离，不收集手机号等敏感信息
- 详见 [docs/privacy.md](docs/privacy.md)

## 自用模式（免云端）

不想用云服务？采集器本身就是一个完整的局域网面板：
`python glm_panel_server.py` 后手机浏览器打开 `http://电脑IP:8787`，加到主屏幕即可（需同一 Wi-Fi）。

## 开发者部署（自建云端）

1. AGC 控制台创建 HarmonyOS 应用（包名对齐你的 App），开通**云托管**与**推送服务**，配置公钥指纹
2. 本仓库关联到云托管（源码部署，Dockerfile 路径 `cloud/Dockerfile`）
3. 环境变量：`CLIENT_ID` / `CLIENT_SECRET`（AGC 应用凭据）；挂载持久盘到 `/app/data`
4. 部署后将默认域名填入 App 的 `CLOUD_BASE`

## License

MIT
