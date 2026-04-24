# -*- coding: utf-8 -*-
import os, json, uuid, hashlib
from math import radians, sin, cos, acos
from typing import List, Optional
from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse, JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv
from openai import AsyncOpenAI
from database import get_db, init_db, ensure_column, hash_password
from config import SYSTEM_PROMPT
from datetime import datetime
from fastapi.staticfiles import StaticFiles
import os
import uvicorn

load_dotenv()
app = FastAPI(title="GuardianSync Emergency ERP")
templates = Jinja2Templates(directory="templates")
init_db()
client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# -- Models --
class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    user_id: str
    incident_id: str
    device_lat: Optional[float] = None
    device_lng: Optional[float] = None
    sos_type: Optional[str] = "self"
    messages: List[ChatMessage]

class AllocateRequest(BaseModel):
    unit_id: str
    incident_id: str

class FleetResourceRequest(BaseModel):
    unit_id: str
    unit_type: Optional[str] = None
    driver_name: Optional[str] = None
    contact: Optional[str] = None
    station_name: Optional[str] = None
    hospital_name: Optional[str] = None
    city: Optional[str] = None
    current_status: Optional[str] = "Standby"
    last_lat: Optional[float] = None
    last_lng: Optional[float] = None

class LocationUpdateRequest(BaseModel):
    incident_id: str
    lat: float
    lng: float

class ResolveRequest(BaseModel):
    incident_id: str

class AdminMessageRequest(BaseModel):
    user_id: str
    user_name: str
    incident_id: str
    message: str

class AdminReplyRequest(BaseModel):
    reply: str

class FollowupUpdateRequest(BaseModel):
    incident_id: str
    step: int
    status: Optional[str] = None
    notes: Optional[str] = None
    safe_location: Optional[str] = None

class DuplicateReportRequest(BaseModel):
    original_incident_id: str
    reporter_user_id: str
    reporter_name: str
    message: Optional[str] = ""
    still_unresolved: Optional[bool] = False

# -- Helpers --
def calculate_distance(lat1, lng1, lat2, lng2):
    try:
        if None in (lat1, lng1, lat2, lng2): return 999999.0
        lat1, lng1, lat2, lng2 = map(radians, [float(lat1), float(lng1), float(lat2), float(lng2)])
        val = sin(lat1)*sin(lat2) + cos(lat1)*cos(lat2)*cos(lng2-lng1)
        val = max(-1.0, min(1.0, val))
        return acos(val) * 6371
    except:
        return 999999.0

def get_session_user(request: Request, role: str = None):
    """Role-scoped session cookies prevent cross-contamination between portals."""
    if role == "admin":
        uid = request.cookies.get("gs_admin_id") or request.cookies.get("gs_user_id")
    elif role == "responder":
        uid = request.cookies.get("gs_responder_id") or request.cookies.get("gs_user_id")
    else:
        uid = request.cookies.get("gs_citizen_id") or request.cookies.get("gs_user_id")
    if not uid:
        return None
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    conn.close()
    if not user:
        return None
    u = dict(user)
    # Role guard: ensure the cookie role matches the required role
    if role == "admin" and u.get("role") not in ("admin",):
        return None
    if role == "responder" and u.get("role") not in ("responder", "admin"):
        return None
    if role is None and u.get("role") not in ("user",):
        return None
    return u

def set_role_cookie(response, user_id: str, role: str):
    cname = {"admin": "gs_admin_id", "responder": "gs_responder_id"}.get(role, "gs_citizen_id")
    response.set_cookie(cname, user_id, max_age=86400*30, httponly=True, samesite="lax")

def delete_role_cookies(response, role: str):
    cname = {"admin": "gs_admin_id", "responder": "gs_responder_id"}.get(role, "gs_citizen_id")
    response.delete_cookie(cname)
    response.delete_cookie("gs_user_id")  # legacy cleanup

def require_login(request: Request, role: str = None):
    user = get_session_user(request, role)
    if not user:
        redirect = "/auth/login" if role != "responder" else "/responder/login"
        raise HTTPException(status_code=302, headers={"Location": redirect})
    return user

def generate_full_report(inc_dict, steps=None, duplicate_count=0):
    """Generate a structured incident report."""
    started = inc_dict.get("created_at","")
    resolved = inc_dict.get("resolved_at","")
    res_mins = inc_dict.get("resolution_time_minutes")
    if not res_mins and started and resolved:
        try:
            t1 = datetime.fromisoformat(started)
            t2 = datetime.fromisoformat(resolved)
            res_mins = int((t2 - t1).total_seconds() / 60)
        except:
            res_mins = None

    report = {
        "incident_id": inc_dict.get("id",""),
        "user": inc_dict.get("user_name","Unknown"),
        "phone": inc_dict.get("phone",""),
        "sos_type": inc_dict.get("sos_type","self"),
        "category": inc_dict.get("category","General"),
        "severity": inc_dict.get("severity","Medium"),
        "location": inc_dict.get("location_details","Unknown"),
        "landmark": inc_dict.get("landmark",""),
        "description": inc_dict.get("description",""),
        "transcript": inc_dict.get("transcript",""),
        "assigned_unit": inc_dict.get("assigned_unit","None"),
        "assigned_unit_type": inc_dict.get("assigned_unit_type",""),
        "hospital": inc_dict.get("assigned_hospital",""),
        "resource_contact": inc_dict.get("resource_contact",""),
        "started": started,
        "resolved": resolved,
        "resolution_time_minutes": res_mins,
        "safe_location": inc_dict.get("safe_location",""),
        "step4_safe_confirmed": inc_dict.get("step4_safe_confirmed", 0),
        "followup_notes": inc_dict.get("followup_notes",""),
        "step2_relatives_alerted": inc_dict.get("step2_relatives_alerted", 0),
        "duplicate_reports": duplicate_count,
        "steps": steps or [],
    }
    return report

