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
    await submit(page.getByRole('button',{name:'Recalcular conferência'}));
    assert.ok((await page.textContent('body')).includes('Estimativa automática: 6 linhas físicas'));
    await page.locator('[name="count"]').fill('6');
    await submit(page.getByRole('button',{name:'Confirmar contagem'}));
    assert.ok((await page.textContent('body')).includes('Não foi possível confirmar a contagem'));
    assert.equal(await page.locator('[name="count"]').inputValue(),'6');
    for (const i of [0,2]) {
      const form = page.locator(`form[action$="/rows/${i}/exclude"]`);
      await form.locator('[name="reason"]').selectOption('out_of_scope');
      await submit(form.getByRole('button',{name:'Guardar exclusão'}));
    }
    await page.locator('[name="count"]').fill('6');
    await submit(page.getByRole('button',{name:'Confirmar contagem'}));
    assert.ok((await page.textContent('body')).includes('Contagem humana registada: 6'));
    await page.getByText('Linhas excluídas (2)',{exact:true}).click();
    await submit(page.getByRole('button',{name:'Restaurar linha 1',exact:true}));
    assert.ok((await page.textContent('body')).includes('precisa de nova conferência'));
    const revision = await page.locator('[name="revision"]').first().inputValue();
    assert.equal((await page.request.post(url+'/recheck',{form:{revision}})).status(),200);
    await go();
    assert.equal(await page.locator('#row-0').count(),1);
  } else {
    await submit(page.getByRole('button',{name:'Recuperar cabeçalho',exact:true}));
    for (let i=0; i<30; i++) {
      await go();
      if ((await page.textContent('body')).includes('Data no papel:')) break;
      await page.waitForTimeout(100);
    }
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
  assert.deepEqual(errors,[]);
  fs.writeFileSync(`${config.output}/${config.kind}-browser.json`,JSON.stringify({kind:config.kind,passed:true,errors},null,2));
  await browser.close();
  console.log(`${config.kind}: browser recovery flows passed`);
})().catch(error=>{console.error(error);process.exit(1);});
