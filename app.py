from flask import Flask, render_template, request, jsonify, redirect, url_for, session
from fitparse import FitFile
import pandas as pd
import os
import json
import requests
import hashlib
import traceback
from functools import wraps

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'cycling_dashboard_secret_2024_xk9_fallback')

import base64
from cryptography.fernet import Fernet

# ── Load from environment variables (never hardcode secrets) ──
GITHUB_CLIENT_ID     = os.environ.get('GITHUB_CLIENT_ID', 'Ov23liWJVdZ4Ks6PYBie')
GITHUB_CLIENT_SECRET = os.environ.get('GITHUB_CLIENT_SECRET', '8cf5d28c8996348ce7e2ca91bd89f0b5e7046de2')
GITHUB_AUTH_URL = 'https://github.com/login/oauth/authorize'
GITHUB_TOKEN_URL = 'https://github.com/login/oauth/access_token'
GITHUB_API_URL   = 'https://api.github.com/user'

UPLOAD_FOLDER = 'uploads'
DATA_FOLDER   = 'user_data'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(DATA_FOLDER, exist_ok=True)

# ── Encryption setup ──────────────────────────────────────────
# On first run, generates a key and saves it. On Render, set
# ENCRYPTION_KEY env var to the value from encryption.key file.
def get_encryption_key():
    key_from_env = os.environ.get('ENCRYPTION_KEY')
    if key_from_env:
        return key_from_env.encode()
    key_file = 'encryption.key'
    if os.path.exists(key_file):
        with open(key_file, 'rb') as f:
            return f.read()
    key = Fernet.generate_key()
    with open(key_file, 'wb') as f:
        f.write(key)
    print(f"[SECURITY] Generated new encryption key. Copy this to ENCRYPTION_KEY env var on Render:\n{key.decode()}")
    return key

ENCRYPTION_KEY = get_encryption_key()
fernet = Fernet(ENCRYPTION_KEY)


def encrypt_data(data: dict) -> bytes:
    """Encrypt a dict to bytes."""
    json_bytes = json.dumps(data).encode('utf-8')
    return fernet.encrypt(json_bytes)


def decrypt_data(encrypted_bytes: bytes) -> dict:
    """Decrypt bytes back to a dict."""
    json_bytes = fernet.decrypt(encrypted_bytes)
    return json.loads(json_bytes.decode('utf-8'))


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated


def get_user_file(username):
    return os.path.join(DATA_FOLDER, f'{username}.json')


def get_profile_file(username):
    return os.path.join(DATA_FOLDER, f'{username}_profile.json')


def load_user_rides(username):
    path = get_user_file(username)
    if os.path.exists(path):
        try:
            with open(path, 'rb') as f:
                raw = f.read()
            # Try decrypting first
            try:
                data = decrypt_data(raw)
                return data.get('rides', []) if isinstance(data, dict) else []
            except Exception:
                # Fallback: try reading as plain JSON (old unencrypted files)
                try:
                    data = json.loads(raw.decode('utf-8'))
                    rides = data if isinstance(data, list) else []
                    # Re-save as encrypted
                    save_user_rides(username, rides)
                    return rides
                except Exception:
                    import shutil
                    shutil.move(path, path + '.corrupted')
                    return []
        except Exception:
            return []
    return []


def save_user_rides(username, rides):
    encrypted = encrypt_data({"rides": rides})
    with open(get_user_file(username), 'wb') as f:
        f.write(encrypted)


def load_profile(username):
    path = get_profile_file(username)
    if os.path.exists(path):
        try:
            with open(path, 'rb') as f:
                raw = f.read()
            try:
                return decrypt_data(raw)
            except Exception:
                # Fallback: plain JSON (old files)
                try:
                    profile = json.loads(raw.decode('utf-8'))
                    save_profile(username, profile)
                    return profile
                except Exception:
                    return {}
        except Exception:
            return {}
    return {}


