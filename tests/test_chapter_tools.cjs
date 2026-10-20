const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const read = file => fs.readFileSync(path.join(__dirname, '..', 'static', 'js', file), 'utf8')
    .replace(/^import .*;\r?$/gm, '').replace(/^export /gm, '');
const ids = {};
for (const id of ['enhanceChaptersBtn', 'translateChaptersBtn', 'translateSlideTitlesBtn',
    'contentTranslationTarget', 'slideTranslationTarget']) {
    ids[id] = {textContent: id, value: 'transcript',
        addEventListener(event, callback) { this[event] = callback; }};
}
const storage = new Map();
const requests = [];
const errors = [];
const events = {};
const state = {
    currentVideoId: 'vid', currentTranslationLanguage: 'en', currentTranscriptSource: 'translation:en',
    currentTranscript: [{text: 'English transcript'}], videoChapters: [{title: 'German chapter'}],
    videoScenes: [{time_seconds: 0}], currentTranscriptVersionId: 'version',
};
let fail = false;
let switchVideo = false;
const context = vm.createContext({
    state, elements: {generateSummaryBtn: {}}, console,
    localStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value),
        removeItem: key => storage.delete(key)},
    document: {getElementById: id => ids[id], addEventListener(name, callback) { events[name] = callback; }},
    updateChapters: content => { state.videoChapters = content; },
    updateScenes: content => { state.videoScenes = content; }, attachChapterSlides() {},
    startElapsedTimer: () => () => {}, showError: text => errors.push(text), showNotification() {},
    fetch: async (url, options) => {
        const body = JSON.parse(options.body);
        requests.push(body);
        if (switchVideo) state.currentVideoId = 'other';
        return {ok: !fail, json: async () => fail ? {detail: 'Provider unavailable'} : {
            language: body.operation === 'enhance' ? body.language : body.target_language === 'transcript' ? 'en' : body.target_language,
            content: [{title: 'Translated', time_seconds: 0}],
        }};
    },
});
vm.runInContext(read('content-languages.js') + '\n' + read('chapter-tools.js'), context);
context.initChapterTools();
(async () => {
    assert.equal(ids.enhanceChaptersBtn.disabled, false);
    state.summaryGenerationInProgress = true;
    events.summaryGenerationChanged();
    assert.equal(ids.enhanceChaptersBtn.disabled, true);
    state.summaryGenerationInProgress = false;
    events.summaryGenerationChanged();
    assert.equal(ids.enhanceChaptersBtn.disabled, false, 'Actions re-enable after generation finishes');
    ids.contentTranslationTarget.value = 'de';
    await ids.translateChaptersBtn.click();
    assert.equal(requests[0].transcript_language, 'en');
    assert.equal(requests[0].language, 'en');
    assert.equal(requests[0].target_language, 'de');
    assert.equal(requests[0].version_id, 'version');
    assert.equal(state.videoChapters[0].title, 'Translated');
    assert.equal(context.savedSummaryUrl('vid', 'en'), '/summary/vid?language=de');
    assert.equal(context.contentLanguage('vid', 'slides', 'en'), 'en', 'Slide language is independent');
    await ids.enhanceChaptersBtn.click();
    assert.equal(requests[1].language, 'de');
    assert.equal(requests[1].operation, 'enhance');
    await ids.translateSlideTitlesBtn.click();
    assert.equal(requests[2].target_language, 'en', 'Current transcript is a valid translation target');
    assert.equal(requests[2].chapter_language, 'de');
    const prior = JSON.stringify(state.videoChapters);
    fail = true;
    await ids.translateChaptersBtn.click();
    assert.equal(JSON.stringify(state.videoChapters), prior);
    assert.match(errors[0], /Provider unavailable/);
    fail = false;
    switchVideo = true;
    await ids.translateChaptersBtn.click();
    assert.equal(context.contentLanguage('other', 'chapters', 'en'), 'en', 'A late response cannot set another video language');
    assert.equal(state.summaryGenerationInProgress, false);
    const template = fs.readFileSync(path.join(__dirname, '..', 'templates', 'index.html'), 'utf8');
    assert.match(template, /id="chapterPasses"[\s\S]*?value="one" selected[\s\S]*?value="two"/);
    assert.match(read('chapters.js'), /const passes = document.getElementById\('chapterPasses'\)/);
    console.log('PASS: independent saved title languages, transcript target, enhancement, errors and selection races');
})().catch(error => { console.error(error); process.exitCode = 1; });
