// main.js - Main application entry point

import { setupVideoPlayer, updateTimeMarker, updateChapterCaptions } from './video.js';
import { navigationParts } from './chapter-navigation.js';
import { loadTranscript, updateTranscriptDisplay, updateActiveTranscript } from './transcript.js';
import { checkSceneDetection, updateScenes, toggleSceneMarkers, findSceneAtTime, downloadSceneScreenshots, downloadSlidesPdf } from './scenes.js';
import { generateChapters, updateChapters, exportChapters, updateExportAvailability, refreshSavedExports, attachChapterSlides } from './chapters.js';
import { setupSearch, setupSlideSearch, toggleTimestamps, toggleFuzzySearch } from './search.js';
import { fetchOcrResults, updateSlideContentDisplay } from './ocr.js';
import { setupTabs, showError, showLoading, showNotification, openSettingsModal, closeSettingsModal, saveSettings, generateWhisperTranscript } from './ui.js';
import { processVideo, checkYoloStatus, processVideoUpload, loadUploadedVideo, detectScenes, regenerateTranscript, uploadTranscriptFile, translateTranscript, updateTranslationModelStatus, updateTranscriptionModelOptions, updateTranscriptModelHelp } from './api-module.js';
import { elements } from './elements.js';
import { initInteractiveLayer } from './interactive-layer.js';
import { initTranscriptVersions } from './transcript-versions.js';
import { initCaptionExport } from './caption-export.js';
import { openOptionsDialog } from './utils.js';

// Global state
export const state = {
    chapterGrouping: 'topic',
    currentTranscript: [],
    videoScenes: [],
    sceneDetectionInterval: null,
    sceneDetectionStartedAt: null,
    transcriptOcrStatusInterval: null,
    whisperTranscriptPollGeneration: 0,
    currentVideoId: null,
    currentVideoFilename: null,
    youtubeTranscriptInterval: null,
    youtubeRetryTimer: null,
    summaryRetryTimer: null,
    summaryGenerationInProgress: false,
    currentDebugScene: null,
    showSceneMarkers: true,
    showTimestamps: true,
    fuzzySearchEnabled: true,
    searchResults: [],
    currentSearchIndex: -1,
    ocrResults: [],
    ocrProcessing: false,
    slideSearchResults: [],
    currentSlideSearchIndex: -1,
    currentTranscriptSource: 'youtube',  // Default transcript source
    currentTranslationLanguage: null,
    currentTranscriptVersionId: null,
    interactiveLayerActive: false,
    showTranscriptHighlighting: true,  // Default to showing transcript highlighting
    // Transcript-OCR relationship data
    transcriptOcrRelationships: null,
    ocr_to_transcript: {}, // Map for quick lookup: scene_index_ocrText -> transcript matches
    transcript_to_ocr: {}, // Map for quick lookup: transcript_index -> OCR matches
    sceneDetectionThreshold: Number(localStorage.getItem('contentCutThresholdV1')) || 27
};

window.addEventListener('video-file-selected', (event) => {
    const file = event.detail;
    if (file) {
        processVideoUpload(file).catch((error) => {
            showError(`Error uploading video: ${error.message}`);
        });
    }
});

/**
 * Navigates to the next detected slide/scene
 */
function navigateToNextSlide() {
    const videoPlayer = elements.videoPlayer;
    if (!videoPlayer || !state.videoScenes || state.videoScenes.length === 0) {
        return;
    }

    const currentTime = videoPlayer.currentTime;
    let currentIndex = -1;
    
    console.log('Next slide navigation - Current time:', currentTime);
    console.log('Available scenes:', state.videoScenes.map((s, i) => `${i}: ${s.time_seconds}s`));
    
    // Find the current scene index - use the scene whose start time is closest to but not greater than current time
    // Add a small tolerance (0.1 seconds) to handle floating-point precision issues
    const tolerance = 0.1;
    
    for (let i = state.videoScenes.length - 1; i >= 0; i--) {
        const scene = state.videoScenes[i];
        
        // If current time is at or after this scene's start time (within tolerance)
        if (currentTime >= (scene.time_seconds - tolerance)) {
            currentIndex = i;
            console.log(`Found current scene at index ${i} (${scene.time_seconds}s)`);
            break;
        }
    }
    
    console.log('Current index:', currentIndex);
    
    // Navigate to next scene
    if (currentIndex >= 0 && currentIndex < state.videoScenes.length - 1) {
        const nextScene = state.videoScenes[currentIndex + 1];
        videoPlayer.currentTime = nextScene.time_seconds+0.1;
        console.log(`Navigating to next scene at index ${currentIndex + 1} (${nextScene.time_seconds}s)`);
        showNotification(`Navigated to slide ${currentIndex + 2}`, 'info');
    } else if (currentIndex === -1 && state.videoScenes.length > 0) {
        // If no current scene found, go to first scene
        videoPlayer.currentTime = state.videoScenes[0].time_seconds;
        console.log('No current scene found, going to first scene');
        showNotification('Navigated to first slide', 'info');
    } else {
        console.log('Already at last slide');
        showNotification('Already at last slide', 'warning');
    }
}

