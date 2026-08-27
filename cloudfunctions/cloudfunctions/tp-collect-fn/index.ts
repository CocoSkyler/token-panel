/**
 * tp-collect-fn：采集器 HTTP 上报入口（免鉴权 HTTP 触发，业务层用采集器密钥校验）
 * 采集器 POST：{ "collectorKey": "tp_xxx", "sources": [...] }
 */
import { cloud } from '@agconnect/cloud-server';

const myHandler = async function (event: any, context: any) {
  let body: any = {};
  try {
    body = typeof event?.body === 'string' ? JSON.parse(event.body) : (event?.body ?? {});
  } catch (e) {
    return { code: 1, error: 'bad json' };
  }
  const key = String(body.collectorKey ?? '');
  const sources = Array.isArray(body.sources) ? body.sources : [];
  if (!key) {
    return { code: 1, error: 'collectorKey required' };
  }
  try {
    const r = await cloud.function().call({
      name: 'tokenpanel',
      data: { method: 'collect', params: [key, sources] }
    });
    return { code: 0, result: r };
  } catch (err) {
    return { code: 2, error: String(err).slice(0, 200) };
  }
};

export { myHandler };
