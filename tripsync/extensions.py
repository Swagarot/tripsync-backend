# The extension objects are created here, unbound, and attached to the app in
# create_app(). That way models.py and the blueprints can import them without
# importing the app itself (which would be a circular import).
from flask_jwt_extended import JWTManager
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
migrate = Migrate()
jwt = JWTManager()
# Limits are per client IP. Behind the Ingress, request.remote_addr would be
# the ingress-nginx pod for everyone; ProxyFix in create_app() swaps in the
# real client IP from X-Forwarded-For first.
limiter = Limiter(key_func=get_remote_address)
