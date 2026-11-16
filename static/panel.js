
/* ======================================================
   LUMINIZ NOC 2026 — PANEL CONTROLLER
   Session Authentication / Modern Dashboard
   ====================================================== */

(() => {
  "use strict";

  const $ = (s, root = document) =>
    root.querySelector(s);

  const $$ = (s, root = document) =>
    Array.from(root.querySelectorAll(s));

  const escapeHTML = value =>
    String(value ?? "").replace(/[&<>"']/g, c => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;"
    })[c]);

  const state = {
    view: "dashboard",
    requestStatus: "",
    timer: null,
    busy: false,
    locked: false,
    modal: null,
    commandTimer: null,
    clients: [],
    diskWarnings: new Set(),
    stopped: false
  };

  const views = {
    dashboard: ["داشبورد مدیریت", "نمای زنده زیرساخت سازمان"],
    requests: ["درخواست‌ها", "مدیریت خرید کارتریج"],
    clients: ["کلاینت‌ها", "مشخصات و سلامت دستگاه‌ها"],
    alarms: ["مرکز هشدارها", "ارسال و بررسی اعلان‌ها"],
    lockdown: ["قفل سیستم", "مدیریت وضعیت قفل"]
  };

  const statusNames = {
    pending: "در انتظار",
    approved: "تأییدشده",
    rejected: "ردشده"
  };

  const alarmNames = {
    info: "اطلاع",
    warning: "هشدار",
    critical: "بحرانی"
  };

  const setText = (selector, value) => {
    const element = $(selector);
    if (element) element.textContent = value;
  };

  function toast(message, type = "ok") {
    const el = $("#toast");
    if (!el) return;

    el.textContent = message;
    el.className = `toast ${type}`;

    clearTimeout(toast.timer);

    toast.timer = setTimeout(() => {
      el.classList.add("hidden");
    }, 4500);
  }

  // -------- API --------

  async function api(path, options = {}) {
    const headers = {
      Accept: "application/json",
      ...(options.headers || {})
    };

    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
    }

    const response = await fetch(path, {
      method: options.method || "GET",
      headers,
      credentials: "same-origin",
      cache: "no-store",
      redirect: "follow",
      body: options.body === undefined
        ? undefined
        : JSON.stringify(options.body)
    });

    if (
      response.status === 401 ||
      response.status === 403
    ) {
      if (response.status === 401) {
        location.assign("/login");
      }
      throw new Error("دسترسی مجاز نیست");
    }

    if (
      response.redirected &&
      new URL(response.url).pathname === "/login"
    ) {
      location.assign("/login");
      throw new Error("نشست منقضی شده است");
    }

    if (!response.ok) {
      throw new Error(`خطای سرور: ${response.status}`);
    }

    const contentType =
      response.headers.get("content-type") || "";

    if (!contentType.includes("application/json")) {
      throw new Error("پاسخ غیرمنتظره از سرور");
    }

    return response.json();
  }

  // -------- NAVIGATION --------

  function switchView(name) {
    if (!views[name]) return;

    state.view = name;

    $$(".view").forEach(el => {
      el.classList.toggle(
        "active",
        el.id === `view-${name}`
      );
    });

    $$(".nav-btn[data-view]").forEach(btn => {
      const active = btn.dataset.view === name;
      btn.classList.toggle("active", active);

      if (active) {
        btn.setAttribute("aria-current", "page");
      } else {
        btn.removeAttribute("aria-current");
      }
    });

    setText("#page-title", views[name][0]);
    setText("#page-subtitle", views[name][1]);

    refresh();
  }

  // -------- DATE --------

  const clockFormatter = new Intl.DateTimeFormat(
    "fa-IR-u-ca-persian",
    {
      timeZone: "Asia/Tehran",
      year: "numeric",
      month: "long",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false
    }
  );

  function startClock() {
    const update = () =>
      setText("#clock", clockFormatter.format(new Date()));

    update();
    setInterval(update, 1000);
  }

  function formatDate(value) {
    if (!value) return "—";

    const raw = String(value);

    // Server already returns Jalali dates.
    if (/^\d{4}\/\d{1,2}\/\d{1,2}/.test(raw)) {
      return raw;
    }

    const date = new Date(raw);

    if (Number.isNaN(date.getTime())) {
      return raw;
    }

    return new Intl.DateTimeFormat(
      "fa-IR-u-ca-persian",
      {
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit"
      }
    ).format(date);
  }

  // -------- HELPERS --------

  function badge(status) {
    const allowed = [
      "pending", "approved", "rejected",
      "info", "warning", "critical"
    ];

    const safeClass = allowed.includes(status)
      ? status
      : "info";

    const title =
      statusNames[status] ||
      alarmNames[status] ||
      status ||
      "نامشخص";

    return `<span class="badge ${safeClass}">
      ${escapeHTML(title)}
    </span>`;
  }

  function formatUptime(seconds) {
    if (typeof seconds !== "number") {
      return seconds || "—";
    }

    const days = Math.floor(seconds / 86400);
    const hours = Math.floor(seconds % 86400 / 3600);
    const minutes = Math.floor(seconds % 3600 / 60);

    return `${days} روز، ${hours} ساعت، ${minutes} دقیقه`;
  }

  function formatBytes(bytes) {
    if (!Number.isFinite(bytes) || bytes < 0) return "—";

    const units = ["B", "GB", "TB"];
    if (bytes < 1024) return `${bytes.toFixed(0)} B`;
    if (bytes < 1024 ** 3) {
      return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
    }
    if (bytes < 1024 ** 4) {
      return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
    }
    return `${(bytes / 1024 ** 4).toFixed(2)} TB`;
  }

  function diskPercent(disk) {
    if (!disk || typeof disk !== "object") return null;

    let pct = disk.percent ??
      disk.used_percent ??
      disk.usage_percent ??
      disk.percent_used;

    if (pct !== undefined && pct !== null && pct !== "") {
      pct = Number(String(pct).replace("%", ""));
      if (Number.isFinite(pct) && pct >= 0 && pct <= 100) {
        return pct;
      }
    }

    // Total and used must be in the same units.
    const total = Number(disk.total_bytes);
    const used = Number(disk.used_bytes);

    if (Number.isFinite(total) && total > 0 &&
        Number.isFinite(used) && used >= 0) {
      return Math.min(100, Math.max(0, used / total * 100));
    }

    return null;
  }

  function normalizeDisks(input) {
    if (!input) return [];

    if (Array.isArray(input)) {
      return input.map((item, index) => {
        if (typeof item === "object" && item !== null) {
          return {
            name: item.name || item.drive ||
              item.mountpoint || `Disk ${index + 1}`,
            data: item
          };
        }

        return {
          name: `Disk ${index + 1}`,
          data: { description: String(item) }
        };
      });
    }

    if (typeof input === "object") {
      return Object.entries(input).map(([name, data]) => ({
        name,
        data: typeof data === "object" && data !== null
          ? data
          : { description: String(data) }
      }));
    }

    return [{ name: "دیسک", data: { description: String(input) } }];
  }

  function renderDisks(client) {
    const disks = normalizeDisks(client.hw_disks);

    if (!disks.length) {
      return `<div class="kv">
        <span>درایوها</span>
        <span>اطلاعات دریافت نشده</span>
      </div>`;
    }

    return disks.map(disk => {
      const pct = diskPercent(disk.data);
      const level = pct === null ? "unknown" :
        pct >= 95 ? "critical" :
        pct >= 85 ? "warning" : "healthy";

      const color = level === "critical"
        ? "var(--red)"
        : level === "warning"
        ? "var(--yellow)"
        : "var(--green)";

      if (level === "critical" || level === "warning") {
        state.diskWarnings.add(
          `${client.hostname || client.ip} / ${disk.name}`
        );
      }

      const details = disk.data.description ||
        ((Number.isFinite(Number(disk.data.total_bytes)) &&
          Number.isFinite(Number(disk.data.used_bytes)))
          ? `${formatBytes(Number(disk.data.used_bytes))} از ${
              formatBytes(Number(disk.data.total_bytes))
            }`
          : "");

      return `
        <div class="noc-disk">
          <div class="noc-disk-head">
            <strong>${escapeHTML(disk.name)}</strong>
            <span style="color:${color}">
              ${pct === null ? "نامشخص" : pct.toFixed(1) + "%"}
            </span>
          </div>

          ${pct === null ? "" : `
            <div class="noc-disk-track">
              <div class="noc-disk-fill"
                   style="width:${pct}%;background:${color}">
              </div>
            </div>
          `}

          <small>${escapeHTML(details)}</small>

          ${level === "critical" ? `
            <div class="noc-disk-alert">
              فضای دیسک به وضعیت بحرانی رسیده است.
            </div>
          ` : level === "warning" ? `
            <div class="noc-disk-alert">
              فضای دیسک رو به اتمام است.
            </div>
          ` : ""}
        </div>
      `;
    }).join("");
  }

  function renderPrinters(client) {
    const printers = client.printers || client.hw_printers;

    if (!Array.isArray(printers)) {
      return `<div class="noc-printers">
        <strong>چاپگرها</strong>
        <p>داده چاپگر از کلاینت ارسال نشده است.</p>
      </div>`;
    }

    if (!printers.length) {
      return `<div class="noc-printers">
        <strong>چاپگرها</strong>
        <p>چاپگری گزارش نشده است.</p>
      </div>`;
    }

    return `<div class="noc-printers">
      <strong>چاپگرهای نصب‌شده</strong>
      ${printers.map(printer => {
        const p = typeof printer === "string"
          ? { name: printer }
          : printer || {};

        return `<div class="noc-printer-item">
          <span>🖨 ${escapeHTML(p.name || "Printer")}</span>
          <small>${escapeHTML(p.status || "وضعیت نامشخص")}</small>
        </div>`;
      }).join("")}
    </div>`;
  }

  // -------- DASHBOARD --------

  async function loadDashboard() {
    const data = await api("/api/panel/dashboard");

    setText("#kpi-clients", data.clients ?? 0);
    setText("#kpi-pending", data.pending ?? 0);
    setText("#kpi-approved", data.approved ?? 0);
    setText("#kpi-alarms", data.alarms ?? 0);

    const lock = $("#kpi-locked");
    if (lock) {
      lock.textContent = data.locked ? "قفل" : "آزاد";
      lock.style.color = data.locked
        ? "var(--red)"
        : "var(--green)";
    }

    const recent = Array.isArray(data.recent)
      ? data.recent
      : [];

    const container = $("#recent-requests");
    if (!container) return;

    container.innerHTML = recent.length
      ? recent.map(r => `
          <div class="row-item">
            <div>
              <strong>${escapeHTML(r.requester)}</strong>
              <div class="meta">
                ${escapeHTML(r.cartridge_model)}
                × ${escapeHTML(r.quantity)}
                · ${escapeHTML(formatDate(r.req_date))}
              </div>
            </div>
            ${badge(r.status)}
          </div>
        `).join("")
      : `<div class="empty">درخواستی ثبت نشده است</div>`;
  }

  // -------- REQUESTS --------

  async function loadRequests() {
    const query = state.requestStatus
      ? `?status=${encodeURIComponent(state.requestStatus)}`
      : "";

    const result = await api("/api/panel/requests" + query);
    const list = Array.isArray(result) ? result : [];

    setText("#req-count", `${list.length} درخواست`);

    const container = $("#req-list");
    if (!container) return;

    container.innerHTML = list.length
      ? list.map(r => `
        <article class="item-card">
          <div class="item-head">
            <strong>${escapeHTML(r.requester)}</strong>
            ${badge(r.status)}
          </div>

          <div class="kv">
            <span>مدل کارتریج</span>
            <span>${escapeHTML(r.cartridge_model)}</span>
          </div>

          <div class="kv">
            <span>تعداد</span>
            <span>${escapeHTML(r.quantity)}</span>
          </div>

          <div class="kv">
            <span>کد پیگیری</span>
            <span class="mono">${escapeHTML(r.tracking_code)}</span>
          </div>

          <div class="kv">
            <span>تاریخ</span>
            <span>${escapeHTML(formatDate(r.req_date))}</span>
          </div>

          ${r.status === "pending" ? `
            <div class="item-actions">
              <button type="button" class="btn success small"
                      data-request-action="approved"
                      data-id="${escapeHTML(r.id)}">
                تأیید درخواست
              </button>

              <button type="button" class="btn danger small"
                      data-request-action="rejected"
                      data-id="${escapeHTML(r.id)}">
                رد درخواست
              </button>
            </div>
          ` : ""}
        </article>
      `).join("")
      : `<div class="empty">درخواستی یافت نشد</div>`;
  }

  async function decideRequest(id, status, button) {
    if (!["approved", "rejected"].includes(status)) return;

    button.disabled = true;

    try {
      await api(
        `/api/panel/request/${encodeURIComponent(id)}`,
        {
          method: "POST",
          body: { status }
        }
      );

      toast(status === "approved"
        ? "درخواست تأیید شد"
        : "درخواست رد شد");

      await loadRequests();
    } catch (error) {
      button.disabled = false;
      toast(error.message, "err");
    }
  }

  // -------- CLIENTS --------

  async function loadClients() {
    const result = await api("/get_clients");

    state.clients = Array.isArray(result) ? result : [];
    state.diskWarnings.clear();

    const container = $("#client-list");
    if (!container) return;

    container.innerHTML = state.clients.length
      ? state.clients.map(client => `
        <article class="item-card noc-client-card">

          <div class="item-head">
            <strong class="mono">
              ${escapeHTML(client.hostname || "Unknown")}
            </strong>

            <span class="badge approved">آنلاین</span>
          </div>

          <div class="kv">
            <span>آدرس IP</span>
            <span class="mono">${escapeHTML(client.ip)}</span>
          </div>

          <div class="kv">
            <span>کاربر</span>
            <span>${escapeHTML(client.username)}</span>
          </div>

          <div class="kv">
            <span>پردازنده</span>
            <span>${escapeHTML(client.hw_cpu || "—")}</span>
          </div>

          <div class="kv">
            <span>حافظه RAM</span>
            <span>${escapeHTML(client.hw_ram || "—")}</span>
          </div>

          <div class="kv">
            <span>مدت روشن بودن</span>
            <span>${escapeHTML(formatUptime(client.hw_uptime))}</span>
          </div>

          <div class="noc-client-section">
            <h4>فضای ذخیره‌سازی</h4>
            ${renderDisks(client)}
          </div>

          ${renderPrinters(client)}

          <div class="item-actions">
            <button type="button"
              class="btn primary small"
              data-client-action="cmd"
              data-ip="${escapeHTML(client.ip)}"
              data-host="${escapeHTML(client.hostname)}">
              مدیریت دستگاه
            </button>
          </div>
        </article>
      `).join("")
      : `<div class="empty">
          هیچ کلاینت آنلاینی گزارش نشده است.
        </div>`;

    if (state.diskWarnings.size) {
      toast(
        `${state.diskWarnings.size} درایو با مصرف بالا شناسایی شد`,
        "err"
      );
    }
  }

  // -------- COMMAND MODAL --------

  function openModal(mode, ip, hostname) {
    state.modal = { mode, ip, hostname };

    setText("#modal-title", "مدیریت دستگاه");
    setText("#modal-target", `${hostname} (${ip})`);
    setText("#modal-output", "");

    const input = $("#modal-input");
    const modal = $("#modal");

    if (!input || !modal) return;

    input.value = "";
    input.classList.add("code");

    input.placeholder = "دستور مدیریتی مجاز را وارد کنید";

    modal.classList.remove("hidden");
    input.focus();
  }

  function closeModal() {
    $("#modal")?.classList.add("hidden");
    clearInterval(state.commandTimer);
    state.commandTimer = null;
    state.modal = null;
  }

  async function pollCommand(id) {
    clearInterval(state.commandTimer);

    let attempts = 0;

    const check = async () => {
      attempts++;

      try {
        const result = await api(
          `/api/panel/command/${encodeURIComponent(id)}`
        );

        if (result.status === "completed" ||
            result.status === "failed") {
          clearInterval(state.commandTimer);
          state.commandTimer = null;

          setText(
            "#modal-output",
            result.output || `وضعیت: ${result.status}`
          );

          $("#modal-run").disabled = false;
          return;
        }

        setText(
          "#modal-output",
          `در انتظار نتیجه از کلاینت... (${result.status || "pending"})`
        );
      } catch (e) {
        clearInterval(state.commandTimer);
        state.commandTimer = null;
        setText("#modal-output", e.message);
        $("#modal-run").disabled = false;
        return;
      }

      if (attempts >= 30) {
        clearInterval(state.commandTimer);
        state.commandTimer = null;

        setText("#modal-output",
          "زمان انتظار تمام شد؛ نتیجه را بعداً بررسی کنید."
        );
        $("#modal-run").disabled = false;
      }
    };

    await check();

    if (state.commandTimer === null && attempts === 1) {
      // The command may already have completed.
      if (!$("#modal-run").disabled) return;
    }

    state.commandTimer = setInterval(check, 2000);
  }

  async function runModal() {
    if (!state.modal) return;

    const input = $("#modal-input");
    const button = $("#modal-run");

    const command = input?.value.trim();

    if (!command) {
      toast("ورودی عملیات خالی است", "err");
      return;
    }

    // Execution is handled by the existing server API.
    // Server-side authorization is mandatory.
    button.disabled = true;

    try {
      const response = await api("/execute_command", {
        method: "POST",
        body: {
          ip: state.modal.ip,
          hostname: state.modal.hostname,
          command
        }
      });

      if (!response.id) {
        throw new Error("شناسه عملیات دریافت نشد");
      }

      setText(
        "#modal-output",
        `درخواست ثبت شد. شناسه: ${response.id}`
      );

      await pollCommand(response.id);

    } catch (error) {
      button.disabled = false;
      setText("#modal-output", error.message);
      toast("عملیات ناموفق بود", "err");
    }
  }

  // -------- ALARMS --------

  async function loadAlarms() {
    const result = await api("/get_alarm_history");
    const list = Array.isArray(result) ? result : [];

    const container = $("#alarm-history");
    if (!container) return;

    container.innerHTML = list.length
      ? list.map(a => `
        <div class="row-item">
          <div>
            <strong>${escapeHTML(a.message)}</strong>
            <div class="meta">
              واحد: ${escapeHTML(a.target_dept || "همه")}
              · ${escapeHTML(formatDate(a.created_at))}
              · تأیید: ${escapeHTML(a.ack_count ?? 0)}
            </div>
          </div>
          ${badge(a.level)}
        </div>
      `).join("")
      : `<div class="empty">هشداری ثبت نشده است</div>`;
  }

  async function sendAlarm() {
    const input = $("#alarm-message");
    const button = $("#alarm-send");
    const message = input?.value.trim();

    if (!message) {
      toast("متن هشدار را وارد کنید", "err");
      return;
    }

    button.disabled = true;

    try {
      await api("/send_alarm", {
        method: "POST",
        body: {
          message,
          level: $("#alarm-level").value,
          target_dept: $("#alarm-dept").value
        }
      });

      input.value = "";
      toast("هشدار ثبت شد");
      await loadAlarms();

    } catch (error) {
      toast(error.message, "err");
    } finally {
      button.disabled = false;
    }
  }

  // -------- LOCKDOWN --------

  async function loadLockdown() {
    const data = await api("/get_lockdown");

    state.locked = Boolean(data.is_active);

    setText(
      "#lock-icon",
      state.locked ? "🔒" : "🔓"
    );

    setText(
      "#lock-state",
      state.locked ? "سیستم قفل است" : "سیستم آزاد است"
    );

    const button = $("#lock-btn");

    if (button) {
      button.textContent = state.locked
        ? "آزاد کردن سیستم"
        : "قفل کردن سیستم";

      button.className = state.locked
        ? "btn big success"
        : "btn big danger";
    }
  }

  async function toggleLock() {
    const next = !state.locked;

    const message = next
      ? "آیا از قفل سراسری کلاینت‌ها مطمئن هستید؟"
      : "آیا سیستم آزاد شود؟";

    if (!confirm(message)) return;

    const button = $("#lock-btn");
    button.disabled = true;

    try {
      await api("/toggle_lockdown", {
        method: "POST",
        body: { active: next ? 1 : 0 }
      });

      toast("وضعیت قفل تغییر کرد");
      await loadLockdown();

    } catch (error) {
      toast(error.message, "err");
    } finally {
      button.disabled = false;
    }
  }

  // -------- REFRESH --------

  async function refresh() {
    if (state.busy || state.stopped) return;
    state.busy = true;

    try {
      switch (state.view) {
        case "dashboard": await loadDashboard(); break;
        case "requests": await loadRequests(); break;
        case "clients": await loadClients(); break;
        case "alarms": await loadAlarms(); break;
        case "lockdown": await loadLockdown(); break;
      }
    } catch (error) {
      console.error("Luminiz refresh:", error);

      if (error.message !== "نشست منقضی شده است") {
        toast(error.message, "err");
      }
    } finally {
      state.busy = false;
    }
  }

  function startRefresh() {
    clearInterval(state.timer);

    state.timer = setInterval(() => {
      if (!document.hidden) refresh();
    }, 5000);
  }

  // -------- EVENTS --------

  function bindEvents() {
    $$(".nav-btn[data-view]").forEach(btn => {
      btn.addEventListener("click", () => {
        switchView(btn.dataset.view);
      });
    });

    $("#logout-btn")?.addEventListener("click", () => {
      state.stopped = true;
      location.assign("/logout");
    });

    $("#req-filters")?.addEventListener("click", e => {
      const btn = e.target.closest("[data-status]");
      if (!btn) return;

      $$("#req-filters .chip").forEach(chip => {
        chip.classList.toggle("active", chip === btn);
      });

      state.requestStatus = btn.dataset.status || "";
      loadRequests().catch(e => toast(e.message, "err"));
    });

    $("#req-list")?.addEventListener("click", e => {
      const btn = e.target.closest("[data-request-action]");
      if (!btn) return;

      decideRequest(
        btn.dataset.id,
        btn.dataset.requestAction,
        btn
      );
    });

    $("#client-list")?.addEventListener("click", e => {
      const btn = e.target.closest("[data-client-action]");
      if (!btn) return;

      openModal(
        btn.dataset.clientAction,
        btn.dataset.ip,
        btn.dataset.host
      );
    });

    $("#alarm-send")?.addEventListener("click", sendAlarm);

    $("#lock-btn")?.addEventListener("click", toggleLock);

    $("#modal-run")?.addEventListener("click", runModal);

    $("#modal-close")?.addEventListener("click", closeModal);

    $("#modal")?.addEventListener("click", e => {
      if (e.target.id === "modal") closeModal();
    });

    document.addEventListener("keydown", e => {
      if (e.key === "Escape") closeModal();
    });

    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) refresh();
    });
  }

  // -------- INITIALIZATION --------

  function init() {
    startClock();
    bindEvents();
    switchView("dashboard");
    startRefresh();

    console.info("Luminiz NOC 2026 initialized");
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

})();