# -- Auth routes --
@app.get("/auth/login")
async def login_page(request: Request, error: str = None, success: str = None, show_register: bool = False):
    return templates.TemplateResponse("auth/login.html", {
        "request": request, "error": error, "success": success, "show_register": show_register
    })

@app.post("/auth/login")
async def do_login(request: Request):
    form = await request.form()
    email = form.get("email", "").strip().lower()
    password = form.get("password", "")
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    conn.close()
    if not user or user["password_hash"] != hash_password(password):
        return templates.TemplateResponse("auth/login.html", {
            "request": request, "error": "Invalid email or password."
        })
    if user["role"] == "admin":
        response = RedirectResponse(url="/admin/feed", status_code=303)
        set_role_cookie(response, user["id"], "admin")
    elif user["role"] == "responder":
        response = RedirectResponse(url="/responder/dashboard", status_code=303)
        set_role_cookie(response, user["id"], "responder")
    else:
        response = RedirectResponse(url="/", status_code=303)
        set_role_cookie(response, user["id"], "citizen")
    return response

@app.post("/auth/register")
async def do_register(request: Request):
    form = await request.form()
    name = form.get("name", "").strip()
    email = form.get("email", "").strip().lower()
    phone = form.get("phone", "").strip()
    password = form.get("password", "")
    if not name or not email or not password:
        return templates.TemplateResponse("auth/login.html", {
            "request": request, "error": "Name, email and password are required.", "show_register": True
        })
    conn = get_db()
    existing = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if existing:
        conn.close()
        return templates.TemplateResponse("auth/login.html", {
            "request": request, "error": "Email already registered. Please sign in.", "show_register": True
        })
    uid = "user-" + uuid.uuid4().hex[:8]
    medical_id = "GS-" + name[:2].upper() + uuid.uuid4().hex[:2].upper()
    conn.execute("INSERT INTO users (id, name, email, phone, password_hash, role) VALUES (?,?,?,?,?,?)",
        (uid, name, email, phone, hash_password(password), "user"))
    conn.execute("INSERT OR IGNORE INTO user_profiles (user_id, medical_id, full_name, phone) VALUES (?,?,?,?)",
        (uid, medical_id, name, phone))
    conn.commit()
    conn.close()
    response = RedirectResponse(url="/", status_code=303)
    set_role_cookie(response, uid, "citizen")
    return response

@app.get("/auth/logout")
async def logout(request: Request):
    response = RedirectResponse(url="/auth/login", status_code=303)
    response.delete_cookie("gs_citizen_id")
    response.delete_cookie("gs_admin_id")
    response.delete_cookie("gs_responder_id")
    response.delete_cookie("gs_user_id")
    return response

@app.get("/auth/logout/citizen")
async def logout_citizen():
    response = RedirectResponse(url="/auth/login", status_code=303)
    response.delete_cookie("gs_citizen_id")
    response.delete_cookie("gs_user_id")
    return response

@app.get("/auth/logout/admin")
async def logout_admin():
    response = RedirectResponse(url="/auth/login", status_code=303)
    response.delete_cookie("gs_admin_id")
    response.delete_cookie("gs_user_id")
    return response

@app.get("/auth/logout/responder")
async def logout_responder():
    response = RedirectResponse(url="/responder/login", status_code=303)
    response.delete_cookie("gs_responder_id")
    response.delete_cookie("gs_user_id")
    return response

# -- User routes --
@app.get("/")
async def root(request: Request):
    user = get_session_user(request, "citizen")
    if not user: return RedirectResponse(url="/auth/login")
    if user["role"] == "admin": return RedirectResponse(url="/admin/feed")
    return templates.TemplateResponse("user/dashboard.html", {"request": request, "user": user})

@app.get("/user/sos")
async def user_sos(request: Request, incident_id: str = None):
    user = get_session_user(request, "citizen")
    if not user: return RedirectResponse(url="/auth/login")
    # Reuse an existing incident_id if provided (prevents losing chat on back navigation)
    if not incident_id:
        incident_id = "KCH-" + uuid.uuid4().hex[:6].upper()
    conn = get_db()
    profile = conn.execute("SELECT * FROM user_profiles WHERE user_id = ?", (user["id"],)).fetchone()
    contacts = conn.execute("SELECT * FROM emergency_contacts WHERE user_id = ?", (user["id"],)).fetchall()
    conn.close()
    return templates.TemplateResponse("user/sos.html", {
        "request": request, "incident_id": incident_id,
        "user_id": user["id"], "user_name": user["name"],
        "user_phone": user.get("phone", ""),
        "user_profile": dict(profile) if profile else None,
        "emergency_contacts": [dict(c) for c in contacts],
    })

