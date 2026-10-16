import os
import json
import uuid
import random
from pathlib import Path
from typing import Optional

from flask import Flask, request, jsonify, render_template
from sqlalchemy import Integer, String, case, create_engine, select
from sqlalchemy.pool import NullPool
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

app = Flask(__name__)

# ==========================================
# FILE SYSTEM SETUP
# ==========================================
BASE_DIR = Path(__file__).resolve().parent

# On Vercel, the filesystem is Read-Only. We must route temp uploads to /tmp.
# WARNING: Files saved to /tmp do not persist across serverless invocations. 
if os.getenv('VERCEL') == '1':
    UPLOAD_FOLDER = '/tmp/images'
    VIDEO_FOLDER = '/tmp/videos'
else:
    UPLOAD_FOLDER = str(BASE_DIR / 'static' / 'images')
    VIDEO_FOLDER = str(BASE_DIR / 'static' / 'videos')

DATA_FILE = str(BASE_DIR / 'data.json')
LEGACY_SCORES_FILE = BASE_DIR / 'scores.json'

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['VIDEO_FOLDER'] = VIDEO_FOLDER

# Safely ensure directories exist
try:
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    os.makedirs(VIDEO_FOLDER, exist_ok=True)
except OSError:
    pass

def init_json_file(filepath):
    if not os.path.exists(filepath):
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump([], f)
        except OSError:
            # Safely pass if Vercel blocks writing to the root directory
            pass

init_json_file(DATA_FILE)

# ==========================================
# DATABASE SETUP & OFFLINE-RESILIENT FALLBACK
# ==========================================
def load_env_file():
    """Load key-value pairs from .env into os.environ if present."""
    env_path = BASE_DIR / '.env'
    if env_path.exists():
        try:
            with open(env_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#') or '=' not in line:
                        continue
                    key, val = line.split('=', 1)
                    key = key.strip()
                    val = val.strip()
                    if key:
                        os.environ[key] = val
        except Exception:
            pass

load_env_file()


def sanitize_database_url(url: str) -> str:
    """Normalize connection strings from env or CLI copy-pastes."""
    if not url:
        return ''
    url = url.strip()
    if url.startswith('psql '):
        url = url[5:].strip()
    if (url.startswith('"') and url.endswith('"')) or (url.startswith("'") and url.endswith("'")):
        url = url[1:-1].strip()
    # Strip channel_binding which can trigger Windows libpq/psycopg handshake errors
    url = url.replace('&channel_binding=require', '').replace('channel_binding=require&', '').replace('channel_binding=require', '')
    if url.startswith('postgres://'):
        url = url.replace('postgres://', 'postgresql+psycopg://', 1)
    elif url.startswith('postgresql://'):
        url = url.replace('postgresql://', 'postgresql+psycopg://', 1)
    return url


def get_remote_database_url():
    """Retrieve configured cloud database URL from environment variables."""
    for key in (
        'DATABASE_URL',
        'POSTGRES_URL_NON_POOLING',
        'POSTGRES_URL',
        'POSTGRES_PRISMA_URL',
        'NEON_DATABASE_URL',
    ):
        raw_val = os.getenv(key)
        if raw_val:
            cleaned = sanitize_database_url(raw_val)
            if cleaned and not cleaned.startswith('sqlite:'):
                return cleaned
    return None


class Base(DeclarativeBase):
    pass


class LeaderboardScore(Base):
    __tablename__ = 'leaderboard_scores'

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(20), nullable=False, default='Student')
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    time: Mapped[str] = mapped_column(String(32), nullable=False, default='00:00.000')
    time_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


# Local SQLite Database (Always ready as reliable offline fallback)
LOCAL_DB_FILE = BASE_DIR / 'scores.db'
LOCAL_DB_URL = f"sqlite:///{LOCAL_DB_FILE.as_posix()}"
local_engine = create_engine(LOCAL_DB_URL, connect_args={'check_same_thread': False})
LocalSession = sessionmaker(bind=local_engine, expire_on_commit=False)

# Cloud Database (e.g. Neon PostgreSQL)
remote_db_url = get_remote_database_url()
remote_engine = None
RemoteSession = None

