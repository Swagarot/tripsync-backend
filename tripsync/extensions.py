# The extension objects are created here, unbound, and attached to the app in
# create_app(). That way models.py and the blueprints can import them without
# importing the app itself (which would be a circular import).
from flask import request
from flask_jwt_extended import JWTManager
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
migrate = Migrate()
jwt = JWTManager()


def client_ip():
    """The address rate limits are counted per.

    Through the Cloudflare Tunnel, every request reaches ingress-nginx from a
    cloudflared pod, so X-Forwarded-For (and remote_addr) is that pod for all
    users. Cloudflare puts the real visitor's IP in CF-Connecting-IP, replacing
    any value the visitor sent, so it can't be faked from the internet.
    Known gap: someone on the LAN/Tailscale path (not via Cloudflare) could
    send their own CF-Connecting-IP to dodge the limit. Only trusted devices
    have that path, so this is accepted.
    Without the header (LAN, Tailscale, compose), fall back to remote_addr,
    which ProxyFix in create_app() has set from X-Forwarded-For.
    """
    return request.headers.get("CF-Connecting-IP") or get_remote_address()


limiter = Limiter(key_func=client_ip)
