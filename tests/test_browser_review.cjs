const {chromium} = require(process.env.PLAYWRIGHT_MODULE);
const fs = require('node:fs');
const assert = require('node:assert/strict');
const config = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
(async () => {
  const browser = await chromium.launch({headless:true});
  for (const state of ['complete', 'error', 'dirty', 'conflict', 'idle', 'http409']) {
    const context = await browser.newContext({viewport: {width:1400, height:900}});
    const page = await context.newPage();
    let navigations = 0, starts = 0, reads = 0, refreshes = 0, release = false;
    const errors = [];
    page.on('framenavigated', frame => { if (frame === page.mainFrame()) navigations++; });
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/sheet/**', async route => {
      const request = route.request();
      if (request.url().includes('/automatic-review')) {
        if (request.method() === 'POST') {
          starts++;
          return route.fulfill({status: state === 'http409' ? 409 : 200, contentType:'application/json', body:JSON.stringify({status:'running'})});
        }
        reads++;
        return route.fulfill({contentType:'application/json', body:JSON.stringify({
          status: release ? (state === 'dirty' ? 'complete' : state) : 'running',
          final_revision: config.revision, error: 'Falha simulada; dados preservados.'
        })});
      }
      if (request.isNavigationRequest()) return route.fulfill({contentType:'text/html',body:config.initial});
      if (request.headers()['x-review-refresh']) refreshes++;
      return route.continue();
    });
    await page.goto(config.url);
    await page.waitForFunction(() => window.kanbanAutomaticReview);
    if (state === 'dirty') await page.locator('[name="header_operador"]').fill('RASCUNHO');
    if (state === 'complete') {
      await page.getByRole('button', {name:'Referências', exact:true}).first().click();
      await page.locator('#tabela-plano').waitFor();
      await page.locator('.plan-filter').fill('REF-2');
    }
    release = true;
    if (['complete', 'error'].includes(state)) {
      await page.waitForFunction(rev => Number(document.querySelector('[data-review-region="header"]').dataset.revision) === rev, config.revision);
      assert.equal(await page.locator('[name="header_operador"]').inputValue(), 'ATUALIZADO');
      if (state === 'complete') {
        await page.waitForTimeout(250);
        assert.equal(await page.locator('#plano-modal').isVisible(), true);
        assert.equal(await page.locator('.plan-filter').inputValue(), 'REF-2');
        assert.equal(await page.locator('#tabela-plano tbody tr:visible').count(), 1);
      } else {
        await page.waitForFunction(() => document.querySelector('[data-automatic-review]')?.textContent.includes('Falha simulada'));
        assert.equal(await page.getByRole('button', {name:'Repetir verificação', exact:true}).count(), 0);
        assert.equal(await page.locator('[data-detail-key="reading"]').getAttribute('open'), null);
      }
    } else if (state === 'dirty') {
      await page.waitForFunction(() => document.querySelector('[data-automatic-review]').textContent.includes('por guardar'));
      assert.equal(await page.locator('[name="header_operador"]').inputValue(),'RASCUNHO');
      assert.equal(refreshes, 0);
    } else {
      await page.waitForTimeout(1800);
      assert.equal(refreshes, 0);
      if (state === 'http409') assert.equal(reads, 0);
    }
    await page.waitForTimeout(1400);
    assert.equal(starts, 1, state + ': repeated start');
    assert.equal(navigations, 1, state + ': navigation');
    assert.deepEqual(errors, [], state);
    await context.close();
  }
  await browser.close();
  console.log('Browser passed: complete, partial error, dirty form, modal, conflict, idle, HTTP 409.');
})().catch(error => { console.error(error); process.exit(1); });
