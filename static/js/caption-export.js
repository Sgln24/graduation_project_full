import { state } from './main.js';
import { refreshSavedExports } from './chapters.js';
import { showError } from './ui.js';
import { saveBlobToUserLocation, openOptionsDialog } from './utils.js';
import { startElapsedTimer } from './progress-time.js';

let submitting = false;
let refreshGeneration = 0;
let savedVideosGeneration = 0;
let savedCaptionVideos = [];
let savedCaptionVideoId = null;
let savedCaptionOptionsKey = null;
let progressVideoId = null;
let downloading = false;
const currentCaptionJobs = new Map();
const renderedJobs = new WeakMap();
let previewGeneration = 0;
let previewController = null;
let previewUrl = null;
let previewTimer = null;
let switchingMedia = false;
let visibleCopyTrack = null;

function selectedCaptionStyle() {
    return {
        preset: document.getElementById('captionStylePreset')?.value || 'box',
        size: document.getElementById('captionStyleSize')?.value || 'medium',
        position: document.getElementById('captionStylePosition')?.value || 'bottom',
    };
}

export function selectedMediaExportId(videoId) {
    const player = document.querySelector('video');
    const id = player?.dataset.captionExportId;
    return id && player.dataset.captionVideoId === videoId
        && player.getAttribute('src') === `/caption_copy/${encodeURIComponent(videoId)}/${id}` ? id : null;
}

export function selectedCaptionTrackIndex() {
    const player = document.querySelector('video');
    const index = Array.from(player?.textTracks || [])
        .filter(track => track.kind === 'subtitles' || track.kind === 'captions')
        .findIndex(track => track.mode === 'showing');
    return index < 0 ? null : index;
}

function clearCopyCaptions(player) {
    player.querySelectorAll('track[data-caption-copy], track[data-chapter-caption]').forEach(element => {
        // Chromium can keep drawing active cues from a detached, showing track.
        element.track.mode = 'disabled';
        element.default = false;
        element.remove();
    });
    visibleCopyTrack = null;
}

