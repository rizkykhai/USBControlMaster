from datetime import datetime, timezone
from pathlib import Path
import secrets
import sqlite3

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
# IMPORTANT: keep using the existing production database.
DB_PATH = BASE_DIR / "master.db"

app = FastAPI(title="USB Control Master V4 - USB + MTP/WPD")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


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


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
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
def get_devices():
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
def create_key():
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
def set_usb(device_id: str, command: USBCommand):
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
def set_mtp(device_id: str, command: MTPCommand):
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
def rename_device(device_id: str, command: RenameCommand):
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
