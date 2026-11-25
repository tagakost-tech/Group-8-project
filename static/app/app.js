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
  const SCREENS = ["today", "review", "leads", "markets", "setup"];
  const loaders = {};
  let current = null;

  function show(name, { focus = true } = {}) {
    if (!SCREENS.includes(name)) name = "today";
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
  window.addEventListener("hashchange", () => show(location.hash.slice(1)));

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
                  <td><span class="score"><span class="num">${fmt(c.fit_score)}</span><span class="score-bar"><i style="width:${c.fit_score || 0}%"></i></span></span></td>
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
  const SETUP_MARK = { done: ["green", "Done"], todo: ["orange", "To do"], off: ["", "Off"], unknown: ["", "Not checked"] };
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

  // ── Command palette ──────────────────────────────────
  const COMMANDS = [
    { label: "Today", hint: "1", run: () => (location.hash = "#today") },
    { label: "Review", hint: "2", run: () => (location.hash = "#review") },
    { label: "Leads", hint: "3", run: () => (location.hash = "#leads") },
    { label: "Markets", hint: "4", run: () => (location.hash = "#markets") },
    { label: "Setup: connect Omega's mailbox", hint: "5", run: () => (location.hash = "#setup") },
    { label: "Import leads from a CSV file", hint: "", run: () => (go("leads"), setTimeout(importLeads, 50)) },
    { label: "Download approved leads (CSV)", hint: "", run: () => (location.href = "/api/export/leads.csv") },
    { label: "Open the classic dashboard", hint: "", run: () => (location.href = "/classic") },
    { label: "Sign out", hint: "", run: () => document.querySelector(".signout").submit() },
  ];
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
    if (q.length < 2) return renderPalette([]);
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
      if (current === "review") (e.preventDefault(), moveIn(null, 1));
    } else if (e.key === "k" || e.key === "ArrowUp") {
      if (current === "review") (e.preventDefault(), moveIn(null, -1));
    } else if (e.key === "/") {
      e.preventDefault();
      openPalette();
    }
  });

  // ── Start ────────────────────────────────────────────
  if (navigator.platform && /Mac/.test(navigator.platform)) document.querySelectorAll(".search-pill kbd").forEach((k) => (k.textContent = "⌘K"));
  api("/api/review/summary").then(({ data }) => updateBadges(data && data.counts));
  api("/api/setup").then(({ ok, data }) => ok && updateSetupBadge(data));
  api("/api/review?view=approved").then(({ data }) => data && data.reviewer && ($("who").textContent = `Signed in as ${data.reviewer}`));
  show(location.hash.slice(1) || "today", { focus: false });
})();
