const $ = (selector, root = document) => root.querySelector(selector);
const content = $('#content');
const notice = $('#notice');
let user = null;
let activeCourse = null;
let activeLecture = null;
let quizQuestions = [];
let authSetup = false;
let editingQuizId = null;
let suggestionsNeedReview = false;
let suggestionsReviewed = false;
let suggestionStatus = '';
let activeVideoCleanup = null;

function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, (character) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    })[character]);
}

async function api(path, options = {}) {
    const response = await fetch(`/api/lms${path}`, {
        credentials: 'same-origin',
        ...options,
        headers: { ...(options.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }), ...options.headers }
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || data.error || `Request failed (${response.status})`);
    return data;
}

function message(text, type = 'success') {
    notice.textContent = text;
    notice.className = `notice show ${type}`;
    window.clearTimeout(message.timer);
    message.timer = window.setTimeout(() => notice.classList.remove('show'), 4500);
}

function setHeading(title, kicker = 'LECTURESEEK LMS') {
    $('#pageTitle').textContent = title;
    $('#pageKicker').textContent = kicker;
}

function navItems() {
    return user.role === 'instructor'
        ? [['dashboard', '⌂', 'Overview'], ['courses', '▤', 'Courses'], ['students', '♙', 'Students'], ['gradebook', '▦', 'Gradebook'], ['analytics', '◔', 'Analytics']]
        : [['dashboard', '⌂', 'My learning'], ['courses', '▤', 'My courses'], ['gradebook', '▦', 'My grades']];
}

function renderNav() {
    $('#navigation').innerHTML = navItems().map(([id, icon, label]) =>
        `<button data-page="${id}"><span>${icon}</span>${label}</button>`).join('');
    $('#navigation').addEventListener('click', (event) => {
        const button = event.target.closest('[data-page]');
        if (button) navigate(button.dataset.page);
    });
    $('#profileName').textContent = user.name;
    $('#profileRole').textContent = user.role;
    $('#avatar').textContent = user.name.slice(0, 1).toUpperCase();
}

async function boot() {
    try {
        const result = await api('/session');
        user = result.user;
        if (!user) {
            window.location.replace('/');
            return;
        }
        $('#authView').hidden = true;
        $('#appView').hidden = false;
        $('#workspaceLink').href = '/workspace';
        renderNav();
        await navigate(user.role === 'student' ? 'dashboard' : 'dashboard');
    } catch (error) {
        showAuth(false);
        $('#authError').textContent = error.message;
    }
}

function showAuth(setup) {
    authSetup = setup;
    $('#appView').hidden = true;
    $('#authView').hidden = false;
    $('#authTitle').textContent = setup ? 'Create your instructor account' : 'Welcome back';
    $('#authDescription').textContent = setup
        ? 'Set up the first instructor account to open your learning space.'
        : 'Sign in to manage your courses and lectures.';
    $('#nameField').hidden = !setup;
    $('#authHint').textContent = setup ? 'Use a strong password with at least 12 characters.' : '';
    $('#authSubmit').textContent = setup ? 'Create instructor account' : 'Sign in';
    $('#authForm').elements.password.autocomplete = setup ? 'new-password' : 'current-password';
}

$('#authForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const values = Object.fromEntries(form.entries());
    $('#authError').textContent = '';
    try {
        const result = await api(authSetup ? '/auth/setup' : '/auth/login', {
            method: 'POST', body: JSON.stringify(values)
        });
        user = result.user;
        $('#authView').hidden = true;
        $('#appView').hidden = false;
        renderNav();
        await navigate('dashboard');
    } catch (error) { $('#authError').textContent = error.message; }
});

$('#signOut').addEventListener('click', async () => {
    try { await api('/auth/logout', { method: 'POST' }); }
    finally { user = null; activeCourse = null; window.location.assign('/'); }
});

async function navigate(page) {
    stopVideoTracking();
    window.clearInterval(window.quizTimer);
    $('#navigation button.active')?.classList.remove('active');
    $(`#navigation [data-page="${page}"]`)?.classList.add('active');
    try {
        if (page === 'dashboard') return await renderDashboard();
        if (page === 'courses') return await renderCourses();
        if (page === 'students') return await renderStudents();
        if (page === 'gradebook') return await renderGradebook();
        if (page === 'analytics') return await renderAnalytics();
    } catch (error) { message(error.message, 'error'); }
}

const pageHead = (title, description, action = '') =>
    `<div class="page-heading"><div><h2>${title}</h2><p>${description}</p></div>${action}</div>`;
const empty = (title, description) =>
    `<div class="empty-state"><b>${title}</b>${description}</div>`;

async function renderDashboard() {
    setHeading(user.role === 'instructor' ? 'Overview' : 'My learning');
    const data = await api('/dashboard');
    if (user.role === 'student') return renderStudentDashboard(data);
    const cards = [
        ['Total courses', data.counts.courses, '▤', 'Your active learning spaces'],
        ['Enrolled students', data.counts.students, '♙', 'Across all your courses'],
        ['Lectures', data.counts.lectures, '▷', 'Video lessons and resources'],
        ['Pending submissions', data.counts.pending_submissions, '▱', 'Ready for your review']
    ];
    content.innerHTML = `
        ${pageHead('Good to see you, ' + escapeHtml(user.name.split(' ')[0]), 'Your teaching workspace at a glance.',
            '<button class="button primary" data-action="create-course">＋ Create course</button>')}
        <div class="stats-grid">${cards.map(([label, value, icon, note]) => `
            <article class="stat-card"><div class="stat-top">${label}<span class="stat-icon">${icon}</span></div>
            <div class="stat-value">${value}</div><div class="stat-note">${note}</div></article>`).join('')}</div>
        <div class="quick-actions">
            <button class="quick-action" data-action="create-course"><span class="quick-icon">＋</span><b>Create a course</b><small>Start a new learning space</small></button>
            <button class="quick-action" data-action="add-lecture"><span class="quick-icon">▷</span><b>Add a lecture</b><small>Upload video or paste YouTube link</small></button>
            <button class="quick-action" data-action="create-quiz"><span class="quick-icon">▤</span><b>Create a quiz</b><small>Build a knowledge check</small></button>
        </div>
        <div class="dashboard-grid">
            <section class="panel"><div class="panel-header"><h3>Recent activity</h3><button class="text-link" data-page="courses">View courses</button></div>
                <div class="activity-list">${data.activity.length ? data.activity.map(item =>
                    `<div class="activity-item"><span class="activity-marker">•</span><p>${escapeHtml(item.message)}<small>${dateText(item.created_at)}</small></p></div>`
                ).join('') : empty('No activity yet', 'Create a course or invite a student to get started.')}</div>
            </section>
            <div>
                <section class="panel"><div class="panel-header"><h3>Upcoming deadlines</h3></div>
                    <div class="deadline-list">${data.deadlines.length ? data.deadlines.map(item =>
                        `<div class="deadline-item"><span class="deadline-date">${dateText(item.due_at)}</span><p>${escapeHtml(item.title)}<small>${escapeHtml(item.course_title)}</small></p></div>`
                    ).join('') : empty('Nothing due soon', 'Published quiz deadlines will appear here.')}</div>
                </section>
                <section class="panel"><div class="panel-header"><h3>Pending tasks</h3></div>
                    <div class="task-list">${data.tasks.length ? data.tasks.map(item =>
                        `<div class="task-item"><span class="activity-marker">◷</span><p>${escapeHtml(item.title)}<small>${escapeHtml(item.kind)} · ${escapeHtml(item.course_title)}</small></p></div>`
                    ).join('') : empty('You’re all caught up', 'Unpublished lecture drafts will appear here.')}</div>
                </section>
            </div>
        </div>
        <h3 class="section-title">Your courses</h3>${courseCards(data.courses)}`;
    content.onclick = handleDashboardAction;
}

function renderStudentDashboard(data) {
    content.innerHTML = `${pageHead('Welcome, ' + escapeHtml(user.name.split(' ')[0]), 'Pick up where you left off.')}
        <div class="stats-grid">${[
            ['Enrolled courses', data.counts.courses], ['Quiz submissions', data.counts.submissions],
            ['Graded quizzes', data.counts.graded], ['Learning activity', '—']
        ].map(([label, value]) => `<article class="stat-card"><div class="stat-top">${label}<span class="stat-icon">◈</span></div><div class="stat-value">${value}</div><div class="stat-note">${label === 'Learning activity' ? 'Not tracked yet' : 'From your enrolled courses'}</div></article>`).join('')}</div>
        <div class="row"><h3 class="section-title">My courses</h3><button class="button" data-browse-public>Browse public courses</button></div>
        ${courseCards(data.courses)}`;
    content.onclick = handleDashboardAction;
}

function courseCards(courses) {
    if (!courses.length) return empty('No courses yet', user.role === 'instructor'
        ? 'Create a course to start organizing your teaching materials.'
        : 'Your instructor will share your enrolled courses here.');
    return `<div class="course-grid">${courses.map(course => `
        <article class="course-card" data-course="${escapeHtml(course.id)}"><div class="course-accent"></div>
            <div class="row"><h3>${escapeHtml(course.title)}</h3><span class="badge ${course.status}">${course.status}</span><span class="badge">${escapeHtml(course.visibility || 'private')}</span></div>
            <p>${escapeHtml(course.description || 'No course description yet.')}</p>
            <div class="course-meta"><span>♙ ${course.student_count} students</span><span>▷ ${course.lecture_count} lectures</span></div>
        </article>`).join('')}</div>`;
}

function dateText(value) {
    if (!value) return 'No deadline';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? escapeHtml(value) : date.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
}

async function handleDashboardAction(event) {
    const card = event.target.closest('[data-course]');
    if (card) return openCourse(card.dataset.course);
    const nav = event.target.closest('[data-page]');
    if (nav) return navigate(nav.dataset.page);
    if (event.target.closest('[data-browse-public]')) return renderPublicCourses();
    const action = event.target.closest('[data-action]')?.dataset.action;
    if (action === 'create-course') return showCreateCourse();
    if (action === 'add-lecture') return renderCourses('lecture');
    if (action === 'create-quiz') return renderCourses('quiz');
}

async function renderCourses(focus = '') {
    setHeading(user.role === 'instructor' ? 'Courses' : 'My courses');
    const { courses } = await api('/courses');
    content.innerHTML = `${pageHead(user.role === 'instructor' ? 'Course management' : 'Your courses',
        user.role === 'instructor' ? 'Build, organize, and publish your learning spaces.' : 'Open an enrolled course to continue learning.',
        user.role === 'instructor' ? '<button class="button primary" data-action="create-course">＋ Create course</button>' : '<button class="button" data-browse-public>Browse public courses</button>')}
        ${courseCards(courses)}`;
    content.onclick = async (event) => {
        const card = event.target.closest('[data-course]');
        if (card) await openCourse(card.dataset.course, focus);
        if (event.target.closest('[data-action="create-course"]')) showCreateCourse();
        if (event.target.closest('[data-browse-public]')) renderPublicCourses();
    };
}

