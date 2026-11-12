from __future__ import annotations

import html
import hashlib
import json
import re
from typing import Any, Dict, List, Optional

MAX_HTML_BYTES = 250_000
ALLOWED_TYPES = {
    "title",
    "statement",
    "bullets",
    "steps",
    "bigNumber",
    "code",
    "chat",
    "outro",
}
ALLOWED_THEMES = {"blueprint", "editorial", "signal", "archive", "neon"}


def _esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _items(scene: Dict[str, Any]) -> List[Dict[str, str]]:
    raw = scene.get("items")
    if not isinstance(raw, list):
        return []
    result: List[Dict[str, str]] = []
    for item in raw:
        if isinstance(item, str):
            result.append({"label": "", "text": item})
        elif isinstance(item, dict):
            result.append(
                {
                    "label": _text(item.get("label")),
                    "text": _text(item.get("text") or item.get("body") or item.get("title")),
                }
            )
    return [item for item in result if item["text"] or item["label"]]


def _kicker(scene: Dict[str, Any], index: int) -> str:
    value = _text(scene.get("kicker"))
    if not value:
        value = f"PART {index + 1:02d}"
    return f'<div class="kicker rv">{_esc(value)}</div>'


def _scene_markup(scene: Dict[str, Any], index: int) -> str:
    scene_type = str(scene.get("type") or "statement")
    if scene_type not in ALLOWED_TYPES:
        scene_type = "statement"
    title = _text(scene.get("title"))
    body = _text(scene.get("body"))
    items = _items(scene)

    if scene_type == "title":
        inner = (
            _kicker(scene, index)
            + f'<h1 class="hd rv">{_esc(title)}</h1>'
            + (f'<p class="note rv">{_esc(body)}</p>' if body else "")
            + '<div class="rule rv"></div>'
        )
    elif scene_type == "outro":
        inner = (
            _kicker(scene, index)
            + f'<h2 class="hd rv">{_esc(title)}</h2>'
            + (f'<p class="cta rv">{_esc(body)}</p>' if body else "")
            + '<div class="handle rv">SAVE THIS</div>'
        )
    elif scene_type == "bigNumber":
        inner = (
            _kicker(scene, index)
            + f'<div class="big rv">{_esc(title)}</div>'
            + (f'<p class="note rv">{_esc(body)}</p>' if body else "")
        )
    elif scene_type == "bullets":
        cards = "".join(
            f'<div class="card rv" style="--i:{i}"><b>{_esc(item["label"] or "•")}</b>'
            f'<span>{_esc(item["text"])}</span></div>'
            for i, item in enumerate(items)
        )
        inner = (
            _kicker(scene, index)
            + f'<h2 class="hd rv">{_esc(title)}</h2>'
            + f'<div class="cards">{cards}</div>'
            + (f'<p class="note rv">{_esc(body)}</p>' if body else "")
        )
    elif scene_type == "steps":
        steps = "".join(
            f'<div class="step rv" style="--i:{i}"><i>{i + 1}</i><div>'
            f'<b>{_esc(item["label"] or item["text"])}</b>'
            + (f'<span>{_esc(item["text"])}</span>' if item["label"] else "")
            + "</div></div>"
            for i, item in enumerate(items)
        )
        inner = (
            _kicker(scene, index)
            + f'<h2 class="hd rv">{_esc(title)}</h2>'
            + f'<div class="steps">{steps}</div>'
        )
    elif scene_type == "code":
        code = str(scene.get("code") or "").strip()
        lines = code.splitlines() or [code]
        code_html = "".join(
            f'<div class="cline"><span>{_esc(line) or " "}</span></div>' for line in lines[:18]
        )
        inner = (
            _kicker(scene, index)
            + f'<h2 class="hd rv">{_esc(title)}</h2>'
            + f'<div class="code rv">{code_html}</div>'
            + (f'<p class="note rv">{_esc(body)}</p>' if body else "")
        )
    elif scene_type == "chat":
        bubbles = []
        for i, item in enumerate(items[:4]):
            side = "u" if i % 2 == 0 else "ai"
            bubbles.append(f'<div class="bub {side} rv">{_esc(item["text"] or item["label"])}</div>')
        inner = (
            _kicker(scene, index)
            + f'<h2 class="hd rv">{_esc(title)}</h2>'
            + f'<div class="chat">{"".join(bubbles)}</div>'
        )
    else:
        inner = (
            _kicker(scene, index)
            + f'<h2 class="hd rv">{_esc(title)}</h2>'
            + (f'<p class="note rv">{_esc(body)}</p>' if body else "")
        )

    return (
        f'<section class="scene type-{scene_type}" id="scene-{index}" '
        f'data-type="{scene_type}"><div class="camera">{inner}</div></section>'
    )


