import csv
import os
import sqlite3
import random
import string
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from functools import wraps
import threading
import time

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
try:
    from flask_cors import CORS
except ImportError:
    class CORS:  # noqa: stub when flask-cors not installed
        def __init__(self, *a, **kw): pass

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# ── Firebase Admin SDK (optional/live database) ───────────────────────────────
try:
    import firebase_admin
    from firebase_admin import credentials, db as rtdb, auth

    _fb_cred_path = os.getenv(
        'FIREBASE_CREDENTIALS',
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'firebase_key.json')
    )
    _fb_db_url = os.getenv('FIREBASE_DB_URL', '').replace('https://https://', 'https://').strip()

    if _fb_cred_path and _fb_db_url and os.path.exists(_fb_cred_path):
        if not firebase_admin._apps:
            _cred = credentials.Certificate(_fb_cred_path)
            firebase_admin.initialize_app(_cred, {'databaseURL': _fb_db_url})
        FIREBASE_ENABLED = True
        print('[Firebase] Connected to Realtime Database')
    else:
        FIREBASE_ENABLED = False
        print('[Firebase] Disabled: set FIREBASE_DB_URL and FIREBASE_CREDENTIALS to enable live sync')
except ImportError:
    FIREBASE_ENABLED = False
    print('[Firebase] firebase-admin not installed; Firebase sync disabled')
except Exception as _fb_err:
    FIREBASE_ENABLED = False
    print(f'[Firebase] Init error: {_fb_err}')

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', os.getenv('SECRET_KEY', 'smart-classroom-secret'))
CORS(app)

# ── Config ────────────────────────────────────────────────────────────────────
BASE_DIR             = os.path.dirname(os.path.abspath(__file__))
CSV_FILE             = os.path.join(BASE_DIR, 'smart_classroom_dataset.csv')
DB_FILE              = os.path.join(BASE_DIR, 'classroom.db')
CSV_REFRESH_INTERVAL = 30

EMAIL_ADDRESS = os.getenv('EMAIL_ADDRESS', '')
EMAIL_PASSWORD = os.getenv('EMAIL_PASSWORD', '')
SMTP_SERVER   = os.getenv('SMTP_SERVER', 'smtp.gmail.com')
SMTP_PORT     = int(os.getenv('SMTP_PORT', 587))

# Auto-mode thresholds
TEMP_THRESHOLD = 28.0   # °C  → turn ventilation ON above this
LDR_THRESHOLD  = 400    # raw → turn light ON below this (dark room)
ESP32_TIMEOUT_SECONDS = int(os.getenv('ESP32_TIMEOUT_SECONDS', 20))

CSV_HEADERS = [
    'server_timestamp','esp_timestamp','temperature','humidity','pressure',
    'pir','ldr','rfid_uid','rfid_present','occupancy','taux_occupation',
    'predicted_temp','light_decision','ventilation_decision',
    'final_light_state','final_ventilation_state','alert_message','source'
]

# ── In-memory caches ──────────────────────────────────────────────────────────
csv_data_cache = {
    'latest_row': {},
    'history_rows': [],
    'all_rows': [],
    'total_rows': 0,
    'last_update': None,
    'columns': []
}
verification_codes = {}  # fallback only when Firebase is unavailable

# ═══════════════════════════════════════════════════════════════════════════════
#  CSV DATA MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════

def _to_float(v, default=None):
    try:
        if v is None or v == '':
            return default
        return float(v)
    except Exception:
        return default

def _to_int(v, default=0):
    try:
        if v is None or v == '':
            return default
        return int(float(v))
    except Exception:
        return default

def parse_timestamp(value):
    if not value:
        return None
    text = str(value).strip().replace('Z', '')
    for fmt in (None, '%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S'):
        try:
            if fmt is None:
                return datetime.fromisoformat(text.replace('T', ' '))
            return datetime.strptime(text.replace('T', ' '), fmt)
        except Exception:
            pass
    return None

def normalize_row(row):
    """Make old and new CSV rows safe for all templates/APIs."""
    r = dict(row or {})

    # Some older files used timestamp instead of server_timestamp.
    if not r.get('server_timestamp'):
        r['server_timestamp'] = r.get('timestamp') or r.get('date_time') or r.get('time') or ''

    # Numeric fields
    r['temperature'] = _to_float(r.get('temperature', r.get('temp')), None)
    r['humidity'] = _to_float(r.get('humidity'), None)
    r['pressure'] = _to_float(r.get('pressure'), None)
    r['taux_occupation'] = _to_float(r.get('taux_occupation'), None)
    r['predicted_temp'] = _to_float(r.get('predicted_temp'), None)
    r['pir'] = _to_int(r.get('pir'), 0)
    r['ldr'] = _to_int(r.get('ldr'), 0)
    r['rfid_present'] = _to_int(r.get('rfid_present'), 0)
    r['occupancy'] = _to_int(r.get('occupancy'), 0)

    # Text/state fields
    r['esp_timestamp'] = str(r.get('esp_timestamp') or '')
    r['rfid_uid'] = str(r.get('rfid_uid') or r.get('rfid') or '')
    r['light_decision'] = str(r.get('light_decision') or '')
    r['ventilation_decision'] = str(r.get('ventilation_decision') or '')
    r['final_light_state'] = str(r.get('final_light_state') or '')
    r['final_ventilation_state'] = str(r.get('final_ventilation_state') or '')
    r['alert_message'] = str(r.get('alert_message') or '')
    r['source'] = str(r.get('source') or 'dataset')

    # Add a simple prediction if missing, just for dashboard display.
    if r['predicted_temp'] is None and r['temperature'] is not None:
        # small realistic offset, not used for training
        r['predicted_temp'] = round(float(r['temperature']) + 0.2, 2)

    for h in CSV_HEADERS:
        r.setdefault(h, '')
    return r

def parse_csv_line(line, headers):
    values = line.rstrip('\n').split(',')
    raw = {h: (values[i].strip() if i < len(values) else '') for i, h in enumerate(headers)}
    return normalize_row(raw)

