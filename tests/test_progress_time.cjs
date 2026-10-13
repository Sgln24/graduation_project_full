const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'progress-time.js'), 'utf8')
    .replace(/^export /gm, '');
let now = 100000;
let nextId = 0;
const intervals = new Map();
const container = {
    children: [], isConnected: true,
    contains(child) { return this.children.includes(child); },
    appendChild(child) { this.children.push(child); child.parent = this; },
};
const context = vm.createContext({
    Date: {now: () => now},
    document: {createElement: () => ({
        remove() {
            if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this);
        },
    })},
    setInterval: callback => { intervals.set(++nextId, callback); return nextId; },
    clearInterval: id => intervals.delete(id),
});
vm.runInContext(source, context);
const tick = seconds => {
    now += seconds * 1000;
    for (const callback of [...intervals.values()]) callback();
};
for (const [value, expected] of [[0, '0:00'], [59.9, '0:59'], [60, '1:00'],
    [3599, '59:59'], [3600, '1:00:00'], [3661, '1:01:01'], [-4, '0:00'], [Infinity, '0:00']]) {
    assert.equal(context.formatElapsedTime(value), expected);
}
const stop = context.startElapsedTimer(container, 58);
assert.equal(container.children[0].textContent, '0:58 elapsed');
const label = container.children[0];
tick(3);
assert.equal(label.textContent, '1:01 elapsed', 'Uses actual time even if interval callbacks are delayed');
assert.equal(container.children[0], label, 'Tick never rebuilds progress content');
assert.equal(intervals.size, 1);
context.startElapsedTimer(container, 125);
assert.equal(container.children.length, 1, 'Re-render replaces only the timer, without duplicate clocks');
assert.equal(intervals.size, 1);
stop();
assert.equal(container.children.length, 1, 'An old operation cannot remove the new clock');
assert.equal(intervals.size, 1);
context.startElapsedTimer(container, 130, false);
tick(9);
assert.equal(container.children[0].textContent, '2:10 elapsed', 'Finished jobs retain their final time');
assert.equal(intervals.size, 0, 'Finished jobs have no active interval');
const stopDownload = context.startElapsedTimer(container);
tick(4);
stopDownload();
assert.equal(container.children.length, 0);
assert.equal(intervals.size, 0, 'Completion/error/cancel cleanup stops intervals');
context.startElapsedTimer(container);
container.children = [];
tick(1);
assert.equal(intervals.size, 0, 'Replacing a progress root releases its timer');
context.startElapsedTimer(container);
container.isConnected = false;
tick(1);
assert.equal(intervals.size, 0, 'Detached progress roots release their timers');
assert.equal(container.children.length, 0);
assert.doesNotThrow(() => context.startElapsedTimer(null)());
container.isConnected = true;
container.style = {};
context.elements = {loadingIndicator: container};
const ui = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'ui.js'), 'utf8')
    .replace(/^import .*;\r?$/gm, '').replace(/^export /gm, '');
vm.runInContext(ui, context);
context.showLoading(true);
tick(3);
const loadingLabel = container.children[0];
context.showLoading(true);
assert.equal(container.children[0], loadingLabel, 'Repeated loading updates do not restart the operation clock');
assert.equal(loadingLabel.textContent, '0:03 elapsed');
context.showLoading(false);
assert.equal(container.style.display, 'none');
assert.equal(container.children.length, 0);
assert.equal(intervals.size, 0);
console.log('PASS: elapsed boundaries, server timing, stable ticks, completion and stale-operation cleanup');
