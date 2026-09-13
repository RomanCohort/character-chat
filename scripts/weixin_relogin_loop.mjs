// 循环等扫码：每张二维码写死到同一个文件，过期就换下一张，直到扫上。
//
//   node scripts/weixin_relogin_loop.mjs [最多几轮]
//
// 固定输出：
//   D:\ling-muxue-logs\weixin_qr.png    ← 永远是最新的码，看这个就行
//   D:\ling-muxue-logs\wx_relogin_loop.log
//
// 扫上之后凭证写进 bridge 自己的 state dir，然后退出。
import { startWeixinLoginWithQr, waitForWeixinLogin } from 'file:///D:/wechat-character-bridge/node_modules/wechat-ai-code-bridge/dist/auth/login-qr.js';
import { normalizeAccountId, registerWeixinAccountId, saveWeixinAccount } from 'file:///D:/wechat-character-bridge/node_modules/wechat-ai-code-bridge/dist/auth/accounts.js';
import { writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';

const BOT_TYPE = '3';
const BASE = 'https://ilinkai.weixin.qq.com';
const QR_PNG = 'D:\\ling-muxue-logs\\weixin_qr.png';
const MAX_ROUNDS = Number(process.argv[2] || 6);

/** 用 Python 的 qrcode 把链接渲染成图片（终端那版粘不动）。 */
function renderPng(url) {
  const py = `
import qrcode, sys
img = qrcode.make(sys.argv[1].strip(), box_size=12, border=4)
img.save(r"${QR_PNG}")
print("ok")
`;
  execFileSync('python', ['-c', py, url], { stdio: ['ignore', 'pipe', 'pipe'] });
}

for (let round = 1; round <= MAX_ROUNDS; round++) {
  console.log(`\n=== 第 ${round}/${MAX_ROUNDS} 轮：取二维码 ${new Date().toLocaleTimeString()} ===`);

  const started = await startWeixinLoginWithQr({ apiBaseUrl: BASE, botType: BOT_TYPE, force: true });
  if (!started.qrcodeUrl) {
    console.log('取二维码失败：' + started.message);
    continue;
  }

  try {
    renderPng(started.qrcodeUrl);
    console.log('图片已更新: ' + QR_PNG);
  } catch (e) {
    console.log('渲染图片失败（用链接吧）: ' + String(e));
  }
  console.log('链接: ' + started.qrcodeUrl);

  const result = await waitForWeixinLogin({
    sessionKey: started.sessionKey,
    apiBaseUrl: BASE,
    botType: BOT_TYPE,
    timeoutMs: 300_000,
  });

  if (result.connected && result.botToken && result.accountId) {
    const id = normalizeAccountId(result.accountId);
    saveWeixinAccount(id, { token: result.botToken, baseUrl: result.baseUrl, userId: result.userId });
    registerWeixinAccountId(id);
    console.log('\n✅ 登录成功');
    console.log('accountId = ' + id);
    console.log('baseUrl   = ' + result.baseUrl);
    console.log('userId    = ' + result.userId);
    process.exit(0);
  }

  console.log(`第 ${round} 轮没扫上：${result.message}`);
  if (/需要输入配对码|多次输入错误/.test(result.message || '')) {
    console.log('需要配对码/被限制，停下来等你处理。');
    process.exit(1);
  }
}

console.log('\n✗ 轮次用完，还没扫上。');
process.exit(1);