/**
 * Navigates to the previous detected slide/scene
 */
function navigateToPreviousSlide() {
    const videoPlayer = elements.videoPlayer;
    if (!videoPlayer || !state.videoScenes || state.videoScenes.length === 0) {
        return;
    }

    const currentTime = videoPlayer.currentTime;
    let currentIndex = -1;
    
    console.log('Previous slide navigation - Current time:', currentTime);
    console.log('Available scenes:', state.videoScenes.map((s, i) => `${i}: ${s.time_seconds}s`));
    
    // Find the current scene index - use the scene whose start time is closest to but not greater than current time
    // Add a small tolerance (0.1 seconds) to handle floating-point precision issues
    const tolerance = 0.1;
    
    for (let i = state.videoScenes.length - 1; i >= 0; i--) {
        const scene = state.videoScenes[i];
        
        // If current time is at or after this scene's start time (within tolerance)
        if (currentTime >= (scene.time_seconds - tolerance)) {
            currentIndex = i;
            console.log(`Found current scene at index ${i} (${scene.time_seconds}s)`);
            break;
        }
    }
    
    console.log('Current index:', currentIndex);
    
    // Navigate to previous scene
    if (currentIndex > 0) {
        const previousScene = state.videoScenes[currentIndex - 1];
        videoPlayer.currentTime = previousScene.time_seconds;
        console.log(`Navigating to previous scene at index ${currentIndex - 1} (${previousScene.time_seconds}s)`);
        showNotification(`Navigated to slide ${currentIndex}`, 'info');
    } else if (currentIndex === -1 && state.videoScenes.length > 0) {
        // If no current scene found, go to first scene
        videoPlayer.currentTime = state.videoScenes[0].time_seconds;
        console.log('No current scene found, going to first scene');
        showNotification('Navigated to first slide', 'info');
    } else {
        console.log('Already at first slide');
        showNotification('Already at first slide', 'warning');
    }
}

// Debounce variables to prevent double keypress
let lastKeyTime = 0;
let lastKey = null;

function navigateChapter(direction) {
    const player = elements.videoPlayer;
    if (!player) return;
    const times = navigationParts(state.videoChapters || [], state.videoScenes || [], state.chapterGrouping)
        .map((part) => part.start);
    if (!times.length) {
        showNotification('Generate chapters or detect slides for the selected navigation mode.', 'info');
        return;
    }
    const currentIndex = times.reduce((index, time, candidate) => (
        time <= player.currentTime + 0.1 ? candidate : index
    ), -1);
    const targetIndex = direction > 0
        ? Math.min(times.length - 1, currentIndex + 1)
        : Math.max(0, currentIndex - 1);
    player.currentTime = times[targetIndex];
}

/**
 * Sets up keyboard controls for slide navigation
 */