def render_html(script: Dict[str, Any]) -> str:
    scenes = script.get("scenes") or []
    title = _text(script.get("title")) or "Explained"
    brand = _text(script.get("brand")) or f"{title.upper()} · EXPLAINED"
    visual_style = script.get("visual_style") if isinstance(script.get("visual_style"), dict) else {}
    requested_theme = _text(visual_style.get("theme")).lower()
    if requested_theme not in ALLOWED_THEMES:
        requested_theme = sorted(ALLOWED_THEMES)[
            int(hashlib.sha256(title.encode("utf-8")).hexdigest(), 16) % len(ALLOWED_THEMES)
        ]
    total = 0.0
    durations: List[Dict[str, float]] = []
    sections: List[str] = []
    for index, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            continue
        try:
            duration = max(1.0, float(scene.get("duration") or 0))
        except (TypeError, ValueError):
            duration = 1.0
        total += duration
        durations.append({"duration": duration})
        sections.append(_scene_markup(scene, index))

    if total <= 0:
        total = 90.0
        durations = [{"duration": total}]
        sections = [
            '<section class="scene type-title active" id="scene-0"><div class="camera">'
            '<h1 class="hd rv">Explained</h1></div></section>'
        ]

    scenes_json = json.dumps(durations, ensure_ascii=False).replace("</", "<\\/")
    safe_title = _esc(title)
    safe_brand = _esc(brand)
    duration_text = f"{total:.3f}"

    return (
        HTML_TEMPLATE.replace("__TITLE__", safe_title)
        .replace("__BRAND__", safe_brand)
        .replace("__THEME__", requested_theme)
        .replace("__SCENES__", scenes_json)
        .replace("__DURATION__", duration_text)
        .replace("__BODY__", "\n".join(sections))
    )