async function renderPublicCourses() {
    setHeading('Public courses', 'Explore courses open to enrolled students');
    const { courses } = await api('/public-courses');
    content.innerHTML = `${pageHead('Browse public courses', 'Published public courses can be joined directly.')}
        ${courses.length ? `<div class="course-grid">${courses.map(course => `<article class="course-card">
            <div class="course-accent"></div><div class="row"><h3>${escapeHtml(course.title)}</h3>${course.enrolled ? '<span class="badge published">Enrolled</span>' : ''}</div>
            <p>${escapeHtml(course.description || 'No course description yet.')}</p>
            <div class="course-meta"><span>Instructor: ${escapeHtml(course.instructor_name)}</span></div>
            ${course.enrolled ? `<button class="button small" data-course="${escapeHtml(course.id)}">Open course</button>` : `<button class="button small primary" data-public-enroll="${escapeHtml(course.id)}">Join course</button>`}
            </article>`).join('')}</div>` : empty('No public courses available', 'Your instructor can share a private course invitation link.')}`;
    content.onclick = async event => {
        const courseCard = event.target.closest('[data-course]');
        if (courseCard) return openCourse(courseCard.dataset.course);
        const enrollButton = event.target.closest('[data-public-enroll]');
        if (!enrollButton) return;
        try {
            await api(`/public-courses/${enrollButton.dataset.publicEnroll}/enroll`, { method: 'POST' });
            message('You joined the course.');
            await renderPublicCourses();
        } catch (error) { message(error.message, 'error'); }
    };
}

async function showCreateCourse() {
    content.innerHTML = `${pageHead('Create a course', 'Add the essentials now; modules and lectures can come next.')}
        <section class="detail-card"><form id="courseForm" class="form-grid">
            <div class="field"><label>Course name</label><input name="title" maxlength="180" required placeholder="e.g. Introduction to Biology"></div>
            <div class="field"><label>Description</label><textarea name="description" maxlength="5000" placeholder="What will students learn?"></textarea></div>
            <div class="field"><label>Enrollment access</label><select name="visibility"><option value="private" selected>Private — invitation link or instructor enrollment</option><option value="public">Public — students can find and join it</option></select></div>
            <div class="row"><button class="button primary">Create course</button><button class="button" type="button" data-page="courses">Cancel</button></div>
        </form></section>`;
    $('#courseForm').onsubmit = async (event) => {
        event.preventDefault();
        const form = new FormData(event.currentTarget);
        try {
            const course = await api('/courses', { method: 'POST', body: JSON.stringify(Object.fromEntries(form.entries())) });
            message('Course created as a draft.');
            await openCourse(course.id);
        } catch (error) { message(error.message, 'error'); }
    };
}

async function openCourse(courseId, focus = '') {
    stopVideoTracking();
    window.clearInterval(window.quizTimer);
    activeCourse = courseId;
    const data = await api(`/courses/${courseId}`);
    const course = data.course;
    const instructor = user.role === 'instructor';
    const lectureRow = (lecture) => {
        const module = data.modules.find(item => item.id === lecture.module_id);
        return `<div class="lecture-row"><div><strong>${escapeHtml(lecture.title)}</strong><small>${escapeHtml(module?.title || 'Unassigned')} · <span class="badge ${lecture.status}">${lecture.status}</span></small></div>
            <div class="row">${instructor
                ? `<button class="button small" data-review="${lecture.id}">Review content</button>${lecture.video_id ? `<button class="button small" data-checkpoints="${lecture.id}">In-video questions</button>` : ''}${lecture.status === 'published'
                    ? `<button class="button small" data-unpublish-lecture="${lecture.id}">Unpublish</button>`
                    : `<button class="button small primary" data-publish-lecture="${lecture.id}">Publish</button>`}`
                : `<button class="button small" data-student-lecture="${lecture.id}">Open lecture</button>`}</div></div>`;
    };
    const modules = data.modules.map((module, index) => `<section class="module-block"><div class="module-heading"><span>${escapeHtml(module.title)} <span class="badge ${module.status}">${module.status}</span></span>
        ${instructor ? `<div class="row"><button class="button small" data-edit-module="${module.id}">Rename</button><button class="button small" data-toggle-module="${module.id}">${module.status === 'published' ? 'Unpublish' : 'Publish'}</button><button class="button small" data-move-module="${module.id}" data-delta="-1" ${index === 0 ? 'disabled' : ''}>↑</button><button class="button small" data-move-module="${module.id}" data-delta="1" ${index === data.modules.length - 1 ? 'disabled' : ''}>↓</button><button class="button small danger" data-delete-module="${module.id}">Remove</button></div>` : ''}</div>
        ${data.lectures.filter(lecture => lecture.module_id === module.id).map(lectureRow).join('')}
        ${data.resources.filter(resource => resource.module_id === module.id).map(resource =>
            `<div class="resource-row"><div><strong>${escapeHtml(resource.title)}</strong><small>Learning resource · <span class="badge ${resource.status}">${resource.status}</span></small></div><div class="row"><a class="button small" href="${escapeHtml(resource.url)}" target="_blank" rel="noopener noreferrer">Open ↗</a>${instructor ? `<button class="button small" data-toggle-resource="${resource.id}">${resource.status === 'published' ? 'Unpublish' : 'Publish'}</button>` : ''}</div></div>`).join('')}
        </section>`).join('');
    const quizzes = data.quizzes.map(quiz => `<div class="lecture-row"><div><strong>${escapeHtml(quiz.title)}</strong><small>${quiz.due_at ? `Due ${dateText(quiz.due_at)}` : 'No deadline'} · ${quiz.time_limit_minutes ? `${quiz.time_limit_minutes} min · ` : ''}${quiz.attempt_limit} attempt${quiz.attempt_limit === 1 ? '' : 's'} · <span class="badge ${quiz.status}">${quiz.status}</span></small></div>
        <div class="row">${instructor ? `<button class="button small" data-edit-quiz="${quiz.id}">Questions</button><button class="button small" data-toggle-quiz="${quiz.id}" data-status="${quiz.status}">${quiz.status === 'published' ? 'Unpublish' : 'Publish'}</button><button class="button small danger" data-delete-quiz="${quiz.id}">Delete</button>` : `<button class="button small primary" data-student-quiz="${quiz.id}">Take quiz</button>`}</div></div>`).join('');
    const looseLectures = data.lectures.filter(lecture => !lecture.module_id).map(lectureRow).join('');
    const controls = instructor ? `<div class="row"><button class="button" data-edit-course>✎ Edit course</button>
        <button class="button" data-toggle-course>${course.status === 'published' ? 'Unpublish course' : 'Publish course'}</button>
        <button class="button primary" data-new-lecture>＋ Add lecture</button><button class="button" data-new-quiz>＋ Create quiz</button></div>` : '';
    content.innerHTML = `${pageHead(escapeHtml(course.title), escapeHtml(course.description || 'No course description.'), controls)}
        ${instructor ? `<div class="row"><span class="badge ${course.status}">${course.status}</span><span class="badge">${escapeHtml(course.visibility)}</span><button class="text-link" data-students-for-course>Manage enrolled students (${course.student_count})</button></div>
        ${course.status === 'published' && course.visibility === 'private' ? `<section class="detail-card"><h3>Private course invitation</h3><p class="muted small">Share this link with your students. They must sign in with a student account to enroll.</p><div class="inline-form"><input id="courseShareLink" readonly value="${escapeHtml(`${window.location.origin}/?join=${encodeURIComponent(course.share_token)}`)}"><button class="button" data-copy-share>Copy link</button></div></section>` : ''}` : ''}
        <section class="detail-card"><div class="panel-header"><h3 class="section-title" style="margin:0">Course content</h3>
        ${instructor ? '<button class="button small" data-new-module>＋ Add module</button>' : ''}</div>
        ${modules || empty('No modules yet', 'Add modules to group lectures and learning resources.')}${looseLectures ? `<div class="module-block">${looseLectures}</div>` : ''}
        ${instructor ? `<div class="inline-form"><div class="field"><label>Resource title</label><input id="resourceTitle" placeholder="Resource title"></div><div class="field"><label>URL</label><input id="resourceUrl" type="url" placeholder="https://"></div><div class="field"><label>Module</label><select id="resourceModule"><option value="">Unassigned</option>${data.modules.map(module => `<option value="${module.id}">${escapeHtml(module.title)}</option>`).join('')}</select></div><button class="button" data-add-resource>Add resource</button></div>` : ''}
        ${data.resources.filter(resource => !resource.module_id).map(resource => `<div class="resource-row"><div><strong>${escapeHtml(resource.title)}</strong><small>Learning resource · <span class="badge ${resource.status}">${resource.status}</span></small></div><div class="row"><a class="button small" href="${escapeHtml(resource.url)}" target="_blank" rel="noopener noreferrer">Open ↗</a>${instructor ? `<button class="button small" data-toggle-resource="${resource.id}">${resource.status === 'published' ? 'Unpublish' : 'Publish'}</button>` : ''}</div></div>`).join('')}
        </section>
        <section class="detail-card"><div class="panel-header"><h3>Quizzes</h3>${instructor ? '<button class="button small" data-new-quiz>＋ Create quiz</button>' : ''}</div>${quizzes || empty('No quizzes available', 'Published quizzes will appear here.')}</section>
        ${instructor ? `<section class="detail-card"><div class="panel-header"><h3>Enrolled students</h3><button class="text-link" data-students-for-course>Manage students</button></div><div id="courseStudents">${empty('Loading students', ' ')}</div></section>` : ''}`;
    content.onclick = (event) => handleCourseAction(event, data);
    if (instructor) loadCourseStudents(courseId);
    if (focus === 'lecture') showLectureForm();
    if (focus === 'quiz') showQuizForm();
}

