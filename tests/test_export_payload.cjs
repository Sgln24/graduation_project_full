const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const vm = require('node:vm');

const template = fs.readFileSync(path.join(__dirname, '..', 'templates', 'index.html'), 'utf8');
assert.match(template, /id="exportWebpage" checked/);
assert.doesNotMatch(template, /id="exportWord" checked/);

const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'chapters.js'), 'utf8')
    .replace(/^import .*;\r?$/gm, '')
    .replace(/^export /gm, '');
const groupingOptions = ['topic', 'slides', 'combined'].map(value => ({ value, disabled: false }));
const grouping = {
    value: 'topic',
    options: groupingOptions,
    get selectedOptions() { return this.options.filter(option => option.value === this.value); },
};
const elements = {
    exportChaptersBtn: {},
    intervalExportToggle: { checked: false },
    intervalDuration: { value: 3 },
    chapterGrouping: grouping,
    exportImages: { checked: false },
    exportTranscripts: { checked: false },
    exportClips: { checked: false },
    exportWord: { checked: false },
    exportPdf: { checked: false },
    exportWebpage: { checked: true },
    exportScreenshotTranscript: { checked: false },
    exportOutline: { checked: false },
    exportScorm: { checked: false },
    exportAvailabilityNote: {},
};
const state = {
    currentVideoId: 'lecture',
    currentTranscript: [{ text: 'A full sentence.', start: 0 }],
    videoChapters: [{ timestamp: '00:00', title: 'Introduction' }],
    videoScenes: [],
};
let payload;
const errors = [];
let downloads = 0;
let dialogClosures = 0;
elements.exportOptionsDialog = {
    open: true,
    close() { this.open = false; dialogClosures++; },
};
let jobFails = false;
const context = vm.createContext({
    elements, state, console,
    selectedMediaExportId: () => 'caption-copy',
    startElapsedTimer: () => () => {},
    document: {getElementById: () => null},
    showError: (message) => { if (message) errors.push(message); },
    showErrorWithActions: () => {}, showNotification: () => {},
    saveBlobToUserLocation: async () => { downloads++; },
    fetch: async (url, options) => {
        if (url === '/job-status') return {
            ok: true, text: async () => JSON.stringify(jobFails
                ? {status: 'error', error: 'Clip encoding failed'}
                : {status: 'complete', download_url: '/download'}),
        };
        if (url === '/download') return {
            ok: true, blob: async () => ({}),
            headers: {get: () => 'attachment; filename="lecture.zip"'},
        };
        assert.equal(url, '/export_jobs/lecture');
        assert.equal(elements.exportOptionsDialog.open, false, 'Dialog closes before the export request');
        payload = JSON.parse(options.body);
        return {
            ok: true, text: async () => JSON.stringify({status_url: '/job-status'}),
            headers: { get: () => 'attachment; filename="lecture.zip"' },
        };
    },
});
vm.runInContext(source, context);
context.updateExportAvailability();
assert.equal(elements.intervalExportToggle.checked, false, 'Do not force fixed intervals');
assert.equal(grouping.value, 'topic');
assert.equal(groupingOptions[2].disabled, false, 'Combined remains available without both inputs');
assert.equal(elements.exportWebpage.disabled, false);

(async () => {
    await context.exportChapters();
    assert.equal(dialogClosures, 1);
    assert.equal(payload.chapter_grouping, 'topic');
    assert.equal(payload.subpart_mode, 'points');
    assert.equal(payload.timestamp_mode, 'subpart');
    assert.equal(payload.include_webpage, true);
    assert.equal(payload.media_export_id, 'caption-copy', 'Chapter clips must use the chosen video copy');
    assert.equal(payload.include_screenshot_transcript, false);
    assert.equal(elements.exportScreenshotTranscript.disabled, true, 'Screenshot export requires detected slides');
    for (const field of ['include_images', 'include_transcripts', 'include_clips', 'include_word', 'include_pdf']) {
        assert.equal(payload[field], false, `${field} must not be implicitly enabled by HTML`);
    }
    delete elements.exportWord;
    delete elements.exportWebpage;
    await context.exportChapters();
    assert.equal(payload.include_word, false);
    assert.equal(payload.include_webpage, true);
    elements.intervalExportToggle.checked = true;
    elements.intervalDuration.value = 0.5;
    grouping.value = 'slides';
    elements.timestampMode = { value: 'part' };
    elements.subpartMode = { value: 'slides' };
    await context.exportChapters();
    assert.equal(payload.interval_minutes, 0.5);
    assert.equal(payload.chapter_grouping, 'slides');
    assert.equal(payload.timestamp_mode, 'part');
    assert.equal(payload.subpart_mode, 'slides');
    state.videoScenes = [{time_seconds: 0}, {time_seconds: 10}];
    elements.exportScreenshotTranscript.checked = true;
    context.updateExportAvailability();
    assert.equal(elements.exportScreenshotTranscript.disabled, false);
    await context.exportChapters();
    assert.equal(payload.include_screenshot_transcript, true);
    assert.deepEqual(payload.screenshot_transcript, state.currentTranscript, 'Snapshot the displayed transcript');
    elements.exportWebpage = {checked: false};
    for (const control of ['exportImages', 'exportTranscripts', 'exportClips', 'exportWord',
        'exportPdf', 'exportWebpage', 'exportOutline', 'exportScorm', 'exportSectionClips']) {
        if (elements[control]) elements[control].checked = false;
    }
    state.videoChapters = [];
    context.updateExportAvailability();
    await context.exportChapters();
    assert.equal(payload.include_screenshot_transcript, true, 'Does not require AI chapters');
    assert.equal(payload.include_webpage, false);
    assert(Object.entries(payload).filter(([key]) => key.startsWith('include_'))
        .every(([key, value]) => key === 'include_screenshot_transcript' || value === false));
    assert.deepEqual(errors, []);
    assert.equal(downloads, 5, 'Each completed export must download, not silently stop after posting');
    jobFails = true;
    await context.exportChapters();
    assert.equal(errors.at(-1), 'Error exporting chapters: Clip encoding failed');
    assert.equal(state.exportInProgress, false);
    assert.equal(elements.exportChaptersBtn.disabled, false);
    assert.equal(downloads, 5, 'A failed job must not produce a download');
    console.log('PASS: content-chapter defaults, HTML-only flags, no forced intervals, explicit modes preserved');
})().catch(error => { console.error(error); process.exitCode = 1; });
