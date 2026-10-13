const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'caption-export.js'), 'utf8')
    .replace(/^import .*;$/gm, '').replace(/^export /gm, '');
const ids = {};
for (const id of ['captionMediaStatus', 'useCaptionCopyBtn', 'restoreOriginalVideoBtn',
    'savedCaptionVideoSelect', 'downloadSavedCaptionVideoBtn', 'savedCaptionVideosStatus']) {
    ids[id] = {textContent: '', value: '', disabled: false, addEventListener() {}};
}
const listeners = new Map();
const tracks = [];
let failLoad = false;
let plays = 0;
const removedTracks = [];
const player = {
    dataset: {}, src: '/video/video', currentTime: 42, duration: 100, paused: false, playbackRate: 1.5,
    getAttribute(name) { return this[name]; },
    addEventListener(name, listener) { listeners.set(name, listener); },
    removeEventListener(name) { listeners.delete(name); },
    load() { queueMicrotask(() => listeners.get(failLoad ? 'error' : 'loadedmetadata')?.()); },
    querySelectorAll() { return tracks.slice(); },
    get textTracks() {
        const items = tracks.map(element => element.track);
        items.addEventListener = (name, listener) => listeners.set(`tracks-${name}`, listener);
        return items;
    },
    append(track) { tracks.push(track); },
    async play() { plays++; },
};
const transcript = [{text: 'Kept', start: 0}];
const scenes = [{timestamp: '00:10'}];
const state = {currentVideoId: 'video', currentTranscript: transcript, videoScenes: scenes};
const errors = [];
let requests = 0;
const job = {id: 'copy', video_id: 'video', status: 'complete',
    title: 'Captioned video - Selectable tracks', filename: 'Lecture_EN_DE caption.mp4',
    caption_tracks: [{label: 'English', language: 'eng', code: 'en'},
        {label: 'German', language: 'deu', code: 'de'}]};
const context = vm.createContext({
    state, console, setTimeout, clearTimeout, setInterval() {},
    startElapsedTimer: () => () => {},
    showError: message => errors.push(message),
    fetch: async () => { requests++; return {ok: true, json: async () => job}; },
    document: {
        getElementById: id => ids[id],
        querySelector: () => player,
        addEventListener() {},
        createElement: () => {
            const track = {dataset: {}, track: {kind: 'subtitles', mode: 'disabled'}, remove() {
                assert.equal(track.track.mode, 'disabled', 'Disable active cues before removing a track');
                assert.equal(track.default, false, 'Detached tracks must no longer be default');
                removedTracks.push(track);
                tracks.splice(tracks.indexOf(track), 1);
            }};
            return track;
        },
    },
});
vm.runInContext(source, context);
(async () => {
    assert.equal(context.selectedMediaExportId('video'), null);
    const first = context.useVideoCopy(job);
    await context.useVideoCopy(job);
    await first;
    assert.equal(requests, 1, 'Concurrent switches must not race');
    assert.equal(context.selectedMediaExportId('video'), 'copy');
    assert.equal(context.selectedMediaExportId('other'), null);
    assert.equal(player.currentTime, 42);
    assert.equal(player.playbackRate, 1.5);
    assert.equal(plays, 1);
    assert.deepEqual(tracks.map(track => track.srclang), ['en', 'de']);
    assert.equal(tracks[0].src, '/caption_copy/video/copy/tracks/0');
    assert.equal(tracks[0].default, true);
    assert.equal(tracks[1].default, false);
    assert.equal(tracks[0].track.mode, 'showing');
    assert.equal(tracks[1].track.mode, 'disabled');
    tracks[1].track.mode = 'showing';
    context.syncCaptionVisibility(player);
    assert.equal(tracks.filter(track => track.track.mode === 'showing').length, 1,
        'Remembered language plus a default track must not show two caption layers');
    tracks[0].track.mode = 'disabled';
    tracks[1].track.mode = 'showing';
    context.syncCaptionVisibility(player);
    assert.equal(tracks[1].track.mode, 'showing', 'User can switch to another caption track');
    assert.equal(state.currentTranscript, transcript);
    assert.equal(state.videoScenes, scenes);
    await context.useVideoCopy();
    assert.equal(player.src, '/video/video');
    assert.equal(context.selectedMediaExportId('video'), null);
    assert.equal(tracks.length, 0);
    assert.equal(removedTracks.length, 2);
    failLoad = true;
    await context.useVideoCopy(job);
    assert.equal(player.src, '/video/video', 'Failed media loads restore the prior source');
    assert.equal(context.selectedMediaExportId('video'), null);
    assert.match(errors.at(-1), /could not be played/);
    failLoad = false;
    await context.useVideoCopy(job);
    tracks[0].track.mode = 'disabled';
    tracks[1].track.mode = 'showing';
    await context.useVideoCopy(job);
    assert.equal(tracks.length, 2, 'Repeated switches must not accumulate captions');
    assert.equal(tracks.filter(track => track.track.mode === 'showing').length, 1);
    const previousTracks = tracks.slice();
    const previousSource = player.src;
    failLoad = true;
    await context.useVideoCopy();
    assert.equal(player.src, previousSource);
    assert.deepEqual(tracks, previousTracks, 'A failed switch restores the previous caption tracks');
    assert.equal(tracks.filter(track => track.track.mode === 'showing').length, 1);
    failLoad = false;
    player.src = '/video/video';
    assert.equal(context.selectedMediaExportId('video'), null, 'Reloading original must not export a stale copy');
    context.initCaptionExport();
    listeners.get('loadstart')();
    assert.equal(tracks.length, 0, 'Loading another original clears the previous copy CC tracks');
    assert.equal(player.dataset.captionExportId, undefined);
    console.log('PASS: media switching, preserved analysis/time, browser CC tracks, restore and load errors');
})().catch(error => { console.error(error); process.exitCode = 1; });