async function handleCourseAction(event, data) {
    const button = event.target.closest('button');
    if (!button) return;
    try {
        if (button.hasAttribute('data-edit-course')) return showEditCourse(data.course);
        if (button.hasAttribute('data-copy-share')) {
            await navigator.clipboard.writeText($('#courseShareLink').value);
            message('Private enrollment link copied.');
            return;
        }
        if (button.hasAttribute('data-toggle-course')) {
            await api(`/courses/${activeCourse}`, { method: 'PUT', body: JSON.stringify({
                title: data.course.title, description: data.course.description,
                status: data.course.status === 'published' ? 'draft' : 'published'
            }) });
            message('Course status updated.'); return openCourse(activeCourse);
        }
        if (button.hasAttribute('data-new-module')) return addModule();
        if (button.hasAttribute('data-edit-module')) {
            const module = data.modules.find(item => item.id === button.dataset.editModule);
            const title = window.prompt('Module title', module.title);
            if (!title?.trim()) return;
            await api(`/modules/${module.id}`, { method: 'PUT', body: JSON.stringify({ title }) });
            message('Module renamed.'); return openCourse(activeCourse);
        }
        if (button.hasAttribute('data-toggle-module')) {
            const module = data.modules.find(item => item.id === button.dataset.toggleModule);
            await api(`/modules/${module.id}`, { method: 'PUT', body: JSON.stringify({
                title: module.title, status: module.status === 'published' ? 'draft' : 'published'
            }) });
            message('Module status updated.'); return openCourse(activeCourse);
        }
        if (button.hasAttribute('data-toggle-resource')) {
            const resource = data.resources.find(item => item.id === button.dataset.toggleResource);
            await api(`/resources/${resource.id}`, { method: 'PUT', body: JSON.stringify({
                title: resource.title, url: resource.url, module_id: resource.module_id,
                status: resource.status === 'published' ? 'draft' : 'published'
            }) });
            message('Resource status updated.'); return openCourse(activeCourse);
        }
        if (button.hasAttribute('data-move-module')) {
            const index = data.modules.findIndex(item => item.id === button.dataset.moveModule);
            const target = index + Number(button.dataset.delta);
            if (target < 0 || target >= data.modules.length) return;
            const currentModule = data.modules[index];
            const targetModule = data.modules[target];
            await api(`/modules/${currentModule.id}`, { method: 'PUT', body: JSON.stringify({
                title: currentModule.title, position: targetModule.position
            }) });
            await api(`/modules/${targetModule.id}`, { method: 'PUT', body: JSON.stringify({
                title: targetModule.title, position: currentModule.position
            }) });
            return openCourse(activeCourse);
        }
        if (button.hasAttribute('data-new-lecture')) return showLectureForm();
        if (button.hasAttribute('data-new-quiz')) return showQuizForm();
        if (button.hasAttribute('data-add-resource')) {
            await api(`/courses/${activeCourse}/resources`, { method: 'POST', body: JSON.stringify({
                title: $('#resourceTitle').value, url: $('#resourceUrl').value,
                module_id: $('#resourceModule').value
            }) });
            message('Resource added.'); return openCourse(activeCourse);
        }
        if (button.hasAttribute('data-review')) return reviewLecture(button.dataset.review);
        if (button.hasAttribute('data-checkpoints')) return manageLectureCheckpoints(button.dataset.checkpoints);
        if (button.hasAttribute('data-publish-lecture')) {
            await api(`/lectures/${button.dataset.publishLecture}/publish`, { method: 'POST' });
            message('Lecture published.'); return openCourse(activeCourse);
        }
        if (button.hasAttribute('data-unpublish-lecture')) {
            await api(`/lectures/${button.dataset.unpublishLecture}/unpublish`, { method: 'POST' });
            message('Lecture moved back to draft.'); return openCourse(activeCourse);
        }
        if (button.hasAttribute('data-delete-module')) {
            await api(`/modules/${button.dataset.deleteModule}`, { method: 'DELETE' });
            message('Module removed.'); return openCourse(activeCourse);
        }
        if (button.hasAttribute('data-students-for-course')) return manageCourseStudents();
        if (button.hasAttribute('data-toggle-quiz')) {
            await api(`/quizzes/${button.dataset.toggleQuiz}`, { method: 'PUT', body: JSON.stringify({
                title: data.quizzes.find(quiz => quiz.id === button.dataset.toggleQuiz).title,
                status: button.dataset.status === 'published' ? 'draft' : 'published'
            }) });
            message('Quiz status updated.'); return openCourse(activeCourse);
        }
        if (button.hasAttribute('data-edit-quiz')) return editQuiz(button.dataset.editQuiz);
        if (button.hasAttribute('data-delete-quiz')) {
            if (!window.confirm('Delete this quiz? Quizzes with student attempts cannot be deleted.')) return;
            await api(`/quizzes/${button.dataset.deleteQuiz}`, { method: 'DELETE' });
            message('Quiz deleted.'); return openCourse(activeCourse);
        }
        if (button.hasAttribute('data-student-lecture')) return openStudentLecture(button.dataset.studentLecture);
        if (button.hasAttribute('data-student-quiz')) return openStudentQuiz(button.dataset.studentQuiz);
    } catch (error) { message(error.message, 'error'); }
}

async function manageLectureCheckpoints(lectureId, editing = null) {
    try {
        const [{ title }, { checkpoints }] = await Promise.all([
            api(`/lectures/${lectureId}`), api(`/lectures/${lectureId}/checkpoints`)
        ]);
        content.innerHTML = `${pageHead(`In-video questions · ${escapeHtml(title)}`, 'Objective checkpoints pause the video and store one response per enrolled student.') }
            <section class="detail-card"><p class="muted small">Question response rates use enrolled students as the denominator. This feature records responses; it does not measure attention.</p>
            <form id="checkpointForm"><input type="hidden" name="checkpoint_id">
                <div class="form-grid"><div class="field"><label>Position (seconds)</label><input name="position_seconds" type="number" min="0" max="86400" step="0.1" required></div>
                <div class="field"><label>Question type</label><select name="kind"><option value="multiple_choice">Multiple choice</option><option value="true_false">True or false</option></select></div></div>
                <div class="field"><label>Prompt</label><textarea name="prompt" maxlength="2000" required></textarea></div>
                <div class="field"><label>Options (one per line; ignored for true/false)</label><textarea name="options" placeholder="First option&#10;Second option"></textarea></div>
                <div class="field"><label>Correct answer</label><input name="answer" required></div>
                <div class="row"><button class="button primary">Save question</button><button type="button" class="button" data-back-course>Back to course</button></div>
            </form></section>
            <section class="detail-card"><h3>Existing checkpoints</h3>${checkpoints.map(item => `<div class="lecture-row"><div><b>${Math.floor(item.position_seconds)}s · ${escapeHtml(item.prompt)}</b><small>${escapeHtml(item.kind)} · answer: ${escapeHtml(item.answer)}</small></div>
                <div class="row"><button class="button small" data-edit-checkpoint="${item.id}">Edit</button><button class="button small danger" data-delete-checkpoint="${item.id}">Delete</button></div></div>`).join('') || empty('No questions yet', 'Add a checkpoint to collect an in-video response.')}</section>`;
        const form = $('#checkpointForm');
        if (editing) {
            form.elements.checkpoint_id.value = editing.id;
            form.elements.position_seconds.value = editing.position_seconds;
            form.elements.kind.value = editing.kind;
            form.elements.prompt.value = editing.prompt;
            form.elements.options.value = editing.options.join('\n');
            form.elements.answer.value = editing.answer;
        }
        form.onsubmit = async event => {
            event.preventDefault();
            const values = new FormData(form);
            const kind = values.get('kind');
            const options = kind === 'true_false' ? ['True', 'False'] :
                String(values.get('options')).split('\n').map(option => option.trim()).filter(Boolean);
            const payload = {
                position_seconds: Number(values.get('position_seconds')),
                kind, prompt: values.get('prompt'), options, answer: values.get('answer')
            };
            const checkpointId = values.get('checkpoint_id');
            try {
                await api(checkpointId ? `/checkpoints/${checkpointId}` : `/lectures/${lectureId}/checkpoints`, {
                    method: checkpointId ? 'PUT' : 'POST', body: JSON.stringify(payload)
                });
                message('In-video question saved.');
                await manageLectureCheckpoints(lectureId);
            } catch (error) { message(error.message, 'error'); }
        };
        content.onclick = async event => {
            if (event.target.closest('[data-back-course]')) return openCourse(activeCourse);
            const edit = event.target.closest('[data-edit-checkpoint]');
            if (edit) return manageLectureCheckpoints(lectureId, checkpoints.find(item => item.id === edit.dataset.editCheckpoint));
            const remove = event.target.closest('[data-delete-checkpoint]');
            if (remove) {
                try {
                    await api(`/checkpoints/${remove.dataset.deleteCheckpoint}`, { method: 'DELETE' });
                    message('In-video question deleted.');
                    await manageLectureCheckpoints(lectureId);
                } catch (error) { message(error.message, 'error'); }
            }
        };
    } catch (error) { message(error.message, 'error'); }
}

function showEditCourse(course) {
    content.innerHTML = `${pageHead('Edit course', 'Update the course information.')}
        <section class="detail-card"><form id="courseForm"><div class="field"><label>Course name</label><input name="title" required value="${escapeHtml(course.title)}"></div>
        <div class="field"><label>Description</label><textarea name="description">${escapeHtml(course.description)}</textarea></div>
        <div class="field"><label>Enrollment access</label><select name="visibility"><option value="private" ${course.visibility === 'private' ? 'selected' : ''}>Private — invitation link or instructor enrollment</option><option value="public" ${course.visibility === 'public' ? 'selected' : ''}>Public — students can find and join it</option></select></div>
        <div class="row"><button class="button primary">Save changes</button><button type="button" class="button" data-back>Cancel</button></div></form></section>`;
    $('#courseForm').onsubmit = async event => {
        event.preventDefault();
        try {
            const values = Object.fromEntries(new FormData(event.currentTarget));
            await api(`/courses/${activeCourse}`, { method: 'PUT', body: JSON.stringify({ ...values, status: course.status }) });
            message('Course updated.'); await openCourse(activeCourse);
        } catch (error) { message(error.message, 'error'); }
    };
    $('[data-back]').onclick = () => openCourse(activeCourse);
}

async function addModule() {
    const title = window.prompt('Module title');
    if (!title?.trim()) return;
    try {
        await api(`/courses/${activeCourse}/modules`, { method: 'POST', body: JSON.stringify({ title }) });
        message('Module added.'); await openCourse(activeCourse);
    } catch (error) { message(error.message, 'error'); }
}

