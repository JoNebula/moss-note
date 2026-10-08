const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium } = require('playwright');

async function main() {
  let uploaded = false;
  let expectedVariant = 'rtn-w8';
  const modelRuntime = { status: 'ready', active: 'bf16', target: null, options: [
    { id: 'bf16', label: 'BF16 (original)', available: true },
    { id: 'rtn-w8', label: 'W8A16 (RTN)', available: true },
    { id: 'rtn-w4', label: 'W4A16 (RTN)', available: true }
  ] };
  const note = { id: 'upload-test', title: 'Upload test', status: 'queued',
    original_filename: 'test.wav', created_at: new Date().toISOString(), segments: [],
    speaker_names: {}, chunk_count: 0, processed_chunks: 0, duration: 0 };
  const server = http.createServer((req, res) => {
    if (req.url === '/api/diagnostics/upload-metrics' && req.method === 'POST') {
      let body = '';
      req.on('data', chunk => { body += chunk; });
      req.on('end', () => {
        const metric = JSON.parse(body);
        assert.equal(metric.bytes, 4 * 1024 * 1024);
        assert(metric.transfer_seconds >= 0 && metric.response_wait_seconds > 0);
        res.writeHead(204); res.end();
      });
      return;
    }
    if (req.url === '/api/notes' && req.method === 'POST') {
      let bytes = 0;
      const chunks = [];
      req.on('data', chunk => { bytes += chunk.length; chunks.push(chunk); });
      req.on('end', async () => {
        assert(bytes > 1024 * 1024);
        const form = await new Request('http://localhost/api/notes', { method: 'POST', headers: req.headers, body: Buffer.concat(chunks) }).formData();
        assert.equal(form.get('model_variant'), expectedVariant);
        note.model_variant = form.get('model_variant');
        setTimeout(() => {
          uploaded = true;
          res.setHeader('Content-Type', 'application/json');
          res.end(JSON.stringify(note));
        }, 1000);
      });
      return;
    }
    if (req.url.startsWith('/api/')) {
      if (req.url === '/api/notes/upload-test' && uploaded) {
        note.status = 'processing';
        note.progress = { phase: 'transcribing', percent: 25, remaining_seconds: 60, estimated: true };
      }
      res.setHeader('Content-Type', 'application/json');
      res.end(JSON.stringify(req.url === '/api/health' ? { vllm: { ok: true }, model_runtime: modelRuntime, limits: { chunk_seconds: 1200, overlap_seconds: 120, max_upload_bytes: 96000000 } } :
        req.url === '/api/notes' ? (uploaded ? [note] : []) : note));
      return;
    }
    const pathname = new URL(req.url, 'http://localhost').pathname;
    const name = pathname === '/' ? 'index.html' : pathname.slice(1);
    if (!['index.html', 'styles.css', 'app.js'].includes(name)) { res.writeHead(404); res.end(); return; }
    res.setHeader('Content-Type', name.endsWith('.css') ? 'text/css' : name.endsWith('.js') ? 'application/javascript' : 'text/html');
    res.end(fs.readFileSync(path.join(__dirname, '../static', name)));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH, headless: true, args: ['--no-sandbox'] });
  try {
    for (const [width, height] of [[1280, 1000], [390, 900], [390, 720]]) {
      uploaded = false;
      expectedVariant = width === 1280 ? 'rtn-w8' : 'rtn-w4';
      const page = await browser.newPage({ viewport: { width, height } });
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.goto(`http://127.0.0.1:${server.address().port}`);
      await page.locator('#chunk-feature').filter({ hasText: '20분 단위 분할' }).waitFor();
      await page.locator('#welcome-upload').click();
      assert.match(await page.locator('#drop-copy').textContent(), /최대 96.0 MB/);
      assert.equal(await page.locator('#model-variant option').count(), 3);
      await page.locator('#model-variant').selectOption(expectedVariant);
      const modelBox = await page.locator('#model-variant').boundingBox();
      assert(modelBox.x >= 0 && modelBox.x + modelBox.width <= width);
      await page.locator('#audio-file').setInputFiles({ name: 'test.wav', mimeType: 'audio/wav', buffer: Buffer.alloc(4 * 1024 * 1024) });
      await page.locator('.submit-upload').click();
      await page.locator('#upload-status').filter({ hasText: '서버 응답 대기 중' }).waitFor();
      assert.equal(await page.locator('#upload-meter').evaluate(el => el.value), 100);
      const box = await page.locator('#upload-progress').boundingBox();
      assert(box.x >= 0 && box.x + box.width <= width);
      if (process.env.SCREENSHOT_DIR) await page.screenshot({ path: `${process.env.SCREENSHOT_DIR}/model-choice-${width}x${height}.png` });
      await page.waitForFunction(() => !document.querySelector('#upload-dialog').open);
      await page.locator('#processing-card').waitFor({ state: 'visible' });
      await page.locator('#transcription-eta').filter({ hasText: '예상 01:00 남음' }).waitFor();
      assert.equal(await page.locator('#transcription-meter').evaluate(el => el.value), 25);
      const etaBox = await page.locator('#transcription-progress').boundingBox();
      assert(etaBox.x >= 0 && etaBox.x + etaBox.width <= width);
      if (process.env.SCREENSHOT_DIR) await page.screenshot({ path: `${process.env.SCREENSHOT_DIR}/eta-${width}x${height}.png` });
      assert.equal(await page.locator('.submit-upload').isEnabled(), true);
      assert.deepEqual(errors, []);
      await page.reload();
      await page.locator('#chunk-feature').filter({ hasText: '20분 단위 분할' }).waitFor();
      await page.locator('#open-upload').click();
      assert.equal(await page.locator('#model-variant').inputValue(), expectedVariant);
      await page.close();
      console.log(`Model choice, preference, upload and completion passed: ${width}x${height}`);
    }
  } finally {
    await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
