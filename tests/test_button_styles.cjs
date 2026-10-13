const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

const root = path.join(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'templates', 'index.html'), 'utf8');
const buttons = new Map([...html.matchAll(/<button\b([^>]*)>/g)].map(([, attributes]) => [
    attributes.match(/\bid="([^"]+)"/)?.[1],
    attributes.match(/\bclass="([^"]+)"/)?.[1]?.split(/\s+/) || [],
]));
const options = ['settingsBtn', 'transcriptOptionsBtn', 'summaryOptionsBtn', 'openCaptionStyleBtn'];
const actions = [
    'identifySpeakersBtn', 'regenerateTranscriptAction', 'uploadTranscriptBtn',
    'translateTranscriptBtn', 'downloadCaptionSrtBtn', 'downloadCaptionVttBtn',
    'refreshCaptionPreviewBtn', 'exportChaptersBtn', 'refreshSavedExportsBtn',
    'detectScenesBtn', 'refreshSlideTitlesBtn', 'downloadScreenshotsBtn',
    'downloadSlidesPdfBtn', 'downloadSlideTextWordBtn',
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
    ['caption-export.js', "link.className = 'btn btn-secondary btn-action'"],
];
for (const [file, expected] of dynamicControls) {
    const source = fs.readFileSync(path.join(root, 'static', 'js', file), 'utf8');
    assert(source.includes(expected), `${file} must classify dynamically created controls`);
}
console.log('PASS: actions, options, navigation, cancellation and dynamic button classifications');
