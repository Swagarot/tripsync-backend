from flask import Flask, jsonify
# Flask: the core class that represents your web application.
# jsonify: converts a Python dict into a proper JSON HTTP response
#          (sets Content-Type to application/json automatically).
from flask_cors import CORS
# CORS: lets us explicitly allow other origins (like the React dev server
# on a different port) to read this API's responses. Without it, browsers
# block cross-origin requests by default as a security measure.
from prometheus_flask_exporter import PrometheusMetrics
# PrometheusMetrics: adds a /metrics endpoint in the text format Prometheus
# scrapes, and counts every request (by endpoint, method and status code)
# and how long it took.

app = Flask(__name__)
# Creates the Flask application object. __name__ tells Flask where this
# file lives on disk so it can locate other things relative to it later
# (templates/, static/, etc.). Flask apps always start this way.

CORS(app)
# Allows all origins. Fine for local dev; lock it down to the real
# frontend domain in production.

metrics = PrometheusMetrics(app)
# Counters live in this process's memory, which is why Gunicorn runs a
# single worker (see the Dockerfile): with several workers each would keep
# its own counts and every scrape would hit a random one. Scale with pod
# replicas instead; Prometheus scrapes each pod separately.
# Note: /metrics is not exposed through the Ingress (only /api/... is), so
# it's reachable from inside the cluster only.


# A decorator: registers the function below with Flask, mapping the URL
# path "/health" to it. Without this line, health() is just a normal
# Python function and Flask doesn't know to call it on a web request.
@app.route("/health")
def health():
    # jsonify() turns the dict into a real HTTP response with JSON headers.
    return jsonify({"status": "ok"})


# Liveness probe: "is the process stuck?" Keep it shallow. A failure makes
# Kubernetes restart the pod, so it must NOT depend on the database.
@app.route("/health/live")
def live():
    return jsonify({"status": "alive"})


# Readiness probe: "can this pod serve traffic right now?" A failure only
# removes the pod from the Service (no restart). Once Postgres exists,
# this is where the DB connection check goes.
@app.route("/health/ready")
def ready():
    return jsonify({"status": "ready"})


if __name__ == "__main__":
    # Local development only: `python app.py`. The container runs Gunicorn
    # instead (see the Dockerfile), which imports `app` and never reaches
    # this block. host="0.0.0.0" makes Flask listen on all interfaces; the
    # default 127.0.0.1 only accepts connections from the same machine.
    # debug=True gives auto-reload and the in-browser debugger, which can
    # run code, so it must never be what serves real traffic. (With debug
    # on, prometheus-flask-exporter also leaves /metrics out by default.)
    app.run(debug=True, host="0.0.0.0", port=5000)
