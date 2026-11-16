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
    <div class="req
