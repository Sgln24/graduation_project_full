(() => {
    const $ = (id) => document.getElementById(id);
    const dialog = $('mindmapDialog');
    const body = $('mindmapBody');
    const status = $('mindmapStatus');
    let data = null;
    let view = 'diagram';

    const el = (tag, className, text) => {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    };

    const videoId = () => (window.lectureSeekState && window.lectureSeekState.currentVideoId) || '';

    function seek(reference) {
        const player = $('videoPlayer');
        if (player && reference && Number.isFinite(reference.seconds)) {
            player.currentTime = reference.seconds;
            dialog.close();
        }
    }

    function refButton(reference, label) {
        const button = el('button', 'mindmap-ref', label || reference.timestamp);
        button.type = 'button';
        let title = 'Jump to ' + reference.timestamp;
        if (reference.slide_index !== null && reference.slide_index !== undefined) title += ' (slide ' + (reference.slide_index + 1) + ')';
        button.title = title + ': ' + reference.text;
        button.addEventListener('click', () => seek(reference));
        return button;
    }

    function listSection(heading, items, render) {
        if (!items || !items.length) return null;
        const wrap = el('div', 'mindmap-block');
        wrap.appendChild(el('strong', '', heading));
        const list = el('ul');
        items.forEach((item) => list.appendChild(render(item)));
        wrap.appendChild(list);
        return wrap;
    }

    function textWithRef(item) {
        const li = el('li');
        li.appendChild(document.createTextNode(item.text + ' '));
        if (item.reference) li.appendChild(refButton(item.reference));
        return li;
    }

    function renderNode(node) {
        const details = el('details', 'mindmap-node mindmap-' + node.kind);
        if (node.kind === 'root') details.open = true;
        const summary = el('summary');
        summary.appendChild(el('span', 'mindmap-title', node.title));
        if (node.references && node.references[0]) summary.appendChild(refButton(node.references[0]));
        details.appendChild(summary);
        const content = el('div', 'mindmap-content');
        if (node.explanation) content.appendChild(el('p', 'mindmap-explanation', node.explanation));
        [
            listSection('Key points', node.key_points, textWithRef),
            listSection('Examples', node.examples, textWithRef),
            listSection('Formulas', node.formulas, (f) => { const li = textWithRef(f); li.firstChild.textContent = f.text + ' '; li.classList.add('mindmap-formula'); return li; }),
        ].forEach((block) => block && content.appendChild(block));
        if (node.kind === 'concept' && node.references) {
            node.references.forEach((r) => content.appendChild(refButton(r, 'Source ' + r.timestamp)));
        }
        if (node.method) content.appendChild(el('small', 'mindmap-method', node.method === 'extractive' ? 'Extracted from transcript' : 'AI explained (' + node.method + ')'));
        (node.children || []).forEach((child) => content.appendChild(renderNode(child)));
        details.appendChild(content);
        return details;
    }

    function renderFull() {
        body.replaceChildren(renderNode(data.root));
        const titles = Object.fromEntries(data.root.children.map((n) => [n.id, n.title]));
        const related = data.relationships.filter((r) => r.type === 'related');
        if (related.length) {
            const wrap = el('div', 'mindmap-relations');
            wrap.appendChild(el('h3', '', 'How ideas connect'));
            const list = el('ul');
            related.forEach((r) => list.appendChild(el('li', '', titles[r.source] + ' \u2194 ' + titles[r.target] + ' (shared: ' + r.label + ')')));
            wrap.appendChild(list);
            body.appendChild(wrap);
        }
    }

    function renderRevision() {
        const list = el('ol', 'mindmap-revision');
        data.revision.forEach((item) => {
            const li = el('li');
            const head = el('strong', '', item.title + ' ');
            head.appendChild(refButton({ seconds: item.seconds, timestamp: item.timestamp, text: item.title, slide_index: null }));
            li.appendChild(head);
            const bullets = el('ul');
            item.bullets.forEach((b) => bullets.appendChild(el('li', '', b)));
            li.appendChild(bullets);
            if (item.formulas.length) li.appendChild(el('div', 'mindmap-formula', 'Formulas: ' + item.formulas.join('; ')));
            if (item.terms.length) li.appendChild(el('small', '', 'Key terms: ' + item.terms.join(', ')));
            list.appendChild(li);
        });
        body.replaceChildren(list);
    }

    const SVG_NS = 'http://www.w3.org/2000/svg';
    const svgEl = (tag, attrs, text) => {
        const node = document.createElementNS(SVG_NS, tag);
        Object.entries(attrs || {}).forEach(([key, value]) => node.setAttribute(key, value));
        if (text !== undefined) node.textContent = text;
        return node;
    };
    const SIZES = { root: { w: 190, h: 60 }, section: { w: 220, h: 52 }, concept: { w: 190, h: 40 } };
    const COLUMN_X = { root: 0, section: 260, concept: 540 };
    const expanded = new Set();
    let selectedId = null;
    let box = null;
    let canvas = null;
    let svg = null;

    function wrapLabel(text, limit) {
        const words = String(text || '').split(/\s+/);
        const lines = [''];
        words.forEach((word) => {
            const current = lines[lines.length - 1];
            if ((current + ' ' + word).trim().length <= limit) {
                lines[lines.length - 1] = (current + ' ' + word).trim();
            } else if (lines.length < 2) {
                lines.push(word);
            } else {
                lines[1] = lines[1].slice(0, Math.max(0, limit - 1)) + '\u2026';
            }
        });
        return lines.map((line) => (line.length > limit ? line.slice(0, limit - 1) + '\u2026' : line));
    }

    function layoutDiagram() {
        const sections = data.root.children || [];
        const nodes = [];
        const edges = [];
        let cursor = 0;
        const placed = sections.map((section) => {
            const kids = expanded.has(section.id) ? (section.children || []) : [];
            const kidsHeight = kids.length ? kids.length * 50 - 10 : 0;
            const slot = Math.max(SIZES.section.h, kidsHeight);
            const top = cursor;
            cursor += slot + 18;
            return { section, kids, slot, top, kidsHeight };
        });
        const total = Math.max(cursor - 18, SIZES.root.h);
        const rootNode = { node: data.root, kind: 'root', x: COLUMN_X.root, y: total / 2 - SIZES.root.h / 2 };
        nodes.push(rootNode);
        placed.forEach(({ section, kids, slot, top, kidsHeight }) => {
            const sectionNode = { node: section, kind: 'section', x: COLUMN_X.section, y: top + slot / 2 - SIZES.section.h / 2, hasKids: (section.children || []).length > 0 };
            nodes.push(sectionNode);
            edges.push([rootNode, sectionNode]);
            kids.forEach((child, index) => {
                const childNode = { node: child, kind: 'concept', x: COLUMN_X.concept, y: top + (slot - kidsHeight) / 2 + index * 50 };
                nodes.push(childNode);
                edges.push([sectionNode, childNode]);
            });
        });
        return { nodes, edges, total };
    }

    function fitView() {
        if (!svg || !box) return;
        const width = canvas.clientWidth || 800;
        const height = canvas.clientHeight || 500;
        const pad = 30;
        const contentW = (expanded.size ? COLUMN_X.concept + SIZES.concept.w : COLUMN_X.section + SIZES.section.w + 30) + pad * 2;
        const contentH = box.total + pad * 2;
        const scale = Math.max(contentW / width, contentH / height);
        box.vb = { w: width * scale, h: height * scale, x: -pad - (width * scale - contentW) / 2, y: -pad - (height * scale - contentH) / 2 };
        applyView();
    }

    function applyView() {
        const { x, y, w, h } = box.vb;
        svg.setAttribute('viewBox', [x, y, w, h].join(' '));
    }

    function zoomAt(factor, clientX, clientY) {
        const rect = svg.getBoundingClientRect();
        const vb = box.vb;
        const px = vb.x + ((clientX - rect.left) / rect.width) * vb.w;
        const py = vb.y + ((clientY - rect.top) / rect.height) * vb.h;
        const nextW = Math.min(Math.max(vb.w * factor, 200), 6000);
        const ratio = nextW / vb.w;
        vb.x = px - (px - vb.x) * ratio;
        vb.y = py - (py - vb.y) * ratio;
        vb.w = nextW;
        vb.h *= ratio;
        applyView();
    }

    function findNode(id, node) {
        node = node || data.root;
        if (node.id === id) return node;
        for (const child of node.children || []) {
            const found = findNode(id, child);
            if (found) return found;
        }
        return null;
    }

    function renderDetail(panel) {
        const node = findNode(selectedId) || data.root;
        panel.replaceChildren(el('h3', '', node.title));
        if (node.references && node.references[0]) panel.appendChild(refButton(node.references[0], 'Jump to ' + node.references[0].timestamp));
        if (node.kind === 'root') {
            panel.appendChild(el('p', '', 'Click a topic to see its explanation, and use the + / \u2212 handle to show or hide its concepts. Drag to move, scroll to zoom.'));
            return;
        }
        if (node.explanation) panel.appendChild(el('p', 'mindmap-explanation', node.explanation));
        [
            listSection('Key points', node.key_points, textWithRef),
            listSection('Examples', node.examples, textWithRef),
            listSection('Formulas', node.formulas, (f) => { const li = textWithRef(f); li.firstChild.textContent = f.text + ' '; li.classList.add('mindmap-formula'); return li; }),
        ].forEach((block) => block && panel.appendChild(block));
        const titles = Object.fromEntries((data.root.children || []).map((n) => [n.id, n.title]));
        const related = (data.relationships || [])
            .filter((r) => r.type === 'related' && (r.source === node.id || r.target === node.id))
            .map((r) => titles[r.source === node.id ? r.target : r.source] + ' (shared: ' + r.label + ')');
        if (related.length) panel.appendChild(listSection('Related topics', related, (text) => el('li', '', text)));
        if (node.method) panel.appendChild(el('small', 'mindmap-method', node.method === 'extractive' ? 'Extracted from transcript' : 'AI explained (' + node.method + ')'));
    }

    function renderDiagram() {
        const wrap = el('div', 'mindmap-diagram');
        canvas = el('div', 'mindmap-canvas');
        const panel = el('aside', 'mindmap-detail');
        wrap.append(canvas, panel);
        body.replaceChildren(wrap);

        const previous = box && box.vb;
        box = layoutDiagram();
        svg = svgEl('svg', { role: 'img', 'aria-label': 'Lecture mind map diagram' });
        const edgeLayer = svgEl('g');
        const nodeLayer = svgEl('g');
        svg.append(edgeLayer, nodeLayer);

        box.edges.forEach(([from, to]) => {
            const x1 = from.x + SIZES[from.kind].w;
            const y1 = from.y + SIZES[from.kind].h / 2;
            const x2 = to.x;
            const y2 = to.y + SIZES[to.kind].h / 2;
            const mid = (x1 + x2) / 2;
            edgeLayer.appendChild(svgEl('path', { class: 'mm-edge', d: 'M' + x1 + ' ' + y1 + ' C' + mid + ' ' + y1 + ' ' + mid + ' ' + y2 + ' ' + x2 + ' ' + y2 }));
        });

        box.nodes.forEach((item) => {
            const size = SIZES[item.kind];
            const group = svgEl('g', { class: 'mm-node mm-' + item.kind + (item.node.id === selectedId ? ' selected' : ''), transform: 'translate(' + item.x + ' ' + item.y + ')', tabindex: '0', role: 'button' });
            group.appendChild(svgEl('title', {}, item.node.title));
            group.appendChild(svgEl('rect', { width: size.w, height: size.h, rx: 12 }));
            const lines = wrapLabel(item.node.title, item.kind === 'root' ? 22 : 26);
            lines.forEach((line, index) => {
                group.appendChild(svgEl('text', { x: size.w / 2, y: size.h / 2 + (index - (lines.length - 1) / 2) * 15 + 4, 'text-anchor': 'middle' }, line));
            });
            const select = () => { selectedId = item.node.id; renderDiagram(); };
            group.addEventListener('click', select);
            group.addEventListener('keydown', (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); select(); } });
            nodeLayer.appendChild(group);
            if (item.kind === 'section' && item.hasKids) {
                const open = expanded.has(item.node.id);
                const toggle = svgEl('g', { class: 'mm-toggle mm-node', transform: 'translate(' + (item.x + size.w) + ' ' + (item.y + size.h / 2) + ')', role: 'button', 'aria-label': (open ? 'Collapse ' : 'Expand ') + item.node.title });
                toggle.append(svgEl('circle', { r: 10 }), svgEl('text', { y: 4.5 }, open ? '\u2212' : '+'));
                toggle.addEventListener('click', (event) => {
                    event.stopPropagation();
                    open ? expanded.delete(item.node.id) : expanded.add(item.node.id);
                    renderDiagram();
                });
                nodeLayer.appendChild(toggle);
            }
        });

        const zoom = el('div', 'mindmap-zoom');
        [['+', 'Zoom in', () => { const r = svg.getBoundingClientRect(); zoomAt(0.8, r.left + r.width / 2, r.top + r.height / 2); }],
            ['\u2212', 'Zoom out', () => { const r = svg.getBoundingClientRect(); zoomAt(1.25, r.left + r.width / 2, r.top + r.height / 2); }],
            ['\u25a3', 'Fit to view', fitView]].forEach(([label, title, handler]) => {
            const button = el('button', 'btn btn-secondary', label);
            button.type = 'button';
            button.title = title;
            button.setAttribute('aria-label', title);
            button.addEventListener('click', handler);
            zoom.appendChild(button);
        });
        canvas.append(svg, zoom);

        svg.addEventListener('wheel', (event) => { event.preventDefault(); zoomAt(event.deltaY > 0 ? 1.12 : 0.89, event.clientX, event.clientY); }, { passive: false });
        svg.addEventListener('pointerdown', (event) => {
            if (event.target.closest('.mm-node')) return;
            const start = { x: event.clientX, y: event.clientY, vx: box.vb.x, vy: box.vb.y };
            const scale = box.vb.w / svg.getBoundingClientRect().width;
            svg.classList.add('panning');
            const move = (e) => {
                box.vb.x = start.vx - (e.clientX - start.x) * scale;
                box.vb.y = start.vy - (e.clientY - start.y) * scale;
                applyView();
            };
            const up = () => {
                svg.classList.remove('panning');
                window.removeEventListener('pointermove', move);
                window.removeEventListener('pointerup', up);
            };
            window.addEventListener('pointermove', move);
            window.addEventListener('pointerup', up);
        });

        renderDetail(panel);
        if (previous && previous.w) {
            box.vb = previous;
            applyView();
        } else {
            fitView();
        }
    }

    function render() {
        if (!data) return;
        $('mindmapDiagramBtn').setAttribute('aria-pressed', String(view === 'diagram'));
        $('mindmapFullBtn').setAttribute('aria-pressed', String(view === 'full'));
        $('mindmapRevisionBtn').setAttribute('aria-pressed', String(view === 'revision'));
        if (view === 'diagram') {
            selectedId = selectedId && findNode(selectedId) ? selectedId : data.root.id;
            renderDiagram();
        } else {
            box = null;
            view === 'full' ? renderFull() : renderRevision();
        }
        const notes = (data.notices || []).slice();
        notes.push('Generated ' + data.generated_at + ' from ' + data.segments_used + ' transcript segments' + (data.slides_available ? ' and ' + data.slides_available + ' slides.' : '.'));
        status.textContent = notes.join(' ');
    }

    async function request(path, options) {
        const response = await fetch(path, options);
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(payload.detail || 'Request failed (' + response.status + ').');
        return payload;
    }

    async function load() {
        const id = videoId();
        if (!id) { status.textContent = 'Load a lecture video first.'; return; }
        try {
            data = await request('/mindmap/' + encodeURIComponent(id));
            box = null;
            expanded.clear();
            selectedId = null;
            render();
        } catch (error) {
            data = null;
            body.replaceChildren();
            status.textContent = 'No map yet. Press "Generate map".';
        }
    }

    async function generate() {
        const id = videoId();
        if (!id) { status.textContent = 'Load a lecture video first.'; return; }
        const cloud = $('mindmapCloud').checked;
        status.textContent = 'Generating map from the transcript...';
        $('mindmapGenerateBtn').disabled = true;
        try {
            data = await request('/mindmap/' + encodeURIComponent(id) + '/generate', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ use_llm: $('mindmapUseLlm').checked, allow_cloud_fallback: cloud }),
            });
            box = null;
            expanded.clear();
            render();
        } catch (error) {
            status.textContent = error.message;
        } finally {
            $('mindmapGenerateBtn').disabled = false;
        }
    }

    $('openMindmapBtn').addEventListener('click', () => { dialog.showModal(); load(); });
    $('mindmapClose').addEventListener('click', () => dialog.close());
    $('mindmapGenerateBtn').addEventListener('click', generate);
    $('mindmapDiagramBtn').addEventListener('click', () => { view = 'diagram'; render(); });
    $('mindmapFullBtn').addEventListener('click', () => { view = 'full'; render(); });
    $('mindmapRevisionBtn').addEventListener('click', () => { view = 'revision'; render(); });
})();
