/** Pi: load TikTok session cookies (JSON exported from a signed-in browser) into the headless
 * Chromium profile, then check TikTok Studio opens as @kotonex_app. Used when QR login is refused
 * for the headless browser.   KOTONEX_LOCAL=1 node pi-import-cookies.js /tmp/cookies.json
 */
const { chromium } = require('playwright');
const fs = require('node:fs');
(async () => {
  const cookies = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  const b = await chromium.connectOverCDP(process.env.TIKTOK_CDP || 'http://127.0.0.1:9222', { timeout: 10000 });
  const ctx = b.contexts()[0];
  await ctx.addCookies(cookies.map(({ partitionKey, ...c }) => c));
  const page = await ctx.newPage();
  await page.goto('https://www.tiktok.com/tiktokstudio', { waitUntil: 'domcontentloaded' });
  const ok = await page.locator('a[href="/@kotonex_app"]').first().waitFor({ timeout: 30000 }).then(() => true, () => false);
  console.log(ok ? 'LOGGED IN as kotonex_app' : `NOT LOGGED IN (${page.url()})`);
  await page.close();
  await b.close();
  process.exitCode = ok ? 0 : 1;
})().catch(e => { console.error(e.message); process.exit(1); });
