// ocr.js - OCR functionality
import { createSlideDownloadButton } from './slide-downloads.js';

import { elements } from './elements.js';
import { state } from './main.js';
import { showNotification } from './ui.js';

// Store the SSE connection globally so it can be accessed from api.js
window.sseConnection = null;

/**
 * Connects to the SSE endpoint for OCR progress updates
 * @param {string} videoId - The YouTube video ID
 */
function connectToSSE(videoId) {
    // Close any existing connection
    if (window.sseConnection) {
        window.sseConnection.close();
        window.sseConnection = null;
    }
    
    // Create a new EventSource connection
    window.sseConnection = new EventSource(`/ocr_progress/${videoId}`);
    
    // Handle connection open
    window.sseConnection.onopen = () => {
        console.log('SSE connection established');
    };
    
    // Handle messages
    window.sseConnection.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            handleSSEEvent(data, videoId);
        } catch (error) {
            console.error('Error parsing SSE message:', error);
        }
    };
    
    // Handle errors
    window.sseConnection.onerror = (error) => {
        console.error('SSE connection error:', error);
        // Try to reconnect after a delay
        setTimeout(() => {
            if (window.sseConnection) {
                window.sseConnection.close();
                connectToSSE(videoId);
            }
        }, 5000);
    };
    
    return window.sseConnection;
}

/**
 * Handles SSE events for OCR progress
 * @param {Object} data - The event data
 * @param {string} videoId - The YouTube video ID
 */
function handleSSEEvent(data, videoId) {
    if (videoId !== state.currentVideoId) return;
    switch (data.event) {
        case 'connected':
            break;

        case 'ocr_progress':
            state.ocrProcessing = true;
            renderOcrStatus(data.data);
            if (data.data.partial_results) {
                updatePartialOcrResults(data.data.partial_results, videoId);
            }
            break;

        case 'ocr_stopping':
            renderOcrStatus({ ...data.data, status: 'stopping' });
            break;

        case 'ocr_complete':
        case 'ocr_cancelled':
            state.ocrProcessing = false;
            renderOcrStatus({ ...data.data, status: data.event === 'ocr_complete' ? 'complete' : 'stopped' });
            if (data.data.final_results) {
                updatePartialOcrResults(data.data.final_results, videoId, true);
            }
            fetchOcrResults(videoId);
            break;

        case 'ocr_error':
            state.ocrProcessing = false;
            renderOcrStatus({ ...data.data, status: 'error' });
            break;

        default:
            console.log('Unknown SSE event:', data);
    }
}

function formatElapsed(seconds) {
    const total = Math.max(0, Math.round(Number(seconds) || 0));
    return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
}

let ocrStatusSnapshot = null;
let ocrStatusTimer = null;

function ensureOcrStatusElement() {
    const container = elements.slideContentContainer;
    let panel = document.getElementById('ocrProgressContainer');
    if (!panel) {
        panel = document.createElement('div');
        panel.id = 'ocrProgressContainer';
        panel.className = 'ocr-progress-container';
        panel.setAttribute('role', 'status');
        panel.setAttribute('aria-live', 'polite');
        panel.innerHTML = `
            <div class="ocr-progress">
                <div class="ocr-progress-header">
                    <i class="fas fa-spinner fa-spin" aria-hidden="true"></i>
                    <span class="ocr-progress-title"></span>
                    <span class="ocr-progress-elapsed"></span>
                    <button type="button" id="stopOcrBtn" class="btn btn-danger">Stop OCR</button>
                </div>
                <div class="ocr-progress-bar"><div class="ocr-progress-fill"></div></div>
                <div class="ocr-progress-text"></div>
                <div class="ocr-progress-detail"></div>
            </div>`;
        panel.querySelector('#stopOcrBtn').addEventListener('click', () => stopOcrProcessing(state.currentVideoId));
        container.prepend(panel);
    }
    return panel;
}

