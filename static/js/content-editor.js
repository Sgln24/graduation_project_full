import { state } from './main.js';
import { elements } from './elements.js';
import { loadTranscript } from './transcript.js';
import { updateChapters, attachChapterSlides } from './chapters.js';
import { updateScenes } from './scenes.js';
import { showNotification } from './ui.js';
import { refreshTranscriptVersions } from './transcript-versions.js';
import { contentLanguage } from './content-languages.js';
import { slideTitle, sceneTopicTitle } from './chapter-navigation.js';

const clone = value => JSON.parse(JSON.stringify(value));
const labels = {chapters: 'Edit chapters', slides: 'Edit slide titles', transcript: 'Edit transcript'};

function context(kind) {
    return {
        videoId: state.currentVideoId,
        language: kind === 'transcript' ? state.currentTranslationLanguage || ''
            : contentLanguage(state.currentVideoId, kind, state.currentTranslationLanguage),
        transcript_language: state.currentTranslationLanguage || '',
        source: state.currentTranscriptSource === 'uploaded' ? 'uploaded'
            : state.currentTranscriptSource === 'youtube' ? 'youtube' : 'whisper',
        version_id: state.currentTranscriptVersionId
            || document.getElementById('transcriptVersionSelect')?.value || '',
    };
}

function currentContent(kind) {
    return kind === 'chapters' ? state.videoChapters || []
        : kind === 'slides' ? state.videoScenes || [] : state.currentTranscript || [];
}

function sameContext(snapshot) {
    const current = context(snapshot.kind);
    return current.videoId === snapshot.videoId && current.language === snapshot.language
        && current.transcript_language === snapshot.transcript_language
        && current.source === snapshot.source && current.version_id === snapshot.version_id;
}

function field(parent, label, value, {multiline = false, blank = false, limit = 500} = {}) {
    const wrapper = document.createElement('label');
    wrapper.textContent = label;
    const input = document.createElement(multiline ? 'textarea' : 'input');
    if (!multiline) input.type = 'text';
    input.value = value || '';
    input.dir = 'auto';
    input.required = !blank;
    input.maxLength = limit;
    if (multiline) input.rows = 3;
    wrapper.appendChild(input);
    parent.appendChild(wrapper);
    return input;
}

