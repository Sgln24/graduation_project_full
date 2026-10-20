const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const read = (name) => fs.readFileSync(path.join(__dirname, '..', 'static', 'js', name), 'utf8')
    .replace(/^import .*;\r?$/gm, '').replace(/^export /gm, '');
const scrolls = [];
const container = {
    classList: {contains() { return false; }},
    scrollTop: 100, scrollLeft: 20, clientHeight: 300, clientWidth: 200,
    getBoundingClientRect: () => ({top: 200, bottom: 500, left: 0, right: 200}),
    scrollTo: (options) => scrolls.push(options),
};
const line = {
    classList: {add() {}, remove() {}},
    getBoundingClientRect: () => ({top: 600, bottom: 640, height: 40}),
    scrollIntoView: () => {throw new Error('Must not scroll the page');},
};
const context = vm.createContext({
    elements: {videoPlayer: {currentTime: 5}, transcriptContainer: container},
    state: {currentTranscript: [{start: 0, duration: 10, text: 'Lecture'}]},
    document: {querySelectorAll: () => [line]},
});
vm.runInContext(read('scroll-utils.js') + '\n' + read('transcript.js'), context);
context.updateActiveTranscript();
assert.equal(scrolls[0].top, 370);
assert.equal(scrolls[0].left, undefined);
line.getBoundingClientRect = () => ({top: 250, bottom: 290, height: 40});
context.updateActiveTranscript();
assert.equal(scrolls.length, 1, 'A visible line must not trigger scrolling');
context.scrollWithinContainer(container, {
    getBoundingClientRect: () => ({left: 300, right: 420, width: 120}),
}, 'horizontal');
assert.equal(scrolls[1].left, 280);
assert.equal(scrolls[1].top, undefined, 'Timeline playback must not move vertically');
console.log('PASS: playback scrolls transcript/timeline containers only, visible lines stay still');
