const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'api-module.js'), 'utf8')
    .replace(/^import .*;\r?$/gm, '').replace(/^export /gm, '');
const requests = [];
const loaded = [];
const errors = [];
const timers = [];
const context = vm.createContext({
    startElapsedTimer: (container, elapsed = 0) => {
        timers.push(elapsed);
        return () => {};
    },
    console, Date, setTimeout, clearTimeout,
    state: {currentVideoId: 'video', currentTranscript: [], whisperTranscriptPollGeneration: 0},
    elements: {transcriptContainer: {}, regenerateTranscriptBtn: {textContent: 'Regenerate'}},
    document: {getElementById: () => null, addEventListener: () => {}},
    showError: (message) => { if (message) errors.push(message); },
    showNotification: () => {},
    loadTranscript: (transcript) => loaded.push(transcript),
    fetch: async (url) => {
        requests.push(url);
        return {ok: true, status: 200, text: async () => JSON.stringify({
            status: 'complete', transcript: [{text: 'Finished', start: 0}],
        })};
    },
});
vm.runInContext(source, context);
(async () => {
    await context.regenerateTranscript({resumeOnly: true});
    assert.deepEqual(requests, ['/whisper_transcript_status/video']);
    assert.equal(loaded.length, 1);
    assert.deepEqual(errors, []);
    context.renderWhisperProgress({status: 'running', steps_elapsed: 125}, Date.now());
    assert.equal(timers.at(-1), 125, 'Reconnect uses server elapsed, not a fresh browser clock');
    console.log('PASS: reconnect to transcription progress without restarting the job');
})().catch(error => {console.error(error); process.exitCode = 1;});
