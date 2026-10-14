// api.js - API interaction functionality

import { elements } from './elements.js';
import { state } from './main.js';
import { extractVideoId } from './utils.js';
import { showError, showErrorWithActions, showLoading, showNotification } from './ui.js';
import { setupVideoPlayer } from './video.js';
import { loadTranscript } from './transcript.js';
import { updateChapters, clearChapterMarkers } from './chapters.js';
import { checkSceneDetection, updateScenes, stopDetectionPolling } from './scenes.js';
import { fetchOcrResults, resetOcrStatus } from './ocr.js';
import { refreshTranscriptVersions } from './transcript-versions.js';
import { startElapsedTimer } from './progress-time.js';

// Translate backend transcript_source/active_language fields (set from the
// persisted "active source" marker) into the state.currentTranscriptSource
// convention used across the app, and build the matching /summary URL so
// chapters/timeline/scenes stay in the same language as the transcript.
function applyActiveTranscriptSource(data, videoId) {
    if (data.transcript_source === 'translation' && data.active_language) {
        state.currentTranscriptSource = `translation:${data.active_language}`;
        state.currentTranslationLanguage = data.active_language;
        return `/summary/${encodeURIComponent(videoId)}?language=${encodeURIComponent(data.active_language)}`;
    }
    if (data.transcript_source === 'uploaded') {
        state.currentTranscriptSource = 'uploaded';
        state.currentTranslationLanguage = null;
        return `/summary/${encodeURIComponent(videoId)}`;
    }
    state.currentTranscriptSource = 'whisper';
    state.currentTranslationLanguage = null;
    return `/summary/${encodeURIComponent(videoId)}`;
}