def save_profile(username, profile):
    encrypted = encrypt_data(profile)
    with open(get_profile_file(username), 'wb') as f:
        f.write(encrypted)


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

    # Elevation
    if 'enhanced_altitude' in df.columns:
        df['elevation'] = pd.to_numeric(df['enhanced_altitude'], errors='coerce').fillna(0)
    elif 'altitude' in df.columns:
        df['elevation'] = pd.to_numeric(df['altitude'], errors='coerce').fillna(0)
    else:
        df['elevation'] = 0.0

    # Speed
    if 'speed' in df.columns:
        df['speed_kmh'] = pd.to_numeric(df['speed'], errors='coerce').fillna(0) * 3.6
    else:
        df['speed_kmh'] = 0.0

    # Cadence
    if 'cadence' in df.columns:
        df['cadence'] = pd.to_numeric(df['cadence'], errors='coerce').fillna(0)
    else:
        df['cadence'] = 0.0

    # Temperature — BEFORE lat/lng dropna
    if 'temperature' in df.columns:
        df['temperature'] = pd.to_numeric(df['temperature'], errors='coerce').ffill().fillna(0)
        has_temperature = bool((df['temperature'] != 0).any())
    else:
        df['temperature'] = 0.0
        has_temperature = False

    # Heart rate — BEFORE lat/lng dropna
    if 'heart_rate' in df.columns:
        df['heart_rate'] = pd.to_numeric(df['heart_rate'], errors='coerce').fillna(0)
        has_hr = bool((df['heart_rate'] > 0).any())
    else:
        df['heart_rate'] = 0.0
        has_hr = False

    # GPS
    if 'position_lat' in df.columns and 'position_long' in df.columns:
        df['lat'] = pd.to_numeric(df['position_lat'], errors='coerce') * (180 / 2 ** 31)
        df['lng'] = pd.to_numeric(df['position_long'], errors='coerce') * (180 / 2 ** 31)
        route_df = df.dropna(subset=['lat', 'lng']).copy()
    else:
        df['lat'] = float('nan')
        df['lng'] = float('nan')
        route_df = pd.DataFrame(columns=['lat', 'lng'])

    # Elevation gain
    df['elevation_diff'] = df['elevation'].diff()
    total_climbing = float(df['elevation_diff'][df['elevation_diff'] > 0].sum())

    # Distance
    if 'distance' in df.columns:
        dist_series = pd.to_numeric(df['distance'], errors='coerce')
        if dist_series.max() > 0:
            total_distance = round(float(dist_series.max()) / 1000, 2)
        else:
            total_distance = round(float(df['speed_kmh'].sum()) / 3600, 2)
    else:
        total_distance = round(float(df['speed_kmh'].sum()) / 3600, 2)

    # Power
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

    # Summary stats on FULL dataset
    valid_cadence = df['cadence'][df['cadence'] > 0]
    valid_hr = df['heart_rate'][(df['heart_rate'] > 30) & (df['heart_rate'] < 220)] if has_hr else pd.Series([], dtype=float)
    valid_temp = df['temperature'][df['temperature'] != 0] if has_temperature else pd.Series([], dtype=float)

    summary = {
        "max_speed":       round(float(df['speed_kmh'].max()), 1),
        "avg_speed":       round(float(df['speed_kmh'].mean()), 1),
        "avg_cadence":     round(float(valid_cadence.mean()), 1) if len(valid_cadence) > 0 else 0,
        "elevation_gain":  round(total_climbing, 1),
        "total_calories":  total_calories,
        "avg_power":       avg_power,
        "max_power":       max_power,
        "distance_km":     total_distance,
        "avg_hr":          round(float(valid_hr.mean()), 0) if len(valid_hr) > 0 else None,
        "max_hr":          round(float(valid_hr.max()), 0) if len(valid_hr) > 0 else None,
        "avg_temp":        round(float(valid_temp.mean()), 1) if len(valid_temp) > 0 else None,
        "max_temp":        round(float(valid_temp.max()), 1) if len(valid_temp) > 0 else None,
        "has_temperature": has_temperature,
        "has_hr":          has_hr,
    }

    # Downsample to 300 points for charts
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
            "timestamps":  timestamps,
            "speed":       [round(float(x), 1) for x in df_ds['speed_kmh'].tolist()],
            "cadence":     [int(float(x)) for x in df_ds['cadence'].tolist()],
            "elevation":   [round(float(x), 1) for x in df_ds['elevation'].tolist()],
            "power":       [round(float(x), 1) for x in power_ds],
            "temperature": [round(float(x), 1) for x in df_ds['temperature'].tolist()] if has_temperature else [],
            "heart_rate":  [int(float(x)) for x in df_ds['heart_rate'].tolist()] if has_hr else [],
        },
        "zone_distribution": zone_distribution,
        "route": map_route
    }


