/**
 * tp-push-fn：推送调度入口。
 * - 定时触发器（AGC 控制台配置，建议每 5 分钟）调用
 * - 或 HTTP POST { "webhookKey": "..." } 触发全量推送
 */
import { cloud } from '@agconnect/cloud-server';

const myHandler = async function (event: any, context: any) {
  let key = '';
  try {
    const body = typeof event?.body === 'string' ? JSON.parse(event.body) : (event?.body ?? {});
    key = String(body.webhookKey ?? '');
  } catch (e) {
    // 定时触发可能无 body
  }
  try {
    const r = await cloud.function().call({
      name: 'tokenpanel',
      data: { method: 'pushAll', params: [key] }
    });
    return { code: 0, result: r };
  } catch (err) {
    return { code: 2, error: String(err).slice(0, 200) };
  }
};

export { myHandler };
