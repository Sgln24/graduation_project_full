// video.js - Video player functionality

import { elements } from './elements.js';
import { formatTime } from './utils.js';
import { state } from './main.js';
import { updateActiveTranscript } from './transcript.js';
import { navigationParts, activePart, conciseTitle, chapterStart, sceneTopicTitle } from './chapter-navigation.js';
import { scrollWithinContainer } from './scroll-utils.js';
import { updateActiveChapter } from './chapters.js';

export function updateChapterCaptions() {
    const parts = navigationParts(state.videoChapters || [], state.videoScenes || [], state.chapterGrouping);
    const caption = document.getElementById('currentChapterTitle');
    if (caption) {
        const text = activePart(parts, elements.videoPlayer.currentTime)?.title
            || 'No chapter or slide title available at this time.';
        caption.title = text;
        if (caption.textContent !== conciseTitle(text)) caption.textContent = conciseTitle(text);
    }
    syncTimelineGrouping();
    document.querySelectorAll('.timeline-item:not(.chapter-card)').forEach((item) => {
        const image = item.querySelector('.timeline-thumbnail');
        if (!image) return;
        let title = item.querySelector('.chapter-caption');
        if (!title) {
            title = document.createElement('div');
            title.className = 'chapter-caption';
            item.appendChild(title);
        }
        const customTitle = (state.videoScenes || []).find(scene =>
            Number(scene.time_seconds) === Number(image.dataset.time))?.title;
        const text = customTitle || sceneTopicTitle(state.videoChapters || [], Number(image.dataset.time))
            || activePart(parts, Number(image.dataset.time))?.title || 'No chapter title available.';
        // The numbered badge already identifies the slide, so show only the topic beneath it.
        const shown = conciseTitle(text.replace(/^\d+:\s*/, '').replace(/^\d+$/, ''));
        title.title = text;
        if (title.textContent !== shown) title.textContent = shown;
    });
    document.querySelectorAll('.scene-item .scene-label').forEach((label) => {
        const time = Number(label.closest('.scene-item').dataset.time);
        const customTitle = (state.videoScenes || []).find(scene => Number(scene.time_seconds) === time)?.title;
        const title = customTitle || sceneTopicTitle(state.videoChapters || [], time) || activePart(parts, time)?.title || '';
        const name = title.replace(/^\d+:\s*/, '').replace(/^\d+$/, '');
        const text = `Scene ${label.dataset.number}` + (name ? ` - ${name}` : '');
        label.title = name;
        if (label.textContent !== text) label.textContent = text;
    });
}

/**
 * Shows the thumbnails that match the navigation grouping: slides, content chapters, or both.
 * Content chapters reuse the screenshot of the slide visible when the chapter starts.
 */
