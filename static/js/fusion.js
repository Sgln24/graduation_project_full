(() => {
    const $ = (id) => document.getElementById(id);
    const MAX_VIDEOS = 5;

    const el = (tag, text, className) => {
        const node = document.createElement(tag);
        if (text !== undefined) node.textContent = text;
        if (className) node.className = className;
        return node;
    };

    async function loadVideos() {
        const box = $('fusionVideos');
        box.replaceChildren(el('small', 'Loading...'));
        try {
            const response = await fetch('/uploaded_videos');
            const payload = await response.json();
            const videos = payload.videos || [];
            box.replaceChildren();
            if (!videos.length) box.appendChild(el('small', 'No saved lectures yet. You can still upload documents.'));
            videos.forEach((video) => {
                const label = el('label');
                label.style.display = 'block';
                const input = document.createElement('input');
                input.type = 'checkbox';
                input.value = video.video_id;
                label.append(input, ' ' + video.filename);
                box.appendChild(label);
            });
        } catch (error) {
            box.replaceChildren(el('small', 'Could not load saved lectures.'));
        }
    }

    async function checkRuntime() {
        const note = $('fusionRuntime');
        try {
            const response = await fetch('/fusion_runtime_status');
            const payload = await response.json();
            note.textContent = response.ok
                ? 'Local model ready: ' + payload.model
                : 'Local model not ready (' + (payload.error || 'Ollama unavailable') + ').'
                    + (payload.cloud_fallback_allowed ? ' Cloud fallback (Groq) is available.' : ' Set GROQ_API_KEY to enable cloud fallback.');
        } catch (error) {
            note.textContent = 'Could not check the local model.';
        }
    }

    async function run() {
        const selected = [...document.querySelectorAll('#fusionVideos input:checked')].map((i) => i.value);
        const files = [...$('fusionFiles').files];
        const status = $('fusionStatus');
        const result = $('fusionResult');
        result.replaceChildren();
        if (selected.length > MAX_VIDEOS) { status.textContent = 'Select at most ' + MAX_VIDEOS + ' lectures.'; return; }
        if (!selected.length && !files.length) { status.textContent = 'Select a lecture or add a document.'; return; }
        const form = new FormData();
        form.append('video_sources', selected.join(','));
        form.append('allow_cloud_fallback', $('fusionCloud').checked ? 'true' : 'false');
        files.forEach((file) => form.append('files', file));
        status.textContent = 'Creating fused lecture...';
        $('fusionRunBtn').disabled = true;
        try {
            const response = await fetch('/fusion_jobs', { method: 'POST', body: form });
            const payload = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(payload.detail || 'Fusion failed (' + response.status + ').');
            status.textContent = 'Done: ' + payload.title + ' (' + payload.source_count + ' sources, ' + payload.local_model.runtime + ' ' + payload.local_model.model + ').';
            [['PowerPoint (.pptx)', payload.pptx_url], ['PDF', payload.pdf_url]].forEach(([name, href]) => {
                const link = el('a', 'Download ' + name, 'btn btn-secondary');
                link.href = href;
                link.style.marginRight = '8px';
                result.appendChild(link);
            });
            if ((payload.relationships || []).length) {
                const list = el('ul');
                payload.relationships.slice(0, 10).forEach((r) => list.appendChild(el('li', typeof r === 'string' ? r : JSON.stringify(r))));
                result.append(el('h3', 'Cross-source relationships'), list);
            }
        } catch (error) {
            status.textContent = error.message;
        } finally {
            $('fusionRunBtn').disabled = false;
        }
    }

    $('fusionRunBtn').addEventListener('click', run);
    loadVideos();
    checkRuntime();
})();