# ── Auth ──────────────────────────────────────────────────────
@app.route('/')
def index():
    if 'user' in session:
        return redirect(url_for('dashboard'))
    return render_template('index.html')


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
    session['user'] = {
        'username': user_data.get('login'),
        'name':     user_data.get('name') or user_data.get('login'),
        'avatar':   user_data.get('avatar_url'),
        'email':    user_data.get('email')
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


# ── Profile ───────────────────────────────────────────────────
@app.route('/profile', methods=['GET'])
@login_required
def get_profile():
    return jsonify(load_profile(session['user']['username']))


@app.route('/profile', methods=['POST'])
@login_required
def update_profile():
    data = request.get_json()
    allowed = ['weight', 'bike_type', 'bike_computer', 'sensors', 'ftp', 'bike_name']
    profile = {k: data[k] for k in allowed if k in data}
    save_profile(session['user']['username'], profile)
    return jsonify({"status": "saved"})


# ── Rides ─────────────────────────────────────────────────────
@app.route('/check-duplicate', methods=['POST'])
@login_required
def check_duplicate():
    file = request.files.get('fitfile')
    if not file:
        return jsonify({"error": "No file"}), 400
    file_bytes = file.read()
    file_hash = get_file_hash(file_bytes)
    rides = load_user_rides(session['user']['username'])
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

        overwrite_id = request.form.get('overwrite_id')
        filename = file.filename
        file_bytes = file.read()
        file_hash = get_file_hash(file_bytes)

        final_path = os.path.join(UPLOAD_FOLDER, filename)
        with open(final_path, 'wb') as f:
            f.write(file_bytes)

        profile = load_profile(session['user']['username'])
        stats = parse_ride(final_path, profile)

        rides = load_user_rides(session['user']['username'])
        if overwrite_id:
            rides = [r for r in rides if str(r.get('id')) != str(overwrite_id)]

        new_id = max((r.get('id', 0) for r in rides), default=0) + 1
        rides.append({
            "id":               new_id,
            "filename":         filename,
            "file_hash":        file_hash,
            "summary":          stats["summary"],
            "streams":          stats["streams"],
            "zone_distribution":stats["zone_distribution"],
            "route":            stats["route"]
        })
        save_user_rides(session['user']['username'], rides)
        return jsonify(stats)

    except Exception as e:
        err = traceback.format_exc()
        with open('upload_error.log', 'w') as ef:
            ef.write(err)
        return jsonify({"error": f"Failed to parse file: {str(e)}",
                        "detail": err}), 500


@app.route('/my-rides')
@login_required
def my_rides():
    rides = load_user_rides(session['user']['username'])
    return jsonify([{
        "id":       r.get("id"),
        "filename": r.get("filename"),
        "summary":  r.get("summary")
    } for r in rides])


@app.route('/ride/<int:ride_id>')
@login_required
def get_ride(ride_id):
    rides = load_user_rides(session['user']['username'])
    ride = next((r for r in rides if r.get('id') == ride_id), None)
    if not ride:
        return jsonify({"error": "Ride not found"}), 404
    return jsonify(ride)


@app.route('/ride/<int:ride_id>', methods=['DELETE'])
@login_required
def delete_ride(ride_id):
    username = session['user']['username']
    rides = load_user_rides(username)
    ride = next((r for r in rides if r.get('id') == ride_id), None)
    if not ride:
        return jsonify({"error": "Ride not found"}), 404
    file_path = os.path.join(UPLOAD_FOLDER, ride.get('filename', ''))
    if os.path.exists(file_path):
        os.remove(file_path)
    rides = [r for r in rides if r.get('id') != ride_id]
    save_user_rides(username, rides)
    return jsonify({"status": "deleted", "id": ride_id})


@app.route('/recalculate-all', methods=['POST'])
@login_required
def recalculate_all():
    username = session['user']['username']
    profile = load_profile(username)
    rides = load_user_rides(username)
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
            ride['summary'] = stats['summary']
            ride['streams'] = stats['streams']
            ride['zone_distribution'] = stats['zone_distribution']
            ride['route'] = stats['route']
            results["updated"] += 1
        except Exception as e:
            results["skipped"] += 1
            results["errors"].append(f"Ride #{ride.get('id')}: {str(e)}")
    save_user_rides(username, rides)
    return jsonify(results)


@app.route('/club')
@login_required
def club():
    all_riders = []
    for filename in os.listdir(DATA_FOLDER):
        if filename.endswith('.json') and not filename.endswith('_profile.json'):
            username = filename.replace('.json', '')
            rides = load_user_rides(username)
            profile = load_profile(username)
            if rides:
                best_ride = max(rides, key=lambda r: r.get('summary', {}).get('avg_speed', 0))
                all_riders.append({
                    'username':       username,
                    'total_rides':    len(rides),
                    'best_avg_speed': best_ride.get('summary', {}).get('avg_speed', 0),
                    'best_max_speed': max(r.get('summary', {}).get('max_speed', 0) for r in rides),
                    'total_elevation':round(sum(r.get('summary', {}).get('elevation_gain', 0) for r in rides), 1),
                    'total_calories': round(sum(r.get('summary', {}).get('total_calories', 0) for r in rides), 0),
                    'bike_type':      profile.get('bike_type', '—')
                })
    all_riders.sort(key=lambda x: x['best_avg_speed'], reverse=True)
    return jsonify(all_riders)



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

        profile = load_profile(session['user']['username'])
        weight = float(profile.get('weight', 75))

        # Build streams from recorded points
        timestamps = [p['timestamp'] for p in points]
        speeds_kmh = [float(p.get('speed_kmh', 0)) for p in points]
        hr_list = [int(p.get('heart_rate', 0)) for p in points]
        cadence_list = [int(p.get('cadence', 0)) for p in points]
        route = [[float(p['lat']), float(p['lng'])] for p in points if p.get('lat') and p.get('lng')]

        # Elevation from GPS altitude if available
        elevation_list = [float(p.get('altitude', 0)) for p in points]

        # Calculate distance from GPS points
        import math
        total_dist = 0.0
        for i in range(1, len(points)):
            p1, p2 = points[i-1], points[i]
            if p1.get('lat') and p2.get('lat'):
                lat1, lon1 = math.radians(p1['lat']), math.radians(p1['lng'])
                lat2, lon2 = math.radians(p2['lat']), math.radians(p2['lng'])
                dlat, dlon = lat2 - lat1, lon2 - lon1
                a = math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
                total_dist += 6371 * 2 * math.asin(math.sqrt(a))

        # Elevation gain
        elev_diffs = [max(0, elevation_list[i] - elevation_list[i-1]) for i in range(1, len(elevation_list))]
        total_climbing = sum(elev_diffs)

        # Estimated power from speed
        import pandas as pd
        df_temp = pd.DataFrame({'speed_kmh': speeds_kmh})
        power_list, total_calories = calculate_calories_and_power(df_temp, profile)

        avg_speed = round(sum(speeds_kmh) / len(speeds_kmh), 1) if speeds_kmh else 0
        max_speed = round(max(speeds_kmh), 1) if speeds_kmh else 0
        valid_hr = [h for h in hr_list if 30 < h < 220]
        valid_cad = [c for c in cadence_list if c > 0]
        ftp = float(profile.get('ftp', 200))
        zone_distribution = classify_power_zones(power_list, ftp)

        summary = {
            "max_speed":       max_speed,
            "avg_speed":       avg_speed,
            "avg_cadence":     round(sum(valid_cad)/len(valid_cad), 1) if valid_cad else 0,
            "elevation_gain":  round(total_climbing, 1),
            "total_calories":  total_calories,
            "avg_power":       round(sum(power_list)/len(power_list), 1),
            "max_power":       round(max(power_list), 1),
            "distance_km":     round(total_dist, 2),
            "avg_hr":          round(sum(valid_hr)/len(valid_hr), 0) if valid_hr else None,
            "max_hr":          round(max(valid_hr), 0) if valid_hr else None,
            "avg_temp":        None,
            "max_temp":        None,
            "has_temperature": False,
            "has_hr":          len(valid_hr) > 0,
        }

        file_hash = hashlib.md5(json.dumps(points).encode()).hexdigest()
        filename = f"live_ride_{file_hash[:8]}.json"

        rides = load_user_rides(session['user']['username'])
        new_id = max((r.get('id', 0) for r in rides), default=0) + 1
        rides.append({
            "id":                new_id,
            "filename":          filename,
            "file_hash":         file_hash,
            "summary":           summary,
            "streams": {
                "timestamps":  timestamps,
                "speed":       speeds_kmh,
                "cadence":     cadence_list,
                "elevation":   elevation_list,
                "power":       [round(float(p), 1) for p in power_list],
                "heart_rate":  hr_list,
                "temperature": [],
            },
            "zone_distribution": zone_distribution,
            "route":             route
        })
        save_user_rides(session['user']['username'], rides)
        return jsonify({"status": "saved", "id": new_id})

    except Exception as e:
        err = traceback.format_exc()
        return jsonify({"error": str(e), "detail": err}), 500

if __name__ == '__main__':
    app.run(debug=True)
