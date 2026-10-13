import { state } from './main.js';
import { refreshSavedExports } from './chapters.js';
import { showError } from './ui.js';

let submitting = false;
let refreshGeneration = 0;
let savedVideosGeneration = 0;
let previewGeneration = 0;
let previewController = null;
let previewUrl = null;
let previewTimer = null;

function selectedCaptionStyle() {
    return {
        preset: document.getElementById('captionStylePreset')?.value || 'box',
        size: document.getElementById('captionStyleSize')?.value || 'medium',
        position: document.getElementById('captionStylePosition')?.value || 'bottom',
    };
}

function clearCaptionPreview() {
    const image = document.getElementById('captionStylePreview');
    if (image) {
        image.hidden = true;
        image.removeAttribute('src');
    }
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = null;
}

export async function refreshCaptionPreview() {
    const generation = ++previewGeneration;
    previewController?.abort();
    clearCaptionPreview();
    const status = document.getElementById('captionPreviewStatus');
    if (!status) return;
    const videoId = state.currentVideoId;
    if (!videoId || !state.currentTranscript?.length) {
        status.textContent = 'Load a video and choose a transcript to preview captions.';
        return;
    }
    const transcriptSource = state.currentTranscript;
    const style = selectedCaptionStyle();
    previewController = new AbortController();
    const snapshot = transcriptSource.map(item => ({ start: item.start, duration: item.duration, text: item.text }));
    status.textContent = 'Rendering caption preview...';
    try {
        const response = await fetch(`/caption_preview/${encodeURIComponent(videoId)}`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            signal: previewController.signal,
            body: JSON.stringify({
                transcript: snapshot, style, at: document.querySelector('video')?.currentTime || 0,
            }),
        });
        if (!response.ok) {
            const data = await response.json();
            throw new Error(data.detail || `HTTP ${response.status}`);
        }
        const blob = await response.blob();
        if (generation !== previewGeneration || videoId !== state.currentVideoId
            || transcriptSource !== state.currentTranscript) return;
        previewUrl = URL.createObjectURL(blob);
        const image = document.getElementById('captionStylePreview');
        image.src = previewUrl;
        image.hidden = false;
        status.textContent = `Export preview: ${style.preset}, ${style.size}, ${style.position}.`;
    } catch (error) {
        if (error.name === 'AbortError' || generation !== previewGeneration
            || videoId !== state.currentVideoId || transcriptSource !== state.currentTranscript) return;
        status.textContent = `Could not render caption preview: ${error.message}`;
    }
}

function scheduleCaptionPreview() {
    clearTimeout(previewTimer);
    ++previewGeneration;
    previewController?.abort();
    clearCaptionPreview();
    previewTimer = setTimeout(() => void refreshCaptionPreview(), 400);
}

function transcriptLabel() {
    const selected = document.getElementById('transcriptVersionSelect')?.selectedOptions[0];
    return selected?.value ? selected.textContent : state.currentTranscriptSource || 'Current transcript';
}

async function readResponse(response) {
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
    return data;
}

function renderCaptionJobs(container, jobs, emptyMessage) {
    container.replaceChildren();
    if (!jobs.length) {
        container.textContent = emptyMessage;
        return;
    }
    jobs.forEach(job => {
        const row = document.createElement('p');
        const message = job.status === 'complete' ? 'Ready to download'
            : job.status === 'error' ? job.error : job.steps?.at(-1)?.message || job.status;
        row.textContent = `${job.filename ? `${job.filename} - ` : ''}${job.title}: ${message}`;
        if (job.created_at) {
            const date = document.createElement('small');
            date.textContent = new Date(job.created_at * 1000).toLocaleString();
            row.append(date);
        }
        if (job.status === 'complete') {
            const link = document.createElement('a');
            link.href = job.download_url;
            link.className = 'btn btn-secondary btn-action';
            link.textContent = 'Download MP4';
            row.append(link);
        }
        container.append(row);
    });
}

export async function refreshSavedCaptionVideos() {
    const generation = ++savedVideosGeneration;
    const container = document.getElementById('savedCaptionVideosList');
    container.textContent = 'Loading saved captioned videos...';
    try {
        const data = await readResponse(await fetch('/saved_caption_videos'));
        if (generation !== savedVideosGeneration) return;
        renderCaptionJobs(container, data.exports, 'No captioned video copies have been saved yet. Use Add captions to video to create one.');
    } catch (error) {
        if (generation === savedVideosGeneration) {
            container.textContent = `Could not load saved captioned videos: ${error.message}`;
        }
    }
}

