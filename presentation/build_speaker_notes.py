import json, re, html, pathlib, sys
src = pathlib.Path(sys.argv[1]); out = pathlib.Path(sys.argv[2]); scale = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0
deck = json.loads((src / "deck.json").read_text())
order = deck["order"]
section_of = {}
budgets = {"cover": "The problem · slides 1–4 · 4 min · end by 4:00", "layers": "The idea · slides 5–8 · 5 min · end by 9:00",
           "where": "Getting started · slides 9–10 · 2.5 min · end by 11:30", "rule-value": "What a rule looks like · slides 11–13 · 4.5 min · end by 16:00",
           "demo-backdoor": "Catching an agent · slides 14–19 · 10 min (incl. demo 1) · end by 26:00", "agent-flow": "At run time · slides 20–25 · 8 min · end by 34:00",
           "results": "Wrap up · slides 26–28 · 3 min · end by 37:00", "questions": "Questions · 7–8 min · end by 45:00", "demos": "Backup slide: only if there’s time"}
checkpoints = {"demo-backdoor": "16:00", "agent-flow": "26:00", "results": "34:00"}
def esc(t): return html.escape(t, quote=False)
def render_notes(n):
    lines = n.strip("\n").split("\n"); parts = []; in_ul = 0; say = False
    def close(to=0):
        nonlocal in_ul
        while in_ul > to: parts.append("</ul>"); in_ul -= 1
    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            close(); continue
        m = re.match(r"^( *)- (.*)$", line)
        if re.match(r"^[A-Z0-9][A-Z0-9 ’'“”\"?:,().&/–\-]+(\([^)]*\))?\??$", line.strip()) and len(line.strip()) < 70 and re.sub(r"\s*\([^)]*\)$", "", line.strip()).upper() == re.sub(r"\s*\([^)]*\)$", "", line.strip()):
            close(); say = line.strip().startswith("SAY IT"); parts.append(f'<h3>{esc(line.strip())}</h3>'); continue
        if m:
            depth = 1 + len(m.group(1)) // 2
            while in_ul < depth: parts.append("<ul>"); in_ul += 1
            close(depth)
            cls = ' class="say"' if say else ""
            parts.append(f"<li{cls}>{esc(m.group(2))}</li>"); continue
        close()
        cls = ' class="say"' if say else ""
        parts.append(f"<p{cls}>{esc(line.strip())}</p>")
    close(); return "\n".join(parts)
pages = []
for i, sid in enumerate(order, 1):
    s = (src / "slides" / f"{sid}.html").read_text()
    t = re.search(r"<h[12][^>]*>(.*?)</h[12]>", s, re.S).group(1)
    title = html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"<br>", " ", t))).strip()
    title = re.sub(r"\s+", " ", title)
    eb = re.search(r'class="eyebrow[^"]*">(.*?)</p>', s, re.S)
    eyebrow = html.unescape(re.sub(r"<[^>]+>", "", eb.group(1))).strip() if eb else ""
    notes = html.unescape(re.search(r"<aside>(.*?)</aside>", s, re.S).group(1))
    live = re.search(r"demo\.sh (\d)", s)
    badges = []
    if sid in budgets: badges.append(f'<span class="b sec">{esc(budgets[sid])}</span>')
    if sid in checkpoints: badges.append(f'<span class="b chk">⏱ Checkpoint: be on this slide by {checkpoints[sid]}</span>')
    if live: badges.append(f'<span class="b live">▶ Optional live demo: DEMO_PAUSE=1 ./presentation/demo.sh {live.group(1)}</span>')
    nlines = notes.count("\n") + len(notes) / 95
    fs = max(8.6, min(13.5, 13.5 * (46 / max(nlines, 1)) ** 0.5)) * scale
    pages.append(f'''<section class="page" style="font-size:{fs:.2f}pt">
<header><div class="num">{i}<span>/ {len(order)}</span></div><div class="ttl"><div class="eb">{esc(eyebrow)}</div><h2>{esc(title)}</h2></div></header>
<div class="badges">{''.join(badges)}</div>
<div class="notes">{render_notes(notes)}</div>
<footer>Invariants-First, Simply · speaker notes · slide {i}</footer>
</section>''')
css = f'''@page {{ size: letter; margin: 0.5in 0.6in; }}
* {{ box-sizing: border-box; }}
body {{ font-family: "IBM Plex Sans", "Helvetica Neue", Arial, sans-serif; color: #16213A; margin: 0; }}
.page {{ page-break-after: always; break-after: page; position: relative; width: 7.3in; height: 9.95in; overflow: hidden; font-size: {13.5*scale}pt; line-height: 1.38; }}
.page.long {{ font-size: {12*scale}pt; line-height: 1.32; }}
header {{ display: flex; gap: 18px; align-items: flex-end; border-bottom: 3px solid #A63F08; padding-bottom: 8px; margin-bottom: 8px; }}
.num {{ font-size: 44pt; font-weight: 700; line-height: 1; color: #A63F08; }}
.num span {{ font-size: 14pt; color: #4A5568; font-weight: 400; margin-left: 4px; }}
.eb {{ font-size: 9pt; letter-spacing: 1.5px; color: #4A5568; text-transform: uppercase; }}
h2 {{ font-size: 19pt; margin: 2px 0 0; line-height: 1.15; }}
.badges {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }}
.b {{ font-size: 9.5pt; padding: 3px 8px; border-radius: 10px; }}
.sec {{ background: #ECE7DC; }} .chk {{ background: #F4A261; font-weight: 700; }} .live {{ background: #16213A; color: #F6F4EF; font-family: Menlo, monospace; font-size: 8.5pt; }}
h3 {{ font-size: 0.78em; letter-spacing: 1.2px; color: #A63F08; margin: 0.75em 0 0.15em; }}
p {{ margin: 0.2em 0; }} ul {{ margin: 0.1em 0 0.1em 1.1em; padding: 0; }} li {{ margin: 0.08em 0; }}
.say {{ font-weight: 700; background: #FFF1E3; }}
footer {{ position: absolute; bottom: 0; font-size: 8pt; color: #8D99AE; }}'''
SCRIPT = "<script>addEventListener('load',()=>{const bad=[...document.querySelectorAll('.page')].map((p,i)=>[i+1,p.scrollHeight-p.clientHeight]).filter(x=>x[1]>2);document.body.setAttribute('data-overflow',JSON.stringify(bad));})</script>"
out.write_text(f'<!doctype html><html><head><meta charset="utf-8"><title>Speaker notes</title>{SCRIPT}<style>{css}</style></head><body>{"".join(pages)}</body></html>')
print("pages:", len(pages))
