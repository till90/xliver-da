#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import re
from typing import Any, Dict, List, Tuple, Optional

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    render_template_string,
    request,
    send_from_directory,
    url_for,
)

# ------------------------------------------------------------
# Config / Paths
# ------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(BASE_DIR, "config")
ERLEBNIS_DIR = os.path.join(BASE_DIR, "erlebnisse")
IMG_DIR = os.path.join(BASE_DIR, "img")

APP_TITLE = os.getenv("APP_TITLE", "Erlebnis-Portal")
APP_SUBTITLE = os.getenv("APP_SUBTITLE", "Wenn niemand weiß, was man machen soll.")
PRIMARY_CITY = os.getenv("PRIMARY_CITY", "Darmstadt")

PORT = int(os.getenv("PORT", "8080"))

SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9\-]{0,80}[a-z0-9])?$")

# ------------------------------------------------------------
# Flask App
# ------------------------------------------------------------

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["JSON_SORT_KEYS"] = False
app.config["JSON_AS_ASCII"] = False

_JSON_CACHE: Dict[str, Tuple[float, Any]] = {}


@app.after_request
def _add_headers(resp: Response) -> Response:
    # Static + JSON friendly defaults; you can tighten CSP later if you want.
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


def _read_json_cached(path: str) -> Any:
    try:
        mtime = os.path.getmtime(path)
    except FileNotFoundError:
        raise

    hit = _JSON_CACHE.get(path)
    if hit and hit[0] == mtime:
        return hit[1]

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    _JSON_CACHE[path] = (mtime, data)
    return data


def load_categories() -> Dict[str, Any]:
    return _read_json_cached(os.path.join(CONFIG_DIR, "categories.json"))


def load_tags() -> Dict[str, Any]:
    return _read_json_cached(os.path.join(CONFIG_DIR, "tags.json"))


def load_questions() -> Dict[str, Any]:
    return _read_json_cached(os.path.join(CONFIG_DIR, "questions.json"))


def load_index() -> Dict[str, Any]:
    idx_path = os.path.join(ERLEBNIS_DIR, "index.json")
    return _read_json_cached(idx_path)


def _find_index_item_by_slug(slug: str) -> Optional[Dict[str, Any]]:
    idx = load_index()
    for it in idx.get("items", []):
        if it.get("slug") == slug:
            return it
    return None


def _safe_slug(slug: str) -> str:
    slug = (slug or "").strip().lower()
    if not SLUG_RE.match(slug):
        abort(404)
    return slug


def _overlap(a_min: int, a_max: int, b_min: int, b_max: int) -> bool:
    return not (a_max < b_min or a_min > b_max)


def _has_any_tag(item: Dict[str, Any], tags: List[str]) -> bool:
    itags = set(item.get("tags", []) or [])
    return any(t in itags for t in tags)


