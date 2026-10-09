"""Minimal dashboard: deal states, live feed, cost-per-decision.

Run: python -m src.app  (after `pip install -r requirements.txt`)
"""
from flask import Flask, jsonify, render_template_string

from . import audit, config

app = Flask(__name__)

PAGE = """
<!doctype html><title>Escrow Agent</title>
<h1>Escrow Agent — live</h1>
<h2>Decisions</h2>
<table border=1 cellpadding=6>
<tr><th>deal</th><th>verdict</th><th>confidence</th><th>tier</th>
<th>cost</th><th>reasoning</th></tr>
{% for d in decisions %}
<tr><td>{{d.deal_id}}</td><td><b>{{d.verdict}}</b></td><td>{{d.confidence}}</td>
<td>{{d.tier}}</td><td>${{d.cost_usd}}</td><td>{{d.reasoning}}</td></tr>
{% endfor %}
</table>
<p><b>Total AI spend:</b> ${{spend }} · <b>decisions:</b> {{ decisions|length }}</p>
"""


@app.get("/")
def index():
    decisions = audit.read_all()
    return render_template_string(PAGE, decisions=decisions,
                                  spend=audit.total_spend())


@app.get("/api/decisions")
def api_decisions():
    return jsonify(audit.read_all())


if __name__ == "__main__":
    app.run(port=config.DASHBOARD_PORT, debug=True)