export async function loadProjectCaptionTracks(videoId, player) {
    const response = await fetch(`/chapter_captions/${encodeURIComponent(videoId)}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Could not load chapter captions.');
    if (state.currentVideoId !== videoId) return;
    clearCopyCaptions(player);
    const chosen = Math.max(0, (data.tracks || []).findIndex(track => track.default));
    for (const [index, item] of (data.tracks || []).entries()) {
        const track = document.createElement('track');
        track.kind = 'subtitles';
        track.label = item.label;
        track.srclang = ({eng: 'en', deu: 'de', ger: 'de', ara: 'ar', pol: 'pl'})[item.language] || item.language;
        track.src = item.url;
        track.dataset.chapterCaption = '';
        track.default = index === chosen;
        player.appendChild(track);
        track.track.mode = index === chosen ? 'showing' : 'disabled';
    }
}

function syncCaptionVisibility(player) {
    if (!player.getAttribute('src')?.startsWith('/caption_copy/')) return;
    const showing = Array.from(player.textTracks).filter(track =>
        (track.kind === 'subtitles' || track.kind === 'captions') && track.mode === 'showing');
    // A browser may restore its remembered language as well as the default track.
    const selected = showing.includes(visibleCopyTrack) ? visibleCopyTrack : showing[0];
    for (const track of showing) {
        if (track !== selected) track.mode = 'disabled';
    }
    visibleCopyTrack = selected || null;
}

async function useVideoCopy(job = null) {
    if (switchingMedia) return;
    const videoId = state.currentVideoId;
    const player = document.querySelector('video');
    const status = document.getElementById('captionMediaStatus');
    if (!player || !videoId) {
        showError('Load a video first.');
        return;
    }
    const button = document.getElementById(job ? 'useCaptionCopyBtn' : 'restoreOriginalVideoBtn');
    switchingMedia = true;
    button.disabled = true;
    status.textContent = 'Loading video source...';
    const stopElapsed = startElapsedTimer(status);
    const previousSource = player.getAttribute('src');
    const at = player.currentTime;
    const playbackRate = player.playbackRate;
    const wasPlaying = !player.paused;
    const previousTracks = Array.from(player.querySelectorAll('track[data-caption-copy], track[data-chapter-caption]'),
        element => ({element, mode: element.track.mode, default: element.default}));
    let sourceChanged = false;
    try {
        if (job) {
            const saved = await readResponse(await fetch(`/export_jobs/${job.id}/status`));
            if (saved.video_id !== videoId || saved.status !== 'complete'
                || !saved.title.startsWith('Captioned video - ')) throw new Error('This copy is not available for the current video.');
            job = saved;
        }
        if (videoId !== state.currentVideoId) return;
        const src = job ? `/caption_copy/${encodeURIComponent(videoId)}/${job.id}` : `/video/${encodeURIComponent(videoId)}`;
        await new Promise((resolve, reject) => {
            const cleanup = () => {
                clearTimeout(timeout);
                player.removeEventListener('loadedmetadata', loaded);
                player.removeEventListener('error', failed);
            };
            const loaded = () => { cleanup(); resolve(); };
            const failed = () => { cleanup(); reject(new Error('The video copy could not be played.')); };
            const timeout = setTimeout(() => { cleanup(); reject(new Error('Video loading timed out.')); }, 30000);
            player.addEventListener('loadedmetadata', loaded);
            player.addEventListener('error', failed);
            clearCopyCaptions(player);
            player.src = src;
            sourceChanged = true;
            player.load();
        });
        if (videoId !== state.currentVideoId || player.getAttribute('src') !== src) return;
        clearCopyCaptions(player);
        const copyTracks = [];
        for (const [index, item] of (job?.caption_tracks || []).entries()) {
            const track = document.createElement('track');
            track.kind = 'subtitles';
            track.label = item.label;
            track.srclang = item.code || item.language;
            track.src = `${src}/tracks/${index}`;
            track.dataset.captionCopy = '';
            track.default = index === 0;
            player.append(track);
            copyTracks.push(track.track);
        }
        visibleCopyTrack = copyTracks[0] || null;
        copyTracks.forEach((track, index) => { track.mode = index === 0 ? 'showing' : 'disabled'; });
        syncCaptionVisibility(player);
        if (!job) await loadProjectCaptionTracks(videoId, player);
        player.dataset.captionExportId = job?.id || '';
        player.dataset.captionVideoId = videoId;
        player.currentTime = Math.min(at, Math.max(0, player.duration - 0.1));
        player.playbackRate = playbackRate;
        status.textContent = job ? `Editor source: ${job.filename}. Analysis kept.` : 'Editor source: original video.';
        if (wasPlaying) await player.play();
    } catch (error) {
        if (videoId !== state.currentVideoId) return;
        if (sourceChanged && !selectedMediaExportId(videoId)) {
            clearCopyCaptions(player);
            player.src = previousSource;
            player.load();
            visibleCopyTrack = previousTracks.find(previous => previous.mode === 'showing')?.element.track || null;
            for (const previous of previousTracks) {
                previous.element.default = previous.default;
                player.append(previous.element);
                previous.element.track.mode = previous.mode;
            }
        }
        status.textContent = `Could not switch video: ${error.message}`;
        showError(status.textContent);
    } finally {
        stopElapsed();
        switchingMedia = false;
        button.disabled = false;
        updateSavedCaptionSelection();
    }
}

function updateCaptionMode() {
    const tracks = document.getElementById('captionExportMode')?.value === 'tracks';
    document.getElementById('captionTrackOptions').hidden = !tracks;
    document.getElementById('captionBurnOptions').hidden = tracks;
    if (tracks) {
        ++previewGeneration;
        previewController?.abort();
        clearTimeout(previewTimer);
        clearCaptionPreview();
    } else if (document.getElementById('captionStyleDialog')?.open) {
        scheduleCaptionPreview();
    }
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
    if (document.getElementById('captionExportMode')?.value === 'tracks') return;
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
    const stopElapsed = startElapsedTimer(status);
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
        status.textContent = '';
    } catch (error) {
        if (error.name === 'AbortError' || generation !== previewGeneration
            || videoId !== state.currentVideoId || transcriptSource !== state.currentTranscript) return;
        status.textContent = `Could not render caption preview: ${error.message}`;
    } finally {
        stopElapsed();
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
    if (!job) {
        renderedJobs.get(container)?.stopElapsed();
        renderedJobs.delete(container);
        container.replaceChildren();
        return;
    }
    const text = job.status === 'complete' ? 'Your video copy is ready.'
        : job.status === 'error' ? `Export failed: ${job.error}`
        : job.steps?.at(-1)?.message || 'Preparing video copy...';
    const encoded = job.steps?.findLast(step => /(?:Burning captions|Adding caption tracks): \d+%/.test(step.message));
    const percent = encoded ? Number(encoded.message.match(/(\d+)%/)[1]) : null;
    const rendered = renderedJobs.get(container);
    if (rendered?.id === job.id && rendered.status === job.status && container.children[0] === rendered.message) {
        if (rendered.message.textContent !== text) rendered.message.textContent = text;
        if (rendered.progress && percent !== null) rendered.progress.value = percent;
        return;
    }
    rendered?.stopElapsed();
    container.replaceChildren();
    const message = document.createElement('p');
    message.textContent = text;
    container.append(message);
    let progress;
    if (job.status === 'queued' || job.status === 'running') {
        progress = document.createElement('progress');
        progress.max = 100;
        progress.setAttribute('aria-label', 'Captioned video generation progress');
        if (percent !== null) progress.value = percent;
        container.append(progress);
    } else if (job.status === 'complete') {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'btn btn-secondary btn-action';
        button.textContent = 'Download MP4';
        button.addEventListener('click', () => void downloadCaptionVideo(job, button, message));
        container.append(button);
        const useButton = document.createElement('button');
        useButton.type = 'button';
        useButton.className = 'btn btn-secondary btn-action';
        useButton.textContent = 'Use in editor';
        useButton.addEventListener('click', () => void useVideoCopy(job));
        container.append(useButton);
    }
    const elapsed = Number.isFinite(job.elapsed) ? job.elapsed
        : job.created_at ? ((job.finished_at || Date.now() / 1000) - job.created_at) : 0;
    const stopElapsed = startElapsedTimer(container, elapsed, job.status === 'queued' || job.status === 'running');
    renderedJobs.set(container, {id: job.id, status: job.status, message, progress, stopElapsed});
}

async function downloadCaptionVideo(job, button, status) {
    if (downloading) return;
    downloading = true;
    button.disabled = true;
    updateSavedCaptionSelection();
    status.textContent = 'Downloading video copy...';
    const stopElapsed = startElapsedTimer(status);
    try {
        const response = await fetch(job.download_url);
        if (!response.ok) {
            const data = await response.json();
            throw new Error(data.detail || `HTTP ${response.status}`);
        }
        const blob = await response.blob();
        const container = Array.from(document.querySelectorAll('dialog:modal')).at(-1) || document.body;
        await saveBlobToUserLocation(blob, job.filename, container);
        status.textContent = 'Video copy sent to browser downloads.';
    } catch (error) {
        status.textContent = error.name === 'AbortError' ? 'Save cancelled.' : `Could not download video copy: ${error.message}`;
        if (error.name !== 'AbortError') showError(status.textContent);
    } finally {
        stopElapsed();
        downloading = false;
        button.disabled = false;
        document.getElementById('downloadSavedCaptionVideoBtn').disabled = !savedCaptionVideos.some(
            item => item.id === document.getElementById('savedCaptionVideoSelect').value
                && item.video_id === state.currentVideoId && item.status === 'complete'
        );
    }
}

export async function refreshSavedCaptionVideos() {
    const videoId = state.currentVideoId;
    const generation = ++savedVideosGeneration;
    const select = document.getElementById('savedCaptionVideoSelect');
    const status = document.getElementById('savedCaptionVideosStatus');
    if (savedCaptionVideoId !== videoId) {
        savedCaptionVideoId = videoId;
        savedCaptionVideos = [];
        savedCaptionOptionsKey = null;
        select.replaceChildren();
        select.value = '';
        document.getElementById('downloadSavedCaptionVideoBtn').disabled = true;
        const mediaStatus = document.getElementById('captionMediaStatus');
        if (mediaStatus) mediaStatus.textContent = '';
    }
    if (!videoId) {
        status.textContent = 'Load a video to see its saved captioned copies.';
        return;
    }
    if (downloading) return;
    if (savedCaptionOptionsKey === null) status.textContent = 'Loading saved captioned videos...';
    try {
        const data = await readResponse(await fetch(`/saved_exports/${encodeURIComponent(videoId)}`));
        if (generation !== savedVideosGeneration || videoId !== state.currentVideoId) return;
        const selected = select.value;
        savedCaptionVideos = data.exports.filter(job => job.video_id === videoId && job.title.startsWith('Captioned video - '));
        const completed = savedCaptionVideos.filter(job => job.status === 'complete');
        const optionsKey = JSON.stringify(completed.map(job => [job.id, job.filename, job.title, job.created_at]));
        if (optionsKey !== savedCaptionOptionsKey) {
            select.replaceChildren();
            const placeholder = document.createElement('option');
            placeholder.value = '';
            placeholder.textContent = 'Saved captioned videos';
            select.append(placeholder);
            completed.forEach(job => {
                const option = document.createElement('option');
                option.value = job.id;
                const date = job.created_at ? ` - ${new Date(job.created_at * 1000).toLocaleString()}` : '';
                option.textContent = `${job.filename || job.title}${date}`;
                select.append(option);
            });
            select.value = completed.some(job => job.id === selected) ? selected : completed[0]?.id || '';
            savedCaptionOptionsKey = optionsKey;
        }
        updateSavedCaptionSelection();
        if (!completed.length) status.textContent = 'No completed captioned videos yet.';
    } catch (error) {
        if (generation === savedVideosGeneration && videoId === state.currentVideoId) {
            status.textContent = `Could not load saved captioned videos: ${error.message}`;
        }
    }
}

function updateSavedCaptionSelection() {
    const id = document.getElementById('savedCaptionVideoSelect').value;
    const job = savedCaptionVideos.find(item => item.id === id && item.video_id === state.currentVideoId);
    document.getElementById('downloadSavedCaptionVideoBtn').disabled = downloading || !job || job.status !== 'complete';
    const useButton = document.getElementById('useCaptionCopyBtn');
    if (useButton) useButton.disabled = switchingMedia || !job || job.status !== 'complete';
    if (downloading) return;
    document.getElementById('savedCaptionVideosStatus').textContent = !job ? ''
        : job.status === 'complete' ? ''
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
    if (progressVideoId !== videoId) {
        progressVideoId = videoId;
        renderCaptionJob(container, null);
    }
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
    const selectable = document.getElementById('captionExportMode')?.value === 'tracks';
    const versionIds = Array.from(document.getElementById('captionTrackVersions')?.selectedOptions || [], item => item.value);
    if (selectable && !versionIds.length) {
        showError('Choose at least one saved transcript for the caption tracks.');
        return;
    }
    const selector = document.getElementById('captionTranscriptVersionSelect');
    if (!selectable && selector?.disabled && selector.value) {
        showError('Wait for the selected transcript to finish loading before creating a video copy.');
        return;
    }
    const videoId = state.currentVideoId;
    if (!videoId || (!selectable && !state.currentTranscript?.length)) {
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
    progressVideoId = videoId;
    progress.textContent = 'Starting video copy...';
    const stopElapsed = startElapsedTimer(progress);
    try {
        const result = await readResponse(await fetch(`/${selectable ? 'caption_track_jobs' : 'caption_video_jobs'}/${encodeURIComponent(videoId)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                transcript, version_ids: versionIds, transcript_label: label, filename: state.currentVideoFilename || 'video',
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
        stopElapsed();
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
    const stopElapsed = startElapsedTimer(button);
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
        stopElapsed();
        button.disabled = false;
    }
}

export function initCaptionExport() {
    const player = document.querySelector('video');
    player?.textTracks?.addEventListener('change', () => syncCaptionVisibility(player));
    player?.addEventListener('loadstart', () => {
        if (!player.getAttribute('src')?.startsWith('/caption_copy/')) {
            clearCopyCaptions(player);
            delete player.dataset.captionExportId;
            delete player.dataset.captionVideoId;
        }
    });
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
    document.getElementById('captionExportMode')?.addEventListener('change', updateCaptionMode);
    document.getElementById('useCaptionCopyBtn')?.addEventListener('click', () => {
        const job = savedCaptionVideos.find(item => item.id === document.getElementById('savedCaptionVideoSelect').value
            && item.video_id === state.currentVideoId && item.status === 'complete');
        if (job) void useVideoCopy(job);
        else showError('Choose a completed video copy.');
    });
    document.getElementById('restoreOriginalVideoBtn')?.addEventListener('click', () => void useVideoCopy());
    document.getElementById('downloadSavedCaptionVideoBtn')?.addEventListener('click', () => {
        const job = savedCaptionVideos.find(item => item.id === document.getElementById('savedCaptionVideoSelect').value
            && item.video_id === state.currentVideoId);
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
        const progress = document.getElementById('captionVideoProgress');
        renderedJobs.get(progress)?.stopElapsed();
        renderedJobs.delete(progress);
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
            void refreshSavedCaptionVideos();
        }
    });
    document.getElementById('downloadCaptionSrtBtn')?.addEventListener('click', () => void downloadCaptionFile('srt'));
    document.getElementById('downloadCaptionVttBtn')?.addEventListener('click', () => void downloadCaptionFile('vtt'));
    setInterval(() => {
        if (document.getElementById('captionStyleDialog')?.open
            && (savedCaptionVideoId !== state.currentVideoId
                || savedCaptionVideos.some(job => job.status === 'queued' || job.status === 'running'))) {
            void refreshSavedCaptionVideos();
        }
        if (document.getElementById('captionStyleDialog')?.open) {
            void refreshCaptionExports();
            void refreshSavedExports();
        }
    }, 3000);
}
