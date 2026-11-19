const { chromium } = require('playwright');
const fs = require('fs');
(async () => {
  const url = fs.readFileSync(process.argv[2], 'utf8').trim();
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1060, height: 740 } });
  const errors = [];
  p.on('pageerror', e => errors.push('pageerror: ' + e.message));
  p.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
  await p.goto(url); await p.waitForSelector('.spot', { state: 'attached' }); await p.waitForTimeout(900);
  await p.screenshot({ path: 'shots/voice.png' });

  const param = async n => (await (await p.request.get(url + 'api/state')).json()).params[n];
  const st = async () => (await p.request.get(url + 'api/state')).json();

  // клик по пресету на карте
  await p.click('.spot[data-name="Демон"]'); await p.waitForTimeout(250);
  console.log('Демон pitch', await param('pitch'), 'readout', await p.textContent('#rPitch'), 'puck', await p.evaluate(() => document.getElementById('puck').style.top));

  // перетаскивание по карте
  const box = await (await p.$('#map')).boundingBox();
  await p.click('.spot[data-name="Мой голос"]'); await p.waitForTimeout(250);
  console.log('клик по подписи, preset', (await st()).preset);
  const lb = await (await p.$('.spot[data-name="Здоровяк"]')).boundingBox();
  await p.mouse.move(lb.x + 6, lb.y + 8); await p.mouse.down(); await p.mouse.move(lb.x + 60, lb.y - 40, { steps: 6 }); await p.mouse.up(); await p.waitForTimeout(600);
  console.log('тянем от подписи: pitch', await param('pitch'), 'formant', await param('formant'), 'preset', (await st()).preset);
  await p.mouse.move(box.x + box.width * .4, box.y + box.height * .6); await p.mouse.down();
  await p.mouse.move(box.x + box.width * .75, box.y + box.height * .25, { steps: 8 }); await p.mouse.up();
  await p.waitForTimeout(700);
  console.log('после перетаскивания pitch', await param('pitch'), 'formant', await param('formant'), 'preset', (await st()).preset);

  // клавиатура
  await p.focus('#puck'); await p.keyboard.press('ArrowUp'); await p.waitForTimeout(300);
  console.log('стрелка вверх pitch', await param('pitch'));

  // чип и ползунок
  await p.click('.chip:has-text("Робот")'); await p.waitForTimeout(300);
  console.log('Робот robot', await param('robot'), 'значение ползунка', await p.inputValue('#robot'));
  await p.fill('#echo', '40'); await p.waitForTimeout(300);
  console.log('эхо', await param('echo'));

  // сохранение своего голоса
  await p.fill('#saveName', 'Мой робот'); await p.click('#saveForm button'); await p.waitForTimeout(350);
  console.log('свой голос', Object.keys((await st()).custom), 'чипов', await p.locator('.chip').count());
  await p.fill('#saveName', 'Робот'); await p.click('#saveForm button'); await p.waitForTimeout(350);
  console.log('ошибка имени:', await p.textContent('#saveError'));
  await p.screenshot({ path: 'shots/voice2.png' });
  await p.click('.chip:has-text("Мой робот") .x'); await p.waitForTimeout(300);
  console.log('после удаления', Object.keys((await st()).custom));

  // переключатели
  await p.click('#mute'); await p.waitForTimeout(250);
  console.log('mute', await param('mute'), await p.textContent('#muteText'));
  await p.click('#mute'); await p.click('#effect'); await p.waitForTimeout(250);
  console.log('bypass', await param('bypass'), await p.textContent('#effectText'));
  await p.click('#effect'); await p.waitForTimeout(200);

  // вкладка подключения
  await p.click('#tabConnect'); await p.waitForTimeout(300);
  await p.screenshot({ path: 'shots/connect.png', fullPage: true });
  console.log('микрофоны', await p.locator('#mic option').allTextContents(), 'мониторинг', await p.locator('#monitor option').allTextContents());
  await p.click('#monitorOn'); await p.waitForTimeout(400);
  console.log('слышать себя', (await st()).monitor_on);
  await p.click('#monitorOn'); await p.waitForTimeout(300);
  await p.click('#denoise'); await p.waitForTimeout(250);
  console.log('шумодав', await param('denoise'));
  await p.fill('#gate_db', '-40'); await p.waitForTimeout(300);
  console.log('порог', await param('gate_db'), await p.textContent('#gate_dbOut'), 'метка', await p.evaluate(() => document.getElementById('gateMark').style.left));
  await p.click('#allApps'); await p.waitForTimeout(300);
  console.log('все программы на этой машине (ожидаемо ошибка не Windows):', (await st()).all_apps, await p.textContent('#allAppsError'));

  const widths = [];
  for (let i = 0; i < 25; i++) { widths.push(parseFloat(await p.evaluate(() => document.getElementById('mIn2').style.width))); await p.waitForTimeout(60); }
  console.log('индикатор: максимум', Math.max(...widths).toFixed(0) + '%', 'задержка', await p.textContent('#latency'));
  console.log('видна только вкладка подключения:', await p.isVisible('#connect'), !(await p.isVisible('#voice')));

  // защита API
  const bad = await p.request.get(url.replace(/\/[^/]+\/$/, '/wrongtoken/api/state'));
  console.log('чужой токен ->', bad.status());
  const noh = await p.request.post(url + 'api/do', { data: { action: 'toggle_mute' } });
  console.log('без заголовка ->', noh.status());

  // узкие окна
  await p.setViewportSize({ width: 820, height: 620 }); await p.click('#tabVoice'); await p.waitForTimeout(250);
  await p.screenshot({ path: 'shots/narrow.png' });
  await p.setViewportSize({ width: 390, height: 780 }); await p.waitForTimeout(250);
  await p.screenshot({ path: 'shots/phone.png', fullPage: true });
  console.log('гориз. прокрутка:', await p.evaluate(() => document.documentElement.scrollWidth > innerWidth));
  await p.setViewportSize({ width: 1060, height: 740 });
  await p.click('#tabConnect'); await p.click('#quit'); await p.waitForTimeout(700);
  console.log('ошибки страницы:', errors.filter(e => !e.includes('403')));
  await b.close();
})().catch(e => { console.error(e); process.exit(1); });