function refreshOcrElapsed() {
    const panel = document.getElementById('ocrProgressContainer');
    if (!panel || !ocrStatusSnapshot) return;
    const live = ['queued', 'running', 'stopping'].includes(ocrStatusSnapshot.status);
    const elapsed = ocrStatusSnapshot.elapsed + (live ? (Date.now() - ocrStatusSnapshot.receivedAt) / 1000 : 0);
    const quiet = live ? (Date.now() - ocrStatusSnapshot.receivedAt) / 1000 : 0;
    panel.querySelector('.ocr-progress-elapsed').textContent = formatElapsed(elapsed);
    const detail = panel.querySelector('.ocr-progress-detail');
    if (live && quiet >= 20) {
        detail.textContent = `No update for ${Math.round(quiet)}s. The current slide may be large or the server may be busy; use Stop OCR to cancel.`;
        detail.classList.add('ocr-progress-warning');
    } else if (detail.classList.contains('ocr-progress-warning')) {
        detail.textContent = ocrStatusSnapshot.detail || '';
        detail.classList.remove('ocr-progress-warning');
    }
}

/**
 * Shows the current OCR step, replacing the previous one.
 * @param {Object} progressData - Latest OCR status from the server
 */
export function renderOcrStatus(progressData) {
    if (!progressData) return;
    const panel = ensureOcrStatusElement();
    const status = progressData.status || 'running';
    const engine = progressData.type === 'surya' ? 'Surya' : 'Tesseract';
    const live = ['queued', 'running', 'stopping'].includes(status);
    const suryaButton = document.getElementById('processSuryaBtn');
    if (suryaButton) suryaButton.hidden = live;
    const titles = {
        queued: `${engine} OCR queued`,
        running: `${engine} OCR running`,
        stopping: 'Stopping OCR',
        complete: `${engine} OCR complete`,
        stopped: 'OCR stopped',
        error: 'OCR failed',
    };
    const icons = {
        complete: 'fa-check-circle',
        stopped: 'fa-stop-circle',
        error: 'fa-exclamation-circle',
    };

    panel.dataset.status = status;
    panel.querySelector('.ocr-progress-header i').className = `fas ${live ? 'fa-spinner fa-spin' : icons[status] || 'fa-info-circle'}`;
    panel.querySelector('.ocr-progress-title').textContent = titles[status] || 'OCR';

    const percent = Number(progressData.percent);
    if (Number.isFinite(percent)) {
        panel.querySelector('.ocr-progress-fill').style.width = `${Math.max(0, Math.min(100, percent))}%`;
    }
    if (progressData.message || progressData.error) {
        panel.querySelector('.ocr-progress-text').textContent = progressData.message || progressData.error;
    }

    const parts = [];
    if (Number.isFinite(Number(progressData.total)) && Number(progressData.total) > 0) {
        parts.push(`${progressData.completed || 0}/${progressData.total} done`);
    }
    if (Number(progressData.failed) > 0) parts.push(`${progressData.failed} unreadable`);
    if (progressData.last_error && Number(progressData.failed) > 0) parts.push(`last problem: ${progressData.last_error}`);
    const detail = parts.join(' \u00b7 ');
    const detailElement = panel.querySelector('.ocr-progress-detail');
    detailElement.textContent = detail;
    detailElement.classList.remove('ocr-progress-warning');

    const stopButton = panel.querySelector('#stopOcrBtn');
    stopButton.hidden = !live;
    stopButton.disabled = status === 'stopping';
    stopButton.textContent = status === 'stopping' ? 'Stopping...' : 'Stop OCR';

    ocrStatusSnapshot = {
        status,
        detail,
        elapsed: Number(progressData.elapsed_seconds) || (ocrStatusSnapshot?.status === status || live ? ocrStatusSnapshot?.elapsed || 0 : 0),
        receivedAt: Date.now(),
    };
    clearInterval(ocrStatusTimer);
    ocrStatusTimer = live ? setInterval(refreshOcrElapsed, 1000) : null;
    refreshOcrElapsed();

    const startButton = document.getElementById('startOcrBtn');
    if (startButton) startButton.hidden = live;
}

export function resetOcrStatus() {
    clearInterval(ocrStatusTimer);
    ocrStatusTimer = null;
    ocrStatusSnapshot = null;
    document.getElementById('ocrProgressContainer')?.remove();
}

