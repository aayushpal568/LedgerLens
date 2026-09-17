const { remote } = require('webdriverio');
const { spawn, execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');
const http = require('http');

const APP = path.join(process.env.LOCALAPPDATA, 'LedgerLens', 'ledgerlens.exe');
const DRIVER = path.join(process.env.USERPROFILE, '.cargo', 'bin', 'tauri-driver.exe');
const FIXTURE = path.resolve(__dirname, 'final_ui_synthetic');
const DOWNLOADS = path.join(process.env.USERPROFILE, 'Downloads');
const CLIENT = `Final UI QA & Special ${Date.now()}`;
const NOTE = '=SUM(A1:A2) +123 -123 @test <review> & synthetic note';
const results = [];
const sleep = ms => new Promise(r => setTimeout(r, ms));

function record(name, pass, detail = '') {
  results.push({ name, pass, detail });
  console.log(`[${pass ? 'PASS' : 'FAIL'}] ${name}${detail ? ` — ${detail}` : ''}`);
}
function getJson(route, timeoutMs = 30000) {
  return new Promise(resolve => {
    const req = http.get(`http://127.0.0.1:8001${route}`, res => {
      let body = '';
      res.on('data', c => body += c);
      res.on('end', () => { try { resolve(JSON.parse(body)); } catch { resolve(null); } });
    });
    req.on('error', () => resolve(null));
    req.setTimeout(timeoutMs, () => { req.destroy(); resolve(null); });
  });
}
async function waitBackend(seconds = 90) {
  for (let i = 0; i < seconds; i++) {
    const firm = await getJson('/api/firm', 2000);
    if (firm?.id === 'firm') return true;
    await sleep(1000);
  }
  return false;
}
function killAll() {
  try {
    execFileSync('powershell.exe', ['-NoProfile', '-Command',
      'Get-Process ledgerlens,ledgerlens-backend,tauri-driver,msedgedriver -ErrorAction SilentlyContinue | Stop-Process -Force'],
      { stdio: 'ignore' });
  } catch {}
}
function processCount() {
  try {
    const out = execFileSync('powershell.exe', ['-NoProfile', '-Command',
      '@(Get-Process ledgerlens,ledgerlens-backend,tauri-driver,msedgedriver -ErrorAction SilentlyContinue).Count'],
      { encoding: 'utf8' }).trim();
    return Number(out || 0);
  } catch { return 0; }
}
async function startSession() {
  const driver = spawn(DRIVER, ['--port', '4444'], { stdio: 'pipe' });
  await sleep(1500);
  const browser = await remote({ hostname: '127.0.0.1', port: 4444, path: '/', capabilities: {
    'tauri:options': { application: APP }
  }});
  return { browser, driver };
}
async function clickTestId(browser, id) {
  const el = await browser.$(`[data-testid="${id}"]`);
  await el.waitForDisplayed({ timeout: 30000 });
  await el.click();
  return el;
}
async function chooseNativeFolder() {
  execFileSync('python', [path.resolve(__dirname, 'select_folder.py'), FIXTURE], { stdio: 'inherit' });
}
function downloadSnapshot() {
  return new Set(fs.readdirSync(DOWNLOADS));
}
async function waitNewDownload(before, ext, seconds = 30) {
  for (let i = 0; i < seconds * 2; i++) {
    const names = fs.readdirSync(DOWNLOADS);
    const found = names.find(n => !before.has(n) && n.toLowerCase().endsWith(ext) && !n.endsWith('.crdownload'));
    if (found) return path.join(DOWNLOADS, found);
    await sleep(500);
  }
  return null;
}
async function chooseFindingAndAct(browser, category, actionId, expectedLabel, note) {
  await clickTestId(browser, `exception-category-${category}`);
  const card = await browser.$('[data-testid="exception-card-item"]');
  await card.waitForDisplayed({ timeout: 20000 });
  await card.click();
  await clickTestId(browser, actionId);
  await browser.waitUntil(async () => (await card.getText()).includes(expectedLabel), { timeout: 15000 });
  if (note) {
    const field = await browser.$('[data-testid="finding-note-input"]');
    await field.setValue(note);
    await clickTestId(browser, 'save-note-button');
    await sleep(800);
  }
  return await card.getText();
}
async function verifyCategoryStatus(browser, category, label) {
  await clickTestId(browser, `exception-category-${category}`);
  const card = await browser.$('[data-testid="exception-card-item"]');
  await card.waitForDisplayed({ timeout: 15000 });
  return (await card.getText()).includes(label);
}

async function main() {
  killAll();
  await sleep(1200);
  let browser;
  let driver;
  let downloaded = {};
  try {
    ({ browser, driver } = await startSession());
    await browser.waitUntil(async () => (await browser.getTitle()).includes('LedgerLens'), { timeout: 30000 });
    record('Application window opens', true, await browser.getTitle());
    const backendReady = await waitBackend();
    record('Backend connects', backendReady, backendReady ? '127.0.0.1:8001' : 'timeout');
    const checks = backendReady ? await getJson('/api/system-check', 120000) : null;
    for (const [id, name] of [['sqlite','SQLite works'],['ocr_engine','PaddleOCR offline ready'],['ollama','Ollama connected'],['qwen','Local Qwen ready'],['offline','Local-only status']]) {
      const item = checks?.checks?.find(x => x.id === id);
      record(name, item?.status === 'PASS', item?.explanation || 'missing check');
    }
    const privacy = await browser.$('[data-testid="privacy-status-badge"]');
    record('Local-only privacy visible in UI', (await privacy.getText()).includes('Local'), await privacy.getText());

    const fullTest = await browser.$('button*=Run Full System Test');
    await fullTest.waitForDisplayed({ timeout: 30000 });
    await fullTest.click();
    const liveResult = await browser.$('div*=Live Synthetic System Test Results');
    await liveResult.waitForDisplayed({ timeout: 120000 });
    const pageText = await browser.$('[data-testid="page-content"]').then(x => x.getText());
    record('OCR and Qwen live self-test through UI', pageText.includes('Synthetic OCR Test') && pageText.includes('Synthetic Ollama/Qwen Test') && pageText.includes('PASS'), 'visible live result');

    const continueButton = await browser.$('button*=Continue to App');
    await continueButton.click();
    await clickTestId(browser, 'nav-clients');
    await clickTestId(browser, 'add-client-button');
    const nameInput = await browser.$('[data-testid="client-name-input"]');
    await nameInput.setValue(CLIENT);
    await clickTestId(browser, 'client-create-submit');
    await browser.waitUntil(async () => (await browser.$('[data-testid="app-titlebar"]').getText()).includes(CLIENT), { timeout: 20000 });
    record('Create synthetic client through UI', true, CLIENT);

    await clickTestId(browser, 'nav-scan_workspace');
    const uploadZone = await browser.$$('[data-testid="upload-dropzone"]');
    record('Desktop upload UI absent', uploadZone.length === 0, `${uploadZone.length} upload controls`);
    await clickTestId(browser, 'pick-folder-button');
    await chooseNativeFolder();
    const folderPath = await browser.$('[data-testid="local-folder-path"]');
    await folderPath.waitForDisplayed({ timeout: 15000 });
    record('Select synthetic folder through native picker', (await folderPath.getText()).includes('final_ui_synthetic'), await folderPath.getText());

    await clickTestId(browser, 'start-local-scan-button');
    const progress = await browser.$('[data-testid="scan-progress-panel"]');
    await progress.waitForDisplayed({ timeout: 15000 });
    record('Scan progress displayed', await progress.isDisplayed(), (await progress.getText()).split('\n')[0]);
    const reviewButton = await browser.$('[data-testid="go-to-review-button"]');
    await reviewButton.waitForDisplayed({ timeout: 180000 });
    record('Scan completes through UI', (await progress.getText()).includes('Scan complete'), await progress.getText());
    await reviewButton.click();

    const findings = await browser.$$('[data-testid="exception-card-item"]');
    record('Review Center shows actual finding cards', findings.length >= 4, `${findings.length} cards`);
    record('Click Keep and reflect decision', (await chooseFindingAndAct(browser, 'exact_duplicate', 'mark-keep-button', 'Keep')).includes('Keep'));
    record('Click Keep Both and reflect decision', (await chooseFindingAndAct(browser, 'unreadable', 'mark-keep-both-button', 'Keep Both')).includes('Keep Both'));
    record('Click Ignore and reflect decision', (await chooseFindingAndAct(browser, 'possible_duplicate', 'mark-ignore-button', 'Ignore')).includes('Ignore'));
    record('Click Review Later and Save Note', (await chooseFindingAndAct(browser, 'wrong_period', 'mark-review-later-button', 'Review Later', NOTE)).includes('Review Later'), NOTE);

    await clickTestId(browser, 'nav-reports');
    await clickTestId(browser, 'nav-review_center');
    record('Keep persists after UI reload', await verifyCategoryStatus(browser, 'exact_duplicate', 'Keep'));
    record('Keep Both persists after UI reload', await verifyCategoryStatus(browser, 'unreadable', 'Keep Both'));
    record('Ignore persists after UI reload', await verifyCategoryStatus(browser, 'possible_duplicate', 'Ignore'));
    record('Review Later persists after UI reload', await verifyCategoryStatus(browser, 'wrong_period', 'Review Later'));
    const wrongCard = await browser.$('[data-testid="exception-card-item"]');
    await wrongCard.click();
    const noteValue = await browser.$('[data-testid="finding-note-input"]').then(x => x.getValue());
    record('Reviewer note persists after UI reload', noteValue === NOTE, noteValue);

    await clickTestId(browser, 'review-export-button');
    for (const [fmt, id, ext] of [['csv','export-csv-button','.csv'],['xlsx','export-xlsx-button','.xlsx'],['pdf','export-pdf-button','.pdf']]) {
      const before = downloadSnapshot();
      await clickTestId(browser, id);
      downloaded[fmt] = await waitNewDownload(before, ext);
      record(`Click ${fmt.toUpperCase()} export and create file`, !!downloaded[fmt] && fs.statSync(downloaded[fmt]).size > 0, downloaded[fmt] || 'no new file');
    }

    await browser.deleteSession(); browser = null;
    if (driver) driver.kill(); driver = null;
    await sleep(3000);
    record('No orphan processes after main shutdown', processCount() === 0, `${processCount()} process(es)`);

    ({ browser, driver } = await startSession());
    record('Application relaunches', (await browser.getTitle()).includes('LedgerLens'));
    record('Backend reconnects after full restart', !!(await waitBackend()));
    await clickTestId(browser, 'nav-clients');
    const clientCard = await browser.$(`//*[contains(@data-testid,'client-card-') and contains(.,"${CLIENT}")]`);
    await clientCard.waitForDisplayed({ timeout: 30000 });
    record('Client survives full restart', await clientCard.isDisplayed(), CLIENT);
    const review = await clientCard.$('button*=Review');
    await review.click();
    const restartCards = await browser.$$('[data-testid="exception-card-item"]');
    record('Scan and findings survive full restart', restartCards.length >= 4, `${restartCards.length} findings`);
    record('Keep survives full restart', await verifyCategoryStatus(browser, 'exact_duplicate', 'Keep'));
    record('Keep Both survives full restart', await verifyCategoryStatus(browser, 'unreadable', 'Keep Both'));
    record('Ignore survives full restart', await verifyCategoryStatus(browser, 'possible_duplicate', 'Ignore'));
    record('Review Later survives full restart', await verifyCategoryStatus(browser, 'wrong_period', 'Review Later'));
    const restartWrong = await browser.$('[data-testid="exception-card-item"]'); await restartWrong.click();
    record('Reviewer note survives full restart', await browser.$('[data-testid="finding-note-input"]').then(x => x.getValue()) === NOTE);
    await browser.deleteSession(); browser = null;
    if (driver) driver.kill(); driver = null;
    await sleep(2500);

    let cold = 0;
    for (let i = 1; i <= 3; i++) {
      killAll(); await sleep(1000);
      const proc = spawn(APP, [], { detached: true, stdio: 'ignore' }); proc.unref();
      const ok = !!(await waitBackend(40));
      if (ok) cold++;
      killAll(); await sleep(1800);
      record(`Cold-start cycle ${i}`, ok && processCount() === 0, `backend=${ok}, orphans=${processCount()}`);
    }
    record('Three cold-start cycles pass', cold === 3, `${cold}/3`);
  } catch (err) {
    record('Unhandled UI automation failure', false, err.stack || err.message);
  } finally {
    if (browser) { try { await browser.deleteSession(); } catch {} }
    if (driver) { try { driver.kill(); } catch {} }
    killAll();
  }

  fs.writeFileSync(path.resolve(__dirname, 'final_ui_results.json'), JSON.stringify({ client: CLIENT, note: NOTE, downloaded, results }, null, 2));
  const passed = results.filter(r => r.pass).length;
  console.log(`\nFINAL: ${passed}/${results.length} passed`);
  process.exit(passed === results.length ? 0 : 1);
}
main();