@app.get("/user/profile")
async def user_profile(request: Request):
    user = get_session_user(request, "citizen")
    if not user: return RedirectResponse(url="/auth/login")
    conn = get_db()
    profile = conn.execute("SELECT * FROM user_profiles WHERE user_id = ?", (user["id"],)).fetchone()
    contacts = conn.execute("SELECT * FROM emergency_contacts WHERE user_id = ?", (user["id"],)).fetchall()
    conn.close()
    return templates.TemplateResponse("user/profile.html", {
        "request": request, "user": user, "user_id": user["id"],
        "profile": dict(profile) if profile else None,
        "contacts": [dict(c) for c in contacts],
        "success": request.query_params.get("success"),
        "error": request.query_params.get("error"),
    })

@app.post("/api/user/save-profile")
async def save_profile(request: Request):
    form = await request.form()
    user_id = form.get("user_id", "")
    conn = get_db()
    try:
        existing = conn.execute("SELECT medical_id FROM user_profiles WHERE user_id = ?", (user_id,)).fetchone()
        medical_id = existing["medical_id"] if existing and existing["medical_id"] else "GS-" + uuid.uuid4().hex[:4].upper()
        conn.execute('''INSERT INTO user_profiles (user_id, medical_id, full_name, dob, aadhar_number, address, blood_type, weight, allergies, conditions, phone, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET medical_id=excluded.medical_id, full_name=excluded.full_name,
            dob=excluded.dob, aadhar_number=excluded.aadhar_number, address=excluded.address,
            blood_type=excluded.blood_type, weight=excluded.weight, allergies=excluded.allergies,
            conditions=excluded.conditions, phone=excluded.phone, updated_at=CURRENT_TIMESTAMP''',
            (user_id, medical_id, form.get("full_name",""), form.get("dob",""), form.get("aadhar_number",""),
             form.get("address",""), form.get("blood_type","O+"), form.get("weight",""),
             form.get("allergies",""), form.get("conditions",""), form.get("phone","")))
        extra = json.loads(form.get("extra_contacts_json","[]") or "[]")
        for c in extra:
            if c.get("name") and c.get("phone"):
                conn.execute("INSERT INTO emergency_contacts (user_id, name, relation, phone) VALUES (?,?,?,?)",
                    (user_id, c["name"], c.get("relation",""), c["phone"]))
        conn.commit()
        return RedirectResponse(url="/user/profile?success=Profile+saved!", status_code=303)
    except Exception as e:
        conn.rollback()
        return RedirectResponse(url="/user/profile?error=" + str(e), status_code=303)
    finally:
        conn.close()

@app.delete("/api/user/delete-contact/{contact_id}")
async def delete_contact(contact_id: int):
    conn = get_db()
    try:
        conn.execute("DELETE FROM emergency_contacts WHERE id = ?", (contact_id,))
        conn.commit()
        return {"status": "deleted"}
    finally:
        conn.close()

@app.get("/api/user/lookup-medical-id")
async def lookup_medical_id(id: str):
    conn = get_db()
    try:
        row = conn.execute("SELECT full_name, blood_type, allergies, conditions, phone FROM user_profiles WHERE medical_id = ?", (id.upper(),)).fetchone()
        if not row: raise HTTPException(status_code=404, detail="Not found")
        return dict(row)
    finally:
        conn.close()

@app.post("/api/user/contact-admin")
async def contact_admin(data: AdminMessageRequest):
    conn = get_db()
    try:
        conn.execute("INSERT INTO admin_messages (user_id, user_name, incident_id, message) VALUES (?,?,?,?)",
            (data.user_id, data.user_name, data.incident_id, data.message))
        conn.commit()
        return {"status": "sent"}
    finally:
        conn.close()

