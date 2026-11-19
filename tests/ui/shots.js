// Скриншоты нового окна: node shots.js <файл-с-адресом> <папка>
const { chromium } = require('playwright');
const fs = require('fs');
(async () => {
  const url = fs.readFileSync(process.argv[2], 'utf8').trim();
  const out = process.argv[3];
  const scale = +(process.argv[4] || 1);
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1100, height: 760 }, deviceScaleFactor: scale });
  const errors = [];
  p.on('pageerror', e => errors.push(e.message));
  await p.goto(url); await p.waitForSelector('.spot', { state: 'attached' }); await p.waitForTimeout(1600);
  console.log('открылась вкладка:', await p.isVisible('#connect') ? 'Подключение' : 'Голос');
  await p.screenshot({ path: out + '/1-connect.png' });
  await p.click('#tabVoice'); await p.click('.spot[data-name="Демон"]'); await p.waitForTimeout(700);
  await p.screenshot({ path: out + '/2-voice.png' });
  await p.click('.notice .btn.main'); await p.waitForTimeout(900);
  console.log('после клика открыта вкладка Подключение:', await p.isVisible('#connect'));
  await p.screenshot({ path: out + '/3-download.png' });
  const seen = new Set();
  for (let i = 0; i < 120; i++) {
    const t = await p.textContent('#setupBox');
    const now = await p.locator('.progress li.now').allTextContents();
    if (now.length) seen.add(now[0].slice(0, 30));
    if (/Устанавливаю/.test(now[0] || '') && !fs.existsSync(out + '/4-install.png')) await p.screenshot({ path: out + '/4-install.png' });
    if (/Готово/.test(t)) break;
    await p.waitForTimeout(100);
  }
  console.log('шаги, которые показало окно:', [...seen]);
  await p.waitForTimeout(600);
  await p.screenshot({ path: out + '/5-done.png' });
  console.log('плашка про VB-CABLE исчезла:', await p.locator('.notice').count() === 0);
  console.log('шаг 1 и 3 отмечены:', await p.getAttribute('#step1', 'class'), await p.getAttribute('#step3', 'class'));
  console.log('кабель выбран:', await p.inputValue('#out'));
  console.log('гориз. прокрутка:', await p.evaluate(() => document.documentElement.scrollWidth > innerWidth));
  console.log('ошибки страницы:', errors);
  await b.close();
})().catch(e => { console.error(e); process.exit(1); });
