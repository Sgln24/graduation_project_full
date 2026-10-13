const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'scenes.js'), 'utf8');
const start = source.indexOf('export async function downloadSlidesPdf()');
const end = source.indexOf('/**', start);
const button = {};
const errors = [];
let downloaded;
let fail = false;
const context = vm.createContext({
    startElapsedTimer: () => () => {},
    state: {currentVideoId: 'lecture', videoScenes: [{}]},
    document: {getElementById: () => button},
    showError: (message) => errors.push(message),
    saveBlobToUserLocation: async (blob, name) => {downloaded = name;},
    fetch: async (url) => {
        assert.equal(url, '/download_slides_pdf/lecture');
        return fail
            ? {ok: false, text: async () => '{"detail":"Missing screenshots"}'}
            : {ok: true, blob: async () => ({})};
    },
});
vm.runInContext(source.slice(start, end).replace('export ', ''), context);
(async () => {
    await context.downloadSlidesPdf();
    assert.equal(downloaded, 'lecture_slides.pdf');
    assert.equal(button.disabled, false);
    fail = true;
    await context.downloadSlidesPdf();
    assert(errors[0].includes('Missing screenshots'));
    assert.equal(button.disabled, false);
    console.log('PASS: slides PDF download, filename and visible missing-image error');
})().catch((error) => {console.error(error); process.exitCode = 1;});