def _recommend(answers: Dict[str, Any], limit: int = 12) -> List[Dict[str, Any]]:
    """
    Recommendation philosophy:
      - Hard filters for the most user-intent-critical constraints (time, distance, setting, budget if strict).
      - Scoring for "vibe", kids-fit, mobility realism, and niceness bonuses.
    """
    idx = load_index()
    items = idx.get("items", []) or []

    # Extract answer payload with sensible defaults
    time_min = int(answers.get("time_min_minutes") or 0)
    time_max = int(answers.get("time_max_minutes") or 24 * 60)

    max_travel = int(answers.get("max_travel_minutes") or 999)

    # mobility modes list: ["walk","public"] / ["bike"] / ["car"] / ...
    mobility_modes = answers.get("modes") or []
    mobility_modes = [str(m) for m in mobility_modes] if isinstance(mobility_modes, list) else []

    kid_age_group = answers.get("kid_age_group")  # "0-5" / "6-10" / "11+" / "mixed"
    kids_selected = answers.get("kids_selected")  # bool or None

    vibe = answers.get("vibe")  # "calm" / "easy" / "sporty" / "action"
    setting = answers.get("setting")  # "indoor" / "outdoor" / "mixed" / "any"

    budget_max = answers.get("max_eur_pp")  # int or None
    if budget_max is not None:
        try:
            budget_max = int(budget_max)
        except Exception:
            budget_max = None

    out: List[Dict[str, Any]] = []

    for it in items:
        reasons: List[str] = []
        score = 0.0

        # --- Hard Filter: time overlap
        d = it.get("duration") or {}
        it_min = int(d.get("min_minutes") or 0)
        it_max = int(d.get("max_minutes") or 24 * 60)

        if time_min or time_max:
            if not _overlap(it_min, it_max, time_min, time_max):
                continue
            # score for closeness (prefer within window)
            if it_min >= time_min and it_max <= time_max:
                score += 22
                reasons.append("Zeit passt sehr gut.")
            else:
                score += 12
                reasons.append("Zeit passt grundsätzlich.")

        # --- Hard Filter: travel time
        tr = it.get("travel_from") or {}
        it_tr_min = int(tr.get("min_minutes") or 0)
        it_tr_max = int(tr.get("max_minutes") or it_tr_min)

        # if user picks max travel, require min travel within that bound
        if it_tr_min > max_travel:
            continue
        # bonus for closer
        if it_tr_min <= 15 and max_travel <= 30:
            score += 12
            reasons.append("Sehr nah dran.")
        elif it_tr_min <= max_travel:
            score += 8

        # --- Hard/Soft: setting
        # We map setting to tags in your dataset: indoor/outdoor/mixed
        if setting and setting != "any":
            if not _has_any_tag(it, [setting]):
                continue
            score += 10
            reasons.append("Setting passt (drinnen/draußen).")

        # --- Budget (semi-hard)
        if budget_max is not None:
            cost = it.get("cost") or {}
            it_max_eur = cost.get("max_eur_pp", 0)
            try:
                it_max_eur = int(it_max_eur or 0)
            except Exception:
                it_max_eur = 0

            # if max_eur_pp is known and > budget -> filter out
            if it_max_eur > 0 and it_max_eur > budget_max:
                continue

            # reward cheap/free, mildly reward "unknown"
            if it_max_eur == 0 and cost.get("type") == "free":
                score += 10
                reasons.append("Kostenlos.")
            elif it_max_eur == 0:
                score += 4
            else:
                score += 6
                reasons.append("Budget passt.")

        # --- Kids fit (soft-to-hard)
        if kids_selected is True or (kid_age_group and kid_age_group != "mixed"):
            if not _has_any_tag(it, ["kids-ok"]):
                # If user explicitly has kids, we treat this as hard
                continue
            score += 10
            reasons.append("Familientauglich.")

        # --- Vibe scoring
        # uses tags: calm / active / action
        if vibe:
            if vibe == "calm":
                score += 14 if _has_any_tag(it, ["calm"]) else 4
                if _has_any_tag(it, ["calm"]):
                    reasons.append("Ruhiger Vibe passt.")
            elif vibe == "easy":
                score += 10 if _has_any_tag(it, ["calm", "active"]) else 4
            elif vibe == "sporty":
                score += 14 if _has_any_tag(it, ["active"]) else 4
                if _has_any_tag(it, ["active"]):
                    reasons.append("Aktivitätslevel passt.")
            elif vibe == "action":
                score += 18 if _has_any_tag(it, ["action"]) else 4
                if _has_any_tag(it, ["action"]):
                    reasons.append("Action-Vibe passt.")

        # --- Mobility realism (soft)
        # If user wants walk/public but travel is large, penalize.
        if mobility_modes:
            if ("walk" in mobility_modes or "public" in mobility_modes) and it_tr_min >= 30:
                score -= 8
                reasons.append("Anfahrt könnte ohne Auto/ÖPNV-Plan aufwändiger sein.")
            else:
                score += 4

        # --- Bonuses: photo, free, rain-ok
        if _has_any_tag(it, ["photo"]):
            score += 2.5
        if _has_any_tag(it, ["free"]):
            score += 3
        if _has_any_tag(it, ["rain-ok"]) and setting in ("indoor", "mixed", "any", None, ""):
            score += 2

        out.append(
            {
                "score": round(score, 2),
                "reasons": reasons[:4],
                "item": it,
            }
        )

    out.sort(key=lambda x: x["score"], reverse=True)
    return out[: max(1, min(int(limit), 50))]


# ------------------------------------------------------------
# HTML Templates (inline for single-file deploy)
# ------------------------------------------------------------

