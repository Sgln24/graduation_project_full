const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'speaker-timeline.js'), 'utf8')
    .replace(/^import .*;\r?$/gm, '').replace(/^export /gm, '');
class Element {
    constructor() {
        this.children = [];
        this.handlers = {};
        this.attributes = {};
        this.style = {setProperty() {}};
        this.classList = {toggle() {}};
    }
    addEventListener(event, callback) { this.handlers[event] = callback; }
    setAttribute(name, value) { this.attributes[name] = value; }
    append(...children) { this.children.push(...children); }
    appendChild(child) { this.children.push(child); }
    replaceChildren() { this.children = []; }
    getBoundingClientRect() { return {left: 10, width: 200}; }
}
const ids = Object.fromEntries(['speakerTimeline', 'speakerTimelineRows', 'speakerTimelineDuration']
    .map(id => [id, new Element()]));
const state = {currentVideoId: 'video', currentTranscript: [
    {start: 0, duration: 5, speaker: 'SPEAKER_00', speaker_name: 'Alex <test>'},
    {start: 4, duration: 4, speaker: 'SPEAKER_00'},
    {start: 6, duration: 3, speaker: 'SPEAKER_01'},
    {start: 15, duration: 2, speaker: 'SPEAKER_00'},
]};
const events = {};
const player = Object.assign(new Element(), {duration: 20, currentTime: 0});
const context = vm.createContext({
    state, elements: {videoPlayer: player}, formatTime: time => `${Math.floor(time)}s`,
    document: {getElementById: id => ids[id], createElement: () => new Element(),
        addEventListener: (name, callback) => { events[name] = callback; }},
});
vm.runInContext(source, context);
const lanes = context.speakerLanes(state.currentTranscript, 20);
assert.equal(lanes.duration, 20);
assert.equal(lanes.lanes.length, 2);
assert.equal(lanes.lanes[0].name, 'Alex <test>');
assert.deepEqual(JSON.parse(JSON.stringify(lanes.lanes[0].intervals)), [{start: 0, end: 8}, {start: 15, end: 17}]);
assert.deepEqual(JSON.parse(JSON.stringify(lanes.lanes[1].intervals)), [{start: 6, end: 9}], 'Overlap belongs to both speakers');
assert.equal(context.speakerLanes([], NaN).lanes.length, 0);
assert.equal(context.speakerLanes([{start: 1, duration: Infinity, speaker: 'A'}], NaN).lanes.length, 0);
assert.equal(context.speakerLanes([{start: 1, duration: 2}], 10).lanes.length, 0);
const clipped = context.speakerLanes([{start: 8, duration: 9, speaker: 'A'}], 10);
assert.equal(clipped.lanes[0].intervals[0].end, 10);
assert.equal(context.speakerLanes([{start: 1, duration: 2, speaker: 'A'}], NaN).duration, 3);
context.initSpeakerTimeline();
assert.equal(ids.speakerTimeline.hidden, false);
const row = ids.speakerTimelineRows.children[0];
assert.equal(row.children[0].children[1].textContent, 'Alex <test>', 'Speaker names use textContent, not markup');
const track = row.children[1];
assert.equal(track.children[0].style.width, '40%');
assert.equal(track.children[1].style.left, '75%');
assert.equal(track.attributes.role, 'slider');
track.handlers.click({clientX: 110});
assert.equal(player.currentTime, 10);
let stopped = false;
track.handlers.keydown({key: 'ArrowRight', preventDefault() {}, stopPropagation() { stopped = true; }});
assert.equal(player.currentTime, 15);
assert(stopped, 'Timeline arrows do not also trigger global navigation');
track.handlers.keydown({key: 'End', preventDefault() {}, stopPropagation() {}});
assert.equal(player.currentTime, 20);
player.currentTime = 5;
player.handlers.timeupdate();
assert.equal(track.attributes['aria-valuenow'], '5');
assert.equal(track.children.at(-1).style.left, '25%');
state.currentTranscript[0].speaker_name = 'Renamed speaker';
events.transcriptUpdated();
assert.equal(ids.speakerTimelineRows.children[0].children[0].children[1].textContent, 'Renamed speaker');
state.currentVideoId = null;
events.videoSelectionChanged();
assert.equal(ids.speakerTimeline.hidden, true);
assert.equal(ids.speakerTimelineRows.children.length, 0);
console.log('PASS: speaker lanes preserve gaps/overlap, names, proportional timing, seek, keyboard, cursor and resets');
