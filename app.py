from flask import Flask, jsonify
from flask_cors import CORS
# Flask: the core class that represents your web application
# jsonify: converts a Python dict into a proper JSON HTTP response
#          (sets the Content-Type header to application/json automatically)

app = Flask(__name__)
# Creates the Flask application object.
# __name__ tells Flask where this file lives on disk, so it can correctly
# locate other things relative to it later (templates/, static/, etc.)
# — not used yet, but Flask apps always start this way.

CORS(app)  # allows all origins for now — fine for local dev,
           # we'll lock this down to your actual frontend domain later

@app.route("/health")
# A decorator: it registers the function below with Flask, mapping the URL
# path "/health" to it. Without this line, health() is just a normal Python
# function — Flask has no idea it should be called on a web request.
def health():
    return jsonify({"status": "ok"})
    # jsonify() takes our dict {"status": "ok"} and turns it into a real
    # HTTP response with the correct JSON headers. Returning a plain dict
    # directly also works in modern Flask, but jsonify() is the explicit,
    # classic way and makes the intent obvious.

if __name__ == "__main__":
    # This block only runs when you execute this file directly
    # (python app.py) — NOT when this file is imported elsewhere
    # (e.g. by a test file or a WSGI server later). Standard Python pattern.
    app.run(debug=True, host="0.0.0.0", port=5000)
    # Starts Flask's built-in development server on port 5000.
    # debug=True enables auto-reload on code changes + detailed error
    # pages in the browser. This is DEV-ONLY — we'll swap in a proper
    # production server (Gunicorn) when we containerize this later,
    # since Flask's built-in server isn't meant to handle real traffic.