export async function stopOcrProcessing(videoId) {
    if (!videoId) return;
    const button = document.getElementById('stopOcrBtn');
    if (button) {
        button.disabled = true;
        button.textContent = 'Stopping...';
    }
    try {
        const response = await fetch(`/stop_ocr/${encodeURIComponent(videoId)}`, { method: 'POST' });
        const data = await response.json();
        if (!response.ok || !data.success) {
            throw new Error(data.error || 'Could not stop OCR processing');
        }
        if (!data.active) {
            state.ocrProcessing = false;
            renderOcrStatus({ status: 'stopped', message: 'OCR is stopped.' });
        }
    } catch (error) {
        if (button) {
            button.disabled = false;
            button.textContent = 'Stop OCR';
        }
        showNotification(`Could not stop OCR: ${error.message}`, 'error');
    }
}

/**
 * Updates the state with partial OCR results and refreshes the display
 * @param {Array} partialResults - Array of new OCR results
 * @param {string} videoId - The YouTube video ID
 * @param {boolean} isFinal - Whether these are the final results
 */
function updatePartialOcrResults(partialResults, videoId, isFinal = false) {
    if (!partialResults || !Array.isArray(partialResults) || partialResults.length === 0) {
        return;
    }
    
    console.log(`Received ${partialResults.length} partial OCR results`);
    
    // Initialize ocrResults array if it doesn't exist
    if (!state.ocrResults) {
        state.ocrResults = [];
    }
    
    // Add new results to the state
    // For each new result, check if it already exists (by scene_index and text)
    // and only add if it's new
    partialResults.forEach(newResult => {
        const isDuplicate = state.ocrResults.some(existingResult => 
            existingResult.scene_index === newResult.scene_index && 
            existingResult.text === newResult.text &&
            existingResult.ocr_class === newResult.ocr_class
        );
        
        if (!isDuplicate) {
            state.ocrResults.push(newResult);
        }
    });
    
    // Enable slide search if we have OCR results
    if (state.ocrResults.length > 0) {
        elements.slideSearch.disabled = false;
    }
    
    // Update the slide content display
    // If these are final results, indicate no pending OCR tasks
    updateSlideContentDisplay(isFinal ? 0 : null);
}

/**
 * Fetches OCR results for a video
 * @param {string} videoId - The YouTube video ID
 */
