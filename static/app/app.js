/* Omega Outreach Agent - the app shell.
   Plain DOM, no framework: every value from the server is escaped, and the
   draft HTML is parsed (DOMParser runs nothing), never injected. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const esc = (v) =>
    String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const fmt = (n, d = 0) =>
    n === null || n === undefined || Number.isNaN(n)
      ? "–"
      : Number(n).toLocaleString("en-GB", { minimumFractionDigits: d, maximumFractionDigits: d });
  const eur = (n, d = 2) => (n === null || n === undefined ? "–" : `${n < 0 ? "−" : ""}€${fmt(Math.abs(n), d)}`);
  const debounce = (fn, ms = 300) => {
    let t;
    return (...a) => {
      clearTimeout(t);
      t = setTimeout(() => fn(...a), ms);
    };
  };
  const host = (u) => {
    try {
      return `Source: ${new URL(u).hostname.replace(/^www\./, "")}`;
    } catch {
      return "Source";
    }
  };
  const CHEV = '<svg class="chev" viewBox="0 0 20 20" aria-hidden="true"><path d="m8 5 5 5-5 5"/></svg>';

  async function api(url, opts = {}) {
    const init = { headers: {}, ...opts };
    if (opts.json !== undefined) {
      init.method = init.method || "POST";
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.json);
      delete init.json;
    }
    try {
      const res = await fetch(url, init);
      const data = await res.json().catch(() => ({}));
      return { ok: res.ok, status: res.status, data };
    } catch {
      return { ok: false, status: 0, data: { error: "The server did not answer." } };
    }
  }

  let toastTimer;
  function toast(text, kind) {
    const t = $("toast");
    t.textContent = text;
    t.className = "toast" + (kind === "error" ? " is-error" : "");
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (t.hidden = true), 4200);
  }

  const typing = (el) => el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));

  // ── Router ───────────────────────────────────────────
  // A partner's login sees only the network; the rest is Omega's own data,
  // so the list of screens shrinks to that one once /api/network/me answers.
  let SCREENS = ["today", "review", "leads", "markets", "network", "setup"];
  let isMember = false;
  const loaders = {};
  let current = null;

  function show(name, { focus = true } = {}) {
    if (!SCREENS.includes(name)) name = SCREENS[0];
    SCREENS.forEach((s) => ($(`screen-${s}`).hidden = s !== name));
    document.querySelectorAll(".nav a").forEach((a) => {
      if (a.dataset.screen === name) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });
    document.title = `${$(`screen-${name}`).dataset.title} · Omega Outreach Agent`;
    current = name;
    if (loaders[name]) loaders[name]();
    if (focus) $("main").focus({ preventScroll: true });
    window.scrollTo({ top: 0 });
  }
  const screenFromHash = () => location.hash.slice(1).split("&")[0];
  window.addEventListener("hashchange", () => show(screenFromHash()));

  // ── Readability: text size, higher contrast, easier reading, less motion ──
  // Saved on this computer only; storage can be blocked, so every read and
  // write is guarded and the choice then lasts until the page is closed.
  const root = document.documentElement;
  const SIZES = [90, 100, 110, 120, 130, 140, 150];
  function readState() {
    return {
      size: Number(String(root.style.fontSize || "100%").replace("%", "")) || 100,
      contrast: root.dataset.contrast === "more" ? "more" : "",
      easy: root.dataset.easy === "on" ? "on" : "",
      motion: root.dataset.motion === "reduce" ? "reduce" : "",
    };
  }
  function applyRead(s) {
    root.style.fontSize = s.size === 100 ? "" : `${s.size}%`;
    for (const [key, on] of [["contrast", "more"], ["easy", "on"], ["motion", "reduce"]]) {
      if (s[key] === on) root.dataset[key] = on;
      else delete root.dataset[key];
    }
    try {
      localStorage.setItem("omega-read", JSON.stringify(s));
    } catch {
      /* storage blocked: the choice lasts until the page is closed */
    }
    syncRead();
  }
  function syncRead() {
    const s = readState();
    $("sizeOut").textContent = `${s.size}%`;
    $("sizeDown").disabled = s.size <= SIZES[0];
    $("sizeUp").disabled = s.size >= SIZES[SIZES.length - 1];
    $("contrastSw").setAttribute("aria-checked", String(s.contrast === "more"));
    $("easySw").setAttribute("aria-checked", String(s.easy === "on"));
    $("motionSw").setAttribute("aria-checked", String(s.motion === "reduce"));
  }
  function stepSize(d) {
    const s = readState();
    const i = SIZES.indexOf(s.size);
    applyRead({ ...s, size: SIZES[Math.max(0, Math.min(SIZES.length - 1, (i === -1 ? 1 : i) + d))] });
  }
  const flip = (key, on) => () => {
    const s = readState();
    applyRead({ ...s, [key]: s[key] === on ? "" : on });
  };
  $("sizeDown").addEventListener("click", () => stepSize(-1));
  $("sizeUp").addEventListener("click", () => stepSize(1));
  $("contrastSw").addEventListener("click", flip("contrast", "more"));
  $("easySw").addEventListener("click", flip("easy", "on"));
  $("motionSw").addEventListener("click", flip("motion", "reduce"));
  $("readReset").addEventListener("click", () => applyRead({ size: 100, contrast: "", easy: "", motion: "" }));
  function setReadOpen(open) {
    $("readPanel").hidden = !open;
    $("readBtn").setAttribute("aria-expanded", String(open));
    if (open) syncRead();
  }
  $("readBtn").addEventListener("click", () => setReadOpen($("readPanel").hidden));
  document.addEventListener("click", (e) => {
    if (!$("readPanel").hidden && !e.target.closest(".read-wrap")) setReadOpen(false);
  });
  syncRead();
  if (/(^|&)read=open(&|$)/.test(location.hash.slice(1))) setReadOpen(true);

  // ── Sheet (a small modal) ────────────────────────────
  let sheetDone = null;
  let sheetReturn = null;
  function openSheet({ title, body, ok, okClass = "primary", onOk }) {
    sheetReturn = document.activeElement;
    $("sheetTitle").textContent = title;
    $("sheetBody").innerHTML = body;
    $("sheetOk").textContent = ok;
    $("sheetOk").className = `btn ${okClass}`;
    sheetDone = onOk;
    $("scrim").hidden = false;
    $("sheet").hidden = false;
    const first = $("sheet").querySelector("input, textarea, select");
    (first || $("sheetOk")).focus();
  }
  function closeSheet() {
    $("sheet").hidden = true;
    $("scrim").hidden = true;
    sheetDone = null;
    if (sheetReturn) sheetReturn.focus();
  }
  $("sheetCancel").addEventListener("click", closeSheet);
  $("scrim").addEventListener("click", closeSheet);
  $("sheetOk").addEventListener("click", async () => {
    if (!sheetDone) return;
    $("sheetOk").disabled = true;
    const keep = await sheetDone();
    $("sheetOk").disabled = false;
    if (!keep) closeSheet();
  });
  $("sheet").addEventListener("keydown", (e) => {
    if (e.key === "Tab") {
      const f = [...$("sheet").querySelectorAll("input, textarea, select, button")].filter((x) => !x.disabled);
      const i = f.indexOf(document.activeElement);
      if (e.shiftKey && i === 0) (e.preventDefault(), f[f.length - 1].focus());
      else if (!e.shiftKey && i === f.length - 1) (e.preventDefault(), f[0].focus());
    }
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) $("sheetOk").click();
  });

  // ── Today ────────────────────────────────────────────
  loaders.today = async () => {
    $("todayDate").textContent = new Date().toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long" });
    const [rv, fn, list] = await Promise.all([api("/api/review/summary"), api("/api/funnel"), api("/api/review?view=waiting")]);
    const c = (rv.data && rv.data.counts) || {};
    const f = fn.ok ? fn.data : { stages: [], rates: {} };
    const n = Object.fromEntries((f.stages || []).map((x) => [x.key, x.count]));
    const pct = (v) => (v === null || v === undefined ? "–" : `${fmt(v, 0)}<small>%</small>`);
    $("todayTiles").innerHTML = [
      tile("review", "Waiting for review", fmt(c.waiting || 0), c.replies_waiting ? `${c.replies_waiting} ${c.replies_waiting === 1 ? "is a reply" : "are replies"}` : "First emails and follow-ups"),
      tile("leads", "Companies emailed", fmt(n.emailed || 0), `of ${fmt(n.found || 0)} found`),
      tile("leads", "Reply rate", pct(f.rates && f.rates.reply), `${fmt(n.replied || 0)} replied`),
      tile("leads", "Meetings booked", fmt(n.meeting || 0), `${fmt(n.won || 0)} became orders`, "green"),
    ].join("");

    const items = ((list.data && list.data.items) || []).slice(0, 6);
    $("needsYou").innerHTML = items.length
      ? items
          .map((it) => {
            const ev = it.evidence || {};
            return `<li><a class="row-btn" href="#review" data-open-review="${it.id}">
              <span class="row-title">${esc(ev.organization || it.organization || it.email)}</span>
              <span class="row-sub">${esc(stageName(it.stage))} · ${esc(it.proposed_subject || "")}</span>
              <span class="row-end">${fitPill(it.fit)}${CHEV}</span></a></li>`;
          })
          .join("")
      : `<li class="list-empty">Nothing is waiting for you.</li>`;

    const top = Math.max(1, ...(f.stages || []).map((x) => x.count));
    $("todayFunnel").innerHTML = (f.stages || []).length
      ? `<ul class="funnel" role="list">${f.stages
          .map(
            (x) => `<li><span class="f-label">${esc(x.label)}</span>
              <span class="score-bar" aria-hidden="true"><i style="width:${(x.count / top) * 100}%"></i></span>
              <span class="num">${fmt(x.count)}</span></li>`
          )
          .join("")}</ul>
         <p class="row-sub">Mark a company as interested, meeting booked or won from Review or Leads.</p>`
      : `<p class="lede">No figures yet.</p>`;
  };
  function tile(screen, label, value, note, cls = "") {
    return `<a class="tile ${cls}" href="#${screen}"><span class="tile-label">${esc(label)}</span>
      <span class="tile-value">${value}</span><span class="tile-note">${esc(note)}</span></a>`;
  }
  document.addEventListener("click", (e) => {
    const r = e.target.closest("[data-open-review]");
    if (r) review.pendingOpen = Number(r.dataset.openReview);
  });

  // ── Review ───────────────────────────────────────────
  const review = { view: "waiting", items: [], current: null, original: null, pendingOpen: null };
  const stageName = (s) => ({ first_email: "First email", followup: "Follow-up", reply: "Answer to a reply" })[s] || s;
  function fitPill(fit) {
    if (!fit) return "";
    const cls = fit.total >= 70 ? "blue" : fit.total >= 45 ? "" : "orange";
    return `<span class="pill ${cls}" title="Fit score">${fmt(fit.total)}</span>`;
  }

  loaders.review = async () => {
    const { ok, data } = await api(`/api/review?view=${review.view}`);
    if (!ok) return toast(data.error || "Could not load the review queue.", "error");
    review.items = data.items || [];
    renderReviewList();
    updateBadges(data.counts);
    if (data.reviewer) $("who").textContent = `Signed in as ${data.reviewer}`;
    const want = review.pendingOpen;
    review.pendingOpen = null;
    if (want && review.items.some((i) => i.id === want)) openReview(want);
    else if (review.current && !review.items.some((i) => i.id === review.current.id)) clearReviewDetail();
  };

  function renderReviewList() {
    const el = $("reviewList");
    if (!review.items.length) {
      el.innerHTML = `<li class="list-empty">${review.view === "waiting" ? "Nothing is waiting. Well done." : "Nothing here yet."}</li>`;
      return;
    }
    el.innerHTML = review.items
      .map((it) => {
        const ev = it.evidence || {};
        const sel = review.current && review.current.id === it.id;
        return `<li><button class="row-btn" role="option" aria-selected="${sel}" data-id="${it.id}" tabindex="-1">
          <span class="row-title">${esc(ev.organization || it.organization || it.email)}</span>
          <span class="row-sub">${esc(stageName(it.stage))} · ${esc(it.company_country || ev.company_country || "")}</span>
          <span class="row-end">${fitPill(it.fit)}</span></button></li>`;
      })
      .join("");
  }
  $("reviewList").addEventListener("click", (e) => {
    const b = e.target.closest("[data-id]");
    if (b) openReview(Number(b.dataset.id));
  });
  $("reviewViews").addEventListener("click", (e) => {
    const b = e.target.closest("[data-view]");
    if (!b) return;
    review.view = b.dataset.view;
    $("reviewViews").querySelectorAll("button").forEach((x) => x.setAttribute("aria-selected", x === b));
    clearReviewDetail();
    loaders.review();
  });

  function clearReviewDetail() {
    review.current = null;
    $("reviewDetail").innerHTML = `<div class="empty"><p class="empty-title">Choose an email</p><p>Use <kbd>J</kbd> and <kbd>K</kbd> to move through the list.</p></div>`;
  }

  async function openReview(id) {
    let it = review.items.find((i) => i.id === id);
    if (!it) return;
    if (it.status === "pending") {
      const { ok, data } = await api(`/api/review/${id}/open`, { method: "POST", json: {} });
      if (!ok) return toast(data.error || "Could not open it.", "error");
      it = { ...it, ...data.item };
    }
    review.current = it;
    renderReviewList();
    const btn = $("reviewList").querySelector(`[data-id="${id}"]`);
    if (btn) btn.scrollIntoView({ block: "nearest" });
    renderReviewDetail(it);
  }

  function renderReviewDetail(it) {
    const ev = it.evidence || {};
    const waiting = it.status === "pending";
    const subject = waiting ? it.proposed_subject : it.final_subject || it.proposed_subject;
    const body = waiting ? it.proposed_body : it.final_body || it.proposed_body;
    review.original = { subject: subject || "", body: body || "", text: draftToText(body) };
    const fit = it.fit || {};
    const words = ev.their_words || ev.found_on_their_site;
    $("reviewDetail").innerHTML = `
      <h2>${esc(ev.organization || it.organization || it.email)}</h2>
      <div class="meta"><span class="pill blue">${esc(stageName(it.stage))}</span>
        <span>to ${esc(it.email || "–")}</span>
        ${(it.flags || []).map((f) => `<span class="pill ${/injection/.test(f) ? "red" : "orange"}">${esc(f)}</span>`).join("")}
      </div>
      ${words ? `<p class="section-label">In their words</p><blockquote class="quote-block">${esc(words)}</blockquote>` : ""}
      <p class="section-label">Subject</p>
      <input class="subject-input" id="rvSubject" value="${esc(review.original.subject)}" ${waiting ? "" : "readonly"} aria-label="Subject">
      <p class="section-label">Email</p>
      <textarea class="body-input" id="rvText" ${waiting ? "" : "readonly"} aria-label="Email text">${esc(review.original.text)}</textarea>
      ${
        waiting
          ? `<div class="actions">
              <span class="spacer"></span>
              <span class="shortcut hide-narrow"><kbd>Ctrl</kbd> <kbd>Enter</kbd> approves</span>
              <button class="btn danger" type="button" id="rvReject">Reject…</button>
              <button class="btn primary" type="button" id="rvApprove">Approve</button>
            </div>`
          : `<div class="actions"><span class="pill ${it.status === "rejected" ? "red" : "green"}">${esc(it.status)}</span>
              <span class="row-sub">${esc(it.reviewer ? `by ${it.reviewer}` : "")}${it.reason ? ` · ${esc(it.reason)}` : ""}</span></div>`
      }
      <p class="section-label">Why this company · fit ${fmt(fit.total)}</p>
      <div class="fit-parts">${(fit.parts || [])
        .map(
          (p) => `<div class="fit-part"><span>${esc(p.name)}</span>
            <span class="score-bar" style="width:100%"><i style="width:${Math.max(0, Math.min(100, p.score))}%"></i></span>
            <span class="num">${fmt(p.score)}</span><span class="why">${esc(p.why)}</span></div>`
        )
        .join("")}</div>
      <p class="section-label">Outcome with this company</p>
      ${outcomeControl(it.contact_id, it.outcome)}`;
    const ta = $("rvText");
    const grow = () => ((ta.style.height = "auto"), (ta.style.height = `${ta.scrollHeight + 2}px`));
    ta.addEventListener("input", grow);
    requestAnimationFrame(grow);
    if (!waiting) return;
    $("rvApprove").addEventListener("click", approveReview);
    $("rvReject").addEventListener("click", rejectReview);
  }

  async function approveReview() {
    const it = review.current;
    if (!it || it.status !== "pending") return;
    const subject = $("rvSubject").value;
    const text = $("rvText").value;
    const body = text === review.original.text ? undefined : textToDraft(review.original.body, text);
    const { ok, data } = await api(`/api/review/${it.id}/approve`, {
      json: { subject: subject === review.original.subject ? undefined : subject, body },
    });
    if (!ok) return toast(data.error || "Refused.", "error");
    toast(data.sent ? `Approved. ${data.sent.message}` : "Approved. It goes out with the next batch.", data.sent && !data.sent.ok ? "error" : "");
    advanceAfterDecision(it.id);
  }

  function rejectReview() {
    const it = review.current;
    openSheet({
      title: "Reject this email",
      body: `<p>Say why. The reason is what the agent learns from, and the company is not contacted.</p>
        <label class="field"><span>Reason</span><textarea id="rejectReason" rows="3" placeholder="e.g. distributor, not a manufacturer"></textarea></label>
        <div class="sheet-error" id="sheetError"></div>`,
      ok: "Reject",
      okClass: "danger",
      onOk: async () => {
        const reason = $("rejectReason").value.trim();
        if (!reason) {
          $("sheetError").textContent = "Write the reason first.";
          return true;
        }
        const { ok, data } = await api(`/api/review/${it.id}/reject`, { json: { reason } });
        if (!ok) {
          $("sheetError").textContent = data.error || "Refused.";
          return true;
        }
        toast("Rejected. The reason is recorded.");
        advanceAfterDecision(it.id);
        return false;
      },
    });
  }

  async function advanceAfterDecision(id) {
    const index = review.items.findIndex((x) => x.id === id);
    clearReviewDetail();
    await loaders.review();
    const next = review.items[Math.min(index, review.items.length - 1)];
    if (next && review.view === "waiting") openReview(next.id);
  }

  function moveIn(list, step) {
    if (current === "network" && network.view === "jobs") {
      if (!network.jobs.length) return;
      const i = network.current ? network.jobs.findIndex((x) => x.id === network.current.id) : -1;
      const n = network.jobs[Math.max(0, Math.min(network.jobs.length - 1, i + step))];
      if (n) openJob(n.id);
    }
    if (current === "review") {
      if (!review.items.length) return;
      const i = review.current ? review.items.findIndex((x) => x.id === review.current.id) : -1;
      const n = review.items[Math.max(0, Math.min(review.items.length - 1, i + step))];
      if (n) openReview(n.id);
    }
  }

  // The draft is HTML; its own paragraphs are edited as plain text and put
  // back in place, so the layout, opt-out line and footer stay as they were.
  function editableParagraphs(doc) {
    return [...doc.body.querySelectorAll("p")].filter((p) => !p.querySelector("a") && !/\{\{\w+\}\}/.test(p.textContent));
  }
  function paragraphText(p) {
    const parts = [];
    p.childNodes.forEach((n) => parts.push(n.nodeName === "BR" ? "\n" : n.textContent));
    return parts.join("").replace(/[ \t]+/g, " ").trim();
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
        anchor.parentNode.insertBefore(p, anchor);
      });
    old.forEach((p) => p.remove());
    return /<html[\s>]/i.test(html) ? "<!DOCTYPE html>\n" + doc.documentElement.outerHTML : doc.body.innerHTML;
  }

  function updateBadges(counts) {
    if (!counts) return;
    const b = $("navReviewCount");
    b.textContent = counts.waiting;
    b.hidden = !counts.waiting;
    b.setAttribute("aria-label", `${counts.waiting} waiting`);
  }

  // ── Leads ────────────────────────────────────────────
  let leads = [];
  const STATUS = { pending: "Not contacted", emailed: "Emailed", responded: "Replied", active_client: "Client", unsubscribed: "Opted out", bounced: "Bounced", rejected_in_review: "Rejected" };
  loaders.leads = async () => {
    if (await loaders.leads.fetch()) renderLeads();
  };
  loaders.leads.fetch = async () => {
    const { ok, data } = await api("/api/contacts?limit=500");
    if (!ok) return toast(data.error || "Could not load the leads.", "error"), false;
    leads = data.filter((c) => c.status !== "erased");
    return true;
  };

  // Where a conversation ended up: set by a person, counted on Today.
  const OUTCOMES = [["", "–"], ["interested", "Interested"], ["meeting", "Meeting booked"], ["won", "Order won"], ["lost", "Not now"]];
  function outcomeControl(contactId, value, compact = false) {
    if (!contactId) return "";
    return `<select class="outcome${compact ? " compact" : ""}" data-outcome="${contactId}" aria-label="Outcome">${OUTCOMES.map(
      ([v, l]) => `<option value="${v}" ${v === (value || "") ? "selected" : ""}>${l}</option>`
    ).join("")}</select>`;
  }
  document.addEventListener("change", async (e) => {
    const sel = e.target.closest("select[data-outcome]");
    if (!sel) return;
    const id = Number(sel.dataset.outcome);
    const { ok, data } = await api(`/api/contacts/${id}/outcome`, { json: { stage: sel.value || null } });
    if (!ok) return toast(data.error || "Could not save the outcome.", "error");
    const c = leads.find((x) => x.id === id);
    if (c) c.outcome = data.stage;
    if (review.current && review.current.contact_id === id) review.current.outcome = data.stage;
    toast(data.stage ? `Saved: ${sel.options[sel.selectedIndex].text}.` : "Outcome cleared.");
  });

  // CSV import: a preview first, then the import, so nothing lands unseen.
  function importLeads() {
    let csv = null;
    let name = "";
    let previewed = false;
    openSheet({
      title: "Import leads from a CSV file",
      body: `<p>One company per row. Columns are matched by name: <b>email</b> is required; company, website, country, sector and name are used when present.</p>
        <label class="field"><span>File</span><input type="file" id="csvFile" accept=".csv,text/csv"></label>
        <label class="field"><span>Where the list came from</span><select id="csvChannel">
          <option value="third_party">A directory or listing</option>
          <option value="trade_fair">A trade fair</option>
          <option value="referral">A referral</option>
          <option value="inbound_request">They asked us to contact them</option>
          <option value="existing_customer">Existing customers</option>
        </select></label>
        <div id="csvPreview" aria-live="polite"></div>
        <div class="sheet-error" id="sheetError"></div>`,
      ok: "Check the file",
      onOk: async () => {
        $("sheetError").textContent = "";
        if (!csv) return ($("sheetError").textContent = "Choose a CSV file first."), true;
        const { ok, data } = await api("/api/leads/import", { json: { csv, filename: name, channel: $("csvChannel").value, dry_run: !previewed } });
        if (!ok) return ($("sheetError").textContent = data.error || "Refused."), true;
        if (!previewed) {
          previewed = true;
          const skipped = Object.entries(data.skipped || {}).filter(([, n]) => n);
          $("csvPreview").innerHTML = `<p><b>${fmt(data.would_add)}</b> new ${data.would_add === 1 ? "company" : "companies"} to add.</p>
            ${skipped.length ? `<ul class="reasons">${skipped.map(([k, n]) => `<li>${fmt(n)} skipped: ${esc(k)}</li>`).join("")}</ul>` : ""}
            ${(data.problems || []).length ? `<p class="row-sub">${data.problems.slice(0, 3).map(esc).join("<br>")}</p>` : ""}`;
          $("sheetOk").textContent = data.would_add ? `Import ${fmt(data.would_add)}` : "Close";
          if (!data.would_add) previewed = "empty";
          return true;
        }
        if (previewed === "empty") return false;
        toast(`Imported ${fmt(data.added)} ${data.added === 1 ? "company" : "companies"}. They wait as new leads, screened like the ones the agent finds.`);
        loaders.leads();
        return false;
      },
    });
    $("csvFile").addEventListener("change", async (e) => {
      const f = e.target.files[0];
      previewed = false;
      $("csvPreview").innerHTML = "";
      $("sheetOk").textContent = "Check the file";
      if (!f) return (csv = null);
      if (f.size > 2 * 1024 * 1024) return ($("sheetError").textContent = "The file is over 2 MB."), (csv = null);
      csv = await f.text();
      name = f.name;
    });
  }
  $("importLeads").addEventListener("click", importLeads);
  function renderLeads() {
    const q = $("leadFilter").value.trim().toLowerCase();
    const rows = leads
      .filter((c) => !q || [c.organization, c.email, c.sector, c.company_country].join(" ").toLowerCase().includes(q))
      .sort((a, b) => (b.fit_score || 0) - (a.fit_score || 0));
    $("leadTable").innerHTML = `<thead><tr><th>Company</th><th class="hide-narrow">Country</th><th class="hide-narrow">Sector</th><th>Fit</th><th>Outcome</th><th class="hide-narrow">Status</th></tr></thead>
      <tbody>${
        rows.length
          ? rows
              .map((c) => {
                return `<tr><td class="company">${esc(c.organization || "–")}<small>${esc(c.email)}</small></td>
                  <td class="hide-narrow">${esc(c.company_country || "–")}</td>
                  <td class="hide-narrow">${esc(c.sector || "–")}</td>
                  <td><span class="score"><span class="num">${fmt(c.fit_score)}</span><span class="score-bar"><i style="width:${Math.max(0, Math.min(100, Number(c.fit_score) || 0))}%"></i></span></span></td>
                  <td>${outcomeControl(c.id, c.outcome, true)}</td>
                  <td class="hide-narrow nowrap">${esc(STATUS[c.status] || c.status)}</td></tr>`;
              })
              .join("")
          : `<tr><td colspan="6" class="list-empty">No leads${q ? " match" : " yet"}.</td></tr>`
      }</tbody>`;
  }
  $("leadFilter").addEventListener("input", renderLeads);

  // ── Markets ──────────────────────────────────────────
  loaders.markets = async () => {
    const [rv, res] = await Promise.all([api("/api/review?view=waiting"), api("/api/results")]);
    const sectors = (rv.data && rv.data.sectors) || [];
    const per = Object.fromEntries((res.data && res.data.per_sector) || []);
    const fit = Object.fromEntries((res.data && res.data.fit_per_sector) || []);
    $("marketGrid").innerHTML = sectors.length
      ? sectors
          .map(
            (s) => `<article class="card market">
              <div class="tags">${s.type === "blue_ocean" ? '<span class="pill green">New market</span>' : '<span class="pill blue">Supplied today</span>'}
                ${(s.countries || []).slice(0, 4).map((c) => `<span class="pill">${esc(c)}</span>`).join("")}</div>
              <h2>${esc(s.name)}</h2>
              <p>${esc(s.why)}</p>
              <div class="stat"><span><b>${fmt(per[s.name] || 0)}</b>leads</span><span><b>${per[s.name] ? fmt(fit[s.name]) : "–"}</b>average fit</span></div>
              ${s.barriers ? `<p class="note">Barrier: ${esc(s.barriers)}</p>` : ""}
              ${(s.evidence || []).slice(0, 2).map((e) => `<a href="${esc(e.source_url)}" target="_blank" rel="noopener noreferrer" class="source-link">${esc(e.title || host(e.source_url))}</a>`).join("")}
            </article>`
          )
          .join("")
      : `<p class="row-sub">No sectors in the client profile.</p>`;
  };

  // ── Setup: what is left before a live email can go ──
  const SETUP_MARK = { done: ["green", "Done"], todo: ["orange", "To do"], off: ["", "Off"], unknown: ["", "Check did not run"] };
  loaders.setup = async () => {
    const { ok, data } = await api("/api/setup");
    if (!ok) return toast(data.error || "Could not run the checks.", "error");
    updateSetupBadge(data);
    $("setupSummary").innerHTML = `<p class="lede"><strong>${fmt(data.done)} of ${fmt(data.required)}</strong> required steps done.
      ${data.can_send_live ? "Live sending can start." : "Until every required step is done, nothing is sent for real."}
      ${data.dry_run ? " The agent is in dry-run mode: it drafts and logs, it sends nothing." : ""}</p>`;
    $("setupSteps").innerHTML = data.steps
      .map((st) => {
        const [cls, word] = SETUP_MARK[st.status] || SETUP_MARK.unknown;
        return `<li class="setup-step"><div class="setup-head"><span class="row-title">${esc(st.title)}</span>
          ${st.required ? "" : '<span class="pill">Optional</span>'}<span class="pill ${cls}">${esc(word)}</span></div>
          <p class="row-sub">${esc(st.detail || "")}</p>
          ${st.status === "done" ? "" : `<p class="setup-how">${esc(st.how)}</p>`}</li>`;
      })
      .join("");
  };
  function updateSetupBadge(data) {
    const b = $("navSetupCount");
    const left = Math.max(0, (data.required || 0) - (data.done || 0));
    b.textContent = left;
    b.hidden = !left;
    b.setAttribute("aria-label", `${left} steps left`);
  }

  // ── Network: Omega and the partner SMEs it routes work to ──
  const SPECS = {
    cold_forming: "Cold forming",
    turning: "Turning",
    heat_treatment: "Heat treatment",
    plating: "Plating & coating",
    machining: "Machining",
    wire: "Wire",
    logistics: "Logistics",
  };
  const KINDS = { capacity: "Extra capacity", operation: "An operation on our parts" };
  const JOB_STATUS = {
    draft: ["orange", "Waiting for approval"],
    rejected: ["", "Rejected"],
    requested: ["orange", "Sent"],
    accepted: ["blue", "Accepted"],
    in_progress: ["blue", "In progress"],
    done: ["green", "Done"],
    declined: ["", "Declined"],
    cancelled: ["", "Cancelled"],
  };
  const JOB_ACTIONS = {
    approve: ["Approve and send", "primary"],
    reject: ["Reject", "danger"],
    accept: ["Accept", "primary"],
    decline: ["Decline", "danger"],
    start: ["Start", ""],
    done: ["Mark done", "primary"],
    cancel: ["Cancel", "danger"],
  };
  const DIR_ICON = {
    in: '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M15 5 5 15M5 8v7h7"/></svg>',
    out: '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M5 15 15 5M8 5h7v7"/></svg>',
    between: '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M3 10h14M13 6l4 4-4 4"/></svg>',
  };
  const network = { me: null, view: "members", members: [], jobs: [], current: null, pendingOpen: null };
  const specName = (k) => SPECS[k] || k;
  const pcs = (n) => `${fmt(n)} ${n === 1 ? "piece" : "pieces"}`;
  // Server times are the wall clock in Omega's own time zone with no offset;
  // read and written back as local, they show exactly as Omega logged them.
  function when(s) {
    if (!s) return "–";
    const d = new Date(String(s).replace(" ", "T"));
    return Number.isNaN(d.getTime())
      ? String(s)
      : d.toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  }
  function day(s) {
    if (!s) return "–";
    const d = new Date(`${s}T00:00:00`);
    return Number.isNaN(d.getTime()) ? String(s) : d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
  }
  const needsMe = (j) => ["accept", "approve"].some((a) => (j.actions || []).includes(a)) || (j.unread || 0) > 0;
  function otherSide(j) {
    if (j.direction === "out") return j.to_member.name;
    if (j.direction === "in") return j.from_member.name;
    return `${j.from_member.name} → ${j.to_member.name}`;
  }
  const dirWords = (j) => ({ in: "Sent to you by", out: "You sent to", between: "Between" })[j.direction] || "";

  loaders.network = async () => {
    const [ms, js] = await Promise.all([api("/api/network/members"), api("/api/network/jobs")]);
    if (!ms.ok) toast(ms.data.error || "Could not load the network.", "error");
    if (!js.ok) toast(js.data.error || "Could not load the jobs.", "error");
    if (ms.ok) network.members = ms.data || [];
    if (js.ok) network.jobs = js.data || [];
    renderMembers();
    renderJobs();
    updateNetworkBadge();
    const want = network.pendingOpen;
    network.pendingOpen = null;
    if (want && network.jobs.some((j) => j.id === want)) openJob(want);
    else if (network.current) {
      const still = network.jobs.find((j) => j.id === network.current.id);
      if (still) renderJobDetail(still, { keepThread: true });
      else clearJobDetail();
    }
  };

  async function refreshNetworkBadge() {
    const { ok, data } = await api("/api/network/jobs");
    if (!ok) return;
    network.jobs = data || [];
    updateNetworkBadge();
  }
  function updateNetworkBadge() {
    const n = network.jobs.filter(needsMe).length;
    [$("navNetworkCount"), $("nwJobsCount")].forEach((b) => {
      b.textContent = n;
      b.hidden = !n;
      b.setAttribute("aria-label", `${n} ${n === 1 ? "job needs" : "jobs need"} you`);
    });
  }

  function setNetworkView(view) {
    network.view = view;
    $("networkViews").querySelectorAll("button").forEach((x) => x.setAttribute("aria-selected", x.dataset.view === view));
    $("nwMembers").hidden = view !== "members";
    $("nwJobs").hidden = view !== "jobs";
  }
  $("networkViews").addEventListener("click", (e) => {
    const b = e.target.closest("[data-view]");
    if (b) setNetworkView(b.dataset.view);
  });
  const onAList = () => current === "review" || (current === "network" && network.view === "jobs");

  function renderMembers() {
    $("nwAddMember").hidden = !(network.me && network.me.manages_members);
    const ms = network.members;
    const others = ms.filter((m) => !m.is_self);
    const free = others.reduce((s, m) => s + Math.max(0, m.free || 0), 0);
    $("nwMembersLede").innerHTML = ms.length
      ? `<strong>${fmt(others.length)} ${others.length === 1 ? "partner" : "partners"}</strong> besides ${esc((ms.find((m) => m.is_self) || {}).name || "you")}, with <strong>${pcs(free)}</strong> of free capacity this week.`
      : "";
    $("nwMemberGrid").innerHTML = ms.length
      ? ms.map(memberCard).join("")
      : `<p class="row-sub">No members in the network yet.</p>`;
  }
  function memberCard(m) {
    const cap = Math.max(0, m.capacity_per_week || 0);
    const free = Math.max(0, m.free || 0);
    const share = cap ? Math.min(100, (free / cap) * 100) : 0;
    const tight = cap && free / cap < 0.2;
    return `<article class="card nw-member${m.is_self ? " is-self" : ""}" aria-label="${esc(m.name)}">
      <div class="nw-member-head">
        <h2>${esc(m.name)}</h2>
        ${m.is_self ? `<span class="pill blue">${isMember ? "You" : "Omega · orchestrator"}</span>` : ""}
      </div>
      <p class="row-sub">${esc([m.city, m.country].filter(Boolean).join(", ") || "–")}</p>
      <div class="tags">${(m.specialisations || []).map((s) => `<span class="pill">${esc(specName(s))}</span>`).join("")}</div>
      ${m.certifications ? `<p class="nw-certs">${esc(m.certifications)}</p>` : ""}
      <div class="nw-cap">
        <div class="nw-cap-text"><span>Free this week</span><span class="num">${fmt(free)} of ${fmt(cap)} free</span></div>
        <span class="bar-track" aria-hidden="true"><span class="bar-fill${tight ? " is-tight" : ""}" style="width:${share}%"></span></span>
      </div>
      <div class="nw-member-foot">
        <span class="nw-rating">${m.rating === null || m.rating === undefined ? "No rating yet" : `Rating <b class="num">${fmt(m.rating, 1)}</b>`}</span>
        <span class="nw-member-btns">
        ${canEdit(m) ? `<button class="btn" type="button" data-capacity="${esc(m.id)}" aria-label="Set the weekly capacity of ${esc(m.name)}">Set capacity</button>` : ""}
        ${m.is_self ? "" : `<button class="btn" type="button" data-send-to="${esc(m.id)}" aria-label="Send a job to ${esc(m.name)}">Send a job</button>`}
        </span>
      </div>
    </article>`;
  }
  // Omega's staff set any card (the server keeps it to the administrator);
  // a partner sets only its own.
  const canEdit = (m) =>
    isMember ? !!(network.me && network.me.member && network.me.member.id === m.id) : !!(network.me && network.me.manages_members);
  $("nwMemberGrid").addEventListener("click", (e) => {
    const b = e.target.closest("[data-send-to]");
    if (b) openSendJob(Number(b.dataset.sendTo));
    const c = e.target.closest("[data-capacity]");
    if (c) openCapacity(Number(c.dataset.capacity));
  });
  $("nwSendJob").addEventListener("click", () => openSendJob());
  $("nwSplitOrder").addEventListener("click", () => openSplitOrder());
  $("nwAddMember").addEventListener("click", () => openAddMember());

  // The administrator admits a partner with its own login; the partner then
  // signs in and sees only the network.
  function openAddMember() {
    openSheet({
      title: "Add a partner",
      body: `<p>Admit a certified company to the network. It signs in with its own login and sees only its jobs and the member list, never Omega's customers.</p>
        <div class="nw-form">
          <label class="field nw-wide"><span>Company name</span><input id="amName" maxlength="120" autocomplete="off"></label>
          <label class="field"><span>City</span><input id="amCity" maxlength="80" autocomplete="off"></label>
          <label class="field"><span>Country (2 letters)</span><input id="amCountry" maxlength="2" value="IT" autocomplete="off"></label>
          <fieldset class="nw-wide nw-specs"><legend>What it does</legend>${Object.entries(SPECS)
            .map(([k, l]) => `<label><input type="checkbox" value="${k}"> ${esc(l)}</label>`)
            .join("")}</fieldset>
          <label class="field"><span>Certifications</span><input id="amCerts" maxlength="200" placeholder="e.g. ISO 9001" autocomplete="off"></label>
          <label class="field"><span>Pieces per week</span><input id="amCap" type="number" inputmode="numeric" min="0" max="10000000" step="1" value="0"></label>
          <label class="field"><span>Contact name</span><input id="amContact" maxlength="120" autocomplete="off"></label>
          <label class="field"><span>Contact email</span><input id="amEmail" type="email" maxlength="254" autocomplete="off"></label>
          <label class="field"><span>Login</span><input id="amLogin" maxlength="40" autocomplete="off" placeholder="e.g. tornitura"></label>
          <label class="field"><span>Password (12+ characters)</span><input id="amPass" type="password" minlength="12" autocomplete="new-password"></label>
        </div>
        <div class="sheet-error" id="sheetError"></div>`,
      ok: "Add the partner",
      onOk: async () => {
        const specs = [...document.querySelectorAll(".nw-specs input:checked")].map((x) => x.value);
        if (!specs.length) return (($("sheetError").textContent = "Tick at least one thing the partner does."), true);
        const { ok, data } = await api("/api/network/members", {
          json: {
            name: $("amName").value.trim(),
            city: $("amCity").value.trim(),
            country: $("amCountry").value.trim(),
            specialisations: specs,
            certifications: $("amCerts").value.trim(),
            capacity_per_week: Number($("amCap").value) || 0,
            contact_name: $("amContact").value.trim(),
            contact_email: $("amEmail").value.trim(),
            login: $("amLogin").value.trim(),
            password: $("amPass").value,
          },
        });
        if (!ok) return (($("sheetError").textContent = data.error || "Refused."), true);
        toast(`${data.name} joined the network.`);
        loaders.network();
        return false;
      },
    });
  }

  function openCapacity(id) {
    const m = network.members.find((x) => x.id === id);
    if (!m) return;
    openSheet({
      title: `Weekly capacity · ${m.name}`,
      body: `<p>How many pieces ${esc(m.is_self ? "Omega" : m.name)} can make in a week. Accepted jobs are booked against it; ${pcs(m.booked || 0)} are booked now.</p>
        <label class="field"><span>Pieces per week</span><input id="nwCap" type="number" inputmode="numeric" min="0" max="10000000" step="1" value="${esc(m.capacity_per_week || 0)}"></label>
        <div class="sheet-error" id="sheetError"></div>`,
      ok: "Save",
      onOk: async () => {
        const n = Number($("nwCap").value);
        if (!Number.isInteger(n) || n < 0 || n > 10000000) return (($("sheetError").textContent = "A whole number from 0 to 10,000,000."), true);
        const { ok, data } = await api(`/api/network/members/${id}`, { method: "PATCH", json: { capacity_per_week: n } });
        if (!ok) return (($("sheetError").textContent = data.error || "Refused."), true);
        toast("Capacity saved.");
        loaders.network();
        return false;
      },
    });
  }

  // One customer order, split: Omega keeps part of its own operation, the rest
  // of it goes to partners with room, and operations Omega does not do go to
  // the members that do. Every line becomes a draft a named person approves.
  function openSplitOrder() {
    const mine = ((network.me && network.me.member) || {}).specialisations || ["cold_forming"];
    const main = mine[0] || "cold_forming";
    let rowSeq = 0;
    const rowHtml = (op, qty) => {
      const n = ++rowSeq;
      return `<div class="nw-line" data-line="${n}">
        <label class="field"><span>Operation</span><select data-op>${Object.entries(SPECS)
          .map(([k, l]) => `<option value="${k}" ${k === op ? "selected" : ""}>${esc(l)}</option>`)
          .join("")}</select></label>
        <label class="field"><span>Partner</span><select data-to><option value="">Finding partners…</option></select></label>
        <label class="field"><span>Pieces</span><input data-qty type="number" inputmode="numeric" min="1" max="10000000" step="1" value="${esc(qty)}"></label>
        <button class="btn" type="button" data-remove aria-label="Remove this line">Remove</button>
      </div>`;
    };
    openSheet({
      title: "Split an order",
      body: `<p>Partners see the part, their quantity and the date, never Omega's customer. Nothing is sent until a named person approves each job.</p>
        <div class="nw-form">
          <label class="field nw-wide"><span>Part</span><input id="soPart" maxlength="120" placeholder="e.g. M10×60 flange bolt, 10.9" autocomplete="off"></label>
          <label class="field"><span>Order quantity (pieces)</span><input id="soQty" type="number" inputmode="numeric" min="1" max="10000000" step="1" value="1000"></label>
          <label class="field"><span>Omega makes itself</span><input id="soSelf" type="number" inputmode="numeric" min="0" max="10000000" step="1" value="200"></label>
          <label class="field"><span>Due date</span><input id="soDue" type="date"></label>
          <label class="field nw-wide"><span>Notes for the partners</span><textarea id="soNotes" rows="2" maxlength="1000" placeholder="Drawing number, tolerances, packaging"></textarea></label>
        </div>
        <fieldset class="nw-lines"><legend>Jobs for partners</legend><div id="soLines">${rowHtml(main, 800)}${rowHtml(Object.keys(SPECS).find((k) => !mine.includes(k)) || "turning", 1000)}</div>
          <button class="btn" type="button" id="soAdd">Add a job</button>
          <p class="row-sub" id="soSum" aria-live="polite"></p></fieldset>
        <div class="sheet-error" id="sheetError"></div>`,
      ok: "Save for approval",
      onOk: async () => {
        const err = (t) => (($("sheetError").textContent = t), true);
        const part = $("soPart").value.trim();
        if (!part) return err("Describe the part first.");
        const jobs = [...$("soLines").querySelectorAll(".nw-line")].map((r) => ({
          operation: r.querySelector("[data-op]").value,
          to_member_id: Number(r.querySelector("[data-to]").value) || null,
          quantity: Number(r.querySelector("[data-qty]").value),
        }));
        if (!jobs.length) return err("Add at least one job for a partner.");
        if (jobs.some((j) => !j.to_member_id)) return err("Choose a partner for every job.");
        const { ok, data } = await api("/api/network/orders", {
          json: {
            part,
            quantity: Number($("soQty").value),
            self_quantity: Number($("soSelf").value),
            due_date: $("soDue").value || null,
            notes: $("soNotes").value.trim(),
            jobs,
          },
        });
        if (!ok) return err(data.error || "Refused.");
        toast(`${data.jobs.length} jobs saved. Each one waits for a named person to approve it.`);
        network.pendingOpen = data.jobs[0].id;
        setNetworkView("jobs");
        if (current === "network") loaders.network();
        else location.hash = "#network";
        return false;
      },
    });
    const sum = () => {
      const total = Number($("soQty").value) || 0;
      const own = Number($("soSelf").value) || 0;
      const shared = [...$("soLines").querySelectorAll(".nw-line")]
        .filter((r) => mine.includes(r.querySelector("[data-op]").value))
        .reduce((t, r) => t + (Number(r.querySelector("[data-qty]").value) || 0), 0);
      const made = own + shared;
      $("soSum").textContent = `${specName(main)}: Omega ${fmt(own)} + partners ${fmt(shared)} = ${fmt(made)} of ${fmt(total)}${made === total ? " ✓" : made < total ? `, ${fmt(total - made)} still to place` : `, ${fmt(made - total)} too many`}.`;
    };
    async function fill(row) {
      const sel = row.querySelector("[data-to]");
      const op = row.querySelector("[data-op]").value;
      const q = Number(row.querySelector("[data-qty]").value);
      const { ok, data } = await api(`/api/network/match?operation=${encodeURIComponent(op)}&quantity=${Number.isInteger(q) && q > 0 ? q : 1}`);
      const keep = sel.value;
      if (!ok || !(data || []).length) {
        sel.innerHTML = `<option value="">No partner does ${esc(specName(op).toLowerCase())}</option>`;
        return;
      }
      sel.innerHTML = data
        .map((m) => `<option value="${esc(m.id)}">${esc(m.name)} · ${fmt(m.free)} free${m.fits ? "" : " (short)"}</option>`)
        .join("");
      if (keep && data.some((m) => String(m.id) === keep)) sel.value = keep;
    }
    const lines = $("soLines");
    lines.querySelectorAll(".nw-line").forEach(fill);
    lines.addEventListener("change", (e) => {
      const row = e.target.closest(".nw-line");
      if (row && e.target.matches("[data-op]")) fill(row);
      sum();
    });
    lines.addEventListener("input", debounce((e) => {
      const row = e.target.closest(".nw-line");
      if (row && e.target.matches("[data-qty]")) fill(row);
      sum();
    }, 250));
    lines.addEventListener("click", (e) => {
      const b = e.target.closest("[data-remove]");
      if (b) (b.closest(".nw-line").remove(), sum());
    });
    $("soAdd").addEventListener("click", () => {
      lines.insertAdjacentHTML("beforeend", rowHtml(Object.keys(SPECS).find((k) => !mine.includes(k)) || "turning", Number($("soQty").value) || 1));
      fill(lines.lastElementChild);
      sum();
    });
    ["soQty", "soSelf"].forEach((id) => $(id).addEventListener("input", sum));
    sum();
  }

  // The sheet asks the server who can do the operation at that quantity, best
  // first, and refreshes the list as the operation or the quantity changes.
  function openSendJob(toId) {
    const target = network.members.find((m) => m.id === toId);
    const mine = ((network.me && network.me.member) || {}).specialisations || [];
    const op0 = (target && (target.specialisations || [])[0]) || Object.keys(SPECS).find((k) => !mine.includes(k)) || "cold_forming";
    let chosen = target ? target.id : null;
    let kindTouched = false;
    let seq = 0;
    const kindFor = (op) => (mine.includes(op) ? "capacity" : "operation");
    openSheet({
      title: "Send a job",
      body: `<p>The partner sees the part, quantity and date, never Omega's customer. Nothing is sent until a named person at Omega approves it.</p>
        <div class="nw-form">
          <label class="field"><span>Operation</span><select id="nwOp">${Object.entries(SPECS)
            .map(([k, l]) => `<option value="${k}" ${k === op0 ? "selected" : ""}>${esc(l)}</option>`)
            .join("")}</select></label>
          <label class="field"><span>What you need</span><select id="nwKind">${Object.entries(KINDS)
            .map(([k, l]) => `<option value="${k}" ${k === kindFor(op0) ? "selected" : ""}>${esc(l)}</option>`)
            .join("")}</select></label>
          <label class="field nw-wide"><span>Part</span><input id="nwPart" maxlength="120" placeholder="e.g. M8×40 flange bolt, 10.9, zinc flake" autocomplete="off"></label>
          <label class="field"><span>Quantity (pieces)</span><input id="nwQty" type="number" inputmode="numeric" min="1" max="10000000" step="1" value="1000"></label>
          <label class="field"><span>Due date</span><input id="nwDue" type="date"></label>
          <label class="field nw-wide"><span>Notes</span><textarea id="nwNotes" rows="2" maxlength="1000" placeholder="Drawing number, tolerances, packaging"></textarea></label>
        </div>
        <fieldset class="nw-recipients"><legend>Send to</legend><div id="nwRecipients" aria-live="polite"><p class="loading">Finding partners…</p></div></fieldset>
        <div class="sheet-error" id="sheetError"></div>`,
      ok: "Save for approval",
      onOk: async () => {
        const err = (t) => (($("sheetError").textContent = t), true);
        const part = $("nwPart").value.trim();
        const quantity = Number($("nwQty").value);
        if (!part) return err("Describe the part first.");
        if (!Number.isInteger(quantity) || quantity < 1 || quantity > 10000000) return err("The quantity is a whole number from 1 to 10,000,000.");
        if (!chosen) return err("Choose who gets the job.");
        const { ok, data } = await api("/api/network/jobs", {
          json: {
            to_member_id: chosen,
            kind: $("nwKind").value,
            operation: $("nwOp").value,
            part,
            quantity,
            due_date: $("nwDue").value || null,
            notes: $("nwNotes").value.trim(),
          },
        });
        if (!ok) return err(data.error || "Refused.");
        toast(`Saved. ${data.to_member ? data.to_member.name : "The partner"} sees it once a named person at Omega approves it.`);
        network.pendingOpen = data.id;
        setNetworkView("jobs");
        if (current === "network") loaders.network();
        else location.hash = "#network";
        return false;
      },
    });
    async function refresh() {
      const mine = ++seq;
      const op = $("nwOp").value;
      const q = Number($("nwQty").value);
      const quantity = Number.isInteger(q) && q > 0 ? q : 1;
      const { ok, data } = await api(`/api/network/match?operation=${encodeURIComponent(op)}&quantity=${quantity}`);
      if (mine !== seq || $("sheet").hidden) return;
      const box = $("nwRecipients");
      if (!box) return;
      if (!ok) return (box.innerHTML = `<p class="row-sub">${esc(data.error || "Could not ask who is free.")}</p>`), (chosen = null);
      const list = data || [];
      if (!list.length) {
        chosen = null;
        box.innerHTML = `<p class="row-sub">No partner does ${esc(specName(op).toLowerCase())} yet.</p>`;
        return;
      }
      if (!list.some((m) => m.id === chosen)) chosen = list[0].id;
      box.innerHTML = list
        .map(
          (m, i) => `<label class="nw-recipient">
            <input type="radio" name="nwTo" value="${esc(m.id)}" ${m.id === chosen ? "checked" : ""}>
            <span class="nw-recipient-text"><span class="row-title">${esc(m.name)}${i === 0 ? ' <span class="hint">best match</span>' : ""}</span>
              <span class="row-sub">${esc([m.city, m.country].filter(Boolean).join(", "))} · ${fmt(m.free)} of ${fmt(m.capacity_per_week)} free</span></span>
            ${m.fits ? '<span class="pill green">Fits</span>' : '<span class="pill orange">Short on capacity</span>'}
          </label>`
        )
        .join("");
    }
    const later = debounce(refresh, 250);
    $("nwOp").addEventListener("change", () => {
      if (!kindTouched) $("nwKind").value = kindFor($("nwOp").value);
      refresh();
    });
    $("nwKind").addEventListener("change", () => (kindTouched = true));
    $("nwQty").addEventListener("input", later);
    $("nwRecipients").addEventListener("change", (e) => {
      if (e.target.name === "nwTo") chosen = Number(e.target.value);
    });
    refresh();
  }

  function renderJobs() {
    const el = $("nwJobList");
    if (!network.jobs.length) {
      el.innerHTML = `<li class="list-empty">No jobs yet. Send one from Members.</li>`;
      return;
    }
    el.innerHTML = network.jobs
      .map((j) => {
        const [cls, word] = JOB_STATUS[j.status] || ["", j.status];
        const sel = network.current && network.current.id === j.id;
        return `<li><button class="row-btn nw-job" role="option" aria-selected="${sel}" data-job="${esc(j.id)}" tabindex="-1">
          <span class="row-title"><span class="nw-dir ${esc(j.direction)}">${DIR_ICON[j.direction] || ""}<span class="sr">${esc(dirWords(j))}</span></span>${esc(otherSide(j))}</span>
          <span class="row-sub">${j.order ? `Order ${esc(j.order.id)} · ` : ""}${esc(j.part)} · ${pcs(j.quantity)} · ${esc(specName(j.operation))}</span>
          <span class="row-end">${j.unread ? `<span class="nw-dot" title="${fmt(j.unread)} new"><span class="sr">${fmt(j.unread)} new ${j.unread === 1 ? "message" : "messages"}</span></span>` : ""}<span class="pill ${cls}">${esc(word)}</span></span>
        </button></li>`;
      })
      .join("");
  }
  $("nwJobList").addEventListener("click", (e) => {
    const b = e.target.closest("[data-job]");
    if (b) openJob(Number(b.dataset.job));
  });

  function clearJobDetail() {
    network.current = null;
    $("nwJobDetail").innerHTML = `<div class="empty"><p class="empty-title">Choose a job</p><p>Use <kbd>J</kbd> and <kbd>K</kbd> to move through the list.</p></div>`;
  }

  async function openJob(id) {
    const j = network.jobs.find((x) => x.id === id);
    if (!j) return;
    network.current = j;
    if (network.view !== "jobs") setNetworkView("jobs");
    renderJobs();
    const btn = $("nwJobList").querySelector(`[data-job="${id}"]`);
    if (btn) btn.scrollIntoView({ block: "nearest" });
    renderJobDetail(j);
  }

  function renderJobDetail(j, { keepThread = false } = {}) {
    const [cls, word] = JOB_STATUS[j.status] || ["", j.status];
    const draft = keepThread && $("nwMsg") ? $("nwMsg").value : "";
    $("nwJobDetail").innerHTML = `
      <h2>${esc(j.part)}</h2>
      <div class="meta"><span class="pill ${cls}">${esc(word)}</span>
        <span>${esc(KINDS[j.kind] || j.kind)}</span><span>${esc(dirWords(j))} ${esc(otherSide(j))}</span></div>
      <dl class="facts">
        <dt>From</dt><dd>${esc(j.from_member.name)}</dd>
        <dt>To</dt><dd>${esc(j.to_member.name)}</dd>
        <dt>Operation</dt><dd>${esc(specName(j.operation))}</dd>
        <dt>Quantity</dt><dd class="num">${pcs(j.quantity)}</dd>
        <dt>Due</dt><dd>${j.due_date ? esc(day(j.due_date)) : "No date set"}</dd>
        ${j.order ? `<dt>Order</dt><dd>Order ${esc(j.order.id)}: ${pcs(j.order.quantity)} of ${esc(j.order.part)}, Omega makes ${pcs(j.order.self_quantity)}</dd>` : ""}
        <dt>Drafted</dt><dd>${esc(when(j.created_at))}${j.created_by ? ` by ${esc(j.created_by)}` : ""}</dd>
        <dt>${j.status === "rejected" ? "Rejected" : "Approved"}</dt><dd>${j.approved_at ? `${esc(when(j.approved_at))} by ${esc(j.approved_by || "–")}` : "Not yet: nothing has been sent"}</dd>
        ${j.decided_at ? `<dt>Decided</dt><dd>${esc(when(j.decided_at))}${j.decided_by ? ` by ${esc(j.decided_by)}` : ""}</dd>` : ""}
        ${j.notes ? `<dt>Notes</dt><dd class="nw-notes">${esc(j.notes)}</dd>` : ""}
      </dl>
      ${
        (j.actions || []).length
          ? `<div class="actions">${j.actions
              .filter((a) => JOB_ACTIONS[a])
              .map((a) => `<button class="btn ${JOB_ACTIONS[a][1]}" type="button" data-decide="${a}">${JOB_ACTIONS[a][0]}</button>`)
              .join("")}</div>`
          : ""
      }
      <p class="section-label" id="nwThreadTitle">Messages</p>
      <ol class="nw-thread" id="nwThread" aria-labelledby="nwThreadTitle"><li class="loading">Loading…</li></ol>
      <label class="field nw-compose"><span>Message</span><textarea id="nwMsg" rows="3" maxlength="2000" placeholder="Write to ${esc(j.direction === "in" ? j.from_member.name : j.direction === "out" ? j.to_member.name : "both sides")}"></textarea></label>
      <div class="actions"><span class="spacer"></span>
        <span class="shortcut hide-narrow"><kbd>Ctrl</kbd> <kbd>Enter</kbd> sends</span>
        <button class="btn primary" type="button" id="nwMsgSend">Send</button></div>`;
    $("nwMsg").value = draft;
    $("nwJobDetail").querySelectorAll("[data-decide]").forEach((b) => b.addEventListener("click", () => decideJob(j, b.dataset.decide)));
    $("nwMsgSend").addEventListener("click", () => postMessage(j));
    $("nwMsg").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) (e.preventDefault(), postMessage(j));
    });
    loadThread(j);
  }

  async function loadThread(j) {
    const { ok, data } = await api(`/api/network/jobs/${j.id}/messages`);
    if (!network.current || network.current.id !== j.id || !$("nwThread")) return;
    if (!ok) return ($("nwThread").innerHTML = `<li class="row-sub">${esc(data.error || "Could not load the messages.")}</li>`);
    renderThread(data || []);
    if (j.unread) {
      j.unread = 0;
      renderJobs();
      updateNetworkBadge();
    }
  }
  function renderThread(msgs) {
    const meId = network.me && network.me.member ? network.me.member.id : null;
    $("nwThread").innerHTML = msgs.length
      ? msgs
          .map(
            (m) => `<li class="nw-msg${m.from_member && m.from_member.id === meId ? " mine" : ""}">
              <p class="nw-msg-head"><b>${esc(m.author)}</b> · ${esc(m.from_member ? m.from_member.name : "")} · <time>${esc(when(m.created_at))}</time></p>
              <p class="nw-msg-body">${esc(m.body)}</p></li>`
          )
          .join("")
      : `<li class="row-sub">No messages yet.</li>`;
  }

  async function postMessage(j) {
    const ta = $("nwMsg");
    const body = ta.value.trim();
    if (!body) return toast("Write the message first.", "error");
    if (body.length > 2000) return toast("A message is at most 2,000 characters.", "error");
    $("nwMsgSend").disabled = true;
    const { ok, data } = await api(`/api/network/jobs/${j.id}/messages`, { json: { body } });
    if ($("nwMsgSend")) $("nwMsgSend").disabled = false;
    if (!ok) return toast(data.error || "The message was not sent.", "error");
    ta.value = "";
    loadThread(j);
  }

  const DECIDED = { approve: "Approved and sent.", reject: "Rejected. It was never sent.", accept: "Accepted. The quantity is booked against your capacity.", decline: "Declined.", start: "Started.", done: "Marked done. The capacity is free again.", cancel: "Cancelled." };
  function decideJob(j, decision) {
    const run = async () => {
      const { ok, data } = await api(`/api/network/jobs/${j.id}/decision`, { json: { decision } });
      if (!ok) return toast(data.error || "Refused.", "error"), false;
      toast(DECIDED[decision] || "Saved.");
      network.pendingOpen = j.id;
      await loaders.network();
      return true;
    };
    if (!["decline", "cancel", "reject"].includes(decision)) return run();
    if (decision === "reject")
      return openSheet({
        title: "Reject this job",
        body: `<p>${esc(pcs(j.quantity))} of ${esc(j.part)} for ${esc(j.to_member.name)}. It has not been sent and never will be; draft a new one if plans change.</p><div class="sheet-error" id="sheetError"></div>`,
        ok: "Reject",
        okClass: "danger",
        onOk: async () => !(await run()),
      });
    openSheet({
      title: decision === "decline" ? "Decline this job" : "Cancel this job",
      body: `<p>${esc(pcs(j.quantity))} of ${esc(j.part)}, ${esc(decision === "decline" ? `from ${j.from_member.name}` : `to ${j.to_member.name}`)}. This cannot be undone; send a new job if plans change.</p>
        <div class="sheet-error" id="sheetError"></div>`,
      ok: decision === "decline" ? "Decline" : "Cancel the job",
      okClass: "danger",
      onOk: async () => !(await run()),
    });
  }

  // ── Command palette ──────────────────────────────────
  // `staff` marks what only Omega's own people may reach; a partner's palette leaves it out.
  const ALL_COMMANDS = [
    { label: "Today", hint: "1", staff: true, run: () => (location.hash = "#today") },
    { label: "Review", hint: "2", staff: true, run: () => (location.hash = "#review") },
    { label: "Leads", hint: "3", staff: true, run: () => (location.hash = "#leads") },
    { label: "Markets", hint: "4", staff: true, run: () => (location.hash = "#markets") },
    { label: "Network: members and jobs", hint: "5", run: () => (location.hash = "#network") },
    { label: "Setup: connect Omega's mailbox", hint: "6", staff: true, run: () => (location.hash = "#setup") },
    { label: "Send a job to a partner", hint: "", run: () => (go("network"), setTimeout(() => openSendJob(), 50)) },
    { label: "Split an order across the network", hint: "", staff: true, run: () => (go("network"), setTimeout(() => openSplitOrder(), 50)) },
    { label: "Import leads from a CSV file", hint: "", staff: true, run: () => (go("leads"), setTimeout(importLeads, 50)) },
    { label: "Download approved leads (CSV)", hint: "", staff: true, run: () => (location.href = "/api/export/leads.csv") },
    { label: "Open the classic dashboard", hint: "", staff: true, run: () => (location.href = "/classic") },
    { label: "Sign out", hint: "", run: () => document.querySelector(".signout").submit() },
  ];
  let COMMANDS = ALL_COMMANDS;
  function go(name) {
    if (current === name) loaders[name]();
    else location.hash = `#${name}`;
  }
  let palItems = [];
  let palIndex = 0;
  let palReturn = null;
  function openPalette() {
    palReturn = document.activeElement;
    $("palette").hidden = false;
    $("paletteInput").value = "";
    renderPalette([]);
    $("paletteInput").focus();
  }
  function closePalette() {
    $("palette").hidden = true;
    if (palReturn) palReturn.focus();
  }
  function renderPalette(companies) {
    const q = $("paletteInput").value.trim().toLowerCase();
    const cmds = COMMANDS.filter((c) => !q || c.label.toLowerCase().includes(q));
    palItems = [...cmds, ...companies.map((c) => ({ label: c.organization || c.email, hint: c.company_country || "", company: c }))];
    palIndex = Math.min(palIndex, Math.max(0, palItems.length - 1));
    let html = cmds.length ? `<li class="pal-head" role="presentation">Go to</li>` : "";
    palItems.forEach((it, i) => {
      if (it.company && i === cmds.length) html += `<li class="pal-head" role="presentation">Companies</li>`;
      html += `<li role="option" id="pal-${i}" data-i="${i}" aria-selected="${i === palIndex}">${esc(it.label)}<small>${esc(it.hint)}</small></li>`;
    });
    if (!palItems.length) html = `<li class="pal-head" role="presentation">Nothing found</li>`;
    $("paletteList").innerHTML = html;
    $("paletteInput").setAttribute("aria-activedescendant", palItems.length ? `pal-${palIndex}` : "");
  }
  const palSearch = debounce(async () => {
    const q = $("paletteInput").value.trim().toLowerCase();
    if (q.length < 2 || isMember) return renderPalette([]);
    if (!leads.length) await loaders.leads.fetch();
    renderPalette(leads.filter((c) => [c.organization, c.email].join(" ").toLowerCase().includes(q)).slice(0, 6));
  }, 150);
  $("paletteInput").addEventListener("input", () => ((palIndex = 0), renderPalette([]), palSearch()));
  function runPalette(i) {
    const it = palItems[i];
    if (!it) return;
    closePalette();
    if (it.company) {
      $("leadFilter").value = it.company.organization || it.company.email;
      go("leads");
      renderLeads();
    } else it.run();
  }
  $("paletteList").addEventListener("click", (e) => {
    const li = e.target.closest("[data-i]");
    if (li) runPalette(Number(li.dataset.i));
  });
  $("palette").addEventListener("click", (e) => e.target === $("palette") && closePalette());
  $("paletteInput").addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      palIndex = (palIndex + (e.key === "ArrowDown" ? 1 : -1) + palItems.length) % Math.max(1, palItems.length);
      $("paletteList").querySelectorAll("[data-i]").forEach((li) => li.setAttribute("aria-selected", Number(li.dataset.i) === palIndex));
      $("paletteInput").setAttribute("aria-activedescendant", `pal-${palIndex}`);
      const sel = $(`pal-${palIndex}`);
      if (sel) sel.scrollIntoView({ block: "nearest" });
    } else if (e.key === "Enter") {
      e.preventDefault();
      runPalette(palIndex);
    }
  });
  document.querySelector("[data-action=palette]").addEventListener("click", openPalette);

  // ── Keyboard ─────────────────────────────────────────
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      return $("palette").hidden ? openPalette() : closePalette();
    }
    if (e.key === "Escape") {
      if (!$("palette").hidden) return closePalette();
      if (!$("sheet").hidden) return closeSheet();
      if (!$("readPanel").hidden) return setReadOpen(false), $("readBtn").focus();
    }
    if (!$("palette").hidden || !$("sheet").hidden) return;
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter" && current === "review" && review.current) {
      e.preventDefault();
      return approveReview();
    }
    if (typing(e.target) || e.ctrlKey || e.metaKey || e.altKey) return;
    const n = Number(e.key);
    if (n >= 1 && n <= SCREENS.length) {
      e.preventDefault();
      location.hash = `#${SCREENS[n - 1]}`;
    } else if (e.key === "j" || e.key === "ArrowDown") {
      if (onAList()) (e.preventDefault(), moveIn(null, 1));
    } else if (e.key === "k" || e.key === "ArrowUp") {
      if (onAList()) (e.preventDefault(), moveIn(null, -1));
    } else if (e.key === "/") {
      e.preventDefault();
      openPalette();
    }
  });

  // ── Start ────────────────────────────────────────────
  if (navigator.platform && /Mac/.test(navigator.platform)) document.querySelectorAll(".search-pill kbd").forEach((k) => (k.textContent = "⌘K"));
  // Who is signed in decides what may be asked for: a partner's login is
  // refused everything outside the network, so it must not ask at all.
  async function start() {
    const me = await api("/api/network/me");
    if (me.ok && me.data) network.me = me.data;
    if (network.me && network.me.role === "member") {
      enterMemberMode();
    } else {
      api("/api/review/summary").then(({ data }) => updateBadges(data && data.counts));
      api("/api/setup").then(({ ok, data }) => ok && updateSetupBadge(data));
      api("/api/review?view=approved").then(({ data }) => data && data.reviewer && ($("who").textContent = `Signed in as ${data.reviewer}`));
    }
    show(screenFromHash() || SCREENS[0], { focus: false });
    if (current !== "network" && network.me) refreshNetworkBadge();
  }

  function enterMemberMode() {
    isMember = true;
    SCREENS = ["network"];
    COMMANDS = ALL_COMMANDS.filter((c) => !c.staff).map((c) => (c.hint === "5" ? { ...c, hint: "1" } : c));
    document.querySelectorAll(".nav a[data-screen]").forEach((a) => {
      if (a.dataset.screen !== "network") a.closest("li").hidden = true;
      else a.setAttribute("aria-keyshortcuts", "1");
    });
    document.querySelector(".sidebar-foot .classic-link[href='/classic']").hidden = true;
    $("networkEyebrow").textContent = "Your work in Omega's network";
    $("nwSplitOrder").hidden = true;
    const m = network.me.member || {};
    $("who").textContent = `Signed in as ${network.me.name || ""}${m.name ? ` · ${m.name}` : ""}`;
  }

  start();
})();
