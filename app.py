import os
import re
import json
from datetime import datetime, timedelta
from functools import wraps

import requests
from flask import Flask, request, jsonify, session, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash

# ─── APP CONFIG ──────────────────────────────────────────────
app = Flask(__name__)
BASE_DIR = os.path.dirname(__file__)

# Security: SECRET_KEY must be set in environment (Render -> Environment tab)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY')
if not app.config['SECRET_KEY']:
    raise RuntimeError("SECRET_KEY environment variable not set. Please set it in Render dashboard.")

# Gemini API key — set this in Render's Environment tab as GEMINI_API_KEY.
# Falling back to the key you were using, so nothing breaks if you haven't
# moved it to an env var yet. For real production use, set the env var and
# remove the hardcoded fallback below.
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', 'AIzaSyDD5DB0OH4tTVgfC9Gv6UYI-d6dO26W5hQ')
GEMINI_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-1.5-flash')
GEMINI_URL = f'https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent'

# Session configuration – cross-origin ready (Netlify frontend -> Render backend)
# Render automatically sets RENDER=True on all its services, so we use that
# to detect production instead of relying on FLASK_ENV (which Render does NOT set).
IS_PROD = bool(os.environ.get('RENDER')) or os.environ.get('FLASK_ENV') == 'production'
# Cross-origin cookies (Netlify → Render) REQUIRE SameSite=None + Secure.
# Without this, the browser silently drops the session cookie after login.
app.config['SESSION_COOKIE_SAMESITE'] = 'None' if IS_PROD else 'Lax'
app.config['SESSION_COOKIE_SECURE'] = True if IS_PROD else False
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)

# Database – use DATABASE_URL (PostgreSQL recommended) or fallback to SQLite
database_url = os.environ.get('DATABASE_URL')
if not database_url:
    database_url = 'sqlite:///saarthi.db'
    print("No DATABASE_URL set. Using local SQLite. For Render, set DATABASE_URL to your PostgreSQL URL.")
if database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# CORS – allow only your frontend origin(s). Set ALLOWED_ORIGINS on Render to
# your Netlify URL, e.g. https://your-site.netlify.app
allowed_origins = [
    "https://gleaming-horse-a63d32.netlify.app",
    "http://localhost:3000",
    "http://127.0.0.1:5500"
]
CORS(
    app,
    supports_credentials=True,
    origins=allowed_origins,
    allow_headers=['Content-Type', 'Authorization'],
    expose_headers=['Content-Type'],
    vary_header=True,
)

db = SQLAlchemy(app)


# ─── DATABASE MODELS ──────────────────────────────────────────
class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    full_name = db.Column(db.String(120), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    profile = db.relationship('Profile', backref='user', uselist=False, cascade='all, delete-orphan')
    applications = db.relationship('Application', backref='user', lazy=True, cascade='all, delete-orphan')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password, method='pbkdf2:sha256')

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def to_dict(self):
        return {
            'id': self.id,
            'username': self.username,
            'email': self.email,
            'full_name': self.full_name,
            'created_at': self.created_at.isoformat()
        }


class Profile(db.Model):
    __tablename__ = 'profiles'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, unique=True)
    age = db.Column(db.Integer, default=30)
    occupation = db.Column(db.String(50), default='other')
    income = db.Column(db.String(20), default='1l_3l')
    state = db.Column(db.String(50), default='other')
    gender = db.Column(db.String(20), default='male')
    preferred_lang = db.Column(db.String(5), default='en')
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            'age': self.age,
            'occupation': self.occupation,
            'income': self.income,
            'state': self.state,
            'gender': self.gender,
            'preferred_lang': self.preferred_lang
        }


class Application(db.Model):
    __tablename__ = 'applications'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    app_code = db.Column(db.String(20), nullable=False)
    scheme_id = db.Column(db.String(50), nullable=False)
    scheme_title = db.Column(db.String(200), nullable=False)
    category = db.Column(db.String(50))
    status = db.Column(db.String(30), default='Pending Review')
    submitted_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.app_code,
            'schemeId': self.scheme_id,
            'title': self.scheme_title,
            'category': self.category,
            'submitted': self.submitted_at.strftime('%d/%m/%Y'),
            'status': self.status
        }


class ChatLog(db.Model):
    """Optional history of chat exchanges, useful for support/debugging."""
    __tablename__ = 'chat_logs'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    message = db.Column(db.Text, nullable=False)
    reply = db.Column(db.Text)
    lang = db.Column(db.String(5), default='en')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ─── HELPERS ──────────────────────────────────────────────────
def get_current_user():
    user_id = session.get('user_id')
    if user_id:
        return User.query.get(user_id)
    return None


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('user_id'):
            return jsonify({'error': 'Authentication required'}), 401
        return f(*args, **kwargs)
    return decorated


def validate_email(email):
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return re.match(pattern, email) is not None


