/**
 * 聚合逻辑（与个人版 glm_panel_server / tokenpanel_cloud 输出同构）
 */
import { randomBytes } from 'crypto';

interface UsageRow {
  date: string;
  source: string;
  tokens: number;
  color: string;
  hitRate: number;
}

const HISTORY_DAYS = 30;
const LIVE_WINDOW_MS = 30 * 60 * 1000;

function pad2(n: number): string {
  return n < 10 ? '0' + n : String(n);
}

/** 中国时区日期串（相对今天偏移 offsetDays） */
function dateStr(offsetDays: number): string {
  const t = new Date(Date.now() + offsetDays * 86400000 + 8 * 3600000);
  return t.toISOString().slice(0, 10);
}

function labelOf(ds: string): string {
  return ds.slice(5).replace('-', '/');
}

/** 与 App 端 UsageApi 完全同构的聚合 JSON */
export function aggregate(rows: UsageRow[], meta: { lastCollect: number }):
  Record<string, Object> {
  const byDay = new Map<string, number>();
  const sources = new Map<string, { color: string; hit: number;
                                    days: Map<string, number> }>();
  for (const r of rows) {
    byDay.set(r.date, (byDay.get(r.date) ?? 0) + r.tokens);
    let g = sources.get(r.source);
    if (!g) {
      g = { color: r.color, hit: r.hitRate, days: new Map() };
      sources.set(r.source, g);
    }
    g.days.set(r.date, (g.days.get(r.date) ?? 0) + r.tokens);
  }

  const daysList: Array<Record<string, Object>> = [];
  for (let i = HISTORY_DAYS - 1; i >= 0; i--) {
    const ds = dateStr(-i);
    daysList.push({ date: ds, label: labelOf(ds), total: byDay.get(ds) ?? 0 });
  }

  const todayS = dateStr(0);
  const yestS = dateStr(-1);
  const dayTotal = (ds: string): number => byDay.get(ds) ?? 0;
  const todayTotal = dayTotal(todayS);
  let prev7sum = 0;
  for (let k = 1; k <= 7; k++) {
    prev7sum += dayTotal(dateStr(-k));
  }
  const prev7Avg = prev7sum / 7;

  // 中国时区当天已过时长（用于预计全天）
  const nowCn = new Date(Date.now() + 8 * 3600000);
  const elapsed = (nowCn.getUTCHours() * 3600 + nowCn.getUTCMinutes() * 60
    + nowCn.getUTCSeconds()) / 86400;
  const yearStart = `${nowCn.getUTCFullYear()}-01-01`;
  let yearTotal = 0;
  byDay.forEach((v, k) => {
    if (k >= yearStart) {
      yearTotal += v;
    }
  });

  const groups: Array<Record<string, Object>> = [];
  const ranked = Array.from(sources.entries()).sort((a, b) => {
    const ta = a[1].days.get(todayS) ?? 0;
    const tb = b[1].days.get(todayS) ?? 0;
    if (tb !== ta) {
      return tb - ta;
    }
    let sa = 0;
    a[1].days.forEach(v => sa += v);
    let sb = 0;
    b[1].days.forEach(v => sb += v);
    return sb - sa;
  });
  for (const [name, g] of ranked) {
    let tot = 0;
    g.days.forEach(v => tot += v);
    const series: Array<Record<string, Object>> = daysList.map(d =>
      ({ date: d['date'], total: g.days.get(d['date'] as string) ?? 0 }));
    groups.push({
      name,
      color: g.color || '#A78BFA',
      today: g.days.get(todayS) ?? 0,
      todayShare: todayTotal > 0 ? (g.days.get(todayS) ?? 0) / todayTotal : 0,
      hitRate: Math.round(g.hit * 10000) / 10000,
      total: tot,
      lastAt: meta.lastCollect,
      days: series
    });
  }

  return {
    generatedAt: Date.now(),
    live: {
      lastActivityAt: meta.lastCollect,
      active: meta.lastCollect > 0 && Date.now() - meta.lastCollect <= LIVE_WINDOW_MS
    },
    today: {
      total: todayTotal,
      yesterdayTotal: dayTotal(yestS),
      projection: elapsed > 0.02 ? Math.round(todayTotal / elapsed) : todayTotal
    },
    prev7Avg: Math.round(prev7Avg),
    yearTotal,
    days: daysList,
    groups,
    definition: { note: '云端多用户聚合（云函数版），口径与个人版一致' }
  };
}

