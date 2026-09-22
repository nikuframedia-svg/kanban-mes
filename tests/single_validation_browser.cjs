const fs = require('node:fs');
const assert = require('node:assert/strict');
const config = JSON.parse(fs.readFileSync(0, 'utf8'));
const {chromium} = require(config.playwright);
(async () => {
  const browser = await chromium.launch({executablePath: config.chromium, headless: true, args: ['--no-sandbox']});
  // The async editor also initializes when an OCR-pending page is refreshed in place.
  const pending = await browser.newPage();
  let firstNavigation = true;
  await pending.route(config.url + '/automatic-review', route => route.fulfill({json: {sheet_status: 'extracted', status: 'idle'}}));
  await pending.route(config.url, async route => {
    if (firstNavigation && route.request().isNavigationRequest()) {
      firstNavigation = false;
      const response = await route.fetch();
      let html = await response.text();
      html = html.replace(/<form[^>]*id="validate-form"[\s\S]*?<\/form>/, '');
      html = html.replace('<body', `<div data-ocr-pending="${config.url}/automatic-review"></div><body`);
      await route.fulfill({response, body: html});
    } else await route.continue();
  });
  await pending.goto(config.url);
  await pending.waitForFunction(() => Boolean(window.reviewEdits));
  await pending.locator('[name="header_operador"]').fill('OCR READY');
  await pending.locator('[name="header_operador"]').press('Enter');
  await pending.waitForFunction(() => document.querySelector('#header-save-status')?.textContent === 'Guardado');
  await pending.close();
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
  const errors = [], dialogs = [], validations = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('dialog', async dialog => { dialogs.push(dialog.message()); await dialog.dismiss(); });
  page.on('request', request => { if (request.method() === 'POST' && request.url().endsWith('/validate')) validations.push(request); });
  const qty = '.cell-edit-form[data-field-path="rows[0].qtd"]';
  const saved = selector => page.waitForFunction(s => document.querySelector(s)?.textContent === 'Guardado', selector);
  await page.goto(config.url);
  await page.waitForFunction(() => Boolean(window.reviewEdits));
  assert.equal(await page.getByText('Corrigir contagem manualmente', {exact: true}).count(), 0);
  assert.equal(await page.locator('[name="count"]').count(), 0);
  await page.locator('[name="header_operador"]').fill('BROWSER');
  await page.locator('[name="header_operador"]').press('Enter');
  await saved('#header-save-status');
  assert.equal(validations.length, 0);
  // Blur while opening the editor must finish the save without navigating.
  await page.route('**/of-lookup?*', route => route.fulfill({json: {
    entries: [], revision: 1, snapshot_id: 'test', selection_kind: 'reference', offset: 0, has_more: false}}));
  const model = '.cell-edit-form[data-field-path="rows[0].modelo"] input[name="value"]';
  const oldModel = await page.locator(model).inputValue();
  await page.locator(model).fill('');
  await page.locator('#edit-row-0').click();
  await page.locator('#of-query').waitFor({state: 'visible'});
  await page.locator('#of-query').fill('');
  await page.locator('#of-query').press('Enter');
  assert.equal(await page.locator('#of-modal').isVisible(), true);
  await page.locator('#of-cancel').click();
  await page.locator(model).fill(oldModel);
  await page.locator(model).press('Enter');
  await saved('.cell-edit-form[data-field-path="rows[0].modelo"] .edit-status');
  assert.equal(validations.length, 0);
  // Preserve a new draft typed before an older save response arrives.
  let delayed = false;
  await page.route('**/edit', async route => {
    if (!delayed) {
      delayed = true;
      const response = await route.fetch();
      await new Promise(resolve => setTimeout(resolve, 350));
      await route.fulfill({response});
    } else await route.continue();
  });
  await page.locator(qty + ' input[name="value"]').fill('17');
  await page.locator(qty + ' input[name="value"]').press('Enter');
  await page.waitForTimeout(100);
  await page.locator(qty + ' input[name="value"]').fill('18');
  await page.waitForTimeout(500);
  assert.equal(await page.locator(qty + ' input[name="value"]').inputValue(), '18');
  await page.locator(qty + ' input[name="value"]').press('Enter');
  await saved(qty + ' .edit-status');
  await page.unroute('**/edit');
  // Errors retain text and inhibit validation until a successful retry.
  await page.route('**/edit', route => route.fulfill({status: 503, json: {ok: false, saved: false, detail: 'Falha simulada'}}));
  await page.locator(qty + ' input[name="value"]').fill('19');
  await page.locator(qty + ' input[name="value"]').press('Enter');
  await page.locator(qty + ' .save-error').waitFor();
  await page.locator('#validate-button').click();
  assert.equal(validations.length, 0);
  assert.equal(await page.locator(qty + ' input[name="value"]').inputValue(), '19');
  await page.unroute('**/edit');
  await page.locator(qty + ' [data-save-retry]').click();
  await saved(qty + ' .edit-status');
  // Removing and undoing uses the same queue and keeps the current editor usable.
  await page.locator('form[data-row-action][data-row="1"] button').click();
  await page.getByRole('button', {name: 'Desfazer', exact: true}).waitFor();
  await page.getByRole('button', {name: 'Desfazer', exact: true}).click();
  await page.waitForFunction(() => document.querySelector('#row-feedback')?.textContent.includes('restaurada'));
  assert.equal(await page.locator(qty + ' input[name="value"]').inputValue(), '19');
  for (const width of [390, 1440]) {
    await page.setViewportSize({width, height: 1000});
    await page.screenshot({path: `${config.output}/validation-${width}.png`, fullPage: true, animations: 'disabled'});
  }
  // A final unsaved header is flushed by the button; repeated activation is ignored.
  await page.locator('[name="header_operador"]').fill('BROWSER FINAL');
  assert.equal(validations.length, 0);
  await page.locator('#validate-button').evaluate(button => {
    document.querySelector('form[data-row-action][data-row="2"] button').click();
    button.click(); button.click();
  });
  await page.waitForURL(url => !url.pathname.startsWith('/sheet/'));
  assert.equal(validations.length, 1);
  assert.deepEqual(dialogs, []);
  assert.deepEqual(errors, []);
  await browser.close();
  console.log('Browser: Enter, clearing popup, delayed writes, errors/retry, remove/undo, responsive view, one validation passed.');
})().catch(error => { console.error(error); process.exit(1); });
