/** Schedule today's KOTONEX queue using the signed-in TikTok Studio UI over Brave CDP. */
const { chromium } = require('playwright');
const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '../..');
const statePath = path.join(root, 'state/tiktok_studio_scheduled.json');
const mediaDir = path.join(root, 'state/tiktok-studio');
const host = 'taku8@100.102.144.116';
const queue = '/home/taku8/sns-poster/queue/pending';
const account = 'kotonex_app';
const cdp = 'http://127.0.0.1:9222';
const remoteList = String.raw`
from pathlib import Path
import json
for path in sorted(Path.home().joinpath('sns-poster/queue/pending').glob('*/post.json')):
    try:
        data = json.loads(path.read_text())
        video = path.with_name('video.mp4')
        if video.is_file() and 'tiktok' in data.get('platforms', []):
            print(json.dumps({'id': path.parent.name,
                              'at': data['not_before'],
                              'caption': data['tiktok']['caption'],
                              'size': video.stat().st_size}, ensure_ascii=False))
    except (KeyError, ValueError):
        continue
`;

function run(command, args, options = {}) {
  const result = spawnSync(command, args, { encoding: 'utf8', timeout: 30000, ...options });
  if (result.error || result.status !== 0) {
    throw new Error(`${command} failed: ${result.error?.message || result.stderr?.trim() || result.status}`);
  }
  return result.stdout;
}

function todayJst() {
  return new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Tokyo', year: 'numeric', month: '2-digit', day: '2-digit' })
    .format(new Date());
}

function jobsToday() {
  const raw = run('ssh', ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', host, 'python3', '-'], { input: remoteList });
  return raw.trim().split('\n').filter(Boolean).map(line => JSON.parse(line)).filter(job =>
    job.at.slice(0, 10) === todayJst() && Date.parse(job.at) > Date.now() + 20 * 60 * 1000);
}

function getVideo(job) {
  if (!/^\d{8}-\d{6}-[a-z0-9]+$/.test(job.id)) throw new Error('Unexpected queue ID');
  fs.mkdirSync(mediaDir, { recursive: true });
  const local = path.join(mediaDir, `${job.id}.mp4`);
  if (!fs.existsSync(local) || fs.statSync(local).size !== job.size) {
    run('scp', ['-q', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
      `${host}:${queue}/${job.id}/video.mp4`, local]);
  }
  if (fs.statSync(local).size !== job.size) throw new Error(`Video size mismatch: ${job.id}`);
  return local;
}

function markPiScheduled(job, url) {
  if (!new RegExp(`^https://www\\.tiktok\\.com/@${account}/video/\\d+$`).test(url))
    throw new Error(`Unexpected TikTok video URL: ${url}`);
  run('ssh', ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', host,
    '/home/taku8/sns-poster/bin/sns-poster', 'mark-tiktok', job.id, url]);
}

async function clickJs(page, selector) {
  const clicked = await page.evaluate(s => {
    const el = document.querySelector(s);
    if (!el) return false;
    el.click();
    return true;
  }, selector);
  if (!clicked) throw new Error(`TikTok Studio control missing: ${selector}`);
}

async function verifyAccount(page) {
  await page.goto('https://www.tiktok.com/tiktokstudio', { waitUntil: 'domcontentloaded' });
  await page.locator(`a[href="/@${account}"]`).first().waitFor({ timeout: 30000 });
}

async function existingUrl(page, job) {
  await page.goto('https://www.tiktok.com/tiktokstudio/content', { waitUntil: 'domcontentloaded' });
  await page.getByText(/\d+件の投稿/).first().waitFor({ timeout: 30000 });
  const firstLine = job.caption.split('\n')[0].trim().slice(0, 55);
  const [year, month, day] = job.at.slice(0, 10).split('-').map(Number);
  const dateLabel = `${month}月${day}日`;
  for (const link of await page.locator(`a[href*="/@${account}/video/"]`).all()) {
    if (!(await link.innerText()).includes(firstLine)) continue;
    const dated = await link.evaluate((el, date) => {
      let node = el;
      for (let i = 0; i < 8 && node; i++, node = node.parentElement) {
        const value = node.innerText || '';
        if (value.includes(date) && value.includes('GMT+9')) return true;
      }
      return false;
    }, dateLabel);
    if (dated) {
      const href = await link.getAttribute('href');
      return new URL(href, 'https://www.tiktok.com').href;
    }
  }
  return null;
}

async function selectTime(page, hour, minute) {
  const hh = hour.toString().padStart(2, '0');
  const mm = minute.toString().padStart(2, '0');
  await clickJs(page, '[aria-label="Select time"]');
  await page.locator(`[role="option"][aria-label="Hours ${hh}"]`).waitFor({ timeout: 10000 });
  await clickJs(page, `[role="option"][aria-label="Hours ${hh}"]`);
  await clickJs(page, `[role="option"][aria-label="Minutes ${mm}"]`);
  const expected = `${hh}:${mm}`;
  await page.waitForFunction(v => document.querySelector('[aria-label="Select time"]')?.innerText === v,
    expected, { timeout: 10000 });
}

