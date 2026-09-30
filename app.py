"""
สมุดบันทึกอนุบาลลูกปูทะเล (Mud Crab Nursery Log)
Flask single-file app + Tailwind CSS (CDN) + SQLite

ฟีเจอร์: บันทึกประจำวัน (คุณภาพน้ำ/วัสดุ/สุขภาพ) · รุ่นการผลิต · จับจำหน่าย
         · ประวัติ + ส่งออก CSV/JSON · สรุปต้นทุน-กำไร + กราฟ · ตั้งค่าราคา/เกณฑ์/โลโก้

วิธีรัน:
    pip install flask
    python app.py
แล้วเปิด http://127.0.0.1:5000

ย้ายข้อมูลจากไฟล์ HTML เดิม: ในไฟล์ HTML เดิมกด "สำรองข้อมูล (JSON)"
แล้วนำไฟล์ไปกด "นำเข้าข้อมูลสำรอง" ในแท็บประวัติของแอปนี้
"""

import copy
import json
import os
import re
import sqlite3
from datetime import date, datetime

from flask import Flask, Response, g, jsonify, request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("CRAB_DB", os.path.join(BASE_DIR, "crab_nursery.db"))

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # รองรับไฟล์นำเข้า/โลโก้
app.json.ensure_ascii = False

# ---------------------------------------------------------------------------
# ค่าเริ่มต้น
# ---------------------------------------------------------------------------

# [เหมาะสมต่ำสุด, เหมาะสมสูงสุด, เฝ้าระวังต่ำสุด, เฝ้าระวังสูงสุด]
DEFAULT_WATER = {
    "temp": [27.5, 29.5, 26.5, 30.5],
    "sal": [27, 29, 25, 31],
    "ph": [7.7, 7.9, 7.5, 8.1],
    "do": [6, 8.5, 5, 9.5],
    "nh3": [0, 0.2, 0, 0.3],
    "no2": [0, 0.3, 0, 0.5],
    "alk": [100, 120, 90, 130],
}

DEFAULT_MATERIALS = [
    {"id": "m_chl", "name": "คลอเรลลา / แพลงก์ตอนพืช", "cat": "อาหาร", "unit": "ลิตร", "price": None},
    {"id": "m_rot", "name": "โรติเฟอร์", "cat": "อาหาร", "unit": "ลิตร", "price": None},
    {"id": "m_art", "name": "ไข่อาร์ทีเมีย", "cat": "อาหาร", "unit": "กรัม", "price": None},
    {"id": "m_pel", "name": "อาหารสำเร็จรูป", "cat": "อาหาร", "unit": "กรัม", "price": None},
    {"id": "m_fresh", "name": "เนื้อสด / หอย / เคย", "cat": "อาหาร", "unit": "กรัม", "price": None},
    {"id": "m_prob", "name": "จุลินทรีย์", "cat": "จุลินทรีย์/สารเคมี", "unit": "กรัม", "price": None},
    {"id": "m_chem", "name": "สารฆ่าเชื้อ / ยา", "cat": "จุลินทรีย์/สารเคมี", "unit": "มล.", "price": None},
]

DEFAULT_SIZES = [
    {"id": "z1", "name": "ระยะเมกาโลปา", "price": 0.5},
    {"id": "z2", "name": "ขนาด 0.30–0.49 ซม.", "price": 1},
    {"id": "z3", "name": "ขนาด 0.50–0.79 ซม.", "price": 2},
    {"id": "z4", "name": "ขนาด 0.80–1.29 ซม.", "price": 3},
    {"id": "z5", "name": "ขนาด 1.30–1.79 ซม.", "price": 5},
    {"id": "z6", "name": "ขนาด 2.50–3.00 ซม.", "price": 10},
    {"id": "z7", "name": "ขนาด 5.00–6.00 ซม.", "price": 15},
]

DEFAULT_ORG = {"name": "", "sub": "", "zoom": 100, "bg": "#ffffff"}

# ชนิดปู: ชื่อไทยอ้างอิงตามชื่อวิทยาศาสตร์ (ใช้แปลงข้อมูลที่บันทึกด้วยชื่อไทยแบบเดิม)
SPECIES_BY_SCI = {
    "S. olivacea": "ปูดำ (S. olivacea)",
    "S. serrata": "ปูเขียว (S. serrata)",
    "S. tranquebarica": "ปูม่วง (S. tranquebarica)",
    "S. paramamosain": "ปูขาว (S. paramamosain)",
}


def migrate_batch(batch):
    """เปลี่ยนชื่อชนิดเดิม (เช่น ปูทองหลาง (S. olivacea)) เป็นชื่อปัจจุบัน คืนค่า True ถ้ามีการเปลี่ยน"""
    sp = batch.get("species")
    m = re.search(r"\((S\. [a-z]+)\)", sp) if isinstance(sp, str) else None
    new = SPECIES_BY_SCI.get(m.group(1)) if m else None
    if new and new != sp:
        batch["species"] = new
        return True
    return False


def default_settings():
    return {
        "water": copy.deepcopy(DEFAULT_WATER),
        "materials": copy.deepcopy(DEFAULT_MATERIALS),
        "sizes": copy.deepcopy(DEFAULT_SIZES),
        "sizesVer": 2,
    }


