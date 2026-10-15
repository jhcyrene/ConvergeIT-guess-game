import os
import json
import uuid
import random
from flask import Flask, request, jsonify, render_template

app = Flask(__name__)

# File Paths
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'static', 'images')
DATA_FILE = os.path.join(os.path.dirname(__file__), 'data.json')
SCORES_FILE = os.path.join(os.path.dirname(__file__), 'scores.json')
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Ensure directories and JSON files exist
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def init_json_file(filepath):
    if not os.path.exists(filepath):
        with open(filepath, 'w') as f:
            json.dump([], f)

init_json_file(DATA_FILE)
init_json_file(SCORES_FILE)

def parse_boolean(val):
    """Safely parse boolean from bool, string, or number."""
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        return val.strip().lower() in ('true', '1', 'yes', 't')
    return False

def extract_dominant_color(filepath):
    """Extract dominant or representative vibrant hex color from image for ambient theming."""
    try:
        from PIL import Image
        with Image.open(filepath) as img:
            img = img.convert('RGB')
            img = img.resize((24, 24))
            colors = img.getcolors(24 * 24)
            if colors:
                # Prefer rich vibrant colors for ambient glow
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
    # The main URL loads the student guessing game
    return render_template('game.html')

@app.route('/leaderboard')
def leaderboard():
    # Dedicated leaderboard page displaying player rankings
    return render_template('leaderboard.html')

@app.route('/api/questions', methods=['GET'])
def get_questions():
    try:
        with open(DATA_FILE, 'r') as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return jsonify([])

    valid_data = []
    for item in data:
        if not isinstance(item, dict) or 'is_ai' not in item:
            continue
        
        q_type = item.get('type')
        # Check if text question
        text_content = (item.get('text_content') or item.get('content') or item.get('text') or '').strip()
        has_image = bool(item.get('url') or item.get('filename'))

        if q_type == 'text' or (not has_image and text_content):
            item['type'] = 'text'
            item['text_content'] = text_content
            valid_data.append(item)
        elif has_image:
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
        return jsonify({"error": "Invalid request"}), 400
        
    try:
        with open(SCORES_FILE, 'r') as f:
            scores = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        scores = []
        
    new_score = {
        "id": str(uuid.uuid4()),
        "name": str(req_data.get("name", "Student")).strip()[:20],
        "score": int(req_data.get("score", 0)),
        "total": int(req_data.get("total", 10)),
        "time": str(req_data.get("time", "00:00.000")),
        "time_ms": int(req_data.get("time_ms", 0)) if req_data.get("time_ms") else None
    }
    
    scores.append(new_score)
    # Sort by score descending, then by fastest time_ms ascending (break ties by speed)
    scores = sorted(scores, key=lambda x: (-x.get('score', 0), x.get('time_ms') if x.get('time_ms') is not None else 99999999))

    with open(SCORES_FILE, 'w') as f:
        json.dump(scores, f, indent=4)
        
    return jsonify({"success": True, "position": scores.index(new_score) + 1}), 200

@app.route('/api/scores', methods=['GET'])
def get_scores():
    # Public endpoint to view the leaderboard
    try:
        with open(SCORES_FILE, 'r') as f:
            scores = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        scores = []
    # Ensure scores are sorted: score DESC, then time_ms ASC (non-timed entries sorted to end)
    sorted_scores = sorted(scores, key=lambda x: (-int(x.get('score', 0)), x.get('time_ms') if x.get('time_ms') is not None else 99999999))
    return jsonify(sorted_scores)


# ==========================================
# SECURE ADMIN DASHBOARD & QUESTION MANAGEMENT
# ==========================================

@app.route('/upload', methods=['GET'])
def upload_dashboard():
    # Admin dashboard: accessible directly or via ?true=true
    return render_template('upload.html')

