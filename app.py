from flask import Flask, render_template, request, jsonify, session, redirect, url_for, send_from_directory
from flask_socketio import SocketIO, emit, join_room, leave_room
import os, hashlib, uuid, json
from datetime import datetime
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'change-this-in-production-please')
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet',
                    ping_timeout=60, ping_interval=25,
                    logger=False, engineio_logger=False,
                    max_http_buffer_size=5 * 1024 * 1024,
                    allow_upgrades=True)

@app.after_request
def add_headers(response):
    if request.path.startswith('/static/uploads/'):
        # uploaded files cache 7 days
        response.headers['Cache-Control'] = 'public, max-age=604800'
    elif request.path.startswith('/static/'):
        # other static assets cache 1 year
        response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    else:
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    # Gzip hint
    response.headers['Vary'] = 'Accept-Encoding'
    return response

UPLOAD_FOLDER = 'static/uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'pdf', 'zip', 'doc', 'docx', 'xls', 'xlsx', 'txt', 'mp4', 'mp3'}
IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024  # 32MB

DATABASE_URL = os.environ.get('DATABASE_URL')

# ─── DATABASE ─────────────────────────────────────────────────────────────────

if DATABASE_URL:
    import psycopg2
    import psycopg2.extras
    from psycopg2 import pool as pg_pool

    _db_pool = pg_pool.ThreadedConnectionPool(1, 10, DATABASE_URL)

    def get_db():
        return _db_pool.getconn()

    def release_db(conn):
        _db_pool.putconn(conn)

    def dict_rows(cursor):
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]

    def dict_row(cursor):
        cols = [d[0] for d in cursor.description]
        row = cursor.fetchone()
        return dict(zip(cols, row)) if row else None

    PH = '%s'  # PostgreSQL placeholder
else:
    import sqlite3
    DB_PATH = 'messenger.db'

    def get_db():
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

    def dict_rows(cursor):
        return [dict(r) for r in cursor.fetchall()]

    def dict_row(cursor):
        r = cursor.fetchone()
        return dict(r) if r else None

    PH = '?'  # SQLite placeholder

    def release_db(conn):
        conn.close()

def init_db():
    db = get_db()
    cur = db.cursor()
    serial = 'SERIAL' if DATABASE_URL else 'INTEGER PRIMARY KEY AUTOINCREMENT'
    pk = 'SERIAL PRIMARY KEY' if DATABASE_URL else 'INTEGER PRIMARY KEY AUTOINCREMENT'

    statements = [
        f'''CREATE TABLE IF NOT EXISTS users (
            id {pk},
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            avatar TEXT DEFAULT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''',
        f'''CREATE TABLE IF NOT EXISTS rooms (
            id {pk},
            name TEXT NOT NULL,
            type TEXT DEFAULT 'group',
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''',
        '''CREATE TABLE IF NOT EXISTS room_members (
            room_id INTEGER,
            user_id INTEGER,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (room_id, user_id)
        )''',
        f'''CREATE TABLE IF NOT EXISTS messages (
            id {pk},
            room_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            content TEXT,
            type TEXT DEFAULT 'text',
            deleted INTEGER DEFAULT 0,
            pinned INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''',
        '''CREATE TABLE IF NOT EXISTS reactions (
            message_id INTEGER,
            user_id INTEGER,
            emoji TEXT,
            PRIMARY KEY (message_id, user_id)
        )''',
        '''CREATE TABLE IF NOT EXISTS read_receipts (
            room_id INTEGER,
            user_id INTEGER,
            last_read_msg_id INTEGER,
            PRIMARY KEY (room_id, user_id)
        )''',
        f'''CREATE TABLE IF NOT EXISTS friendships (
            id {pk},
            sender_id INTEGER NOT NULL,
            receiver_id INTEGER NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(sender_id, receiver_id)
        )'''
    ]
    for s in statements:
        cur.execute(s)

    # Seed default rooms
    cur.execute('SELECT COUNT(*) FROM rooms')
    count = cur.fetchone()[0]
    if count == 0:
        cur.execute("INSERT INTO rooms (name, type, created_by) VALUES ('General', 'group', 1)")
        cur.execute("INSERT INTO rooms (name, type, created_by) VALUES ('Random', 'group', 1)")
        cur.execute("INSERT INTO rooms (name, type, created_by) VALUES ('Dev Talk', 'group', 1)")
    # Indexes để query nhanh hơn
    indexes = [
        'CREATE INDEX IF NOT EXISTS idx_messages_room_id ON messages(room_id)',
        'CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(created_at)',
        'CREATE INDEX IF NOT EXISTS idx_room_members_user_id ON room_members(user_id)',
        'CREATE INDEX IF NOT EXISTS idx_reactions_message_id ON reactions(message_id)',
        'CREATE INDEX IF NOT EXISTS idx_friendships_sender ON friendships(sender_id)',
        'CREATE INDEX IF NOT EXISTS idx_friendships_receiver ON friendships(receiver_id)',
    ]
    for idx in indexes:
        try:
            cur.execute(idx)
        except: pass
    db.commit()
    cur.close()
    db.close()

