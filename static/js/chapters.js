// chapters.js - Chapter generation functionality

import { elements } from './elements.js';
import { state } from './main.js';
import { showError, showErrorWithActions, showNotification } from './ui.js';
import { saveBlobToUserLocation } from './utils.js';
import { conciseTitle, slideTitle, sceneTopicTitle } from './chapter-navigation.js';
import { scrollWithinContainer } from './scroll-utils.js';
import { createSlideDownloadButton } from './slide-downloads.js';
import { startElapsedTimer } from './progress-time.js';
import { selectedMediaExportId } from './caption-export.js';
import { createChapterProjectButton } from './chapter-projects.js';
import { contentLanguage, setContentLanguage } from './content-languages.js';

let chapterMarkerGeneration = 0;

export function clearChapterMarkers() {
    chapterMarkerGeneration += 1;
    const progressContainer = elements.videoProgress;
    if (!progressContainer) return;
    progressContainer.querySelectorAll('.chapter-marker, .chapter-segment').forEach((marker) => marker.remove());
}

export function updateExportAvailability() {
    const hasVideo = Boolean(state.currentVideoId);
    const hasTranscript = Array.isArray(state.currentTranscript) && state.currentTranscript.length > 0;
    const hasTopics = Array.isArray(state.videoChapters) && state.videoChapters.length > 0;
    const hasSlides = Array.isArray(state.videoScenes) && state.videoScenes.length > 0;
    const hasParts = hasTopics || hasSlides;
    const intervalToggle = elements.intervalExportToggle;

    if (intervalToggle) {
        intervalToggle.disabled = false;
    }

    const useIntervals = Boolean(intervalToggle?.checked);
    const canBuildParts = hasVideo;
    const chapterGrouping = elements.chapterGrouping;
    if (chapterGrouping) {
        [...chapterGrouping.options].forEach((option) => {
            const requiresTopics = option.value === 'topic';
            const requiresSlides = option.value === 'slides';
            option.disabled = !useIntervals && (
                (requiresTopics && !hasTopics) || (requiresSlides && !hasSlides)
            );
        });
        if (chapterGrouping.selectedOptions[0]?.disabled) {
            const availableOption = [...chapterGrouping.options].find((option) => !option.disabled);
            if (availableOption) chapterGrouping.value = availableOption.value;
        }
        chapterGrouping.disabled = !canBuildParts;
    }

    [
        elements.timestampMode,
        elements.subpartMode,
    ].forEach((control) => {
        if (control) control.disabled = !canBuildParts;
    });

    [elements.exportImages, elements.exportClips].forEach((control) => {
        if (control) control.disabled = !hasVideo || !canBuildParts;
    });
    if (elements.exportTranscripts) {
        elements.exportTranscripts.disabled = !hasTranscript || !canBuildParts;
    }
    [
        elements.exportWord,
        elements.exportPdf,
        elements.exportWebpage,
        elements.exportScorm,
    ].forEach((control) => {
        if (control) control.disabled = !canBuildParts || !hasTranscript;
    });
    if (elements.exportOutline) {
        elements.exportOutline.disabled = !canBuildParts || !hasTranscript;
    }
    if (elements.exportScreenshotTranscript) {
        elements.exportScreenshotTranscript.disabled = !hasVideo || !hasSlides || !hasTranscript;
    }
    const sectionClips = document.getElementById('exportSectionClips');
    if (sectionClips) sectionClips.disabled = !canBuildParts || !hasTranscript;

    if (elements.exportAvailabilityNote) {
        if (!hasVideo) {
            elements.exportAvailabilityNote.textContent = 'Load a video before exporting.';
        } else if (!hasParts && !useIntervals) {
            elements.exportAvailabilityNote.textContent =
                'Without chapters or slides, the full video is one part; transcript-based sections are still available.';
        } else if (!hasTranscript) {
            elements.exportAvailabilityNote.textContent =
                'Transcript-dependent exports are disabled until a transcript is available.';
        } else {
            elements.exportAvailabilityNote.textContent = '';
        }
    }
}

function renderExportSteps(status) {
    const container = document.getElementById('exportProgress');
    if (!container) return;
    container.replaceChildren();
    const heading = document.createElement('strong');
    heading.textContent = status.status === 'complete' ? 'Export ready' : status.status === 'error' ? 'Export stopped' : 'Exporting…';
    container.appendChild(heading);
    const list = document.createElement('ol');
    const steps = status.steps || [];
    // Show only the latest step; each new step replaces the previous one.
    const index = steps.length - 1;
    if (index >= 0) {
        const step = steps[index];
        const item = document.createElement('li');
        const duration = Math.max(0, (status.elapsed || step.elapsed) - step.elapsed);
        const running = status.status === 'running';
        item.textContent = `${Math.floor(step.elapsed / 60)}:${String(Math.floor(step.elapsed % 60)).padStart(2, '0')} — ${step.message} (${running ? 'running for' : 'took'} ${Math.round(duration)}s)`;
        list.appendChild(item);
    }
    container.appendChild(list);
    if (status.error) {
        const message = document.createElement('p');
        message.textContent = status.error;
        container.appendChild(message);
    }
    startElapsedTimer(container, status.elapsed || 0, status.status === 'running' || status.status === 'queued');
}

