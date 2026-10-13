const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'caption-export.js'), 'utf8')
    .replace(/^import .+;$/gm, '').replace(/^export /gm, '');
function element() {
    return { disabled: false, children: [], textContent: '', listeners: {}, style: {},
        append(child) { this.children.push(child); },
        appendChild(child) { this.children.push(child); },
        replaceChildren() { this.children = []; },
        addEventListener(event, handler) { this.listeners[event] = handler; },
        click() { this.clicked = true; }, remove() { this.removed = true; },
        showModal() { this.open = true; this.modal = true; },
        show() { this.open = true; this.modal = false; },
        close() { this.open = false; },
        setAttribute(name, value) { this[name] = value; },
        removeAttribute(name) { delete this[name]; } };
}
const ids = {};
['exportCaptionedVideoBtn', 'downloadCaptionSrtBtn', 'downloadCaptionVttBtn',
 'captionVideoProgress', 'captionTranscriptLabel', 'transcriptVersionSelect', 'transcriptOptionsDialog',
 'captionStylePreset', 'captionStyleSize', 'captionStylePosition', 'captionStylePreview',
 'captionPreviewStatus', 'refreshCaptionPreviewBtn', 'captionStyleDialog',
 'openCaptionStyleBtn', 'downloadTranscriptBtn', 'transcriptDownloadDialog', 'downloadTranscriptLabel',
 'savedCaptionVideoSelect', 'downloadSavedCaptionVideoBtn', 'savedCaptionVideosStatus', 'refreshCaptionVideosBtn',
 'captionLargePreviewDialog', 'captionLargePreviewImage', 'captionTranscriptVersionSelect']
    .forEach(id => ids[id] = element());
ids.transcriptVersionSelect.selectedOptions = [{value: 'de-version', textContent: 'German translation'}];
ids.captionTranscriptVersionSelect.selectedOptions = ids.transcriptVersionSelect.selectedOptions;
ids.captionStylePreset.value = 'yellow';
ids.captionStylePreset.selectedOptions = [{textContent: 'Yellow text with a dark outline'}];
ids.captionStyleSize.value = 'large';
ids.captionStylePosition.value = 'top';
const state = { currentVideoId: 'video', currentTranscriptSource: 'translation:de',
    currentVideoFilename: 'Lecture.mp4', currentTranscript: [{start: 1, duration: 2, text: 'Gewählte Version'}] };
