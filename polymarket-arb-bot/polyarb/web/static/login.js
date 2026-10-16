(() => {
  'use strict';
  const form = document.getElementById('login-form');
  const input = document.getElementById('password');
  const button = document.getElementById('login-btn');
  const err = document.getElementById('login-err');
  const card = document.getElementById('login-card');

  const fail = msg => {
    err.textContent = msg;
    card.classList.remove('shake');
    void card.offsetWidth;
    card.classList.add('shake');
    input.select();
  };

  form.addEventListener('submit', async e => {
    e.preventDefault();
    if (!input.value) return;
    button.disabled = true;
    err.textContent = '';
    try {
      const r = await fetch('/api/login', {
        method: 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password: input.value }),
      });
      if (r.ok) { location.replace('/'); return; }
      fail(r.status === 429 ? '嘗試次數過多，請 10 分鐘後再試' : r.status === 401 ? '密碼不正確' : `登入失敗（HTTP ${r.status}）`);
    } catch (_) {
      fail('無法連接伺服器');
    } finally {
      button.disabled = false;
    }
  });
})();
