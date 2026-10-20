import { state } from './main.js';
import { showError } from './ui.js';
import { saveBlobToUserLocation } from './utils.js';
import { startElapsedTimer } from './progress-time.js';
import { contentLanguage } from './content-languages.js';
import { slideTitle, sceneTopicTitle } from './chapter-navigation.js';

function shortFilename(value) {
    return String(value || '').replace(/[<>:"/\\|?*\x00-\x1f]/g, '_')
        .replace(/\s+/g, '_').replace(/^[._-]+|[._-]+$/g, '').slice(0, 60);
}

export function slideDownloadFilename(index) {
    const scene = state.videoScenes?.[index] || {};
    const title = slideTitle(scene, contentLanguage(state.currentVideoId, 'slides', state.currentTranslationLanguage))
        || sceneTopicTitle(state.videoChapters || [], Number(scene.time_seconds));
    const description = shortFilename(title);
    return `Slide_${String(index + 1).padStart(3, '0')}${description ? '_' + description : ''}.jpg`;
}

export function initFrameScreenshot() {
    const button = document.getElementById('takeScreenshotBtn');
    const player = document.getElementById('videoPlayer');
    const refresh = () => { button.disabled = player.readyState < 2 || !player.videoWidth; };
    for (const event of ['loadeddata', 'canplay', 'emptied']) player.addEventListener(event, refresh);
    refresh();
    button.addEventListener('click', async () => {
        button.disabled = true;
        try {
            if (player.readyState < 2 || !player.videoWidth) throw new Error('Wait for a video frame to load.');
            const time = player.currentTime;
            const clock = [Math.floor(time / 3600), Math.floor(time / 60) % 60, Math.floor(time) % 60]
                .map(value => String(value).padStart(2, '0')).join('-');
            const title = shortFilename(sceneTopicTitle(state.videoChapters || [], time));
            const canvas = document.createElement('canvas');
            canvas.width = player.videoWidth;
            canvas.height = player.videoHeight;
            const context = canvas.getContext('2d');
            if (!context) throw new Error('Screenshot capture is unavailable in this browser.');
            context.drawImage(player, 0, 0);
            const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/png'));
            if (!blob) throw new Error('Could not create a screenshot from this video frame.');
            await saveBlobToUserLocation(blob, `Screenshot_${clock}${title ? '_' + title : ''}.png`);
        } catch (error) {
            if (error.name !== 'AbortError') showError(`Could not take screenshot: ${error.message}`);
        } finally { refresh(); }
    });
}

export function screenshotArchiveFilename() {
    const stem = (state.currentVideoFilename || 'lecture').replace(/\.[^.]+$/, '')
        .normalize('NFKD').replace(/[^A-Za-z0-9._-]+/g, '_')
        .replace(/^[._-]+|[._-]+$/g, '').slice(0, 80) || 'lecture';
    return `${stem}_screenshots.zip`;
}

async function saveSlideExport(url, filename, button, options) {
    const content = button.innerHTML;
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    const stopElapsed = startElapsedTimer(button);
    try {
        const response = await fetch(url, options);
        if (!response.ok) {
            const error = await response.json().catch(() => ({}));
            throw new Error(error.detail || `Slide export failed (${response.status})`);
        }
        if (filename.endsWith('.zip')) {
            const disposition = response.headers?.get('content-disposition') || '';
            const match = disposition.match(/filename="?([^";]+)"?/i);
            if (match) filename = match[1];
        }
        await saveBlobToUserLocation(await response.blob(), filename);
    } catch (error) {
        if (error.name !== 'AbortError') showError(`Could not download slides: ${error.message}`);
    } finally {
        stopElapsed();
        button.innerHTML = content;
        button.disabled = false;
        button.removeAttribute('aria-busy');
    }
}

export function createSlideDownloadButton(indices, label, {archive = false, videoId = state.currentVideoId} = {}) {
    const archiveFilename = screenshotArchiveFilename();
    const singleFilename = slideDownloadFilename(indices[0]);
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn btn-secondary btn-action btn-icon slide-download';
    button.innerHTML = '<i class="fas fa-download" aria-hidden="true"></i>';
    button.title = label;
    button.setAttribute('aria-label', label);
    button.disabled = !videoId || !indices.length;
    button.addEventListener('keydown', (event) => event.stopPropagation());
    button.addEventListener('dblclick', (event) => {
        event.stopPropagation();
        event.preventDefault();
    });
    button.addEventListener('click', (event) => {
        event.stopPropagation();
        event.preventDefault();
        if (!videoId || !indices.length || button.disabled) return;
        const base = encodeURIComponent(videoId);
        if (indices.length === 1 && !archive) {
            void saveSlideExport(`/download_slide/${base}/${indices[0]}`,
                singleFilename, button);
        } else {
            void saveSlideExport(`/download_slides/${base}`, archiveFilename, button, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({indices}),
            });
        }
    });
    return button;
}

export async function downloadSlideTextWord() {
    const button = document.getElementById('downloadSlideTextWordBtn');
    const videoId = state.currentVideoId;
    if (!videoId || !button || button.disabled) return;
    const content = button.innerHTML;
    button.textContent = 'Reading all slides and building Word document…';
    const language = contentLanguage(videoId, 'chapters', state.currentTranslationLanguage);
    const query = language ? `?chapter_language=${encodeURIComponent(language)}` : '';
    await saveSlideExport(`/download_slide_text_docx/${encodeURIComponent(videoId)}${query}`,
        `${videoId}_slide_text.docx`, button);
    button.innerHTML = content;
    button.disabled = !state.videoScenes?.length;
}

export function setupSlideTextDownload(slideCount) {
    const button = document.getElementById('downloadSlideTextWordBtn');
    if (!button) return;
    if (!button.dataset.downloadBound) {
        button.addEventListener('click', downloadSlideTextWord);
        button.dataset.downloadBound = 'true';
    }
    if (button.getAttribute('aria-busy') !== 'true') button.disabled = !slideCount;
}