def validate_password(password):
    if len(password) < 6:
        return False, 'Password must be at least 6 characters'
    return True, 'Valid'


def get_or_create_profile(user):
    profile = Profile.query.filter_by(user_id=user.id).first()
    if not profile:
        profile = Profile(user_id=user.id)
        db.session.add(profile)
        db.session.commit()
    return profile


# ─── AUTH ROUTES ──────────────────────────────────────────────
@app.route('/api/auth/signup', methods=['POST'])
def signup():
    try:
        data = request.get_json(force=True) or {}
        username = (data.get('username') or '').strip()
        email = (data.get('email') or '').strip().lower()
        password = data.get('password') or ''
        full_name = (data.get('full_name') or username).strip()

        if not username or not email or not password:
            return jsonify({'error': 'Username, email and password are required'}), 400
        if len(username) < 3:
            return jsonify({'error': 'Username must be at least 3 characters'}), 400
        if not validate_email(email):
            return jsonify({'error': 'Invalid email address'}), 400
        valid, msg = validate_password(password)
        if not valid:
            return jsonify({'error': msg}), 400

        if User.query.filter_by(username=username).first():
            return jsonify({'error': 'Username already taken'}), 409
        if User.query.filter_by(email=email).first():
            return jsonify({'error': 'Email already registered'}), 409

        user = User(username=username, email=email, full_name=full_name)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        # Create a default profile row for this user
        db.session.add(Profile(user_id=user.id))
        db.session.commit()

        session.permanent = True
        session['user_id'] = user.id

        return jsonify({'message': 'Account created successfully', 'user': user.to_dict()}), 201
    except Exception as e:
        db.session.rollback()
        print(f'Signup error: {str(e)}')
        return jsonify({'error': 'Signup failed. Please try again.'}), 500


@app.route('/api/auth/login', methods=['POST'])
def login():
    try:
        data = request.get_json(force=True) or {}
        identifier = (data.get('username') or data.get('email') or '').strip()
        password = data.get('password') or ''

        if not identifier or not password:
            return jsonify({'error': 'Username/email and password are required'}), 400

        user = User.query.filter(
            (User.username == identifier) | (User.email == identifier.lower())
        ).first()

        if not user or not user.check_password(password):
            return jsonify({'error': 'Invalid username or password'}), 401

        session.permanent = True
        session['user_id'] = user.id

        profile = get_or_create_profile(user)

        return jsonify({
            'message': 'Login successful',
            'user': user.to_dict(),
            'profile': profile.to_dict()
        }), 200
    except Exception as e:
        print(f'Login error: {str(e)}')
        return jsonify({'error': 'Login failed. Please try again.'}), 500