def rewrite_csv_with_schema():
    """Upgrade older CSVs that have fewer columns so future ESP32 rows save correctly."""
    if not os.path.exists(CSV_FILE):
        return
    try:
        with open(CSV_FILE, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        if not lines:
            return
        headers = [h.strip() for h in lines[0].strip().split(',')]
        if headers == CSV_HEADERS:
            return
        rows = [parse_csv_line(line, headers) for line in lines[1:] if line.strip()]
        with open(CSV_FILE, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(CSV_HEADERS)
            for r in rows:
                writer.writerow([r.get(h, '') for h in CSV_HEADERS])
    except Exception as e:
        print(f"CSV schema upgrade error: {e}")

def load_csv_data():
    global csv_data_cache
    if not os.path.exists(CSV_FILE):
        return False
    try:
        rewrite_csv_with_schema()
        with open(CSV_FILE, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        if not lines:
            return False
        headers = [h.strip() for h in lines[0].strip().split(',')]
        data_rows = [parse_csv_line(line, headers) for line in lines[1:] if line.strip()]
        data_rows = [r for r in data_rows if r.get('server_timestamp') or r.get('temperature') is not None]
        if not data_rows:
            return False
        csv_data_cache = {
            'latest_row':   data_rows[-1],
            'history_rows': list(reversed(data_rows[-20:])),
            'all_rows':     data_rows,
            'total_rows':   len(data_rows),
            'last_update':  datetime.now().isoformat(timespec='seconds'),
            'columns':      CSV_HEADERS
        }
        return True
    except Exception as e:
        print(f"CSV load error: {e}")
        return False

def append_to_csv(data):
    try:
        rewrite_csv_with_schema()
        file_exists = os.path.exists(CSV_FILE)
        row = normalize_row(data)
        with open(CSV_FILE, 'a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            if not file_exists or os.path.getsize(CSV_FILE) == 0:
                writer.writerow(CSV_HEADERS)
            writer.writerow([row.get(h, '') for h in CSV_HEADERS])
        return True
    except Exception as e:
        print(f"CSV append error: {e}")
        return False

def get_esp32_status():
    """Realistic status: online only after a recent /api/sensor_data row from ESP32."""
    latest = csv_data_cache.get('latest_row') or {}
    ts = parse_timestamp(latest.get('server_timestamp'))
    source = (latest.get('source') or '').lower()
    if not ts or source != 'esp32':
        return {
            'online': False,
            'label': 'ESP32 Offline',
            'last_seen': latest.get('server_timestamp') or None,
            'age_seconds': None
        }
    age = (datetime.now() - ts).total_seconds()
    online = 0 <= age <= ESP32_TIMEOUT_SECONDS
    return {
        'online': online,
        'label': 'ESP32 Live' if online else 'ESP32 Offline',
        'last_seen': latest.get('server_timestamp'),
        'age_seconds': int(age) if age >= 0 else None
    }

def start_csv_updater():
    def loop():
        while True:
            load_csv_data()
            time.sleep(CSV_REFRESH_INTERVAL)
    threading.Thread(target=loop, daemon=True).start()

load_csv_data()
start_csv_updater()

# ═══════════════════════════════════════════════════════════════════════════════
#  DATABASE
# ═══════════════════════════════════════════════════════════════════════════════

def get_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def now_iso():
    return datetime.now().isoformat(timespec='seconds')

def init_db():
    conn = get_conn()
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS users (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT    NOT NULL,
        email       TEXT    NOT NULL UNIQUE,
        password    TEXT    NOT NULL,
        role        TEXT    NOT NULL CHECK(role IN ('admin','user')),
        created_at  TEXT    NOT NULL,
        verified    INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS control_state (
        id                 INTEGER PRIMARY KEY CHECK(id=1),
        mode               TEXT NOT NULL,
        light_mode         TEXT NOT NULL,
        light_state        TEXT NOT NULL,
        ventilation_mode   TEXT NOT NULL,
        ventilation_state  TEXT NOT NULL,
        temp_threshold     REAL NOT NULL DEFAULT 28.0,
        ldr_threshold      INTEGER NOT NULL DEFAULT 400,
        updated_at         TEXT NOT NULL,
        updated_by         TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS activity_log (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        message   TEXT NOT NULL
    );
    ''')

    # Migrate: add threshold columns if missing
    cols = [r['name'] for r in conn.execute("PRAGMA table_info(control_state)").fetchall()]
    if 'temp_threshold' not in cols:
        conn.execute('ALTER TABLE control_state ADD COLUMN temp_threshold REAL NOT NULL DEFAULT 28.0')
    if 'ldr_threshold' not in cols:
        conn.execute('ALTER TABLE control_state ADD COLUMN ldr_threshold INTEGER NOT NULL DEFAULT 400')

    # Migrate: add verified column if missing
    ucols = [r['name'] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    if 'verified' not in ucols:
        conn.execute('ALTER TABLE users ADD COLUMN verified INTEGER DEFAULT 0')

    # Default admin
    if not conn.execute("SELECT 1 FROM users WHERE email=?", ('admin@classroom.local',)).fetchone():
        conn.execute(
            'INSERT INTO users (name,email,password,role,created_at,verified) VALUES (?,?,?,?,?,?)',
            ('Administrator','admin@classroom.local','admin123','admin',now_iso(),1)
        )
    # Default test user
    if not conn.execute("SELECT 1 FROM users WHERE email=?", ('user@classroom.local',)).fetchone():
        conn.execute(
            'INSERT INTO users (name,email,password,role,created_at,verified) VALUES (?,?,?,?,?,?)',
            ('Test User','user@classroom.local','user123','user',now_iso(),1)
        )
    # Default control state
    if not conn.execute("SELECT 1 FROM control_state WHERE id=1").fetchone():
        conn.execute('''
            INSERT INTO control_state
            (id,mode,light_mode,light_state,ventilation_mode,ventilation_state,
             temp_threshold,ldr_threshold,updated_at,updated_by)
            VALUES (1,'auto','auto','OFF','auto','OFF',28.0,400,?,'system')
        ''', (now_iso(),))

    conn.commit()
    conn.close()

init_db()

def get_user_by_email(email):
    conn = get_conn()
    row = conn.execute('SELECT * FROM users WHERE lower(email)=lower(?)', (email,)).fetchone()
    conn.close()
    return row

def get_user_by_id(uid):
    conn = get_conn()
    row = conn.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
    conn.close()
    return row

def create_user(name, email, password, role='user'):
    conn = get_conn()
    try:
        conn.execute(
            'INSERT INTO users (name,email,password,role,created_at,verified) VALUES (?,?,?,?,?,?)',
            (name, email, password, role, now_iso(), 0)
        )
        conn.commit()
        uid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        user = {'id': uid, 'name': name, 'email': email, 'password': password, 'role': role, 'created_at': now_iso(), 'verified': 0}
        conn.close()
        sync_user_to_firebase(user, password=password)
        firebase_push('/activity_log', {'event_type': 'user_created', 'description': f'User created: {email}', 'user': 'system', 'timestamp': now_iso()})
        return uid, True, 'User created'
    except sqlite3.IntegrityError:
        conn.close()
        return None, False, 'Email already exists'

def verify_user_email(email):
    conn = get_conn()
    conn.execute('UPDATE users SET verified=1 WHERE lower(email)=lower(?)', (email,))
    conn.commit()
    row = conn.execute('SELECT * FROM users WHERE lower(email)=lower(?)', (email,)).fetchone()
    conn.close()
    if row:
        sync_user_to_firebase(row)
    if FIREBASE_ENABLED and email:
        try:
            fb_user = auth.get_user_by_email(email)
            auth.update_user(fb_user.uid, email_verified=True, disabled=False)
            firebase_update(f'/users/{fb_user.uid}', {'verified': 1, 'email_verified': True, 'verified_at': now_iso()})
        except Exception as e:
            print(f'[Firebase Auth] verify flag skipped for {email}: {e}')

def get_control_state():
    conn = get_conn()
    row = conn.execute('SELECT * FROM control_state WHERE id=1').fetchone()
    conn.close()
    return row

def update_control_state(**kwargs):
    cur = get_control_state()
    if not cur:
        return None
    conn = get_conn()
    conn.execute('''
        UPDATE control_state
        SET mode=?, light_mode=?, light_state=?,
            ventilation_mode=?, ventilation_state=?,
            temp_threshold=?, ldr_threshold=?,
            updated_at=?, updated_by=?
        WHERE id=1
    ''', (
        kwargs.get('mode',               cur['mode']),
        kwargs.get('light_mode',         cur['light_mode']),
        kwargs.get('light_state',        cur['light_state']),
        kwargs.get('ventilation_mode',   cur['ventilation_mode']),
        kwargs.get('ventilation_state',  cur['ventilation_state']),
        float(kwargs.get('temp_threshold') if kwargs.get('temp_threshold') not in (None, '') else cur['temp_threshold']),
        int(float(kwargs.get('ldr_threshold') if kwargs.get('ldr_threshold') not in (None, '') else cur['ldr_threshold'])),
        now_iso(),
        kwargs.get('updated_by', 'system'),
    ))
    conn.commit()
    conn.close()
    row = get_control_state()
    sync_controls_to_firebase(row, user=kwargs.get('updated_by', 'system'))
    return row

def _firebase_safe_key(value):
    return str(value or 'unknown').replace('.', '_').replace('#', '_').replace('$', '_').replace('[', '_').replace(']', '_').replace('/', '_')

def firebase_get(path, default=None):
    if not FIREBASE_ENABLED:
        return default
    try:
        value = rtdb.reference(path).get()
        return default if value is None else value
    except Exception as e:
        print(f'[Firebase] get error at {path}: {e}')
        return default

def firebase_set(path, data):
    if not FIREBASE_ENABLED:
        return False
    try:
        rtdb.reference(path).set(data)
        return True
    except Exception as e:
        print(f'[Firebase] set error at {path}: {e}')
        return False

def firebase_update(path, data):
    if not FIREBASE_ENABLED:
        return False
    try:
        rtdb.reference(path).update(data)
        return True
    except Exception as e:
        print(f'[Firebase] update error at {path}: {e}')
        return False

def firebase_push(path, data):
    if not FIREBASE_ENABLED:
        return None
    try:
        return rtdb.reference(path).push(data).key
    except Exception as e:
        print(f'[Firebase] push error at {path}: {e}')
        return None

def save_email_verification(email, name, code, expires, local_user_id=None):
    """Store signup verification code in Firebase RTDB; fallback to memory."""
    key = _firebase_safe_key(email)
    payload = {
        'email': email,
        'name': name,
        'code': code,
        'expires_at': expires.isoformat(),
        'created_at': now_iso(),
        'status': 'pending',
        'local_user_id': local_user_id,
    }
    if firebase_set(f'/email_verifications/{key}', payload):
        return True
    verification_codes[email] = {
        'code': code,
        'name': name,
        'expires': expires,
        'local_user_id': local_user_id,
    }
    return False

def get_email_verification(email):
    """Read verification code from Firebase RTDB, with memory fallback."""
    key = _firebase_safe_key(email)
    data = firebase_get(f'/email_verifications/{key}', None)
    if data:
        expires = parse_timestamp(data.get('expires_at')) or datetime.now()
        return {'code': str(data.get('code', '')), 'name': data.get('name', ''), 'expires': expires, 'local_user_id': data.get('local_user_id'), 'source': 'firebase'}
    stored = verification_codes.get(email)
    if stored:
        stored = dict(stored)
        stored['source'] = 'memory'
    return stored

def delete_email_verification(email, status='verified'):
    """Remove/close pending verification in Firebase and memory."""
    key = _firebase_safe_key(email)
    if FIREBASE_ENABLED:
        firebase_update(f'/email_verifications/{key}', {'status': status, 'closed_at': now_iso()})
        firebase_set(f'/email_verifications/{key}', None)
    verification_codes.pop(email, None)

def sync_user_to_firebase(user_or_dict, password=None):
    """Sync local CRUD users to Firebase Authentication + Realtime Database."""
    if not user_or_dict:
        return False
    user = dict(user_or_dict)
    email = str(user.get('email') or '').strip()
    if not email:
        return False
    uid = str(user.get('id') or _firebase_safe_key(email))

    if FIREBASE_ENABLED:
        try:
            try:
                fb_user = auth.get_user_by_email(email)
                fb_uid = fb_user.uid
                update_fields = {
                    'display_name': user.get('name') or email,
                    'disabled': False,
                    'email_verified': bool(user.get('verified', 0))
                }
                if password:
                    update_fields['password'] = password
                auth.update_user(fb_uid, **update_fields)
                uid = fb_uid
            except Exception:
                fb_user = auth.create_user(
                    email=email,
                    password=password or user.get('password') or 'SmartClassroom123!',
                    display_name=user.get('name') or email,
                    email_verified=bool(user.get('verified', 0))
                )
                uid = fb_user.uid
        except Exception as e:
            print(f'[Firebase Auth] user sync skipped for {email}: {e}')

    profile = {
        'local_id': user.get('id'),
        'name': user.get('name') or email,
        'email': email,
        'role': user.get('role', 'user'),
        'created_at': user.get('created_at') or now_iso(),
        'updated_at': now_iso(),
        'verified': int(user.get('verified', 0)),
    }
    return firebase_set(f'/users/{uid}', profile)

def delete_user_from_firebase(email, local_id=None):
    email = str(email or '').strip()
    uid = None
    if FIREBASE_ENABLED and email:
        try:
            uid = auth.get_user_by_email(email).uid
            auth.delete_user(uid)
        except Exception as e:
            print(f'[Firebase Auth] delete skipped for {email}: {e}')
    for key in [uid, str(local_id) if local_id else None, _firebase_safe_key(email) if email else None]:
        if key:
            firebase_set(f'/users/{key}', None)

def sync_controls_to_firebase(ctrl, action='control_state_updated', old_value=None, new_value=None, user='system'):
    if not ctrl:
        return False
    data = dict(ctrl)
    payload = {
        'mode': data.get('mode'),
        'light_mode': data.get('light_mode'),
        'light_state': data.get('light_state'),
        'fan': data.get('ventilation_state'),
        'ventilation': data.get('ventilation_state'),
        'ventilation_mode': data.get('ventilation_mode'),
        'ventilation_state': data.get('ventilation_state'),
        'temp_threshold': data.get('temp_threshold'),
        'ldr_threshold': data.get('ldr_threshold'),
        'updated_by': data.get('updated_by') or user or 'system',
        'updated_at': data.get('updated_at') or now_iso(),
    }
    firebase_set('/control_state', payload)
    firebase_set('/controls', payload)  # compatibility for older ESP32/dashboard code
    firebase_push('/control_history', {
        'action': action,
        'old_value': old_value or '',
        'new_value': new_value or payload,
        'user': user or payload.get('updated_by', 'system'),
        'timestamp': now_iso()
    })
    return True

def sync_sensor_to_firebase(row):
    if not row:
        return False
    r = normalize_row(row)
    latest = {
        'temperature': r.get('temperature'),
        'humidity': r.get('humidity'),
        'pressure': r.get('pressure'),
        'light_level': r.get('ldr'),
        'ldr': r.get('ldr'),
        'pir': r.get('pir'),
        'motion': bool(r.get('pir')),
        'rfid': r.get('rfid_uid'),
        'rfid_uid': r.get('rfid_uid'),
        'occupation_count': r.get('occupancy'),
        'occupancy': r.get('occupancy'),
        'occupation_rate': r.get('taux_occupation'),
        'taux_occupation': r.get('taux_occupation'),
        'predicted_temp': r.get('predicted_temp'),
        'light_decision': r.get('light_decision'),
        'ventilation_decision': r.get('ventilation_decision'),
        'final_light_state': r.get('final_light_state'),
        'final_ventilation_state': r.get('final_ventilation_state'),
        'alert_message': r.get('alert_message'),
        'timestamp': r.get('server_timestamp') or now_iso(),
        'server_timestamp': r.get('server_timestamp') or now_iso(),
        'source': r.get('source') or 'esp32',
    }
    firebase_set('/latest_data', latest)
    firebase_set('/latest_sensor', latest)
    firebase_push('/sensor_history', latest)
    firebase_set('/esp32_status', {'connected': True, 'last_seen': now_iso(), 'source': latest.get('source', 'esp32')})
    if latest.get('predicted_temp') not in (None, '', 'None'):
        firebase_push('/predictions', {'predicted_temperature': latest.get('predicted_temp'), 'model': 'temperature_prediction_model', 'timestamp': now_iso()})
    if latest.get('alert_message'):
        firebase_push('/alerts', {'type': 'system', 'message': latest.get('alert_message'), 'status': 'active', 'timestamp': now_iso()})
    return True

def get_firebase_history(limit=200):
    data = firebase_get('/sensor_history', {})
    if not data:
        return []
    rows = []
    for key, item in data.items():
        if isinstance(item, dict):
            row = normalize_row(item)
            row['firebase_id'] = key
            rows.append(row)
    rows.sort(key=lambda r: r.get('server_timestamp') or r.get('timestamp') or '', reverse=True)
    return rows[:limit]

def log_activity(message):
    conn = get_conn()
    conn.execute('INSERT INTO activity_log (timestamp,message) VALUES (?,?)', (now_iso(), message))
    conn.commit()
    conn.close()
    firebase_push('/activity_log', {
        'event_type': 'activity',
        'description': message,
        'user': session.get('email', 'system') if 'session' in globals() else 'system',
        'timestamp': now_iso()
    })

# ─── Auto-mode logic ──────────────────────────────────────────────────────────
def apply_auto_logic(temperature, ldr):
    """Return (light_state, ventilation_state) based on thresholds."""
    ctrl = get_control_state()
    t_thr = ctrl['temp_threshold'] if ctrl else TEMP_THRESHOLD
    l_thr = ctrl['ldr_threshold']  if ctrl else LDR_THRESHOLD

    vent  = 'ON' if (temperature is not None and temperature > t_thr) else 'OFF'
    light = 'ON' if (ldr is not None and ldr < l_thr) else 'OFF'
    return light, vent

# ═══════════════════════════════════════════════════════════════════════════════
#  EMAIL
# ═══════════════════════════════════════════════════════════════════════════════

def generate_verification_code():
    return ''.join(random.choices(string.digits, k=6))

def send_verification_email(email, code):
    if not EMAIL_ADDRESS or not EMAIL_PASSWORD:
        print(f"[EMAIL] Verification code for {email}: {code}")
        return True, 'Code generated (email not configured)'
    try:
        msg = MIMEMultipart()
        msg['From']    = EMAIL_ADDRESS
        msg['To']      = email
        msg['Subject'] = 'Smart Classroom — Email Verification'
        msg.attach(MIMEText(f'''<html><body>
          <div style="font-family:Arial;padding:20px;">
            <h2>Smart Classroom Verification</h2>
            <p>Your verification code:</p>
            <div style="font-size:32px;font-weight:bold;color:#3b6ff0;">{code}</div>
            <p>Expires in 10 minutes.</p>
          </div></body></html>''', 'html'))
        srv = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        srv.starttls()
        srv.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
        srv.send_message(msg)
        srv.quit()
        return True, 'Email sent'
    except Exception as e:
        print(f"Email error: {e}")
        return False, str(e)

# ═══════════════════════════════════════════════════════════════════════════════
#  AUTH DECORATORS
# ═══════════════════════════════════════════════════════════════════════════════

def login_required(f):
    @wraps(f)
    def wrap(*a, **kw):
        if 'user_id' not in session:
            flash('Please login first.', 'error')
            return redirect(url_for('login'))
        return f(*a, **kw)
    return wrap

def admin_required(f):
    @wraps(f)
    def wrap(*a, **kw):
        if session.get('role') != 'admin':
            flash('Admin access required.', 'error')
            return redirect(url_for('dashboard'))
        return f(*a, **kw)
    return wrap

def can_control():
    return session.get('role') == 'admin'

# ═══════════════════════════════════════════════════════════════════════════════
#  AUTH ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/')
def root():
    return redirect(url_for('dashboard') if 'user_id' in session else url_for('login'))

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        email    = request.form.get('email','').strip()
        password = request.form.get('password','').strip()
        user     = get_user_by_email(email)
        if user and user['password'] == password:
            if not user['verified']:
                flash('Please verify your email first.', 'error')
                return redirect(url_for('login'))
            session.update({'user_id': user['id'], 'name': user['name'],
                            'email': user['email'], 'role': user['role']})
            log_activity(f"{user['name']} logged in")
            return redirect(url_for('dashboard'))
        flash('Invalid email or password.', 'error')
    return render_template('login.html')

@app.route('/signup', methods=['GET','POST'])
def signup():
    if request.method == 'POST':
        name     = request.form.get('name','').strip()
        email    = request.form.get('email','').strip()
        password = request.form.get('password','').strip()
        confirm  = request.form.get('confirm_password','').strip()
        if not name or not email or not password:
            flash('All fields are required.', 'error')
            return redirect(url_for('signup'))
        if password != confirm:
            flash('Passwords do not match.', 'error')
            return redirect(url_for('signup'))
        if len(password) < 6:
            flash('Password must be at least 6 characters.', 'error')
            return redirect(url_for('signup'))

        existing = get_user_by_email(email)
        if existing and existing['verified']:
            flash('Email already registered.', 'error')
            return redirect(url_for('login'))
        if existing:
            uid = existing['id']
        else:
            uid, ok, msg = create_user(name, email, password)
            if not ok:
                flash(msg, 'error')
                return redirect(url_for('signup'))

        code = generate_verification_code()
        expires = datetime.now() + timedelta(minutes=10)
        save_email_verification(email, name, code, expires, local_user_id=uid)
        ok, msg = send_verification_email(email, code)
        if ok:
            firebase_push('/activity_log', {'event_type': 'verification_code_sent', 'description': f'Verification code sent to {email}', 'user': 'system', 'timestamp': now_iso()})
            flash('Verification code sent to your email.', 'success')
            return redirect(url_for('verify_email', email=email))
        flash(f'Failed to send email: {msg}', 'error')
    return render_template('signup.html')

@app.route('/verify_email', methods=['GET','POST'])
def verify_email():
    email = request.args.get('email','')
    if request.method == 'POST':
        email = request.form.get('email','').strip()
        code  = request.form.get('code','').strip()
        stored = get_email_verification(email)
        if not stored:
            flash('Invalid or expired verification request.', 'error')
            return redirect(url_for('signup'))
        if datetime.now() > stored['expires']:
            delete_email_verification(email, status='expired')
            flash('Code expired. Please sign up again.', 'error')
            return redirect(url_for('signup'))
        if str(stored['code']) == str(code):
            verify_user_email(email)
            delete_email_verification(email, status='verified')
            firebase_push('/activity_log', {'event_type': 'email_verified', 'description': f'Email verified: {email}', 'user': email, 'timestamp': now_iso()})
            flash('Email verified! You can now login.', 'success')
            return redirect(url_for('login'))
        flash('Invalid code.', 'error')
        return redirect(url_for('verify_email', email=email))
    return render_template('verify_email.html', email=email)

@app.route('/resend_code', methods=['POST'])
def resend_code():
    email = request.form.get('email','').strip()
    user = get_user_by_email(email)
    if not user:
        flash('Invalid request.', 'error')
        return redirect(url_for('signup'))
    if user['verified']:
        flash('This account is already verified. Please login.', 'success')
        return redirect(url_for('login'))
    code = generate_verification_code()
    expires = datetime.now() + timedelta(minutes=10)
    save_email_verification(email, user['name'], code, expires, local_user_id=user['id'])
    ok, msg = send_verification_email(email, code)
    flash('New code sent.' if ok else f'Failed: {msg}', 'success' if ok else 'error')
    return redirect(url_for('verify_email', email=email))

@app.route('/logout')
def logout():
    if 'user_id' in session:
        log_activity(f"{session.get('name')} logged out")
    session.clear()
    flash('Logged out.', 'success')
    return redirect(url_for('login'))

# ═══════════════════════════════════════════════════════════════════════════════
#  PAGE ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/dashboard')
@login_required
def dashboard():
    controls = get_control_state()
    firebase_latest = firebase_get('/latest_data', None)
    return render_template('dashboard.html', controls=controls, firebase_enabled=FIREBASE_ENABLED, firebase_latest=firebase_latest)

@app.route('/controls', methods=['GET','POST'])
@login_required
def controls_page():
    if request.method == 'POST':
        if not can_control():
            flash('Admin access required to change controls.', 'error')
            return redirect(url_for('controls_page'))

        form = request.form.get('form_name','')

        if form == 'global_mode':
            mode = request.form.get('mode','auto')
            if mode == 'manual':
                update_control_state(mode='manual', light_mode='manual', ventilation_mode='manual', updated_by=session['email'])
            else:
                update_control_state(mode='auto', light_mode='auto', ventilation_mode='auto', updated_by=session['email'])
            log_activity(f"{session['name']} set system mode → {mode}")
            flash(f'System mode set to {mode.upper()}.', 'success')

        elif form == 'lighting':
            current = get_control_state()
            lm = request.form.get('light_mode','auto')
            ls = request.form.get('light_state','OFF')
            if current['mode'] == 'auto' or lm == 'auto':
                update_control_state(light_mode='auto', updated_by=session['email'])
                log_activity(f"{session['name']} set light → AUTO mode; manual state ignored")
                flash('Lighting set to AUTO. Manual ON/OFF is disabled in auto mode.', 'success')
            else:
                update_control_state(light_mode='manual', light_state=ls, updated_by=session['email'])
                log_activity(f"{session['name']} set light → mode=manual, state={ls}")
                flash('Lighting updated.', 'success')

        elif form == 'ventilation':
            current = get_control_state()
            vm = request.form.get('ventilation_mode','auto')
            vs = request.form.get('ventilation_state','OFF')
            if current['mode'] == 'auto' or vm == 'auto':
                update_control_state(ventilation_mode='auto', updated_by=session['email'])
                log_activity(f"{session['name']} set ventilation → AUTO mode; manual state ignored")
                flash('Ventilation set to AUTO. Manual ON/OFF is disabled in auto mode.', 'success')
            else:
                update_control_state(ventilation_mode='manual', ventilation_state=vs, updated_by=session['email'])
                log_activity(f"{session['name']} set ventilation → mode=manual, state={vs}")
                flash('Ventilation updated.', 'success')

        elif form == 'thresholds':
            try:
                tt = float(request.form.get('temp_threshold', TEMP_THRESHOLD))
                lt = int(request.form.get('ldr_threshold',  LDR_THRESHOLD))
                update_control_state(temp_threshold=tt, ldr_threshold=lt, updated_by=session['email'])
                log_activity(f"{session['name']} updated thresholds: temp>{tt}°C, ldr<{lt}")
                flash(f'Thresholds updated: Temp > {tt}°C, LDR < {lt}.', 'success')
            except ValueError:
                flash('Invalid threshold values.', 'error')

        return redirect(url_for('controls_page'))

    controls = get_control_state()
    return render_template('controls.html', controls=controls, can_control=can_control())

@app.route('/history')
@login_required
def history_page():
    # Date filter from query params
    date_from = request.args.get('date_from','').strip()
    date_to   = request.args.get('date_to','').strip()
    search    = request.args.get('search','').strip().lower()

    rows = get_firebase_history(1000) or csv_data_cache['all_rows']  # Firebase first, CSV fallback

    # Apply date filter using the date part only (works with space or T timestamps)
    if date_from:
        rows = [r for r in rows if (r.get('server_timestamp') or '')[:10] >= date_from]
    if date_to:
        rows = [r for r in rows if (r.get('server_timestamp') or '')[:10] <= date_to]

    # Apply text search (timestamp, source, alert)
    if search:
        rows = [r for r in rows if search in ' '.join([
            str(r.get('server_timestamp') or ''),
            str(r.get('source') or ''),
            str(r.get('alert_message') or '')
        ]).lower()]

    # Reverse newest-first, cap at 200
    rows = list(reversed(rows))[:200]

    return render_template('history.html',
        rows=rows,
        total_rows=csv_data_cache['total_rows'],
        date_from=date_from,
        date_to=date_to,
        search=search
    )

@app.route('/users')
@login_required
@admin_required
def users_page():
    conn = get_conn()
    users = conn.execute('SELECT * FROM users ORDER BY id ASC').fetchall()
    conn.close()
    return render_template('users.html', users=users)

@app.route('/users/create', methods=['POST'])
@login_required
@admin_required
def users_create():
    name     = request.form.get('name','').strip()
    email    = request.form.get('email','').strip()
    password = request.form.get('password','').strip()
    role     = request.form.get('role','user')
    if not name or not email or not password:
        flash('All fields are required.', 'error')
        return redirect(url_for('users_page'))
    _, ok, msg = create_user(name, email, password, role)
    if ok:
        # Auto-verify admin-created users
        verify_user_email(email)
        log_activity(f"{session['name']} created user: {email} ({role})")
        flash(f'User {email} created successfully.', 'success')
    else:
        flash(msg, 'error')
    return redirect(url_for('users_page'))

@app.route('/users/update/<int:uid>', methods=['POST'])
@login_required
@admin_required
def users_update(uid):
    name  = request.form.get('name','').strip()
    role  = request.form.get('role','user')
    passw = request.form.get('password','').strip()
    conn  = get_conn()
    if passw:
        conn.execute('UPDATE users SET name=?,role=?,password=? WHERE id=?', (name, role, passw, uid))
    else:
        conn.execute('UPDATE users SET name=?,role=? WHERE id=?', (name, role, uid))
    conn.commit()
    user = conn.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
    conn.close()
    if user:
        sync_user_to_firebase(user, password=passw or None)
    log_activity(f"{session['name']} updated user id={uid}")
    firebase_push('/activity_log', {'event_type': 'user_updated', 'description': f'User updated id={uid}', 'user': session.get('email','admin'), 'timestamp': now_iso()})
    flash('User updated.', 'success')
    return redirect(url_for('users_page'))

@app.route('/users/delete/<int:uid>', methods=['POST'])
@login_required
@admin_required
def users_delete(uid):
    if uid == session.get('user_id'):
        flash("You can't delete yourself.", 'error')
        return redirect(url_for('users_page'))
    conn = get_conn()
    user = conn.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
    if user:
        delete_user_from_firebase(user['email'], local_id=uid)
    conn.execute('DELETE FROM users WHERE id=?', (uid,))
    conn.commit()
    conn.close()
    log_activity(f"{session['name']} deleted user id={uid}")
    firebase_push('/activity_log', {'event_type': 'user_deleted', 'description': f'User deleted id={uid}', 'user': session.get('email','admin'), 'timestamp': now_iso()})
    flash('User deleted.', 'success')
    return redirect(url_for('users_page'))

@app.route('/admin')
@login_required
@admin_required
def admin_page():
    conn     = get_conn()
    users    = conn.execute('SELECT * FROM users ORDER BY id ASC').fetchall()
    activity = conn.execute('SELECT * FROM activity_log ORDER BY id DESC LIMIT 50').fetchall()
    conn.close()
    return render_template('admin.html', users=users, activity=activity)

# Legacy redirects
@app.route('/manual')
@login_required
def manual_page():
    return redirect(url_for('controls_page'))

@app.route('/auto')
@login_required
def auto_page():
    return redirect(url_for('controls_page'))

@app.route('/ventilation')
@login_required
def ventilation_page():
    return redirect(url_for('controls_page'))

@app.route('/lighting')
@login_required
def lighting_page():
    return redirect(url_for('controls_page'))

# ═══════════════════════════════════════════════════════════════════════════════
#  API ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/api/latest_data')
def api_latest_data():
    ctrl = get_control_state()
    latest = firebase_get('/latest_data', None) or csv_data_cache['latest_row']
    history = get_firebase_history(20) or csv_data_cache['history_rows']
    live_controls = firebase_get('/control_state', None) or (dict(ctrl) if ctrl else {})
    return jsonify({
        'latest':     latest,
        'history':    history,
        'total_rows': len(history) if history else csv_data_cache['total_rows'],
        'last_update':csv_data_cache['last_update'],
        'columns':    csv_data_cache['columns'],
        'controls':   live_controls,
        'esp32':      firebase_get('/esp32_status', None) or get_esp32_status(),
        'firebase_enabled': FIREBASE_ENABLED
    })

@app.route('/api/sensor_data', methods=['POST'])
def api_sensor_data():
    """ESP32 → Flask: receive sensor data, apply auto logic, return control state."""
    try:
        data = request.get_json(force=True) or {}
        temp = float(data.get('temperature', 0))
        ldr  = int(data.get('ldr', 1023))

        ctrl = get_control_state()

        # Apply auto-mode logic
        auto_light, auto_vent = apply_auto_logic(temp, ldr)
        final_light = auto_light if ctrl['light_mode'] == 'auto' else ctrl['light_state']
        final_vent  = auto_vent  if ctrl['ventilation_mode'] == 'auto' else ctrl['ventilation_state']

        # If system is in full manual, use manual states
        if ctrl['mode'] == 'manual':
            final_light = ctrl['light_state']
            final_vent  = ctrl['ventilation_state']

        row_data = {
            'server_timestamp':      now_iso(),
            'esp_timestamp':         str(data.get('esp_timestamp','')),
            'temperature':           temp,
            'humidity':              float(data.get('humidity', 0)),
            'pressure':              float(data.get('pressure', 0)),
            'pir':                   int(data.get('pir', 0)),
            'ldr':                   ldr,
            'rfid_uid':              str(data.get('rfid') or data.get('rfid_uid') or ''),
            'rfid_present':          1 if (data.get('rfid') or data.get('rfid_uid')) else 0,
            'occupancy':             int(data.get('occupancy', data.get('occupation_count', 0))),
            'taux_occupation':       float(data.get('taux_occupation', data.get('occupation_rate', 0))),
            'predicted_temp':        data.get('predicted_temp',''),
            'light_decision':        auto_light,
            'ventilation_decision':  auto_vent,
            'final_light_state':     final_light,
            'final_ventilation_state': final_vent,
            'alert_message':         data.get('alert_message',''),
            'source':                'esp32',
        }

        if append_to_csv(row_data):
            sync_sensor_to_firebase(row_data)
            load_csv_data()
            return jsonify({
                'status':           'success',
                'light':            1 if final_light == 'ON' else 0,
                'ventilation':      1 if final_vent  == 'ON' else 0,
                'light_state':      final_light,
                'ventilation_state':final_vent,
                'mode':             ctrl['mode'],
            }), 200
        return jsonify({'status':'error','message':'CSV write failed'}), 500

    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'status':'error','message':str(e)}), 500

@app.route('/get_control')
@app.route('/api/controls')
def api_controls():
    """ESP32 polls this to get current control state."""
    ctrl = get_control_state()
    if not ctrl:
        return jsonify({'light':0,'ventilation':0})

    # If auto mode, compute from latest sensor reading
    latest = firebase_get('/latest_data', None) or csv_data_cache['latest_row']
    temp   = latest.get('temperature')
    ldr    = latest.get('ldr')
    auto_light, auto_vent = apply_auto_logic(temp, ldr)

    final_light = auto_light if ctrl['light_mode'] == 'auto' else ctrl['light_state']
    final_vent  = auto_vent  if ctrl['ventilation_mode'] == 'auto' else ctrl['ventilation_state']
    if ctrl['mode'] == 'manual':
        final_light = ctrl['light_state']
        final_vent  = ctrl['ventilation_state']

    return jsonify({
        'light':             1 if final_light == 'ON' else 0,
        'ventilation':       1 if final_vent  == 'ON' else 0,
        'light_state':       final_light,
        'ventilation_state': final_vent,
        'mode':              ctrl['mode'],
        'light_mode':        ctrl['light_mode'],
        'ventilation_mode':  ctrl['ventilation_mode'],
        'temp_threshold':    ctrl['temp_threshold'],
        'ldr_threshold':     ctrl['ldr_threshold'],
    })

@app.route('/update_control', methods=['POST'])
def update_control():
    """Dashboard/ESP32 can POST here to change control state. Manual states are blocked in auto mode."""
    if 'user_id' not in session and request.headers.get('X-API-Key') != os.getenv('API_KEY',''):
        return jsonify({'status':'unauthorized'}), 401
    data = request.get_json(silent=True) or request.form.to_dict() or {}
    current = get_control_state()
    payload = {
        'mode': data.get('mode'),
        'light_mode': data.get('light_mode'),
        'light_state': data.get('light_state'),
        'ventilation_mode': data.get('ventilation_mode'),
        'ventilation_state': data.get('ventilation_state'),
        'temp_threshold': data.get('temp_threshold'),
        'ldr_threshold': data.get('ldr_threshold'),
        'updated_by': session.get('email','api'),
    }
    requested_mode = payload.get('mode') or (current['mode'] if current else 'auto')
    requested_light_mode = payload.get('light_mode') or (current['light_mode'] if current else 'auto')
    requested_vent_mode = payload.get('ventilation_mode') or (current['ventilation_mode'] if current else 'auto')

    if requested_mode == 'auto':
        payload['mode'] = 'auto'
        payload['light_mode'] = 'auto'
        payload['ventilation_mode'] = 'auto'
        payload.pop('light_state', None)
        payload.pop('ventilation_state', None)
    else:
        if requested_light_mode == 'auto':
            payload.pop('light_state', None)
        if requested_vent_mode == 'auto':
            payload.pop('ventilation_state', None)

    ctrl = update_control_state(**payload)
    firebase_push('/activity_log', {'event_type': 'api_control_update', 'description': 'Control updated through API; manual ON/OFF ignored when mode is auto', 'user': session.get('email','api'), 'timestamp': now_iso()})
    return jsonify({'status':'ok', 'controls': dict(ctrl or get_control_state())})

@app.route('/api/history')
@login_required
def api_history():
    """Paginated/filtered history for AJAX use."""
    date_from = request.args.get('date_from','')
    date_to   = request.args.get('date_to','')
    limit     = min(int(request.args.get('limit', 100)), 500)
    rows = get_firebase_history(1000) or csv_data_cache['all_rows']
    if date_from:
        rows = [r for r in rows if (r.get('server_timestamp') or '')[:10] >= date_from]
    if date_to:
        rows = [r for r in rows if (r.get('server_timestamp') or '')[:10] <= date_to]
    return jsonify({'rows': list(reversed(rows))[:limit], 'total': len(rows)})

@app.route('/api/refresh')
def api_refresh():
    load_csv_data()
    return jsonify({'status':'refreshed','last_update':csv_data_cache['last_update']})

@app.route('/api/firebase_status')
def api_firebase_status():
    paths = {}
    if FIREBASE_ENABLED:
        for path in ['users','email_verifications','latest_data','sensor_history','control_state','control_history','activity_log','alerts','predictions','esp32_status']:
            paths[path] = firebase_get('/' + path, None) is not None
    return jsonify({'firebase_enabled': FIREBASE_ENABLED, 'paths': paths})

@app.route('/api/esp32_status')
def api_esp32_status():
    status = firebase_get('/esp32_status', None) or get_esp32_status()
    return jsonify(status)

# ─── Debug routes (keep from original) ───────────────────────────────────────
@app.route('/check_users')
def check_users():
    conn  = get_conn()
    users = conn.execute('SELECT id,name,email,verified FROM users').fetchall()
    conn.close()
    return '<h2>Users</h2>' + ''.join(f"<p>ID:{u['id']} {u['email']} verified={u['verified']}</p>" for u in users)

@app.route('/manual_verify/<email>')
def manual_verify(email):
    conn = get_conn()
    conn.execute('UPDATE users SET verified=1 WHERE email=?', (email,))
    conn.commit(); conn.close()
    return f'User {email} verified'

@app.route('/view_csv')
def view_csv():
    if not os.path.exists(CSV_FILE):
        return 'CSV file not found'
    try:
        with open(CSV_FILE,'r',encoding='utf-8') as f:
            lines = f.readlines()[:30]
        return f"<pre>{''.join(lines)}</pre>"
    except Exception as e:
        return f'Error: {e}'

# ═══════════════════════════════════════════════════════════════════════════════
if __name__ == '__main__':
    print("\n" + "="*50)
    print("Smart Classroom Flask Server")
    print("="*50)
    print(f"  CSV:  {CSV_FILE}")
    print(f"  DB:   {DB_FILE}")
    print(f"  Auto thresholds: Temp>{TEMP_THRESHOLD}°C | LDR<{LDR_THRESHOLD}")
    print(f"  Firebase enabled: {FIREBASE_ENABLED}")
    print("="*50)
    print("  Admin: admin@classroom.local / admin123")
    print("  User:  user@classroom.local  / user123")
    print("="*50 + "\n")
    app.run(host='0.0.0.0', port=5000, debug=True)