@app.get("/api/user/messages/{incident_id}")
async def get_user_messages(incident_id: str, user_id: str):
    """Get admin replies for a user's incident messages."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM admin_messages WHERE incident_id = ? AND user_id = ? ORDER BY created_at ASC",
            (incident_id, user_id)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

@app.get("/user/history")
async def user_history(request: Request):
    user = get_session_user(request, "citizen")
    if not user: return RedirectResponse(url="/auth/login")
    conn = get_db()
    history = conn.execute("SELECT * FROM incidents WHERE user_id = ? ORDER BY created_at DESC", (user["id"],)).fetchall()
    conn.close()
    return templates.TemplateResponse("user/history.html", {"request": request, "user": user, "history": history})

# -- Admin routes --
@app.get("/admin/feed")
async def admin_feed(request: Request):
    user = get_session_user(request, "admin")
    if not user: return RedirectResponse(url="/auth/login")
    return templates.TemplateResponse("admin/feed.html", {"request": request, "user": user})

@app.get("/admin/fleet")
async def admin_fleet(request: Request):
    user = get_session_user(request, "admin")
    if not user: return RedirectResponse(url="/auth/login")
    return templates.TemplateResponse("admin/fleet.html", {"request": request, "user": user})

@app.get("/admin/analytics")
async def admin_analytics(request: Request):
    user = get_session_user(request, "admin")
    if not user: return RedirectResponse(url="/auth/login")
    return templates.TemplateResponse("admin/analytics.html", {"request": request, "user": user})

@app.get("/admin/past-emergencies")
async def admin_past_emergencies(request: Request):
    user = get_session_user(request, "admin")
    if not user: return RedirectResponse(url="/auth/login")
    return templates.TemplateResponse("admin/past_emergencies.html", {"request": request, "user": user})

@app.get("/admin/reports")
async def admin_reports(request: Request):
    user = get_session_user(request, "admin")
    if not user: return RedirectResponse(url="/auth/login")
    return templates.TemplateResponse("admin/reports.html", {"request": request, "user": user})

@app.get("/admin/logs")
async def admin_logs(request: Request):
    user = get_session_user(request, "admin")
    if not user: return RedirectResponse(url="/auth/login")
    return templates.TemplateResponse("admin/logs.html", {"request": request, "user": user})

@app.get("/admin/messages")
async def admin_messages_page(request: Request):
    user = get_session_user(request, "admin")
    if not user: return RedirectResponse(url="/auth/login")
    return templates.TemplateResponse("admin/messages.html", {"request": request, "user": user})

# -- API endpoints --
@app.get("/api/admin/incidents")
async def api_get_incidents():
    conn = get_db()
    rows = conn.execute("SELECT * FROM incidents ORDER BY created_at DESC LIMIT 200").fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get("/api/admin/fleet")
async def api_get_fleet():
    conn = get_db()
    rows = conn.execute("SELECT * FROM fleet ORDER BY unit_type, city, unit_id").fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get("/api/admin/messages")
async def api_get_messages():
    conn = get_db()
    rows = conn.execute("SELECT * FROM admin_messages ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/admin/messages/{msg_id}/read")
async def mark_message_read(msg_id: int):
    conn = get_db()
    try:
        conn.execute("UPDATE admin_messages SET status='read' WHERE id=?", (msg_id,))
        conn.commit()
        return {"status": "ok"}
    finally:
        conn.close()

@app.post("/api/admin/messages/{msg_id}/reply")
async def admin_reply_message(msg_id: int, data: AdminReplyRequest):
    conn = get_db()
    try:
        conn.execute("UPDATE admin_messages SET reply=?, status='replied' WHERE id=?", (data.reply, msg_id))
        conn.commit()
        return {"status": "replied"}
    finally:
        conn.close()

@app.post("/api/admin/allocate")
async def allocate_resource(data: AllocateRequest):
    conn = get_db()
    try:
        fleet_row = conn.execute("SELECT unit_type, hospital_name, contact FROM fleet WHERE unit_id = ?", (data.unit_id,)).fetchone()
        hospital = fleet_row["hospital_name"] if fleet_row else None
        unit_type = fleet_row["unit_type"] if fleet_row else None
        contact = fleet_row["contact"] if fleet_row else None
        conn.execute("""UPDATE incidents SET status='RESOURCE_ALLOCATED', assigned_unit=?,
            resource_contact=?, assigned_unit_type=?, assigned_hospital=? WHERE id=?""",
            (data.unit_id, contact, unit_type, hospital, data.incident_id))
        conn.execute("UPDATE fleet SET current_status='En Route', assigned_incident_id=? WHERE unit_id=?",
            (data.incident_id, data.unit_id))
        # Create step 1 (dispatch) as complete
        conn.execute("INSERT OR IGNORE INTO incident_steps (incident_id, step_number, step_name, status, completed_at) VALUES (?,1,'Resource Dispatched','completed',CURRENT_TIMESTAMP)",
            (data.incident_id,))
        conn.commit()
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

@app.post("/api/admin/followup")
async def update_followup(data: FollowupUpdateRequest):
    """Update incident followup steps (step 2, 3, 4)."""
    conn = get_db()
    try:
        if data.step == 2:
            conn.execute("UPDATE incidents SET step2_relatives_alerted=1, updated_at=CURRENT_TIMESTAMP WHERE id=?", (data.incident_id,))
            conn.execute("""INSERT INTO incident_steps (incident_id, step_number, step_name, status, notes, completed_at)
                VALUES (?,2,'Relatives Alerted','completed',?,CURRENT_TIMESTAMP)
                ON CONFLICT DO NOTHING""", (data.incident_id, data.notes or "Emergency contacts notified"))
        elif data.step == 3:
            conn.execute("UPDATE incidents SET step3_followup_status=?, followup_notes=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (data.status or "in_progress", data.notes or "", data.incident_id))
            conn.execute("""INSERT OR IGNORE INTO incident_steps (incident_id, step_number, step_name, status, notes, completed_at)
                VALUES (?,3,'Following Up',?,?,CURRENT_TIMESTAMP)""",
                (data.incident_id, data.status or "in_progress", data.notes or ""))
        elif data.step == 4:
            conn.execute("UPDATE incidents SET step4_safe_confirmed=1, safe_location=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (data.safe_location or "", data.incident_id))
            conn.execute("""INSERT OR IGNORE INTO incident_steps (incident_id, step_number, step_name, status, notes, completed_at)
                VALUES (?,4,'Person Safe','completed',?,CURRENT_TIMESTAMP)""",
                (data.incident_id, (data.safe_location or "") + " - " + (data.notes or "")))
        conn.commit()
        return {"status": "updated"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

@app.post("/api/admin/resolve-incident")
async def resolve_incident(data: ResolveRequest):
    conn = get_db()
    try:
        inc = conn.execute("SELECT * FROM incidents WHERE id = ?", (data.incident_id,)).fetchone()
        if not inc: raise HTTPException(status_code=404, detail="Incident not found")
        inc_dict = dict(inc)

        # Calculate resolution time
        res_mins = None
        try:
            t1 = datetime.fromisoformat(inc_dict.get("created_at",""))
            t2 = datetime.now()
            res_mins = int((t2 - t1).total_seconds() / 60)
        except: pass

        conn.execute("UPDATE incidents SET status='resolved', resolved_at=CURRENT_TIMESTAMP, report_generated=1, resolution_time_minutes=? WHERE id=?",
            (res_mins, data.incident_id))
        conn.execute("UPDATE fleet SET current_status='Standby', assigned_incident_id=NULL WHERE assigned_incident_id=?", (data.incident_id,))

        # Get steps
        steps = conn.execute("SELECT * FROM incident_steps WHERE incident_id=? ORDER BY step_number", (data.incident_id,)).fetchall()
        dup_count = conn.execute("SELECT COUNT(*) FROM duplicate_reports WHERE original_incident_id=?", (data.incident_id,)).fetchone()[0]

        inc_dict["resolution_time_minutes"] = res_mins
        report = generate_full_report(inc_dict, [dict(s) for s in steps], dup_count)

        existing = conn.execute("SELECT id FROM incident_reports WHERE incident_id = ?", (data.incident_id,)).fetchone()
        if existing:
            conn.execute("UPDATE incident_reports SET report_data=?, generated_at=CURRENT_TIMESTAMP WHERE incident_id=?",
                (json.dumps(report), data.incident_id))
        else:
            conn.execute("INSERT INTO incident_reports (incident_id, user_id, report_data) VALUES (?,?,?)",
                (data.incident_id, inc_dict.get("user_id",""), json.dumps(report)))
        conn.commit()
        return {"status": "resolved"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

@app.post("/api/admin/fleet/add")
async def fleet_add(data: FleetResourceRequest):
    conn = get_db()
    try:
        conn.execute("""INSERT INTO fleet (unit_id, unit_type, current_status, contact, city, station_name, driver_name, hospital_name, last_lat, last_lng)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (data.unit_id, data.unit_type, data.current_status, data.contact, data.city,
             data.station_name, data.driver_name, data.hospital_name,
             data.last_lat or 9.9312, data.last_lng or 76.2673))
        conn.commit()
        return {"status": "added"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()

@app.post("/api/admin/fleet/update")
async def fleet_update(data: FleetResourceRequest):
    conn = get_db()
    try:
        conn.execute("""UPDATE fleet SET unit_type=?, current_status=?, contact=?, city=?,
               station_name=?, driver_name=?, hospital_name=? WHERE unit_id=?""",
            (data.unit_type, data.current_status, data.contact, data.city,
             data.station_name, data.driver_name, data.hospital_name, data.unit_id))
        conn.commit()
        return {"status": "updated"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

@app.get("/api/incident/status/{incident_id}")
async def get_incident_status(incident_id: str):
    conn = get_db()
    try:
        row = conn.execute("""SELECT id, status, assigned_unit, resource_contact, assigned_unit_type,
            assigned_hospital, severity, category, lat, lng, step2_relatives_alerted,
            step3_followup_status, step4_safe_confirmed, safe_location FROM incidents WHERE id = ?""",
            (incident_id,)).fetchone()
        if not row: raise HTTPException(status_code=404, detail="Not found")
        result = dict(row)
        # Include admin replies
        msgs = conn.execute("SELECT reply, created_at FROM admin_messages WHERE incident_id=? AND reply IS NOT NULL AND reply != '' ORDER BY created_at DESC LIMIT 5",
            (incident_id,)).fetchall()
        result["admin_replies"] = [dict(m) for m in msgs]
        return result
    finally:
        conn.close()

@app.post("/api/incident/update-location")
async def update_incident_location(data: LocationUpdateRequest):
    conn = get_db()
    try:
        conn.execute("UPDATE incidents SET lat=?, lng=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (data.lat, data.lng, data.incident_id))
        conn.commit()
        return {"status": "updated"}
    finally:
        conn.close()

@app.get("/api/incidents/similar")
async def similar_incidents(incident_id: str = None, lat: float = None, lng: float = None):
    conn = get_db()
    try:
        rows = conn.execute("""SELECT id, category, severity, location_details, lat, lng, created_at,
            resolved_at, assigned_unit, assigned_hospital, description, resolution_time_minutes,
            step4_safe_confirmed, safe_location
            FROM incidents WHERE status IN ('resolved','closed') AND id != ?
            ORDER BY created_at DESC LIMIT 50""", (incident_id or "",)).fetchall()
        result = []
        for r in rows:
            dist = calculate_distance(lat, lng, r["lat"], r["lng"]) if lat and lng and r["lat"] and r["lng"] else 999
            if dist < 10:
                d = dict(r)
                d["distance_km"] = round(dist, 2)
                result.append(d)
        result.sort(key=lambda x: x.get("distance_km", 999))
        return result[:5]
    finally:
        conn.close()

@app.get("/api/incident/report/{incident_id}")
async def get_incident_report(incident_id: str):
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM incident_reports WHERE incident_id = ?", (incident_id,)).fetchone()
        if not row:
            # Try generating on-the-fly for resolved incidents
            inc = conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
            if not inc: raise HTTPException(status_code=404, detail="Report not generated yet")
            steps = conn.execute("SELECT * FROM incident_steps WHERE incident_id=? ORDER BY step_number", (incident_id,)).fetchall()
            dup_count = conn.execute("SELECT COUNT(*) FROM duplicate_reports WHERE original_incident_id=?", (incident_id,)).fetchone()[0]
            report = generate_full_report(dict(inc), [dict(s) for s in steps], dup_count)
            return {"incident_id": incident_id, "report": report, "generated_at": "on-demand"}
        return {"incident_id": incident_id, "report": json.loads(row["report_data"]), "generated_at": row["generated_at"]}
    finally:
        conn.close()

@app.get("/api/incident/steps/{incident_id}")
async def get_incident_steps(incident_id: str):
    conn = get_db()
    try:
        inc = conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
        if not inc: raise HTTPException(status_code=404, detail="Incident not found")
        steps = conn.execute("SELECT * FROM incident_steps WHERE incident_id=? ORDER BY step_number", (incident_id,)).fetchall()
        return {"incident": dict(inc), "steps": [dict(s) for s in steps]}
    finally:
        conn.close()

@app.post("/api/incidents/report-duplicate")
async def report_duplicate(data: DuplicateReportRequest):
    """User reports that the same incident is happening or is still unresolved."""
    conn = get_db()
    try:
        conn.execute("""INSERT INTO duplicate_reports (original_incident_id, reporter_user_id, reporter_name, message, still_unresolved)
            VALUES (?,?,?,?,?)""", (data.original_incident_id, data.reporter_user_id, data.reporter_name,
            data.message, 1 if data.still_unresolved else 0))
        conn.execute("UPDATE incidents SET duplicate_count=COALESCE(duplicate_count,0)+1 WHERE id=?",
            (data.original_incident_id,))
        conn.commit()
        return {"status": "reported"}
    finally:
        conn.close()

@app.get("/api/incidents/check-duplicate")
async def check_duplicate(lat: float, lng: float, category: str = ""):
    """Check if a similar incident already exists nearby."""
    conn = get_db()
    try:
        rows = conn.execute("""SELECT id, category, severity, status, location_details, lat, lng, created_at, user_name
            FROM incidents WHERE status NOT IN ('resolved','closed')
            ORDER BY created_at DESC LIMIT 50""").fetchall()
        result = []
        for r in rows:
            if r["lat"] and r["lng"]:
                dist = calculate_distance(lat, lng, r["lat"], r["lng"])
                if dist < 0.5:  # Within 500m
                    d = dict(r)
                    d["distance_km"] = round(dist, 3)
                    result.append(d)
        result.sort(key=lambda x: x.get("distance_km", 999))
        return result[:3]
    finally:
        conn.close()

# -- PWA manifest --
@app.get("/manifest.json")
async def manifest():
    return JSONResponse({
        "name": "GuardianSync Emergency",
        "short_name": "Guardian SOS",
        "description": "Emergency SOS & incident reporting for citizens",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#1c1917",
        "theme_color": "#dc2626",
        "categories": ["emergency", "utilities"],
        "icons": [
            {"src": "/static/icon.jpeg", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            # {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}
        ],
        "shortcuts": [
            {
                "name": "Emergency SOS",
                "short_name": "SOS",
                "description": "Start emergency SOS immediately",
                "url": "/user/sos",
                "icons": [{"src": "/static/icon.jpeg", "sizes": "192x192"}]
            }
        ]
    })

# -- Analyze endpoint --
@app.post("/analyze")
async def chat_analyze(request: ChatRequest):
    try:
        conn_check = get_db()
        incident_row = conn_check.execute(
            "SELECT status, assigned_unit, resource_contact, assigned_unit_type, assigned_hospital FROM incidents WHERE id = ?",
            (request.incident_id,)).fetchone()
        user_profile = conn_check.execute(
            "SELECT full_name, phone, blood_type, allergies, conditions, address FROM user_profiles WHERE user_id = ?",
            (request.user_id,)).fetchone()
        conn_check.close()

        dynamic_prompt = SYSTEM_PROMPT

        if user_profile and user_profile["full_name"]:
            p = dict(user_profile)
            dynamic_prompt += (
                "\n\nREGISTERED USER PROFILE ON FILE (DO NOT ask for this info again):\n"
                "Name: {full_name}\nPhone: {phone}\nBlood Type: {blood_type}\n"
                "Allergies: {allergies}\nConditions: {conditions}\nAddress: {address}\n"
                "SKIP identification phase. Go directly to TRIAGE & INSTRUCTION."
            ).format(**{k: (v or 'N/A') for k, v in p.items()})

        sos_labels = {"self": "the caller themselves", "relative": "a relative/known person", "stranger": "an unknown stranger"}
        dynamic_prompt += "\n\nSOS TYPE: This emergency is for {}.".format(sos_labels.get(request.sos_type or "self", "the caller"))

        if incident_row and incident_row["status"] == "RESOURCE_ALLOCATED" and incident_row["assigned_unit"]:
            unit_id = incident_row["assigned_unit"]
            contact = incident_row["resource_contact"] or "108"
            unit_type = incident_row["assigned_unit_type"] or "emergency unit"
            hospital = incident_row["assigned_hospital"] or ""
            eta_map = {"Ambulance": "8-12 minutes", "Fire Truck": "6-10 minutes", "Police": "5-8 minutes"}
            eta = eta_map.get(unit_type, "10-15 minutes")
            dynamic_prompt += (
                "\n\n*** DISPATCH ALERT -- RESOURCE ASSIGNED ***\n"
                "A {} (Unit: {}) has been dispatched. Contact: {}. ETA: {}. {}\n"
                "IMMEDIATELY announce this to the citizen. Give them the contact number.\n"
                "Tell them to stay at their location. Set status to 'closed'."
            ).format(unit_type, unit_id, contact, eta, "Taking to: " + hospital if hospital else "")

        history = [{"role": m.role, "content": m.content} for m in request.messages]
        visible_history = [h for h in history if not h["content"].startswith("[SYSTEM:")]

        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": dynamic_prompt}] + visible_history,
            response_format={"type": "json_object"}
        )

        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"): raw = raw[4:]
        raw = raw.strip()

        try:
            ai_data = json.loads(raw)
        except json.JSONDecodeError:
            ai_data = {"ai_response": raw, "category": "General", "severity": "Medium", "status": "collecting"}

        if "ai_response" not in ai_data:
            ai_data["ai_response"] = ai_data.get("response", ai_data.get("message", "I am here to help. Please continue."))

        last_msg = visible_history[-1]["content"] if visible_history else ""
        category = ai_data.get("category", "General")
        severity = ai_data.get("severity", "Medium")
        status = ai_data.get("status", "collecting")

        # Extract location if mentioned
        location_details = ai_data.get("location", "") or ai_data.get("location_details", "")
        landmark = ai_data.get("landmark", "")

        conn = get_db()
        for col, defn in [("transcript","TEXT"), ("description","TEXT"), ("category","TEXT"),
                          ("severity","TEXT"), ("lat","REAL"), ("lng","REAL"),
                          ("assigned_unit","TEXT"), ("resource_contact","TEXT"), ("sos_type","TEXT"),
                          ("location_details","TEXT"), ("landmark","TEXT")]:
            ensure_column(conn, "incidents", col, defn)

        conn.execute("""INSERT OR IGNORE INTO incidents
            (id, user_id, status, sos_type, category, severity, transcript, description, lat, lng, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
            (request.incident_id, request.user_id, status, request.sos_type,
             category, severity, last_msg, last_msg, request.device_lat, request.device_lng))
        conn.execute("""UPDATE incidents SET status=?, category=?, severity=?, transcript=?, description=?,
            lat=COALESCE(?, lat), lng=COALESCE(?, lng),
            location_details=CASE WHEN ? != '' THEN ? ELSE location_details END,
            landmark=CASE WHEN ? != '' THEN ? ELSE landmark END,
            updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (status, category, severity, last_msg, last_msg, request.device_lat, request.device_lng,
             location_details, location_details, landmark, landmark, request.incident_id))
        conn.commit()
        conn.close()
        return ai_data

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    
    uvicorn.run(app, host="0.0.0.0", port=8000)

# ==================== RESPONDER PORTAL ====================

class ResponderStatusUpdate(BaseModel):
    unit_id: str
    incident_id: str
    unit_status: str  # "reached_location" | "picked_up" | "going_to_hospital" | "completed"
    notes: Optional[str] = ""

class ResponderRegisterRequest(BaseModel):
    name: str
    email: str
    phone: str
    password: str
    unit_type: str
    station_name: str
    hospital_name: Optional[str] = ""
    city: str
    contact: str

@app.get("/responder/login")
async def responder_login_page(request: Request, error: str = None):
    return templates.TemplateResponse("responder/login.html", {"request": request, "error": error})

@app.post("/responder/login")
async def responder_do_login(request: Request):
    form = await request.form()
    email = form.get("email", "").strip().lower()
    password = form.get("password", "")
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE email = ? AND role IN ('responder','admin')", (email,)).fetchone()
    conn.close()
    if not user or user["password_hash"] != hash_password(password):
        return templates.TemplateResponse("responder/login.html", {
            "request": request, "error": "Invalid credentials or not a responder account."
        })
    response = RedirectResponse(url="/responder/dashboard", status_code=303)
    set_role_cookie(response, user["id"], "responder")
    return response

@app.post("/responder/register")
async def responder_register(request: Request):
    form = await request.form()
    name = form.get("name", "").strip()
    email = form.get("email", "").strip().lower()
    phone = form.get("phone", "").strip()
    password = form.get("password", "")
    unit_type = form.get("unit_type", "Ambulance")
    station_name = form.get("station_name", "").strip()
    hospital_name = form.get("hospital_name", "").strip()
    city = form.get("city", "Kochi").strip()
    contact = form.get("contact", phone).strip()

    if not all([name, email, password, unit_type, station_name]):
        return templates.TemplateResponse("responder/login.html", {
            "request": request, "error": "All fields are required.", "show_register": True
        })
    conn = get_db()
    existing = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if existing:
        conn.close()
        return templates.TemplateResponse("responder/login.html", {
            "request": request, "error": "Email already registered.", "show_register": True
        })
    uid = "resp-" + uuid.uuid4().hex[:8]
    # Auto-generate unit_id
    unit_prefix = {"Ambulance": "AMB", "Fire Truck": "FIRE", "Police": "POLICE"}.get(unit_type, "UNIT")
    city_code = city[:3].upper()
    unit_id = f"{unit_prefix}-{city_code}-{uuid.uuid4().hex[:4].upper()}"

    conn.execute("INSERT INTO users (id, name, email, phone, password_hash, role, unit_id) VALUES (?,?,?,?,?,?,?)",
        (uid, name, email, phone, hash_password(password), "responder", unit_id))
    # Add to fleet automatically
    conn.execute("""INSERT OR IGNORE INTO fleet
        (unit_id, unit_type, current_status, contact, city, station_name, driver_name, hospital_name, last_lat, last_lng, responder_user_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (unit_id, unit_type, "Standby", contact, city, station_name, name, hospital_name or None,
         9.9312, 76.2673, uid))
    conn.commit()
    conn.close()
    response = RedirectResponse(url="/responder/dashboard", status_code=303)
    set_role_cookie(response, uid, "responder")
    return response

@app.get("/responder/dashboard")
async def responder_dashboard(request: Request):
    user = get_session_user(request, "responder")
    if not user: return RedirectResponse(url="/responder/login")
    if user["role"] not in ("responder", "admin"):
        return RedirectResponse(url="/")
    conn = get_db()
    # Find this responder's unit
    fleet_row = conn.execute("SELECT * FROM fleet WHERE responder_user_id = ?", (user["id"],)).fetchone()
    active_incident = None
    if fleet_row and fleet_row["assigned_incident_id"]:
        inc = conn.execute("SELECT * FROM incidents WHERE id = ?", (fleet_row["assigned_incident_id"],)).fetchone()
        active_incident = dict(inc) if inc else None
    conn.close()
    return templates.TemplateResponse("responder/dashboard.html", {
        "request": request, "user": user,
        "fleet": dict(fleet_row) if fleet_row else None,
        "active_incident": active_incident
    })

@app.post("/api/responder/update-status")
async def responder_update_status(data: ResponderStatusUpdate):
    """Responder updates their field status (reached, picked up, going to hospital, etc.)"""
    conn = get_db()
    try:
        STATUS_MAP = {
            "reached_location": "Reached Location",
            "picked_up": "Picked Up Patient",
            "going_to_hospital": "Going to Hospital",
            "completed": "Completed",
        }
        friendly = STATUS_MAP.get(data.unit_status, data.unit_status)
        conn.execute("UPDATE incidents SET unit_status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (data.unit_status, data.incident_id))
        conn.execute("UPDATE fleet SET current_status=? WHERE unit_id=?",
            (friendly, data.unit_id))
        # Log as step 2.5 in incident_steps
        conn.execute("""INSERT INTO incident_steps (incident_id, step_number, step_name, status, notes, completed_at)
            VALUES (?,2,'Responder Update','completed',?,CURRENT_TIMESTAMP)""",
            (data.incident_id, f"{friendly}: {data.notes or ''}"))
        conn.commit()
        return {"status": "updated", "friendly_status": friendly}
    finally:
        conn.close()

@app.get("/api/responder/my-incident")
async def responder_my_incident(request: Request):
    user = get_session_user(request, "responder")
    if not user: raise HTTPException(status_code=401)
    conn = get_db()
    try:
        fleet_row = conn.execute("SELECT * FROM fleet WHERE responder_user_id=?", (user["id"],)).fetchone()
        if not fleet_row or not fleet_row["assigned_incident_id"]:
            return {"incident": None, "fleet": dict(fleet_row) if fleet_row else None}
        inc = conn.execute("SELECT * FROM incidents WHERE id=?", (fleet_row["assigned_incident_id"],)).fetchone()
        steps = conn.execute("SELECT * FROM incident_steps WHERE incident_id=? ORDER BY id DESC LIMIT 10",
            (fleet_row["assigned_incident_id"],)).fetchall()
        return {
            "incident": dict(inc) if inc else None,
            "fleet": dict(fleet_row),
            "steps": [dict(s) for s in steps]
        }
    finally:
        conn.close()

@app.get("/api/admin/fleet/detail/{unit_id}")
async def fleet_unit_detail(unit_id: str):
    """Get full detail for a fleet unit including current incident and crew."""
    conn = get_db()
    try:
        fleet = conn.execute("SELECT * FROM fleet WHERE unit_id=?", (unit_id,)).fetchone()
        if not fleet: raise HTTPException(status_code=404, detail="Unit not found")
        result = dict(fleet)
        if fleet["assigned_incident_id"]:
            inc = conn.execute("SELECT * FROM incidents WHERE id=?", (fleet["assigned_incident_id"],)).fetchone()
            result["active_incident"] = dict(inc) if inc else None
            steps = conn.execute("SELECT * FROM incident_steps WHERE incident_id=? ORDER BY id DESC LIMIT 8",
                (fleet["assigned_incident_id"],)).fetchall()
            result["incident_steps"] = [dict(s) for s in steps]
        else:
            result["active_incident"] = None
            result["incident_steps"] = []
        # Get responder user info
        if fleet["responder_user_id"]:
            resp = conn.execute("SELECT name, email, phone FROM users WHERE id=?", (fleet["responder_user_id"],)).fetchone()
            result["responder"] = dict(resp) if resp else None
        else:
            result["responder"] = None
        return result
    finally:
        conn.close()

@app.get("/api/admin/relatives/{incident_id}")
async def get_relatives_for_incident(incident_id: str):
    """Get emergency contacts for the user who filed the incident (for Step 3 alerting)."""
    conn = get_db()
    try:
        inc = conn.execute("SELECT user_id, user_name, phone, category, severity, location_details FROM incidents WHERE id=?",
            (incident_id,)).fetchone()
        if not inc: raise HTTPException(status_code=404, detail="Incident not found")
        contacts = conn.execute("SELECT * FROM emergency_contacts WHERE user_id=?", (inc["user_id"],)).fetchall()
        return {
            "incident": dict(inc),
            "contacts": [dict(c) for c in contacts]
        }
    finally:
        conn.close()

@app.get("/api/admin/seed-demo-data")
async def seed_demo_data():
    """Seed demo incidents if you want test data. Safe to call multiple times."""
    from database import seed_demo_incidents
    try:
        seed_demo_incidents()
        return {"status": "seeded"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
