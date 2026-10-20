export function contentLanguage(videoId, kind, transcriptLanguage = '') {
    return contentPreference(videoId, kind) || transcriptLanguage || '';
}

export function contentPreference(videoId, kind) {
    return localStorage.getItem(`lectureseek-${kind}-language-${videoId}`) || '';
}

export function setContentLanguage(videoId, kind, language) {
    const key = `lectureseek-${kind}-language-${videoId}`;
    if (language === null) localStorage.removeItem(key);
    else localStorage.setItem(key, language);
}

export function savedSummaryUrl(videoId, transcriptLanguage = '') {
    const language = contentLanguage(videoId, 'chapters', transcriptLanguage);
    return `/summary/${encodeURIComponent(videoId)}` + (language ? `?language=${encodeURIComponent(language)}` : '');
}
