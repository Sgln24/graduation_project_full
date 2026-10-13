import { state } from './main.js';
import { refreshSavedExports } from './chapters.js';
import { showError } from './ui.js';
import { saveBlobToUserLocation, openOptionsDialog } from './utils.js';

let submitting = false;
let refreshGeneration = 0;
let savedVideosGeneration = 0;
let savedCaptionVideos = [];
let downloading = false;
const currentCaptionJobs = new Map();
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
    const largeDialog = document.getElementById('captionLargePreviewDialog');
    if (largeDialog?.open) largeDialog.close();
    document.getElementById('captionLargePreviewImage')?.removeAttribute('src');
    const image = document.getElementById('captionStylePreview');
    if (image) {
        image.hidden = true;
        image.removeAttribute('src');
    }
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = null;
}

export async function refreshCaptionPreview() {
    clearTimeout(previewTimer);
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
    const selector = document.getElementById('captionStyleDialog')?.open
        ? document.getElementById('captionTranscriptVersionSelect')
        : document.getElementById('transcriptVersionSelect');
    const selected = selector?.selectedOptions[0];
    return selected?.value ? selected.textContent : state.currentTranscriptSource || 'Current transcript';
}

async function readResponse(response) {
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
    return data;
}

function renderCaptionJob(container, job) {
    container.replaceChildren();
    if (!job) return;
    const message = document.createElement('p');
    message.textContent = job.status === 'complete' ? 'Your video copy is ready.'
        : job.status === 'error' ? `Export failed: ${job.error}`
        : job.steps?.at(-1)?.message || 'Preparing video copy...';
    container.append(message);
    if (job.status === 'queued' || job.status === 'running') {
        const progress = document.createElement('progress');
        progress.max = 100;
        progress.setAttribute('aria-label', 'Captioned video generation progress');
        const encoded = job.steps?.findLast(step => /Burning captions: \d+%/.test(step.message));
        if (encoded) progress.value = Number(encoded.message.match(/(\d+)%/)[1]);
        container.append(progress);
    } else if (job.status === 'complete') {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'btn btn-secondary btn-action';
        button.textContent = 'Download MP4';
        button.addEventListener('click', () => void downloadCaptionVideo(job, button, message));
        container.append(button);
    }
}

async function downloadCaptionVideo(job, button, status) {
    if (downloading) return;
    downloading = true;
    button.disabled = true;
    updateSavedCaptionSelection();
    status.textContent = 'Downloading video copy...';
    try {
        const response = await fetch(job.download_url);
        if (!response.ok) {
            const data = await response.json();
            throw new Error(data.detail || `HTTP ${response.status}`);
        }
        const blob = await response.blob();
        const container = Array.from(document.querySelectorAll('dialog:modal')).at(-1) || document.body;
        await saveBlobToUserLocation(blob, job.filename, container);
        status.textContent = 'Video copy sent to your save dialog or browser downloads.';
    } catch (error) {
        status.textContent = error.name === 'AbortError' ? 'Save cancelled.' : `Could not download video copy: ${error.message}`;
        if (error.name !== 'AbortError') showError(status.textContent);
    } finally {
        downloading = false;
        button.disabled = false;
        document.getElementById('downloadSavedCaptionVideoBtn').disabled = !savedCaptionVideos.some(
            item => item.id === document.getElementById('savedCaptionVideoSelect').value && item.status === 'complete'
        );
    }
}

export async function refreshSavedCaptionVideos() {
    if (downloading) return;
    const generation = ++savedVideosGeneration;
    const select = document.getElementById('savedCaptionVideoSelect');
    const status = document.getElementById('savedCaptionVideosStatus');
    status.textContent = 'Loading saved captioned videos...';
    try {
        const data = await readResponse(await fetch('/saved_caption_videos'));
        if (generation !== savedVideosGeneration) return;
        const selected = select.value;
        savedCaptionVideos = data.exports;
        select.replaceChildren();
        const placeholder = document.createElement('option');
        placeholder.value = '';
        placeholder.textContent = 'Saved captioned videos';
        select.append(placeholder);
        const completed = savedCaptionVideos.filter(job => job.status === 'complete');
        completed.forEach(job => {
            const option = document.createElement('option');
            option.value = job.id;
            const date = job.created_at ? ` - ${new Date(job.created_at * 1000).toLocaleString()}` : '';
            option.textContent = `${job.filename || job.title}${date}`;
            select.append(option);
        });
        select.value = completed.some(job => job.id === selected) ? selected : completed[0]?.id || '';
        updateSavedCaptionSelection();
        if (!completed.length) status.textContent = 'No completed captioned videos yet. Unfinished copies are not available to download.';
    } catch (error) {
        if (generation === savedVideosGeneration) {
            status.textContent = `Could not load saved captioned videos: ${error.message}`;
        }
    }
}

