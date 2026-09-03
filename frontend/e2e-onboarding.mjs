// One-off: drive the full onboarding wizard to completion and verify the
// save actually landed with correct unit conversion. Delete after use.
import { chromium } from 'playwright';

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
  const errors = [];
  page.on('console', (m) => m.type() === 'error' && errors.push(m.text()));
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));

  await page.goto('http://localhost:5173/login');
  await page.fill('input[type=email]', 'investor1@example.com');
  await page.fill('input[type=password]', 'testpass123');
  await page.click('button:has-text("Sign in")');
  await page.waitForURL('**/app/chat');

  await page.goto('http://localhost:5173/onboarding');
  await page.waitForSelector('text=Welcome to Aboyeur');
  await page.click('text=Get Started');

  // Household
  await page.waitForSelector('text=Who are we planning for');
  await page.fill('input[placeholder="Me"]', 'Jamie');
  await page.fill('input[placeholder="e.g. Sam, or Kid 1"]', 'Sam');
  await page.click('button[aria-label="Add member"]');
  await page.click('button:has-text("Continue")');

  // About you — a fresh profile defaults to metric (matches the backend's
  // DEFAULT_PROFILE); switch to imperial to exercise the ft/in + lb
  // conversion path, the more complex one.
  await page.waitForSelector('text=Tell us about yourself');
  await page.click('button:has-text("Imperial")');
  await page.click('button:has-text("Female")');
  const dash1 = page.locator('input[placeholder="—"]'); // just [age] at this point
  await dash1.nth(0).fill('29');
  await page.fill('input[placeholder="ft"]', '5');
  await page.fill('input[placeholder="in"]', '10');
  const dash2 = page.locator('input[placeholder="—"]'); // [age, weight]
  await dash2.nth(1).fill('165');
  await page.click('text=Moderate');
  await page.click('button:has-text("Continue")');

  // Goal
  await page.waitForSelector("text=What's your goal?");
  await page.click('button:has-text("Lose")');
  await page.waitForSelector('text=Goal weight');
  await page.locator('input[placeholder="—"]').first().fill('150');
  await page.click('button:has-text("Continue")');

  // Allergies
  await page.waitForSelector('text=Any allergies?');
  await page.click('button:has-text("Peanut")');
  await page.click('button:has-text("Continue")');

  // Taste
  await page.waitForSelector('text=Likes & dislikes');
  await page.click('button:has-text("Continue")');

  // Cuisine
  await page.waitForSelector('text=Cuisine & diet');
  await page.click('button:has-text("Italian")');
  await page.click('button:has-text("Vegetarian")');
  await page.click('button:has-text("Continue")');

  // Meals
  await page.waitForSelector('text=Meals per day');
  await page.click('button:has-text("Continue")');

  // Review
  await page.waitForSelector('text=Review your profile');
  await page.screenshot({ path: '/tmp/aboyeur-shots/review.png' });
  await page.click('button:has-text("Finish Setup")');

  await page.waitForSelector("text=You're all set", { timeout: 15000 });
  await page.screenshot({ path: '/tmp/aboyeur-shots/done.png' });

  const saved = await page.evaluate(() => fetch('/profile/data', { credentials: 'include' }).then((r) => r.json()));
  console.log('SAVED PROFILE:', JSON.stringify(saved, null, 2));

  console.log('console errors:', errors.length, errors);
  await browser.close();
})().catch((e) => {
  console.error('FAILED:', e);
  process.exit(1);
});
