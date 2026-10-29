(() => {
    const { el, setStatus, setAlert, readJson } = window.AppUI;
    const $ = (id) => document.getElementById(id);
    const MAX_VIDEOS = 5;

    async function loadVideos() {
        const box = $('fusionVideos');
        box.replaceChildren(el('div', 'empty-state', 'Loading saved lectures…'));
        try {
            const payload = await readJson(await fetch('/uploaded_videos'), 'Could not load saved lectures.');
            const videos = payload.videos || [];
            box.replaceChildren();
            if (!videos.length) {
                box.appendChild(el('div', 'empty-state', 'No saved lectures yet. Upload a lecture in Lecture tools, or add documents below.'));
                return;
            }
            videos.forEach((video) => {
                const label = el('label', 'check');
                const input = document.createElement('input');
                input.type = 'checkbox';
                input.value = video.video_id;
                label.append(input, el('span', '', video.filename));
                box.appendChild(label);
            });
        } catch (error) {
            box.replaceChildren(el('div', 'alert alert-error', error.message));
        }
    }

    async function checkRuntime() {
        const note = $('fusionRuntime');
        try {
            const response = await fetch('/fusion_runtime_status');
            const payload = await response.json().catch(() => ({}));
            if (response.ok) {
                setAlert(note, 'Local model ready: ' + payload.model + '. Fusion will run on your machine.', 'ok');
            } else {
                const cloud = payload.cloud_fallback_allowed
                    ? 'Cloud fallback (Groq) is available.'
                    : 'Set GROQ_API_KEY to enable cloud fallback.';
                setAlert(note, 'Local model not ready — ' + (payload.error || 'Ollama unavailable') + ' ' + cloud, 'warn');
            }
        } catch (error) {
            setAlert(note, 'Could not check the local model runtime.', 'warn');
        }
    }

    function renderResult(payload) {
        const result = $('fusionResult');
        result.replaceChildren();
        const downloads = el('div', 'download-row');
        [['Download PowerPoint (.pptx)', payload.pptx_url], ['Download PDF', payload.pdf_url]].forEach(([name, href]) => {
            const link = el('a', 'btn btn-secondary link-button', name);
            link.href = href;
            downloads.appendChild(link);
        });
        result.appendChild(downloads);
        const relationships = payload.relationships || [];
        result.appendChild(el('h3', '', 'Cross-source relationships'));
        if (!relationships.length) {
            result.appendChild(el('div', 'empty-state', 'No strong overlap was detected between the selected sources.'));
            return;
        }
        const list = el('div', 'result-grid');
        relationships.slice(0, 10).forEach((item) => {
            const card = el('article', 'result-card');
            card.appendChild(el('p', '', typeof item === 'string' ? item : (item.explanation || [item.source, item.target].filter(Boolean).join(' ↔ '))));
            if (item.type) card.appendChild(el('div', 'tag-row')).appendChild(el('span', 'tag tag-brand', item.type));
            list.appendChild(card);
        });
        result.appendChild(list);
    }

    async function run() {
        const selected = [...document.querySelectorAll('#fusionVideos input:checked')].map((i) => i.value);
        const files = [...$('fusionFiles').files];
        const status = $('fusionStatus');
        $('fusionResult').replaceChildren();
        if (selected.length > MAX_VIDEOS) { setStatus(status, 'Select at most ' + MAX_VIDEOS + ' lectures.'); return; }
        if (!selected.length && !files.length) { setStatus(status, 'Select a lecture or add a document first.'); return; }
        const form = new FormData();
        form.append('video_sources', selected.join(','));
        form.append('allow_cloud_fallback', $('fusionCloud').checked ? 'true' : 'false');
        files.forEach((file) => form.append('files', file));
        setStatus(status, 'Creating the fused lecture…', 'busy');
        $('fusionRunBtn').disabled = true;
        try {
            const payload = await readJson(await fetch('/fusion_jobs', { method: 'POST', body: form }), 'Fusion failed.');
            setStatus(status, 'Done: ' + payload.title + ' (' + payload.source_count + ' sources, ' + payload.local_model.runtime + ' ' + payload.local_model.model + ').');
            renderResult(payload);
        } catch (error) {
            setStatus(status, '');
            $('fusionResult').replaceChildren(el('div', 'alert alert-error', error.message));
        } finally {
            $('fusionRunBtn').disabled = false;
        }
    }

    $('fusionRunBtn').addEventListener('click', run);
    loadVideos();
    checkRuntime();
})();
