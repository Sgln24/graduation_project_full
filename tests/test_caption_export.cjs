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
        click() { this.clicked = true; }, remove() { this.removed = true; },
        showModal() { this.open = true; },
        removeAttribute(name) { delete this[name]; } };
}
const ids = {};
['exportCaptionedVideoBtn', 'downloadCaptionSrtBtn', 'downloadCaptionVttBtn',
 'captionVideoProgress', 'captionTranscriptLabel', 'transcriptVersionSelect', 'transcriptOptionsDialog',
 'captionStylePreset', 'captionStyleSize', 'captionStylePosition', 'captionStylePreview',
 'captionPreviewStatus', 'refreshCaptionPreviewBtn', 'captionStyleDialog',
 'openCaptionStyleBtn', 'downloadTranscriptBtn', 'transcriptDownloadDialog', 'downloadTranscriptLabel',
 'savedCaptionVideosBtn', 'savedCaptionVideosDialog', 'savedCaptionVideosList', 'refreshCaptionVideosBtn']
    .forEach(id => ids[id] = element());
ids.transcriptVersionSelect.selectedOptions = [{value: 'de-version', textContent: 'German translation'}];
ids.captionStylePreset.value = 'yellow';
ids.captionStylePreset.selectedOptions = [{textContent: 'Yellow text with a dark outline'}];
ids.captionStyleSize.value = 'large';
ids.captionStylePosition.value = 'top';
const state = { currentVideoId: 'video', currentTranscriptSource: 'translation:de',
    currentVideoFilename: 'Lecture.mp4', currentTranscript: [{start: 1, duration: 2, text: 'Gewählte Version'}] };
const requests = [], errors = [], links = [];
const context = vm.createContext({
    state, showError: message => errors.push(message), refreshSavedExports: async () => {},
    fetch: (url, options) => new Promise((resolve, reject) => requests.push({url, options, resolve, reject})),
    document: {getElementById: id => ids[id], body: element(), querySelector: () => ({currentTime: 12}),
        createElement: tag => { const item = element(); if(tag === 'a') links.push(item); return item; }},
    URL: {createObjectURL: () => 'blob:captions', revokeObjectURL() {}},
    setTimeout() {}, setInterval() {}, clearTimeout() {}, AbortController,
});
vm.runInContext(source, context);
const reply = (request, data) => request.resolve({ok: true, json: async () => data});

(async () => {
    context.initCaptionExport();
    assert.equal(typeof ids.downloadCaptionSrtBtn.listeners.click, 'function');
    assert.equal(typeof ids.downloadCaptionVttBtn.listeners.click, 'function');
    ids.downloadTranscriptBtn.listeners.click();
    assert.equal(ids.transcriptDownloadDialog.open, true);
    assert.equal(ids.downloadTranscriptLabel.textContent, 'Source: German translation');
    assert.equal(requests.length, 0, 'Choosing a format must not start a download');
    const exporting = context.exportCaptionedVideo();
    const payload = JSON.parse(requests[0].options.body);
    assert.equal(payload.transcript_label, 'German translation');
    assert.equal(payload.transcript[0].text, 'Gewählte Version');
    assert.deepEqual(payload.style, {preset:'yellow', size:'large', position:'top'});
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
    assert(ids.transcriptDownloadDialog.children.includes(links[0]), 'Download link must be inside the active modal');
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
    state.currentVideoId = 'video';
    const preview = context.refreshCaptionPreview();
    const previewPayload = JSON.parse(requests[4].options.body);
    assert.equal(previewPayload.at, 12);
    assert.equal(previewPayload.style.preset, 'yellow');
    requests[4].resolve({ok: true, blob: async () => ({})});
    await preview;
    assert.equal(ids.captionStylePreview.hidden, false);
    assert.equal(ids.captionStylePreview.src, 'blob:captions');
    ids.captionStyleDialog.listeners.close();
    assert.equal(ids.captionStylePreview.hidden, true, 'Closing the style dialog clears the preview');
    const oldPreview = context.refreshCaptionPreview();
    state.currentVideoId = 'other';
    requests[5].resolve({ok: true, blob: async () => ({})});
    await oldPreview;
    assert.equal(ids.captionStylePreview.hidden, true, 'Old preview must not appear on another video');
    state.currentTranscript = [];
    await context.downloadCaptionFile('srt');
    assert.equal(requests.length, 6);
    assert.equal(errors.length, 1);
    ids.openCaptionStyleBtn.listeners.click();
    assert.equal(ids.captionStyleDialog.open, true);
    assert.match(ids.captionPreviewStatus.textContent, /choose a transcript/);
    assert.equal(requests.length, 7);
    assert(requests[6].url.startsWith('/saved_exports/'), 'Opening captions only refreshes saved jobs, not video encoding');
    reply(requests[6], {exports: []});
    state.currentTranscript = [{start: 1, duration: 2, text: 'Chosen caption'}];
    ids.openCaptionStyleBtn.listeners.click();
    assert.equal(requests[7].url, '/caption_preview/other');
    assert.equal(JSON.parse(requests[7].options.body).transcript[0].text, 'Chosen caption');
    assert.equal(requests[8].url, '/saved_exports/other');
    assert.equal(requests.length, 9, 'Opening the caption dialog must not start encoding');
    requests[7].resolve({ok: true, blob: async () => ({})});
    reply(requests[8], {exports: []});
    ids.savedCaptionVideosBtn.listeners.click();
    assert.equal(ids.savedCaptionVideosDialog.open, true);
    assert.equal(requests[9].url, '/saved_caption_videos', 'Saved copies must include every lecture');
    reply(requests[9], {exports: [
        {title: 'Captioned video - Older transcript', filename: 'Previous-lecture-captioned.mp4',
            status: 'complete', download_url: '/export_jobs/old/download', video_id: 'previous', created_at: 1},
    ]});
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(ids.savedCaptionVideosList.children.length, 1);
    assert.match(ids.savedCaptionVideosList.children[0].textContent, /Previous-lecture-captioned.mp4/);
    assert.equal(ids.savedCaptionVideosList.children[0].children.at(-1).href, '/export_jobs/old/download');
    const empty = context.refreshSavedCaptionVideos();
    reply(requests[10], {exports: []});
    await empty;
    assert.match(ids.savedCaptionVideosList.textContent, /No captioned video copies/);
    const failed = context.refreshSavedCaptionVideos();
    requests[11].reject(new Error('Offline'));
    await failed;
    assert.match(ids.savedCaptionVideosList.textContent, /Could not load.*Offline/);
    const earlier = context.refreshSavedCaptionVideos();
    const newer = context.refreshSavedCaptionVideos();
    reply(requests[13], {exports: []});
    await newer;
    reply(requests[12], {exports: [{title: 'Captioned video - Stale', status: 'complete'}]});
    await earlier;
    assert.match(ids.savedCaptionVideosList.textContent, /No captioned video copies/, 'Stale history must not replace newer results');
    console.log('PASS: selected transcript snapshot, sidecar downloads, saved jobs, races, and input guards');
})().catch(error => { console.error(error); process.exitCode = 1; });