function setupKeyboardControls() {
    document.addEventListener('keydown', (event) => {
        // Only handle keyboard shortcuts when not typing in input fields
        if (event.target.tagName === 'INPUT' || event.target.tagName === 'TEXTAREA' || event.target.isContentEditable) {
            return;
        }

        const currentTime = Date.now();
        
        // Debounce: ignore if same key pressed within 200ms
        if (event.key === lastKey && (currentTime - lastKeyTime) < 200) {
            console.log(`Ignoring duplicate ${event.key} keypress (${currentTime - lastKeyTime}ms ago)`);
            event.preventDefault();
            return;
        }
        
        lastKey = event.key;
        lastKeyTime = currentTime;

        switch (event.key) {
            case 'l':
            case 'L':
                console.log('L pressed - navigating to next slide');
                event.preventDefault();
                navigateToNextSlide();
                break;
            case 'k':
            case 'K':
                console.log('K pressed - navigating to previous slide');
                event.preventDefault();
                navigateToPreviousSlide();
                break;
        }
    });
}

let exportSuggestionGeneration = 0;

async function loadExportSuggestions() {
    if (!state.currentVideoId) return;
    const generation = ++exportSuggestionGeneration;
    const videoId = state.currentVideoId;
    const language = state.currentTranslationLanguage;
    const chapterSnapshot = JSON.stringify(state.videoChapters || []);
    const isCurrent = () => generation === exportSuggestionGeneration
        && state.currentVideoId === videoId && state.currentTranslationLanguage === language
        && JSON.stringify(state.videoChapters || []) === chapterSnapshot;
    const sourceTitle = (state.currentVideoFilename || '').replace(/\.[^.]+$/, '');
    const fallbackSuggestion = (state.videoChapters || []).find((chapter) => chapter.title?.trim())?.title
        || sourceTitle || 'Chapter export';
    const fallbackFilename = (sourceTitle || fallbackSuggestion).normalize('NFKD')
        .replace(/[^A-Za-z0-9._-]+/g, '_').replace(/^[._-]+|[._-]+$/g, '').slice(0, 80)
        || 'chapter_export';
    // A title or file name typed for another video must not be reused for this one.
    for (const input of [elements.exportTitle, elements.exportFilename]) {
        if (input.dataset.videoId !== state.currentVideoId) {
            input.value = '';
            input.dataset.autoValue = '';
            input.dataset.videoId = state.currentVideoId;
        }
    }
    // Values the user has not edited follow the suggestion, e.g. after switching language.
    const isAuto = (input) => !input.value.trim() || input.value === input.dataset.autoValue;
    const setAuto = (input, value) => {
        input.value = value;
        input.dataset.autoValue = value;
    };
    if (isAuto(elements.exportTitle)) setAuto(elements.exportTitle, fallbackSuggestion);
    if (isAuto(elements.exportFilename)) setAuto(elements.exportFilename, fallbackFilename);
    const titleSuggestionHint = document.getElementById('exportTitleSuggestion');
    if (titleSuggestionHint) titleSuggestionHint.textContent = 'Suggesting a title...';
    const filenameSuggestionHint = document.getElementById('exportFilenameSuggestion');
    if (filenameSuggestionHint) filenameSuggestionHint.textContent = '';
    try {
        const response = await fetch(
            `/export_suggestions/${encodeURIComponent(videoId)}`,
            {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({chapters: (state.videoChapters || []).map((chapter) => ({
                    title: chapter.title, summary: chapter.summary || '',
                }))}),
            }
        );
        if (!response.ok) {
            throw new Error(`Could not load export suggestions (${response.status})`);
        }
        const suggestions = await response.json();
        if (!isCurrent()) return;
        if (suggestions.title_suggestion && isAuto(elements.exportTitle)) {
            setAuto(elements.exportTitle, suggestions.title_suggestion);
        }
        if (suggestions.filename_suggestion && isAuto(elements.exportFilename)) {
            setAuto(elements.exportFilename, suggestions.filename_suggestion);
        }
        const titleSuggestion = document.getElementById('exportTitleSuggestion');
        const filenameSuggestion = document.getElementById('exportFilenameSuggestion');
        if (titleSuggestion && suggestions.title_suggestion) {
            titleSuggestion.textContent = `Suggestion: ${suggestions.title_suggestion}`;
        }
        if (filenameSuggestion && suggestions.filename_suggestion) {
            filenameSuggestion.textContent = `Suggestion: ${suggestions.filename_suggestion}`;
        }
    } catch (error) {
        if (!isCurrent()) return;
        if (titleSuggestionHint) titleSuggestionHint.textContent = '';
        showError(`Could not load export suggestions: ${error.message}`);
    }
}

