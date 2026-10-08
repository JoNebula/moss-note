const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium } = require('playwright');

async function main() {
  const original = [
    { id: 0, start: 0, end: 10, speaker: 'S01', text: '쿠버네티스에서 레디스 캐시를 쓰면 될 것 같습니다.' },
    { id: 1, start: 10, end: 20, speaker: 'S02', text: '금요일까지 캐시를 테스트하고 결과를 공유하겠습니다.' }
  ];
  let note;
  let mergeHistory;
  let mediaLoads = 0;
  const reset = () => {
    note = { id: 'correction-test', title: '랩 미팅', status: 'done', original_filename: 'meeting.wav',
      created_at: new Date().toISOString(), duration: 20, chunk_count: 1, processed_chunks: 1,
      model_variant: 'rtn-w4', segments: structuredClone(original), raw_segments: structuredClone(original),
      speaker_names: { S01: '연구원 "A"', S02: '연구원 B' }, correction_status: 'idle', corrected_segments: [], summary: {},
      speaker_merge_undo_count: 0, summary_stale: false };
    mergeHistory = [];
    mediaLoads = 0;
  };
  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, 'http://localhost');
    const json = body => { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(body)); };
    if (url.pathname.endsWith('/media')) {
      mediaLoads++;
      const wav = Buffer.alloc(44 + 32000 * 20);
      wav.write('RIFF'); wav.writeUInt32LE(wav.length - 8, 4); wav.write('WAVEfmt ', 8);
      wav.writeUInt32LE(16, 16); wav.writeUInt16LE(1, 20); wav.writeUInt16LE(1, 22);
      wav.writeUInt32LE(16000, 24); wav.writeUInt32LE(32000, 28);
      wav.writeUInt16LE(2, 32); wav.writeUInt16LE(16, 34); wav.write('data', 36); wav.writeUInt32LE(wav.length - 44, 40);
      res.setHeader('Content-Type', 'audio/wav'); res.end(wav); return;
    }
    if (url.pathname.includes('/export/') || url.pathname.includes('/summary/')) {
      res.setHeader('Content-Type', 'text/plain');
      res.setHeader('Content-Disposition', 'attachment; filename=result.txt');
      res.end(url.pathname.includes('/summary/') ? '# Redis 캐시 검토' : 'Kubernetes에서 Redis 캐시를 쓰면 될 것 같습니다.'); return;
    }
    if (url.pathname === '/api/health') {
      json({ vllm: { ok: true }, codex: { available: true },
        limits: { chunk_seconds: 1200, overlap_seconds: 120, max_upload_bytes: 96000000 },
        model_runtime: { active: 'rtn-w4', target: null, options: [{ id: 'rtn-w4', label: 'W4A16 (RTN)', available: true }] } }); return;
    }
    if (url.pathname.endsWith('/postprocess')) {
      assert.equal(req.method, 'POST');
      note.correction_status = 'processing'; note.correction_phase = 'whole-file'; note.correction_mode = 'whole-file';
      note.correction_service_tier = 'priority';
      note.correction_total_windows = 1; note.correction_processed_windows = 0;
      note.correction_source_segments = structuredClone(note.segments);
      json(note); return;
    }
    if (url.pathname.endsWith('/speakers/merge')) {
      assert.equal(req.method, 'POST');
      let body = ''; for await (const chunk of req) body += chunk;
      const selection = JSON.parse(body);
      mergeHistory.push({ assignments: note.segments.map(item => item.speaker), names: structuredClone(note.speaker_names) });
      for (const field of ['segments', 'corrected_segments', 'correction_source_segments']) {
        note[field] = note[field].map(item => ({ ...item, speaker: selection.sources.includes(item.speaker) ? selection.target : item.speaker }));
      }
      for (const speaker of selection.sources) if (speaker !== selection.target) delete note.speaker_names[speaker];
      note.speaker_merge_undo_count = mergeHistory.length; note.summary_stale = true;
      json(note); return;
    }
    if (url.pathname.endsWith('/speakers/undo')) {
      assert.equal(req.method, 'POST');
      const previous = mergeHistory.pop();
      for (const field of ['segments', 'corrected_segments', 'correction_source_segments']) {
        note[field] = note[field].map(item => ({ ...item, speaker: previous.assignments[item.id] }));
      }
      note.speaker_names = { ...previous.names, ...note.speaker_names };
      note.speaker_merge_undo_count = mergeHistory.length;
      json(note); return;
    }
    if (url.pathname === '/api/notes') { json([note]); return; }
    if (url.pathname === '/api/notes/correction-test') {
      if (req.method === 'PATCH') {
        let body = ''; for await (const chunk of req) body += chunk;
        const changes = JSON.parse(body);
        for (const field of ['segments', 'corrected_segments']) {
          if (changes[field]) changes[field] = note[field].map(item => ({ ...item, text: changes[field].find(edit => edit.id === item.id).text }));
        }
        note = { ...note, ...changes, summary_stale: Boolean(note.summary.title) };
      }
      json(note); return;
    }
    const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    if (!['index.html', 'styles.css', 'app.js'].includes(name)) { res.writeHead(404); res.end(); return; }
    res.setHeader('Content-Type', name.endsWith('.css') ? 'text/css' : name.endsWith('.js') ? 'application/javascript' : 'text/html');
    res.end(fs.readFileSync(path.join(__dirname, '../static', name)));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH, headless: true, args: ['--no-sandbox'] });
  try {
    for (const [width, height] of [[1280, 1000], [390, 900], [390, 720]]) {
      reset();
      const page = await browser.newPage({ viewport: { width, height }, acceptDownloads: true });
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.goto(`http://127.0.0.1:${server.address().port}`);
      if (width <= 620) await page.locator('#mobile-note-picker').selectOption('correction-test');
      else await page.locator('.note-item').click();
      await page.locator('#correction-copy').filter({ hasText: 'gpt-6.1-sol' }).waitFor();
      assert.equal(await page.locator('[data-speaker="S01"]').inputValue(), '연구원 "A"');
      assert.equal(await page.locator('#merge-speakers').isDisabled(), true);
      assert.equal(await page.locator('#undo-speaker-merge').isDisabled(), true);
      assert.equal(await page.locator('[data-transcript-view="summary"]').isDisabled(), true);
      await page.locator('#media-player').evaluate(async audio => { await audio.play(); });
      const loads = mediaLoads;
      await page.locator('#start-correction').click();
      await page.locator('#start-correction').filter({ hasText: '교정 중' }).waitFor();
      assert.equal(await page.locator('#start-correction').isDisabled(), true);
      assert.equal(await page.locator('#delete-note').isDisabled(), true);
      assert.equal(await page.locator('.segment-text').first().getAttribute('contenteditable'), 'false');
      assert.equal(await page.locator('[data-merge-speaker="S02"]').isDisabled(), true);
      assert.equal(await page.locator('#merge-speaker-target').isDisabled(), true);
      assert.equal(await page.locator('#correction-progress').getAttribute('value'), null);
      await page.locator('#correction-copy').filter({ hasText: '전체 전사 파일 교정·요약 중' }).waitFor();
      await page.waitForFunction(() => document.querySelector('#media-player').currentTime >= 3);
      assert.equal(mediaLoads, loads, 'Polling must not reload audio');
      assert(await page.locator('#media-player').evaluate(audio => audio.currentTime > 0 && !audio.paused));
      note.corrected_segments = original.map(segment => ({ ...segment,
        text: segment.id === 0 ? 'Kubernetes에서 Redis 캐시를 쓰면 될 것 같습니다.' : segment.text }));
      note.correction_status = 'done'; note.correction_phase = 'done'; note.correction_changes = 1;
      note.correction_processed_windows = 1;
      note.correction_model = 'gpt-6.1-sol'; note.correction_usage = { input_tokens: 8000, output_tokens: 400 };
      note.summary = { title: 'Redis 캐시 도입 제안과 테스트 계획', overview: 'Kubernetes에서 Redis 캐시 사용을 검토했다.',
        key_points: [{ text: 'Kubernetes에서 Redis 캐시를 사용하는 방안이 제안되었다.', segment_ids: [0] }],
        decisions: [], action_items: [{ text: '연구원 B가 금요일까지 캐시를 테스트하고 결과를 공유한다.', segment_ids: [1] }],
        open_questions: [{ text: '<img src=x onerror="window.injected=true">는 전사 데이터다.', segment_ids: [0] }] };
      await page.locator('#result-downloads').waitFor({ state: 'visible' });
      await page.locator('[data-transcript-view="diff"]').click();
      assert.equal(await page.locator('.original-text').count(), 1);
      await page.locator('[data-transcript-view="summary"]').click();
      await page.locator('#summary-note').waitFor({ state: 'visible' });
      assert.equal(await page.locator('#summary-note img').count(), 0);
      assert.equal(await page.evaluate(() => window.injected), undefined);
      assert.equal(await page.locator('#segments').isVisible(), false);
      assert.equal(await page.locator('#summary-note [data-seek]').count(), 0);
      assert.equal(await page.locator('#summary-note button').count(), 0);
      assert.equal(await page.locator('#summary-note').textContent().then(text => text.includes('00:10')), false);
      await page.locator('#correction-copy').filter({ hasText: 'Fast' }).waitFor();
      for (const selector of ['#summary-note', '#result-downloads', '#transcript-views', '#correction-card']) {
        const box = await page.locator(selector).boundingBox();
        assert(box.x >= 0 && box.x + box.width <= width, `${selector} fits at ${width}`);
      }
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.waitForFunction(() => window.scrollY === 0);
      if (process.env.SCREENSHOT_DIR) await page.screenshot({ path: `${process.env.SCREENSHOT_DIR}/summary-${width}x${height}.png`, fullPage: true });
      const download = page.waitForEvent('download');
      await page.locator('#summary-download').click();
      const result = await download;
      assert.equal(result.suggestedFilename(), 'result.txt');
      await page.locator('[data-transcript-view="corrected"]').click();
      assert.equal(await page.locator('.segment-text').first().textContent(), 'Kubernetes에서 Redis 캐시를 쓰면 될 것 같습니다.');
      await page.locator('#segments [data-seek="10"]').click();
      await page.waitForFunction(() => document.querySelector('#media-player').currentTime >= 9.9);
      await page.locator('[data-merge-speaker="S02"]').check();
      await page.locator('#merge-speaker-target').selectOption('S01');
      assert.equal(await page.locator('#merge-speakers').isDisabled(), false);
      await page.locator('#merge-speakers').click();
      await page.waitForFunction(() => document.querySelectorAll('[data-merge-speaker]').length === 1 && !document.querySelector('#undo-speaker-merge').disabled);
      assert.equal(await page.locator('#segment-count').textContent(), '2개 구간 · 1명');
      assert.deepEqual(note.raw_segments, original);
      assert(note.corrected_segments.every(item => item.speaker === 'S01'));
      const editedText = 'Kubernetes에서 Redis 캐시를 쓰겠습니다. 문장 편집 보존.';
      await page.locator('.segment-text').first().fill(editedText);
      await page.locator('[data-speaker="S01"]').fill('A" onfocus="window.injected=1');
      await page.locator('#undo-speaker-merge').click();
      await page.waitForFunction(() => document.querySelectorAll('[data-merge-speaker]').length === 2 && document.querySelector('#undo-speaker-merge').disabled);
      assert.equal(await page.locator('.segment-text').first().textContent(), editedText);
      assert.equal(await page.locator('[data-speaker="S01"]').inputValue(), 'A" onfocus="window.injected=1');
      assert.equal(await page.locator('[data-speaker="S01"]').getAttribute('onfocus'), null);
      assert.equal(await page.evaluate(() => window.injected), undefined);
      assert.deepEqual(note.corrected_segments.map(item => item.speaker), ['S01', 'S02']);
      assert.deepEqual(note.raw_segments, original);
      for (const selector of ['#speaker-tools', '#speaker-panel', '#segments', '#merge-speakers']) {
        const box = await page.locator(selector).boundingBox();
        assert(box.x >= 0 && box.x + box.width <= width, `${selector} fits at ${width}`);
      }
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      if (process.env.SCREENSHOT_DIR) await page.screenshot({ path: `${process.env.SCREENSHOT_DIR}/speakers-${width}x${height}.png`, fullPage: true });
      await page.locator('[data-transcript-view="summary"]').click();
      await page.locator('.summary-stale').waitFor({ state: 'visible' });
      assert.deepEqual(errors, []);
      await page.close();
      console.log(`Whole-file correction, summary, speaker merge/undo, safe rendering, audio continuity and download passed: ${width}x${height}`);
    }
  } finally {
    await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