async function scheduleOne(page, job, video) {
  const [year, month, day] = job.at.slice(0, 10).split('-').map(Number);
  const [hour, minute] = job.at.slice(11, 16).split(':').map(Number);
  if (job.at.slice(0, 10) !== todayJst() || minute % 5) throw new Error('Unsupported schedule date/time');
  await page.goto('https://www.tiktok.com/tiktokstudio/upload', { waitUntil: 'domcontentloaded' });
  const upload = page.locator('input[type="file"][accept="video/*"]');
  await upload.waitFor({ state: 'attached', timeout: 30000 });
  await upload.setInputFiles(video);
  await page.getByText('アップロード完了').first().waitFor({ timeout: 120000 });

  const editor = page.locator('[contenteditable="true"][role="combobox"]');
  await editor.evaluate(el => el.focus());
  await page.keyboard.press('Meta+A');
  await page.keyboard.press('Backspace');
  await page.keyboard.insertText(job.caption);
  if ((await editor.innerText()).trim() !== job.caption.trim()) throw new Error('Caption mismatch');

  await clickJs(page, 'input[name="postSchedule"][value="schedule"]');
  await page.waitForFunction(() =>
    document.querySelector('input[name=postSchedule][value=schedule]')?.checked ||
    !![...document.querySelectorAll('[role=dialog]')].find(e => e.innerText.includes('投稿予約用に動画を保存しますか？')),
  null, { timeout: 10000 });
  if (await page.getByRole('dialog', { name: '投稿予約用に動画を保存しますか？' }).count()) {
    await clickJs(page, '[role="dialog"] button[data-type="primary"]');
  }
  await page.waitForFunction(() => document.querySelector('input[name=postSchedule][value=schedule]')?.checked);
  await selectTime(page, hour, minute);
  const dateText = await page.locator('[aria-label="Select date"]').innerText();
  if (!dateText.includes(`${year}年${month}月${day}日`)) throw new Error(`Unexpected date: ${dateText}`);
  if (!(await page.getByRole('combobox').filter({ hasText: '誰でも' }).count())) throw new Error('Public visibility missing');

  await page.evaluate(() => {
    const more = [...document.querySelectorAll('*')].find(e => e.textContent?.trim() === 'さらに表示' && !e.children.length);
    more?.click();
  });
  const disclosure = page.locator('[data-e2e="disclose_content_container"] input[role="switch"]');
  await disclosure.waitFor({ timeout: 10000 });
  if (!(await disclosure.isChecked())) await disclosure.evaluate(el => el.click());
  const ownBrand = page.getByText('あなたのブランド', { exact: true }).locator('xpath=..').locator('input[type="checkbox"]');
  if (!(await ownBrand.isChecked())) await ownBrand.evaluate(el => el.click());
  if (!(await ownBrand.isChecked())) throw new Error('Own-brand disclosure missing');
  if ((await page.locator('[aria-label="Select time"]').innerText()) !==
      `${hour.toString().padStart(2, '0')}:${minute.toString().padStart(2, '0')}`)
    throw new Error('Schedule time changed');
  // TikTok prompts for immediate continuation if its content check is still running.
  // Wait for the check instead of accepting that ambiguous confirmation.
  await page.getByText('違反は見つかりませんでした。').first().waitFor({ timeout: 12 * 60 * 1000 });
  const button = page.getByRole('button', { name: '投稿予約する', exact: true });
  if ((await button.count()) !== 1) throw new Error('Schedule button ambiguous');
  await button.evaluate(el => el.click());
  await page.waitForFunction(() => location.pathname.endsWith('/tiktokstudio/content') ||
    !![...document.querySelectorAll('[role=dialog]')].find(e => e.innerText.includes('投稿に進みますか？')),
  null, { timeout: 60000 });
  if (await page.getByRole('dialog', { name: '投稿に進みますか？' }).count()) {
    await page.getByRole('dialog', { name: '投稿に進みますか？' })
      .getByRole('button', { name: 'キャンセル' }).evaluate(el => el.click());
    throw new Error('TikTok requested early posting; schedule not confirmed');
  }
  await page.waitForURL('**/tiktokstudio/content', { timeout: 60000 });
  const url = await existingUrl(page, job);
  if (!url) throw new Error('Submitted but not verified in Studio; inspect before retry');
  return url;
}

async function main() {
  const jobs = jobsToday();
  const state = fs.existsSync(statePath) ? JSON.parse(fs.readFileSync(statePath, 'utf8')) : {};
  console.log(`${jobs.length} future TikTok job(s) today; ${jobs.filter(j => state[j.id]).length} already recorded`);
  if (process.argv.includes('--dry-run') || jobs.length === 0) return;
  const browser = await chromium.connectOverCDP(cdp, { timeout: 10000 });
  const page = await browser.contexts()[0].newPage();
  try {
    await verifyAccount(page);
    for (const job of jobs) {
      if (state[job.id]) continue;
      const url = await existingUrl(page, job) || await scheduleOne(page, job, getVideo(job));
      markPiScheduled(job, url);
      state[job.id] = { at: job.at, url };
      fs.mkdirSync(path.dirname(statePath), { recursive: true });
      fs.writeFileSync(statePath + '.tmp', JSON.stringify(state, null, 2));
      fs.renameSync(statePath + '.tmp', statePath);
      console.log(`scheduled ${job.id} at ${job.at}: ${url}`);
    }
  } finally {
    await page.close();
    await browser.close();
  }
}

async function lockedMain() {
  fs.mkdirSync(path.dirname(statePath), { recursive: true });
  const lockPath = statePath + '.lock';
  try {
    const priorPid = Number(fs.readFileSync(lockPath, 'utf8'));
    if (priorPid) {
      try { process.kill(priorPid, 0); console.log(`already running (PID ${priorPid})`); return; }
      catch (err) { if (err.code !== 'ESRCH') throw err; }
    }
    fs.unlinkSync(lockPath);
  } catch (err) {
    if (err.code !== 'ENOENT') throw err;
  }
  const lock = fs.openSync(lockPath, 'wx');
  fs.writeSync(lock, String(process.pid));
  try { await main(); }
  finally { fs.closeSync(lock); fs.unlinkSync(lockPath); }
}

lockedMain().catch(err => { console.error(`TikTok Studio scheduling stopped: ${err.message}`); process.exitCode = 1; });