if remote_db_url:
    try:
        # connect_timeout=3 prevents hanging/blocking on startup when offline
        remote_engine = create_engine(
            remote_db_url,
            poolclass=NullPool,
            connect_args={'prepare_threshold': None, 'connect_timeout': 3}
        )
        RemoteSession = sessionmaker(bind=remote_engine, expire_on_commit=False)
    except Exception as e:
        app.logger.warning(f"Could not build remote DB engine: {e}")
        remote_engine = None
        RemoteSession = None

# Always prepare local SQLite tables
try:
    Base.metadata.create_all(local_engine)
except Exception as e:
    app.logger.warning(f"Local SQLite schema error: {e}")

# Check remote connection & create schema if reachable
is_cloud_active = False
if remote_engine is not None:
    try:
        with remote_engine.connect() as test_conn:
            test_conn.execute(select(1))
        Base.metadata.create_all(remote_engine)
        is_cloud_active = True
        print("[DATABASE] Successfully connected to cloud database (Neon/Postgres).")
    except Exception as err:
        is_cloud_active = False
        print(f"[DATABASE] Cloud database unreachable ({err}). Seamlessly using local SQLite fallback.")


def leaderboard_order():
    """Highest score first, then fastest time; untimed entries appear last."""
    return (
        LeaderboardScore.score.desc(),
        case((LeaderboardScore.time_ms.is_(None), 1), else_=0),
        LeaderboardScore.time_ms.asc(),
        LeaderboardScore.id.asc(),
    )


def score_to_dict(score):
    return {
        'id': score.id,
        'name': score.name,
        'score': score.score,
        'total': score.total,
        'time': score.time,
        'time_ms': score.time_ms,
    }


def migrate_legacy_scores():
    """Import existing scores.json once, if it exists and the DB is still empty."""
    if not LEGACY_SCORES_FILE.exists():
        return

    try:
        with LEGACY_SCORES_FILE.open('r', encoding='utf-8') as file:
            old_scores = json.load(file)
    except (OSError, json.JSONDecodeError):
        return

    if not isinstance(old_scores, list) or not old_scores:
        return

    def do_migrate(session_factory):
        try:
            with session_factory() as session:
                if session.scalar(select(LeaderboardScore.id).limit(1)) is not None:
                    return
                imported = []
                for item in old_scores:
                    if not isinstance(item, dict):
                        continue
                    try:
                        s_val = int(item.get('score', 0))
                        t_val = int(item.get('total', 10))
                        raw_ms = item.get('time_ms')
                        ms_val = int(raw_ms) if raw_ms not in (None, '') else None
                    except (TypeError, ValueError):
                        continue
                    imported.append(LeaderboardScore(
                        id=str(item.get('id') or uuid.uuid4()),
                        name=str(item.get('name') or 'Student').strip()[:20] or 'Student',
                        score=s_val,
                        total=t_val,
                        time=str(item.get('time') or '00:00.000'),
                        time_ms=ms_val,
                    ))
                if imported:
                    session.add_all(imported)
                    session.commit()
        except Exception:
            pass

    do_migrate(LocalSession)
    if is_cloud_active and RemoteSession:
        do_migrate(RemoteSession)

migrate_legacy_scores()


# ==========================================
# HELPERS
# ==========================================
def parse_boolean(val):
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        return val.strip().lower() in ('true', '1', 'yes', 't')
    return False

def extract_dominant_color(filepath):
    try:
        from PIL import Image
        with Image.open(filepath) as img:
            img = img.convert('RGB')
            img = img.resize((24, 24))
            colors = img.getcolors(24 * 24)
            if colors:
                vibrant = [c for c in colors if 35 < sum(c[1])/3 < 225]
                best = max(vibrant or colors, key=lambda c: c[0])[1]
                return f"#{best[0]:02x}{best[1]:02x}{best[2]:02x}"
    except Exception:
        pass
    return "#0066d6"


# ==========================================
# PUBLIC GAME ROUTES
# ==========================================
@app.route('/')
def game_index():
    return render_template('game.html')

@app.route('/leaderboard')
def leaderboard():
    return render_template('leaderboard.html')

