/**
 * 云数据库对象类型对应的服务端 Model（按 DevEco 生成模板手写）。
 * 字段与 clouddb/objecttypes/*.json 一致。
 */

export class Users {
  uid?: string;
  session?: string;
  collectorKey?: string;
  unionId?: string;
  lastCollect?: number;
  lastPush?: number;
  createdAt?: number;

  constructor() {
  }

  getClassName(): string {
    return 'Users';
  }

  getFieldTypeMap(): Map<string, string> {
    const map = new Map<string, string>();
    map.set('uid', 'String');
    map.set('session', 'String');
    map.set('collectorKey', 'String');
    map.set('unionId', 'String');
    map.set('lastCollect', 'Integer');
    map.set('lastPush', 'Integer');
    map.set('createdAt', 'Integer');
    return map;
  }

  getPrimaryKeyList(): string[] {
    return ['uid'];
  }

  getIndexList(): string[] {
    return ['collectorKey'];
  }

  getEncryptedFieldList(): string[] {
    return [];
  }

  parseFrom(record: Record<string, Object): Users {
    this.uid = record['uid'] as string;
    this.session = record['session'] as string;
    this.collectorKey = record['collectorKey'] as string;
    this.unionId = record['unionId'] as string;
    this.lastCollect = record['lastCollect'] as number;
    this.lastPush = record['lastPush'] as number;
    this.createdAt = record['createdAt'] as number;
    return this;
  }
}

export class UsageRecords {
  uid?: string;
  date?: string;
  source?: string;
  tokens?: number;
  color?: string;
  hitRate?: number;
  updatedAt?: number;

  constructor() {
  }

  getClassName(): string {
    return 'UsageRecords';
  }

  getFieldTypeMap(): Map<string, string> {
    const map = new Map<string, string>();
    map.set('uid', 'String');
    map.set('date', 'String');
    map.set('source', 'String');
    map.set('tokens', 'Integer');
    map.set('color', 'String');
    map.set('hitRate', 'Double');
    map.set('updatedAt', 'Integer');
    return map;
  }

  getPrimaryKeyList(): string[] {
    return ['uid', 'date', 'source'];
  }

  getIndexList(): string[] {
    return ['uid', 'date'];
  }

  getEncryptedFieldList(): string[] {
    return [];
  }

  parseFrom(record: Record<string, Object): UsageRecords {
    this.uid = record['uid'] as string;
    this.date = record['date'] as string;
    this.source = record['source'] as string;
    this.tokens = record['tokens'] as number;
    this.color = record['color'] as string;
    this.hitRate = record['hitRate'] as number;
    this.updatedAt = record['updatedAt'] as number;
    return this;
  }
}

export class Devices {
  uid?: string;
  pushToken?: string;
  formIds?: string;
  updatedAt?: number;

  constructor() {
  }

  getClassName(): string {
    return 'Devices';
  }

  getFieldTypeMap(): Map<string, string> {
    const map = new Map<string, string>();
    map.set('uid', 'String');
    map.set('pushToken', 'String');
    map.set('formIds', 'String');
    map.set('updatedAt', 'Integer');
    return map;
  }

  getPrimaryKeyList(): string[] {
    return ['uid', 'pushToken'];
  }

  getIndexList(): string[] {
    return ['uid'];
  }

  getEncryptedFieldList(): string[] {
    return [];
  }

  parseFrom(record: Record<string, Object): Devices {
    this.uid = record['uid'] as string;
    this.pushToken = record['pushToken'] as string;
    this.formIds = record['formIds'] as string;
    this.updatedAt = record['updatedAt'] as number;
    return this;
  }
}