def hash_password(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def is_image(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in IMAGE_EXTENSIONS

init_db()

# ─── ROUTES ───────────────────────────────────────────────────────────────────

@app.route('/ping')
def ping():
    return 'pong', 200

@app.route('/keepalive')
def keepalive():
    """Endpoint để client tự ping mỗi 4 phút, tránh Railway cold start"""
    return 'ok', 200

@app.route('/api/init')
def api_init():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    db = get_db(); cur = db.cursor()
    # Rooms
    cur.execute(f'''
        SELECT r.id, r.name, r.type,
               (SELECT content FROM messages WHERE room_id=r.id ORDER BY created_at DESC LIMIT 1) as last_msg,
               (SELECT created_at FROM messages WHERE room_id=r.id ORDER BY created_at DESC LIMIT 1) as last_time
        FROM rooms r
        JOIN room_members rm ON rm.room_id = r.id
        WHERE rm.user_id = {PH} AND r.type != 'dm'
        ORDER BY last_time DESC NULLS LAST
    ''' if DATABASE_URL else f'''
        SELECT r.id, r.name, r.type,
               (SELECT content FROM messages WHERE room_id=r.id ORDER BY created_at DESC LIMIT 1) as last_msg,
               (SELECT created_at FROM messages WHERE room_id=r.id ORDER BY created_at DESC LIMIT 1) as last_time
        FROM rooms r
        JOIN room_members rm ON rm.room_id = r.id
        WHERE rm.user_id = {PH} AND r.type != 'dm'
        ORDER BY last_time DESC
    ''', (session['user_id'],))
    rooms = dict_rows(cur)
    cur.close(); release_db(db)
    stickers = {
        'Cảm xúc': ['😀','😂','🥹','😍','🥰','😎','🤩','😭','😤','🥳','😴','🤔','😱','🤣','😊'],
        'Động vật': ['🐶','🐱','🐭','🐹','🐰','🦊','🐻','🐼','🐨','🐯','🦁','🐮','🐷','🐸','🐙'],
        'Đồ ăn': ['🍕','🍔','🌮','🍜','🍣','🍩','🍦','🧋','☕','🍺','🎂','🍎','🍓','🥑','🌽'],
        'Hoạt động': ['⚽','🏀','🎮','🎵','🎬','🏖️','✈️','🚀','💎','🎁','🔥','💯','✨','🌈','❤️']
    }
    return jsonify({
        'me': {'id': session['user_id'], 'username': session['username']},
        'rooms': rooms,
        'stickers': stickers
    })

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
        db = get_db(); cur = db.cursor()
        cur.execute(f'SELECT * FROM users WHERE username={PH} AND password={PH}',
                    (username, hash_password(password)))
        user = dict_row(cur)
        cur.close(); release_db(db)
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
            db = get_db(); cur = db.cursor()
            cur.execute(f'INSERT INTO users (username, password) VALUES ({PH},{PH})',
                        (username, hash_password(password)))
            if DATABASE_URL:
                cur.execute(f'SELECT * FROM users WHERE username={PH}', (username,))
            else:
                cur.execute(f'SELECT * FROM users WHERE username={PH}', (username,))
            user = dict_row(cur)
            rooms_cur = db.cursor()
            rooms_cur.execute('SELECT id FROM rooms')
            rooms = dict_rows(rooms_cur)
            rooms_cur.close()
            for r in rooms:
                try:
                    cur.execute(f'INSERT INTO room_members (room_id, user_id) VALUES ({PH},{PH})',
                                (r['id'], user['id']))
                except: pass
            db.commit(); cur.close(); release_db(db)
            session['user_id'] = user['id']
            session['username'] = username
            return jsonify({'ok': True})
        except Exception as e:
            if 'unique' in str(e).lower() or 'duplicate' in str(e).lower():
                return jsonify({'error': 'Tên đã tồn tại'}), 409
            return jsonify({'error': str(e)}), 500
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
    db = get_db(); cur = db.cursor()
    cur.execute(f'''
        SELECT r.id, r.name, r.type,
               (SELECT content FROM messages WHERE room_id=r.id ORDER BY created_at DESC LIMIT 1) as last_msg,
               (SELECT created_at FROM messages WHERE room_id=r.id ORDER BY created_at DESC LIMIT 1) as last_time
        FROM rooms r
        JOIN room_members rm ON rm.room_id = r.id
        WHERE rm.user_id = {PH} AND r.type != 'dm'
        ORDER BY last_time DESC NULLS LAST
    ''', (session['user_id'],))
    rooms = dict_rows(cur)
    cur.close(); release_db(db)
    return jsonify(rooms)

@app.route('/api/rooms', methods=['POST'])
def create_room():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json()
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Tên phòng không được để trống'}), 400
    db = get_db(); cur = db.cursor()
    if DATABASE_URL:
        cur.execute(f'INSERT INTO rooms (name, type, created_by) VALUES ({PH},{PH},{PH}) RETURNING id',
                    (name, 'group', session['user_id']))
        room_id = cur.fetchone()[0]
    else:
        cur.execute(f'INSERT INTO rooms (name, type, created_by) VALUES ({PH},{PH},{PH})',
                    (name, 'group', session['user_id']))
        room_id = cur.lastrowid
    cur.execute(f'INSERT INTO room_members (room_id, user_id) VALUES ({PH},{PH})',
                (room_id, session['user_id']))
    db.commit(); cur.close(); release_db(db)
    return jsonify({'id': room_id, 'name': name})

@app.route('/api/rooms/<int:room_id>/messages')
def get_messages(room_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    db = get_db(); cur = db.cursor()
    cur.execute(f'''
        SELECT m.id, m.content, m.type, m.created_at, m.deleted, m.pinned,
               u.username, u.id as user_id,
               (SELECT json_agg(json_build_object('emoji', r.emoji, 'user_id', r.user_id))
                FROM reactions r WHERE r.message_id = m.id) as reactions
        FROM messages m
        JOIN users u ON u.id = m.user_id
        WHERE m.room_id = {PH}
        ORDER BY m.created_at ASC
        LIMIT 50
    ''' if DATABASE_URL else f'''
        SELECT m.id, m.content, m.type, m.created_at, m.deleted, m.pinned,
               u.username, u.id as user_id,
               (SELECT json_group_array(json_object('emoji', r.emoji, 'user_id', r.user_id))
                FROM reactions r WHERE r.message_id = m.id) as reactions
        FROM messages m
        JOIN users u ON u.id = m.user_id
        WHERE m.room_id = {PH}
        ORDER BY m.created_at ASC
        LIMIT 50
    ''', (room_id,))
    msgs = dict_rows(cur)
    # Join room if not member
    try:
        cur.execute(f'INSERT INTO room_members (room_id, user_id) VALUES ({PH},{PH})',
                    (room_id, session['user_id']))
        db.commit()
    except: db.rollback() if DATABASE_URL else None
    cur.close(); release_db(db)
    return jsonify(msgs)

@app.route('/api/upload', methods=['POST'])
def upload_file():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400
    file = request.files['file']
    if not file or not file.filename:
        return jsonify({'error': 'File trống'}), 400
    if not allowed_file(file.filename):
        return jsonify({'error': 'Loại file không được hỗ trợ'}), 400
    ext = file.filename.rsplit('.', 1)[1].lower()
    original_name = secure_filename(file.filename)
    filename = f"{uuid.uuid4().hex}.{ext}"
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
    file_type = 'image' if is_image(file.filename) else 'file'
    return jsonify({
        'url': f'/static/uploads/{filename}',
        'type': file_type,
        'original_name': original_name
    })

@app.route('/api/users')
def get_users():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    q = request.args.get('q', '').strip()
    db = get_db(); cur = db.cursor()
    if q:
        cur.execute(f'SELECT id, username FROM users WHERE id != {PH} AND username ILIKE {PH} ORDER BY username LIMIT 20',
                    (session['user_id'], f'%{q}%')) if DATABASE_URL else \
        cur.execute(f'SELECT id, username FROM users WHERE id != {PH} AND username LIKE {PH} ORDER BY username LIMIT 20',
                    (session['user_id'], f'%{q}%'))
    else:
        cur.execute(f'SELECT id, username FROM users WHERE id != {PH} ORDER BY username', (session['user_id'],))
    users = dict_rows(cur)
    result = []
    for u in users:
        cur.execute(f'''SELECT status, sender_id FROM friendships
            WHERE (sender_id={PH} AND receiver_id={PH}) OR (sender_id={PH} AND receiver_id={PH})''',
            (session['user_id'], u['id'], u['id'], session['user_id']))
        fs = dict_row(cur)
        status = None
        if fs:
            status = fs['status']
            if fs['status'] == 'pending' and fs['sender_id'] != session['user_id']:
                status = 'incoming'
        result.append({'id': u['id'], 'username': u['username'], 'friend_status': status})
    cur.close(); release_db(db)
    return jsonify(result)

@app.route('/api/friends')
def get_friends():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    db = get_db(); cur = db.cursor()
    cur.execute(f'''
        SELECT u.id, u.username FROM users u
        JOIN friendships f ON (f.sender_id=u.id OR f.receiver_id=u.id)
        WHERE f.status='accepted'
        AND (f.sender_id={PH} OR f.receiver_id={PH})
        AND u.id != {PH}
    ''', (session['user_id'], session['user_id'], session['user_id']))
    friends = dict_rows(cur)
    cur.close(); release_db(db)
    return jsonify(friends)

@app.route('/api/friend/request/<int:other_id>', methods=['POST'])
def send_friend_request(other_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        db = get_db(); cur = db.cursor()
        cur.execute(f'INSERT INTO friendships (sender_id, receiver_id, status) VALUES ({PH},{PH},{PH})',
                    (session['user_id'], other_id, 'pending'))
        db.commit(); cur.close(); release_db(db)
        # Thông báo realtime cho người nhận nếu đang online
        if other_id in user_sids:
            socketio.emit('friend_request', {
                'from_id': session['user_id'],
                'from_username': session['username']
            }, to=user_sids[other_id])
        return jsonify({'ok': True})
    except:
        return jsonify({'error': 'Đã gửi lời mời rồi'}), 400

@app.route('/api/friend/accept/<int:other_id>', methods=['POST'])
def accept_friend(other_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    db = get_db(); cur = db.cursor()
    cur.execute(f"UPDATE friendships SET status='accepted' WHERE sender_id={PH} AND receiver_id={PH}",
                (other_id, session['user_id']))
    db.commit(); cur.close(); release_db(db)
    return jsonify({'ok': True})

@app.route('/api/friend/decline/<int:other_id>', methods=['POST'])
def decline_friend(other_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    db = get_db(); cur = db.cursor()
    cur.execute(f'DELETE FROM friendships WHERE (sender_id={PH} AND receiver_id={PH}) OR (sender_id={PH} AND receiver_id={PH})',
                (other_id, session['user_id'], session['user_id'], other_id))
    db.commit(); cur.close(); release_db(db)
    return jsonify({'ok': True})

@app.route('/api/friend/requests')
def get_friend_requests():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    db = get_db(); cur = db.cursor()
    cur.execute(f'''SELECT u.id, u.username FROM users u
        JOIN friendships f ON f.sender_id=u.id
        WHERE f.receiver_id={PH} AND f.status='pending'
    ''', (session['user_id'],))
    reqs = dict_rows(cur)
    cur.close(); release_db(db)
    return jsonify(reqs)

@app.route('/api/dm/<int:other_id>')
def get_or_create_dm(other_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    me_id = session['user_id']
    db = get_db(); cur = db.cursor()
    cur.execute(f'''
        SELECT r.id, r.name FROM rooms r
        WHERE r.type = 'dm'
        AND r.id IN (SELECT room_id FROM room_members WHERE user_id = {PH})
        AND r.id IN (SELECT room_id FROM room_members WHERE user_id = {PH})
        AND (SELECT COUNT(*) FROM room_members WHERE room_id = r.id) = 2
    ''', (me_id, other_id))
    existing = dict_row(cur)
    if existing:
        cur.close(); release_db(db)
        return jsonify({'id': existing['id'], 'name': existing['name'], 'type': 'dm'})
    cur.execute(f'SELECT username FROM users WHERE id = {PH}', (other_id,))
    other = dict_row(cur)
    cur.execute(f'SELECT username FROM users WHERE id = {PH}', (me_id,))
    my = dict_row(cur)
    if not other or not my:
        cur.close(); release_db(db)
        return jsonify({'error': 'User not found'}), 404
    room_name = f"{my['username']},{other['username']}"
    if DATABASE_URL:
        cur.execute(f"INSERT INTO rooms (name, type, created_by) VALUES ({PH},'dm',{PH}) RETURNING id",
                    (room_name, me_id))
        room_id = cur.fetchone()[0]
    else:
        cur.execute(f"INSERT INTO rooms (name, type, created_by) VALUES ({PH},'dm',{PH})",
                    (room_name, me_id))
        room_id = cur.lastrowid
    cur.execute(f'INSERT INTO room_members (room_id, user_id) VALUES ({PH},{PH})', (room_id, me_id))
    cur.execute(f'INSERT INTO room_members (room_id, user_id) VALUES ({PH},{PH})', (room_id, other_id))
    db.commit(); cur.close(); release_db(db)
    return jsonify({'id': room_id, 'name': room_name, 'type': 'dm'})

@app.route('/api/stickers')
def get_stickers():
    packs = {
        'Cảm xúc': ['😀','😂','🥹','😍','🥰','😎','🤩','😭','😤','🥳','😴','🤔','😱','🤣','😊'],
        'Động vật': ['🐶','🐱','🐭','🐹','🐰','🦊','🐻','🐼','🐨','🐯','🦁','🐮','🐷','🐸','🐙'],
        'Đồ ăn': ['🍕','🍔','🌮','🍜','🍣','🍩','🍦','🧋','☕','🍺','🎂','🍎','🍓','🥑','🌽'],
        'Hoạt động': ['⚽','🏀','🎮','🎵','🎬','🏖️','✈️','🚀','💎','🎁','🔥','💯','✨','🌈','❤️']
    }
    return jsonify(packs)

# ─── SOCKET EVENTS ────────────────────────────────────────────────────────────

online_users = {}   # user_id -> username
user_sids = {}      # user_id -> socket id (để gửi realtime cho đúng người)
sid_users = {}      # socket id -> {user_id, username} — dùng thay session trong socket handlers

@socketio.on('connect')
def on_connect():
    print(f'[CONNECT] sid={request.sid} session_keys={list(session.keys())}')
    if 'user_id' in session:
        uid = session['user_id']
        uname = session['username']
        online_users[uid] = uname
        user_sids[uid] = request.sid
        sid_users[request.sid] = {'user_id': uid, 'username': uname}
        print(f'[CONNECT] user={uname} uid={uid} sid={request.sid}')
        emit('online_users', list(online_users.values()), broadcast=True)
    else:
        print(f'[CONNECT] WARNING: no session! sid={request.sid}')

@socketio.on('disconnect')
def on_disconnect():
    user_info = sid_users.pop(request.sid, None)
    if user_info:
        uid = user_info['user_id']
        online_users.pop(uid, None)
        user_sids.pop(uid, None)
    elif 'user_id' in session:
        online_users.pop(session['user_id'], None)
        user_sids.pop(session['user_id'], None)
    emit('online_users', list(online_users.values()), broadcast=True)

@socketio.on('join_room')
def on_join(data):
    rid = str(data['room_id'])
    join_room(rid)
    user_info = sid_users.get(request.sid, {})
    print(f'[JOIN_ROOM] sid={request.sid} room={rid} user={user_info.get("username","unknown")}')

@socketio.on('leave_room')
def on_leave(data):
    leave_room(str(data['room_id']))

@socketio.on('send_message')
def on_message(data):
    # Lấy user từ sid_users trước, fallback về session
    print(f'[SEND_MSG] sid={request.sid} data={data} sid_users_keys={list(sid_users.keys())[:5]}')
    user_info = sid_users.get(request.sid)
    if not user_info:
        print(f'[SEND_MSG] no sid_users entry, checking session: {list(session.keys())}')
        if 'user_id' not in session:
            print(f'[SEND_MSG] ERROR: no session either! dropping message')
            return
        user_info = {'user_id': session['user_id'], 'username': session['username']}
        sid_users[request.sid] = user_info

    uid = user_info['user_id']
    uname = user_info['username']
    room_id = data.get('room_id')
    content = data.get('content', '').strip()
    msg_type = data.get('type', 'text')
    print(f'[SEND_MSG] user={uname} room={room_id} content={content[:30]}')

    if not content and msg_type == 'text':
        return
    if not room_id:
        print(f'[SEND_MSG] ERROR: room_id is None!')
        return

    # Lưu DB trước để lấy id thật
    try:
        db = get_db(); cur = db.cursor()
        if DATABASE_URL:
            cur.execute(f'INSERT INTO messages (room_id, user_id, content, type) VALUES ({PH},{PH},{PH},{PH}) RETURNING id',
                        (room_id, uid, content, msg_type))
            msg_id = cur.fetchone()[0]
        else:
            cur.execute(f'INSERT INTO messages (room_id, user_id, content, type) VALUES ({PH},{PH},{PH},{PH})',
                        (room_id, uid, content, msg_type))
            msg_id = cur.lastrowid
        db.commit(); cur.close(); release_db(db)
    except Exception as e:
        print(f'DB save error: {e}')
        return

    # Emit sau khi DB thành công
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    emit('new_message', {
        'id': msg_id, 'room_id': room_id, 'content': content, 'type': msg_type,
        'username': uname, 'user_id': uid,
        'created_at': now_str, 'reactions': '[]'
    }, room=str(room_id))

@socketio.on('react_message')
def on_react(data):
    user_info = sid_users.get(request.sid)
    if not user_info and 'user_id' not in session:
        return
    if not user_info:
        user_info = {'user_id': session['user_id'], 'username': session['username']}
    msg_id = data['message_id']
    emoji = data['emoji']
    room_id = data['room_id']
    uid = user_info['user_id']
    db = get_db(); cur = db.cursor()
    cur.execute(f'SELECT emoji FROM reactions WHERE message_id={PH} AND user_id={PH}',
                (msg_id, uid))
    existing = dict_row(cur)
    if existing and existing['emoji'] == emoji:
        cur.execute(f'DELETE FROM reactions WHERE message_id={PH} AND user_id={PH}',
                    (msg_id, uid))
    else:
        if DATABASE_URL:
            cur.execute(f'INSERT INTO reactions (message_id, user_id, emoji) VALUES ({PH},{PH},{PH}) ON CONFLICT (message_id, user_id) DO UPDATE SET emoji={PH}',
                        (msg_id, uid, emoji, emoji))
        else:
            cur.execute(f'INSERT OR REPLACE INTO reactions (message_id, user_id, emoji) VALUES ({PH},{PH},{PH})',
                        (msg_id, uid, emoji))
    db.commit()
    cur.execute(f'SELECT emoji, user_id FROM reactions WHERE message_id={PH}', (msg_id,))
    reactions = dict_rows(cur)
    cur.close(); release_db(db)
    emit('reaction_update', {'message_id': msg_id, 'reactions': reactions}, room=str(room_id))

@socketio.on('typing')
def on_typing(data):
    user_info = sid_users.get(request.sid, {})
    uname = user_info.get('username') or session.get('username', '')
    emit('user_typing', {'username': uname, 'typing': data.get('typing')},
         room=str(data.get('room_id')), include_self=False)

@socketio.on('delete_message')
def on_delete(data):
    user_info = sid_users.get(request.sid)
    if not user_info and 'user_id' not in session:
        return
    if not user_info:
        user_info = {'user_id': session['user_id'], 'username': session['username']}
    uid = user_info['user_id']
    msg_id = data['message_id']
    room_id = data['room_id']
    db = get_db(); cur = db.cursor()
    cur.execute(f'SELECT user_id FROM messages WHERE id={PH}', (msg_id,))
    msg = dict_row(cur)
    if not msg or msg['user_id'] != uid:
        cur.close(); release_db(db); return
    cur.execute(f"UPDATE messages SET deleted=1, content='Tin nhắn đã bị xoá' WHERE id={PH}", (msg_id,))
    db.commit(); cur.close(); release_db(db)
    emit('message_deleted', {'message_id': msg_id}, room=str(room_id))

@socketio.on('pin_message')
def on_pin(data):
    user_info = sid_users.get(request.sid)
    if not user_info and 'user_id' not in session:
        return
    msg_id = data['message_id']
    room_id = data['room_id']
    db = get_db(); cur = db.cursor()
    cur.execute(f'SELECT pinned FROM messages WHERE id={PH}', (msg_id,))
    msg = dict_row(cur)
    if not msg:
        cur.close(); release_db(db); return
    new_pin = 0 if msg['pinned'] else 1
    cur.execute(f'UPDATE messages SET pinned={PH} WHERE id={PH}', (new_pin, msg_id))
    cur.execute(f'''SELECT m.id, m.content, m.type, u.username
                   FROM messages m JOIN users u ON u.id=m.user_id
                   WHERE m.room_id={PH} AND m.pinned=1 ORDER BY m.id DESC LIMIT 1''', (room_id,))
    pinned = dict_row(cur)
    db.commit(); cur.close(); release_db(db)
    emit('pin_update', {'room_id': room_id, 'pinned': pinned}, room=str(room_id))

@socketio.on('mark_read')
def on_mark_read(data):
    user_info = sid_users.get(request.sid)
    if not user_info and 'user_id' not in session:
        return
    if not user_info:
        user_info = {'user_id': session['user_id'], 'username': session['username']}
    uid = user_info['user_id']
    uname = user_info['username']
    room_id = data['room_id']
    msg_id = data.get('msg_id', 0)
    db = get_db(); cur = db.cursor()
    if DATABASE_URL:
        cur.execute(f'INSERT INTO read_receipts (room_id, user_id, last_read_msg_id) VALUES ({PH},{PH},{PH}) ON CONFLICT (room_id, user_id) DO UPDATE SET last_read_msg_id={PH}',
                    (room_id, uid, msg_id, msg_id))
    else:
        cur.execute(f'INSERT OR REPLACE INTO read_receipts (room_id, user_id, last_read_msg_id) VALUES ({PH},{PH},{PH})',
                    (room_id, uid, msg_id))
    db.commit(); cur.close(); release_db(db)
    emit('read_update', {'room_id': room_id, 'user_id': uid,
                         'username': uname, 'msg_id': msg_id}, room=str(room_id))

if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)
