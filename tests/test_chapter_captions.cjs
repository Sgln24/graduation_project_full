const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const template = fs.readFileSync(path.join(__dirname, '..', 'templates', 'index.html'), 'utf8');
for (const id of ['timelineSectionsToggle', 'timelineSlidesToggle']) {
    const input = template.match(new RegExp(`<input\\b[^>]*\\bid="${id}"[^>]*>`));
    assert(input, `${id} exists`);
    assert(!/\bchecked\b/.test(input[0]), `${id} is off by default`);
}
const read = (name) => fs.readFileSync(path.join(__dirname, '..', 'static', 'js', name), 'utf8')
    .replace(/^import .*;\r?$/gm, '').replace(/^export /gm, '');
const caption = {textContent: ''};
const sceneLabel = {
    dataset: {number: '2'}, textContent: '',
    closest() { return {dataset: {time: '25'}}; },
};
const items = [5, 25].map((time) => ({
    image: {dataset: {time}}, caption: null,
    querySelector(selector) { return selector === '.timeline-thumbnail' ? this.image : this.caption; },
    appendChild(child) { this.caption = child; },
}));
const state = {
    chapterGrouping: 'combined',
    videoChapters: [{timestamp: '00:00', title: 'Introduction'}, {timestamp: '00:20', title: '<Learning>'}],
    videoScenes: [{time_seconds: 0}, {time_seconds: 25}],
};
const elements = {videoPlayer: {currentTime: 23}};
const context = vm.createContext({
    state, elements, console,
    contentLanguage: (_, __, language) => language || '',
    document: {
        getElementById: () => caption,
        querySelectorAll: (selector) => selector === '.scene-item .scene-label' ? [sceneLabel] : items,
        createElement: () => ({textContent: ''}),
    },
});
vm.runInContext(read('chapter-navigation.js') + '\n' + read('video.js'), context);
context.updateChapterCaptions();
assert.equal(caption.textContent, '<Learning>');
assert.equal(items[0].caption.textContent, 'Introduction');
assert.equal(items[1].caption.textContent, '<Learning>', 'Timeline titles omit the number already shown in the badge');
assert.equal(sceneLabel.textContent, '2 - <Learning>', 'Slide list reuses the timeline name safely without a Scene prefix');
state.videoChapters[1].sections = [
    {timestamp: '00:20', title: 'Specific section'},
    {timestamp: '00:30', title: 'Later section'},
];
context.updateChapterCaptions();
assert.equal(sceneLabel.textContent, '2 - Specific section');
assert.equal(items[1].caption.textContent, 'Specific section');
assert.equal(context.sceneTopicTitle(state.videoChapters, 31), 'Later section');
assert.equal(context.sceneTopicTitle(state.videoChapters, 5), 'Introduction');
assert.equal(context.sceneTopicTitle([
    ...state.videoChapters, {timestamp: '00:40', title: 'Next chapter'},
], 45), 'Next chapter', 'Section names never leak into the next chapter');
delete state.videoChapters[1].sections;
assert.equal(context.navigationParts(state.videoChapters, state.videoScenes).length, 2, 'Default navigation uses content chapters only');
assert.equal(context.navigationParts(state.videoChapters, state.videoScenes, 'combined').length, 3);
assert.equal(JSON.stringify(context.navigationParts([], state.videoScenes).map((part) => part.start)), '[0,25]', 'Without chapters, content mode falls back to slides');
state.chapterGrouping = 'topic';
context.updateChapterCaptions();
assert.equal(items[1].caption.textContent, '<Learning>');
state.chapterGrouping = 'slides';
context.updateChapterCaptions();
assert.equal(caption.textContent, '1: Introduction');
elements.videoPlayer.currentTime = 26;
context.updateChapterCaptions();
assert.equal(caption.textContent, '2: <Learning>');
state.videoChapters[1].title = 'A very long chapter title describing many different concepts and examples';
context.updateChapterCaptions();
assert(caption.textContent.length <= 36);
assert(caption.textContent.endsWith('...'));
assert(caption.title.includes('many different concepts'));
state.videoChapters = [];
state.videoScenes = [];
context.updateChapterCaptions();
assert(caption.textContent.includes('No chapter'));
assert(items[0].caption.textContent.includes('No chapter'));
assert.equal(sceneLabel.textContent, '2', 'No invented slide name when no titles exist');
console.log('PASS: screenshot/video titles, grouping boundaries, safe text and reset');