export async function fetchOcrResults(videoId) {
    try {
        const slideContentContainer = elements.slideContentContainer;

        // Connect to SSE for real-time updates if not already connected
        if (!window.sseConnection) {
            connectToSSE(videoId);
        }
        
        const response = await fetch(`/ocr_text/${videoId}`);
        const data = await response.json();
        
        if (data.success) {
            if (state.currentVideoId && state.currentVideoId !== videoId) return;
            state.ocrResults = data.ocr_results || [];
            state.ocrProcessing = Boolean(data.ocr_running);
            
            // Enable slide search if we have OCR results
            if (state.ocrResults.length > 0) {
                elements.slideSearch.disabled = false;
            }
            
            // Update the slide content display
            updateSlideContentDisplay(data.pending_ocr_count);
            if (data.ocr_status) {
                renderOcrStatus(data.ocr_status);
            }
            
            const needsOcr = (data.pending_ocr_count || 0) + (data.failed_ocr_count || 0) > 0;
            if (data.detections_complete && needsOcr && !document.getElementById('startOcrBtn')) {
                const startButton = document.createElement('button');
                startButton.id = 'startOcrBtn';
                startButton.className = 'btn btn-accent';
                const remaining = (data.pending_ocr_count || 0) + (data.failed_ocr_count || 0);
                startButton.textContent = `Start Slide OCR (${remaining} text elements on ${data.pending_slide_count} slides)`;
                startButton.hidden = Boolean(data.ocr_running);
                if (data.tesseract_available === false) {
                    startButton.disabled = true;
                    startButton.title = 'Tesseract OCR is not installed on the server.';
                }
                startButton.addEventListener('click', async () => {
                    startButton.disabled = true;
                    startButton.textContent = 'Starting OCR...';
                    try {
                        state.ocrProcessing = true;
                        const startResponse = await fetch(`/start_ocr/${encodeURIComponent(videoId)}`, { method: 'POST' });
                        const startData = await startResponse.json();
                        if (!startResponse.ok || !startData.success) {
                            throw new Error(startData.error || 'Could not start OCR');
                        }
                        startButton.remove();
                        if (startData.ocr_status) renderOcrStatus(startData.ocr_status);
                    } catch (error) {
                        state.ocrProcessing = false;
                        startButton.disabled = false;
                        startButton.textContent = 'Start Slide OCR';
                        renderOcrStatus({ status: 'error', message: error.message });
                    }
                });
                slideContentContainer.prepend(startButton);
            }
            if (data.tesseract_available === false && needsOcr && !data.ocr_status) {
                renderOcrStatus({
                    status: 'error',
                    message: 'Tesseract OCR is not installed on the server, so slide text cannot be read. Install it and restart the app.',
                });
            }

            // Add a button to trigger Surya OCR if we have no unmatched results yet
            const hasUnmatchedResults = state.ocrResults.some(result => result.ocr_class === 'unmatched');
            
            // Slide detection alone does not mean the primary OCR pass has finished.
            const primaryOcrComplete = data.detections_complete
                && !needsOcr && data.ocr_count > 0 && !data.ocr_running;
            if (!hasUnmatchedResults && primaryOcrComplete) {
                // Check if the button already exists
                if (!document.getElementById('processSuryaBtn')) {
                    const suryaButton = document.createElement('button');
                    suryaButton.id = 'processSuryaBtn';
                    suryaButton.className = 'btn btn-accent';
                    suryaButton.innerHTML = '<i class="fas fa-magic"></i> Process with Surya OCR';
                    suryaButton.style.marginBottom = '16px';
                    
                    // Add click handler to trigger Surya OCR
                    suryaButton.addEventListener('click', async () => {
                        try {
                            suryaButton.disabled = true;
                            suryaButton.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Processing with Surya OCR...';
                            
                            const response = await fetch(`/process_surya_ocr/${videoId}`, {
                                method: 'POST'
                            });
                            const data = await response.json();
                            
                            if (data.success) {
                                // Show success message
                                const successMessage = document.createElement('div');
                                successMessage.className = 'alert alert-info';
                                successMessage.style.display = 'block';
                                successMessage.innerHTML = `
                                    <i class="fas fa-check-circle"></i>
                                    ${data.message}. Results will appear shortly.
                                `;
                                slideContentContainer.insertBefore(successMessage, suryaButton);
                                
                                // Remove the button
                                suryaButton.remove();
                                
                                // We'll get updates via SSE, no need to poll
                            } else {
                                // Show error message
                                suryaButton.innerHTML = '<i class="fas fa-magic"></i> Process with Surya OCR';
                                suryaButton.disabled = false;
                                
                                const errorMessage = document.createElement('div');
                                errorMessage.className = 'alert alert-error';
                                errorMessage.style.display = 'block';
                                errorMessage.innerHTML = `
                                    <i class="fas fa-exclamation-circle"></i>
                                    Error: ${data.error || 'Failed to process with Surya OCR'}
                                `;
                                slideContentContainer.insertBefore(errorMessage, suryaButton);
                                
                                // Remove error message after 5 seconds
                                setTimeout(() => errorMessage.remove(), 5000);
                            }
                        } catch (error) {
                            console.error('Error triggering Surya OCR:', error);
                            suryaButton.innerHTML = '<i class="fas fa-magic"></i> Process with Surya OCR';
                            suryaButton.disabled = false;
                        }
                    });
                    
                    // Add the button to the slide content container
                    slideContentContainer.prepend(suryaButton);
                }
            } else {
                document.getElementById('processSuryaBtn')?.remove();
            }
        } else {
            console.error('Error fetching OCR results:', data.error);
        }
    } catch (error) {
        console.error('Error fetching OCR results:', error);
    }
}

/**
 * Updates the slide content display with OCR results
 * @param {number} pendingOcrCount - The number of pending OCR tasks
 */