const requests = [], errors = [], links = [], saves = [];
const context = vm.createContext({
    state, showError: message => errors.push(message), refreshSavedExports: async () => {},
    saveBlobToUserLocation: async (blob, filename, container) => saves.push({blob, filename, container}),
    openOptionsDialog: dialog => dialog.show(),
    fetch: (url, options) => new Promise((resolve, reject) => requests.push({url, options, resolve, reject})),
    document: {getElementById: id => ids[id], body: element(),
        querySelector: selector => selector === 'dialog:modal' ? (ids.captionStyleDialog.open ? ids.captionStyleDialog : null) : {currentTime: 12},
        querySelectorAll: selector => selector === 'dialog:modal'
            ? [ids.captionStyleDialog, ids.captionLargePreviewDialog].filter(dialog => dialog.open && dialog.modal)
            : [ids.captionStyleDialog, ids.transcriptDownloadDialog].filter(dialog => dialog.open),
        createElement: tag => { const item = element(); if(tag === 'a') links.push(item); return item; },
        addEventListener() {}},
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
    assert.equal(payload.caption_language, 'de');
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
    assert.equal(ids.captionVideoProgress.children.length, 0, 'Completed copies must appear only in the dropdown');
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
    assert.equal(requests.length, 8);
    assert(requests[6].url.startsWith('/saved_exports/'), 'Opening captions only refreshes saved jobs, not video encoding');
    reply(requests[6], {exports: []});
    reply(requests[7], {exports: []});
    state.currentTranscript = [{start: 1, duration: 2, text: 'Chosen caption'}];
    ids.openCaptionStyleBtn.listeners.click();
    assert.equal(requests[8].url, '/caption_preview/other');
    assert.equal(JSON.parse(requests[8].options.body).transcript[0].text, 'Chosen caption');
    assert.equal(requests[9].url, '/saved_exports/other');
    assert.equal(requests.length, 11, 'Opening the caption dialog must not start encoding');
    requests[8].resolve({ok: true, blob: async () => ({})});
    reply(requests[9], {exports: []});
    assert.equal(requests[10].url, '/saved_exports/other', 'Saved copies must belong to the current lecture');
    reply(requests[10], {exports: [
        {title: 'Captioned video - Older transcript', filename: 'Previous-lecture-captioned.mp4',
            id: 'old', status: 'complete', download_url: '/export_jobs/old/download', video_id: 'other', created_at: 1},
        {title: 'Captioned video - Another lecture', id: 'foreign', status: 'complete', video_id: 'previous'},
        {title: 'Chapter export', id: 'chapter', status: 'complete', video_id: 'other'},
        {title: 'Captioned video - Running', id: 'running', status: 'running', video_id: 'other', steps: [{message: 'Encoding 50%'}]},
    ]});
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(ids.savedCaptionVideoSelect.children.length, 2, 'Only completed copies are downloadable');
    assert.match(ids.savedCaptionVideoSelect.children[1].textContent, /Previous-lecture-captioned.mp4/);
    assert.equal(ids.savedCaptionVideoSelect.value, 'old', 'Select newest completed copy automatically');
    ids.savedCaptionVideoSelect.value = 'running';
    ids.savedCaptionVideoSelect.listeners.change();
    assert.equal(ids.downloadSavedCaptionVideoBtn.disabled, true);
    assert.equal(ids.savedCaptionVideosStatus.textContent, 'Encoding 50%');
    ids.savedCaptionVideoSelect.value = 'old';
    ids.savedCaptionVideoSelect.listeners.change();
    assert.equal(ids.downloadSavedCaptionVideoBtn.disabled, false);
    const savedDownload = ids.downloadSavedCaptionVideoBtn.listeners.click();
    assert.equal(ids.downloadSavedCaptionVideoBtn.disabled, true);
    assert.equal(requests.at(-1).url, '/export_jobs/old/download');
    const count = requests.length;
    await ids.downloadSavedCaptionVideoBtn.listeners.click();
    assert.equal(requests.length, count, 'Prevent duplicate downloads');
    requests.at(-1).resolve({ok: true, blob: async () => ({mp4: true})});
    await savedDownload;
    assert.equal(saves.at(-1).filename, 'Previous-lecture-captioned.mp4');
    assert.equal(saves.at(-1).container, context.document.body, 'Non-modal options must not block normal downloads');
    assert.equal(ids.downloadSavedCaptionVideoBtn.disabled, false);
    const preserving = context.refreshSavedCaptionVideos();
    reply(requests.at(-1), {exports: [{id: 'old', title: 'Captioned video - Saved', video_id: 'other', status: 'complete', filename: 'Old.mp4', download_url: '/export_jobs/old/download'}]});
    await preserving;
    assert.equal(ids.savedCaptionVideoSelect.value, 'old', 'Refresh must preserve selected copy');
    const failedDownload = ids.downloadSavedCaptionVideoBtn.listeners.click();
    requests.at(-1).resolve({ok: false, json: async () => ({detail: 'Export download not found'})});
    await failedDownload;
    assert.match(ids.savedCaptionVideosStatus.textContent, /Could not download.*Export download not found/);
    assert.equal(ids.downloadSavedCaptionVideoBtn.disabled, false);
    const closingDownload = ids.downloadSavedCaptionVideoBtn.listeners.click();
    ids.captionStyleDialog.close();
    requests.at(-1).resolve({ok: true, blob: async () => ({mp4: true})});
    await closingDownload;
    assert.equal(saves.at(-1).container, context.document.body, 'Closing the popup must not leave the fallback link inside a hidden dialog');
    ids.captionStyleDialog.showModal();
    const empty = context.refreshSavedCaptionVideos();
    reply(requests.at(-1), {exports: []});
    await empty;
    assert.match(ids.savedCaptionVideosStatus.textContent, /No completed captioned videos/);
    assert.equal(ids.downloadSavedCaptionVideoBtn.disabled, true);
    const failed = context.refreshSavedCaptionVideos();
    requests.at(-1).reject(new Error('Offline'));
    await failed;
    assert.match(ids.savedCaptionVideosStatus.textContent, /Could not load.*Offline/);
    const earlier = context.refreshSavedCaptionVideos();
    const earlierRequest = requests.at(-1);
    const newer = context.refreshSavedCaptionVideos();
    reply(requests.at(-1), {exports: []});
    await newer;
    reply(earlierRequest, {exports: [{title: 'Captioned video - Stale', status: 'complete'}]});
    await earlier;
    assert.match(ids.savedCaptionVideosStatus.textContent, /No completed captioned videos/, 'Stale history must not replace newer results');
    state.currentTranslationLanguage = 'fr';
    state.currentVideoId = 'video';
    const french = context.exportCaptionedVideo();
    assert.equal(JSON.parse(requests.at(-1).options.body).caption_language, 'fr');
    requests.at(-1).resolve({ok: false, json: async () => ({detail: 'Unavailable'})});
    await french;
    ids.captionTranscriptVersionSelect.value = 'loading-version';
    ids.captionTranscriptVersionSelect.disabled = true;
    const beforeSwitchGuard = requests.length;
    await context.exportCaptionedVideo();
    assert.equal(requests.length, beforeSwitchGuard, 'Do not encode the previous transcript while another is loading');
    assert.match(errors.at(-1), /finish loading/);
    ids.captionTranscriptVersionSelect.disabled = false;
    ids.captionStyleSize.value = 'large';
    const largePreview = ids.refreshCaptionPreviewBtn.listeners.click();
    assert.equal(JSON.parse(requests.at(-1).options.body).style.size, 'large');
    requests.at(-1).resolve({ok: true, blob: async () => ({})});
    await largePreview;
    assert.equal(ids.captionLargePreviewDialog.open, true);
    assert.equal(ids.captionLargePreviewImage.src, 'blob:captions');
    ids.captionStyleDialog.listeners.close();
    assert.equal(ids.captionLargePreviewDialog.open, false, 'Closing options closes the enlarged preview');
    state.currentVideoId = 'other';
    const running = {id: 'active-copy', title: 'Captioned video - Current', status: 'running',
        steps: [{message: 'Burning captions: 42% of video encoded'}]};
    const runningRefresh = context.refreshCaptionExports();
    reply(requests.at(-1), {exports: [
        {id: 'old-failed', title: 'Captioned video - Old', status: 'error', error: 'Interrupted'},
        running,
        {id: 'old-complete', title: 'Captioned video - Old', status: 'complete'},
    ]});
    await runningRefresh;
    assert.equal(ids.captionVideoProgress.children.length, 2, 'Only the active export is shown, not history');
    assert.match(ids.captionVideoProgress.children[0].textContent, /42%/);
    assert.equal(ids.captionVideoProgress.children[1].value, 42);
    assert.equal(ids.captionVideoProgress.children[1].max, 100);
    const completed = {...running, video_id: 'other', status: 'complete', filename: 'Lecture_DE caption.mp4', download_url: '/export_jobs/active-copy/download'};
    const completedRefresh = context.refreshCaptionExports();
    reply(requests.at(-1), {exports: [completed]});
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(requests.at(-1).url, '/saved_exports/other', 'Completion must update the current video dropdown');
    reply(requests.at(-1), {exports: [completed]});
    await completedRefresh;
    assert.equal(ids.captionVideoProgress.children[0].textContent, 'Your video copy is ready.');
    const completedButton = ids.captionVideoProgress.children[1];
    assert.equal(completedButton.textContent, 'Download MP4');
    completedButton.listeners.click();
    assert.equal(requests.at(-1).url, completed.download_url);
    requests.at(-1).resolve({ok: true, blob: async () => ({mp4: true})});
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(saves.at(-1).filename, completed.filename);
    assert.equal(completedButton.disabled, false);
    state.currentVideoId = 'new-video';
    const historyOnly = context.refreshCaptionExports();
    reply(requests.at(-1), {exports: [{id: 'historical', title: 'Captioned video - Old', status: 'error', error: 'Old failure'}]});
    await historyOnly;
    assert.equal(ids.captionVideoProgress.children.length, 0, 'Opening another lecture must not display historical failures');
    state.currentVideoId = 'other';
    const oldVideoRefresh = context.refreshSavedCaptionVideos();
    const oldVideoRequest = requests.at(-1);
    state.currentVideoId = 'new-video';
    const newVideoRefresh = context.refreshSavedCaptionVideos();
    assert.equal(ids.savedCaptionVideoSelect.value, '', 'Clear the previous video selection immediately');
    assert.equal(ids.downloadSavedCaptionVideoBtn.disabled, true);
    reply(requests.at(-1), {exports: []});
    await newVideoRefresh;
    reply(oldVideoRequest, {exports: [completed]});
    await oldVideoRefresh;
    assert.equal(ids.savedCaptionVideoSelect.children.length, 1, 'Late results from another video must not repopulate the dropdown');
    assert.equal(ids.downloadSavedCaptionVideoBtn.disabled, true);
    const beforeNoVideo = requests.length;
    state.currentVideoId = null;
    await context.refreshSavedCaptionVideos();
    assert.equal(requests.length, beforeNoVideo, 'Do not fetch caption history without a video');
    assert.match(ids.savedCaptionVideosStatus.textContent, /Load a video/);
    const utils = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'utils.js'), 'utf8').replace(/^export /gm, '');
    context.window = {};
    vm.runInContext(utils, context);
    const before = links.length;
    await context.saveBlobToUserLocation({mp4: true}, 'Lecture_EN caption.mp4', ids.captionStyleDialog);
    assert.equal(links.length, before + 1);
    assert.equal(links.at(-1).download, 'Lecture_EN caption.mp4');
    assert(ids.captionStyleDialog.children.includes(links.at(-1)));
    assert(links.at(-1).clicked && links.at(-1).removed);
    ids.transcriptDownloadDialog.show();
    ids.captionStyleDialog.close();
    context.openOptionsDialog(ids.captionStyleDialog);
    assert.equal(ids.captionStyleDialog.open, true);
    assert.equal(ids.captionStyleDialog.modal, false, 'Side-panel dialogs must not make resize handles inert');
    assert.equal(ids.transcriptDownloadDialog.open, false, 'Opening options closes other side-panel dialogs');
    console.log('PASS: selected transcript snapshot, sidecar downloads, saved jobs, races, and input guards');
})().catch(error => { console.error(error); process.exitCode = 1; });