export function initContentEditor() {
    const dialog = document.getElementById('contentEditorDialog');
    const form = document.getElementById('contentEditorForm');
    const rows = document.getElementById('contentEditorRows');
    const error = document.getElementById('contentEditorError');
    const save = document.getElementById('saveContentEditsBtn');
    let editing = null;
    let reading = null;
    let opening = 0;
    let saving = false;
    let dirty = false;
    form.addEventListener('input', () => { dirty = true; });
    dialog.addEventListener('cancel', event => {
        if (saving) event.preventDefault();
    });
    document.getElementById('cancelContentEditsBtn').addEventListener('click', () => {
        if (saving) return;
        if (dirty) {
            document.getElementById('discardContentEditsBtn').hidden = false;
            error.textContent = 'You have unsaved edits. Choose Discard edits to close without saving.';
        } else {
            dialog.close();
        }
    });
    document.getElementById('discardContentEditsBtn').addEventListener('click', () => {
        if (!saving) dialog.close();
    });
    dialog.addEventListener('cancel', event => {
        if (dirty) {
            event.preventDefault();
            document.getElementById('cancelContentEditsBtn').click();
        }
    });
    const refresh = () => {
        for (const kind of Object.keys(labels)) {
            document.getElementById(`edit-${kind}-btn`).disabled =
                !state.currentVideoId || !currentContent(kind).length;
        }
    };
    for (const name of ['transcriptUpdated', 'chaptersUpdated', 'slidesUpdated']) {
        document.addEventListener(name, refresh);
    }
    for (const kind of Object.keys(labels)) {
        document.getElementById(`edit-${kind}-btn`).addEventListener('click', async () => {
            const generation = ++opening;
            const snapshot = {...context(kind), kind};
            editing = null;
            dirty = false;
            reading = null;
            rows.replaceChildren();
            error.textContent = '';
            document.getElementById('discardContentEditsBtn').hidden = true;
            save.disabled = true;
            document.getElementById('contentEditorHeading').textContent = labels[kind];
            document.getElementById('contentEditorHelp').textContent = kind === 'transcript'
                ? 'Correct text without changing timing or speakers. Save creates an edited version; the original remains available.'
                : kind === 'slides'
                    ? 'Edit titles for the displayed title language. Leave a title blank to use the topic-derived title. Slide timing and images stay unchanged.'
                    : 'Change the title fields below, then choose Save edits. You can also change summaries, section text and MM:SS or HH:MM:SS timestamps. Chapters must stay ordered.';
            document.querySelectorAll('dialog.options-dialog[open]').forEach(open => {
                if (open !== dialog) open.close();
            });
            if (!dialog.open) dialog.showModal();
            try {
                const query = new URLSearchParams({...snapshot, kind});
                const response = await fetch(`/editable_content/${encodeURIComponent(snapshot.videoId)}?${query}`);
                const data = await response.json();
                if (!response.ok) throw new Error(data.detail || `Could not load content (${response.status})`);
                if (generation !== opening || !dialog.open) return;
                if (!sameContext(snapshot)) throw new Error('The selected video or transcript changed. Reopen the editor.');
                if (kind === 'transcript' && JSON.stringify(data.content) !== JSON.stringify(currentContent(kind))) {
                    throw new Error('Displayed content differs from the saved version. Reload this video/version before editing.');
                }
                const original = clone(currentContent(kind));
                const getters = data.content.map((item, index) => {
                    const row = document.createElement('fieldset');
                    const legend = document.createElement('legend');
                    legend.textContent = `${kind === 'transcript' ? 'Transcript' : kind === 'slides' ? 'Slide' : 'Chapter'} ${index + 1}`
                        + (item.timestamp ? ` - ${item.timestamp}` : kind === 'transcript' ? ` - ${item.start}s` : '');
                    row.appendChild(legend);
                    rows.appendChild(row);
                    if (kind === 'slides') {
                        const title = field(row, 'Slide title', slideTitle(item, snapshot.language)
                            || sceneTopicTitle(state.videoChapters || [], Number(item.time_seconds)), {blank: true});
                        return () => title.value;
                    }
                    if (kind === 'transcript') {
                        const text = field(row, item.speaker ? `Text (${item.speaker})` : 'Text', item.text,
                            {multiline: true, limit: 20000});
                        return () => text.value;
                    }
                    const title = field(row, 'Chapter title', item.title);
                    const timestamp = field(row, 'Start timestamp', item.timestamp, {limit: 30});
                    const summary = field(row, 'Summary', item.summary, {multiline: true, blank: true, limit: 20000});
                    const sections = (item.sections || []).map((section, sectionIndex) => {
                        const group = document.createElement('fieldset');
                        const heading = document.createElement('legend');
                        heading.textContent = `Section ${index + 1}.${sectionIndex + 1}`;
                        group.appendChild(heading);
                        row.appendChild(group);
                        const title = field(group, 'Section title', section.title);
                        const timestamp = field(group, 'Section timestamp', section.timestamp, {limit: 30});
                        const point = field(group, 'Section text', section.point, {multiline: true, blank: true, limit: 20000});
                        return () => ({title: title.value, timestamp: timestamp.value, point: point.value});
                    });
                    return () => ({title: title.value, timestamp: timestamp.value, summary: summary.value,
                        sections: sections.map(get => get())});
                });
                editing = {...snapshot, kind, revision: data.revision, original};
                reading = () => getters.map(get => get());
                save.disabled = false;
                rows.querySelector('input, textarea')?.focus();
            } catch (failure) {
                if (generation === opening) error.textContent = failure.message;
            }
        });
    }
    form.addEventListener('submit', async event => {
        event.preventDefault();
        if (!editing || !reading || saving) return;
        if (!sameContext(editing) || JSON.stringify(currentContent(editing.kind)) !== JSON.stringify(editing.original)) {
            error.textContent = 'The selected video, transcript or content changed. Reopen the editor before saving.';
            return;
        }
        saving = true;
        save.disabled = true;
        error.textContent = 'Saving edits...';
        const snapshot = editing;
        try {
            const response = await fetch(`/editable_content/${encodeURIComponent(snapshot.videoId)}`, {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({...snapshot, original: undefined, edits: reading()}),
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.detail || `Could not save edits (${response.status})`);
            if (sameContext(snapshot)) {
                if (snapshot.kind === 'transcript') {
                    state.currentTranscriptSource = data.version.kind === 'translation'
                        ? `translation:${data.version.language}` : data.version.source === 'uploaded' ? 'uploaded' : 'whisper';
                    loadTranscript(data.content);
                    state.currentTranscriptVersionId = data.version.id;
                    await refreshTranscriptVersions();
                } else if (snapshot.kind === 'chapters') {
                    updateChapters(data.content);
                } else {
                    updateScenes(data.content, elements.videoPlayer);
                    attachChapterSlides();
                }
            }
            dirty = false;
            dialog.close();
            showNotification('Edits saved. New exports will use the corrected content; existing exports are unchanged.', 'success');
        } catch (failure) {
            error.textContent = failure.message;
        } finally {
            saving = false;
            save.disabled = false;
        }
    });
    refresh();
}
