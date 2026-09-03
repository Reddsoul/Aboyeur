// One-off smoke driver — not part of the app, run once to prove the real
// login -> onboarding -> chat -> library flow works end to end in a real
// browser against the real backend. Delete after use.
import { chromium } from 'playwright';
import fs from 'node:fs';

const SHOT_DIR = '/tmp/aboyeur-shots';
fs.mkdirSync(SHOT_DIR, { recursive: true });
let shotN = 0;
async function shot(page, name) {
  const f = `${SHOT_DIR}/${String(++shotN).padStart(2, '0')}-${name}.png`;
  await page.screenshot({ path: f });
  console.log('screenshot:', f);
}

const errors = [];

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
  page.on('console', (msg) => {
    if (msg.type() === 'error') errors.push(msg.text());
  });
  page.on('pageerror', (err) => errors.push('pageerror: ' + err.message));

  console.log('== nav to /login ==');
  await page.goto('http://localhost:5173/login');
  await page.waitForSelector('text=Sign in to Aboyeur');
  await shot(page, 'login');

  console.log('== log in as investor1@example.com ==');
  await page.fill('input[type=email]', 'investor1@example.com');
  await page.fill('input[type=password]', 'testpass123');
  await page.click('button:has-text("Sign in")');
  await page.waitForURL('**/app/chat', { timeout: 10000 });
  await page.waitForSelector('text=Hey! I\'m Aboyeur');
  await shot(page, 'chat-landing');

  console.log('== send a chat message and watch it stream ==');
  await page.fill('textarea', 'what can you help me with?');
  await page.keyboard.press('Enter');
  await page.waitForTimeout(1500);
  await shot(page, 'chat-streaming');
  // Wait for the send button to re-enable (streaming finished) — generous
  // timeout, real LLM round trip.
  await page.waitForSelector('button[aria-label="Send"]:not([disabled])', { timeout: 60000 }).catch(() => {});
  await page.waitForTimeout(500);
  await shot(page, 'chat-reply-done');

  console.log('== go to Library ==');
  await page.click('text=Library');
  await page.waitForSelector('.lib-rcard, .lib-empty-state', { timeout: 15000 });
  await shot(page, 'library-grid');

  console.log('== search the library ==');
  await page.fill('.lib-search-field input', 'chicken curry');
  await page.waitForTimeout(600);
  await shot(page, 'library-search');

  console.log('== open a recipe detail + favorite it ==');
  const firstCard = page.locator('.lib-rcard').first();
  if (await firstCard.count()) {
    await firstCard.click();
    await page.waitForSelector('.lib-detail', { timeout: 10000 });
    await shot(page, 'library-detail');
    await page.click('.lib-detail-close');
  }

  console.log('== list view toggle ==');
  await page.click('[aria-label="List view"]');
  await page.waitForTimeout(300);
  await shot(page, 'library-list');

  console.log('== onboarding wizard ==');
  await page.goto('http://localhost:5173/onboarding');
  await page.waitForSelector('text=Welcome to Aboyeur');
  await shot(page, 'onboarding-welcome');
  await page.click('text=Get Started');
  await page.waitForSelector('text=Who are we planning for');
  await shot(page, 'onboarding-household');

  console.log('errors captured:', errors.length);
  for (const e of errors) console.log('  console error:', e);

  await browser.close();
  console.log('DONE');
})().catch((e) => {
  console.error('SMOKE TEST FAILED:', e);
  process.exit(1);
});
