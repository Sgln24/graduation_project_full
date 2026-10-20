import { state } from './main.js';
import { elements } from './elements.js';
import { startElapsedTimer } from './progress-time.js';

export function initVideoSummary() {
    const button = document.getElementById('generateVideoSummaryBtn');
    const paragraph = document.getElementById('videoSummaryText');
    const status = document.getElementById('videoSummaryStatus');
    let selection = 0;
    let currentKey = '';
    let busyKey = '';
    const snapshot = () => ({
        videoId: state.currentVideoId, transcript: state.currentTranscript || [],
        transcript_language: state.currentTranslationLanguage || '',
    });
    const key = value => JSON.stringify([value.videoId, value.transcript_language,
        value.transcript.map(item => item.text)]);
    const refresh = async () => {
        const current = snapshot();
        const nextKey = key(current);
        button.disabled = !current.videoId || !current.transcript.length || Boolean(busyKey);
        if (nextKey === currentKey) return;
        currentKey = nextKey;
        const generation = ++selection;
        paragraph.textContent = '';
        status.textContent = '';
        if (!current.videoId || !current.transcript.length) {
            status.textContent = 'Load a transcript to generate a short whole-video summary.';
            return;
        }
        status.textContent = 'Loading saved summary...';
        try {
            const response = await fetch(`/video_summary/${encodeURIComponent(current.videoId)}/load`, {
                method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(current),
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.detail || `Could not load summary (${response.status})`);
            if (generation !== selection || nextKey !== key(snapshot())) return;
            paragraph.textContent = data.summary || '';
            status.textContent = data.exists ? 'Saved summary of the open transcript.' : 'Generate a short paragraph covering the whole video.';
        } catch (error) {
            if (generation === selection) status.textContent = error.message;
        }
    };
    button.addEventListener('click', async () => {
        const current = snapshot();
        const requestKey = key(current);
        if (!current.videoId || !current.transcript.length || busyKey) return;
        busyKey = requestKey;
        ++selection;
        button.disabled = true;
        button.textContent = 'Generating summary...';
        status.textContent = 'Reading the whole transcript and writing one short paragraph...';
        const stopElapsed = startElapsedTimer(status);
        try {
            const response = await fetch(`/video_summary/${encodeURIComponent(current.videoId)}/generate`, {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({...current, model: elements.summaryModel?.value || null}),
            });
            const data = await response.json();
            if (!response.ok || !data.success) throw new Error(data.detail || data.error || 'Summary generation failed.');
            if (requestKey !== key(snapshot())) return;
            ++selection;
            paragraph.textContent = data.summary;
            status.textContent = 'Summary saved for this transcript.';
        } catch (error) {
            if (requestKey === key(snapshot())) status.textContent = `${error.message} Any previously saved summary was kept.`;
        } finally {
            stopElapsed();
            busyKey = '';
            button.textContent = 'Generate summary';
            button.disabled = !state.currentVideoId || !state.currentTranscript?.length;
        }
    });
    for (const event of ['transcriptUpdated', 'videoSelectionChanged']) {
        document.addEventListener(event, () => void refresh());
    }
    void refresh();
}
