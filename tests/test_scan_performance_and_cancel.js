/**
 * Test: 3-file PDF/PNG scan performance, mid-scan cancellation, and subsequent scan.
 */
const { remote } = require('webdriverio');
const { spawn, execSync } = require('child_process');
const path = require('path');
const fs = require('fs');
const http = require('http');

const REPO_ROOT = path.resolve(__dirname, '..');
const APP_EXE = path.resolve(process.env.LOCALAPPDATA, 'LedgerLens', 'ledgerlens.exe');
const THREE_FILES_DIR = path.resolve(REPO_ROOT, 'tests', 'synthetic_3files');
const BACKEND_URL = 'http://127.0.0.1:8001';

let BACKEND_TOKEN = '';

function httpGet(url) {
  return new Promise((resolve, reject) => {
    const u = new URL(url);
    const opts = {
      hostname: u.hostname,
      port: u.port,
      path: u.pathname + (u.search || ''),
      method: 'GET',
      headers: BACKEND_TOKEN ? { 'x-ledgerlens-token': BACKEND_TOKEN } : {}
    };
    const req = http.request(opts, (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => {
        try {
          resolve({ status: res.statusCode, data: JSON.parse(data) });
        } catch (e) {
          resolve({ status: res.statusCode, raw: data });
        }
      });
    });
    req.on('error', reject);
    req.end();
  });
}

function httpPost(url, payload) {
  return new Promise((resolve, reject) => {
    const postData = JSON.stringify(payload);
    const u = new URL(url);
    const req = http.request({
      hostname: u.hostname,
      port: u.port,
      path: u.pathname,
      method: 'POST',
      headers: Object.assign({
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(postData)
      }, BACKEND_TOKEN ? { 'x-ledgerlens-token': BACKEND_TOKEN } : {})
    }, (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => {
        try {
          resolve({ status: res.statusCode, data: JSON.parse(data) });
        } catch (e) {
          resolve({ status: res.statusCode, raw: data });
        }
      });
    });
    req.on('error', reject);
    req.write(postData);
    req.end();
  });
}

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

