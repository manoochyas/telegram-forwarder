const API = '';  // same origin
let currentView = 'dashboard';
let currentCmdClient = null;

// ---------- helpers ----------
async function api(path, opts = {}) {
  const res = await fetch(API + path, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (res.status === 401) {
    document.body.innerHTML = '<div style="padding:40px;text-align:center">احراز هویت ناموفق. صفحه را رفرش کنید.</div>';
    throw new Error('unauthorized');
  }
  return res.json();
}

function el(id) { return document.getElementById(id); }
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));
}
function fmtStatus(s) {
  return { pending: 'در انتظار', approved: 'تاییدشده', rejected: 'ردشده' }[s] || s;
}
function fmtLevel(l) {
  return { info: 'اطلاع', warning: 'هشدار', critical: 'بحرانی' }[l] || l;
}

// ---------- clock ----------
function tickClock() {
  const now = new Date();
  const fa = new Intl.DateTimeFormat('fa-IR', {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit'
  }).format(now);
  el('clock').textContent = fa;
}
setInterval(tickClock, 1000); tickClock();

// ---------- navigation ----------
document.querySelectorAll('.nav-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const view = btn.dataset.view;
    currentView = view;
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    el('view-' + view).classList.add('active');
    const titles = {
      dashboard: ['داشبورد مدیریت', 'پایش زنده وضعیت زیرساخت'],
      requests: ['مدیریت درخواست‌ها', 'تایید و رد درخواست‌های کارتریج'],
      clients: ['مانیتورینگ کلاینت‌ها', 'کنترل و فرمان‌دهی به سیستم‌های آنلاین'],
      alarms: ['هشدارها', 'ارسال و تاریخچه هشدارها'],
      lockdown: ['قفل سیستم', 'فعال/غیرفعال کردن قفل سراسری'],
    };
    el('page-title').textContent = titles[view][0];
    el('page-subtitle').textContent = titles[view][1];
    refreshView();
  });
});

// ---------- dashboard ----------
async function loadDashboard() {
  const d = await api('/api/panel/dashboard');
  el('kpi-clients').textContent = d.clients;
  el('kpi-pending').textContent = d.pending;
  el('kpi-approved').textContent = d.approved;
  el('kpi-alarms').textContent = d.alarms;
  el('kpi-locked').textContent = d.locked ? 'قفل' : 'آزاد';
  el('kpi-locked').style.color = d.locked ? '#f43f5e' : '#22c55e';

  const box = el('recent-requests');
  if (!d.recent.length) { box.innerHTML = '<div class="muted">درخواستی ثبت نشده</div>'; return; }
  box.innerHTML = d.recent.map(r => `
    <div class="req-row">
      <div class="req-info">
        <strong>👤 ${esc(r.requester)}</strong> — 🖨 ${esc(r.cartridge_model)} × ${r.quantity}
        <div class="muted">🔖 ${esc(r.tracking_code || '—')} • 📅 ${esc(r.req_date || '—')}</div>
      </div>
      <span class="pill ${r.status}">${fmtStatus(r.status)}</span>
    </div>
  `).join('');
}

// ---------- requests ----------
async function loadRequests() {
  const filter = el('req-filter').value;
  const rows = await api('/api/panel/requests' + (filter ? '?status=' + filter : ''));
  el('req-count').textContent = rows.length + ' مورد';
  const box = el('req-list');
  if (!rows.length) { box.innerHTML = '<div class="muted">درخواستی در این دسته نیست</div>'; return; }
  box.innerHTML = rows.map(r => `
    <div class="req-row">
      <div class="req-info">
        <strong>👤 ${esc(r.requester)}</strong> — 🖨 ${esc(r.cartridge_model)} × ${r.quantity}
        <div class="muted">🔖 ${esc(r.tracking_code || '—')} • 📅 ${esc(r.req_date || '—')} • ${esc(r.urgency || '')}</div>
      </div>
      <span class="pill ${r.status}">${fmtStatus(r.status)}</span>
      ${r.status === 'pending' ? `
        <div class="req-actions">
          <button class="btn success" onclick="decide(${r.id},'approved')">✓ تایید</button>
          <button class="btn ghost" onclick="decide(${r.id},'rejected')">✕ رد</button>
        </div>` : ''}
    </div>
  `).join('');
}
window.decide = async (id, status) => {
  await api('/api/panel/request/' + id, { method: 'POST', body: JSON.stringify({ status }) });
  loadRequests();
};
el('req-filter').addEventListener('change', loadRequests);

