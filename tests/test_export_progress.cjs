const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'chapters.js'), 'utf8')
    .replace(/^import .*;\r?$/gm, '').replace(/^export /gm, '');
const element = () => ({
    children: [], textContent: '',
    appendChild(child) { this.children.push(child); },
    replaceChildren() { this.children = []; },
    setAttribute() {}, removeAttribute() {},
    classList: {add() {}, remove() {}},
    listeners: {},
    addEventListener(type, handler) { this.listeners[type] = handler; },
});
const progress = element();
const history = element();
const errors = [];
const saved = [];
const context = vm.createContext({
    startElapsedTimer: () => () => {},
    state: {currentVideoId: 'video'}, elements: {}, console, TypeError,
    document: {
        createElement: element,
        getElementById: (id) => id === 'exportProgress' ? progress : history,
    },
    showError: (message) => errors.push(message),
    saveBlobToUserLocation: async (blob, filename) => saved.push([blob, filename]),
    fetch: async () => ({
        ok: true, text: async () => JSON.stringify({exports: [{
            status: 'complete', title: '<unsafe title>', created_at: 100,
            open_url: '/export_jobs/test/webpage/index.html',
            download_url: '/export_jobs/test/download',
        }, {status: 'error', error: 'Previous export failed'}]}),
    }),
});
vm.runInContext(source, context);
context.renderExportSteps({
    status: 'running', elapsed: 31,
    steps: [{elapsed: 0, message: 'Starting'}, {elapsed: 10, message: 'Encoding clip'}],
});
assert.equal(progress.children[1].children.length, 1, 'Only the current export step is shown');
assert(progress.children[1].children[0].textContent.includes('Encoding clip'));
assert(progress.children[1].children[0].textContent.includes('running for 21s'));
(async () => {
    await context.refreshSavedExports();
    assert.equal(errors.length, 0);
    const list = history.children[0];
    assert.equal(list.className, 'saved-exports-list');
    const row = list.children[0];
    const [info, actions] = row.children;
    assert.equal(info.children[0].textContent, '<unsafe title>', 'Titles are set as text, not HTML');
    assert(info.children[1].textContent.length > 0, 'Saved date is shown');
    assert.equal(actions.children[0].title, 'Open HTML in browser');
    assert.equal(actions.children[0].target, '_blank');
    assert.equal(actions.children[0].rel, 'noopener');
    assert.equal(actions.children[1].title, 'Download');
    assert.equal(actions.children[1].href, '/export_jobs/test/download');
    let prevented = false;
    const listFetch = context.fetch;
    context.fetch = async (url) => ({
        ok: true, blob: async () => `blob:${url}`,
        headers: {get: () => 'attachment; filename="Lecture.zip"'},
    });
    await actions.children[1].listeners.click({preventDefault() { prevented = true; }});
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert(prevented, 'Download click does not navigate the page');
    assert.deepEqual(saved, [['blob:/export_jobs/test/download', 'Lecture.zip']]);
    context.fetch = listFetch;
    const goodFetch = context.fetch;
    context.fetch = async () => {throw new TypeError('Failed to fetch');};
    await context.refreshSavedExports();
    assert(history.children[0].textContent.includes('Could not reach the app server'));
    assert(history.children[0].textContent.includes('Refresh saved exports'));
    assert.equal(errors.length, 0, 'History connection errors belong beside the history, not unrelated global errors');
    context.fetch = goodFetch;
    await context.refreshSavedExports();
    assert.equal(history.children[0].className, 'saved-exports-list', 'Successful retry removes the connection error');
    console.log('PASS: export step timing, safe saved titles and browser/download links');
})().catch((error) => {console.error(error); process.exitCode = 1;});
