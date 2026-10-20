const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'library.js'), 'utf8')
    .replace(/^import .+;$/gm, '').replace(/^export /gm, '');

function element() {
    return {
        children: [], listeners: {}, dataset: {},
        append(...children) { this.children.push(...children); },
        appendChild(child) { this.children.push(child); },
        replaceChildren() { this.children = []; },
        setAttribute() {},
        addEventListener(name, callback) { this.listeners[name] = callback; },
        showModal() {},
    };
}

(async () => {
    const ids = Object.fromEntries(['manageLibraryBtn', 'libraryDialog', 'libraryItems', 'libraryStatus',
        'deleteUploadExports'].map(id => [id, element()]));
    const track = {track: {mode: 'showing'}, default: true};
    const player = {
        currentTime: 42, src: '/caption_copy/parent/saved',
        dataset: {captionExportId: 'saved', captionVideoId: 'parent'},
        tracks: [track], listeners: {},
        pause() {},
        getAttribute() { return this.src; },
        removeAttribute() { this.src = ''; },
        querySelectorAll() { return this.tracks; },
        appendChild(item) { this.tracks.push(item); },
        addEventListener(name, callback) { this.listeners[name] = callback; },
        load() {
            this.tracks.forEach(item => { item.track.mode = 'disabled'; item.default = false; });
            this.tracks = [];
            this.dataset = {};
            this.currentTime = 0;
            if (this.src && this.listeners.loadedmetadata) {
                const loaded = this.listeners.loadedmetadata;
                delete this.listeners.loadedmetadata;
                loaded();
            }
        },
    };
    let allowDeletion = false;
    let reloads = 0;
    let removed = false;
    const state = {currentVideoId: 'parent'};
    const context = vm.createContext({
        state, window: {confirm: () => true}, showError() {},
        location: {reload: () => { reloads++; }},
        document: {
            getElementById: id => ids[id],
            createElement: element,
            querySelector: selector => selector === 'video' ? player : {remove() { removed = true; }},
        },
        fetch: async (_, options) => options
            ? {ok: allowDeletion, json: async () => ({detail: 'Upload has active work.'})}
            : {ok: true, json: async () => ({
                videos: [{video_id: 'parent', filename: 'Parent.mp4'}], exports: [],
            })},
    });
    vm.runInContext(source, context);
    context.initLibrary();
    await ids.manageLibraryBtn.listeners.click();
    const row = ids.libraryItems.children.find(child => child.className === 'library-row');
    const remove = row.children[1];
    await remove.listeners.click();
    assert.equal(ids.libraryStatus.textContent, 'Upload has active work.');
    assert.equal(player.src, '/caption_copy/parent/saved');
    assert.equal(player.currentTime, 42);
    assert.equal(player.dataset.captionExportId, 'saved');
    assert.equal(player.dataset.captionVideoId, 'parent');
    assert.equal(player.tracks[0], track);
    assert.equal(track.track.mode, 'showing');
    assert.equal(track.default, true);
    assert.equal(reloads, 0);
    assert.equal(removed, false);
    allowDeletion = true;
    await remove.listeners.click();
    assert.equal(removed, true);
    assert.equal(reloads, 1);
    console.log('PASS: failed deletion restores media, captions and time; successful deletion clears the upload');
})().catch(error => { console.error(error); process.exitCode = 1; });