export function syncTimelineGrouping() {
    const timeline = elements.thumbnailTimeline;
    if (!timeline) return;
    const scenes = state.videoScenes || [];
    const mode = state.chapterGrouping || 'topic';
    const chapters = (state.videoChapters || [])
        .map((chapter) => ({
            start: chapterStart(chapter),
            title: chapter.title || '',
            sections: (Array.isArray(chapter.sections) ? chapter.sections : [])
                .map((section) => ({ start: chapterStart(section), title: section.title || '' }))
                .filter((section) => Number.isFinite(section.start)),
        }))
        .filter((chapter) => Number.isFinite(chapter.start))
        .sort((a, b) => a.start - b.start);
    const hasSections = chapters.some((chapter) => chapter.sections.length > 0);
    const sectionsToggle = document.getElementById('timelineSectionsToggle');
    const sectionsControl = document.getElementById('timelineSectionsControl');
    if (sectionsControl) sectionsControl.hidden = !hasSections || mode === 'slides';
    const showSections = hasSections && Boolean(sectionsToggle?.checked);
    const slidesControl = document.getElementById('timelineSlidesControl');
    if (slidesControl) slidesControl.hidden = mode !== 'topic' || !chapters.length || !scenes.length;
    const nestSlides = mode === 'topic' && Boolean(document.getElementById('timelineSlidesToggle')?.checked);
    const signature = JSON.stringify([
        mode, scenes.map(scene => [scene.time_seconds, scene.title]), showSections, nestSlides,
        chapters.map((c) => [c.start, c.title, c.sections.map((s) => [s.start, s.title])]),
    ]);
    if (timeline.dataset.groupingSignature === signature) return;
    timeline.dataset.groupingSignature = signature;
    timeline.querySelectorAll('.chapter-card').forEach((card) => card.remove());
    const sceneItems = [...timeline.querySelectorAll('.timeline-item:not(.chapter-card)')];
    const showChapters = mode !== 'slides' && chapters.length > 0 && sceneItems.length > 0;
    const hideSlides = mode === 'topic' && showChapters && !nestSlides;
    sceneItems.forEach((item) => {
        item.classList.toggle('grouping-hidden', hideSlides);
        // Slide changes listed beneath their chapter (and section) cards.
        const time = Number(item.querySelector('.timeline-thumbnail')?.dataset.time);
        const chapter = chapters.filter((candidate) => candidate.start <= time + 0.01).at(-1);
        item.classList.toggle('nested-slide', showChapters && nestSlides);
        item.classList.toggle('under-section', showChapters && nestSlides && showSections
            && Boolean(chapter?.sections.some((section) => section.start <= time + 0.01)));
    });
    if (!showChapters) return;
    const sceneTimes = sceneItems.map((item) => Number(item.querySelector('.timeline-thumbnail')?.dataset.time));
    const screenshotAt = (start) => {
        let sourceIndex = 0;
        sceneTimes.forEach((time, i) => { if (time <= start + 0.01) sourceIndex = i; });
        return sceneItems[sourceIndex].querySelector('.timeline-thumbnail')?.src || '';
    };
    const insertByTime = (card, start) => {
        const next = [...timeline.querySelectorAll('.timeline-item')]
            .find((item) => item !== card && Number(item.querySelector('.timeline-thumbnail')?.dataset.time) > start);
        timeline.insertBefore(card, next || null);
    };
    chapters.forEach((chapter, index) => {
        const number = `${index + 1}`;
        if (!(mode === 'combined' && sceneTimes.some((time) => Math.abs(time - chapter.start) < 1))) {
            insertByTime(timelineCard('chapter-card', number, `Chapter ${number}`, chapter, screenshotAt(chapter.start)), chapter.start);
        }
        if (!showSections) return;
        chapter.sections.forEach((section, sectionIndex) => {
            // The chapter card already marks a section that starts with the chapter.
            if (Math.abs(section.start - chapter.start) < 1) return;
            const label = `${number}.${sectionIndex + 1}`;
            insertByTime(timelineCard('chapter-card section-card', label, `Section ${label}`, section, screenshotAt(section.start)), section.start);
        });
    });
}

function timelineCard(className, number, label, part, src) {
    const card = document.createElement('div');
    card.className = `timeline-item ${className}`;
    card.title = part.title;
    const badge = document.createElement('div');
    badge.className = 'detection-badge chapter-badge';
    badge.textContent = number;
    badge.setAttribute('aria-label', label);
    const img = document.createElement('img');
    img.className = 'timeline-thumbnail';
    img.src = src;
    img.alt = `${label}: ${part.title}`;
    img.dataset.time = part.start;
    const timestamp = document.createElement('div');
    timestamp.className = 'timeline-timestamp';
    timestamp.textContent = formatTime(part.start);
    const caption = document.createElement('div');
    caption.className = 'chapter-caption';
    caption.textContent = conciseTitle(part.title);
    card.append(badge, img, timestamp, caption);
    card.onclick = () => { elements.videoPlayer.currentTime = part.start; };
    return card;
}

/**
 * Sets up the video progress bar
 * @param {HTMLVideoElement} videoPlayer - The video player element
 */
