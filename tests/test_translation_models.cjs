const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'api-module.js'), 'utf8');
const start = source.indexOf('export async function updateTranslationModelStatus()');
const end = source.indexOf('\nfunction updateTranslationOptions(', start);
const select = {
    options: [], value: '', dataset: {},
    replaceChildren(...options) { this.options = options; },
    append(option) { this.options.push(option); },
    get selectedIndex() { return this.options.findIndex(option => option.value === this.value); },
    get selectedOptions() { return this.options.filter(option => option.value === this.value); },
};
let data = {
    translation_default_provider: 'Groq', translation_default_model: 'openai/gpt-oss-120b',
    groq_configured: true, gemini_configured: true, openai_configured: false,
    translation_models: [
        {id: 'groq:llama-3.3-70b-versatile', label: 'Unavailable Llama', available: false},
        {id: 'groq:openai/gpt-oss-20b', label: 'GPT-OSS 20B', available: true},
        {id: 'gemini-test', label: 'Unavailable Gemini', available: false},
    ],
};
const context = vm.createContext({
    elements: {translationModel: select, translationModelStatus: {}, translateTranscriptBtn: {}},
    document: {createElement: () => ({disabled: false})},
    fetch: async () => ({}),
    readJsonResponse: async () => data,
    applyTranscriptionModelStatus: () => {},
});
vm.runInContext(source.slice(start, end).replace('export ', ''), context);
(async () => {
    select.value = 'groq:llama-3.3-70b-versatile';
    await context.updateTranslationModelStatus();
    assert.equal(select.options.find(option => option.value.includes('llama')).disabled, true);
    assert.equal(select.options.find(option => option.value === 'gemini-test').disabled, true);
    assert.equal(select.options.find(option => option.value.endsWith('20b')).disabled, false);
    assert.equal(select.value, '', 'Unavailable selection resets to Auto');
    select.value = 'groq:openai/gpt-oss-20b';
    await context.updateTranslationModelStatus();
    assert.equal(select.value, 'groq:openai/gpt-oss-20b');
    delete data.translation_models;
    data.groq_configured = false;
    await context.updateTranslationModelStatus();
    assert.equal(select.options.find(option => option.value.endsWith('20b')).disabled, true);
    assert.equal(select.value, '', 'Legacy configuration still disables unconfigured providers');
    console.log('PASS: per-model availability survives configured-provider checks');
})().catch(error => { console.error(error); process.exitCode = 1; });
