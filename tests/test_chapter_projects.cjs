const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'chapter-projects.js'), 'utf8')
    .replace(/^import .+;$/gm, '').replace(/^export /gm, '');

function fixture() {
    const ids = {};
    const storage = new Map();
    const timers = [];
    const requests = [];
    const errors = [];
    const opened = [];
    const state = {currentVideoId: 'parent', currentTranscriptSource: 'translation:de', currentTranslationLanguage: 'de'};
    function element() {
        return {
            options: [], hidden: false, dataset: {}, listeners: {},
            appendChild(child) { this.options.push(child); },
            addEventListener(name, handler) { this.listeners[name] = handler; },
            click() { return this.listeners.click({stopPropagation() {}}); },
        };
    }
    for (const id of ['chapterProjectStatus', 'openChapterProjectBtn', 'uploadedVideos']) ids[id] = element();
    let status = 'running';
    const context = vm.createContext({
        state,
        contentLanguage: (_, __, language) => language || '',
        document: {getElementById: id => ids[id], createElement: element},
        localStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value),
            removeItem: key => storage.delete(key)},
        setTimeout: callback => { timers.push(callback); return timers.length; }, clearTimeout() {},
        loadUploadedVideo: async id => { opened.push(id); state.currentVideoId = id; },
        showError: error => errors.push(error), showNotification() {},
        fetch: async (url, options) => {
            requests.push({url, options});
            return {ok: true, json: async () => options ? {job_id: 'job'} : {
                status, elapsed: 3, steps: [{message: 'Encoding'}],
                project: {video_id: 'child', filename: 'My chapter.mp4', slides: 2},
                error: 'Encoding failed',
            }};
        },
    });
    vm.runInContext(source, context);
    return {context, state, ids, timers, storage, requests, errors, opened, status: value => { status = value; }};
}
const flush = () => new Promise(resolve => setImmediate(resolve));
const chapters = [{timestamp: '00:00', title: 'First'}, {timestamp: '01:00', title: 'Next'}];

(async () => {
    const first = fixture();
    const button = first.context.createChapterProjectButton(chapters[0], 0, chapters);
    let stopped = false;
    await button.listeners.click({stopPropagation() { stopped = true; }});
    await flush();
    assert.equal(stopped, true, 'Project action must not also seek/play the chapter');
    const payload = JSON.parse(first.requests[0].options.body);
    assert.equal(payload.timestamp, '00:00');
    assert.equal(payload.next_timestamp, '01:00');
    assert.equal(payload.language, 'de');
    assert.equal(first.requests[0].url, '/chapter_projects/parent');
    assert.match(first.ids.chapterProjectStatus.textContent, /Encoding/);
    assert.equal(first.storage.size, 1, 'Keep job id across reloads');
    await button.click();
    assert.equal(first.requests.filter(request => request.options).length, 1, 'Prevent duplicate jobs');
    first.status('complete');
    await first.timers.shift()();
    await flush();
    assert.deepEqual(first.opened, ['child']);
    assert.equal(first.storage.size, 0);
    assert.equal(first.ids.uploadedVideos.options[0].dataset.filename, 'My chapter.mp4');

    const changed = fixture();
    const changing = changed.context.createChapterProjectButton(chapters[1], 1, chapters);
    await changing.click();
    await flush();
    assert.equal(JSON.parse(changed.requests[0].options.body).next_timestamp, null);
    changed.state.currentVideoId = 'unrelated';
    changed.status('complete');
    await changed.timers.shift()();
    await flush();
    assert.deepEqual(changed.opened, [], 'Do not replace a different project the user selected');
    assert.equal(changed.ids.openChapterProjectBtn.hidden, false);
    await changed.ids.openChapterProjectBtn.onclick();
    await flush();
    assert.deepEqual(changed.opened, ['child']);

    const failed = fixture();
    await failed.context.createChapterProjectButton(chapters[0], 0, chapters).click();
    await flush();
    failed.status('error');
    await failed.timers.shift()();
    await flush();
    assert.deepEqual(failed.errors, ['Encoding failed']);
    assert.equal(failed.storage.size, 0);

    const resumed = fixture();
    resumed.storage.set('lectureseek-chapter-project-job', JSON.stringify({jobId: 'persisted', parentId: 'parent'}));
    resumed.context.initChapterProjects();
    await flush();
    assert.equal(resumed.requests[0].url, '/chapter_project_jobs/persisted');
    assert.match(resumed.ids.chapterProjectStatus.textContent, /Encoding/);
    console.log('PASS: chapter project snapshots, progress, persistence, errors, duplicate and selection guards');
})().catch(error => { console.error(error); process.exitCode = 1; });
