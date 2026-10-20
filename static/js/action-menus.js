export function rowActions(row, label = 'Actions') {
    let menu = row.querySelector(':scope > .action-menu');
    if (!menu) {
        menu = document.createElement('details');
        menu.className = 'action-menu';
        const summary = document.createElement('summary');
        summary.textContent = /^(Chapter \d+|Part) actions$/.test(label) ? 'Actions' : label;
        summary.setAttribute('aria-label', label);
        const content = document.createElement('div');
        content.className = 'row-actions-content';
        menu.append(summary, content);
        menu.addEventListener('click', event => event.stopPropagation());
        menu.addEventListener('keydown', event => {
            event.stopPropagation();
            if (event.key === 'Escape') { menu.open = false; summary.focus(); }
        });
        row.appendChild(menu);
    }
    return menu.querySelector('.row-actions-content');
}

export function initActionMenus() {
    const groups = [
        ['transcript-controls', 'Transcript actions', [
            'edit-transcript-btn', 'transcriptOptionsBtn', 'openCaptionStyleBtn',
            'downloadTranscriptBtn', 'timestampToggle', 'fuzzySearchToggle',
        ]],
        ['chapter-controls', 'Chapter actions', [
            'edit-chapters-btn', 'enhanceChaptersBtn', 'summaryOptionsBtn',
            'contentTranslationTarget', 'translateChaptersBtn', 'chapterSlidesToggle',
        ]],
        ['slide-controls', 'Slide actions', [
            'edit-slides-btn', 'refreshSlideTitlesBtn', 'slideTranslationTarget', 'translateSlideTitlesBtn',
            'downloadScreenshotsBtn', 'downloadSlidesPdfBtn', 'downloadSlideTextWordBtn',
        ]],
    ];
    for (const [className, label, ids] of groups) {
        const bar = document.querySelector(`.${className}`);
        if (!bar) continue;
        const content = rowActions(bar, label);
        for (const id of ids) {
            const element = document.getElementById(id);
            if (!element) continue;
            const outerLabel = element.closest('.toggle-container') || element.closest('label');
            const labelElement = bar.querySelector(`label[for="${id}"]`);
            if (labelElement) content.appendChild(labelElement);
            content.appendChild(outerLabel || element);
        }
    }
    document.addEventListener('click', event => {
        document.querySelectorAll('.action-menu[open]').forEach(menu => {
            if (!menu.contains(event.target)) menu.open = false;
        });
    });
}