export async function refreshCaptionExports() {
    const videoId = state.currentVideoId;
    const generation = ++refreshGeneration;
    const container = document.getElementById('captionVideoProgress');
    if (!container) return;
    document.getElementById('captionTranscriptLabel').textContent = `Caption source: ${transcriptLabel()}`;
    container.replaceChildren();
    if (!videoId) return;
    try {
        const data = await readResponse(await fetch(`/saved_exports/${encodeURIComponent(videoId)}`));
        if (generation !== refreshGeneration || videoId !== state.currentVideoId) return;
        const jobs = data.exports.filter(job => job.title.startsWith('Captioned video - '));
        renderCaptionJobs(container, jobs, 'No captioned copies of this video yet. Copies from other lectures are available under Saved captioned videos in the Transcript toolbar.');
    } catch (error) {
        if (generation !== refreshGeneration || videoId !== state.currentVideoId) return;
        container.textContent = `Could not load captioned-video exports: ${error.message}`;
    }
}

export async function exportCaptionedVideo() {
    if (submitting) return;
    const videoId = state.currentVideoId;
    if (!videoId || !state.currentTranscript?.length) {
        showError('Load a video and choose a transcript or translation before exporting captions.');
        return;
    }
    const label = transcriptLabel();
    const transcript = state.currentTranscript.map(item => ({ start: item.start, duration: item.duration, text: item.text }));
    const button = document.getElementById('exportCaptionedVideoBtn');
    submitting = true;
    button.disabled = true;
    try {
        await readResponse(await fetch(`/caption_video_jobs/${encodeURIComponent(videoId)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                transcript, transcript_label: label, filename: state.currentVideoFilename || 'video',
                style: selectedCaptionStyle(),
            }),
        }));
        if (videoId === state.currentVideoId) await refreshCaptionExports();
    } catch (error) {
        if (videoId === state.currentVideoId) showError(`Could not start captioned-video export: ${error.message}`);
    } finally {
        submitting = false;
        button.disabled = false;
    }
}

export async function downloadCaptionFile(format) {
    const videoId = state.currentVideoId;
    if (!videoId || !state.currentTranscript?.length) {
        showError('Load a video and choose a transcript or translation before downloading captions.');
        return;
    }
    const button = document.getElementById(format === 'srt' ? 'downloadCaptionSrtBtn' : 'downloadCaptionVttBtn');
    const filename = (state.currentVideoFilename || 'video').replace(/\.[^.]+$/, '');
    const transcript = state.currentTranscript.map(item => ({ start: item.start, duration: item.duration, text: item.text }));
    button.disabled = true;
    try {
        const response = await fetch(`/caption_file/${encodeURIComponent(videoId)}/${format}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ transcript, filename: state.currentVideoFilename || 'video' }),
        });
        if (!response.ok) {
            const data = await response.json();
            throw new Error(data.detail || `HTTP ${response.status}`);
        }
        const url = URL.createObjectURL(await response.blob());
        const link = document.createElement('a');
        link.href = url;
        link.download = `${filename}.${format}`;
        const dialog = document.getElementById('transcriptDownloadDialog');
        (dialog?.open ? dialog : document.body).append(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 60000);
    } catch (error) {
        showError(`Could not download captions: ${error.message}`);
    } finally {
        button.disabled = false;
    }
}

export function initCaptionExport() {
    document.getElementById('exportCaptionedVideoBtn')?.addEventListener('click', () => void exportCaptionedVideo());
    document.getElementById('openCaptionStyleBtn')?.addEventListener('click', () => {
        document.getElementById('captionStyleDialog').showModal();
        void refreshCaptionPreview();
        void refreshCaptionExports();
    });
    document.getElementById('downloadTranscriptBtn')?.addEventListener('click', () => {
        document.getElementById('downloadTranscriptLabel').textContent = `Source: ${transcriptLabel()}`;
        document.getElementById('transcriptDownloadDialog').showModal();
    });
    document.getElementById('savedCaptionVideosBtn')?.addEventListener('click', () => {
        document.getElementById('savedCaptionVideosDialog').showModal();
        void refreshSavedCaptionVideos();
    });
    document.getElementById('refreshCaptionVideosBtn')?.addEventListener('click', () => void refreshSavedCaptionVideos());
    document.getElementById('captionStyleDialog')?.addEventListener('close', () => {
        ++previewGeneration;
        previewController?.abort();
        clearTimeout(previewTimer);
        clearCaptionPreview();
    });
    ['captionStylePreset', 'captionStyleSize', 'captionStylePosition'].forEach(id => {
        document.getElementById(id)?.addEventListener('change', scheduleCaptionPreview);
    });
    document.getElementById('refreshCaptionPreviewBtn')?.addEventListener('click', () => void refreshCaptionPreview());
    document.getElementById('downloadCaptionSrtBtn')?.addEventListener('click', () => void downloadCaptionFile('srt'));
    document.getElementById('downloadCaptionVttBtn')?.addEventListener('click', () => void downloadCaptionFile('vtt'));
    setInterval(() => {
        if (document.getElementById('savedCaptionVideosDialog')?.open) {
            void refreshSavedCaptionVideos();
        }
        if (document.getElementById('captionStyleDialog')?.open) {
            void refreshCaptionExports();
            void refreshSavedExports();
        }
    }, 3000);
}
