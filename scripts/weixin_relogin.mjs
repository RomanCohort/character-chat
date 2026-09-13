// 重新扫码登录：直接调 bridge 自己的登录函数（force:true，绕开"token 有效"那条捷径），
// 二维码渲染到 stdout，扫完把凭证写到它自己的 state dir。
//
//   node scripts/weixin_relogin.mjs
//
// 输出重定向到文件时二维码照样是 ASCII，可以直接读。
import { startWeixinLoginWithQr, waitForWeixinLogin } from 'file:///D:/wechat-character-bridge/node_modules/wechat-ai-code-bridge/dist/auth/login-qr.js';
import { normalizeAccountId, registerWeixinAccountId, saveWeixinAccount } from 'file:///D:/wechat-character-bridge/node_modules/wechat-ai-code-bridge/dist/auth/accounts.js';
import qrcodeTerminal from 'file:///D:/wechat-character-bridge/node_modules/qrcode-terminal/lib/main.js';

const BOT_TYPE = '3';
const BASE = 'https://ilinkai.weixin.qq.com';

const started = await startWeixinLoginWithQr({
  apiBaseUrl: BASE,
  botType: BOT_TYPE,
  force: true,
});

if (!started.qrcodeUrl) {
  console.log('取二维码失败：' + started.message);
  process.exit(1);
}

console.log('=== 用手机微信扫描下面的二维码 ===');
qrcodeTerminal.generate(started.qrcodeUrl, { small: true }, (qr) => console.log(qr));
console.log('=== 二维码结束；等扫码确认（最多 5 分钟）===');
console.log('浏览器备用链接: ' + started.qrcodeUrl);

const result = await waitForWeixinLogin({
  sessionKey: started.sessionKey,
  apiBaseUrl: BASE,
  botType: BOT_TYPE,
  timeoutMs: 300_000,
});

if (!result.connected || !result.botToken || !result.accountId) {
  console.log('登录未完成：' + result.message);
  process.exit(1);
}

const id = normalizeAccountId(result.accountId);
saveWeixinAccount(id, {
  token: result.botToken,
  baseUrl: result.baseUrl,
  userId: result.userId,
});
registerWeixinAccountId(id);
console.log('✅ 登录成功');
console.log('accountId = ' + id);
console.log('baseUrl   = ' + result.baseUrl);
console.log('userId    = ' + result.userId);
