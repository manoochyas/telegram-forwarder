/* ============================================================
   Luminiz Enterprise NOC — منطق پنل
   JavaScript خالص، بدون وابستگی خارجی
   ============================================================ */
(function () {
  'use strict';

  /* ---------- ابزارهای کمکی ---------- */
  const $  = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

  // جلوگیری از XSS: هر مقدار سروری قبل از innerHTML escape می‌شود
  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, c => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
  }

  const state = {
    view: 'dashboard',
    reqStatus: '',
    authHeader: null,     // مقدار هدر Authorization (اگر از فرم لاگین آمده باشد)
    timer: null,
    modal: { ip: '', hostname: '', mode: 'command' },
    lockActive: false
  };

  const VIEW_META = {
    dashboard: ['داشبورد مدیریت', 'پایش زنده وضعیت زیرساخت'],
    requests:  ['درخواست‌ها', 'مدیریت درخواست‌های کارتریج'],
    clients:   ['کلاینت‌ها', 'ماشین‌های آنلاین و کنترل از راه دور'],
    alarms:    ['هشدارها', 'ارسال و مشاهده تاریخچه هشدارها'],
    lockdown:  ['قفل سیستم', 'کنترل قفل سراسری کلاینت‌ها']
  };

  /* ---------- لایه شبکه ---------- */
  // همه درخواست‌ها same-origin هستند. اگر کاربر از فرم لاگین وارد شده باشد،
  // هدر Basic هم اضافه می‌شود؛ وگرنه مرورگر خودش احراز هویت را انجام می‌دهد.
  async function api(url, opts = {}) {
    const headers = Object.assign({}, opts.headers || {});
    if (state.authHeader) headers['Authorization'] = state.authHeader;
    if (opts.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';

    const res = await fetch(url, {
      method: opts.method || 'GET',
      headers,
      credentials: 'same-origin',
      body: opts.body ? JSON.stringify(opts.body) : undefined,
      cache: 'no-store'
    });

    if (res.status === 401) {
      doLogout(true);
      throw new Error('نشست منقضی شده است');
    }
    if (!res.ok) throw new Error('خطای سرور (' + res.status + ')');

    const text = await res.text();
    try { return JSON.parse(text); } catch (_) { return text; }
  }

  function toast(msg, type) {
    const t = $('#toast');
    t.textContent = msg;
    t.className = 'toast ' + (type || 'ok');
    clearTimeout(toast._t);
    toast._t = setTimeout(() => t.classList.add('hidden'), 3000);
  }

  /* ============================================================
     لاگین
     ============================================================ */
  // base64 با پشتیبانی از یونیکد (رمزهای غیر ASCII)
  function basicHeader(user, pass) {
    const bytes = new TextEncoder().encode(user + ':' + pass);
    let bin = '';
    bytes.forEach(b => bin += String.fromCharCode(b));
    return 'Basic ' + btoa(bin);
  }

  function showApp() {
    $('#login-screen').classList.add('hidden');
    $('#app').classList.remove('hidden');
    switchView(state.view);
    startRefresh();
  }

  function showLogin(msg) {
    stopRefresh();
    $('#app').classList.add('hidden');
    $('#login-screen').classList.remove('hidden');
    const err = $('#login-error');
    if (msg) { err.textContent = msg; err.classList.remove('hidden'); }
    else err.classList.add('hidden');
  }

  // تست یک route محافظت‌شده
  async function probe(header) {
    const headers = header ? { Authorization: header } : {};
    const res = await fetch('/api/panel/dashboard', {
      headers, credentials: 'same-origin', cache: 'no-store'
    });
    return res.ok;
  }

  async function onLogin(e) {
    e.preventDefault();
    const user = $('#login-user').value.trim();
    const pass = $('#login-pass').value;
    const btn = $('#login-btn');
    btn.disabled = true; btn.textContent = 'در حال بررسی…';
    try {
      const header = basicHeader(user, pass);
      if (await probe(header)) {
        state.authHeader = header;
        try { sessionStorage.setItem('noc_auth', header); } catch (_) {}
        $('#login-pass').value = '';
        showApp();
      } else {
        showLogin('نام کاربری یا رمز عبور اشتباه است');
      }
    } catch (_) {
      showLogin('ارتباط با سرور برقرار نشد');
    } finally {
      btn.disabled = false; btn.textContent = 'ورود به پنل';
    }
  }

  function doLogout(expired) {
    state.authHeader = null;
    try { sessionStorage.removeItem('noc_auth'); } catch (_) {}
    showLogin(expired ? 'نشست شما پایان یافت، دوباره وارد شوید' : '');
  }

  // هنگام بارگذاری: اگر مرورگر قبلاً احراز شده باشد یا هدر ذخیره باشد، مستقیم وارد پنل می‌شویم
  async function init() {
    startClock();
    bindEvents();
    let saved = null;
    try { saved = sessionStorage.getItem('noc_auth'); } catch (_) {}
    try {
      if (saved && await probe(saved)) { state.authHeader = saved; return showApp(); }
      if (await probe(null)) return showApp();
    } catch (_) {}
    showLogin();
  }

  /* ============================================================
     ناوبری و رفرش خودکار
     ============================================================ */
  function switchView(name) {
    state.view = name;
    $$('.view').forEach(v => v.classList.toggle('active', v.id === 'view-' + name));
    $$('.nav-btn[data-view]').forEach(b => b.classList.toggle('active', b.dataset.view === name));
    $('#page-title').textContent = VIEW_META[name][0];
    $('#page-subtitle').textContent = VIEW_META[name][1];
    refresh();
  }

  async function refresh() {
    try {
      switch (state.view) {
        case 'dashboard': await loadDashboard(); break;
        case 'requests':  await loadRequests();  break;
        case 'clients':   await loadClients();   break;
        case 'alarms':    await loadAlarms();    break;
        case 'lockdown':  await loadLockdown();  break;
      }
    } catch (err) {
      console.warn(err);
    }
  }

  function startRefresh() { stopRefresh(); state.timer = setInterval(refresh, 5000); }
  function stopRefresh()  { clearInterval(state.timer); state.timer = null; }

  /* ============================================================
     ساعت شمسی زنده
     ============================================================ */
  function startClock() {
    const fmt = new Intl.DateTimeFormat('fa-IR-u-ca-persian', {
      weekday: 'long', year: 'numeric', month: 'long', day: 'numeric',
      hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
      timeZone: 'Asia/Tehran'
    });
    const tick = () => { $('#clock').textContent = fmt.format(new Date()); };
    tick();
    setInterval(tick, 1000);
  }

  // تبدیل تاریخ سرور به متن شمسی (اگر قابل تبدیل نبود، همان متن اصلی)
  function fmtDate(v) {
    if (!v) return '—';
    const d = new Date(String(v).replace(' ', 'T'));
    if (isNaN(d)) return String(v);
    return new Intl.DateTimeFormat('fa-IR-u-ca-persian', {
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', hour12: false
    }).format(d);
  }

  /* ============================================================
     داشبورد
     ============================================================ */
  async function loadDashboard() {
    const d = await api('/api/panel/dashboard');
    $('#kpi-clients').textContent  = d.clients  ?? 0;
    $('#kpi-pending').textContent  = d.pending  ?? 0;
    $('#kpi-approved').textContent = d.approved ?? 0;
    $('#kpi-alarms').textContent   = d.alarms   ?? 0;
    const lk = $('#kpi-locked');
    lk.textContent = d.locked ? 'قفل' : 'آزاد';
    lk.style.color = d.locked ? 'var(--red)' : 'var(--green)';

    const recent = (d.recent || []).slice(0, 8);
    $('#recent-requests').innerHTML = recent.length ? recent.map(r => `
      <div class="row-item">
        <div>
          <strong>${esc(r.requester)}</strong>
          <div class="meta">${esc(r.cartridge_model)} × ${esc(r.quantity)} · ${esc(fmtDate(r.req_date))}</div>
        </div>
        ${statusBadge(r.status)}
      </div>`).join('') : '<div class="empty">درخواستی ثبت نشده است</div>';
  }

  /* ============================================================
     درخواست‌ها
     ============================================================ */
  const STATUS_FA = { pending: 'در انتظار', approved: 'تاییدشده', rejected: 'ردشده' };
  function statusBadge(s) {
    return `<span class="badge ${esc(s)}">${esc(STATUS_FA[s] || s)}</span>`;
  }

  async function loadRequests() {
    const q = state.reqStatus ? '?status=' + encodeURIComponent(state.reqStatus) : '';
    const list = await api('/api/panel/requests' + q);
    $('#req-count').textContent = list.length + ' مورد';
    $('#req-list').innerHTML = list.length ? list.map(r => `
      <div class="item-card">
        <div class="item-head"><strong>${esc(r.requester)}</strong>${statusBadge(r.status)}</div>
        <div class="kv"><span>مدل کارتریج</span><span>${esc(r.cartridge_model)}</span></div>
        <div class="kv"><span>تعداد</span><span>${esc(r.quantity)}</span></div>
        <div class="kv"><span>کد پیگیری</span><span class="mono">${esc(r.tracking_code)}</span></div>
        <div class="kv"><span>تاریخ</span><span>${esc(fmtDate(r.req_date))}</span></div>
        ${r.status === 'pending' ? `
        <div class="item-actions">
          <button class="btn success small" data-act="approve" data-id="${esc(r.id)}">✓ تایید</button>
          <button class="btn ghost small"   data-act="reject"  data-id="${esc(r.id)}">✕ رد</button>
        </div>` : ''}
      </div>`).join('') : '<div class="empty">موردی یافت نشد</div>';
  }

  async function setRequestStatus(id, status) {
    try {
      await api('/api/panel/request/' + encodeURIComponent(id), { method: 'POST', body: { status } });
      toast(status === 'approved' ? 'درخواست تایید شد' : 'درخواست رد شد');
      loadRequests();
    } catch (e) { toast(e.message, 'err'); }
  }

  /* ============================================================
     کلاینت‌ها
     ============================================================ */
  // تبدیل مقادیر نامشخص (رشته/عدد/آرایه) به متن قابل نمایش
  function fmtUptime(v) {
    if (typeof v === 'number') {
      const d = Math.floor(v / 86400), h = Math.floor(v % 86400 / 3600), m = Math.floor(v % 3600 / 60);
      return (d ? d + ' روز ' : '') + h + ' ساعت ' + m + ' دقیقه';
    }
    return v || '—';
  }
  function fmtDisks(v) {
    if (Array.isArray(v)) return v.map(x => typeof x === 'object' ? Object.values(x).join(' ') : x).join(' | ');
    if (v && typeof v === 'object') return Object.entries(v).map(([k, x]) => k + ': ' + x).join(' | ');
    return v || '—';
  }

  async function loadClients() {
    const list = await api('/get_clients');
    $('#client-list').innerHTML = list.length ? list.map(c => `
      <div class="item-card">
        <div class="item-head"><strong class="mono">${esc(c.hostname)}</strong><span class="badge approved">آنلاین</span></div>
        <div class="kv"><span>IP</span><span class="mono">${esc(c.ip)}</span></div>
        <div class="kv"><span>کاربر</span><span>${esc(c.username)}</span></div>
        <div class="kv"><span>CPU</span><span class="mono">${esc(c.hw_cpu || '—')}</span></div>
        <div class="kv"><span>RAM</span><span class="mono">${esc(c.hw_ram || '—')}</span></div>
        <div class="kv"><span>Uptime</span><span>${esc(fmtUptime(c.hw_uptime))}</span></div>
        <div class="kv"><span>دیسک</span><span class="mono">${esc(fmtDisks(c.hw_disks))}</span></div>
        <div class="item-actions">
          <button class="btn primary small" data-act="cmd" data-ip="${esc(c.ip)}" data-host="${esc(c.hostname)}">⚙️ دستور</button>
          <button class="btn small" data-act="msg" data-ip="${esc(c.ip)}" data-host="${esc(c.hostname)}">🔔 پیام</button>
        </div>
      </div>`).join('') : '<div class="empty">هیچ کلاینت آنلاینی وجود ندارد</div>';
  }

  /* ---------- مودال دستور / پیام ---------- */
  function openModal(mode, ip, hostname) {
    state.modal = { ip, hostname, mode };
    $('#modal-title').textContent = mode === 'cmd' ? '⚙️ اجرای دستور PowerShell' : '🔔 ارسال پیام شخصی';
    $('#modal-target').textContent = hostname + ' (' + ip + ')';
    const input = $('#modal-input');
    input.value = '';
    input.placeholder = mode === 'cmd' ? 'Get-Process | Select -First 5' : 'متن پیام برای کاربر…';
    input.classList.toggle('code', mode === 'cmd');
    $('#modal-output').textContent = '';
    $('#modal').classList.remove('hidden');
    input.focus();
  }
  function closeModal() { $('#modal').classList.add('hidden'); }

  async function runModal() {
    const { ip, hostname, mode } = state.modal;
    let command = $('#modal-input').value.trim();
    if (!command) return;

    // پیام شخصی از طریق دستور msg ویندوز ارسال می‌شود
    if (mode === 'msg') {
      const safe = command.replace(/'/g, "''");
      command = "msg * /TIME:0 '" + safe + "'";
    }

    const out = $('#modal-output');
    const btn = $('#modal-run');
    out.textContent = 'در حال اجرا…';
    btn.disabled = true;
    try {
      const res = await api('/execute_command', { method: 'POST', body: { ip, hostname, command } });
      out.textContent = typeof res === 'string' ? res : JSON.stringify(res, null, 2);
    } catch (e) {
      out.textContent = 'خطا: ' + e.message;
    } finally { btn.disabled = false; }
  }

  /* ============================================================
     هشدارها
     ============================================================ */
  const LEVEL_FA = { info: 'اطلاع', warning: 'هشدار', critical: 'بحرانی' };

  async function loadAlarms() {
    const list = await api('/get_alarm_history');
    $('#alarm-history').innerHTML = list.length ? list.map(a => `
      <div class="row-item">
        <div>
          <div>${esc(a.message)}</div>
          <div class="meta">واحد: ${esc(a.target_dept || 'همه')} · ${esc(fmtDate(a.created_at))} · تایید: ${esc(a.ack_count ?? 0)}</div>
        </div>
        <span class="badge ${esc(a.level)}">${esc(LEVEL_FA[a.level] || a.level)}</span>
      </div>`).join('') : '<div class="empty">هشداری ثبت نشده است</div>';
  }

  async function sendAlarm() {
    const message = $('#alarm-message').value.trim();
    if (!message) return toast('متن هشدار را وارد کنید', 'err');
    try {
      await api('/send_alarm', {
        method: 'POST',
        body: { message, level: $('#alarm-level').value, target_dept: $('#alarm-dept').value }
      });
      $('#alarm-message').value = '';
      toast('هشدار ارسال شد');
      loadAlarms();
    } catch (e) { toast(e.message, 'err'); }
  }

  /* ============================================================
     قفل سیستم
     ============================================================ */
  async function loadLockdown() {
    const d = await api('/get_lockdown');
    state.lockActive = !!d.is_active;
    $('#lock-icon').textContent = state.lockActive ? '🔒' : '🔓';
    const st = $('#lock-state');
    st.textContent = state.lockActive ? 'سیستم قفل است' : 'سیستم آزاد است';
    st.style.color = state.lockActive ? 'var(--red)' : 'var(--green)';
    const btn = $('#lock-btn');
    btn.textContent = state.lockActive ? '🔓 آزاد کردن سیستم' : '🔒 قفل کردن سیستم';
    btn.className = 'btn big ' + (state.lockActive ? 'success' : 'danger');
  }

  async function toggleLock() {
    const next = state.lockActive ? 0 : 1;
    if (next && !confirm('کلاینت‌ها قفل می‌شوند. ادامه می‌دهید؟')) return;
    try {
      await api('/toggle_lockdown', { method: 'POST', body: { active: next } });
      toast(next ? 'سیستم قفل شد' : 'سیستم آزاد شد');
      loadLockdown();
    } catch (e) { toast(e.message, 'err'); }
  }

  /* ============================================================
     اتصال رویدادها
     ============================================================ */
  function bindEvents() {
    $('#login-form').addEventListener('submit', onLogin);
    $('#logout-btn').addEventListener('click', () => doLogout(false));

    $$('.nav-btn[data-view]').forEach(b =>
      b.addEventListener('click', () => switchView(b.dataset.view)));

    // فیلتر درخواست‌ها
    $('#req-filters').addEventListener('click', e => {
      const chip = e.target.closest('.chip'); if (!chip) return;
      $$('#req-filters .chip').forEach(c => c.classList.toggle('active', c === chip));
      state.reqStatus = chip.dataset.status;
      loadRequests();
    });

    // دکمه‌های تایید/رد (event delegation)
    $('#req-list').addEventListener('click', e => {
      const b = e.target.closest('[data-act]'); if (!b) return;
      setRequestStatus(b.dataset.id, b.dataset.act === 'approve' ? 'approved' : 'rejected');
    });

    // دکمه‌های دستور/پیام کلاینت
    $('#client-list').addEventListener('click', e => {
      const b = e.target.closest('[data-act]'); if (!b) return;
      openModal(b.dataset.act, b.dataset.ip, b.dataset.host);
    });

    $('#alarm-send').addEventListener('click', sendAlarm);
    $('#lock-btn').addEventListener('click', toggleLock);
    $('#modal-run').addEventListener('click', runModal);
    $('#modal-close').addEventListener('click', closeModal);
    $('#modal').addEventListener('click', e => { if (e.target.id === 'modal') closeModal(); });
    document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });
  }

  document.addEventListener('DOMContentLoaded', init);
})();
