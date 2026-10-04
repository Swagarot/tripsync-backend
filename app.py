from flask import Flask, jsonify
# Flask: the core class that represents your web application.
# jsonify: converts a Python dict into a proper JSON HTTP response
#          (sets Content-Type to application/json automatically).
from flask_cors import CORS
# CORS: lets us explicitly allow other origins (like the React dev server
# on a different port) to read this API's responses. Without it, browsers
# block cross-origin requests by default as a security measure.

app = Flask(__name__)
# Creates the Flask application object. __name__ tells Flask where this
# file lives on disk so it can locate other things relative to it later
# (templates/, static/, etc.). Flask apps always start this way.

CORS(app)
# Allows all origins. Fine for local dev; lock it down to the real
# frontend domain in production.


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
    # Runs only when this file is executed directly (python app.py), not
    # when it's imported elsewhere (tests, a WSGI server). Standard pattern.
    # host="0.0.0.0" makes Flask listen on all interfaces inside the
    # container; the default 127.0.0.1 only accepts connections from inside
    # the container itself. debug=True is dev-only (auto-reload, debugger
    # in the browser). Swap in Gunicorn for real deployments.
    app.run(debug=True, host="0.0.0.0", port=5000)