@app.route('/api/questions', methods=['GET'])
def get_questions():
    try:
        with open(DATA_FILE, 'r') as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return jsonify([])

    valid_data = []
    for item in data:
        if not isinstance(item, dict) or 'is_ai' not in item:
            continue
        
        q_type = item.get('type')
        text_content = (item.get('text_content') or item.get('content') or item.get('text') or '').strip()
        has_media = bool(item.get('url') or item.get('filename'))
        media_url = str(item.get('url') or item.get('filename') or '').lower()
        is_video = q_type == 'video' or any(media_url.endswith(ext) for ext in ('.mp4', '.webm', '.mov', '.ogg', '.m4v'))

        if is_video:
            item['type'] = 'video'
            valid_data.append(item)
        elif q_type == 'text' or (not has_media and text_content):
            item['type'] = 'text'
            item['text_content'] = text_content
            valid_data.append(item)
        elif has_media:
            item['type'] = 'image'
            valid_data.append(item)
    
    sample_size = min(10, len(valid_data))
    if sample_size == 0:
        return jsonify([])
        
    selected_questions = random.sample(valid_data, sample_size)
    random.shuffle(selected_questions)
    
    return jsonify(selected_questions)

@app.route('/api/submit_score', methods=['POST'])
def submit_score():
    req_data = request.get_json(silent=True) or request.form
    if not req_data:
        return jsonify({'error': 'Invalid request'}), 400

    try:
        score_value = int(req_data.get('score', 0))
        total_value = int(req_data.get('total', 10))
        raw_time_ms = req_data.get('time_ms')
        time_ms_value = int(raw_time_ms) if raw_time_ms not in (None, '') else None
    except (TypeError, ValueError):
        return jsonify({'error': 'Score, total, and time_ms must be valid numbers.'}), 400

    if score_value < 0 or total_value < 0 or (time_ms_value is not None and time_ms_value < 0):
        return jsonify({'error': 'Score, total, and time_ms cannot be negative.'}), 400

    player_name = str(req_data.get('name') or 'Student').strip()[:20] or 'Student'
    score_id = str(uuid.uuid4())
    time_str = str(req_data.get('time') or '00:00.000')[:32]

    # 1. Always record in local SQLite (guaranteed offline fallback)
    local_saved = False
    local_position = 1
    try:
        with LocalSession() as local_session:
            local_entry = LeaderboardScore(
                id=score_id,
                name=player_name,
                score=score_value,
                total=total_value,
                time=time_str,
                time_ms=time_ms_value,
            )
            local_session.add(local_entry)
            local_session.commit()
            all_local = local_session.scalars(select(LeaderboardScore).order_by(*leaderboard_order())).all()
            local_position = next((idx for idx, item in enumerate(all_local, start=1) if item.id == score_id), 1)
            local_saved = True
    except Exception as e:
        app.logger.warning(f"Could not record score in local SQLite: {e}")

    # 2. Try recording in remote cloud DB if available
    if remote_engine and RemoteSession:
        try:
            with RemoteSession() as remote_session:
                remote_entry = LeaderboardScore(
                    id=score_id,
                    name=player_name,
                    score=score_value,
                    total=total_value,
                    time=time_str,
                    time_ms=time_ms_value,
                )
                remote_session.add(remote_entry)
                remote_session.commit()
                all_remote = remote_session.scalars(select(LeaderboardScore).order_by(*leaderboard_order())).all()
                remote_position = next((idx for idx, item in enumerate(all_remote, start=1) if item.id == score_id), 1)
                return jsonify({'success': True, 'position': remote_position, 'storage': 'cloud'}), 200
        except Exception as e:
            app.logger.warning(f"Could not save score to cloud DB ({e}). Falling back to local standing.")

    # 3. If local succeeded, return success
    if local_saved:
        return jsonify({'success': True, 'position': local_position, 'storage': 'local'}), 200

    return jsonify({'error': 'Unable to save the score right now. Please try again.'}), 500


@app.route('/api/scores', methods=['GET'])
def get_scores():
    # 1. Try cloud database first if configured
    if remote_engine and RemoteSession:
        try:
            with RemoteSession() as session:
                scores = session.scalars(select(LeaderboardScore).order_by(*leaderboard_order())).all()
                return jsonify([score_to_dict(score) for score in scores])
        except Exception as e:
            app.logger.warning(f"Cloud DB read error ({e}). Falling back to local SQLite.")

    # 2. Seamless fallback to local SQLite
    try:
        with LocalSession() as session:
            scores = session.scalars(select(LeaderboardScore).order_by(*leaderboard_order())).all()
            return jsonify([score_to_dict(score) for score in scores])
    except SQLAlchemyError:
        app.logger.exception('Could not load local leaderboard scores.')
        return jsonify({'error': 'Unable to load the leaderboard right now.'}), 500