@app.route('/api/auth/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'message': 'Logged out successfully'}), 200


@app.route('/api/auth/me', methods=['GET'])
@login_required
def me():
    user = get_current_user()
    if not user:
        session.clear()
        return jsonify({'error': 'User not found'}), 404
    profile = get_or_create_profile(user)
    return jsonify({'user': user.to_dict(), 'profile': profile.to_dict()}), 200


# ─── PROFILE ROUTES ────────────────────────────────────────────
@app.route('/api/profile', methods=['GET'])
@login_required
def get_profile():
    user = get_current_user()
    profile = get_or_create_profile(user)
    return jsonify({'profile': profile.to_dict(), 'full_name': user.full_name}), 200


@app.route('/api/profile', methods=['PUT'])
@login_required
def update_profile():
    try:
        user = get_current_user()
        profile = get_or_create_profile(user)
        data = request.get_json(force=True) or {}

        if 'full_name' in data and data['full_name']:
            user.full_name = data['full_name'].strip()
        if 'age' in data:
            try:
                age = int(data['age'])
                if 0 <= age <= 120:
                    profile.age = age
            except (TypeError, ValueError):
                pass
        if 'occupation' in data:
            profile.occupation = data['occupation']
        if 'income' in data:
            profile.income = data['income']
        if 'state' in data:
            profile.state = data['state']
        if 'gender' in data:
            profile.gender = data['gender']
        if 'preferred_lang' in data:
            profile.preferred_lang = data['preferred_lang']

        db.session.commit()
        return jsonify({
            'message': 'Profile updated successfully',
            'profile': profile.to_dict(),
            'full_name': user.full_name
        }), 200
    except Exception as e:
        db.session.rollback()
        print(f'Profile update error: {str(e)}')
        return jsonify({'error': 'Failed to update profile'}), 500


# ─── APPLICATION (SCHEME TRACKING) ROUTES ─────────────────────
@app.route('/api/applications', methods=['GET'])
@login_required
def get_applications():
    user = get_current_user()
    apps = Application.query.filter_by(user_id=user.id).order_by(Application.submitted_at.desc()).all()
    return jsonify({'applications': [a.to_dict() for a in apps]}), 200


@app.route('/api/applications', methods=['POST'])
@login_required
def create_application():
    try:
        user = get_current_user()
        data = request.get_json(force=True) or {}
        scheme_id = data.get('scheme_id')
        scheme_title = data.get('scheme_title')
        category = data.get('category', '')

        if not scheme_id or not scheme_title:
            return jsonify({'error': 'scheme_id and scheme_title are required'}), 400

        existing = Application.query.filter_by(user_id=user.id, scheme_id=scheme_id).first()
        if existing:
            return jsonify({'error': 'You already have an application for this scheme', 'application': existing.to_dict()}), 409

        count = Application.query.filter_by(user_id=user.id).count()
        app_code = 'APP' + str(count + 1).zfill(3)

        application = Application(
            user_id=user.id,
            app_code=app_code,
            scheme_id=scheme_id,
            scheme_title=scheme_title,
            category=category
        )
        db.session.add(application)
        db.session.commit()

        return jsonify({'message': 'Application created', 'application': application.to_dict()}), 201
    except Exception as e:
        db.session.rollback()
        print(f'Create application error: {str(e)}')
        return jsonify({'error': 'Failed to create application'}), 500


# ─── GEMINI AI CHAT PROXY ──────────────────────────────────────
# The Gemini API key never reaches the browser — the frontend calls this
# endpoint, and this endpoint calls Google using the server-side key.
@app.route('/api/chat', methods=['POST'])
@login_required
def chat():
    try:
        data = request.get_json(force=True) or {}
        message = (data.get('message') or '').strip()
        lang = data.get('lang', 'en')

        if not message:
            return jsonify({'error': 'Message is required'}), 400
        if len(message) > 2000:
            return jsonify({'error': 'Message too long (max 2000 characters)'}), 400

        user = get_current_user()
        reply_text = None
        used_ai = False

        if GEMINI_API_KEY:
            try:
                prompt = (
                    "You are Saarthi AI, a helpful government services assistant for India. "
                    "Answer the user's question about Indian government schemes, documents, "
                    "procedures, and eligibility. Provide clear, step-by-step guidance. "
                    "Keep responses concise and helpful. "
                    f"User's language preference: {lang}. "
                    f"User question: {message}"
                )
                payload = {"contents": [{"parts": [{"text": prompt}]}]}
                resp = requests.post(
                    GEMINI_URL,
                    params={'key': GEMINI_API_KEY},
                    json=payload,
                    timeout=20
                )
                if resp.ok:
                    result = resp.json()
                    candidates = result.get('candidates', [])
                    if candidates and candidates[0].get('content', {}).get('parts'):
                        reply_text = candidates[0]['content']['parts'][0].get('text')
                        used_ai = True
            except requests.RequestException as e:
                print(f'Gemini request failed: {str(e)}')

        if not reply_text:
            reply_text = None  # Frontend will use its own rule-based fallback if this is None

        # Log the exchange (best-effort, don't fail the request if this fails)
        try:
            log = ChatLog(user_id=user.id, message=message, reply=reply_text, lang=lang)
            db.session.add(log)
            db.session.commit()
        except Exception:
            db.session.rollback()

        return jsonify({'reply': reply_text, 'used_ai': used_ai}), 200
    except Exception as e:
        print(f'Chat error: {str(e)}')
        return jsonify({'error': 'Chat request failed'}), 500


# ─── HEALTH / STATIC ────────────────────────────────────────────
@app.route('/', methods=['GET'])
def health_check():
    if 'text/html' in request.headers.get('Accept', '').lower():
        return send_from_directory(BASE_DIR, 'index.html')
    return jsonify({
        'status': 'success',
        'message': 'Saarthi AI API is running',
        'version': '1.0.0',
        'endpoints': {
            'auth': ['POST /api/auth/signup', 'POST /api/auth/login', 'POST /api/auth/logout', 'GET /api/auth/me'],
            'profile': ['GET /api/profile', 'PUT /api/profile'],
            'applications': ['GET /api/applications', 'POST /api/applications'],
            'chat': ['POST /api/chat']
        }
    }), 200


@app.route('/api/health', methods=['GET'])
def api_health():
    return jsonify({'status': 'healthy'}), 200


@app.errorhandler(404)
def not_found(error):
    return jsonify({'error': 'Endpoint not found'}), 404


@app.errorhandler(500)
def internal_error(error):
    db.session.rollback()
    return jsonify({'error': 'Internal server error'}), 500


# ─── INIT DB ────────────────────────────────────────────────────
with app.app_context():
    db.create_all()
    print("Database tables created (if not already present).")

# ─── ENTRY POINT ────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print("\n" + "=" * 70)
    print("  SAARTHI AI API")
    print("=" * 70)
    print(f"  Database: {database_url}")
    print(f"  Server:   http://0.0.0.0:{port}")
    print("=" * 70 + "\n")
    app.run(host='0.0.0.0', port=port, debug=False)
