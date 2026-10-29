/* Small DOM helpers shared by the standalone pages (Fusion, Resource Finder). */
(() => {
    const el = (tag, className, text) => {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    };

    // Status line with optional spinner (kind: 'busy' | undefined).
    const setStatus = (node, text, kind) => {
        node.textContent = text || '';
        node.classList.toggle('is-busy', kind === 'busy' && Boolean(text));
    };

    // Alert box (kind: info | ok | warn | error); empty text hides it.
    const setAlert = (node, text, kind) => {
        node.textContent = text || '';
        node.className = 'alert alert-' + (kind || 'info');
    };

    const readJson = async (response, fallback) => {
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(payload.detail || fallback || 'Request failed (' + response.status + ').');
        return payload;
    };

    window.AppUI = { el, setStatus, setAlert, readJson };
})();
