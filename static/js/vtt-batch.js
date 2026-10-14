import { openOptionsDialog, saveBlobToUserLocation } from './utils.js';
import { startElapsedTimer } from './progress-time.js';
import { showError } from './ui.js';

let working = false;
let downloading = false;
let savedBatches = [];
let historyKey = null;
const element = id => document.getElementById(id);

function updateBatchDetails(job) {
    const details = element('vttBatchDetails');
    if (details) details.textContent = job?.steps?.find(step => step.message.startsWith('Translation result:'))?.message || '';
}

async function readResponse(response) {
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
    return data;
}

export async function refreshVttBatches() {
    const select = element('vttBatchSaved');
    const status = element('vttBatchHistoryStatus');
    try {
        const data = await readResponse(await fetch('/saved_exports/vtt-batch'));
        savedBatches = data.exports.filter(job => job.status === 'complete');
        const key = JSON.stringify(savedBatches.map(job => [job.id, job.filename, job.created_at]));
        if (key !== historyKey) {
            const selected = select.value;
            select.replaceChildren();
            const placeholder = document.createElement('option');
            placeholder.value = '';
            placeholder.textContent = 'Choose a completed batch';
            select.append(placeholder);
            for (const job of savedBatches) {
                const option = document.createElement('option');
                option.value = job.id;
                option.textContent = `${job.title} - ${new Date(job.created_at * 1000).toLocaleString()}`;
                select.append(option);
            }
            select.value = savedBatches.some(job => job.id === selected) ? selected : savedBatches[0]?.id || '';
            historyKey = key;
        }
        element('downloadVttBatchBtn').disabled = downloading || !select.value;
        status.textContent = savedBatches.length ? '' : 'No completed batches yet.';
        const interrupted = data.exports.find(job => job.status === 'error');
        if (!working && interrupted) status.textContent = `Previous batch failed: ${interrupted.error}`;
        if (!working) updateBatchDetails(savedBatches.find(job => job.id === select.value));
    } catch (error) {
        status.textContent = `Could not load saved batches: ${error.message}`;
    }
}

export async function translateVttFiles() {
    if (working) return;
    if (element('translationModel').dataset?.available === 'false') {
        showError(element('translationModelStatus')?.textContent || 'No translation provider is available.');
        return;
    }
    const files = Array.from(element('vttBatchFiles').files || []);
    if (!files.length || files.length > 30) {
        showError('Choose between 1 and 30 VTT files.');
        return;
    }
    if (files.some(file => !file.name.toLowerCase().endsWith('.vtt'))) {
        showError('Only .vtt files are supported.');
        return;
    }
    if (files.some(file => file.size > 5 * 1024 * 1024)
        || files.reduce((sum, file) => sum + file.size, 0) > 20 * 1024 * 1024) {
        showError('Limit: 5 MB per file and 20 MB per batch.');
        return;
    }
    const allowFallback = element('vttBatchFallback')?.checked ?? true;
    if (!allowFallback && !element('vttBatchModel').value) {
        showError('Choose a model when automatic fallback is disabled.');
        return;
    }
    if (element('vttBatchModel').selectedOptions?.[0]?.disabled) {
        showError('The selected translation model is unavailable. Choose another model.');
        return;
    }
    working = true;
    const button = element('translateVttBatchBtn');
    button.disabled = true;
    const container = element('vttBatchProgress');
    container.replaceChildren();
    updateBatchDetails(null);
    const message = document.createElement('p');
    message.textContent = 'Uploading VTT files...';
    container.append(message);
    const stopElapsed = startElapsedTimer(container);
    try {
        const form = new FormData();
        files.forEach(file => form.append('files', file));
        form.append('target_language', element('vttBatchTarget').value);
        form.append('model', element('vttBatchModel').value);
        form.append('allow_fallback', String(allowFallback));
        const result = await readResponse(await fetch('/vtt_translation_jobs', {method: 'POST', body: form}));
        localStorage.setItem('activeVttBatchJob', result.job_id);
        await watchVttBatch(result.job_id, message);
    } catch (error) {
        message.textContent = `Could not translate VTT files: ${error.message}`;
        showError(message.textContent);
    } finally {
        stopElapsed();
        working = false;
        button.disabled = false;
    }
}