function showLectureForm() {
    api(`/courses/${activeCourse}`).then(({ modules }) => {
        content.innerHTML = `${pageHead('Add a lecture', 'Upload a video or import a YouTube lecture. AI processing continues in the background.')}
            <section class="detail-card"><form id="lectureForm">
            <div class="form-grid"><div class="field"><label>Lecture title</label><input name="title" required maxlength="180"></div>
            <div class="field"><label>Module</label><select name="module_id"><option value="">Unassigned</option>${modules.map(module => `<option value="${module.id}">${escapeHtml(module.title)}</option>`).join('')}</select></div></div>
            <div class="field"><label>YouTube URL (HTTPS)</label><input name="youtube_url" type="url" placeholder="https://www.youtube.com/watch?v=..."></div>
            <div class="field"><label>Or upload a video (maximum 2 GB)</label><input name="video" type="file" accept="video/*"></div>
            <p class="muted small">AI transcripts and chapters remain drafts. Review and approve them before publishing.</p>
            <div class="row"><button class="button primary" id="lectureSubmit">Add lecture</button><button type="button" class="button" data-return-course>Cancel</button></div>
            </form></section>`;
        $('#lectureForm').onsubmit = async event => {
            event.preventDefault();
            const form = new FormData(event.currentTarget);
            if (!form.get('video')?.size) form.delete('video');
            const url = `/courses/${activeCourse}/lectures?${new URLSearchParams({
                title: form.get('title'), module_id: form.get('module_id'), youtube_url: form.get('youtube_url')
            })}`;
            $('#lectureSubmit').disabled = true;
            try {
                const result = await api(url, { method: 'POST', body: form });
                message('Lecture added. AI processing has started.');
                await openCourse(activeCourse);
            } catch (error) { message(error.message, 'error'); $('#lectureSubmit').disabled = false; }
        };
        $('[data-return-course]').onclick = () => openCourse(activeCourse);
    }).catch(error => message(error.message, 'error'));
}

async function reviewLecture(lectureId) {
    activeLecture = lectureId;
    try {
        const lecture = await api(`/lectures/${lectureId}`);
        const draft = lecture.content || {};
        const transcript = Array.isArray(draft.transcript) ? draft.transcript : [];
        const chapters = Array.isArray(draft.chapters) ? draft.chapters : [];
        content.innerHTML = `${pageHead('Review lecture', `${escapeHtml(lecture.title)} · AI material is not visible to students until approved and published.`)}
            <section class="detail-card"><div class="row"><span class="badge ${lecture.status}">${lecture.status}</span>
                ${lecture.approved_at ? '<span class="badge published">Instructor approved</span>' : '<span class="badge draft">Needs approval</span>'}</div>
                ${lecture.video_id ? `<p><video controls preload="metadata" style="width:100%;max-height:350px;margin-top:15px;border-radius:9px;background:#101828" src="/api/lms/lectures/${lecture.id}/video"></video></p>` : ''}
                <form id="reviewForm">
                    <div class="field"><label>Summary</label><textarea name="summary" rows="4">${escapeHtml(draft.summary || '')}</textarea></div>
                    <div class="field"><label>Transcript segments (JSON; edit text and timestamps)</label><textarea class="content-editor" name="transcript">${escapeHtml(JSON.stringify(transcript, null, 2))}</textarea></div>
                    <div class="field"><label>Chapters (JSON)</label><textarea class="content-editor" name="chapters">${escapeHtml(JSON.stringify(chapters, null, 2))}</textarea></div>
                    <div class="row"><button type="button" class="button" id="generateContent">✦ Generate chapters &amp; summary</button><button class="button primary">Save draft edits</button><button type="button" class="button" id="approveContent">Approve content</button><button type="button" class="button" data-return-course>Back to course</button></div>
                </form></section>`;
        $('#reviewForm').onsubmit = async event => {
            event.preventDefault();
            const form = new FormData(event.currentTarget);
            try {
                await api(`/lectures/${lectureId}/content`, { method: 'PUT', body: JSON.stringify({
                    summary: form.get('summary'), transcript: JSON.parse(form.get('transcript')),
                    chapters: JSON.parse(form.get('chapters'))
                }) });
                message('Draft edits saved. Approve them before publishing.');
                await reviewLecture(lectureId);
            } catch (error) { message(error instanceof SyntaxError ? 'Transcript and chapters must be valid JSON.' : error.message, 'error'); }
        };
        $('#approveContent').onclick = async () => {
            try {
                await api(`/lectures/${lectureId}/approve`, { method: 'POST' });
                message('Lecture content approved. It is still a draft until you publish it.');
                await reviewLecture(lectureId);
            } catch (error) { message(error.message, 'error'); }
        };
        $('#generateContent').onclick = async () => {
            const button = $('#generateContent');
            button.disabled = true;
            button.textContent = 'Generating draft content…';
            try {
                const result = await api(`/lectures/${lectureId}/generate-content`, { method: 'POST' });
                message(result.summary_error
                    ? `Chapters generated as a draft; summary generation failed: ${result.summary_error}`
                    : 'Draft chapters and summary generated. Review and approve before publishing.',
                result.summary_error ? 'error' : 'success');
                await reviewLecture(lectureId);
            } catch (error) {
                message(error.message, 'error');
                button.disabled = false;
                button.textContent = '✦ Generate chapters & summary';
            }
        };
        $('[data-return-course]').onclick = () => openCourse(activeCourse);
    } catch (error) { message(error.message, 'error'); }
}

function showQuizForm() {
    editingQuizId = null;
    suggestionsNeedReview = false;
    suggestionsReviewed = false;
    suggestionStatus = '';
    quizQuestions = [{ prompt: '', kind: 'multiple_choice', options: ['Option A', 'Option B', 'Option C', 'Option D'], answer: 'Option A', points: 1 }];
    renderQuizForm();
}

function renderQuizForm(existingTitle = '', dueAt = '', timeLimit = '', attemptLimit = 1) {
    content.innerHTML = `${pageHead(editingQuizId ? 'Edit a quiz' : 'Create a quiz', 'Build objective or manually graded questions with per-question marks.')}
        <section class="detail-card"><form id="quizForm"><div class="form-grid">
            <div class="field"><label>Quiz title</label><input name="title" value="${escapeHtml(existingTitle)}" required maxlength="180"></div>
            <div class="field"><label>Deadline</label><input name="due_at" type="datetime-local" value="${escapeHtml(dueAt)}"></div>
            <div class="field"><label>Time limit in minutes (optional)</label><input name="time_limit_minutes" type="number" min="1" max="720" step="1" value="${escapeHtml(timeLimit)}"></div>
            <div class="field"><label>Maximum attempts</label><input name="attempt_limit" type="number" min="1" max="20" step="1" value="${escapeHtml(attemptLimit)}" required></div></div>
            <div id="questions">${quizQuestions.map(questionEditor).join('')}</div>
            <div class="row"><button type="button" class="button" id="addQuestion">＋ Add question</button><button type="button" class="button" id="suggestQuestions">✦ Suggest from lecture</button></div>
            <p class="muted small" id="suggestStatus">${escapeHtml(suggestionStatus)}</p>
            ${suggestionsNeedReview ? '<button type="button" class="button primary" id="confirmSuggestions">I reviewed these AI suggestions</button>' : ''}
            <div class="row" style="margin-top:16px"><button class="button primary">${editingQuizId ? 'Save quiz changes' : 'Save quiz as draft'}</button><button type="button" class="button" data-return-course>Cancel</button></div>
        </form></section>`;
    $('#questions').oninput = updateQuestion;
    $('#questions').onchange = updateQuestion;
    $('#questions').onclick = event => {
        const remove = event.target.closest('[data-remove-question]');
        if (remove && quizQuestions.length > 1) { quizQuestions.splice(Number(remove.dataset.removeQuestion), 1); renderQuizForm(existingTitle, dueAt, timeLimit, attemptLimit); }
    };
    $('#addQuestion').onclick = () => { quizQuestions.push({ prompt: '', kind: 'multiple_choice', options: ['Option A', 'Option B'], answer: 'Option A', points: 1 }); renderQuizForm(existingTitle, dueAt, timeLimit, attemptLimit); };
    $('#suggestQuestions').onclick = suggestQuestions;
    $('#confirmSuggestions')?.addEventListener('click', () => {
        suggestionsNeedReview = false;
        suggestionsReviewed = true;
        suggestionStatus = 'Reviewed. You may now save these suggestions as quiz questions.';
        renderQuizForm(existingTitle, dueAt, timeLimit, attemptLimit);
    });
    $('[data-return-course]').onclick = () => openCourse(activeCourse);
    $('#quizForm').onsubmit = async event => {
        event.preventDefault();
        const form = new FormData(event.currentTarget);
        const due = form.get('due_at') ? new Date(form.get('due_at')).toISOString() : null;
        if (suggestionsNeedReview) {
            message('Review and confirm the AI-suggested questions before saving.', 'error');
            return;
        }
        const values = {
            title: form.get('title'), due_at: due,
            time_limit_minutes: form.get('time_limit_minutes') ? Number(form.get('time_limit_minutes')) : null,
            attempt_limit: Number(form.get('attempt_limit')), questions: quizQuestions,
            reviewed_suggestions: suggestionsReviewed
        };
        try {
            if (editingQuizId) {
                const current = await api(`/quizzes/${editingQuizId}`);
                await api(`/quizzes/${editingQuizId}`, { method: 'PUT', body: JSON.stringify({
                    ...values, status: current.quiz.status
                }) });
            } else {
                await api(`/courses/${activeCourse}/quizzes`, { method: 'POST', body: JSON.stringify(values) });
            }
            message(editingQuizId ? 'Quiz changes saved.' : 'Quiz saved as a draft. Publish it from the course when ready.');
            await openCourse(activeCourse);
        } catch (error) { message(error.message, 'error'); }
    };
}

function questionEditor(question, index) {
    const options = question.kind === 'true_false' ? ['True', 'False'] : (question.options || []);
    const subjective = ['short_answer', 'essay'].includes(question.kind);
    return `<div class="question-card"><div class="panel-header"><b>Question ${index + 1}</b>${quizQuestions.length > 1 ? `<button class="button small danger" type="button" data-remove-question="${index}">Remove</button>` : ''}</div>
        <div class="field"><label>Question</label><input data-question="${index}" data-key="prompt" value="${escapeHtml(question.prompt)}" required></div>
        <div class="form-grid"><div class="field"><label>Question type</label><select data-question="${index}" data-key="kind">
            <option value="multiple_choice" ${question.kind === 'multiple_choice' ? 'selected' : ''}>Multiple choice</option>
            <option value="true_false" ${question.kind === 'true_false' ? 'selected' : ''}>True / false</option>
            <option value="short_answer" ${question.kind === 'short_answer' ? 'selected' : ''}>Short answer</option>
            <option value="essay" ${question.kind === 'essay' ? 'selected' : ''}>Essay</option></select></div>
            <div class="field"><label>Marks</label><input data-question="${index}" data-key="points" type="number" min="0.1" max="1000" step="0.1" value="${question.points}"></div></div>
        ${subjective ? '<p class="muted small">Students provide a text response. The answer will be marked pending until an instructor grades it.</p>' : ''}
        ${question.kind === 'multiple_choice' ? `<div class="field"><label>Answer options (one per line)</label><textarea data-question="${index}" data-key="options">${escapeHtml(options.join('\n'))}</textarea></div>` : ''}
        ${subjective ? '' : `<div class="field"><label>Correct answer</label><select data-question="${index}" data-key="answer">${options.map(option => `<option ${option === question.answer ? 'selected' : ''}>${escapeHtml(option)}</option>`).join('')}</select></div>`}</div>`;
}

