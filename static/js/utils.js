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
 * Saves a generated file using the browser's location picker when available.
 * Browsers without the File System Access API fall back to their normal download flow.
 */
export async function saveBlobToUserLocation(blob, filename, container = document.body) {
    const picker = window.showSaveFilePicker;
    if (typeof picker === 'function') {
        const extension = filename.includes('.') ? filename.split('.').pop().toLowerCase() : '';
        const mimeTypes = {
            zip: 'application/zip',
            docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            pdf: 'application/pdf',
            mp4: 'video/mp4',
        };
        try {
            const handle = await picker({
                suggestedName: filename,
                types: extension && mimeTypes[extension]
                    ? [{ description: extension.toUpperCase(), accept: { [mimeTypes[extension]]: [`.${extension}`] } }]
                    : undefined,
            });
            const writable = await handle.createWritable();
            await writable.write(blob);
            await writable.close();
            return;
        } catch (error) {
            const isGestureError = (
                error instanceof DOMException
                && (error.name === 'NotAllowedError' || error.name === 'SecurityError')
            ) || (
                error instanceof Error
                && /showSaveFilePicker|user gesture/i.test(error.message)
            );
            if (!isGestureError) {
                throw error;
            }
            console.warn('Save picker was unavailable outside a user gesture; using browser download.', error);
        }
    }

    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    link.style.display = 'none';
    container.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
}