async function downloadSavedExport(item, link) {
    link.classList.add('is-loading');
    link.setAttribute('aria-busy', 'true');
    const stopElapsed = startElapsedTimer(link);
    try {
        const response = await fetch(item.download_url);
        if (!response.ok) throw new Error(`server answered ${response.status}`);
        const blob = await response.blob();
        const disposition = response.headers.get('content-disposition') || '';
        const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i);
        const plain = disposition.match(/filename="?([^";]+)"?/i);
        const filename = encoded ? decodeURIComponent(encoded[1]) : plain ? plain[1] : (item.filename || 'export.zip');
        await saveBlobToUserLocation(blob, filename);
    } catch (error) {
        if (error?.name === 'AbortError') return;
        showError(`Could not download saved export: ${error.message}`);
    } finally {
        stopElapsed();
        link.classList.remove('is-loading');
        link.removeAttribute('aria-busy');
    }
}

export async function refreshSavedExports() {
    const container = document.getElementById('savedExports');
    if (!container) return;
    if (!state.currentVideoId) {
        container.replaceChildren();
        document.getElementById('exportProgress')?.replaceChildren();
        return;
    }
    const videoId = state.currentVideoId;
    try {
        const response = await fetch(`/saved_exports/${encodeURIComponent(videoId)}`);
        const data = await readJsonResponse(response, 'Saved exports');
        if (state.currentVideoId !== videoId) return;
        container.replaceChildren();
        const completed = data.exports.filter((item) => item.status === 'complete');
        if (!completed.length) {
            const message = document.createElement('p');
            message.className = 'saved-exports-empty';
            message.textContent = 'Completed exports will appear here and remain available after server restarts.';
            container.appendChild(message);
        }
        const list = document.createElement('ul');
        list.className = 'saved-exports-list';
        completed.forEach((item) => {
            const row = document.createElement('li');
            row.className = 'saved-export-row';
            const info = document.createElement('div');
            info.className = 'saved-export-info';
            const title = document.createElement('span');
            title.className = 'saved-export-title';
            title.textContent = item.title;
            title.title = item.title;
            const date = document.createElement('time');
            date.className = 'saved-export-date';
            const created = new Date(item.created_at * 1000);
            date.dateTime = created.toISOString();
            date.textContent = created.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
            info.appendChild(title);
            info.appendChild(date);
            row.appendChild(info);
            const actions = document.createElement('div');
            actions.className = 'saved-export-actions';
            if (item.open_url) {
                const open = document.createElement('a');
                open.innerHTML = '<i class="fas fa-external-link-alt" aria-hidden="true"></i> Open';
                open.title = 'Open HTML in browser';
                open.setAttribute('aria-label', `Open ${item.title} in browser`);
                open.href = item.open_url;
                open.target = '_blank';
                open.rel = 'noopener';
                open.className = 'btn btn-secondary btn-action';
                actions.appendChild(open);
            }
            const download = document.createElement('a');
            download.innerHTML = '<i class="fas fa-download" aria-hidden="true"></i>';
            download.title = 'Download';
            download.setAttribute('aria-label', `Download ${item.title}`);
            download.href = item.download_url;
            download.className = 'btn btn-secondary btn-action btn-icon';
            download.addEventListener('click', (event) => {
                event.preventDefault();
                void downloadSavedExport(item, download);
            });
            actions.appendChild(download);
            row.appendChild(actions);
            list.appendChild(row);
        });
        if (completed.length) container.appendChild(list);
        if (completed.some((item) => item.open_url)) {
            const help = document.createElement('p');
            help.className = 'saved-exports-empty';
            help.textContent = 'For downloaded HTML: extract the entire ZIP, then open index.html from the extracted folder. Keep its images and videos alongside it, or use Open above.';
            container.appendChild(help);
        }
    } catch (error) {
        if (state.currentVideoId !== videoId) return;
        container.replaceChildren();
        const message = document.createElement('p');
        message.setAttribute('role', 'alert');
        message.textContent = error instanceof TypeError
            ? 'Could not reach the app server to load saved exports. A failed export does not remove saved history. Wait for the server to finish restarting, then click Refresh saved exports.'
            : `Could not load saved exports: ${error.message}. Click Refresh saved exports to retry.`;
        container.appendChild(message);
    }
}