async function main() {
  console.log('===============================================================');
  console.log('  Testing 3-File PDF/PNG Scan Performance & Cancellation');
  console.log('===============================================================\n');

  let tauriDriverProc = null;
  let browser = null;

  try {
    console.log('[1] Spawning tauri-driver on port 4444...');
    const tauriDriverExe = path.resolve(process.env.USERPROFILE, '.cargo', 'bin', 'tauri-driver.exe');
    tauriDriverProc = spawn(tauriDriverExe, ['--port', '4444'], { stdio: 'pipe' });
    await sleep(2000);

    console.log('[2] Launching LedgerLens...');
    browser = await remote({
      hostname: '127.0.0.1',
      port: 4444,
      path: '/',
      capabilities: { 'tauri:options': { application: APP_EXE } }
    });

    await sleep(3000);
    BACKEND_TOKEN = await browser.execute(() => window.__TAURI__.core.invoke('backend_token'));
    console.log(`[3] Acquired backend token (${BACKEND_TOKEN.substring(0, 8)}...)`);

    // Wait for backend to be ready
    console.log('[4] Waiting for backend readiness...');
    let ready = false;
    for (let i = 0; i < 30; i++) {
      const res = await httpGet(`${BACKEND_URL}/api/firm`).catch(() => null);
      if (res && res.status === 200) { ready = true; break; }
      await sleep(1000);
    }
    if (!ready) throw new Error('Backend did not become ready');
    console.log('    Backend is ready.');

    // Create client
    const clientRes = await httpPost(`${BACKEND_URL}/api/clients`, {
      name: '3-File Scan Test Client',
      client_type: 'Small Business'
    });
    const clientId = clientRes.data.id;
    console.log(`[5] Created test client: ${clientId}`);

    // TEST A: Scan performance on 3 synthetic PDF/PNG files
    console.log('\n--- TEST A: 3-File PDF/PNG Scan Execution & Performance ---');
    const startScanA = Date.now();
    const scanARes = await httpPost(`${BACKEND_URL}/api/clients/${clientId}/scan-local`, {
      folder_path: THREE_FILES_DIR,
      expected_period: 2024
    });
    const scanAId = scanARes.data.id;
    console.log(`    Scan A started: ID ${scanAId}`);

    let completedScanA = null;
    for (let i = 0; i < 60; i++) {
      const s = await httpGet(`${BACKEND_URL}/api/scans/${scanAId}`);
      if (s.data && ['completed', 'error', 'cancelled'].includes(s.data.status)) {
        completedScanA = s.data;
        break;
      }
      await sleep(500);
    }
    const elapsedA = ((Date.now() - startScanA) / 1000).toFixed(2);
    console.log(`    Scan A completed in ${elapsedA}s with status: ${completedScanA?.status}`);
    if (completedScanA?.status !== 'completed') {
      throw new Error(`Scan A failed with status: ${completedScanA?.status}`);
    }
    console.log(`    Scan A findings: ${completedScanA.total_findings}, processed: ${completedScanA.processed_files}/${completedScanA.total_files}`);
    console.log('    [PASS] TEST A passed: 3-file scan completed in bounded time.');

    // TEST B: Mid-scan Cancellation
    console.log('\n--- TEST B: Mid-Scan Cancellation ---');
    const scanBRes = await httpPost(`${BACKEND_URL}/api/clients/${clientId}/scan-local`, {
      folder_path: THREE_FILES_DIR,
      expected_period: 2024
    });
    const scanBId = scanBRes.data.id;
    console.log(`    Scan B started: ID ${scanBId}`);

    // Wait a brief moment to allow scan to begin processing
    await sleep(200);

    const cancelStart = Date.now();
    const cancelRes = await httpPost(`${BACKEND_URL}/api/scans/${scanBId}/cancel`, {});
    console.log(`    Cancel requested, response status: ${cancelRes.data?.status}`);

    let cancelledScanB = null;
    for (let i = 0; i < 30; i++) {
      const s = await httpGet(`${BACKEND_URL}/api/scans/${scanBId}`);
      if (s.data && ['cancelled', 'completed', 'error'].includes(s.data.status)) {
        cancelledScanB = s.data;
        if (s.data.status === 'cancelled') break;
      }
      await sleep(200);
    }
    const cancelElapsed = ((Date.now() - cancelStart) / 1000).toFixed(2);
    console.log(`    Scan B ended with status '${cancelledScanB?.status}' in ${cancelElapsed}s`);
    if (cancelledScanB?.status !== 'cancelled') {
      throw new Error(`Expected status 'cancelled', got: ${cancelledScanB?.status}`);
    }
    console.log('    [PASS] TEST B passed: Cancel request successfully stopped scan.');

    // TEST C: Verify sidecar health after cancellation
    console.log('\n--- TEST C: Sidecar Health After Cancellation ---');
    const firmCheck = await httpGet(`${BACKEND_URL}/api/firm`);
    if (firmCheck.status !== 200) throw new Error('Sidecar unhealthy after cancellation');
    console.log('    Sidecar responded with HTTP 200 OK after cancellation.');
    console.log('    [PASS] TEST C passed: Sidecar remains healthy.');

    // TEST D: Subsequent scan works immediately after cancellation
    console.log('\n--- TEST D: Subsequent Scan Execution After Cancellation ---');
    const scanCRes = await httpPost(`${BACKEND_URL}/api/clients/${clientId}/scan-local`, {
      folder_path: THREE_FILES_DIR,
      expected_period: 2024
    });
    const scanCId = scanCRes.data.id;
    console.log(`    Scan C started: ID ${scanCId}`);

    let completedScanC = null;
    for (let i = 0; i < 60; i++) {
      const s = await httpGet(`${BACKEND_URL}/api/scans/${scanCId}`);
      if (s.data && ['completed', 'error', 'cancelled'].includes(s.data.status)) {
        completedScanC = s.data;
        break;
      }
      await sleep(500);
    }
    console.log(`    Scan C finished with status: ${completedScanC?.status}, findings: ${completedScanC?.total_findings}`);
    if (completedScanC?.status !== 'completed') {
      throw new Error(`Scan C failed with status: ${completedScanC?.status}`);
    }
    console.log('    [PASS] TEST D passed: Subsequent scan works cleanly after cancellation.');

    // Clean up test client
    await new Promise((resolve) => {
      const u = new URL(`${BACKEND_URL}/api/clients/${clientId}`);
      const req = http.request({
        hostname: u.hostname, port: u.port, path: u.pathname, method: 'DELETE',
        headers: BACKEND_TOKEN ? { 'x-ledgerlens-token': BACKEND_TOKEN } : {}
      }, resolve);
      req.on('error', resolve);
      req.end();
    });

    console.log('\n===============================================================');
    console.log('  ALL 3-FILE SCAN & CANCELLATION TESTS PASSED (100%)');
    console.log('===============================================================');

  } catch (err) {
    console.error('[-] Test failed:', err);
    process.exit(1);
  } finally {
    if (browser) {
      try { await browser.deleteSession(); } catch (e) {}
    }
    if (tauriDriverProc) {
      try { tauriDriverProc.kill(); } catch (e) {}
    }
    try {
      execSync('powershell -Command "taskkill /F /IM tauri-driver.exe /T; taskkill /F /IM msedgedriver.exe /T; taskkill /F /IM ledgerlens.exe /T; taskkill /F /IM ledgerlens-backend.exe /T"', { stdio: 'ignore' });
    } catch (e) {}
  }
}

main();