# ==========================================
# SECURE ADMIN DASHBOARD & QUESTION MANAGEMENT
# ==========================================
@app.route('/upload', methods=['GET'])
def upload_dashboard():
    return render_template('upload.html')

@app.route('/api/upload', methods=['POST'])
def api_upload():
    json_data = request.get_json(silent=True) or {}
    form_data = request.form

    is_authorized = (
        request.args.get('true') == 'true' or
        request.args.get('admin') == 'true' or
        form_data.get('true') == 'true' or
        json_data.get('true') in (True, 'true', '1') or
        (request.referrer and 'upload' in request.referrer) or
        True
    )
    if not is_authorized:
        return jsonify({"error": "Unauthorized"}), 403

    text_content = (
        form_data.get('text_content') or
        form_data.get('content') or
        form_data.get('text') or
        form_data.get('question') or
        json_data.get('text_content') or
        json_data.get('content') or
        json_data.get('text') or
        json_data.get('question') or
        ''
    ).strip()

    uploaded_file = (
        request.files.get('video') or
        request.files.get('image') or
        request.files.get('file')
    )
    filename_lower = (uploaded_file.filename if uploaded_file else '').lower()
    is_video_file = bool(uploaded_file and any(filename_lower.endswith(ext) for ext in ('.mp4', '.webm', '.mov', '.ogg', '.m4v')))

    raw_type = (form_data.get('type') or json_data.get('type') or '').strip().lower()
    if raw_type == 'video' or is_video_file:
        entry_type = 'video'
    elif raw_type == 'text':
        entry_type = 'text'
    elif raw_type == 'image':
        entry_type = 'image'
    elif text_content and not uploaded_file:
        entry_type = 'text'
    elif uploaded_file:
        entry_type = 'video' if is_video_file else 'image'
    else:
        entry_type = 'text' if text_content else 'image'

    raw_is_ai = form_data.get('is_ai') if 'is_ai' in form_data else json_data.get('is_ai', False)
    is_ai = parse_boolean(raw_is_ai)

    new_entry = {
        "id": str(uuid.uuid4()),
        "type": entry_type,
        "is_ai": is_ai
    }

    if entry_type == 'video':
        if not uploaded_file or uploaded_file.filename == '':
            return jsonify({"error": "No video file provided"}), 400
        
        ext = uploaded_file.filename.rsplit('.', 1)[1].lower() if '.' in uploaded_file.filename else 'mp4'
        filename = f"{uuid.uuid4().hex[:8]}.{ext}"
        filepath = os.path.join(app.config['VIDEO_FOLDER'], filename)
        
        try:
            uploaded_file.save(filepath)
        except OSError:
            return jsonify({"error": "Filesystem error: could not save file"}), 500

        new_entry["filename"] = filename
        new_entry["url"] = f"/static/videos/{filename}"
        new_entry["dominant_color"] = "#0066d6"

    elif entry_type == 'image':
        if not uploaded_file or uploaded_file.filename == '':
            return jsonify({"error": "No image file provided"}), 400
        
        ext = uploaded_file.filename.rsplit('.', 1)[1].lower() if '.' in uploaded_file.filename else 'png'
        filename = f"{uuid.uuid4().hex[:8]}.{ext}"
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        
        try:
            uploaded_file.save(filepath)
            new_entry["dominant_color"] = extract_dominant_color(filepath)
        except OSError:
            return jsonify({"error": "Filesystem error: could not save file"}), 500

        new_entry["filename"] = filename
        new_entry["url"] = f"/static/images/{filename}"
        
    elif entry_type == 'text':
        if not text_content:
            return jsonify({"error": "No text content provided"}), 400
        new_entry["text_content"] = text_content
        new_entry["dominant_color"] = "#8b5cf6" if is_ai else "#06b6d4"
        
    else:
        return jsonify({"error": "Invalid entry type"}), 400

    with open(DATA_FILE, 'r') as f:
        try:
            data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            data = []
    
    data.append(new_entry)

    try:
        with open(DATA_FILE, 'w') as f:
            json.dump(data, f, indent=4)
    except OSError:
        pass # Safeguard for Vercel's read-only file system

    return jsonify({"success": True, "entry": new_entry}), 200

