/**
 * 华为 OAuth（登录码交换 / 服务级 token）与 Push Kit 卡片推送
 */
import axios from 'axios';

const OAUTH_TOKEN_URL = 'https://oauth-login.cloud.huawei.com/oauth2/v3/token';
const OAUTH_USERINFO_URL = 'https://oauth-login.cloud.huawei.com/oauth2/v3/userinfo';
const PUSH_URL = (clientId: string) =>
  `https://push-api.cloud.huawei.com/v1/${clientId}/messages:send`;

interface HuaweiIdentity {
  openId: string;
  unionId: string;
}

/** 授权码换 openID（服务端直连，无需 AGC Auth） */
export async function huaweiExchangeCode(clientId: string, clientSecret: string,
  code: string): Promise<HuaweiIdentity> {
  const resp = await axios.post(OAUTH_TOKEN_URL, null, {
    params: {
      grant_type: 'authorization_code',
      client_id: clientId,
      client_secret: clientSecret,
      code
    },
    timeout: 10000
  });
  const d = resp.data ?? {};
  if (!d.access_token) {
    throw new Error(d.error_description || 'exchange failed');
  }
  let openId = d.openID || d.openId || '';
  let unionId = d.unionID || d.unionId || '';
  if (!openId) {
    try {
      const u = await axios.get(OAUTH_USERINFO_URL, {
        headers: { Authorization: `Bearer ${d.access_token}` },
        timeout: 10000
      });
      openId = u.data?.openID || u.data?.sub || '';
      unionId = unionId || u.data?.unionID || '';
    } catch (e) {
      // 走 id_token 兜底
    }
  }
  if (!openId && typeof d.id_token === 'string' && d.id_token.split('.').length === 3) {
    try {
      const payload = JSON.parse(Buffer.from(
        d.id_token.split('.')[1], 'base64url').toString('utf-8'));
      openId = payload.openID || payload.sub || '';
      unionId = unionId || payload.unionID || '';
    } catch (e) {
      // 忽略
    }
  }
  if (!openId) {
    throw new Error('cannot resolve openID');
  }
  return { openId, unionId };
}

let cached: { token: string; at: number } = { token: '', at: 0 };

async function serviceToken(clientId: string, clientSecret: string):
  Promise<string> {
  if (cached.token && Date.now() - cached.at < 50 * 60 * 1000) {
    return cached.token;
  }
  const resp = await axios.post(OAUTH_TOKEN_URL, null, {
    params: {
      grant_type: 'client_credentials',
      client_id: clientId,
      client_secret: clientSecret
    },
    timeout: 10000
  });
  if (!resp.data?.access_token) {
    throw new Error('service token failed');
  }
  cached = { token: resp.data.access_token, at: Date.now() };
  return cached.token;
}

/** 推送卡片刷新消息（formName 目前固定 4x4 主卡） */
export async function huaweiPushCard(clientId: string, clientSecret: string,
  pushToken: string, formId: string, formData: Record<string, Object>):
  Promise<Record<string, Object>> {
  const access = await serviceToken(clientId, clientSecret);
  const inner = JSON.stringify(formData);
  const body = {
    validate_only: false,
    message: {
      data: JSON.stringify({ formId, formName: 'GlmToken4x4', formData: inner }),
      pushMsgType: 3,
      token: [pushToken],
      target: { type: 1 }
    }
  };
  const resp = await axios.post(PUSH_URL(clientId), body, {
    headers: {
      'Content-Type': 'application/json; charset=UTF-8',
      Authorization: `Bearer ${access}`
    },
    timeout: 10000
  });
  return resp.data ?? {};
}
