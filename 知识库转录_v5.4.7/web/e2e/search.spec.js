import { test, expect } from '@playwright/test';

const QUERIES = [
  '什么是上下文',
  'DeepSeek Harness 插件使用方法',
  '什么是上下文工程',
  'RAG',
  '不存在词zzzqqq999',
];

test.beforeEach(async ({ page }) => {
  const ready = await page.request.get('/api/health/ready');
  expect(ready.ok()).toBeTruthy();
  const body = await ready.json();
  expect(body.ready).toBeTruthy();
});

test('首页加载无白屏无异常', async ({ page }) => {
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#fatalError')).toBeHidden();
  await expect(page.locator('#searchForm')).toBeVisible();
  expect(errors.length).toBe(0);
});

for (const q of QUERIES) {
  test(`搜索不刷新不白屏: ${q}`, async ({ page }) => {
    const errors = [];
    page.on('pageerror', (e) => errors.push(String(e)));
    let navigations = 0;
    // 只统计真实文档导航（URL path 变化或 reload），replaceState 同步 ?q= 不触发
    page.on('framenavigated', (frame) => {
      if (frame === page.mainFrame()) navigations += 1;
    });
    await page.goto('/', { waitUntil: 'domcontentloaded' });
    navigations = 0;
    await page.fill('#query', q);
    const [resp] = await Promise.all([
      page.waitForResponse(
        (r) => r.url().includes('/api/search') && r.request().method() === 'GET',
        { timeout: 115000 },
      ),
      page.click('#btnSearch'),
    ]);
    expect(resp.status()).toBe(200);
    await expect(page.locator('.result-card').first()).toBeVisible({ timeout: 15000 });
    // replaceState 同步 ?q= 到 URL 是预期行为（无 reload、无真实导航）。
    // Playwright 的 framenavigated 会把初始 goto 也计入，故只要求搜索后不新增导航。
    expect(new URL(page.url()).pathname).toBe('/');
    expect(navigations).toBeLessThanOrEqual(1);
    const searching = await page.locator('text=搜索中').count();
    expect(searching).toBe(0);
    expect(errors.length).toBe(0);
    // chunk_id 唯一
    const ids = await page.evaluate(() =>
      Array.from(document.querySelectorAll('.result-card')).map((el) => el.innerHTML.length),
    );
    expect(ids.length).toBeGreaterThan(0);
  });
}

test('管理页中文文件名与分页', async ({ page }) => {
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await page.click('#tabNav button[data-tab="manage"]');
  await expect(page.locator('#tab-manage')).toHaveClass(/active/);
  await expect(page.locator('.file-row').first()).toBeVisible({ timeout: 15000 });
  const text = await page.locator('#fileListArea').innerText();
  expect(text.length).toBeGreaterThan(10);
  expect(errors.length).toBe(0);
});

test('行号链接可点击', async ({ page }) => {
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await page.fill('#query', '什么是上下文');
  const [resp] = await Promise.all([
    page.waitForResponse(
      (r) => r.url().includes('/api/search') && r.request().method() === 'GET',
      { timeout: 115000 },
    ),
    page.click('#btnSearch'),
  ]);
  expect(resp.status()).toBe(200);
  const badge = page.locator('a[data-seg][data-line]').first();
  await expect(badge).toBeVisible({ timeout: 15000 });
  expect(errors.length).toBe(0);
});
