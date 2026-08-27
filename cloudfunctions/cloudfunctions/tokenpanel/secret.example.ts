/**
 * 敏感配置：部署前把本文件改名为 secret.ts 并填入真实值（已在 .gitignore 排除 secret.ts）
 */
export const CLIENT_ID = '6917614777804814700';
export const CLIENT_SECRET = 'PUT_CLIENT_SECRET_HERE';

/** 外部触发 pushAll 的口令（定时器/采集器触发推送时校验；留空则不校验，仅建议内网触发） */
export const PUSH_WEBHOOK_KEY = '';
