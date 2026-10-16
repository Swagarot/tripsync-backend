# Entry point. The code lives in the tripsync/ package; this file only builds
# the app, so Gunicorn's `app:app` (see the Dockerfile) and `flask run` find it.
from tripsync import create_app

app = create_app()


if __name__ == "__main__":
    # Local development only: `python app.py`. The container runs Gunicorn
    # instead, which imports `app` and never reaches this block. host="0.0.0.0"
    # makes Flask listen on all interfaces; the default 127.0.0.1 only accepts
    # connections from the same machine. debug=True gives auto-reload and the
    # in-browser debugger, which can run code, so it must never serve real
    # traffic. (With debug on, prometheus-flask-exporter also leaves /metrics
    # out by default.)
    app.run(debug=True, host="0.0.0.0", port=5000)