export async function regenerateTranscript(options = {}) {
    const speakersOnly = options?.speakersOnly === true;
    const videoId = getActiveVideoId();
    if (!videoId) {
        showError('Load a video before regenerating its transcript.');
        return;
    }
    state.currentVideoId = videoId;
    const originalTranscript = state.currentTranscript.map((item) => ({ ...item }));

    const diarization = speakersOnly || elements.transcriptDiarization?.checked || false;
    const model = elements.transcriptModel?.value || '';
    if (model) sessionStorage.setItem('transcriptModelSessionV1', model);
    const prompt = elements.transcriptPrompt?.value.trim() || '';
    const button = speakersOnly ? document.getElementById('identifySpeakersBtn') : elements.regenerateTranscriptBtn;
    const idleLabel = button?.textContent;
    if (button) {
        button.disabled = true;
        button.textContent = speakersOnly ? 'Identifying speakers...' : 'Regenerating...';
    }
    const pollGeneration = ++state.whisperTranscriptPollGeneration;
    const startedAt = Date.now();
    renderWhisperProgress({ status: 'queued', progress: 0 }, startedAt);
    try {
        if (!options.resumeOnly) {
        const response = await fetch(`/generate_whisper_transcript/${encodeURIComponent(videoId)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                diarization,
                model,
                prompt,
                force: true,
                speakers_only: speakersOnly,
            })
        });
        const data = await readJsonResponse(response, 'Transcript regeneration');
        if (!data.success) throw new Error(data.error || 'Transcript regeneration failed');
        elements.transcriptOptionsDialog?.close();
        showNotification(
            data.phase === 'identifying_speakers'
                ? 'Using the existing transcript. Speaker identification started.'
                : data.status === 'in_progress'
                ? 'Transcript generation is already running. Showing its progress.'
                : 'Transcript regeneration started.',
            'info'
        );
        }

        while (
            state.currentVideoId === videoId
            && state.whisperTranscriptPollGeneration === pollGeneration
        ) {
            const statusResponse = await fetch(`/whisper_transcript_status/${encodeURIComponent(videoId)}`);
            const status = await readJsonResponse(statusResponse, 'Transcript status');
            if (state.currentVideoId !== videoId || state.whisperTranscriptPollGeneration !== pollGeneration) return;
            if (status.status === 'complete') {
                state.currentTranscriptSource = 'whisper';
                loadTranscript(applySpeakerNames(status.transcript, status.speaker_names || {}));
                renderSpeakerNames(status.speaker_names || {});
                showNotification(`Transcript generated with ${describeTranscriptionModel(status.model || model)}.`, 'success');
                if (status.metadata?.diarization_error) {
                    showError(
                        `The transcript was kept, but speaker identification failed: ${status.metadata.diarization_error}`
                    );
                }
                return;
            }
            if (status.status === 'error') {
                const error = new Error(status.error || 'Transcript regeneration failed');
                error.steps = status;
                throw error;
            }
            renderWhisperProgress(status, startedAt);
            const browse = document.getElementById('browseDuringTranscriptionBtn');
            if (browse) browse.onclick = () => {
                state.whisperTranscriptPollGeneration++;
                loadTranscript(originalTranscript);
                if (button) { button.disabled = false; button.textContent = idleLabel; }
                showNotification('Processing continues in the background. You can choose a saved transcript above.', 'info');
            };
            await delay(3000);
        }
    } catch (error) {
        const lastStep = error.steps?.steps?.at(-1)?.message;
        showError(
            `Error regenerating transcript: ${error.message}`
            + (lastStep && lastStep !== error.message ? ` (last step: ${lastStep})` : '')
        );
        if (originalTranscript.length) {
            loadTranscript(originalTranscript);
        } else {
            elements.transcriptContainer.innerHTML = `
                <div class="transcript-processing transcript-error">
                    <i class="fas fa-info-circle"></i>
                    <p>Transcript regeneration stopped. ${escapeHtml(error.message)}</p>
                    ${renderTranscriptSteps(error.steps, { running: false })}
                </div>
            `;
        }
    } finally {
        if (button && state.whisperTranscriptPollGeneration === pollGeneration) {
            button.disabled = false;
            button.textContent = idleLabel;
        }
    }
}

function formatStepTime(seconds) {
    const total = Math.max(0, Math.round(Number(seconds) || 0));
    return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
}

export function renderTranscriptSteps(status, { running = true } = {}) {
    const steps = Array.isArray(status?.steps) ? status.steps : [];
    if (!steps.length) return '';
    const now = Number(status.steps_elapsed);
    // Show only the latest step; each new step replaces the previous one.
    const index = steps.length - 1;
    const items = [steps[index]].map((step) => {
        const isCurrent = running;
        const isFailure = !running;
        const icon = isCurrent ? 'fa-spinner fa-spin' : isFailure ? 'fa-times' : 'fa-check';
        const duration = Number.isFinite(now) ? now - Number(step.elapsed) : null;
        const durationText = duration !== null && duration >= 1
            ? ` <span class="transcript-step-duration">${isCurrent ? 'running for ' : 'took '}${formatStepTime(duration)}</span>`
            : '';
        return `
            <li class="transcript-step${isCurrent ? ' transcript-step-current' : ''}">
                <span class="transcript-step-time">${formatStepTime(step.elapsed)}</span>
                <i class="fas ${icon}"></i>
                <span>${escapeHtml(step.message)}${durationText}</span>
            </li>
        `;
    }).join('');
    return `<ol class="transcript-steps">${items}</ol>`;
}

function renderWhisperProgress(status, startedAt) {
    const progress = Number.isFinite(Number(status.progress))
        ? Math.max(0, Math.min(100, Number(status.progress)))
        : 0;
    const elapsedSeconds = Number.isFinite(status.steps_elapsed) ? status.steps_elapsed
        : Math.max(0, (Date.now() - startedAt) / 1000);
    const phase = status.phase === 'identifying_speakers'
        ? 'Identifying speakers with pyannote'
        : status.phase === 'transcribing' || progress > 0
            ? 'Transcribing audio'
            : 'Starting transcription';
    const lastUpdate = Number.isFinite(Number(status.last_updated_seconds_ago))
        ? ` Server heartbeat: ${Math.round(Number(status.last_updated_seconds_ago))}s ago.`
        : '';
    elements.transcriptContainer.innerHTML = `
        <div class="transcript-processing">
            <i class="fas fa-spinner fa-spin"></i>
            <div class="progress-container">
                <div class="progress-bar">
                    <div class="progress-fill" style="width: ${progress}%"></div>
                </div>
                <div class="progress-text">${Math.round(progress)}%</div>
            </div>
            <p>${phase}.${lastUpdate}</p>
            ${renderTranscriptSteps(status)}
            <p>You can continue using slide detection and video controls while this runs.</p>
            <button id="stopTranscriptionBtn" type="button" class="btn btn-danger">Stop processing</button>
            <button id="browseDuringTranscriptionBtn" type="button" class="btn btn-secondary">Keep processing and view transcript</button>
        </div>
    `;
    startElapsedTimer(elements.transcriptContainer, elapsedSeconds);
    const stop = document.getElementById('stopTranscriptionBtn');
    if (stop) stop.onclick = async () => {
        stop.disabled = true;
        try {
            const response = await fetch(`/cancel_whisper_transcript/${encodeURIComponent(state.currentVideoId)}`, {method: 'POST'});
            const data = await readJsonResponse(response, 'Stopping transcription');
            if (!data.success) throw new Error(data.error);
            state.whisperTranscriptPollGeneration++;
            loadTranscript(data.transcript || []);
            const regenerate = elements.regenerateTranscriptBtn;
            if (regenerate) { regenerate.disabled = false; regenerate.textContent = 'Regenerate transcript'; }
            const speakers = document.getElementById('identifySpeakersBtn');
            if (speakers) { speakers.disabled = false; speakers.textContent = 'Identify speakers in the current transcript'; }
            showNotification('Processing stopped. Your previous transcript was preserved.', 'info');
        } catch (error) {
            showError(error.message);
            stop.disabled = false;
        }
    };
}

function delay(milliseconds) {
    return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

let transcriptInfoGeneration = 0;

async function refreshTranscriptSourceInfo() {
    const generation = ++transcriptInfoGeneration;
    const source = state.currentTranscriptSource;
    const videoId = state.currentVideoId;
    if (!state.currentTranscript?.length || !videoId) {
        showTranscriptSourceInfo(null, null);
        return;
    }
    let metadata = null;
    if (source === 'whisper') {
        try {
            const response = await fetch(`/transcript_metadata/${encodeURIComponent(videoId)}`);
            metadata = (await response.json()).metadata || null;
        } catch (error) {
            metadata = null;
        }
    }
    if (generation === transcriptInfoGeneration) {
        showTranscriptSourceInfo(source, metadata);
    }
}

document.addEventListener('transcriptUpdated', () => {
    queueMicrotask(refreshTranscriptSourceInfo);
});

export function showTranscriptSourceInfo(source, metadata) {
    const info = elements.transcriptSourceInfo;
    if (!info) return;
    let text = '';
    if (source === 'whisper') {
        if (metadata?.model) {
            const parts = [describeTranscriptionModel(metadata.model)];
            if (metadata.device === 'cuda') parts.push('GPU');
            else if (metadata.device === 'cpu') parts.push('CPU');
            if (metadata.transcription_method) parts.push(metadata.transcription_method);
            parts.push(
                metadata.diarization
                    ? 'speakers identified'
                    : metadata.diarization_error
                    ? `speaker identification failed: ${metadata.diarization_error}`
                    : 'no speaker identification'
            );
            if (metadata.prompt) parts.push(`prompt: “${metadata.prompt.length > 80 ? `${metadata.prompt.slice(0, 80)}…` : metadata.prompt}”`);
            if (metadata.generated_at) parts.push(`generated ${metadata.generated_at}`);
            text = `Transcript: ${parts.join(' · ')}`;
        } else {
            text = 'Transcript: Whisper (generation options were not recorded for this transcript)';
        }
    } else if (source === 'youtube') {
        text = 'Transcript: YouTube captions';
    } else if (source === 'uploaded') {
        text = 'Transcript: uploaded file';
    } else if (typeof source === 'string' && source.startsWith('translation:')) {
        text = `Transcript: translated to ${source.slice('translation:'.length).toUpperCase()}`;
    }
    info.textContent = text;
    info.hidden = !text;
}

function escapeHtml(value) {
    const element = document.createElement('div');
    element.textContent = String(value);
    return element.innerHTML;
}

const TRANSCRIPTION_MODEL_LABELS = {
    'groq:whisper-large-v3-turbo': 'Groq Whisper Large v3 Turbo (online)',
    'groq:whisper-large-v3': 'Groq Whisper Large v3 (online)',
    'gemini:gemini-3.8-flash': 'Gemini 3.8 Flash (online)',
    'gemini:gemini-3.6-flash': 'Gemini 3.6 Flash (online)',
    'gemini:gemini-2.5-flash': 'Gemini 2.5 Flash (online)',
};

function describeTranscriptionModel(model) {
    return TRANSCRIPTION_MODEL_LABELS[model] || `Whisper ${model}`;
}

let transcriptionRuntime = null;

function applyTranscriptionModelStatus(data) {
    const select = elements.transcriptModel;
    if (!select || !data) return;
    transcriptionRuntime = data;
    const providers = data.transcription_providers || {};
    const keyNames = { groq: 'GROQ_API_KEY', gemini: 'GOOGLE_API_KEY' };
    [...select.options].forEach((option) => {
        const provider = option.value.split(':')[0];
        if (!(provider in keyNames)) return;
        if (!option.dataset.label) option.dataset.label = option.textContent;
        const available = Boolean(providers[provider]);
        option.disabled = !available;
        option.textContent = available
            ? option.dataset.label
            : `${option.dataset.label} (needs ${keyNames[provider]})`;
    });
    const usable = (value) => [...select.options].some(
        (option) => option.value === value && !option.disabled
    );
    const chosen = sessionStorage.getItem('transcriptModelSessionV1');
    const recommended = data.recommended_transcription_model;
    if (usable(chosen)) {
        select.value = chosen;
    } else if (usable(recommended)) {
        select.value = recommended;
    } else if (select.selectedOptions[0]?.disabled) {
        select.value = 'turbo';
    }
    updateTranscriptModelHelp();
}

export function updateTranscriptModelHelp() {
    const help = elements.transcriptModelHelp;
    const model = elements.transcriptModel?.value || '';
    if (!help) return;
    const recommended = transcriptionRuntime?.recommended_transcription_model;
    const suffix = recommended
        ? ` Default for this session: ${describeTranscriptionModel(recommended)}.`
        : '';
    if (model.startsWith('groq:')) {
        help.textContent = 'Runs on Groq\'s free tier: very fast, no GPU needed. Audio is sent to Groq.' + suffix;
    } else if (model.startsWith('gemini:')) {
        help.textContent = 'Uses your Gemini key: no GPU needed, but timestamps are approximate and it uses your Gemini quota. Audio is sent to Google.' + suffix;
    } else if (transcriptionRuntime && transcriptionRuntime.whisper_device !== 'cuda') {
        help.textContent = (transcriptionRuntime.whisper_device_warning
            || 'No usable GPU in this session: local Whisper runs on the CPU and is slow. Small or Base, or an online model, is much faster.') + suffix;
    } else {
        help.textContent = 'Runs on this server\'s GPU.' + suffix;
    }
}

export async function updateTranscriptionModelOptions() {
    try {
        const response = await fetch('/runtime_status');
        applyTranscriptionModelStatus(await readJsonResponse(response, 'Runtime status'));
    } catch (error) {
        console.warn('Could not check transcription models:', error);
    }
}

export async function uploadTranscriptFile() {
        const file = elements.transcriptFile?.files?.[0];
        if (!state.currentVideoId || !file) {
            showError('Select a transcript file after loading a video.');
            return;
        }
        const form = new FormData();
        form.append('transcript', file);
        const response = await fetch(`/upload_transcript/${encodeURIComponent(state.currentVideoId)}`, { method: 'POST', body: form });
        const data = await readJsonResponse(response, 'Transcript upload');
        loadTranscript(data.transcript);
        state.currentTranscriptSource = 'uploaded';
        updateTranslationOptions(data.source_language);
        showNotification(`Uploaded transcript loaded (${data.source_language}).`, 'success');
}

export async function translateTranscript() {
    const button = elements.translateTranscriptBtn;
    const originalText = button?.textContent || 'Translate transcript';
    if (!state.currentVideoId || !state.currentTranscript.length) {
        showError('Load a transcript before translating it.');
        return;
    }
    if (button) {
        button.disabled = true;
    }
    const originalTranscript = state.currentTranscript.map((item) => ({ ...item }));
    const selectedModel = elements.translationModel?.value || '';
    const allowFallback = document.getElementById('translationFallback')?.checked ?? true;
    if (!allowFallback && !selectedModel) {
        if (button) button.disabled = false;
        showError('Choose a model when automatic fallback is disabled.');
        return;
    }
    const selectedModelLabel = elements.translationModel?.selectedOptions[0]?.textContent || 'Auto';
    const startedAt = Date.now();
    const updateElapsed = () => {
        const elapsed = Math.max(0, Math.round((Date.now() - startedAt) / 1000));
        if (button) button.textContent = `Translating with ${selectedModelLabel}… ${elapsed}s`;
        if (elements.translationModelStatus) {
            elements.translationModelStatus.textContent =
                `Translation requested: ${selectedModelLabel} (${elapsed}s elapsed).`;
        }
    };
    updateElapsed();
    const elapsedTimer = setInterval(updateElapsed, 1000);
    let translationProgressTimer = null;
    try {
        const target = elements.translationTarget.value;
        if (!target) {
            state.currentTranslationLanguage = null;
            showNotification('No translation selected. The transcript remains in its original language.', 'info');
            return;
        }
        elements.transcriptOptionsDialog?.close();
        elements.transcriptContainer.innerHTML = `
            <div class="transcript-processing">
                <i class="fas fa-spinner fa-spin"></i>
                <p id="translationProgressText">Preparing translation with ${escapeHtml(selectedModelLabel)}...</p>
                <div class="progress-bar"><div class="progress-fill" id="translationProgressFill" style="width: 0%"></div></div>
                <p id="translationProgressDetail">The transcript and chapter titles are translated in parallel batches.</p>
            </div>
        `;
        const progressText = document.getElementById('translationProgressText');
        const progressFill = document.getElementById('translationProgressFill');
        const progressDetail = document.getElementById('translationProgressDetail');
        const progressUrl = `/translation_progress/${encodeURIComponent(state.currentVideoId)}/${encodeURIComponent(target)}`;
        let latestProgress = null;
        let progressRequest = null;
        translationProgressTimer = setInterval(() => {
            if (!progressText) return;
            const elapsed = Math.max(0, Math.round((Date.now() - startedAt) / 1000));
            const done = latestProgress?.completed_segments;
            const total = latestProgress?.total_segments;
            const counts = Number.isFinite(done) && total ? ` \u2014 ${done} of ${total} lines` : '';
            progressText.textContent = `Translating (${elapsed}s)${counts}`;
            if (progressFill && total) {
                progressFill.style.width = `${Math.min(100, Math.round((done / total) * 100))}%`;
            }
            if (progressDetail && latestProgress?.message) {
                progressDetail.textContent = latestProgress.message;
            }
            if (progressRequest) return;
            progressRequest = fetch(progressUrl)
                .then((progressResponse) => (progressResponse.ok ? progressResponse.json() : null))
                .then((progress) => {
                    if (progress && progress.status === 'running') latestProgress = progress;
                })
                .catch(() => {})
                .finally(() => { progressRequest = null; });
        }, 1000);
        const response = await fetch(`/translate_transcript/${encodeURIComponent(state.currentVideoId)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                transcript: state.currentTranscript,
                target_language: target,
                model: selectedModel,
                allow_fallback: allowFallback
            })
        });
        const data = await readJsonResponse(response, 'Transcript translation');
        clearInterval(translationProgressTimer);
        translationProgressTimer = null;
        if (
            !Array.isArray(data.transcript)
            || data.transcript.length !== originalTranscript.length
        ) {
            throw new Error('The translation model returned an incomplete transcript.');
        }
        const sourceText = originalTranscript.map((item) => String(item.text || '').trim()).join('\n');
        const translatedText = data.transcript.map((item) => String(item.text || '').trim()).join('\n');
        if (sourceText && translatedText === sourceText) {
            throw new Error(
                'The translation model returned the original text unchanged. Try another translation model.'
            );
        }
        loadTranscript(data.transcript);
        state.currentTranscriptSource = `translation:${target}`;
        state.currentTranslationLanguage = target;
        if (Array.isArray(data.chapters)) {
            state.videoChapters = data.chapters;
            updateChapters(data.chapters);
        }
        const usage = data.usage;
        const tokenSummary = usage?.reported_requests
            ? `${usage.total_tokens.toLocaleString()} tokens (${usage.input_tokens.toLocaleString()} input, ${usage.output_tokens.toLocaleString()} output)${usage.complete ? '' : '; partial usage'}`
            : 'Token usage unavailable';
        const models = usage?.models?.join(', ') || `${data.provider} ${data.model}`;
        const translationSummary =
            `${models}; ${data.batch_count} batch${data.batch_count === 1 ? '' : 'es'} in ${data.elapsed_seconds}s; ${tokenSummary}`;
        if (elements.translationModelStatus) {
            elements.translationModelStatus.textContent = `Last translation: ${translationSummary}.`;
        }
        showNotification(`Transcript translated to ${data.language} using ${translationSummary}.`, 'success');
    } catch (error) {
        loadTranscript(originalTranscript);
        if (elements.translationModelStatus) {
            elements.translationModelStatus.textContent = `Translation failed: ${error.message}`;
        }
        throw new Error(
            `Translation failed, so the original transcript was kept. ${error.message}`
        );
    } finally {
        clearInterval(elapsedTimer);
        if (translationProgressTimer) clearInterval(translationProgressTimer);
        if (button) {
            button.disabled =
                !elements.translationTarget.value
                || elements.translationModel?.dataset.available === 'false';
            button.textContent = originalText;
        }
    }
}

