const {chromium} = require(process.env.PLAYWRIGHT_MODULE);
const fs = require('node:fs');
const assert = require('node:assert/strict');
const config = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
(async () => {
  const browser = await chromium.launch({headless:true});
  const context = await browser.newContext({viewport:{width:1450,height:1000}});
  const page = await context.newPage();
  let navigations=0, edits=0;
  const errors=[];
  page.on('framenavigated', f => {if(f===page.mainFrame()) navigations++;});
  page.on('pageerror', e => errors.push(e.message));
  page.on('request', r => {if(r.method()==='POST' && r.url().endsWith('/edit')) edits++;});
  await page.goto(config.url);
  await page.waitForFunction(() => window.kanbanRowActions);
  assert.equal(await page.locator('[name="reason"], [name="duplicate_of"]').count(),0);
  await page.locator('[name="header_operador"]').fill('RASCUNHO DO OPERADOR');
  // A cell's automatic blur submit must not race the removal button.
  const quantity=page.locator('#row-1 form').filter({has:page.locator('[name="field_path"][value="rows[1].qtd"]')}).locator('[name="value"]');
  await quantity.fill('987');
  await page.getByRole('button',{name:'Retirar linha 1',exact:true}).click();
  await page.locator('#row-0').waitFor({state:'detached'});
  await page.getByRole('button',{name:'Desfazer',exact:true}).waitFor();
  assert.equal(await page.locator('[name="header_operador"]').inputValue(),'RASCUNHO DO OPERADOR');
  assert.equal(await quantity.inputValue(),'987');
  assert.equal(edits,0);
  assert.equal(await page.locator('[data-detail-key="excluded"]').getAttribute('open'),null);
  await page.getByRole('button',{name:'Desfazer',exact:true}).click();
  await page.locator('#row-0').waitFor();
  assert.equal(await quantity.inputValue(),'987');
  // Remove again and restore later from the collapsed section.
  await page.getByRole('button',{name:'Retirar linha 1',exact:true}).click();
  await page.locator('#row-0').waitFor({state:'detached'});
  await page.locator('[data-detail-key="excluded"] summary').click();
  await page.getByRole('button',{name:'Restaurar linha 1',exact:true}).click();
  await page.locator('#row-0').waitFor();
  // Fill all fields before saving; opening the editor creates no blank row.
  for(const [position,value] of [['start','7'],['after:0','8'],['end','9']]) {
    await page.locator('[data-new-row]').click();
    await page.locator('#new-row-position').selectOption(position);
    await page.locator('#new-row-form [name="qtd"]').fill(value);
    if(value==='8') {
      await page.locator('#new-row-lookup summary').click();
      await page.locator('#new-row-search [name="q"]').fill('42');
      await page.locator('#new-row-search button').click();
      await page.locator('#new-row-results button').first().click();
      assert.equal(await page.locator('#new-row-form [name="qtd"]').inputValue(),'8');
    } else {
      await page.locator('#new-row-form [name="of"]').fill('250009');
      await page.locator('#new-row-form [name="modelo"]').fill('MANUAL'+value);
    }
    await page.getByRole('button',{name:'Guardar linha',exact:true}).click();
    await page.waitForFunction(() => !window.kanbanRowActions.busy && !document.querySelector('#new-row-dialog').open);
    assert.equal(await page.locator('[name="header_operador"]').inputValue(),'RASCUNHO DO OPERADOR');
    assert.equal(await quantity.inputValue(),'987');
  }
  assert.deepEqual(await page.locator('[data-production-row]').evaluateAll(rows=>rows.map(r=>Number(r.dataset.productionRow))),[2,0,3,1,4]);
  // A second tab cannot remove rows using a stale revision or overwrite drafts.
  const other=await context.newPage();
  await other.goto(config.url);
  // Reverting an edited value must also discard its saved in-page draft.
  await quantity.fill('33');
  await page.getByRole('button',{name:'Retirar linha 1',exact:true}).click();
  await page.locator('#row-2').waitFor({state:'detached'});
  assert.equal(await quantity.inputValue(),'33');
  await other.locator('[name="header_operador"]').fill('OUTRA ABA');
  await other.getByRole('button',{name:'Retirar linha 2',exact:true}).click();
  await other.waitForFunction(()=>document.querySelector('#row-feedback').textContent.includes('noutra aba'));
  assert.equal(await other.locator('[name="header_operador"]').inputValue(),'OUTRA ABA');
  assert.equal(await other.locator('#row-0').count(),1);
  assert.equal(navigations,1);
  assert.deepEqual(errors,[]);
  await context.close(); await browser.close();
  console.log('Browser passed: remove, undo, later restore, draft cell/header, add start/middle/end, lookup, stale tab; one navigation.');
})().catch(e=>{console.error(e);process.exit(1);});