function updateQuestion(event) {
    const input = event.target.closest('[data-question]');
    if (!input) return;
    const index = Number(input.dataset.question);
    const key = input.dataset.key;
    if (key === 'options') {
        const options = input.value.split('\n').map(value => value.trim()).filter(Boolean);
        quizQuestions[index].options = options;
        if (!options.includes(quizQuestions[index].answer)) quizQuestions[index].answer = options[0] || '';
        const answer = input.closest('.question-card').querySelector('[data-key="answer"]');
        if (answer) answer.innerHTML = options.map(option => `<option>${escapeHtml(option)}</option>`).join('');
    } else if (key === 'kind') {
        quizQuestions[index].kind = input.value;
        if (['short_answer', 'essay'].includes(input.value)) {
            quizQuestions[index].options = [];
            delete quizQuestions[index].answer;
        } else if (input.value === 'true_false') quizQuestions[index].options = ['True', 'False'];
        else if (!quizQuestions[index].options || quizQuestions[index].options.length < 2) quizQuestions[index].options = ['Option A', 'Option B'];
        if (!['short_answer', 'essay'].includes(input.value)) {
            quizQuestions[index].answer = quizQuestions[index].options[0];
        }
        renderQuizForm($('#quizForm').elements.title.value, $('#quizForm').elements.due_at.value,
            $('#quizForm').elements.time_limit_minutes.value, Number($('#quizForm').elements.attempt_limit.value));
    } else {
        quizQuestions[index][key] = key === 'points' ? Number(input.value) : input.value;
    }
}

async function suggestQuestions() {
    suggestionStatus = 'Generating questions from the lecture transcript…';
    $('#suggestStatus').textContent = suggestionStatus;
    $('#suggestQuestions').disabled = true;
    try {
        if (!activeCourse) throw new Error('Open a course to choose a lecture.');
        const { lectures } = await api(`/courses/${activeCourse}`);
        const publishedLectures = lectures.filter(lecture => lecture.status === 'published');
        if (!publishedLectures.length) throw new Error('Publish an approved lecture before generating question suggestions.');
        const selected = window.prompt(`Enter a lecture number:\n${publishedLectures.map((lecture, index) => `${index + 1}. ${lecture.title}`).join('\n')}`);
        const lecture = publishedLectures[Number(selected) - 1];
        if (!lecture) throw new Error('Choose a valid lecture number.');
        const result = await api(`/lectures/${lecture.id}/quiz-suggestions`);
        quizQuestions = result.questions;
        suggestionsNeedReview = true;
        suggestionsReviewed = false;
        suggestionStatus = `Generated from a published lecture transcript using ${result.provider}. Edit and review the questions, then confirm.`;
        renderQuizForm($('#quizForm').elements.title.value, $('#quizForm').elements.due_at.value,
            $('#quizForm').elements.time_limit_minutes.value, Number($('#quizForm').elements.attempt_limit.value));
    } catch (error) {
        suggestionStatus = error.message;
        $('#suggestStatus').textContent = error.message;
    }
    finally { $('#suggestQuestions')?.removeAttribute('disabled'); }
}

async function loadCourseStudents(courseId) {
    const target = $('#courseStudents');
    if (!target) return;
    try {
        const { students } = await api(`/students?course_id=${encodeURIComponent(courseId)}`);
        target.innerHTML = students.length ? students.map(student => `<div class="lecture-row"><div><strong>${escapeHtml(student.name)}</strong><small>${escapeHtml(student.email)}</small></div>
            <button class="button small danger" data-remove-enrollment="${student.id}">Remove</button></div>`).join('')
            : empty('No enrolled students', 'Create student accounts and enroll them in this course.');
        target.onclick = async event => {
            const button = event.target.closest('[data-remove-enrollment]');
            if (!button) return;
            try { await api(`/courses/${courseId}/enrollments/${button.dataset.removeEnrollment}`, { method: 'DELETE' }); message('Student removed.'); await openCourse(courseId); }
            catch (error) { message(error.message, 'error'); }
        };
    } catch (error) { target.textContent = error.message; }
}

async function manageCourseStudents() {
    const { students } = await api('/students');
    content.innerHTML = `${pageHead('Manage enrollment', 'Enroll existing student accounts in this course.')}
        <section class="detail-card"><div class="inline-form"><div class="field"><label>Student</label><select id="enrollStudent">${students.map(student =>
            `<option value="${student.id}">${escapeHtml(student.name)} · ${escapeHtml(student.email)}</option>`).join('')}</select></div>
            <button class="button primary" id="enrollButton" ${students.length ? '' : 'disabled'}>Enroll student</button></div>
            ${students.length ? '' : empty('No student accounts yet', 'Create a student account in Student management first.')}
            <button class="button" data-back-course>Back to course</button></section>`;
    $('#enrollButton').onclick = async () => {
        try { await api(`/courses/${activeCourse}/enrollments`, { method: 'POST', body: JSON.stringify({ student_id: $('#enrollStudent').value }) }); message('Student enrolled.'); await openCourse(activeCourse); }
        catch (error) { message(error.message, 'error'); }
    };
    $('[data-back-course]').onclick = () => openCourse(activeCourse);
}

async function renderStudents() {
    setHeading('Students');
    const { students } = await api('/students');
    const { instructors } = await api('/instructors');
    content.innerHTML = `${pageHead('Student management', 'Create student accounts and manage course enrollment.',
        '<button class="button primary" data-add-student>＋ Add student</button>')}
        <section class="detail-card"><div class="table-wrap"><table><thead><tr><th>Student</th><th>Email</th><th>Account created</th></tr></thead><tbody>
        ${students.map(student => `<tr><td><b>${escapeHtml(student.name)}</b></td><td>${escapeHtml(student.email)}</td><td>${dateText(student.created_at || student.enrolled_at)}</td></tr>`).join('')}
        </tbody></table>${students.length ? '' : empty('No students yet', 'Create a student account and enroll them in a course.')}</div></section>
        <section class="detail-card"><h3>Enroll students</h3><p class="muted small">Choose a course and an existing student account.</p>
        <div class="inline-form"><div class="field"><label>Course</label><select id="enrollCourse"></select></div><div class="field"><label>Student</label><select id="enrollUser">${students.map(student => `<option value="${student.id}">${escapeHtml(student.name)} · ${escapeHtml(student.email)}</option>`).join('')}</select></div>
        <button class="button primary" id="enrollFromStudents">Enroll</button></div></section>
        <section class="detail-card"><div class="panel-header"><h3>Instructor accounts</h3><button class="button small" data-add-instructor>＋ Add instructor</button></div>
        <div class="table-wrap"><table><thead><tr><th>Instructor</th><th>Email</th><th>Account created</th></tr></thead><tbody>
        ${instructors.map(instructor => `<tr><td><b>${escapeHtml(instructor.name)}</b></td><td>${escapeHtml(instructor.email)}</td><td>${dateText(instructor.created_at)}</td></tr>`).join('')}
        </tbody></table></div><p class="muted small">Only instructors can create additional instructor accounts. Instructors cannot self-register after the initial setup.</p></section>`;
    const courses = await api('/courses');
    $('#enrollCourse').innerHTML = courses.courses.map(course => `<option value="${course.id}">${escapeHtml(course.title)}</option>`).join('');
    content.onclick = event => {
        if (event.target.closest('[data-add-student]')) return studentForm();
        if (event.target.closest('[data-add-instructor]')) return instructorForm();
        if (event.target.closest('#enrollFromStudents')) enrollFromStudents();
    };
}

function instructorForm() {
    content.innerHTML = `${pageHead('Add an instructor', 'Create a trusted instructor account.') }
        <section class="detail-card"><form id="instructorForm"><div class="field"><label>Instructor name</label><input name="name" required maxlength="120"></div>
        <div class="field"><label>Email</label><input type="email" name="email" required maxlength="254"></div>
        <div class="field"><label>Temporary password (at least 12 characters)</label><input type="password" name="password" minlength="12" maxlength="256" required></div>
        <div class="row"><button class="button primary">Create instructor</button><button type="button" class="button" data-back-students>Cancel</button></div></form></section>`;
    $('#instructorForm').onsubmit = async event => {
        event.preventDefault();
        try {
            await api('/instructors', { method: 'POST', body: JSON.stringify(Object.fromEntries(new FormData(event.currentTarget))) });
            message('Instructor account created. Share the login credentials securely.');
            await renderStudents();
        } catch (error) { message(error.message, 'error'); }
    };
    $('[data-back-students]').onclick = renderStudents;
}

function studentForm() {
    content.innerHTML = `${pageHead('Add a student', 'Create a login, then enroll them in a course.')}
        <section class="detail-card"><form id="studentForm"><div class="field"><label>Student name</label><input name="name" required></div>
        <div class="field"><label>Email</label><input type="email" name="email" required></div>
        <div class="field"><label>Temporary password (at least 12 characters)</label><input type="password" name="password" minlength="12" required></div>
        <div class="row"><button class="button primary">Create student</button><button type="button" class="button" data-back-students>Cancel</button></div></form></section>`;
    $('#studentForm').onsubmit = async event => {
        event.preventDefault();
        try {
            await api('/students', { method: 'POST', body: JSON.stringify(Object.fromEntries(new FormData(event.currentTarget))) });
            message('Student account created. Enroll them in the course to grant access.');
            await renderStudents();
        } catch (error) { message(error.message, 'error'); }
    };
    $('[data-back-students]').onclick = renderStudents;
}

async function enrollFromStudents() {
    try {
        await api(`/courses/${$('#enrollCourse').value}/enrollments`, { method: 'POST', body: JSON.stringify({ student_id: $('#enrollUser').value }) });
        message('Student enrolled in the course.');
    } catch (error) { message(error.message, 'error'); }
}