export function updateSlideContentDisplay(pendingOcrCount = null) {
    const slideContentContainer = elements.slideContentContainer;
    
    // Save the progress container if it exists
    const progressContainer = document.getElementById('ocrProgressContainer');
    
    // Save the Surya button if it exists
    const suryaButton = document.getElementById('processSuryaBtn');
    
    // Clear existing content
    slideContentContainer.innerHTML = '';
    
    // Restore the progress container if it existed
    if (progressContainer) {
        slideContentContainer.appendChild(progressContainer);
    }
    
    // Live progress is shown only in the single #ocrProgressContainer status panel.
    if (!state.ocrResults || state.ocrResults.length === 0) {
        // Show the "coming soon" message if no OCR results
        if (state.ocrProcessing && pendingOcrCount > 0) {
            // Show loading indicator if OCR is in progress
            const comingSoon = document.createElement('div');
            comingSoon.className = 'coming-soon';
            comingSoon.innerHTML = `
                <i class="fas fa-spinner fa-spin"></i>
                <h3>Processing Text Elements</h3>
                <p>OCR is in progress. Text elements are being processed and will appear here as they become available.</p>
            `;
            slideContentContainer.appendChild(comingSoon);
        } else {
            const comingSoon = document.createElement('div');
            comingSoon.className = 'coming-soon';
            comingSoon.innerHTML = `
                <i class="fas fa-cogs"></i>
                <h3>Processing Text Elements</h3>
                <p>No text elements have been detected yet. This may take some time as scenes are processed.</p>
            `;
            slideContentContainer.appendChild(comingSoon);
        }
        
        // Restore the Surya button if it existed
        if (suryaButton) {
            slideContentContainer.appendChild(suryaButton);
        }
        
        return;
    }
    
    // Group OCR results by scene
    const resultsByScene = {};
    state.ocrResults.forEach(result => {
        const sceneIndex = result.scene_index;
        if (!resultsByScene[sceneIndex]) {
            resultsByScene[sceneIndex] = [];
        }
        resultsByScene[sceneIndex].push(result);
    });
    
    // Create sections for each scene
    Object.keys(resultsByScene).sort((a, b) => parseInt(a) - parseInt(b)).forEach(sceneIndex => {
        const sceneResults = resultsByScene[sceneIndex];
        const timestamp = sceneResults[0].timestamp;
        
        const sceneSection = document.createElement('div');
        sceneSection.className = 'ocr-scene-section';
        
        // Add scene header
        const sceneHeader = document.createElement('div');
        sceneHeader.className = 'ocr-scene-header';
        sceneHeader.innerHTML = `
            <span class="ocr-scene-timestamp">${timestamp}</span>
            <span>Scene ${parseInt(sceneIndex) + 1} (${sceneResults.length} text elements)</span>
        `;
        const downloadIndex = Number(sceneIndex);
        if (Number.isInteger(downloadIndex) && downloadIndex >= 0
            && downloadIndex < (state.videoScenes || []).length) {
            sceneHeader.appendChild(createSlideDownloadButton([downloadIndex],
                `Download slide ${downloadIndex + 1}`));
        }
        sceneSection.appendChild(sceneHeader);
        
        // Group results by OCR class
        const resultsByClass = {};
        sceneResults.forEach(result => {
            const ocrClass = result.ocr_class || 'text';
            if (!resultsByClass[ocrClass]) {
                resultsByClass[ocrClass] = [];
            }
            resultsByClass[ocrClass].push(result);
        });
        
        // Create sections for each OCR class
        Object.keys(resultsByClass).forEach(ocrClass => {
            const classResults = resultsByClass[ocrClass];
            if (classResults.length === 0) return;
            
            // Sort results by y position first (top to bottom), then by x position (left to right)
            classResults.sort((a, b) => {
                // Make sure bbox exists and has at least 2 elements
                if (!a.bbox || a.bbox.length < 2) return -1;
                if (!b.bbox || b.bbox.length < 2) return 1;
                
                // Get y coordinates (y1) from bbox [x1, y1, x2, y2]
                const aY = a.bbox[1];
                const bY = b.bbox[1];
                
                // If y positions are similar (within 20 pixels), sort by x position
                if (Math.abs(aY - bY) < 20) {
                    return a.bbox[0] - b.bbox[0]; // Sort by x1 (left to right)
                }
                
                // Otherwise sort by y position (top to bottom)
                return aY - bY;
            });
            const classHeader = document.createElement('div');
            classHeader.className = 'ocr-class-header';
            
            // Get appropriate icon and label for each class
            let icon, label;
            switch(ocrClass) {
                case 'title':
                    icon = 'fa-heading';
                    label = 'Titles';
                    break;
                case 'page-text':
                    icon = 'fa-file-alt';
                    label = 'Page Text';
                    break;
                case 'caption':
                    icon = 'fa-quote-right';
                    label = 'Captions';
                    break;
                case 'other-text':
                    icon = 'fa-font';
                    label = 'Other Text';
                    break;
                case 'unmatched':
                    icon = 'fa-question-circle';
                    label = 'Unmatched Text (Surya)';
                    break;
                default:
                    icon = 'fa-text-height';
                    label = 'Text';
            }
            
            classHeader.innerHTML = `
                <i class="fas ${icon}"></i>
                <span>${label} (${classResults.length})</span>
            `;
            
            sceneSection.appendChild(classHeader);
            
            // Add each OCR result for this class
            classResults.forEach(result => {
                const ocrItem = document.createElement('div');
                ocrItem.className = `ocr-item ocr-${ocrClass}`;
                
                // Add source indicator for unmatched results
                let sourceInfo = '';
                if (ocrClass === 'unmatched') {
                    sourceInfo = `<div class="ocr-source">(Surya OCR)</div>`;
                } else if (result.ocr_source) {
                    sourceInfo = ` <span class="ocr-source">(${result.ocr_source})</span>`;
                }
                
                ocrItem.innerHTML = `
                    <div class="ocr-text">${result.text}</div>
                    ${sourceInfo}
                `;
                
                // Add click handler to jump to timestamp
                ocrItem.addEventListener('click', () => {
                    elements.videoPlayer.currentTime = result.time_seconds;
                });
                
                // Add highlight animation for new items
                if (result.isNew) {
                    ocrItem.classList.add('ocr-item-new');
                    // Remove the isNew flag after animation
                    setTimeout(() => {
                        result.isNew = false;
                    }, 2000);
                }
                
                sceneSection.appendChild(ocrItem);
            });
        });
        
        slideContentContainer.appendChild(sceneSection);
    });
    
    // Restore the Surya button if it existed
    if (suryaButton) {
        slideContentContainer.appendChild(suryaButton);
    }
}

