const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'vtt-batch.js'), 'utf8')
    .replace(/^import .*;$/gm, '').replace(/^export /gm, '');
function element() {
    return {children: [], value: '', disabled: false, files: [], textContent: '', listeners: {},
        append(child) { this.children.push(child); },
        replaceChildren(...children) { this.children = children; },
        addEventListener(type, listener) { this.listeners[type] = listener; },
        get options() { return this.children; },
        cloneNode() { return {...this}; }};
}
const ids = {};
for (const name of ['openVttBatchBtn', 'vttBatchDialog', 'vttBatchFiles', 'vttBatchTarget',
    'vttBatchModel', 'translationModel', 'translateVttBatchBtn', 'vttBatchProgress', 'vttBatchSaved',
    'downloadVttBatchBtn', 'vttBatchHistoryStatus', 'vttBatchDetails', 'vttBatchFallback']) ids[name] = element();
ids.vttBatchFallback.checked = true;
ids.translationModel.children = [{value: '', textContent: 'Auto', cloneNode() {return {...this};}}];
ids.vttBatchTarget.value = 'de';
const requests = [], errors = [], timers = [], downloads = [], storage = new Map();
let jobFails = false, historyFails = false, downloadingFails = false;
let holdStatus = false, releaseStatus;
const lockedControls = ['translateVttBatchBtn', 'vttBatchFiles', 'vttBatchTarget',
    'vttBatchModel', 'vttBatchFallback'];
let modelsRefreshed = 0;
const job = {id: 'batch', status: 'complete', elapsed: 10, title: 'VTT translation - 2 files - DE',
    filename: 'Translated_VTT_DE.zip', created_at: 100, download_url: '/download',
    steps: [{message: 'Translation result: Models tried: Groq. 123 tokens.'},
        {message: 'Translation finished'}]};
