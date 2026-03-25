from flask import Flask, render_template, request, jsonify, session, redirect, url_for, send_from_directory
from flask_socketio import SocketIO, emit, join_room, leave_room
import sqlite3, os, hashlib, uuid, json
from datetime import datetime
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'change-this-in-production-please')
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

UPLOAD_FOLDER = 'static/uploads'
STICKER_FOLDER = 'static/stickers'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB

DB_PATH = 'messenger.db'

# ─── DATABASE ─────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                avatar TEXT DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS rooms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                type TEXT DEFAULT 'group',
                created_by INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS room_members (
                room_id INTEGER,
                user_id INTEGER,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (room_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                content TEXT,
                type TEXT DEFAULT 'text',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS reactions (
                message_id INTEGER,
                user_id INTEGER,
                emoji TEXT,
                PRIMARY KEY (message_id, user_id)
            );
        ''')
        # Seed default rooms
        existing = db.execute('SELECT COUNT(*) as c FROM rooms').fetchone()['c']
        if existing == 0:
            db.execute("INSERT INTO rooms (name, type, created_by) VALUES ('General', 'group', 1)")
            db.execute("INSERT INTO rooms (name, type, created_by) VALUES ('Random', 'group', 1)")
            db.execute("INSERT INTO rooms (name, type, created_by) VALUES ('Dev Talk', 'group', 1)")
            db.commit()

def hash_password(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

init_db()

# ─── ROUTES ───────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    return render_template('chat.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        data = request.get_json()
        username = data.get('username', '').strip()
        password = data.get('password', '')
        if not username or not password:
            return jsonify({'error': 'Thiếu thông tin'}), 400
        with get_db() as db:
            user = db.execute('SELECT * FROM users WHERE username=? AND password=?',
                              (username, hash_password(password))).fetchone()
        if not user:
            return jsonify({'error': 'Sai tên đăng nhập hoặc mật khẩu'}), 401
        session['user_id'] = user['id']
        session['username'] = user['username']
        return jsonify({'ok': True})
    return render_template('auth.html', mode='login')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        data = request.get_json()
        username = data.get('username', '').strip()
        password = data.get('password', '')
        if not username or not password:
            return jsonify({'error': 'Thiếu thông tin'}), 400
        if len(username) < 3:
            return jsonify({'error': 'Tên ít nhất 3 ký tự'}), 400
        if len(password) < 6:
            return jsonify({'error': 'Mật khẩu ít nhất 6 ký tự'}), 400
        try:
            with get_db() as db:
                db.execute('INSERT INTO users (username, password) VALUES (?, ?)',
                           (username, hash_password(password)))
                db.commit()
                user = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
                # Add to all default rooms
                rooms = db.execute('SELECT id FROM rooms').fetchall()
                for r in rooms:
                    db.execute('INSERT OR IGNORE INTO room_members (room_id, user_id) VALUES (?,?)',
                               (r['id'], user['id']))
                db.commit()
            session['user_id'] = user['id']
            session['username'] = username
            return jsonify({'ok': True})
        except sqlite3.IntegrityError:
            return jsonify({'error': 'Tên đã tồn tại'}), 409
    return render_template('auth.html', mode='register')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/api/me')
def me():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    return jsonify({'id': session['user_id'], 'username': session['username']})

@app.route('/api/rooms')
def get_rooms():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    with get_db() as db:
        rooms = db.execute('''
            SELECT r.id, r.name, r.type,
                   (SELECT content FROM messages WHERE room_id=r.id ORDER BY created_at DESC LIMIT 1) as last_msg,
                   (SELECT created_at FROM messages WHERE room_id=r.id ORDER BY created_at DESC LIMIT 1) as last_time
            FROM rooms r
            JOIN room_members rm ON rm.room_id = r.id
            WHERE rm.user_id = ?
            ORDER BY last_time DESC NULLS LAST
        ''', (session['user_id'],)).fetchall()
    return jsonify([dict(r) for r in rooms])

@app.route('/api/rooms', methods=['POST'])
def create_room():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json()
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Tên phòng không được để trống'}), 400
    with get_db() as db:
        cur = db.execute('INSERT INTO rooms (name, type, created_by) VALUES (?, ?, ?)',
                         (name, 'group', session['user_id']))
        room_id = cur.lastrowid
        db.execute('INSERT INTO room_members (room_id, user_id) VALUES (?, ?)',
                   (room_id, session['user_id']))
        db.commit()
    return jsonify({'id': room_id, 'name': name})

@app.route('/api/rooms/<int:room_id>/messages')
def get_messages(room_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    with get_db() as db:
        msgs = db.execute('''
            SELECT m.id, m.content, m.type, m.created_at,
                   u.username, u.id as user_id,
                   (SELECT json_group_array(json_object('emoji', r.emoji, 'user_id', r.user_id))
                    FROM reactions r WHERE r.message_id = m.id) as reactions
            FROM messages m
            JOIN users u ON u.id = m.user_id
            WHERE m.room_id = ?
            ORDER BY m.created_at ASC
            LIMIT 100
        ''', (room_id,)).fetchall()
        # Join room if not member
        db.execute('INSERT OR IGNORE INTO room_members (room_id, user_id) VALUES (?, ?)',
                   (room_id, session['user_id']))
        db.commit()
    return jsonify([dict(m) for m in msgs])

@app.route('/api/upload', methods=['POST'])
def upload_file():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400
    file = request.files['file']
    if file and allowed_file(file.filename):
        ext = file.filename.rsplit('.', 1)[1].lower()
        filename = f"{uuid.uuid4().hex}.{ext}"
        os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
        file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
        return jsonify({'url': f'/static/uploads/{filename}'})
    return jsonify({'error': 'File không hợp lệ'}), 400

@app.route('/api/users')
def get_users():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    with get_db() as db:
        users = db.execute('SELECT id, username FROM users WHERE id != ? ORDER BY username', (session['user_id'],)).fetchall()
    return jsonify([dict(u) for u in users])

@app.route('/api/dm/<int:other_id>')
def get_or_create_dm(other_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    me = session['user_id']
    with get_db() as db:
        # Find existing DM room between these 2 users
        existing = db.execute('''
            SELECT r.id, r.name FROM rooms r
            WHERE r.type = 'dm'
            AND r.id IN (SELECT room_id FROM room_members WHERE user_id = ?)
            AND r.id IN (SELECT room_id FROM room_members WHERE user_id = ?)
        ''', (me, other_id)).fetchone()
        if existing:
            return jsonify({'id': existing['id'], 'name': existing['name'], 'type': 'dm'})
        # Create new DM room
        other = db.execute('SELECT username FROM users WHERE id = ?', (other_id,)).fetchone()
        my = db.execute('SELECT username FROM users WHERE id = ?', (me,)).fetchone()
        room_name = f"{my['username']},{other['username']}"
        cur = db.execute('INSERT INTO rooms (name, type, created_by) VALUES (?, ?, ?)', (room_name, 'dm', me))
        room_id = cur.lastrowid
        db.execute('INSERT INTO room_members (room_id, user_id) VALUES (?, ?)', (room_id, me))
        db.execute('INSERT INTO room_members (room_id, user_id) VALUES (?, ?)', (room_id, other_id))
        db.commit()
    return jsonify({'id': room_id, 'name': room_name, 'type': 'dm'})

@app.route('/api/stickers')
def get_stickers():
    # Return built-in emoji sticker packs
    packs = {
        'Cảm xúc': ['😀','😂','🥹','😍','🥰','😎','🤩','😭','😤','🥳','😴','🤔','😱','🤣','😊'],
        'Động vật': ['🐶','🐱','🐭','🐹','🐰','🦊','🐻','🐼','🐨','🐯','🦁','🐮','🐷','🐸','🐙'],
        'Đồ ăn': ['🍕','🍔','🌮','🍜','🍣','🍩','🍦','🧋','☕','🍺','🎂','🍎','🍓','🥑','🌽'],
        'Hoạt động': ['⚽','🏀','🎮','🎵','🎬','🏖️','✈️','🚀','💎','🎁','🔥','💯','✨','🌈','❤️']
    }
    return jsonify(packs)

# ─── SOCKET EVENTS ────────────────────────────────────────────────────────────

online_users = {}

@socketio.on('connect')
def on_connect():
    if 'user_id' in session:
        online_users[session['user_id']] = session['username']
        emit('online_users', list(online_users.values()), broadcast=True)

@socketio.on('disconnect')
def on_disconnect():
    if 'user_id' in session:
        online_users.pop(session['user_id'], None)
        emit('online_users', list(online_users.values()), broadcast=True)

@socketio.on('join_room')
def on_join(data):
    room = str(data['room_id'])
    join_room(room)

@socketio.on('leave_room')
def on_leave(data):
    room = str(data['room_id'])
    leave_room(room)

@socketio.on('send_message')
def on_message(data):
    if 'user_id' not in session:
        return
    room_id = data.get('room_id')
    content = data.get('content', '').strip()
    msg_type = data.get('type', 'text')
    if not content and msg_type == 'text':
        return
    with get_db() as db:
        cur = db.execute('INSERT INTO messages (room_id, user_id, content, type) VALUES (?, ?, ?, ?)',
                         (room_id, session['user_id'], content, msg_type))
        msg_id = cur.lastrowid
        db.commit()
    payload = {
        'id': msg_id,
        'room_id': room_id,
        'content': content,
        'type': msg_type,
        'username': session['username'],
        'user_id': session['user_id'],
        'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'reactions': '[]'
    }
    emit('new_message', payload, room=str(room_id))

@socketio.on('react_message')
def on_react(data):
    if 'user_id' not in session:
        return
    msg_id = data['message_id']
    emoji = data['emoji']
    room_id = data['room_id']
    with get_db() as db:
        existing = db.execute('SELECT emoji FROM reactions WHERE message_id=? AND user_id=?',
                              (msg_id, session['user_id'])).fetchone()
        if existing and existing['emoji'] == emoji:
            db.execute('DELETE FROM reactions WHERE message_id=? AND user_id=?',
                       (msg_id, session['user_id']))
        else:
            db.execute('INSERT OR REPLACE INTO reactions (message_id, user_id, emoji) VALUES (?,?,?)',
                       (msg_id, session['user_id'], emoji))
        db.commit()
        reactions = db.execute('SELECT emoji, user_id FROM reactions WHERE message_id=?',
                               (msg_id,)).fetchall()
    emit('reaction_update', {
        'message_id': msg_id,
        'reactions': [dict(r) for r in reactions]
    }, room=str(room_id))

@socketio.on('typing')
def on_typing(data):
    room_id = str(data.get('room_id'))
    emit('user_typing', {'username': session.get('username'), 'typing': data.get('typing')},
         room=room_id, include_self=False)

if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)
