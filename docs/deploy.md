# 云端部署清单（DevEco + AGC，约 15 分钟）

架构：AGC 云对象 `tokenpanel`（登录/查询/上报/设备注册/推送）+ 两个 HTTP/定时入口函数。
免服务器、免运维、免费额度内。

## 1. AGC 控制台准备（一次性）

1. 项目 TokenPanel → **云开发**：若未开通则开通（同意协议即可，免费额度）
2. **云数据库** → 存储区 → 创建：名称 `TokenPanelZone`
3. **云数据库** → 对象类型 → 导入 `CloudProgram/clouddb/objecttypes/` 下三个 JSON
   （users / usageRecords / devices）

## 2. 工程配置

1. AGC → 项目设置 → 常规 → 下载 **agconnect-services.json** → 放到
   `GlmTokenPanel/AppScope/`
2. 复制 `CloudProgram/cloudfunctions/tokenpanel/secret.example.ts` 为 **secret.ts**，
   填入 `CLIENT_SECRET`（该文件已 gitignore，不会上传仓库）

## 3. 部署函数

DevEco 打开 GlmTokenPanel → 右键 `CloudProgram/cloudfunctions` →
**Deploy Cloud Functions**（会要求登录华为账号）→ 等待部署完成。

部署三个函数：
- `tokenpanel`（云对象）
- `tp-collect-fn`（采集器上报 HTTP 入口，免鉴权）
- `tp-push-fn`（推送调度入口）

## 4. 触发器配置（AGC 控制台 → 云函数）

1. `tp-collect-fn` → 触发器：确认 **HTTP 触发器（免鉴权）** 已随部署创建；
   复制**触发 URL**（形如 `https://xxx/agc/tp-collect-fn-$latest`）
2. `tp-push-fn` → 触发器 → 新建**定时触发器**：每 5 分钟

## 5. 启动采集器云上报（你自己的电脑）

```
python collector/glm_panel_server.py --cloud-url <tp-collect-fn 触发URL> --cloud-key <App里显示的密钥>
```

## 6. 真机验证闭环

1. DevEco Run 到手机 → App 登录（华为账号）→ 显示采集器密钥
2. 电脑采集器用该密钥上报 → 手机卡片/App 收到云端数据
3. 关 Wi-Fi 用流量：5 分钟内卡片自动推送刷新

## 排错速查

- 部署报对象类型不存在 → 先完成步骤 1.3，且确认存储区名 = `TokenPanelZone`
- HTTP 触发器被拒绝（authFlag:false 不生效）→ 告知，改走 App 转发方案
- App 登录报「云端不可达」→ 检查 agconnect-services.json 是否就位、函数已部署
- 推送没到 → AGC 云函数日志看 tp-push-fn / tokenpanel 日志输出
