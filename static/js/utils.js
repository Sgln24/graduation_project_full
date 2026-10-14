// utils.js - Utility functions

export function openOptionsDialog(dialog) {
    document.querySelectorAll('dialog.options-dialog[open]').forEach(open => {
        if (open !== dialog) open.close();
    });
    if (!dialog.open) dialog.show();
}

/**
 * Extracts the YouTube video ID from a URL
 * @param {string} url - The YouTube URL
 * @returns {string|null} - The video ID or null if not found
 */
export function extractVideoId(url) {
    const pattern = /(?:v=|\/)([0-9A-Za-z_-]{11})/;
    const match = url.match(pattern);
    return match ? match[1] : null;
}

/**
 * Formats seconds into MM:SS format
 * @param {number} seconds - The time in seconds
 * @returns {string} - Formatted time string
 */
export function formatTime(seconds) {
    const minutes = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    return `${minutes.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
}

/**
 * Sends a generated file through one browser download flow.
 * Browser download settings control whether to prompt for a location.
 */
export async function saveBlobToUserLocation(blob, filename, container = document.body) {
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    link.style.display = 'none';
    container.appendChild(link);
    try {
        link.click();
    } finally {
        link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 60000);
    }
}