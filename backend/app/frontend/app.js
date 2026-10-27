// BetWatch dashboard — renders /api/dashboard. Same interface as the design; all data comes from the backend.
(function () {
  "use strict";

  // ---------------------------------------------------------------- helpers
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const safeUrl = (u) => (/^https?:\/\//i.test(u || "") ? esc(u) : "#");
  const toneCls = (t) => "tone " + esc(t);
  const TONE_LABEL = { All: "All", Negative: "Bad news", Neutral: "Neutral", Positive: "Good news" };
  const inNum = (n) => (n == null ? "0" : Number(n).toLocaleString("en-IN"));

  function relTime(iso, fallback) {
    if (!iso && !fallback) return "";
    try {
      const d = new Date(iso || fallback);
      if (isNaN(d.getTime())) return `<span class="dt">${esc(fallback || iso)}</span>`;
      const diff = (Date.now() - d.getTime()) / 1000;
      let rel = "";
      if (diff < 60) rel = "just now";
      else if (diff < 3600) rel = Math.floor(diff / 60) + "m ago";
      else if (diff < 86400) rel = Math.floor(diff / 3600) + "h ago";
      else if (diff < 2592000) rel = Math.floor(diff / 86400) + "d ago";
      else rel = d.toLocaleDateString("en-IN", { day: "numeric", month: "short" });
      const full = d.toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" });
      return `<span class="dt rel-time" title="${esc(full)}">${esc(rel)}</span>`;
    } catch (_) {
      return `<span class="dt">${esc(fallback || iso)}</span>`;
    }
  }

  function initTheme() {
    const saved = localStorage.getItem("bw-theme");
    const prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    const theme = saved || (prefersDark ? "dark" : "light");
    document.documentElement.setAttribute("data-theme", theme);
    const btn = $("theme-toggle");
    if (btn) {
      btn.onclick = () => {
        const current = document.documentElement.getAttribute("data-theme") || "light";
        const next = current === "dark" ? "light" : "dark";
        document.documentElement.setAttribute("data-theme", next);
        localStorage.setItem("bw-theme", next);
      };
    }
  }

  const CATS = ["All", "Betting", "Law", "Gaming app", "Company", "Stock market"];
  const TIMES = [["day", "Daily"], ["week", "Weekly"], ["month", "Monthly"], ["year", "Yearly"], ["all", "All time"]];
  const SCOPES = [["Both", "National + Intl"], ["National", "National"], ["International", "International"]];
  const TONES = ["All", "Negative", "Neutral", "Positive"];
  const SUGGESTIONS = ["Mahadev", "Online Gaming Act", "Dream11", "Parimatch"];
  const PRESETS = [
    { id: "d-bet", label: "Daily betting news", s: { cat: "Betting", time: "day", tab: "feed" } },
    { id: "w-bet", label: "Weekly betting news", s: { cat: "Betting", time: "week", tab: "feed" } },
    { id: "m-bet", label: "Monthly betting news", s: { cat: "Betting", time: "month", tab: "feed" } },
    { id: "m-law", label: "Monthly new betting laws", s: { cat: "Law", time: "month", tab: "feed" } },
    { id: "y-law", label: "Yearly new betting laws", s: { cat: "Law", time: "year", tab: "feed" } },
    { id: "mahadev", label: "Mahadev betting coverage", s: { q: "Mahadev", cat: "All", time: "all", tab: "feed" } },
    { id: "plat", label: "Platform posts & pages", s: { tab: "social", time: "all" } },
    { id: "chan", label: "Check a channel", s: { tab: "channels", time: "all" } },
  ];
  const DEFAULTS = { q: "", cat: "All", time: "all", scope: "Both", outlet: "", journo: "", tone: "All", tab: "feed", preset: null };

  // State lives in the URL so views are shareable and survive refresh.
  const S = Object.assign({}, DEFAULTS);
  const params = new URLSearchParams(location.search);
  Object.keys(DEFAULTS).forEach((k) => { if (params.has(k)) S[k] = params.get(k); });
  let data = null, limit = 60, maxId = 0, pending = 0, inflight = null;

  function set(patch) {
    Object.assign(S, patch);
    limit = 60;
    const p = new URLSearchParams();
    Object.keys(DEFAULTS).forEach((k) => { if (S[k] && S[k] !== DEFAULTS[k]) p.set(k, S[k]); });
    history.replaceState(null, "", p.toString() ? "?" + p : location.pathname);
    load();
  }

  async function load() {
    const p = new URLSearchParams({ q: S.q, cat: S.cat, time: S.time, scope: S.scope, outlet: S.outlet, journo: S.journo, tone: S.tone, limit: String(limit) });
    if (inflight) inflight.abort();
    inflight = new AbortController();
    if (!data) {
      $("view").innerHTML = `
        <div class="skeleton skel-row"></div>
        <div class="skeleton skel-row"></div>
        <div class="skeleton skel-row"></div>
        <div class="skeleton skel-row"></div>
      `;
    }
    try {
      const r = await fetch("/api/dashboard?" + p, { signal: inflight.signal });
      if (!r.ok) throw new Error("HTTP " + r.status);
      data = await r.json();
      maxId = Math.max(maxId, data.meta.max_article_id || 0);
      render();
    } catch (e) {
      if (e.name !== "AbortError") $("view").innerHTML = `<div class="err">Could not load data: ${esc(e.message)}. Is the API running?</div>`;
    }
  }

  function renderActiveChips() {
    const c = $("active-filter-chips");
    const cont = $("active-chips-container");
    if (!c || !cont) return;

    const active = [];
    if (S.q) active.push({ label: `Subject: "${S.q}"`, clear: () => set({ q: "" }) });
    if (S.cat && S.cat !== "All") active.push({ label: `Category: ${S.cat}`, clear: () => set({ cat: "All" }) });
    if (S.time && S.time !== "all") {
      const tLabel = (TIMES.find(([v]) => v === S.time) || [])[1] || S.time;
      active.push({ label: `Time: ${tLabel}`, clear: () => set({ time: "all" }) });
    }
    if (S.scope && S.scope !== "Both") active.push({ label: `Scope: ${S.scope}`, clear: () => set({ scope: "Both" }) });
    if (S.outlet) active.push({ label: `Channel: ${S.outlet}`, clear: () => set({ outlet: "" }) });
    if (S.journo) active.push({ label: `Journalist: ${S.journo}`, clear: () => set({ journo: "" }) });
    if (S.tone && S.tone !== "All") active.push({ label: `Tone: ${TONE_LABEL[S.tone] || S.tone}`, clear: () => set({ tone: "All" }) });

    if (active.length === 0) {
      c.hidden = true;
      cont.innerHTML = "";
      return;
    }

    c.hidden = false;
    cont.innerHTML = active.map((a, idx) => `
      <span class="act-chip">
        ${esc(a.label)}
        <button type="button" class="act-chip-remove" data-act-idx="${idx}" aria-label="Remove filter">✕</button>
      </span>
    `).join("") + `<button type="button" class="act-chip-clear" id="act-clear-all">Clear all</button>`;

    cont.querySelectorAll(".act-chip-remove").forEach((btn) => {
      btn.onclick = (e) => {
        e.stopPropagation();
        const idx = Number(btn.getAttribute("data-act-idx"));
        if (active[idx]) active[idx].clear();
      };
    });

    const clearAllBtn = $("act-clear-all");
    if (clearAllBtn) {
      clearAllBtn.onclick = () => set(Object.assign({}, DEFAULTS));
    }
  }

  // ---------------------------------------------------------------- render
  function render() {
    const d = data, m = d.meta;
    const counts = `${inNum(m.total_stories)} STORIES · ${inNum(m.total_posts)} POSTS · ${inNum(m.total_outlets)} OUTLETS`;
    $("totals").textContent = (m.is_live ? "LIVE DATA · " : "NOT UPDATING · ") + counts;
    $("totals").classList.toggle("stale", !m.is_live);
    const delay = m.median_delay_minutes != null ? ` · median delay ${m.median_delay_minutes} min` : "";
    const failing = m.sources_failing ? ` · ${m.sources_failing} source(s) failing` : "";
    $("freshness").textContent = m.is_live
      ? `Updated ${m.last_success}${delay}${failing}`
      : `Last collected ${m.last_success} — the collector is not running, so nothing newer is shown. Start it (EMBEDDED_WORKER=true or python -m app.worker).`;

    $("subject").value = document.activeElement === $("subject") ? $("subject").value : S.q;
    $("suggestions").innerHTML = `<span class="muted">Try:</span>` + SUGGESTIONS.map((w) => `<button type="button" class="sugg" data-q="${esc(w)}">${esc(w)}</button>`).join("");
    $("cats").innerHTML = d.cats.map((c) => `<button type="button" class="chip${S.cat === c.label ? " on" : ""}" data-cat="${esc(c.label)}">${esc(c.label)}<span class="ct">${inNum(c.count)}</span></button>`).join("");
    $("times").innerHTML = TIMES.map(([v, l]) => `<button type="button" class="seg${S.time === v ? " on" : ""}" data-time="${v}">${l}</button>`).join("");
    $("scopes").innerHTML = SCOPES.map(([v, l]) => `<button type="button" class="seg${S.scope === v ? " on" : ""}" data-scope="${v}">${l}</button>`).join("");
    $("tones").innerHTML = TONES.map((t) => `<button type="button" class="seg${S.tone === t ? " on" : ""}" data-tone="${t}">${TONE_LABEL[t]}</button>`).join("");
    $("ch").innerHTML = `<option value="">All channels (${inNum(d.outlet_options.length)})</option>` + d.outlet_options.map((o) => `<option value="${esc(o.name)}"${o.name === S.outlet ? " selected" : ""}>${esc(o.name)} (${inNum(o.count)})</option>`).join("");
    $("jr").innerHTML = `<option value="">All journalists</option>` + d.journo_options.map((o) => `<option value="${esc(o.name)}"${o.name === S.journo ? " selected" : ""}>${esc(o.name)} (${inNum(o.count)})</option>`).join("");
    $("clear").hidden = !(S.q || S.outlet || S.journo || S.tone !== "All" || S.cat !== "All" || S.scope !== "Both");

    renderActiveChips();

    $("heading").textContent = d.heading;
    const k = d.k;
    $("kpis").innerHTML = `
      <div class="kpi"><span class="lbl">Total posts</span><span class="v">${inNum(k.posts)}</span><span class="s">times this was published</span></div>
      <div class="kpi"><span class="lbl">Stories</span><span class="v">${inNum(k.stories)}</span><span class="s">distinct news events</span></div>
      <div class="kpi"><span class="lbl">Channels</span><span class="v">${inNum(k.outlets)}</span><span class="s">outlets that covered it</span></div>
      <div class="kpi"><span class="lbl">Journalists</span><span class="v">${inNum(k.journos)}</span><span class="s">${inNum(k.noByline)} posts without byline</span></div>
      <div class="kpi"><span class="lbl">Videos · Social</span><span class="v">${inNum(k.videos)} · ${inNum(k.social)}</span><span class="s">video reports · social posts</span></div>
      <div class="kpi wide"><span class="lbl">First reported</span><span class="big">${esc(k.first)}</span><span class="s">${esc(k.firstDate)}</span></div>
      <div class="kpi wide"><span class="lbl">Tone split</span>
        <div class="split" aria-hidden="true"><div style="width:${k.negPct}%;background:#C2410C"></div><div style="width:${k.neuPct}%;background:#9CA3AF"></div><div style="width:${k.posPct}%;background:#1D4ED8"></div></div>
        <span class="s">Negative ${inNum(k.neg)} · Neutral ${inNum(k.neu)} · Positive ${inNum(k.pos)}</span></div>`;

    $("presets").innerHTML = PRESETS.map((p) => `<button type="button" class="preset${S.preset === p.id ? " on" : ""}" data-preset="${p.id}"><span>${esc(p.label)}</span><span class="x">${d.presets[p.id] != null ? inNum(d.presets[p.id]) : ""}</span></button>`).join("");

    const tabs = [["feed", "News feed", d.stories_total], ["channels", "Channels", d.channels.filter((c) => c.n).length],
      ["journos", "Journalists", d.journalists.length], ["az", "A–Z subjects", null], ["live", "Live", d.live.length],
      ["video", "YouTube & video", d.videos.length], ["social", "Social media", d.social.length + d.pages.length]];
    $("tabs").innerHTML = tabs.map(([id, l, n]) => `<button type="button" role="tab" class="tab${S.tab === id ? " on" : ""}" aria-selected="${S.tab === id}" data-tab="${id}">${l}${n != null ? `<span class="ct">${inNum(n)}</span>` : ""}</button>`).join("");

    const views = { feed: viewFeed, channels: viewChannels, journos: viewJournos, az: viewAZ, live: viewLive, video: viewVideo, social: viewSocial };
    (views[S.tab] || viewFeed)(d);

    $("rail-sub").textContent = `Which news platform to buy — across all ${inNum(m.total_posts)} collected posts`;
    const rank = (rows, cls) => rows.length ? rows.map((r) => `<li class="rk"><button type="button" class="lst rk-btn" data-goto='${esc(JSON.stringify(r.goto))}' title="Show all ${inNum(r.n)} posts from ${esc(r.name)}"><span><span class="mono muted rk-n">${r.rank}</span>${esc(r.name)}</span><span class="n">${inNum(r.n)}</span></button><div class="track"><div class="bar ${cls}" style="width:${r.pct}%"></div></div><a class="rk-link" href="/channel/${r.outlet_id}" target="_blank" rel="noopener">View channel</a></li>`).join("") : `<li class="muted">No data yet</li>`;
    if ($("rank-mah-title")) $("rank-mah-title").textContent = d.subject_heading || ("Most " + (S.q || "Mahadev") + " betting posts");
    $("rank-mah").innerHTML = rank(d.rank_subject || d.rank_mahadev, "blue");
    $("rank-bet").innerHTML = rank(d.rank_betting, "");
    loadSaved();
    loadKeywords();
  }

  function postLine(p) {
    return `<div class="prow">${p.is_first ? `<span class="first">1ST</span>` : ""}<span class="o">${esc(p.o)}</span><span>${esc(p.by)}</span><span class="d">${esc(p.dl)}</span><span>${esc(p.tone)}</span><a href="${safeUrl(p.url)}" target="_blank" rel="noopener noreferrer" style="margin-left:auto">Open</a></div>`;
  }

  function viewFeed(d) {
    if (!d.stories.length) { $("view").innerHTML = `<div class="ph empty">No stories match these filters yet. Try a longer timeframe — the collector keeps adding coverage.</div>`; return; }
    const srcLabel = { ai: "AI SUMMARY", publisher: "PUBLISHER EXCERPT", none: "NO SUMMARY" };
    const rows = d.stories.map((s) => {
      const firstPost = s.posts[0] || {};
      const byline = firstPost.by && firstPost.by !== "Byline not captured" ? firstPost.by : "byline n/a";
      return `
      <article class="row" data-story="${s.id}" tabindex="0" role="button" aria-label="Open story ${esc(s.title)}">
        ${relTime(s.last_seen || s.first_seen, s.date_label)}
        <div class="txt">
          <div class="ttl">${esc(s.title)}</div>
          <div class="meta">First: <strong style="color:var(--accent-orange)">${esc(s.first_name)}</strong> · ${inNum(s.outlet_count)} channels · <span class="tag ${s.scope === "National" ? "nat" : "intl"}">${esc(s.scope)}</span></div>
        </div>
        <span class="${toneCls(s.tone)}">${esc(s.tone)}</span>
        <div class="cnt"><b>${inNum(s.post_count)}</b><span>posts</span></div>
        <a href="/story/${s.id}" target="_blank" rel="noopener" class="peek open-story-btn" title="Open full story in new tab" aria-label="Open story in new tab" onclick="event.stopPropagation()">View ↗</a>
        <button type="button" class="peek info-btn" aria-label="Toggle story preview" data-toggle="${s.id}" onclick="event.stopPropagation()"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="9"></circle><path d="M12 11v5M12 8h.01"></path></svg></button>
        <div class="pop" role="dialog" aria-label="Story details" data-pop="${s.id}" onclick="event.stopPropagation()">
          <div class="tags" style="margin-bottom:10px">${s.cats.map((c) => `<span class="tag">${esc(c)}</span>`).join("")}<span class="${toneCls(s.tone)}">${esc(s.tone)}</span></div>
          <div class="ptitle" style="font-size:16px;margin-bottom:6px">${esc(s.title)}</div>
          <div style="font-size:12px;color:#D1D5DB;margin-bottom:8px">
            First: <strong style="color:#FDBA74">${esc(s.first_name)}</strong> · By: <span>${esc(byline)}</span> · <span class="mono">${esc(s.date_label)}</span> · <span>${s.post_count} posts</span>
          </div>
          <div class="lbl" style="margin-bottom:4px">Summary <span class="srcnote">${srcLabel[s.summary_source] || "SUMMARY"}</span></div>
          <p style="margin:0 0 10px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden">${esc(s.summary)}</p>
          ${s.highlights && s.highlights.length ? `<div class="lbl" style="margin-bottom:4px">What outlets highlighted</div><ul style="margin:0 0 10px;padding-left:18px">${s.highlights.slice(0, 2).map((h) => `<li>${esc(h)}</li>`).join("")}</ul>` : ""}
          <div class="lbl" style="margin-bottom:4px">Posts (${s.post_count}) — channel · journalist · date · tone</div>
          ${s.posts.slice(0, 3).map(postLine).join("")}
          ${s.posts.length > 3 ? `<div class="muted small" style="margin-top:4px;color:#9CA3AF">+ ${s.posts.length - 3} more posts in full story view</div>` : ""}
          <div style="display:flex;justify-content:space-between;align-items:center;margin-top:10px;padding-top:10px;border-top:1px solid #374151">
            <span style="font-size:12px;color:#9CA3AF">${esc(s.date_note)}</span>
            <a href="/story/${s.id}" target="_blank" rel="noopener" class="btn-amber" style="min-height:32px;line-height:32px;padding:0 14px;font-size:13px;border-radius:6px;font-weight:700;color:#111827 !important;background:#FDBA74 !important;text-decoration:none !important;display:inline-block" onclick="event.stopPropagation()">View full story ↗</a>
          </div>
        </div>
      </article>`;
    }).join("");
    const more = d.stories_total > d.stories.length ? `<button type="button" class="peek more" id="more">Load more (${d.stories_total - d.stories.length} more stories)</button>` : "";
    $("view").innerHTML = `<span class="muted">One line per story. Hover a row, or press info button, for summary preview. Click any row to open the full story page.</span>${rows}${more}`;
  }

  function postBlock(p, extra) {
    return `<div class="post"><div class="tags"><span class="mono muted">${esc(p.dl)}</span><span class="${toneCls(p.tone)}">${esc(p.tone)}</span>${extra || ""}${p.is_first ? `<span class="tag nat">First to report</span>` : ""}</div>
      <div class="h">${esc(p.h)}</div><div class="m">By ${esc(p.by)}${p.story ? " · Story: " + esc(p.story) : ""} · <a href="${safeUrl(p.url)}" target="_blank" rel="noopener noreferrer">Open post</a></div></div>`;
  }

  function viewChannels(d) {
    const list = d.channels.map((c) => `<button type="button" class="lst${S.outlet === c.name ? " on" : ""}${c.n ? "" : " zero"}" data-outlet="${esc(c.name)}"><span><span>${esc(c.name)}</span><span class="k">${esc(c.kind)} · ${c.total} posts total</span></span><span class="n">${c.n}</span></button>`).join("");
    let detail = `<div style="font-size:15px;color:var(--text-main);line-height:1.5">Pick a channel to check whether it ever covered this subject and see every post it published.</div>`;
    const cd = d.channel_detail;
    if (cd) {
      const subj = S.q ? `“${esc(S.q)}”` : "these filters";
      const resetBtn = cd.reason_kind === "filters" ? ` <button type="button" class="peek" id="clear" style="margin-top:10px">Reset filters</button>` : "";
      const reasonHtml = cd.reason ? `<div class="ph" style="margin:12px 0;line-height:1.5">${esc(cd.reason)}${resetBtn}</div>` : "";
      const profileBtn = cd.outlet_id ? `<a href="/channel/${cd.outlet_id}" target="_blank" rel="noopener" class="peek" style="text-decoration:none;font-weight:600">View channel profile →</a>` : "";
      detail = `<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px"><div><div class="lbl">${esc(cd.kind)}</div><h2>${esc(cd.name)}</h2></div>${profileBtn}</div>
        <div class="tags" style="margin-bottom:6px"><span class="tone ${cd.covered ? "Positive" : "Negative"}">${cd.covered ? "YES — COVERED" : "NO COVERAGE FOUND"}</span>
        <span style="font-size:14px;color:var(--text-main)">${cd.covered ? `${cd.count} post${cd.count > 1 ? "s" : ""} on ${subj} · ${cd.total} in total` : `Nothing on ${subj} yet · ${cd.total} posts on other topics`}</span></div>
        ${reasonHtml}
        ${cd.posts.map((p) => postBlock(p)).join("")}`;
    }
    $("view").innerHTML = `<div class="split2"><div class="card list"><div class="lbl" style="padding:10px 14px">All channels A–Z · posts on this subject</div>${list}</div><div class="card detail">${detail}</div></div>`;
  }

  function viewJournos(d) {
    const list = d.journalists.map((j) => `<button type="button" class="lst${S.journo === j.name ? " on" : ""}" data-journo="${esc(j.name)}"><span><span>${esc(j.name)}</span><span class="k">${esc(j.outlets)}</span></span><span class="n">${j.n}</span></button>`).join("") || `<div class="ph" style="margin:12px">No posts in this view.</div>`;
    let detail = `<div style="font-size:15px;color:var(--text-main);line-height:1.5">Pick a journalist to see everything they posted on this subject and whether their coverage reads as good or bad news.<br><br><span class="muted">Bylines come from publisher feeds and article metadata. Google News results carry no byline.</span></div>`;
    const jd = d.journalist_detail;
    if (jd) detail = `<h2 style="margin-top:0">${esc(jd.name)}</h2><div style="font-size:14px;color:var(--text-main);margin-bottom:6px">${jd.count} post(s) · Negative ${jd.neg} · Neutral ${jd.neu} · Positive ${jd.pos}</div>` +
      jd.posts.map((p) => postBlock(p, `<span class="tag">${esc(p.o)}</span>`)).join("");
    $("view").innerHTML = `<div class="split2"><div class="card list"><div class="lbl" style="padding:10px 14px">Journalists &amp; bylines</div>${list}</div><div class="card detail">${detail}</div></div>`;
  }

  async function viewAZ() {
    $("view").innerHTML = `<div class="card">Loading subjects…</div>`;
    const rows = await (await fetch("/api/subjects")).json();
    $("view").innerHTML = `<div class="card" style="padding:0;overflow-x:auto"><table class="az"><thead><tr><th class="lbl" style="padding-left:16px">Subject A–Z</th><th class="lbl num">Posts</th><th class="lbl num">Stories</th><th class="lbl num">Channels</th><th class="lbl">First seen</th><th class="lbl">Latest</th></tr></thead><tbody>` +
      rows.map((a) => `<tr><td style="padding:4px 8px"><button type="button" class="lst" style="border:0;padding:8px" data-az="${esc(a.name)}"><span><span class="mono" style="color:#9A3412;margin-right:10px">${esc(a.letter)}</span>${esc(a.name)}</span></button></td>
        <td class="num"><b>${a.posts}</b></td><td class="num">${a.stories}</td><td class="num">${a.outlets}</td><td class="mono muted">${esc(a.first)}</td><td class="mono muted">${esc(a.last)}</td></tr>`).join("") +
      `</tbody></table></div><div class="ph">Add new subjects to track in <a href="/admin">Admin</a> — collection for a new subject starts immediately.</div>`;
  }

  function viewLive(d) {
    const status = d.live.length
      ? `<span class="tag live">LIVE</span><span style="font-size:14px;color:var(--text-main)">${d.live.length} live item(s) on “${esc(d.subject_name)}” in the last 24 hours</span>`
      : `<span class="tag">NO LIVE COVERAGE DETECTED</span><span style="font-size:14px;color:var(--text-main)">on “${esc(d.subject_name)}” in the last 24 hours</span>`;
    const yt = d.meta.youtube_enabled ? "" : `<div class="ph"><strong>YouTube Live</strong> needs YOUTUBE_API_KEY. Live blogs are detected from headlines now.</div>`;
    $("view").innerHTML = `<div class="card stack"><div class="tags">${status}</div>
      <div style="font-size:14px;line-height:1.55;color:var(--text-main)">Live blogs (headlines marked “live updates”) and YouTube live streams on this subject appear here.</div>
      ${d.live.map((p) => postBlock(p, `<span class="tag">${esc(p.o)}</span><span class="tag live">LIVE</span>`)).join("")}</div>${yt}`;
  }

  function viewVideo(d) {
    const cards = d.videos.map((p) => `<div class="card" style="display:flex;flex-wrap:wrap;gap:16px;align-items:center">
      ${p.thumbnail ? `<img class="thumb" src="${safeUrl(p.thumbnail)}" alt="" loading="lazy">` : `<div class="thumb" aria-hidden="true"></div>`}
      <div style="flex:1 1 260px;min-width:0;display:flex;flex-direction:column;gap:6px">
        <div class="tags"><span class="tag">${esc(p.o)}</span><span class="mono muted">${esc(p.dl)}</span><span class="${toneCls(p.tone)}">${esc(p.tone)}</span>${p.is_live ? `<span class="tag live">LIVE</span>` : ""}</div>
        <div class="h" style="font-size:15px;font-weight:600;line-height:1.4">${esc(p.h)}</div>
        <div class="m muted">Posted by ${esc(p.o)} · <a href="${safeUrl(p.url)}" target="_blank" rel="noopener noreferrer">Watch</a></div></div></div>`).join("");
    const note = `<div class="ph">Showing video reports from YouTube and news broadcasters for “${esc(d.subject_name)}”.</div>`;
    $("view").innerHTML = (cards || `<div class="ph empty">No videos on this subject in this timeframe.</div>`) + note;
  }

  function viewSocial(d) {
    const posts = d.social.map((p) => `<div class="card stack" style="gap:6px"><div class="tags"><span class="tag intl">${esc(p.platform || "Social")}</span><strong style="font-size:14px">${esc(p.by)}</strong><span class="mono muted">${esc(p.dl)}</span><span class="${toneCls(p.tone)}">${esc(p.tone)}</span></div>
      ${p.thumbnail ? `<div style="margin:6px 0"><img src="${safeUrl(p.thumbnail)}" alt="" style="max-height:260px;border-radius:8px;object-fit:cover;box-shadow:0 1px 3px rgba(0,0,0,0.1)" loading="lazy"></div>` : ""}
      <div style="font-size:15px;line-height:1.45">${esc(p.h)}</div><div class="m muted">${esc(p.o)} · <a href="${safeUrl(p.url)}" target="_blank" rel="noopener noreferrer">Open post</a></div></div>`).join("");
    const pages = d.pages.map((pg) => `<div style="display:flex;flex-wrap:wrap;justify-content:space-between;gap:8px;padding:14px 18px;border-bottom:1px solid #F3F4F6"><span><strong>${esc(pg.handle)}</strong> <span class="muted">· ${esc(pg.net)}</span></span><span class="mono">${pg.n} posts</span></div>`).join("");
    const note = `<div class="ph">Showing social media coverage across Instagram, Reddit, and Bluesky.</div>`;
    $("view").innerHTML = `<div class="lbl">Posts on social media</div>${posts || `<div class="ph">No social posts on this subject yet.</div>`}
      <div class="lbl" style="margin-top:8px">Pages, channels &amp; accounts covering this subject</div><div class="card" style="padding:0">${pages || `<div class="ph" style="margin:12px">None yet.</div>`}</div>${note}`;
  }

  // ---------------------------------------------------------------- saved searches & keywords
  async function loadSaved() {
    try {
      const rows = await (await fetch("/api/saved-searches")).json();
      $("saved").innerHTML = rows.length ? rows.map((r) => `<div style="display:flex;gap:4px"><button type="button" class="preset" data-saved='${esc(JSON.stringify(r.params))}'><span>${esc(r.name)}</span></button><button type="button" class="peek" aria-label="Delete saved search" data-del-saved="${r.id}">×</button></div>`).join("") : `<div class="muted" style="padding:0 12px;font-size:13px">None yet</div>`;
    } catch (e) { /* optional */ }
  }

  async function loadKeywords() {
    try {
      const res = await fetch("/api/keywords");
      if (!res.ok) return;
      const keywords = await res.json();
      const el = $("keywords-list");
      if (!el) return;
      if (!keywords.length) {
        el.innerHTML = `<div class="muted" style="padding:0 12px;font-size:13px">No saved keywords yet</div>`;
        return;
      }
      el.innerHTML = keywords.map((k) => {
        const isCollecting = k.status && k.status.startsWith("Collecting");
        const isFailed = k.status && k.status.startsWith("Failed");
        const statusColor = isCollecting ? "#B45309" : (isFailed ? "#B91C1C" : "#047857");
        const statusBg = isCollecting ? "#FEF3C7" : (isFailed ? "#FEE2E2" : "#D1FAE5");
        return `
          <div class="kw-card" style="display:flex;flex-direction:column;gap:4px;padding:6px 10px;border-radius:8px;background:#FFFFFF;border:1px solid #E5E7EB;margin-bottom:6px">
            <div style="display:flex;align-items:center;justify-content:space-between;gap:6px">
              <button type="button" class="kw-btn" data-search-kw="${esc(k.query || k.name)}" style="border:0;background:transparent;text-align:left;font-weight:600;font-size:13px;color:#111827;cursor:pointer;flex:1;padding:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
                ${esc(k.name)}
              </button>
              <span class="mono" style="font-size:12px;font-weight:600;color:#9A3412">${k.posts}</span>
            </div>
            <div style="display:flex;align-items:center;justify-content:space-between;gap:4px;font-size:11px">
              <span style="padding:2px 6px;border-radius:4px;font-family:'IBM Plex Mono',monospace;background:${statusBg};color:${statusColor}" title="${esc(k.status)}">${esc(k.status)}</span>
              <div style="display:flex;gap:2px">
                <button type="button" class="peek" style="min-width:24px;min-height:24px;padding:0 4px;font-size:11px" data-edit-kw="${k.id}" data-name="${esc(k.name)}" data-query="${esc(k.query)}" title="Edit keyword">✎</button>
                <button type="button" class="peek" style="min-width:24px;min-height:24px;padding:0 4px;font-size:11px" data-toggle-kw="${k.id}" data-enabled="${k.enabled}" title="${k.enabled ? 'Pause collection' : 'Resume collection'}">${k.enabled ? '⏸' : '▶'}</button>
                <button type="button" class="peek" style="min-width:24px;min-height:24px;padding:0 4px;font-size:11px" data-del-kw="${k.id}" title="Delete keyword">×</button>
              </div>
            </div>
          </div>
        `;
      }).join("");
    } catch (e) { /* optional */ }
  }

  // ---------------------------------------------------------------- hover preview (150ms delay)
  let hoverTimer = null;
  let activeHoverRow = null;

  function clearHoverTimer() {
    if (hoverTimer) { clearTimeout(hoverTimer); hoverTimer = null; }
  }

  function handleRowEnter(row) {
    clearHoverTimer();
    if (activeHoverRow && activeHoverRow !== row) {
      activeHoverRow.classList.remove("hover-open");
    }
    activeHoverRow = row;
    row.classList.add("hover-open");
  }

  function handleRowLeave(row) {
    clearHoverTimer();
    hoverTimer = setTimeout(() => {
      row.classList.remove("hover-open");
      if (activeHoverRow === row) activeHoverRow = null;
    }, 150);
  }

  document.addEventListener("mouseover", (e) => {
    const row = e.target.closest(".row");
    if (row) handleRowEnter(row);
  });

  document.addEventListener("mouseout", (e) => {
    const row = e.target.closest(".row");
    if (row) {
      const rel = e.relatedTarget;
      if (!rel || !row.contains(rel)) handleRowLeave(row);
    }
  });

  // ---------------------------------------------------------------- events (delegated)
  document.addEventListener("click", async (e) => {
    const t = e.target.closest("button, [data-search-kw]");
    if (t) {
      const ds = t.dataset;
      if (ds.searchKw) { set({ q: ds.searchKw, time: "all", preset: null }); return; }
      if (ds.editKw) {
        const newName = prompt("Rename keyword:", ds.name);
        if (newName && newName.trim() && newName.trim() !== ds.name) {
          await fetch("/api/keywords/" + ds.editKw, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: newName.trim() })
          });
          loadKeywords();
        }
        return;
      }
      if (ds.toggleKw) {
        const en = ds.enabled !== "false";
        await fetch("/api/keywords/" + ds.toggleKw, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: !en })
        });
        loadKeywords();
        return;
      }
      if (ds.delKw) {
        if (confirm("Delete this saved keyword?")) {
          await fetch("/api/keywords/" + ds.delKw, { method: "DELETE" });
          loadKeywords();
        }
        return;
      }
      if (t.id === "save-keyword") {
        const cur = S.q || $("subject").value.trim();
        const kw = prompt("Enter keyword to track and collect news for:", cur || "");
        if (kw && kw.trim()) {
          try {
            const r = await fetch("/api/keywords", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ name: kw.trim(), query: kw.trim() })
            });
            if (r.ok) {
              loadKeywords();
              set({ q: kw.trim(), time: "all" });
            } else {
              const err = await r.json();
              alert(err.detail || "Could not save keyword");
            }
          } catch (err) {
            alert("Error saving keyword: " + err.message);
          }
        }
        return;
      }
      if (t.id === "export-pdf") {
        const p = new URLSearchParams({ q: S.q, cat: S.cat, time: S.time, scope: S.scope, outlet: S.outlet, journo: S.journo, tone: S.tone });
        window.open("/api/reports/subject.pdf?" + p, "_blank");
        return;
      }
      if (ds.q) set({ q: ds.q, time: "all", preset: null });
      else if (ds.cat) set({ cat: ds.cat, preset: null });
      else if (ds.time) set({ time: ds.time, preset: null });
      else if (ds.scope) set({ scope: ds.scope });
      else if (ds.tone) set({ tone: ds.tone });
      else if (ds.tab) set({ tab: ds.tab });
      else if (ds.outlet !== undefined) set({ outlet: S.outlet === ds.outlet ? "" : ds.outlet });
      else if (ds.goto) { const g = JSON.parse(ds.goto); set(Object.assign({ preset: null }, g)); }
      else if (ds.journo !== undefined) set({ journo: S.journo === ds.journo ? "" : ds.journo });
      else if (ds.az) set({ q: ds.az, tab: "feed", time: "all", cat: "All", preset: null });
      else if (ds.preset) { const p = PRESETS.find((x) => x.id === ds.preset); set(Object.assign({ q: "", cat: "All", scope: "Both", outlet: "", journo: "", tone: "All", preset: p.id }, p.s)); }
      else if (ds.saved) set(Object.assign({}, DEFAULTS, JSON.parse(ds.saved), { tab: "feed" }));
      else if (ds.delSaved) { await fetch("/api/saved-searches/" + ds.delSaved, { method: "DELETE" }); loadSaved(); }
      else if (ds.toggle) { const row = t.closest(".row"); const open = row.classList.toggle("open"); t.setAttribute("aria-expanded", String(open)); }
      else if (t.id === "more") { limit += 60; load(); }
      else if (t.id === "clear") set({ q: "", cat: "All", scope: "Both", outlet: "", journo: "", tone: "All", preset: null });
      else if (t.id === "save") {
        const name = prompt("Name this search", S.q || S.cat + " · " + S.time);
        if (name) { await fetch("/api/saved-searches", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, params: { q: S.q, cat: S.cat, time: S.time, scope: S.scope, outlet: S.outlet, journo: S.journo, tone: S.tone } }) }); loadSaved(); }
      } else if (t.id === "newbtn") { $("newbar").hidden = true; pending = 0; load(); }
      return;
    }

    // Row click opening full story in new tab
    const row = e.target.closest(".row");
    if (row && row.dataset.story && !e.target.closest("a, button, .pop")) {
      window.open("/story/" + row.dataset.story, "_blank");
    }
  });

  let qTimer;
  $("subject").addEventListener("input", (e) => { clearTimeout(qTimer); qTimer = setTimeout(() => set({ q: e.target.value.trim(), preset: null }), 350); });
  $("ch").addEventListener("change", (e) => set({ outlet: e.target.value }));
  $("jr").addEventListener("change", (e) => set({ journo: e.target.value }));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      document.querySelectorAll(".row.open, .row.hover-open").forEach((r) => {
        r.classList.remove("open");
        r.classList.remove("hover-open");
      });
    } else if (e.key === "Enter" && e.target.classList && e.target.classList.contains("row") && e.target.dataset.story) {
      window.open("/story/" + e.target.dataset.story, "_blank");
    }
  });

  // ---------------------------------------------------------------- live updates (SSE)
  function connect() {
    const es = new EventSource("/api/stream" + (maxId ? "?since=" + maxId : ""));
    es.addEventListener("new", (ev) => {
      const p = JSON.parse(ev.data);
      maxId = p.max_id; pending += p.new;
      const latest = p.latest && p.latest[0] ? ` — latest: “${p.latest[0].title}” (${p.latest[0].outlet})` : "";
      $("newbtn").textContent = `${pending} new post${pending > 1 ? "s" : ""} collected${latest}. Click to refresh.`;
      $("newbar").hidden = false;
    });
    es.onerror = () => { es.close(); setTimeout(connect, 15000); };
  }

  initTheme();
  load().then(connect);
})();
