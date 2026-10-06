'use strict';
const form = document.getElementById('photo-login-form');
form.onsubmit = async event => {
  event.preventDefault();
  const button = form.querySelector('button'), notice = document.getElementById('notice');
  button.disabled = true;
  try {
    const response = await fetch('/api/photo-access/login', {method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Slideshow-Token': document.querySelector('meta[name="slideshow-token"]').content},
      body: JSON.stringify({password: document.getElementById('photo-password').value})});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Sign-in failed.');
    document.getElementById('photo-password').value = '';
    location.replace(location.pathname === '/files' ? '/files' : '/');
  } catch (error) { notice.textContent = error.message; notice.classList.add('error'); }
  finally { button.disabled = false; }
};
