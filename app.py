from flask import Flask, render_template, request, jsonify, redirect, url_for, session, send_file
from fitparse import FitFile
import pandas as pd
import os
import json
import requests
import hashlib
import traceback
from functools import wraps
import psycopg2
from psycopg2.extras import Json
from datetime import datetime
import math
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont, ImageFilter

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'cycling_dashboard_secret_2024_xk9_fallback')

# ─── IMPORTANT: Increase upload limit to 100 MB ────────────
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024

# ── Database Connection ───────────────────────────────────────
def get_db_connection():
    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        raise ValueError("DATABASE_URL environment variable not set!")
    conn = psycopg2.connect(database_url, sslmode='require')
    return conn

def init_db():
    """Create tables if they don't exist"""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username VARCHAR(100) PRIMARY KEY,
            github_name VARCHAR(200),
            avatar_url TEXT,
            email VARCHAR(200),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            profile JSONB DEFAULT '{}'::jsonb
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS rides (
            id SERIAL PRIMARY KEY,
            username VARCHAR(100) REFERENCES users(username) ON DELETE CASCADE,
            filename VARCHAR(255) NOT NULL,
            file_hash VARCHAR(64) NOT NULL,
            ride_type VARCHAR(50) DEFAULT 'cycling',
            ride_date TIMESTAMP,
            summary JSONB NOT NULL,
            streams JSONB NOT NULL,
            zone_distribution JSONB NOT NULL,
            route JSONB,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(username, file_hash)
        )
    """)
    cur.execute("""
        SELECT column_name 
        FROM information_schema.columns 
        WHERE table_name='rides' AND column_name='ride_type'
    """)
    if not cur.fetchone():
        cur.execute("ALTER TABLE rides ADD COLUMN ride_type VARCHAR(50) DEFAULT 'cycling'")
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_rides_username ON rides(username);
        CREATE INDEX IF NOT EXISTS idx_rides_date ON rides(ride_date);
        CREATE INDEX IF NOT EXISTS idx_rides_type ON rides(ride_type);
    """)
    conn.commit()
    cur.close()
    conn.close()
    print("[DB] Database initialized successfully!")

# ─── Lazy DB init ─────────────────────────────────────────────
_db_initialized = False

@app.before_request
def initialize_db():
    global _db_initialized
    if not _db_initialized:
        try:
            init_db()
            _db_initialized = True
        except Exception as e:
            print(f"[DB] Init failed: {e}")

# ── GitHub OAuth ──────────────────────────────────────────────
GITHUB_CLIENT_ID = os.environ.get('GITHUB_CLIENT_ID')
GITHUB_CLIENT_SECRET = os.environ.get('GITHUB_CLIENT_SECRET')
GITHUB_AUTH_URL = 'https://github.com/login/oauth/authorize'
GITHUB_TOKEN_URL = 'https://github.com/login/oauth/access_token'
GITHUB_API_URL = 'https://api.github.com/user'

UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ── Valid Ride Types ──────────────────────────────────────────
VALID_RIDE_TYPES = [
    'cycling', 'running', 'swimming', 'hiking', 'walking',
    'skiing', 'snowboarding', 'kayaking', 'rowing', 'yoga',
    'strength', 'elliptical'
]

RIDE_TYPES_INFO = [
    {"id": "cycling", "name": "Cycling", "icon": "🚴", "color": "#3498db"},
    {"id": "running", "name": "Running", "icon": "🏃", "color": "#e74c3c"},
    {"id": "swimming", "name": "Swimming", "icon": "🏊", "color": "#2ecc71"},
    {"id": "hiking", "name": "Hiking", "icon": "🥾", "color": "#f39c12"},
    {"id": "walking", "name": "Walking", "icon": "🚶", "color": "#27ae60"},
    {"id": "skiing", "name": "Skiing", "icon": "⛷️", "color": "#8e44ad"},
    {"id": "snowboarding", "name": "Snowboarding", "icon": "🏂", "color": "#3498db"},
    {"id": "kayaking", "name": "Kayaking", "icon": "🚣", "color": "#1abc9c"},
    {"id": "rowing", "name": "Rowing", "icon": "🚣‍♂️", "color": "#16a085"},
    {"id": "yoga", "name": "Yoga", "icon": "🧘", "color": "#9b59b6"},
    {"id": "strength", "name": "Strength", "icon": "💪", "color": "#e67e22"},
    {"id": "elliptical", "name": "Elliptical", "icon": "🚶", "color": "#2c3e50"}
]

# ── Decorators ────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated

# ── Database Helpers ──────────────────────────────────────────
def get_user_profile(username):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT profile FROM users WHERE username = %s", (username,))
    result = cur.fetchone()
    cur.close()
    conn.close()
    return result[0] if result else {}

def save_user_profile(username, profile):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET profile = %s WHERE username = %s", (Json(profile), username))
    conn.commit()
    cur.close()
    conn.close()