BASE_HTML = r"""
<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <meta name="theme-color" content="#0b0f19" />
  <title>{{ page_title }}</title>
  <link rel="stylesheet" href="{{ url_for('static', filename='app.css') }}" />
</head>
<body data-page="{{ page_id }}" data-primary-city="{{ primary_city }}">
  <div class="bg-ambient" aria-hidden="true"></div>

  <header class="topbar">
    <div class="brand">
      <a class="brand__link" href="{{ url_for('home') }}">
        <span class="brand__mark">◈</span>
        <span class="brand__name">{{ app_title }}</span>
      </a>
    </div>

    <nav class="topnav">
      <a class="navlink" href="{{ url_for('categories_page') }}">Kategorien</a>
      <button class="navlink navlink--ghost" id="openWizardBtn" type="button">Auswahlhilfe</button>
    </nav>
  </header>

  <main class="wrap">
    {{ body|safe }}
  </main>

  <footer class="footer">
    <div class="footer__left">
    </div>
    <div class="footer__right">
      <span class="footer__mono">Build: {{ build_stamp }}</span>
    </div>
  </footer>

  <!-- Wizard Overlay -->
  <div id="wizard" class="wizard" hidden>
    <div class="wizard__backdrop" data-close="1"></div>

    <section class="wizard__panel" role="dialog" aria-modal="true" aria-labelledby="wizTitle">
      <header class="wizard__top">
        <div class="wizard__meta">
          <div class="wizard__orb" aria-hidden="true"></div>
          <div class="wizard__titles">
            <div id="wizTitle" class="wizard__title">Auswahlhilfe</div>
            <div class="wizard__sub">Ein paar Antworten – und dein Abenteuer steht.</div>
          </div>
        </div>

        <div class="wizard__actions">
          <button class="iconbtn" type="button" id="wizBackBtn" title="Zurück">←</button>
          <button class="iconbtn" type="button" id="wizCloseBtn" title="Schließen">✕</button>
        </div>
      </header>

      <div class="wizard__stage">
        <div class="wizard__question" id="wizQuestion">…</div>
        <div class="wizard__options" id="wizOptions"></div>

        <div class="wizard__bottom">
          <div class="wizard__progress">
            <div class="wizard__bar"><div class="wizard__barfill" id="wizBar"></div></div>
            <div class="wizard__step" id="wizStep">0/0</div>
          </div>

          <button class="btn btn--ghost" type="button" id="wizSkipBtn" hidden>Überspringen</button>
        </div>
      </div>

      <div class="wizard__results" id="wizResults" hidden>
        <div class="results__head">
          <div>
            <div class="results__title">Deine Top-Erlebnisse</div>
            <div class="results__sub">Ausgewählt ab {{ primary_city }} – klick für Details.</div>
          </div>
          <div class="results__actions">
            <button class="btn btn--ghost" type="button" id="wizShuffleBtn">Überrasch mich</button>
            <button class="btn" type="button" id="wizRestartBtn">Nochmal</button>
          </div>
        </div>

        <div class="cardgrid" id="resultsGrid"></div>
      </div>
    </section>
  </div>

  <script defer src="{{ url_for('static', filename='app.js') }}"></script>
</body>
</html>
"""


def _render(page_id: str, page_title: str, body_html: str) -> str:
    return render_template_string(
        BASE_HTML,
        page_id=page_id,
        page_title=page_title,
        body=body_html,
        app_title=APP_TITLE,
        primary_city=PRIMARY_CITY,
        build_stamp=os.getenv("BUILD_STAMP", "local"),
    )


def _card_html(it: Dict[str, Any]) -> str:
    img = it.get("image") or ""
    title = it.get("title") or ""
    summary = it.get("summary") or ""
    slug = it.get("slug") or ""
    emojis = " ".join((it.get("emoji_tags") or [])[:5])
    d = it.get("duration") or {}
    tr = it.get("travel_from") or {}
    dur = f"{d.get('min_minutes', '')}–{d.get('max_minutes', '')} min".replace("– min", " min")
    dist = f"{tr.get('min_minutes', '')}–{tr.get('max_minutes', '')} min Anfahrt".replace("– min", " min")

    img_html = ""
    if img:
        img_html = f'<div class="card__media"><img src="/{img}" alt="" loading="lazy" /></div>'

    return f"""
    <a class="card" href="{url_for('erlebnis_page', slug=slug)}" data-portal-card="1">
      {img_html}
      <div class="card__body">
        <div class="card__top">
          <div class="card__title">{title}</div>
          <div class="card__emojis" aria-hidden="true">{emojis}</div>
        </div>
        <div class="card__summary">{summary}</div>
        <div class="card__meta">
          <span class="chip">{dur}</span>
          <span class="chip">{dist}</span>
        </div>
      </div>
    </a>
    """


# ------------------------------------------------------------
# Web Pages
# ------------------------------------------------------------

@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})


