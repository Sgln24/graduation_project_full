import { state } from './main.js';
import { showError } from './ui.js';

export async function deleteSavedExport(item, kind = 'exports') {
    if (!window.confirm(`Permanently delete "${item.title}" and its exported files? This cannot be undone.`)) return false;
    const response = await fetch(`/library/exports/${kind}/${encodeURIComponent(item.id)}?confirm=true`, {method: 'DELETE'});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Could not delete export.');
    return true;
}

export function initLibrary() {
    const button = document.getElementById('manageLibraryBtn');
    const dialog = document.getElementById('libraryDialog');
    const list = document.getElementById('libraryItems');
    const status = document.getElementById('libraryStatus');
    async function refresh() {
        status.textContent = 'Loading saved files...';
        const response = await fetch('/library');
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || 'Could not load library.');
        list.replaceChildren();
        const includeExports = document.getElementById('deleteUploadExports');
        function row(title, remove) {
            const item = document.createElement('div');
            item.className = 'library-row';
            const name = document.createElement('span');
            name.textContent = title;
            const action = document.createElement('button');
            action.type = 'button';
            action.className = 'btn btn-secondary btn-action';
            action.textContent = 'Delete';
            action.setAttribute('aria-label', `Delete ${title}`);
            action.addEventListener('click', async () => {
                action.disabled = true;
                try { if (await remove()) await refresh(); }
                catch (error) { status.textContent = error.message; }
                finally { action.disabled = false; }
            });
            item.append(name, action);
            list.appendChild(item);
        }
        const heading = text => {
            const h = document.createElement('h3');
            h.textContent = text;
            list.appendChild(h);
        };
        heading('Saved uploads and chapter projects');
        for (const video of data.videos) row(video.filename, async () => {
            const exports = includeExports.checked;
            if (!window.confirm(`Permanently delete "${video.filename}", its transcript, chapters and slides? `
                    + (exports ? 'Its exports will also be deleted.' : 'Completed exports will be kept.')
                    + ' Independent chapter projects are not deleted. This cannot be undone.')) return false;
            let released = null;
            if (state.currentVideoId === video.video_id) {
                const player = document.querySelector('video');
                released = {player, src: player.getAttribute('src'), time: player.currentTime,
                    captionExportId: player.dataset.captionExportId, captionVideoId: player.dataset.captionVideoId,
                    tracks: Array.from(player.querySelectorAll('track[data-caption-copy], track[data-chapter-caption]'),
                        element => ({element, mode: element.track.mode, default: element.default}))};
                player.pause();
                player.removeAttribute('src');
                player.load();
            }
            try {
                const response = await fetch(`/library/videos/${encodeURIComponent(video.video_id)}?confirm=true&include_exports=${exports}`, {method: 'DELETE'});
                const data = await response.json();
                if (!response.ok) throw new Error(data.detail || 'Could not delete upload.');
            } catch (error) {
                if (released?.src && state.currentVideoId === video.video_id) {
                    released.player.addEventListener('loadedmetadata', () => {
                        if (state.currentVideoId !== video.video_id || released.player.getAttribute('src') !== released.src) return;
                        released.player.currentTime = released.time;
                        released.player.dataset.captionExportId = released.captionExportId || '';
                        released.player.dataset.captionVideoId = released.captionVideoId || '';
                        for (const track of released.tracks) {
                            track.element.default = track.default;
                            released.player.appendChild(track.element);
                            track.element.track.mode = track.mode;
                        }
                    }, {once: true});
                    released.player.src = released.src;
                    released.player.load();
                }
                throw error;
            }
            document.querySelector(`#uploadedVideos option[value="${video.video_id}"]`)?.remove();
            if (state.currentVideoId === video.video_id) {
                // Reload clears all player/transcript state referencing the deleted upload.
                location.reload();
            }
            return true;
        });
        heading('Exports');
        for (const item of data.exports) row(`${item.title} (${item.status})`, () => deleteSavedExport(item, item.store));
        status.textContent = 'Deletion is permanent. Running jobs cannot be deleted.';
    }
    button.addEventListener('click', async () => {
        dialog.showModal();
        try { await refresh(); } catch (error) { status.textContent = error.message; showError(error.message); }
    });
}
