const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
    constructor(tag = '') {
        this.tag = tag;
        this.children = [];
        this.listeners = {};
        this.value = '';
        this.textContent = '';
    }
    addEventListener(name, handler) {
        (this.listeners[name] ||= []).push(handler);
    }
    async emit(name, event = {}) {
        for (const handler of this.listeners[name] || []) await handler(event);
    }
    click() { return this.emit('click'); }
    appendChild(child) { this.children.push(child); }
    replaceChildren() { this.children = []; }
    focus() {}
    close() { this.open = false; }
    showModal() { this.open = true; }
    querySelector() { return null; }
    inputs() {
        return this.children.flatMap(child => ['input', 'textarea'].includes(child.tag) ? [child] : child.inputs());
    }
}

const ids = {};
for (const id of ['contentEditorDialog', 'contentEditorForm', 'contentEditorRows', 'contentEditorError',
    'saveContentEditsBtn', 'cancelContentEditsBtn', 'discardContentEditsBtn', 'contentEditorHeading',
    'contentEditorHelp', 'transcriptVersionSelect', 'edit-transcript-btn', 'edit-chapters-btn', 'edit-slides-btn']) {
    ids[id] = new Element();
}
const state = {
    currentVideoId: 'video', currentTranscriptSource: 'whisper',
    currentTranscript: [{start: 0, duration: 4, text: 'Original', speaker: 'A'}],
    videoChapters: [{timestamp: '00:00', title: 'Chapter', sections: []}],
    videoScenes: [{time_seconds: 0, index: 0}],
};
let fail = false;
const requests = [];
const events = {};
const context = vm.createContext({
    state, elements: {}, URLSearchParams,
    document: {
        getElementById: id => ids[id],
        createElement: tag => new Element(tag),
        addEventListener: (name, handler) => { events[name] = handler; },
        querySelectorAll: () => [],
    },
    openOptionsDialog: dialog => { dialog.open = true; },
    loadTranscript: content => { state.currentTranscript = content; },
    updateChapters: content => { state.videoChapters = content; },
    updateScenes: content => { state.videoScenes = content; },
    attachChapterSlides() {}, showNotification() {}, refreshTranscriptVersions: async () => {},
    contentLanguage: (_, __, language) => language || '',
    slideTitle: scene => scene.title || '',
    sceneTopicTitle: () => '',
    fetch: async (url, options) => {
        requests.push({url, options});
        const kind = options ? JSON.parse(options.body).kind : new URLSearchParams(url.split('?')[1]).get('kind');
        const original = kind === 'transcript' ? state.currentTranscript
            : kind === 'slides' ? state.videoScenes : state.videoChapters;
        const edits = options ? JSON.parse(options.body).edits : null;
        return {
            ok: !options || !fail,
            json: async () => fail && options ? {detail: 'Save conflict'} : {
                revision: 'revision',
                content: edits ? original.map((item, i) => kind === 'transcript'
                    ? {...item, text: edits[i]} : kind === 'slides' ? {...item, title: edits[i]} : edits[i])
                    : kind === 'slides' ? original.map(({index, ...scene}) => scene) : original,
                version: {id: 'edited', kind: 'transcript', source: 'whisper'},
            },
        };
    },
});
const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'content-editor.js'), 'utf8')
    .replace(/^import .+;$/gm, '').replace(/^export /gm, '');
vm.runInContext(source, context);
context.initContentEditor();
const submit = () => ids.contentEditorForm.emit('submit', {preventDefault() {}});

(async () => {
    assert.equal(ids['edit-transcript-btn'].disabled, false);
    await ids['edit-transcript-btn'].click();
    const text = ids.contentEditorRows.inputs()[0];
    assert.equal(text.value, 'Original');
    text.value = 'Corrected';
    await ids.contentEditorForm.emit('input');
    await ids.cancelContentEditsBtn.click();
    assert.equal(ids.contentEditorDialog.open, true);
    assert.equal(ids.discardContentEditsBtn.hidden, false);
    let prevented = false;
    await ids.contentEditorDialog.emit('cancel', {preventDefault() { prevented = true; }});
    assert.equal(prevented, true);
    fail = true;
    await submit();
    assert.equal(ids.contentEditorDialog.open, true);
    assert.equal(ids.contentEditorError.textContent, 'Save conflict');
    assert.equal(state.currentTranscript[0].text, 'Original');
    fail = false;
    await submit();
    assert.equal(ids.contentEditorDialog.open, false);
    assert.equal(state.currentTranscript[0].text, 'Corrected');
    assert.equal(state.currentTranscript[0].speaker, 'A');
    assert.equal(state.currentTranscript[0].duration, 4);
    assert.equal(state.currentTranscriptVersionId, 'edited');
    await ids['edit-transcript-btn'].click();
    state.currentVideoId = 'different';
    const count = requests.length;
    await submit();
    assert.equal(requests.length, count, 'Do not save into a changed selection');
    assert.match(ids.contentEditorError.textContent, /changed/);
    await ids.cancelContentEditsBtn.click();
    assert.equal(ids.contentEditorDialog.open, false);
    state.currentVideoId = 'video';
    await ids['edit-chapters-btn'].click();
    ids.contentEditorRows.inputs()[0].value = 'Edited chapter';
    await submit();
    assert.equal(state.videoChapters[0].title, 'Edited chapter');
    await ids['edit-slides-btn'].click();
    ids.contentEditorRows.inputs()[0].value = 'Edited slide';
    await submit();
    assert.equal(state.videoScenes[0].title, 'Edited slide');
    state.currentTranscript = [];
    events.transcriptUpdated();
    assert.equal(ids['edit-transcript-btn'].disabled, true);
    console.log('PASS: content editors save, cancel, discard warning, conflicts, context guards and metadata');
})().catch(error => { console.error(error); process.exitCode = 1; });