def create_or_update_user(username, name, avatar, email):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO users (username, github_name, avatar_url, email)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (username) DO UPDATE SET
            github_name = EXCLUDED.github_name,
            avatar_url = EXCLUDED.avatar_url,
            email = EXCLUDED.email
    """, (username, name, avatar, email))
    conn.commit()
    cur.close()
    conn.close()

def get_user_rides(username, ride_type=None):
    conn = get_db_connection()
    cur = conn.cursor()
    if ride_type:
        cur.execute("""
            SELECT id, filename, file_hash, ride_type, summary, streams, zone_distribution, route, ride_date, created_at
            FROM rides WHERE username = %s AND ride_type = %s
            ORDER BY ride_date DESC, created_at DESC
        """, (username, ride_type))
    else:
        cur.execute("""
            SELECT id, filename, file_hash, ride_type, summary, streams, zone_distribution, route, ride_date, created_at
            FROM rides WHERE username = %s
            ORDER BY ride_date DESC, created_at DESC
        """, (username,))
    rides = cur.fetchall()
    cur.close()
    conn.close()
    return [{
        "id": r[0],
        "filename": r[1],
        "file_hash": r[2],
        "ride_type": r[3],
        "summary": r[4],
        "streams": r[5],
        "zone_distribution": r[6],
        "route": r[7],
        "ride_date": r[8].isoformat() if r[8] else None,
        "created_at": r[9].isoformat() if r[9] else None
    } for r in rides]

def save_user_ride(username, ride_data, ride_type='cycling'):
    if ride_type not in VALID_RIDE_TYPES:
        ride_type = 'cycling'
    conn = get_db_connection()
    cur = conn.cursor()
    ride_date = None
    timestamps = ride_data.get('streams', {}).get('timestamps', [])
    if timestamps:
        try:
            ride_date = datetime.fromisoformat(timestamps[0])
        except:
            ride_date = datetime.now()
    else:
        ride_date = datetime.now()
    cur.execute("""
        INSERT INTO rides (username, filename, file_hash, ride_type, ride_date, summary, streams, zone_distribution, route)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (username, file_hash) DO UPDATE SET
            filename = EXCLUDED.filename,
            ride_type = EXCLUDED.ride_type,
            ride_date = EXCLUDED.ride_date,
            summary = EXCLUDED.summary,
            streams = EXCLUDED.streams,
            zone_distribution = EXCLUDED.zone_distribution,
            route = EXCLUDED.route
        RETURNING id
    """, (
        username,
        ride_data['filename'],
        ride_data['file_hash'],
        ride_type,
        ride_date,
        Json(ride_data['summary']),
        Json(ride_data['streams']),
        Json(ride_data['zone_distribution']),
        Json(ride_data.get('route', []))
    ))
    ride_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return ride_id

def delete_user_ride(username, ride_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM rides WHERE username = %s AND id = %s RETURNING filename", (username, ride_id))
    result = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    return result[0] if result else None

def get_all_users_stats(ride_type=None):
    conn = get_db_connection()
    cur = conn.cursor()
    if ride_type and ride_type in VALID_RIDE_TYPES:
        cur.execute("""
            SELECT u.username, COUNT(r.id) as total_rides,
                   COALESCE(MAX((r.summary->>'avg_speed')::float), 0) as best_avg_speed,
                   COALESCE(MAX((r.summary->>'max_speed')::float), 0) as best_max_speed,
                   COALESCE(SUM((r.summary->>'elevation_gain')::float), 0) as total_elevation,
                   COALESCE(SUM((r.summary->>'total_calories')::float), 0) as total_calories,
                   r.ride_type
            FROM users u LEFT JOIN rides r ON u.username = r.username
            WHERE r.id IS NOT NULL AND r.ride_type = %s
            GROUP BY u.username, r.ride_type
            ORDER BY best_avg_speed DESC
        """, (ride_type,))
    else:
        cur.execute("""
            SELECT u.username, COUNT(r.id) as total_rides,
                   COALESCE(MAX((r.summary->>'avg_speed')::float), 0) as best_avg_speed,
                   COALESCE(MAX((r.summary->>'max_speed')::float), 0) as best_max_speed,
                   COALESCE(SUM((r.summary->>'elevation_gain')::float), 0) as total_elevation,
                   COALESCE(SUM((r.summary->>'total_calories')::float), 0) as total_calories,
                   r.ride_type
            FROM users u LEFT JOIN rides r ON u.username = r.username
            WHERE r.id IS NOT NULL
            GROUP BY u.username, r.ride_type
            ORDER BY best_avg_speed DESC
        """)
    results = cur.fetchall()
    cur.close()
    conn.close()
    return [{
        "username": r[0],
        "total_rides": r[1],
        "best_avg_speed": round(r[2], 1),
        "best_max_speed": round(r[3], 1),
        "total_elevation": round(r[4], 1),
        "total_calories": round(r[5], 0),
        "ride_type": r[6]
    } for r in results]

def get_stats_by_type(username):
    rides = get_user_rides(username)
    stats = {}
    for ride in rides:
        rt = ride.get('ride_type', 'cycling')
        if rt not in stats:
            stats[rt] = {"count": 0, "total_distance": 0, "total_elevation": 0, "total_calories": 0}
        s = ride.get('summary', {})
        stats[rt]["count"] += 1
        stats[rt]["total_distance"] += s.get('distance_km', 0)
        stats[rt]["total_elevation"] += s.get('elevation_gain', 0)
        stats[rt]["total_calories"] += s.get('total_calories', 0)
    return stats

# ── Core parsing functions ────────────────────────────────────
def get_file_hash(data):
    return hashlib.md5(data).hexdigest()

def calculate_calories_and_power(df, profile):
    weight = float(profile.get('weight', 75))
    bike_type = profile.get('bike_type', 'road')
    cda_map = {'road': 0.32, 'mountain': 0.50, 'hybrid': 0.40, 'gravel': 0.36}
    crr_map = {'road': 0.004, 'mountain': 0.012, 'hybrid': 0.007, 'gravel': 0.007}
    CdA = cda_map.get(bike_type, 0.32)
    Crr = crr_map.get(bike_type, 0.004)
    air_density = 1.225
    gravity = 9.81
    total_mass = weight + 9
    efficiency = 0.97
    speeds_ms = df['speed_kmh'] / 3.6
    P_drag = 0.5 * air_density * CdA * speeds_ms ** 3
    P_roll = Crr * total_mass * gravity * speeds_ms
    power = (P_drag + P_roll) / efficiency
    power = power.clip(lower=0).round(1)
    calories_series = power / (4.184 * 1000 * 0.25)
    total_calories = round(float(calories_series.sum()), 0)
    return [float(x) for x in power.tolist()], int(total_calories)

def get_power_zones(ftp=200):
    return [
        {'zone': 'Z1 Recovery',  'min': 0,          'max': ftp * 0.55},
        {'zone': 'Z2 Endurance', 'min': ftp * 0.55, 'max': ftp * 0.75},
        {'zone': 'Z3 Tempo',     'min': ftp * 0.75, 'max': ftp * 0.90},
        {'zone': 'Z4 Threshold', 'min': ftp * 0.90, 'max': ftp * 1.05},
        {'zone': 'Z5 VO2 Max',   'min': ftp * 1.05, 'max': 9999},
    ]

def classify_power_zones(power_list, ftp=200):
    zones = get_power_zones(ftp)
    zone_time = {z['zone']: 0 for z in zones}
    for p in power_list:
        for z in zones:
            if z['min'] <= p < z['max']:
                zone_time[z['zone']] += 1
                break
    total = sum(zone_time.values()) or 1
    return [{'zone': k, 'seconds': v, 'pct': round(v / total * 100, 1)} for k, v in zone_time.items()]

def parse_ride(file_path, profile):
    fitfile = FitFile(file_path)
    data_points = []
    for record in fitfile.get_messages('record'):
        record_data = {}
        for field in record:
            if field.name in ['timestamp', 'cadence', 'speed', 'enhanced_altitude',
                              'altitude', 'position_lat', 'position_long',
                              'temperature', 'heart_rate', 'power', 'distance']:
                record_data[field.name] = field.value
        data_points.append(record_data)

    df = pd.DataFrame(data_points)
    df.dropna(subset=['timestamp'], inplace=True)

    # ── Calculate total duration ──────────────────────────
    total_duration_seconds = 0
    if len(df) > 0:
        try:
            first_ts = df['timestamp'].iloc[0]
            last_ts = df['timestamp'].iloc[-1]
            if first_ts and last_ts:
                if hasattr(first_ts, 'timestamp'):
                    total_duration_seconds = int((last_ts - first_ts).total_seconds())
                else:
                    total_duration_seconds = len(df)
        except Exception as e:
            print(f"Duration calculation error: {e}")
            total_duration_seconds = len(df)

    hours = total_duration_seconds // 3600
    minutes = (total_duration_seconds % 3600) // 60
    seconds = total_duration_seconds % 60
    total_time_formatted = f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    if 'enhanced_altitude' in df.columns:
        df['elevation'] = pd.to_numeric(df['enhanced_altitude'], errors='coerce').fillna(0)
    elif 'altitude' in df.columns:
        df['elevation'] = pd.to_numeric(df['altitude'], errors='coerce').fillna(0)
    else:
        df['elevation'] = 0.0

    if 'speed' in df.columns:
        df['speed_kmh'] = pd.to_numeric(df['speed'], errors='coerce').fillna(0) * 3.6
    else:
        df['speed_kmh'] = 0.0

    if 'cadence' in df.columns:
        df['cadence'] = pd.to_numeric(df['cadence'], errors='coerce').fillna(0)
    else:
        df['cadence'] = 0.0

    if 'temperature' in df.columns:
        df['temperature'] = pd.to_numeric(df['temperature'], errors='coerce').ffill().fillna(0)
        has_temperature = bool((df['temperature'] != 0).any())
    else:
        df['temperature'] = 0.0
        has_temperature = False

    if 'heart_rate' in df.columns:
        df['heart_rate'] = pd.to_numeric(df['heart_rate'], errors='coerce').fillna(0)
        has_hr = bool((df['heart_rate'] > 0).any())
    else:
        df['heart_rate'] = 0.0
        has_hr = False

    if 'position_lat' in df.columns and 'position_long' in df.columns:
        df['lat'] = pd.to_numeric(df['position_lat'], errors='coerce') * (180 / 2 ** 31)
        df['lng'] = pd.to_numeric(df['position_long'], errors='coerce') * (180 / 2 ** 31)
        route_df = df.dropna(subset=['lat', 'lng']).copy()
    else:
        df['lat'] = float('nan')
        df['lng'] = float('nan')
        route_df = pd.DataFrame(columns=['lat', 'lng'])

    df['elevation_diff'] = df['elevation'].diff()
    total_climbing = float(df['elevation_diff'][df['elevation_diff'] > 0].sum())

    if 'distance' in df.columns:
        dist_series = pd.to_numeric(df['distance'], errors='coerce')
        if dist_series.max() > 0:
            total_distance = round(float(dist_series.max()) / 1000, 2)
        else:
            total_distance = round(float(df['speed_kmh'].sum()) / 3600, 2)
    else:
        total_distance = round(float(df['speed_kmh'].sum()) / 3600, 2)

    if 'power' in df.columns:
        pwr_series = pd.to_numeric(df['power'], errors='coerce').fillna(0)
        if pwr_series.sum() > 0:
            power_list = [float(x) for x in pwr_series.tolist()]
            cal_series = pd.Series(power_list) / (4.184 * 1000 * 0.25)
            total_calories = int(round(float(cal_series.sum()), 0))
        else:
            power_list, total_calories = calculate_calories_and_power(df, profile)
    else:
        power_list, total_calories = calculate_calories_and_power(df, profile)

    avg_power = round(float(pd.Series(power_list).mean()), 1)
    max_power = round(float(pd.Series(power_list).max()), 1)
    ftp = float(profile.get('ftp', 200))
    zone_distribution = classify_power_zones(power_list, ftp)

    valid_cadence = df['cadence'][df['cadence'] > 0]
    valid_hr = df['heart_rate'][(df['heart_rate'] > 30) & (df['heart_rate'] < 220)] if has_hr else pd.Series([], dtype=float)
    valid_temp = df['temperature'][df['temperature'] != 0] if has_temperature else pd.Series([], dtype=float)

    summary = {
        "max_speed": round(float(df['speed_kmh'].max()), 1),
        "avg_speed": round(float(df['speed_kmh'].mean()), 1),
        "avg_cadence": round(float(valid_cadence.mean()), 1) if len(valid_cadence) > 0 else 0,
        "elevation_gain": round(total_climbing, 1),
        "elevation_loss": 0,
        "min_elevation": round(float(df['elevation'].min()), 1) if len(df['elevation']) > 0 else 0,
        "max_elevation": round(float(df['elevation'].max()), 1) if len(df['elevation']) > 0 else 0,
        "total_calories": total_calories,
        "avg_power": avg_power,
        "max_power": max_power,
        "distance_km": total_distance,
        "avg_hr": round(float(valid_hr.mean()), 0) if len(valid_hr) > 0 else None,
        "max_hr": round(float(valid_hr.max()), 0) if len(valid_hr) > 0 else None,
        "avg_temp": round(float(valid_temp.mean()), 1) if len(valid_temp) > 0 else None,
        "min_temp": round(float(valid_temp.min()), 1) if len(valid_temp) > 0 else None,
        "max_temp": round(float(valid_temp.max()), 1) if len(valid_temp) > 0 else None,
        "has_temperature": has_temperature,
        "has_hr": has_hr,
        "total_duration_seconds": total_duration_seconds,
        "total_time_formatted": total_time_formatted,
    }

    MAX_POINTS = 300
    if len(df) > MAX_POINTS:
        step = max(1, len(df) // MAX_POINTS)
        df_ds = df.iloc[::step].reset_index(drop=True)
        power_ds = power_list[::step][:len(df_ds)]
    else:
        df_ds = df
        power_ds = power_list

    timestamps = pd.to_datetime(df_ds['timestamp']).dt.strftime('%H:%M:%S').tolist()
    map_route = [[float(r), float(c)] for r, c in route_df[['lat', 'lng']].values.tolist()] if len(route_df) > 0 else []

    return {
        "summary": summary,
        "streams": {
            "timestamps": timestamps,
            "speed": [round(float(x), 1) for x in df_ds['speed_kmh'].tolist()],
            "cadence": [int(float(x)) for x in df_ds['cadence'].tolist()],
            "elevation": [round(float(x), 1) for x in df_ds['elevation'].tolist()],
            "power": [round(float(x), 1) for x in power_ds],
            "temperature": [round(float(x), 1) for x in df_ds['temperature'].tolist()] if has_temperature else [],
            "heart_rate": [int(float(x)) for x in df_ds['heart_rate'].tolist()] if has_hr else [],
        },
        "zone_distribution": zone_distribution,
        "route": map_route
    }

# ── Routes ─────────────────────────────────────────────────────
@app.route('/')
def index():
    if 'user' in session:
        return redirect(url_for('dashboard'))
    return render_template('landing.html')

@app.route('/auth/login')
def auth_login():
    return redirect(f'{GITHUB_AUTH_URL}?client_id={GITHUB_CLIENT_ID}&scope=read:user')

@app.route('/auth/callback')
def auth_callback():
    code = request.args.get('code')
    if not code:
        return redirect(url_for('index'))
    token_response = requests.post(GITHUB_TOKEN_URL, data={
        'client_id': GITHUB_CLIENT_ID,
        'client_secret': GITHUB_CLIENT_SECRET,
        'code': code
    }, headers={'Accept': 'application/json'})
    token_data = token_response.json()
    access_token = token_data.get('access_token')
    if not access_token:
        return redirect(url_for('index'))
    user_response = requests.get(GITHUB_API_URL, headers={
        'Authorization': f'token {access_token}', 'Accept': 'application/json'
    })
    user_data = user_response.json()
    username = user_data.get('login')
    name = user_data.get('name') or username
    avatar = user_data.get('avatar_url')
    email = user_data.get('email', '')
    create_or_update_user(username, name, avatar, email)
    session['user'] = {
        'username': username,
        'name': name,
        'avatar': avatar,
        'email': email
    }
    return redirect(url_for('dashboard'))

@app.route('/auth/logout')
def auth_logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/dashboard')
@login_required
def dashboard():
    return render_template('dashboard.html', user=session['user'])

@app.route('/profile', methods=['GET'])
@login_required
def get_profile():
    return jsonify(get_user_profile(session['user']['username']))

@app.route('/profile', methods=['POST'])
@login_required
def update_profile():
    data = request.get_json()
    allowed = ['weight', 'bike_type', 'bike_computer', 'sensors', 'ftp', 'bike_name']
    profile = {k: data[k] for k in allowed if k in data}
    save_user_profile(session['user']['username'], profile)
    return jsonify({"status": "saved"})

@app.route('/ride-types')
def get_ride_types():
    return jsonify(RIDE_TYPES_INFO)

@app.route('/rides/filter/<ride_type>')
@login_required
def get_rides_by_type(ride_type):
    if ride_type not in VALID_RIDE_TYPES:
        return jsonify({"error": "Invalid ride type"}), 400
    rides = get_user_rides(session['user']['username'], ride_type)
    return jsonify([{
        "id": r["id"],
        "filename": r["filename"],
        "ride_type": r["ride_type"],
        "summary": r["summary"]
    } for r in rides])

@app.route('/ride/<int:ride_id>', methods=['PUT'])
@login_required
def update_ride_type(ride_id):
    data = request.get_json()
    ride_type = data.get('ride_type')
    if ride_type not in VALID_RIDE_TYPES:
        return jsonify({"error": "Invalid ride type"}), 400
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE rides SET ride_type = %s WHERE username = %s AND id = %s RETURNING id",
                (ride_type, session['user']['username'], ride_id))
    if cur.fetchone():
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"status": "updated", "ride_type": ride_type})
    else:
        conn.rollback()
        cur.close()
        conn.close()
        return jsonify({"error": "Ride not found"}), 404

@app.route('/stats/by-type')
@login_required
def stats_by_type():
    return jsonify(get_stats_by_type(session['user']['username']))

@app.route('/check-duplicate', methods=['POST'])
@login_required
def check_duplicate():
    file = request.files.get('fitfile')
    if not file:
        return jsonify({"error": "No file"}), 400
    file_bytes = file.read()
    file_hash = get_file_hash(file_bytes)
    rides = get_user_rides(session['user']['username'])
    duplicate = next((r for r in rides if r.get('file_hash') == file_hash), None)
    return jsonify({
        "is_duplicate": duplicate is not None,
        "duplicate_id": duplicate.get('id') if duplicate else None,
    })

@app.route('/upload', methods=['POST'])
@login_required
def upload_file():
    try:
        file = request.files.get('fitfile')
        if not file:
            return jsonify({"error": "No file uploaded"}), 400
        ride_type = request.form.get('ride_type', 'cycling')
        if ride_type not in VALID_RIDE_TYPES:
            ride_type = 'cycling'
        overwrite_id = request.form.get('overwrite_id')
        filename = file.filename
        file_bytes = file.read()
        file_hash = get_file_hash(file_bytes)
        if overwrite_id:
            delete_user_ride(session['user']['username'], int(overwrite_id))
        final_path = os.path.join(UPLOAD_FOLDER, filename)
        with open(final_path, 'wb') as f:
            f.write(file_bytes)
        profile = get_user_profile(session['user']['username'])
        stats = parse_ride(final_path, profile)
        ride_data = {
            "filename": filename,
            "file_hash": file_hash,
            "summary": stats["summary"],
            "streams": stats["streams"],
            "zone_distribution": stats["zone_distribution"],
            "route": stats["route"]
        }
        ride_id = save_user_ride(session['user']['username'], ride_data, ride_type)
        stats["ride_type"] = ride_type
        stats["id"] = ride_id
        return jsonify(stats)
    except Exception as e:
        import traceback
        err = traceback.format_exc()
        print(err)
        return jsonify({"error": str(e), "trace": err}), 500

@app.route('/my-rides')
@login_required
def my_rides():
    ride_type = request.args.get('type')
    if ride_type and ride_type not in VALID_RIDE_TYPES:
        return jsonify({"error": "Invalid ride type"}), 400
    rides = get_user_rides(session['user']['username'], ride_type)
    return jsonify([{
        "id": r["id"],
        "filename": r["filename"],
        "ride_type": r["ride_type"],
        "summary": r["summary"]
    } for r in rides])

@app.route('/ride/<int:ride_id>')
@login_required
def get_ride(ride_id):
    rides = get_user_rides(session['user']['username'])
    ride = next((r for r in rides if r.get('id') == ride_id), None)
    if not ride:
        return jsonify({"error": "Ride not found"}), 404
    return jsonify(ride)

@app.route('/ride/<int:ride_id>', methods=['DELETE'])
@login_required
def delete_ride(ride_id):
    filename = delete_user_ride(session['user']['username'], ride_id)
    if filename:
        file_path = os.path.join(UPLOAD_FOLDER, filename)
        if os.path.exists(file_path):
            os.remove(file_path)
    return jsonify({"status": "deleted", "id": ride_id})

@app.route('/recalculate-all', methods=['POST'])
@login_required
def recalculate_all():
    username = session['user']['username']
    profile = get_user_profile(username)
    rides = get_user_rides(username)
    if not profile.get('weight') or not profile.get('bike_type'):
        return jsonify({"error": "Please set your weight and bike type in Profile first"}), 400
    results = {"updated": 0, "skipped": 0, "errors": []}
    for ride in rides:
        file_path = os.path.join(UPLOAD_FOLDER, ride.get('filename', ''))
        if not os.path.exists(file_path):
            results["skipped"] += 1
            results["errors"].append(f"Ride #{ride.get('id')}: file not found on disk")
            continue
        try:
            stats = parse_ride(file_path, profile)
            ride_data = {
                "filename": ride['filename'],
                "file_hash": ride['file_hash'],
                "summary": stats["summary"],
                "streams": stats["streams"],
                "zone_distribution": stats["zone_distribution"],
                "route": stats["route"]
            }
            save_user_ride(username, ride_data, ride.get('ride_type', 'cycling'))
            results["updated"] += 1
        except Exception as e:
            results["skipped"] += 1
            results["errors"].append(f"Ride #{ride.get('id')}: {str(e)}")
    return jsonify(results)

@app.route('/club')
@login_required
def club():
    ride_type = request.args.get('type')
    if ride_type and ride_type not in VALID_RIDE_TYPES:
        return jsonify({"error": "Invalid ride type"}), 400
    return jsonify(get_all_users_stats(ride_type))

# ─── FIX EXISTING RIDES - Recalculate times ──────────────────
@app.route('/fix-times', methods=['POST'])
@login_required
def fix_times():
    username = session['user']['username']
    rides = get_user_rides(username)
    results = {"fixed": 0, "errors": [], "updated": []}
    
    for ride in rides:
        try:
            streams = ride.get('streams', {})
            timestamps = streams.get('timestamps', [])
            
            if timestamps and len(timestamps) > 1:
                try:
                    first = datetime.strptime(timestamps[0], '%H:%M:%S')
                    last = datetime.strptime(timestamps[-1], '%H:%M:%S')
                    total_seconds = int((last - first).total_seconds())
                    if total_seconds < 0:
                        total_seconds = len(timestamps)
                except:
                    total_seconds = len(timestamps)
                
                hours = total_seconds // 3600
                minutes = (total_seconds % 3600) // 60
                seconds = total_seconds % 60
                time_formatted = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
                
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute("""
                    UPDATE rides 
                    SET summary = jsonb_set(
                        jsonb_set(summary, '{total_duration_seconds}', %s::jsonb),
                        '{total_time_formatted}', %s::jsonb
                    )
                    WHERE username = %s AND id = %s
                """, (json.dumps(total_seconds), json.dumps(time_formatted), username, ride['id']))
                conn.commit()
                cur.close()
                conn.close()
                results["fixed"] += 1
                results["updated"].append({
                    "id": ride['id'],
                    "filename": ride['filename'],
                    "old_time": "00:00:00",
                    "new_time": time_formatted
                })
        except Exception as e:
            results["errors"].append(f"Ride {ride['id']}: {str(e)}")
    
    return jsonify(results)

# ─── SAVE LIVE RIDE ──────────────────────────────────────────
@app.route('/save-live-ride', methods=['POST'])
@login_required
def save_live_ride():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data"}), 400
        points = data.get('points', [])
        if len(points) < 2:
            return jsonify({"error": "Not enough data points"}), 400
        ride_type = data.get('ride_type', 'cycling')
        if ride_type not in VALID_RIDE_TYPES:
            ride_type = 'cycling'

        # ── Extract temperature stream ──────────────────────
        temperature_stream = data.get('temperature', [])
        temperature_values = [t['temp_c'] for t in temperature_stream if 'temp_c' in t]
        temperature_timestamps = [t['timestamp'] for t in temperature_stream if 'temp_c' in t]

        profile = get_user_profile(session['user']['username'])

        timestamps = [p['timestamp'] for p in points]
        speeds_kmh = [float(p.get('speed_kmh', 0)) for p in points]
        hr_list = [int(p.get('heart_rate', 0)) for p in points]
        cadence_list = [int(p.get('cadence', 0)) for p in points]
        route = [[float(p['lat']), float(p['lng'])] for p in points if p.get('lat') and p.get('lng')]
        elevation_list = [float(p.get('altitude', 0)) for p in points]

        # ── Calculate total duration ──────────────────────────
        total_duration_seconds = 0
        if len(points) > 1:
            first_ts = points[0].get('timestamp')
            last_ts = points[-1].get('timestamp')
            if first_ts and last_ts:
                try:
                    if isinstance(first_ts, str):
                        first_dt = datetime.fromisoformat(first_ts.replace('Z', '+00:00'))
                        last_dt = datetime.fromisoformat(last_ts.replace('Z', '+00:00'))
                        total_duration_seconds = int((last_dt - first_dt).total_seconds())
                except Exception as e:
                    print(f"Duration calculation error: {e}")
                    total_duration_seconds = len(points)

        hours = total_duration_seconds // 3600
        minutes = (total_duration_seconds % 3600) // 60
        seconds = total_duration_seconds % 60
        total_time_formatted = f"{hours:02d}:{minutes:02d}:{seconds:02d}"

        total_dist = 0.0
        for i in range(1, len(points)):
            p1, p2 = points[i-1], points[i]
            if p1.get('lat') and p2.get('lat'):
                lat1, lon1 = math.radians(p1['lat']), math.radians(p1['lng'])
                lat2, lon2 = math.radians(p2['lat']), math.radians(p2['lng'])
                dlat, dlon = lat2 - lat1, lon2 - lon1
                a = math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
                total_dist += 6371 * 2 * math.asin(math.sqrt(a))

        elev_diffs = [max(0, elevation_list[i] - elevation_list[i-1]) for i in range(1, len(elevation_list))]
        total_climbing = sum(elev_diffs)

        df_temp = pd.DataFrame({'speed_kmh': speeds_kmh})
        power_list, total_calories = calculate_calories_and_power(df_temp, profile)

        avg_speed = round(sum(speeds_kmh) / len(speeds_kmh), 1) if speeds_kmh else 0
        max_speed = round(max(speeds_kmh), 1) if speeds_kmh else 0

        valid_hr = [h for h in hr_list if 30 < h < 220]
        valid_cad = [c for c in cadence_list if c > 0]

        ftp = float(profile.get('ftp', 200))
        zone_distribution = classify_power_zones(power_list, ftp)

        # ── Temperature stats ──────────────────────────────
        if temperature_values:
            avg_temp = round(sum(temperature_values) / len(temperature_values), 1)
            min_temp = round(min(temperature_values), 1)
            max_temp = round(max(temperature_values), 1)
            has_temperature = True
        else:
            avg_temp = min_temp = max_temp = None
            has_temperature = False

        # ── Elevation stats ──────────────────────────────────
        if elevation_list:
            min_elevation = round(min(elevation_list), 1)
            max_elevation = round(max(elevation_list), 1)
        else:
            min_elevation = max_elevation = 0

        summary = {
            "max_speed": max_speed,
            "avg_speed": avg_speed,
            "avg_cadence": round(sum(valid_cad)/len(valid_cad), 1) if valid_cad else 0,
            "elevation_gain": round(total_climbing, 1),
            "elevation_loss": 0,
            "min_elevation": min_elevation,
            "max_elevation": max_elevation,
            "total_calories": total_calories,
            "avg_power": round(sum(power_list)/len(power_list), 1),
            "max_power": round(max(power_list), 1),
            "distance_km": round(total_dist, 2),
            "avg_hr": round(sum(valid_hr)/len(valid_hr), 0) if valid_hr else None,
            "max_hr": round(max(valid_hr), 0) if valid_hr else None,
            "avg_temp": avg_temp,
            "min_temp": min_temp,
            "max_temp": max_temp,
            "has_temperature": has_temperature,
            "has_hr": len(valid_hr) > 0,
            "total_duration_seconds": total_duration_seconds,
            "total_time_formatted": total_time_formatted,
        }

        file_hash = hashlib.md5(json.dumps(points).encode()).hexdigest()
        filename = f"live_ride_{file_hash[:8]}.json"

        streams = {
            "timestamps": timestamps,
            "speed": speeds_kmh,
            "cadence": cadence_list,
            "elevation": elevation_list,
            "power": [round(float(p), 1) for p in power_list],
            "heart_rate": hr_list,
        }
        
        if temperature_values:
            streams["temperature"] = temperature_values
            streams["temperature_timestamps"] = temperature_timestamps

        ride_data = {
            "filename": filename,
            "file_hash": file_hash,
            "summary": summary,
            "streams": streams,
            "zone_distribution": zone_distribution,
            "route": route
        }

        ride_id = save_user_ride(session['user']['username'], ride_data, ride_type)
        return jsonify({"status": "saved", "id": ride_id, "ride_type": ride_type})

    except Exception as e:
        err = traceback.format_exc()
        print(err)
        return jsonify({"error": str(e), "detail": err}), 500

@app.route('/download/<int:ride_id>')
@login_required
def download_ride(ride_id):
    rides = get_user_rides(session['user']['username'])
    ride = next((r for r in rides if r.get('id') == ride_id), None)
    if not ride:
        return jsonify({"error": "Ride not found"}), 404
    filename = ride.get('filename')
    if not filename:
        return jsonify({"error": "File not found"}), 404
    file_path = os.path.join(UPLOAD_FOLDER, filename)
    if not os.path.exists(file_path):
        return jsonify({"error": "File not found on server"}), 404
    return send_file(file_path, as_attachment=True, download_name=filename)

# ─── SHARE IMAGE ROUTE ────────────────────────────────────────
@app.route('/share-image/<int:ride_id>', methods=['POST'])
@login_required
def share_image(ride_id):
    try:
        data = request.get_json()
        style = data.get('style', 'strava')
        
        rides = get_user_rides(session['user']['username'])
        ride = next((r for r in rides if r.get('id') == ride_id), None)
        if not ride:
            return jsonify({"error": "Ride not found"}), 404

        summary = ride.get('summary', {})
        ride_type = ride.get('ride_type', 'cycling')
        ride_type_info = next((t for t in RIDE_TYPES_INFO if t['id'] == ride_type), RIDE_TYPES_INFO[0])
        route = ride.get('route', [])

        width, height = 1080, 1920
        
        if style == 'strava':
            img = generate_strava_style(summary, ride_type_info, route, width, height)
        elif style == 'clean':
            img = generate_clean_style(summary, ride_type_info, route, width, height)
        elif style == 'minimal':
            img = generate_minimal_style(summary, ride_type_info, route, width, height)
        elif style == 'transparent':
            img = generate_transparent_style(summary, ride_type_info, route, width, height)
        else:
            img = generate_strava_style(summary, ride_type_info, route, width, height)

        buffer = BytesIO()
        img.save(buffer, format='PNG', optimize=True)
        buffer.seek(0)
        return send_file(buffer, mimetype='image/png', as_attachment=True, 
                        download_name=f"ride_share_{style}_{ride_id}.png")

    except Exception as e:
        print(f"Share image error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

# ── Share style generation functions ──────────────────────────
def load_font(size):
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "C:/Windows/Fonts/Arial.ttf",
    ]
    for path in font_paths:
        try:
            return ImageFont.truetype(path, size)
        except:
            continue
    return ImageFont.load_default()

def draw_route_on_image(draw, route, width, height, color=(252, 76, 2, 200)):
    """Draw the GPS route on the image"""
    if not route or len(route) < 2:
        return
    
    lats = [p[0] for p in route]
    lngs = [p[1] for p in route]
    min_lat, max_lat = min(lats), max(lats)
    min_lng, max_lng = min(lngs), max(lngs)
    lat_range = (max_lat - min_lat) or 1e-6
    lng_range = (max_lng - min_lng) or 1e-6
    
    padding = 60
    map_w, map_h = width - 2*padding, height - 2*padding
    scale = min(map_w / lng_range, map_h / lat_range) * 0.85
    
    points = []
    for lat, lng in route:
        x = padding + (lng - min_lng) * scale + (map_w - lng_range * scale) / 2
        y = padding + (max_lat - lat) * scale + (map_h - lat_range * scale) / 2
        points.append((x, y))
    
    if len(points) > 1:
        # Draw the route line with glow effect
        for width_mult in [3, 2, 1]:
            draw.line(points, fill=(252, 76, 2, 60 // width_mult), width=12 * width_mult, joint="curve")
        draw.line(points, fill=color, width=6, joint="curve")
        
        # Draw start marker (green)
        r = 16
        draw.ellipse([points[0][0]-r, points[0][1]-r, points[0][0]+r, points[0][1]+r], 
                    fill=(72, 187, 120, 255))
        draw.ellipse([points[0][0]-r//2, points[0][1]-r//2, points[0][0]+r//2, points[0][1]+r//2], 
                    fill=(72, 187, 120, 255))
        # Draw finish marker (orange)
        draw.ellipse([points[-1][0]-r, points[-1][1]-r, points[-1][0]+r, points[-1][1]+r], 
                    fill=(252, 76, 2, 255))
        draw.ellipse([points[-1][0]-r//2, points[-1][1]-r//2, points[-1][0]+r//2, points[-1][1]+r//2], 
                    fill=(252, 76, 2, 255))

def generate_strava_style(summary, ride_type_info, route, width, height):
    """Strava-style share - FULLY TRANSPARENT boxes, COLORFUL stats"""
    # Dark background with gradient
    img = Image.new('RGBA', (width, height), (10, 14, 26, 255))
    
    # Draw subtle grid pattern
    draw = ImageDraw.Draw(img)
    for i in range(0, width, 40):
        draw.line([(i, 0), (i, height)], fill=(30, 35, 50, 20), width=1)
    for i in range(0, height, 40):
        draw.line([(0, i), (width, i)], fill=(30, 35, 50, 20), width=1)
    
    # Draw the route on the image
    draw_route_on_image(draw, route, width, height, color=(252, 76, 2, 220))
    
    # Title
    font = load_font(50)
    draw.text((40, 40), f"{ride_type_info['icon']}  FIT READER", font=font, fill=(255,255,255,200))
    
    # Stats with FULLY TRANSPARENT backgrounds and COLORFUL text
    font_small = load_font(30)
    font_big = load_font(55)
    
    stats = [
        ("Distance", f"{summary.get('distance_km', 0):.1f} km", '#60a5fa'),
        ("Time", summary.get('total_time_formatted', '00:00:00'), '#facc15'),
        ("Elevation", f"{summary.get('elevation_gain', 0):.0f} m", '#34d399'),
        ("Avg Speed", f"{summary.get('avg_speed', 0):.1f} km/h", '#f87171'),
    ]
    
    y_start = 350
    for i, (label, value, color) in enumerate(stats):
        col = i % 2
        row = i // 2
        x = 150 + col * 500
        y = y_start + row * 280
        
        # FULLY TRANSPARENT box behind value (just a subtle border)
        bbox = draw.textbbox((0, 0), value, font=font_big)
        val_w = bbox[2] - bbox[0]
        val_h = bbox[3] - bbox[1]
        
        # Just draw a subtle outline, NO fill
        draw.rounded_rectangle(
            [(x - val_w//2 - 30, y - 15),
             (x + val_w//2 + 30, y + val_h + 20)],
            radius=15,
            fill=(0, 0, 0, 0),  # COMPLETELY TRANSPARENT
            outline=(255, 255, 255, 20),
            width=1
        )
        # COLORFUL text
        draw.text((x - val_w//2, y), value, font=font_big, fill=color)
        
        # Label - completely transparent box
        bbox = draw.textbbox((0, 0), label, font=font_small)
        label_w = bbox[2] - bbox[0]
        label_h = bbox[3] - bbox[1]
        
        draw.rounded_rectangle(
            [(x - label_w//2 - 15, y + val_h + 15),
             (x + label_w//2 + 15, y + val_h + label_h + 30)],
            radius=10,
            fill=(0, 0, 0, 0),
            outline=(255, 255, 255, 15),
            width=1
        )
        draw.text((x - label_w//2, y + val_h + 20), label, font=font_small, fill=(255, 255, 255, 180))
    
    # Extra stats with colors
    extra_y = 1200
    extra_stats = []
    if summary.get('avg_hr'):
        extra_stats.append(f"❤️ {summary['avg_hr']:.0f} bpm")
    if summary.get('total_calories'):
        extra_stats.append(f"🔥 {summary['total_calories']} cal")
    if extra_stats:
        extra_text = "  •  ".join(extra_stats)
        bbox = draw.textbbox((0, 0), extra_text, font=font_small)
        extra_w = bbox[2] - bbox[0]
        extra_h = bbox[3] - bbox[1]
        
        draw.rounded_rectangle(
            [(width//2 - extra_w//2 - 25, extra_y - 10),
             (width//2 + extra_w//2 + 25, extra_y + extra_h + 20)],
            radius=15,
            fill=(0, 0, 0, 0),
            outline=(255, 255, 255, 20),
            width=1
        )
        draw.text((width//2 - extra_w//2, extra_y + 4), extra_text, font=font_small, fill=(255, 255, 255, 220))
    
    # Brand at bottom
    font_brand = load_font(28)
    draw.text((width//2 - 80, height - 50), "🚴 FIT READER", font=font_brand, fill=(252, 76, 2, 180))
    
    return img

def generate_clean_style(summary, ride_type_info, route, width, height):
    """Clean stats card - FULLY TRANSPARENT boxes, COLORFUL stats"""
    img = Image.new('RGBA', (width, height), (13, 13, 26, 255))
    draw = ImageDraw.Draw(img)
    
    # Draw route
    draw_route_on_image(draw, route, width, height, color=(252, 76, 2, 160))
    
    # Border - subtle
    draw.rectangle([(20, 20), (width-20, height-20)], outline=(252,76,2,60), width=2)
    
    # Header
    font = load_font(60)
    draw.text((width//2 - 100, 50), f"{ride_type_info['icon']}  {ride_type_info['name']}", 
              font=font, fill=(255, 255, 255, 220))
    
    stats = [
        ("Distance", f"{summary.get('distance_km', 0):.1f} km", '#60a5fa'),
        ("Time", summary.get('total_time_formatted', '00:00:00'), '#facc15'),
        ("Elevation", f"{summary.get('elevation_gain', 0):.0f} m", '#34d399'),
        ("Avg Speed", f"{summary.get('avg_speed', 0):.1f} km/h", '#f87171'),
        ("Avg HR", f"{summary.get('avg_hr', 0):.0f} bpm" if summary.get('avg_hr') else "—", '#fb923c'),
        ("Calories", f"{summary.get('total_calories', 0)}", '#f472b6'),
        ("Avg Power", f"{summary.get('avg_power', 0):.0f} W", '#c084fc'),
        ("Cadence", f"{summary.get('avg_cadence', 0):.0f} rpm", '#22d3ee'),
    ]
    
    font_label = load_font(28)
    font_value = load_font(45)
    
    cols = 2
    rows = (len(stats) + cols - 1) // cols
    cell_w = width // cols
    cell_h = (height - 200) // rows
    
    for i, (label, value, color) in enumerate(stats):
        col = i % cols
        row = i // cols
        x = col * cell_w + cell_w // 2
        y = 160 + row * cell_h
        
        bbox = draw.textbbox((0, 0), value, font=font_value)
        val_w = bbox[2] - bbox[0]
        val_h = bbox[3] - bbox[1]
        
        # FULLY TRANSPARENT box
        draw.rounded_rectangle(
            [(x - val_w//2 - 25, y - 12),
             (x + val_w//2 + 25, y + val_h + 16)],
            radius=12,
            fill=(0, 0, 0, 0),
            outline=(255, 255, 255, 15),
            width=1
        )
        # COLORFUL text
        draw.text((x - val_w//2, y), value, font=font_value, fill=color)
        
        # FULLY TRANSPARENT box for label
        bbox = draw.textbbox((0, 0), label, font=font_label)
        label_w = bbox[2] - bbox[0]
        draw.rounded_rectangle(
            [(x - label_w//2 - 15, y + val_h + 10),
             (x + label_w//2 + 15, y + val_h + label_h + 24)],
            radius=8,
            fill=(0, 0, 0, 0),
            outline=(255, 255, 255, 15),
            width=1
        )
        draw.text((x - label_w//2, y + val_h + 14), label, font=font_label, fill=(255, 255, 255, 180))
    
    return img

def generate_minimal_style(summary, ride_type_info, route, width, height):
    """Minimal style - FULLY TRANSPARENT boxes, COLORFUL stats"""
    img = Image.new('RGBA', (width, height), (10, 10, 20, 255))
    draw = ImageDraw.Draw(img)
    
    # Draw route
    draw_route_on_image(draw, route, width, height, color=(252, 76, 2, 140))
    
    font_big = load_font(180)
    font_med = load_font(50)
    font_small = load_font(35)
    
    # Big distance with FULLY TRANSPARENT box
    dist_text = f"{summary.get('distance_km', 0):.1f}"
    bbox = draw.textbbox((0, 0), dist_text, font=font_big)
    dist_w = bbox[2] - bbox[0]
    dist_h = bbox[3] - bbox[1]
    
    draw.rounded_rectangle(
        [(width//2 - dist_w//2 - 50, 350 - 20),
         (width//2 + dist_w//2 + 50, 350 + dist_h + 25)],
        radius=20,
        fill=(0, 0, 0, 0),
        outline=(255, 255, 255, 20),
        width=2
    )
    # BLUE distance
    draw.text((width//2 - dist_w//2, 350), dist_text, font=font_big, fill='#60a5fa')
    
    bbox = draw.textbbox((0, 0), "km", font=font_med)
    km_w = bbox[2] - bbox[0]
    draw.rounded_rectangle(
        [(width//2 - km_w//2 - 25, 550 - 10),
         (width//2 + km_w//2 + 25, 550 + 60)],
        radius=10,
        fill=(0, 0, 0, 0),
        outline=(255, 255, 255, 15),
        width=1
    )
    draw.text((width//2 - km_w//2, 550), "km", font=font_med, fill=(200, 200, 205, 255))
    
    # Mini stats with FULLY TRANSPARENT boxes and COLORS
    stats = [
        ("⏱️", summary.get('total_time_formatted', '00:00:00'), '#facc15'),
        ("⛰️", f"{summary.get('elevation_gain', 0)}m", '#34d399'),
        ("⚡", f"{summary.get('avg_speed', 0)} km/h", '#f87171'),
    ]
    
    y = 850
    x_positions = [200, 540, 880]
    for i, (icon, value, color) in enumerate(stats):
        x = x_positions[i]
        text = f"{icon} {value}"
        bbox = draw.textbbox((0, 0), text, font=font_small)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        
        draw.rounded_rectangle(
            [(x - text_w//2 - 25, y - 10),
             (x + text_w//2 + 25, y + text_h + 15)],
            radius=12,
            fill=(0, 0, 0, 0),
            outline=(255, 255, 255, 15),
            width=1
        )
        # COLORFUL text
        draw.text((x - text_w//2, y), text, font=font_small, fill=color)
    
    # Brand
    font_brand = load_font(28)
    draw.text((width//2 - 80, height - 50), "🚴 FIT READER", font=font_brand, fill=(252, 76, 2, 180))
    
    return img

def generate_transparent_style(summary, ride_type_info, route, width, height):
    """Pure transparent style - FULLY TRANSPARENT boxes, COLORFUL stats"""
    img = Image.new('RGBA', (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    
    font_large = load_font(80)
    font_medium = load_font(55)
    font_small = load_font(35)
    
    # Header with FULLY TRANSPARENT box
    header_text = f"{ride_type_info['icon']}  {ride_type_info['name']}"
    bbox = draw.textbbox((0, 0), header_text, font=font_large)
    text_w = bbox[2] - bbox[0]
    
    header_y = 80
    draw.rounded_rectangle(
        [(width//2 - text_w//2 - 40, header_y - 20), 
         (width//2 + text_w//2 + 40, header_y + 75)],
        radius=20,
        fill=(0, 0, 0, 0),
        outline=(255, 255, 255, 20),
        width=2
    )
    draw.text((width//2 - text_w//2, header_y), header_text, font=font_large, fill=(255, 255, 255, 255))
    
    # Stats with FULLY TRANSPARENT boxes and COLORS
    stats = [
        ("Distance", f"{summary.get('distance_km', 0):.1f} km", '#60a5fa'),
        ("Time", summary.get('total_time_formatted', '00:00:00'), '#facc15'),
        ("Elevation", f"{summary.get('elevation_gain', 0):.0f} m", '#34d399'),
        ("Avg Speed", f"{summary.get('avg_speed', 0):.1f} km/h", '#f87171'),
    ]
    
    y_start = 350
    for i, (label, value, color) in enumerate(stats):
        col = i % 2
        row = i // 2
        x = 120 + col * 500
        y = y_start + row * 280
        
        bbox = draw.textbbox((0, 0), value, font=font_medium)
        val_w = bbox[2] - bbox[0]
        val_h = bbox[3] - bbox[1]
        
        # FULLY TRANSPARENT box for value
        draw.rounded_rectangle(
            [(x - val_w//2 - 30, y - 15),
             (x + val_w//2 + 30, y + val_h + 20)],
            radius=15,
            fill=(0, 0, 0, 0),
            outline=(255, 255, 255, 20),
            width=2
        )
        # COLORFUL text
        draw.text((x - val_w//2, y), value, font=font_medium, fill=color)
        
        # FULLY TRANSPARENT box for label
        bbox_label = draw.textbbox((0, 0), label.upper(), font=font_small)
        label_w = bbox_label[2] - bbox_label[0]
        label_h = bbox_label[3] - bbox_label[1]
        
        draw.rounded_rectangle(
            [(x - label_w//2 - 15, y + val_h + 20),
             (x + label_w//2 + 15, y + val_h + label_h + 32)],
            radius=10,
            fill=(0, 0, 0, 0),
            outline=(255, 255, 255, 15),
            width=1
        )
        draw.text((x - label_w//2, y + val_h + 24), label.upper(), font=font_small, fill=(255, 255, 255, 200))
    
    # Extra stats
    extra_y = 1200
    extra_stats = []
    if summary.get('avg_hr'):
        extra_stats.append(f"❤️ {summary['avg_hr']:.0f} bpm")
    if summary.get('total_calories'):
        extra_stats.append(f"🔥 {summary['total_calories']} cal")
    if summary.get('avg_power'):
        extra_stats.append(f"⚡ {summary['avg_power']:.0f} W")
    
    if extra_stats:
        extra_text = "  •  ".join(extra_stats)
        bbox = draw.textbbox((0, 0), extra_text, font=font_small)
        extra_w = bbox[2] - bbox[0]
        extra_h = bbox[3] - bbox[1]
        
        draw.rounded_rectangle(
            [(width//2 - extra_w//2 - 30, extra_y - 10),
             (width//2 + extra_w//2 + 30, extra_y + extra_h + 20)],
            radius=15,
            fill=(0, 0, 0, 0),
            outline=(255, 255, 255, 20),
            width=1
        )
        draw.text((width//2 - extra_w//2, extra_y + 4), extra_text, font=font_small, fill=(255, 255, 255, 220))
    
    # Brand
    brand_text = "FIT READER"
    bbox = draw.textbbox((0, 0), brand_text, font=font_small)
    brand_w = bbox[2] - bbox[0]
    
    draw.rounded_rectangle(
        [(width//2 - brand_w//2 - 25, height - 75),
         (width//2 + brand_w//2 + 25, height - 35)],
        radius=12,
        fill=(0, 0, 0, 0),
        outline=(255, 255, 255, 15),
        width=1
    )
    draw.text((width//2 - brand_w//2, height - 62), brand_text, font=font_small, fill=(255, 255, 255, 180))
    
    return img

# ─── SHARE ROUTE ─────────────────────────────────────────────
@app.route('/share/<int:ride_id>')
@login_required
def share_ride(ride_id):
    rides = get_user_rides(session['user']['username'])
    ride = next((r for r in rides if r.get('id') == ride_id), None)
    if not ride:
        return jsonify({"error": "Ride not found"}), 404

    summary = ride.get('summary', {})
    ride_type = ride.get('ride_type', 'cycling')
    ride_type_info = next((t for t in RIDE_TYPES_INFO if t['id'] == ride_type), RIDE_TYPES_INFO[0])
    route = ride.get('route', [])

    width, height = 1080, 1920
    img = Image.new('RGBA', (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "C:/Windows/Fonts/Arial.ttf",
    ]
    def load_font(size):
        for path in font_paths:
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
        return ImageFont.load_default()

    font_header = load_font(64)
    font_value = load_font(68)
    font_label = load_font(28)
    font_footer = load_font(34)

    def centered_text(y, text, font, fill, cx=width // 2):
        bbox = draw.textbbox((0, 0), text, font=font)
        w = bbox[2] - bbox[0]
        draw.text((cx - w / 2, y), text, font=font, fill=fill)

    centered_text(90, f"{ride_type_info['icon']}  {ride_type_info['name']}", font_header, (255, 255, 255, 255))

    stats = [
        ("Distance", f"{summary.get('distance_km', 0):.2f} km", (255, 255, 255, 255)),
        ("Avg Speed", f"{summary.get('avg_speed', 0):.1f} km/h", (66, 153, 225, 255)),
        ("Elevation", f"{summary.get('elevation_gain', 0):.0f} m", (241, 196, 15, 255)),
        ("Calories", f"{summary.get('total_calories', 0)}", (255, 138, 101, 255)),
        ("Avg Power", f"{summary.get('avg_power', 0):.0f} W", (155, 89, 182, 255)),
        ("Avg HR", f"{summary.get('avg_hr', 0):.0f} bpm" if summary.get('avg_hr') else "— bpm", (231, 76, 60, 255)),
    ]
    col_centers = [width * 0.28, width * 0.72]
    row_start_y, row_height = 300, 190
    for i, (label, value, color) in enumerate(stats):
        col, row = i % 2, i // 2
        cx = col_centers[col]
        y = row_start_y + row * row_height
        centered_text(y, label.upper(), font_label, (150, 150, 155, 255), cx=cx)
        centered_text(y + 40, value, font_value, color, cx=cx)

    box_x0, box_y0 = 120, 950
    box_x1, box_y1 = width - 120, 1550
    box_w, box_h = box_x1 - box_x0, box_y1 - box_y0

    if len(route) > 1:
        lats = [p[0] for p in route]
        lngs = [p[1] for p in route]
        min_lat, max_lat = min(lats), max(lats)
        min_lng, max_lng = min(lngs), max(lngs)
        lat_range = (max_lat - min_lat) or 1e-6
        lng_range = (max_lng - min_lng) or 1e-6

        padding = 40
        avail_w, avail_h = box_w - 2 * padding, box_h - 2 * padding
        scale = min(avail_w / lng_range, avail_h / lat_range)

        drawn_w, drawn_h = lng_range * scale, lat_range * scale
        offset_x = box_x0 + padding + (avail_w - drawn_w) / 2
        offset_y = box_y0 + padding + (avail_h - drawn_h) / 2

        points = [
            (offset_x + (lng - min_lng) * scale, offset_y + (max_lat - lat) * scale)
            for lat, lng in route
        ]

        draw.line(points, fill=(252, 76, 2, 255), width=8, joint="curve")

        r = 14
        draw.ellipse([points[0][0]-r, points[0][1]-r, points[0][0]+r, points[0][1]+r], fill=(72, 187, 120, 255))
        draw.ellipse([points[-1][0]-r, points[-1][1]-r, points[-1][0]+r, points[-1][1]+r], fill=(252, 76, 2, 255))
    else:
        centered_text((box_y0 + box_y1) // 2, "No GPS data available", font_footer, (120, 120, 125, 255))

    centered_text(height - 130, f"{ride_type_info['icon']}  FIT READER", font_footer, (252, 76, 2, 255))

    buffer = BytesIO()
    img.save(buffer, format='PNG')
    buffer.seek(0)
    return send_file(buffer, mimetype='image/png', as_attachment=True, download_name=f"ride_story_{ride_id}.png")

@app.route('/ride-summary/<int:ride_id>')
@login_required
def ride_summary(ride_id):
    rides = get_user_rides(session['user']['username'])
    ride = next((r for r in rides if r.get('id') == ride_id), None)
    if not ride:
        return "Ride not found", 404
    ride_type_info = next((t for t in RIDE_TYPES_INFO if t['id'] == ride.get('ride_type', 'cycling')), RIDE_TYPES_INFO[0])
    return render_template('ride_summary.html',
                           ride=ride,
                           ride_type=ride_type_info,
                           route=ride.get('route', []),
                           summary=ride.get('summary', {}),
                           streams=ride.get('streams', {}),
                           zone_distribution=ride.get('zone_distribution', []),
                           user=session['user'])

@app.route('/test-db')
def test_db():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT version()")
        version = cur.fetchone()
        cur.close()
        conn.close()
        return jsonify({"status": "✅ Connected!", "version": version[0]})
    except Exception as e:
        return jsonify({"status": "❌ Failed", "error": str(e)}), 500

@app.route('/db-status')
def db_status():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY table_name")
        tables = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify({"status": "✅ OK", "tables": [t[0] for t in tables]})
    except Exception as e:
        return jsonify({"status": "❌ Error", "error": str(e)}), 500

@app.route('/health')
def health():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.close()
        conn.close()
        return jsonify({"status": "healthy"})
    except:
        return jsonify({"status": "unhealthy"}), 500

if __name__ == '__main__':
    app.run(debug=True)
