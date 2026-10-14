const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'utils.js'), 'utf8')
    .replace(/^export /gm, '');

async function checkDownload(extension, pickerAvailable, clickFails = false) {
    const blob = new Blob(['complete file bytes']);
    const links = [], urls = [], revoked = [], timers = [];
    let pickerCalls = 0;
    const container = { appendChild(link) { links.push(link); } };
    const context = vm.createContext({
        window: pickerAvailable ? {
            showSaveFilePicker() {
                pickerCalls++;
                throw new Error('The app must not create a save-picker placeholder');
            },
        } : {},
        document: {
            body: container,
            createElement(tag) {
                assert.equal(tag, 'a');
                return {
                    style: {}, clicks: 0, removed: false,
                    click() {
                        this.clicks++;
                        if (clickFails) throw new Error('Download blocked');
                    },
                    remove() { this.removed = true; },
                };
            },
        },
        URL: {
            createObjectURL(value) { urls.push(value); return 'blob:download'; },
            revokeObjectURL(value) { revoked.push(value); },
        },
        setTimeout(callback, delay) { timers.push({ callback, delay }); },
    });
    vm.runInContext(source, context);
    const filename = `lecture.${extension}`;
    if (clickFails) {
        await assert.rejects(context.saveBlobToUserLocation(blob, filename), /Download blocked/);
    } else {
        await context.saveBlobToUserLocation(blob, filename);
    }
    assert.equal(pickerCalls, 0);
    assert.equal(links.length, 1);
    assert.equal(links[0].clicks, 1);
    assert.equal(links[0].download, filename);
    assert.equal(links[0].href, 'blob:download');
    assert.equal(links[0].removed, true);
    assert.equal(urls[0], blob, 'Download uses the complete original blob');
    assert.equal(timers.length, 1);
    assert.equal(timers[0].delay, 60000, 'Keep the URL alive while the browser starts downloading');
    timers[0].callback();
    assert.deepEqual(revoked, ['blob:download']);
}

(async () => {
    for (const extension of ['zip', 'mp4', 'pdf', 'docx']) {
        await checkDownload(extension, true);
        await checkDownload(extension, false);
    }
    await checkDownload('zip', true, true);
    console.log('PASS: one complete download, no save-picker placeholder, cleanup and visible errors');
})().catch(error => { console.error(error); process.exitCode = 1; });
