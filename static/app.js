
/* ---------------------------------------------------------
   Erlebnis-Portal – App JS
   - Portal hover glow follows cursor
   - Wizard (Dark Stage questions -> recommend -> reveal results)
   --------------------------------------------------------- */

function qs(sel, el=document){ return el.querySelector(sel); }
function qsa(sel, el=document){ return [...el.querySelectorAll(sel)]; }

function clamp(n, a, b){ return Math.max(a, Math.min(b, n)); }

async function jget(url){
  const r = await fetch(url, {headers: {"Accept":"application/json"}});
  if(!r.ok) throw new Error(`GET ${url} failed: ${r.status}`);
  return await r.json();
}
async function jpost(url, body){
  const r = await fetch(url, {
    method: "POST",
    headers: {"Content-Type":"application/json", "Accept":"application/json"},
    body: JSON.stringify(body)
  });
  if(!r.ok) throw new Error(`POST ${url} failed: ${r.status}`);
  return await r.json();
}

function setPortalGlow(el, ev){
  const rect = el.getBoundingClientRect();
  const x = clamp(((ev.clientX - rect.left) / rect.width) * 100, 0, 100);
  const y = clamp(((ev.clientY - rect.top) / rect.height) * 100, 0, 100);
  el.style.setProperty("--mx", `${x}%`);
  el.style.setProperty("--my", `${y}%`);
}

function enablePortalHover(){
  const portals = qsa(".portal, [data-portal-card]");
  portals.forEach(el => {
    el.addEventListener("mousemove", (ev) => setPortalGlow(el, ev));
    el.addEventListener("mouseenter", (ev) => setPortalGlow(el, ev));
  });
}

/* ------------- Wizard ------------- */

function openWizard(){
  const wiz = qs("#wizard");
  if(!wiz) return;
  wiz.hidden = false;
  document.body.classList.add("wizard-open");
  // prevent background scroll
  document.documentElement.style.overflow = "hidden";
}

function closeWizard(){
  const wiz = qs("#wizard");
  if(!wiz) return;
  wiz.hidden = true;
  document.body.classList.remove("wizard-open");
  document.documentElement.style.overflow = "";
}

function typeIn(el, text, {speed=16} = {}){
  // quick type effect, but keep it readable
  el.textContent = "";
  let i = 0;
  return new Promise(resolve => {
    const tick = () => {
      i = Math.min(text.length, i + 2);
      el.textContent = text.slice(0, i);
      if(i >= text.length) return resolve();
      setTimeout(tick, speed);
    };
    tick();
  });
}

function normalizeAnswers(collected, questions){
  // Convert collected option payloads into a compact scoring input for /api/recommend
  const out = {};

  // helper: find option details
  const byId = {};
  for(const q of questions){
    const opts = q.options || [];
    for(const o of opts){
      byId[`${q.id}:${o.id}`] = {q, o};
    }
  }

  // time_budget
  if(collected.time_budget){
    const {o} = byId[`time_budget:${collected.time_budget}`] || {};
    if(o){
      out.time_min_minutes = o.min_minutes ?? 0;
      out.time_max_minutes = o.max_minutes ?? 24*60;
    }
  }

  // travel_time_max
  if(collected.travel_time_max){
    const {o} = byId[`travel_time_max:${collected.travel_time_max}`] || {};
    if(o){
      out.max_travel_minutes = o.max_travel_minutes ?? 999;
    }
  }

  // mobility
  if(collected.mobility){
    const {o} = byId[`mobility:${collected.mobility}`] || {};
    if(o){
      out.modes = o.modes || [];
    }
  }

  // kids
  if(collected.kids){
    const {o} = byId[`kids:${collected.kids}`] || {};
    if(o){
      out.kids_selected = (o.kids === false) ? false : true;
      out.kid_age_group = o.kid_age_group || (o.kids === false ? null : "mixed");
    }
  }

  // vibe
  if(collected.vibe){
    // map to simple vibe key
    const id = collected.vibe;
    if(id === "v_calm") out.vibe = "calm";
    else if(id === "v_easy") out.vibe = "easy";
    else if(id === "v_sporty") out.vibe = "sporty";
    else if(id === "v_action") out.vibe = "action";
  }

  // setting
  if(collected.setting){
    const {o} = byId[`setting:${collected.setting}`] || {};
    if(o){
      out.setting = o.setting || "any";
    }
  }

  // budget (optional)
  if(collected.budget){
    const {o} = byId[`budget:${collected.budget}`] || {};
    if(o){
      out.max_eur_pp = o.max_eur_pp ?? 999;
    }
  }

  return out;
}