def validate_html(markup: str, expected_duration: Optional[float] = None) -> List[str]:
    errors: List[str] = []
    if "<html" not in markup.lower():
        errors.append("missing <html> element")
    if "window.__seek" not in markup:
        errors.append("missing window.__seek")
    if "window.__duration" not in markup:
        errors.append("missing window.__duration")
    if re.search(r"""(?:src|href)\s*=\s*["']https?://""", markup, re.IGNORECASE):
        errors.append("external http(s) assets are not allowed")
    if len(markup.encode("utf-8")) > MAX_HTML_BYTES:
        errors.append(f"HTML exceeds {MAX_HTML_BYTES // 1000} KB")
    if expected_duration is not None:
        match = re.search(r"window\.__duration\s*=\s*([0-9.]+)", markup)
        if not match:
            errors.append("duration is not assigned to window.__duration")
        else:
            try:
                if abs(float(match.group(1)) - expected_duration) > 0.5:
                    errors.append("window.__duration does not match the script")
            except ValueError:
                errors.append("window.__duration is not numeric")
    return errors


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>__TITLE__</title>
<style>
:root{box-sizing:border-box;--page:#05070f;--fg:#eef2ff;--mut:#93a1c4;--card:#141b38;--line:#2b3566;--a:#38bdf8;--b:#a78bfa;--c:#34d399;--r:#fb7185}
*{box-sizing:inherit}
html,body{height:100%;margin:0;overflow:hidden;background:var(--page)}
body{font-family:Inter,"Segoe UI",system-ui,-apple-system,Roboto,sans-serif;color:var(--fg)}
#fit{position:fixed;inset:0;display:grid;place-items:center;overflow:hidden}
#stage{position:relative;width:min(100vw,56.25vh);aspect-ratio:9/16;container-type:size;overflow:hidden;background:#0b1020}
.bg{position:absolute;inset:0;z-index:0;overflow:hidden;background:#0b1020}
.blob{position:absolute;border-radius:50%;filter:blur(13cqw);opacity:.55;mix-blend-mode:screen;will-change:transform;transition:transform .12s linear}
.b1{width:78cqw;height:78cqw;left:-24cqw;top:-18cqw;background:radial-gradient(circle at 40% 40%,#1e3a8a 0%,#1e3a8a00 68%)}
.b2{width:76cqw;height:76cqw;right:-26cqw;bottom:-14cqw;background:radial-gradient(circle at 60% 40%,#5b21b6 0%,#5b21b600 68%)}
.b3{width:54cqw;height:54cqw;left:24cqw;bottom:22cqw;background:radial-gradient(circle at 50% 50%,#0e7490 0%,#0e749000 70%);opacity:.35}
.grid{position:absolute;inset:-25%;background-image:linear-gradient(#ffffff0a 1px,transparent 1px),linear-gradient(90deg,#ffffff0a 1px,transparent 1px);background-size:6cqw 6cqw;-webkit-mask-image:radial-gradient(62% 52% at 50% 46%,#000 0%,transparent 76%);mask-image:radial-gradient(62% 52% at 50% 46%,#000 0%,transparent 76%);opacity:.55}
.vignette{position:absolute;inset:0;background:radial-gradient(120% 90% at 50% 40%,transparent 55%,#00000088 100%),radial-gradient(80% 40% at 50% 108%,#05070f 10%,transparent 70%)}
.grain{position:absolute;inset:0;opacity:.05;mix-blend-mode:overlay;pointer-events:none;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='160'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.85' numOctaves='2' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E")}
.brand{position:absolute;top:4.2cqw;left:6cqw;right:6cqw;z-index:6;display:flex;align-items:center;gap:1.4cqw;font-size:2.1cqw;letter-spacing:.22em;color:#8fa0c8;font-weight:700;text-transform:uppercase}
.brand .dot{width:1.1cqw;height:1.1cqw;border-radius:50%;background:linear-gradient(90deg,var(--a),var(--b));box-shadow:0 0 1.8cqw var(--a);flex:none}
.brand span:last-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.scene{position:absolute;inset:0;z-index:2;opacity:0;will-change:opacity}
.scene.active{opacity:1}
.camera{position:absolute;inset:0;padding:14cqw 7cqw 22cqw;display:flex;flex-direction:column;justify-content:center;gap:3.2cqw}
.kicker{font-size:2.6cqw;letter-spacing:.28em;color:var(--a);font-weight:800;text-transform:uppercase}
.hd{font-size:8.2cqw;font-weight:850;line-height:1.04;letter-spacing:-.028em;margin:0;color:var(--fg)}
.type-title .hd{font-size:10.2cqw}
.type-title .camera{gap:4.4cqw}
.rule{width:18cqw;height:.5cqw;border-radius:99px;background:linear-gradient(90deg,var(--a),var(--b));box-shadow:0 0 2cqw #38bdf866}
.note{font-size:3.8cqw;color:var(--mut);line-height:1.4;margin:0}
.cta{font-size:4.2cqw;color:var(--mut);line-height:1.4;margin:0}
.big{font-size:15cqw;font-weight:900;text-align:center;letter-spacing:-.05em;line-height:1;background:linear-gradient(92deg,var(--a),var(--b));-webkit-background-clip:text;background-clip:text;color:transparent}
.cards{display:flex;flex-direction:column;gap:2.4cqw}
.card{display:flex;align-items:center;gap:3.4cqw;padding:3.4cqw 4.2cqw;border-radius:3cqw;background:linear-gradient(180deg,#151d3d,#10162e);border:.28cqw solid var(--line);border-left:1.2cqw solid var(--a);box-shadow:0 2cqw 6cqw #00000055}
.card b{font-size:5cqw;line-height:1.1;min-width:7cqw;color:var(--a)}
.card span{font-size:3.6cqw;color:var(--fg);line-height:1.3}
.steps{display:flex;flex-direction:column;gap:2.6cqw}
.step{display:flex;gap:3cqw;align-items:flex-start;padding:3.2cqw 3.8cqw;border-radius:2.8cqw;background:#121936;border:.28cqw solid var(--line)}
.step i{display:grid;place-items:center;flex:none;width:7.2cqw;height:7.2cqw;border-radius:50%;background:linear-gradient(135deg,var(--a),var(--b));color:#06101f;font-size:3.8cqw;font-weight:900;font-style:normal}
.step b{display:block;font-size:4.4cqw;line-height:1.2}
.step span{display:block;font-size:3.2cqw;color:var(--mut);line-height:1.32;margin-top:.6cqw}
.chat{display:flex;flex-direction:column;gap:2.4cqw}
.bub{max-width:88%;padding:3.2cqw 4.2cqw;border-radius:4cqw;font-size:4.2cqw;line-height:1.3}
.bub.u{align-self:flex-end;font-weight:700;color:#06101f;border-bottom-right-radius:1.2cqw;background:linear-gradient(135deg,var(--a),var(--b));box-shadow:0 2.4cqw 7cqw #38bdf833}
.bub.ai{align-self:flex-start;color:var(--fg);border:.28cqw solid var(--line);border-bottom-left-radius:1.2cqw;background:linear-gradient(180deg,#182046,#111731);box-shadow:0 2.4cqw 7cqw #00000055}
.code{position:relative;overflow:hidden;background:#070b18;border:.28cqw solid var(--line);border-radius:3cqw;padding:3.6cqw 4cqw;font:500 3cqw/1.65 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:#cbd5f5;box-shadow:0 4cqw 12cqw #00000088}
.cline{white-space:pre;overflow:hidden;width:0}
.cline span{display:block;white-space:pre}
.handle{align-self:center;padding:2.2cqw 4.8cqw;border-radius:99px;background:linear-gradient(90deg,var(--a),var(--b));color:#06101f;font-weight:900;font-size:3.6cqw;letter-spacing:.12em;box-shadow:0 2cqw 6cqw #38bdf833}
#progress{position:absolute;left:6cqw;right:6cqw;bottom:4.6cqw;height:.7cqw;border-radius:99px;background:#ffffff14;z-index:9;overflow:hidden}
#progress i{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--a),var(--b));box-shadow:0 0 2cqw #38bdf899}
#counter{position:absolute;right:6cqw;bottom:7cqw;z-index:9;font:700 2.4cqw ui-monospace,monospace;color:#7f8db3;letter-spacing:.12em}
/* Topic-directed art directions selected by the script. */
.theme-blueprint{--page:#06131d;--fg:#e5f7ff;--mut:#93b8c9;--line:#28566d;--a:#37d4ff;--b:#4c89ff}.theme-blueprint .grid{opacity:.9;background-size:4cqw 4cqw}.theme-blueprint .card,.theme-blueprint .step{border-radius:.8cqw;background:#092130cc}.theme-blueprint .camera{justify-content:flex-start;padding-top:25cqw}
.theme-editorial{--page:#241712;--fg:#fff8ee;--mut:#d7bca6;--line:#704532;--a:#ffbb70;--b:#df6c56}.theme-editorial .grid{display:none}.theme-editorial .blob{filter:blur(18cqw);opacity:.7}.theme-editorial .card{border-left-color:var(--b);border-radius:0;background:#372018dd}.theme-editorial .hd{font-family:Georgia,serif;letter-spacing:-.04em}.theme-editorial .camera{padding-left:9cqw;padding-right:9cqw}
.theme-signal{--page:#100b24;--fg:#faf5ff;--mut:#c3aedc;--line:#523a80;--a:#f472d0;--b:#8b5cf6}.theme-signal .grid{transform:skewY(-12deg);opacity:.72}.theme-signal .big{font-size:18cqw}.theme-signal .card{border-radius:5cqw;background:linear-gradient(100deg,#2b164c,#17112f)}.theme-signal .camera{gap:4.4cqw}
.theme-archive{--page:#1b1a14;--fg:#fffde9;--mut:#c6c19e;--line:#696444;--a:#e8cb63;--b:#b5813c}.theme-archive .grid{background-size:9cqw 9cqw;opacity:.25}.theme-archive .blob{opacity:.2;mix-blend-mode:normal}.theme-archive .card,.theme-archive .step{border-radius:0;background:#292619e8;box-shadow:none}.theme-archive .kicker{color:var(--a)}
.theme-neon{--page:#05030d;--fg:#f5efff;--mut:#b4a9cf;--line:#54327b;--a:#35f2ca;--b:#dc4cff}.theme-neon .grid{opacity:.75;background-size:3cqw 3cqw}.theme-neon .blob{opacity:.85}.theme-neon .card{border-left-color:var(--b);box-shadow:0 0 5cqw #b739ce44}.theme-neon .handle{background:linear-gradient(90deg,var(--a),var(--b))}
</style>
</head>
<body>
<div id="fit">
<div id="stage" class="theme-__THEME__">
  <div class="bg">
    <div class="blob b1"></div><div class="blob b2"></div><div class="blob b3"></div>
    <div class="grid"></div><div class="vignette"></div><div class="grain"></div>
  </div>
  <div class="brand"><span class="dot"></span><span>__BRAND__</span></div>
  <div id="scenes">
__BODY__
  </div>
  <div id="progress"><i></i></div>
  <div id="counter">01 / 01</div>
</div>
</div>
<script>
const SCENES = __SCENES__;
window.__duration = __DURATION__;
const DURATION = window.__duration;
const sections = Array.from(document.querySelectorAll('.scene'));
const progress = document.querySelector('#progress i');
const counter = document.querySelector('#counter');
const stage = document.querySelector('#stage');
const blobs = Array.from(document.querySelectorAll('.blob'));
function clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }
function ease(x) { return 1 - Math.pow(1 - clamp(x, 0, 1), 3); }
function renderAt(t) {
  t = clamp(t, 0, DURATION);
  let start = 0;
  let index = 0;
  let local = 0;
  for (let i = 0; i < SCENES.length; i++) {
    const d = SCENES[i].duration;
    if (t < start + d || i === SCENES.length - 1) {
      index = i;
      local = t - start;
      break;
    }
    start += d;
  }
  const scene = SCENES[index];
  const p = scene ? clamp(local / scene.duration, 0, 1) : 0;
  sections.forEach((el, i) => {
    const on = i === index;
    el.classList.toggle('active', on);
    if (!on) return;
    el.style.setProperty('--p', p);
    el.querySelectorAll('.rv').forEach((node, j) => {
      const r = ease((p + 0.09 - j * 0.05) / 0.26);
      node.style.opacity = r;
      node.style.transform = 'translateY(' + ((1 - r) * 22) + 'px) scale(' + (0.985 + 0.015 * r) + ')';
    });
    el.querySelectorAll('.cline').forEach((line, j) => {
      const r = ease((p - 0.12 - j * 0.065) / 0.18);
      line.style.width = (r * 100) + '%';
    });
  });
  progress.style.width = (t / DURATION * 100) + '%';
  counter.textContent = String(index + 1).padStart(2, '0') + ' / ' + String(SCENES.length).padStart(2, '0');
  const q = t / DURATION;
  blobs.forEach((blob, i) => {
    const k = i % 2 ? -1 : 1;
    blob.style.transform = 'translate3d(' + (Math.sin(q * 6.283 + i) * 4 * k) + 'cqw,' + (Math.cos(q * 4.712 + i) * 3) + 'cqw,0) scale(' + (1 + 0.05 * Math.sin(q * 3.14 + i)) + ')';
  });
}
window.__seek = renderAt;
window.__ready = true;
const params = new URLSearchParams(location.search);
if (params.has('t')) {
  window.__paused = true;
  renderAt(parseFloat(params.get('t')) || 0);
} else {
  const started = performance.now();
  function loop(now) {
    if (!window.__paused) renderAt(((now - started) / 1000) % DURATION);
    requestAnimationFrame(loop);
  }
  requestAnimationFrame(loop);
}
</script>
</body>
</html>
"""
