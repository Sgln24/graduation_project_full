const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'slide-downloads.js'), 'utf8')
    .replace(/^import .+;$/gm, '').replace(/^export /gm, '');
const saved = [], errors = [];
const button = {addEventListener(type, fn) { this[type] = fn; }};
const video = {readyState: 4, videoWidth: 640, videoHeight: 360, currentTime: 65, paused: false,
    addEventListener() {}};
const canvas = {getContext: () => ({drawImage(player, x, y) {
    assert.equal(player, video); assert.equal(x, 0); assert.equal(y, 0);
}}), toBlob: fn => fn({type: 'image/png', size: 1234})};
const context = vm.createContext({
    state: {videoChapters: []}, sceneTopicTitle: () => 'A useful topic', showError: error => errors.push(error),
    document: {getElementById: id => id === 'videoPlayer' ? video : button, createElement: () => canvas},
    saveBlobToUserLocation: async (blob, name) => saved.push({blob, name}),
});
vm.runInContext(source, context);
(async () => {
    context.initFrameScreenshot();
    assert.equal(button.disabled, false);
    await button.click();
    assert.equal(saved[0].name, 'Screenshot_00-01-05_A_useful_topic.png');
    assert.equal(canvas.width, 640);
    assert.equal(canvas.height, 360);
    assert.equal(video.paused, false, 'Frame capture must not pause playback');
    video.readyState = 0;
    await button.click();
    assert.match(errors[0], /frame to load/);
    video.readyState = 4;
    canvas.toBlob = fn => fn(null);
    await button.click();
    assert.match(errors[1], /Could not create/);
    console.log('PASS: current-frame PNG, descriptive name, playback preserved and explicit capture errors');
})().catch(error => { console.error(error); process.exitCode = 1; });
