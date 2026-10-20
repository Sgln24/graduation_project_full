import { state } from './main.js';
import { elements } from './elements.js';
import { formatTime } from './utils.js';

export function speakerLanes(transcript, videoDuration) {
    const lanes = new Map();
    const intervals = [];
    for (let index = 0; index < transcript.length; index++) {
        const item = transcript[index];
        const id = item.speaker || item.speaker_name;
        const start = Number(item.start);
        if (!id || !Number.isFinite(start) || start < 0) continue;
        let end = Number(item.end);
        if (!(end > start)) end = start + Number(item.duration);
        if (!(end > start)) end = Number(transcript[index + 1]?.start);
        if (!Number.isFinite(end) || !(end > start)) continue;
        intervals.push({id, name: item.speaker_name || String(id).replace(/_/g, ' '), start, end});
    }
    const duration = Number.isFinite(videoDuration) && videoDuration > 0
        ? videoDuration : intervals.reduce((end, item) => Math.max(end, item.end), 0);
    for (const item of intervals.sort((a, b) => a.start - b.start)) {
        const end = Math.min(item.end, duration);
        if (item.start >= end) continue;
        if (!lanes.has(item.id)) lanes.set(item.id, {id: item.id, name: item.name, intervals: []});
        const lane = lanes.get(item.id);
        if (item.name !== String(item.id).replace(/_/g, ' ') && item.name !== item.id) lane.name = item.name;
        const previous = lane.intervals.at(-1);
        if (previous && item.start <= previous.end) previous.end = Math.max(previous.end, end);
        else lane.intervals.push({start: item.start, end});
    }
    return {duration, lanes: [...lanes.values()]};
}

export function initSpeakerTimeline() {
    const panel = document.getElementById('speakerTimeline');
    const rows = document.getElementById('speakerTimelineRows');
    const player = elements.videoPlayer;
    let tracks = [];
    let duration = 0;
    const update = () => {
        const time = Math.min(duration, Math.max(0, Number(player.currentTime) || 0));
        for (const {track, cursor, intervals} of tracks) {
            cursor.style.left = `${duration ? time / duration * 100 : 0}%`;
            track.setAttribute('aria-valuenow', String(Math.round(time)));
            track.setAttribute('aria-valuetext', formatTime(time));
            track.classList.toggle('is-speaking', intervals.some(item => item.start <= time && time < item.end));
        }
    };
    const render = () => {
        const data = state.currentVideoId
            ? speakerLanes(state.currentTranscript || [], player.duration) : {duration: 0, lanes: []};
        duration = data.duration;
        tracks = [];
        rows.replaceChildren();
        panel.hidden = !data.lanes.length;
        document.getElementById('speakerTimelineDuration').textContent = duration ? formatTime(duration) : '';
        for (const [index, lane] of data.lanes.entries()) {
            const row = document.createElement('div');
            row.className = 'speaker-timeline-row';
            row.style.setProperty('--speaker-color', `hsl(${[205, 350, 95, 275, 30, 165][index % 6]} 68% 43%)`);
            const label = document.createElement('div');
            label.className = 'speaker-timeline-label';
            const badge = document.createElement('span');
            badge.className = 'speaker-timeline-badge';
            badge.textContent = `S${index + 1}`;
            const name = document.createElement('span');
            name.textContent = lane.name;
            label.append(badge, name);
            const track = document.createElement('div');
            track.className = 'speaker-timeline-track';
            track.tabIndex = 0;
            track.setAttribute('role', 'slider');
            track.setAttribute('aria-label', `Seek video on ${lane.name}'s speaking timeline`);
            track.setAttribute('aria-valuemin', '0');
            track.setAttribute('aria-valuemax', String(Math.ceil(duration)));
            track.setAttribute('aria-orientation', 'horizontal');
            track.title = `${lane.name}: click to seek; use arrow keys to move 5 seconds`;
            const seek = time => {
                player.currentTime = Math.min(duration, Math.max(0, time));
                update();
            };
            track.addEventListener('click', event => {
                const rect = track.getBoundingClientRect();
                if (rect.width > 0) seek((event.clientX - rect.left) / rect.width * duration);
            });
            track.addEventListener('keydown', event => {
                const targets = {ArrowLeft: player.currentTime - 5, ArrowRight: player.currentTime + 5,
                    Home: 0, End: duration};
                if (!Object.hasOwn(targets, event.key)) return;
                event.preventDefault();
                event.stopPropagation();
                seek(targets[event.key]);
            });
            for (const interval of lane.intervals) {
                const segment = document.createElement('span');
                segment.className = 'speaker-timeline-segment';
                segment.style.left = `${interval.start / duration * 100}%`;
                segment.style.width = `${(interval.end - interval.start) / duration * 100}%`;
                segment.title = `${lane.name}: ${formatTime(interval.start)} - ${formatTime(interval.end)}`;
                track.appendChild(segment);
            }
            const cursor = document.createElement('span');
            cursor.className = 'speaker-timeline-cursor';
            cursor.setAttribute('aria-hidden', 'true');
            track.appendChild(cursor);
            tracks.push({track, cursor, intervals: lane.intervals});
            row.append(label, track);
            rows.appendChild(row);
        }
        update();
    };
    document.addEventListener('transcriptUpdated', render);
    document.addEventListener('videoSelectionChanged', render);
    for (const event of ['loadedmetadata', 'durationchange', 'emptied']) player.addEventListener(event, render);
    player.addEventListener('timeupdate', update);
    render();
}