async function openStudentLecture(lectureId) {
    stopVideoTracking();
    try {
        const lecture = await api(`/lectures/${lectureId}`);
        const checkpoints = lecture.video_id
            ? (await api(`/lectures/${lectureId}/checkpoints`)).checkpoints : [];
        const transcript = lecture.content?.transcript || [];
        const chapters = lecture.content?.chapters || [];
        content.innerHTML = `${pageHead(escapeHtml(lecture.title), 'Course lecture')}
            <section class="detail-card">${lecture.video_id ? `<video controls preload="metadata" style="width:100%;max-height:420px;border-radius:9px;background:#101828" src="/api/lms/lectures/${lecture.id}/video"></video><p class="muted small" id="videoTrackingStatus" role="status">Playback progress is recorded for course analytics. It is approximate and does not indicate attention.</p>` : ''}
            <div id="checkpointPrompt" aria-live="polite"></div>
            ${lecture.content?.summary ? `<h3 class="section-title">Summary</h3><p>${escapeHtml(lecture.content.summary)}</p>` : ''}
            ${chapters.length ? `<h3 class="section-title">Chapters</h3>${chapters.map(chapter => `<div class="lecture-row"><b>${escapeHtml(chapter.timestamp || '')} · ${escapeHtml(chapter.title || '')}</b><span>${escapeHtml(chapter.summary || '')}</span></div>`).join('')}` : ''}
            ${transcript.length ? `<h3 class="section-title">Transcript</h3><div class="transcript-preview">${transcript.map(item => `${escapeHtml(item.start ?? '')}  ${escapeHtml(item.text || '')}`).join('\n')}</div>` : empty('Transcript not ready', 'Your instructor will publish reviewed lecture content here.')}
            <button class="button" data-back-course>Back to course</button></section>`;
        const video = $('video', content);
        if (video) {
            const tracker = trackLectureVideo(video, lecture.id);
            attachCheckpointQuestions(video, lecture.id, checkpoints, tracker);
        }
        $('[data-back-course]').onclick = () => openCourse(activeCourse);
    } catch (error) { message(error.message, 'error'); }
}

