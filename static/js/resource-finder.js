(() => {
    const $ = (id) => document.getElementById(id);
    const results = $('resourceFinderResults');
    const status = $('resourceFinderStatus');
    const alertBox = $('resourceFinderAlert');
    const filters = $('resourceFinderFilters');
    let searchId = '';
    let nextOffset = 0;
    let total = 0;
    let loading = false;

    const el = (tag, text, className) => {
        const node = document.createElement(tag);
        if (text !== undefined) node.textContent = text;
        if (className) node.className = className;
        return node;
    };

    function selectedFilters() {
        return {
            resource_type: $('resourceFilterType').value,
            platform: $('resourceFilterPlatform').value,
            difficulty: $('resourceFilterDifficulty').value,
            language: $('resourceFilterLanguage').value,
        };
    }

    function populateFilter(selectId, values, placeholder) {
        const select = $(selectId);
        const selected = select.value;
        select.replaceChildren(new Option(placeholder, ''));
        Object.keys(values || {}).forEach((value) => select.add(new Option(value, value)));
        if ([...select.options].some((option) => option.value === selected)) select.value = selected;
    }

    function updateFilters(facets) {
        populateFilter('resourceFilterType', facets.type, 'All types');
        populateFilter('resourceFilterPlatform', facets.platform, 'All platforms');
        populateFilter('resourceFilterDifficulty', facets.difficulty, 'All levels');
        populateFilter('resourceFilterLanguage', facets.language, 'All languages');
        filters.hidden = false;
    }

    function renderResource(resource) {
        const card = el('article', undefined, 'result-card');
        const heading = el('h3');
        const link = el('a', resource.title || 'Untitled resource');
        link.href = resource.url;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        heading.appendChild(link);
        card.appendChild(heading);
        if (resource.description) card.appendChild(el('p', resource.description));
        if (resource.explanation) card.appendChild(el('p', resource.explanation));

        const metadata = el('div', undefined, 'tag-row');
        [
            resource.type,
            resource.platform,
            resource.difficulty,
            resource.language,
            resource.published,
        ].filter(Boolean).forEach((item) => metadata.appendChild(el('span', item, 'tag')));
        if (Number.isFinite(resource.score)) metadata.appendChild(el('span', `Relevance ${Math.round(resource.score * 100)}%`, 'tag tag-brand'));
        card.appendChild(metadata);
        if (resource.covered_concepts?.length) {
            card.appendChild(el('p', `Topics: ${resource.covered_concepts.join(', ')}`));
        }
        return card;
    }

    function setStatus(text, busy) {
        status.textContent = text || '';
        status.classList.toggle('is-busy', Boolean(busy && text));
    }

    function setAlert(text, kind) {
        alertBox.textContent = text || '';
        alertBox.className = 'alert alert-' + (kind || 'info');
    }

    function renderPage(payload, append) {
        if (!append) results.replaceChildren();
        payload.results.forEach((resource) => results.appendChild(renderResource(resource)));
        if (!payload.results.length && !append) {
            results.appendChild(el('div', 'No resources matched. Try a broader topic or other filters.', 'empty-state'));
        }
        nextOffset = payload.offset + payload.results.length;
        total = payload.total;
        $('resourceFinderMore').hidden = !payload.has_more;

        const parts = [`Showing ${Math.min(nextOffset, total)} of ${total} matching resources.`];
        if (payload.analysis?.topic) parts.push(`Topic: ${payload.analysis.topic}.`);
        if (payload.ranking_method) parts.push(`Ranking: ${payload.ranking_method}.`);
        setStatus(parts.join(' '));
        const notices = [...(payload.notices || []), ...(payload.warnings || [])];
        setAlert(notices.join(' '), 'warn');
        updateFilters(payload.facets);
    }

    async function readResponse(response) {
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(payload.detail || `Request failed (${response.status}).`);
        return payload;
    }

    async function loadPage({ append = false } = {}) {
        if (!searchId || loading) return;
        loading = true;
        $('resourceFinderMore').disabled = true;
        if (!append) setStatus('Loading resources…', true);
        try {
            const params = new URLSearchParams({ offset: append ? String(nextOffset) : '0' });
            Object.entries(selectedFilters()).forEach(([key, value]) => {
                if (value) params.set(key, value);
            });
            const response = await fetch(`/resource_finder/${encodeURIComponent(searchId)}?${params}`);
            renderPage(await readResponse(response), append);
        } catch (error) {
            setStatus('');
            setAlert(error.message, 'error');
        } finally {
            loading = false;
            $('resourceFinderMore').disabled = false;
        }
    }

    async function search() {
        const file = $('resourceFile').files[0];
        const topic = $('resourceTopic').value.trim();
        if (!topic && !file) {
            setStatus('');
            setAlert('Enter a topic or choose a source document.', 'warn');
            return;
        }

        const form = new FormData();
        form.append('topic', topic);
        if (file) form.append('file', file);
        document.querySelectorAll('input[name="resourceType"]:checked')
            .forEach((input) => form.append('types', input.value));
        form.append('use_llm', String($('resourceUseLlm').checked));
        form.append('allow_cloud_fallback', String($('resourceCloudFallback').checked));

        searchId = '';
        results.replaceChildren();
        filters.hidden = true;
        $('resourceFinderMore').hidden = true;
        $('resourceSearchBtn').disabled = true;
        status.textContent = '';
        setAlert('');
        setStatus('Analyzing the topic and searching learning providers…', true);
        try {
            const response = await fetch('/resource_finder/search', { method: 'POST', body: form });
            const payload = await readResponse(response);
            searchId = payload.search_id;
            renderPage(payload, false);
        } catch (error) {
            setStatus('');
            setAlert(error.message, 'error');
        } finally {
            $('resourceSearchBtn').disabled = false;
        }
    }

    $('resourceSearchBtn').addEventListener('click', search);
    $('resourceFinderMore').addEventListener('click', () => loadPage({ append: true }));
    ['resourceFilterType', 'resourceFilterPlatform', 'resourceFilterDifficulty', 'resourceFilterLanguage']
        .forEach((id) => $(id).addEventListener('change', () => loadPage()));
})();
