/* Omega Carbon Advantage - the app shell.
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
  const SCREENS = ["today", "review", "quotes", "carbon", "leads", "markets"];
  const loaders = {};
  let current = null;

  function show(name, { focus = true } = {}) {
    if (!SCREENS.includes(name)) name = "today";
    SCREENS.forEach((s) => ($(`screen-${s}`).hidden = s !== name));
    document.querySelectorAll(".nav a").forEach((a) => {
      if (a.dataset.screen === name) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });
    document.title = `${$(`screen-${name}`).dataset.title} · Omega Carbon Advantage`;
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
    const [rv, cs, list] = await Promise.all([
      api("/api/review/summary"),
      api("/api/carbon/summary"),
      api("/api/review?view=waiting"),
    ]);
    const c = (rv.data && rv.data.counts) || {};
    const k = cs.data || {};
    $("todayTiles").innerHTML = [
      tile("review", "Waiting for review", fmt(c.waiting || 0), c.replies_waiting ? `${c.replies_waiting} ${c.replies_waiting === 1 ? "is a reply" : "are replies"}` : "First emails and follow-ups"),
      tile("carbon", "Passports to approve", fmt(k.drafts || 0), `${fmt(k.approved || 0)} approved`),
      tile("carbon", "Lower than imports", k.median_times_lower ? `${fmt(k.median_times_lower, 1)}<small>×</small>` : "–", "Median, against the EU default for China", "green"),
      tile("carbon", "CO₂ avoided", `${fmt(k.avoided_t_co2 || 0, 1)}<small>t</small>`, `Across ${fmt(k.lots || 0)} measured lots`, "green"),
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

    $("todayCarbon").innerHTML = k.lots
      ? `<div class="figure">${fmt(k.median_times_lower, 1)}<small>× lower</small></div>
         <p class="lede">Omega's measured lots carry <strong>${fmt(k.median_times_lower, 1)} times less CO₂</strong> than the EU's default for the same part made in China. That is the figure a Tier-1 needs for its Scope 3 report.</p>
         <div class="actions"><a class="btn primary" href="#carbon">Open passports</a><button class="btn" type="button" data-goto-cbam>See the CBAM curve</button></div>`
      : `<p class="lede">No lot measured yet. Record one in Carbon to start.</p><a class="btn primary" href="#carbon">Record a lot</a>`;
  };
  function tile(screen, label, value, note, cls = "") {
    return `<a class="tile ${cls}" href="#${screen}"><span class="tile-label">${esc(label)}</span>
      <span class="tile-value">${value}</span><span class="tile-note">${esc(note)}</span></a>`;
  }
  document.addEventListener("click", (e) => {
    const r = e.target.closest("[data-open-review]");
    if (r) review.pendingOpen = Number(r.dataset.openReview);
    if (e.target.closest("[data-goto-cbam]")) {
      carbon.view = "cbam";
      location.hash = "#carbon";
      if (current === "carbon") loaders.carbon();
    }
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
    const cp = fit.carbon_pressure || { level: "none", reasons: [] };
    const cpPill = { high: "green", some: "blue" }[cp.level] || "";
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
              <button class="btn ghost" type="button" id="rvCarbon"><svg viewBox="0 0 20 20" aria-hidden="true"><path d="M10 4v12M4 10h12"/></svg> Add the carbon paragraph</button>
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
      <p class="section-label">Carbon pressure <span class="pill ${cpPill}">${esc(cp.level)}</span></p>
      ${cp.reasons.length ? `<ul class="reasons">${cp.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : `<p class="row-sub">No sign this buyer is asked about carbon yet.</p>`}`;
    const ta = $("rvText");
    const grow = () => ((ta.style.height = "auto"), (ta.style.height = `${ta.scrollHeight + 2}px`));
    ta.addEventListener("input", grow);
    requestAnimationFrame(grow);
    if (!waiting) return;
    $("rvApprove").addEventListener("click", approveReview);
    $("rvReject").addEventListener("click", rejectReview);
    $("rvCarbon").addEventListener("click", addCarbonParagraph);
  }

  async function addCarbonParagraph() {
    const it = review.current;
    const { data } = await api(`/api/carbon/paragraph?lang=${it.language === "it" ? "it" : "en"}`);
    if (!data.paragraph) return toast(data.why || "No approved passport yet.", "error");
    const ta = $("rvText");
    if (ta.value.includes(data.paragraph)) return toast("The paragraph is already in.");
    const paras = ta.value.split(/\n\s*\n/);
    const signOff = paras.findIndex((p) => /^(best|kind|warm)?\s*regards|^cordiali|^distinti|^saluti|^un cordiale/i.test(p.trim()));
    paras.splice(signOff > 0 ? signOff : Math.max(1, paras.length - 1), 0, data.paragraph);
    ta.value = paras.join("\n\n");
    ta.dispatchEvent(new Event("input"));
    toast(`Added, from ${data.lots} approved passport${data.lots === 1 ? "" : "s"}. Read it before you approve.`);
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
    } else if (current === "carbon" && carbon.view === "passports") {
      if (!carbon.lots.length) return;
      const i = carbon.current ? carbon.lots.findIndex((x) => x.id === carbon.current) : -1;
      const n = carbon.lots[Math.max(0, Math.min(carbon.lots.length - 1, i + step))];
      if (n) openLot(n.id);
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

  // ── Quotes ───────────────────────────────────────────
  const quote = { part: null, estimate: null, drawing: null };
  const PART_FIELDS = [
    ["length", "Length", "mm"],
    ["shank_d", "Shank diameter", "mm"],
    ["head_d", "Head diameter", "mm"],
    ["head_h", "Head height", "mm"],
    ["collar_d", "Collar diameter", "mm"],
    ["collar_h", "Collar height", "mm"],
    ["thread", "Thread", ""],
    ["thread_len", "Thread length", "mm"],
    ["tolerance_mm", "Tightest tolerance", "± mm"],
    ["annual_volume", "Parts a year", "pcs"],
  ];
  loaders.quotes = () => {};

  $("drop").addEventListener("click", (e) => {
    if (e.target.closest("#useSample")) return;
    $("drawingInput").click();
  });
  $("drop").addEventListener("keydown", (e) => {
    if (e.target === $("drop") && (e.key === "Enter" || e.key === " ")) (e.preventDefault(), $("drawingInput").click());
  });
  ["dragenter", "dragover"].forEach((t) =>
    $("drop").addEventListener(t, (e) => (e.preventDefault(), $("drop").classList.add("is-over")))
  );
  ["dragleave", "drop"].forEach((t) => $("drop").addEventListener(t, () => $("drop").classList.remove("is-over")));
  $("drop").addEventListener("drop", (e) => {
    e.preventDefault();
    const f = e.dataTransfer.files[0];
    if (f) readDrawing(f, f.name);
  });
  $("drawingInput").addEventListener("change", (e) => {
    const f = e.target.files[0];
    if (f) readDrawing(f, f.name);
    e.target.value = "";
  });
  $("useSample").addEventListener("click", async (e) => {
    e.stopPropagation();
    const res = await fetch("/api/quotes/sample");
    if (!res.ok) return toast("No sample drawing on this server.", "error");
    readDrawing(await res.blob(), "sample-drawing.pdf");
  });

  async function readDrawing(blob, name) {
    const fd = new FormData();
    fd.append("drawing", blob, name);
    $("drop").querySelector(".drop-title").textContent = "Reading the drawing…";
    const { ok, data } = await api("/api/quotes/read", { method: "POST", body: fd });
    $("drop").querySelector(".drop-title").textContent = "Drop another drawing";
    if (!ok) return toast(data.error || "Could not read it.", "error");
    $("drop").classList.add("is-compact");
    quote.drawing = data.drawing_name;
    quote.part = data.part;
    $("partSource").textContent = `read from ${data.drawing_name}`;
    renderPartForm();
    showEstimate(data.estimate);
    $("quoteResult").hidden = false;
    $("quoteText").hidden = true;
    refreshMemory();
  }

  function renderPartForm() {
    const p = quote.part;
    $("partForm").innerHTML =
      PART_FIELDS.map(
        ([k, label, unit]) => `<label class="form-row"><span>${esc(label)}</span>
          <input name="${k}" value="${esc(p[k] || (k === "thread" ? "" : 0))}" inputmode="${k === "thread" ? "text" : "decimal"}"><em>${esc(unit)}</em></label>`
      ).join("") +
      `<label class="form-row"><span>Material</span><select name="material">
        ${["carbon_steel", "stainless", "brass"].map((m) => `<option value="${m}" ${p.material === m ? "selected" : ""}>${m.replace("_", " ")}</option>`).join("")}
      </select><em></em></label>` +
      ((p.notes || []).length ? `<p class="row-sub" style="padding:8px 16px">${esc(p.notes.join(" · "))}</p>` : "");
  }
  $("partForm").addEventListener(
    "input",
    debounce(async () => {
      const f = new FormData($("partForm"));
      quote.part = { ...quote.part, ...Object.fromEntries(f.entries()) };
      const { ok, data } = await api("/api/quotes/estimate", { json: { part: quote.part } });
      if (!ok) return toast(data.error || "Check the sizes.", "error");
      showEstimate(data.estimate);
      refreshMemory();
    }, 350)
  );

  function showEstimate(est) {
    quote.estimate = est;
    const f = est.formability || {};
    const verdictCls = { formable: "green", "not formable": "red" }[f.verdict] || "orange";
    const c = est.cost;
    const k = est.carbon;
    $("estimateCard").innerHTML = `
      <span class="pill ${verdictCls}">${esc(f.verdict)}</span>
      ${(f.reasons || []).length ? `<ul class="reasons" style="margin-top:12px">${f.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}
      <div class="estimate-grid">
        <div><div class="k">Price per 1,000</div><div class="v">${c ? eur(c.per_1000_eur) : "–"}</div></div>
        <div><div class="k">Tooling, once</div><div class="v">${c ? eur(c.tooling_once_eur, 0) : "–"}</div></div>
        <div><div class="k">Forming stations</div><div class="v">${fmt(f.stations)}</div></div>
        <div><div class="k">Mass</div><div class="v">${fmt(est.mass_g, 1)}<small>g</small></div></div>
        <div><div class="k">CO₂ per part, formed</div><div class="v">${k ? fmt(k.formed_g_co2_per_part, 1) : "–"}<small>g</small></div></div>
        <div><div class="k">Less CO₂ than machining</div><div class="v" style="color:var(--green)">${k ? fmt(k.saving_pct) : "–"}<small>%</small></div></div>
      </div>
      ${(f.secondary || []).length ? `<p class="note">Then: ${esc(f.secondary.join(", "))}</p>` : ""}
      <p class="note">${esc(c ? c.assumptions : "")}</p>`;
    $("approveQuote").disabled = !c;
    renderQuoteCbam();
  }

  const refreshMemory = debounce(async () => {
    const { ok, data } = await api("/api/memory/similar", { json: { part: quote.part, description: quote.drawing || "" } });
    if (!ok) return;
    $("madeBefore").innerHTML = data.length ? data.map(jobRow).join("") : `<li class="list-empty">Nothing like it in the records.</li>`;
  }, 200);
  function jobRow(j) {
    return `<li class="job-row">
      <div class="job-head"><strong>${esc(j.part)}</strong><span class="closeness">${fmt(j.closeness)}% alike</span></div>
      <div class="job-meta">${esc(j.ref)} · ${esc(j.year)} · ${fmt(j.stations)} stations · ${eur(j.per_1000_eur)}/1,000 · tooling ${eur(j.tooling_eur, 0)}</div>
      <p class="job-note">${esc(j.notes || "")}</p></li>`;
  }

  function renderQuoteCbam() {
    const c = quote.estimate && quote.estimate.cost;
    if (!c) {
      $("quoteCbam").innerHTML = `<p class="row-sub">Quote the part first.</p>`;
      return;
    }
    $("quoteCbam").innerHTML = `
      <label class="field"><span>A Chinese supplier's price, € per 1,000</span>
        <input id="qImport" inputmode="decimal" placeholder="e.g. ${fmt(c.per_1000_eur * 0.7, 0)}"></label>
      <div id="qCbamOut" style="margin-top:16px"><p class="row-sub">CBAM adds a carbon cost to that price each year. Type it to see.</p></div>`;
    $("qImport").addEventListener(
      "input",
      debounce(async () => {
        const v = parseFloat($("qImport").value.replace(",", "."));
        if (!(v >= 0)) return;
        const q = new URLSearchParams({ mass_g: quote.estimate.mass_g, competitor: v, omega: c.per_1000_eur });
        const { ok, data } = await api(`/api/carbon/cbam?${q}`);
        if (!ok) return toast(data.error, "error");
        const pick = data.rows.filter((r) => [2026, 2030, 2034].includes(r.year));
        $("qCbamOut").innerHTML = `<table class="table"><tr><th>Year</th><th class="r">CBAM</th><th class="r">Import landed</th><th class="r">Omega − import</th></tr>
          ${pick.map((r) => `<tr><td>${r.year}</td><td class="r">${eur(r.cbam_eur_per_1000)}</td><td class="r">${eur(r.competitor_landed_eur_per_1000)}</td><td class="r">${eur(r.gap_eur_per_1000)}</td></tr>`).join("")}</table>
          <p class="verdict">${cbamSentence(data)}</p>`;
      }, 300)
    );
  }
  function cbamSentence(d) {
    const last = d.rows[d.rows.length - 1];
    if (d.crossover_year)
      return `From <strong>${d.crossover_year}</strong> the import costs more than Omega once CBAM is paid.`;
    return `Even in ${last.year}, CBAM adds <strong>${eur(last.cbam_eur_per_1000)}</strong> per 1,000: not enough to close a ${eur(last.gap_eur_per_1000)} gap on its own. Lead with the footprint, not the tax.`;
  }

  $("approveQuote").addEventListener("click", async () => {
    const { ok, data } = await api("/api/quotes", {
      json: { part: quote.part, email: $("quoteEmail").value.trim(), language: $("quoteLang").value, drawing_name: quote.drawing },
    });
    if (!ok) return toast(data.error || "Refused.", "error");
    $("quoteText").textContent = data.quote_text;
    $("quoteText").hidden = false;
    toast(data.reply_item_id ? "Quote approved. Its answer is waiting in Review." : "Quote approved and stored.");
  });

  $("memoryQuery").addEventListener(
    "input",
    debounce(async () => {
      const q = $("memoryQuery").value.trim();
      if (!q) return ($("memoryHits").innerHTML = "");
      const { data } = await api(`/api/memory/search?q=${encodeURIComponent(q)}`);
      $("memoryHits").innerHTML = (data || []).length ? data.map(jobRow).join("") : `<li class="list-empty">No note mentions that.</li>`;
    }, 250)
  );

  // ── Carbon ───────────────────────────────────────────
  const carbon = { view: "passports", lots: [], current: null };
  loaders.carbon = async () => {
    $("carbonViews").querySelectorAll("button").forEach((x) => x.setAttribute("aria-selected", x.dataset.view === carbon.view));
    $("carbonPassports").hidden = carbon.view !== "passports";
    $("carbonCbam").hidden = carbon.view !== "cbam";
    if (carbon.view === "cbam") return runCbam();
    const { ok, data } = await api("/api/carbon/lots");
    if (!ok) return toast(data.error || "Could not load the lots.", "error");
    carbon.lots = data;
    renderLots();
    if (carbon.current) openLot(carbon.current);
  };
  $("carbonViews").addEventListener("click", (e) => {
    const b = e.target.closest("[data-view]");
    if (!b) return;
    carbon.view = b.dataset.view;
    loaders.carbon();
  });

  function renderLots() {
    const drafts = carbon.lots.filter((l) => l.status === "draft").length;
    const b = $("navCarbonCount");
    b.textContent = drafts;
    b.hidden = !drafts;
    $("lotList").innerHTML = carbon.lots.length
      ? carbon.lots
          .map(
            (l) => `<li><button class="row-btn" role="option" tabindex="-1" aria-selected="${carbon.current === l.id}" data-lot="${l.id}">
              <span class="row-title">${esc(l.part_name)}</span>
              <span class="row-sub">${esc(l.lot_code)} · ${esc(l.customer || "no customer")}</span>
              <span class="row-end"><span class="num">${fmt(l.footprint.per_part_g_co2, 1)} g</span>
                <span class="pill ${l.status === "approved" ? "green" : "orange"}">${l.status === "approved" ? "Approved" : "Draft"}</span></span>
            </button></li>`
          )
          .join("")
      : `<li class="list-empty">No lots yet.</li>`;
  }
  $("lotList").addEventListener("click", (e) => {
    const b = e.target.closest("[data-lot]");
    if (b) openLot(Number(b.dataset.lot));
  });

  async function openLot(id) {
    const { ok, data: l } = await api(`/api/carbon/lots/${id}`);
    if (!ok) return;
    carbon.current = id;
    renderLots();
    const fp = l.footprint;
    const max = Math.max(fp.china_default_g_co2, fp.per_part_g_co2);
    const approved = l.status === "approved";
    $("lotDetail").innerHTML = `
      <h2>${esc(l.part_name)}</h2>
      <div class="meta"><span class="pill ${approved ? "green" : "orange"}">${approved ? "Approved" : "Draft"}</span>
        <span>${esc(l.lot_code)}</span><span>·</span><span>${esc(l.customer || "no customer")}</span></div>
      <div class="figure">${fmt(fp.per_part_g_co2, 2)}<small>g CO₂e per part</small></div>
      <p class="lede"><strong>${fmt(fp.times_lower, 1)}× lower</strong> than the EU default for the same part made in China.</p>
      <div class="bars" role="img" aria-label="Omega ${fmt(fp.per_part_g_co2, 1)} grams against ${fmt(fp.china_default_g_co2, 1)} grams for China">
        <div class="bar-row"><span>This lot, Omega</span><span class="bar-track"><span class="bar-fill green" style="display:block;width:${(fp.per_part_g_co2 / max) * 100}%"></span></span><span class="num">${fmt(fp.per_part_g_co2, 1)} g</span></div>
        <div class="bar-row"><span>China, EU default</span><span class="bar-track"><span class="bar-fill" style="display:block;width:${(fp.china_default_g_co2 / max) * 100}%"></span></span><span class="num">${fmt(fp.china_default_g_co2, 1)} g</span></div>
      </div>
      <dl class="facts">
        <dt>Steel</dt><dd>${l.steel_mill ? `${esc(l.steel_mill)}, ` : "Mill not named, "}${fmt(fp.steel_factor, 2)} t CO₂/t (${esc(fp.steel_factor_from)})</dd>
        <dt>Good parts</dt><dd>${fmt(l.good_parts)} from ${fmt(l.wire_kg)} kg of wire · yield ${fmt((fp.yield || 0) * 100, 1)}%</dd>
        <dt>Energy</dt><dd>${fmt(l.energy_kwh)} kWh</dd>
        <dt>Lot total</dt><dd>${fmt(fp.lot_kg_co2, 1)} kg CO₂e · steel ${fmt((fp.steel_share || 0) * 100)}%</dd>
        <dt>Primary data</dt><dd>${fmt(fp.primary_data_share * 100)}%</dd>
        <dt>Method</dt><dd>${esc(l.pcf.crossSectoralStandardsUsed.join(", "))}; Catena-X PCF fields</dd>
        ${approved ? `<dt>Approved by</dt><dd>${esc(l.reviewer)} · ${esc(l.decided_at)}</dd>` : ""}
      </dl>
      <div class="actions">
        ${approved ? "" : `<button class="btn primary" type="button" id="approveLot">Approve passport</button>`}
        <a class="btn" ${approved ? `href="/api/carbon/lots/${l.id}/passport.pdf"` : `aria-disabled="true" tabindex="-1" style="opacity:.4;pointer-events:none"`}>Passport PDF</a>
        <a class="btn" ${approved ? `href="/api/carbon/lots/${l.id}/passport.json"` : `aria-disabled="true" tabindex="-1" style="opacity:.4;pointer-events:none"`}>PCF data (JSON)</a>
        ${approved ? "" : `<span class="note">Downloads open once a person approves.</span>`}
      </div>
      <p class="note">${esc(l.pcf.boundaryProcessesDescription)} ${esc(l.pcf.comment)}</p>`;
    const a = $("approveLot");
    if (a)
      a.addEventListener("click", async () => {
        const { ok: done, data } = await api(`/api/carbon/lots/${l.id}/approve`, { json: {} });
        if (!done) return toast(data.error || "Refused.", "error");
        toast("Passport approved. It can now go to the customer.");
        loaders.carbon();
      });
  }

  const LOT_FIELDS = [
    ["lot_code", "Lot code", "", "L-26-0500"],
    ["part_name", "Part", "", "Stepped pin"],
    ["customer", "Customer", "", "optional"],
    ["part_mass_g", "Mass of one part", "g", "11.9"],
    ["steel_mill", "Steel mill", "", "from the certificate"],
    ["mill_t_co2_per_t", "Mill CO₂ factor", "t/t", "route average if empty"],
    ["wire_kg", "Wire used", "kg", "1250"],
    ["good_parts", "Good parts", "pcs", "102000"],
    ["energy_kwh", "Energy", "kWh", "310"],
  ];
  $("newLot").addEventListener("click", () =>
    openSheet({
      title: "Record a lot",
      body: `<p>Steel parts only. The figures come from the steel certificate, the press counter and the meter. It is saved as a draft passport.</p>
        <form class="form-list" id="lotForm">${LOT_FIELDS.map(
          ([k, l, u, ph]) => `<label class="form-row"><span>${esc(l)}</span><input name="${k}" placeholder="${esc(ph)}"><em>${esc(u)}</em></label>`
        ).join("")}
        <label class="form-row"><span>Steel route</span><select name="steel_route"><option value="eaf_scrap">Electric arc</option><option value="bf_bof">Blast furnace</option></select><em></em></label></form>
        <div class="sheet-error" id="sheetError"></div>`,
      ok: "Save draft",
      onOk: async () => {
        const body = Object.fromEntries(new FormData($("lotForm")).entries());
        Object.keys(body).forEach((k) => body[k] === "" && delete body[k]);
        const { ok, data } = await api("/api/carbon/lots", { json: body });
        if (!ok) {
          $("sheetError").textContent = data.error || "Refused.";
          return true;
        }
        carbon.current = data.id;
        toast("Lot recorded as a draft passport.");
        loaders.carbon();
        return false;
      },
    })
  );

  const runCbam = async () => {
    const f = Object.fromEntries(new FormData($("cbamForm")).entries());
    Object.keys(f).forEach((k) => {
      f[k] = String(f[k]).replace(",", ".").trim();
      if (f[k] === "") delete f[k];
    });
    const { ok, data } = await api(`/api/carbon/cbam?${new URLSearchParams(f)}`);
    if (!ok) {
      $("cbamVerdict").textContent = data.error || "Check the figures.";
      return;
    }
    drawCbam(data);
    $("cbamTable").innerHTML = `<thead><tr><th>Year</th><th class="r">Charged</th><th class="r">CBAM per 1,000</th><th class="r">Import landed</th><th class="r">Omega</th><th class="r">Omega minus import</th></tr></thead>
      <tbody>${data.rows
        .map(
          (r) => `<tr class="${r.gap_eur_per_1000 <= 0 ? "is-cross" : ""}"><td>${r.year}</td><td class="r">${fmt(r.charged_share * 100, 1)}%</td>
          <td class="r">${eur(r.cbam_eur_per_1000)}</td><td class="r">${eur(r.competitor_landed_eur_per_1000)}</td>
          <td class="r">${eur(r.omega_eur_per_1000)}</td><td class="r">${eur(r.gap_eur_per_1000)}</td></tr>`
        )
        .join("")}</tbody>`;
    $("cbamVerdict").innerHTML = cbamSentence(data);
    $("cbamSources").innerHTML = data.assumptions.map((a) => `<li>${esc(a)}</li>`).join("");
  };
  $("cbamForm").addEventListener("input", debounce(runCbam, 250));
  window.addEventListener("resize", debounce(() => current === "carbon" && carbon.view === "cbam" && runCbam(), 200));
  $("cbamForm").addEventListener("submit", (e) => e.preventDefault());

  function drawCbam(d) {
    const W = Math.max(300, $("cbamChart").clientWidth || 720), H = W < 500 ? 220 : 280, L = 52, R = 12, T = 12, B = 28;
    const narrow = W < 500;
    const rows = d.rows;
    const vals = rows.flatMap((r) => [r.competitor_landed_eur_per_1000, r.omega_eur_per_1000]);
    const lo = 0, hi = Math.max(...vals) * 1.1;
    const x = (i) => L + (i * (W - L - R)) / (rows.length - 1);
    const y = (v) => T + (1 - (v - lo) / (hi - lo || 1)) * (H - T - B);
    const ticks = 4;
    let g = '<g class="grid axis">';
    for (let i = 0; i <= ticks; i++) {
      const v = lo + ((hi - lo) * i) / ticks;
      g += `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/><text x="${L - 8}" y="${y(v) + 4}" text-anchor="end">€${fmt(v, 0)}</text>`;
    }
    rows.forEach((r, i) => (g += `<text x="${x(i)}" y="${H - 8}" text-anchor="middle">${narrow ? `’${String(r.year).slice(2)}` : r.year}</text>`));
    g += "</g>";
    const path = (k) => rows.map((r, i) => `${i ? "L" : "M"}${x(i)},${y(r[k])}`).join("");
    const dots = (k, cls) => rows.map((r, i) => `<circle class="${cls}" cx="${x(i)}" cy="${y(r[k])}" r="3.5"><title>${r.year}: ${eur(r[k])}</title></circle>`).join("");
    const last = rows[rows.length - 1];
    $("cbamChart").innerHTML = `<div><svg viewBox="0 0 ${W} ${H}">${g}
      <path class="import" d="${path("competitor_landed_eur_per_1000")}"/><path class="omega" d="${path("omega_eur_per_1000")}"/>
      ${dots("competitor_landed_eur_per_1000", "dot-import")}${dots("omega_eur_per_1000", "dot-omega")}
      </svg></div><div class="legend"><span><i style="background:var(--orange)"></i>Import from China, with CBAM</span><span><i style="background:var(--accent)"></i>Omega</span></div>`;
    $("cbamChart").setAttribute(
      "aria-label",
      `Import price with CBAM rises from ${eur(rows[0].competitor_landed_eur_per_1000)} in ${rows[0].year} to ${eur(last.competitor_landed_eur_per_1000)} in ${last.year}; Omega stays at ${eur(last.omega_eur_per_1000)}.`
    );
  }

  // ── Leads ────────────────────────────────────────────
  let leads = [];
  const STATUS = { pending: "Not contacted", emailed: "Emailed", responded: "Replied", active_client: "Client", unsubscribed: "Opted out", bounced: "Bounced", rejected_in_review: "Rejected" };
  loaders.leads = async () => {
    const { ok, data } = await api("/api/contacts?limit=200");
    if (!ok) return toast(data.error || "Could not load the leads.", "error");
    leads = data.filter((c) => c.status !== "erased");
    renderLeads();
  };
  function renderLeads() {
    const q = $("leadFilter").value.trim().toLowerCase();
    const rows = leads
      .filter((c) => !q || [c.organization, c.email, c.sector, c.company_country].join(" ").toLowerCase().includes(q))
      .sort((a, b) => (b.fit_score || 0) - (a.fit_score || 0));
    $("leadTable").innerHTML = `<thead><tr><th>Company</th><th class="hide-narrow">Country</th><th class="hide-narrow">Sector</th><th>Fit</th><th>Carbon</th><th class="hide-narrow">Status</th></tr></thead>
      <tbody>${
        rows.length
          ? rows
              .map((c) => {
                const cp = (c.carbon_pressure || {}).level || "none";
                return `<tr><td class="company">${esc(c.organization || "–")}<small>${esc(c.email)}</small></td>
                  <td class="hide-narrow">${esc(c.company_country || "–")}</td>
                  <td class="hide-narrow">${esc(c.sector || "–")}</td>
                  <td><span class="score"><span class="num">${fmt(c.fit_score)}</span><span class="score-bar"><i style="width:${c.fit_score || 0}%"></i></span></span></td>
                  <td><span class="pill ${{ high: "green", some: "blue" }[cp] || ""}" title="${esc(((c.carbon_pressure || {}).reasons || []).join("; "))}">${esc(cp)}</span></td>
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

  // ── Command palette ──────────────────────────────────
  const COMMANDS = [
    { label: "Today", hint: "1", run: () => (location.hash = "#today") },
    { label: "Review", hint: "2", run: () => (location.hash = "#review") },
    { label: "Quotes", hint: "3", run: () => (location.hash = "#quotes") },
    { label: "Carbon passports", hint: "4", run: () => ((carbon.view = "passports"), go("carbon")) },
    { label: "CBAM comparison", hint: "", run: () => ((carbon.view = "cbam"), go("carbon")) },
    { label: "Leads", hint: "5", run: () => (location.hash = "#leads") },
    { label: "Markets", hint: "6", run: () => (location.hash = "#markets") },
    { label: "Record a lot", hint: "", run: () => (go("carbon"), setTimeout(() => $("newLot").click(), 50)) },
    { label: "Quote the sample drawing", hint: "", run: () => (go("quotes"), setTimeout(() => $("useSample").click(), 50)) },
    { label: "Open the classic dashboard", hint: "", run: () => (location.href = "/classic") },
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
  function renderPalette(jobs) {
    const q = $("paletteInput").value.trim().toLowerCase();
    const cmds = COMMANDS.filter((c) => !q || c.label.toLowerCase().includes(q));
    palItems = [...cmds, ...jobs.map((j) => ({ label: j.part, hint: j.ref, job: j }))];
    palIndex = Math.min(palIndex, Math.max(0, palItems.length - 1));
    let html = cmds.length ? `<li class="pal-head" role="presentation">Go to</li>` : "";
    palItems.forEach((it, i) => {
      if (it.job && (i === cmds.length)) html += `<li class="pal-head" role="presentation">Past jobs</li>`;
      html += `<li role="option" id="pal-${i}" data-i="${i}" aria-selected="${i === palIndex}">${esc(it.label)}<small>${esc(it.hint)}</small></li>`;
    });
    if (!palItems.length) html = `<li class="pal-head" role="presentation">Nothing found</li>`;
    $("paletteList").innerHTML = html;
    $("paletteInput").setAttribute("aria-activedescendant", palItems.length ? `pal-${palIndex}` : "");
  }
  const palSearch = debounce(async () => {
    const q = $("paletteInput").value.trim();
    if (q.length < 3) return renderPalette([]);
    const { data } = await api(`/api/memory/search?q=${encodeURIComponent(q)}`);
    renderPalette(Array.isArray(data) ? data.slice(0, 5) : []);
  }, 200);
  $("paletteInput").addEventListener("input", () => ((palIndex = 0), renderPalette([]), palSearch()));
  function runPalette(i) {
    const it = palItems[i];
    if (!it) return;
    closePalette();
    if (it.job) {
      go("quotes");
      $("memoryQuery").value = it.job.ref + " " + it.job.part;
      $("memoryHits").innerHTML = jobRow(it.job);
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
      if (current === "review" || current === "carbon") (e.preventDefault(), moveIn(null, 1));
    } else if (e.key === "k" || e.key === "ArrowUp") {
      if (current === "review" || current === "carbon") (e.preventDefault(), moveIn(null, -1));
    } else if (e.key === "/") {
      e.preventDefault();
      openPalette();
    }
  });

  // ── Start ────────────────────────────────────────────
  if (navigator.platform && /Mac/.test(navigator.platform)) document.querySelectorAll(".search-pill kbd").forEach((k) => (k.textContent = "⌘K"));
  api("/api/review/summary").then(({ data }) => updateBadges(data && data.counts));
  api("/api/carbon/summary").then(({ data }) => {
    const b = $("navCarbonCount");
    if (data && data.drafts) (b.textContent = data.drafts), (b.hidden = false);
  });
  api("/api/review?view=approved").then(({ data }) => data && data.reviewer && ($("who").textContent = `Signed in as ${data.reviewer}`));
  show(location.hash.slice(1) || "today", { focus: false });
})();