function updateSavedCaptionSelection() {
    const id = document.getElementById('savedCaptionVideoSelect').value;
    const job = savedCaptionVideos.find(item => item.id === id);
    document.getElementById('downloadSavedCaptionVideoBtn').disabled = downloading || !job || job.status !== 'complete';
    if (downloading) return;
    document.getElementById('savedCaptionVideosStatus').textContent = !job ? ''
        : job.status === 'complete' ? job.title
        : job.status === 'error' ? `Export failed: ${job.error}`
        : job.steps?.at(-1)?.message || job.status;
}

export async function refreshCaptionExports() {
    if (downloading) return;
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
        const tracked = currentCaptionJobs.get(videoId);
        const job = (tracked && jobs.find(item => item.id === tracked))
            || jobs.find(item => item.status === 'queued' || item.status === 'running');
        if (job) currentCaptionJobs.set(videoId, job.id);
        renderCaptionJob(container, job);
        if (job?.status === 'complete' && !savedCaptionVideos.some(item => item.id === job.id && item.status === 'complete')) {
            await refreshSavedCaptionVideos();
        }
    } catch (error) {
        if (generation !== refreshGeneration || videoId !== state.currentVideoId) return;
        container.textContent = `Could not load captioned-video exports: ${error.message}`;
    }
}

export async function exportCaptionedVideo() {
    if (submitting) return;
    const selector = document.getElementById('captionTranscriptVersionSelect');
    if (selector?.disabled && selector.value) {
        showError('Wait for the selected transcript to finish loading before creating a video copy.');
        return;
    }
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
    ++refreshGeneration;
    const progress = document.getElementById('captionVideoProgress');
    progress.replaceChildren();
    progress.textContent = 'Starting video copy...';
    try {
        const result = await readResponse(await fetch(`/caption_video_jobs/${encodeURIComponent(videoId)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                transcript, transcript_label: label, filename: state.currentVideoFilename || 'video',
                style: selectedCaptionStyle(),
                caption_language: state.currentTranslationLanguage
                    || (state.currentTranscriptSource?.startsWith('translation:') ? state.currentTranscriptSource.slice('translation:'.length) : ''),
            }),
        }));
        currentCaptionJobs.set(videoId, result.job_id);
        if (videoId === state.currentVideoId) await refreshCaptionExports();
        if (videoId === state.currentVideoId) await refreshSavedCaptionVideos();
    } catch (error) {
        if (videoId === state.currentVideoId) {
            progress.textContent = `Could not start captioned-video export: ${error.message}`;
            showError(progress.textContent);
        }
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
        openOptionsDialog(document.getElementById('captionStyleDialog'));
        void refreshCaptionPreview();
        void refreshCaptionExports();
        void refreshSavedCaptionVideos();
    });
    document.getElementById('downloadTranscriptBtn')?.addEventListener('click', () => {
        document.getElementById('downloadTranscriptLabel').textContent = `Source: ${transcriptLabel()}`;
        openOptionsDialog(document.getElementById('transcriptDownloadDialog'));
    });
    document.getElementById('savedCaptionVideoSelect')?.addEventListener('change', updateSavedCaptionSelection);
    document.getElementById('downloadSavedCaptionVideoBtn')?.addEventListener('click', () => {
        const job = savedCaptionVideos.find(item => item.id === document.getElementById('savedCaptionVideoSelect').value);
        if (!job || job.status !== 'complete') {
            showError('Choose a completed captioned video to download.');
            return;
        }
        return downloadCaptionVideo(job, document.getElementById('downloadSavedCaptionVideoBtn'),
            document.getElementById('savedCaptionVideosStatus'));
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
    document.getElementById('refreshCaptionPreviewBtn')?.addEventListener('click', async () => {
        await refreshCaptionPreview();
        if (!previewUrl || !document.getElementById('captionStyleDialog').open) return;
        document.getElementById('captionLargePreviewImage').src = previewUrl;
        const dialog = document.getElementById('captionLargePreviewDialog');
        if (!dialog.open) dialog.showModal();
    });
    document.addEventListener('transcriptUpdated', () => {
        if (document.getElementById('captionStyleDialog')?.open) {
            scheduleCaptionPreview();
            void refreshCaptionExports();
        }
    });
    document.getElementById('downloadCaptionSrtBtn')?.addEventListener('click', () => void downloadCaptionFile('srt'));
    document.getElementById('downloadCaptionVttBtn')?.addEventListener('click', () => void downloadCaptionFile('vtt'));
    setInterval(() => {
        if (document.getElementById('captionStyleDialog')?.open
            && savedCaptionVideos.some(job => job.status === 'queued' || job.status === 'running')) {
            void refreshSavedCaptionVideos();
        }
        if (document.getElementById('captionStyleDialog')?.open) {
            void refreshCaptionExports();
            void refreshSavedExports();
        }
    }, 3000);
}
