"""
Assignment Service - FastAPI backend (runs on Vercel and locally)

Vercel:  set the environment variables in .env.example, connect an Upstash Redis
         database and a *private* Blob store, then deploy. See README.md.
Local:   pip install -r requirements.txt && python main.py  ->  http://127.0.0.1:8000
         (without Redis / Blob configured, data is kept in ./data and ./uploads)
"""
import os, re, json, time, hmac, base64, hashlib, secrets, uuid
from contextlib import AsyncExitStack
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Optional, List, Any
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException, Request, Response, Depends, UploadFile, File
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

BASE_DIR = Path(__file__).parent
try:  # optional: load .env / .env.local for local development
    from dotenv import load_dotenv
    for _name in (".env.local", ".env"):
        load_dotenv(BASE_DIR / _name)
except ImportError:
    pass

# ---------- CONFIG ----------
IS_VERCEL = bool(os.getenv("VERCEL"))
PUBLIC_DIR = BASE_DIR / "public"

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "trishitdas2009@gmail.com").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "ADMIN@#123")
SESSION_SECRET = os.getenv("SESSION_SECRET", "")

if IS_VERCEL:
    _missing = [k for k, v in {"ADMIN_EMAIL": ADMIN_EMAIL, "ADMIN_PASSWORD": ADMIN_PASSWORD, "SESSION_SECRET": SESSION_SECRET}.items() if not v]
    if _missing:
        raise RuntimeError("Missing environment variables: " + ", ".join(_missing))
    if len(SESSION_SECRET) < 32:
        raise RuntimeError("SESSION_SECRET must be at least 32 characters.")
else:  # local convenience: never ship a default password
    ADMIN_EMAIL = ADMIN_EMAIL or "admin@example.com"
    SESSION_SECRET = SESSION_SECRET or secrets.token_hex(32)
    if not ADMIN_PASSWORD:
        ADMIN_PASSWORD = secrets.token_urlsafe(12)
        print(f"\n[dev] ADMIN_PASSWORD not set. Temporary admin login: {ADMIN_EMAIL} / {ADMIN_PASSWORD}\n")

COOKIE_NAME = "assignment_session"
SESSION_LIFETIME = 60 * 60 * 24 * 7
COOKIE_SECURE = IS_VERCEL or os.getenv("COOKIE_SECURE", "").lower() in ("1", "true", "yes")

# Vercel Functions reject request bodies over 4.5 MB, so each file must stay below that.
MAX_UPLOAD_MB = 4
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
ALLOWED_EXT = {".pdf", ".doc", ".docx", ".jpg", ".jpeg", ".png", ".heic", ".webp", ".zip", ".txt", ".ppt", ".pptx", ".xls", ".xlsx"}
STORED_RE = re.compile(r"[A-Za-z0-9._-]{1,120}")
OWNER_RE = re.compile(r"[A-Za-z0-9_]{1,40}")

# Persistent storage (Vercel's filesystem is read-only / ephemeral)
REDIS_URL = (os.getenv("UPSTASH_REDIS_REST_URL") or os.getenv("KV_REST_API_URL") or "").rstrip("/")
REDIS_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN") or os.getenv("KV_REST_API_TOKEN") or ""
USE_REDIS = bool(REDIS_URL and REDIS_TOKEN)
USE_BLOB = bool(os.getenv("BLOB_STORE_ID") or os.getenv("BLOB_READ_WRITE_TOKEN"))

DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = BASE_DIR / "uploads"
USERS_FILE = DATA_DIR / "users.json"
REQUESTS_FILE = DATA_DIR / "requests.json"
if not IS_VERCEL:
    DATA_DIR.mkdir(exist_ok=True)
    UPLOAD_DIR.mkdir(exist_ok=True)

app = FastAPI(title="Assignment Service API")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if request.url.path.startswith("/api/"):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


# ---------- SESSION ----------
def now_iso(): return datetime.now(timezone.utc).isoformat()
def b64url(b: bytes): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