# ---------------------------------------------------------------------------
# ฐานข้อมูล (แต่ละรายการเก็บเป็น JSON เพื่อให้โครงสร้างยืดหยุ่นเหมือนต้นฉบับ)
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id   TEXT PRIMARY KEY,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS logs (
    id       TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    data     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS harvests (
    id       TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    data     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_batch ON logs(batch_id);
CREATE INDEX IF NOT EXISTS idx_harvests_batch ON harvests(batch_id);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False)


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(SCHEMA)
        defaults = {"settings": default_settings(), "org": DEFAULT_ORG}
        for key, value in defaults.items():
            conn.execute("INSERT OR IGNORE INTO kv(key, value) VALUES (?, ?)", (key, dumps(value)))
        # แปลงชื่อชนิดปูแบบเดิมในรุ่นที่บันทึกไว้แล้วให้เป็นชื่อปัจจุบัน
        for bid, data in conn.execute("SELECT id, data FROM batches").fetchall():
            b = json.loads(data)
            if migrate_batch(b):
                conn.execute("UPDATE batches SET data = ? WHERE id = ?", (dumps(b), bid))
        conn.commit()
    finally:
        conn.close()


def kv_get(key, default=None):
    row = get_db().execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row and row["value"] is not None else default


def kv_set(key, value):
    get_db().execute(
        "INSERT INTO kv(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, dumps(value)),
    )


def kv_del(key):
    get_db().execute("DELETE FROM kv WHERE key = ?", (key,))


def load_rows(table, newest_first=False):
    order = "DESC" if newest_first else "ASC"
    rows = get_db().execute(f"SELECT data FROM {table} ORDER BY rowid {order}")
    return [json.loads(r["data"]) for r in rows]


def upsert_batch(item):
    get_db().execute(
        "INSERT INTO batches(id, data) VALUES (?, ?) "
        "ON CONFLICT(id) DO UPDATE SET data = excluded.data",
        (item["id"], dumps(item)),
    )


def upsert_child(table, item):
    get_db().execute(
        f"INSERT INTO {table}(id, batch_id, data) VALUES (?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET batch_id = excluded.batch_id, data = excluded.data",
        (item["id"], item.get("batch") or "", dumps(item)),
    )


def batch_exists(bid):
    return get_db().execute("SELECT 1 FROM batches WHERE id = ?", (bid,)).fetchone() is not None


def get_settings():
    s = kv_get("settings") or default_settings()
    s["water"] = {**copy.deepcopy(DEFAULT_WATER), **(s.get("water") or {})}
    s.setdefault("materials", [])
    s.setdefault("sizes", [])
    return s


def full_state():
    return {
        "batches": load_rows("batches", newest_first=True),
        "logs": load_rows("logs"),
        "harvests": load_rows("harvests"),
        "settings": get_settings(),
        "org": {**DEFAULT_ORG, **(kv_get("org") or {})},
        "logo": kv_get("logo"),
        "defaultWater": DEFAULT_WATER,
    }


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def bad_request(msg, code=400):
    return jsonify(error=msg), code


def body_dict():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


def is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def valid_date(v):
    if not isinstance(v, str) or not DATE_RE.match(v):
        return False
    try:
        datetime.strptime(v, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def valid_logo(v):
    return isinstance(v, str) and v.startswith("data:image/") and len(v) < 8 * 1024 * 1024


def migrate_log(log):
    """แปลงบันทึกรูปแบบแรกของไฟล์ HTML (feed -> mats) ให้เป็นรูปแบบปัจจุบัน"""
    feed = log.get("feed")
    if isinstance(feed, dict) and not log.get("mats"):
        mats = {}
        for old, new in (("algae", "m_chl"), ("pellet", "m_pel"), ("fresh", "m_fresh")):
            if feed.get(old) is not None:
                mats[new] = feed[old]
        log["mats"] = mats
        log["denrot"] = feed.get("rotifer")
        log["denart"] = feed.get("artemia")
        log["meals"] = feed.get("meals")
        del log["feed"]
    return log


def merge_by_id(current, incoming):
    merged = {x["id"]: x for x in current if isinstance(x, dict) and "id" in x}
    for x in incoming or []:
        if isinstance(x, dict) and isinstance(x.get("id"), str):
            merged[x["id"]] = x
    return list(merged.values())


def clean_org(d):
    org = {**DEFAULT_ORG, **(kv_get("org") or {})}
    if isinstance(d.get("name"), str):
        org["name"] = d["name"][:200]
    if isinstance(d.get("sub"), str):
        org["sub"] = d["sub"][:300]
    if is_num(d.get("zoom")):
        org["zoom"] = max(40, min(200, int(d["zoom"])))
    if d.get("bg") in ("#ffffff", "transparent"):
        org["bg"] = d["bg"]
    return org


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.get("/api/state")
def api_state():
    return jsonify(full_state())


# ---- รุ่นการผลิต ----
@app.put("/api/batches/<bid>")
def api_put_batch(bid):
    b = body_dict()
    if not ID_RE.match(bid) or b is None:
        return bad_request("ข้อมูลไม่ถูกต้อง")
    b["id"] = bid
    b["code"] = str(b.get("code") or "").strip()
    if not b["code"]:
        return bad_request("กรุณาระบุรหัสรุ่น")
    if not valid_date(b.get("hatch")):
        return bad_request("วันที่ฟักไม่ถูกต้อง")
    if not is_num(b.get("init")) or b["init"] <= 0:
        return bad_request("จำนวนเริ่มต้นต้องมากกว่า 0")
    if any(x.get("code") == b["code"] and x.get("id") != bid for x in load_rows("batches")):
        return bad_request("รหัสรุ่นนี้มีอยู่แล้ว", 409)
    migrate_batch(b)
    upsert_batch(b)
    get_db().commit()
    return jsonify(b)


@app.delete("/api/batches/<bid>")
def api_del_batch(bid):
    db = get_db()
    db.execute("DELETE FROM logs WHERE batch_id = ?", (bid,))
    db.execute("DELETE FROM harvests WHERE batch_id = ?", (bid,))
    db.execute("DELETE FROM batches WHERE id = ?", (bid,))
    db.commit()
    return jsonify(ok=True)


# ---- บันทึกประจำวัน ----
@app.put("/api/logs/<lid>")
def api_put_log(lid):
    r = body_dict()
    if not ID_RE.match(lid) or r is None:
        return bad_request("ข้อมูลไม่ถูกต้อง")
    if not valid_date(r.get("date")):
        return bad_request("กรุณาระบุวันที่")
    if not batch_exists(r.get("batch")):
        return bad_request("ไม่พบรุ่นการผลิตที่เลือก")
    r["id"] = lid
    r["updated"] = datetime.now().isoformat(timespec="seconds")
    upsert_child("logs", r)
    get_db().commit()
    return jsonify(r)


@app.delete("/api/logs/<lid>")
def api_del_log(lid):
    db = get_db()
    db.execute("DELETE FROM logs WHERE id = ?", (lid,))
    db.commit()
    return jsonify(ok=True)


# ---- จับจำหน่าย ----
@app.put("/api/harvests/<hid>")
def api_put_harvest(hid):
    h = body_dict()
    if not ID_RE.match(hid) or h is None:
        return bad_request("ข้อมูลไม่ถูกต้อง")
    if not valid_date(h.get("date")):
        return bad_request("กรุณาระบุวันที่จับ")
    if not batch_exists(h.get("batch")):
        return bad_request("ไม่พบรุ่นการผลิตที่เลือก")
    if not isinstance(h.get("items"), list) or not h["items"]:
        return bad_request("กรุณากรอกจำนวนปูที่จับได้อย่างน้อย 1 ขนาด")
    h["id"] = hid
    upsert_child("harvests", h)
    get_db().commit()
    return jsonify(h)


@app.delete("/api/harvests/<hid>")
def api_del_harvest(hid):
    db = get_db()
    db.execute("DELETE FROM harvests WHERE id = ?", (hid,))
    db.commit()
    return jsonify(ok=True)


# ---- ตั้งค่า / หน่วยงาน / โลโก้ ----
@app.put("/api/settings")
def api_put_settings():
    s = body_dict()
    if (s is None or not isinstance(s.get("water"), dict)
            or not isinstance(s.get("materials"), list) or not isinstance(s.get("sizes"), list)):
        return bad_request("รูปแบบการตั้งค่าไม่ถูกต้อง")
    kv_set("settings", s)
    get_db().commit()
    return jsonify(get_settings())


@app.put("/api/org")
def api_put_org():
    d = body_dict()
    if d is None:
        return bad_request("ข้อมูลไม่ถูกต้อง")
    org = clean_org(d)
    kv_set("org", org)
    get_db().commit()
    return jsonify(org)


@app.put("/api/logo")
def api_put_logo():
    d = body_dict()
    if d is None or not valid_logo(d.get("data")):
        return bad_request("ไฟล์โลโก้ไม่ถูกต้อง")
    kv_set("logo", d["data"])
    get_db().commit()
    return jsonify(ok=True)


@app.delete("/api/logo")
def api_del_logo():
    kv_del("logo")
    get_db().commit()
    return jsonify(ok=True)


# ---- สำรอง / นำเข้า ----
@app.get("/api/export")
def api_export():
    s = full_state()
    payload = {
        "app": "crab-nursery", "version": 2,
        "exported": datetime.now().isoformat(timespec="seconds"),
        "settings": s["settings"], "org": s["org"], "logo": s["logo"],
        "batches": s["batches"], "logs": s["logs"], "harvests": s["harvests"],
    }
    return Response(
        json.dumps(payload, ensure_ascii=False, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename=crab-nursery-backup-{date.today()}.json"},
    )


@app.post("/api/import")
def api_import():
    d = body_dict()
    if d is None or not isinstance(d.get("batches"), list) or not isinstance(d.get("logs"), list):
        return bad_request("ไฟล์ไม่ถูกต้อง")

    def ok_item(x):
        return isinstance(x, dict) and isinstance(x.get("id"), str) and ID_RE.match(x["id"])

    for b in d["batches"]:
        if ok_item(b):
            migrate_batch(b)
            upsert_batch(b)
    for lg in d["logs"]:
        if ok_item(lg):
            upsert_child("logs", migrate_log(lg))
    for h in d.get("harvests") or []:
        if ok_item(h):
            upsert_child("harvests", h)

    src = d.get("settings")
    if d.get("useSettings") and isinstance(src, dict):
        s = get_settings()
        s["materials"] = merge_by_id(s["materials"], src.get("materials"))
        s["sizes"] = merge_by_id(s["sizes"], src.get("sizes"))
        if isinstance(src.get("water"), dict):
            s["water"].update({k: v for k, v in src["water"].items()
                               if isinstance(v, list) and len(v) == 4 and all(is_num(n) for n in v)})
        kv_set("settings", s)
    if isinstance(d.get("org"), dict):
        kv_set("org", clean_org(d["org"]))
    if valid_logo(d.get("logo")):
        kv_set("logo", d["logo"])

    get_db().commit()
    return jsonify(full_state())


# ---------------------------------------------------------------------------
# หน้าเว็บ (HTML + Tailwind CDN + JavaScript ในไฟล์เดียว)
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return Response(PAGE_HTML, mimetype="text/html")


PAGE_HTML = r"""<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>สมุดบันทึกอนุบาลลูกปูทะเล</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Sarabun:wght@400;500;600;700&family=Prompt:wght@500;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.tailwindcss.com"></script>
<script>
tailwind.config={theme:{extend:{
  colors:{
    bg:'#eef5f7', ink:'#15313a', muted:'#5d7780', line:'#d6e4e8',
    pri:{DEFAULT:'#0e7c86',2:'#0a5f67',soft:'#dff1f2'},
    crab:{DEFAULT:'#e0603a',soft:'#fdebe4'},
    ok:{DEFAULT:'#2e9b5f',soft:'#e2f5ea'},
    warn:{DEFAULT:'#d99a12',soft:'#fdf3dc'},
    bad:{DEFAULT:'#d23c3c',soft:'#fbe4e4'}
  },
  fontFamily:{sans:['Sarabun','system-ui','sans-serif'],head:['Prompt','Sarabun','sans-serif']},
  boxShadow:{card:'0 6px 24px rgba(16,60,72,.08)'},
  keyframes:{fade:{from:{opacity:'0',transform:'translateY(6px)'},to:{opacity:'1',transform:'none'}}},
  animation:{fade:'fade .25s'}
}}}
</script>
<style type="text/tailwindcss">
@layer base{
  body{@apply bg-bg font-sans text-base leading-normal text-ink;}
  h1,h2,h3{@apply font-head;}
  input:where(:not([type=radio],[type=checkbox],[type=range],[type=file])),select,textarea{
    @apply w-full rounded-[10px] border-[1.5px] border-line bg-[#f7fbfc] px-3 py-2.5 text-ink transition
           focus:border-pri focus:bg-white focus:outline-none focus:ring-[3px] focus:ring-pri/15;}
  textarea{@apply min-h-[80px] resize-y;}
  input[type=range]{@apply w-full accent-pri;}
  i{@apply italic;}
}
@layer components{
  .tab{@apply flex min-w-max flex-1 cursor-pointer items-center justify-center gap-1.5 rounded-[10px] px-3.5 py-2.5 font-head text-[.95rem] font-semibold text-muted transition hover:bg-pri-soft hover:text-pri-2;}
  .tab.active{@apply bg-pri text-white hover:bg-pri hover:text-white;}
  .panel{@apply hidden;}
  .panel.active{@apply block animate-fade;}

  .card{@apply mb-[18px] rounded-2xl bg-white p-5 shadow-card max-sm:p-4 print:border print:border-gray-300 print:shadow-none;}
  .card-h{@apply mb-3.5 flex items-center gap-2.5;}
  .card-h h2{@apply text-[1.08rem] font-semibold;}
  .card-h small{@apply block font-sans text-sm font-normal text-muted;}
  .ic{@apply grid h-[34px] w-[34px] flex-none place-items-center rounded-[10px] bg-pri-soft text-pri;}
  .ic.crab{@apply bg-crab-soft text-crab;}

  .fgrid{@apply grid grid-cols-[repeat(auto-fit,minmax(170px,1fr))] gap-3.5;}
  .fgrid.g2{@apply grid-cols-[repeat(auto-fit,minmax(240px,1fr))];}
  .field{@apply flex flex-col gap-[5px];}
  .field.full{@apply col-span-full;}
  .field>label,.lbl{@apply text-[.9rem] font-semibold text-[#29474f];}
  .unit{@apply font-normal text-muted;}
  .req{@apply text-crab;}
  .hint{@apply min-h-[1em] text-[.8rem] text-muted;}
  .hint.warn{@apply text-[#9a6a00];}
  .hint.bad{@apply text-bad;}
  .hint.ok{@apply text-ok;}
  input.st-ok{@apply border-ok bg-ok-soft;}
  input.st-warn{@apply border-warn bg-warn-soft;}
  input.st-bad{@apply border-bad bg-bad-soft;}

  .spbtn{@apply flex w-full items-center justify-between gap-2 rounded-[10px] border-[1.5px] border-line bg-[#f7fbfc] px-3 py-2.5 text-left text-ink transition
                focus:border-pri focus:bg-white focus:outline-none focus:ring-[3px] focus:ring-pri/15 aria-expanded:border-pri aria-expanded:bg-white;}
  .spopt{@apply cursor-pointer px-3 py-2 data-[active=true]:bg-pri-soft aria-selected:font-semibold aria-selected:text-pri-2;}

  .chips{@apply flex flex-wrap gap-2;}
  .chip{@apply relative;}
  .chip input{@apply pointer-events-none absolute opacity-0;}
  .chip span{@apply inline-block cursor-pointer select-none rounded-full border-[1.5px] border-line bg-[#f7fbfc] px-3.5 py-[7px] text-[.92rem] transition;}
  .chip input:checked+span{@apply border-pri bg-pri text-white;}
  .chip input:focus-visible+span{@apply ring-[3px] ring-pri/25;}

  .autobox{@apply mt-3 flex flex-wrap gap-3;}
  .auto{@apply min-w-[150px] flex-1 rounded-xl bg-pri-soft px-3.5 py-2.5;}
  .auto b{@apply block font-head text-[1.35rem] font-bold text-pri-2;}
  .auto small{@apply text-muted;}
  .auto.crab{@apply bg-crab-soft;}  .auto.crab b{@apply text-crab;}
  .auto.good{@apply bg-ok-soft;}    .auto.good b{@apply text-ok;}
  .auto.loss{@apply bg-bad-soft;}   .auto.loss b{@apply text-bad;}

  .btn{@apply inline-flex cursor-pointer items-center gap-2 rounded-[10px] border-0 px-[18px] py-[11px] font-head text-[.95rem] font-semibold transition disabled:cursor-wait disabled:opacity-60;}
  .btn-pri{@apply bg-pri text-white hover:bg-pri-2;}
  .btn-crab{@apply bg-crab text-white hover:brightness-95;}
  .btn-ghost{@apply border-[1.5px] border-solid border-line bg-white text-pri-2 hover:border-pri;}
  .btn-danger{@apply bg-bad-soft text-bad hover:bg-bad hover:text-white;}
  .btn-sm{@apply rounded-lg px-2.5 py-1.5 text-[.85rem];}
  .actions{@apply sticky bottom-3 flex flex-wrap justify-end gap-2.5 rounded-[14px] bg-bg/90 p-2.5 backdrop-blur print:hidden;}
  .actions .btn{@apply max-sm:flex-1 max-sm:justify-center;}

  .stat{@apply rounded-[14px] bg-white p-4 shadow-card print:border print:shadow-none;}
  .stat small{@apply text-[.85rem] text-muted;}
  .stat b{@apply mt-0.5 block font-head text-2xl font-bold text-pri-2;}
  .stat.crab b{@apply text-crab;}
  .stat.good b{@apply text-ok;}
  .stat.loss b{@apply text-bad;}

  .toolbar{@apply mb-3.5 flex flex-wrap items-center gap-2.5;}
  .toolbar input,.toolbar select{@apply w-auto min-w-[140px] flex-1;}
  .tbl-wrap{@apply overflow-x-auto rounded-xl border border-line;}
  .tbl{@apply w-full min-w-[820px] border-collapse text-[.9rem];}
  .tbl.compact{@apply min-w-[560px];}
  .tbl th,.tbl td{@apply whitespace-nowrap border-b border-line px-3 py-2.5 text-left;}
  .tbl th{@apply bg-[#f3f9fa] font-semibold text-[#29474f];}
  .tbl tbody tr:hover td{@apply bg-[#f9fcfd];}
  .tbl .num{@apply text-right tabular-nums;}
  .tbl tfoot td{@apply bg-[#f3f9fa] font-bold;}
  .tbl td input,.tbl td select{@apply rounded-lg px-2 py-1.5;}

  .badge{@apply inline-block rounded-full px-2.5 py-0.5 text-[.8rem] font-semibold;}
  .b-z{@apply bg-[#e5f0ff] text-[#2556a8];}
  .b-m{@apply bg-[#f1e7ff] text-[#6a3bb5];}
  .b-c{@apply bg-crab-soft text-crab;}
  .b-ok{@apply bg-ok-soft text-ok;}
  .b-warn{@apply bg-warn-soft text-[#9a6a00];}
  .b-bad{@apply bg-bad-soft text-bad;}
  .pos{@apply text-ok;}
  .neg{@apply text-bad;}

  .note{@apply mt-3 rounded-[10px] bg-warn-soft px-3.5 py-2.5 text-[.88rem] text-[#7a5600];}
  .note.info{@apply bg-pri-soft text-pri-2;}
  .note a{@apply font-semibold underline;}

  .orglogo{@apply relative grid h-16 w-16 flex-none place-items-center overflow-hidden rounded-full bg-white shadow-[0_2px_10px_rgba(0,0,0,.15)];}
  .orglogo img{@apply h-full w-full origin-center object-contain;}
  .orgname{@apply mb-0.5 font-head text-[.95rem] font-semibold opacity-95;}
  .orgname small{@apply block font-sans text-[.82rem] font-normal opacity-85;}

  .sizecols{@apply grid grid-cols-[1.4fr_1fr_1fr_1fr] items-center gap-2.5 max-sm:grid-cols-2;}

  .dl{@apply my-3 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5;}
  .dl dt{@apply text-muted;}
  .dl dd{@apply m-0 font-medium;}
  .no-print{@apply print:hidden;}
}
</style>
</head>
<body>

<header class="relative overflow-hidden bg-gradient-to-br from-[#0a5f67] via-[#0e7c86] to-[#139aa0] px-4 pb-[70px] pt-[22px] text-white print:hidden">
  <div class="mx-auto flex max-w-[1100px] items-center gap-3.5">
    <div class="grid h-[54px] w-[54px] flex-none place-items-center rounded-[14px] bg-white/15">
      <svg width="34" height="34" viewBox="0 0 64 64" fill="none" stroke="#fff" stroke-width="3" stroke-linecap="round" stroke-linejoin="round">
        <ellipse cx="32" cy="38" rx="16" ry="11" fill="rgba(255,255,255,.25)"/>
        <path d="M24 29l-6-10M40 29l6-10"/><circle cx="17" cy="16" r="5"/><circle cx="47" cy="16" r="5"/>
        <path d="M17 38l-9-4M17 43l-9 2M18 47l-7 6M47 38l9-4M47 43l9 2M46 47l7 6"/>
        <circle cx="27" cy="34" r="1.5" fill="#fff"/><circle cx="37" cy="34" r="1.5" fill="#fff"/>
      </svg>
    </div>
    <button type="button" class="orglogo group cursor-pointer" id="orgLogo" title="ตั้งค่าโลโก้และชื่อหน่วยงาน"
            onclick="showTab('settings');setTimeout(()=>$('orgCard').scrollIntoView({behavior:'smooth'}),50)">
      <img id="orgLogoImg" alt="โลโก้หน่วยงาน" hidden>
      <span id="orgLogoPh" class="text-center text-[.7rem] font-semibold leading-tight text-pri-2">＋<br>โลโก้</span>
      <span class="absolute inset-0 grid place-items-center bg-[rgba(10,60,70,.55)] text-xs text-white opacity-0 transition group-hover:opacity-100">เปลี่ยน</span>
    </button>
    <div>
      <div class="orgname" id="orgNameH" hidden></div>
      <h1 class="text-[1.45rem] max-sm:text-[1.2rem]">สมุดบันทึกอนุบาลลูกปูทะเล</h1>
      <p class="mt-0.5 text-[.95rem] opacity-85">คุณภาพน้ำ · วัสดุและต้นทุน · จับจำหน่ายและกำไร · ข้อมูลเก็บในฐานข้อมูล SQLite</p>
    </div>
  </div>
  <svg class="absolute -bottom-px left-0 h-[50px] w-full" viewBox="0 0 1440 50" preserveAspectRatio="none"><path fill="#eef5f7" d="M0,30 C240,60 480,0 720,20 C960,40 1200,10 1440,30 L1440,50 L0,50 Z"/></svg>
</header>

<main class="relative mx-auto -mt-12 max-w-[1100px] px-4 pb-16 print:m-0">
  <nav class="mb-[18px] flex gap-1.5 overflow-x-auto rounded-[14px] bg-white p-1.5 shadow-card print:hidden">
    <button class="tab active" data-tab="daily">📝 บันทึกประจำวัน</button>
    <button class="tab" data-tab="batches">🦀 รุ่นการผลิต</button>
    <button class="tab" data-tab="harvest">💰 จับจำหน่าย</button>
    <button class="tab" data-tab="history">📋 ประวัติ</button>
    <button class="tab" data-tab="summary">📊 สรุปต้นทุน-กำไร</button>
    <button class="tab" data-tab="settings">⚙️ ตั้งค่าราคา/เกณฑ์</button>
  </nav>

  <div id="loadErr"></div>

  <!-- ================= DAILY ================= -->
  <section class="panel active" id="p-daily">
    <form id="dailyForm" autocomplete="off">
      <input type="hidden" id="d_id">

      <div class="card">
        <div class="card-h"><div class="ic">📅</div><div><h2>ข้อมูลทั่วไป</h2><small>เลือกรุ่นเพื่อคำนวณอายุ อัตรารอด และต้นทุนสะสมอัตโนมัติ</small></div></div>
        <div class="fgrid">
          <div class="field"><label>วันที่ <span class="req">*</span></label><input type="date" id="d_date" required></div>
          <div class="field"><label>เวลา</label><input type="time" id="d_time"></div>
          <div class="field"><label>รุ่นการผลิต <span class="req">*</span></label><select id="d_batch" required></select></div>
          <div class="field"><label>บ่อ / ถังอนุบาล <span class="req">*</span></label><input id="d_tank" list="tankList" placeholder="เช่น T-01" required><datalist id="tankList"></datalist></div>
          <div class="field"><label>ผู้บันทึก</label><input id="d_recorder" list="recList" placeholder="ชื่อผู้บันทึก"><datalist id="recList"></datalist></div>
        </div>
        <div class="field mt-3.5"><label>ระยะพัฒนาการ</label>
          <div class="chips" id="stageChips"></div>
        </div>
        <div class="autobox">
          <div class="auto"><small>อายุหลังฟัก</small><b id="a_dph">–</b></div>
          <div class="auto crab"><small>อัตรารอดสะสม</small><b id="a_surv">–</b></div>
          <div class="auto"><small>จำนวนเริ่มต้นของรุ่น</small><b id="a_init">–</b></div>
        </div>
      </div>

      <div class="card">
        <div class="card-h"><div class="ic">💧</div><div><h2>คุณภาพน้ำ</h2><small>ช่องจะเปลี่ยนสีตามเกณฑ์ที่ตั้งไว้ในแท็บตั้งค่า</small></div></div>
        <div class="fgrid" id="waterGrid"></div>
        <div class="fgrid mt-3.5">
          <div class="field"><label>เปลี่ยนถ่ายน้ำ <span class="unit">(%)</span></label><input type="number" id="d_exchange" min="0" max="100" step="1"></div>
          <div class="field"><label>สีน้ำ / ความใส</label>
            <select id="d_watercolor"><option value="">— เลือก —</option><option>ใส</option><option>เขียวอ่อน</option><option>เขียวเข้ม</option><option>น้ำตาล</option><option>ขุ่น</option></select></div>
          <div class="field"><label>สภาพพื้น/ตะกอน</label>
            <select id="d_bottom"><option value="">— เลือก —</option><option>สะอาด</option><option>มีเศษอาหารเล็กน้อย</option><option>ตะกอนมาก</option><option>มีกลิ่น</option></select></div>
        </div>
      </div>

      <div class="card">
        <div class="card-h"><div class="ic crab">🦐</div><div><h2>การใช้วัสดุ อาหาร และสารต่าง ๆ</h2><small>กรอกปริมาณที่ใช้วันนี้ · เพิ่มรายการหรือแก้ราคาได้ที่แท็บตั้งค่า</small></div></div>
        <div class="fgrid" id="matGrid"></div>
        <div class="autobox">
          <div class="auto crab"><small>ต้นทุนวัสดุวันนี้</small><b id="a_daycost">–</b></div>
          <div class="auto"><small>ต้นทุนวัสดุสะสมของรุ่น (รวมวันนี้)</small><b id="a_cumcost">–</b></div>
        </div>
        <div class="fgrid mt-3.5">
          <div class="field"><label>ความหนาแน่นโรติเฟอร์ในถัง <span class="unit">(ตัว/มล.)</span></label><input type="number" id="d_denrot" min="0" step="0.1"></div>
          <div class="field"><label>ความหนาแน่นอาร์ทีเมียในถัง <span class="unit">(ตัว/มล.)</span></label><input type="number" id="d_denart" min="0" step="0.1"></div>
          <div class="field"><label>จำนวนมื้อ <span class="unit">(มื้อ/วัน)</span></label><input type="number" id="d_meals" min="0" step="1"></div>
        </div>
        <div class="field mt-3.5"><label>การกินอาหาร</label>
          <div class="chips">
            <label class="chip"><input type="radio" name="appetite" value="ดี"><span>😋 กินดี</span></label>
            <label class="chip"><input type="radio" name="appetite" value="ปกติ"><span>🙂 ปกติ</span></label>
            <label class="chip"><input type="radio" name="appetite" value="น้อย"><span>😕 กินน้อย</span></label>
            <label class="chip"><input type="radio" name="appetite" value="ไม่กิน"><span>🚫 ไม่กิน</span></label>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-h"><div class="ic crab">🦀</div><div><h2>จำนวนและสุขภาพ</h2><small>ประมาณจำนวนคงเหลือจากการสุ่มนับ</small></div></div>
        <div class="fgrid">
          <div class="field"><label>จำนวนคงเหลือโดยประมาณ <span class="unit">(ตัว)</span></label><input type="number" id="d_count" min="0" step="1"></div>
          <div class="field"><label>ตายที่พบ <span class="unit">(ตัว)</span></label><input type="number" id="d_dead" min="0" step="1"></div>
          <div class="field"><label>ลอกคราบ</label>
            <select id="d_molt"><option value="">— เลือก —</option><option>ไม่พบ</option><option>พบบางส่วน</option><option>พบมาก</option><option>ลอกคราบไม่ผ่าน</option></select></div>
          <div class="field"><label>กินกันเอง</label>
            <select id="d_cannibal"><option value="">— เลือก —</option><option>ไม่พบ</option><option>พบเล็กน้อย</option><option>พบมาก</option></select></div>
          <div class="field"><label>ความกว้างกระดอง <span class="unit">(มม.)</span></label><input type="number" id="d_cw" min="0" step="0.1"></div>
        </div>
        <div class="field mt-3.5"><label>อาการผิดปกติที่พบ</label>
          <div class="chips">
            <label class="chip"><input type="checkbox" name="symptom" value="ว่ายน้ำผิดปกติ"><span>ว่ายน้ำผิดปกติ</span></label>
            <label class="chip"><input type="checkbox" name="symptom" value="ตัวขุ่น/ขาว"><span>ตัวขุ่น/ขาว</span></label>
            <label class="chip"><input type="checkbox" name="symptom" value="มีเมือก/ตะไคร่เกาะ"><span>มีเมือก/ตะไคร่เกาะ</span></label>
            <label class="chip"><input type="checkbox" name="symptom" value="ลำไส้ว่าง"><span>ลำไส้ว่าง</span></label>
            <label class="chip"><input type="checkbox" name="symptom" value="เรืองแสง"><span>เรืองแสง</span></label>
            <label class="chip"><input type="checkbox" name="symptom" value="จมก้นถัง"><span>จมก้นถัง</span></label>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-h"><div class="ic">🧪</div><div><h2>การจัดการและหมายเหตุ</h2></div></div>
        <div class="fgrid g2">
          <div class="field"><label>รายละเอียดสาร/ยาที่ใช้</label><input id="d_treat" placeholder="ชื่อผลิตภัณฑ์ วิธีใช้"></div>
          <div class="field"><label>งานอื่น ๆ</label><input id="d_task" placeholder="เช่น ล้างถัง, คัดขนาด, ย้ายบ่อ"></div>
          <div class="field full"><label>หมายเหตุ</label><textarea id="d_note" placeholder="สิ่งที่สังเกตเห็น ปัญหา หรือแผนวันพรุ่งนี้"></textarea></div>
        </div>
      </div>

      <div class="actions">
        <button type="button" class="btn btn-ghost" id="btnReset">ล้างฟอร์ม</button>
        <button type="submit" class="btn btn-pri" id="btnSave">💾 บันทึกข้อมูล</button>
      </div>
    </form>
  </section>

  <!-- ================= BATCHES ================= -->
  <section class="panel" id="p-batches">
    <div class="card">
      <div class="card-h"><div class="ic crab">➕</div><div><h2 id="batchFormTitle">เพิ่มรุ่นการผลิต</h2><small>สร้างรุ่นก่อนเริ่มบันทึกประจำวัน</small></div></div>
      <form id="batchForm" autocomplete="off">
        <input type="hidden" id="b_id">
        <div class="fgrid">
          <div class="field"><label>รหัสรุ่น <span class="req">*</span></label><input id="b_code" required placeholder="เช่น B2026-10"></div>
          <div class="field"><label>วันที่ลูกปูฟัก <span class="req">*</span></label><input type="date" id="b_hatch" required></div>
          <div class="field"><label>จำนวนเริ่มต้น <span class="unit">(ตัว)</span> <span class="req">*</span></label><input type="number" id="b_init" min="1" required></div>
          <div class="field"><label>ที่มาแม่พันธุ์</label><input id="b_source" placeholder="เช่น ธรรมชาติ / ฟาร์ม"></div>
          <div class="field"><label id="spLbl">ชนิด</label>
            <div class="relative" id="spBox">
              <input type="hidden" id="b_species">
              <button type="button" id="spBtn" class="spbtn" aria-haspopup="listbox" aria-expanded="false" aria-labelledby="spLbl spBtn">
                <span id="spLabel" class="truncate"></span>
                <svg class="h-4 w-4 flex-none text-muted" viewBox="0 0 20 20" fill="currentColor"><path d="M5.3 7.3a1 1 0 0 1 1.4 0L10 10.6l3.3-3.3a1 1 0 1 1 1.4 1.4l-4 4a1 1 0 0 1-1.4 0l-4-4a1 1 0 0 1 0-1.4z"/></svg>
              </button>
              <ul id="spList" role="listbox" aria-labelledby="spLbl" class="absolute inset-x-0 z-30 mt-1 hidden max-h-64 overflow-auto rounded-[10px] border border-line bg-white py-1 shadow-card"></ul>
            </div></div>
          <div class="field"><label>สถานะ</label><select id="b_status"><option>กำลังอนุบาล</option><option>ย้ายลงบ่อแล้ว</option><option>ขายแล้ว</option><option>ยกเลิก</option></select></div>
          <div class="field"><label>ต้นทุนอื่นของรุ่น <span class="unit">(บาท)</span></label><input type="number" id="b_other" min="0" step="0.01" placeholder="แม่พันธุ์ ค่าไฟ ค่าแรง ฯลฯ"></div>
          <div class="field full"><label>หมายเหตุ</label><input id="b_note"></div>
        </div>
        <div class="mt-3.5 flex flex-wrap justify-end gap-2.5">
          <button type="button" class="btn btn-ghost" id="btnBatchReset">ยกเลิก</button>
          <button type="submit" class="btn btn-crab">🦀 บันทึกรุ่น</button>
        </div>
      </form>
    </div>
    <div class="card">
      <div class="card-h"><div class="ic">🗂️</div><div><h2>รุ่นทั้งหมด</h2></div></div>
      <div class="grid grid-cols-[repeat(auto-fill,minmax(260px,1fr))] gap-3.5" id="batchList"></div>
    </div>
  </section>

  <!-- ================= HARVEST ================= -->
  <section class="panel" id="p-harvest">
    <form id="harvForm" autocomplete="off">
      <input type="hidden" id="hv_id">
      <div class="card">
        <div class="card-h"><div class="ic crab">💰</div><div><h2 id="harvTitle">บันทึกการจับจำหน่าย</h2><small>กรอกจำนวนลูกปูที่จับได้แยกตามขนาด ระบบจะประเมินรายได้และกำไร/ขาดทุนให้ทันที</small></div></div>
        <div class="fgrid">
          <div class="field"><label>วันที่จับ <span class="req">*</span></label><input type="date" id="hv_date" required></div>
          <div class="field"><label>รุ่นการผลิต <span class="req">*</span></label><select id="hv_batch" required></select></div>
          <div class="field"><label>บ่อ / ถัง</label><select id="hv_tank"></select></div>
          <div class="field"><label>ผู้ซื้อ / หมายเหตุ</label><input id="hv_note" placeholder="ไม่บังคับ"></div>
        </div>

        <h3 class="mb-2 mt-[18px] text-base">จำนวนที่จับได้ตามขนาด</h3>
        <div class="sizecols px-3 text-[.82rem] text-muted max-sm:hidden"><span>ขนาด</span><span>ราคา (บาท/ตัว)</span><span>จำนวน (ตัว)</span><span class="text-right">เป็นเงิน (บาท)</span></div>
        <div class="mt-1.5 grid gap-2.5" id="hvSizes"></div>
        <div id="hvWarn"></div>
      </div>

      <div class="card">
        <div class="card-h"><div class="ic">🧮</div><div><h2>ผลการประเมิน</h2><small id="hvScope">–</small></div></div>
        <div class="autobox" id="hvResult"></div>
        <div id="hvDetail"></div>
      </div>

      <div class="actions">
        <button type="button" class="btn btn-ghost" id="btnHvReset">ล้างฟอร์ม</button>
        <button type="submit" class="btn btn-crab" id="btnHvSave">💰 บันทึกการจับ</button>
      </div>
    </form>

    <div class="card mt-[18px]">
      <div class="card-h"><div class="ic">📦</div><div><h2>ประวัติการจับจำหน่าย</h2></div></div>
      <div class="tbl-wrap"><table class="tbl compact">
        <thead><tr><th>วันที่</th><th>รุ่น</th><th>บ่อ</th><th class="num">จำนวน (ตัว)</th><th class="num">รายได้ (บาท)</th><th>รายละเอียด</th><th class="no-print"></th></tr></thead>
        <tbody id="hvBody"></tbody>
      </table></div>
    </div>
  </section>

  <!-- ================= HISTORY ================= -->
  <section class="panel" id="p-history">
    <div class="card">
      <div class="toolbar no-print">
        <input type="search" id="h_search" placeholder="🔍 ค้นหา บ่อ ผู้บันทึก หมายเหตุ">
        <select id="h_batch"></select>
        <input type="date" id="h_from" title="จากวันที่">
        <input type="date" id="h_to" title="ถึงวันที่">
      </div>
      <div class="toolbar no-print">
        <button class="btn btn-ghost btn-sm" id="btnCSV">⬇️ ส่งออก CSV (Excel)</button>
        <a class="btn btn-ghost btn-sm" href="/api/export" onclick="toast('กำลังดาวน์โหลดไฟล์สำรอง…')">💾 สำรองข้อมูล (JSON)</a>
        <label class="btn btn-ghost btn-sm m-0">📂 นำเข้าข้อมูลสำรอง<input type="file" id="fileImport" accept=".json" hidden></label>
        <button class="btn btn-ghost btn-sm" onclick="window.print()">🖨️ พิมพ์</button>
      </div>
      <div class="tbl-wrap"><table class="tbl">
        <thead><tr><th>วันที่</th><th>รุ่น</th><th>บ่อ</th><th>ระยะ</th><th class="num">อายุ</th><th class="num">อุณหภูมิ</th><th class="num">เค็ม</th><th class="num">pH</th><th class="num">DO</th><th class="num">คงเหลือ</th><th class="num">รอด</th><th class="num">ต้นทุนวัสดุ</th><th>สถานะน้ำ</th><th class="no-print"></th></tr></thead>
        <tbody id="histBody"></tbody>
      </table></div>
    </div>
  </section>

  <!-- ================= SUMMARY ================= -->
  <section class="panel" id="p-summary">
    <div class="card no-print !py-3.5">
      <div class="flex items-center gap-2.5"><label class="lbl whitespace-nowrap" for="s_batch">เลือกรุ่น</label><select id="s_batch"></select></div>
    </div>
    <div class="mb-[18px] grid grid-cols-[repeat(auto-fit,minmax(160px,1fr))] gap-3.5" id="statBox"></div>
    <div class="card">
      <div class="card-h"><div class="ic crab">🧾</div><div><h2>การใช้วัสดุสะสมและต้นทุน</h2><small>คำนวณจากราคาต่อหน่วยปัจจุบันในแท็บตั้งค่า</small></div></div>
      <div class="tbl-wrap"><table class="tbl compact" id="costTable"></table></div>
    </div>
    <div class="card">
      <div class="card-h"><div class="ic crab">📈</div><div><h2>อัตรารอดตามอายุ</h2></div></div>
      <canvas id="chartSurv" height="220" class="w-full"></canvas>
    </div>
    <div class="card">
      <div class="card-h"><div class="ic">💧</div><div><h2>คุณภาพน้ำ</h2><small>แถบสีเขียว = ช่วงเกณฑ์เหมาะสม · จุดสีตามสถานะ เขียว/เหลือง/แดง</small></div></div>
      <div class="grid grid-cols-[repeat(auto-fill,minmax(300px,1fr))] gap-3.5 max-sm:grid-cols-1" id="waterCharts"></div>
    </div>
  </section>

  <!-- ================= SETTINGS ================= -->
  <section class="panel" id="p-settings">
    <div class="note info !mt-0 mb-[18px]">การแก้ไขในหน้านี้บันทึกลงฐานข้อมูลอัตโนมัติทันที</div>
    <div class="card" id="orgCard">
      <div class="card-h"><div class="ic">🏛️</div><div><h2>ข้อมูลหน่วยงาน</h2><small>โลโก้และชื่อที่แสดงบนหัวหน้า</small></div></div>
      <div class="mb-4 flex flex-wrap items-center gap-[18px] rounded-[14px] bg-gradient-to-br from-[#0a5f67] to-[#139aa0] p-4 text-white">
        <div class="orglogo !h-24 !w-24" id="orgPrevLogo"><img id="orgPrevImg" alt="" hidden><span id="orgPrevPh" class="text-center text-[.7rem] font-semibold leading-tight text-pri-2">ยังไม่มี<br>โลโก้</span></div>
        <div><div class="orgname" id="orgPrevName">ชื่อหน่วยงาน</div><h1 class="text-[1.2rem]">สมุดบันทึกอนุบาลลูกปูทะเล</h1></div>
      </div>
      <div class="fgrid g2">
        <div class="field"><label>ชื่อหน่วยงาน</label><input id="org_name" placeholder="เช่น กรมประมง"></div>
        <div class="field"><label>ชื่อหน่วยงานย่อย / ศูนย์</label><input id="org_sub" placeholder="เช่น ศูนย์วิจัยและพัฒนาประมงชายฝั่ง..."></div>
        <div class="field"><label>ไฟล์โลโก้</label>
          <div class="flex flex-wrap gap-2">
            <label class="btn btn-ghost btn-sm m-0">📂 เลือกไฟล์โลโก้<input type="file" id="orgLogoFile" accept="image/*" hidden></label>
            <button type="button" class="btn btn-danger btn-sm" id="btnLogoDel">🗑️ ลบโลโก้</button>
          </div></div>
        <div class="field"><label>ขนาดโลโก้ในวงกลม <span class="unit" id="org_zoom_v">100%</span></label>
          <input type="range" id="org_zoom" min="40" max="200" step="1" value="100">
          <div class="hint">เลื่อนซ้ายเพื่อย่อ เลื่อนขวาเพื่อขยาย ให้โลโก้พอดีกับวงกลม</div></div>
        <div class="field"><label>พื้นหลังวงกลม</label>
          <select id="org_bg"><option value="#ffffff">สีขาว</option><option value="transparent">โปร่งใส</option></select></div>
      </div>
    </div>
    <div class="card">
      <div class="card-h"><div class="ic crab">🧺</div><div><h2>รายการวัสดุและราคาต่อหน่วย</h2><small>ใช้คำนวณปริมาณการใช้สะสมและต้นทุนการผลิต</small></div></div>
      <div class="tbl-wrap"><table class="tbl compact">
        <thead><tr><th>ชื่อวัสดุ</th><th>หมวด</th><th>หน่วย</th><th>ราคา (บาท/หน่วย)</th><th></th></tr></thead>
        <tbody id="matSetBody"></tbody>
      </table></div>
      <div class="mt-3"><button class="btn btn-ghost btn-sm" id="btnAddMat">➕ เพิ่มวัสดุ</button></div>
    </div>
    <div class="card">
      <div class="card-h"><div class="ic crab">🏷️</div><div><h2>ราคาจำหน่ายตามขนาด</h2><small>ใช้เป็นราคาเริ่มต้นในหน้าจับจำหน่าย (แก้ไขรายครั้งได้)</small></div></div>
      <div class="tbl-wrap"><table class="tbl compact">
        <thead><tr><th>ขนาด</th><th>ราคา (บาท/ตัว)</th><th></th></tr></thead>
        <tbody id="sizeSetBody"></tbody>
      </table></div>
      <div class="mt-3"><button class="btn btn-ghost btn-sm" id="btnAddSize">➕ เพิ่มขนาด</button></div>
    </div>
    <div class="card">
      <div class="card-h"><div class="ic">💧</div><div><h2>เกณฑ์คุณภาพน้ำ</h2><small>ช่วงเหมาะสม = สีเขียว · ช่วงเฝ้าระวัง = สีเหลือง · นอกช่วงเฝ้าระวัง = สีแดง</small></div></div>
      <div class="tbl-wrap"><table class="tbl compact">
        <thead><tr><th>พารามิเตอร์</th><th>เหมาะสม ต่ำสุด</th><th>เหมาะสม สูงสุด</th><th>เฝ้าระวัง ต่ำสุด</th><th>เฝ้าระวัง สูงสุด</th></tr></thead>
        <tbody id="waterSetBody"></tbody>
      </table></div>
      <div class="mt-3"><button class="btn btn-ghost btn-sm" id="btnWaterDefault">↺ คืนค่าเกณฑ์เริ่มต้น</button></div>
    </div>
  </section>
</main>

<div class="fixed inset-0 z-40 hidden items-center justify-center bg-[rgba(10,30,36,.45)] p-4" id="modal">
  <div class="max-h-[85vh] w-full max-w-[640px] overflow-auto rounded-2xl bg-white p-5" id="modalBox"></div>
</div>
<div class="fixed bottom-6 left-1/2 z-50 max-w-[90vw] -translate-x-1/2 translate-y-[120px] rounded-xl bg-ink px-5 py-3 text-white shadow-card transition-transform duration-300" id="toast"></div>

<script>
/* ---------- Config ---------- */
const STAGES=['Z1','Z2','Z3','Z4','Z5','เมกาโลปา','C1','C2','C3','C4+'];
const STAGE_TH={Z1:'ซูเอีย 1',Z2:'ซูเอีย 2',Z3:'ซูเอีย 3',Z4:'ซูเอีย 4',Z5:'ซูเอีย 5','เมกาโลปา':'เมกาโลปา',C1:'ปูวัยอ่อน C1',C2:'C2',C3:'C3','C4+':'C4 ขึ้นไป'};
// [id, label, unit, step, maxOnly]
const WATER=[
  ['temp','อุณหภูมิ','°C',0.1],
  ['sal','ความเค็ม','ppt',0.1],
  ['ph','pH','',0.01],
  ['do','ออกซิเจนละลาย (DO)','mg/L',0.1],
  ['nh3','แอมโมเนียรวม (TAN)','mg/L',0.01,true],
  ['no2','ไนไตรท์','mg/L',0.01,true],
  ['alk','ความเป็นด่าง','mg/L',1]
];
const CATS=['อาหาร','จุลินทรีย์/สารเคมี','วัสดุสิ้นเปลือง','อื่น ๆ'];
// [ชื่อไทย, ชื่อวิทยาศาสตร์] — ค่าที่บันทึกคือ "ชื่อไทย (ชื่อวิทยาศาสตร์)"
const SPECIES=[['ปูดำ','S. olivacea'],['ปูเขียว','S. serrata'],['ปูม่วง','S. tranquebarica'],['ปูขาว','S. paramamosain'],['ไม่ระบุ','']]
  .map(([th,sci])=>({th,sci,value:sci?`${th} (${sci})`:th}));
const ORG_DEFAULT={name:'',sub:'',zoom:100,bg:'#ffffff'};

/* ---------- State (โหลดจากเซิร์ฟเวอร์) ---------- */
let DEFAULT_WATER={},logs=[],batches=[],harvests=[],settings={water:{},materials:[],sizes:[]},org={...ORG_DEFAULT},logo=null;

function applyState(s){
  DEFAULT_WATER=s.defaultWater||{};
  batches=s.batches||[];logs=s.logs||[];harvests=s.harvests||[];
  settings=s.settings||{};
  settings.water=Object.assign(JSON.parse(JSON.stringify(DEFAULT_WATER)),settings.water||{});
  settings.materials=settings.materials||[];settings.sizes=settings.sizes||[];
  org=Object.assign({...ORG_DEFAULT},s.org||{});logo=s.logo||null;
}

/* ---------- API ---------- */
async function api(method,url,body){
  const opt={method,headers:{}};
  if(body!==undefined){opt.headers['Content-Type']='application/json';opt.body=JSON.stringify(body)}
  const res=await fetch(url,opt);
  let data=null;try{data=await res.json()}catch(e){}
  if(!res.ok)throw new Error(data?.error||('HTTP '+res.status));
  return data;
}
// คืนค่า null เมื่อบันทึกไม่สำเร็จ (แสดง toast แจ้งเตือนให้แล้ว)
async function persist(method,url,body){
  try{return await api(method,url,body)}catch(e){toast('⚠️ บันทึกไม่สำเร็จ: '+e.message);return null}
}

const uid=()=>Date.now().toString(36)+Math.random().toString(36).slice(2,7);
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=n=>n==null||n===''||isNaN(n)?'–':Number(n).toLocaleString('th-TH',{maximumFractionDigits:2});
const baht=n=>n==null||isNaN(n)?'–':Number(n).toLocaleString('th-TH',{minimumFractionDigits:2,maximumFractionDigits:2});
const thDate=d=>d?new Date(d+'T00:00').toLocaleDateString('th-TH',{day:'numeric',month:'short',year:'2-digit'}):'–';
const today=()=>{const d=new Date();d.setMinutes(d.getMinutes()-d.getTimezoneOffset());return d.toISOString().slice(0,10)};
const num=v=>v===''||v==null?null:Number(v);

function toast(m){
  const t=$('toast');t.textContent=m;t.classList.replace('translate-y-[120px]','translate-y-0');
  clearTimeout(t._t);t._t=setTimeout(()=>t.classList.replace('translate-y-0','translate-y-[120px]'),2800);
}
async function busy(btn,fn){btn.disabled=true;try{return await fn()}finally{btn.disabled=false}}

/* ---------- Tabs ---------- */
document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>showTab(b.dataset.tab));
function showTab(t){
  document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active',x.dataset.tab===t));
  document.querySelectorAll('.panel').forEach(p=>p.classList.toggle('active',p.id==='p-'+t));
  if(t==='history')renderHistory(); if(t==='summary')renderSummary(); if(t==='batches')renderBatches();
  if(t==='harvest'){renderHarvestList();calcHarvest()} if(t==='settings')renderSettings();
  window.scrollTo({top:0,behavior:'smooth'});
}

/* ---------- Materials / cost helpers ---------- */
const matById=id=>settings.materials.find(m=>m.id===id);
function matTotals(list){const t={};list.forEach(l=>Object.entries(l.mats||{}).forEach(([k,v])=>{if(v!=null&&!isNaN(v))t[k]=(t[k]||0)+Number(v)}));return t}
function costOf(totals){let c=0;Object.entries(totals).forEach(([k,q])=>{const m=matById(k);if(m&&m.price!=null)c+=q*m.price});return c}
function logCost(l){return costOf(matTotals([l]))}
const unpriced=()=>settings.materials.filter(m=>m.price==null||m.price==='');

/* ---------- Water ---------- */
function waterStatus(id,v){
  if(v==null||isNaN(v))return null;
  const r=settings.water[id];if(!r)return null;const[a,b,c,d]=r;
  if(v>=a&&v<=b)return'ok'; if(v>=c&&v<=d)return'warn'; return'bad';
}
function rangeTxt(id,a,b){const w=WATER.find(x=>x[0]===id);return w[4]?`ไม่เกิน ${b}`:`${a} – ${b}`}
function renderWaterInputs(){
  const keep={};WATER.forEach(([id])=>{const e=$('w_'+id);if(e)keep[id]=e.value});
  $('waterGrid').innerHTML=WATER.map(([id,l,u,st])=>{const[a,b]=settings.water[id];
    return`<div class="field"><label>${l} ${u?`<span class="unit">(${u})</span>`:''}</label><input type="number" id="w_${id}" step="${st}" data-w="${id}" placeholder="เกณฑ์ ${rangeTxt(id,a,b)}"><div class="hint" id="h_${id}"></div></div>`}).join('');
  document.querySelectorAll('[data-w]').forEach(i=>{i.value=keep[i.dataset.w]??'';i.addEventListener('input',()=>checkWater(i));checkWater(i)});
}
function checkWater(inp){
  const id=inp.dataset.w,v=num(inp.value),s=waterStatus(id,v),h=$('h_'+id);
  inp.classList.remove('st-ok','st-warn','st-bad');h.className='hint';h.textContent='';
  if(!s)return;
  inp.classList.add('st-'+s);h.classList.add(s);
  h.textContent=s==='ok'?'✓ อยู่ในเกณฑ์':s==='warn'?'⚠ ควรเฝ้าระวัง':'✕ นอกเกณฑ์';
}

/* ---------- Batch helpers ---------- */
const batchById=id=>batches.find(b=>b.id===id);
function dph(batch,date){if(!batch||!date)return null;return Math.round((new Date(date+'T00:00')-new Date(batch.hatch+'T00:00'))/864e5)}
function surv(batch,count){if(!batch||count==null||!batch.init)return null;return count/batch.init*100}

function fillBatchSelects(){
  const act=batches.filter(b=>b.status==='กำลังอนุบาล'), other=batches.filter(b=>b.status!=='กำลังอนุบาล');
  const opt=b=>`<option value="${b.id}">${esc(b.code)}${b.status!=='กำลังอนุบาล'?' ('+esc(b.status)+')':''}</option>`;
  const none='<option value="">ยังไม่มีรุ่น — ไปที่แท็บ "รุ่นการผลิต"</option>';
  const pick='<option value="">— เลือกรุ่น —</option>'+act.map(opt).join('')+other.map(opt).join('');
  ['d_batch','hv_batch'].forEach(id=>{const cur=$(id).value;$(id).innerHTML=batches.length?pick:none;$(id).value=batchById(cur)?cur:(act.length===1?act[0].id:'')});
  const all='<option value="">ทุกรุ่น</option>'+batches.map(opt).join('');
  const hb=$('h_batch').value;$('h_batch').innerHTML=all;$('h_batch').value=batchById(hb)?hb:'';
  const sb=$('s_batch').value;$('s_batch').innerHTML=all;$('s_batch').value=batchById(sb)?sb:(act[0]?.id||'');
  const uniq=k=>[...new Set(logs.map(l=>l[k]).filter(Boolean))];
  $('tankList').innerHTML=uniq('tank').map(t=>`<option value="${esc(t)}">`).join('');
  $('recList').innerHTML=uniq('recorder').map(t=>`<option value="${esc(t)}">`).join('');
  fillHarvestTanks();updateAuto();
}

/* ---------- Daily form: materials ---------- */
function renderMatInputs(){
  const keep={};settings.materials.forEach(m=>{const e=$('m_'+m.id);if(e)keep[m.id]=e.value});
  $('matGrid').innerHTML=settings.materials.length?settings.materials.map(m=>`<div class="field"><label>${esc(m.name)} <span class="unit">(${esc(m.unit)})</span></label><input type="number" id="m_${m.id}" data-m="${m.id}" min="0" step="any"><div class="hint" id="mh_${m.id}"></div></div>`).join('')
    :'<div class="hint">ยังไม่มีรายการวัสดุ — เพิ่มได้ที่แท็บตั้งค่า</div>';
  document.querySelectorAll('[data-m]').forEach(i=>{i.value=keep[i.dataset.m]??'';i.addEventListener('input',updateAuto)});
  updateAuto();
}
function formMats(){const o={};document.querySelectorAll('[data-m]').forEach(i=>{const v=num(i.value);if(v!=null)o[i.dataset.m]=v});return o}

function updateAuto(){
  const b=batchById($('d_batch').value),d=dph(b,$('d_date').value),s=surv(b,num($('d_count').value));
  $('a_dph').textContent=d==null?'–':`${d} วัน`;
  $('a_surv').textContent=s==null?'–':s.toFixed(1)+'%';
  $('a_init').textContent=b?fmt(b.init)+' ตัว':'–';
  const cur=formMats(),editId=$('d_id').value;
  const prev=b?matTotals(logs.filter(l=>l.batch===b.id&&l.id!==editId)):{};
  settings.materials.forEach(m=>{
    const h=$('mh_'+m.id);if(!h)return;
    const tot=(prev[m.id]||0)+(cur[m.id]||0);
    h.textContent=(b?`สะสมรุ่นนี้ ${fmt(tot)} ${m.unit}`:'')+(m.price!=null?` · ${baht(m.price)} ฿/${m.unit}`:' · ยังไม่ใส่ราคา');
  });
  const day=costOf(cur);
  $('a_daycost').textContent=baht(day)+' ฿';
  $('a_cumcost').textContent=b?baht(costOf(prev)+day)+' ฿':'–';
}
['d_batch','d_date','d_count'].forEach(i=>$(i).addEventListener('input',updateAuto));

/* ---------- Daily form ---------- */
const DF=['date','time','batch','tank','recorder','exchange','watercolor','bottom','count','dead','molt','cannibal','cw','treat','task','note','denrot','denart','meals'];
const NUMS=new Set(['exchange','count','dead','cw','denrot','denart','meals']);

function resetDaily(keep){
  $('dailyForm').reset();$('d_id').value='';
  document.querySelectorAll('[data-w]').forEach(checkWater);
  if(keep){['batch','tank','recorder'].forEach(k=>$('d_'+k).value=keep[k]||'')}
  $('d_date').value=today();
  $('d_time').value=new Date().toTimeString().slice(0,5);
  $('btnSave').innerHTML='💾 บันทึกข้อมูล';
  fillBatchSelects();
}
$('btnReset').onclick=()=>resetDaily();

$('dailyForm').onsubmit=e=>{
  e.preventDefault();
  if(!batches.length){toast('กรุณาสร้างรุ่นการผลิตก่อน');showTab('batches');return}
  const r={id:$('d_id').value||uid()};
  DF.forEach(k=>r[k]=NUMS.has(k)?num($('d_'+k).value):$('d_'+k).value.trim());
  r.water={};WATER.forEach(([id])=>r.water[id]=num($('w_'+id).value));
  r.mats=formMats();
  r.stage=document.querySelector('[name=stage]:checked')?.value||'';
  r.appetite=document.querySelector('[name=appetite]:checked')?.value||'';
  r.symptoms=[...document.querySelectorAll('[name=symptom]:checked')].map(x=>x.value);
  busy($('btnSave'),async()=>{
    const saved=await persist('PUT','/api/logs/'+r.id,r);if(!saved)return;
    const i=logs.findIndex(l=>l.id===saved.id),editing=i>=0;
    if(editing)logs[i]=saved;else logs.push(saved);
    const bad=WATER.filter(([id])=>waterStatus(id,saved.water[id])==='bad').map(w=>w[1]);
    toast(editing?'✅ แก้ไขข้อมูลแล้ว':bad.length?`✅ บันทึกแล้ว · ⚠️ นอกเกณฑ์: ${bad.join(', ')}`:'✅ บันทึกข้อมูลเรียบร้อย');
    resetDaily(saved);
    if(editing)showTab('history');
  });
};

function editLog(id){
  const r=logs.find(l=>l.id===id);if(!r)return;
  closeModal();showTab('daily');fillBatchSelects();
  $('d_id').value=r.id;
  DF.forEach(k=>$('d_'+k).value=r[k]??'');
  WATER.forEach(([w])=>{$('w_'+w).value=r.water?.[w]??'';checkWater($('w_'+w))});
  document.querySelectorAll('[data-m]').forEach(i=>i.value=r.mats?.[i.dataset.m]??'');
  document.querySelectorAll('[name=stage]').forEach(x=>x.checked=x.value===r.stage);
  document.querySelectorAll('[name=appetite]').forEach(x=>x.checked=x.value===r.appetite);
  document.querySelectorAll('[name=symptom]').forEach(x=>x.checked=(r.symptoms||[]).includes(x.value));
  $('btnSave').innerHTML='💾 บันทึกการแก้ไข';updateAuto();
}
async function delLog(id){
  if(!confirm('ลบรายการนี้?'))return;
  if(!await persist('DELETE','/api/logs/'+id))return;
  logs=logs.filter(l=>l.id!==id);closeModal();renderHistory();toast('ลบแล้ว');
}

/* ---------- Species dropdown (ชื่อวิทยาศาสตร์ตัวเอียง) ---------- */
// "ปูดำ (S. olivacea)" -> ปูดำ (<i>S. olivacea</i>)
function speciesHTML(v){const m=/^(.*?)\s*\((S\. [^)]+)\)$/.exec(v||'');return m?`${esc(m[1])} (<i>${esc(m[2])}</i>)`:esc(v)}
const sp={box:$('spBox'),btn:$('spBtn'),list:$('spList'),inp:$('b_species'),active:0};
sp.list.innerHTML=SPECIES.map((s,i)=>`<li role="option" id="sp_${i}" data-i="${i}" class="spopt">${speciesHTML(s.value)}</li>`).join('');
function setSpecies(v){
  sp.inp.value=v||SPECIES[0].value;
  $('spLabel').innerHTML=speciesHTML(sp.inp.value);
  [...sp.list.children].forEach((li,j)=>li.setAttribute('aria-selected',SPECIES[j].value===sp.inp.value));
}
function spIsOpen(){return !sp.list.classList.contains('hidden')}
function spHighlight(){
  [...sp.list.children].forEach((li,j)=>li.dataset.active=j===sp.active);
  sp.btn.setAttribute('aria-activedescendant','sp_'+sp.active);
  sp.list.children[sp.active]?.scrollIntoView({block:'nearest'});
}
function spOpen(open){
  sp.list.classList.toggle('hidden',!open);sp.btn.setAttribute('aria-expanded',open);
  if(open){sp.active=Math.max(0,SPECIES.findIndex(s=>s.value===sp.inp.value));spHighlight()}
}
sp.btn.onclick=()=>spOpen(!spIsOpen());
sp.list.onclick=e=>{const li=e.target.closest('li');if(!li)return;setSpecies(SPECIES[+li.dataset.i].value);spOpen(false);sp.btn.focus()};
sp.list.onmousemove=e=>{const li=e.target.closest('li');if(li&&+li.dataset.i!==sp.active){sp.active=+li.dataset.i;spHighlight()}};
sp.btn.onkeydown=e=>{
  if(e.key==='ArrowDown'||e.key==='ArrowUp'){
    e.preventDefault();if(!spIsOpen()){spOpen(true);return}
    sp.active=(sp.active+(e.key==='ArrowDown'?1:-1)+SPECIES.length)%SPECIES.length;spHighlight();
  }else if(e.key==='Enter'&&spIsOpen()){e.preventDefault();setSpecies(SPECIES[sp.active].value);spOpen(false)}
  else if(e.key==='Escape'&&spIsOpen()){e.preventDefault();spOpen(false)}
  else if(e.key==='Tab')spOpen(false);
};
document.addEventListener('click',e=>{if(!sp.box.contains(e.target))spOpen(false)});
setSpecies(SPECIES[0].value);

/* ---------- Batches ---------- */
function resetBatch(){$('batchForm').reset();$('b_id').value='';setSpecies(SPECIES[0].value);$('batchFormTitle').textContent='เพิ่มรุ่นการผลิต'}
$('btnBatchReset').onclick=resetBatch;
$('batchForm').onsubmit=async e=>{
  e.preventDefault();
  const b={id:$('b_id').value||uid(),code:$('b_code').value.trim(),hatch:$('b_hatch').value,init:num($('b_init').value),
    source:$('b_source').value.trim(),species:$('b_species').value,status:$('b_status').value,other:num($('b_other').value),note:$('b_note').value.trim()};
  if(batches.some(x=>x.code===b.code&&x.id!==b.id)){toast('รหัสรุ่นนี้มีอยู่แล้ว');return}
  const saved=await persist('PUT','/api/batches/'+b.id,b);if(!saved)return;
  const i=batches.findIndex(x=>x.id===saved.id);if(i>=0)batches[i]=saved;else batches.unshift(saved);
  resetBatch();renderBatches();fillBatchSelects();toast('🦀 บันทึกรุ่นแล้ว');
};
function editBatch(id){
  const b=batchById(id);if(!b)return;
  $('b_id').value=b.id;['code','hatch','init','source','status','other','note'].forEach(k=>$('b_'+k).value=b[k]??'');setSpecies(b.species);
  $('batchFormTitle').textContent='แก้ไขรุ่น '+b.code;window.scrollTo({top:0,behavior:'smooth'});
}
async function delBatch(id){
  const n=logs.filter(l=>l.batch===id).length,h=harvests.filter(x=>x.batch===id).length;
  if(!confirm(n||h?`รุ่นนี้มีบันทึกประจำวัน ${n} รายการ และการจับ ${h} ครั้ง จะถูกลบทั้งหมด ยืนยัน?`:'ลบรุ่นนี้?'))return;
  if(!await persist('DELETE','/api/batches/'+id))return;
  batches=batches.filter(b=>b.id!==id);logs=logs.filter(l=>l.batch!==id);harvests=harvests.filter(x=>x.batch!==id);
  renderBatches();fillBatchSelects();toast('ลบรุ่นแล้ว');
}
function latestLog(bid){return logs.filter(l=>l.batch===bid).sort((a,b)=>(b.date+b.time).localeCompare(a.date+a.time))[0]}
function batchFinance(bid){
  const b=batchById(bid);
  const mat=costOf(matTotals(logs.filter(l=>l.batch===bid)));
  const other=b?.other||0;
  const hv=harvests.filter(h=>h.batch===bid);
  const n=hv.reduce((a,h)=>a+hvCount(h),0),rev=hv.reduce((a,h)=>a+hvRev(h),0);
  return{mat,other,cost:mat+other,n,rev,profit:rev-(mat+other)};
}
function renderBatches(){
  if(!batches.length){$('batchList').innerHTML=emptyHTML('ยังไม่มีรุ่นการผลิต เริ่มเพิ่มรุ่นแรกด้านบน');return}
  const row=(l,v)=>`<div class="mt-1.5 flex justify-between text-[.88rem]"><span>${l}</span>${v}</div>`;
  $('batchList').innerHTML=batches.map(b=>{
    const last=latestLog(b.id),s=last?surv(b,last.count):null,n=logs.filter(l=>l.batch===b.id).length,f=batchFinance(b.id);
    const st=b.status==='กำลังอนุบาล'?'b-ok':b.status==='ยกเลิก'?'b-bad':'b-m';
    return `<div class="rounded-[14px] border-[1.5px] border-line bg-[#fbfdfe] p-3.5">
      <h3 class="flex items-center justify-between gap-2 text-[1.05rem]">${esc(b.code)} <span class="badge ${st}">${esc(b.status)}</span></h3>
      <div class="mb-2.5 mt-1.5 text-[.88rem] text-muted">${speciesHTML(b.species)}<br>ฟัก ${thDate(b.hatch)} · อายุ ${dph(b,today())} วัน · เริ่ม ${fmt(b.init)} ตัว</div>
      <div class="h-2 overflow-hidden rounded-full bg-line"><i class="block h-full bg-gradient-to-r from-crab to-[#f39b6b]" style="width:${Math.min(100,s||0)}%"></i></div>
      ${row('อัตรารอดล่าสุด',`<b>${s==null?'–':s.toFixed(1)+'%'}</b>`)}
      ${row('ระยะล่าสุด',`<span>${last?.stage?STAGE_TH[last.stage]:'–'}</span>`)}
      ${row('บันทึกประจำวัน',`<span>${n} รายการ</span>`)}
      ${row('ต้นทุนรวม',`<span>${baht(f.cost)} ฿</span>`)}
      ${f.n?row('จับขายแล้ว',`<span>${fmt(f.n)} ตัว · ${baht(f.rev)} ฿</span>`)+row(f.profit>=0?'กำไร':'ขาดทุน',`<b class="${f.profit>=0?'pos':'neg'}">${baht(Math.abs(f.profit))} ฿</b>`):''}
      <div class="mt-3 flex flex-wrap gap-1.5">
        <button class="btn btn-ghost btn-sm" onclick="editBatch('${b.id}')">✏️ แก้ไข</button>
        <button class="btn btn-ghost btn-sm" onclick="$('s_batch').value='${b.id}';showTab('summary')">📊 สรุป</button>
        <button class="btn btn-ghost btn-sm" onclick="$('hv_batch').value='${b.id}';fillHarvestTanks();showTab('harvest')">💰 จับ</button>
        <button class="btn btn-danger btn-sm" onclick="delBatch('${b.id}')">🗑️</button>
      </div></div>`}).join('');
}

/* ---------- Harvest ---------- */
const hvCount=h=>(h.items||[]).reduce((a,i)=>a+(i.count||0),0);
const hvRev=h=>(h.items||[]).reduce((a,i)=>a+(i.count||0)*(i.price||0),0);

function fillHarvestTanks(){
  const bid=$('hv_batch').value,cur=$('hv_tank').value;
  const tanks=[...new Set(logs.filter(l=>l.batch===bid).map(l=>l.tank).filter(Boolean))].sort();
  $('hv_tank').innerHTML='<option value="">ทั้งรุ่น (ทุกบ่อ)</option>'+tanks.map(t=>`<option value="${esc(t)}">${esc(t)}</option>`).join('');
  $('hv_tank').value=tanks.includes(cur)?cur:'';
}
function renderHarvestSizes(vals){
  const keep=vals||{};
  if(!vals)settings.sizes.forEach(s=>{const c=$('hvc_'+s.id),p=$('hvp_'+s.id);if(c)keep[s.id]={count:c.value,price:p.value}});
  $('hvSizes').innerHTML=settings.sizes.length?settings.sizes.map(s=>`<div class="sizecols rounded-xl border-[1.5px] border-line bg-[#fbfdfe] px-3 py-2.5">
      <div class="font-semibold max-sm:col-span-full">${esc(s.name)}</div>
      <input type="number" id="hvp_${s.id}" data-hp="${s.id}" min="0" step="0.01" placeholder="ราคา/ตัว">
      <input type="number" id="hvc_${s.id}" data-hc="${s.id}" min="0" step="1" placeholder="จำนวน">
      <div class="text-right font-semibold tabular-nums text-pri-2" id="hvs_${s.id}">0.00</div></div>`).join('')
    :'<div class="hint">ยังไม่มีขนาด — เพิ่มได้ที่แท็บตั้งค่า</div>';
  settings.sizes.forEach(s=>{
    $('hvp_'+s.id).value=keep[s.id]?.price!==undefined&&keep[s.id]?.price!==''?keep[s.id].price:(s.price??'');
    $('hvc_'+s.id).value=keep[s.id]?.count??'';
  });
  document.querySelectorAll('[data-hp],[data-hc]').forEach(i=>i.addEventListener('input',calcHarvest));
  calcHarvest();
}
function harvestItems(){
  return settings.sizes.map(s=>({sid:s.id,name:s.name,price:num($('hvp_'+s.id)?.value),count:num($('hvc_'+s.id)?.value)}))
    .filter(i=>i.count);
}
function calcHarvest(){
  const b=batchById($('hv_batch').value),tank=$('hv_tank').value,editId=$('hv_id').value;
  const items=harvestItems();
  settings.sizes.forEach(s=>{const e=$('hvs_'+s.id);if(e){const c=num($('hvc_'+s.id).value)||0,p=num($('hvp_'+s.id).value)||0;e.textContent=baht(c*p)}});
  const n=items.reduce((a,i)=>a+i.count,0),rev=items.reduce((a,i)=>a+i.count*(i.price||0),0);
  const missingPrice=items.filter(i=>i.price==null).map(i=>i.name);
  let warn='';
  if(missingPrice.length)warn+=`<div class="note">⚠️ ยังไม่ได้ใส่ราคาขนาด: ${missingPrice.map(esc).join(', ')}</div>`;
  const up=unpriced();
  if(up.length)warn+=`<div class="note">⚠️ วัสดุที่ยังไม่มีราคาต่อหน่วย (ไม่ถูกนับในต้นทุน): ${up.map(m=>esc(m.name)).join(', ')} — <a href="#" onclick="showTab('settings');return false">ไปใส่ราคา</a></div>`;
  $('hvWarn').innerHTML=warn;
  if(!b){$('hvScope').textContent='เลือกรุ่นการผลิตก่อน';$('hvResult').innerHTML='';$('hvDetail').innerHTML='';return}

  const scopeLogs=logs.filter(l=>l.batch===b.id&&(!tank||l.tank===tank));
  const matCost=costOf(matTotals(scopeLogs)),other=tank?0:(b.other||0),cost=matCost+other;
  const prev=harvests.filter(h=>h.batch===b.id&&h.id!==editId&&(!tank||h.tank===tank));
  const pn=prev.reduce((a,h)=>a+hvCount(h),0),pr=prev.reduce((a,h)=>a+hvRev(h),0);
  const N=pn+n,R=pr+rev,P=R-cost;
  $('hvScope').textContent=`รุ่น ${b.code} · ${tank?'บ่อ '+tank+' (ต้นทุนเฉพาะวัสดุที่ใช้ในบ่อนี้)':'ทั้งรุ่น (รวมต้นทุนอื่นของรุ่น)'}`;
  const box=(l,v,c,sub)=>`<div class="auto ${c||''}"><small>${l}</small><b>${v}</b>${sub?`<small>${sub}</small>`:''}</div>`;
  let html=box('จับครั้งนี้',fmt(n)+' ตัว','crab')+box('รายได้ครั้งนี้',baht(rev)+' ฿','');
  if(pn)html+=box('จับสะสม (รวมครั้งก่อน)',fmt(N)+' ตัว','',`รายได้สะสม ${baht(R)} ฿`);
  html+=box('ต้นทุนการผลิต',baht(cost)+' ฿','',`วัสดุ ${baht(matCost)}${other?` + อื่น ๆ ${baht(other)}`:''}`);
  html+=box(P>=0?'กำไรประมาณ':'ขาดทุนประมาณ',(P>=0?'+':'−')+baht(Math.abs(P))+' ฿',P>=0?'good':'loss',cost?`${P>=0?'+':''}${(P/cost*100).toFixed(1)}% ของต้นทุน`:'');
  $('hvResult').innerHTML=html;
  const perCrab=N?cost/N:null,avgPrice=N?R/N:null,breakeven=avgPrice?Math.ceil(cost/avgPrice):null;
  $('hvDetail').innerHTML=`<div class="tbl-wrap mt-3.5"><table class="tbl compact"><tbody>
    <tr><td>ต้นทุนต่อตัว</td><td class="num">${perCrab==null?'–':baht(perCrab)+' ฿/ตัว'}</td></tr>
    <tr><td>ราคาขายเฉลี่ย</td><td class="num">${avgPrice==null?'–':baht(avgPrice)+' ฿/ตัว'}</td></tr>
    <tr><td>จำนวนที่ต้องขายให้คุ้มทุน (ที่ราคาเฉลี่ยนี้)</td><td class="num">${breakeven==null?'–':fmt(breakeven)+' ตัว'}</td></tr>
    ${!tank&&b.init?`<tr><td>อัตรารอดจากการจับ (เทียบจำนวนเริ่มต้น ${fmt(b.init)} ตัว)</td><td class="num">${(N/b.init*100).toFixed(2)}%</td></tr>`:''}
    <tr><td>บันทึกประจำวันที่นำมาคิดต้นทุน</td><td class="num">${scopeLogs.length} รายการ</td></tr>
  </tbody></table></div>`;
}
$('hv_batch').addEventListener('input',()=>{fillHarvestTanks();calcHarvest()});
$('hv_tank').addEventListener('input',calcHarvest);

function resetHarvest(){
  $('harvForm').reset();$('hv_id').value='';$('hv_date').value=today();
  $('harvTitle').textContent='บันทึกการจับจำหน่าย';$('btnHvSave').innerHTML='💰 บันทึกการจับ';
  fillBatchSelects();renderHarvestSizes({});
}
$('btnHvReset').onclick=resetHarvest;
$('harvForm').onsubmit=e=>{
  e.preventDefault();
  const items=harvestItems();
  if(!items.length){toast('กรุณากรอกจำนวนปูที่จับได้อย่างน้อย 1 ขนาด');return}
  const h={id:$('hv_id').value||uid(),date:$('hv_date').value,batch:$('hv_batch').value,tank:$('hv_tank').value,note:$('hv_note').value.trim(),items};
  busy($('btnHvSave'),async()=>{
    const saved=await persist('PUT','/api/harvests/'+h.id,h);if(!saved)return;
    const i=harvests.findIndex(x=>x.id===saved.id);if(i>=0)harvests[i]=saved;else harvests.push(saved);
    toast(`💰 บันทึกการจับ ${fmt(hvCount(saved))} ตัว · ${baht(hvRev(saved))} ฿`);
    resetHarvest();renderHarvestList();
  });
};
function editHarvest(id){
  const h=harvests.find(x=>x.id===id);if(!h)return;
  showTab('harvest');
  $('hv_id').value=h.id;$('hv_date').value=h.date;$('hv_batch').value=h.batch;fillHarvestTanks();$('hv_tank').value=h.tank||'';$('hv_note').value=h.note||'';
  const vals={};settings.sizes.forEach(s=>{const it=h.items.find(i=>i.sid===s.id);vals[s.id]=it?{count:it.count,price:it.price??''}:{count:'',price:s.price??''}});
  renderHarvestSizes(vals);
  const orphan=h.items.filter(i=>!settings.sizes.some(s=>s.id===i.sid));
  if(orphan.length)toast('⚠️ บางขนาดในรายการนี้ถูกลบจากตั้งค่าแล้ว');
  $('harvTitle').textContent='แก้ไขการจับจำหน่าย';$('btnHvSave').innerHTML='💾 บันทึกการแก้ไข';
}
async function delHarvest(id){
  if(!confirm('ลบรายการจับนี้?'))return;
  if(!await persist('DELETE','/api/harvests/'+id))return;
  harvests=harvests.filter(x=>x.id!==id);renderHarvestList();calcHarvest();toast('ลบแล้ว');
}
function renderHarvestList(){
  const rows=[...harvests].sort((a,b)=>b.date.localeCompare(a.date));
  $('hvBody').innerHTML=rows.length?rows.map(h=>`<tr><td>${thDate(h.date)}</td><td>${esc(batchById(h.batch)?.code||'?')}</td><td>${esc(h.tank||'ทั้งรุ่น')}</td>
    <td class="num">${fmt(hvCount(h))}</td><td class="num">${baht(hvRev(h))}</td>
    <td class="!whitespace-normal text-[.85rem] text-muted">${h.items.map(i=>`${esc(i.name)} ${fmt(i.count)}×${i.price??'?'}`).join(' · ')}${h.note?' · '+esc(h.note):''}</td>
    <td class="no-print"><button class="btn btn-ghost btn-sm" onclick="editHarvest('${h.id}')">✏️</button> <button class="btn btn-danger btn-sm" onclick="delHarvest('${h.id}')">🗑️</button></td></tr>`).join('')
    :`<tr><td colspan="7">${emptyHTML('ยังไม่มีการจับจำหน่าย')}</td></tr>`;
}

/* ---------- History ---------- */
const emptyHTML=m=>`<div class="col-span-full px-4 py-10 text-center text-muted"><svg class="mx-auto mb-2 opacity-50" width="48" height="48" viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="3"><ellipse cx="32" cy="38" rx="16" ry="11"/><path d="M24 29l-6-10M40 29l6-10"/><circle cx="17" cy="16" r="5"/><circle cx="47" cy="16" r="5"/></svg><div>${m}</div></div>`;
function stageBadge(s){if(!s)return'–';const c=s.startsWith('Z')?'b-z':s==='เมกาโลปา'?'b-m':'b-c';return`<span class="badge ${c}">${STAGE_TH[s]||esc(s)}</span>`}
function waterBadge(r){
  const st=WATER.map(([id])=>waterStatus(id,r.water?.[id])).filter(Boolean);
  if(!st.length)return'–';
  if(st.includes('bad'))return'<span class="badge b-bad">นอกเกณฑ์</span>';
  if(st.includes('warn'))return'<span class="badge b-warn">เฝ้าระวัง</span>';
  return'<span class="badge b-ok">ปกติ</span>';
}
function filtered(){
  const q=$('h_search').value.toLowerCase(),b=$('h_batch').value,f=$('h_from').value,t=$('h_to').value;
  return logs.filter(l=>(!b||l.batch===b)&&(!f||l.date>=f)&&(!t||l.date<=t)&&
    (!q||[l.tank,l.recorder,l.note,l.task,l.treat,batchById(l.batch)?.code].join(' ').toLowerCase().includes(q)))
    .sort((a,c)=>(c.date+c.time).localeCompare(a.date+a.time));
}
['h_search','h_batch','h_from','h_to'].forEach(i=>$(i).addEventListener('input',renderHistory));
function renderHistory(){
  const rows=filtered();
  if(!rows.length){$('histBody').innerHTML=`<tr><td colspan="14">${emptyHTML(logs.length?'ไม่พบข้อมูลตามเงื่อนไข':'ยังไม่มีบันทึก')}</td></tr>`;return}
  $('histBody').innerHTML=rows.map(r=>{const b=batchById(r.batch),s=surv(b,r.count),w=r.water||{};
    return`<tr>
    <td>${thDate(r.date)} <small class="text-muted">${esc(r.time)}</small></td><td>${esc(b?.code||'?')}</td><td>${esc(r.tank)}</td>
    <td>${stageBadge(r.stage)}</td><td class="num">${dph(b,r.date)??'–'}</td>
    <td class="num">${w.temp??'–'}</td><td class="num">${w.sal??'–'}</td><td class="num">${w.ph??'–'}</td><td class="num">${w.do??'–'}</td>
    <td class="num">${fmt(r.count)}</td><td class="num">${s==null?'–':s.toFixed(1)+'%'}</td><td class="num">${baht(logCost(r))}</td><td>${waterBadge(r)}</td>
    <td class="no-print"><button class="btn btn-ghost btn-sm" onclick="viewLog('${r.id}')">ดู</button> <button class="btn btn-ghost btn-sm" onclick="editLog('${r.id}')">✏️</button></td></tr>`}).join('');
}
function viewLog(id){
  const r=logs.find(l=>l.id===id);if(!r)return;const b=batchById(r.batch);
  const dot={ok:'🟢',warn:'🟡',bad:'🔴'};
  const W=WATER.map(([k,l,u])=>r.water?.[k]!=null?`<dt>${l}</dt><dd>${r.water[k]} ${u} ${dot[waterStatus(k,r.water[k])]||''}</dd>`:'').join('');
  const M=Object.entries(r.mats||{}).map(([k,q])=>{const m=matById(k);return`<dt>${esc(m?.name||'(วัสดุที่ถูกลบ)')}</dt><dd>${fmt(q)} ${esc(m?.unit||'')}${m?.price!=null?` · ${baht(q*m.price)} ฿`:''}</dd>`}).join('');
  const row=(l,v)=>v!=null&&v!==''&&!(Array.isArray(v)&&!v.length)?`<dt>${l}</dt><dd>${esc(Array.isArray(v)?v.join(', '):v)}</dd>`:'';
  const sv=surv(b,r.count);
  $('modalBox').innerHTML=`<h2 class="text-lg">บันทึก ${thDate(r.date)} ${esc(r.time)}</h2>
  <dl class="dl">${row('รุ่น',b?.code)}${row('บ่อ',r.tank)}${row('ระยะ',STAGE_TH[r.stage])}${row('อายุ',b?dph(b,r.date)+' วัน':'')}${row('ผู้บันทึก',r.recorder)}</dl>
  <h3 class="text-base">💧 คุณภาพน้ำ</h3><dl class="dl">${W}${row('เปลี่ยนถ่ายน้ำ',r.exchange!=null?r.exchange+'%':'')}${row('สีน้ำ',r.watercolor)}${row('พื้นถัง',r.bottom)}</dl>
  <h3 class="text-base">🦐 วัสดุ/อาหาร</h3><dl class="dl">${M}${row('ต้นทุนวัสดุวันนี้',baht(logCost(r))+' ฿')}${row('โรติเฟอร์ในถัง',r.denrot!=null?r.denrot+' ตัว/มล.':'')}${row('อาร์ทีเมียในถัง',r.denart!=null?r.denart+' ตัว/มล.':'')}${row('มื้อ',r.meals)}${row('การกิน',r.appetite)}</dl>
  <h3 class="text-base">🦀 จำนวนและสุขภาพ</h3><dl class="dl">${row('คงเหลือ',r.count!=null?fmt(r.count)+' ตัว':'')}${row('อัตรารอด',sv==null?'':sv.toFixed(1)+'%')}${row('ตาย',r.dead)}${row('ลอกคราบ',r.molt)}${row('กินกันเอง',r.cannibal)}${row('กระดอง',r.cw!=null?r.cw+' มม.':'')}${row('อาการผิดปกติ',r.symptoms)}</dl>
  <dl class="dl">${row('สาร/ยา',r.treat)}${row('งานอื่น',r.task)}${row('หมายเหตุ',r.note)}</dl>
  <div class="flex justify-end gap-2"><button class="btn btn-danger btn-sm" onclick="delLog('${r.id}')">🗑️ ลบ</button><button class="btn btn-ghost btn-sm" onclick="editLog('${r.id}')">✏️ แก้ไข</button><button class="btn btn-pri btn-sm" onclick="closeModal()">ปิด</button></div>`;
  $('modal').classList.replace('hidden','flex');
}
function closeModal(){$('modal').classList.replace('flex','hidden')}
$('modal').onclick=e=>{if(e.target.id==='modal')closeModal()};
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeModal()});

/* ---------- Export / Import ---------- */
function download(name,text,type){const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([text],{type}));a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000)}
$('btnCSV').onclick=()=>{
  const rows=filtered();if(!rows.length){toast('ไม่มีข้อมูลให้ส่งออก');return}
  const mats=settings.materials;
  const H=['วันที่','เวลา','รุ่น','บ่อ','ผู้บันทึก','ระยะ','อายุ(วัน)',...WATER.map(w=>w[1]+(w[2]?`(${w[2]})`:'')),'เปลี่ยนน้ำ(%)','สีน้ำ','พื้นถัง',
    ...mats.map(m=>`${m.name}(${m.unit})`),'ต้นทุนวัสดุ(บาท)','โรติเฟอร์ในถัง(ตัว/มล.)','อาร์ทีเมียในถัง(ตัว/มล.)','มื้อ','การกิน','คงเหลือ','อัตรารอด(%)','ตาย','ลอกคราบ','กินกันเอง','กระดอง(มม.)','อาการ','สาร/ยา','งานอื่น','หมายเหตุ'];
  const q=v=>{v=v==null?'':String(v);return/[",\n]/.test(v)?'"'+v.replace(/"/g,'""')+'"':v};
  const L=rows.map(r=>{const b=batchById(r.batch),s=surv(b,r.count);return[r.date,r.time,b?.code,r.tank,r.recorder,STAGE_TH[r.stage]||'',dph(b,r.date),
    ...WATER.map(([k])=>r.water?.[k]),r.exchange,r.watercolor,r.bottom,...mats.map(m=>r.mats?.[m.id]),logCost(r).toFixed(2),r.denrot,r.denart,r.meals,r.appetite,r.count,s?.toFixed(2),r.dead,r.molt,r.cannibal,r.cw,(r.symptoms||[]).join('; '),r.treat,r.task,r.note].map(q).join(',')});
  download(`crab-nursery-${today()}.csv`,'﻿'+[H.map(q).join(','),...L].join('\n'),'text/csv;charset=utf-8');toast('ส่งออก CSV แล้ว');
};
$('fileImport').onchange=e=>{
  const f=e.target.files[0];if(!f)return;const rd=new FileReader();
  rd.onload=async()=>{try{
    let d;try{d=JSON.parse(rd.result)}catch(err){d=null}
    if(!d||!Array.isArray(d.logs)||!Array.isArray(d.batches)){toast('ไฟล์ไม่ถูกต้อง');return}
    if(!confirm(`พบ ${d.batches.length} รุ่น, ${d.logs.length} บันทึก, ${(d.harvests||[]).length} การจับ\nรวมเข้ากับข้อมูลเดิมหรือไม่?`))return;
    const useSettings=!!d.settings&&confirm('ใช้การตั้งค่า (ราคาวัสดุ ราคาขาย เกณฑ์น้ำ) จากไฟล์นี้ด้วยหรือไม่?');
    const s=await persist('POST','/api/import',{batches:d.batches,logs:d.logs,harvests:d.harvests||[],settings:d.settings||null,useSettings,org:d.org||null,logo:d.logo||null});
    if(!s)return;
    applyState(s);fillOrgForm();showOrg();settingsChanged();
    fillBatchSelects();renderBatches();renderHistory();renderHarvestList();
    toast('นำเข้าข้อมูลเรียบร้อย');
  }finally{e.target.value=''}};
  rd.readAsText(f);
};

/* ---------- Summary ---------- */
$('s_batch').addEventListener('input',renderSummary);
function renderSummary(){
  const bid=$('s_batch').value,rows=logs.filter(l=>!bid||l.batch===bid).sort((a,b)=>(a.date+a.time).localeCompare(b.date+b.time));
  const b=batchById(bid),last=[...rows].reverse().find(r=>r.count!=null);
  const avg=k=>{const v=rows.map(r=>r.water?.[k]).filter(x=>x!=null);return v.length?(v.reduce((a,c)=>a+c,0)/v.length).toFixed(1):'–'};
  const alerts=rows.filter(r=>WATER.some(([k])=>waterStatus(k,r.water?.[k])==='bad')).length;
  const s=b&&last?surv(b,last.count):null;
  const tot=matTotals(rows),mat=costOf(tot);
  const other=b?(b.other||0):batches.reduce((a,x)=>a+(x.other||0),0);
  const hv=harvests.filter(h=>!bid||h.batch===bid),hn=hv.reduce((a,h)=>a+hvCount(h),0),hr=hv.reduce((a,h)=>a+hvRev(h),0);
  const cost=mat+other,P=hr-cost;
  $('statBox').innerHTML=[
    ['จำนวนบันทึก',rows.length+' รายการ'],
    b?['อายุปัจจุบัน',dph(b,today())+' วัน']:['จำนวนรุ่น',batches.length+' รุ่น'],
    ['อัตรารอดล่าสุด',s==null?'–':s.toFixed(1)+'%','crab'],
    ['คงเหลือล่าสุด',last?fmt(last.count)+' ตัว':'–','crab'],
    ['อุณหภูมิเฉลี่ย',avg('temp')+' °C'],['ความเค็มเฉลี่ย',avg('sal')+' ppt'],
    ['ครั้งที่น้ำนอกเกณฑ์',alerts+' ครั้ง',alerts?'crab':''],
    ['ต้นทุนรวม',baht(cost)+' ฿'],
    ['จับขายแล้ว',fmt(hn)+' ตัว'],
    ['รายได้',baht(hr)+' ฿'],
    ['ต้นทุนต่อตัว (ที่จับได้)',hn?baht(cost/hn)+' ฿':'–'],
    [P>=0?'กำไร':'ขาดทุน',(P>=0?'+':'−')+baht(Math.abs(P))+' ฿',P>=0?'good':'loss']
  ].map(([l,v,c])=>`<div class="stat ${c||''}"><small>${l}</small><b>${v}</b></div>`).join('');

  const ids=[...new Set([...settings.materials.map(m=>m.id),...Object.keys(tot)])];
  const trs=ids.map(id=>{const m=matById(id),q=tot[id]||0;
    return`<tr><td>${esc(m?.name||'(วัสดุที่ถูกลบ)')}</td><td class="num">${fmt(q)}</td><td>${esc(m?.unit||'')}</td><td class="num">${m?.price!=null?baht(m.price):'<span class="neg">ยังไม่ใส่</span>'}</td><td class="num">${m?.price!=null?baht(q*m.price):'–'}</td></tr>`}).join('');
  $('costTable').innerHTML=`<thead><tr><th>วัสดุ</th><th class="num">ใช้สะสม</th><th>หน่วย</th><th class="num">ราคา/หน่วย</th><th class="num">ต้นทุน (บาท)</th></tr></thead><tbody>${trs}</tbody>
   <tfoot><tr><td colspan="4">รวมต้นทุนวัสดุ</td><td class="num">${baht(mat)}</td></tr>
   <tr><td colspan="4">ต้นทุนอื่นของรุ่น</td><td class="num">${baht(other)}</td></tr>
   <tr><td colspan="4">ต้นทุนการผลิตรวม</td><td class="num">${baht(cost)}</td></tr>
   <tr><td colspan="4">รายได้จากการจับจำหน่าย (${fmt(hn)} ตัว)</td><td class="num">${baht(hr)}</td></tr>
   <tr><td colspan="4">${P>=0?'กำไร':'ขาดทุน'}</td><td class="num ${P>=0?'pos':'neg'}">${P>=0?'+':'−'}${baht(Math.abs(P))}</td></tr></tfoot>`;

  const pts=b?rows.filter(r=>r.count!=null).map(r=>({x:dph(b,r.date),y:surv(b,r.count)})):[];
  drawChart($('chartSurv'),[{pts,color:'#e0603a',label:'อัตรารอด (%)'}],b?'อายุ (วัน)':'เลือกรุ่นเพื่อดูกราฟอัตรารอด');
  renderWaterCharts(rows,b);
}
const ST_COLOR={ok:'#2e9b5f',warn:'#d99a12',bad:'#d23c3c'};
function renderWaterCharts(rows,b){
  $('waterCharts').innerHTML=WATER.map(([id,l,u])=>{
    const vals=rows.filter(r=>r.water?.[id]!=null),last=vals[vals.length-1];
    const st=last?waterStatus(id,last.water[id]):null,[a,bb]=settings.water[id];
    const badge=st?`<span class="badge ${st==='ok'?'b-ok':st==='warn'?'b-warn':'b-bad'}">${st==='ok'?'ปกติ':st==='warn'?'เฝ้าระวัง':'นอกเกณฑ์'}</span>`:'';
    return`<div class="rounded-[14px] border-[1.5px] border-line bg-[#fbfdfe] px-3.5 py-3">
      <div class="mb-1 flex items-baseline justify-between gap-2"><b class="font-head text-[.95rem] font-semibold">${l}</b><span class="text-[.8rem] text-muted">เกณฑ์ ${rangeTxt(id,a,bb)} ${u}</span></div>
      <div class="flex items-center gap-2 font-head text-xl font-bold text-pri-2">${last?fmt(last.water[id])+` <small class="font-sans text-[.8rem] font-normal text-muted">${u}</small>`:'–'} ${badge}</div>
      <div class="hint">${last?`ค่าล่าสุด ${thDate(last.date)} · ทั้งหมด ${vals.length} ค่า`:'&nbsp;'}</div>
      <canvas class="mt-1.5 block w-full" id="wc_${id}" height="120"></canvas></div>`}).join('');
  WATER.forEach(([id])=>{
    const pts=rows.map((r,i)=>({x:b?dph(b,r.date):i+1,y:r.water?.[id]})).filter(p=>p.y!=null);
    drawMini($('wc_'+id),pts,id,b?'อายุ (วัน)':'ลำดับบันทึก');
  });
}
function drawMini(cv,pts,id,xlabel){
  const dpr=window.devicePixelRatio||1,W=cv.clientWidth,H=120;cv.width=W*dpr;cv.height=H*dpr;
  const c=cv.getContext('2d');c.scale(dpr,dpr);c.font='11px Sarabun, sans-serif';
  const[a,b]=settings.water[id];const maxOnly=WATER.find(w=>w[0]===id)[4];
  if(!pts.length){c.fillStyle='#5d7780';c.textAlign='center';c.fillText('ยังไม่มีข้อมูล',W/2,H/2);return}
  const ys=pts.map(p=>p.y),xs=pts.map(p=>p.x);
  let y0=Math.min(maxOnly?0:a,...ys),y1=Math.max(b,...ys);const pad=(y1-y0||1)*0.15;y0=maxOnly?0:y0-pad;y1+=pad;
  let x0=Math.min(...xs),x1=Math.max(...xs);if(x0===x1){x0-=1;x1+=1}
  const P={l:38,r:10,t:8,b:22};
  const X=v=>P.l+(v-x0)/(x1-x0)*(W-P.l-P.r),Y=v=>H-P.b-(v-y0)/(y1-y0)*(H-P.t-P.b);
  // ok band
  c.fillStyle='rgba(46,155,95,.13)';c.fillRect(P.l,Y(b),W-P.l-P.r,Y(a)-Y(b));
  c.strokeStyle='rgba(46,155,95,.5)';c.setLineDash([4,3]);[a,b].forEach(v=>{if(maxOnly&&v===a)return;c.beginPath();c.moveTo(P.l,Y(v));c.lineTo(W-P.r,Y(v));c.stroke()});c.setLineDash([]);
  // y labels
  c.fillStyle='#5d7780';c.textAlign='right';
  const dec=(y1-y0)<2?2:(y1-y0)<20?1:0;
  [y0,(y0+y1)/2,y1].forEach(v=>c.fillText(v.toFixed(dec),P.l-5,Y(v)+4));
  // x labels
  c.textAlign='center';const step=Math.max(1,Math.ceil((x1-x0)/6));
  for(let v=Math.ceil(x0);v<=x1;v+=step)if(pts.length>1||v===pts[0].x)c.fillText(v,X(v),H-6);
  // line
  const p=[...pts].sort((q,r)=>q.x-r.x);
  if(p.length>1){c.strokeStyle='#0e7c86';c.lineWidth=2;c.beginPath();p.forEach((q,i)=>i?c.lineTo(X(q.x),Y(q.y)):c.moveTo(X(q.x),Y(q.y)));c.stroke()}
  p.forEach(q=>{c.fillStyle=ST_COLOR[waterStatus(id,q.y)]||'#0e7c86';c.beginPath();c.arc(X(q.x),Y(q.y),4,0,7);c.fill();c.strokeStyle='#fff';c.lineWidth=1.5;c.stroke()});
}
function drawChart(cv,series,xlabel){
  const dpr=window.devicePixelRatio||1,W=cv.clientWidth,H=220;cv.width=W*dpr;cv.height=H*dpr;
  const c=cv.getContext('2d');c.scale(dpr,dpr);c.clearRect(0,0,W,H);c.font='12px Sarabun, sans-serif';
  const all=series.flatMap(s=>s.pts);
  if(!all.length){c.fillStyle='#5d7780';c.textAlign='center';c.fillText(xlabel.startsWith('เลือก')?xlabel:'ยังไม่มีข้อมูล',W/2,H/2);return}
  const P={l:42,r:14,t:26,b:34},xs=all.map(p=>p.x),ys=all.map(p=>p.y);
  let x0=Math.min(...xs),x1=Math.max(...xs),y0=Math.min(0,...ys),y1=Math.max(...ys);if(x0===x1){x0-=1;x1+=1}if(y0===y1)y1+=1;y1+=(y1-y0)*0.08;
  const X=v=>P.l+(v-x0)/(x1-x0)*(W-P.l-P.r),Y=v=>H-P.b-(v-y0)/(y1-y0)*(H-P.t-P.b);
  c.strokeStyle='#d6e4e8';c.fillStyle='#5d7780';c.textAlign='right';
  for(let i=0;i<=4;i++){const v=y0+(y1-y0)*i/4,y=Y(v);c.beginPath();c.moveTo(P.l,y);c.lineTo(W-P.r,y);c.stroke();c.fillText(v.toFixed(v<10?1:0),P.l-6,y+4)}
  c.textAlign='center';const step=Math.max(1,Math.ceil((x1-x0)/8));const one=new Set(xs).size===1;for(let v=Math.ceil(x0);v<=x1;v+=step)if(!one||v===xs[0])c.fillText(v,X(v),H-P.b+16);
  c.fillText(xlabel,(W+P.l)/2,H-4);
  let lx=P.l;series.forEach(s=>{
    if(!s.pts.length)return;const p=[...s.pts].sort((a,b)=>a.x-b.x);
    c.strokeStyle=s.color;c.lineWidth=2.5;c.beginPath();p.forEach((q,i)=>i?c.lineTo(X(q.x),Y(q.y)):c.moveTo(X(q.x),Y(q.y)));c.stroke();
    c.fillStyle=s.color;p.forEach(q=>{c.beginPath();c.arc(X(q.x),Y(q.y),3.5,0,7);c.fill()});
    c.fillRect(lx,8,12,12);c.fillStyle='#15313a';c.textAlign='left';c.fillText(s.label,lx+16,18);lx+=c.measureText(s.label).width+36;
  });
}
window.addEventListener('resize',()=>{if($('p-summary').classList.contains('active'))renderSummary()});

/* ---------- Settings ---------- */
function settingsChanged(){renderMatInputs();renderWaterInputs();renderHarvestSizes();}
async function persistSettings(){
  const s=await persist('PUT','/api/settings',settings);
  settingsChanged();
  return !!s;
}
function renderSettings(){
  $('matSetBody').innerHTML=settings.materials.map(m=>`<tr>
    <td><input data-mat="${m.id}" data-k="name" value="${esc(m.name)}"></td>
    <td><select data-mat="${m.id}" data-k="cat">${[...new Set([...CATS,m.cat||''])].filter(Boolean).map(c=>`<option ${c===m.cat?'selected':''}>${esc(c)}</option>`).join('')}</select></td>
    <td><input data-mat="${m.id}" data-k="unit" value="${esc(m.unit)}" class="max-w-[100px]"></td>
    <td><input type="number" data-mat="${m.id}" data-k="price" value="${m.price??''}" min="0" step="0.0001" placeholder="0.00" class="max-w-[140px]"></td>
    <td><button class="btn btn-danger btn-sm" onclick="delMat('${m.id}')">🗑️</button></td></tr>`).join('')||`<tr><td colspan="5" class="hint">ยังไม่มีวัสดุ</td></tr>`;
  $('sizeSetBody').innerHTML=settings.sizes.map(s=>`<tr>
    <td><input data-size="${s.id}" data-k="name" value="${esc(s.name)}"></td>
    <td><input type="number" data-size="${s.id}" data-k="price" value="${s.price??''}" min="0" step="0.01" placeholder="0.00" class="max-w-[140px]"></td>
    <td><button class="btn btn-danger btn-sm" onclick="delSize('${s.id}')">🗑️</button></td></tr>`).join('')||`<tr><td colspan="3" class="hint">ยังไม่มีขนาด</td></tr>`;
  $('waterSetBody').innerHTML=WATER.map(([id,l,u,st])=>`<tr><td>${l} ${u?`<span class="hint">(${u})</span>`:''}</td>${[0,1,2,3].map(i=>`<td><input type="number" step="${st}" data-wid="${id}" data-i="${i}" value="${settings.water[id][i]}" class="max-w-[110px]"></td>`).join('')}</tr>`).join('');
}
$('p-settings').addEventListener('change',async e=>{
  const t=e.target;
  if(t.dataset.mat){const m=matById(t.dataset.mat);if(!m)return;const k=t.dataset.k;m[k]=k==='price'?num(t.value):t.value.trim()}
  else if(t.dataset.size){const s=settings.sizes.find(x=>x.id===t.dataset.size);if(!s)return;const k=t.dataset.k;s[k]=k==='price'?num(t.value):t.value.trim()}
  else if(t.dataset.wid){const v=num(t.value);if(v==null)return;settings.water[t.dataset.wid][+t.dataset.i]=v}
  else return;
  if(await persistSettings())toast('✓ บันทึกการตั้งค่าแล้ว');
});
$('btnAddMat').onclick=async()=>{settings.materials.push({id:'m_'+uid(),name:'วัสดุใหม่',cat:'อื่น ๆ',unit:'หน่วย',price:null});await persistSettings();renderSettings()};
$('btnAddSize').onclick=async()=>{settings.sizes.push({id:'s_'+uid(),name:'ขนาดใหม่',price:null});await persistSettings();renderSettings()};
$('btnWaterDefault').onclick=async()=>{
  if(!confirm('คืนค่าเกณฑ์คุณภาพน้ำเป็นค่าเริ่มต้น?'))return;
  settings.water=JSON.parse(JSON.stringify(DEFAULT_WATER));
  if(await persistSettings())toast('คืนค่าแล้ว');renderSettings();
};
async function delMat(id){
  const used=logs.filter(l=>l.mats&&l.mats[id]!=null).length;
  if(!confirm(used?`วัสดุนี้ถูกใช้ใน ${used} บันทึก หากลบจะไม่ถูกนับในต้นทุนอีก ยืนยัน?`:'ลบวัสดุนี้?'))return;
  settings.materials=settings.materials.filter(m=>m.id!==id);await persistSettings();renderSettings();
}
async function delSize(id){
  if(!confirm('ลบขนาดนี้? (ประวัติการจับเดิมยังคงอยู่)'))return;
  settings.sizes=settings.sizes.filter(s=>s.id!==id);await persistSettings();renderSettings();
}

/* ---------- Organization logo ---------- */
let orgTimer;
function saveOrg(){clearTimeout(orgTimer);orgTimer=setTimeout(()=>persist('PUT','/api/org',org),400)}
function showLogo(src){
  [['orgLogoImg','orgLogoPh','orgLogo'],['orgPrevImg','orgPrevPh','orgPrevLogo']].forEach(([i,ph,box])=>{
    const img=$(i);
    if(src){img.src=src;img.hidden=false;$(ph).hidden=true}else{img.removeAttribute('src');img.hidden=true;$(ph).hidden=false}
    img.style.transform=`scale(${(org.zoom||100)/100})`;
    $(box).style.background=src?org.bg:'#fff';$(box).style.boxShadow=src&&org.bg==='transparent'?'none':'';
  });
}
function showOrg(){
  const name=(org.name||'').trim(),sub=(org.sub||'').trim();
  const h=name||sub?`${esc(name)}${sub?`<small>${esc(sub)}</small>`:''}`:'';
  $('orgNameH').innerHTML=h;$('orgNameH').hidden=!h;
  $('orgPrevName').innerHTML=h||'<span class="opacity-60">ชื่อหน่วยงาน</span>';
  $('org_zoom_v').textContent=(org.zoom||100)+'%';
  showLogo(logo);
}
function fillOrgForm(){
  $('org_name').value=org.name||'';$('org_sub').value=org.sub||'';
  $('org_zoom').value=org.zoom||100;$('org_bg').value=org.bg||'#ffffff';
}
$('org_name').addEventListener('input',e=>{org.name=e.target.value;saveOrg();showOrg()});
$('org_sub').addEventListener('input',e=>{org.sub=e.target.value;saveOrg();showOrg()});
$('org_zoom').addEventListener('input',e=>{org.zoom=+e.target.value;saveOrg();showOrg()});
$('org_bg').addEventListener('change',e=>{org.bg=e.target.value;saveOrg();showOrg()});
$('btnLogoDel').onclick=async()=>{
  if(!confirm('ลบโลโก้ออก?'))return;
  if(!await persist('DELETE','/api/logo'))return;
  logo=null;showOrg();
};
$('orgLogoFile').onchange=e=>{
  const f=e.target.files[0];if(!f)return;
  const rd=new FileReader();
  rd.onload=()=>{
    // ย่อให้ด้านยาวสุดไม่เกิน 400px เพื่อให้คมชัดแต่ไฟล์ไม่ใหญ่
    const im=new Image();im.onload=async()=>{
      const s=Math.min(1,400/Math.max(im.width,im.height)),cv=document.createElement('canvas');
      cv.width=Math.round(im.width*s);cv.height=Math.round(im.height*s);cv.getContext('2d').drawImage(im,0,0,cv.width,cv.height);
      const url=cv.toDataURL('image/png');
      if(!await persist('PUT','/api/logo',{data:url}))return;
      logo=url;showOrg();toast('✓ บันทึกโลโก้แล้ว ปรับขนาดได้ด้วยแถบเลื่อน');
    };
    im.onerror=()=>toast('⚠️ อ่านไฟล์รูปไม่สำเร็จ');
    im.src=rd.result;
  };rd.readAsDataURL(f);e.target.value='';
};

/* ---------- Init ---------- */
async function init(){
  try{applyState(await api('GET','/api/state'))}
  catch(e){$('loadErr').innerHTML=`<div class="note !mt-0 mb-[18px]">⚠️ โหลดข้อมูลจากเซิร์ฟเวอร์ไม่สำเร็จ: ${esc(e.message)}</div>`;return}
  $('stageChips').innerHTML=STAGES.map(s=>`<label class="chip"><input type="radio" name="stage" value="${s}"><span>${STAGE_TH[s]}</span></label>`).join('');
  fillOrgForm();showOrg();
  renderWaterInputs();renderMatInputs();
  resetDaily();resetHarvest();renderBatches();renderHarvestList();
  if(!batches.length)setTimeout(()=>toast('👋 เริ่มต้น: ใส่ราคาวัสดุในแท็บตั้งค่า แล้วสร้าง "รุ่นการผลิต"'),500);
  else if(unpriced().length)setTimeout(()=>toast(`💡 มีวัสดุ ${unpriced().length} รายการที่ยังไม่ใส่ราคา (แท็บตั้งค่า)`),500);
}
init();
</script>
</body>
</html>
"""


init_db()

if __name__ == "__main__":
    app.run(debug=True)
