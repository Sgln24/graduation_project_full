import { state } from './main.js';
import { loadUploadedVideo } from './api-module.js';
import { showError, showNotification } from './ui.js';
import { contentLanguage } from './content-languages.js';
import { saveBlobToUserLocation } from './utils.js';
import { selectedMediaExportId, selectedCaptionTrackIndex } from './caption-export.js';

const storageKey = 'lectureseek-chapter-project-job';
let pending = null;
let timer = null;
let checking = false;
let starting = false;

function statusElements() {
    return {
        status: document.getElementById('chapterProjectStatus'),
        open: document.getElementById('openChapterProjectBtn'),
    };
}

function savePending() {
    if (pending) localStorage.setItem(storageKey, JSON.stringify(pending));
    else localStorage.removeItem(storageKey);
}

function render(message, project = null) {
    const {status, open} = statusElements();
    status.hidden = false;
    status.textContent = message;
    open.hidden = !project;
    open.onclick = project ? () => void openProject(project) : null;
    const cancel = document.getElementById('cancelChapterProjectBtn');
    if (cancel) cancel.hidden = !pending || Boolean(pending.ready);
}

async function openProject(project) {
    const select = document.getElementById('uploadedVideos');
    if (select) {
        let option = Array.from(select.options).find(item => item.value === project.video_id);
        if (!option) {
            option = document.createElement('option');
            option.value = project.video_id;
            option.textContent = project.filename;
            option.dataset.filename = project.filename;
            select.appendChild(option);
        }
        select.value = project.video_id;
    }
    await loadUploadedVideo(project.video_id);
    if (state.currentVideoId !== project.video_id) return;
    pending = null;
    savePending();
    statusElements().status.hidden = true;
    statusElements().open.hidden = true;
    showNotification('Opened an independent chapter project. The original project is unchanged.', 'success');
}

async function checkJob() {
    if (!pending || checking) return;
    checking = true;
    try {
        const response = await fetch(`/chapter_project_jobs/${encodeURIComponent(pending.jobId)}`);
        const job = await response.json();
        if (!response.ok) throw new Error(job.detail || `Could not check chapter project (${response.status})`);
        if (job.status === 'complete') {
            pending.ready = true;
            savePending();
            render(`Chapter project ready: ${job.project.filename} (${job.project.slides} slides).`, job.project);
            if (state.currentVideoId === pending.parentId) await openProject(job.project);
            return;
        }
        if (job.status === 'error') {
            pending = null;
            savePending();
            throw new Error(job.error || 'Chapter project creation failed.');
        }
        if (job.status === 'cancelled') {
            pending = null;
            savePending();
            render('Chapter project cancelled. The original lecture is unchanged.');
            return;
        }
        const step = job.steps?.at(-1)?.message || 'Preparing chapter project';
        render(`${step} (${Math.floor(job.elapsed || 0)}s elapsed)`);
        timer = setTimeout(() => void checkJob(), 2000);
    } catch (error) {
        render(error.message);
        showError(error.message);
        if (pending) timer = setTimeout(() => void checkJob(), 5000);
    } finally {
        checking = false;
    }
}

export function createChapterProjectButton(chapter, index, chapters) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn btn-secondary btn-action chapter-project-button';
    button.textContent = 'Open as project';
    button.title = 'Create a separate project with this chapter video, transcript and available slides';
    button.addEventListener('click', async event => {
        event.stopPropagation();
        if (button.closest('.is-inline-editing')) {
            showError('Save or cancel your edits before opening a chapter project.');
            return;
        }
        if ((pending && !pending.ready) || starting) {
            showNotification('A chapter project is already being created. Its progress is shown above.', 'info');
            return;
        }
        const parentId = state.currentVideoId;
        const snapshot = {
            chapter_index: index, timestamp: chapter.timestamp, title: chapter.title,
            next_timestamp: chapters[index + 1]?.timestamp || null,
            language: state.currentTranslationLanguage || '',
            chapter_language: contentLanguage(parentId, 'chapters', state.currentTranslationLanguage),
            source: state.currentTranscriptSource === 'uploaded' ? 'uploaded'
                : state.currentTranscriptSource === 'youtube' ? 'youtube' : 'whisper',
            transcript_version_id: state.currentTranscriptVersionId || '',
            media_export_id: selectedMediaExportId(parentId),
            caption_track_index: selectedCaptionTrackIndex(),
        };
        button.disabled = true;
        starting = true;
        pending = null;
        try {
            if (!parentId) throw new Error('Load a video before opening a chapter project.');
            render('Creating chapter project. Video encoding may take some time; the original stays unchanged.');
            const response = await fetch(`/chapter_projects/${encodeURIComponent(parentId)}`, {
                method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(snapshot),
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.detail || `Could not create chapter project (${response.status})`);
            pending = {jobId: data.job_id, parentId};
            savePending();
            void checkJob();
        } catch (error) {
            render(error.message);
            showError(error.message);
        } finally {
            starting = false;
            button.disabled = false;
        }
    });
    return button;
}

