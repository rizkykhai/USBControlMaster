from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib
import hmac
import os
import secrets
import sqlite3

from fastapi import FastAPI, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
# IMPORTANT: keep using the existing production database.
DB_PATH = BASE_DIR / "master.db"

app = FastAPI(title="USB Control Master V4 - USB + MTP/WPD")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# --- Dashboard auth (email + password, session cookie 30 menit) ---
SESSION_COOKIE = "usb_session"
SESSION_TTL_SECONDS = 30 * 60


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def now():
    return datetime.now(timezone.utc).isoformat()


def is_online(last_seen_str, max_age_seconds=120):
    """Return True if last_seen is within max_age_seconds."""
    if not last_seen_str:
        return False
    try:
        last = datetime.fromisoformat(last_seen_str)
        delta = datetime.now(timezone.utc) - last
        return delta.total_seconds() < max_age_seconds
    except Exception:
        return False


def ensure_column(conn, table, column, definition):
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def hash_password(password: str) -> str:
    """PBKDF2-HMAC-SHA256, format: salt_hex$hash_hex (kompatibel hashlib)."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return f"{salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, hash_hex = stored.split("$", 1)
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), 200_000
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def create_session(conn, email: str) -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(seconds=SESSION_TTL_SECONDS)
    conn.execute(
        "INSERT INTO sessions(token, email, expires_at, created_at) VALUES (?, ?, ?, ?)",
        (token, email, expires.isoformat(), now()),
    )
    return token


def get_session(conn, token: str):
    """Return session row jika token valid & belum kedaluwarsa, selain itu None."""
    if not token:
        return None
    row = conn.execute(
        "SELECT token, email, expires_at FROM sessions WHERE token=?", (token,)
    ).fetchone()
    if not row:
        return None
    try:
        expires = datetime.fromisoformat(row["expires_at"])
    except Exception:
        return None
    if datetime.now(timezone.utc) >= expires:
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()
        return None
    return row


def current_user(request: Request):
    """Return email user jika cookie session valid, selain itu None."""
    token = request.cookies.get(SESSION_COOKIE, "")
    conn = db()
    try:
        row = get_session(conn, token)
        return row["email"] if row else None
    finally:
        conn.close()


def init_db():
    conn = db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT UNIQUE NOT NULL,
            device_name TEXT NOT NULL,
            username TEXT DEFAULT '',
            status TEXT DEFAULT 'offline',
            usb_enabled INTEGER DEFAULT 1,
            last_seen TEXT,
            agent_token TEXT UNIQUE,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS enrollment_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT UNIQUE NOT NULL,
            used INTEGER DEFAULT 0,
            device_id TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    # Akun admin awal: hanya dibuat jika tabel users masih kosong.
    admin_email = os.environ.get("ADMIN_EMAIL", "admin@usbcontrol.local")
    admin_password = os.environ.get("ADMIN_PASSWORD", "admin123")
    count = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    if count == 0:
        conn.execute(
            "INSERT INTO users(email, password_hash, created_at) VALUES (?, ?, ?)",
            (admin_email.lower().strip(), hash_password(admin_password), now()),
        )

    # Safe migration for the old master.db: add only missing fields.
    ensure_column(conn, "devices", "display_name", "TEXT")
    ensure_column(conn, "devices", "username", "TEXT DEFAULT ''")
    ensure_column(conn, "devices", "status", "TEXT DEFAULT 'offline'")
    ensure_column(conn, "devices", "usb_enabled", "INTEGER DEFAULT 1")
    ensure_column(conn, "devices", "mtp_enabled", "INTEGER DEFAULT 1")
    ensure_column(conn, "devices", "last_seen", "TEXT")
    ensure_column(conn, "devices", "agent_token", "TEXT")
    ensure_column(conn, "devices", "created_at", "TEXT")
    ensure_column(conn, "enrollment_keys", "used", "INTEGER DEFAULT 0")
    ensure_column(conn, "enrollment_keys", "device_id", "TEXT")
    ensure_column(conn, "enrollment_keys", "created_at", "TEXT")

    conn.execute("UPDATE devices SET display_name=device_name WHERE display_name IS NULL OR display_name=''")
    conn.execute("UPDATE devices SET username='' WHERE username IS NULL")
    conn.execute("UPDATE devices SET usb_enabled=1 WHERE usb_enabled IS NULL")
    conn.execute("UPDATE devices SET mtp_enabled=1 WHERE mtp_enabled IS NULL")
    conn.execute("UPDATE enrollment_keys SET used=0 WHERE used IS NULL")
    conn.commit()
    conn.close()


init_db()


class USBCommand(BaseModel):
    enabled: bool


class MTPCommand(BaseModel):
    enabled: bool


class RenameCommand(BaseModel):
    display_name: str


class Enrollment(BaseModel):
    key: str
    device_id: str
    device_name: str


@app.get("/health")
def health():
    return {"status": "ok", "database": "master.db"}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html", context={})


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    conn = db()
    try:
        row = conn.execute(
            "SELECT email, password_hash FROM users WHERE email=?",
            (email.strip().lower(),),
        ).fetchone()
        if not row or not verify_password(password, row["password_hash"]):
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context={"error": "Email atau password salah."},
                status_code=401,
            )
        token = create_session(conn, row["email"])
        conn.commit()
    finally:
        conn.close()
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
    )
    return response


@app.post("/logout")
def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE, "")
    if token:
        conn = db()
        try:
            conn.execute("DELETE FROM sessions WHERE token=?", (token,))
            conn.commit()
        finally:
            conn.close()
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    conn = db()
    devices = conn.execute("""
        SELECT id, device_id, device_name, display_name, username,
               status, usb_enabled, mtp_enabled, last_seen
        FROM devices ORDER BY id DESC
    """).fetchall()
    conn.close()
    device_list = []
    for d in devices:
        item = dict(d)
        item["online"] = is_online(d["last_seen"])
        device_list.append(item)
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={"devices": device_list},
    )


@app.get("/api/devices")
def get_devices(request: Request):
    if not current_user(request):
        raise HTTPException(401, "Unauthorized")
    conn = db()
    rows = conn.execute("""
        SELECT id, device_id, device_name, display_name, username,
               status, usb_enabled, mtp_enabled, last_seen
        FROM devices ORDER BY id DESC
    """).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["usb_enabled"] = bool(d.get("usb_enabled"))
        d["mtp_enabled"] = bool(d.get("mtp_enabled"))
        d["online"] = is_online(d.get("last_seen"))
        result.append(d)
    return result


@app.get("/api/devices/{device_id}/status")
def get_device_status(device_id: str):
    """Return detailed status for a single device (for diagnostics)."""
    conn = db()
    row = conn.execute(
        "SELECT * FROM devices WHERE device_id=?", (device_id,)
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Device tidak ditemukan")
    d = dict(row)
    d["usb_enabled"] = bool(d.get("usb_enabled"))
    d["mtp_enabled"] = bool(d.get("mtp_enabled"))
    d["online"] = is_online(d.get("last_seen"))
    return d


@app.post("/api/enrollment-keys")
def create_key(request: Request):
    if not current_user(request):
        raise HTTPException(401, "Unauthorized")
    conn = db()
    for _ in range(10):
        key = "-".join(secrets.token_hex(2).upper() for _ in range(4))
        try:
            conn.execute(
                "INSERT INTO enrollment_keys(key, used, created_at) VALUES (?, 0, ?)",
                (key, now()),
            )
            conn.commit()
            conn.close()
            return {"status": "ok", "key": key}
        except sqlite3.IntegrityError:
            continue
    conn.close()
    raise HTTPException(500, "Gagal membuat Registration Key unik")


@app.post("/api/agent/enroll")
def enroll(payload: Enrollment):
    conn = db()
    row = conn.execute(
        "SELECT id, used FROM enrollment_keys WHERE key=?",
        (payload.key.strip(),),
    ).fetchone()
    if not row or row["used"]:
        conn.close()
        raise HTTPException(403, "Enrollment key tidak valid atau sudah digunakan")

    existing = conn.execute(
        "SELECT id, agent_token, usb_enabled FROM devices WHERE device_id=?",
        (payload.device_id,),
    ).fetchone()
    token = secrets.token_urlsafe(32)

    if existing:
        # Preserve the existing USB policy when a known device re-enrolls.
        conn.execute("""
            UPDATE devices
            SET device_name=?, agent_token=?, status='online', last_seen=?
            WHERE device_id=?
        """, (payload.device_name, token, now(), payload.device_id))
    else:
        conn.execute("""
            INSERT INTO devices(
                device_id, device_name, display_name, username,
                status, usb_enabled, mtp_enabled, agent_token, last_seen, created_at
            ) VALUES (?, ?, ?, '', 'online', 1, 1, ?, ?, ?)
        """, (
            payload.device_id,
            payload.device_name,
            payload.device_name,
            token,
            now(),
            now(),
        ))

    conn.execute(
        "UPDATE enrollment_keys SET used=1, device_id=? WHERE id=?",
        (payload.device_id, row["id"]),
    )
    conn.commit()

    dev = conn.execute(
        "SELECT device_id, usb_enabled, mtp_enabled FROM devices WHERE device_id=?",
        (payload.device_id,),
    ).fetchone()
    conn.close()
    return {
        "status": "registered",
        "device_id": dev["device_id"],
        "agent_token": token,
        "usb_enabled": bool(dev["usb_enabled"]),
        "mtp_enabled": bool(dev["mtp_enabled"]),
    }


@app.get("/api/agent/policy")
def agent_policy(request: Request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Missing agent token")
    token = auth[7:].strip()
    conn = db()
    row = conn.execute(
        "SELECT * FROM devices WHERE agent_token=?", (token,)
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(401, "Invalid agent token")
    conn.execute(
        "UPDATE devices SET status='online', last_seen=? WHERE id=?",
        (now(), row["id"]),
    )
    conn.commit()
    result = {"device_id": row["device_id"], "usb_enabled": bool(row["usb_enabled"]), "mtp_enabled": bool(row["mtp_enabled"])}
    conn.close()
    return result


@app.post("/api/devices/{device_id}/usb")
def set_usb(request: Request, device_id: str, command: USBCommand):
    if not current_user(request):
        raise HTTPException(401, "Unauthorized")
    conn = db()
    cur = conn.execute(
        "UPDATE devices SET usb_enabled=? WHERE device_id=?",
        (1 if command.enabled else 0, device_id),
    )
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        raise HTTPException(404, "Device tidak ditemukan")
    return {"status": "ok", "device_id": device_id, "usb_enabled": command.enabled}


@app.post("/api/devices/{device_id}/mtp")
def set_mtp(request: Request, device_id: str, command: MTPCommand):
    if not current_user(request):
        raise HTTPException(401, "Unauthorized")
    conn = db()
    cur = conn.execute(
        "UPDATE devices SET mtp_enabled=? WHERE device_id=?",
        (1 if command.enabled else 0, device_id),
    )
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        raise HTTPException(404, "Device tidak ditemukan")
    return {"status": "ok", "device_id": device_id, "mtp_enabled": command.enabled}


@app.post("/api/devices/{device_id}/rename")
def rename_device(request: Request, device_id: str, command: RenameCommand):
    if not current_user(request):
        raise HTTPException(401, "Unauthorized")
    name = command.display_name.strip()
    if not name:
        raise HTTPException(400, "Nama tidak boleh kosong")
    if len(name) > 80:
        raise HTTPException(400, "Nama maksimal 80 karakter")
    conn = db()
    cur = conn.execute(
        "UPDATE devices SET display_name=? WHERE device_id=?",
        (name, device_id),
    )
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        raise HTTPException(404, "Device tidak ditemukan")
    return {"status": "ok", "device_id": device_id, "display_name": name}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)