export async function updateTranslationModelStatus() {
    if (!elements.translationModelStatus) return;
    try {
        const response = await fetch('/runtime_status');
        const data = await readJsonResponse(response, 'Runtime status');
        applyTranscriptionModelStatus(data);
        if (elements.speakerDiarizationTokenStatus) {
            const hasToken = Boolean(data.huggingface_token_configured);
            elements.speakerDiarizationTokenStatus.textContent = hasToken
                ? 'Speakers are identified with pyannote after transcription.'
                : 'No Hugging Face token detected.';
            if (elements.speakerDiarizationSetup) {
                elements.speakerDiarizationSetup.hidden = hasToken;
            }
        }
        const hasTranslationModel = Boolean(
            data.translation_default_provider && data.translation_default_model
        );
        elements.translationModel.dataset.available = String(hasTranslationModel);
        if (!hasTranslationModel) {
            elements.translationModelStatus.textContent =
                'No translation model is configured. Set GOOGLE_API_KEY, GROQ_API_KEY or OPENAI_API_KEY in Colab and restart the server.';
            elements.translateTranscriptBtn.disabled = true;
            return;
        }
        const defaultDescription = `${data.translation_default_provider} ${data.translation_default_model}`;
        elements.translationModelStatus.textContent =
            `Auto currently uses ${defaultDescription}. Groq uses sequential batches within provider limits; other providers use parallel batches.`;
        if (Array.isArray(data.translation_models)) {
            const selected = elements.translationModel.value;
            const automatic = document.createElement('option');
            automatic.value = '';
            automatic.textContent = 'Auto (Groq preferred when configured)';
            elements.translationModel.replaceChildren(automatic);
            for (const model of data.translation_models) {
                const option = document.createElement('option');
                option.value = model.id;
                option.textContent = model.label;
                option.disabled = !model.available;
                elements.translationModel.append(option);
            }
            elements.translationModel.value = selected;
            if (elements.translationModel.selectedIndex < 0) elements.translationModel.value = '';
        } else {
            [...elements.translationModel.options].forEach((option) => {
                if (option.value.startsWith('gemini-')) option.disabled = !data.gemini_configured;
                if (option.value.startsWith('groq:')) option.disabled = !data.groq_configured;
                if (option.value.startsWith('gpt-')) option.disabled = !data.openai_configured;
            });
        }
        if (elements.translationModel.selectedOptions[0]?.disabled) {
            elements.translationModel.value = '';
        }
    } catch (error) {
        elements.translationModel.dataset.available = 'false';
        elements.translateTranscriptBtn.disabled = true;
        elements.translationModelStatus.textContent =
            `Could not check translation model configuration: ${error.message}`;
    }
}