// Add CSS for progress bar
const style = document.createElement('style');
style.textContent = `
.ocr-progress-container {
    margin-bottom: 10px;
    width: 100%;
}

.ocr-progress {
    background: var(--surface-2, #f5f5f5);
    border: 1px solid var(--border, rgba(0,0,0,0.1));
    border-radius: 10px;
    padding: 8px 10px;
}

.ocr-progress-header {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 6px;
    font-weight: 600;
    font-size: 0.9em;
}

.ocr-progress-header i {
    color: var(--brand, #4a6cf7);
}

.ocr-progress-title {
    flex: 1;
}

.ocr-progress-elapsed {
    font-variant-numeric: tabular-nums;
    color: var(--text-2, #666);
    font-weight: 500;
}

.ocr-progress-header .btn {
    padding: 2px 8px;
    font-size: 0.8em;
}

.ocr-progress-bar {
    height: 6px;
    background: var(--border, #e0e0e0);
    border-radius: 999px;
    overflow: hidden;
    margin-bottom: 6px;
}

.ocr-progress-fill {
    height: 100%;
    background: var(--brand, #4a6cf7);
    width: 0%;
    transition: width 0.3s ease;
}

.ocr-progress-text,
.ocr-progress-detail {
    font-size: 0.82em;
    color: var(--text-2, #666);
    overflow-wrap: anywhere;
}

.ocr-progress-detail:empty {
    display: none;
}

.ocr-progress-warning {
    color: var(--warning-color, #b45309);
    font-weight: 600;
}

/* Animation for new OCR items */
@keyframes highlightNew {
    0% { background-color: rgba(74, 108, 247, 0.2); }
    100% { background-color: transparent; }
}

.ocr-item-new {
    animation: highlightNew 2s ease-out;
}
`;
document.head.appendChild(style);

// Add event listener to close SSE connection when the user navigates away
window.addEventListener('beforeunload', () => {
    if (window.sseConnection) {
        console.log('Closing SSE connection before unload');
        window.sseConnection.close();
        window.sseConnection = null;
    }
}); 