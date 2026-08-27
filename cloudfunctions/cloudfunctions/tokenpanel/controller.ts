/**
 * 云数据库访问层（@agconnect/cloud-server CloudDB）
 */
import { cloud } from '@agconnect/cloud-server';
import { randomBytes } from 'crypto';
import { Users, UsageRecords, Devices } from './model';

const ZONE = 'TokenPanelZone';

interface UsageRow {
  date: string;
  source: string;
  tokens: number;
  color: string;
  hitRate: number;
}

interface DeviceTarget {
  pushToken: string;
  formId: string;
}

function col<T>(clazz: Function): any {
  return cloud.database({ zoneName: ZONE }).collection(clazz as new () => T);
}

export class Controller {

  async userBySession(session: string): Promise<Users | undefined> {
    if (!session) {
      return undefined;
    }
    const list = await col(Users).query().equalTo('session', session).limit(2).get() as Users[];
    return list.length > 0 ? list[0] : undefined;
  }

  async userByUid(uid: string): Promise<Users | undefined> {
    const list = await col(Users).query().equalTo('uid', uid).limit(2).get() as Users[];
    return list.length > 0 ? list[0] : undefined;
  }

  async userByKey(key: string): Promise<Users | undefined> {
    if (!key) {
      return undefined;
    }
    const list = await col(Users).query().equalTo('collectorKey', key).limit(2).get() as Users[];
    return list.length > 0 ? list[0] : undefined;
  }

  async issueSession(uid: string, unionId: string): Promise<Record<string, Object>> {
    const session = randomBytes(24).toString('hex');
    const existing = await this.userByUid(uid);
    const ckey = existing?.collectorKey ?? ('tp_' + randomBytes(16).toString('hex'));
    const user = new Users();
    user.uid = uid;
    user.unionId = unionId;
    user.session = session;
    user.collectorKey = ckey;
    user.lastCollect = existing?.lastCollect ?? 0;
    user.lastPush = existing?.lastPush ?? 0;
    user.createdAt = existing?.createdAt ?? Date.now();
    await col(Users).upsert(user);
    return { uid, session, collectorKey: ckey };
  }

  async rotateKey(uid: string): Promise<string> {
    const ckey = 'tp_' + randomBytes(16).toString('hex');
    const user = await this.userByUid(uid);
    if (user) {
      user.collectorKey = ckey;
      await col(Users).upsert(user);
    }
    return ckey;
  }

  async usageRows(uid: string): Promise<UsageRow[]> {
    const since = this.dateStr(-29);
    const list = await col(UsageRecords).query()
      .equalTo('uid', uid)
      .greaterThanOrEqualTo('date', since)
      .limit(2000)
      .get() as UsageRecords[];
    return list.map(r => ({
      date: r.date ?? '',
      source: r.source ?? '',
      tokens: r.tokens ?? 0,
      color: r.color ?? '',
      hitRate: r.hitRate ?? 0
    }));
  }

  async upsertUsage(uid: string, sources: Array<Record<string, Object>>):
    Promise<number> {
    const now = Date.now();
    const records: UsageRecords[] = [];
    for (const s of sources) {
      const name = String(s['name'] ?? '').slice(0, 64);
      if (!name) {
        continue;
      }
      const color = String(s['color'] ?? '').slice(0, 16);
      const hit = Number(s['hitRate'] ?? 0);
      const days = s['days'] as Record<string, Object> ?? {};
      for (const d of Object.keys(days)) {
        const v = Number(days[d]);
        if (!Number.isFinite(v) || v < 0 || !/^\d{4}-\d{2}-\d{2}$/.test(d)) {
          continue;
        }
        const rec = new UsageRecords();
        rec.uid = uid;
        rec.date = d;
        rec.source = name;
        rec.tokens = Math.round(v);
        rec.color = color;
        rec.hitRate = hit;
        rec.updatedAt = now;
        records.push(rec);
      }
    }
    let n = 0;
    // 分批 upsert，避免单次过大
    for (let i = 0; i < records.length; i += 100) {
      const batch = records.slice(i, i + 100);
      n += await col(UsageRecords).upsert(batch);
    }
    const user = await this.userByUid(uid);
    if (user) {
      user.lastCollect = now;
      await col(Users).upsert(user);
    }
    return n;
  }

  async registerDevice(uid: string, pushToken: string, formIds: string[]):
    Promise<void> {
    const existing = (await col(Devices).query()
      .equalTo('uid', uid)
      .equalTo('pushToken', pushToken)
      .limit(1)
      .get() as Devices[])[0];
    let merged: string[] = Array.from(formIds || []);
    if (existing?.formIds) {
      try {
        const old = JSON.parse(existing.formIds) as string[];
        merged = Array.from(new Set(old.concat(merged)));
      } catch (e) {
        // 忽略旧数据解析失败
      }
    }
    const dev = new Devices();
    dev.uid = uid;
    dev.pushToken = pushToken;
    dev.formIds = JSON.stringify(merged);
    dev.updatedAt = Date.now();
    await col(Devices).upsert(dev);
  }

  async deviceTargets(uid: string): Promise<DeviceTarget[]> {
    const list = await col(Devices).query().equalTo('uid', uid).limit(50).get() as Devices[];
    const out: DeviceTarget[] = [];
    for (const d of list) {
      let fids: string[] = [];
      try {
        fids = JSON.parse(d.formIds ?? '[]') as string[];
      } catch (e) {
        // 忽略
      }
      for (const fid of fids) {
        out.push({ pushToken: d.pushToken ?? '', formId: fid });
      }
    }
    return out;
  }

  async usersWithDevices(): Promise<string[]> {
    const list = await col(Devices).query().limit(500).get() as Devices[];
    return Array.from(new Set(list.map(d => d.uid ?? ''))).filter(u => u !== '');
  }

  async markPushed(uid: string): Promise<void> {
    const user = await this.userByUid(uid);
    if (user) {
      user.lastPush = Date.now();
      await col(Users).upsert(user);
    }
  }

  /** 中国时区日期串，offsetDays 为相对今天的偏移 */
  dateStr(offsetDays: number): string {
    const t = new Date(Date.now() + offsetDays * 86400000 + 8 * 3600000);
    return t.toISOString().slice(0, 10);
  }
}