export function initChapterProjects() {
    document.getElementById('cancelChapterProjectBtn')?.addEventListener('click', async event => {
        if (!pending || pending.ready) return;
        const button = event.currentTarget;
        button.disabled = true;
        try {
            const response = await fetch(`/chapter_project_jobs/${encodeURIComponent(pending.jobId)}/cancel`, {method: 'POST'});
            const data = await response.json();
            if (!response.ok) throw new Error(data.detail || 'Could not cancel chapter project.');
            render('Stopping chapter encoding and removing incomplete output...');
        } catch (error) {
            showError(error.message);
        } finally { button.disabled = false; }
    });
    const saved = localStorage.getItem(storageKey);
    if (!saved) return;
    try {
        const value = JSON.parse(saved);
        if (typeof value.jobId !== 'string' || typeof value.parentId !== 'string') {
            throw new Error('Saved chapter project progress is invalid.');
        }
        pending = value;
        if (timer) clearTimeout(timer);
        void checkJob();
    } catch (error) {
        localStorage.removeItem(storageKey);
        showError(`Could not restore chapter project progress: ${error.message}`);
    }
}

export function createChapterDownloadButton(chapter, index, chapters) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn btn-secondary btn-action btn-icon chapter-video-download';
    button.innerHTML = '<i class="fas fa-download" aria-hidden="true"></i>';
    button.title = 'Download chapter video';
    button.setAttribute('aria-label', 'Download chapter video');
    button.addEventListener('click', async event => {
        event.stopPropagation();
        if (button.closest('.is-inline-editing')) {
            showError('Save or cancel your edits before downloading a chapter.');
            return;
        }
        button.disabled = true;
        const videoId = state.currentVideoId;
        const status = document.getElementById('chapterProjectStatus');
        try {
            const response = await fetch(`/chapter_video/${encodeURIComponent(videoId)}`, {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    chapter_index: index, title: chapter.title, timestamp: chapter.timestamp,
                    next_timestamp: chapters[index + 1]?.timestamp || null,
                    chapter_language: contentLanguage(videoId, 'chapters', state.currentTranslationLanguage),
                    language: state.currentTranslationLanguage || '',
                    source: state.currentTranscriptSource === 'uploaded' ? 'uploaded'
                        : state.currentTranscriptSource === 'youtube' ? 'youtube' : 'whisper',
                    transcript_version_id: state.currentTranscriptVersionId || '',
                    media_export_id: selectedMediaExportId(videoId),
                    caption_track_index: selectedCaptionTrackIndex(),
                }),
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.detail || 'Could not prepare chapter video.');
            while (true) {
                const response = await fetch(`/chapter_video_jobs/${encodeURIComponent(data.job_id)}`);
                const job = await response.json();
                if (!response.ok || job.status === 'error') throw new Error(job.detail || job.error || 'Chapter video encoding failed.');
                if (state.currentVideoId === videoId) {
                    status.hidden = false;
                    status.textContent = job.steps?.at(-1)?.message || 'Preparing chapter video...';
                }
                if (job.status === 'complete') {
                    const download = await fetch(job.download_url);
                    if (!download.ok) throw new Error('Could not download the chapter video.');
                    await saveBlobToUserLocation(await download.blob(), job.filename);
                    if (state.currentVideoId === videoId) status.textContent = 'Chapter video ready.';
                    break;
                }
                await new Promise(resolve => setTimeout(resolve, 1500));
            }
        } catch (error) {
            if (error.name !== 'AbortError') showError(error.message);
        } finally { button.disabled = false; }
    });
    return button;
}