@app.get("/")
def home():
    # Optional: show a few featured items (top of index) but keep the page visually "two portals".
    body = f"""
    <section class="hero">
      <h1 class="hero__title">{APP_SUBTITLE}</h1>
      <p class="hero__sub">
        Zwei Wege: lass dich führen — oder stöbere nach Kategorien.
      </p>

      <div class="portals">

        <button class="portal" id="portalWizard" type="button" data-portal="wizard">
          <div class="portal__glow" aria-hidden="true"></div>
          <div class="portal__inner">
            <div class="portal__title">Abenteuer finden</div>
            <div class="portal__desc">Dunkel. Leuchtend. Schnell zur richtigen Idee.</div>
          </div>
        </button>

        <a class="portal" id="portalCategories" href="{url_for('categories_page')}" data-portal="categories">
          <div class="portal__glow" aria-hidden="true"></div>
          <div class="portal__inner">
            <div class="portal__title">Kategorien</div>
            <div class="portal__desc">Einfach stöbern. Wie ein gutes Regal voller Pläne.</div>
          </div>
        </a>

      </div>

      <div class="hero__note">
      </div>
    </section>
    """
    return _render("home", f"{APP_TITLE}", body)


@app.get("/categories")
def categories_page():
    cats = (load_categories().get("categories") or [])
    # render sorted by order
    cats = sorted(cats, key=lambda c: int(c.get("order") or 9999))

    cards = []
    for c in cats:
        slug = c.get("slug")
        title = c.get("title")
        emoji = c.get("portal_emoji", "✨")
        desc = c.get("description", "")
        cards.append(
            f"""
            <a class="catcard" href="{url_for('category_page', slug=slug)}" data-portal-card="1">
              <div class="catcard__emoji" aria-hidden="true">{emoji}</div>
              <div class="catcard__title">{title}</div>
              <div class="catcard__desc">{desc}</div>
            </a>
            """
        )

    body = f"""
    <section class="pagehead">
      <h1 class="pagehead__title">Kategorien</h1>
      <p class="pagehead__sub">Wähle ein Portal — und du landest direkt bei passenden Erlebnissen.</p>
    </section>

    <section class="catgrid">
      {''.join(cards)}
    </section>
    """
    return _render("categories", f"Kategorien · {APP_TITLE}", body)


@app.get("/category/<slug>")
def category_page(slug: str):
    slug = _safe_slug(slug)
    cats = load_categories().get("categories") or []
    cat = next((c for c in cats if c.get("slug") == slug), None)
    if not cat:
        abort(404)

    idx = load_index()
    items = [it for it in (idx.get("items") or []) if it.get("main_category") == slug]

    cards = "".join(_card_html(it) for it in items)

    body = f"""
    <section class="pagehead">
      <div class="pagehead__kicker">
        <a class="backlink" href="{url_for('categories_page')}">← Kategorien</a>
      </div>
      <h1 class="pagehead__title">{cat.get("title")}</h1>
      <p class="pagehead__sub">{cat.get("description","")}</p>
    </section>

    <section class="cardgrid">
      {cards if cards else '<div class="empty">Noch keine Erlebnisse in dieser Kategorie.</div>'}
    </section>
    """
    return _render("category", f"{cat.get('title')} · {APP_TITLE}", body)


