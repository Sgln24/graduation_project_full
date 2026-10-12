const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'caption-export.js'), 'utf8')
    .replace(/^import .+;$/gm, '').replace(/^export /gm, '');
function element() {
    return { disabled: false, children: [], textContent: '', listeners: {},
        append(child) { this.children.push(child); },
        replaceChildren() { this.children = []; },
        addEventListener(event, handler) { this.listeners[event] = handler; },
        click() { this.clicked = true; }, remove() { this.removed = true; } };
}
const ids = {};
['exportCaptionedVideoBtn', 'downloadCaptionSrtBtn', 'downloadCaptionVttBtn',
 'captionVideoProgress', 'captionTranscriptLabel', 'transcriptVersionSelect', 'transcriptOptionsDialog']
    .forEach(id => ids[id] = element());
ids.transcriptVersionSelect.selectedOptions = [{value: 'de-version', textContent: 'German translation'}];
const state = { currentVideoId: 'video', currentTranscriptSource: 'translation:de',
    currentVideoFilename: 'Lecture.mp4', currentTranscript: [{start: 1, duration: 2, text: 'Gewählte Version'}] };
const requests = [], errors = [], links = [];
const context = vm.createContext({
    state, showError: message => errors.push(message), refreshSavedExports: async () => {},
    fetch: (url, options) => new Promise((resolve, reject) => requests.push({url, options, resolve, reject})),
    document: {getElementById: id => ids[id], body: element(),
        createElement: tag => { const item = element(); if(tag === 'a') links.push(item); return item; }},
    URL: {createObjectURL: () => 'blob:captions', revokeObjectURL() {}},
    setTimeout() {}, setInterval() {},
});
vm.runInContext(source, context);
const reply = (request, data) => request.resolve({ok: true, json: async () => data});

(async () => {
    context.initCaptionExport();
    assert.equal(typeof ids.downloadCaptionSrtBtn.listeners.click, 'function');
    assert.equal(typeof ids.downloadCaptionVttBtn.listeners.click, 'function');
    const exporting = context.exportCaptionedVideo();
    const payload = JSON.parse(requests[0].options.body);
    assert.equal(payload.transcript_label, 'German translation');
    assert.equal(payload.transcript[0].text, 'Gewählte Version');
    state.currentTranscript[0].text = 'Changed later';
    state.currentVideoId = 'other';
    await context.exportCaptionedVideo();
    assert.equal(requests.length, 1, 'Avoid double submission');
    reply(requests[0], {job_id: 'job'});
    await exporting;
    assert.equal(requests.length, 1, 'Do not refresh previous video exports after switching');
    assert.equal(ids.exportCaptionedVideoBtn.disabled, false);
    state.currentVideoId = 'video';
    const download = context.downloadCaptionFile('vtt');
    assert.equal(requests[1].url, '/caption_file/video/vtt');
    requests[1].resolve({ok: true, blob: async () => ({})});
    await download;
    assert.equal(links[0].download, 'Lecture.vtt');
    assert(links[0].clicked && links[0].removed);
    assert.equal(ids.downloadCaptionVttBtn.disabled, false);
    const refresh = context.refreshCaptionExports();
    reply(requests[2], {exports: [
        {title: 'Captioned video - German', status: 'complete', download_url: '/export_jobs/job/download', steps: []},
        {title: 'Chapter export', status: 'complete'},
    ]});
    await refresh;
    assert.equal(ids.captionVideoProgress.children.length, 1);
    assert.equal(ids.captionVideoProgress.children[0].children[0].href, '/export_jobs/job/download');
    const stale = context.refreshCaptionExports();
    state.currentVideoId = 'other';
    reply(requests[3], {exports: [{title: 'Captioned video - Wrong video', status: 'complete', steps: []}]});
    await stale;
    assert.equal(ids.captionVideoProgress.children.length, 0);
    state.currentTranscript = [];
    await context.downloadCaptionFile('srt');
    assert.equal(requests.length, 4);
    assert.equal(errors.length, 1);
    console.log('PASS: selected transcript snapshot, sidecar downloads, saved jobs, races, and input guards');
})().catch(error => { console.error(error); process.exitCode = 1; });
