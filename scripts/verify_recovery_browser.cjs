const fs = require('node:fs');
const assert = require('node:assert/strict');
const config = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const {chromium} = require(config.playwright);
(async () => {
  const browser = await chromium.launch({headless:true});
  const page = await browser.newPage({viewport:{width:1440,height:1000}});
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const url = `${config.base}/sheet/${config.uid}`;
  const go = async () => assert.equal((await page.goto(url)).status(), 200);
  const submit = async locator => Promise.all([page.waitForNavigation(), locator.click()]);
  await go();
  if (config.kind === 'perfis') {
    await page.waitForFunction(() => !document.querySelector('[data-automatic-review]'), null, {timeout:15000}).catch(async error => { console.log(JSON.stringify({errors, panel:await page.locator('[data-automatic-review]').textContent(), inputs:await page.locator('input').evaluateAll(inputs=>inputs.filter(i=>i.value!==i.defaultValue).map(i=>({name:i.name,value:i.value,default:i.defaultValue}))) })); throw error; });
    assert.equal(await page.locator('[name="count"]').count(),0);
    assert.ok((await page.textContent('body')).includes('6 linhas detetadas no papel'));
    for (const i of [0,2]) {
      const form = page.locator(`form[action$="/rows/${i}/exclude"]`);
      await form.locator('[name="reason"]').selectOption('out_of_scope');
      await submit(form.getByRole('button',{name:'Guardar exclusão'}));
    }
    assert.equal(await page.locator('[name="count"]').count(),0);
    assert.equal(await page.locator('[data-automatic-review]').count(),0);
    await page.getByText('Linhas excluídas (2)',{exact:true}).click();
    await submit(page.getByRole('button',{name:'Restaurar linha 1',exact:true}));
    assert.equal(await page.locator('[name="count"]').count(),0);
    const revision = await page.locator('[name="revision"]').first().inputValue();
    assert.equal((await page.request.post(url+'/recheck',{form:{revision}})).status(),200);
    await go();
    assert.equal(await page.locator('#row-0').count(),1);
  } else {
    await page.waitForFunction(() => !document.querySelector('[data-automatic-review]'), null, {timeout:15000}).catch(async error => { console.log(JSON.stringify({errors, panel:await page.locator('[data-automatic-review]').textContent(), inputs:await page.locator('input').evaluateAll(inputs=>inputs.filter(i=>i.value!==i.defaultValue).map(i=>({name:i.name,value:i.value,default:i.defaultValue}))) })); throw error; });
    assert.equal(await page.getByRole('button',{name:'Recuperar cabeçalho',exact:true}).count(),0);
    assert.equal(await page.locator('[name="header_n_operador"]').inputValue(),'2849');
    const body = await page.textContent('body');
    assert.ok(body.includes('21/08/2026') && body.includes('20/08/2026'));
    await submit(page.getByRole('button',{name:'Confirmar data pela regra'}));
    await page.locator('[name="header_data"]').fill('2026-08-21');
    await submit(page.locator('#save-header'));
    const revision = await page.locator('[name="revision"]').first().inputValue();
    assert.equal((await page.request.post(url+'/recheck',{form:{revision}})).status(),200);
    await go();
    assert.equal(await page.locator('[name="header_data"]').inputValue(),'2026-08-21');
  }
  for (const width of [1440,390]) {
    await page.setViewportSize({width,height:1000});
    await go();
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    await page.waitForFunction(()=>document.querySelector('#folha-img').naturalWidth>0);
    await page.screenshot({path:`${config.output}/${config.kind}-${width}.png`,fullPage:true});
  }
  // A background completion must not reload over a user's unsaved input.
  const draftPage = await browser.newPage();
  let ready = false, navigations = 0;
  draftPage.on('framenavigated', () => { navigations++; });
  const automaticScript = fs.readFileSync(require('node:path').join(__dirname,'../app/web/static/automatic-review.js'),'utf8');
  await draftPage.route('http://automatic.test/**', async route => {
    if (route.request().url().endsWith('/automatic-review')) {
      await route.fulfill({contentType:'application/json',body:JSON.stringify({status:ready ? 'complete' : 'running'})});
    } else {
      await route.fulfill({contentType:'text/html; charset=utf-8',body:`<!doctype html><meta charset="utf-8"><body><input name="operator"><div data-automatic-review="/sheet/test/automatic-review" data-revision="1">A verificar</div><script>${automaticScript}</script></body>`});
    }
  });
  await draftPage.goto('http://automatic.test/sheet/test');
  await draftPage.locator('[name="operator"]').fill('ALTERAÇÃO POR GUARDAR');
  ready = true;
  await draftPage.getByText('As tuas alterações por guardar foram mantidas.',{exact:false}).waitFor({timeout:5000}).catch(async error => {console.log('DRAFT',await draftPage.content(),navigations);throw error;});
  assert.equal(await draftPage.locator('[name="operator"]').inputValue(),'ALTERAÇÃO POR GUARDAR');
  assert.equal(navigations,1);
  await draftPage.close();
  assert.deepEqual(errors,[]);
  fs.writeFileSync(`${config.output}/${config.kind}-browser.json`,JSON.stringify({kind:config.kind,passed:true,errors},null,2));
  await browser.close();
  console.log(`${config.kind}: browser recovery flows passed`);
})().catch(error=>{console.error(error);process.exit(1);});
