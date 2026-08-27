/**
 * Token 面板云对象：登录/查询/上报/设备注册/推送调度
 * 部署：DevEco 中右键 cloudfunctions -> Deploy Cloud Functions
 */
import { cloud } from '@agconnect/cloud-server';
import { Controller } from './controller';
import { aggregate, flattenCardData } from './aggregate';
import { huaweiExchangeCode, huaweiPushCard } from './huawei';
import { CLIENT_ID, CLIENT_SECRET, PUSH_WEBHOOK_KEY } from './secret';

export class TokenPanel {
  controller: Controller = new Controller();

  /** App 登录：华为授权码换 openID，签发会话与采集器密钥 */
  async login(authorizationCode: string): Promise<Record<string, Object>> {
    if (!authorizationCode) {
      return { error: 'authorizationCode required' };
    }
    let openId = '';
    let unionId = '';
    try {
      const info = await huaweiExchangeCode(CLIENT_ID, CLIENT_SECRET, authorizationCode);
      openId = info.openId;
      unionId = info.unionId;
    } catch (err) {
      return { error: `huawei: ${String(err).slice(0, 160)}` };
    }
    const out = await this.controller.issueSession(openId, unionId);
    return out;
  }

  /** 聚合查询（App 端） */
  async usage(session: string): Promise<Record<string, Object>> {
    const user = await this.controller.userBySession(session);
    if (!user || !user.uid) {
      return { error: 'unauthorized' };
    }
    const rows = await this.controller.usageRows(user.uid);
    const meta = { lastCollect: user.lastCollect ?? 0 };
    return aggregate(rows, meta) as Record<string, Object>;
  }

  /** 采集器上报（按天×来源，幂等覆盖） */
  async collect(collectorKey: string, sources: Array<Record<string, Object>>):
    Promise<Record<string, Object>> {
    const user = await this.controller.userByKey(collectorKey);
    if (!user || !user.uid) {
      return { error: 'bad collector key' };
    }
    const n = await this.controller.upsertUsage(user.uid, sources);
    // 节流触发推送（≥4 分钟）
    const now = Date.now();
    if (now - (user.lastPush ?? 0) > 4 * 60 * 1000) {
      this.pushForUser(user.uid, 'collect');
    }
    return { ok: true, rows: n };
  }

  /** 设备注册（App 推送令牌 + 卡片ID） */
  async registerDevice(session: string, token: string, formIds: string[]):
    Promise<Record<string, Object>> {
    const user = await this.controller.userBySession(session);
    if (!user || !user.uid || !token) {
      return { error: 'unauthorized' };
    }
    await this.controller.registerDevice(user.uid, token, formIds);
    return { ok: true };
  }

  /** 生成/重置采集器密钥 */
  async rotateKey(session: string): Promise<Record<string, Object>> {
    const user = await this.controller.userBySession(session);
    if (!user || !user.uid) {
      return { error: 'unauthorized' };
    }
    const key = await this.controller.rotateKey(user.uid);
    return { collectorKey: key };
  }

  /** 定时/触发式：推某用户全部卡片 */
  async pushForUser(uid: string, reason: string): Promise<void> {
    try {
      const targets = await this.controller.deviceTargets(uid);
      if (targets.length === 0) {
        return;
      }
      const user = await this.controller.userByUid(uid);
      if (!user || !user.uid) {
        return;
      }
      const rows = await this.controller.usageRows(uid);
      const data = flattenCardData(aggregate(rows,
        { lastCollect: user?.lastCollect ?? 0 }));
      for (const t of targets) {
        await huaweiPushCard(CLIENT_ID, CLIENT_SECRET,
          t.pushToken, t.formId, data);
      }
      await this.controller.markPushed(uid);
      console.log(`[PUSH] ${uid} ${reason} targets=${targets.length}`);
    } catch (err) {
      console.error(`[PUSH] ${uid} failed: ${String(err).slice(0, 200)}`);
    }
  }

  /** 定时/HTTP 触发：推送所有有设备的用户 */
  async pushAll(webhookKey?: string): Promise<Record<string, Object>> {
    if (PUSH_WEBHOOK_KEY && webhookKey !== PUSH_WEBHOOK_KEY) {
      return { error: 'bad webhook key' };
    }
    const uids = await this.controller.usersWithDevices();
    let sent = 0;
    for (const uid of uids) {
      await this.pushForUser(uid, 'timer');
      sent += 1;
    }
    return { ok: true, users: sent };
  }
}
