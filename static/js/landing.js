const dialog = document.querySelector('#authDialog');
const form = document.querySelector('#authForm');
const roles = [...document.querySelectorAll('[data-role-option]')];
let mode = 'signup';
let role = 'student';
const inviteToken = new URLSearchParams(window.location.search).get('join');

function setRole(value) {
    role = value;
    roles.forEach((option) => option.classList.toggle('selected', option.dataset.roleOption === role));
}

function setMode(value) {
    mode = value;
    const signup = mode === 'signup';
    document.querySelector('#authTitle').textContent = signup ? 'Create your account' : 'Welcome back';
    document.querySelector('#authDescription').textContent = signup
        ? 'Choose your role to get started.' : `Log in to your ${role} space.`;
    document.querySelector('#nameField').hidden = !signup;
    document.querySelector('[name="name"]').required = signup;
    document.querySelector('[name="password"]').autocomplete = signup ? 'new-password' : 'current-password';
    document.querySelector('[name="password"]').minLength = signup ? 12 : 1;
    document.querySelector('#authSubmit').innerHTML = signup ? 'Create account <span>→</span>' : 'Log in <span>→</span>';
    document.querySelector('#switchText').textContent = signup ? 'Already have an account?' : 'New to LectureSeek?';
    document.querySelector('[data-toggle-auth]').textContent = signup ? 'Log in' : 'Create an account';
    document.querySelector('.auth-hint').hidden = !signup;
    document.querySelector('#authError').textContent = '';
}

document.querySelectorAll('[data-open-auth]').forEach((button) => {
    button.addEventListener('click', () => {
        if (button.dataset.role) setRole(button.dataset.role);
        setMode(button.dataset.openAuth);
        dialog.showModal();
    });
});
document.querySelectorAll('[data-role-option]').forEach((button) =>
    button.addEventListener('click', () => {
        setRole(button.dataset.roleOption);
        if (mode === 'login') setMode('login');
    }));
document.querySelectorAll('[data-close-auth]').forEach((button) =>
    button.addEventListener('click', () => dialog.close()));
document.querySelector('[data-toggle-auth]').addEventListener('click', () =>
    setMode(mode === 'signup' ? 'login' : 'signup'));

if (inviteToken) {
    setRole('student');
    setMode('signup');
    document.querySelector('#authDescription').textContent = 'Create or log in to a student account to join this private course.';
    document.querySelector('#joinNotice').textContent = 'You have a private course invitation. Sign in or create a student account to enroll.';
    document.querySelector('#joinNotice').hidden = false;
    dialog.showModal();
}

async function api(path, body) {
    const response = await fetch(path, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || 'Unable to complete your request.');
    return data;
}

form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = document.querySelector('#authSubmit');
    const errorElement = document.querySelector('#authError');
    const values = Object.fromEntries(new FormData(form));
    values.role = role;
    button.disabled = true;
    errorElement.textContent = '';
    try {
        const result = await api(mode === 'signup' ? '/api/lms/auth/register' : '/api/lms/auth/login', values);
        if (result.user.role !== role) {
            await api('/api/lms/auth/logout', {});
            throw new Error(`This account is registered as a ${result.user.role}. Choose that role to log in.`);
        }
        if (inviteToken) {
            const enrollment = await api(`/api/lms/enrollments/by-link/${encodeURIComponent(inviteToken)}`, {});
            document.querySelector('#joinNotice').textContent = `Enrolled in ${enrollment.course_title}. Opening your student dashboard…`;
        }
        window.location.assign(result.user.role === 'instructor' ? '/instructor' : '/student');
    } catch (error) {
        errorElement.textContent = error.message;
    } finally {
        button.disabled = false;
    }
});

fetch('/api/lms/session', { credentials: 'same-origin' })
    .then((response) => response.json())
    .then(({ user }) => {
        if (user) {
            const dashboard = document.querySelector('#goDashboard');
            dashboard.href = user.role === 'instructor' ? '/instructor' : '/student';
            dashboard.hidden = false;
            document.querySelector('[data-open-auth="login"]').hidden = true;
            document.querySelector('[data-open-auth="signup"]').hidden = true;
            if (inviteToken) {
                setMode('login');
                document.querySelector('#authDescription').textContent = 'Log in to your student account to join this private course.';
                dialog.showModal();
            }
        }
    })
    .catch((error) => console.error('Could not check the existing session:', error));