@app.get("/erlebnis/<slug>")
def erlebnis_page(slug: str):
    slug = _safe_slug(slug)
    it = _find_index_item_by_slug(slug)
    if not it:
        abort(404)

    # Load full JSON
    file_rel = it.get("file") or f"erlebnisse/{slug}.json"
    file_path = os.path.normpath(os.path.join(BASE_DIR, file_rel))
    if not file_path.startswith(os.path.abspath(ERLEBNIS_DIR)):
        abort(404)

    data = _read_json_cached(file_path)

    title = data.get("title", slug)
    summary = data.get("summary", "")
    desc = data.get("description", "")
    emojis = " ".join((data.get("emoji_tags") or [])[:8])
    img_list = data.get("images") or []
    hero = img_list[0]["src"] if (img_list and isinstance(img_list[0], dict) and img_list[0].get("src")) else ""

    dur = data.get("duration") or {}
    tr = data.get("travel_from") or {}
    cost = data.get("cost") or {}
    dur_txt = f"{dur.get('min_minutes','')}–{dur.get('max_minutes','')} min".replace("– min", " min")
    tr_txt = f"{tr.get('min_minutes','')}–{tr.get('max_minutes','')} min ab {PRIMARY_CITY}".replace("– min", " min")
    cost_note = (cost.get("notes") or "").strip()
    cost_type = cost.get("type", "")
    cost_txt = "Kostenlos" if cost_type == "free" else ("Kostenpflichtig" if cost_type in ("paid", "mixed") else "Kosten: unbekannt")

    highlights = data.get("highlights") or []
    steps = data.get("itinerary_steps") or []

    hero_html = ""
    if hero:
        hero_html = f"""
        <div class="detail__hero">
          <img src="/{hero}" alt="" loading="eager" />
          <div class="detail__veil" aria-hidden="true"></div>
        </div>
        """

    hl_html = ""
    if highlights:
        hl_html = "<ul class='bullets'>" + "".join(f"<li>{h}</li>" for h in highlights[:8]) + "</ul>"

    steps_html = ""
    if steps:
        rows = []
        for s in steps[:6]:
            t = (s or {}).get("title", "")
            h = (s or {}).get("hint", "")
            rows.append(f"<div class='step'><div class='step__t'>{t}</div><div class='step__h'>{h}</div></div>")
        steps_html = "<div class='steps'>" + "".join(rows) + "</div>"

    api_link = url_for("api_erlebnis", slug=slug)

    body = f"""
    <section class="detail">
      <div class="detail__top">
        <a class="backlink" href="{request.referrer or url_for('home')}">← Zurück</a>
        <a class="tinylink" href="{api_link}">JSON</a>
      </div>

      {hero_html}

      <div class="detail__body">
        <h1 class="detail__title">{title}</h1>
        <div class="detail__emojis" aria-hidden="true">{emojis}</div>
        <p class="detail__summary">{summary}</p>

        <div class="detail__facts">
          <span class="chip chip--strong">{dur_txt}</span>
          <span class="chip">{tr_txt}</span>
          <span class="chip">{cost_txt}</span>
        </div>

        {f"<p class='detail__note'>{cost_note}</p>" if cost_note else ""}

        <div class="detail__desc">{desc}</div>

        <div class="detail__grid">
          <div class="detail__block">
            <div class="blocktitle">Highlights</div>
            {hl_html if hl_html else "<div class='muted'>Keine Highlights hinterlegt.</div>"}
          </div>
          <div class="detail__block">
            <div class="blocktitle">Mini-Ablauf</div>
            {steps_html if steps_html else "<div class='muted'>Kein Ablauf hinterlegt.</div>"}
          </div>
        </div>
      </div>
    </section>
    """
    return _render("detail", f"{title} · {APP_TITLE}", body)


# Serve /img/<file> from img/ (not static/)
@app.get("/img/<path:filename>")
def img_file(filename: str):
    # very small safety: disallow path traversal
    filename = filename.replace("\\", "/")
    if ".." in filename or filename.startswith("/"):
        abort(404)
    return send_from_directory(IMG_DIR, filename, max_age=3600)


# ------------------------------------------------------------
# API
# ------------------------------------------------------------

@app.get("/api/index")
def api_index():
    return jsonify(load_index())


@app.get("/api/categories")
def api_categories():
    return jsonify(load_categories())


@app.get("/api/tags")
def api_tags():
    return jsonify(load_tags())


@app.get("/api/questions")
def api_questions():
    return jsonify(load_questions())


@app.get("/api/erlebnis/<slug>")
def api_erlebnis(slug: str):
    slug = _safe_slug(slug)
    it = _find_index_item_by_slug(slug)
    if not it:
        abort(404)

    file_rel = it.get("file") or f"erlebnisse/{slug}.json"
    file_path = os.path.normpath(os.path.join(BASE_DIR, file_rel))
    if not file_path.startswith(os.path.abspath(ERLEBNIS_DIR)):
        abort(404)

    return jsonify(_read_json_cached(file_path))


@app.post("/api/recommend")
def api_recommend():
    payload = request.get_json(silent=True) or {}
    answers = payload.get("answers") or {}

    # limit can be controlled by client; keep bounded
    limit = payload.get("limit", 12)
    try:
        limit = int(limit)
    except Exception:
        limit = 12

    recs = _recommend(answers=answers, limit=limit)

    # Flatten for client
    out = []
    for r in recs:
        it = r["item"]
        out.append(
            {
                "score": r["score"],
                "reasons": r["reasons"],
                "id": it.get("id"),
                "slug": it.get("slug"),
                "title": it.get("title"),
                "summary": it.get("summary"),
                "main_category": it.get("main_category"),
                "tags": it.get("tags", []),
                "emoji_tags": it.get("emoji_tags", []),
                "duration": it.get("duration", {}),
                "travel_from": it.get("travel_from", {}),
                "cost": it.get("cost", {}),
                "image": it.get("image", ""),
                "url": url_for("erlebnis_page", slug=it.get("slug")),
            }
        )

    return jsonify({"origin": PRIMARY_CITY, "count": len(out), "items": out})


# ------------------------------------------------------------
# Entrypoint
# ------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=True)