function updateTranslationOptions(sourceLanguage) {
        [...elements.translationTarget.options].forEach((option) => {
            option.disabled = Boolean(option.value) && option.value === sourceLanguage;
        });
        if (elements.translationTarget.value === sourceLanguage) {
            elements.translationTarget.value = '';
        }
        elements.translateTranscriptBtn.disabled = !elements.translationTarget.value;
    }

export function renderSpeakerNames(names = {}) {
        const panel = elements.speakerNamesPanel;
        if (!panel) return;
        panel.innerHTML = '';
        const speakers = new Set(state.currentTranscript.map((item) => item.speaker).filter(Boolean));
        if (!speakers.size) {
            panel.hidden = true;
            return;
        }

        panel.hidden = false;
        const title = document.createElement('strong');
        title.textContent = 'Speaker names';
        panel.appendChild(title);
        const updatedNames = { ...names };
        speakers.forEach((speaker) => {
            const row = document.createElement('label');
            row.className = 'speaker-name-row';
            row.innerHTML = `<span>${speaker}</span>`;
            const input = document.createElement('input');
            input.value = names[speaker] || speaker.replace(/_/g, ' ');
            input.setAttribute('aria-label', `Name for ${speaker}`);
            input.addEventListener('change', async () => {
                updatedNames[speaker] = input.value.trim() || speaker;
                const response = await fetch(`/speaker_names/${encodeURIComponent(state.currentVideoId)}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ names: updatedNames })
                });
                const data = await readJsonResponse(response, 'Saving speaker names');
                showNotification('Speaker name saved.', 'success');
                loadTranscript(state.currentTranscript.map((item) => ({ ...item, speaker_name: data.speaker_names[item.speaker] || item.speaker })));
            });
            row.appendChild(input);
            panel.appendChild(row);
        });
    }

function applySpeakerNames(transcript, names) {
    return transcript.map((item) => ({
        ...item,
        speaker_name: item.speaker ? (names[item.speaker] || item.speaker) : undefined
    }));
}

async function readJsonResponse(response, operation) {
    const responseText = await response.text();
    if (!responseText.trim()) {
        throw new Error(`${operation} failed (HTTP ${response.status}). The server returned an empty response.`);
    }

    let data;
    try {
        data = JSON.parse(responseText);
    } catch (error) {
        const detail = responseText.replace(/\s+/g, ' ').trim().slice(0, 240);
        throw new Error(
            `${operation} failed (HTTP ${response.status}). The server returned invalid JSON${detail ? `: ${detail}` : '.'}`
        );
    }

    if (!response.ok) {
        throw new Error(data.error || data.detail || `${operation} failed (HTTP ${response.status}).`);
    }
    return data;
}

async function startYoutubeWhisperPolling(videoId) {
    const updateTranscript = (transcript) => {
        state.currentTranscript = transcript;
        state.currentTranscriptSource = 'whisper';
        loadTranscript(transcript);
        elements.generateSummaryBtn.disabled = false;
        elements.exportChaptersBtn.disabled = false;
        showNotification('Whisper transcript generation complete. Generate Summary is ready.', 'success');
    };

    const pollStartedAt = Date.now();
    const checkStatus = async () => {
        const response = await fetch(`/whisper_transcript_status/${videoId}`);
        const data = await response.json();
        if (data.status === 'in_progress') {
            renderWhisperProgress(data, pollStartedAt);
            return false;
        }
        if (data.status === 'complete' && Array.isArray(data.transcript)) {
            updateTranscript(data.transcript);
            if (state.youtubeTranscriptInterval) {
                clearInterval(state.youtubeTranscriptInterval);
                state.youtubeTranscriptInterval = null;
            }
            return true;
        }
        if (data.status === 'error') {
            if (state.youtubeTranscriptInterval) {
                clearInterval(state.youtubeTranscriptInterval);
                state.youtubeTranscriptInterval = null;
            }
            elements.transcriptContainer.innerHTML = `
                <div class="transcript-processing transcript-error">
                    <i class="fas fa-info-circle"></i>
                    <p>Transcript generation stopped: ${escapeHtml(data.error || 'No recognizable speech was found in this video.')}</p>
                    <p>You can still detect slides, but transcript-based summaries are unavailable.</p>
                    ${renderTranscriptSteps(data, { running: false })}
                </div>
            `;
            elements.generateSummaryBtn.disabled = true;
            elements.exportChaptersBtn.disabled = false;
            throw new Error(data.error || 'Whisper transcript generation failed.');
        }
        return false;
    };

    let status = await fetch(`/whisper_transcript_status/${videoId}`).then((response) => response.json());
    if (status.status === 'queued') {
        const response = await fetch(`/generate_whisper_transcript/${videoId}`, { method: 'POST' });
        const data = await response.json();
        if (!response.ok || !data.success) {
            throw new Error(data.error || 'Could not start Whisper transcript generation.');
        }
    } else if (status.status === 'complete' && Array.isArray(status.transcript)) {
        updateTranscript(status.transcript);
        return;
    } else if (status.status === 'error') {
        throw new Error(status.error || 'Whisper transcript generation failed.');
    }

    elements.transcriptContainer.innerHTML = `
        <div class="transcript-processing">
            <i class="fas fa-spinner fa-spin"></i>
            <p>Generating transcript with Whisper AI. You can generate the summary when it is ready.</p>
        </div>
    `;
    elements.loadingIndicator.querySelector('p').textContent =
        'Video ready. Generating transcript in the background...';
    await checkStatus();
    if (!state.currentTranscript.length) {
        state.youtubeTranscriptInterval = setInterval(() => {
            checkStatus().catch((error) => showError(`Error generating transcript: ${error.message}`));
        }, 5000);
    }
}

function getActiveVideoId() {
    if (state.currentVideoId) {
        return state.currentVideoId;
    }

    const source = elements.videoPlayer?.currentSrc || elements.videoPlayer?.src || '';
    const sourceMatch = source.match(/\/video\/([^/?#]+)/);
    if (sourceMatch) {
        return decodeURIComponent(sourceMatch[1]);
    }

    return extractVideoId(elements.youtubeUrl?.value || '');
}

/**
 * Resets all video-related states when loading a new video
 */
function resetVideoStates() {
    if (state.youtubeTranscriptInterval) {
        clearInterval(state.youtubeTranscriptInterval);
        state.youtubeTranscriptInterval = null;
    }
    if (state.summaryRetryTimer) {
        clearInterval(state.summaryRetryTimer);
        state.summaryRetryTimer = null;
    }
    state.summaryGenerationInProgress = false;

    // Reset SSE connection if it exists
    if (window.sseConnection) {
        console.log('Closing existing SSE connection');
        window.sseConnection.close();
        window.sseConnection = null;
    }
    stopDetectionPolling();
    if (state.transcriptOcrStatusInterval) {
        clearInterval(state.transcriptOcrStatusInterval);
        state.transcriptOcrStatusInterval = null;
    }
    
    // Reset OCR state
    state.ocrResults = [];
    state.ocrProcessing = false;
    
    // Remove any existing progress containers and stop the elapsed timer
    resetOcrStatus();
    const progressContainer = document.getElementById('ocrProgressContainer');
    if (progressContainer) {
        progressContainer.remove();
    }

    // Reset video player
    const videoPlayer = elements.videoPlayer;
    state.currentVideoId = null;
    state.currentVideoFilename = null;
    state.currentTranslationLanguage = null;
    state.whisperTranscriptPollGeneration += 1;
    if (elements.translationTarget) {
        elements.translationTarget.value = '';
    }
    if (elements.translateTranscriptBtn) {
        elements.translateTranscriptBtn.disabled = true;
    }
    if (elements.regenerateTranscriptBtn) {
        elements.regenerateTranscriptBtn.disabled = false;
        elements.regenerateTranscriptBtn.textContent = 'Regenerate transcript';
    }
    clearChapterMarkers();
    videoPlayer.pause();
    videoPlayer.removeAttribute('src');
    videoPlayer.load();
    elements.exportChaptersBtn.disabled = true;

    // Clear existing transcript and chapters
    elements.transcriptContainer.innerHTML = '';
    elements.chaptersContainer.innerHTML = '<p>Click "Generate Summary" to create chapter markers from the transcript.</p>';
    
    // Reset chapter state
    state.videoChapters = [];
    document.dispatchEvent(new CustomEvent('chaptersUpdated', { detail: [] }));
    
    // Reset scene detection state
    if (state.sceneDetectionInterval) {
        clearInterval(state.sceneDetectionInterval);
        state.sceneDetectionInterval = null;
    }
    state.videoScenes = [];
    const slideTextDownload = document.getElementById('downloadSlideTextWordBtn');
    if (slideTextDownload) slideTextDownload.disabled = true;
    state.sceneDetectionStartedAt = null;
    state.currentDebugScene = null;
    elements.detectScenesBtn.disabled = true;
    if (elements.downloadScreenshotsBtn) {
        elements.downloadScreenshotsBtn.disabled = true;
    }
    if (elements.downloadSlidesPdfBtn) {
        elements.downloadSlidesPdfBtn.disabled = true;
    }
    
    // Reset slide content and disable search
    elements.slideContentContainer.innerHTML = `
        <div class="coming-soon">
            <i class="fas fa-cogs"></i>
            <h3>Processing Text Elements</h3>
            <p>Text detection and OCR will begin after scene detection is complete.</p>
        </div>
    `;
    elements.slideSearch.disabled = true;
    elements.slideSearch.value = '';
    elements.clearSlideSearchBtn.classList.remove('visible');
    elements.slideSearchResultsCount.textContent = '0/0';
    elements.prevSlideSearchBtn.disabled = true;
    elements.nextSlideSearchBtn.disabled = true;

    // Reset search states
    state.searchResults = [];
    state.currentSearchIndex = -1;
    state.slideSearchResults = [];
    state.currentSlideSearchIndex = -1;

    // Reset transcript-OCR relationships
    state.transcriptOcrRelationships = null;
    state.ocr_to_transcript = {};
    state.transcript_to_ocr = {};

    // Reset current transcript
    state.currentTranscript = [];
}

/**
 * Processes a YouTube video
 */
export async function processVideo() {
    const url = elements.youtubeUrl.value;
    const videoId = extractVideoId(url);
    
    if (!videoId) {
        showError('Invalid YouTube URL');
        return;
    }

    // Save the URL to localStorage
    localStorage.setItem('lastYoutubeUrl', url);

    showError('');
    const savedUploads = document.getElementById('uploadedVideos');
    if (savedUploads) savedUploads.value = '';
    showLoading(true);
    elements.loadingIndicator.querySelector('p').textContent = 'Uploading video...';
    elements.generateSummaryBtn.disabled = true;
    elements.exportChaptersBtn.disabled = true;

    try {
        // Reset all video-related states
        resetVideoStates();

        const videoQuality = Number(elements.youtubeVideoQuality?.value || 480);
        const response = await fetch(`/download/${videoId}?quality=${encodeURIComponent(videoQuality)}`);
        const data = await readJsonResponse(response, 'YouTube video download');

        if (!data.success) {
            throw new Error(data.error || 'The server could not download this YouTube video.');
        }
        state.currentVideoId = data.video_id || videoId;
        elements.exportChaptersBtn.disabled = false;

        // Set new source and wait for metadata to load
        await new Promise((resolve, reject) => {
            const timeout = setTimeout(() => {
                reject(new Error('Upload completed, but the browser could not load the video preview.'));
            }, 30000);
            elements.videoPlayer.src = data.video_url;
            
            elements.videoPlayer.onloadedmetadata = () => {
                clearTimeout(timeout);
                setupVideoPlayer(elements.videoPlayer);
                resolve();
            };
            
            elements.videoPlayer.onerror = () => {
                clearTimeout(timeout);
                reject(new Error('Failed to load video'));
            };
        });

        // Store the ID returned by the server so downloaded files and
        // subsequent scene requests always refer to the same video.
        if (state.youtubeRetryTimer) {
            clearInterval(state.youtubeRetryTimer);
            state.youtubeRetryTimer = null;
        }
        elements.detectScenesBtn.disabled = false;
        
        // Check transcript availability
        const hasYoutubeTranscript = data.has_youtube_transcript;
        const hasWhisperTranscript = data.has_whisper_transcript;
        
        // Determine which transcript to use based on preference and availability
        let transcriptToUse = data.transcript;
        let transcriptSource = state.currentTranscriptSource;
        // Restore the active translation (if any) so this video reopens in whichever
        // language it was last viewed, instead of always reverting to the original.
        let translationLanguage = null;
        if (data.transcript_source === 'translation' && data.active_language) {
            translationLanguage = data.active_language;
        } else if (data.transcript_source === 'uploaded') {
            transcriptSource = transcriptSource === 'youtube' ? transcriptSource : 'uploaded';
        }

        if (transcriptSource === 'whisper' && !hasWhisperTranscript) {
            // If whisper is preferred but not available, show notification
            if (hasYoutubeTranscript) {
                showNotification('Whisper transcript not available. Using YouTube transcript instead.', 'info');
                transcriptSource = 'youtube';
            } else {
                showNotification('No transcripts available. Please generate a Whisper transcript in Settings.', 'info');
                transcriptSource = null;
            }
        } else if (transcriptSource === 'youtube' && !hasYoutubeTranscript) {
            // If youtube is preferred but not available, try whisper
            if (hasWhisperTranscript) {
                showNotification('YouTube transcript not available. Using Whisper transcript instead.', 'info');
                transcriptSource = 'whisper';
                translationLanguage = null;
                
                // Fetch Whisper transcript
                const whisperResponse = await fetch(`/get_transcript/${videoId}/whisper`);
                const whisperData = await whisperResponse.json();
                if (whisperData.success) {
                    transcriptToUse = whisperData.transcript;
                }
            } else {
                showNotification('No transcripts available. Please generate a Whisper transcript in Settings.', 'info');
                transcriptSource = null;
            }
        } else if (transcriptSource === 'whisper' && translationLanguage) {
            transcriptSource = `translation:${translationLanguage}`;
        }
        
        // Update current transcript source
        state.currentTranscriptSource = transcriptSource;
        state.currentTranslationLanguage = transcriptSource?.startsWith('translation:') ? translationLanguage : null;

        // Transcript generation is independent from slide detection. Make
        // scene controls available even when Whisper has no usable speech.
        elements.detectScenesBtn.disabled = false;

        if (transcriptToUse) {
            state.currentTranscript = transcriptToUse;
            
            // Check for existing summary
            const summaryUrl = state.currentTranslationLanguage
                ? `/summary/${videoId}?language=${encodeURIComponent(state.currentTranslationLanguage)}`
                : `/summary/${videoId}`;
            const summaryResponse = await fetch(summaryUrl);
            const summaryData = await summaryResponse.json();
            
            if (summaryData.success && summaryData.exists) {
                // Load existing summary
                updateChapters(summaryData.chapters);
            } else {
                elements.chaptersContainer.innerHTML = '<p>Click "Generate Summary" to create chapter markers from the transcript.</p>';
            }

            // Update transcript display
            loadTranscript(transcriptToUse);

            // Enable generate summary button if transcript is available
            elements.generateSummaryBtn.disabled = false;
            elements.exportChaptersBtn.disabled = false;
        } else if (data.transcript_in_progress) {
            try {
                await startYoutubeWhisperPolling(state.currentVideoId);
            } catch (error) {
                showError(`Transcript unavailable: ${error.message}`);
            }
        } else {
            try {
                await startYoutubeWhisperPolling(state.currentVideoId);
            } catch (error) {
                showError(`Transcript unavailable: ${error.message}`);
            }
        }

        elements.scenesContainer.innerHTML = '<p>Click "Detect Slides" to analyze this video.</p>';

    } catch (error) {
        const message = `Error: ${error.message}`;
        const restrictionError = /youtube|sign in|authentication|not a bot|bot|private|age-restricted|unavailable|forbidden|403|429/i.test(error.message);
        if (restrictionError) {
            const startAutoRetry = () => {
                if (state.youtubeRetryTimer) return;
                state.youtubeRetryTimer = setInterval(() => processVideo(), 15000);
                showNotification('Automatic YouTube retry enabled. Use Stop automatic retry to stop it.', 'info');
            };
            const stopAutoRetry = () => {
                if (state.youtubeRetryTimer) {
                    clearInterval(state.youtubeRetryTimer);
                    state.youtubeRetryTimer = null;
                }
                showError('');
            };
            showErrorWithActions(
                `${message} YouTube may be restricting this download. You can retry.`,
                [
                    { label: 'Retry YouTube download', action: () => processVideo() },
                    { label: 'Retry automatically', action: startAutoRetry },
                    { label: 'Stop automatic retry', action: stopAutoRetry }
                ]
            );
        } else {
            showError(message);
        }
    } finally {
        showLoading(false);
    }
}

/**
 * Processes a video file upload
 */
export async function processVideoUpload(file) {
    showError('');
    showLoading(true);
    elements.loadingIndicator.querySelector('p').textContent = 'Uploading video...';
    elements.generateSummaryBtn.disabled = true;

    try {
        // Reset all video-related states
        resetVideoStates();

        // Create FormData and append file
        const formData = new FormData();
        formData.append('video', file);

        const response = await uploadVideoWithProgress(formData);

        elements.loadingIndicator.querySelector('p').textContent =
            'Upload complete. Preparing video preview...';
        const data = await response.json();

        if (!data.success) {
            throw new Error(data.error);
        }
        state.currentVideoId = data.video_id;
        state.currentVideoFilename = file.name;
        await refreshTranscriptVersions();
        elements.exportChaptersBtn.disabled = false;

        // Set new source and wait for metadata to load
        await new Promise((resolve, reject) => {
            elements.videoPlayer.src = data.video_url;
            
            elements.videoPlayer.onloadedmetadata = () => {
                setupVideoPlayer(elements.videoPlayer);
                resolve();
            };
            
            elements.videoPlayer.onerror = () => {
                reject(new Error('Failed to load video'));
            };
        });

        elements.detectScenesBtn.disabled = false;
        elements.exportChaptersBtn.disabled = false;
        elements.loadingIndicator.querySelector('p').textContent =
            'Video ready. Scene detection is separate and must be started manually.';
        
        // Check if we have an existing transcript in the response
        console.log("check transcript", data.transcript)
        if (data.transcript) {
            console.log('Using existing transcript');
            state.currentTranscript = data.transcript;
            state.currentTranscriptSource = data.has_whisper_transcript ? 'whisper' : 'youtube';
            
            // Update transcript display
            loadTranscript(data.transcript);
            
            // Enable generate summary button
            elements.generateSummaryBtn.disabled = false;
            elements.exportChaptersBtn.disabled = false;
            
            // Check for existing summary
            const summaryResponse = await fetch(`/summary/${data.video_id}`);
            const summaryData = await summaryResponse.json();
            
            if (summaryData.success && summaryData.exists) {
                // Load existing summary
                updateChapters(summaryData.chapters);
            } else {
                elements.chaptersContainer.innerHTML = '<p>Click "Generate Summary" to create chapter markers from the transcript.</p>';
            }
        }
        // If transcript is being generated, show progress
        else if (data.transcript_in_progress) {
            const transcriptStartedAt = Date.now();
            // Show a message in the transcript container
            renderWhisperProgress({ status: 'queued', progress: 0 }, transcriptStartedAt);
            elements.loadingIndicator.querySelector('p').textContent =
                'Video ready. Generating transcript in the background...';
            
            // Start polling for transcript completion
            let whisperCheckInterval = null;
            const checkWhisperTranscript = async () => {
                if (state.currentVideoId !== data.video_id) {
                    if (whisperCheckInterval) clearInterval(whisperCheckInterval);
                    return;
                }
                try {
                    const whisperResponse = await fetch(`/whisper_transcript_status/${data.video_id}`);
                    const whisperData = await readJsonResponse(whisperResponse, 'Transcript status');
                    
                    if (whisperData.status === 'complete') {
                        if (whisperCheckInterval) clearInterval(whisperCheckInterval);
                        
                        // Transcript is ready, update UI
                        showNotification('Whisper transcript generation complete!', 'success');
                        
                        // Update current transcript
                        state.currentTranscript = whisperData.transcript;
                        state.currentTranscriptSource = 'whisper';
                        
                        // Update transcript display
                        loadTranscript(whisperData.transcript);
                        
                        // Enable generate summary button
                        elements.generateSummaryBtn.disabled = false;
                    } else if (whisperData.status === 'error') {
                        if (whisperCheckInterval) clearInterval(whisperCheckInterval);
                        elements.transcriptContainer.innerHTML = `
                            <div class="transcript-processing transcript-error">
                                <i class="fas fa-info-circle"></i>
                                <p>Transcript generation stopped: ${escapeHtml(whisperData.error || 'No recognizable speech was found in this video.')}</p>
                                <p>You can still detect slides, but transcript-based summaries are unavailable.</p>
                                ${renderTranscriptSteps(whisperData, { running: false })}
                            </div>
                        `;
                        elements.generateSummaryBtn.disabled = true;
                        elements.exportChaptersBtn.disabled = false;
                        showError(`Error generating transcript: ${whisperData.error}`);
                    } else {
                        renderWhisperProgress(whisperData, transcriptStartedAt);
                    }
                } catch (error) {
                    console.error('Error checking Whisper transcript status:', error);
                    elements.transcriptContainer
                        .querySelectorAll('.transcript-status-warning')
                        .forEach((warning) => warning.remove());
                    elements.transcriptContainer.insertAdjacentHTML(
                        'beforeend',
                        `<p class="transcript-status-warning">Could not refresh transcript status: ${escapeHtml(error.message)}</p>`
                    );
                }
            };
            
            // Check immediately and then every 5 seconds
            whisperCheckInterval = setInterval(checkWhisperTranscript, 5000);
            void checkWhisperTranscript();
        }
        // No transcript available and not being generated
        else {
            elements.transcriptContainer.innerHTML = '<p>No transcript available for this video.</p>';
            elements.generateSummaryBtn.disabled = true;
            elements.loadingIndicator.querySelector('p').textContent =
                'Video ready. No transcript is available.';
        }
        
        elements.scenesContainer.innerHTML = '<p>Click "Detect Slides" to analyze this video.</p>';

    } catch (error) {
        showError(`Error: ${error.message}`);
    } finally {
        showLoading(false);
    }

}

function uploadVideoWithProgress(formData) {
    return new Promise((resolve, reject) => {
        const request = new XMLHttpRequest();
        request.open('POST', '/upload_video');
        request.upload.addEventListener('progress', (event) => {
            if (!event.lengthComputable) return;
            const percent = Math.round((event.loaded / event.total) * 100);
            elements.loadingIndicator.querySelector('p').textContent =
                percent >= 100
                    ? 'Upload sent. Finalizing video...'
                    : `Uploading video... ${percent}%`;
        });
        request.addEventListener('load', () => {
            const responseText = request.responseText || '';
            try {
                JSON.parse(responseText);
            } catch (error) {
                reject(new Error(
                    `Video upload failed (HTTP ${request.status}). The server returned invalid JSON.`
                ));
                return;
            }
            resolve(new Response(responseText, {
                status: request.status,
                headers: { 'Content-Type': 'application/json' }
            }));
        });
        request.addEventListener('error', () => reject(new Error('Network error while uploading the video.')));
        request.addEventListener('abort', () => reject(new Error('Video upload was cancelled.')));
        request.send(formData);
    });
}

export async function loadUploadedVideo(videoId) {
    showError('');
    showLoading(true);
    elements.loadingIndicator.querySelector('p').textContent = 'Loading saved video...';
    elements.youtubeUrl.value = '';
    const exampleVideos = document.getElementById('exampleVideos');
    if (exampleVideos) exampleVideos.value = '';
    try {
        resetVideoStates();
        const response = await fetch(`/uploaded_videos/${encodeURIComponent(videoId)}`);
        const data = await readJsonResponse(response, 'Loading saved video');
        if (!response.ok || !data.success) {
            throw new Error(data.detail || data.error || 'Could not load saved video');
        }

        await new Promise((resolve, reject) => {
            const timeout = setTimeout(() => reject(new Error('Saved video preview could not be loaded.')), 30000);
            elements.videoPlayer.src = data.video_url;
            elements.videoPlayer.onloadedmetadata = () => {
                clearTimeout(timeout);
                setupVideoPlayer(elements.videoPlayer);
                resolve();
            };
            elements.videoPlayer.onerror = () => {
                clearTimeout(timeout);
                reject(new Error('Failed to load saved video'));
            };
        });

        state.currentVideoId = data.video_id;
        state.currentVideoFilename = Array.from(document.getElementById('uploadedVideos')?.options || [])
            .find((option) => option.value === data.video_id)?.dataset.filename || null;
        await refreshTranscriptVersions();
        elements.detectScenesBtn.disabled = false;
        elements.exportChaptersBtn.disabled = false;
        if (Array.isArray(data.scenes) && data.scenes.length > 0) {
            updateScenes(data.scenes, elements.videoPlayer);
            elements.scenesContainer.insertAdjacentHTML(
                'afterbegin',
                '<p class="scene-help">Loaded previously detected slides.</p>'
            );
            if (elements.downloadScreenshotsBtn) {
                elements.downloadScreenshotsBtn.disabled = false;
            }
            fetchOcrResults(data.video_id);
        } else {
            elements.scenesContainer.innerHTML = '<p>Click "Detect Slides" to analyze this video.</p>';
        }

        if (data.transcript) {
            state.currentTranscript = data.transcript;
            const summaryUrl = applyActiveTranscriptSource(data, data.video_id);
            loadTranscript(data.transcript);
            elements.generateSummaryBtn.disabled = false;
            elements.exportChaptersBtn.disabled = false;
            const summaryResponse = await fetch(summaryUrl);
            const summaryData = await summaryResponse.json();
            if (summaryData.success && summaryData.exists) {
                updateChapters(summaryData.chapters);
            }
        } else if (data.transcript_in_progress) {
            void regenerateTranscript({resumeOnly: true});
        } else {
            elements.transcriptContainer.innerHTML = '<p>No transcript available for this video.</p>';
        }
    } catch (error) {
        showError(`Error loading saved video: ${error.message}`);
    } finally {
        showLoading(false);
    }
}

// Mirrored by PRESET_LIMITS in tests/test_scene_processor.py.
const DETAIL_LEVEL_LIMITS = {
    more: { label: 'More slides', minimumDurationPercent: 0.25, maximumSlidesPerHour: 0 },
    balanced: { label: 'Balanced', minimumDurationPercent: 0.5, maximumSlidesPerHour: 0 },
    fewer: { label: 'Fewer slides', minimumDurationPercent: 1, maximumSlidesPerHour: 60 },
};

function resolveSlideLimits(mode, detail) {
    const preset = DETAIL_LEVEL_LIMITS[mode === 'adaptive' ? detail : 'balanced']
        || DETAIL_LEVEL_LIMITS.balanced;
    const durationValue = elements.minimumSlideDuration.value;
    const limitValue = elements.maximumSlidesPerHour.value;
    return {
        minimumDurationPercent: durationValue === 'auto' ? preset.minimumDurationPercent : Number(durationValue),
        maximumSlidesPerHour: limitValue === 'auto' ? preset.maximumSlidesPerHour : Number(limitValue),
        durationSource: durationValue === 'auto' ? 'detail level' : 'custom',
        limitSource: limitValue === 'auto' ? 'detail level' : 'custom',
    };
}

function describeAppliedSlideSettings(mode, detail, contentThreshold, limits) {
    if (mode === 'chapters') return 'Method: Transcript chapters.';
    const method = mode === 'adaptive'
        ? `Adaptive (${(DETAIL_LEVEL_LIMITS[detail] || DETAIL_LEVEL_LIMITS.balanced).label})`
        : `Content cuts (threshold ${contentThreshold})`;
    const limit = limits.maximumSlidesPerHour
        ? `${limits.maximumSlidesPerHour} slides/hour`
        : 'no slide limit';
    return `Method: ${method} ? minimum ${limits.minimumDurationPercent}% of video (${limits.durationSource}) ? ${limit} (${limits.limitSource}) ? similar-slide merging ${elements.mergeSimilarSlides.checked ? 'on' : 'off'}.`;
}

export async function detectScenes() {
    const videoId = state.currentVideoId;
    if (!videoId) return;
    const button = elements.detectScenesBtn;
    const mode = elements.sceneDetectionMode.value;
    const hasChapters = Array.isArray(state.videoChapters) && state.videoChapters.length > 0;
    if (mode === 'chapters' && !hasChapters) {
        showError('Generate transcript chapters before using chapter-only slide detection.');
        return;
    }
    const contentThreshold = Number(elements.sceneDetectionThreshold.value);
    const detail = elements.adaptiveDetail.value;
    const limits = resolveSlideLimits(mode, detail);
    const { minimumDurationPercent, maximumSlidesPerHour } = limits;
    const screenshotHeight = Number(elements.slideImageQuality.value);
    if (
        !Number.isFinite(contentThreshold)
        || !Number.isFinite(minimumDurationPercent)
        || !Number.isFinite(maximumSlidesPerHour)
        || !Number.isFinite(screenshotHeight)
    ) {
        showError('Please select valid slide-detection options.');
        return;
    }
    state.sceneDetectionThreshold = contentThreshold;
    localStorage.setItem('contentCutThresholdV1', String(contentThreshold));
    button.disabled = true;
    button.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Detecting Slides...';
    state.sceneDetectionStartedAt = Date.now();
    // New detection replaces the slides, so OCR controls for the old slides no longer apply.
    document.getElementById('startOcrBtn')?.remove();
    document.getElementById('processSuryaBtn')?.remove();
    resetOcrStatus();
    elements.scenesContainer.innerHTML = '<p>Detecting scene changes...</p><p class="scene-progress-status"><i class="fas fa-spinner fa-spin"></i> Analysis is running...</p>';
    const stopElapsed = startElapsedTimer(elements.scenesContainer);
    try {
        const response = await fetch(`/detect_scenes/${videoId}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                mode,
                adaptive_detail: detail,
                content_threshold: contentThreshold,
                minimum_slide_duration_percent: minimumDurationPercent,
                maximum_slides_per_hour: maximumSlidesPerHour,
                merge_similar_slides: elements.mergeSimilarSlides.checked,
                include_chapter_boundaries: hasChapters && elements.includeChapterBoundaries.checked,
                screenshot_height: screenshotHeight
            })
        });
        const data = await response.json();
        if (!response.ok || !data.success) throw new Error(data.detail || data.error || 'Could not start scene detection');
        elements.scenesContainer.insertAdjacentHTML(
            'afterbegin',
            `<p class="scene-progress-status">${describeAppliedSlideSettings(mode, detail, contentThreshold, limits)}</p>`
        );
        await checkSceneDetection(videoId);
        state.sceneDetectionInterval = setInterval(() => checkSceneDetection(videoId), 2000);
        fetchOcrResults(videoId);
        loadTranscriptOcrRelationships(videoId);
    } catch (error) {
        showError(`Error detecting scenes: ${error.message}`);
        button.disabled = false;
        button.innerHTML = '<i class="fas fa-film"></i> Detect Slides';
    } finally {
        stopElapsed();
    }
}

