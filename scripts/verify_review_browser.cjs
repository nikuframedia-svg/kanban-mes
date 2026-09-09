const fs = require('node:fs');
const assert = require('node:assert/strict');
const config = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const {chromium} = require(config.playwright);
(async () => {
  const browser = await chromium.launch({headless: true});
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => { if (!page.url().includes('/original/')) errors.push(error.message); });
  page.on('dialog', dialog => dialog.accept());
  const shots = [];
  async function shot(name) {
    await page.screenshot({path: `${config.output}/${name}.png`, fullPage: !/^(of-|full-profile|popup-error|original-of)/.test(name), animations: 'disabled'});
    shots.push(name);
  }
  const history = '/?status=pending&operador=ANA&of=42&page=1';
  const sheetUrl = `/sheet/${config.review}?back=${encodeURIComponent(history)}`;
  for (const width of [1440, 1024, 390]) {
    await page.setViewportSize({width, height: 1000});
    await page.goto(config.base + '/?status=', {waitUntil: 'networkidle'});
    await shot(`history-${width}`);
    await page.goto(config.base + sheetUrl, {waitUntil: 'networkidle'});
    await shot(`review-${width}`);
    assert.equal(await page.locator('[name="header_ultima_coluna"]').count(), 0);
    const layout = await page.locator('.review-layout').evaluate(el => ({columns: getComputedStyle(el).gridTemplateColumns.split(' ').length,
      overflow: document.documentElement.scrollWidth > innerWidth}));
    assert.equal(layout.columns, width < 900 ? 1 : 2);
    assert.equal(layout.overflow, false, `page overflow at ${width}`);
    await page.locator('.row-of-wand-btn').first().click();
    await page.locator('.of-entry').first().waitFor();
    await shot(`of-picker-${width}`);
    await page.locator('#of-query').fill('inexistente');
    await page.locator('#of-search-form button').click();
    await page.getByText('Sem resultados.', {exact: false}).waitFor();
    await shot(`of-empty-${width}`);
    await page.keyboard.press('Escape');
    await page.locator('button[onclick*="origem=perf_comp"]').first().click();
    await page.locator('#tabela-plano').waitFor();
    await shot(`full-profile-${width}`);
    await page.keyboard.press('Escape');
    await page.goto(config.base + `/sheet/${config.validated}`, {waitUntil: 'networkidle'});
    assert.equal(await page.locator('.row-of-wand-btn').count(), 0);
    await shot(`validated-${width}`);
    for (const view of ['history', 'sheet']) {
      const reference = await page.goto(`${config.base}/original/${view}?width=${width}`, {waitUntil: 'networkidle'});
      assert.equal(reference.status(), 200);
      await shot(`original-${view}-${width}`);
      if (view === 'sheet') {
        await page.route(/\/sheet\/1\/of-lookup/, route => route.fulfill({contentType: 'application/json', body: JSON.stringify({
          found: true, of: '42', q: '42', mode: 'of', entries: [
            {of: '42', ov: '21', cliente: 'CLIENTE', modelo: 'REF-0', perfil: '60.3x2.9', comp_mm: 1000, remaining: 5, quanttrp: 8, orig_idx: 0},
            {of: '42', ov: '21', cliente: 'CLIENTE', modelo: 'REF-1', perfil: '60.3x2.9', comp_mm: 1500, remaining: 2, quanttrp: 9, orig_idx: 1}
          ]})}));
        await page.locator('.row-of-wand-btn').first().click();
        await page.locator('.of-entry-card').first().waitFor();
        await shot(`original-of-picker-${width}`);
        await page.unroute(/\/sheet\/1\/of-lookup/);
      }
    }
  }
  await page.setViewportSize({width: 1440, height: 1000});
  await page.goto(config.base + sheetUrl, {waitUntil: 'networkidle'});
  // A delayed response from one row cannot overwrite the next dialog.
  await page.route(/\/plano\/0(?:\?|$)/, async route => {
    await new Promise(resolve => setTimeout(resolve, 400));
    await route.fulfill({body: '<div id="stale-response">OLD ROW</div>', contentType: 'text/html'}).catch(() => {});
  });
  await page.locator('button[onclick*="origem=perf_comp"]').first().click();
  await page.keyboard.press('Escape');
  await page.locator('#row-1 button[onclick*="abrirPlano"]').first().click();
  await page.locator('#tabela-plano').waitFor();
  await page.waitForTimeout(500);
  assert.equal(await page.locator('#stale-response').count(), 0);
  await page.keyboard.press('Escape');
  await page.unroute(/\/plano\/0(?:\?|$)/);
  // Read errors offer a retry; errors do not leave focus behind the dialog.
  await page.route(/\/plano\/0(?:\?|$)/, route => { return route.fulfill({status: 503, body: 'offline'}); });
  const opener = page.locator('button[onclick*="origem=perf_comp"]').first();
  await opener.click();
  await page.getByRole('button', {name: 'Tentar novamente'}).waitFor();
  await shot('popup-error-1440');
  await page.keyboard.press('Tab');
  assert.equal(await page.evaluate(() => !!document.activeElement.closest('#plano-modal')), true);
  await page.keyboard.press('Escape');
  assert.equal(await opener.evaluate(el => el === document.activeElement), true);
  await page.unroute(/\/plano\/0(?:\?|$)/);
  // Header edits remain visible across selection + navigation; save only closes on success.
  await page.locator('[name="header_turno"]').fill('T');
  await Promise.all([page.waitForNavigation(), page.locator('#save-header').click()]);
  assert.equal(await page.locator('[name="header_turno"]').inputValue(), 'T');
  await page.locator('[name="header_turno"]').fill('N');
  await page.locator('#row-1 .row-of-wand-btn').click();
  await page.locator('.of-entry').first().waitFor();
  await page.locator('#of-include-done').check();
  await page.waitForFunction(() => document.querySelectorAll('.of-entry').length === 3);
  await page.locator('.of-entry').nth(1).click();
  await Promise.all([page.waitForURL(/focus=row-1/), page.locator('#of-apply').click()]);
  assert.equal(await page.locator('[name="header_turno"]').inputValue(), 'N');
  await Promise.all([page.waitForURL(url => url.pathname === '/'), page.locator('#validate-form button[type=submit]').click()]);
  const final = new URL(page.url());
  assert.equal(final.searchParams.get('status'), 'pending');
  assert.equal(final.searchParams.get('of'), '42');
  assert.equal(final.searchParams.get('operador'), 'ANA');
  assert.equal(final.searchParams.get('page'), '1');
  const response = await context.request.get(`${config.base}/export/basedados`);
  assert.equal(response.status(), 200);
  assert.ok((await response.body()).subarray(0, 2).equals(Buffer.from('PK')));
  // Explicit clear updates storage before navigation; polling cannot restore old filters.
  await page.locator('[data-clear-history]').click();
  assert.ok(!new URL(page.url()).searchParams.has('of'));
  await page.goto(config.base + '/');
  await page.waitForTimeout(200);
  assert.ok(!new URL(page.url()).searchParams.has('of'));
  assert.deepEqual(errors, []);
  fs.writeFileSync(`${config.output}/browser-verification.json`, JSON.stringify({source_app: config.source_app,
    screenshots: shots, passed: ['responsive layout', 'read-only validated sheet', 'OF search/closed entries', 'empty results',
      'stale request cancellation', 'read error/retry', 'focus trap/return', 'independent header save', 'header draft preservation',
      'OF selection save', 'validation redirect/filters', 'BaseDados download', 'clear history memory'], errors}, null, 2));
  await browser.close();
  console.log(`${config.source_app}: browser flow passed, ${shots.length} screenshots`);
})().catch(error => { console.error(error); process.exit(1); });
