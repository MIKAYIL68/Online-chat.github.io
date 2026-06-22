import sqlite3
import os
import json
import uuid
from datetime import datetime
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, UploadFile, File, Cookie
from fastapi.responses import JSONResponse

# Создаем роутер, который потом подключим к основному приложению
chat_router = APIRouter()

# Создаем папку для картинок из чата
os.makedirs("static/chat_uploads", exist_ok=True)

# Инициализация НОВОЙ базы данных для чата
def init_chat_db():
    with sqlite3.connect("chat.db") as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender TEXT NOT NULL,
                receiver TEXT NOT NULL, -- 'GLOBAL' или логин пользователя для ЛС
                type TEXT NOT NULL, -- 'text' или 'image'
                content TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS blocks (
                blocker TEXT NOT NULL,
                blocked TEXT NOT NULL,
                UNIQUE(blocker, blocked)
            )
        """)
        conn.commit()

init_chat_db()

# Утилита для получения логина из старой базы users.db
def get_user_from_token(token: str):
    if not token: return None
    with sqlite3.connect("users.db") as conn:
        row = conn.execute("SELECT login FROM sessions WHERE token = ? AND expires_at > ?", (token, datetime.now())).fetchone()
        return row[0] if row else None

def is_blocked(sender: str, receiver: str):
    with sqlite3.connect("chat.db") as conn:
        row = conn.execute("SELECT 1 FROM blocks WHERE blocker = ? AND blocked = ?", (receiver, sender)).fetchone()
        return bool(row)

# Хранилище активных подключений: {login: websocket}
active_connections = {}

@chat_router.get("/api/me")
def get_me(session_token: str = Cookie(default=None)):
    """API для получения имени текущего пользователя во фронтенд"""
    user = get_user_from_token(session_token)
    return {"login": user} if user else {"login": None}

@chat_router.get("/api/chat/history")
def get_chat_history(target: str = "GLOBAL", session_token: str = Cookie(default=None)):
    """Загрузка старых сообщений при обновлении страницы"""
    user = get_user_from_token(session_token)
    if not user: return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    with sqlite3.connect("chat.db") as conn:
        if target == "GLOBAL":
            # Исключаем сообщения от тех, кого мы заблокировали
            rows = conn.execute("""
                SELECT sender, content, type, timestamp FROM messages 
                WHERE receiver = 'GLOBAL' 
                AND sender NOT IN (SELECT blocked FROM blocks WHERE blocker = ?)
                ORDER BY timestamp ASC LIMIT 100
            """, (user,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT sender, content, type, timestamp FROM messages 
                WHERE ((sender = ? AND receiver = ?) OR (sender = ? AND receiver = ?))
                ORDER BY timestamp ASC LIMIT 100
            """, (user, target, target, user)).fetchall()
            
    return [{"sender": r[0], "content": r[1], "type": r[2], "timestamp": r[3]} for r in rows]

@chat_router.post("/api/chat/action")
def user_action(action: str, target: str, session_token: str = Cookie(default=None)):
    """Блок и разблок пользователей"""
    user = get_user_from_token(session_token)
    if not user: return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    
    with sqlite3.connect("chat.db") as conn:
        if action == "block":
            conn.execute("INSERT OR IGNORE INTO blocks (blocker, blocked) VALUES (?, ?)", (user, target))
        elif action == "unblock":
            conn.execute("DELETE FROM blocks WHERE blocker = ? AND blocked = ?", (user, target))
        conn.commit()
    return {"status": "ok"}

@chat_router.post("/api/chat/upload")
async def upload_image(file: UploadFile = File(...), session_token: str = Cookie(default=None)):
    """Загрузка картинок"""
    user = get_user_from_token(session_token)
    if not user: return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    
    ext = file.filename.split('.')[-1]
    filename = f"{uuid.uuid4().hex}.{ext}"
    filepath = f"static/chat_uploads/{filename}"
    
    with open(filepath, "wb") as f:
        f.write(await file.read())
        
    return {"url": f"/{filepath}"}

@chat_router.websocket("/ws/chat")
async def websocket_endpoint(websocket: WebSocket):
    """Веб-сокет для моментального обмена сообщениями"""
    token = websocket.cookies.get("session_token")
    user = get_user_from_token(token)
    
    if not user:
        await websocket.close()
        return

    await websocket.accept()
    active_connections[user] = websocket

    try:
        while True:
            data = await websocket.receive_text()
            payload = json.loads(data)
            receiver = payload.get("receiver", "GLOBAL")
            msg_type = payload.get("type", "text")
            content = payload.get("content", "")

            # Проверка блокировки перед сохранением
            if receiver != "GLOBAL" and is_blocked(user, receiver):
                continue # Если получатель заблокировал отправителя, игнорируем

            # Сохраняем в БД
            with sqlite3.connect("chat.db") as conn:
                conn.execute("INSERT INTO messages (sender, receiver, type, content) VALUES (?, ?, ?, ?)",
                             (user, receiver, msg_type, content))
                conn.commit()

            message_data = json.dumps({
                "sender": user,
                "receiver": receiver,
                "type": msg_type,
                "content": content
            })

            # Отправка
            if receiver == "GLOBAL":
                for connected_user, ws in active_connections.items():
                    if not is_blocked(user, connected_user): # Не шлем заблокировавшим
                        await ws.send_text(message_data)
            else:
                # Отправляем получателю
                if receiver in active_connections:
                    await active_connections[receiver].send_text(message_data)
                # Отправляем обратно себе (чтобы отобразилось в интерфейсе ЛС)
                if user in active_connections and receiver != user:
                    await active_connections[user].send_text(message_data)

    except WebSocketDisconnect:
        if user in active_connections:
            del active_connections[user]