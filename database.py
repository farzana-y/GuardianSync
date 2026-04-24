# -*- coding: utf-8 -*-
import sqlite3
import os
import uuid
import hashlib
from datetime import datetime, timedelta
import random

DB_PATH = "emergency_system.db"

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def ensure_column(conn, table, column, definition):
    columns = [row["name"] for row in conn.execute("PRAGMA table_info({})".format(table)).fetchall()]
    if column not in columns:
        conn.execute("ALTER TABLE {} ADD COLUMN {} {}".format(table, column, definition))

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def init_db():
    conn = get_db()

    conn.execute('''CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        phone TEXT,
        password_hash TEXT NOT NULL,
        role TEXT DEFAULT "user",
        unit_id TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')

    conn.execute('''CREATE TABLE IF NOT EXISTS user_profiles (
        user_id TEXT PRIMARY KEY,
        medical_id TEXT UNIQUE,
        full_name TEXT,
        dob TEXT,
        aadhar_number TEXT,
        address TEXT,
        blood_type TEXT,
        weight TEXT,
        allergies TEXT,
        conditions TEXT,
        phone TEXT,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')

    conn.execute('''CREATE TABLE IF NOT EXISTS emergency_contacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        name TEXT,
        relation TEXT,
        phone TEXT)''')

    conn.execute('''CREATE TABLE IF NOT EXISTS incidents (
        id TEXT PRIMARY KEY,
        user_id TEXT,
        user_name TEXT,
        phone TEXT,
        sos_type TEXT DEFAULT "self",
        affected_name TEXT,
        affected_medical_id TEXT,
        category TEXT,
        severity TEXT,
        status TEXT DEFAULT "pending",
        description TEXT,
        transcript TEXT,
        location_details TEXT,
        landmark TEXT,
        lat REAL,
        lng REAL,
        assigned_unit TEXT,
        resource_contact TEXT,
        assigned_unit_type TEXT,
        assigned_hospital TEXT,
        tracking_lat REAL,
        tracking_lng REAL,
        resolved_at DATETIME,
        report_generated INTEGER DEFAULT 0,
        duplicate_of TEXT,
        duplicate_count INTEGER DEFAULT 0,
        step2_relatives_alerted INTEGER DEFAULT 0,
        step3_followup_status TEXT DEFAULT "pending",
        step4_safe_confirmed INTEGER DEFAULT 0,
        followup_notes TEXT,
        safe_location TEXT,
        resolution_time_minutes INTEGER,
        unit_status TEXT DEFAULT "dispatched",
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')

    conn.execute('''CREATE TABLE IF NOT EXISTS fleet (
        unit_id TEXT PRIMARY KEY,
        unit_type TEXT,
        current_status TEXT DEFAULT "Standby",
        last_lat REAL,
        last_lng REAL,
        contact TEXT,
        city TEXT,
        station_name TEXT,
        driver_name TEXT,
        hospital_name TEXT,
        assigned_incident_id TEXT,
        responder_user_id TEXT)''')

    conn.execute('''CREATE TABLE IF NOT EXISTS incident_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        incident_id TEXT UNIQUE,
        user_id TEXT,
        report_data TEXT,
        generated_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')

    conn.execute('''CREATE TABLE IF NOT EXISTS admin_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        user_name TEXT,
        incident_id TEXT,
        message TEXT,
        reply TEXT,
        status TEXT DEFAULT "unread",
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')

    conn.execute('''CREATE TABLE IF NOT EXISTS transcripts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        incident_id TEXT,
        user_id TEXT,
        role TEXT,
        text TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')

    conn.execute('''CREATE TABLE IF NOT EXISTS incident_steps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        incident_id TEXT,
        step_number INTEGER,
        step_name TEXT,
        status TEXT DEFAULT "pending",
        notes TEXT,
        completed_at DATETIME,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')

    conn.execute('''CREATE TABLE IF NOT EXISTS duplicate_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        original_incident_id TEXT,
        reporter_user_id TEXT,
        reporter_name TEXT,
        message TEXT,
        still_unresolved INTEGER DEFAULT 0,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')

    migration_cols = [
        ("incidents", "user_name", "TEXT"), ("incidents", "phone", "TEXT"),
        ("incidents", "location_details", "TEXT"), ("incidents", "category", "TEXT"),
        ("incidents", "severity", "TEXT"), ("incidents", "lat", "REAL"),
        ("incidents", "lng", "REAL"), ("incidents", "assigned_unit", "TEXT"),
        ("incidents", "resource_contact", "TEXT"), ("incidents", "description", "TEXT"),
        ("incidents", "resolved_at", "DATETIME"), ("incidents", "transcript", "TEXT"),
        ("incidents", "sos_type", "TEXT"), ("incidents", "affected_name", "TEXT"),
        ("incidents", "affected_medical_id", "TEXT"), ("incidents", "landmark", "TEXT"),
        ("incidents", "assigned_unit_type", "TEXT"), ("incidents", "assigned_hospital", "TEXT"),
        ("incidents", "tracking_lat", "REAL"), ("incidents", "tracking_lng", "REAL"),
        ("incidents", "report_generated", "INTEGER"),
        ("incidents", "duplicate_of", "TEXT"), ("incidents", "duplicate_count", "INTEGER"),
        ("incidents", "step2_relatives_alerted", "INTEGER"),
        ("incidents", "step3_followup_status", "TEXT"),
        ("incidents", "step4_safe_confirmed", "INTEGER"),
        ("incidents", "followup_notes", "TEXT"), ("incidents", "safe_location", "TEXT"),
        ("incidents", "resolution_time_minutes", "INTEGER"),
        ("incidents", "unit_status", "TEXT"),
        ("user_profiles", "medical_id", "TEXT"), ("user_profiles", "full_name", "TEXT"),
        ("user_profiles", "dob", "TEXT"), ("user_profiles", "aadhar_number", "TEXT"),
        ("user_profiles", "address", "TEXT"), ("user_profiles", "phone", "TEXT"),
        ("fleet", "station_name", "TEXT"), ("fleet", "driver_name", "TEXT"),
        ("fleet", "hospital_name", "TEXT"), ("fleet", "responder_user_id", "TEXT"),
        ("admin_messages", "reply", "TEXT"),
        ("users", "unit_id", "TEXT"),
    ]
    for table, col, defn in migration_cols:
        ensure_column(conn, table, col, defn)

    # --- Seed essential accounts only (no demo incidents) ---
    conn.execute("INSERT OR IGNORE INTO users (id, name, email, phone, password_hash, role) VALUES (?,?,?,?,?,?)",
        ('admin-1', 'Admin Farzana', 'admin@guardiansync.in', '+91 98765 00001', hash_password('admin123'), 'admin'))
    conn.execute("INSERT OR IGNORE INTO users (id, name, email, phone, password_hash, role) VALUES (?,?,?,?,?,?)",
        ('demo-user-1', 'Farzana Ashraf', 'farzana@guardiansync.in', '+91 98765 43210', hash_password('demo123'), 'user'))
    conn.execute("""INSERT OR IGNORE INTO user_profiles
        (user_id, medical_id, full_name, dob, aadhar_number, address, blood_type, weight, allergies, conditions, phone)
        VALUES ('demo-user-1', 'GS-FA01', 'Farzana Ashraf', '1998-06-15',
        '9876 5432 1012', 'Flat 4B, Marine Drive Residency, Ernakulam, Kochi - 682011',
        'B+', '58', 'Penicillin', 'Mild Asthma (2020)', '+91 98765 43210')""")

    contact_count = conn.execute("SELECT COUNT(*) FROM emergency_contacts WHERE user_id='demo-user-1'").fetchone()[0]
    if contact_count == 0:
        conn.executemany("INSERT INTO emergency_contacts (user_id, name, relation, phone) VALUES (?,?,?,?)", [
            ('demo-user-1', 'Ahmed Ashraf', 'Father', '+91 94470 12345'),
            ('demo-user-1', 'Sana Ashraf', 'Mother', '+91 94470 67890'),
        ])

    # Seed fleet only if empty
    fleet_count = conn.execute("SELECT COUNT(*) FROM fleet").fetchone()[0]
    if fleet_count == 0:
        kochi_units = [
            ('AMB-KOCHI-01', 'Ambulance', 'Standby', 9.9760, 76.2840, '108', 'Kochi', 'General Hospital Kochi', 'Rajan K.', 'General Hospital Kochi', None),
            ('AMB-KOCHI-02', 'Ambulance', 'En Route', 9.9643, 76.2948, '108', 'Kochi', 'Aster Medcity', 'Priya M.', 'Aster Medcity', None),
            ('AMB-KOCHI-03', 'Ambulance', 'Standby', 9.9558, 76.3002, '108', 'Kochi', 'Medical Trust Hospital', 'Dinesh R.', 'Medical Trust Hospital', None),
            ('FIRE-KCH-01', 'Fire Truck', 'Standby', 9.9650, 76.2420, '101', 'Kochi', 'Ernakulam Fire Station', 'Suresh P.', None, None),
            ('FIRE-KCH-02', 'Fire Truck', 'En Route', 9.9816, 76.2999, '101', 'Kochi', 'Fort Kochi Fire Station', 'Anil T.', None, None),
            ('POLICE-KCH-01', 'Police', 'Standby', 9.9816, 76.2999, '112', 'Kochi', 'Ernakulam Central', 'Inspector Nair', None, None),
            ('POLICE-KCH-02', 'Police', 'Standby', 9.9501, 76.3266, '112', 'Kochi', 'Kakkanad Police Station', 'SI Divya R.', None, None),
            ('POLICE-KCH-03', 'Police', 'Unavailable', 9.9312, 76.2673, '112', 'Kochi', 'Marine Drive Post', 'HC Biju', None, None),
        ]
        conn.executemany("""
            INSERT INTO fleet (unit_id, unit_type, current_status, last_lat, last_lng, contact, city, station_name, driver_name, hospital_name, responder_user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, kochi_units)

    conn.commit()
    conn.close()
    print("Database initialized successfully.")


def seed_demo_incidents():
    """Run this separately if you want demo incident data."""
    conn = get_db()
    now = datetime.now()
    categories = ['Medical', 'Fire', 'Security', 'Accident', 'Flood', 'Medical', 'Medical', 'Fire']
    severities = ['Critical', 'High', 'Medium', 'Low']
    sev_weights = [0.2, 0.3, 0.35, 0.15]
    sos_types = ['self', 'self', 'relative', 'stranger']
    locations = [
        (9.9312, 76.2673, 'Marine Drive, Ernakulam'),
        (9.9816, 76.2999, 'MG Road, Kochi'),
        (9.9558, 76.3002, 'Kaloor Junction, Kochi'),
        (9.9643, 76.2948, 'Lulu Mall Area, Kochi'),
        (9.9760, 76.2840, 'Vytilla Hub, Kochi'),
        (9.9501, 76.3266, 'Kakkanad IT Park, Kochi'),
        (9.9650, 76.2420, 'Fort Kochi, Kerala'),
        (10.0027, 76.3120, 'Aluva Junction, Kochi'),
    ]
    user_names = ['Arjun Menon', 'Priya Nair', 'Rahul Das', 'Sneha Pillai', 'Arun Kumar',
                  'Divya Krishnan', 'Sanjay Varma', 'Meera Suresh', 'Kiran Thomas', 'Ananya Raj']
    descriptions = {
        'Medical': ['Person collapsed, unresponsive.', 'Elderly patient with chest pain.', 'Head injury near flyover.'],
        'Fire': ['Kitchen fire spreading fast.', 'Electrical short circuit fire.'],
        'Security': ['Chain snatching near ATM.', 'Domestic violence reported.'],
        'Accident': ['Two-vehicle collision, two injured.', 'Biker hit by car, serious injury.'],
        'Flood': ['Road flooded, vehicles stranded.'],
    }
    units = ['AMB-KOCHI-01', 'AMB-KOCHI-02', 'FIRE-KCH-01', 'POLICE-KCH-01', None, None]
    random.seed(42)
    incidents = []
    for day_offset in range(7):
        base_date = now - timedelta(days=day_offset)
        for _ in range(random.randint(2, 5)):
            inc_id = 'KCH-' + uuid.uuid4().hex[:6].upper()
            cat = random.choice(categories)
            sev = random.choices(severities, weights=sev_weights)[0]
            loc = random.choice(locations)
            name = random.choice(user_names)
            desc = random.choice(descriptions.get(cat, ['Emergency reported.']))
            unit = random.choice(units)
            sos_type = random.choice(sos_types)
            hour = random.randint(6, 23)
            inc_time = base_date.replace(hour=hour, minute=random.randint(0, 59), second=0, microsecond=0)
            if day_offset >= 1:
                status = random.choices(['resolved', 'closed', 'RESOURCE_ALLOCATED'], weights=[0.6, 0.3, 0.1])[0]
            else:
                status = random.choices(['voice_active', 'collecting', 'RESOURCE_ALLOCATED', 'resolved'], weights=[0.2, 0.3, 0.3, 0.2])[0]
            resolved_at = None
            resolution_time = None
            if status in ('resolved', 'closed'):
                res_mins = random.randint(15, 90)
                resolved_at = (inc_time + timedelta(minutes=res_mins)).strftime('%Y-%m-%d %H:%M:%S')
                resolution_time = res_mins
            assigned = unit if status in ('RESOURCE_ALLOCATED', 'resolved', 'closed') else None
            hospital = None
            if assigned and 'AMB' in (assigned or ''):
                hospital = random.choice(['General Hospital Kochi', 'Aster Medcity', 'Medical Trust Hospital'])
            safe_location = None
            step4_safe = 0
            if status in ('resolved', 'closed'):
                step4_safe = 1
                safe_location = hospital or ('Ernakulam Central Police Station' if assigned and 'POLICE' in (assigned or '') else 'Home / Safe Location')
            incidents.append((inc_id, 'demo-user-1', name,
                '+91 9{}'.format(random.randint(100000000, 999999999)),
                sos_type, cat, sev, status, desc, desc, loc[2],
                loc[0], loc[1], assigned,
                '108' if assigned and 'AMB' in (assigned or '') else ('101' if assigned and 'FIRE' in (assigned or '') else '112') if assigned else None,
                hospital, resolved_at,
                inc_time.strftime('%Y-%m-%d %H:%M:%S'),
                inc_time.strftime('%Y-%m-%d %H:%M:%S'),
                step4_safe, safe_location, resolution_time))
    conn.executemany("""
        INSERT OR IGNORE INTO incidents
        (id, user_id, user_name, phone, sos_type, category, severity, status, description, transcript,
         location_details, lat, lng, assigned_unit, resource_contact, assigned_hospital,
         resolved_at, created_at, updated_at, step4_safe_confirmed, safe_location, resolution_time_minutes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, incidents)
    conn.commit()
    conn.close()
    print("Demo incidents seeded: {}".format(len(incidents)))


if __name__ == "__main__":
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
        print("Old database removed.")
    init_db()
    # Uncomment below to also seed demo incidents:
    # seed_demo_incidents()
