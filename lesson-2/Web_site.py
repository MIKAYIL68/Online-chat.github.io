import sqlite3
import smtplib
import secrets
import bcrypt
import os
from datetime import datetime, timedelta
from fastapi import FastAPI, Form, Request, Response, Cookie
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from email.mime.text import MIMEText
from dotenv import load_dotenv
from chat_app import chat_router
import uvicorn

load_dotenv("data.env")

app = FastAPI()
templates = Jinja2Templates(directory="templates")
app.include_router(chat_router)
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

SENDER_EMAIL    = os.getenv("SENDER_EMAIL")
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD")
CODE_EXPIRY_MINUTES = 10

if not SENDER_EMAIL or not SENDER_PASSWORD:
    print("⚠ ВНИМАНИЕ: SENDER_EMAIL или SENDER_PASSWORD не заданы.")
    print("  Проверьте, что файл .env лежит рядом со скриптом и содержит обе переменные.")
else:
    print(f"✓ SENDER_EMAIL загружен: {SENDER_EMAIL}")
    print(f"✓ SENDER_PASSWORD загружен, длина: {len(SENDER_PASSWORD)} символов")
    if " " in SENDER_PASSWORD:
        print("⚠ В SENDER_PASSWORD есть пробелы — Google App Password их не должен содержать. Удалите пробелы в .env.")


# ──────────────────────────── БД ─────────────────────────────

def init_db():
    with sqlite3.connect("users.db") as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                login      TEXT UNIQUE NOT NULL,
                password   TEXT NOT NULL,
                phone      TEXT,
                email      TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_verifications (
                login      TEXT PRIMARY KEY,
                code       TEXT NOT NULL,
                password   TEXT NOT NULL,
                phone      TEXT,
                email      TEXT NOT NULL,
                expires_at TIMESTAMP NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token      TEXT PRIMARY KEY,
                login      TEXT NOT NULL,
                expires_at TIMESTAMP NOT NULL
            )
        """)
        conn.commit()

init_db()


# ──────────────────────────── Утилиты ────────────────────────

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def check_password(password: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except (ValueError, TypeError) as e:
        print(f"⚠ Повреждённый хэш пароля в базе, не удалось проверить: {e}")
        return False

def create_session(login: str) -> str:
    token = secrets.token_hex(32)
    expires_at = datetime.now() + timedelta(hours=24)
    with sqlite3.connect("users.db") as conn:
        conn.execute(
            "INSERT INTO sessions (token, login, expires_at) VALUES (?, ?, ?)",
            (token, login, expires_at)
        )
        conn.commit()
    return token

def get_session_user(token: str | None) -> str | None:
    if not token:
        return None
    with sqlite3.connect("users.db") as conn:
        row = conn.execute(
            "SELECT login FROM sessions WHERE token = ? AND expires_at > ?",
            (token, datetime.now())
        ).fetchone()
    return row[0] if row else None

def send_email(to_email: str, subject: str, body: str):
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = SENDER_EMAIL
    msg["To"] = to_email
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.sendmail(SENDER_EMAIL, to_email, msg.as_string())


# ──────────────────────────── Маршруты ───────────────────────

@app.get("/", response_class=HTMLResponse)
def read_root(request: Request):
    return templates.TemplateResponse(request=request, name="main.html")


# ── Регистрация ──

@app.get("/reg", response_class=HTMLResponse)
def show_reg_form(request: Request, session_token: str = Cookie(default=None)):
    if get_session_user(session_token):
        return RedirectResponse(url="/main_page", status_code=302)
    return templates.TemplateResponse(request=request, name="reg.html")

@app.post("/send")
def register(
    request: Request,
    login:    str = Form(...),
    password: str = Form(...),
    phone:    str = Form(default=""),
    Gmail:    str = Form(...),
):
    if len(login) < 3:
        return templates.TemplateResponse(
            request=request, name="error.html",
            context={"message": "Логин должен содержать минимум 3 символа."}
        )
    if len(password) < 8:
        return templates.TemplateResponse(
            request=request, name="error.html",
            context={"message": "Пароль должен содержать минимум 8 символов."}
        )

    with sqlite3.connect("users.db") as conn:
        existing = conn.execute(
            "SELECT id FROM users WHERE login = ?", (login,)
        ).fetchone()
    if existing:
        return templates.TemplateResponse(
            request=request, name="error.html",
            context={"message": "Этот логин уже занят. Выберите другой."}
        )

    code = str(secrets.randbelow(900000) + 100000)
    hashed_pw = hash_password(password)
    expires_at = datetime.now() + timedelta(minutes=CODE_EXPIRY_MINUTES)

    with sqlite3.connect("users.db") as conn:
        conn.execute("DELETE FROM pending_verifications WHERE login = ?", (login,))
        conn.execute(
            """INSERT INTO pending_verifications
               (login, code, password, phone, email, expires_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (login, code, hashed_pw, phone, Gmail, expires_at)
        )
        conn.commit()

    try:
        send_email(
            Gmail,
            "Код подтверждения",
            f"Привет, {login}!\n\n"
            f"Твой код подтверждения: {code}\n\n"
            f"Код действителен {CODE_EXPIRY_MINUTES} минут."
        )
    except smtplib.SMTPAuthenticationError as e:
        print(f"Ошибка авторизации SMTP: {e}")
        return templates.TemplateResponse(
            request=request, name="error.html",
            context={"message": "Не удалось отправить письмо: ошибка авторизации почтового сервера. "
                                 "Администратору нужно проверить App Password в .env."}
        )
    except Exception as e:
        print(f"Ошибка при отправке письма: {e}")
        return templates.TemplateResponse(
            request=request, name="error.html",
            context={"message": "Не удалось отправить письмо. Проверь адрес почты."}
        )

    return templates.TemplateResponse(
        request=request, name="verify.html",
        context={"login": login}
    )

