document.addEventListener("DOMContentLoaded", () => {
  // ── XSS Protection ──
  function escapeHtml(str) {
    if (str === null || str === undefined) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  // Tracks whether the agent is currently running (drives adaptive polling).
  let agentRunning = false;

  // ── Table states ──────────────────────────────────────────────────────
  // A table that has loaded and found nothing used to render as a bare box -
  // pixel-identical to one that had not loaded yet and to one whose fetch had
  // failed. Three different situations, one blank rectangle, and no way for
  // the operator to tell "no contacts yet" from "the backend is down".

  function tableParts(tableId) {
    const table = document.getElementById(tableId);
    if (!table) return null;
    return {
      tbody: table.querySelector("tbody"),
      cols: table.querySelectorAll("thead th").length || 1,
    };
  }

  function showTableState(tableId, html) {
    const parts = tableParts(tableId);
    if (!parts) return;
    parts.tbody.innerHTML =
      '<tr><td class="state-cell" colspan="' +
      parts.cols +
      '">' +
      html +
      "</td></tr>";
  }

  function showTableSkeleton(tableId, rows = 5) {
    const parts = tableParts(tableId);
    if (!parts || parts.tbody.dataset.loaded === "1") return; // never flash over real data
    let html = "";
    for (let r = 0; r < rows; r++) {
      html += "<tr>";
      for (let c = 0; c < parts.cols; c++) {
        // Varying widths so the placeholder reads as content rather than a grid.
        html +=
          '<td><span class="skeleton" style="width:' +
          (50 + ((r * 7 + c * 23) % 45)) +
          '%"></span></td>';
      }
      html += "</tr>";
    }
    parts.tbody.innerHTML = html;
  }

  function showTableEmpty(tableId, icon, title, hint) {
    showTableState(
      tableId,
      '<div class="empty-state"><i class="fa-solid ' +
        icon +
        '" aria-hidden="true"></i>' +
        '<span class="empty-title">' +
        escapeHtml(title) +
        "</span>" +
        (hint
          ? '<span class="empty-hint">' + escapeHtml(hint) + "</span>"
          : "") +
        "</div>",
    );
  }

  function showTableError(tableId, what) {
    showTableState(
      tableId,
      '<div class="empty-state is-error">' +
        '<i class="fa-solid fa-plug-circle-exclamation" aria-hidden="true"></i>' +
        '<span class="empty-title">Could not load ' +
        escapeHtml(what) +
        "</span>" +
        '<span class="empty-hint">The dashboard could not reach the server. ' +
        "It will try again on the next refresh.</span></div>",
    );
  }

  // Marks a table as having rendered real rows at least once, so a later poll
  // never replaces visible data with a skeleton.
  function markLoaded(tableId, hasRows) {
    const parts = tableParts(tableId);
    if (parts) parts.tbody.dataset.loaded = hasRows ? "1" : "0";
  }

  // Tab Navigation
  const navLinks = document.querySelectorAll(".nav-links li");
  const tabContents = document.querySelectorAll(".tab-content");

  function selectTab(link, { focus = false } = {}) {
    navLinks.forEach((l) => {
      const on = l === link;
      l.classList.toggle("active", on);
      l.setAttribute("aria-selected", on ? "true" : "false");
      // Roving tabindex: the tablist is one tab stop, and the arrow keys move
      // within it. Seven separate stops would make Tab useless for reaching
      // the panel content behind them.
      l.tabIndex = on ? 0 : -1;
    });
    tabContents.forEach((t) => t.classList.remove("active"));

    const targetId = link.getAttribute("data-target");
    document.getElementById(targetId).classList.add("active");
    if (link.closest("#navAdvanced")) setAdvancedOpen(true);
    if (focus) link.focus();

    if (targetId === "tab-template") {
      loadTemplate();
    }
    if (targetId === "tab-insights") {
      loadInsights();
    }
    if (targetId === "tab-review" || targetId === "tab-markets") loadReview();
    if (targetId === "tab-overview") loadResults();
    if (targetId === "tab-quotes") loadQuotes();
    // A canvas in a hidden tab has no size, so its chart must be re-measured
    // once the tab is shown or the marks land outside the visible area.
    if (targetId === "tab-overview" && window.__resizeOverviewCharts) {
      window.__resizeOverviewCharts();
    }
    if (targetId === "tab-overview") loadReadiness();
    closeSidebar(); // collapse the mobile drawer after picking a tab
  }

  // The technical tabs fold away under "Advanced"; opening one unfolds it.
  const navMore = document.getElementById("navMore");
  const navAdvanced = document.getElementById("navAdvanced");
  function setAdvancedOpen(open) {
    navAdvanced.hidden = !open;
    navMore.setAttribute("aria-expanded", open ? "true" : "false");
    navMore.classList.toggle("open", open);
  }
  navMore.addEventListener("click", () => setAdvancedOpen(navAdvanced.hidden));

  navLinks.forEach((link, i) => {
    link.addEventListener("click", () => selectTab(link));
    link.addEventListener("keydown", (e) => {
      const last = navLinks.length - 1;
      let next = null;
      if (e.key === "ArrowDown" || e.key === "ArrowRight")
        next = navLinks[i === last ? 0 : i + 1];
      else if (e.key === "ArrowUp" || e.key === "ArrowLeft")
        next = navLinks[i === 0 ? last : i - 1];
      else if (e.key === "Home") next = navLinks[0];
      else if (e.key === "End") next = navLinks[last];
      else if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        selectTab(link);
        return;
      }
      if (next) {
        e.preventDefault();
        selectTab(next, { focus: true });
      }
    });
  });

  // ── Mobile sidebar drawer ──
  const sidebarEl = document.querySelector(".sidebar");
  const overlayEl = document.getElementById("sidebarOverlay");
  const menuToggleEl = document.getElementById("menuToggle");
  function openSidebar() {
    sidebarEl.classList.add("open");
    overlayEl.classList.add("show");
  }
  function closeSidebar() {
    sidebarEl.classList.remove("open");
    overlayEl.classList.remove("show");
  }
  if (menuToggleEl) {
    menuToggleEl.addEventListener("click", () => {
      sidebarEl.classList.contains("open") ? closeSidebar() : openSidebar();
    });
  }
  if (overlayEl) overlayEl.addEventListener("click", closeSidebar);
  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeSidebar();
  });

  // Toast Notification. Optionally carries one action - which is how Undo is
  // offered on a block, so the reversible path is the one in front of the
  // operator at the moment they need it rather than buried in a menu.
  let toastTimer = null;

  function showToast(message, type = "success", action = null) {
    const toast = document.getElementById("toast");
    clearTimeout(toastTimer);
    toast.textContent = "";

    const text = document.createElement("span");
    text.className = "toast-text";
    text.textContent = message;
    toast.appendChild(text);

    if (action) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "toast-action";
      btn.textContent = action.label;
      btn.addEventListener("click", () => {
        clearTimeout(toastTimer);
        toast.classList.remove("show");
        action.onClick();
      });
      toast.appendChild(btn);
    }

    toast.className = `toast show ${type}`;
    // An undo the operator has to read, decide on and reach for needs longer
    // than the 3s a bare notification gets.
    toastTimer = setTimeout(
      () => toast.classList.remove("show"),
      action ? 9000 : 3000,
    );
  }

  // Agent Control
  const startBtn = document.getElementById("startBtn");
  const stopBtn = document.getElementById("stopBtn");

  // res.json() resolves for a 409 as happily as for a 200, so neither of these
  // used to look at the status: a rejected request landed in the success branch
  // and toasted "Agent started successfully" over the server's refusal. The
  // reachable case is an ordinary double-click - the button was only disabled
  // inside .then(), i.e. after the response, so both clicks fired and the
  // second got the agent_lock's 409. contactAction already guards this way;
  // these two controls start and stop a live campaign, so they matter more.
  function agentControl(
    btn,
    other,
    url,
    body,
    okMessage,
    failMessage,
    running,
  ) {
    btn.disabled = true; // synchronously, so a second click cannot fire
    fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
      .then((res) => res.json().then((data) => ({ ok: res.ok, data })))
      .then(({ ok, data }) => {
        if (!ok || (data && data.error)) {
          showToast((data && data.error) || failMessage, "error");
          btn.disabled = false;
          return;
        }
        showToast(okMessage);
        agentRunning = running;
        other.disabled = false;
      })
      .catch(() => {
        showToast(failMessage, "error");
        btn.disabled = false;
      });
  }

  startBtn.addEventListener("click", () => {
    agentControl(
      startBtn,
      stopBtn,
      "/api/agent/start",
      { mode: "continuous" },
      "Agent started successfully",
      "Failed to start agent",
      true,
    );
  });

  stopBtn.addEventListener("click", () => {
    agentControl(
      stopBtn,
      startBtn,
      "/api/agent/stop",
      {},
      "Agent stopped",
      "Failed to stop agent",
      false,
    );
  });

  // Add Query
  document.getElementById("addQueryForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const input = document.getElementById("newQuery");
    const query = input.value.trim();
    if (!query) return;

    fetch("/api/queries/add", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
    })
      .then((res) => res.json())
      .then((data) => {
        if (data.status === "ok") {
          showToast("Added query: " + escapeHtml(query));
          input.value = "";
          loadSearches();
        } else {
          showToast(data.error || "Failed to add query", "error");
        }
      })
      .catch(() => showToast("Network error adding query", "error"));
  });

  // ── Direct / manual send ──
  function submitManualSend(dryRun, confirmRepeat = false) {
    const email = document.getElementById("msEmail").value.trim();
    const result = document.getElementById("msResult");
    if (!email) {
      showToast("Enter an email address", "error");
      return;
    }

    const sendBtn = document.getElementById("msSendBtn");
    const prevBtn = document.getElementById("msPreviewBtn");
    sendBtn.disabled = true;
    prevBtn.disabled = true;
    result.className = "ms-result";
    result.textContent = dryRun ? "Building the draft…" : "Sending…";
    let resent = false;

    fetch("/api/send-manual", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email,
        name: document.getElementById("msName").value.trim(),
        organization: document.getElementById("msOrg").value.trim(),
        enroll_followup: document.getElementById("msFollow").checked,
        channel: document.getElementById("msChannel").value,
        dry_run: dryRun,
        confirm_repeat: confirmRepeat,
      }),
    })
      .then((res) => res.json().then((data) => ({ ok: res.ok, data })))
      .then(({ ok, data }) => {
        // Nothing went out: the operator decides whether this address gets
        // a second email, and only a yes sends it.
        if (data.already_emailed && !confirmRepeat) {
          if (
            window.confirm(
              "This address was already emailed on " +
                data.last_sent_on +
                ". Send another email?",
            )
          ) {
            resent = true;
            submitManualSend(dryRun, true);
            return;
          }
        }
        const good = ok && data.ok;
        result.className = "ms-result " + (good ? "ok" : "err");
        result.textContent = data.message || (good ? "Done." : "Failed.");
        showToast(
          data.message || (good ? "Sent" : "Failed"),
          good ? "success" : "error",
        );
        if (good && !dryRun) {
          document.getElementById("msEmail").value = "";
          document.getElementById("msName").value = "";
          document.getElementById("msOrg").value = "";
          document.getElementById("msFollow").checked = false;
        }
      })
      .catch(() => {
        result.className = "ms-result err";
        result.textContent = "Network error.";
        showToast("Network error sending", "error");
      })
      .finally(() => {
        if (resent) return;
        sendBtn.disabled = false;
        prevBtn.disabled = false;
      });
  }

  document.getElementById("manualSendForm").addEventListener("submit", (e) => {
    e.preventDefault();
    submitManualSend(false);
  });
  document
    .getElementById("msPreviewBtn")
    .addEventListener("click", () => submitManualSend(true));

  // ── Spam / inbox test (mail-tester.com) ──
  // The template disables the card and says why, so none of this fires until
  // the card has a send route of its own.
  const stOpenBtn = document.getElementById("stOpenBtn");
  if (stOpenBtn) {
    stOpenBtn.addEventListener("click", () => {
      window.open("https://www.mail-tester.com", "_blank", "noopener");
    });
    document.getElementById("spamTestForm").addEventListener("submit", (e) => {
      e.preventDefault();
      const addr = document.getElementById("stAddr").value.trim();
      const res = document.getElementById("stResult");
      const link = document.getElementById("stScoreLink");
      const btn = document.getElementById("stSendBtn");
      if (!/@.+\.mail-tester\.com$/i.test(addr)) {
        res.className = "ms-result err";
        res.textContent =
          "Paste the address from mail-tester.com (…@srv1.mail-tester.com).";
        return;
      }
      btn.disabled = true;
      res.className = "ms-result";
      res.textContent = "Sending test email…";
      fetch("/api/send-manual", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: addr,
          name: "Deliverability Test",
          dry_run: false,
        }),
      })
        .then((r) => r.json().then((d) => ({ ok: r.ok, d })))
        .then(({ ok, d }) => {
          const good = ok && d.ok;
          res.className = "ms-result " + (good ? "ok" : "err");
          res.textContent = good
            ? 'Sent. Now open mail-tester.com and click "Then check your score".'
            : d.message || "Send failed (is Outlook configured?)";
          if (good) link.style.display = "";
        })
        .catch(() => {
          res.className = "ms-result err";
          res.textContent = "Network error.";
        })
        .finally(() => {
          btn.disabled = false;
        });
    });
  }

  // ── Chart theme ───────────────────────────────────────────────────────
  // Derived from the SUPPLECO brand, not Chart.js defaults: mustard carries the
  // data, silver is the secondary, and the frame (grid/axes/ticks) stays
  // recessive so the marks read first. The charts previously used leftover
  // blue/purple, which looked like a stock template inside a branded page.
  const BRAND = {
    gold: "#C6B128", // Pantone 606 "Giallo energia"
    goldHi: "#DAC53E",
    goldSoft: "rgba(0, 198, 255, 0.16)",
    silver: "#81878A", // Pantone 8420
    ink: "#EDEDEA",
    muted: "#8E8E94",
    grid: "rgba(255, 255, 255, 0.055)",
    surface: "#1F1F22",
  };
  // Ordinal ramp for the funnel: it is a SEQUENCE (each stage a subset of the
  // one above), so one hue stepping light -> deep, never a categorical mix.
  const FUNNEL_RAMP = ["#E8D86A", "#DAC53E", "#C6B128", "#AE9A21", "#8F7E1A"];

  // Chart.js can fail to load (it came from a CDN an office network could
  // block; the copy in static/vendor can still be missing or refused).
  // Without it there is no Chart, and the
  // first use of it threw out of this handler before the contacts table, its
  // row actions or the refresh buttons were set up: no contacts, and nothing
  // said why. So each chart box says the charts are unavailable instead, and
  // everything else runs as it does with them.
  const chartsOk = typeof Chart !== "undefined";

  if (chartsOk) {
    Chart.defaults.color = BRAND.muted;
    Chart.defaults.borderColor = BRAND.grid;
    Chart.defaults.font.family = "'Inter', system-ui, sans-serif";
    Chart.defaults.font.size = 11;
  }

  // Dark card tooltip that matches the dashboard's own surfaces.
  const TOOLTIP = {
    backgroundColor: BRAND.surface,
    titleColor: BRAND.ink,
    bodyColor: BRAND.ink,
    borderColor: "rgba(255,255,255,0.10)",
    borderWidth: 1,
    cornerRadius: 8,
    padding: 10,
    displayColors: false,
    titleFont: { weight: "600", size: 12 },
    bodyFont: { size: 12 },
  };

  // Hairline horizontal grid only — vertical lines add clutter without
  // helping anyone read a value.
  const yAxis = {
    beginAtZero: true,
    border: { display: false },
    grid: { color: BRAND.grid, drawTicks: false },
    ticks: { precision: 0, padding: 8, color: BRAND.muted },
  };
  const xAxisCat = {
    border: { display: false },
    grid: { display: false },
    ticks: {
      color: BRAND.muted,
      // Show a handful of readable dates rather than all 30 - and let Chart.js
      // rotate them if it needs to. Pinning maxRotation to 0 forces every label
      // to stay horizontal, which expands the scale far beyond the canvas and
      // pushes the bars out of view entirely.
      autoSkip: true,
      maxTicksLimit: 7,
      maxRotation: 30,
      minRotation: 0,
      // "2026-07-02" -> "Jul 2": shorter labels, less crowding.
      callback(value) {
        const raw = this.getLabelForValue(value);
        const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(raw));
        if (!m) return raw;
        const d = new Date(+m[1], +m[2] - 1, +m[3]);
        return d.toLocaleDateString(undefined, {
          month: "short",
          day: "numeric",
        });
      },
    },
  };

  const commonOpts = {
    responsive: true,
    maintainAspectRatio: false,
    animation: { duration: 500, easing: "easeOutQuart" },
    interaction: { mode: "index", intersect: false },
    plugins: { legend: { display: false }, tooltip: TOOLTIP },
    scales: { y: yAxis, x: xAxisCat },
  };

  // Vertical mustard wash under the trend line — the one deliberate flourish,
  // used in a single place so it stays a signature rather than decoration.
  function goldFill(ctx, chartArea) {
    if (!chartArea) return BRAND.goldSoft;
    const g = ctx.createLinearGradient(0, chartArea.top, 0, chartArea.bottom);
    g.addColorStop(0, "rgba(0, 198, 255, 0.30)");
    g.addColorStop(1, "rgba(0, 198, 255, 0.00)");
    return g;
  }

  // Charts can be created before their container has a settled width (or while
  // their tab is hidden), which leaves Chart.js with a stale, enormous scale -
  // the marks then draw far outside the canvas and the panel looks empty.
  // Re-measuring before each update keeps the layout honest; resize() is cheap
  // and a no-op when the size already matches.
  function refresh(chart) {
    chart.update();
    // Re-measure on the next frame. On the first paint the grid container has
    // not settled, so Chart.js computes an oversized scale and draws the marks
    // far outside the canvas - the panel then looks empty. resize() alone is a
    // no-op when the canvas dimensions appear unchanged, so the stale scale
    // survives; forcing an update after the resize is what actually re-lays out.
    //
    // setTimeout, not requestAnimationFrame: rAF does not fire while the tab is
    // in the background, and a monitoring dashboard spends most of its life
    // there - the charts would stay broken until it was focused.
    setTimeout(() => {
      try {
        chart.resize();
        chart.update("none");
      } catch (e) {
        /* canvas not laid out yet */
      }
    }, 0);
  }

  // Draws the numeric value next to each funnel bar so every stage stays
  // visible — including zero-value stages (Opened/Replied) whose bars have
  // no width and would otherwise render as blank, looking broken.
  const funnelValueLabels = {
    id: "funnelValueLabels",
    afterDatasetsDraw(chart) {
      const meta = chart.getDatasetMeta(0);
      if (!meta || !meta.data) return;
      const ctx = chart.ctx;
      ctx.save();
      ctx.font = "600 12px Inter, system-ui, sans-serif";
      ctx.fillStyle = BRAND.ink;
      ctx.textBaseline = "middle";
      meta.data.forEach((bar, i) => {
        const val = chart.data.datasets[0].data[i] ?? 0;
        ctx.fillText(String(val), bar.x + 10, bar.y);
      });
      ctx.restore();
    },
  };

  // Distinct colour per contact status, in the dataviz palette's dark
  // adjacency-safe order (validated: worst adjacent CVD dE 8.4). Identity is
  // carried by the bar labels, so no two statuses ever share a colour.
  // This is the source of truth for status hue; the .badge-* rules in
  // dashboard.css mirror it, so changing a colour here means changing it there.
  const STATUS_META = {
    pending: { label: "Pending", color: "#3987e5" }, // blue
    emailed: { label: "Emailed", color: "#d95926" }, // orange
    responded: { label: "Replied", color: "#199e70" }, // aqua
    unverified: { label: "Unverified", color: "#c98500" }, // yellow
    low_score: { label: "Low score", color: "#d55181" }, // magenta
    irrelevant: { label: "Off-target", color: "#008300" }, // green
    ai_irrelevant: { label: "Off-target (AI)", color: "#9085e9" }, // violet
    failed: { label: "Failed", color: "#e66767" }, // red
  };
  const STATUS_ORDER = [
    "pending",
    "emailed",
    "responded",
    "unverified",
    "low_score",
    "irrelevant",
    "ai_irrelevant",
    "failed",
  ];
  const STATUS_OTHER = { label: "Other", color: "#898781" }; // dry_run/blocked/…

  // Without Chart.js, the box says so, and the chart is a stand-in that holds
  // its data and ignores redraws: loadCharts fills it as it would a chart, and
  // goes on to the figures it fills after the charts.
  function makeChart(canvasId, config) {
    const canvas = document.getElementById(canvasId);
    if (chartsOk) return new Chart(canvas, config);
    if (canvas) {
      const note = document.createElement("div");
      note.className = "empty-state";
      note.innerHTML =
        '<span class="empty-title">Charts unavailable</span>' +
        '<span class="empty-hint">This computer could not download the ' +
        "chart library. The rest of the dashboard still works.</span>";
      canvas.replaceWith(note);
    }
    return { data: config.data, update() {}, resize() {} };
  }

  const emailsChart = makeChart("emailsPerDayChart", {
    type: "bar",
    data: {
      labels: [],
      datasets: [
        {
          data: [],
          backgroundColor: BRAND.gold,
          hoverBackgroundColor: BRAND.goldHi,
          // Rounded only at the data end, anchored to the baseline.
          borderRadius: {
            topLeft: 5,
            topRight: 5,
            bottomLeft: 0,
            bottomRight: 0,
          },
          borderSkipped: "bottom",
          maxBarThickness: 34,
        },
      ],
    },
    options: {
      ...commonOpts,
      plugins: {
        ...commonOpts.plugins,
        tooltip: {
          ...TOOLTIP,
          callbacks: {
            label: (c) =>
              `${c.parsed.y} email${c.parsed.y === 1 ? "" : "s"} sent`,
          },
        },
      },
    },
  });

  // Horizontal bars for the funnel, coloured with an ORDINAL mustard ramp:
  // each stage is a subset of the one above, so the colour should step in one
  // hue rather than change identity like a categorical set would.
  const horizontalBarOpts = {
    ...commonOpts,
    indexAxis: "y",
    layout: { padding: { right: 40 } },
    interaction: { mode: "nearest", intersect: true },
    scales: {
      x: {
        beginAtZero: true,
        border: { display: false },
        grid: { color: BRAND.grid, drawTicks: false },
        ticks: { precision: 0, padding: 6, color: BRAND.muted },
      },
      y: {
        border: { display: false },
        grid: { display: false },
        ticks: { color: BRAND.ink, padding: 6, font: { size: 12 } },
      },
    },
  };

  const funnelChart = makeChart("leadFunnelChart", {
    type: "bar",
    data: {
      labels: [],
      datasets: [
        {
          data: [],
          backgroundColor: FUNNEL_RAMP,
          hoverBackgroundColor: BRAND.goldHi,
          borderRadius: {
            topLeft: 0,
            bottomLeft: 0,
            topRight: 5,
            bottomRight: 5,
          },
          borderSkipped: "left",
          maxBarThickness: 26,
        },
      ],
    },
    options: horizontalBarOpts,
    plugins: [funnelValueLabels],
  });

  // Horizontal bar (not a doughnut): a doughnut can't give ~10 status
  // categories colours that stay distinct under colour-blindness (all-pairs),
  // and bars read magnitudes better. Labels carry identity; each bar keeps
  // its own validated colour.
  const statusChart = makeChart("contactStatusChart", {
    type: "bar",
    data: {
      labels: [],
      datasets: [
        {
          data: [],
          backgroundColor: [],
          borderRadius: {
            topLeft: 0,
            bottomLeft: 0,
            topRight: 5,
            bottomRight: 5,
          },
          borderSkipped: "left",
          maxBarThickness: 22,
        },
      ],
    },
    options: horizontalBarOpts,
    plugins: [funnelValueLabels],
  });

  const successChart = makeChart("successRateChart", {
    type: "line",
    data: {
      labels: [],
      datasets: [
        {
          data: [],
          borderColor: BRAND.gold,
          borderWidth: 2, // thin mark; the fill carries the weight
          tension: 0.35,
          fill: true,
          backgroundColor: (c) => goldFill(c.chart.ctx, c.chart.chartArea),
          // Points appear on hover only — a dot on every reading is noise.
          pointRadius: 0,
          pointHoverRadius: 5,
          pointBackgroundColor: BRAND.goldHi,
          pointBorderColor: BRAND.surface,
          pointBorderWidth: 2,
          pointHitRadius: 12, // generous target, invisible mark
        },
      ],
    },
    options: {
      ...commonOpts,
      scales: {
        ...commonOpts.scales,
        y: {
          ...yAxis,
          suggestedMax: 100,
          ticks: { ...yAxis.ticks, callback: (v) => v + "%" },
        },
      },
      plugins: {
        ...commonOpts.plugins,
        tooltip: {
          ...TOOLTIP,
          callbacks: { label: (c) => `${c.parsed.y}% delivered` },
        },
      },
    },
  });

  // Exposed so the tab switcher can re-measure the Overview charts when that
  // tab becomes visible, and so a window resize keeps them correct.
  window.__resizeOverviewCharts = function () {
    [emailsChart, funnelChart, statusChart, successChart].forEach((c) => {
      try {
        c.resize();
      } catch (e) {
        /* not laid out */
      }
    });
  };
  window.addEventListener("resize", () => window.__resizeOverviewCharts());

  // ── Sidebar status ──
  // "Running" is not the same as "sending": a reputation guard can pause
  // sends while the loop keeps monitoring the inbox, and a failed readiness
  // check refuses every live first email while the loop runs. Showing plain
  // green there would tell the operator everything is fine while nothing goes
  // out. The pause keeps precedence: it names the guard that stopped sending.
  let lastAgentStatus = null;

  function renderSidebarStatus() {
    const data = lastAgentStatus || {};
    const dot = document.querySelector(".status-indicator");
    const label =
      dot && dot.parentElement ? dot.parentElement.querySelector("span") : null;
    const dryRun = !!(data.dry_run || (readiness && readiness.dry_run));
    const blocked = !!(readiness && readiness.can_send_live === false);
    // A failed check keeps the last answer: a blocked state stays blocked,
    // and with nothing blocked known the sidebar says it could not check.
    const unchecked = readinessFailed && !blocked;
    const warn = !!data.sending_paused || ((blocked || unchecked) && !dryRun);
    if (dot) {
      dot.classList.toggle("paused", warn);
      dot.classList.toggle("online", !warn);
    }
    if (!label) return;
    if (data.sending_paused) {
      label.textContent =
        "Sending PAUSED (" + escapeHtml(data.pause_reason || "guard") + ")";
    } else if (dryRun) {
      label.textContent = "DRY RUN - nothing is sent";
    } else if (blocked) {
      label.textContent = "Live sending blocked - see Ready to send";
    } else if (unchecked) {
      label.textContent = "Sending could not be checked - see Ready to send";
    } else {
      label.textContent = data.running ? "Agent running" : "System Online";
    }
  }

  // ── Ready to send ──
  // /api/readiness answers from the doctor's checks that decide a live first
  // email. It opens the database several times, so it is asked when the
  // Overview opens, by Check again, and at most once a minute from the poll.
  const READINESS_EVERY_MS = 60000;
  let readiness = null;
  let readinessFailed = false;
  let readinessAt = 0;

  // [doctor label, the name the card shows]. null is the follow-up row,
  // which the doctor does not check.
  const READINESS_ROWS = [
    ["Sender title", "Sender title"],
    ["PDF attachment present", "Company Profile PDF"],
    ["Italian email template present", "Italian first email"],
    [null, "Italian follow-ups 1 and 2"],
    ["Client list file", "Client list"],
    ["Competitor list file", "Competitor list"],
    ["Inbox read", "Inbox read"],
    ["Country lock", "Enabled countries"],
    ["Legal identity (email footer)", "Legal identity"],
    ["Opt-out route", "Opt-out route"],
    ["Legal footer in templates", "Legal footer in templates"],
    ["SMTP credentials set", "SMTP login"],
  ];

  function readinessState(ok, blocking) {
    if (ok === true) return '<span class="ok">OK</span>';
    if (ok === false)
      return '<span class="' + (blocking ? "bad" : "warn") + '">Problem</span>';
    return '<span class="muted">Not checked</span>';
  }

  function followupCheck(d) {
    const f = d.italian_followups || {};
    const missing = ["followup_1_it", "followup_2_it"].filter((n) => !f[n]);
    if (!missing.length)
      return {
        ok: true,
        detail: "templates/followup_1_it.html and followup_2_it.html present",
      };
    return {
      ok: false,
      blocking: false,
      detail:
        missing.map((n) => n + ".html").join(" and ") +
        " missing - Italian recipients get the English follow-up instead",
    };
  }

  // The doctor reads the list itself now, so its detail names the file with
  // its count, or why it refuses; a prefix only repeated it.
  function competitorDetail(check, d) {
    const c = d.competitor || {};
    return c.state === "off" ? "off - " + check.detail : check.detail;
  }

  function renderReadiness(d) {
    readiness = d;
    readinessFailed = false;
    const byLabel = {};
    (d.checks || []).forEach((c) => (byLabel[c.label] = c));
    const rows = [];
    let firstRefusal = null;
    READINESS_ROWS.forEach(([label, name]) => {
      let c = label ? byLabel[label] : followupCheck(d);
      if (!c) return;
      if (label === "Competitor list file") {
        c = Object.assign({}, c, {
          detail: competitorDetail(c, d),
          ok: d.competitor && d.competitor.state === "unreadable" ? false : c.ok,
        });
      }
      if (c.blocking && c.ok === false && !firstRefusal)
        firstRefusal = name + ": " + c.detail;
      rows.push(checkRow(name, c));
    });
    renderPrivacyChecks(d.privacy);
    document.getElementById("readinessChecks").innerHTML =
      '<table class="kv-table">' + rows.join("") + "</table>";

    // can_send_live is false only when no live first email can go; what
    // stops some of them (one language, an A/B arm) comes as held_back.
    const heldBack = d.held_back || [];
    const headline = document.getElementById("readinessHeadline");
    headline.className =
      "readiness-headline " +
      (!d.can_send_live ? "bad" : heldBack.length ? "warn" : "ok");
    headline.textContent = d.can_send_live
      ? "A live first email can go out" +
        (heldBack.length ? ", but " + heldBack.join("; ") : "")
      : "Live first emails are refused" +
        (firstRefusal ? ": " + firstRefusal : "");
    document.getElementById("readinessDryRun").hidden = !d.dry_run;

    const title = byLabel["Sender title"];
    const titleRefused = !!(title && title.ok === false && title.blocking);
    const titleMissing = !!(title && title.ok === false);
    const anyTitled = (d.mailboxes || []).some((m) => m.title);
    const boxes = (d.mailboxes || [])
      .map(
        (m) =>
          '<tr><td class="k">' +
          escapeHtml(m.address || "Primary mailbox (no address set)") +
          '</td><td class="v">' +
          (m.title
            ? escapeHtml(m.title)
            : '<span class="' +
              (titleRefused ? "bad" : titleMissing ? "warn" : "muted") +
              '">no title' +
              (titleRefused
                ? " - first emails from it refused"
                : !titleMissing
                  ? ""
                  : anyTitled
                    ? " - first emails go through a mailbox with one"
                    : " - first emails that name the title are held back") +
              "</span>") +
          "</td></tr>",
      )
      .join("");
    document.getElementById("readinessMailboxes").innerHTML = boxes
      ? '<h4 class="panel-sub">Mailboxes and their titles</h4>' +
        '<table class="kv-table">' +
        boxes +
        "</table>"
      : "";
    renderSidebarStatus();
  }

  // ── Privacy and inbox ──
  // The doctor's privacy checks come with the readiness answer, from the
  // same run; the open data requests from their own read-only route.
  function checkRow(name, c) {
    return (
      '<tr><td class="k">' +
      escapeHtml(name) +
      '</td><td class="v">' +
      readinessState(c.ok, c.blocking) +
      ' <span class="readiness-detail">' +
      escapeHtml(c.detail) +
      "</span></td></tr>"
    );
  }

  function renderPrivacyChecks(checks) {
    const el = document.getElementById("privacyChecks");
    if (!el) return;
    el.innerHTML =
      '<table class="kv-table">' +
      (checks || []).map((c) => checkRow(c.label, c)).join("") +
      "</table>";
  }

  function loadDataRequests() {
    const el = document.getElementById("dsrTable");
    if (!el) return;
    fetch("/api/data-requests")
      .then((r) => r.json().then((data) => ({ ok: r.ok, data })))
      .then(({ ok, data }) => {
        if (!ok || !Array.isArray(data)) {
          el.innerHTML =
            '<p class="muted is-error">' +
            escapeHtml(
              (data && data.error) || "Could not load the data requests.",
            ) +
            "</p>";
          return;
        }
        if (!data.length) {
          el.innerHTML = '<p class="muted">No open data requests</p>';
          return;
        }
        const day = (s) => escapeHtml(String(s || "").slice(0, 10));
        el.innerHTML =
          '<table class="modern-table dsr-table"><thead><tr>' +
          '<th scope="col">#</th><th scope="col">Kind</th>' +
          '<th scope="col">Channel</th><th scope="col">Received</th>' +
          '<th scope="col">Due</th><th scope="col">Days left</th>' +
          "</tr></thead><tbody>" +
          data
            .map(
              (r) =>
                "<tr" +
                (r.overdue ? ' class="dsr-overdue"' : "") +
                "><td>#" +
                escapeHtml(r.id) +
                "</td><td>" +
                escapeHtml(r.kind) +
                "</td><td>" +
                escapeHtml(r.channel) +
                "</td><td>" +
                day(r.received_at) +
                "</td><td>" +
                day(r.due_at) +
                "</td><td>" +
                (r.overdue
                  ? '<span class="status-badge badge-failed">' +
                    (r.days_left < 0
                      ? "overdue by " +
                        escapeHtml(-r.days_left) +
                        (r.days_left === -1 ? " day" : " days")
                      : "overdue today") +
                    "</span>"
                  : escapeHtml(r.days_left)) +
                "</td></tr>",
            )
            .join("") +
          "</tbody></table>";
      })
      .catch((e) => {
        console.warn("Data requests error:", e);
        el.innerHTML =
          '<p class="muted is-error">Could not load the data requests.</p>';
      });
  }

  function readinessError(message) {
    readinessFailed = true;
    const headline = document.getElementById("readinessHeadline");
    headline.className = "readiness-headline is-error";
    headline.textContent =
      message || "Could not run the checks - the server did not answer.";
    document.getElementById("readinessChecks").innerHTML = "";
    document.getElementById("readinessMailboxes").innerHTML = "";
    const privacy = document.getElementById("privacyChecks");
    if (privacy)
      privacy.innerHTML =
        '<p class="muted is-error">Could not run the checks - see Ready to send.</p>';
    renderSidebarStatus();
  }

  function loadReadiness() {
    if (!document.getElementById("readinessCard")) return;
    readinessAt = Date.now();
    loadDataRequests();
    fetch("/api/readiness")
      .then((r) => r.json().then((data) => ({ ok: r.ok, data })))
      .then(({ ok, data }) => {
        if (!ok || data.error) {
          readinessError(data.error);
          return;
        }
        renderReadiness(data);
      })
      .catch((e) => {
        console.warn("Readiness error:", e);
        readinessError();
      });
  }

  function readinessIsStale() {
    return Date.now() - readinessAt >= READINESS_EVERY_MS;
  }

  // ── Load Data Functions ──
  function loadStats() {
    fetch("/api/agent/status")
      .then((r) => r.json())
      .then((data) => {
        agentRunning = !!data.running;
        lastAgentStatus = data;
        renderSidebarStatus();
        const startBtn = document.getElementById("startBtn");
        const stopBtn = document.getElementById("stopBtn");
        if (data.running) {
          startBtn.disabled = true;
          stopBtn.disabled = false;
        } else {
          startBtn.disabled = false;
          stopBtn.disabled = true;
        }
        if (data.stats) {
          document.getElementById("statTotalContacts").textContent =
            data.stats.total_contacts || 0;
          document.getElementById("statEmailsSent").textContent =
            data.stats.emails_sent || 0;
        }
      })
      .catch((e) => console.warn("Agent status error:", e));

    fetch("/api/stats")
      .then((r) => r.json())
      .then((data) => {
        document.getElementById("statTotalContacts").textContent =
          data.total_contacts || 0;
        document.getElementById("statEmailsSent").textContent =
          data.emails_sent || 0;
        // Authoritative per-status totals for the Contacts chips. Keeping them
        // here means the chips and the Overview cards read from one source, so
        // they cannot disagree the way they did.
        if (data.contact_status_counts) {
          contactStatusCounts = data.contact_status_counts;
          statsLoaded = true;
          buildStatusChips("contactsTable", "contactStatusChips");
        }
      })
      .catch((e) => console.warn("Stats API error:", e));
  }

  // The charts and the warm-up figure move slowly, and /api/charts reads
  // every contact and email, so the poll asks for them at most once a minute,
  // as it does for the readiness card.
  const CHARTS_EVERY_MS = 60000;
  let chartsAt = 0;

  function chartsAreStale() {
    return Date.now() - chartsAt >= CHARTS_EVERY_MS;
  }

  function loadCharts() {
    chartsAt = Date.now();
    fetch("/api/charts")
      .then((r) => r.json())
      .then((data) => {
        // Update emails per day chart
        if (data.emails_per_day) {
          emailsChart.data.labels = data.emails_per_day.labels || [];
          emailsChart.data.datasets[0].data = data.emails_per_day.data || [];
          refresh(emailsChart);
        }
        // Update success rate chart
        if (data.success_rate_trend) {
          successChart.data.labels = data.success_rate_trend.labels || [];
          successChart.data.datasets[0].data =
            data.success_rate_trend.data || [];
          refresh(successChart);
        }
        // Update contacts by status chart (distinct colour per status,
        // fixed order; unknown/rare statuses fold into "Other").
        if (data.contacts_by_status) {
          const raw = {};
          (data.contacts_by_status.labels || []).forEach((lab, i) => {
            raw[lab] = (data.contacts_by_status.data || [])[i] || 0;
          });
          const labels = [],
            values = [],
            colors = [];
          STATUS_ORDER.forEach((s) => {
            if (raw[s] != null) {
              labels.push(STATUS_META[s].label);
              values.push(raw[s]);
              colors.push(STATUS_META[s].color);
              delete raw[s];
            }
          });
          const other = Object.values(raw).reduce((a, b) => a + (b || 0), 0);
          if (other > 0) {
            labels.push(STATUS_OTHER.label);
            values.push(other);
            colors.push(STATUS_OTHER.color);
          }
          statusChart.data.labels = labels;
          statusChart.data.datasets[0].data = values;
          statusChart.data.datasets[0].backgroundColor = colors;
          refresh(statusChart);
        }
        // Update lead funnel chart
        if (data.lead_funnel) {
          funnelChart.data.labels = data.lead_funnel.labels || [];
          funnelChart.data.datasets[0].data = data.lead_funnel.data || [];
          refresh(funnelChart);
        }
        // Update opens stat card from funnel data
        if (data.lead_funnel && data.lead_funnel.data) {
          const opensEl = document.getElementById("statOpens");
          if (opensEl) opensEl.textContent = data.lead_funnel.data[2] || 0;
        }
      })
      .catch((e) => console.warn("Charts API not ready:", e));

    fetch("/api/warmup")
      .then((r) => r.json())
      .then((data) => {
        if (data.day_number) {
          document.getElementById("statWarmup").textContent =
            "Day " +
            data.day_number +
            " (" +
            data.sent_today +
            "/" +
            data.daily_allowance +
            ")";
        }
      })
      .catch(() => {});
  }

  // Every status a badge knows, with the words it shows. An email that may
  // have reached the person must not look like one waiting in the queue, so
  // the send states say what they mean. Any other status shows as it is.
  const BADGES = {
    emailed: { label: "emailed", cls: "badge-emailed" },
    sent: { label: "sent", cls: "badge-emailed" },
    success: { label: "success", cls: "badge-emailed" },
    failed: { label: "failed", cls: "badge-failed" },
    blocked: { label: "blocked", cls: "badge-failed" },
    preparing: { label: "Sending - not settled", cls: "badge-pending" },
    sent_unconfirmed: {
      label: "Sent, not confirmed - not repeated",
      cls: "badge-emailed",
    },
    rejected: { label: "Refused by the server", cls: "badge-failed" },
    dry_run: { label: "Preview only - not sent", cls: "badge-pending" },
  };

  function getBadge(status, title) {
    const known = BADGES[status];
    const label = known ? known.label : status;
    const cls = known ? known.cls : "badge-pending";
    return (
      '<span class="status-badge ' +
      cls +
      '"' +
      (title ? ' title="' + escapeHtml(title) + '"' : "") +
      ">" +
      escapeHtml(label) +
      "</span>"
    );
  }

  // The language of an email, read from the notice it carried: the Italian
  // versions end in "-it". Older emails recorded none.
  function noticeLanguage(notice) {
    if (!notice) return null;
    return String(notice).endsWith("-it") ? "it" : "en";
  }

  // The language every email to a contact is written in, from the server's
  // one rule (compliance.language_for), and why when the server says; none
  // for an erased contact.
  function langBadge(language, reason) {
    if (language !== "it" && language !== "en") return "";
    return (
      ' <span class="lang-badge" title="' +
      escapeHtml(
        "Written in " +
          (language === "it" ? "Italian" : "English") +
          (reason ? ": " + reason : ""),
      ) +
      '">' +
      language.toUpperCase() +
      "</span>"
    );
  }

  // A cell that gives up width so every column fits the window: the text is
  // cut with an ellipsis on a desktop screen, so its title keeps the whole.
  function longCell(text) {
    return (
      '<td class="cell-long" title="' +
      escapeHtml(text) +
      '">' +
      escapeHtml(text) +
      "</td>"
    );
  }

  // The old version resolved r.json() and announced success unconditionally,
  // so a 409 or a 500 still toasted "Action block executed" - the operator was
  // told the thing had happened when the server had just refused it.
  function contactAction(id, action, opts = {}) {
    return fetch("/api/contacts/" + id + "/action", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action }),
    })
      .then((r) => r.json().then((data) => ({ ok: r.ok, data })))
      .then(({ ok, data }) => {
        if (!ok || data.error) {
          showToast(data.error || "Action failed", "error");
          return false;
        }
        // The server's account wins over the page's: a Block it found already
        // closed kept the status, and offers Undo only if it added something.
        const message = data.message || opts.message || "Done";
        if (opts.undo && data.undo) {
          showToast(message, "success", {
            label: "Undo",
            onClick: () =>
              contactAction(id, opts.undo, {
                message: opts.undoMessage || "Undone",
              }),
          });
        } else {
          showToast(message, "success");
        }
        loadContacts();
        return true;
      })
      .catch(() => {
        showToast("Could not reach the server", "error");
        return false;
      });
  }
  window.contactAction = contactAction;

  // Both of these used to take the email/company as a second argument, spliced
  // into an onclick="" attribute in the row markup. HTML-escaping does not make
  // a value safe there: the browser decodes &quot; back to a real quote while
  // parsing the attribute, so a quote in the value closed the JS string and
  // everything after it ran as code - and these values are scraped from the
  // prospect's own page, so the prospect chooses them. They are read from
  // contactsById instead, which already holds the full record for the detail
  // panel, and the markup now carries nothing but a numeric id.
  function blockContact(id) {
    const email = (contactsById[id] || {}).email;
    // No confirm dialog: an undo you can actually take is better than a modal
    // you learn to dismiss without reading.
    contactAction(id, "block", {
      undo: "unblock",
      message:
        (email ? email + " blocked" : "Contact blocked") + " and suppressed",
      undoMessage: "Block reversed - contact is back in the queue",
    });
  }
  window.blockContact = blockContact;

  function markClient(id) {
    const org = (contactsById[id] || {}).organization;
    // Operator-driven on purpose: only a human knows when a deal actually
    // closed. Confirmed because it edits the company's client master list,
    // which is a commercial record rather than agent state.
    if (
      !window.confirm(
        (org ? org : "This company") +
          " will be added to the active-client list and never cold-emailed " +
          "again. Only do this once they are actually a customer.",
      )
    ) {
      return;
    }
    contactAction(id, "mark_client", {
      message: (org || "Contact") + " marked as an active client",
    });
  }
  window.markClient = markClient;

  function unblockContact(id) {
    contactAction(id, "unblock", { message: "Contact unblocked" });
  }
  window.unblockContact = unblockContact;

  // Row detail: why this contact was or wasn't emailed. The contact's own
  // fields render at once from the list payload; the explanation - a
  // headline, the facts behind it, the history newest first - comes from
  // /api/contacts/<id>/why. The raw discard_reason is not shown on its own:
  // it can outlive the verdict that wrote it, and the endpoint only reports a
  // reason that belongs to the current status.
  //
  // Open panels are remembered by contact id. loadContacts replaces the table
  // body on every poll (5-15 s), which used to close every panel; they are
  // put back after each rebuild, and refetched only when the status changed.
  const openDetails = new Map(); // id -> { status, html }

  function detailRowFor(id) {
    return document.querySelector(
      '#contactsTable tr.detail-row[data-for="' + Number(id) + '"]',
    );
  }

  function contactFacts(c) {
    const pairs = [
      ["Role", c.role],
      ["Qualification", c.qualification_rationale],
      ["Verification detail", c.verification_details],
      ["Found by query", c.search_query],
      ["Source page", c.source_url],
    ].filter(([, v]) => v !== null && v !== undefined && v !== "");
    if (!pairs.length) return "";
    return (
      '<dl class="why-facts">' +
      pairs
        .map(([k, v]) => {
          // Only an http(s) address becomes a link: escaping does not stop
          // a javascript: URL from running when it is clicked.
          const value =
            k === "Source page" && /^https?:\/\//i.test(String(v))
              ? '<a href="' +
                escapeHtml(v) +
                '" target="_blank" rel="noopener noreferrer">' +
                escapeHtml(v) +
                "</a>"
              : escapeHtml(v);
          return "<dt>" + escapeHtml(k) + "</dt><dd>" + value + "</dd>";
        })
        .join("") +
      "</dl>"
    );
  }

  function renderWhy(why) {
    const facts = (why.facts || [])
      .map(
        ([k, v]) =>
          "<dt>" + escapeHtml(k) + "</dt><dd>" + escapeHtml(v ?? "") + "</dd>",
      )
      .join("");
    const timeline = (why.timeline || [])
      .map(
        (e) =>
          "<li><time>" +
          escapeHtml(e.when) +
          "</time><span>" +
          escapeHtml(e.what) +
          "</span></li>",
      )
      .join("");
    return (
      '<p class="why-headline">' +
      escapeHtml(why.headline) +
      "</p>" +
      (facts ? '<dl class="why-facts">' + facts + "</dl>" : "") +
      (timeline
        ? '<h4 class="why-subhead">History, newest first</h4>' +
          '<ol class="why-timeline">' +
          timeline +
          "</ol>"
        : "")
    );
  }

  // The operator's language for a contact not yet emailed: Automatic (the
  // rule), Italian or English. A contact sent an email keeps that email's
  // language, so it gets a sentence instead of a control the server would
  // refuse. The values are fixed words; the id is a number.
  function languageControl(c) {
    if (c.language !== "it" && c.language !== "en") return "";
    if (c.language_locked) {
      return '<p class="muted lang-fixed">Language fixed by the first email</p>';
    }
    const chosen =
      c.language_choice === "it" || c.language_choice === "en"
        ? c.language_choice
        : "auto";
    const options = [
      ["auto", "Automatic"],
      ["it", "Italian"],
      ["en", "English"],
    ]
      .map(
        ([value, label]) =>
          '<option value="' +
          value +
          '"' +
          (value === chosen ? " selected" : "") +
          ">" +
          label +
          "</option>",
      )
      .join("");
    return (
      '<label class="lang-choice-label">Language ' +
      '<select class="lang-choice" data-id="' +
      Number(c.id) +
      '">' +
      options +
      "</select></label>"
    );
  }

  function setContactLanguage(select) {
    const id = Number(select.dataset.id);
    const c = contactsById[id] || {};
    const before =
      c.language_choice === "it" || c.language_choice === "en"
        ? c.language_choice
        : "auto";
    fetch("/api/contacts/" + id + "/language", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ language: select.value }),
    })
      .then((r) => r.json().then((data) => ({ ok: r.ok, data })))
      .then(({ ok, data }) => {
        if (!ok || data.error) {
          showToast(data.error || "Could not set the language", "error");
          select.value = before;
          return;
        }
        showToast(data.reason || "Language set", "success");
        loadContacts();
        if (openDetails.has(id)) fetchWhy(id);
      })
      .catch(() => {
        showToast("Could not reach the server", "error");
        select.value = before;
      });
  }

  const contactsTableEl = document.getElementById("contactsTable");
  if (contactsTableEl) {
    contactsTableEl.addEventListener("change", (event) => {
      const select = event.target.closest("select.lang-choice");
      if (select) setContactLanguage(select);
    });
    // One listener for every row button, so the markup carries no script
    // and the page's Content-Security-Policy can refuse inline handlers.
    contactsTableEl.addEventListener("click", (event) => {
      const btn = event.target.closest("button[data-action]");
      if (!btn || !contactsTableEl.contains(btn)) return;
      const id = Number(btn.dataset.id);
      switch (btn.dataset.action) {
        case "expand":
          toggleContactDetail(btn, id);
          break;
        case "client":
          markClient(id);
          break;
        case "unblock":
          unblockContact(id);
          break;
        case "send":
          contactAction(id, "send_now", {
            message: "Queued for the next send",
          });
          break;
        case "block":
          blockContact(id);
          break;
      }
    });
  }

  function fillDetail(id, whyHtml) {
    const tr = detailRowFor(id);
    if (!tr) return;
    const c = contactsById[id] || {};
    tr.querySelector(".detail-body").innerHTML =
      whyHtml + contactFacts(c) + languageControl(c);
  }

  function fetchWhy(id) {
    fetch("/api/contacts/" + Number(id) + "/why")
      .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
      .then((why) => {
        const entry = openDetails.get(id);
        if (!entry) return; // closed while loading
        entry.status = why.status;
        entry.html = renderWhy(why);
        fillDetail(id, entry.html);
      })
      .catch(() => {
        if (!openDetails.has(id)) return;
        fillDetail(
          id,
          '<p class="why-error">Could not load the explanation. ' +
            "It will be tried again on the next refresh.</p>",
        );
        openDetails.get(id).status = null; // refetch on the next rebuild
      });
  }

  function buildDetailRow(id, html) {
    const tr = document.createElement("tr");
    tr.className = "detail-row";
    tr.dataset.for = String(Number(id));
    tr.innerHTML =
      '<td colspan="' +
      tableParts("contactsTable").cols +
      '"><div class="detail-body"></div></td>';
    const c = contactsById[id] || {};
    tr.querySelector(".detail-body").innerHTML =
      html + contactFacts(c) + languageControl(c);
    return tr;
  }

  const LOADING_WHY =
    '<p class="muted">Loading why this contact is where it is...</p>';

  function toggleContactDetail(btn, id) {
    const row = btn.closest("tr");
    const existing = detailRowFor(id);
    if (existing) {
      existing.remove();
      openDetails.delete(id);
      btn.setAttribute("aria-expanded", "false");
      return;
    }
    const c = contactsById[id];
    if (!c) return;
    openDetails.set(id, { status: c.status, html: null });
    row.after(buildDetailRow(id, LOADING_WHY));
    btn.setAttribute("aria-expanded", "true");
    fetchWhy(id);
  }

  // After loadContacts rebuilds the body: put each open panel back under its
  // row, and refetch only for a contact whose status moved since. A contact
  // no longer on the page (filtered out, or gone) is forgotten.
  function restoreOpenDetails() {
    for (const [id, entry] of [...openDetails]) {
      const btn = document.querySelector(
        '#contactsTable button.act-expand[data-id="' + Number(id) + '"]',
      );
      const c = contactsById[id];
      if (!btn || !c) {
        openDetails.delete(id);
        continue;
      }
      btn.closest("tr").after(buildDetailRow(id, entry.html || LOADING_WHY));
      btn.setAttribute("aria-expanded", "true");
      if (!entry.html || c.status !== entry.status) fetchWhy(id);
    }
  }
  window.toggleContactDetail = toggleContactDetail;

  // Keyed by id so the detail panel can read fields the table does not render.
  let contactsById = {};

  const CONTACTS_PAGE_SIZE = 200;

  // The table shows one page, ranked by lead score. Saying so is the point:
  // a table that silently shows 200 of 1000 invites exactly the conclusion
  // that the 200 ARE the 1000.
  function clearContactsPageNote() {
    const el = document.getElementById("contactsPageNote");
    if (el) el.textContent = "";
  }

  function showContactsPageNote(shown) {
    const el = document.getElementById("contactsPageNote");
    if (!el) return;
    const want = statusFilter.contactsTable;
    const scope = want ? " " + want.replace(/_/g, " ") : "";
    // The totals come from /api/stats, a different request from the rows. If it
    // has not answered - or failed, and its .catch only logs - the counts are
    // still {}, which made `total` 0, made `shown < total` false, and printed
    // "Showing all 200 contacts" over a table that was one page of 826. An
    // unknown total is not a total; say how many are on screen and stop there.
    if (!statsLoaded) {
      el.textContent = `Showing ${shown}${scope} contact${shown === 1 ? "" : "s"}.`;
      return;
    }
    const total = want
      ? (contactStatusCounts || {})[want] || 0
      : Object.values(contactStatusCounts || {}).reduce((a, b) => a + b, 0);
    el.textContent =
      shown < total
        ? `Showing the top ${shown} of ${total}${scope} contacts by lead score.`
        : `Showing all ${shown}${scope} contact${shown === 1 ? "" : "s"}.`;
  }

  function loadContacts() {
    const want = statusFilter.contactsTable;
    const url =
      "/api/contacts?limit=" +
      CONTACTS_PAGE_SIZE +
      (want ? "&status=" + encodeURIComponent(want) : "");
    fetch(url)
      .then((r) => r.json())
      .then((data) => {
        const tbody = document.querySelector("#contactsTable tbody");
        let html = "";
        contactsById = {};
        data.forEach((c) => {
          contactsById[c.id] = c;
          const blocked = c.status === "blocked";
          const fit = c.fit_score ?? "";
          const band = fit === "" ? "" : fit >= 70 ? "good" : fit >= 45 ? "mid" : "low";
          html +=
            '<tr data-status="' +
            escapeHtml(c.status || "unknown") +
            '" data-sector="' +
            escapeHtml(c.sector || "") +
            '">' +
            '<td data-sort="' +
            escapeHtml(fit === "" ? -1 : fit) +
            '"><span class="fit-badge fit-' +
            band +
            '">' +
            escapeHtml(fit) +
            "</span></td>" +
            longCell(c.name || "N/A") +
            '<td class="cell-long" data-sort="' +
            escapeHtml(c.email) +
            '" title="' +
            escapeHtml(c.email) +
            '"><div class="cell-clip"><span class="clip-text">' +
            escapeHtml(c.email) +
            "</span>" +
            langBadge(c.language, c.language_reason) +
            "</div></td>" +
            longCell(c.organization || "N/A") +
            '<td data-sort="' +
            escapeHtml(c.status || "") +
            '">' +
            getBadge(c.status) +
            "</td>" +
            '<td class="cell-sector" title="' +
            escapeHtml(c.sector || "") +
            '"><div class="cell-clip"><span class="clip-text">' +
            escapeHtml(c.sector || "–") +
            "</span></div></td>" +
            "<td>" +
            // Icon-only, with a real accessible name: three word-buttons
            // pushed the Actions column off the right edge at 1440px.
            '<button class="action-btn act-expand" aria-expanded="false" ' +
            'data-id="' +
            c.id +
            '" ' +
            'aria-label="Show details" title="Details" ' +
            'data-action="expand"><i class="fa-solid fa-chevron-down" aria-hidden="true"></i></button>' +
            '<button class="action-btn client" data-action="client" data-id="' +
            c.id +
            '" title="Add this company to the active-client list so it ' +
            'is never cold-emailed">Client</button>' +
            (blocked
              ? '<button class="action-btn send" data-action="unblock" data-id="' +
                c.id +
                '">Unblock</button>'
              : '<button class="action-btn send" data-action="send" data-id="' +
                c.id +
                '">Send</button>' +
                // A closed row (replied, client, opted out, bounced, erased)
                // already receives nothing; Block there only overwrote why.
                (c.closed
                  ? ""
                  : '<button class="action-btn block" data-action="block" data-id="' +
                    c.id +
                    '">Block</button>')) +
            "</td></tr>";
        });
        if (html) {
          tbody.innerHTML = html;
          restoreOpenDetails();
          markLoaded("contactsTable", true);
          buildStatusChips("contactsTable", "contactStatusChips");
          // Deliberately NOT applyStatusFilter here: the server already
          // filtered, and calling it would re-enter loadContacts forever.
          applySort("contactsTable");
          reapplyFilter("contactsTable");
          showContactsPageNote(data.length);
        } else {
          markLoaded("contactsTable", false);
          clearContactsPageNote();
          showTableEmpty(
            "contactsTable",
            "fa-user-slash",
            "No contacts discovered yet",
            "Add a target query in the bar above, then start the agent - " +
              "discovered contacts appear here as they are found.",
          );
        }
      })
      .catch((e) => {
        console.warn("Contacts load error:", e);
        markLoaded("contactsTable", false);
        // The table swaps to its error state; without this the note above it
        // keeps describing the rows that used to be there ("Showing the top
        // 200 of 826 pending contacts") over a banner saying they could not
        // be loaded.
        clearContactsPageNote();
        showTableError("contactsTable", "contacts");
      });
  }

  function loadEmails() {
    fetch("/api/emails")
      .then((r) => r.json())
      .then((data) => {
        const tbody = document.querySelector("#emailsTable tbody");
        let html = "";
        data.forEach((e) => {
          html +=
            "<tr>" +
            "<td>" +
            escapeHtml(e.timestamp) +
            "</td>" +
            longCell(e.recipient_email) +
            (e.sender_account
              ? longCell(e.sender_account)
              : '<td class="cell-long">—</td>') +
            '<td class="cell-long" data-sort="' +
            escapeHtml(e.subject) +
            '" title="' +
            escapeHtml(e.subject) +
            '"><div class="cell-clip"><span class="clip-text">' +
            escapeHtml(e.subject) +
            "</span>" +
            langBadge(noticeLanguage(e.notice_version)) +
            (e.has_attachment
              ? ' <span class="file-badge" title="' +
                escapeHtml(e.attachment_filename || "Attached") +
                '">PDF</span>'
              : "") +
            "</div></td>" +
            '<td data-sort="' +
            escapeHtml(e.delivery_status || "") +
            '">' +
            getBadge(e.delivery_status, e.error_message || e.smtp_response) +
            "</td>" +
            '<td data-sort="' +
            (e.opened_at ? "1" : "0") +
            '">' +
            (e.opened_at ? "Opened" : "-") +
            "</td>" +
            "</tr>";
        });
        if (html) {
          tbody.innerHTML = html;
          markLoaded("emailsTable", true);
          applySort("emailsTable");
          reapplyFilter("emailsTable");
        } else {
          markLoaded("emailsTable", false);
          showTableEmpty(
            "emailsTable",
            "fa-envelope",
            "No emails sent yet",
            "Every send the agent makes is logged here, with its delivery " +
              "status and whether it was opened.",
          );
        }
      })
      .catch((e) => {
        console.warn("Emails load error:", e);
        markLoaded("emailsTable", false);
        showTableError("emailsTable", "campaign logs");
      });
  }

  function loadDeliverability() {
    const el = document.getElementById("deliverabilityPanel");
    if (!el) return;
    fetch("/api/deliverability")
      .then((r) => r.json())
      .then((d) => {
        const m = d.metrics || {},
          s = d.sending || {},
          w = d.send_window || {},
          g = d.guard || {};
        const pct = (x) => (x * 100).toFixed(2) + "%";
        const yn = (v) =>
          v === true
            ? '<span class="ok">✓</span>'
            : v === false
              ? '<span class="bad">✗</span>'
              : '<span class="muted">?</span>';
        const bounceBad =
          (m.bounce_rate || 0) >
          (d.thresholds ? d.thresholds.provider_bounce_limit : 0.02);
        const rows = [
          [
            "Sending address",
            escapeHtml(s.from_address || "—") +
              (s.personal_gmail
                ? ' <span class="bad">(personal inbox — use a dedicated domain)</span>'
                : ""),
          ],
          ["SPF", yn(s.spf)],
          ["DMARC", yn(s.dmarc)],
          ...(s.dns_note
            ? [["", '<span class="muted">' + escapeHtml(s.dns_note) + "</span>"]]
            : []),
          ["Emails sent", m.sent || 0],
          [
            "Bounce rate",
            '<span class="' +
              (bounceBad ? "bad" : "ok") +
              '">' +
              pct(m.bounce_rate || 0) +
              '</span> <span class="muted">(' +
              (m.bounced || 0) +
              " bounced; provider limit 2%)</span>",
          ],
          ["Unsubscribes", m.unsubscribed || 0],
          ["Suppressed total", m.suppressed || 0],
          [
            "Circuit breaker",
            g.tripped
              ? '<span class="bad">TRIPPED — sending paused</span>'
              : g.enabled
                ? '<span class="ok">armed</span>'
                : '<span class="muted">off</span>',
          ],
          [
            "Send window",
            w.enabled
              ? (w.open
                  ? '<span class="ok">open</span>'
                  : '<span class="muted">closed</span>') +
                ' <span class="muted">' +
                w.start +
                ":00–" +
                w.end +
                ":00, " +
                escapeHtml(w.now_local || "") +
                "</span>"
              : '<span class="muted">always on</span>',
          ],
          [
            "Profile on 1st email",
            s.attach_profile
              ? "attached (PDF)"
              : s.profile_url
                ? "linked"
                : '<span class="muted">none</span>',
          ],
          [
            "Blacklist status",
            (() => {
              const b = d.blacklist || {};
              if (b.status === "listed")
                return (
                  '<span class="bad">⚠ LISTED on ' +
                  escapeHtml((b.listed_on || []).join(", ")) +
                  '</span> <span class="muted">— delivery is being blocked; request removal</span>'
                );
              if (b.status === "clean")
                return (
                  '<span class="ok">clean</span> <span class="muted">(' +
                  b.checked +
                  "/" +
                  b.total +
                  " blocklists checked)</span>"
                );
              if (b.status === "partial")
                return (
                  '<span class="warn">no listing found</span> <span class="muted">(only ' +
                  b.checked +
                  " of " +
                  b.total +
                  " blocklists answered — not a full all-clear)</span>"
                );
              if (b.status === "off")
                return '<span class="muted">checking disabled</span>';
              return (
                '<span class="muted">could not check — your DNS resolver may be ' +
                "blocked by the blocklists</span>"
              );
            })(),
          ],
        ];
        el.innerHTML =
          '<table class="kv-table">' +
          rows
            .map(
              (r) =>
                '<tr><td class="k">' +
                r[0] +
                '</td><td class="v">' +
                r[1] +
                "</td></tr>",
            )
            .join("") +
          "</table>";
      })
      .catch((e) => {
        el.innerHTML = '<p class="muted">Unavailable</p>';
        console.warn("deliverability error:", e);
      });
  }

  function loadLeads() {
    const el = document.getElementById("leadsPanel");
    if (!el) return;
    fetch("/api/leads")
      .then((r) => r.json())
      .then((d) => {
        if (!d.configured) {
          el.innerHTML =
            '<p class="muted">Not set up yet — add the person\'s address as ' +
            "<strong>LEAD_ALERT_EMAIL</strong> in .env and they'll be emailed " +
            "automatically whenever a prospect replies with interest.</p>";
          return;
        }
        const who = (d.recipients || []).map(escapeHtml).join(", ");
        const rows = (d.alerts || [])
          .map(
            (a) =>
              "<tr><td>" +
              escapeHtml(a.timestamp || "") +
              "</td>" +
              "<td>" +
              escapeHtml(a.organization || "—") +
              "</td>" +
              "<td>" +
              escapeHtml(a.email) +
              "</td>" +
              '<td class="lead-quote">' +
              escapeHtml((a.reply_excerpt || "").slice(0, 120)) +
              "</td></tr>",
          )
          .join("");
        el.innerHTML =
          '<p class="muted" style="padding-top:0;">Alerts go to <strong>' +
          who +
          "</strong>.</p>" +
          '<div class="table-responsive"><table class="modern-table">' +
          "<thead><tr><th>When</th><th>Company</th><th>Email</th><th>Their reply</th></tr></thead>" +
          "<tbody>" +
          (rows || '<tr><td colspan="4">No interested replies yet</td></tr>') +
          "</tbody></table></div>";
      })
      .catch((e) => {
        el.innerHTML = '<p class="muted">Unavailable</p>';
        console.warn("leads error:", e);
      });
  }

  function loadInsights() {
    loadDeliverability();
    loadLeads();
    // Active feature flags
    fetch("/api/config")
      .then((r) => r.json())
      .then((data) => {
        const grid = document.getElementById("featuresGrid");
        if (!grid) return;
        const feats = data.features || {};
        grid.innerHTML = Object.keys(feats)
          .map((k) => {
            const on = feats[k];
            return (
              '<div class="feature-chip ' +
              (on ? "on" : "off") +
              '">' +
              '<span class="dot"></span>' +
              escapeHtml(k.replace(/_/g, " ")) +
              "</div>"
            );
          })
          .join("");
      })
      .catch((e) => console.warn("config error:", e));

    // A/B performance
    fetch("/api/abtest")
      .then((r) => r.json())
      .then((rows) => {
        const tb = document.querySelector("#abTable tbody");
        if (!tb) return;
        tb.innerHTML =
          rows && rows.length
            ? rows
                .map(
                  (r) =>
                    "<tr><td>" +
                    escapeHtml(r.variant) +
                    "</td><td>" +
                    escapeHtml(r.sent) +
                    "</td><td>" +
                    escapeHtml(r.opened) +
                    "</td><td>" +
                    escapeHtml(r.open_rate) +
                    "%</td></tr>",
                )
                .join("")
            : '<tr><td colspan="4">No sends yet</td></tr>';
      })
      .catch((e) => console.warn("abtest error:", e));

    // Sequence stats
    fetch("/api/sequences")
      .then((r) => r.json())
      .then((s) => {
        const el = document.getElementById("sequenceStats");
        if (!el) return;
        if (s.error) {
          el.innerHTML = '<p class="muted">Not available</p>';
          return;
        }
        el.innerHTML = ["active_enrollments", "completed", "cancelled", "total"]
          .map(
            (k) =>
              '<div class="mini-stat"><span class="num">' +
              escapeHtml(s[k] || 0) +
              "</span>" +
              '<span class="lbl">' +
              k.replace(/_/g, " ") +
              "</span></div>",
          )
          .join("");
      })
      .catch((e) => console.warn("sequences error:", e));

    // Per-domain sends
    fetch("/api/domains")
      .then((r) => r.json())
      .then((rows) => {
        const tb = document.querySelector("#domainTable tbody");
        if (!tb) return;
        if (rows && rows.error) {
          tb.innerHTML = '<tr><td colspan="4">Not available</td></tr>';
          return;
        }
        tb.innerHTML =
          rows && rows.length
            ? rows
                .map(
                  (r) =>
                    "<tr><td>" +
                    escapeHtml(r.domain) +
                    "</td><td>" +
                    escapeHtml(r.count) +
                    "</td><td>" +
                    escapeHtml(r.limit) +
                    "</td><td>" +
                    escapeHtml(r.remaining) +
                    "</td></tr>",
                )
                .join("")
            : '<tr><td colspan="4">No sends today</td></tr>';
      })
      .catch((e) => console.warn("domains error:", e));

    // Recent errors
    fetch("/api/errors")
      .then((r) => r.json())
      .then((rows) => {
        const el = document.getElementById("errorFeed");
        if (!el) return;
        if (!rows || !rows.length) {
          el.innerHTML = '<p class="muted">No recent errors</p>';
          return;
        }
        el.innerHTML = rows
          .map(
            (a) =>
              '<div class="error-item"><span class="err-time">' +
              escapeHtml(a.timestamp) +
              "</span>" +
              '<span class="err-action">' +
              escapeHtml(a.action) +
              "</span>" +
              '<span class="err-detail">' +
              escapeHtml(a.details || "") +
              "</span></div>",
          )
          .join("");
      })
      .catch((e) => console.warn("errors error:", e));

    loadRetries();
    loadLearning();
    loadConfig();
  }

  // ── Editable configuration (/api/config) ──────────────────────────────
  // Only the operational dials on the server's RUNTIME_EDITABLE allow-list are
  // rendered. The server re-checks that list on POST - this is presentation,
  // not the security boundary.
  const CONFIG_LABELS = {
    min_lead_score_to_email: "Minimum lead score to email",
    max_per_domain_per_day: "Max sends per domain per day",
    max_pending_backlog: "Pause discovery above this backlog",
    delay_between_emails: "Delay between emails (s)",
    send_jitter_seconds: "Random jitter added to that delay (s)",
    emails_per_hour: "Emails per hour",
    max_bounce_rate:
      "Pause all sending above this bounce rate (as a fraction: 0.05 = 5%)",
    bounce_guard_min_sends: "Sends needed before judging bounce rate",
    max_domain_bounce_rate:
      "Back off one domain above this bounce rate (as a fraction: 0.3 = 30%)",
    domain_bounce_min_sends: "Sends needed before judging a domain",
    send_window_start_hour: "Send window opens (hour)",
    send_window_end_hour: "Send window closes (hour)",
    send_window_days: "Send days (0=Mon … 6=Sun)",
    imap_check_interval_minutes: "Inbox check interval (min)",
  };

  // What the server last reported, so saveConfig can send only genuine
  // changes. Sending every field pinned all 14 into the database as
  // "overridden", which made the badge meaningless and meant a later .env
  // edit would silently stop taking effect.
  let configLoaded = {};

  function loadConfig() {
    const box = document.getElementById("configForm");
    if (!box) return;
    fetch("/api/config")
      .then((r) => r.json())
      .then((d) => {
        const current = d.current || {};
        configLoaded = { ...current };
        const overridden = new Set(d.overridden || []);
        box.innerHTML =
          (d.editable || [])
            .map((key) => {
              const v = current[key];
              return (
                '<label class="config-row' +
                (overridden.has(key) ? " is-overridden" : "") +
                '"><span class="config-label">' +
                escapeHtml(CONFIG_LABELS[key] || key.replace(/_/g, " ")) +
                (overridden.has(key)
                  ? '<span class="config-badge" title="Changed from the .env ' +
                    'value">overridden</span>'
                  : "") +
                '</span><input type="text" data-key="' +
                escapeHtml(key) +
                '" value="' +
                escapeHtml(v === null || v === undefined ? "" : v) +
                '" autocomplete="off" /></label>'
              );
            })
            .join("") +
          '<div class="config-actions">' +
          '<button type="button" class="btn-primary" id="configSave">' +
          '<i class="fa-solid fa-check" aria-hidden="true"></i> Save changes' +
          "</button>" +
          '<button type="button" class="btn-secondary" id="configReset">' +
          "Reset changed to .env</button></div>";

        box.querySelector("#configSave").addEventListener("click", saveConfig);
        box
          .querySelector("#configReset")
          .addEventListener("click", () => saveConfig(true));
      })
      .catch((e) => {
        console.warn("config error:", e);
        box.innerHTML = '<p class="muted">Could not load the settings.</p>';
      });
  }

  function saveConfig(reset) {
    const box = document.getElementById("configForm");
    const out = document.getElementById("configResult");
    const inputs = [...box.querySelectorAll("input[data-key]")];

    let payload;
    if (reset === true) {
      const changed = [
        ...box.querySelectorAll(".config-row.is-overridden input"),
      ];
      if (!changed.length) {
        out.className = "ms-result";
        out.textContent = "Nothing is overridden.";
        return;
      }
      payload = { reset: true, settings: {} };
      changed.forEach((i) => (payload.settings[i.dataset.key] = i.value));
    } else {
      payload = { settings: {} };
      inputs.forEach((i) => {
        // Numeric fields must go over as numbers: "0.3" would fail a float
        // validator as a string, and the server is strict on purpose.
        const raw = i.value.trim();
        const value = raw !== "" && !isNaN(Number(raw)) ? Number(raw) : raw;
        // Only genuine changes. Posting every field pinned all of them into
        // the database as overrides, which made the "overridden" badge
        // meaningless and meant a later .env edit silently stopped applying.
        if (String(value) !== String(configLoaded[i.dataset.key] ?? "")) {
          payload.settings[i.dataset.key] = value;
        }
      });
      if (!Object.keys(payload.settings).length) {
        out.className = "ms-result";
        out.textContent = "No changes to save.";
        return;
      }
    }

    out.className = "ms-result";
    out.textContent = "Saving…";
    fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
      .then((r) => r.json().then((data) => ({ ok: r.ok, data })))
      .then(({ ok, data }) => {
        if (!ok || data.error) {
          out.className = "ms-result err";
          out.textContent = data.error || "Could not save.";
          return;
        }
        out.className = "ms-result ok";
        out.textContent = reset === true ? "Reset to .env." : data.note;
        showToast("Settings saved");
        loadConfig();
      })
      .catch(() => {
        out.className = "ms-result err";
        out.textContent = "Could not reach the server.";
      });
  }

  // ── Live activity feed (/api/activity) ────────────────────────────────
  // The agent has written a running narrative to activity_log since day one
  // and the endpoint has always served it. Nothing on the front end ever
  // asked for it, so the only way to watch a run was to tail the log file.
  const ACTIVITY_STATUS = {
    error: "act-error",
    warning: "act-warn",
    success: "act-ok",
  };

  function loadActivity() {
    const el = document.getElementById("activityFeed");
    if (!el) return;
    // Preserve the reading position when the operator has scrolled up to look
    // at something; only autoscroll when they are following the tail.
    const follow = document.getElementById("activityFollow");
    const following = !follow || follow.checked;

    fetch("/api/activity?limit=50")
      .then((r) => r.json())
      .then((rows) => {
        if (!rows || !rows.length) {
          el.innerHTML =
            '<div class="empty-state"><i class="fa-solid fa-wave-square" ' +
            'aria-hidden="true"></i><span class="empty-title">No activity yet' +
            '</span><span class="empty-hint">Start the agent and its progress ' +
            "will stream here — searches run, contacts scored, mail sent.</span></div>";
          return;
        }
        // The API returns newest first; a feed you read top-to-bottom wants
        // oldest first, like a log.
        el.innerHTML = rows
          .slice()
          .reverse()
          .map(
            (a) =>
              '<div class="act-row ' +
              (ACTIVITY_STATUS[a.status] || "act-info") +
              '"><span class="act-time">' +
              escapeHtml(a.timestamp || "") +
              '</span><span class="act-action">' +
              escapeHtml(a.action) +
              '</span><span class="act-detail">' +
              escapeHtml(a.details || "") +
              "</span></div>",
          )
          .join("");
        if (following) el.scrollTop = el.scrollHeight;
      })
      .catch((e) => {
        console.warn("activity error:", e);
        if (!el.querySelector(".act-row")) {
          el.innerHTML =
            '<div class="empty-state is-error"><i class="fa-solid ' +
            'fa-plug-circle-exclamation" aria-hidden="true"></i>' +
            '<span class="empty-title">Could not load activity</span></div>';
        }
      });
  }

  // ── Retry queue (/api/retries) ────────────────────────────────────────
  // The endpoint has always reported the queue; nothing displayed it, so a
  // backlog of sends waiting on a transient failure was invisible unless you
  // went reading the log.
  function loadRetries() {
    const el = document.getElementById("retryStats");
    const note = document.getElementById("retryNote");
    if (!el) return;
    fetch("/api/retries")
      .then((r) => r.json())
      .then((s) => {
        if (s.error) {
          el.innerHTML = '<p class="muted">Not available</p>';
          if (note) note.textContent = "";
          return;
        }
        el.innerHTML = [
          "pending",
          "succeeded",
          "exhausted",
          "cancelled",
          "total",
        ]
          .map(
            (k) =>
              '<div class="mini-stat"><span class="num' +
              // A non-zero exhausted count means sends were given up on; it
              // should not read the same as "0 exhausted".
              (k === "exhausted" && s[k] ? " num-bad" : "") +
              (k === "pending" && s[k] ? " num-warn" : "") +
              '">' +
              escapeHtml(s[k] || 0) +
              "</span>" +
              '<span class="lbl">' +
              k +
              "</span></div>",
          )
          .join("");
        if (note) {
          note.textContent = s.exhausted
            ? s.exhausted +
              " send" +
              (s.exhausted === 1 ? " has" : "s have") +
              " used every attempt and will not be tried again."
            : s.pending
              ? s.pending +
                " send" +
                (s.pending === 1 ? " is" : "s are") +
                " queued for another attempt."
              : "Nothing waiting to be retried.";
        }
      })
      .catch((e) => {
        console.warn("retries error:", e);
        el.innerHTML = '<p class="muted">Could not load the retry queue</p>';
        if (note) note.textContent = "";
      });
  }

  // ── Learning snapshot (/api/learning) ─────────────────────────────────
  // This steers query generation. Seeing it is the difference between "the
  // agent is searching" and knowing what it has decided is worth searching.
  function loadLearning() {
    const el = document.getElementById("learningPanel");
    if (!el) return;
    fetch("/api/learning")
      .then((r) => r.json())
      .then((d) => {
        if (d.error) {
          el.innerHTML = '<p class="muted">Not available</p>';
          return;
        }
        const rank = (rows, kind) => {
          if (!rows || !rows.length)
            return (
              '<p class="muted">No ' +
              kind +
              " scored yet — the agent needs a few completed searches first.</p>"
            );
          const max = Math.max(...rows.map((r) => r.weight || 0), 0.0001);
          return (
            '<ul class="rank-list">' +
            rows
              .map(
                (r) =>
                  '<li class="rank-row"><span class="rank-name">' +
                  escapeHtml(r.name) +
                  "</span>" +
                  '<span class="rank-bar"><span class="rank-fill" style="width:' +
                  Math.round(((r.weight || 0) / max) * 100) +
                  '%"></span></span>' +
                  '<span class="rank-meta">' +
                  escapeHtml(r.verified || 0) +
                  " verified · " +
                  escapeHtml(r.responded || 0) +
                  " replied · " +
                  escapeHtml(r.searches || 0) +
                  " searches</span></li>",
              )
              .join("") +
            "</ul>"
          );
        };
        el.innerHTML =
          '<div class="learning-cols">' +
          '<div><h4 class="panel-sub">Best verticals</h4>' +
          rank(d.top_verticals, "verticals") +
          "</div>" +
          '<div><h4 class="panel-sub">Best countries</h4>' +
          rank(d.top_countries, "countries") +
          "</div></div>" +
          (d.avoid && d.avoid.length
            ? '<div class="learning-avoid"><h4 class="panel-sub">Backed off</h4>' +
              '<div class="features-grid">' +
              d.avoid
                .map(
                  (a) =>
                    '<span class="feature-chip off"><span class="dot"></span>' +
                    escapeHtml(typeof a === "string" ? a : a.name || "") +
                    "</span>",
                )
                .join("") +
              "</div>" +
              '<p class="panel-note">Searched repeatedly and returned nothing, ' +
              "so the agent skips them - with an occasional re-probe, in case " +
              "the market changes.</p></div>"
            : "");
      })
      .catch((e) => {
        console.warn("learning error:", e);
        el.innerHTML =
          '<p class="muted">Could not load the learning snapshot</p>';
      });
  }

  // ── On-demand blocklist re-check (/api/blacklist) ─────────────────────
  // The deliverability panel shows a CACHED verdict. This endpoint clears the
  // cache, re-queries, and returns the per-list breakdown - which is the only
  // way to see WHICH blocklist is the problem. Neither was reachable from
  // the UI before.
  function recheckBlacklist() {
    const btn = document.getElementById("recheckBlacklist");
    const out = document.getElementById("blacklistDetail");
    if (!out) return;
    const restore = btn ? btn.innerHTML : "";
    if (btn) {
      btn.disabled = true;
      btn.innerHTML =
        '<i class="fa-solid fa-spinner fa-spin" aria-hidden="true"></i> Checking…';
    }
    out.innerHTML = '<p class="muted">Querying blocklists…</p>';

    fetch("/api/blacklist")
      .then((r) => r.json())
      .then((d) => {
        if (d.status === "off") {
          out.innerHTML =
            '<p class="muted">Blocklist checking is disabled in the ' +
            "configuration, so there is nothing to re-check.</p>";
          return;
        }
        const results = d.results || [];
        if (!results.length) {
          out.innerHTML =
            '<p class="muted">No blocklist answered. Your DNS resolver may ' +
            "itself be blocked by them — this is not an all-clear.</p>";
          return;
        }
        const cls = { listed: "bad", clean: "ok", unavailable: "muted" };
        out.innerHTML =
          '<p class="panel-note">Fresh lookup for <strong>' +
          escapeHtml(d.domain || "the sending domain") +
          "</strong> — " +
          escapeHtml(d.checked || 0) +
          " of " +
          escapeHtml(d.total || results.length) +
          " blocklists answered.</p>" +
          '<table class="kv-table">' +
          results
            .map(
              (r) =>
                '<tr><td class="k">' +
                escapeHtml(r.list) +
                '</td><td class="v"><span class="' +
                (cls[r.status] || "muted") +
                '">' +
                escapeHtml(r.status) +
                "</span>" +
                (r.detail ? " " + escapeHtml(r.detail) : "") +
                "</td></tr>",
            )
            .join("") +
          "</table>";
        // A fresh verdict makes the cached summary above it stale.
        loadDeliverability();
      })
      .catch((e) => {
        console.warn("blacklist error:", e);
        out.innerHTML =
          '<p class="muted">Could not reach the server for a re-check.</p>';
      })
      .finally(() => {
        if (btn) {
          btn.disabled = false;
          btn.innerHTML = restore;
        }
      });
  }

  function loadSearches() {
    fetch("/api/searches")
      .then((r) => r.json())
      .then((data) => {
        const tbody = document.querySelector("#searchesTable tbody");
        let html = "";
        data.forEach((s) => {
          html +=
            "<tr>" +
            "<td>" +
            escapeHtml(s.timestamp || "") +
            "</td>" +
            "<td>" +
            escapeHtml(s.search_query) +
            "</td>" +
            "<td>" +
            escapeHtml(s.source) +
            "</td>" +
            '<td data-sort="' +
            escapeHtml(s.results_count || 0) +
            '">' +
            escapeHtml(s.results_count || 0) +
            "</td>" +
            '<td data-sort="' +
            escapeHtml(s.urls_scraped || 0) +
            '">' +
            escapeHtml(s.urls_scraped || 0) +
            "</td>" +
            '<td data-sort="' +
            escapeHtml(s.status || "") +
            '">' +
            getBadge(s.status) +
            "</td>" +
            "</tr>";
        });
        if (html) {
          tbody.innerHTML = html;
          markLoaded("searchesTable", true);
          applySort("searchesTable");
          reapplyFilter("searchesTable");
        } else {
          markLoaded("searchesTable", false);
          showTableEmpty(
            "searchesTable",
            "fa-magnifying-glass",
            "No searches run yet",
            "Queries you add in the bar above are listed here once the " +
              "agent has run them.",
          );
        }
      })
      .catch((e) => {
        console.warn("Searches load error:", e);
        markLoaded("searchesTable", false);
        showTableError("searchesTable", "search logs");
      });
  }

  // ── Column sorting ────────────────────────────────────────────────────
  // The tables arrive in one fixed server order (newest first) and there was
  // no way to change it. "Which lead scored highest" and "what failed most
  // recently" both meant reading every row.

  // Sorts on the cell's data-sort attribute when present, so a score of "9"
  // and "88" compare as numbers and a date compares on its raw value rather
  // than its rendered text. Rows with no data-sort fall back to text.
  function cellKey(tr, col) {
    const td = tr.children[col];
    if (!td) return "";
    const raw = td.dataset.sort;
    return raw !== undefined ? raw : td.textContent.trim();
  }

  function sortTable(tableId, col, dir) {
    const parts = tableParts(tableId);
    if (!parts) return;
    // Direct children only, and not the detail panels: a descendant query
    // also picked up rows nested inside a panel, and sorting a panel as if
    // it were data tore it away from its contact (and its text turned the
    // score column into a text sort).
    const rows = [...parts.tbody.children].filter(
      (tr) =>
        !tr.querySelector(".state-cell") &&
        !tr.classList.contains("detail-row"),
    );
    if (!rows.length) return;
    const panelOf = new Map();
    rows.forEach((tr) => {
      const next = tr.nextElementSibling;
      if (next && next.classList.contains("detail-row")) panelOf.set(tr, next);
    });

    const numeric = rows.every((tr) => {
      const v = cellKey(tr, col);
      return v === "" || !isNaN(Number(v));
    });
    rows.sort((a, b) => {
      const x = cellKey(a, col);
      const y = cellKey(b, col);
      const cmp = numeric
        ? Number(x || 0) - Number(y || 0)
        : x.localeCompare(y, undefined, { numeric: true, sensitivity: "base" });
      return dir === "desc" ? -cmp : cmp;
    });
    rows.forEach((tr) => {
      parts.tbody.appendChild(tr);
      if (panelOf.has(tr)) parts.tbody.appendChild(panelOf.get(tr));
    });

    // A state row (empty / no-match) must stay at the bottom, not get sorted
    // into the middle of the data.
    parts.tbody
      .querySelectorAll("tr:has(.state-cell)")
      .forEach((tr) => parts.tbody.appendChild(tr));
  }

  // Remembered per table so a poll refresh does not silently reset the order
  // the operator chose.
  const sortState = {};

  function applySort(tableId) {
    const s = sortState[tableId];
    if (s) sortTable(tableId, s.col, s.dir);
  }

  function setSort(tableId, th, col) {
    const prev = sortState[tableId];
    const dir = prev && prev.col === col && prev.dir === "asc" ? "desc" : "asc";
    sortState[tableId] = { col, dir };

    const table = document.getElementById(tableId);
    table.querySelectorAll("thead th").forEach((h) => {
      h.setAttribute("aria-sort", "none");
      h.classList.remove("sorted-asc", "sorted-desc");
    });
    th.setAttribute("aria-sort", dir === "asc" ? "ascending" : "descending");
    th.classList.add(dir === "asc" ? "sorted-asc" : "sorted-desc");
    sortTable(tableId, col, dir);
  }

  document.querySelectorAll(".modern-table.sortable").forEach((table) => {
    table.querySelectorAll("thead th").forEach((th, col) => {
      if (th.dataset.nosort !== undefined) return;
      th.classList.add("th-sortable");
      th.tabIndex = 0;
      th.setAttribute("role", "columnheader");
      th.setAttribute("aria-sort", "none");
      th.addEventListener("click", () => setSort(table.id, th, col));
      th.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          setSort(table.id, th, col);
        }
      });
    });
  });

  // ── Status filter chips ───────────────────────────────────────────────
  // The contacts table lists every contact whatever its state, so the
  // discards the agent made - low score, off-target, failed verification -
  // were buried among the ones it emailed.
  const statusFilter = { contactsTable: null };

  // Per-status totals for the whole contacts table, from /api/stats (which the
  // dashboard already polls). Populated by loadStats.
  let contactStatusCounts = {};
  // Whether that has actually happened. An empty object is indistinguishable
  // from "every count is zero", and the page note used to read it as the
  // latter, so a stats failure produced a confident wrong total.
  let statsLoaded = false;

  function applyStatusFilter(tableId) {
    // Server-side. Hiding rows locally could only ever filter the page already
    // fetched, so picking "pending" over 1000 contacts offered the 41 pending
    // rows that happened to be in the top 200 by lead score - and presented
    // them as the whole set. The other 396 were unreachable from the UI.
    if (tableId === "contactsTable") {
      loadContacts();
      return;
    }
    const want = statusFilter[tableId];
    document.querySelectorAll("#" + tableId + " tbody tr").forEach((tr) => {
      if (tr.querySelector(".state-cell")) return;
      tr.dataset.statusHidden = want && tr.dataset.status !== want ? "1" : "";
    });
    reapplyFilter(tableId);
  }

  function buildStatusChips(tableId, containerId) {
    const box = document.getElementById(containerId);
    if (!box) return;
    // TRUE totals from the database, not a tally of the rendered rows.
    // /api/contacts returns one page (200 by default) ordered by lead score,
    // so counting what is on screen produced a count of the PAGE wearing the
    // clothes of a total: the chip said "pending 41" while the Overview said
    // 437, for the same quantity, on the same screen.
    const counts = { ...(contactStatusCounts || {}) };
    const total = Object.values(counts).reduce((a, b) => a + b, 0);
    if (!total) {
      box.innerHTML = "";
      box.dataset.sig = "";
      return;
    }

    // loadContacts runs on every poll. Re-rendering identical chips each time
    // would replace the DOM node under a keyboard user's focus every few
    // seconds, so the markup is only rebuilt when the counts actually move.
    const sig = JSON.stringify(counts);
    if (box.dataset.sig === sig) {
      markActiveChip(box, tableId);
      return;
    }
    box.dataset.sig = sig;
    const chip = (value, label, n) =>
      '<button type="button" class="filter-chip' +
      (statusFilter[tableId] === value ? " on" : "") +
      '" data-status="' +
      escapeHtml(value === null ? "" : value) +
      '" aria-pressed="' +
      (statusFilter[tableId] === value) +
      '">' +
      escapeHtml(label) +
      '<span class="chip-count">' +
      n +
      "</span></button>";

    box.innerHTML =
      chip(null, "All", total) +
      Object.keys(counts)
        .sort()
        .map((s) => chip(s, s.replace(/_/g, " "), counts[s]))
        .join("");

    box.querySelectorAll(".filter-chip").forEach((b) => {
      b.addEventListener("click", () => {
        const v = b.dataset.status || null;
        statusFilter[tableId] = statusFilter[tableId] === v ? null : v;
        // Repaint the pressed state in place. Re-rendering the chip HTML here
        // would destroy the element that was just activated, dropping keyboard
        // focus back to the top of the document mid-interaction.
        markActiveChip(box, tableId);
        applyStatusFilter(tableId);
      });
    });
    markActiveChip(box, tableId);
  }

  function markActiveChip(box, tableId) {
    box.querySelectorAll(".filter-chip").forEach((b) => {
      const on = (b.dataset.status || null) === statusFilter[tableId];
      b.classList.toggle("on", on);
      b.setAttribute("aria-pressed", String(on));
    });
  }

  // ── Table search / filter ──
  function filterTable(tableId, query) {
    const q = (query || "").trim().toLowerCase();
    const rows = document.querySelectorAll("#" + tableId + " > tbody > tr");
    let shown = 0;
    let dataRows = 0;
    rows.forEach((tr) => {
      // The empty/error/skeleton row is not data; filtering it would hide the
      // very message explaining why there is nothing to filter.
      if (tr.querySelector(".state-cell")) return;
      // A detail panel is not a row of its own: it follows its contact.
      if (tr.classList.contains("detail-row")) return;
      // A status chip and the search box are two filters over one set of rows.
      // They have to AND together, so the chip records its verdict on the row
      // and the search honours it rather than overwriting display outright.
      if (tr.dataset.statusHidden === "1") {
        tr.style.display = "none";
        return;
      }
      dataRows++;
      const match = !q || tr.textContent.toLowerCase().includes(q);
      tr.style.display = match ? "" : "none";
      if (match) shown++;
    });
    rows.forEach((tr) => {
      if (!tr.classList.contains("detail-row")) return;
      const parent = tr.previousElementSibling;
      tr.style.display =
        parent && parent.style.display === "none" ? "none" : "";
    });

    // A filter matching nothing produced the same blank box as an empty table.
    const noMatch = document.getElementById(tableId + "NoMatch");
    if (noMatch) noMatch.remove();
    if (q && dataRows > 0 && shown === 0) {
      const parts = tableParts(tableId);
      const tr = document.createElement("tr");
      tr.id = tableId + "NoMatch";
      tr.innerHTML =
        '<td class="state-cell" colspan="' +
        parts.cols +
        '"><div class="empty-state">' +
        '<i class="fa-solid fa-filter-circle-xmark" aria-hidden="true"></i>' +
        '<span class="empty-title">No rows match "' +
        escapeHtml(query) +
        '"</span>' +
        '<span class="empty-hint">' +
        dataRows +
        " row" +
        (dataRows === 1 ? "" : "s") +
        " hidden by this search.</span></div></td>";
      parts.tbody.appendChild(tr);
    }
  }
  function reapplyFilter(tableId) {
    const inp = document.querySelector(
      '.table-search input[data-table="' + tableId + '"]',
    );
    // Must run even with an empty search box. It used to early-return when the
    // input was blank, which was harmless while text was the only filter -
    // but the status chips also route through here, so selecting a chip set
    // the rows' hidden flag and then never applied it. The chip lit up and
    // the table did not change.
    filterTable(tableId, inp ? inp.value : "");
  }
  document.querySelectorAll(".table-search input").forEach((inp) => {
    inp.addEventListener("input", () =>
      filterTable(inp.dataset.table, inp.value),
    );
  });

  // Refresh buttons
  document
    .getElementById("refreshContacts")
    .addEventListener("click", loadContacts);
  document
    .getElementById("refreshEmails")
    .addEventListener("click", loadEmails);
  document
    .getElementById("refreshSearches")
    .addEventListener("click", loadSearches);
  document
    .getElementById("refreshInsights")
    .addEventListener("click", loadInsights);
  const refreshDeliv = document.getElementById("refreshDeliverability");
  if (refreshDeliv) refreshDeliv.addEventListener("click", loadDeliverability);
  const refreshLeadsBtn = document.getElementById("refreshLeads");
  if (refreshLeadsBtn) refreshLeadsBtn.addEventListener("click", loadLeads);
  const refreshActivityBtn = document.getElementById("refreshActivity");
  if (refreshActivityBtn)
    refreshActivityBtn.addEventListener("click", loadActivity);
  const refreshRetriesBtn = document.getElementById("refreshRetries");
  if (refreshRetriesBtn)
    refreshRetriesBtn.addEventListener("click", loadRetries);
  const refreshLearningBtn = document.getElementById("refreshLearning");
  if (refreshLearningBtn)
    refreshLearningBtn.addEventListener("click", loadLearning);
  const refreshConfigBtn = document.getElementById("refreshConfig");
  if (refreshConfigBtn) refreshConfigBtn.addEventListener("click", loadConfig);
  const recheckBlBtn = document.getElementById("recheckBlacklist");
  if (recheckBlBtn) recheckBlBtn.addEventListener("click", recheckBlacklist);
  const checkReadinessBtn = document.getElementById("checkReadiness");
  if (checkReadinessBtn)
    checkReadinessBtn.addEventListener("click", loadReadiness);

  // Template Editor Logic
  const editor = document.getElementById("templateEditor");
  const preview = document.getElementById("templatePreview");
  const saveBtn = document.getElementById("saveTemplateBtn");

  // What the send path puts in each placeholder, from GET /api/template:
  // the installation's own sender and a sample recipient, already escaped.
  // Filled in one pass, as the send fills them; a placeholder the send does
  // not know is marked, since the email would carry it as written.
  let previewValues = {};

  // srcdoc, not document.write: the frame is sandboxed without same-origin,
  // so the page cannot reach into it, and the template cannot reach out.
  function updatePreview() {
    // As the send path does, a value that ends in a full stop of its own
    // ("Sample Brand S.r.l.") takes the place of the template's, so the
    // preview reads "S.r.l." where the email will, not "S.r.l..".
    const html = editor.value.replace(
      /\{\{(\w+)\}\}(\.?)/g,
      (_m, key, stop) => {
        if (!Object.prototype.hasOwnProperty.call(previewValues, key))
          return "[unknown placeholder: " + escapeHtml(key) + "]" + stop;
        const value = String(previewValues[key]);
        return value.endsWith(".") ? value : value + stop;
      },
    );
    preview.srcdoc = html;
  }

  // Guards the save path: an editor that never loaded looks identical to an
  // empty one, and saving that truncates email_template.html.
  let templateLoaded = false;

  function setSaveEnabled(enabled) {
    if (!saveBtn) return;
    saveBtn.disabled = !enabled;
    saveBtn.title = enabled
      ? ""
      : "Template has not loaded - reload the page before saving.";
  }

  // Which text the editor holds: the first email or a follow-up, in English
  // or in the Italian Italian prospects read. savedContent is what the server
  // last gave or took, so a change of text can ask before dropping edits.
  const templateSelect = document.getElementById("templateName");
  const templateNote = document.getElementById("templateNote");
  let templateName = "email_template";
  let savedContent = "";

  function templateLabel(name) {
    const opt = [...templateSelect.options].find((o) => o.value === name);
    return opt ? opt.textContent : name;
  }

  function loadTemplate() {
    const name = templateSelect.value;
    templateLoaded = false;
    setSaveEnabled(false);
    fetch("/api/template?name=" + encodeURIComponent(name))
      .then((r) => r.json())
      .then((data) => {
        if (typeof data.content === "string") {
          templateName = name;
          editor.value = data.content;
          savedContent = data.content;
          previewValues = data.preview || {};
          templateNote.textContent = !data.missing
            ? ""
            : name.endsWith("_it")
              ? "Missing - Italian recipients get the English text"
              : "Missing - this step sends the English first email instead";
          editor.placeholder = data.missing
            ? "This text does not exist yet - write it here and save to create it."
            : "";
          updatePreview();
          templateLoaded = true;
          setSaveEnabled(true);
        } else {
          throw new Error(data.error || "No content returned");
        }
      })
      .catch((e) => {
        console.warn("Template load error:", e);
        editor.value = "";
        editor.placeholder =
          "Could not load the template. Reload the page - do not save, it would overwrite the stored template.";
        showToast("Could not load template - saving is disabled", "error");
      });
  }

  editor.addEventListener("input", updatePreview);

  templateSelect.addEventListener("change", () => {
    if (
      templateLoaded &&
      editor.value !== savedContent &&
      !window.confirm(
        templateLabel(templateName) +
          " has changes that are not saved. Open " +
          templateLabel(templateSelect.value) +
          " and lose them?",
      )
    ) {
      templateSelect.value = templateName;
      return;
    }
    loadTemplate();
  });

  saveBtn.addEventListener("click", () => {
    const content = editor.value;
    const name = templateName;
    if (!templateLoaded) {
      showToast(
        "Template never loaded - reload the page before saving",
        "error",
      );
      return;
    }
    if (!content.trim()) {
      showToast("Refusing to save an empty template", "error");
      return;
    }
    saveBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Saving...';

    fetch("/api/template", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, content }),
    })
      .then((r) => r.json().then((data) => ({ ok: r.ok, data })))
      .then(({ ok, data }) => {
        if (ok && data.status === "ok") {
          if (name === templateName) {
            savedContent = content;
            templateNote.textContent = "";
          }
          showToast(templateLabel(name) + " saved");
        } else {
          showToast(data.error || "Error saving template", "error");
        }
        saveBtn.innerHTML = '<i class="fa-solid fa-save"></i> Save Template';
      })
      .catch(() => {
        showToast("Network error", "error");
        saveBtn.innerHTML = '<i class="fa-solid fa-save"></i> Save Template';
      });
  });

  // Initial Load. The skeletons go up first so the tables read as "fetching"
  // for the moment before the responses land, rather than as "empty".
  showTableSkeleton("contactsTable");
  showTableSkeleton("emailsTable");
  showTableSkeleton("searchesTable");
  loadStats();
  loadCharts();
  loadReadiness();
  loadContacts();
  loadEmails();
  loadSearches();
  loadActivity();

  // Adaptive polling: refresh every 5s while the agent is running,
  // and back off to every 15s when idle to reduce server load.
  function pollTick() {
    loadStats();
    if (chartsAreStale()) loadCharts();
    if (readinessIsStale()) loadReadiness();
    if (document.getElementById("tab-overview").classList.contains("active"))
      loadActivity();
    if (document.getElementById("tab-contacts").classList.contains("active"))
      loadContacts();
    if (document.getElementById("tab-emails").classList.contains("active"))
      loadEmails();
    if (document.getElementById("tab-searches").classList.contains("active"))
      loadSearches();
    if (document.getElementById("tab-insights").classList.contains("active"))
      loadInsights();
    // Counts only: a reviewer mid-edit must not lose the draft to a refresh.
    loadReviewSummary();
    scheduleNextPoll();
  }

  function scheduleNextPoll() {
    const interval = agentRunning ? 5000 : 15000;
    setTimeout(pollTick, interval);
  }


  // ── Review: the agent proposes, a person decides ──
  // The server enforces every rule (modules/review_queue.py): an item must be
  // opened before it is approved, a rejection needs a reason, the footer and
  // the opt-out link stay. The page makes the same rules visible first.
  const review = {
    view: "waiting",
    items: [],
    current: null,
    original: { subject: "", body: "", text: "" },
    htmlMode: false,
    isAdmin: false,
    pendingLevel: null,
  };
  const $ = (id) => document.getElementById(id);

  function reviewPost(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    }).then((res) => res.json().then((data) => ({ ok: res.ok, data })));
  }

  // ── The draft as paragraphs of plain text ──
  // The proposal is HTML. Its own paragraphs are edited as text; everything
  // else - the layout, the opt-out line, the footer placeholder - is kept.
  function isFullDocument(html) {
    return /<html[\s>]/i.test(html || "");
  }

  function editableParagraphs(doc) {
    return [...doc.body.querySelectorAll("p")].filter(
      (p) => !p.querySelector("a") && !/\{\{\w+\}\}/.test(p.textContent)
    );
  }

  function paragraphText(p) {
    const parts = [];
    p.childNodes.forEach((n) => {
      if (n.nodeName === "BR") parts.push("\n");
      else parts.push(n.textContent);
    });
    return parts
      .join("")
      .split("\n")
      .map((l) => l.replace(/\s+/g, " ").trim())
      .join("\n")
      .trim();
  }

  function draftToText(html) {
    const doc = new DOMParser().parseFromString(html || "", "text/html");
    return editableParagraphs(doc).map(paragraphText).filter(Boolean).join("\n\n");
  }

  function textToDraft(html, text) {
    const doc = new DOMParser().parseFromString(html || "", "text/html");
    const old = editableParagraphs(doc);
    if (!old.length) return html;
    const anchor = old[0];
    const parent = anchor.parentNode;
    const styles = old.map((p) => p.getAttribute("style"));
    text
      .split(/\n\s*\n/)
      .map((t) => t.trim())
      .filter(Boolean)
      .forEach((para, i) => {
        const p = doc.createElement("p");
        const style = styles[Math.min(i, styles.length - 1)];
        if (style) p.setAttribute("style", style);
        para.split("\n").forEach((line, j) => {
          if (j) p.appendChild(doc.createElement("br"));
          p.appendChild(doc.createTextNode(line));
        });
        parent.insertBefore(p, anchor);
      });
    old.forEach((p) => p.remove());
    return isFullDocument(html)
      ? "<!DOCTYPE html>\n" + doc.documentElement.outerHTML
      : doc.body.innerHTML;
  }

  function currentBody() {
    if (review.htmlMode) return $("rvBody").value;
    const text = $("rvText").value;
    if (text === review.original.text) return review.original.body;
    return textToDraft(review.original.body, text);
  }

  function textOfHtml(html) {
    const doc = new DOMParser().parseFromString(html || "", "text/html");
    doc.querySelectorAll("script, style").forEach((n) => n.remove());
    return (doc.body.textContent || "")
      .split("\n")
      .map((line) => line.replace(/\s+/g, " ").trim())
      .filter(Boolean)
      .join("\n\n");
  }

  function updateReviewPreview() {
    $("rvPreview").textContent = textOfHtml(
      currentBody()
        .replace(/\{\{legal_footer\}\}/g, "[Omega's legal details and privacy notice]")
        .replace(/\{\{tracking_pixel\}\}/g, "")
    );
  }

  // ── Counts: badge, queue tabs and the overview's "Needs you" ──
  function renderCounts(counts) {
    if (!counts) return;
    const set = (id, n) => {
      const el = $(id);
      if (el) el.textContent = n ? String(n) : "";
    };
    set("reviewCount", counts.waiting);
    set("qnWaiting", counts.waiting);
    set("qnApproved", counts.approved);
    const ny = (id, n) => {
      const el = $(id);
      if (el) el.textContent = String(n || 0);
    };
    ny("nyWaiting", counts.waiting - counts.replies_waiting);
    ny("nyReplies", counts.replies_waiting);
    ny("nyApproved", counts.approved);
    document
      .querySelectorAll(".needs-list li")
      .forEach((li) =>
        li.classList.toggle("is-zero", li.querySelector(".needs-count").textContent === "0")
      );
    const note = $("nyNote");
    if (note)
      note.textContent = counts.waiting
        ? ""
        : "Nothing waits for you. The agent keeps looking; new proposals appear here.";
  }

  function loadReviewSummary() {
    fetch("/api/review/summary")
      .then((r) => r.json())
      .then((d) => renderCounts(d.counts))
      .catch(() => {});
  }

  function renderReviewMetrics(m) {
    const box = $("reviewMetrics");
    if (!m) return;
    const pct = (x) => (x === null || x === undefined ? "–" : `${Math.round(x * 100)}%`);
    const secs = m.median_seconds_to_decide;
    const time =
      secs === null || secs === undefined
        ? "–"
        : secs < 90
          ? `${Math.round(secs)} s`
          : `${Math.round(secs / 60)} min`;
    // Pairs, read together: a rising approval rate means better drafts only
    // if the share approved without edits does not fall with it.
    box.innerHTML = [
      ["Decided", String(m.decided)],
      ["Approved", pct(m.approval_rate)],
      ["Approved without edits", pct(m.approved_without_edits)],
      ["Median time to decide", time],
    ]
      .map(
        ([k, v]) =>
          `<div class="metric"><span class="metric-v">${escapeHtml(v)}</span><span class="metric-k">${escapeHtml(k)}</span></div>`
      )
      .join("");
  }

  // ── Who decides at each stage ──
  const STAGE_NAMES = { first_email: "First emails", followup: "Follow-ups", reply: "Answers to replies" };
  const LEVEL_NAMES = { manual: "A person sends", approve: "A person approves", auto: "The agent alone" };

  function renderLevels(levels) {
    const box = $("reviewLevels");
    box.textContent = "";
    Object.entries(levels || {}).forEach(([stage, level]) => {
      const group = document.createElement("div");
      group.className = "level-group";
      const name = document.createElement("span");
      name.className = "level-stage";
      name.textContent = STAGE_NAMES[stage] || stage;
      group.appendChild(name);
      if (!review.isAdmin) {
        const v = document.createElement("span");
        v.className = `level-value level-${level}`;
        v.textContent = LEVEL_NAMES[level] || level;
        group.appendChild(v);
      } else {
        const seg = document.createElement("div");
        seg.className = "level-seg";
        seg.setAttribute("role", "radiogroup");
        seg.setAttribute("aria-label", `Who decides: ${STAGE_NAMES[stage] || stage}`);
        ["manual", "approve", "auto"].forEach((lv) => {
          const b = document.createElement("button");
          b.type = "button";
          b.className = `level-opt level-${lv}` + (lv === level ? " on" : "");
          b.setAttribute("role", "radio");
          b.setAttribute("aria-checked", lv === level ? "true" : "false");
          b.textContent = { manual: "Person", approve: "Approve", auto: "Agent" }[lv];
          b.title = LEVEL_NAMES[lv];
          b.addEventListener("click", () => {
            if (lv === level) return;
            review.pendingLevel = { stage, level: lv };
            $("levelFormLabel").textContent = `${STAGE_NAMES[stage]}: ${LEVEL_NAMES[lv].toLowerCase()}. Why?`;
            $("levelForm").hidden = false;
            $("levelReason").value = "";
            $("levelReason").focus();
          });
          seg.appendChild(b);
        });
        group.appendChild(seg);
      }
      box.appendChild(group);
    });
  }

  $("levelForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const reason = $("levelReason").value.trim();
    if (!review.pendingLevel || !reason) return;
    reviewPost("/api/autonomy", { ...review.pendingLevel, reason }).then(({ ok, data }) => {
      if (!ok) return showToast(data.error || "Refused.", "error");
      showToast(`${STAGE_NAMES[data.stage]}: ${LEVEL_NAMES[data.level].toLowerCase()}.`);
      $("levelForm").hidden = true;
      review.pendingLevel = null;
      loadReview();
    });
  });
  $("levelCancel").addEventListener("click", () => {
    $("levelForm").hidden = true;
    review.pendingLevel = null;
  });

  // ── The queue ──
  function stageName(item) {
    return { first_email: "First email", followup: "Follow-up", reply: "Answer to a reply" }[item.stage] || item.stage;
  }

  function renderReviewList() {
    const list = $("reviewList");
    list.textContent = "";
    const empty = $("reviewEmpty");
    empty.hidden = review.items.length > 0;
    empty.textContent = {
      waiting: "Nothing waits for a decision. New proposals appear here as the agent finds companies.",
      approved: "No approved email is waiting to go out.",
      decided: "No decisions yet.",
    }[review.view];
    review.items.forEach((item) => {
      const li = document.createElement("li");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "review-item" + (review.current && review.current.id === item.id ? " active" : "");
      btn.dataset.id = item.id;
      const flags = (item.flags || []).length;
      let meta = stageName(item);
      if (review.view === "waiting" && flags) meta += `, ${flags} to check`;
      if (review.view !== "waiting" && item.reviewer) meta += `, ${item.status} by ${item.reviewer}`;
      btn.innerHTML =
        `<span class="ri-org">${escapeHtml(item.organization || item.email)}</span>` +
        `<span class="ri-meta">${escapeHtml(meta)}</span>` +
        `<span class="ri-score" title="Lead score">${escapeHtml(String(item.lead_score ?? ""))}</span>`;
      if (flags && review.view === "waiting") btn.classList.add("has-flags");
      btn.addEventListener("click", () => openReview(item.id));
      li.appendChild(btn);
      list.appendChild(li);
    });
  }

  function loadReview(view) {
    if (view) review.view = view;
    document.querySelectorAll(".queue-tab").forEach((t) => {
      const on = t.dataset.view === review.view;
      t.classList.toggle("active", on);
      t.setAttribute("aria-selected", on ? "true" : "false");
    });
    return fetch(`/api/review?view=${review.view}`)
      .then((r) => r.json())
      .then((data) => {
        review.items = data.items || [];
        review.isAdmin = !!data.is_admin;
        renderCounts(data.counts);
        renderReviewMetrics(data.metrics);
        renderLevels(data.levels);
        renderStrategy(data.sectors || []);
        if (review.current && !review.items.some((i) => i.id === review.current.id)) {
          review.current = null;
          $("reviewDetail").hidden = true;
        }
        renderReviewList();
      })
      .catch(() => showToast("Could not load the review queue.", "error"));
  }

  document.querySelectorAll(".queue-tab").forEach((t) =>
    t.addEventListener("click", () => {
      review.current = null;
      $("reviewDetail").hidden = true;
      loadReview(t.dataset.view);
    })
  );

  function evidenceRows(it) {
    const ev = it.evidence || {};
    const rows =
      it.stage === "reply"
        ? [
            ["Their words", ev.their_words],
            ["Reads as", ev.reads_as_interested ? "interested" : "not clearly interested"],
            ["Their subject", ev.their_subject],
            ["Country", ev.company_country],
            ["Role", ev.role],
          ]
        : [
            ["On their site", ev.found_on_their_site],
            ["Found at", ev.source_url],
            ["Role", ev.role],
            ["Country", ev.company_country],
            ["Lead score", ev.lead_score !== undefined && ev.lead_score !== null ? `${ev.lead_score}, tier ${ev.lead_tier || "–"}` : ""],
            ["Language", ev.language === "it" ? "Italian" : ev.language === "en" ? "English" : ev.language],
            ["Written by", it.ai_written ? "the AI model, from Omega's template" : "Omega's template"],
          ];
    if (it.lookalike && it.lookalike.score !== null) {
      rows.push([
        "Like your best customers",
        `${it.lookalike.score} of 100: closest to "${it.lookalike.like}"` + (it.lookalike.unlike ? `; also close to a company you rejected: "${it.lookalike.unlike}"` : ""),
      ]);
    }
    if (it.fit) {
      const f = it.fit;
      rows.push(["Fit score", `${f.total} of 100${f.sector ? `, ${f.sector}` : ""}`]);
      f.parts.forEach((part) =>
        rows.push([`  ${part.name[0].toUpperCase()}${part.name.slice(1)} (${part.weight}%)`, `${part.score}: ${part.why}`])
      );
      rows.push(["Company type", f.company_type]);
      if (f.certifications_needed.length) rows.push(["May require", f.certifications_needed.join(", ")]);
    }
    if (it.reviewer && it.status !== "pending")
      rows.push(["Decision", `${it.status} by ${it.reviewer}${it.reason ? `: ${it.reason}` : ""}`]);
    return rows
      .filter(([, v]) => v)
      .map(([k, v]) => `<tr><th scope="row">${escapeHtml(k)}</th><td>${escapeHtml(String(v))}</td></tr>`)
      .join("");
  }

  function showSheet(it) {
    review.current = it;
    const ev = it.evidence || {};
    const waiting = it.status === "pending";
    $("reviewDetail").hidden = false;
    $("rvOrg").textContent = ev.organization || it.organization || it.email || "";
    $("rvStage").textContent = `${stageName(it)} to ${it.email || "–"}`;
    $("rvEvidence").innerHTML = evidenceRows(it);
    $("rvFlags").innerHTML = (it.flags || [])
      .map((f) => `<span class="review-flag${/injection/.test(f) ? " is-alarm" : ""}">${escapeHtml(f)}</span>`)
      .join("");
    const subject = waiting ? it.proposed_subject : it.final_subject || it.proposed_subject;
    const body = waiting ? it.proposed_body : it.final_body || it.proposed_body;
    review.original = { subject: subject || "", body: body || "", text: draftToText(body) };
    review.htmlMode = false;
    $("rvSubject").value = review.original.subject;
    $("rvText").value = review.original.text;
    $("rvBody").value = review.original.body;
    $("rvText").hidden = false;
    $("rvBody").hidden = true;
    $("rvHtmlToggle").setAttribute("aria-pressed", "false");
    $("rvHtmlToggle").textContent = "Edit the HTML";
    [$("rvSubject"), $("rvText"), $("rvBody")].forEach((el) => (el.readOnly = !waiting));
    $("rvReason").value = "";
    $("rvReason").hidden = !waiting;
    $("rvApprove").hidden = !waiting;
    $("rvReject").hidden = !waiting;
    $("rvReopen").hidden = it.status !== "approved";
    $("rvQuote").hidden = !(it.stage === "reply" && waiting);
    $("rvMsg").textContent = "";
    $("reviewDetail").classList.toggle("is-readonly", !waiting);
    updateReviewPreview();
    renderReviewList();
  }

  function openReview(id) {
    const it = review.items.find((i) => i.id === id);
    if (!it) return;
    if (it.status !== "pending") return showSheet(it);
    reviewPost(`/api/review/${id}/open`).then(({ ok, data }) => {
      if (!ok) return showToast(data.error || "Could not open it.", "error");
      showSheet(data.item);
    });
  }

  $("rvHtmlToggle").addEventListener("click", () => {
    if (!review.htmlMode) $("rvBody").value = currentBody();
    else {
      review.original.body = $("rvBody").value;
      review.original.text = draftToText(review.original.body);
      $("rvText").value = review.original.text;
    }
    review.htmlMode = !review.htmlMode;
    $("rvText").hidden = review.htmlMode;
    $("rvBody").hidden = !review.htmlMode;
    $("rvHtmlToggle").setAttribute("aria-pressed", review.htmlMode ? "true" : "false");
    $("rvHtmlToggle").textContent = review.htmlMode ? "Edit as text" : "Edit the HTML";
    updateReviewPreview();
  });

  function moveReview(step) {
    if (!review.items.length) return;
    const i = review.current ? review.items.findIndex((x) => x.id === review.current.id) : -1;
    const next = review.items[Math.max(0, Math.min(review.items.length - 1, i + step))];
    if (next) openReview(next.id);
    const el = document.querySelector(`.review-item[data-id="${next && next.id}"]`);
    if (el) el.scrollIntoView({ block: "nearest" });
  }

  function decideReview(action) {
    const it = review.current;
    if (!it || it.status !== "pending") return;
    const reason = $("rvReason").value.trim();
    const msg = $("rvMsg");
    if (action === "reject" && !reason) {
      msg.textContent = "Write why first: the reason is what the agent learns from.";
      $("rvReason").focus();
      return;
    }
    const subject = $("rvSubject").value;
    const body = currentBody();
    const payload =
      action === "approve"
        ? {
            subject: subject === review.original.subject ? undefined : subject,
            body: body === it.proposed_body ? undefined : body,
            note: reason,
          }
        : { reason };
    const index = review.items.findIndex((x) => x.id === it.id);
    reviewPost(`/api/review/${it.id}/${action}`, payload).then(({ ok, data }) => {
      if (!ok) {
        msg.textContent = data.error || "Refused.";
        return;
      }
      const sent = data.sent;
      showToast(
        action === "reject"
          ? "Rejected. The reason is recorded."
          : sent
            ? `Approved. ${sent.message}`
            : "Approved. It goes out with the next batch.",
        sent && !sent.ok ? "error" : "success"
      );
      review.current = null;
      $("reviewDetail").hidden = true;
      loadReview().then(() => {
        const next = review.items[Math.min(index, review.items.length - 1)];
        if (next) openReview(next.id);
      });
    });
  }

  $("rvReopen").addEventListener("click", () => {
    const it = review.current;
    if (!it) return;
    reviewPost(`/api/review/${it.id}/reopen`).then(({ ok, data }) => {
      if (!ok) return showToast(data.error || "Refused.", "error");
      showToast("Reopened: it waits for a decision again.");
      review.current = null;
      $("reviewDetail").hidden = true;
      loadReview("waiting").then(() => openReview(it.id));
    });
  });

  ["rvText", "rvBody"].forEach((id) => $(id).addEventListener("input", updateReviewPreview));
  $("rvApprove").addEventListener("click", () => decideReview("approve"));
  $("rvReject").addEventListener("click", () => decideReview("reject"));

  document.addEventListener("keydown", (e) => {
    if (!$("tab-review").classList.contains("active")) return;
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
    if (typing) {
      if (e.key === "Escape") document.activeElement.blur();
      return;
    }
    const key = e.key.toLowerCase();
    if (key === "j") moveReview(1);
    else if (key === "k") moveReview(-1);
    else if (key === "a") decideReview("approve");
    else if (key === "r") {
      if ($("rvReason").value.trim()) decideReview("reject");
      else $("rvReason").focus();
    } else if (key === "e") (review.htmlMode ? $("rvBody") : $("rvText")).focus();
    else return;
    e.preventDefault();
  });

  // ── Where to look next ──
  function safeUrl(u) {
    return /^https?:\/\//i.test(u || "") ? u : null;
  }

  function renderStrategy(sectors) {
    const list = $("strategyList");
    if (!list) return;
    const open = new Set([...list.querySelectorAll("details[open]")].map((d) => d.dataset.id));
    list.textContent = "";
    if (!sectors.length) {
      list.innerHTML = '<li class="panel-note">No sectors in Omega\'s profile yet.</li>';
      return;
    }
    sectors.forEach((s) => {
      const li = document.createElement("li");
      const d = document.createElement("details");
      d.dataset.id = s.id;
      if (open.has(s.id)) d.open = true;
      const decided = s.approved + s.rejected;
      const outcome = decided
        ? `${s.approved} approved, ${s.rejected} rejected`
        : s.queued_at
          ? `queued ${s.queued_at.slice(0, 10)}`
          : "not searched yet";
      d.innerHTML =
        `<summary><span class="sec-name">${escapeHtml(s.name)}</span>` +
        `<span class="sec-kind ${s.type === "blue_ocean" ? "is-new" : ""}">${s.type === "blue_ocean" ? "New to Omega" : "Fits today"}</span>` +
        `<span class="sec-outcome">${escapeHtml(outcome)}</span></summary>` +
        `<div class="sec-body"><p>${escapeHtml(s.why)}</p>` +
        (s.barriers && s.barriers.length
          ? `<p><strong>Barriers.</strong> ${escapeHtml([].concat(s.barriers).join("; "))}</p>`
          : "") +
        (s.buyer_roles.length ? `<p><strong>Who buys.</strong> ${escapeHtml(s.buyer_roles.join(", "))}</p>` : "") +
        (s.countries.length ? `<p><strong>Countries.</strong> ${escapeHtml(s.countries.join(", "))}</p>` : "") +
        (s.evidence.length
          ? `<ul class="sec-evidence">${s.evidence
              .map((e) => {
                const url = safeUrl(e.source_url);
                const src = url
                  ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">source${e.date ? `, ${escapeHtml(e.date)}` : ""}</a>`
                  : "";
                return `<li>${escapeHtml(e.claim || "")} ${src}</li>`;
              })
              .join("")}</ul>`
          : "") +
        `</div>`;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn-secondary btn-sm";
      btn.textContent = `Queue ${s.queries.length} searches`;
      btn.addEventListener("click", () =>
        reviewPost("/api/strategy/queue", { sector: s.id }).then(({ ok, data }) => {
          showToast(ok ? `Queued ${data.queued} searches for ${s.name}.` : data.error || "Refused.", ok ? "success" : "error");
          if (ok) loadReview();
        })
      );
      d.querySelector(".sec-body").appendChild(btn);
      li.appendChild(d);
      list.appendChild(li);
    });
  }

  // The overview's buttons lead into Review, on the right view.
  document.querySelectorAll("[data-go]").forEach((b) =>
    b.addEventListener("click", () => {
      const link = document.querySelector(`.nav-links li[data-target="${b.dataset.go}"]`);
      review.view = b.dataset.view || "waiting";
      if (link) link.click();
    })
  );

  // The go-live checks are open until someone hides them; the choice is this
  // browser's own, remembered, and the checks open again on a fresh browser.
  const readinessCardEl = $("readinessCard");
  const readinessToggle = $("toggleReadiness");
  function setReadinessCollapsed(collapsed) {
    readinessCardEl.classList.toggle("is-collapsed", collapsed);
    readinessToggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
    readinessToggle.textContent = collapsed ? "Show the checks" : "Hide the checks";
  }
  try {
    if (localStorage.getItem("omega.readinessCollapsed") === "1") setReadinessCollapsed(true);
  } catch (e) {
    /* storage blocked: the checks stay open */
  }
  readinessToggle.addEventListener("click", () => {
    const collapsed = !readinessCardEl.classList.contains("is-collapsed");
    setReadinessCollapsed(collapsed);
    try {
      localStorage.setItem("omega.readinessCollapsed", collapsed ? "1" : "0");
    } catch (e) {
      /* not remembered */
    }
  });

  // ── Leads: sector filter ──
  const sectorFilter = $("sectorFilter");
  function applySectorFilter() {
    const want = sectorFilter.value;
    document.querySelectorAll("#contactsTable tbody tr[data-sector]").forEach((tr) => {
      tr.classList.toggle("sector-hidden", !!want && tr.dataset.sector !== want);
    });
  }
  function fillSectorFilter() {
    const sectors = new Set(
      [...document.querySelectorAll("#contactsTable tbody tr[data-sector]")]
        .map((tr) => tr.dataset.sector)
        .filter(Boolean)
    );
    const keep = sectorFilter.value;
    sectorFilter.length = 1;
    [...sectors].sort().forEach((name) => sectorFilter.add(new Option(name, name)));
    sectorFilter.value = sectors.has(keep) ? keep : "";
    applySectorFilter();
  }
  sectorFilter.addEventListener("change", applySectorFilter);
  new MutationObserver(fillSectorFilter).observe(document.querySelector("#contactsTable tbody"), {
    childList: true,
  });

  // ── Today: results so far, as plain bars ──
  function bars(id, rows, max) {
    const list = $(id);
    if (!list) return;
    list.textContent = "";
    if (!rows.length) {
      const li = document.createElement("li");
      li.className = "bar-empty";
      li.textContent = "Nothing yet.";
      list.appendChild(li);
      return;
    }
    const top = max || Math.max(...rows.map((r) => r[1]), 1);
    rows.slice(0, 8).forEach(([label, value]) => {
      const li = document.createElement("li");
      const name = document.createElement("span");
      name.className = "bar-label";
      name.textContent = label;
      const track = document.createElement("span");
      track.className = "bar-track";
      const fill = document.createElement("span");
      fill.className = "bar-fill";
      fill.style.width = `${Math.max(2, Math.round((value / top) * 100))}%`;
      track.appendChild(fill);
      const num = document.createElement("span");
      num.className = "bar-value";
      num.textContent = String(value);
      li.append(name, track, num);
      list.appendChild(li);
    });
  }
  function loadResults() {
    fetch("/api/results")
      .then((r) => r.json())
      .then((d) => {
        if (d.error) return;
        bars("chartSector", d.per_sector);
        bars("chartStatus", d.per_status);
        bars("chartFit", d.fit_per_sector, 100);
      })
      .catch(() => {});
  }
  loadResults();

  // ── Review: word count ──
  function updateWordCount() {
    const words = ($("rvText").value.match(/\S+/g) || []).length;
    const el = $("rvWords");
    el.textContent = `${words} words` + (words > 120 ? ": long for a first email, aim for 120 or fewer" : "");
    el.classList.toggle("is-long", words > 120);
  }
  $("rvText").addEventListener("input", updateWordCount);
  new MutationObserver(updateWordCount).observe($("reviewDetail"), { attributes: true, attributeFilter: ["hidden"] });

  // ── Review: a reply pasted from someone's own mailbox ──
  $("pasteForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const msg = $("pasteMsg");
    reviewPost("/api/review/paste-reply", {
      email: $("pasteEmail").value.trim(),
      subject: $("pasteSubject").value.trim(),
      body: $("pasteBody").value,
    }).then(({ ok, data }) => {
      if (!ok) {
        msg.textContent = data.error || "Refused.";
        return;
      }
      msg.textContent = `Read as ${data.label}. ${data.applied}`;
      $("pasteBody").value = "";
      loadReview("waiting");
    });
  });

  // ── How it works ──
  $("openHelp").addEventListener("click", () => $("helpDialog").showModal());

  // ── Quotes: from a drawing to a quote a person approves ──
  const Q_FIELDS = ["length", "shank_d", "head_d", "head_h", "collar_d", "collar_h", "thread", "thread_len", "material", "tolerance_mm", "annual_volume"];
  const quote = { drawing: "", forItem: null, timer: null };

  function partFromForm() {
    const part = {};
    Q_FIELDS.forEach((k) => (part[k] = $(`q_${k}`).value));
    return part;
  }
  function fillForm(part) {
    Q_FIELDS.forEach((k) => {
      const v = part[k];
      $(`q_${k}`).value = v === 0 || v === "0" ? "" : v ?? "";
    });
  }
  const eur = (x) => (x === null || x === undefined ? "–" : `€${Number(x).toLocaleString("en", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);

  function showEstimate(est) {
    const f = est.formability;
    const word = { formable: "Can be cold-formed", "formable with secondary operations": "Can be cold-formed, with extra operations", "not formable": "Cannot be cold-formed", unknown: "Not enough to judge yet" }[f.verdict] || f.verdict;
    $("qVerdict").textContent = word;
    $("qVerdict").className = `q-${f.verdict.replace(/\s+/g, "-")}`;
    $("qStations").textContent = f.stations ? `${f.stations} forming station(s) from ${f.wire_mm} mm wire, ${est.material}, ${est.mass_g} g a part` : "";
    const rows = [...f.reasons.map((r) => ["Forming", r]), ...f.secondary.map((r) => ["Also needs", r])];
    if (est.cost) {
      const b = est.cost.breakdown;
      rows.push(["Per 1,000", `material ${eur(b.material)}, forming ${eur(b.forming)}, rolling and sorting ${eur(b["rolling and sorting"])}` + (b["tooling spread over a year"] ? `, tooling ${eur(b["tooling spread over a year"])}` : "") + `, margin ${Math.round(b.margin * 100)}%`]);
    }
    $("qReasons").innerHTML = rows.map(([k, v]) => `<tr><th scope="row">${escapeHtml(k)}</th><td>${escapeHtml(v)}</td></tr>`).join("");
    $("qPrice").textContent = est.cost ? eur(est.cost.per_1000_eur) : "–";
    $("qTooling").textContent = est.cost ? eur(est.cost.tooling_once_eur) : "–";
    const k = est.carbon;
    $("qSaving").textContent = k && k.saving_pct !== null ? `${k.saving_pct}%` : "–";
    $("qYear").textContent = k && k.per_year_kg_co2_saved ? k.per_year_kg_co2_saved.toLocaleString("en") : "–";
    $("qCarbon").innerHTML = k
      ? `<div class="cc-row"><span>Cold-formed</span><span class="cc-bar"><span style="width:${Math.max(3, Math.round((k.formed_g_co2_per_part / k.machined_g_co2_per_part) * 100))}%"></span></span><span>${k.formed_g_co2_per_part} g CO2, ${k.material_used_formed_g} g steel</span></div>` +
        `<div class="cc-row is-machined"><span>Machined from bar</span><span class="cc-bar"><span style="width:100%"></span></span><span>${k.machined_g_co2_per_part} g CO2, ${k.material_used_machined_g} g steel</span></div>`
      : "";
    $("qAssumptions").textContent = est.cost ? `Estimate only: ${est.cost.assumptions}; ${k ? k.assumptions : ""}.` : "";
    $("qApprove").disabled = !est.cost;
  }

  function estimateNow() {
    reviewPost("/api/quotes/estimate", { part: partFromForm() }).then(({ ok, data }) => {
      if (ok) showEstimate(data.estimate);
    });
  }
  $("quoteForm").addEventListener("input", () => {
    clearTimeout(quote.timer);
    quote.timer = setTimeout(estimateNow, 250);
  });

  function readDrawing(blob, name) {
    const fd = new FormData();
    fd.append("drawing", blob, name);
    $("quoteRead").textContent = "Reading the drawing…";
    fetch("/api/quotes/read", { method: "POST", body: fd })
      .then((r) => r.json().then((d) => ({ ok: r.ok, d })))
      .then(({ ok, d }) => {
        if (!ok) {
          $("quoteRead").textContent = d.error || "Could not read it.";
          return;
        }
        quote.drawing = d.drawing_name;
        fillForm(d.part);
        $("quoteRead").textContent = `Read from ${d.drawing_name}. Check every size against the drawing.`;
        $("quoteNotes").innerHTML = (d.part.notes || []).map((n) => `<li>${escapeHtml(n)}</li>`).join("");
        showEstimate(d.estimate);
        $("qApproved").hidden = true;
      });
  }
  $("quoteFile").addEventListener("change", (e) => {
    const file = e.target.files[0];
    if (file) readDrawing(file, file.name);
    e.target.value = "";
  });
  $("quoteSample").addEventListener("click", (e) => {
    e.preventDefault();
    fetch("/api/quotes/sample")
      .then((r) => r.blob())
      .then((b) => readDrawing(b, "sample-drawing-stepped-pin.pdf"));
  });

  $("qApprove").addEventListener("click", () => {
    reviewPost("/api/quotes", {
      part: partFromForm(),
      email: $("q_email").value.trim(),
      language: $("q_language").value,
      drawing_name: quote.drawing,
    }).then(({ ok, data }) => {
      if (!ok) return showToast(data.error || "Refused.", "error");
      $("qApproved").hidden = false;
      $("qText").textContent = data.quote_text;
      quote.forItem = data.reply_item_id;
      $("qToAnswer").hidden = !data.reply_item_id;
      showToast("Quote approved.");
      loadQuotes();
    });
  });

  $("qToAnswer").addEventListener("click", () => {
    const text = $("qText").textContent;
    const id = quote.forItem;
    $("nav-tab-review").click();
    setTimeout(() => {
      openReview(id);
      setTimeout(() => {
        const t = $("rvText");
        const parts = t.value.split(/\n\s*\n/);
        parts.splice(Math.max(1, parts.length - 1), 0, text);
        t.value = parts.join("\n\n");
        t.dispatchEvent(new Event("input"));
        $("rvMsg").textContent = "The quote is in the draft. Read it, then approve the answer.";
      }, 700);
    }, 700);
  });

  function loadQuotes() {
    fetch("/api/quotes")
      .then((r) => r.json())
      .then((rows) => {
        const tbody = document.querySelector("#quoteTable tbody");
        tbody.innerHTML = rows.length
          ? rows
              .map(
                (q) =>
                  `<tr><td>${escapeHtml((q.created_at || "").slice(0, 16))}</td><td>${escapeHtml(q.company || "–")}</td><td>${escapeHtml(q.drawing || "–")}</td><td>${escapeHtml(eur(q.per_1000_eur))}</td><td>${q.saving_pct === null || q.saving_pct === undefined ? "–" : escapeHtml(q.saving_pct + "%")}</td><td>${escapeHtml(q.reviewer || "–")}</td></tr>`
              )
              .join("")
          : '<tr><td colspan="6">No quotes yet.</td></tr>';
      })
      .catch(() => {});
  }

  // A reply in Review leads straight to a quote for that company.
  $("rvQuote").addEventListener("click", () => {
    const it = review.current;
    if (!it) return;
    $("nav-tab-quotes").click();
    $("q_email").value = it.email || "";
    $("q_language").value = (it.language || "en") === "it" ? "it" : "en";
  });

  loadReviewSummary();

  scheduleNextPoll();
});