async function watchVttBatch(jobId, message) {
    const container = element('vttBatchProgress');
    let stopElapsed;
    let completed = false;
    try {
        let status;
        do {
            status = await readResponse(await fetch(`/export_jobs/${encodeURIComponent(jobId)}/status`));
            if (!stopElapsed) stopElapsed = startElapsedTimer(container, status.elapsed || 0);
            message.textContent = status.steps?.at(-1)?.message || 'Preparing translation...';
            if (status.status === 'error') {
                localStorage.removeItem('activeVttBatchJob');
                throw new Error(status.error);
            }
            if (status.status !== 'complete') await new Promise(resolve => setTimeout(resolve, 1500));
        } while (status.status !== 'complete');
        localStorage.removeItem('activeVttBatchJob');
        stopElapsed();
        stopElapsed = startElapsedTimer(container, status.elapsed || 0, false);
        completed = true;
        message.textContent = 'Translation ready. Download your ZIP below.';
        updateBatchDetails(status);
        await refreshVttBatches();
        element('vttBatchSaved').value = jobId;
        element('downloadVttBatchBtn').disabled = !element('vttBatchSaved').value;
    } finally {
        // Keep the final duration visible, but stop live timers after errors.
        if (!completed) stopElapsed?.();
    }
}

async function downloadVttBatch() {
    if (downloading) return;
    const job = savedBatches.find(item => item.id === element('vttBatchSaved').value);
    if (!job) {
        showError('Choose a completed translation batch.');
        return;
    }
    downloading = true;
    const button = element('downloadVttBatchBtn');
    const status = element('vttBatchHistoryStatus');
    button.disabled = true;
    status.textContent = 'Downloading ZIP...';
    const stopElapsed = startElapsedTimer(status);
    try {
        const response = await fetch(job.download_url);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        await saveBlobToUserLocation(await response.blob(), job.filename, element('vttBatchDialog'));
        status.textContent = 'ZIP sent to your save dialog or browser downloads.';
    } catch (error) {
        const message = error.name === 'TypeError' && /fetch|network/i.test(error.message)
            ? 'Cannot reach the server. Your saved batch is kept; retry when the app is available.'
            : error.message;
        status.textContent = error.name === 'AbortError' ? 'Save cancelled.' : `Could not download ZIP: ${message}`;
        if (error.name !== 'AbortError') showError(status.textContent);
    } finally {
        stopElapsed();
        downloading = false;
        button.disabled = false;
    }
}

export function initVttBatch(refreshModels) {
    element('openVttBatchBtn').addEventListener('click', async () => {
        const model = element('vttBatchModel');
        const selectedModel = model.value;
        const menu = element('extraOptionsMenu');
        if (menu) menu.open = false;
        openOptionsDialog(element('vttBatchDialog'));
        element('translateVttBatchBtn').disabled = true;
        try {
            await refreshModels?.();
        } finally {
            element('translateVttBatchBtn').disabled = working;
        }
        model.replaceChildren(...Array.from(element('translationModel').children, child => child.cloneNode(true)));
        model.value = selectedModel;
        if (model.selectedOptions?.[0]?.disabled) model.value = '';
        const status = element('vttBatchModelStatus');
        if (status) status.textContent = element('translationModel').dataset?.available === 'false'
            ? element('translationModelStatus')?.textContent || 'No translation provider is available.' : '';
        await refreshVttBatches();
        const jobId = localStorage.getItem('activeVttBatchJob');
        if (!jobId || working) return;
        working = true;
        element('translateVttBatchBtn').disabled = true;
        const container = element('vttBatchProgress');
        container.replaceChildren();
        const message = document.createElement('p');
        container.append(message);
        try {
            await watchVttBatch(jobId, message);
        } catch (error) {
            message.textContent = `Could not resume translation: ${error.message}`;
            showError(message.textContent);
        } finally {
            working = false;
            element('translateVttBatchBtn').disabled = false;
        }
    });
    element('translateVttBatchBtn').addEventListener('click', () => void translateVttFiles());
    element('downloadVttBatchBtn').addEventListener('click', () => void downloadVttBatch());
    element('vttBatchSaved').addEventListener('change', () => {
        element('downloadVttBatchBtn').disabled = downloading || !element('vttBatchSaved').value;
        updateBatchDetails(savedBatches.find(job => job.id === element('vttBatchSaved').value));
    });
}
