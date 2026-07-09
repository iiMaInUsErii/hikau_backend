from flask import Flask, request, jsonify, send_from_directory, render_template
from flask_cors import CORS
from flask_socketio import SocketIO, join_room
import sqlite3
import time
import os
import hashlib
from werkzeug.utils import secure_filename
from contextlib import closing

app = Flask(__name__)
app.config['SECRET_KEY'] = 'super-secret-key'
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'txt', 'mp3', 'mp4', 'wav', 'doc', 'docx', 'webp'}

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

CORS(app, origins=["http://localhost:5173", "http://localhost:5000"], supports_credentials=True)
socketio = SocketIO(app, cors_allowed_origins=["http://localhost:5173", "http://localhost:5000"], async_mode='eventlet')

DATABASE = 'chat.db'

def get_db():
    conn = sqlite3.connect(DATABASE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

# ====================== ИНИЦИАЛИЗАЦИЯ БД ======================
with closing(get_db()) as conn:
    cursor = conn.cursor()
    cursor.executescript('''
        CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT UNIQUE, password TEXT, created_at INTEGER);
        CREATE TABLE IF NOT EXISTS conversations (id INTEGER PRIMARY KEY, type TEXT, name TEXT, created_at INTEGER);
        CREATE TABLE IF NOT EXISTS participants (conversation_id INTEGER, user_id INTEGER, joined_at INTEGER, PRIMARY KEY(conversation_id, user_id));
        
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY,
            conversation_id INTEGER,
            user_id INTEGER,
            username TEXT,
            text TEXT,
            is_file INTEGER DEFAULT 0,
            file_url TEXT,
            timestamp INTEGER
        );
    ''')
    
    # Добавляем колонку file_url, если её нет (для старых БД)
    try:
        cursor.execute("ALTER TABLE messages ADD COLUMN file_url TEXT")
    except sqlite3.OperationalError:
        pass  # колонка уже существует
    
    conn.commit()

def hash_password(p): 
    return hashlib.sha256(p.encode()).hexdigest()

@app.route('/')
def index():
    return render_template('index.html')

# ====================== AUTH ======================
@app.route("/api/register", methods=['POST'])
def register():
    d = request.get_json()
    try:
        with closing(get_db()) as conn:
            conn.execute("INSERT INTO users (username, password, created_at) VALUES (?,?,?)",
                        (d['username'], hash_password(d['password']), int(time.time())))
            conn.commit()
        return jsonify({"success": True})
    except Exception:
        return jsonify({"error": "Пользователь уже существует"}), 409

@app.route("/api/login", methods=['POST'])
def login():
    d = request.get_json()
    with closing(get_db()) as conn:
        user = conn.execute("SELECT id, username FROM users WHERE username=? AND password=?", 
                           (d['username'], hash_password(d['password']))).fetchone()
        if user:
            return jsonify({"success": True, "user_id": user['id'], "username": user['username']})
    return jsonify({"error": "Неверные данные"}), 401

@app.route("/api/upload", methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({"error": "Нет файла"}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "Файл не выбран"}), 400

    # Проверяем расширение
    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({"error": "Недопустимый тип файла"}), 400

    filename = secure_filename(file.filename)
    # Добавляем timestamp чтобы избежать перезаписи
    unique_filename = f"{int(time.time())}_{filename}"
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)
    file.save(filepath)
    
    file_url = f"/uploads/{unique_filename}"
    return jsonify({"success": True, "file_url": file_url})

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/assets/<path:filename>')
def serve_assets(filename):
    return send_from_directory('assets', filename)

# ====================== CHATS ======================
@app.route("/api/my_chats", methods=['GET'])
def my_chats():
    user_id = request.args.get('user_id')
    with closing(get_db()) as conn:
        chats = conn.execute("""
            SELECT c.id, c.type, COALESCE(c.name, u.username) as name,
                   m.text as last_message, m.timestamp as last_time
            FROM conversations c
            JOIN participants p ON c.id = p.conversation_id
            LEFT JOIN users u ON u.id = (SELECT user_id FROM participants 
                                        WHERE conversation_id = c.id AND user_id != ? LIMIT 1)
            LEFT JOIN messages m ON m.id = (SELECT id FROM messages 
                                          WHERE conversation_id = c.id 
                                          ORDER BY timestamp DESC LIMIT 1)
            WHERE p.user_id = ?
            GROUP BY c.id
            ORDER BY COALESCE(m.timestamp, c.created_at) DESC
        """, (user_id, user_id)).fetchall()
        
        return jsonify({"chats": [dict(c) for c in chats]})

@app.route("/api/create_chat", methods=['POST'])
def create_chat():
    d = request.get_json()
    user_id = int(d['user_id'])
    target = d['target_username']

    with closing(get_db()) as conn:
        target_user = conn.execute("SELECT id, username FROM users WHERE username=?", (target,)).fetchone()
        if not target_user or target_user['id'] == user_id:
            return jsonify({"error": "Пользователь не найден"}), 400

        conn.execute("INSERT INTO conversations (type, created_at) VALUES ('private', ?)", (int(time.time()),))
        conv_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        conn.execute("INSERT INTO participants VALUES (?, ?, ?)", (conv_id, user_id, int(time.time())))
        conn.execute("INSERT INTO participants VALUES (?, ?, ?)", (conv_id, target_user['id'], int(time.time())))
        conn.commit()

        chat = {"id": conv_id, "name": target_user['username'], "type": "private"}
        return jsonify({"success": True, "chat": chat})

# ====================== MESSAGES ======================
@app.route("/api/messages", methods=['GET'])
def get_messages():
    conv_id = request.args.get('conversation_id')
    with closing(get_db()) as conn:
        msgs = conn.execute("""
            SELECT * FROM messages 
            WHERE conversation_id = ? 
            ORDER BY timestamp
        """, (conv_id,)).fetchall()
        return jsonify({"messages": [dict(m) for m in msgs]})

# ====================== SOCKETS ======================
@socketio.on('join_room')
def on_join(room_id):
    """room_id приходит как int"""
    if isinstance(room_id, (int, str)):
        join_room(str(room_id))  # Socket.IO требует строку для комнаты
    else:
        join_room(str(room_id.get('room') if isinstance(room_id, dict) else room_id))

@socketio.on('send_message')
def handle_message(data):
    with closing(get_db()) as conn:
        conn.execute("""
            INSERT INTO messages 
            (conversation_id, user_id, username, text, is_file, file_url, timestamp) 
            VALUES (?,?,?,?,?,?,?)
        """, (
            data['conversation_id'], 
            data['user_id'], 
            data['username'], 
            data.get('text', ''),
            data.get('is_file', 0),
            data.get('file_url'),
            data.get('timestamp', int(time.time()))
        ))
        conn.commit()

    socketio.emit('new_message', data, room=str(data['conversation_id']))

@socketio.on('typing')
def handle_typing(data):
    socketio.emit('typing', data, room=str(data['conversation_id']))

if __name__ == '__main__':
    print("🚀 Сервер запущен на http://localhost:5000")
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)