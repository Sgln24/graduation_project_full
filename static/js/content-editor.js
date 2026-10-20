import { state } from './main.js';
import { elements } from './elements.js';
import { updateChapters, attachChapterSlides } from './chapters.js';
import { updateScenes } from './scenes.js';
import { contentLanguage } from './content-languages.js';
import { slideTitle, sceneTopicTitle } from './chapter-navigation.js';
import { showError, showNotification } from './ui.js';
import { loadTranscript } from './transcript.js';
import { refreshTranscriptVersions } from './transcript-versions.js';

const clone = value => JSON.parse(JSON.stringify(value));
const seconds = stamp => String(stamp).split(':').reduce((sum, part) => sum * 60 + Number(part), 0);

export function applyOutlineOperation(chapters, op, savedChapters = chapters) {
    const timestamps = items => items.map(item => [item.timestamp, ...(item.sections || []).map(part => part.timestamp)]);
    if (JSON.stringify(timestamps(chapters)) !== JSON.stringify(timestamps(savedChapters))) {
        throw new Error('Save timestamp changes before changing chapter grouping.');
    }
    const result = clone(chapters);
    const chapter = result[op.chapter];
    const sections = chapter.sections ||= [];
    if (op.action === 'merge_chapter') {
        if (!result[op.chapter + 1]) throw new Error('There is no following chapter.');
        const following = result.splice(op.chapter + 1, 1)[0];
        for (const item of [chapter, following]) {
            if (!item.sections?.length) item.sections = [{
                timestamp: item.timestamp, title: item.title, point: item.summary || '',
            }];
        }
        chapter.sections.push(...following.sections);
        chapter.summary = [chapter.summary, following.summary].filter(Boolean).join('\n\n');
    } else if (op.action === 'merge_section') {
        if (!sections[op.section + 1]) throw new Error('Choose adjacent parts in the same chapter.');
        const following = sections.splice(op.section + 1, 1)[0];
        sections[op.section].point = [sections[op.section].point, following.title, following.point].filter(Boolean).join('\n\n');
    } else if (op.action === 'promote') {
        const selected = sections[op.section];
        if (seconds(selected.timestamp) <= seconds(chapter.timestamp)) {
            throw new Error('This part already starts its chapter. Edit the chapter title instead.');
        }
        result.splice(op.chapter + 1, 0, {
            timestamp: selected.timestamp, title: selected.title, summary: '', sections: sections.splice(op.section),
        });
    } else if (op.action === 'move_section') {
        if (Math.abs(op.target - op.chapter) !== 1 || !result[op.target]) throw new Error('Choose an adjacent chapter.');
        if (sections.length < 2) throw new Error('Use Merge chapters to move the last remaining part.');
        const destination = result[op.target];
        if (!destination.sections?.length) destination.sections = [{
            timestamp: destination.timestamp, title: destination.title, point: destination.summary || '',
        }];
        if (op.target < op.chapter && op.section === 0) {
            destination.sections.push(sections.shift());
            chapter.timestamp = sections[0].timestamp;
        } else if (op.target > op.chapter && op.section === sections.length - 1) {
            const moved = sections.pop();
            destination.sections.unshift(moved);
            destination.timestamp = moved.timestamp;
        } else {
            throw new Error('To keep video order, move only the first part back or the last part forward.');
        }
    } else {
        throw new Error('Unknown outline action.');
    }
    return result;
}

function selection(kind) {
    return {
        videoId: state.currentVideoId, kind,
        language: kind === 'transcript' ? state.currentTranslationLanguage || ''
            : contentLanguage(state.currentVideoId, kind, state.currentTranslationLanguage),
        transcript_language: state.currentTranslationLanguage || '',
        source: state.currentTranscriptSource === 'uploaded' ? 'uploaded'
            : state.currentTranscriptSource === 'youtube' ? 'youtube' : 'whisper',
        version_id: state.currentTranscriptVersionId || '',
    };
}

