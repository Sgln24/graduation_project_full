// transcript-versions.js - choose between saved transcripts and translations of a video

import { state } from './main.js';
import { loadTranscript } from './transcript.js';
import { updateChapters, clearChapterMarkers } from './chapters.js';
import { showError, showNotification } from './ui.js';
import { contentLanguage, savedSummaryUrl } from './content-languages.js';

let versions = [];
let refreshGeneration = 0;
let switching = false;

const select = () => document.getElementById('transcriptVersionSelect');
const deleteButton = () => document.getElementById('deleteTranscriptVersionBtn');
const control = () => document.getElementById('transcriptVersionsControl');

function versionLabel(version) {
    const date = version.created_at ? ` · ${version.created_at}` : '';
    return `${version.label || 'Transcript'}${date}`;
}

function currentVersionId() {
    const source = state.currentTranscriptSource || '';
    if (source.startsWith('translation:')) {
        const language = source.slice('translation:'.length);
        return state.currentTranscriptVersionId
            && versions.some((version) => version.id === state.currentTranscriptVersionId && version.language === language)
            ? state.currentTranscriptVersionId
            : versions.find((version) => version.kind === 'translation' && version.language === language)?.id;
    }
    if (source === 'uploaded') {
        return state.currentTranscriptVersionId
            || versions.find((version) => version.source === 'uploaded')?.id;
    }
    if (source === 'whisper') {
        return versions.find((version) => version.active_whisper)?.id;
    }
    return '';
}

function render() {
    const element = select();
    if (!element) return;
    const selected = currentVersionId() || '';
    const originals = versions.filter((version) => version.kind === 'transcript');
    const translations = versions.filter((version) => version.kind === 'translation');
    const option = (version) => {
        const item = document.createElement('option');
        item.value = version.id;
        item.textContent = versionLabel(version);
        if (version.based_on) item.title = `Translated from: ${version.based_on}`;
        return item;
    };
    element.replaceChildren();
    if (!selected) {
        const placeholder = document.createElement('option');
        placeholder.value = '';
        placeholder.textContent = state.currentTranscriptSource === 'youtube'
            ? 'YouTube captions (current)' : 'Saved transcripts…';
        element.append(placeholder);
    }
    [['Transcripts', originals], ['Translations', translations]].forEach(([label, items]) => {
        if (!items.length) return;
        const group = document.createElement('optgroup');
        group.label = label;
        items.forEach((version) => group.append(option(version)));
        element.append(group);
    });
    element.value = selected;
    const captionSelect = document.getElementById('captionTranscriptVersionSelect');
    if (captionSelect) {
        captionSelect.replaceChildren(...Array.from(element.children, child => child.cloneNode(true)));
        captionSelect.value = selected;
        captionSelect.disabled = switching || !versions.length;
    }
    const tracks = document.getElementById('captionTrackVersions');
    if (tracks) {
        const chosen = new Set(Array.from(tracks.selectedOptions, item => item.value));
        tracks.replaceChildren(...versions.map(version => {
            const item = option(version);
            item.selected = chosen.has(version.id) || (!chosen.size && version.id === selected);
            return item;
        }));
    }
    const wrapper = control();
    if (wrapper) wrapper.hidden = !versions.length;
    const remove = deleteButton();
    if (remove) {
        const version = versions.find((item) => item.id === selected);
        remove.disabled = !version || Boolean(version.active_whisper);
        remove.title = version?.active_whisper
            ? 'This is the current transcript; choose another version before deleting it'
            : 'Delete this saved version';
    }
}

