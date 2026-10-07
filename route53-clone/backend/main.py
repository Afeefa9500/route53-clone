from contextlib import contextmanager
import hashlib
import hmac
import ipaddress
import secrets
import sqlite3
import time
from typing import Literal, Optional

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

DB = "route53.db"
SESSION_TTL = 24 * 60 * 60
RECORD_TYPES = ["A", "AAAA", "CNAME", "TXT", "MX", "NS", "PTR", "SRV", "CAA"]
ALL_TYPES = RECORD_TYPES + ["SOA"]
ALLOWED_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000"]

app = FastAPI(title="Route 53 Clone API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS hosted_zones (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL CHECK(type IN ('public','private')),
    comment TEXT NOT NULL DEFAULT '',
    vpc_id TEXT,
    created_at REAL NOT NULL,
    UNIQUE(name, type)
);
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id TEXT NOT NULL REFERENCES hosted_zones(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    ttl INTEGER NOT NULL,
    value TEXT NOT NULL,
    routing_policy TEXT NOT NULL DEFAULT 'Simple',
    created_at REAL NOT NULL,
    UNIQUE(zone_id, name, type)
);
CREATE INDEX IF NOT EXISTS idx_records_zone ON records(zone_id);
CREATE INDEX IF NOT EXISTS idx_records_name ON records(name);
CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);
"""

@contextmanager
def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    rounds = 200_000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, rounds)
    return f"pbkdf2_sha256${rounds}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt, expected = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(rounds)).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def zone_id() -> str:
    return "Z" + secrets.token_hex(9).upper()


def normalize_zone(name: str) -> str:
    value = name.strip().lower().rstrip(".")
    if not value or len(value) > 253:
        raise HTTPException(422, "Enter a valid hosted zone name.")
    labels = value.split(".")
    if any(not x or len(x) > 63 or x.startswith("-") or x.endswith("-") for x in labels):
        raise HTTPException(422, "Enter a valid hosted zone name.")
    return value + "."


def normalize_record_name(name: str, zone: str) -> str:
    value = name.strip().lower().rstrip(".")
    if value in ("", "@", zone.rstrip(".")):
        return zone
    if value.endswith("." + zone.rstrip(".")):
        return value + "."
    return value + "." + zone


def get_zone(zone_id_value: str) -> dict:
    with db() as c:
        row = c.execute("""
            SELECT z.*, COUNT(r.id) AS record_count
            FROM hosted_zones z LEFT JOIN records r ON r.zone_id=z.id
            WHERE z.id=? GROUP BY z.id
        """, (zone_id_value,)).fetchone()
    if not row:
        raise HTTPException(404, "Hosted zone not found.")
    return dict(row)


def validate_value(record_type: str, value: str) -> None:
    if not value.strip():
        raise HTTPException(422, "Record value cannot be empty.")
    lines = [x.strip() for x in value.splitlines() if x.strip()]
    if record_type == "A":
        for x in lines:
            try:
                if ipaddress.ip_address(x).version != 4: raise ValueError
            except ValueError:
                raise HTTPException(422, f"Invalid IPv4 address: {x}")
    elif record_type == "AAAA":
        for x in lines:
            try:
                if ipaddress.ip_address(x).version != 6: raise ValueError
            except ValueError:
                raise HTTPException(422, f"Invalid IPv6 address: {x}")
    elif record_type == "MX":
        for x in lines:
            p = x.split()
            if len(p) != 2 or not p[0].isdigit() or not (0 <= int(p[0]) <= 65535):
                raise HTTPException(422, "MX must be: priority hostname")
    elif record_type == "SRV":
        for x in lines:
            p = x.split()
            if len(p) != 4 or any(not v.isdigit() for v in p[:3]):
                raise HTTPException(422, "SRV must be: priority weight port target")
            if any(not (0 <= int(v) <= 65535) for v in p[:3]):
                raise HTTPException(422, "SRV priority, weight and port must be 0-65535.")
    elif record_type == "CNAME" and len(lines) != 1:
        raise HTTPException(422, "CNAME must contain exactly one value.")


def validate_conflict(c, zone: str, name: str, record_type: str, record_id: Optional[int] = None):
    params = [zone, name]
    query = "SELECT id,type FROM records WHERE zone_id=? AND name=?"
    if record_id is not None:
        query += " AND id != ?"
        params.append(record_id)
    rows = c.execute(query, params).fetchall()
    if record_type == "CNAME" and rows:
        raise HTTPException(409, f"A CNAME cannot coexist with another record at {name}.")
    if record_type != "CNAME" and any(r["type"] == "CNAME" for r in rows):
        raise HTTPException(409, f"A CNAME already exists at {name}.")


def seed_zone(c, zid: str, name: str):
    ns = [
        "ns-1024.awsdns-00.org.",
        "ns-512.awsdns-00.net.",
        "ns-1536.awsdns-00.co.uk.",
        "ns-256.awsdns-00.com.",
    ]
    now = time.time()
    c.execute("INSERT INTO records(zone_id,name,type,ttl,value,routing_policy,created_at) VALUES(?,?,?,?,?,?,?)",
              (zid, name, "NS", 172800, "\n".join(ns), "Simple", now))
    c.execute("INSERT INTO records(zone_id,name,type,ttl,value,routing_policy,created_at) VALUES(?,?,?,?,?,?,?)",
              (zid, name, "SOA", 900,
               f"{ns[0]} awsdns-hostmaster.amazon.com. 1 7200 900 1209600 86400", "Simple", now))


def init_db():
    with db() as c:
        c.executescript(SCHEMA)
        user = c.execute("SELECT id FROM users WHERE username='admin'").fetchone()
        if not user:
            c.execute("INSERT INTO users(username,password_hash) VALUES(?,?)", ("admin", password_hash("admin123")))
        if not c.execute("SELECT 1 FROM hosted_zones LIMIT 1").fetchone():
            zid = zone_id(); name = "example.com."
            c.execute("INSERT INTO hosted_zones(id,name,type,comment,vpc_id,created_at) VALUES(?,?,?,?,?,?)",
                      (zid, name, "public", "Demo hosted zone", None, time.time()))
            seed_zone(c, zid, name)
            demo = [("example.com.", "A", "192.0.2.10"), ("www.example.com.", "CNAME", "example.com."),
                    ("example.com.", "MX", "10 mail.example.com."), ("example.com.", "TXT", '"v=spf1 -all"')]
            for name2, typ, val in demo:
                c.execute("INSERT INTO records(zone_id,name,type,ttl,value,routing_policy,created_at) VALUES(?,?,?,?,?,?,?)",
                          (zid, name2, typ, 300, val, "Simple", time.time()))
        c.execute("DELETE FROM sessions WHERE expires_at <= ?", (time.time(),))


@app.on_event("startup")
def startup():
    init_db()


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


def current_user(authorization: Optional[str] = Header(None)):
    token = (authorization or "").replace("Bearer ", "", 1).strip()
    if not token:
        raise HTTPException(401, "Not authenticated.")
    with db() as c:
        row = c.execute("SELECT u.id,u.username,s.token,s.expires_at FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?", (token,)).fetchone()
        if not row:
            raise HTTPException(401, "Not authenticated.")
        if row["expires_at"] <= time.time():
            c.execute("DELETE FROM sessions WHERE token=?", (token,))
            raise HTTPException(401, "Session expired. Please sign in again.")
    return dict(row)


@app.post("/api/auth/login")
def login(body: LoginIn):
    with db() as c:
        user = c.execute("SELECT id,username,password_hash FROM users WHERE username=?", (body.username.strip(),)).fetchone()
        if not user or not verify_password(body.password, user["password_hash"]):
            raise HTTPException(401, "Your authentication information is incorrect. Please try again.")
        token = secrets.token_hex(32); now = time.time()
        c.execute("INSERT INTO sessions(token,user_id,created_at,expires_at) VALUES(?,?,?,?)",
                  (token, user["id"], now, now + SESSION_TTL))
    return {"token": token, "username": user["username"]}


@app.post("/api/auth/logout")
def logout(user=Depends(current_user)):
    with db() as c: c.execute("DELETE FROM sessions WHERE token=?", (user["token"],))
    return {"ok": True}


@app.get("/api/auth/me")
def me(user=Depends(current_user)):
    return {"username": user["username"]}


class ZoneIn(BaseModel):
    name: str = Field(min_length=1, max_length=253)
    type: Literal["public", "private"] = "public"
    comment: str = Field(default="", max_length=256)
    vpc_id: Optional[str] = Field(default=None, max_length=100)

class ZoneEdit(BaseModel):
    comment: str = Field(default="", max_length=256)


@app.get("/api/hosted-zones")
def list_zones(q: str = "", type: str = "", page: int = Query(1, ge=1), page_size: int = Query(10, ge=1, le=100), user=Depends(current_user)):
    where = ["1=1"]; args = []
    if q:
        where.append("(z.name LIKE ? OR z.id LIKE ? OR z.comment LIKE ?)"); s = f"%{q}%"; args += [s,s,s]
    if type in ("public", "private"):
        where.append("z.type=?"); args.append(type)
    cond = " AND ".join(where)
    with db() as c:
        total = c.execute(f"SELECT COUNT(*) FROM hosted_zones z WHERE {cond}", args).fetchone()[0]
        rows = c.execute(f"SELECT z.*,COUNT(r.id) record_count FROM hosted_zones z LEFT JOIN records r ON r.zone_id=z.id WHERE {cond} GROUP BY z.id ORDER BY z.name LIMIT ? OFFSET ?",
                          args + [page_size, (page-1)*page_size]).fetchall()
    return {"items":[dict(x) for x in rows], "total":total, "page":page, "page_size":page_size}


@app.get("/api/hosted-zones/{zid}")
def zone_details(zid: str, user=Depends(current_user)): return get_zone(zid)


@app.post("/api/hosted-zones", status_code=201)
def create_zone(body: ZoneIn, user=Depends(current_user)):
    name = normalize_zone(body.name)
    if body.type == "private" and not body.vpc_id:
        raise HTTPException(422, "VPC ID is required for a private hosted zone.")
    zid = zone_id()
    try:
        with db() as c:
            c.execute("INSERT INTO hosted_zones(id,name,type,comment,vpc_id,created_at) VALUES(?,?,?,?,?,?)",
                      (zid,name,body.type,body.comment.strip(),body.vpc_id.strip() if body.vpc_id else None,time.time()))
            seed_zone(c,zid,name)
    except sqlite3.IntegrityError:
        raise HTTPException(409, "A hosted zone with the same name and type already exists.")
    return get_zone(zid)


@app.put("/api/hosted-zones/{zid}")
def edit_zone(zid: str, body: ZoneEdit, user=Depends(current_user)):
    get_zone(zid)
    with db() as c: c.execute("UPDATE hosted_zones SET comment=? WHERE id=?", (body.comment.strip(),zid))
    return get_zone(zid)


@app.delete("/api/hosted-zones/{zid}")
def delete_zone(zid: str, user=Depends(current_user)):
    get_zone(zid)
    with db() as c:
        extra = c.execute("SELECT COUNT(*) FROM records WHERE zone_id=? AND type NOT IN ('NS','SOA')", (zid,)).fetchone()[0]
        if extra: raise HTTPException(400, "Delete the non-required records before deleting this hosted zone.")
        c.execute("DELETE FROM hosted_zones WHERE id=?", (zid,))
    return {"ok":True}


class RecordIn(BaseModel):
    name: str = Field(min_length=1, max_length=253)
    type: Literal["A","AAAA","CNAME","TXT","MX","NS","PTR","SRV","CAA"]
    ttl: int = Field(default=300, ge=0, le=2147483647)
    value: str = Field(min_length=1, max_length=65535)
    routing_policy: Literal["Simple","Weighted","Latency","Failover","Geolocation"] = "Simple"


@app.get("/api/hosted-zones/{zid}/records")
def list_records(zid: str, q: str = "", type: str = "", routing_policy: str = "", page: int = Query(1,ge=1), page_size: int = Query(10,ge=1,le=100), user=Depends(current_user)):
    get_zone(zid); where=["zone_id=?"]; args=[zid]
    if q:
        where.append("(name LIKE ? OR value LIKE ?)"); s=f"%{q}%"; args += [s,s]
    if type in ALL_TYPES: where.append("type=?"); args.append(type)
    if routing_policy in {"Simple","Weighted","Latency","Failover","Geolocation"}: where.append("routing_policy=?"); args.append(routing_policy)
    cond=" AND ".join(where)
    with db() as c:
        total=c.execute(f"SELECT COUNT(*) FROM records WHERE {cond}",args).fetchone()[0]
        rows=c.execute(f"SELECT * FROM records WHERE {cond} ORDER BY name,type LIMIT ? OFFSET ?",args+[page_size,(page-1)*page_size]).fetchall()
    return {"items":[dict(x) for x in rows],"total":total,"page":page,"page_size":page_size}


def write_record(zid: str, body: RecordIn, rid: Optional[int] = None):
    zone=get_zone(zid); name=normalize_record_name(body.name,zone["name"])
    if body.type == "CNAME" and name == zone["name"]: raise HTTPException(400,"A CNAME record cannot be created at the zone apex.")
    validate_value(body.type,body.value)
    with db() as c:
        if rid is not None:
            old=c.execute("SELECT * FROM records WHERE id=? AND zone_id=?",(rid,zid)).fetchone()
            if not old: raise HTTPException(404,"Record not found.")
            if old["type"] in ("NS","SOA"): raise HTTPException(403,"NS and SOA records are protected.")
        if body.type in ("NS","SOA"): raise HTTPException(403,"NS and SOA records are protected.")
        validate_conflict(c,zid,name,body.type,rid)
        try:
            if rid is None:
                cur=c.execute("INSERT INTO records(zone_id,name,type,ttl,value,routing_policy,created_at) VALUES(?,?,?,?,?,?,?)",
                               (zid,name,body.type,body.ttl,body.value.strip(),body.routing_policy,time.time()))
                rid=cur.lastrowid
            else:
                c.execute("UPDATE records SET name=?,type=?,ttl=?,value=?,routing_policy=? WHERE id=? AND zone_id=?",
                          (name,body.type,body.ttl,body.value.strip(),body.routing_policy,rid,zid))
        except sqlite3.IntegrityError:
            raise HTTPException(409,"A record with the same name and type already exists.")
        row=c.execute("SELECT * FROM records WHERE id=?",(rid,)).fetchone()
    return dict(row)


@app.post("/api/hosted-zones/{zid}/records", status_code=201)
def create_record(zid: str, body: RecordIn, user=Depends(current_user)): return write_record(zid,body)

@app.put("/api/hosted-zones/{zid}/records/{rid}")
def edit_record(zid: str, rid: int, body: RecordIn, user=Depends(current_user)): return write_record(zid,body,rid)

@app.delete("/api/hosted-zones/{zid}/records/{rid}")
def delete_record(zid: str, rid: int, user=Depends(current_user)):
    get_zone(zid)
    with db() as c:
        row=c.execute("SELECT type FROM records WHERE id=? AND zone_id=?",(rid,zid)).fetchone()
        if not row: raise HTTPException(404,"Record not found.")
        if row["type"] in ("NS","SOA"): raise HTTPException(403,"NS and SOA records are protected.")
        c.execute("DELETE FROM records WHERE id=? AND zone_id=?",(rid,zid))
    return {"ok":True}


@app.post("/api/hosted-zones/{zid}/records/bulk-delete")
def bulk_delete(zid: str, ids: list[int], user=Depends(current_user)):
    get_zone(zid)
    if not ids: return {"deleted":0}
    placeholders=",".join("?" for _ in ids)
    with db() as c:
        rows=c.execute(f"SELECT id,type FROM records WHERE zone_id=? AND id IN ({placeholders})",[zid,*ids]).fetchall()
        safe=[r["id"] for r in rows if r["type"] not in ("NS","SOA")]
        if not safe: return {"deleted":0}
        ph=",".join("?" for _ in safe); cur=c.execute(f"DELETE FROM records WHERE zone_id=? AND id IN ({ph})",[zid,*safe])
    return {"deleted":cur.rowcount}


@app.get("/api/hosted-zones/{zid}/export")
def export_zone(zid: str, format: Literal["json","bind"]="json", user=Depends(current_user)):
    zone=get_zone(zid)
    with db() as c: rows=[dict(x) for x in c.execute("SELECT * FROM records WHERE zone_id=? ORDER BY name,type",(zid,)).fetchall()]
    if format=="json": return {"zone":zone,"records":rows}
    lines=[f"$ORIGIN {zone['name']}", f"$TTL 300", ""]
    for r in rows:
        for value in r["value"].splitlines(): lines.append(f"{r['name']}\t{r['ttl']}\tIN\t{r['type']}\t{value}")
    return PlainTextResponse("\n".join(lines)+"\n", media_type="text/plain")


@app.post("/api/hosted-zones/{zid}/import-bind")
async def import_bind(zid: str, file: UploadFile = File(...), user=Depends(current_user)):
    zone=get_zone(zid); raw=await file.read()
    try: text=raw.decode("utf-8")
    except UnicodeDecodeError: raise HTTPException(400,"BIND file must be UTF-8 text.")
    imported=0; skipped=0
    for raw_line in text.splitlines():
        line=raw_line.split(";",1)[0].strip()
        if not line or line.startswith("$"): continue
        parts=line.split()
        if len(parts)<5 or parts[2].upper() not in ("IN",): continue
        name,ttl,_,typ=parts[:4]; value=" ".join(parts[4:])
        if typ not in RECORD_TYPES or typ in ("NS",): skipped+=1; continue
        try:
            model=RecordIn(name=name,type=typ,ttl=int(ttl),value=value,routing_policy="Simple")
            write_record(zid,model); imported+=1
        except Exception:
            skipped+=1
    return {"imported":imported,"skipped":skipped,"zone":zone["name"]}
