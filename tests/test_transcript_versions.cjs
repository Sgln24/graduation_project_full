const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'transcript-versions.js'), 'utf8')
    .replace(/^import .+;$/gm, '').replace(/^export /gm, '');
function element() {
    return {
        children: [], listeners: {}, disabled: false,
        append(child) { this.children.push(child); },
        replaceChildren(...children) { this.children = children; },
        addEventListener(event, handler) { this.listeners[event] = handler; },
        cloneNode() {
            const clone = Object.assign(element(), this);
            clone.children = this.children.map(child => child.cloneNode(true));
            return clone;
        },
    };
}
const ids = {};
for (const id of ['transcriptVersionSelect', 'captionTranscriptVersionSelect',
    'deleteTranscriptVersionBtn', 'transcriptVersionsControl', 'translationTarget']) ids[id] = element();
const state = {currentVideoId: 'video', currentTranscriptSource: 'whisper'};
const requests = [], errors = [];
const context = vm.createContext({
    state, showError: message => errors.push(message), showNotification() {},
    clearChapterMarkers() {}, updateChapters: chapters => { state.videoChapters = chapters; },
    contentLanguage: (_, __, language) => language || '',
    savedSummaryUrl: (id, language) => `/summary/${id}` + (language ? `?language=${language}` : ''),
    loadTranscript: transcript => { state.currentTranscript = transcript; },
    fetch: (url, options) => new Promise((resolve, reject) => requests.push({url, options, resolve, reject})),
    document: {getElementById: id => ids[id], createElement: element, dispatchEvent() {}, addEventListener() {}},
    CustomEvent: class {}, setTimeout() {}, clearTimeout() {},
});
vm.runInContext(source, context);
const originals = {id: 'original', kind: 'transcript', source: 'whisper', label: 'Original', active_whisper: true};
const translation = {id: 'de-version', kind: 'translation', language: 'de', label: 'German translation'};
const versions = [originals, translation];
const reply = (request, data) => request.resolve({ok: true, json: async () => data});

(async () => {
    context.initTranscriptVersions();
    assert.equal(typeof ids.captionTranscriptVersionSelect.listeners.change, 'function');
    const refreshing = context.refreshTranscriptVersions();
    reply(requests.at(-1), {versions});
    await refreshing;
    assert.equal(ids.transcriptVersionSelect.value, 'original');
    assert.equal(ids.captionTranscriptVersionSelect.value, 'original');
    assert.equal(ids.captionTranscriptVersionSelect.children.length, 2, 'Preserve transcript/translation groups');
    assert.equal(ids.captionTranscriptVersionSelect.children[1].children[0].value, 'de-version');
    assert.notEqual(ids.captionTranscriptVersionSelect.children[0], ids.transcriptVersionSelect.children[0],
        'Caption dropdown must clone options, not move them from the main toolbar');
    const switching = context.chooseTranscriptVersion('de-version');
    assert.equal(ids.captionTranscriptVersionSelect.disabled, true);
    assert.equal(ids.transcriptVersionSelect.disabled, true);
    reply(requests.at(-1), {version: translation, transcript: [{start: 0, text: 'Deutsch'}], chapters: []});
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(requests.at(-1).url, '/summary/video?language=de');
    reply(requests.at(-1), {success: true, exists: true, chapters: [{title: 'Saved fallback chapter'}]});
    await switching;
    assert.equal(state.videoChapters[0].title, 'Saved fallback chapter', 'Missing version chapters load the same saved fallback used by editors');
    assert.equal(state.currentTranslationLanguage, 'de');
    assert.equal(state.currentTranscript[0].text, 'Deutsch');
    reply(requests.at(-1), {versions});
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(ids.captionTranscriptVersionSelect.value, 'de-version');
    assert.equal(ids.transcriptVersionSelect.value, 'de-version');
    assert.equal(ids.captionTranscriptVersionSelect.disabled, false);
    const failed = context.chooseTranscriptVersion('original');
    requests.at(-1).reject(new Error('Offline'));
    await failed;
    assert.match(errors.at(-1), /Could not switch transcript: Offline/);
    reply(requests.at(-1), {versions});
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(ids.captionTranscriptVersionSelect.value, 'de-version', 'Failed switches must preserve the active version');
    assert.equal(ids.captionTranscriptVersionSelect.disabled, false);
    console.log('PASS: caption transcript dropdown shares versions, activation, language and failure handling');
})().catch(error => { console.error(error); process.exitCode = 1; });