const context = vm.createContext({
    console, FormData, Blob, setTimeout,
    localStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value),
        removeItem: key => storage.delete(key)},
    document: {getElementById: id => ids[id], createElement: element},
    showError: message => errors.push(message),
    openOptionsDialog: dialog => { dialog.open = true; },
    startElapsedTimer: (container, elapsed, running = true) => {
        const timer = {container, elapsed, running, stopped: false};
        timers.push(timer);
        return () => {timer.stopped = true;};
    },
    saveBlobToUserLocation: async (blob, filename, container) => downloads.push({blob, filename, container}),
    fetch: async (url, options) => {
        requests.push({url, options});
        if (url === '/vtt_translation_jobs') return {ok: true, json: async () => ({job_id: 'batch'})};
        if (url === '/export_jobs/batch/status') return {ok: true, json: async () => {
            for (const id of lockedControls) assert.equal(ids[id].disabled, true, `${id} locked during a job`);
            if (holdStatus) await new Promise(resolve => { releaseStatus = resolve; });
            return jobFails ? {status: 'error', error: 'Provider unavailable', elapsed: 5} : job;
        }};
        if (url === '/saved_exports/vtt-batch') {
            if (historyFails) throw new Error('Connection lost');
            return {ok: true, json: async () => ({exports: [job]})};
        }
        if (url === '/download') {
            if (downloadingFails === 'network') throw new TypeError('Failed to fetch');
            return {ok: !downloadingFails, status: 500, blob: async () => new Blob(['zip'])};
        }
        throw new Error(`Unexpected URL: ${url}`);
    },
});
vm.runInContext(source, context);
(async () => {
    context.initVttBatch(async () => { modelsRefreshed += 1; });
    await ids.openVttBatchBtn.listeners.click();
    assert.equal(ids.vttBatchDialog.open, true);
    assert.equal(modelsRefreshed, 1, 'Standalone opening refreshes configured models without a video');
    assert.equal(ids.vttBatchModel.options[0].textContent, 'Auto');
    assert.equal(ids.downloadVttBatchBtn.disabled, false);
    const originalOptions = ids.vttBatchSaved.children.slice();
    await context.refreshVttBatches();
    assert.deepEqual(ids.vttBatchSaved.children, originalOptions, 'Unchanged history retains option nodes');
    requests.length = 0;
    await context.translateVttFiles();
    assert.equal(requests.length, 0);
    assert.match(errors.at(-1), /Choose between/);
    ids.vttBatchFiles.files = [Object.assign(new Blob(['WEBVTT']), {name: 'one.txt'})];
    await context.translateVttFiles();
    assert.match(errors.at(-1), /Only .vtt/);
    ids.vttBatchFiles.files = [Object.assign(new Blob(['WEBVTT']), {name: 'one.vtt'}),
        Object.assign(new Blob(['WEBVTT']), {name: 'two.vtt'})];
    await context.translateVttFiles();
    const upload = requests.find(item => item.url === '/vtt_translation_jobs');
    assert.equal(upload.options.method, 'POST');
    assert.equal(upload.options.body.getAll('files').length, 2);
    assert.equal(upload.options.body.get('target_language'), 'de');
    assert.equal(upload.options.body.get('allow_fallback'), 'true');
    assert(!requests.some(item => item.url.includes('/video')), 'Batch translation needs no video');
    assert.equal(ids.translateVttBatchBtn.disabled, false);
    assert.equal(storage.has('activeVttBatchJob'), false);
    assert.equal(ids.vttBatchSaved.value, 'batch');
    assert.match(ids.vttBatchProgress.children[0].textContent, /Translation ready/);
    assert.match(ids.vttBatchDetails.textContent, /123 tokens/);
    assert.equal(timers.filter(timer => timer.running && !timer.stopped).length, 0);
    await ids.downloadVttBatchBtn.listeners.click();
    await new Promise(resolve => setTimeout(resolve, 0));
    assert.equal(downloads[0].filename, 'Translated_VTT_DE.zip');
    assert.equal(downloads[0].container, ids.vttBatchDialog);
    ids.vttBatchFallback.checked = false;
    ids.vttBatchModel.value = '';
    const requestCount = requests.length;
    await context.translateVttFiles();
    assert.equal(requests.length, requestCount);
    assert.match(errors.at(-1), /Choose a model/);
    ids.vttBatchModel.value = 'groq:openai/gpt-oss-20b';
    ids.vttBatchFallback.checked = true;
    ids.vttBatchModel.listeners.change();
    assert.equal(ids.vttBatchFallback.checked, false, 'Choosing Groq defaults to strict mode');
    await context.translateVttFiles();
    assert.equal(requests.filter(item => item.url === '/vtt_translation_jobs').at(-1).options.body.get('allow_fallback'), 'false');
    for (const id of lockedControls) assert.equal(ids[id].disabled, false, `${id} unlocked after completion`);
    holdStatus = true;
    const pendingTranslation = context.translateVttFiles();
    await new Promise(resolve => setTimeout(resolve, 0));
    const selectedOptions = ids.vttBatchModel.children;
    await ids.openVttBatchBtn.listeners.click();
    assert.equal(ids.vttBatchModel.children, selectedOptions, 'Reopening leaves active model selection intact');
    for (const id of lockedControls) assert.equal(ids[id].disabled, true);
    await context.translateVttFiles();
    releaseStatus();
    holdStatus = false;
    await pendingTranslation;
    for (const id of lockedControls) assert.equal(ids[id].disabled, false);
    ids.vttBatchModel.value = '';
    ids.vttBatchModel.listeners.change();
    assert.equal(ids.vttBatchFallback.checked, true, 'Auto re-enables fallback');
    ids.vttBatchModel.value = 'groq:openai/gpt-oss-20b';
    ids.vttBatchFallback.checked = true;
    jobFails = true;
    await context.translateVttFiles();
    assert.match(errors.at(-1), /Provider unavailable/);
    assert.equal(ids.translateVttBatchBtn.disabled, false);
    for (const id of lockedControls) assert.equal(ids[id].disabled, false, `${id} unlocked after failure`);
    assert.equal(storage.has('activeVttBatchJob'), false);
    assert.equal(timers.filter(timer => timer.running && !timer.stopped).length, 0, 'Error stops live clocks');
    jobFails = false;
    storage.set('activeVttBatchJob', 'batch');
    await ids.openVttBatchBtn.listeners.click();
    assert.equal(storage.has('activeVttBatchJob'), false, 'Reopening resumes the saved batch');
    downloadingFails = true;
    await ids.downloadVttBatchBtn.listeners.click();
    await new Promise(resolve => setTimeout(resolve, 0));
    assert.match(errors.at(-1), /Could not download ZIP/);
    downloadingFails = 'network';
    await ids.downloadVttBatchBtn.listeners.click();
    await new Promise(resolve => setTimeout(resolve, 0));
    assert.match(errors.at(-1), /Cannot reach the server.*saved batch is kept/);
    historyFails = true;
    await context.refreshVttBatches();
    assert.match(ids.vttBatchHistoryStatus.textContent, /Connection lost/);
    console.log('PASS: video-free VTT batch upload, stable history, clocks, ZIP downloads, failures and resume');
})().catch(error => {console.error(error); process.exitCode = 1;});
