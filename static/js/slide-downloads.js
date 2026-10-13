import { state } from './main.js';
import { showError } from './ui.js';
import { saveBlobToUserLocation } from './utils.js';
import { startElapsedTimer } from './progress-time.js';

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
                `${videoId}_slide_${indices[0] + 1}.jpg`, button);
        } else {
            const safeLabel = label.replace(/^Download /, '').replace(/[^a-z0-9_-]+/gi, '_').slice(0, 80);
            void saveSlideExport(`/download_slides/${base}`, `${videoId}_${safeLabel}.zip`, button, {
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
    const query = state.currentTranslationLanguage
        ? `?transcript_language=${encodeURIComponent(state.currentTranslationLanguage)}` : '';
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
