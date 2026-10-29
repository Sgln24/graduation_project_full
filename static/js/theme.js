(function () {
    const allowed = ['light', 'dim', 'night'];
    let theme = 'light';
    try {
        const stored = localStorage.getItem('lectureSeekTheme');
        if (allowed.includes(stored)) theme = stored;
    } catch (error) {
        console.warn('Could not read saved theme preference:', error);
    }
    document.documentElement.dataset.theme = theme;

    function apply(value) {
        if (!allowed.includes(value)) return;
        document.documentElement.dataset.theme = value;
        try {
            localStorage.setItem('lectureSeekTheme', value);
        } catch (error) {
            console.warn('Could not save theme preference:', error);
        }
        document.querySelectorAll('[data-theme-picker]').forEach((select) => {
            select.value = value;
        });
    }

    document.addEventListener('change', (event) => {
        if (event.target.matches('[data-theme-picker]')) apply(event.target.value);
    });
    document.addEventListener('DOMContentLoaded', () => {
        document.querySelectorAll('[data-theme-picker]').forEach((select) => {
            select.value = document.documentElement.dataset.theme;
        });
    });
})();
