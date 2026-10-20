import { state } from './main.js';
import { elements } from './elements.js';
import { updateChapters, attachChapterSlides } from './chapters.js';
import { updateScenes } from './scenes.js';
import { showError, showNotification } from './ui.js';
import { startElapsedTimer } from './progress-time.js';
import { contentLanguage, contentPreference, setContentLanguage } from './content-languages.js';

export function initChapterTools() {
    const actions = [
        ['enhanceChaptersBtn', 'chapters', 'enhance'],
        ['translateChaptersBtn', 'chapters', 'translate'],
        ['translateSlideTitlesBtn', 'slides', 'translate'],
    ];
    let busy = false;
    let displayedContext = '';
    const refresh = () => {
        const context = `${state.currentVideoId || ''}:${state.currentTranslationLanguage || ''}`;
        if (!busy && context !== displayedContext) {
            document.getElementById('contentTranslationTarget').value =
                contentPreference(state.currentVideoId, 'chapters') || 'transcript';
            document.getElementById('slideTranslationTarget').value =
                contentPreference(state.currentVideoId, 'slides') || 'transcript';
            displayedContext = context;
        }
        for (const [id, kind] of actions) {
            document.getElementById(id).disabled = busy || state.summaryGenerationInProgress
                || !state.currentVideoId || !(kind === 'slides' ? state.videoScenes : state.videoChapters)?.length;
        }
    };
    for (const event of ['chaptersUpdated', 'slidesUpdated', 'transcriptUpdated', 'summaryGenerationChanged']) {
        document.addEventListener(event, refresh);
    }
    for (const [id, kind, operation] of actions) {
        const button = document.getElementById(id);
        button.addEventListener('click', async () => {
            if (busy || state.summaryGenerationInProgress) return;
            const videoId = state.currentVideoId;
            const transcriptLanguage = state.currentTranslationLanguage || '';
            const versionId = state.currentTranscriptVersionId || '';
            const source = state.currentTranscriptSource === 'uploaded' ? 'uploaded'
                : state.currentTranscriptSource === 'youtube' ? 'youtube' : 'whisper';
            const language = contentLanguage(videoId, kind, transcriptLanguage);
            const original = JSON.stringify(kind === 'slides' ? state.videoScenes : state.videoChapters);
            const target = document.getElementById(kind === 'slides'
                ? 'slideTranslationTarget' : 'contentTranslationTarget').value;
            const label = button.textContent;
            busy = true;
            state.summaryGenerationInProgress = true;
            elements.generateSummaryBtn.disabled = true;
            refresh();
            button.textContent = operation === 'enhance' ? 'Reviewing topic boundaries...' : 'Translating saved text...';
            const stopElapsed = startElapsedTimer(button);
            try {
                const response = await fetch(`/chapter_tools/${encodeURIComponent(videoId)}`, {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        kind, operation, language, source, version_id: versionId,
                        transcript_language: transcriptLanguage,
                        chapter_language: contentLanguage(videoId, 'chapters', transcriptLanguage),
                        target_language: target === 'transcript' ? transcriptLanguage || 'transcript' : target,
                        model: elements.summaryModel?.value || null,
                        detail: elements.chapterDetail?.value || 'balanced',
                        instructions: (elements.chapterInstructions?.value || '').trim(),
                    }),
                });
                const data = await response.json();
                if (!response.ok) throw new Error(data.detail || `${operation} failed (${response.status})`);
                const sameContext = videoId === state.currentVideoId
                    && transcriptLanguage === (state.currentTranslationLanguage || '')
                    && versionId === (state.currentTranscriptVersionId || '')
                    && original === JSON.stringify(kind === 'slides' ? state.videoScenes : state.videoChapters);
                if (sameContext) {
                    setContentLanguage(videoId, kind, data.language || null);
                    if (kind === 'chapters') updateChapters(data.content);
                    else {
                        updateScenes(data.content, elements.videoPlayer);
                        attachChapterSlides();
                    }
                }
                showNotification(sameContext
                    ? operation === 'enhance' ? 'Chapters enhanced and saved.' : 'Translation saved. Timing and structure are unchanged.'
                    : 'Result saved for the video and content that were open when you started. Reload it to view the result.',
                'success');
            } catch (error) {
                showError(`Could not ${operation} ${kind}: ${error.message}. Existing content was kept.`);
            } finally {
                stopElapsed();
                button.textContent = label;
                busy = false;
                state.summaryGenerationInProgress = false;
                elements.generateSummaryBtn.disabled = !state.currentTranscript?.length;
                refresh();
            }
        });
    }
    refresh();
}