async function startWizardFlow(){
  const wiz = qs("#wizard");
  if(!wiz) return;

  const closeBtn = qs("#wizCloseBtn");
  const backBtn = qs("#wizBackBtn");
  const skipBtn = qs("#wizSkipBtn");
  const qEl = qs("#wizQuestion");
  const optsEl = qs("#wizOptions");
  const barEl = qs("#wizBar");
  const stepEl = qs("#wizStep");
  const stageEl = qs(".wizard__stage");
  const resultsEl = qs("#wizResults");
  const resultsGrid = qs("#resultsGrid");
  const restartBtn = qs("#wizRestartBtn");
  const shuffleBtn = qs("#wizShuffleBtn");

  // close hooks
  closeBtn?.addEventListener("click", closeWizard);
  qs(".wizard__backdrop")?.addEventListener("click", closeWizard);
  document.addEventListener("keydown", (e) => {
    if(!wiz.hidden && e.key === "Escape") closeWizard();
  });

  // load questions
  const qdata = await jget("/api/questions");
  const questions = qdata.wizard || [];
  if(!questions.length){
    qEl.textContent = "Keine Fragen konfiguriert.";
    return;
  }

  let idx = 0;
  let collected = {}; // {questionId: optionId}
  let renderedResults = null;

  function setProgress(){
    const total = questions.length;
    const pct = ((idx) / total) * 100;
    barEl.style.width = `${clamp(pct, 0, 100)}%`;
    stepEl.textContent = `${idx}/${total}`;
  }

  function renderOptions(q){
    optsEl.innerHTML = "";
    const opts = q.options || [];

    // skip button for optional question types
    const isOptional = (q.type || "").includes("optional");
    skipBtn.hidden = !isOptional;

    if(isOptional){
      skipBtn.onclick = () => {
        collected[q.id] = null;
        idx++;
        renderStep();
      };
    } else {
      skipBtn.onclick = null;
    }

    opts.forEach(o => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "opt";
      btn.innerHTML = `<span>${o.label}</span>` + (o.sub ? `<span class="opt__sub">${o.sub}</span>` : "");
      btn.addEventListener("click", () => {
        collected[q.id] = o.id;
        idx++;
        renderStep();
      });
      optsEl.appendChild(btn);
    });
  }

  async function renderStep(){
    // if results showing, do nothing
    if(idx >= questions.length){
      await renderResults();
      return;
    }

    resultsEl.hidden = true;
    stageEl.hidden = false;

    const q = questions[idx];
    setProgress();

    // back button
    backBtn.disabled = (idx <= 0);
    backBtn.onclick = () => {
      if(idx <= 0) return;
      idx--;
      renderStep();
    };

    // question
    await typeIn(qEl, q.text || "…", {speed: 14});
    renderOptions(q);
  }

  async function renderResults(){
    // lock stage, show results
    stageEl.hidden = true;
    resultsEl.hidden = false;

    const answers = normalizeAnswers(collected, questions);

    // request recommendations
    const rec = await jpost("/api/recommend", {answers, limit: 12});
    renderedResults = rec.items || [];

    resultsGrid.innerHTML = "";
    renderedResults.forEach((it, i) => {
      const card = document.createElement("a");
      card.className = "card";
      card.href = it.url;
      card.setAttribute("data-portal-card", "1");
      card.addEventListener("click", closeWizard);

      const img = it.image ? `<div class="card__media"><img src="/${it.image}" alt="" loading="lazy"></div>` : "";
      const emojis = (it.emoji_tags || []).slice(0,5).join(" ");
      const dur = it.duration ? `${it.duration.min_minutes || ""}–${it.duration.max_minutes || ""} min` : "";
      const dist = it.travel_from ? `${it.travel_from.min_minutes || ""}–${it.travel_from.max_minutes || ""} min Anfahrt` : "";

      const reasons = (it.reasons || []).slice(0,3).map(r => `<span class="chip">${r}</span>`).join("");

      card.innerHTML = `
        ${img}
        <div class="card__body">
          <div class="card__top">
            <div class="card__title">${it.title || ""}</div>
            <div class="card__emojis" aria-hidden="true">${emojis}</div>
          </div>
          <div class="card__summary">${it.summary || ""}</div>
          <div class="card__meta">
            ${dur ? `<span class="chip chip--strong">${dur}</span>` : ""}
            ${dist ? `<span class="chip">${dist}</span>` : ""}
          </div>
          <div class="card__meta" style="margin-top:10px;">
            ${reasons}
          </div>
        </div>
      `;
      resultsGrid.appendChild(card);
    });

    // progress to full
    barEl.style.width = "100%";
    stepEl.textContent = `${questions.length}/${questions.length}`;

    restartBtn.onclick = () => {
      idx = 0;
      collected = {};
      renderedResults = null;
      renderStep();
    };

    shuffleBtn.onclick = () => {
      if(!renderedResults || !renderedResults.length) return;
      const pick = renderedResults[Math.floor(Math.random() * renderedResults.length)];
      if(pick && pick.url) window.location.href = pick.url;
    };
  }

  // Start
  await renderStep();
}

function wireGlobalWizardButtons(){
  const openWizardBtn = qs("#openWizardBtn");
  openWizardBtn?.addEventListener("click", () => {
    openWizard();
    // only init flow once
    if(!window.__wizInit){
      window.__wizInit = true;
      startWizardFlow().catch(err => {
        console.error(err);
        const qEl = qs("#wizQuestion");
        if(qEl) qEl.textContent = "Fehler beim Laden der Auswahlhilfe.";
      });
    }
  });

  const portalWizard = qs("#portalWizard");
  portalWizard?.addEventListener("click", () => {
    openWizardBtn?.click();
  });
}

document.addEventListener("DOMContentLoaded", () => {
  enablePortalHover();
  wireGlobalWizardButtons();

  // Optional: open wizard via hash
  if(window.location.hash === "#wizard"){
    const btn = qs("#openWizardBtn");
    btn?.click();
  }
});