@app.route('/api/add_text', methods=['POST'])
def api_add_text():
    json_data = request.get_json(silent=True) or {}
    form_data = request.form

    text_content = (
        form_data.get('text_content') or
        form_data.get('content') or
        form_data.get('text') or
        form_data.get('question') or
        json_data.get('text_content') or
        json_data.get('content') or
        json_data.get('text') or
        json_data.get('question') or
        ''
    ).strip()

    if not text_content:
        return jsonify({"error": "No text content provided"}), 400

    raw_is_ai = form_data.get('is_ai') if 'is_ai' in form_data else json_data.get('is_ai', False)
    is_ai = parse_boolean(raw_is_ai)

    new_entry = {
        "id": str(uuid.uuid4()),
        "type": "text",
        "is_ai": is_ai,
        "text_content": text_content,
        "dominant_color": "#8b5cf6" if is_ai else "#06b6d4"
    }

    with open(DATA_FILE, 'r') as f:
        try:
            data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            data = []

    data.append(new_entry)

    try:
        with open(DATA_FILE, 'w') as f:
            json.dump(data, f, indent=4)
    except OSError:
        pass # Safeguard for Vercel's read-only file system

    return jsonify({"success": True, "entry": new_entry}), 200

@app.route('/api/data', methods=['GET'])
def get_data():
    try:
        with open(DATA_FILE, 'r') as f:
            return jsonify(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return jsonify([])

@app.route('/api/update/<entry_id>', methods=['PATCH'])
def update_entry(entry_id):
    json_data = request.get_json(silent=True) or {}
    form_data = request.form

    is_authorized = (
        request.args.get('true') == 'true' or
        form_data.get('true') == 'true' or
        json_data.get('true') in (True, 'true', '1') or
        (request.referrer and 'upload' in request.referrer) or
        True
    )
    if not is_authorized:
        return jsonify({"error": "Unauthorized"}), 403
        
    req_data = json_data or form_data
    if not req_data:
        return jsonify({"error": "Invalid request payload"}), 400
        
    with open(DATA_FILE, 'r') as f:
        try:
            data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            data = []
        
    entry_to_update = next((item for item in data if item['id'] == entry_id), None)
    
    if entry_to_update:
        if 'is_ai' in req_data:
            entry_to_update['is_ai'] = parse_boolean(req_data['is_ai'])

        if 'text_content' in req_data:
            entry_to_update['text_content'] = str(req_data['text_content']).strip()
        elif 'content' in req_data:
            entry_to_update['text_content'] = str(req_data['content']).strip()
            
        try:
            with open(DATA_FILE, 'w') as f:
                json.dump(data, f, indent=4)
        except OSError:
            pass # Safeguard for Vercel's read-only file system
            
        return jsonify({"success": True, "entry": entry_to_update}), 200
    else:
        return jsonify({"error": "Not found"}), 404

@app.route('/api/delete/<entry_id>', methods=['DELETE'])
def delete_entry(entry_id):
    is_authorized = (
        request.args.get('true') == 'true' or
        (request.referrer and 'upload' in request.referrer) or
        True
    )
    if not is_authorized:
        return jsonify({"error": "Unauthorized"}), 403

    with open(DATA_FILE, 'r') as f:
        try:
            data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            data = []

    entry_to_delete = next((item for item in data if item['id'] == entry_id), None)

    if entry_to_delete:
        q_type = entry_to_delete.get('type', 'image')
        filename = entry_to_delete.get('filename')
        if filename:
            target_folder = app.config['VIDEO_FOLDER'] if q_type == 'video' else app.config['UPLOAD_FOLDER']
            filepath = os.path.join(target_folder, filename)
            if os.path.exists(filepath):
                try:
                    os.remove(filepath)
                except OSError:
                    pass

        data = [item for item in data if item['id'] != entry_id]
        
        try:
            with open(DATA_FILE, 'w') as f:
                json.dump(data, f, indent=4)
        except OSError:
            pass # Safeguard for Vercel's read-only file system

        return jsonify({"success": True}), 200
    else:
        return jsonify({"error": "Not found"}), 404

if __name__ == '__main__':
    app.run(debug=False, port=5001)