/* ------------------ 卡片扁平化（与 App 端 UsageApi 一致） ------------------ */

function fmt(n: number): string {
  n = Math.max(0, n || 0);
  const seg = (v: number, d: number): string => {
    const s = v.toFixed(d);
    return s.includes('.') ? s.replace(/0+$/, '').replace(/\.$/, '') : s;
  };
  const dec = (v: number): number => (v >= 100 ? 0 : (v >= 10 ? 1 : 2));
  if (n >= 1e9) {
    return seg(n / 1e9, dec(n / 1e9)) + 'B';
  }
  if (n >= 1e6) {
    return seg(n / 1e6, dec(n / 1e6)) + 'M';
  }
  if (n >= 1e3) {
    const v = n / 1e3;
    return seg(v, v < 10 ? 1 : 0) + 'K';
  }
  return String(Math.round(n));
}

function ago(ms: number): string {
  if (!ms) {
    return '';
  }
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ms / 1000));
  if (s < 60) {
    return '刚刚';
  }
  if (s < 3600) {
    return `${Math.floor(s / 60)} 分钟前`;
  }
  if (s < 86400) {
    return `${Math.floor(s / 3600)} 小时前`;
  }
  return `${Math.floor(s / 86400)} 天前`;
}

export function flattenCardData(p: Record<string, Object>): Record<string, Object> {
  const today = p['today'] as Record<string, number>;
  const total = today?.total ?? 0;
  const yest = today?.yesterdayTotal ?? 0;
  const diff = total - yest;
  const avg = p['prev7Avg'] as number ?? 0;
  const ratio = avg > 0 ? total / avg : 0;
  const vpct = avg > 0 ? Math.round((ratio - 1) * 100) : 0;
  const allDays = p['days'] as Array<Record<string, Object>>;
  const days = allDays.slice(-7);
  let maxv = 1;
  for (const d of days) {
    maxv = Math.max(maxv, d['total'] as number);
  }
  const groups = p['groups'] as Array<Record<string, Object>>;
  return {
    ok: true,
    err: '',
    isLive: (p['live'] as Record<string, Object>)?.['active'] === true,
    updatedText: ago((p['live'] as Record<string, number>)?.['lastActivityAt'] ?? 0),
    todayTotal: fmt(total),
    delta: (diff > 0 ? '+' : '') + fmt(diff),
    deltaBad: diff > 0,
    yesterday: fmt(yest),
    proj: fmt(today?.projection ?? 0),
    prevAvg: fmt(avg),
    prevAvgRaw: Math.round(avg),
    vsPct: `${Math.round(ratio * 100)}%`,
    ringValue: Math.min(100, Math.round(ratio * 100)),
    vsDelta: `较均值 ${vpct > 0 ? '+' : ''}${vpct}%`,
    vsBad: vpct > 0,
    yearTotal: fmt(p['yearTotal'] as number ?? 0),
    groups: groups.slice(0, 4).map(g => ({
      name: g['name'],
      color: g['color'],
      value: fmt(g['today'] as number),
      share: `${Math.round((g['todayShare'] as number) * 100)}%`,
      shareNum: Math.max(4, Math.round((g['todayShare'] as number) * 100)),
      hit: `${Math.round((g['hitRate'] as number) * 100)}%`,
      hitNum: Math.min(100, Math.round((g['hitRate'] as number) * 100)),
      spark: (g['days'] as Array<Record<string, number>>).slice(-7)
        .map(d => d['total'])
    })),
    days: days.map((d, i) => ({
      v: d['total'],
      h: 6 + Math.round((d['total'] as number) / maxv * 38),
      today: i === days.length - 1,
      l: d['label']
    }))
  };
}
