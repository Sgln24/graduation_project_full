const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'content-editor.js'), 'utf8')
    .replace(/^import .+;$/gm, '').replace(/^export /gm, '');
const context = vm.createContext({});
vm.runInContext(source, context);
const outline = [
    {timestamp: '00:00', title: 'First', summary: 'One', sections: [
        {timestamp: '00:00', title: 'A', point: 'Alpha'},
        {timestamp: '00:10', title: 'B', point: 'Beta'},
        {timestamp: '00:20', title: 'C', point: 'Gamma'}]},
    {timestamp: '00:30', title: 'Second', summary: 'Two', sections: [
        {timestamp: '00:30', title: 'D', point: 'Delta'},
        {timestamp: '00:40', title: 'E', point: 'Epsilon'}]},
];
const apply = op => JSON.parse(JSON.stringify(context.applyOutlineOperation(outline, op)));
const merged = apply({action: 'merge_section', chapter: 0, section: 0});
assert.equal(merged[0].sections.length, 2);
assert.equal(merged[0].sections[0].title, 'A');
assert.equal(merged[0].sections[0].timestamp, '00:00');
assert.match(merged[0].sections[0].point, /Alpha[\s\S]*B[\s\S]*Beta/);
const chapter = apply({action: 'merge_chapter', chapter: 0});
assert.equal(chapter.length, 1);
assert.equal(chapter[0].title, 'First');
assert.equal(chapter[0].sections.length, 5);
const promoted = apply({action: 'promote', chapter: 0, section: 1});
assert.equal(promoted.length, 3);
assert.equal(promoted[1].title, 'B');
assert.equal(promoted[1].timestamp, '00:10');
assert.equal(promoted[0].sections.length, 1);
const forward = apply({action: 'move_section', chapter: 0, section: 2, target: 1});
assert.equal(forward[1].timestamp, '00:20');
assert.equal(forward[1].sections[0].title, 'C');
const back = apply({action: 'move_section', chapter: 1, section: 0, target: 0});
assert.equal(back[1].timestamp, '00:40');
assert.equal(back[0].sections.at(-1).title, 'D');
assert.throws(() => apply({action: 'move_section', chapter: 0, section: 1, target: 1}), /video order/);
assert.throws(() => apply({action: 'promote', chapter: 0, section: 0}), /already starts/);
assert.equal(outline[0].sections.length, 3, 'Draft transforms must not mutate the saved source');
const retimed = JSON.parse(JSON.stringify(outline));
retimed[0].sections[1].timestamp = '00:00';
assert.throws(() => context.applyOutlineOperation(retimed, {action: 'promote', chapter: 0, section: 1}, outline),
    /Save timestamp changes/, 'Regrouping must not replay against different saved boundaries');
const renamed = JSON.parse(JSON.stringify(outline));
renamed[0].sections[1].title = 'Renamed';
assert.equal(context.applyOutlineOperation(renamed, {action: 'promote', chapter: 0, section: 1}, outline)[1].title, 'Renamed');
assert(!source.includes('showModal'), 'Content editing never opens a popup');
assert(source.includes("['chapters', 'slides', 'transcript']"));
console.log('PASS: inline outline transforms preserve timing, text, source and chronological grouping');