@app.post("/verify")
def verify_code(request: Request, login: str = Form(), code: str = Form()):
    with sqlite3.connect("users.db") as conn:
        row = conn.execute(
            "SELECT code, password, phone, email, expires_at "
            "FROM pending_verifications WHERE login = ?",
            (login,)
        ).fetchone()

    if not row:
        return templates.TemplateResponse(
            request=request, name="error.html",
            context={"message": "Запрос не найден. Пройди регистрацию заново."}
        )

    stored_code, hashed_pw, phone, email, expires_at = row

    if datetime.now() > datetime.fromisoformat(expires_at):
        with sqlite3.connect("users.db") as conn:
            conn.execute("DELETE FROM pending_verifications WHERE login = ?", (login,))
            conn.commit()
        return templates.TemplateResponse(
            request=request, name="error.html",
            context={"message": f"Код истёк (срок {CODE_EXPIRY_MINUTES} мин). Зарегистрируйся заново."}
        )

    if stored_code != code.strip():
        return templates.TemplateResponse(
            request=request, name="error.html",
            context={"message": "Неверный код! Попробуй снова."}
        )

    with sqlite3.connect("users.db") as conn:
        conn.execute(
            "INSERT INTO users (login, password, phone, email) VALUES (?, ?, ?, ?)",
            (login, hashed_pw, phone, email)
        )
        conn.execute("DELETE FROM pending_verifications WHERE login = ?", (login,))
        conn.commit()

    # После регистрации → на главную страницу аккаунта
    token = create_session(login)
    response = RedirectResponse(url="/main_page", status_code=302)
    response.set_cookie("session_token", token, httponly=True, max_age=86400)
    return response


# ── Вход / Выход ──

@app.get("/login", response_class=HTMLResponse)
def show_login_form(request: Request, session_token: str = Cookie(default=None)):
    if get_session_user(session_token):
        return RedirectResponse(url="/main_page", status_code=302)
    return templates.TemplateResponse(request=request, name="login.html")

@app.post("/check-login")
def check_login(request: Request, login: str = Form(), password: str = Form()):
    with sqlite3.connect("users.db") as conn:
        row = conn.execute(
            "SELECT password FROM users WHERE login = ?", (login,)
        ).fetchone()

    if not row or not check_password(password, row[0]):
        return templates.TemplateResponse(
            request=request, name="error.html",
            context={"message": "Ошибка: Неверный логин или пароль!"}
        )

    # После входа → на главную страницу аккаунта
    token = create_session(login)
    response = RedirectResponse(url="/main_page", status_code=302)
    response.set_cookie("session_token", token, httponly=True, max_age=86400)
    return response

@app.get("/logout")
def logout(session_token: str = Cookie(default=None)):
    if session_token:
        with sqlite3.connect("users.db") as conn:
            conn.execute("DELETE FROM sessions WHERE token = ?", (session_token,))
            conn.commit()
    response = RedirectResponse(url="/", status_code=302)
    response.delete_cookie("session_token")
    return response


# ── Главная страница аккаунта (после входа/регистрации) ──

@app.get("/main_page", response_class=HTMLResponse)
def main_page(request: Request, session_token: str = Cookie(default=None)):
    user_login = get_session_user(session_token)
    if not user_login:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse(
        request=request, name="main_page.html",
        context={"login": user_login}
    )


# ── Профиль (защищённая страница) ──

@app.get("/profile", response_class=HTMLResponse)
def profile(request: Request, session_token: str = Cookie(default=None)):
    user_login = get_session_user(session_token)
    if not user_login:
        return RedirectResponse(url="/login", status_code=302)

    with sqlite3.connect("users.db") as conn:
        user = conn.execute(
            "SELECT login, phone, email, created_at FROM users WHERE login = ?",
            (user_login,)
        ).fetchone()

    return templates.TemplateResponse(
        request=request, name="profile.html",
        context={
            "login":      user[0],
            "phone":      user[1],
            "email":      user[2],
            "created_at": user[3],
        }
    )
@app.get('/chat_page', response_class=HTMLResponse)
async def open_chat(request: Request):
    from fastapi.templating import Jinja2Templates
    tmpl = Jinja2Templates(directory="templates")
    
    return tmpl.TemplateResponse(request=request, name="Chat.html", context={})


if __name__ == "__main__":
    uvicorn.run("Web_site:app", host="127.0.0.1", port=8000, reload=True)