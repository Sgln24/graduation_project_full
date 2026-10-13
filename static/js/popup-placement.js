// Keeps --panel-* CSS variables in sync with the right panel so popups open over it.
(function () {
    const GAP = 6;
    const root = document.documentElement;

    function update() {
        const panel = document.querySelector('.content-tabs');
        if (!panel) return;
        const rect = panel.getBoundingClientRect();
        const top = Math.max(rect.top, GAP);
        const bottom = Math.min(rect.bottom, window.innerHeight - GAP);
        root.style.setProperty('--panel-left', `${Math.round(rect.left)}px`);
        root.style.setProperty('--panel-top', `${Math.round(top)}px`);
        root.style.setProperty('--panel-width', `${Math.round(rect.width)}px`);
        root.style.setProperty('--panel-right', `${Math.round(rect.right)}px`);
        root.style.setProperty('--panel-height', `${Math.max(240, Math.round(bottom - top))}px`);
    }

    function start() {
        update();
        window.addEventListener('resize', update);
        window.addEventListener('scroll', update, {passive: true, capture: true});
        document.addEventListener('keydown', event => {
            if (event.key !== 'Escape' || document.querySelector('dialog:modal')) return;
            const dialog = document.querySelector('dialog.options-dialog[open]');
            if (dialog) {
                dialog.close();
                event.preventDefault();
            }
        });
        const panel = document.querySelector('.content-tabs');
        if (panel && 'ResizeObserver' in window) new ResizeObserver(update).observe(panel);
        // Refresh just before a dialog or the settings modal becomes visible.
        const popups = document.querySelectorAll('dialog, .modal, .debug-overlay, .config-dialog');
        if (popups.length) {
            const observer = new MutationObserver(update);
            popups.forEach((popup) => observer.observe(popup, {attributes: true, attributeFilter: ['open', 'style', 'class']}));
        }
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
    else start();
})();