export async function exportChapters() {
    if (state.exportInProgress) {
        showNotification('An export is already running.', 'info');
        return;
    }
    state.exportInProgress = true;
    const videoId = state.currentVideoId;
    const button = elements.exportChaptersBtn;
    button.disabled = true;
    button.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Exporting...';
    let stopDownloadElapsed;
    showError('');
    try {
        const useIntervals = elements.intervalExportToggle?.checked;
        const intervalMinutes = Number(elements.intervalDuration?.value);
        if (useIntervals && (!Number.isFinite(intervalMinutes) || intervalMinutes <= 0)) {
            throw new Error('Enter an interval duration greater than zero minutes.');
        }
        const options = {
            ...(useIntervals ? { interval_minutes: intervalMinutes } : {}),
            include_images: !elements.exportImages?.disabled && (elements.exportImages?.checked ?? false),
            include_transcripts: !elements.exportTranscripts?.disabled && (elements.exportTranscripts?.checked ?? false),
            include_clips: !elements.exportClips?.disabled && (elements.exportClips?.checked ?? false),
            include_word: !elements.exportWord?.disabled && (elements.exportWord?.checked ?? false),
            include_pdf: !elements.exportPdf?.disabled && (elements.exportPdf?.checked ?? false),
            include_webpage: !elements.exportWebpage?.disabled && (elements.exportWebpage?.checked ?? true),
            include_screenshot_transcript: !elements.exportScreenshotTranscript?.disabled
                && (elements.exportScreenshotTranscript?.checked ?? false),
            include_outline: !elements.exportOutline?.disabled && (elements.exportOutline?.checked ?? false),
            include_scorm: !elements.exportScorm?.disabled && (elements.exportScorm?.checked ?? false),
            chapter_grouping: elements.chapterGrouping?.value || 'topic',
            timestamp_mode: elements.timestampMode?.value || 'subpart',
            subpart_mode: elements.subpartMode?.value || 'both',
            clip_overlap: document.getElementById('exportClipOverlap')?.checked ?? true,
            include_section_clips: !document.getElementById('exportSectionClips')?.disabled
                && (document.getElementById('exportSectionClips')?.checked ?? false),
            include_slide_text: document.getElementById('exportSlideText')?.checked ?? false,
            document_title: elements.exportTitle?.value.trim() || '',
            export_filename: elements.exportFilename?.value.trim() || '',
            transcript_language: state.currentTranslationLanguage || '',
            chapter_language: contentLanguage(videoId, 'chapters', state.currentTranslationLanguage),
            slide_title_language: contentLanguage(videoId, 'slides', state.currentTranslationLanguage),
            transcript_source: state.currentTranscriptSource || '',
            media_export_id: selectedMediaExportId(videoId),
        };
        if (options.include_screenshot_transcript) {
            options.screenshot_transcript = state.currentTranscript.map(item => ({...item}));
        }
        if (elements.exportOptionsDialog?.open) elements.exportOptionsDialog.close();
        renderExportSteps({status: 'running', steps: [{elapsed: 0, message: 'Starting export job'}]});
        const response = await fetch(`/export_jobs/${encodeURIComponent(videoId)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(options)
        });
        if (!response.ok) {
            const contentType = response.headers.get('content-type') || '';
            let message = `Chapter export failed (${response.status})`;
            if (contentType.includes('application/json')) {
                const error = await response.json();
                message = error.detail || error.error || message;
            } else {
                const text = (await response.text()).trim();
                if (text) {
                    message = text;
                }
            }
            throw new Error(message);
        }
        const job = await readJsonResponse(response, 'Start export');
        let status;
        do {
            const statusResponse = await fetch(job.status_url);
            status = await readJsonResponse(statusResponse, 'Export progress');
            if (state.currentVideoId === videoId) renderExportSteps(status);
            if (status.status === 'error') throw new Error(status.error || 'Export failed');
            if (status.status !== 'complete') {
                await new Promise((resolve) => setTimeout(resolve, 1000));
            }
        } while (status.status !== 'complete');
        await refreshSavedExports();
        button.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Downloading...';
        stopDownloadElapsed = startElapsedTimer(button);
        const downloadResponse = await fetch(status.download_url);
        if (!downloadResponse.ok) throw new Error(`Could not download export (${downloadResponse.status})`);
        const blob = await downloadResponse.blob();
        const directWord = options.include_word && !options.include_pdf
            && !options.include_webpage && !options.include_outline && !options.include_scorm
            && !options.include_screenshot_transcript
            && !options.include_images && !options.include_transcripts && !options.include_clips;
        const directPdf = options.include_pdf && !options.include_word
            && !options.include_webpage && !options.include_outline && !options.include_scorm
            && !options.include_screenshot_transcript
            && !options.include_images && !options.include_transcripts && !options.include_clips;
        const directScreenshotHtml = options.include_screenshot_transcript
            && !options.include_images && !options.include_transcripts && !options.include_clips
            && !options.include_word && !options.include_pdf && !options.include_webpage
            && !options.include_outline && !options.include_scorm && !options.include_section_clips;
        const fallbackFilename = directScreenshotHtml
            ? `${videoId}_screenshots_transcript.html`
            : directWord
            ? `${videoId}_chapter_document.docx`
            : directPdf
                ? `${videoId}_chapter_document.pdf`
                : `${videoId}_${useIntervals ? 'intervals' : 'chapters'}.zip`;
        const disposition = downloadResponse.headers.get('content-disposition') || '';
        const filenameMatch = disposition.match(/filename="?([^";]+)"?/i);
        const filename = filenameMatch ? filenameMatch[1] : fallbackFilename;
        await saveBlobToUserLocation(blob, filename);
    } catch (error) {
        showError(`Error exporting chapters: ${error.message}`);
    } finally {
        stopDownloadElapsed?.();
        state.exportInProgress = false;
        button.disabled = false;
        button.innerHTML = '<i class="fas fa-download"></i> Export Chapters';
    }
}

/**
 * Generates chapter markers from the transcript
 */
export async function generateChapters() {
    if (state.summaryGenerationInProgress) {
        showNotification('Summary generation is already running.', 'info');
        return;
    }
    if (!state.currentTranscript?.length) {
        showError('Generate or load a transcript before generating a summary.');
        return;
    }
    state.summaryGenerationInProgress = true;
    document.dispatchEvent(new CustomEvent('summaryGenerationChanged'));
    const generateSummaryBtn = elements.generateSummaryBtn;
    let stopElapsed;
    
    try {
        generateSummaryBtn.disabled = true;
        generateSummaryBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Generating...';
        const passes = document.getElementById('chapterPasses')?.value || 'one';
        const status = setSummaryStatus('generating', '<i class="fas fa-spinner fa-spin"></i> '
            + (passes === 'two' ? 'Generating chapters, then reviewing topic boundaries (two passes)...'
                : 'Generating summary and chapters...'));
        stopElapsed = startElapsedTimer(status);
        showError('');
        const requestVideoId = state.currentVideoId;
        const requestLanguage = state.currentTranslationLanguage || '';
        const response = await fetch('/generate_summary', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                transcript: state.currentTranscript,
                video_id: state.currentVideoId,
                model: elements.summaryModel?.value || null,
                transcript_language: requestLanguage,
                structure: elements.chapterStructure?.value || 'sections',
                detail: elements.chapterDetail?.value || 'balanced',
                instructions: (elements.chapterInstructions?.value || '').trim(),
                passes,
            })
        });

        const data = await readJsonResponse(response, 'Summary generation');

        if (!data.success) {
            throw new Error(data.error);
        }
        if (requestVideoId !== state.currentVideoId || requestLanguage !== (state.currentTranslationLanguage || '')) {
            // The user switched video or transcript meanwhile; the chapters stay saved with their transcript.
            setSummaryStatus('complete', '<i class="fas fa-info-circle"></i> Chapters were saved for the transcript that was open when you started.');
            return;
        }

        // Store the chapters in state
        setContentLanguage(requestVideoId, 'chapters', null);
        state.videoChapters = data.chapters;
        if (state.summaryRetryTimer) {
            clearInterval(state.summaryRetryTimer);
            state.summaryRetryTimer = null;
        }
        elements.exportChaptersBtn.disabled = false;
        
        updateChapters(data.chapters);
        if (data.notice) {
            showNotification(data.notice, 'info');
        }
        setSummaryStatus('complete', '<i class="fas fa-check-circle"></i> Summary and chapters ready.');
    } catch (error) {
        showError(`Error generating summary: ${error.message}`);
        const isModelError = /gemini|openai|model|429|404|quota|overload|unavailable|high demand/i.test(error.message);
        const detail = document.createElement('span');
        detail.textContent = error.message;
        const openAiHint = /OPENAI_API_KEY/.test(error.message)
            ? ' Add OPENAI_API_KEY in Colab to enable the OpenAI fallback.'
            : '';
        const statusMessage = isModelError
            ? `<i class="fas fa-exclamation-circle"></i> The AI provider could not generate chapters: ${detail.innerHTML}. Your existing chapters were kept.${openAiHint}`
            : error instanceof TypeError && /fetch|network/i.test(error.message)
            ? '<i class="fas fa-exclamation-circle"></i> Lost connection to the app server. It may have stopped or restarted. Your existing chapters were kept. Restore the server connection before retrying.'
            : `<i class="fas fa-exclamation-circle"></i> Summary generation failed: ${detail.innerHTML}. Your existing chapters were kept. You can retry without reloading the video.`;
        const status = setSummaryStatus(
            'error',
            statusMessage
        );
        const retryButton = document.createElement('button');
        retryButton.type = 'button';
        retryButton.className = 'btn btn-secondary btn-action summary-retry-btn';
        retryButton.innerHTML = '<i class="fas fa-redo"></i> Retry';
        retryButton.addEventListener('click', generateChapters);
        status.appendChild(retryButton);
        if (isModelError) {
            const autoRetryButton = document.createElement('button');
            autoRetryButton.type = 'button';
            autoRetryButton.className = 'btn btn-secondary btn-action summary-retry-btn';
            autoRetryButton.innerHTML = '<i class="fas fa-sync"></i> Retry automatically';
            autoRetryButton.addEventListener('click', () => {
                if (state.summaryRetryTimer) return;
                autoRetryButton.disabled = true;
                state.summaryRetryTimer = setInterval(() => {
                    if (!state.summaryGenerationInProgress) {
                        void generateChapters();
                    }
                }, 15000);
                showNotification('Retrying summary generation now; automatic retries will continue every 15 seconds.', 'info');
                void generateChapters();
            });
            status.appendChild(autoRetryButton);
            const stopRetryButton = document.createElement('button');
            stopRetryButton.type = 'button';
            stopRetryButton.className = 'btn btn-danger summary-retry-btn';
            stopRetryButton.innerHTML = '<i class="fas fa-stop"></i> Stop automatic retry';
            stopRetryButton.addEventListener('click', () => {
                if (state.summaryRetryTimer) {
                    clearInterval(state.summaryRetryTimer);
                    state.summaryRetryTimer = null;
                }
                showNotification('Automatic summary retry stopped.', 'info');
            });
            status.appendChild(stopRetryButton);
        }
    } finally {
        stopElapsed?.();
        state.summaryGenerationInProgress = false;
        document.dispatchEvent(new CustomEvent('summaryGenerationChanged'));
        generateSummaryBtn.disabled = false;
        generateSummaryBtn.innerHTML = '<i class="fas fa-magic"></i> Generate Summary';
    }
}

function setSummaryStatus(state, message) {
    let status = elements.summaryStatus;
    if (!status) {
        status = document.createElement('div');
        status.id = 'summaryStatus';
        status.setAttribute('role', 'status');
        status.setAttribute('aria-live', 'polite');
        elements.chaptersContainer.prepend(status);
    }

    status.className = `summary-status summary-status-${state.split(' ')[0]}`;
    status.innerHTML = message;
    status.hidden = false;
    return status;
}

async function readJsonResponse(response, operation) {
    const responseText = await response.text();
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

/**
 * Updates the chapters display with new chapter data
 * @param {Array} chapters - The chapter data
 */
export function updateChapters(chapters) {
    const chaptersContainer = elements.chaptersContainer;
    const status = elements.summaryStatus;
    chaptersContainer.querySelectorAll(':scope > .chapter-item, :scope > .chapter-sections, :scope > p').forEach(element => element.remove());
    if (status) {
        status.hidden = false;
    }
    
    if (!chapters || chapters.length === 0) {
        chaptersContainer.innerHTML = '<p>No chapters available.</p>';
        setSummaryStatus('complete', '<i class="fas fa-info-circle"></i> No chapters are available.');
        // Still clear/refresh any stale timeline and scene captions from a
        // previously loaded video/language instead of leaving them stale.
        state.videoChapters = [];
        document.dispatchEvent(new CustomEvent('chaptersUpdated', { detail: [] }));
        return;
    }

    // Update the list of chapters in the sidebar
    chapters.forEach((chapter, index) => {
        const div = document.createElement('div');
        div.className = 'chapter-item';
        div.dataset.start = timestampSeconds(chapter.timestamp);
        const timestamp = document.createElement('span');
        timestamp.className = 'chapter-timestamp';
        timestamp.textContent = chapter.timestamp;
        const title = document.createElement('span');
        title.textContent = conciseTitle(chapter.title);
        title.title = chapter.title;
        div.appendChild(timestamp);
        div.appendChild(title);
        div.appendChild(createChapterProjectButton(chapter, index, chapters));
        if (chapter.summary) div.title = chapter.summary;
        div.onclick = () => {
            const time = timestampSeconds(chapter.timestamp);
            elements.videoPlayer.currentTime = time;
            elements.videoPlayer.play().catch((error) => {
                if (error.name !== 'AbortError') {
                    console.warn('Unable to play chapter:', error);
                }
            });
        };
        chaptersContainer.appendChild(div);
        if (Array.isArray(chapter.sections) && chapter.sections.length) {
            const sections = renderChapterSections(chapter.sections);
            sections.hidden = true;
            div.classList.add('has-sections');
            div.prepend(expandToggle(div, sections, 'sections'));
            chaptersContainer.appendChild(sections);
        }
    });
    // Store the chapters in state
    state.videoChapters = chapters;
    attachChapterSlides();
    document.dispatchEvent(new CustomEvent('chaptersUpdated', { detail: chapters }));
    elements.exportChaptersBtn.disabled = false;
    setSummaryStatus('complete', '<i class="fas fa-check-circle"></i> Summary and chapters ready.');
    
    // Add chapter markers to the timeline
    addChapterMarkersToTimeline(chapters);
    activeChapterKey = '';
    updateActiveChapter();
}

function expandToggle(row, list, noun) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'chapter-expand';
    const setExpanded = (expanded) => {
        list.hidden = !expanded;
        row.classList.toggle('expanded', expanded);
        button.setAttribute('aria-expanded', String(expanded));
        const count = list.querySelectorAll('.chapter-section, .chapter-slide').length;
        button.title = expanded ? `Hide ${noun}` : `Show ${count} ${count === 1 ? noun.replace(/s$/, '') : noun}`;
        button.setAttribute('aria-label', button.title);
    };
    setExpanded(false);
    button.addEventListener('click', (event) => {
        event.stopPropagation();
        setExpanded(list.hidden);
        activeChapterKey = '';
        updateActiveChapter();
    });
    button.addEventListener('keydown', (event) => event.stopPropagation());
    return button;
}

/** Lists the detected slide changes beneath each section (or each chapter without sections). */
export function attachChapterSlides() {
    const container = elements.chaptersContainer;
    if (!container) return;
    container.querySelectorAll('.slide-download').forEach((element) => element.remove());
    container.querySelectorAll('.chapter-slides, .chapter-slides-wrap').forEach((element) => element.remove());
    container.querySelectorAll('.has-slides').forEach((row) => {
        row.classList.remove('has-slides', 'expanded');
        row.querySelector(':scope > .chapter-expand')?.remove();
    });
    if (!document.getElementById('chapterSlidesToggle')?.checked) return;
    const scenes = (state.videoScenes || [])
        .map((scene, index) => ({ scene, index, time: Number(scene.time_seconds) }))
        .filter((slide) => Number.isFinite(slide.time));
    const rows = [];
    container.querySelectorAll(':scope > .chapter-item').forEach((chapter) => {
        const list = chapter.nextElementSibling?.classList.contains('chapter-sections') ? chapter.nextElementSibling : null;
        const sections = list ? [...list.querySelectorAll(':scope > .chapter-section')] : [];
        if (!sections.length) {
            rows.push({ row: chapter, start: Number(chapter.dataset.start) });
            return;
        }
        sections.forEach((section, index) => rows.push({
            row: section,
            start: index === 0 ? Math.min(Number(chapter.dataset.start), Number(section.dataset.start)) : Number(section.dataset.start),
        }));
    });
    if (!scenes.length || !rows.length) return;
    rows.sort((a, b) => a.start - b.start);
    rows.forEach(({ row, start }, rowIndex) => {
        const end = rowIndex + 1 < rows.length ? rows[rowIndex + 1].start : Infinity;
        // Slides shown before the first chapter belong to it; a slide counts from half a second early.
        const inside = scenes.filter(({ time }) => (rowIndex === 0 || time >= start - 0.5) && time < end - 0.5);
        if (!inside.length) return;
        const list = document.createElement('ol');
        list.className = 'chapter-slides';
        inside.forEach(({ scene, index, time }) => list.appendChild(slideRow(scene, index, time)));
        row.appendChild(createSlideDownloadButton(inside.map(({ index }) => index),
            `Download all slides in ${row.tagName === 'LI' ? 'section' : 'chapter'} (${inside.length})`, {archive: true}));
        row.classList.add('has-slides');
        row.prepend(expandToggle(row, list, 'slides'));
        if (row.tagName === 'LI') {
            const wrap = document.createElement('li');
            wrap.className = 'chapter-slides-wrap';
            wrap.appendChild(list);
            row.after(wrap);
        } else {
            row.after(list);
        }
    });
    container.querySelectorAll(':scope > .chapter-item.has-sections').forEach((chapter) => {
        const sections = chapter.nextElementSibling;
        const indices = [...sections.querySelectorAll('.chapter-slide')].map((slide) => Number(slide.dataset.index));
        if (indices.length) chapter.appendChild(createSlideDownloadButton(indices,
            `Download all slides in chapter (${indices.length})`, {archive: true}));
    });
    activeChapterKey = '';
    updateActiveChapter();
}

function slideRow(scene, index, time) {
    const item = document.createElement('li');
    item.className = 'chapter-slide';
    item.tabIndex = 0;
    item.dataset.start = time;
    item.dataset.index = index;
    if (scene.thumbnail) {
        const image = document.createElement('img');
        image.src = scene.thumbnail;
        image.alt = '';
        image.loading = 'lazy';
        item.appendChild(image);
    }
    const timestamp = document.createElement('span');
    timestamp.className = 'chapter-timestamp';
    timestamp.textContent = scene.timestamp || formatClock(time);
    const title = document.createElement('span');
    title.textContent = slideTitle(scene, contentLanguage(state.currentVideoId, 'slides', state.currentTranslationLanguage))
        || sceneTopicTitle(state.videoChapters || [], Number(scene.time_seconds)) || `Slide ${index + 1}`;
    item.append(timestamp, title);
    item.appendChild(createSlideDownloadButton([index], `Download slide ${index + 1}`));
    const seek = (event) => {
        event.stopPropagation();
        elements.videoPlayer.currentTime = time;
    };
    item.addEventListener('click', seek);
    item.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            seek(event);
        }
    });
    return item;
}

function formatClock(seconds) {
    const total = Math.max(0, Math.floor(seconds));
    const hours = Math.floor(total / 3600);
    const clock = `${String(Math.floor((total % 3600) / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`;
    return hours ? `${hours}:${clock}` : clock;
}

let activeChapterKey = '';

/** Highlights the chapter and section being played, like the active transcript line. */
export function updateActiveChapter() {
    const container = elements.chaptersContainer;
    const time = elements.videoPlayer?.currentTime;
    if (!container || !Number.isFinite(time)) return;
    const latest = (items) => items.reduce((found, item) => (
        Number(item.dataset.start) <= time + 0.01 ? item : found
    ), null);
    const chapter = latest([...container.querySelectorAll('.chapter-item')]);
    const sectionList = chapter?.nextElementSibling?.classList.contains('chapter-sections') ? chapter.nextElementSibling : null;
    const section = sectionList ? latest([...sectionList.querySelectorAll('.chapter-section')]) : null;
    const slide = latest([...container.querySelectorAll('.chapter-slide')]);
    const slideList = slide?.closest('.chapter-slides');
    const slideVisible = Boolean(slide && slideList && !slideList.hidden && !slideList.closest('[hidden]'));
    const key = `${chapter?.dataset.start ?? ''}|${section?.dataset.start ?? ''}|${sectionList?.hidden ?? ''}|${slide?.dataset.start ?? ''}|${slideVisible}`;
    if (key === activeChapterKey) return;
    activeChapterKey = key;
    container.querySelectorAll('.chapter-item.active, .chapter-section.active, .chapter-slide.active')
        .forEach((item) => item.classList.remove('active'));
    chapter?.classList.add('active');
    section?.classList.add('active');
    slide?.classList.add('active');
    const visible = slideVisible ? slide : section && !sectionList.hidden ? section : chapter;
    if (visible) scrollWithinContainer(scrollParent(container), visible);
}

function scrollParent(element) {
    for (let node = element; node; node = node.parentElement) {
        const overflow = getComputedStyle(node).overflowY;
        if ((overflow === 'auto' || overflow === 'scroll') && node.scrollHeight > node.clientHeight) return node;
    }
    return element;
}

function timestampSeconds(timestamp) {
    return String(timestamp).split(':').map(Number).reduce((total, part) => total * 60 + part, 0);
}

function renderChapterSections(sections) {
    const list = document.createElement('ol');
    list.className = 'chapter-sections';
    sections.forEach((section) => {
        const item = document.createElement('li');
        item.className = 'chapter-section';
        item.tabIndex = 0;
        item.dataset.start = timestampSeconds(section.timestamp);
        const timestamp = document.createElement('span');
        timestamp.className = 'chapter-timestamp';
        timestamp.textContent = section.timestamp;
        const title = document.createElement('span');
        title.textContent = section.title;
        item.appendChild(timestamp);
        item.appendChild(title);
        if (section.point) item.title = section.point;
        const seek = () => {
            elements.videoPlayer.currentTime = timestampSeconds(section.timestamp);
            elements.videoPlayer.play().catch((error) => {
                if (error.name !== 'AbortError') console.warn('Unable to play section:', error);
            });
        };
        item.addEventListener('click', seek);
        item.addEventListener('keydown', (event) => {
            if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                seek();
            }
        });
        list.appendChild(item);
    });
    return list;
}

/**
 * Adds chapter markers to the timeline
 * @param {Array} chapters - The chapter data
 */
function addChapterMarkersToTimeline(chapters) {
    if (!chapters || chapters.length === 0) {
        return;
    }
    
    // Get the progress container and video player
    const progressContainer = elements.videoProgress;
    const videoPlayer = elements.videoPlayer;
    const generation = chapterMarkerGeneration;
    
    if (!progressContainer || !videoPlayer) {
        console.error('Progress container or video player not found');
        return;
    }
    
    // Remove any existing chapter markers first
    const existingMarkers = progressContainer.querySelectorAll('.chapter-marker');
    existingMarkers.forEach(marker => marker.remove());
    
    // If the video hasn't loaded yet or duration is unavailable, set up a one-time event listener
    if (!videoPlayer.duration || isNaN(videoPlayer.duration)) {
        console.log('Video duration not available yet, waiting for loadedmetadata event');
        
        // Set up a one-time event listener for when metadata is loaded
        const onMetadataLoaded = () => {
            if (generation !== chapterMarkerGeneration) return;
            addChapterMarkersWithDuration(chapters, videoPlayer, progressContainer);
            videoPlayer.removeEventListener('loadedmetadata', onMetadataLoaded);
        };
        
        videoPlayer.addEventListener('loadedmetadata', onMetadataLoaded);
        return;
    }
    
    // If duration is available, add markers immediately
    addChapterMarkersWithDuration(chapters, videoPlayer, progressContainer);
}

/**
 * Helper function to add chapter markers once video duration is available
 * @param {Array} chapters - The chapter data
 * @param {HTMLVideoElement} videoPlayer - The video player
 * @param {HTMLElement} progressContainer - The progress container
 */
function addChapterMarkersWithDuration(chapters, videoPlayer, progressContainer) {
    const duration = videoPlayer.duration;
    
    // First, remove any existing chapter segments
    const existingSegments = progressContainer.querySelectorAll('.chapter-segment');
    existingSegments.forEach(segment => segment.remove());
    
    // Add segments representing chapter regions
    for (let i = 0; i < chapters.length; i++) {
        // Convert timestamp to seconds for current chapter
        console.log(chapters)
        console.log(chapters[0])
        const currTimeInSeconds = timestampSeconds(chapters[i].timestamp);
        
        // Determine the end time of this chapter (start of next chapter or end of video)
        let endTimeInSeconds;
        if (i < chapters.length - 1) {
            endTimeInSeconds = timestampSeconds(chapters[i + 1].timestamp);
        } else {
            endTimeInSeconds = duration;
        }
        
        // Create segment element
        const segment = document.createElement('div');
        segment.className = 'chapter-segment';
        
        // Calculate position and width
        const startPosition = (currTimeInSeconds / duration) * 100;
        const endPosition = (endTimeInSeconds / duration) * 100;
        const width = endPosition - startPosition;
        
        // Set position and width
        segment.style.left = `${startPosition}%`;
        segment.style.width = `${width}%`;
        
        // Add data attributes for reference
        segment.dataset.chapterIndex = i;
        segment.dataset.startTime = currTimeInSeconds;
        segment.dataset.endTime = endTimeInSeconds;
        
        // Add tooltip to the segment
        const tooltip = document.createElement('div');
        tooltip.className = 'chapter-tooltip';
        tooltip.textContent = `Chapter ${i + 1}: ${chapters[i].title}`;
        segment.appendChild(tooltip);
        
        // Add mouseenter event to check and adjust tooltip position if needed
        segment.addEventListener('mouseenter', () => adjustTooltipPosition(segment, tooltip));
        
        // Add click behavior to the segment
        segment.addEventListener('click', (e) => {
            // Only seek if clicked directly on segment (not on a marker)
            if (e.target === segment || e.target === tooltip) {
                // Seek to the middle of the chapter unless it's very short
                const chapterDuration = endTimeInSeconds - currTimeInSeconds;
                const seekTime = currTimeInSeconds + (chapterDuration > 10 ? 1 : chapterDuration / 10);
                videoPlayer.currentTime = seekTime;
                videoPlayer.play().catch((error) => {
                    if (error.name !== 'AbortError') {
                        console.warn('Unable to play chapter segment:', error);
                    }
                });
            }
        });
        
        // Add to container
        progressContainer.appendChild(segment);
    }
    
    // Add each chapter marker to the timeline
    chapters.forEach((chapter, index) => {
        // Convert timestamp to seconds
        const timeInSeconds = timestampSeconds(chapter.timestamp);
        
        // Calculate position
        const position = (timeInSeconds / duration) * 100;
        
        // Create the marker
        const marker = document.createElement('div');
        marker.className = 'chapter-marker';
        marker.style.left = `${position}%`;
        
        // Add title attribute for accessibility
        marker.title = `Chapter ${index + 1}: ${chapter.title}`;
        
        // Add click behavior
        marker.addEventListener('click', (e) => {
            videoPlayer.currentTime = timeInSeconds;
            videoPlayer.play().catch((error) => {
                if (error.name !== 'AbortError') {
                    console.warn('Unable to play chapter marker:', error);
                }
            });
            e.stopPropagation(); // Prevent the progress bar click from firing too
        });
        
        // Add to the container
        progressContainer.appendChild(marker);
    });
    
    console.log(`Added ${chapters.length} chapter markers and segments to timeline`);
}

/**
 * Adjusts tooltip position dynamically to prevent it from going off-screen
 * @param {HTMLElement} segment - The chapter segment element
 * @param {HTMLElement} tooltip - The tooltip element
 */
function adjustTooltipPosition(segment, tooltip) {
    // Wait for tooltip to be visible to get its dimensions
    setTimeout(() => {
        // Reset any previous adjustments
        tooltip.style.transform = 'translateX(-50%)';
        
        // Get the tooltip's position and dimensions
        const tooltipRect = tooltip.getBoundingClientRect();
        const viewportWidth = window.innerWidth;
        
        // Check if tooltip overflows left edge
        if (tooltipRect.left < 0) {
            const leftOverflow = Math.abs(tooltipRect.left);
            // Adjust to the right to keep it within viewport
            tooltip.style.transform = `translateX(calc(-50% + ${leftOverflow + 10}px))`;
        }
        // Check if tooltip overflows right edge
        else if (tooltipRect.right > viewportWidth) {
            const rightOverflow = tooltipRect.right - viewportWidth;
            // Adjust to the left to keep it within viewport
            tooltip.style.transform = `translateX(calc(-50% - ${rightOverflow + 10}px))`;
        }
    }, 10); // Small delay to ensure tooltip is visible
} 