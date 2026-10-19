const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'main.js'), 'utf8').replace(/\r\n/g, '\n');
const start = source.indexOf("    [\n        ['summaryOptionsBtn'");
const end = source.indexOf('\n    // Wire the core video', start);
const attributes = {};
const button = {
    firstChild: {textContent: 'Chapter options'},
    appendChild(child) { this.child = child; },
    contains(child) { return this.child === child; },
    setAttribute(key, value) { attributes[key] = value; },
    classList: {toggle(name, value) { attributes[name] = value; }},
};
const panel = {hidden: true, tagName: 'DIV', addEventListener() {}};
let update;
vm.runInNewContext(source.slice(start, end), {
    document: {
        getElementById(id) { return id === 'summaryOptionsBtn' ? button : id === 'summaryOptionsPanel' ? panel : null; },
        createElement() { return {setAttribute() {}}; },
    },
    MutationObserver: class { constructor(callback) { update = callback; } observe() {} },
});
assert.equal(attributes['aria-expanded'], 'false');
panel.hidden = false;
update();
assert.equal(attributes['aria-expanded'], 'true');
assert.equal(attributes['options-open'], true);
assert.equal(button.firstChild.textContent, 'Hide chapter options');
panel.hidden = true;
update();
assert.equal(attributes['options-open'], false);
assert.equal(button.firstChild.textContent, 'Chapter options');
const theme = fs.readFileSync(path.join(__dirname, '..', 'static', 'theme.css'), 'utf8');
for (const selectors of [
    '.btn.btn-action,\\s*\\.btn\\.btn-options',
    '.btn.btn-action:hover:not\\(:disabled\\),\\s*\\.btn\\.btn-options:hover:not\\(:disabled\\)',
]) {
    assert.match(theme, new RegExp(selectors + '\\s*\\{'),
        'Options and action buttons share the same visible styling in both themes');
}
assert.match(theme, /\.btn:focus-visible\s*\{[^}]*outline:/,
    'Toolbar buttons retain a visible keyboard focus indicator');
assert.match(theme, /\.btn\.btn-action:disabled,\s*\.btn\.btn-options:disabled,/,
    'Options buttons preserve disabled-state styling');
console.log('PASS: options open/closed indicators and accessible state');