function trackLectureVideo(video, lectureId) {
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('');
    const sessionId = `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
    const status = $('#videoTrackingStatus');
    let played = false;
    let seeking = false;
    let lastProgressSentAt = 0;
    let eventChain = Promise.resolve();
    const controller = new AbortController();
    const options = { signal: controller.signal };
    const send = (eventType, checkpointId = null) => {
        const duration = Number.isFinite(video.duration) && video.duration > 0 ? video.duration : null;
        const position = Number.isFinite(video.currentTime) ? video.currentTime : 0;
        eventChain = eventChain.then(() => api(`/lectures/${lectureId}/video-events`, {
            method: 'POST',
            keepalive: eventType === 'interrupted',
            body: JSON.stringify({
                session_id: sessionId, event_type: eventType, position_seconds: position,
                duration_seconds: duration, ...(checkpointId ? { checkpoint_id: checkpointId } : {})
            })
        })).then(() => {
            if (status) status.textContent = 'Playback progress saved. This is approximate activity, not a measure of attention.';
        }).catch(error => {
            if (status) status.textContent = `Playback tracking could not be saved: ${error.message}`;
        });
        return eventChain;
    };
    const sendProgress = () => {
        if (played && !seeking) {
            lastProgressSentAt = Date.now();
            return send('progress');
        }
    };
    send('access');
    video.addEventListener('play', () => {
        send(played ? 'resume' : 'play');
        played = true;
    }, options);
    video.addEventListener('pause', () => {
        if (!video.ended) {
            sendProgress();
            send('pause');
        }
    }, options);
    video.addEventListener('seeking', () => { seeking = true; }, options);
    video.addEventListener('seeked', () => {
        seeking = false;
        send('seek');
    }, options);
    video.addEventListener('timeupdate', () => {
        const now = Date.now();
        if (!video.paused && !seeking && now - lastProgressSentAt >= 10000) {
            sendProgress();
        }
    }, options);
    video.addEventListener('ended', () => {
        sendProgress();
        send('ended');
    }, options);
    document.addEventListener('visibilitychange', () => {
        if (document.hidden && !video.paused) {
            sendProgress();
            send('interrupted');
        }
        else if (!document.hidden && !video.paused) send('resume');
    }, options);
    window.addEventListener('pagehide', () => {
        if (!video.paused && !video.ended) {
            sendProgress();
            send('interrupted');
        }
    }, { once: true, signal: controller.signal });
    activeVideoCleanup = () => {
        if (!video.paused && !video.ended) {
            sendProgress();
            send('interrupted');
        }
        controller.abort();
    };
    return { recordCheckpoint: checkpointId => send('checkpoint', checkpointId) };
}

function attachCheckpointQuestions(video, lectureId, checkpoints, tracker) {
    const prompt = $('#checkpointPrompt');
    const answered = new Set(checkpoints.filter(item => item.response).map(item => item.id));
    let active = null;
    video.addEventListener('timeupdate', () => {
        if (active) return;
        const checkpoint = checkpoints.find(item =>
            !answered.has(item.id) && video.currentTime >= item.position_seconds
        );
        if (!checkpoint) return;
        active = checkpoint.id;
        tracker.recordCheckpoint(checkpoint.id);
        video.pause();
        prompt.innerHTML = `<section class="detail-card" role="group" aria-labelledby="checkpointTitle">
            <h3 id="checkpointTitle">Quick question · ${Math.floor(checkpoint.position_seconds)}s</h3>
            <p>${escapeHtml(checkpoint.prompt)}</p>
            <form id="checkpointResponse"><fieldset><legend>Choose one answer</legend>
            ${checkpoint.options.map((option, index) => `<label class="choice-row"><input type="radio" name="answer" value="${escapeHtml(option)}" required>${escapeHtml(option)}</label>`).join('')}
            </fieldset><button class="button primary">Submit response</button></form></section>`;
        $('#checkpointResponse').onsubmit = async event => {
            event.preventDefault();
            const answer = new FormData(event.currentTarget).get('answer');
            const submit = $('button', event.currentTarget);
            submit.disabled = true;
            try {
                const result = await api(`/lectures/${lectureId}/checkpoints/${checkpoint.id}/responses`, {
                    method: 'POST', body: JSON.stringify({ answer })
                });
                answered.add(checkpoint.id);
                prompt.innerHTML = `<section class="detail-card" role="status">
                    <p>${result.is_correct ? 'Correct response.' : 'Response recorded; review this concept if needed.'}</p>
                    <p class="muted small">Your answer is stored for course learning analytics.</p>
                    <button class="button" id="continueVideo">Continue video</button></section>`;
                $('#continueVideo').onclick = () => {
                    prompt.innerHTML = '';
                    active = null;
                    video.play().catch(error => {
                        const status = $('#videoTrackingStatus');
                        if (status) status.textContent = `Video could not resume: ${error.message}`;
                    });
                };
            } catch (error) {
                submit.disabled = false;
                message(error.message, 'error');
            }
        };
    });
}

function stopVideoTracking() {
    if (activeVideoCleanup) {
        activeVideoCleanup();
        activeVideoCleanup = null;
    }
}

async function openStudentQuiz(quizId) {
    window.clearInterval(window.quizTimer);
    try {
        const data = await api(`/students/quizzes/${quizId}`);
        const current = data.attempts.find(attempt => attempt.status === 'in_progress');
        const previous = data.attempts.filter(attempt => attempt.status === 'submitted');
        content.innerHTML = `${pageHead(escapeHtml(data.quiz.title), data.quiz.due_at ? `Due ${dateText(data.quiz.due_at)}` : 'Quiz assessment')}
            <section class="detail-card"><p class="muted">Attempts used: ${data.attempts_used} of ${data.quiz.attempt_limit}${data.quiz.time_limit_minutes ? ` · Time limit: ${data.quiz.time_limit_minutes} minutes` : ''}</p>
            ${current ? `<button class="button primary" data-resume-attempt="${current.id}">Resume attempt ${current.attempt_number}</button>` :
                data.attempts_remaining > 0 ? '<button class="button primary" data-start-attempt>Start attempt</button>' : empty('No attempts remaining', 'This quiz has reached its attempt limit.')}
            ${previous.map(attempt => `<div class="lecture-row"><div><b>Attempt ${attempt.attempt_number}</b><small>${dateText(attempt.submitted_at)} · ${escapeHtml(attempt.feedback || (attempt.grading_status === 'pending' ? 'Pending manual grading' : 'Automatically graded.'))}</small></div><span>${attempt.grade === null ? 'Pending' : `${attempt.grade} / ${data.questions.reduce((sum, question) => sum + question.points, 0)}`}</span></div>`).join('')}
            <button class="button" data-back-course>Back to course</button></section>`;
        $('[data-start-attempt], [data-resume-attempt]')?.addEventListener('click', () => startQuizAttempt(quizId));
        $('[data-back-course]').onclick = () => openCourse(activeCourse);
    } catch (error) { message(error.message, 'error'); }
}

async function startQuizAttempt(quizId) {
    try {
        const data = await api(`/quizzes/${quizId}/attempts`, { method: 'POST' });
        renderActiveQuiz(data);
    } catch (error) { message(error.message, 'error'); await openStudentQuiz(quizId); }
}

function renderActiveQuiz(data) {
    const { attempt } = data;
    const questions = data.questions;
    content.innerHTML = `${pageHead(escapeHtml(data.quiz.title), data.quiz.due_at ? `Due ${dateText(data.quiz.due_at)}` : 'Quiz assessment')}
        <section class="detail-card"><p class="muted">Attempt ${attempt.attempt_number}${data.quiz.time_limit_minutes ? ` · Time remaining: <b id="attemptTimer"></b>` : ''}</p>
        <p id="attemptSaveStatus" class="muted small" role="status">Your answers are saved as you respond.</p>
        <form id="studentQuiz">${questions.map((question, index) =>
            `<div class="question-card"><b>${index + 1}. ${escapeHtml(question.prompt)}</b><p class="muted small">${question.points} marks</p>
            ${['short_answer', 'essay'].includes(question.kind)
                ? `<textarea name="${question.id}" maxlength="20000" required aria-label="Your response to question ${index + 1}">${escapeHtml(attempt.answers[question.id] || '')}</textarea>`
                : question.options.map(option => `<label class="lecture-row"><span><input type="radio" name="${question.id}" value="${escapeHtml(option)}" ${attempt.answers[question.id] === option ? 'checked' : ''} required style="width:auto;margin-right:8px">${escapeHtml(option)}</span></label>`).join('')}</div>`).join('')}
            <button class="button primary" id="submitAttempt">Submit attempt</button></form>
            <button class="button" data-back-course>Back to course</button></section>`;
    const form = $('#studentQuiz');
    let saveTimer = null;
    let saveChain = Promise.resolve();
    const queueAnswerSave = () => {
        window.clearTimeout(saveTimer);
        saveTimer = window.setTimeout(() => {
            const answers = Object.fromEntries(new FormData(form));
            saveChain = saveChain.then(() => api(`/quiz-attempts/${attempt.id}`, {
                method: 'PUT', body: JSON.stringify({ answers })
            })).then(() => {
                const saveStatus = $('#attemptSaveStatus');
                if (saveStatus) saveStatus.textContent = 'Progress saved.';
            }).catch(error => {
                const saveStatus = $('#attemptSaveStatus');
                if (saveStatus) saveStatus.textContent = error.message;
            });
        }, 250);
    };
    form.addEventListener('change', queueAnswerSave);
    form.addEventListener('input', queueAnswerSave);
    form.addEventListener('submit', async event => {
        event.preventDefault();
        window.clearTimeout(saveTimer);
        const button = $('#submitAttempt');
        button.disabled = true;
        try {
            await saveChain;
            const result = await api(`/quiz-attempts/${attempt.id}/submit`, {
                method: 'POST', body: JSON.stringify({ answers: Object.fromEntries(new FormData(form)) })
            });
            message(result.grading_status === 'pending'
                ? 'Attempt submitted. Your written responses are pending instructor grading.'
                : `Attempt submitted and automatically graded: ${result.grade} / ${questions.reduce((sum, question) => sum + question.points, 0)}.`);
            await openStudentQuiz(data.quiz.id);
        } catch (error) {
            button.disabled = false;
            message(error.message, 'error');
        }
    });
    $('[data-back-course]').onclick = () => openCourse(activeCourse);
    if (attempt.expires_at) {
        const expiresAt = new Date(attempt.expires_at).getTime();
        const updateTimer = () => {
            const timer = $('#attemptTimer');
            if (!timer) {
                window.clearInterval(window.quizTimer);
                return;
            }
            const remaining = Math.max(0, Math.floor((expiresAt - Date.now()) / 1000));
            timer.textContent = `${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, '0')}`;
            if (!remaining) {
                $('#submitAttempt').disabled = true;
                $('#attemptSaveStatus').textContent = 'Time expired. This attempt can no longer be submitted.';
                window.clearInterval(window.quizTimer);
            }
        };
        updateTimer();
        window.clearInterval(window.quizTimer);
        window.quizTimer = window.setInterval(updateTimer, 1000);
    }
}

async function renderGradebook() {
    setHeading(user.role === 'instructor' ? 'Gradebook' : 'My grades');
    if (user.role === 'student') {
        const { courses } = await api('/courses');
        content.innerHTML = `${pageHead('My grades', 'Feedback and results from your submitted quizzes.')}
            ${courses.map(course => `<section class="detail-card"><h3>${escapeHtml(course.title)}</h3><div data-gradebook="${course.id}"></div></section>`).join('') || empty('No courses', 'Your grades appear after enrolling in a course.')}`;
        for (const course of courses) {
            const { grades, totals } = await api(`/students/courses/${course.id}/gradebook`);
            const target = $(`[data-gradebook="${course.id}"]`);
            const total = totals[0];
            target.innerHTML = `<p class="muted small">Course total: ${total.earned_points} / ${total.available_points} points · ${total.completed_quizzes} of ${total.published_quizzes} quizzes completed${total.percentage === null ? '' : ` · ${total.percentage}%`}. Latest submitted attempt per quiz counts.</p>` +
                grades.filter(row => row.status === 'submitted' || row.status === 'in_progress').map(row =>
                `<div class="lecture-row"><div><b>${escapeHtml(row.quiz_title)} · Attempt ${row.attempt_number || 1}</b><small>${escapeHtml(row.feedback || (row.status === 'in_progress' ? 'Attempt in progress' : row.grading_status === 'pending' ? 'Pending manual grading' : 'No feedback yet'))}</small></div>
                <span>${row.status === 'in_progress' ? 'In progress' : row.grade === null ? 'Pending' : `${row.grade} / ${row.max_grade}`}</span></div>`).join('')
                || empty('No submissions yet', 'Submitted quizzes will appear here.');
        }
        return;
    }
    const { courses } = await api('/courses');
    content.innerHTML = `${pageHead('Gradebook', 'Review quiz submissions and post grades with feedback.')}
        <div class="field"><label>Select course</label><select id="gradeCourse">${courses.map(course => `<option value="${course.id}">${escapeHtml(course.title)}</option>`).join('')}</select></div>
        <div id="gradebookTable"></div>`;
    $('#gradeCourse').onchange = loadGradebook;
    await loadGradebook();
}

async function loadGradebook() {
    const courseId = $('#gradeCourse')?.value;
    if (!courseId) return;
    const [{ submissions }, gradebook] = await Promise.all([
        api(`/courses/${courseId}/submissions`), api(`/courses/${courseId}/gradebook`)
    ]);
    const totals = gradebook.totals || [];
    $('#gradebookTable').innerHTML = `<section class="detail-card"><h3>Course totals</h3><p class="muted small">Latest submitted attempt per published quiz counts; quizzes not yet submitted count as zero.</p>
        <div class="table-wrap"><table><thead><tr><th>Student</th><th>Earned points</th><th>Quiz points available</th><th>Completed quizzes</th><th>Percentage</th></tr></thead><tbody>
        ${totals.map(row => `<tr><td>${escapeHtml(row.student_name)}</td><td>${row.earned_points}</td><td>${row.available_points}</td>
        <td>${row.completed_quizzes} / ${row.published_quizzes}</td><td>${row.percentage === null ? '—' : `${row.percentage}%`}</td></tr>`).join('')}
        </tbody></table>${totals.length ? '' : empty('No enrolled students', 'Course totals appear for enrolled students.')}</div></section>
        ${submissions.length ? `<div class="table-wrap"><table><thead><tr><th>Student</th><th>Quiz attempt</th><th>Status</th><th>Answers</th><th>Grade</th><th>Feedback</th><th></th></tr></thead><tbody>
        ${submissions.map(submission => `<tr data-submission="${submission.id}"><td><b>${escapeHtml(submission.student_name)}</b><small>${escapeHtml(submission.student_email)}</small></td>
        <td>${escapeHtml(submission.quiz_title)}${submission.attempt_number ? `<small>Attempt ${submission.attempt_number}</small>` : ''}</td>
        <td>${escapeHtml(submission.status || 'not started')}${submission.submitted_at ? `<small>${dateText(submission.submitted_at)}</small>` : ''}</td>
        <td>${Object.entries(submission.answers || {}).map(([id, answer]) => {
            const question = submission.questions.find(item => item.id === id);
            return `<div><small>${escapeHtml(question?.prompt || 'Question')}</small><br>${escapeHtml(answer)} ${question?.kind === 'short_answer' || question?.kind === 'essay' ? '· Manual review' : submission.answer_key?.[id] === answer ? '✓' : '·'}</div>`;
        }).join('') || '—'}</td>
        <td>${submission.status === 'submitted' ? `<input class="grade-input" type="number" min="0" max="${submission.max_grade}" step="0.1" value="${submission.grade ?? ''}" placeholder="${submission.grading_status === 'pending' ? 'Pending' : 'Grade'}">${submission.grade_history.length ? `<small>History: ${submission.grade_history.map(item => `${item.grade} (${escapeHtml(item.source)})`).join(' → ')}</small>` : ''}` : '—'}</td>
        <td>${submission.status === 'submitted' ? `<input class="feedback-input" value="${escapeHtml(submission.feedback)}" placeholder="Feedback">` : '—'}</td>
        <td>${submission.status === 'submitted' ? '<button class="button small primary" data-grade>Save</button>' : '—'}</td></tr>`).join('')}
        </tbody></table></div>` : empty('No submissions yet', 'Student quiz submissions will appear here for grading.')}`;
    $('#gradebookTable').onclick = async event => {
        const button = event.target.closest('[data-grade]');
        if (!button) return;
        const row = button.closest('[data-submission]');
        try {
            await api(`/submissions/${row.dataset.submission}/grade`, { method: 'PUT', body: JSON.stringify({
                grade: Number($('.grade-input', row).value), feedback: $('.feedback-input', row).value
            }) });
            message('Grade and feedback saved.');
        } catch (error) { message(error.message, 'error'); }
    };
}

async function renderAnalytics() {
    setHeading('Analytics');
    const { courses } = await api('/courses');
    if (!courses.length) {
        content.innerHTML = `${pageHead('Learning analytics', 'Recorded course metrics from published learning activity.')}${empty('No courses yet', 'Create a course and enroll learners before reviewing analytics.')}`;
        return;
    }
    content.innerHTML = `${pageHead('Learning analytics', 'Only recorded enrollment, assessment, and playback telemetry is shown. Playback does not prove attention.')}
        <div class="field"><label>Select course</label><select id="analyticsCourse">${courses.map(course => `<option value="${course.id}">${escapeHtml(course.title)}</option>`).join('')}</select></div><div id="analytics"></div>`;
    $('#analyticsCourse').onchange = loadAnalytics;
    await loadAnalytics();
}

async function loadAnalytics() {
    const courseId = $('#analyticsCourse')?.value;
    if (!courseId) return;
    const data = await api(`/courses/${courseId}/analytics`);
    const engagement = data.lecture_engagement;
    const completion = data.course_completion;
    const analytics = $('#analytics');
    analytics.innerHTML = `<p class="muted small">${escapeHtml(data.completion_basis)}</p>
        <div class="stats-grid"><article class="stat-card"><div class="stat-top">Enrolled students</div><div class="stat-value">${data.students}</div></article>
        <article class="stat-card"><div class="stat-top">Published lectures</div><div class="stat-value">${data.published_lectures}</div></article>
        <article class="stat-card"><div class="stat-top">Quiz assessments</div><div class="stat-value">${data.quiz_performance.length}</div></article>
        <article class="stat-card"><div class="stat-top">${engagement.playback_available ? 'Approx. watch time' : 'Lecture accesses'}</div><div class="stat-value">${engagement.playback_available ? `${Math.round(engagement.approx_watch_seconds / 60)} min` : engagement.available ? engagement.access_count : '—'}</div><div class="stat-note">${engagement.available ? `${engagement.tracked_sessions} tracked sessions; playback is not proof of attention.` : escapeHtml(engagement.reason)}</div></article></div>
        <section class="detail-card"><h3>Course completion signals</h3><p class="muted small">Aggregate of submitted quizzes and tracked videos only; completion is recorded activity, not proof of learning. Students without activity are excluded from the average.</p>
        <div class="stats-grid"><article class="stat-card"><div class="stat-top">Average observed completion</div><div class="stat-value">${completion.average_completion_percent === null ? 'Insufficient data' : `${completion.average_completion_percent}%`}</div></article>
        <article class="stat-card"><div class="stat-top">Fully observed students</div><div class="stat-value">${completion.fully_observed_students} / ${completion.students_enrolled}</div></article>
        <article class="stat-card"><div class="stat-top">All trackable items completed</div><div class="stat-value">${completion.students_with_all_items_completed} / ${completion.students_enrolled}</div></article>
        <article class="stat-card"><div class="stat-top">Tracked item coverage</div><div class="stat-value">${completion.trackable_student_items === 0 ? 'No trackable items' : completion.coverage_percent === null ? 'No activity recorded' : `${completion.observed_student_items} / ${completion.trackable_student_items} (${completion.coverage_percent}%)`}</div></article></div></section>
        <section class="detail-card"><h3>Module completion coverage</h3><p class="muted small">Only published video lectures have completion telemetry. Resources and non-video lecture reading are not tracked.</p>
        <div class="table-wrap"><table><thead><tr><th>Module</th><th>Completed video items</th><th>Observed item coverage</th><th>Completion</th></tr></thead><tbody>${data.module_progress.map(row =>
            `<tr><td>${escapeHtml(row.module_title)}</td><td>${row.completed_student_items} / ${row.trackable_student_items}</td><td>${row.trackable_student_items === 0 ? 'No trackable videos' : row.observed_student_items === 0 ? 'No activity recorded' : `${row.observed_student_items} / ${row.trackable_student_items} (${row.coverage_percent}%)`}</td><td>${row.completion_percent === null ? 'Insufficient data' : `${row.completion_percent}% observed`}</td></tr>`).join('')}</tbody></table>
            ${data.module_progress.length ? '' : empty('No published modules', 'Published modules with video lectures will appear here.')}</div></section>
        ${engagement.playback_available ? `<section class="detail-card"><h3>Lecture playback telemetry</h3><p class="muted small">Intervals are deduplicated across sessions. Completion means at least 90% unique playback coverage; telemetry is approximate and does not establish attention.</p>
        <div class="table-wrap"><table><thead><tr><th>Lecture</th><th>Students tracked</th><th>Accesses</th><th>Approx. watch time</th><th>Unique media minutes</th><th>Completed</th></tr></thead><tbody>${engagement.lectures.filter(row => row.tracked_sessions).map(row =>
            `<tr><td>${escapeHtml(row.lecture)}</td><td>${row.tracked_students}</td><td>${row.access_count}</td><td>${Math.round(row.approx_watch_seconds / 60)}</td><td>${Math.round(row.unique_watched_seconds / 60)}</td><td>${row.completion_count}</td></tr>`).join('')}</tbody></table></div></section>` :
            engagement.available ? `<section class="detail-card"><h3>Lecture access</h3><p class="muted small">${escapeHtml(engagement.reason)}</p><div class="table-wrap"><table><thead><tr><th>Lecture</th><th>Students tracked</th><th>Accesses</th></tr></thead><tbody>${engagement.lectures.filter(row => row.tracked_sessions).map(row =>
                `<tr><td>${escapeHtml(row.lecture)}</td><td>${row.tracked_students}</td><td>${row.access_count}</td></tr>`).join('')}</tbody></table></div></section>` : ''}
        <section class="detail-card"><h3>Quiz performance</h3><p class="muted small">Uses each learner’s latest submitted attempt; pending manual grades are excluded from score distributions.</p><div class="table-wrap"><table><thead><tr><th>Quiz</th><th>Graded / pending</th><th>Average score</th><th>Score distribution</th></tr></thead><tbody>${data.quiz_performance.map(row =>
            `<tr><td>${escapeHtml(row.quiz)}</td><td>${row.graded_submissions} / ${row.pending_grading} pending</td><td>${row.average_percent === null ? 'No graded data' : `${row.average_percent}%`}</td><td>${row.grade_distribution.map(bin => `${escapeHtml(bin.label)}: ${bin.count}`).join(' · ')}</td></tr>
            ${row.question_performance.map(question => `<tr><td colspan="4"><div><b>${escapeHtml(question.prompt)}</b><small>Objective responses: ${question.responses} · ${question.sufficient_data ? `${question.correct_percent}% correct · ${escapeHtml(question.difficulty_indicator.replaceAll('_', ' '))}` : 'Insufficient data (fewer than 5 responses)'}</small></div></td></tr>`).join('')}`).join('')}</tbody></table>
            ${data.quiz_performance.length ? '' : empty('No published quizzes', 'Publish quizzes to begin recording assessment results.')}</div></section>
        <section class="detail-card"><h3>In-video questions</h3><p class="muted small">Response rates count submitted answers among enrolled learners. Correctness is shown only with at least 5 responses.</p><div class="table-wrap"><table><thead><tr><th>Lecture</th><th>Question</th><th>Responses</th><th>Response rate</th><th>Correctness</th></tr></thead><tbody>${data.checkpoint_performance.map(row =>
            `<tr><td>${escapeHtml(row.lecture)}</td><td>${escapeHtml(row.prompt)}</td><td>${row.responses} / ${row.enrolled_students}</td><td>${row.response_rate_percent === null ? 'No enrolled students' : `${row.response_rate_percent}%`}</td><td>${row.sufficient_data ? `${row.correct_percent}% correct` : 'Insufficient data'}</td></tr>`).join('')}</tbody></table>
            ${data.checkpoint_performance.length ? '' : empty('No in-video questions', 'Instructor-authored checkpoint responses will appear here.')}</div></section>
        <section class="detail-card"><h3>Student progress</h3><p class="muted small">Completion is based on submitted published quizzes and tracked video coverage. Unobserved activity is not assumed to be completed.</p><div class="table-wrap"><table><thead><tr><th>Student</th><th>Completed items</th><th>Observed coverage</th><th>Completion</th><th></th></tr></thead><tbody>${data.student_progress.map(row =>
            `<tr><td>${escapeHtml(row.name)}</td><td>${row.completed_items} / ${row.trackable_items}</td><td>${row.coverage_percent === null ? 'No activity recorded' : `${row.observed_items} / ${row.trackable_items} (${row.coverage_percent}%)`}</td><td>${row.completion_percent === null ? 'Insufficient data' : `${row.completion_percent}%`}</td><td><button class="button small" data-student-progress="${row.student_id}">View details</button></td></tr>`).join('')}</tbody></table>
            ${data.student_progress.length ? '' : empty('No students enrolled', 'Enroll students to see recorded learning progress.')}</div></section>`;
    analytics.onclick = async event => {
        const button = event.target.closest('[data-student-progress]');
        if (!button) return;
        try {
            const detail = await api(`/courses/${courseId}/analytics/students/${encodeURIComponent(button.dataset.studentProgress)}`);
            $('#studentAnalytics')?.remove();
            const panel = document.createElement('section');
            panel.className = 'detail-card';
            panel.id = 'studentAnalytics';
            panel.innerHTML = `<div class="panel-header"><h3>${escapeHtml(detail.student.name)} · Learning progress</h3><button class="button small" data-close-student>Close details</button></div>
                <p class="muted small">${escapeHtml(detail.warning)}</p>
                <div class="stats-grid"><article class="stat-card"><div class="stat-top">Completed items</div><div class="stat-value">${detail.completed_items} / ${detail.trackable_items}</div></article>
                <article class="stat-card"><div class="stat-top">Observed coverage</div><div class="stat-value">${detail.coverage_percent === null ? '—' : `${detail.coverage_percent}%`}</div></article>
                <article class="stat-card"><div class="stat-top">Observed completion</div><div class="stat-value">${detail.completion_percent === null ? '—' : `${detail.completion_percent}%`}</div></article></div>
                <h4>Published quizzes and video lectures</h4><div class="table-wrap"><table><thead><tr><th>Item</th><th>Type</th><th>Status</th><th>Progress / grade</th></tr></thead><tbody>${detail.items.map(item =>
                    `<tr><td>${escapeHtml(item.title)}</td><td>${escapeHtml(item.kind.replaceAll('_', ' '))}</td><td>${escapeHtml(item.status.replaceAll('_', ' '))}</td><td>${item.kind === 'quiz' ? item.grade === null ? escapeHtml(item.grading_status || 'No grade') : `${item.grade} points` : item.coverage_percent === null ? 'No interval data' : `${item.coverage_percent}% unique coverage`}</td></tr>`).join('')}</tbody></table>
                    ${detail.items.length ? '' : empty('No trackable items', 'Published quizzes and video lectures will be listed here.')}</div>
                <h4>Module video progress</h4><div class="table-wrap"><table><thead><tr><th>Module</th><th>Completed / trackable videos</th><th>Observed coverage</th></tr></thead><tbody>${detail.modules.map(module =>
                    `<tr><td>${escapeHtml(module.module_title)}</td><td>${module.completed_items} / ${module.trackable_items}</td><td>${module.trackable_items === 0 ? 'No trackable videos' : module.observed_items === 0 ? 'No activity recorded' : `${module.observed_items} / ${module.trackable_items} (${module.coverage_percent}%)`}</td></tr>`).join('')}</tbody></table>
                    ${detail.modules.length ? '' : empty('No published modules', 'Published modules will appear here.')}</div>
                <h4>In-video question responses</h4><div class="table-wrap"><table><thead><tr><th>Lecture</th><th>Question</th><th>Response</th><th>Correct</th></tr></thead><tbody>${detail.checkpoint_responses.map(item =>
                    `<tr><td>${escapeHtml(item.lecture)}</td><td>${escapeHtml(item.prompt)}</td><td>${escapeHtml(item.answer)}</td><td>${item.is_correct ? 'Yes' : 'No'}</td></tr>`).join('')}</tbody></table>
                    ${detail.checkpoint_responses.length ? '' : empty('No checkpoint responses', 'In-video question responses will appear here.')}</div>`;
            analytics.appendChild(panel);
            panel.querySelector('[data-close-student]').onclick = () => panel.remove();
        } catch (error) { message(error.message, 'error'); }
    };
}

async function editQuiz(quizId) {
    const { quiz, questions } = await api(`/quizzes/${quizId}`);
    quizQuestions = questions.map(({ id, answer, ...question }) => question);
    editingQuizId = quizId;
    suggestionsNeedReview = false;
    suggestionsReviewed = false;
    suggestionStatus = '';
    renderQuizForm(quiz.title, quiz.due_at ? new Date(quiz.due_at).toISOString().slice(0, 16) : '',
        quiz.time_limit_minutes || '', quiz.attempt_limit);
}

boot();
