const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

const root = path.join(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'templates', 'index.html'), 'utf8');
const buttons = new Map([...html.matchAll(/<button\b([^>]*)>/g)].map(([, attributes]) => [
    attributes.match(/\bid="([^"]+)"/)?.[1],
    attributes.match(/\bclass="([^"]+)"/)?.[1]?.split(/\s+/) || [],
]));
const options = ['settingsBtn', 'transcriptOptionsBtn', 'summaryOptionsBtn'];
const actions = [
    'identifySpeakersBtn', 'regenerateTranscriptAction', 'uploadTranscriptBtn',
    'translateTranscriptBtn', 'downloadCaptionSrtBtn', 'downloadCaptionVttBtn',
    'refreshCaptionPreviewBtn', 'exportChaptersBtn', 'refreshSavedExportsBtn',
    'detectScenesBtn', 'refreshSlideTitlesBtn', 'downloadScreenshotsBtn',
    'downloadSlidesPdfBtn', 'downloadSlideTextWordBtn',
    'openCaptionStyleBtn', 'downloadTranscriptBtn',
    'downloadSavedCaptionVideoBtn', 'refreshCaptionVideosBtn',
];
for (const id of options) {
    assert(buttons.get(id)?.includes('btn-options'), `${id} must remain an options control`);
    assert(!buttons.get(id).includes('btn-action'), `${id} must not look like a processing action`);
}
for (const id of actions) {
    assert(buttons.get(id)?.includes('btn-action'), `${id} must stand out as an action`);
}
assert(buttons.get('exportCaptionedVideoBtn').includes('btn-primary'));
assert(buttons.get('deleteTranscriptVersionBtn').includes('btn-danger'));
const dialog = id => html.match(new RegExp(`<dialog id="${id}"[\\s\\S]*?<\\/dialog>`))[0];
for (const [, attributes] of html.matchAll(/<dialog\b([^>]*)>/g)) {
    if (attributes.includes('id="captionLargePreviewDialog"')) {
        assert(attributes.includes('caption-large-preview-dialog'), 'Only the enlarged image preview may use a larger dialog');
    } else {
        assert(attributes.includes('options-dialog'), 'Option dialogs must share side-panel positioning');
    }
}
const layout = fs.readFileSync(path.join(root, 'static', 'compact-layout.css'), 'utf8');
assert(layout.includes('--popup-width: var(--panel-width, 420px)'), 'Desktop popups must match the panel, not widen across the video');
assert(layout.includes('--popup-top: min(var(--panel-top, 12px)'), 'Narrow-screen popups must share a viewport-clamped panel position');
for (const selector of ['html .options-dialog', 'html .modal-content', 'html .debug-content', 'html .config-dialog-content']) {
    assert(layout.includes(selector), `${selector} must use shared popup placement`);
}
const transcriptOptions = dialog('transcriptOptionsDialog');
for (const id of ['openCaptionStyleBtn', 'downloadTranscriptBtn', 'exportCaptionedVideoBtn',
    'downloadCaptionSrtBtn', 'downloadCaptionVttBtn', 'captionStylePreset']) {
    assert(!transcriptOptions.includes(`id="${id}"`), `${id} must not be inside Transcript options`);
}
assert(dialog('captionStyleDialog').includes('id="exportCaptionedVideoBtn"'));
assert(dialog('captionStyleDialog').includes('id="captionStylePreview"'));
assert(dialog('captionStyleDialog').includes('id="savedCaptionVideoSelect"'));
assert(dialog('captionStyleDialog').includes('<option value="box" selected>'));
assert(dialog('captionStyleDialog').includes('<option value="medium" selected>'));
assert(!dialog('captionStyleDialog').includes('<option value="xl">'));
assert(dialog('captionStyleDialog').includes('id="captionTranscriptVersionSelect"'));
assert(!html.includes('id="captionLanguage"'), 'Language must come from the selected transcript');
assert(dialog('captionLargePreviewDialog').includes('id="captionLargePreviewImage"'));
assert.equal([...html.matchAll(/id="savedCaptionVideoSelect"/g)].length, 1, 'Only one saved video dropdown');
assert(!html.includes('id="savedCaptionVideosDialog"'), 'No duplicate saved-video popup');
for (const id of ['downloadCaptionSrtBtn', 'downloadCaptionVttBtn']) {
    assert(dialog('transcriptDownloadDialog').includes(`id="${id}"`), 'Formats belong in the download chooser');
}
for (const id of ['previousChapterBtn', 'nextChapterBtn']) {
    assert(!buttons.get(id).includes('btn-action'), 'Playback navigation must remain neutral');
}
for (const [, attributes, text] of html.matchAll(/<button\b([^>]*)>(Cancel)<\/button>/g)) {
    assert(!attributes.includes('btn-action'), `${text} must remain neutral`);
}
const dynamicControls = [
    ['chapters.js', "download.className = 'btn btn-secondary btn-action btn-icon'"],
    ['chapters.js', "retryButton.className = 'btn btn-secondary btn-action summary-retry-btn'"],
    ['slide-downloads.js', "button.className = 'btn btn-secondary btn-action btn-icon slide-download'"],
    ['scenes.js', "reloadBtn.className = 'btn btn-secondary btn-action detection-toggle-btn'"],
    ['interactive-layer.js', 'id="extractAllBtn" class="btn btn-secondary btn-action"'],
    ['interactive-layer.js', 'id="autoExtractConfigBtn" class="btn btn-secondary btn-options"'],
    ['api-module.js', 'id="stopTranscriptionBtn" type="button" class="btn btn-danger"'],
    ['ocr.js', 'id="stopOcrBtn" class="btn btn-danger"'],
    ['ui.js', "actionButton.className = 'btn btn-secondary btn-action error-action-btn'"],
    ['caption-export.js', "button.className = 'btn btn-secondary btn-action'"],
];
for (const [file, expected] of dynamicControls) {
    const source = fs.readFileSync(path.join(root, 'static', 'js', file), 'utf8');
    assert(source.includes(expected), `${file} must classify dynamically created controls`);
}
console.log('PASS: actions, options, navigation, cancellation and dynamic button classifications');