def sign_session(payload: dict):
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    body = b64url(raw)
    sig = hmac.new(SESSION_SECRET.encode(), body.encode(), hashlib.sha256).digest()
    return body + "." + b64url(sig)

def read_session(token: Optional[str]):
    if not token or "." not in token: return None
    body, sig = token.rsplit(".", 1)
    exp = b64url(hmac.new(SESSION_SECRET.encode(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, exp): return None
    try:
        p = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if int(p.get("exp", 0)) < int(time.time()): return None
        return p
    except Exception:
        return None

def set_session(res: Response, user: dict):
    token = sign_session({"sub": user["id"], "name": user["name"], "role": user["role"], "email": user.get("email", ""), "exp": int(time.time()) + SESSION_LIFETIME, "nonce": secrets.token_hex(12)})
    res.set_cookie(COOKIE_NAME, token, httponly=True, secure=COOKIE_SECURE, samesite="lax", max_age=SESSION_LIFETIME, path="/")

def current_user(req: Request):
    s = read_session(req.cookies.get(COOKIE_NAME))
    if not s: raise HTTPException(401, "Please sign in.")
    return s

def require_admin(user=Depends(current_user)):
    if user.get("role") != "admin" or user.get("sub") != "admin":
        raise HTTPException(403, "Administrator access required.")
    return user


# ---------- DATABASE (Upstash Redis REST on Vercel, JSON files locally) ----------
def load_json(f: Path):
    if not f.exists(): return []
    try: return json.loads(f.read_text())
    except Exception: return []

def save_json(f: Path, data): f.write_text(json.dumps(data, indent=2))

def need_local():
    if IS_VERCEL:
        raise HTTPException(503, "Database is not configured. Connect an Upstash Redis database to this Vercel project.")

async def redis(*cmd):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(REDIS_URL, headers={"Authorization": f"Bearer {REDIS_TOKEN}"}, json=list(cmd))
        data = r.json() if r.content else {}
    except Exception as e:
        print("redis error:", repr(e))
        raise HTTPException(503, "Database is unreachable. Please try again.")
    if r.status_code >= 400 or (isinstance(data, dict) and "error" in data):
        print("redis error:", r.status_code, data)
        raise HTTPException(503, "Database error. Please try again.")
    return data.get("result")

async def hash_all(key: str):
    raw = await redis("HGETALL", key) or []
    vals = list(raw.values()) if isinstance(raw, dict) else raw[1::2]
    return [json.loads(v) for v in vals]

async def users_all():
    if USE_REDIS: return await hash_all("users")
    need_local(); return load_json(USERS_FILE)

async def users_add(row: dict):
    if USE_REDIS:
        await redis("HSET", "users", row["id"], json.dumps(row)); return
    need_local(); users = load_json(USERS_FILE); users.append(row); save_json(USERS_FILE, users)

async def requests_all():
    if USE_REDIS: return await hash_all("requests")
    need_local(); return load_json(REQUESTS_FILE)

async def requests_get(ref: str):
    if USE_REDIS:
        raw = await redis("HGET", "requests", ref)
        return json.loads(raw) if raw else None
    need_local(); return next((r for r in load_json(REQUESTS_FILE) if r["ref"] == ref), None)

async def requests_save(row: dict):
    if USE_REDIS:
        await redis("HSET", "requests", row["ref"], json.dumps(row)); return
    need_local(); reqs = load_json(REQUESTS_FILE)
    for i, r in enumerate(reqs):
        if r["ref"] == row["ref"]:
            reqs[i] = row; break
    else:
        reqs.append(row)
    save_json(REQUESTS_FILE, reqs)


# ---------- LOGIN THROTTLE (blocks password guessing) ----------
LOGIN_MAX_FAILS, LOGIN_WINDOW = 10, 15 * 60
_mem_fails: dict = {}

def client_key(req: Request):
    ip = req.headers.get("x-forwarded-for", "").split(",")[0].strip() or (req.client.host if req.client else "unknown")
    return "rl:login:" + hashlib.sha256(ip.encode()).hexdigest()[:16]

async def login_blocked(key: str):
    if USE_REDIS: return int(await redis("GET", key) or 0) >= LOGIN_MAX_FAILS
    n, until = _mem_fails.get(key, (0, 0))
    if until < time.time(): _mem_fails.pop(key, None); return False
    return n >= LOGIN_MAX_FAILS

async def login_failed(key: str):
    if USE_REDIS:
        if await redis("INCR", key) == 1: await redis("EXPIRE", key, LOGIN_WINDOW)
        return
    n, until = _mem_fails.get(key, (0, time.time() + LOGIN_WINDOW))
    _mem_fails[key] = (n + 1, until)

async def login_ok(key: str):
    if USE_REDIS: await redis("DEL", key)
    else: _mem_fails.pop(key, None)


# ---------- FILE STORAGE (private Vercel Blob on Vercel, ./uploads locally) ----------
def blob_path(owner: str, stored: str): return f"uploads/{owner}/{stored}"

def need_files():
    if IS_VERCEL and not USE_BLOB:
        raise HTTPException(503, "File storage is not configured. Connect a private Blob store to this Vercel project.")

async def file_put(owner: str, stored: str, content: bytes, ctype: Optional[str]):
    need_files()
    if USE_BLOB:
        from vercel import blob
        try:
            await blob.put_blob(blob_path(owner, stored), content, access="private", content_type=ctype or "application/octet-stream")
        except Exception as e:
            print("blob upload error:", repr(e))
            raise HTTPException(503, "Could not store the file. Please try again.")
        return
    d = UPLOAD_DIR / owner
    d.mkdir(parents=True, exist_ok=True)
    (d / stored).write_bytes(content)

async def file_size(owner: str, stored: str) -> Optional[int]:
    if USE_BLOB:
        from vercel import blob
        try:
            return (await blob.head_blob(blob_path(owner, stored))).size
        except blob.BlobNotFoundError:
            return None
    if IS_VERCEL: return None
    p = UPLOAD_DIR / owner / stored
    return p.stat().st_size if p.is_file() else None

async def file_response(owner: str, stored: str):
    shown = stored.split("_", 1)[-1]
    if USE_BLOB:
        from vercel import blob
        stack = AsyncExitStack()
        try:
            download = await stack.enter_async_context(blob.stream_blob(blob_path(owner, stored), access="private"))
        except blob.BlobError as e:
            await stack.aclose()
            if isinstance(e, blob.BlobNotFoundError): raise HTTPException(404, "File not found.")
            raise
        return StreamingResponse(
            download,
            media_type=download.metadata.content_type or "application/octet-stream",
            headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(shown), "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"},
            background=BackgroundTask(stack.aclose),
        )
    p = UPLOAD_DIR / owner / stored
    if IS_VERCEL or not p.is_file(): raise HTTPException(404, "File not found.")
    return FileResponse(p, filename=shown)


# ---------- PASSWORD - SALT + HASH, NO LENGTH RULE ----------
def hash_pw(pw: str):
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return b64url(salt) + "$" + b64url(h)

def pw_match(pw: str, stored: str):
    try:
        s_b, h_b = stored.split("$")
        salt = base64.urlsafe_b64decode(s_b + "=" * (-len(s_b) % 4))
        exp = base64.urlsafe_b64decode(h_b + "=" * (-len(h_b) % 4))
        test = hashlib.scrypt(pw.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
        return hmac.compare_digest(test, exp)
    except Exception:
        return False


# ---------- MODELS ----------
class RegisterBody(BaseModel):
    model_config = {"extra": "allow"}
    name: str = Field(min_length=2, max_length=100)
    password: str = Field(max_length=128)  # no minimum length

class LoginBody(BaseModel):
    model_config = {"extra": "allow"}
    loginId: Optional[str] = None
    name: Optional[str] = None
    email: Optional[str] = None
    role: Optional[str] = None
    password: Optional[str] = None

class RequestBody(BaseModel):
    subject: str = Field(min_length=3, max_length=200)
    pages: int = Field(ge=1, le=500)
    deadline: str
    notes: str = ""
    files: List[Any] = []  # [{name, stored, ...}] returned by /api/upload
    method: str = Field(pattern="^(mobile|instagram)$")
    contact: str = Field(min_length=3, max_length=100)
    guardian: str = ""

class QuoteBody(BaseModel):
    amount: float = Field(ge=0, le=1000000)
    delivery: date
    note: str = ""

class StatusBody(BaseModel):
    status: str

def as_api(r: dict):
    return {**r, "owner_name": r.get("by", ""), "history": r.get("hist", []), "order_id": r.get("order", "")}


# ---------- API ----------
@app.get("/api/health")
async def health():
    return {"ok": True, "db": "redis" if USE_REDIS else ("local" if not IS_VERCEL else "missing"),
            "files": "blob" if USE_BLOB else ("local" if not IS_VERCEL else "missing"), "max_upload_mb": MAX_UPLOAD_MB}

@app.post("/api/auth/register")
async def register(body: RegisterBody, res: Response):
    name = body.name.strip()
    if "@" in name:
        raise HTTPException(422, "Students use Name, not email. Admin uses email to login.")
    if not body.password:
        raise HTTPException(422, "Enter a password.")
    user_id = "user_" + uuid.uuid4().hex[:12]
    row = {"id": user_id, "name": name, "password_hash": await run_in_threadpool(hash_pw, body.password), "created_at": now_iso()}
    await users_add(row)
    user = {"id": user_id, "name": name, "role": "student"}
    set_session(res, user)
    return {"success": True, "user": user}

@app.post("/api/auth/login")
async def login(body: LoginBody, req: Request, res: Response):
    login_id = (body.loginId or body.email or body.name or "").strip()
    pwd = body.password or ""
    if not login_id: raise HTTPException(400, "Enter your name or admin email.")
    if not pwd: raise HTTPException(400, "Enter password.")
    key = client_key(req)
    if await login_blocked(key):
        raise HTTPException(429, "Too many failed attempts. Try again in 15 minutes.")
    if "@" in login_id or (body.role or "").lower() == "admin":
        ok = hmac.compare_digest(login_id.lower().encode(), ADMIN_EMAIL.lower().encode()) and hmac.compare_digest(pwd.encode(), ADMIN_PASSWORD.encode())
        if not ok:
            await login_failed(key)
            raise HTTPException(401, "Incorrect admin email or password.")
        await login_ok(key)
        user = {"id": "admin", "name": "Administrator", "email": ADMIN_EMAIL, "role": "admin"}
        set_session(res, user)
        return {"success": True, "user": user}
    candidates = [u for u in await users_all() if u["name"].lower() == login_id.lower()]
    for row in candidates:
        if await run_in_threadpool(pw_match, pwd, row.get("password_hash", "")):
            await login_ok(key)
            user = {"id": row["id"], "name": row["name"], "role": "student"}
            set_session(res, user)
            return {"success": True, "user": user}
    await login_failed(key)
    raise HTTPException(401, "Incorrect name or password.")

@app.post("/api/auth/logout")
def logout(res: Response):
    res.delete_cookie(COOKIE_NAME, path="/", httponly=True, secure=COOKIE_SECURE, samesite="lax")
    return {"success": True}

@app.get("/api/me")
def me(req: Request):
    s = read_session(req.cookies.get(COOKIE_NAME))
    if not s: return {"user": None}
    return {"user": {"id": s["sub"], "name": s["name"], "role": s["role"], "email": s.get("email", "")}}


# ---------- FILE UPLOAD ----------
@app.post("/api/upload")
async def upload_files(files: List[UploadFile] = File(...), user=Depends(current_user)):
    if user["role"] != "student":
        raise HTTPException(403, "Only students can upload.")
    if len(files) > 10:
        raise HTTPException(422, "You can upload up to 10 files at a time.")
    # validate everything first so a bad file doesn't leave earlier ones orphaned in storage
    pending = []
    for f in files:
        if not f.filename: continue
        if Path(f.filename).suffix.lower() not in ALLOWED_EXT:
            raise HTTPException(422, f"{f.filename}: this file type is not allowed.")
        content = await f.read()
        if not content:
            raise HTTPException(422, f"{f.filename} is empty.")
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(422, f"{f.filename} is too large. Max {MAX_UPLOAD_MB}MB per file.")
        pending.append((f.filename, content, f.content_type))
    if not pending:
        raise HTTPException(422, "No file received. Choose at least one file to upload.")
    saved = []
    for name, content, ctype in pending:
        safe = re.sub(r"\.{2,}", ".", re.sub(r"[^A-Za-z0-9._-]", "_", name))[:80]
        stored = f"{uuid.uuid4().hex[:10]}_{safe}"
        await file_put(user["sub"], stored, content, ctype)
        saved.append({"name": name, "stored": stored, "url": f"/uploads/{user['sub']}/{stored}", "size": len(content)})
    return {"success": True, "files": saved}

@app.get("/uploads/{owner}/{filename}")
async def serve_upload(owner: str, filename: str, user=Depends(current_user)):
    if not OWNER_RE.fullmatch(owner) or not STORED_RE.fullmatch(filename):
        raise HTTPException(404, "File not found.")
    # only the student who uploaded it, or the admin, can open a file
    if user.get("role") != "admin" and user.get("sub") != owner:
        raise HTTPException(404, "File not found.")
    return await file_response(owner, filename)


# ---------- REQUESTS ----------
@app.get("/api/requests")
async def list_requests(user=Depends(current_user)):
    items = await requests_all()
    items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    if user["role"] != "admin":
        items = [r for r in items if r.get("owner") == user["sub"]]
    return {"requests": [as_api(r) for r in items]}

@app.post("/api/requests")
async def create_request(body: RequestBody, user=Depends(current_user)):
    if user["role"] != "student": raise HTTPException(403, "Only student accounts can submit requests.")
    contact = body.contact.strip()
    if body.method == "mobile" and not re.fullmatch(r"\+?[0-9\s\-]{8,15}", contact): raise HTTPException(422, "Enter a valid phone number.")
    if body.method == "instagram" and not re.fullmatch(r"@?[A-Za-z0-9._]{1,30}", contact): raise HTTPException(422, "Enter a valid Instagram username.")
    # STRICT: at least one resource that was really uploaded by THIS student through /api/upload.
    # url and size come from the server, never from the client.
    norm_files = []
    for x in body.files[:10]:
        if not isinstance(x, dict): continue
        stored = str(x.get("stored", ""))
        if not STORED_RE.fullmatch(stored): continue
        size = await file_size(user["sub"], stored)
        if size is None: continue
        norm_files.append({"name": str(x.get("name") or stored)[:120], "url": f"/uploads/{user['sub']}/{stored}", "stored": stored, "size": size})
    if not norm_files:
        raise HTTPException(422, "Upload your assignment resource (question paper or notes). At least one file is required.")
    ref = "REQ-" + datetime.now(timezone.utc).strftime("%Y%m%d") + "-" + secrets.token_hex(3).upper()
    item = {"ref": ref, "subject": body.subject, "pages": body.pages, "deadline": body.deadline, "notes": body.notes, "files": norm_files, "method": body.method, "contact": contact, "guardian": body.guardian, "status": "Submitted", "owner": user["sub"], "by": user["name"], "quote": None, "hist": [{"to": "Submitted", "by": user["name"], "at": now_iso(), "note": ""}], "order": "", "created_at": now_iso()}
    await requests_save(item)
    return {"success": True, "request": as_api(item)}

@app.get("/api/requests/{ref}")
async def get_request(ref: str, user=Depends(current_user)):
    row = await requests_get(ref)
    if not row: raise HTTPException(404, "Request not found.")
    if user["role"] != "admin" and row.get("owner") != user["sub"]: raise HTTPException(404, "Request not found.")
    return {"request": as_api(row)}

@app.post("/api/requests/{ref}/quote")
async def send_quote(ref: str, body: QuoteBody, user=Depends(require_admin)):
    row = await requests_get(ref)
    if not row: raise HTTPException(404, "Request not found.")
    if row["status"] not in ["Submitted", "Under review"]: raise HTTPException(409, "This request is not awaiting a quotation.")
    if body.delivery < date.today(): raise HTTPException(422, "Delivery date cannot be in the past.")
    row["quote"] = {"amount": body.amount, "delivery": body.delivery.isoformat(), "note": body.note}
    row["status"] = "Quotation sent"
    row["hist"].insert(0, {"to": "Quotation sent", "by": "Administrator", "at": now_iso(), "note": f"₹{body.amount}"})
    await requests_save(row)
    return {"success": True, "request": as_api(row)}

@app.post("/api/requests/{ref}/status")
async def change_status(ref: str, body: StatusBody, user=Depends(require_admin)):
    transitions = {"Submitted": {"Under review", "Rejected"}, "Under review": {"Rejected"}, "Confirmed": {"In progress"}, "In progress": {"Ready"}, "Ready": {"Completed"}}
    row = await requests_get(ref)
    if not row: raise HTTPException(404, "Request not found.")
    if body.status == "Quotation": body.status = "Under review"
    if body.status not in transitions.get(row["status"], set()):
        if not (row["status"] in ["Submitted", "Under review"] and body.status == "Under review"):
            raise HTTPException(409, f"Cannot move from {row['status']} to {body.status}")
    row["status"] = body.status
    row["hist"].insert(0, {"to": body.status, "by": "Administrator", "at": now_iso(), "note": ""})
    await requests_save(row)
    return {"success": True, "request": as_api(row)}

@app.post("/api/requests/{ref}/decision")
async def decide(ref: str, approved: bool, user=Depends(current_user)):
    row = await requests_get(ref)
    if not row: raise HTTPException(404, "Request not found.")
    if user["role"] != "student" or row.get("owner") != user["sub"]: raise HTTPException(404, "Request not found.")
    if row["status"] != "Quotation sent": raise HTTPException(409, "No quotation is awaiting your decision.")
    if approved:
        row["status"] = "Confirmed"; row["order"] = "ORD-" + row["ref"][-6:]; note = "Quotation approved"
    else:
        row["status"] = "Cancelled"; note = "Quotation declined"
    row["hist"].insert(0, {"to": row["status"], "by": user["name"], "at": now_iso(), "note": note})
    await requests_save(row)
    return {"success": True, "request": as_api(row)}


# ---------- FRONTEND ----------
# On Vercel, everything in public/ is served by the CDN before it reaches this app.
# These routes make the same files available when running locally with `python main.py`.
def _public(name: str, media_type: Optional[str] = None):
    path = PUBLIC_DIR / name
    if not path.is_file(): raise HTTPException(404, "Not found")
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-cache"})

@app.get("/")
def frontend(): return _public("index.html")

@app.get("/skeleton.js")
def skeleton_js(): return _public("skeleton.js", "application/javascript")

@app.get("/{full_path:path}")
def spa(full_path: str):
    if full_path.startswith("api/") or full_path.startswith("uploads/"): raise HTTPException(404, "Not found")
    return _public("index.html")


if __name__ == "__main__":
    import uvicorn
    print(f"\nRunning locally -> http://127.0.0.1:8000  (db: {'redis' if USE_REDIS else 'local files'}, files: {'blob' if USE_BLOB else 'local disk'})\n")
    uvicorn.run(app, host="127.0.0.1", port=8000)