// Initialize the application
function initApp() {
    if (elements.videoPlayer) {
        elements.videoPlayer.controls = true;
        elements.videoPlayer.setAttribute('controls', '');
    }
    const bind = (element, event, handler) => {
        if (element) element.addEventListener(event, handler);
    };
    [
        ['summaryOptionsBtn', 'summaryOptionsPanel'],
        ['transcriptOptionsBtn', 'transcriptOptionsDialog'],
        ['openCaptionStyleBtn', 'captionStyleDialog'],
        ['downloadTranscriptBtn', 'transcriptDownloadDialog'],
        ['exportChaptersBtn', 'exportOptionsDialog'],
        ['detectScenesBtn', 'sceneDetectionOptionsDialog'],
        ['settingsBtn', 'settingsModal'],
    ].forEach(([buttonId, panelId]) => {
        const button = document.getElementById(buttonId);
        const panel = document.getElementById(panelId);
        if (!button || !panel) return;
        const indicator = document.createElement('span');
        indicator.className = 'options-state-indicator';
        indicator.setAttribute('aria-hidden', 'true');
        button.appendChild(indicator);
        button.setAttribute('aria-controls', panelId);
        const update = () => {
            const open = panel.tagName === 'DIALOG' ? panel.open
                : panelId === 'settingsModal' ? getComputedStyle(panel).display !== 'none' : !panel.hidden;
            button.setAttribute('aria-expanded', String(open));
            button.classList.toggle('options-open', open);
            indicator.textContent = open ? ' \u25b2 Open' : ' \u25bc';
            if (buttonId === 'summaryOptionsBtn') {
                button.firstChild.textContent = open ? 'Hide chapter options' : 'Chapter options';
            }
            if (!button.contains(indicator)) button.appendChild(indicator);
        };
        new MutationObserver(update).observe(panel, {attributes: true, attributeFilter: ['open', 'hidden', 'style', 'class']});
        panel.addEventListener('close', update);
        update();
    });

    // Wire the core video controls before optional analysis features initialize.
    bind(elements.loadVideoBtn, 'click', processVideo);
    bind(elements.detectScenesBtn, 'click', () => {
        if (!elements.sceneDetectionOptionsDialog.open) {
            openOptionsDialog(elements.sceneDetectionOptionsDialog);
        }
    });
    bind(elements.startSceneDetectionBtn, 'click', () => {
        elements.sceneDetectionOptionsDialog.close();
        void detectScenes();
    });
    if (elements.downloadScreenshotsBtn) {
        bind(elements.downloadScreenshotsBtn, 'click', downloadSceneScreenshots);
    }

    // Non-essential startup checks must not block the rest of the interface.
    try {
        checkYoloStatus();
    } catch (error) {
        console.error('YOLO status check failed:', error);
    }
    
    // Set up tabs
    try {
        setupTabs();
    } catch (error) {
        console.error('Tab initialization failed:', error);
    }
    
    // Optional analysis features must not prevent the core controls or
    // dynamically-created video selectors from initializing.
    try {
        setupSearch();
        setupSlideSearch();
        setupKeyboardControls();
    } catch (error) {
        console.error('Search/navigation initialization failed:', error);
    }

    try {
        initInteractiveLayer();
        console.log('Interactive layer initialized');
    } catch (error) {
        console.error('Interactive layer initialization failed:', error);
    }
    
    // Add event listeners
    bind(elements.generateSummaryBtn, 'click', generateChapters);
    bind(document.getElementById('downloadSlidesPdfBtn'), 'click', downloadSlidesPdf);
    const navigationGrouping = document.getElementById('navigationGrouping');
    const setGrouping = (value) => {
        state.chapterGrouping = value;
        if (navigationGrouping) navigationGrouping.value = value;
        if (elements.chapterGrouping) elements.chapterGrouping.value = value;
        updateChapterCaptions();
    };
    bind(navigationGrouping, 'change', (event) => setGrouping(event.target.value));
    bind(elements.chapterGrouping, 'change', (event) => setGrouping(event.target.value));
    [['timelineSectionsToggle', 'timelineSectionsV2'], ['timelineSlidesToggle', 'timelineSlidesV2']].forEach(([id, key]) => {
        const toggle = document.getElementById(id);
        if (!toggle) return;
        try {
            const saved = localStorage.getItem(key);
            if (saved !== null) toggle.checked = saved === 'true';
        } catch (error) { /* storage unavailable */ }
        bind(toggle, 'change', () => {
            try { localStorage.setItem(key, String(toggle.checked)); } catch (error) { /* storage unavailable */ }
            updateChapterCaptions();
        });
    });
    document.addEventListener('chaptersUpdated', updateChapterCaptions);
    document.addEventListener('scenesLoaded', updateChapterCaptions);
    document.addEventListener('scenesLoaded', () => attachChapterSlides());
    document.addEventListener('slidesUpdated', () => attachChapterSlides());
    bind(document.getElementById('chapterSlidesToggle'), 'change', attachChapterSlides);
    bind(document.getElementById('refreshSlideTitlesBtn'), 'click', async () => {
        if (!state.videoScenes?.length) {
            showError('Detect or load slides first. Adding titles does not detect slides.');
            return;
        }
        const button = document.getElementById('refreshSlideTitlesBtn');
        const videoId = state.currentVideoId;
        const language = state.currentTranslationLanguage || '';
        button.disabled = true;
        try {
            if (!state.videoChapters?.length) {
                if (!state.currentTranscript?.length) {
                    showError('Load a transcript or chapters before adding slide titles.');
                    return;
                }
                await generateChapters();
            }
            if (videoId !== state.currentVideoId || language !== (state.currentTranslationLanguage || '')) return;
            if (!state.videoChapters?.length) return;
            updateChapterCaptions();
            showNotification('Slide titles refreshed from the open chapters and sections. Existing slide images were reused.', 'success');
        } finally {
            button.disabled = false;
        }
    });
    bind(elements.summaryOptionsBtn, 'click', () => {
        elements.summaryOptionsPanel.hidden = !elements.summaryOptionsPanel.hidden;
    });
    [
        [elements.chapterStructure, 'chapterStructureV1'],
        [elements.chapterDetail, 'chapterDetailV1'],
        [elements.chapterInstructions, 'chapterInstructionsV1'],
    ].forEach(([control, key]) => {
        if (!control) return;
        try {
            const saved = localStorage.getItem(key);
            if (saved !== null && (control.tagName !== 'SELECT' || [...control.options].some((option) => option.value === saved))) {
                control.value = saved;
            }
        } catch (error) {
            console.warn('Chapter options could not be restored:', error);
        }
        bind(control, 'change', () => {
            try { localStorage.setItem(key, control.value); } catch (error) { /* storage unavailable */ }
        });
    });
    bind(elements.exportChaptersBtn, 'click', () => {
        if (!elements.exportOptionsDialog.open) {
            updateExportAvailability();
            setGrouping(elements.chapterGrouping.value);
            openOptionsDialog(elements.exportOptionsDialog);
            void loadExportSuggestions();
            void refreshSavedExports();
        }
    });
    bind(elements.startExportBtn, 'click', () => {
        void exportChapters();
    });
    document.addEventListener('transcriptUpdated', () => void refreshSavedExports());
    initTranscriptVersions();
    initCaptionExport();
    document.addEventListener('chaptersUpdated', () => void refreshSavedExports());
    bind(document.getElementById('refreshSavedExportsBtn'), 'click', refreshSavedExports);
    bind(elements.regenerateTranscriptBtn, 'click', () => regenerateTranscript());
    bind(document.getElementById('identifySpeakersBtn'), 'click', () => regenerateTranscript({speakersOnly: true}));
    bind(elements.uploadTranscriptBtn, 'click', () => void uploadTranscriptFile().catch((error) => showError(error.message)));
    bind(elements.translateTranscriptBtn, 'click', () => void translateTranscript().catch((error) => showError(error.message)));
    bind(elements.translationTarget, 'change', (event) => {
        elements.translateTranscriptBtn.disabled =
            !event.target.value || elements.translationModel?.dataset.available === 'false';
        if (!event.target.value) {
            state.currentTranslationLanguage = null;
        }
    });
    bind(elements.previousChapterBtn, 'click', () => navigateChapter(-1));
    bind(elements.nextChapterBtn, 'click', () => navigateChapter(1));
    bind(elements.playbackSpeed, 'change', (event) => {
        elements.videoPlayer.playbackRate = Number(event.target.value);
    });
    bind(elements.transcriptOptionsBtn, 'click', () => {
        if (!elements.transcriptOptionsDialog.open) {
            openOptionsDialog(elements.transcriptOptionsDialog);
            void updateTranslationModelStatus();
        }
    });
    bind(elements.intervalExportToggle, 'change', (event) => {
        elements.intervalDuration.disabled = !event.target.checked;
        updateExportAvailability();
    });
    bind(elements.sceneDetectionThreshold, 'input', (event) => {
        elements.sceneDetectionThresholdValue.textContent = event.target.value;
        state.sceneDetectionThreshold = Number(event.target.value);
        localStorage.setItem('contentCutThresholdV1', event.target.value);
    });
    const updateDetectionOptions = () => {
        const mode = elements.sceneDetectionMode.value;
        const hasChapters = Array.isArray(state.videoChapters) && state.videoChapters.length > 0;
        const chaptersOption = [...elements.sceneDetectionMode.options]
            .find((option) => option.value === 'chapters');
        if (chaptersOption) chaptersOption.disabled = !hasChapters;
        elements.includeChapterBoundaries.disabled = !hasChapters;
        elements.includeChapterBoundariesLabel.title = hasChapters
            ? 'Adds topic boundaries that are not already close to a detected slide.'
            : 'Generate transcript chapters first to enable this option.';
        if (mode === 'chapters' && !hasChapters) {
            elements.sceneDetectionMode.value = 'content';
            return updateDetectionOptions();
        }
        elements.adaptiveDetectionOptions.hidden = mode !== 'adaptive';
        elements.contentCutOptions.hidden = mode !== 'content';
        elements.visualDetectionOptions.hidden = mode === 'chapters';
        elements.includeChapterBoundariesLabel.hidden = mode === 'chapters';
        elements.sceneDetectionHelp.textContent = mode === 'adaptive'
            ? 'Adaptive mode finds settled slide states, ignores cursor and webcam movement, and merges builds according to the detail level.'
            : mode === 'content'
                ? 'Content cuts detect abrupt visual transitions using the separate PySceneDetect threshold above.'
                : 'Uses existing transcript chapter timestamps without scanning the complete video.';
    };
    elements.sceneDetectionMode.value = localStorage.getItem('sceneDetectionModeV2') || 'content';
    elements.adaptiveDetail.value = localStorage.getItem('adaptiveDetailV1') || 'balanced';
    elements.minimumSlideDuration.value = localStorage.getItem('minimumSlideDurationPercentV1') || 'auto';
    elements.maximumSlidesPerHour.value = localStorage.getItem('maximumSlidesPerHourV1') || 'auto';
    elements.slideImageQuality.value = localStorage.getItem('slideImageQualityV1') || '720';
    if (elements.transcriptModel) {
        bind(elements.transcriptModel, 'change', (event) => {
            sessionStorage.setItem('transcriptModelSessionV1', event.target.value);
            updateTranscriptModelHelp();
        });
        void updateTranscriptionModelOptions();
    }
    elements.youtubeVideoQuality.value = localStorage.getItem('youtubeVideoQualityV1') || '480';
    elements.mergeSimilarSlides.checked = localStorage.getItem('mergeSimilarSlidesV1') !== 'false';
    elements.includeChapterBoundaries.checked = localStorage.getItem('includeChapterBoundariesV1') !== 'false';
    bind(elements.sceneDetectionMode, 'change', (event) => {
        localStorage.setItem('sceneDetectionModeV2', event.target.value);
        updateDetectionOptions();
    });
    bind(elements.adaptiveDetail, 'change', (event) => {
        localStorage.setItem('adaptiveDetailV1', event.target.value);
    });
    bind(elements.minimumSlideDuration, 'change', (event) => {
        localStorage.setItem('minimumSlideDurationPercentV1', event.target.value);
    });
    bind(elements.maximumSlidesPerHour, 'change', (event) => {
        localStorage.setItem('maximumSlidesPerHourV1', event.target.value);
    });
    bind(elements.mergeSimilarSlides, 'change', (event) => {
        localStorage.setItem('mergeSimilarSlidesV1', String(event.target.checked));
    });
    bind(elements.includeChapterBoundaries, 'change', (event) => {
        localStorage.setItem('includeChapterBoundariesV1', String(event.target.checked));
    });
    bind(elements.slideImageQuality, 'change', (event) => {
        localStorage.setItem('slideImageQualityV1', event.target.value);
        if (event.target.value === '1080') {
            showNotification('High-quality screenshots require more processing time and storage.', 'warning');
        }
    });
    bind(elements.youtubeVideoQuality, 'change', (event) => {
        localStorage.setItem('youtubeVideoQualityV1', event.target.value);
        if (event.target.value === '720') {
            showNotification('720p videos download and process more slowly than the 480p default.', 'warning');
        }
    });
    document.addEventListener('chaptersUpdated', updateDetectionOptions);
    updateDetectionOptions();
    if (elements.sceneDetectionThreshold) {
        elements.sceneDetectionThreshold.value = state.sceneDetectionThreshold;
    }
    if (elements.sceneDetectionThresholdValue) {
        elements.sceneDetectionThresholdValue.textContent = state.sceneDetectionThreshold;
    }
    bind(elements.closeDetectionBtn, 'click', () => {
        if (elements.detectionOverlay) elements.detectionOverlay.style.display = 'none';
    });
    bind(elements.debugDetectionsBtn, 'click', () => {
        debugDetections(state.currentDebugScene, state.currentVideoId);
    });
    bind(elements.closeDebugBtn, 'click', () => {
        if (elements.debugOverlay) elements.debugOverlay.style.display = 'none';
    });
    bind(elements.sceneToggle, 'change', toggleSceneMarkers);
    bind(elements.timestampToggle, 'change', toggleTimestamps);
    bind(elements.fuzzySearchToggle, 'change', toggleFuzzySearch);
    
    // Settings modal
    bind(elements.settingsBtn, 'click', openSettingsModal);
    bind(elements.closeSettingsBtn, 'click', closeSettingsModal);
    bind(elements.saveSettingsBtn, 'click', saveSettings);
    
    // Whisper transcript generation
    bind(elements.generateWhisperBtn, 'click', () => {
        if (state.currentVideoId) {
            generateWhisperTranscript(state.currentVideoId);
        } else {
            showNotification('Please load a video first', 'error');
        }
    });
    
    // Listen for transcript loaded events
    document.addEventListener('transcriptLoaded', (event) => {
        loadTranscript(event.detail);
    });
    
    // Check for last URL in localStorage
    const lastUrl = localStorage.getItem('lastYoutubeUrl');
    if (lastUrl) {
        if (elements.youtubeUrl) elements.youtubeUrl.value = lastUrl;
    }
    
    // Fetch current transcript preference
    fetch('/get_transcript_preference')
        .then(response => response.json())
        .then(data => {
            state.currentTranscriptSource = data.preference || 'youtube';
        })
        .catch(error => {
            console.error('Error fetching transcript preference:', error);
        });
}

// Debug detections function
function debugDetections(scene, videoId) {
    if (!scene) return;
    
    const sceneIndex = scene.index || 0;
    
    fetch(`/scene_detections/${videoId}/${sceneIndex}`)
        .then(response => response.json())
        .then(data => {
            console.log("Debug detection data:", data);
            
            // Show debug info in the debug overlay
            const debugContent = elements.debugContent;
            debugContent.innerHTML = `
                <h3>Detection Debug Info</h3>
                <p>Video ID: ${videoId}</p>
                <p>Scene Index: ${sceneIndex}</p>
                <p>Has detections: ${data.has_detections}</p>
                <p>Detection count: ${data.detection_count}</p>
                <pre>${JSON.stringify(data, null, 2)}</pre>
            `;
            
            elements.debugOverlay.style.display = 'block';
        })
        .catch(error => {
            console.error("Error debugging detections:", error);
            showError("Error debugging detections. See console for details.");
        });
}

// Export functions and state for use in other modules
export {
    initApp,
    debugDetections,
    navigateToNextSlide,
    navigateToPreviousSlide
}; 