/**
 * Checks the status of the YOLO model
 */
export async function checkYoloStatus() {
    try {
        const response = await fetch('/yolo_status');
        const data = await response.json();
        
        if (!data.loaded) {
            console.warn("YOLO model not loaded:", data.error);
            showError('Object detection (YOLO) is not available: ' + data.error);
        }
    } catch (error) {
        console.error("Error checking YOLO status:", error);
    }
}

/**
 * Loads the transcript-OCR relationships for a video
 * @param {string} videoId - The YouTube video ID
 * @returns {Promise<boolean>} - Whether the relationships were loaded successfully
 */
export async function loadTranscriptOcrRelationships(videoId) {
    try {
        console.log('Loading transcript-OCR relationships...');
        const response = await fetch(`/get_transcript_ocr_relationships/${videoId}`);
        const data = await readJsonResponse(response, 'Transcript-OCR relationships');
        
        if (data.success) {
            // Store relationships in state for later use
            state.transcriptOcrRelationships = data;
            
            // Store for quick lookups
            state.ocr_to_transcript = {};
            if (data.ocr_to_transcript_relationships) {
                data.ocr_to_transcript_relationships.forEach(rel => {
                    // Create key using scene_index and ocr_text
                    const key = `${rel.scene_index}_${rel.ocr_text}`;
                    state.ocr_to_transcript[key] = rel.matches;
                });
            }
            
            // Store transcript to OCR relationships for quick lookups
            state.transcript_to_ocr = {};
            if (data.transcript_to_ocr_relationships) {
                data.transcript_to_ocr_relationships.forEach(rel => {
                    state.transcript_to_ocr[rel.transcript_index] = rel.matches;
                });
            }
            console.log('Transcript-OCR relationships loaded successfully');
            return true;
        } else if (data.error === "Embeddings not computed yet") {
            // Trigger computation of embeddings
            console.log('Triggering computation of transcript-OCR relationships...');
            const computeResponse = await fetch(`/compute_embeddings/${videoId}`, {
                method: 'POST'
            });
            
            if (computeResponse.ok) {
                // Set up polling to check for completion
                const checkEmbeddings = async () => {
                    const statusResponse = await fetch(`/embeddings_status/${videoId}`);
                    const statusData = await readJsonResponse(statusResponse, 'Embedding status');
                    
                    if (statusData.success) {
                        if (statusData.status === 'completed') {
                            clearInterval(state.transcriptOcrStatusInterval);
                            state.transcriptOcrStatusInterval = null;
                            // Load the completed relationships
                            return loadTranscriptOcrRelationships(videoId);
                        } else if (statusData.status === 'failed') {
                            clearInterval(state.transcriptOcrStatusInterval);
                            state.transcriptOcrStatusInterval = null;
                            console.error('Failed to compute transcript-OCR relationships:', statusData.error);
                            return false;
                        }
                        // Still in progress, continue polling
                    } else {
                        clearInterval(state.transcriptOcrStatusInterval);
                        state.transcriptOcrStatusInterval = null;
                        console.error('Failed to check embedding status:', statusData.error);
                        return false;
                    }
                };
                
                // Check every 3 seconds
                if (state.transcriptOcrStatusInterval) {
                    clearInterval(state.transcriptOcrStatusInterval);
                }
                state.transcriptOcrStatusInterval = setInterval(
                    () => checkEmbeddings().catch((error) => {
                        if (state.transcriptOcrStatusInterval) {
                            clearInterval(state.transcriptOcrStatusInterval);
                            state.transcriptOcrStatusInterval = null;
                        }
                        console.error('Error checking embedding status:', error);
                    }),
                    3000
                );
                
                // Also check immediately
                await checkEmbeddings().catch((error) => {
                    if (state.transcriptOcrStatusInterval) {
                        clearInterval(state.transcriptOcrStatusInterval);
                        state.transcriptOcrStatusInterval = null;
                    }
                    console.error('Error checking embedding status:', error);
                });
                return true;
            } else {
                const errorData = await computeResponse.text();
                console.error(
                    'Failed to trigger transcript-OCR relationship computation:',
                    errorData.slice(0, 240)
                );
                return false;
            }
        } else {
            console.error('Failed to load transcript-OCR relationships:', data.error);
            return false;
        }
    } catch (error) {
        console.error('Error loading transcript-OCR relationships:', error);
        return false;
    }
} 