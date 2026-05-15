// Triage list: persist session tag in localStorage.
const sessionInput = document.getElementById('session');
if (sessionInput) {
  sessionInput.value = localStorage.getItem('triage_session') || '';
  sessionInput.addEventListener('change', () => {
    localStorage.setItem('triage_session', sessionInput.value);
  });
}
