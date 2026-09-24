"""Proof report: one self-contained HTML file a reviewer (or examiner) can read."""
import html
from pathlib import Path

from jinja2 import Template

TPL = Template(r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Proof · {{ run_id }}</title>
<style>
:root{--ink:#15212B;--muted:#5A6874;--line:#D5DCE2;--bg:#F2F4F6;--panel:#fff;--ok:#2E7D55;--okbg:#DDF1E5;--bad:#B23A3A;--badbg:#F8E1E1;--acc:#2753C9}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,system-ui,sans-serif;padding:24px 16px 60px}
.w{max-width:960px;margin:0 auto;display:flex;flex-direction:column;gap:22px}
h1{font-size:28px;margin:0}h2{font-size:18px;margin:0 0 10px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px}
.verdict{border-radius:10px;padding:18px;font-size:20px;font-weight:700}
.pass{background:var(--okbg);color:var(--ok)}.fail{background:var(--badbg);color:var(--bad)}
.pill{display:inline-block;font:600 12px ui-monospace,Menlo,monospace;padding:3px 8px;border-radius:99px}
.muted{color:var(--muted)}table{width:100%;border-collapse:collapse}td,th{padding:7px 6px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;font-size:14px}
pre{background:#0E141A;color:#E3E9EF;padding:12px;border-radius:8px;overflow:auto;font-size:12px;max-height:520px}
.steps{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:10px}
.step{border:1px solid var(--line);border-radius:8px;overflow:hidden;font-size:12.5px}.step img{width:100%;display:block;border-bottom:1px solid var(--line)}
.step div{padding:7px}.two{display:grid;grid-template-columns:1fr 1fr;gap:14px}@media(max-width:700px){.two{grid-template-columns:1fr}}
code{font:12.5px ui-monospace,Menlo,monospace}
</style></head><body><div class="w">
<div><div class="muted">Carbon Copy proof report · run {{ run_id }} · {{ when }}</div><h1>{{ reqs.title }}</h1><div class="muted">Request: “{{ request }}”</div></div>
<div class="verdict {{ 'pass' if shipped else 'fail' }}">{{ '✔ Ready to ship. Every gate passed on the carbon copy' if shipped else '✖ Blocked. Not safe to ship' }}</div>

<div class="card"><h2>Before vs after, same AI tests on the same copy</h2><div class="two">
{% for label, b in [('Before (original code)', before), ('After (agent change)', after)] %}{% if b %}
<div><b>{{ label }}</b><table>{% for t in b.tests %}<tr><td>{{ t.name }}</td><td><span class="pill {{ 'pass' if t.passed else 'fail' }}">{{ 'PASS' if t.passed else 'FAIL' }}</span></td></tr>{% endfor %}
{% for d in b.db_checks %}<tr><td>DB: {{ d.description }}<br><span class="muted">expected {{ d.expect }}, got {{ d.got }}</span></td><td><span class="pill {{ 'pass' if d.passed else 'fail' }}">{{ 'PASS' if d.passed else 'FAIL' }}</span></td></tr>{% endfor %}</table></div>
{% endif %}{% endfor %}</div></div>

<div class="card"><h2>Approved requirements</h2><p>{{ reqs.summary }}</p><span class="pill" style="background:#FBEFD9;color:#B7700F">risk: {{ reqs.risk }}</span>
<ol>{% for a in reqs.acceptance_criteria %}<li>{{ a }}</li>{% endfor %}</ol></div>

<div class="card"><h2>Gate checks (final round)</h2><table>{% for c in final_checks %}<tr><td><b>{{ c.name }}</b><br><span class="muted">{{ c.summary }}</span>
{% if c.details and not c.passed %}<pre>{{ c.details|join('\n') }}</pre>{% endif %}</td><td><span class="pill {{ 'pass' if c.passed else 'fail' }}">{{ 'PASS' if c.passed else 'FAIL' }}</span></td></tr>{% endfor %}</table>
<p class="muted">Agent rounds on the copy: {{ rounds|length }} · {% for r in rounds %}#{{ r.round }} {{ '✔' if r.passed else '✖' }} {% endfor %}</p></div>

{% if after %}<div class="card"><h2>What the AI tester did (after)</h2>{% for t in after.tests %}<p><b>{{ t.name }}</b>: <span class="muted">{{ t.instructions }}</span></p>
<div class="steps">{% for s in t.steps %}<div class="step"><img src="data:image/png;base64,{{ s.screenshot }}" alt="step {{ loop.index }}"><div>
<b>{{ loop.index }}. {{ s.kind }} · {{ s.action }}</b> {{ s.target }} {{ s.value }}<br>{{ '✔' if s.ok else '✖ ' ~ s.error }}</div></div>{% endfor %}</div>{% endfor %}</div>{% endif %}

<div class="card"><h2>Agent summary</h2><p style="white-space:pre-wrap">{{ agent.summary }}</p><p class="muted">Files changed: {{ agent.edited_files|join(', ') }}</p></div>
<div class="card"><h2>Code change</h2><pre>{{ diff }}</pre></div>
<div class="card"><h2>Environment</h2><table><tr><td>Copy fidelity</td><td>{{ fidelity.score }}% {% if fidelity.missing_env %}(missing: {{ fidelity.missing_env|join(', ') }}){% endif %}</td></tr>
<tr><td>Services</td><td>app · Postgres 17 · LocalStack (AWS) · Mailpit</td></tr><tr><td>AI cost</td><td>${{ '%.2f'|format(cost) }}</td></tr><tr><td>Duration</td><td>{{ duration }}</td></tr></table></div>
<div class="card"><h2>Audit trail</h2><p>{{ audit|length }} events · hash chain <b>{{ 'VERIFIED' if audit_ok else 'BROKEN' }}</b></p>
<table>{% for e in audit %}<tr><td><code>{{ e.hash[:12] }}</code></td><td>{{ e.event }}</td><td class="muted">{{ e.who }}</td></tr>{% endfor %}</table></div>
</div></body></html>""")


def write(path: Path, **ctx) -> Path:
    ctx["diff"] = ctx.get("diff") or "(no changes)"
    path.write_text(TPL.render(**ctx))
    return path


def esc(s: str) -> str:
    return html.escape(s)
