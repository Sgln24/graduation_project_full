export function chapterStart(chapter) {
    const parts = String(chapter.timestamp).split(':').map(Number);
    if (parts.length < 2 || parts.length > 3 || parts.some((part) => !Number.isFinite(part) || part < 0)) {
        return NaN;
    }

    return parts.reduce((seconds, part) => seconds * 60 + part, 0);
}

export function conciseTitle(title, limit = 36) {
    const text = String(title || '').replace(/\s+/g, ' ').trim();
    if (text.length <= limit) return text;
    const prefix = text.slice(0, limit - 3);
    const split = prefix.lastIndexOf(' ');
    return `${split > 0 ? prefix.slice(0, split) : prefix}...`;
}

export function slideTitle(scene, language = '') {
    const translated = scene.title_translations || {};
    if (language && Object.hasOwn(translated, language)) return translated[language];
    return !language || !scene.title_language || scene.title_language === language ? scene.title || '' : '';
}

export function navigationParts(chapters = [], scenes = [], mode = 'topic', language = '') {
    const topics = chapters.map((chapter) => ({start: chapterStart(chapter), title: chapter.title}))
        .filter((part) => Number.isFinite(part.start)).sort((a, b) => a.start - b.start);
    const slides = scenes.map((scene, index) => {
        const topic = topics.filter((part) => part.start <= scene.time_seconds).at(-1);
        return {
            start: Number(scene.time_seconds),
            title: slideTitle(scene, language) || `${index + 1}${topic?.title ? `: ${topic.title}` : ''}`,
        };
    }).filter((part) => Number.isFinite(part.start));
    // Before chapters are generated, content mode falls back to slide changes so navigation still works.
    const selected = mode === 'topic' ? (topics.length ? topics : slides)
        : mode === 'slides' ? slides : [...slides, ...topics];
    return [...new Map(selected.map((part) => [part.start, part])).values()].sort((a, b) => a.start - b.start);
}

export function activePart(parts, time) {
    return parts.filter((part) => part.start <= time + 0.01).at(-1);
}

export function sceneTopicTitle(chapters, time) {
    const ordered = chapters.map((chapter) => ({...chapter, start: chapterStart(chapter)}))
        .filter((chapter) => Number.isFinite(chapter.start))
        .sort((a, b) => a.start - b.start);
    const chapter = activePart(ordered, time);
    if (!chapter) return '';
    const sections = (Array.isArray(chapter.sections) ? chapter.sections : [])
        .map((section) => ({start: chapterStart(section), title: section.title}))
        .filter((section) => Number.isFinite(section.start))
        .sort((a, b) => a.start - b.start);
    return activePart(sections, time)?.title || chapter.title || '';
}