export function setupVideoPlayer(videoPlayer) {
    const progressBar = elements.videoProgress;
    const hoverTime = elements.progressHoverTime;

    if (videoPlayer._navigationHandlers) {
        videoPlayer.removeEventListener('timeupdate', videoPlayer._navigationHandlers.timeupdate);
        videoPlayer.removeEventListener('timeupdate', videoPlayer._navigationHandlers.timeline);
        videoPlayer.removeEventListener('timeupdate', videoPlayer._navigationHandlers.transcript);
    }
    if (progressBar._navigationHandlers) {
        progressBar.removeEventListener('mousemove', progressBar._navigationHandlers.mousemove);
        progressBar.removeEventListener('mouseleave', progressBar._navigationHandlers.mouseleave);
        progressBar.removeEventListener('click', progressBar._navigationHandlers.click);
    }

    const navigationHandlers = {
        timeupdate: () => {
            updateTimeMarker(videoPlayer);
            updateChapterCaptions();
            updateActiveChapter();
        },
        timeline: updateTimelineHighlight,
        transcript: updateActiveTranscript
    };
    videoPlayer._navigationHandlers = navigationHandlers;

    const progressHandlers = {
        mousemove: (e) => {
        const rect = progressBar.getBoundingClientRect();
        const pos = (e.clientX - rect.left) / rect.width;
        const time = pos * videoPlayer.duration;
        hoverTime.textContent = formatTime(time);
        hoverTime.style.left = `${pos * 100}%`;
        hoverTime.style.display = 'block';
        },

        mouseleave: () => {
            hoverTime.style.display = 'none';
        },

        click: (e) => {
            const rect = progressBar.getBoundingClientRect();
            const pos = (e.clientX - rect.left) / rect.width;
            videoPlayer.currentTime = pos * videoPlayer.duration;
        }
    };
    progressBar._navigationHandlers = progressHandlers;
    progressBar.addEventListener('mousemove', progressHandlers.mousemove);
    progressBar.addEventListener('mouseleave', progressHandlers.mouseleave);
    progressBar.addEventListener('click', progressHandlers.click);

    // Update time marker on timeupdate
    videoPlayer.addEventListener('timeupdate', navigationHandlers.timeupdate);
    
    // Add timeupdate listener to highlight current thumbnail
    videoPlayer.addEventListener('timeupdate', navigationHandlers.timeline);
    
    // Add listener for transcript updates
    videoPlayer.addEventListener('timeupdate', navigationHandlers.transcript);
    updateChapterCaptions();
}

/**
 * Updates the time marker position
 * @param {HTMLVideoElement} videoPlayer - The video player element
 */
export function updateTimeMarker(videoPlayer) {
    const timeMarker = elements.timeMarker;
    const progress = (videoPlayer.currentTime / videoPlayer.duration) * 100;
    timeMarker.style.left = `${progress}%`;
}

/**
 * Updates the highlighted thumbnail in the timeline
 */
export function updateTimelineHighlight() {
    const videoPlayer = elements.videoPlayer;
    const currentTime = videoPlayer.currentTime;
    // Highlight by time among visible cards, since chapter cards can replace or join slide cards.
    const thumbnails = [...document.querySelectorAll('.timeline-item:not(.grouping-hidden) .timeline-thumbnail')];
    let currentThumbnailIndex = -1;
    thumbnails.forEach((thumbnail, index) => {
        if (currentTime + 0.01 >= Number(thumbnail.dataset.time)) currentThumbnailIndex = index;
    });
    document.querySelectorAll('.timeline-item.grouping-hidden .timeline-thumbnail.active')
        .forEach((thumbnail) => thumbnail.classList.remove('active'));
    
    // Update thumbnail highlighting
    thumbnails.forEach((thumbnail, index) => {
        if (index === currentThumbnailIndex) {
            thumbnail.classList.add('active');
            
            // Scroll the active thumbnail into view if it's not visible
            const timelineContainer = elements.thumbnailTimeline;
            const thumbnailItem = thumbnail.parentElement;
            scrollWithinContainer(timelineContainer, thumbnailItem, 'both');
        } else {
            thumbnail.classList.remove('active');
        }
    });
} 