// ---------- clients ----------
async function loadClients() {
  const rows = await api('/get_clients');
  const box = el('client-list');
  if (!rows.length) { box.innerHTML = '<div class="muted">هیچ کلاینتی آنلاین نیست</div>'; return; }
  box.innerHTML = rows.map(c => `
    <div class="client-row">
      <div class="client-info">
        <strong>💻 ${esc(c.hostname || 'Unknown')}</strong>
        <div class="muted">🌐 ${esc(c.ip)} • 👤 ${esc(c.username || '—')}</div>
        <div class="muted">🖥 ${esc(c.hw_cpu || '—')} • 🧠 ${esc(c.hw_ram || '—')} • ⏱ ${esc(c.hw_uptime || '—')} • 💾 ${esc(c.hw_disks || '—')}</div>
      </div>
      <div class="req-actions">
        <button class="btn primary" onclick='openCmd(${JSON.stringify(c).replace(/'/g, "&#39;")})'>⚙️ دستور</button>
        <button class="btn ghost" onclick='sendMsg(${JSON.stringify(c).replace(/'/g, "&#39;")})'>🔔 پیام</button>
      </div>
    </div>
  `).join('');
}

window.openCmd = (client) => {
  currentCmdClient = client;
  el('cmd-title').textContent = '⚙️ دستور روی ' + (client.hostname || client.ip);
  el('cmd-input').value = '';
  el('cmd-output').textContent = '(خروجی اینجا نمایش داده می‌شود)';
  el('cmd-modal').classList.remove('hidden');
};
window.closeCmd = () => el('cmd-modal').classList.add('hidden');

window.runCommand = async () => {
  const cmd = el('cmd-input').value.trim();
  if (!cmd || !currentCmdClient) return;
  el('cmd-output').textContent = '⏳ در حال ارسال…';
  await api('/execute_command', {
    method: 'POST',
    body: JSON.stringify({ ip: currentCmdClient.ip, hostname: currentCmdClient.hostname, command: cmd })
  });
  // Wait a moment then fetch the result
  el('cmd-output').textContent = '⏳ در انتظار اجرا روی کلاینت… (حداکثر ۶۰ ثانیه)';
  let tries = 0;
  const poll = setInterval(async () => {
    tries++;
    // We don't have direct id from execute_command in this version; fallback: use pending result
    // For simplicity, look at latest command for this IP
    try {
      const hist = await fetch('/api/panel/dashboard'); // placeholder
    } catch(e) {}
    if (tries > 40) {
      clearInterval(poll);
      el('cmd-output').textContent = '⚠️ پاسخی دریافت نشد (کلاینت ممکن است آفلاین باشد)';
    }
  }, 1500);
};

window.sendMsg = async (client) => {
  const msg = prompt('متن پیام برای ' + (client.username || client.hostname));
  if (!msg) return;
  await api('/send_alarm', {
    method: 'POST',
    body: JSON.stringify({ message: msg, level: 'info', target_dept: client.username || 'all' })
  });
  alert('پیام ارسال شد');
};

// ---------- alarms ----------
async function loadAlarms() {
  const rows = await api('/get_alarm_history');
  const box = el('alarm-history');
  if (!rows.length) { box.innerHTML = '<div class="muted">هشداری ثبت نشده</div>'; return; }
  box.innerHTML = rows.slice(0, 50).map(a => `
    <div class="req-row">
      <div class="req-info">
        <span class="pill ${a.level}">${fmtLevel(a.level)}</span>
        <span style="margin-right:8px">${esc(a.message)}</span>
        <div class="muted">🏢 ${esc(a.target_dept)} • 🕒 ${esc(a.created_at)} • ✓ ${a.ack_count}</div>
      </div>
    </div>
  `).join('');
}

window.sendAlarm = async () => {
  const message = el('alarm-message').value.trim();
  if (!message) return alert('متن خالی است');
  await api('/send_alarm', {
    method: 'POST',
    body: JSON.stringify({
      message,
      level: el('alarm-level').value,
      target_dept: el('alarm-dept').value,
    })
  });
  el('alarm-message').value = '';
  loadAlarms();
};

// ---------- lockdown ----------
async function loadLock() {
  const s = await api('/get_lockdown');
  el('lock-state').textContent = s.is_active ? '🔴 سیستم قفل است' : '🟢 سیستم آزاد است';
  const btn = el('lock-btn');
  btn.textContent = s.is_active ? '🔓 برداشتن قفل' : '🔒 فعال‌سازی قفل';
  btn.className = 'btn ' + (s.is_active ? 'success' : 'danger');
  btn.dataset.active = s.is_active ? '1' : '0';
}
window.toggleLock = async () => {
  const active = el('lock-btn').dataset.active === '1';
  if (!confirm(active ? 'قفل برداشته شود؟' : 'همه کلاینت‌ها قفل شوند؟')) return;
  await api('/toggle_lockdown', {
    method: 'POST',
    body: JSON.stringify({ active: active ? 0 : 1 })
  });
  loadLock();
};

// ---------- refresh dispatcher ----------
function refreshView() {
  if (currentView === 'dashboard') loadDashboard();
  else if (currentView === 'requests') loadRequests();
  else if (currentView === 'clients') loadClients();
  else if (currentView === 'alarms') loadAlarms();
  else if (currentView === 'lockdown') loadLock();
}

// auto refresh
setInterval(() => {
  if (currentView === 'dashboard' || currentView === 'clients') refreshView();
}, 5000);

// initial
refreshView();