export async function refreshTranscriptVersions() {
    const videoId = state.currentVideoId;
    const generation = ++refreshGeneration;
    if (!videoId) {
        versions = [];
        render();
        return;
    }
    try {
        const response = await fetch(`/transcript_versions/${encodeURIComponent(videoId)}`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        if (generation !== refreshGeneration || videoId !== state.currentVideoId) return;
        versions = Array.isArray(data.versions) ? data.versions : [];
    } catch (error) {
        console.warn('Could not load saved transcripts:', error);
        versions = [];
    }
    render();
}

// Chapters belong to one transcript language; never keep showing another version's chapters.
function showNoChapters() {
    state.videoChapters = [];
    clearChapterMarkers();
    updateChapters([]);
    document.dispatchEvent(new CustomEvent('chaptersUpdated', { detail: [] }));
}

async function loadOriginalChapters(videoId) {
    try {
        const response = await fetch(savedSummaryUrl(videoId, state.currentTranslationLanguage));
        const data = await response.json();
        if (!response.ok || !data.success) throw new Error(data.error || 'Could not read saved chapters.');
        if (videoId !== state.currentVideoId) return;
        if (data.success && data.exists && Array.isArray(data.chapters) && data.chapters.length) {
            state.videoChapters = data.chapters;
            updateChapters(data.chapters);
        } else {
            showNoChapters();
        }
    } catch (error) {
        if (videoId === state.currentVideoId) showError(`Could not reload chapters: ${error.message}`);
    }
}

export async function chooseTranscriptVersion(versionId) {
    const videoId = state.currentVideoId;
    if (!videoId || !versionId || switching) return;
    switching = true;
    const element = select();
    if (element) element.disabled = true;
    const captionSelect = document.getElementById('captionTranscriptVersionSelect');
    if (captionSelect) captionSelect.disabled = true;
    try {
        const response = await fetch(
            `/transcript_versions/${encodeURIComponent(videoId)}/${encodeURIComponent(versionId)}/activate`,
            { method: 'POST' }
        );
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
        if (videoId !== state.currentVideoId) return;
        const version = data.version;
        state.whisperTranscriptPollGeneration = (state.whisperTranscriptPollGeneration || 0) + 1;
        state.currentTranscriptVersionId = version.id;
        const targetSelect = document.getElementById('translationTarget');
        if (version.kind === 'translation') {
            state.currentTranscriptSource = `translation:${version.language}`;
            state.currentTranslationLanguage = version.language;
            if (targetSelect) targetSelect.value = version.language;
        } else {
            state.currentTranscriptSource = version.source === 'uploaded' ? 'uploaded' : 'whisper';
            state.currentTranslationLanguage = null;
            if (targetSelect) targetSelect.value = '';
        }
        loadTranscript(data.transcript);
        if (contentLanguage(videoId, 'chapters', state.currentTranslationLanguage) !== (state.currentTranslationLanguage || '')
                || !Array.isArray(data.chapters) || !data.chapters.length) {
            await loadOriginalChapters(videoId);
        } else if (Array.isArray(data.chapters)) {
            state.videoChapters = data.chapters;
            updateChapters(data.chapters);
        }
        showNotification(`Switched to: ${version.label}`, 'success');
    } catch (error) {
        showError(`Could not switch transcript: ${error.message}`);
    } finally {
        switching = false;
        if (element) element.disabled = false;
        if (captionSelect) captionSelect.disabled = false;
        void refreshTranscriptVersions();
    }
}

async function deleteSelectedVersion() {
    const videoId = state.currentVideoId;
    const versionId = select()?.value;
    const version = versions.find((item) => item.id === versionId);
    if (!videoId || !version) return;
    if (!window.confirm(`Delete the saved version "${versionLabel(version)}"?`)) return;
    try {
        const response = await fetch(
            `/transcript_versions/${encodeURIComponent(videoId)}/${encodeURIComponent(versionId)}`,
            { method: 'DELETE' }
        );
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
        if (state.currentTranscriptVersionId === versionId) state.currentTranscriptVersionId = null;
        showNotification('Saved version deleted. The transcript on screen stays until you choose another one.', 'info');
    } catch (error) {
        showError(`Could not delete the saved version: ${error.message}`);
    }
    void refreshTranscriptVersions();
}

let refreshTimer = null;
function scheduleRefresh() {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(() => void refreshTranscriptVersions(), 150);
}

export function initTranscriptVersions() {
    select()?.addEventListener('change', (event) => {
        if (event.target.value) void chooseTranscriptVersion(event.target.value);
    });
    document.getElementById('captionTranscriptVersionSelect')?.addEventListener('change', (event) => {
        if (event.target.value) void chooseTranscriptVersion(event.target.value);
    });
    deleteButton()?.addEventListener('click', () => void deleteSelectedVersion());
    document.addEventListener('transcriptUpdated', () => {
        if (!switching) state.currentTranscriptVersionId = null;
        scheduleRefresh();
    });
}