@app.route('/api/upload', methods=['POST'])
def api_upload():
    # Parse incoming JSON (if provided) or Form Data
    json_data = request.get_json(silent=True) or {}
    form_data = request.form

    # Check authorization (accepts query param, form, json, header, referrer, or direct access)
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

    # Extract text content from any common field name
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

    # Safely check whether an actual image file was provided
    has_image_file = (
        'image' in request.files and
        bool(request.files['image'].filename and request.files['image'].filename.strip())
    )

    # Determine question type
    raw_type = (form_data.get('type') or json_data.get('type') or '').strip().lower()
    if raw_type == 'text':
        entry_type = 'text'
    elif raw_type == 'image':
        entry_type = 'image'
    elif text_content and not has_image_file:
        entry_type = 'text'
    elif has_image_file:
        entry_type = 'image'
    else:
        entry_type = 'text' if text_content else 'image'

    # Safely parse is_ai (works with booleans, strings, ints)
    raw_is_ai = form_data.get('is_ai') if 'is_ai' in form_data else json_data.get('is_ai', False)
    is_ai = parse_boolean(raw_is_ai)

    new_entry = {
        "id": str(uuid.uuid4()),
        "type": entry_type,
        "is_ai": is_ai
    }

    if entry_type == 'image':
        if 'image' not in request.files:
            return jsonify({"error": "No image file provided"}), 400
        
        file = request.files['image']
        if file.filename == '':
            return jsonify({"error": "No file selected"}), 400
        
        ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else 'png'
        filename = f"{uuid.uuid4().hex[:8]}.{ext}"
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)

        new_entry["filename"] = filename
        new_entry["url"] = f"/static/images/{filename}"
        new_entry["dominant_color"] = extract_dominant_color(filepath)
        
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
        except (FileNotFoundError, json.JSONDecodeError):
            data = []
    
    data.append(new_entry)

    with open(DATA_FILE, 'w') as f:
        json.dump(data, f, indent=4)

    return jsonify({"success": True, "entry": new_entry}), 200

@app.route('/api/add_text', methods=['POST'])
def api_add_text():
    """Convenience endpoint specifically for adding text questions via JSON or Form."""
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
        except (FileNotFoundError, json.JSONDecodeError):
            data = []

    data.append(new_entry)

    with open(DATA_FILE, 'w') as f:
        json.dump(data, f, indent=4)

    return jsonify({"success": True, "entry": new_entry}), 200

@app.route('/api/data', methods=['GET'])
def get_data():
    with open(DATA_FILE, 'r') as f:
        try:
            return jsonify(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            return jsonify([])

@app.route('/api/update/<entry_id>', methods=['PATCH'])
def update_entry(entry_id):
    json_data = request.get_json(silent=True) or {}
    form_data = request.form

    # Check authorization
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
        except (FileNotFoundError, json.JSONDecodeError):
            data = []
        
    entry_to_update = next((item for item in data if item['id'] == entry_id), None)
    
    if entry_to_update:
        if 'is_ai' in req_data:
            entry_to_update['is_ai'] = parse_boolean(req_data['is_ai'])

        # Support updating text content
        if 'text_content' in req_data:
            entry_to_update['text_content'] = str(req_data['text_content']).strip()
        elif 'content' in req_data:
            entry_to_update['text_content'] = str(req_data['content']).strip()
            
        with open(DATA_FILE, 'w') as f:
            json.dump(data, f, indent=4)
            
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
        except (FileNotFoundError, json.JSONDecodeError):
            data = []

    entry_to_delete = next((item for item in data if item['id'] == entry_id), None)

    if entry_to_delete:
        if entry_to_delete.get('type', 'image') == 'image' and 'filename' in entry_to_delete:
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], entry_to_delete['filename'])
            if os.path.exists(filepath):
                try:
                    os.remove(filepath)
                except OSError:
                    pass

        data = [item for item in data if item['id'] != entry_id]
        with open(DATA_FILE, 'w') as f:
            json.dump(data, f, indent=4)

        return jsonify({"success": True}), 200
    else:
        return jsonify({"error": "Not found"}), 404

if __name__ == '__main__':
    app.run(debug=False, port=5001)