// transcript.js - Transcript-related functionality

import { elements } from './elements.js';
import { state } from './main.js';
import { scrollWithinContainer } from './scroll-utils.js';

/**
 * Loads the transcript data into the application
 * @param {Array} transcript - The transcript data
 */
export function loadTranscript(transcript) {
    state.currentTranscript = Array.isArray(transcript)
        ? transcript
        : (Array.isArray(transcript?.segments) ? transcript.segments : []);
    if (elements.generateSummaryBtn) {
        elements.generateSummaryBtn.disabled =
            state.summaryGenerationInProgress || state.currentTranscript.length === 0;
    }
    updateTranscriptDisplay();
    
    // Dispatch event that transcript is loaded
    const event = new CustomEvent('transcriptUpdated', { detail: transcript });
    document.dispatchEvent(event);
}

/**
 * Updates the transcript display based on current settings
 */
export function updateTranscriptDisplay() {
    const transcriptContainer = elements.transcriptContainer;
    transcriptContainer.innerHTML = '';
    
    // Clear any existing search
    if (elements.transcriptSearch) {
        elements.transcriptSearch.value = '';
        elements.clearSearchBtn.classList.remove('visible');
        // Clear search results
        state.searchResults = [];
        state.currentSearchIndex = -1;
        elements.searchResultsCount.textContent = '0/0';
        elements.prevSearchBtn.disabled = true;
        elements.nextSearchBtn.disabled = true;
    }
    
    state.currentTranscript.forEach((item, index) => {
        const div = document.createElement('div');
        div.className = 'transcript-line';
        
        // Format timestamp
        const minutes = Math.floor(item.start / 60);
        const seconds = Math.floor(item.start % 60);
        const timestamp = `${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}`;
        
        // Create HTML with or without visible timestamp
        const speaker = item.speaker_name || item.speaker;
        const speakerMarkup = speaker ? `<span class="transcript-speaker">${speaker}</span>` : '';
        if (state.showTimestamps) {
            div.innerHTML = `
                <span class="transcript-timestamp">${timestamp}</span>
                ${speakerMarkup}
                <span class="transcript-text">${item.text}</span>
            `;
        } else {
            div.innerHTML = `${speakerMarkup}<span class="transcript-text">${item.text}</span>`;
            // Still store the timestamp as a data attribute for seeking
            div.dataset.timestamp = timestamp;
            div.dataset.time = item.start;
        }
        
        div.onclick = (event) => {
            if (div.closest('.is-inline-editing') || event.target.closest('textarea, input')) return;
            const time = parseFloat(item.start);
            if (!isNaN(time)) {
                elements.videoPlayer.currentTime = time;
            }
        };
        
        transcriptContainer.appendChild(div);
    });
}

/**
 * Updates the active transcript line based on current video time
 */
export function updateActiveTranscript() {
    if (elements.transcriptContainer?.classList.contains('is-inline-editing')) return;
    const videoPlayer = elements.videoPlayer;
    const currentTime = videoPlayer.currentTime;
    
    if (isNaN(currentTime) || !state.currentTranscript || state.currentTranscript.length === 0) {
        return;
    }

    // Find the appropriate transcript line
    let activeIndex = state.currentTranscript.findIndex((item, index) => {
        const currentStart = item.start;
        const nextStart = index < state.currentTranscript.length - 1 
            ? state.currentTranscript[index + 1].start 
            : currentStart + item.duration;
        return currentTime >= currentStart && currentTime < nextStart;
    });

    // If no exact match found, find the closest previous transcript line
    if (activeIndex === -1) {
        activeIndex = state.currentTranscript.reduce((closest, item, index) => {
            if (item.start <= currentTime && 
                (closest === -1 || item.start > state.currentTranscript[closest].start)) {
                return index;
            }
            return closest;
        }, -1);
    }

    const transcriptLines = document.querySelectorAll('.transcript-line');
    
    // Remove all active classes first
    transcriptLines.forEach(line => line.classList.remove('active'));

    // Add active class to current line if found
    if (activeIndex !== -1 && transcriptLines[activeIndex]) {
        const activeLine = transcriptLines[activeIndex];
        activeLine.classList.add('active');

        // Only scroll if not visible
        const container = elements.transcriptContainer;
        scrollWithinContainer(container, activeLine);
    }
} 