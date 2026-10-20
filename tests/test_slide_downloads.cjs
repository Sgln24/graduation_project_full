const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.join(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'static', 'js', 'slide-downloads.js'), 'utf8')
    .replace(/^import .*;\r?$/gm, '').replace(/^export /gm, '');
const state = {currentVideoId: 'video-id', currentVideoFilename: 'Course Intro.mp4',
    videoScenes: [{}, {}], currentTranslationLanguage: 'de'};
const wordButton = element();
wordButton.disabled = false;
wordButton.innerHTML = 'Export Word';
const requests = [], saves = [], errors = [];
function element() {
    return {
        disabled: false, innerHTML: '', dataset: {}, handlers: {}, attributes: {},
        setAttribute(key, value) { this.attributes[key] = value; },
        getAttribute(key) { return this.attributes[key]; },
        removeAttribute(key) { delete this.attributes[key]; },
        addEventListener(key, handler) { this.handlers[key] = handler; },
    };
}
const context = vm.createContext({
    startElapsedTimer: () => () => {},
    state, console,
    document: {createElement: element, getElementById: () => wordButton},
    contentLanguage: (_, __, language) => language || '',
    fetch: async (url, options) => {
        requests.push({url, options});
        return {ok: true, blob: async () => 'blob'};
    },
    saveBlobToUserLocation: async (blob, filename) => saves.push({blob, filename}),
    showError: (message) => errors.push(message),
});
vm.runInContext(source, context);
const tick = () => new Promise((resolve) => setImmediate(resolve));
const event = () => ({stopped: false, prevented: false,
    stopPropagation() { this.stopped = true; }, preventDefault() { this.prevented = true; }});
(async () => {
    const slide = context.createSlideDownloadButton([1], 'Download slide 2');
    const click = event();
    slide.handlers.click(click);
    assert(click.stopped && click.prevented, 'Download clicks must not seek or expand');
    assert(slide.disabled && slide.attributes['aria-busy'] === 'true');
    state.currentVideoId = 'changed-video';
    await tick();
    assert.equal(requests[0].url, '/download_slide/video-id/1', 'Row retains its video identity');
    assert.equal(saves[0].filename, 'video-id_slide_2.jpg');
    assert(!slide.disabled && !slide.attributes['aria-busy']);
    const key = event();
    slide.handlers.keydown(key);
    assert(key.stopped, 'Keyboard events do not bubble into slide seeking');
    const doubleClick = event();
    slide.handlers.dblclick(doubleClick);
    assert(doubleClick.stopped && doubleClick.prevented, 'Double-clicking download must not open detections');

    const chapter = context.createSlideDownloadButton([0, 1], 'Download all slides in chapter (2)', {archive: true});
    chapter.handlers.click(event());
    await tick();
    assert.equal(requests[1].url, '/download_slides/changed-video');
    assert.deepEqual(JSON.parse(requests[1].options.body).indices, [0, 1]);
    assert.equal(requests[1].options.method, 'POST');
    assert.equal(saves[1].filename, 'Course_Intro_screenshots.zip');
    const section = context.createSlideDownloadButton([0], 'Download all slides in section (1)', {archive: true});
    section.handlers.click(event());
    await tick();
    assert.equal(requests[2].url, '/download_slides/changed-video', 'One-slide groups still download ZIP');
    assert.equal(saves[2].filename, 'Course_Intro_screenshots.zip', 'Do not include hashes or action labels');
    const captured = context.createSlideDownloadButton([0, 1], 'Download all screenshots', {archive: true});
    state.currentVideoFilename = 'Another video.mp4';
    captured.handlers.click(event());
    await tick();
    assert.equal(saves[3].filename, 'Course_Intro_screenshots.zip', 'Capture the filename with the row');
    assert.equal(context.screenshotArchiveFilename(), 'Another_video_screenshots.zip');
    state.currentVideoFilename = 'x'.repeat(200) + '.mp4';
    assert.equal(context.screenshotArchiveFilename().length, 80 + '_screenshots.zip'.length);
    state.currentVideoFilename = null;
    assert.equal(context.screenshotArchiveFilename(), 'lecture_screenshots.zip');

    context.setupSlideTextDownload(0);
    assert(wordButton.disabled && wordButton.handlers.click);
    context.setupSlideTextDownload(2);
    assert(!wordButton.disabled);
    await context.downloadSlideTextWord();
    assert.equal(requests[4].url, '/download_slide_text_docx/changed-video?chapter_language=de');
    assert(saves[4].filename.endsWith('_slide_text.docx'));
    assert.equal(wordButton.innerHTML, 'Export Word');
    context.fetch = async () => ({ok: false, json: async () => ({detail: 'Missing screenshot'})});
    chapter.handlers.click(event());
    await tick();
    assert(errors[0].includes('Missing screenshot'));
    assert(!chapter.disabled, 'Failures restore the download control');
    context.saveBlobToUserLocation = async () => { throw {name: 'AbortError'}; };
    context.fetch = async () => ({ok: true, blob: async () => 'blob'});
    slide.handlers.click(event());
    await tick();
    assert.equal(errors.length, 1, 'Cancelling a save is not reported as a failure');
    context.saveBlobToUserLocation = async (blob, filename) => saves.push({blob, filename});
    context.fetch = async () => ({ok: true, blob: async () => 'blob',
        headers: {get: () => 'attachment; filename="Saved_Course_screenshots.zip"'}});
    chapter.handlers.click(event());
    await tick();
    assert.equal(saves.at(-1).filename, 'Saved_Course_screenshots.zip', 'Respect the readable server filename');

    const allSource = fs.readFileSync(path.join(root, 'static', 'js', 'scenes.js'), 'utf8');
    const allStart = allSource.indexOf('export async function downloadSceneScreenshots()');
    const allEnd = allSource.indexOf('export async function downloadSlidesPdf()', allStart);
    context.elements = {downloadScreenshotsBtn: element()};
    vm.runInContext(allSource.slice(allStart, allEnd).replace('export ', ''), context);
    await context.downloadSceneScreenshots();
    assert.equal(saves.at(-1).filename, 'Saved_Course_screenshots.zip', 'All-screenshot ZIP uses the same naming');

    const chapters = fs.readFileSync(path.join(root, 'static', 'js', 'chapters.js'), 'utf8');
    const scenes = fs.readFileSync(path.join(root, 'static', 'js', 'scenes.js'), 'utf8');
    const ocr = fs.readFileSync(path.join(root, 'static', 'js', 'ocr.js'), 'utf8');
    assert(chapters.includes('inside.map(({ index }) => index)'), 'Section downloads follow nested slide ownership');
    assert(chapters.includes("sections.querySelectorAll('.chapter-slide')"), 'Chapter ZIP aggregates nested sections');
    assert(scenes.includes('createSlideDownloadButton([index], `Download slide ${index + 1}`)'));
    assert(scenes.includes('timelineItem.appendChild(createSlideDownloadButton([index]'),
        'Detected slide timeline cards have downloads');
    assert(scenes.includes('imageContainer.appendChild(createSlideDownloadButton([downloadIndex]'),
        'Enlarged detected-slide view has a download');
    assert(ocr.includes('sceneHeader.appendChild(createSlideDownloadButton([downloadIndex]'),
        'OCR slide headers have downloads');
    assert(fs.readFileSync(path.join(root, 'templates', 'index.html'), 'utf8').includes('id="downloadSlideTextWordBtn"'));
    console.log('PASS: individual/group downloads, event isolation, OCR Word export and failures');
})().catch((error) => { console.error(error); process.exitCode = 1; });
