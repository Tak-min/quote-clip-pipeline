/** Pi: log the headless Chromium profile into TikTok by QR code.
 * Saves the QR page to /tmp/tiktok-qr.png every few seconds (fresh code every 30 s) (copy it to a screen and scan it with the
 * TikTok app: Profile > ☰ > QR code scanner). Exits 0 once TikTok Studio opens as @kotonex_app.
 *   ./pi-run.sh --browser-only && KOTONEX_LOCAL=1 node pi-login.js
 */
const { chromium } = require('playwright');
const account = 'kotonex_app';
const shot = '/tmp/tiktok-qr.png';

async function loggedIn(page) {
  await page.goto('https://www.tiktok.com/tiktokstudio', { waitUntil: 'domcontentloaded' });
  return page.locator(`a[href="/@${account}"]`).first().waitFor({ timeout: 15000 }).then(() => true, () => false);
}

(async () => {
  const browser = await chromium.connectOverCDP(process.env.TIKTOK_CDP || 'http://127.0.0.1:9222', { timeout: 10000 });
  const ctx = browser.contexts()[0];
  const page = await ctx.newPage();
  await page.setViewportSize({ width: 1200, height: 900 });
  try {
    if (await loggedIn(page)) { console.log('already logged in'); return; }
    await page.goto('https://www.tiktok.com/login/qrcode', { waitUntil: 'domcontentloaded' });
    let loadedAt = Date.now();
    const deadline = Date.now() + 10 * 60 * 1000;
    while (Date.now() < deadline) {
      await page.waitForTimeout(3000);
      if (!page.url().includes('/login')) break;
      // The page does not swap in a new code when one expires: reload for a fresh code every 30 s.
      const scanned = /scanned|confirm on|スキャン済み|確認してください/i.test(await page.locator('body').innerText().catch(() => ''));
      if (!scanned && Date.now() - loadedAt > 30000) {
        await page.goto('https://www.tiktok.com/login/qrcode', { waitUntil: 'domcontentloaded' });
        loadedAt = Date.now();
        await page.waitForTimeout(4000);
      }
      if (!page.url().includes('/login')) break;
      await page.screenshot({ path: shot });
      console.log(`qr updated ${new Date().toISOString()}`);
      await page.waitForTimeout(2000);
    }
    const ok = await loggedIn(page);
    console.log(ok ? 'LOGGED IN' : 'NOT LOGGED IN');
    process.exitCode = ok ? 0 : 1;
  } finally {
    await page.close();
    await browser.close();
  }
})().catch(e => { console.error(e.message); process.exit(1); });
