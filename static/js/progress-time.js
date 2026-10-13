const timers = new WeakMap();

export function formatElapsedTime(seconds) {
    const total = Number.isFinite(Number(seconds)) ? Math.max(0, Math.floor(Number(seconds))) : 0;
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const remainder = String(total % 60).padStart(2, '0');
    return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${remainder}` : `${minutes}:${remainder}`;
}

export function startElapsedTimer(container, elapsedSeconds = 0, running = true) {
    if (!container) return () => {};
    timers.get(container)?.();
    const label = document.createElement('small');
    label.className = 'progress-elapsed';
    container.appendChild(label);
    const elapsed = Number.isFinite(Number(elapsedSeconds)) ? Math.max(0, Number(elapsedSeconds)) : 0;
    const startedAt = Date.now() - elapsed * 1000;
    const update = () => {
        label.textContent = `${formatElapsedTime((Date.now() - startedAt) / 1000)} elapsed`;
    };
    update();
    let interval;
    const stop = () => {
        clearInterval(interval);
        label.remove();
        if (timers.get(container) === stop) timers.delete(container);
    };
    timers.set(container, stop);
    if (running) {
        interval = setInterval(() => {
            if (!container.isConnected || !container.contains(label)) {
                stop();
                return;
            }
            update();
        }, 1000);
    }
    return stop;
}