export function initInlineEditor() {
    let editing = null;
    let loading = false;
    const sameSelection = snapshot => JSON.stringify(selection(snapshot.kind)) === JSON.stringify(snapshot.context);
    const content = kind => kind === 'chapters' ? state.videoChapters
        : kind === 'slides' ? state.videoScenes : state.currentTranscript;
    const container = kind => kind === 'chapters' ? elements.chaptersContainer
        : kind === 'slides' ? elements.scenesContainer : elements.transcriptContainer;
    const toolbar = document.createElement('div');
    toolbar.className = 'inline-edit-toolbar';
    toolbar.hidden = true;
    const status = document.createElement('p');
    status.setAttribute('role', 'status');
    const save = document.createElement('button');
    save.type = 'button';
    save.className = 'btn btn-accent';
    save.textContent = 'Save changes';
    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.className = 'btn btn-secondary';
    cancel.textContent = 'Cancel';
    toolbar.append(save, cancel, status);
    function input(row, selector, label, item, key, multiline = false) {
        const old = row.querySelector(selector);
        if (!old) return;
        const field = document.createElement(multiline ? 'textarea' : 'input');
        field.className = `inline-field ${key === 'timestamp' ? 'inline-time' : 'inline-title'}`;
        if (!multiline) field.type = 'text';
        field.value = item[key] || '';
        field.dir = 'auto';
        field.maxLength = key === 'timestamp' ? 30 : multiline ? 20000 : 500;
        field.setAttribute('aria-label', label);
        field.addEventListener('input', () => { item[key] = field.value; });
        for (const event of ['click', 'keydown', 'pointerdown']) field.addEventListener(event, e => e.stopPropagation());
        old.replaceWith(field);
    }
    function actionButton(parent, text, op) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'btn btn-secondary btn-action';
        button.textContent = text;
        button.addEventListener('click', event => {
            event.stopPropagation();
            operate(op);
        });
        parent.appendChild(button);
    }
    function notes(parent, label, item, key) {
        const wrapper = document.createElement('label');
        wrapper.textContent = label;
        const field = document.createElement('textarea');
        field.value = item[key] || '';
        field.rows = 3;
        field.maxLength = 20000;
        field.dir = 'auto';
        field.addEventListener('input', () => { item[key] = field.value; });
        wrapper.appendChild(field);
        parent.appendChild(wrapper);
    }
    function operate(op) {
        try {
            const savedChapters = editing.operations.reduce(
                (chapters, operation) => applyOutlineOperation(chapters, operation), editing.initialDraft);
            if (op.action.startsWith('merge') && !window.confirm(
                'Merge these adjacent items? The earlier title and timestamp will be kept, and all text retained. Save changes to apply.')) return;
            editing.draft = applyOutlineOperation(editing.draft, op, savedChapters);
            editing.operations.push(op);
            render();
            status.textContent = 'Grouping changed. Save changes to keep it, or Cancel to undo.';
        } catch (error) {
            status.textContent = error.message;
        }
    }
    function draggable(row, ref) {
        const handle = document.createElement('span');
        handle.textContent = ':::';
        handle.className = 'outline-drag-handle';
        handle.draggable = true;
        handle.title = 'Drag onto an adjacent item to merge, or into an adjacent chapter’s move zone';
        handle.addEventListener('dragstart', event => {
            event.dataTransfer.setData('application/x-lecture-part', JSON.stringify(ref));
            event.dataTransfer.effectAllowed = 'move';
        });
        row.prepend(handle);
        row.addEventListener('dragover', event => {
            event.preventDefault();
            event.stopPropagation();
            row.classList.add('outline-drop-merge');
        });
        row.addEventListener('dragleave', () => row.classList.remove('outline-drop-merge'));
        row.addEventListener('drop', event => {
            event.preventDefault();
            event.stopPropagation();
            row.classList.remove('outline-drop-merge');
            const raw = event.dataTransfer.getData('application/x-lecture-part');
            if (!raw) return;
            const from = JSON.parse(raw);
            if (from.section == null && ref.section == null && Math.abs(from.chapter - ref.chapter) === 1) {
                operate({action: 'merge_chapter', chapter: Math.min(from.chapter, ref.chapter)});
            } else if (from.chapter === ref.chapter && from.section != null && ref.section != null
                    && Math.abs(from.section - ref.section) === 1) {
                operate({action: 'merge_section', chapter: ref.chapter, section: Math.min(from.section, ref.section)});
            } else status.textContent = 'Merge adjacent chapters or adjacent parts within one chapter. Use a move zone to change grouping.';
        });
    }
    function render() {
        if (!editing) return;
        const {kind, draft} = editing;
        container(kind).classList.add('is-inline-editing');
        if (kind === 'chapters') {
            updateChapters(clone(draft), {editing: true});
            container(kind).querySelectorAll(':scope > .chapter-item').forEach((row, ci) => {
                const chapter = draft[ci];
                input(row, '.chapter-timestamp', `Chapter ${ci + 1} start`, chapter, 'timestamp');
                input(row, '.chapter-title', `Chapter ${ci + 1} title`, chapter, 'title');
                draggable(row, {chapter: ci});
                const menu = row.querySelector('.row-actions-content');
                notes(menu, 'Chapter summary', chapter, 'summary');
                if (ci + 1 < draft.length) actionButton(menu, 'Merge with next chapter', {action: 'merge_chapter', chapter: ci});
                const list = row.nextElementSibling;
                if (list?.classList.contains('chapter-sections')) {
                    list.hidden = false;
                    row.classList.add('expanded');
                    row.querySelector('.chapter-expand')?.setAttribute('aria-expanded', 'true');
                    list.querySelectorAll(':scope > .chapter-section').forEach((part, si) => {
                        const section = chapter.sections[si];
                        input(part, '.chapter-timestamp', `Part ${ci + 1}.${si + 1} start`, section, 'timestamp');
                        input(part, '.section-title', `Part ${ci + 1}.${si + 1} title`, section, 'title');
                        draggable(part, {chapter: ci, section: si});
                        const actions = part.querySelector('.row-actions-content');
                        notes(actions, 'Part text', section, 'point');
                        actionButton(actions, 'Make a new chapter', {action: 'promote', chapter: ci, section: si});
                        if (si + 1 < chapter.sections.length) actionButton(actions, 'Merge with next part',
                            {action: 'merge_section', chapter: ci, section: si});
                        if (si === 0 && ci > 0) actionButton(actions, 'Move to previous chapter',
                            {action: 'move_section', chapter: ci, section: si, target: ci - 1});
                        if (si === chapter.sections.length - 1 && ci + 1 < draft.length) actionButton(actions, 'Move to next chapter',
                            {action: 'move_section', chapter: ci, section: si, target: ci + 1});
                    });
                }
                const zone = document.createElement('div');
                zone.className = 'outline-move-zone';
                zone.textContent = 'Drop a boundary part here to move it into this chapter';
                zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('is-dragover'); });
                zone.addEventListener('dragleave', () => zone.classList.remove('is-dragover'));
                zone.addEventListener('drop', e => {
                    e.preventDefault();
                    zone.classList.remove('is-dragover');
                    const raw = e.dataTransfer.getData('application/x-lecture-part');
                    if (!raw) return;
                    const from = JSON.parse(raw);
                    operate({action: 'move_section', ...from, target: ci});
                });
                (list?.classList.contains('chapter-sections') ? list : row).after(zone);
            });
        } else if (kind === 'slides') {
            updateScenes(clone(draft), elements.videoPlayer);
            container(kind).querySelectorAll('.scene-item').forEach((row, index) => {
                input(row, '.scene-timestamp', `Slide ${index + 1} start`, draft[index], 'timestamp');
                input(row, '.scene-label', `Slide ${index + 1} title`, draft[index], 'title');
            });
        } else {
            loadTranscript(clone(draft));
            container(kind).querySelectorAll('.transcript-line').forEach((row, index) => {
                input(row, '.transcript-text', `Transcript text ${index + 1}`, draft[index], 'text', true);
            });
        }
        container(kind).classList.add('is-inline-editing');
        toolbar.hidden = false;
        container(kind).before(toolbar);
        status.textContent = kind === 'chapters'
            ? 'Edit here. Drag handles onto adjacent items to merge; use move zones to regroup boundary parts. Menus also work without dragging. Video order stays unchanged.'
            : kind === 'slides' ? 'Edit slide titles and times here. Images stay unchanged; times must remain ordered.'
                : 'Edit transcript text here. Timing and speakers stay unchanged. Saving creates an edited version; the original remains available.';
        document.querySelector(`.${kind === 'chapters' ? 'chapter-controls' : kind === 'slides' ? 'slide-controls' : 'transcript-controls'}`)
            ?.setAttribute('inert', '');
        document.querySelectorAll('.controls-bar .action-menu[open]').forEach(menu => { menu.open = false; });
    }
    function close(restore = true) {
        if (!editing) return;
        const previous = editing;
        editing = null;
        toolbar.hidden = true;
        container(previous.kind).classList.remove('is-inline-editing');
        document.querySelector(`.${previous.kind === 'chapters' ? 'chapter-controls' : previous.kind === 'slides' ? 'slide-controls' : 'transcript-controls'}`)
            ?.removeAttribute('inert');
        if (restore && sameSelection(previous)) {
            if (previous.kind === 'chapters') updateChapters(previous.original);
            else if (previous.kind === 'slides') updateScenes(previous.original, elements.videoPlayer);
            else loadTranscript(previous.original);
        }
    }
    const refresh = () => {
        for (const kind of ['chapters', 'slides', 'transcript']) {
            document.getElementById(`edit-${kind}-btn`).disabled = !state.currentVideoId || !content(kind)?.length;
        }
    };
    for (const event of ['chaptersUpdated', 'slidesUpdated', 'transcriptUpdated']) document.addEventListener(event, refresh);
    refresh();
    for (const kind of ['chapters', 'slides', 'transcript']) {
        document.getElementById(`edit-${kind}-btn`).addEventListener('click', async () => {
            if (loading || editing?.saving) return;
            if (editing) {
                if (editing.kind === kind) { container(kind).querySelector('input')?.focus(); return; }
                showError('Save or cancel the current inline edits first.');
                return;
            }
            const snapshot = selection(kind);
            loading = true;
            try {
                const query = new URLSearchParams(snapshot);
                const response = await fetch(`/editable_content/${encodeURIComponent(snapshot.videoId)}?${query}`);
                const data = await response.json();
                if (!response.ok) throw new Error(data.detail || 'Could not open inline editing.');
                if (JSON.stringify(selection(kind)) !== JSON.stringify(snapshot)) return;
                const draft = clone(data.content);
                if (kind === 'transcript' && JSON.stringify(draft) !== JSON.stringify(content(kind))) {
                    throw new Error('The displayed transcript differs from the saved version. Reload it before editing.');
                }
                if (kind === 'slides') draft.forEach(item => {
                    item.title = slideTitle(item, snapshot.language)
                        || sceneTopicTitle(state.videoChapters || [], Number(item.time_seconds)) || '';
                });
                editing = {kind, context: snapshot, original: clone(content(kind)), draft,
                    initialDraft: clone(draft), revision: data.revision, operations: [], saving: false};
                render();
                container(kind).querySelector('input')?.focus();
            } catch (error) {
                showError(error.message);
            } finally { loading = false; }
        });
    }
    cancel.addEventListener('click', () => {
        if (editing?.saving) return;
        if (editing && JSON.stringify(editing.draft) !== JSON.stringify(editing.initialDraft)
                && !window.confirm('Discard unsaved changes? Saved content will remain unchanged.')) return;
        close();
    });
    save.addEventListener('click', async () => {
        if (!editing || editing.saving) return;
        const snapshot = editing;
        if (!sameSelection(snapshot)) { close(false); showError('The video or transcript changed. Reopen inline editing.'); return; }
        snapshot.saving = true;
        save.disabled = cancel.disabled = true;
        container(snapshot.kind).inert = true;
        status.textContent = 'Saving changes...';
        try {
            const response = await fetch(`/editable_content/${encodeURIComponent(snapshot.context.videoId)}`, {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({...snapshot.context, revision: snapshot.revision, operations: snapshot.operations,
                    edits: snapshot.kind === 'slides'
                        ? snapshot.draft.map(item => ({title: item.title, timestamp: item.timestamp}))
                        : snapshot.kind === 'transcript' ? snapshot.draft.map(item => item.text) : snapshot.draft}),
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.detail || 'Could not save changes.');
            if (sameSelection(snapshot)) {
                close(false);
                if (snapshot.kind === 'chapters') updateChapters(data.content);
                else if (snapshot.kind === 'slides') { updateScenes(data.content, elements.videoPlayer); attachChapterSlides(); }
                else {
                    state.currentTranscriptSource = data.version.kind === 'translation'
                        ? `translation:${data.version.language}` : data.version.source === 'uploaded' ? 'uploaded' : 'whisper';
                    loadTranscript(data.content);
                    state.currentTranscriptVersionId = data.version.id;
                    await refreshTranscriptVersions();
                }
            }
            showNotification('Changes saved. New exports will use them; existing exports are unchanged.', 'success');
        } catch (error) {
            status.textContent = `${error.message} Your edits are still here.`;
        } finally {
            snapshot.saving = false;
            save.disabled = cancel.disabled = false;
            container(snapshot.kind).inert = false;
        }
    });
    document.addEventListener('videoSelectionChanged', () => close(false));
    document.addEventListener('transcriptUpdated', () => {
        if (editing && !sameSelection(editing)) close(false);
    });
    window.addEventListener('beforeunload', event => {
        if (editing && JSON.stringify(editing.draft) !== JSON.stringify(editing.initialDraft)) {
            event.preventDefault();
            event.returnValue = '';
        }
    });
}
