import os
import sqlite3
import json
import time
import re
from typing import Dict, List, Any, Optional

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(APP_DIR, "app_data.db")

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn

def init_db():
    conn = get_db()
    try:
        cursor = conn.cursor()
        
        # Create users table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                phone TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                api_id INTEGER NOT NULL,
                api_hash TEXT NOT NULL,
                cycle_delay_min INTEGER DEFAULT 7,
                msg_delay_sec INTEGER DEFAULT 30,
                groups TEXT DEFAULT '[]',
                plan_expiry TEXT DEFAULT 'Lifetime',
                updated_at REAL DEFAULT 0.0
            );
        """)
        
        # Create errors table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                message TEXT NOT NULL,
                details TEXT,
                FOREIGN KEY(phone) REFERENCES users(phone) ON DELETE CASCADE
            );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_errors_phone ON errors(phone);")
        
        # Create settings table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)

        # Create analytics table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS analytics (
                phone TEXT NOT NULL,
                group_url TEXT NOT NULL,
                success_count INTEGER DEFAULT 0,
                fail_count INTEGER DEFAULT 0,
                last_status TEXT DEFAULT 'OK',
                last_sent_at REAL DEFAULT 0.0,
                PRIMARY KEY(phone, group_url),
                FOREIGN KEY(phone) REFERENCES users(phone) ON DELETE CASCADE
            );
        """)

        # Migration: Ensure proxy, group_map, blacklist, and is_paused columns exist on users table
        cursor.execute("PRAGMA table_info(users);")
        cols = [c[1] for c in cursor.fetchall()]
        if "proxy" not in cols:
            cursor.execute("ALTER TABLE users ADD COLUMN proxy TEXT DEFAULT NULL;")
        if "group_map" not in cols:
            cursor.execute("ALTER TABLE users ADD COLUMN group_map TEXT DEFAULT '{}';")
        if "blacklist" not in cols:
            cursor.execute("ALTER TABLE users ADD COLUMN blacklist TEXT DEFAULT '[]';")
        if "is_paused" not in cols:
            cursor.execute("ALTER TABLE users ADD COLUMN is_paused INTEGER DEFAULT 0;")
        
        conn.commit()
    finally:
        conn.close()
        
    # Auto-run migration
    migrate_old_json()

def migrate_old_json():
    users_file = os.path.join(APP_DIR, "users.json")
    autonight_file = os.path.join(APP_DIR, "autonight.json")
    users_dir = os.path.join(APP_DIR, "users")
    
    # Check if migration is needed
    if not (os.path.exists(users_file) or os.path.exists(autonight_file)):
        return
        
    print("[*] Migrating legacy JSON storage to SQLite database...")
    conn = get_db()
    try:
        cursor = conn.cursor()
        
        # 1. Migrate Autonight settings
        if os.path.exists(autonight_file):
            try:
                with open(autonight_file, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                cursor.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES ('autonight', ?)",
                    (json.dumps(cfg),)
                )
                conn.commit()
                os.remove(autonight_file)
                print("  [OK] Migrated autonight.json")
            except Exception as e:
                print(f"  [!] Failed to migrate autonight.json: {e}")
                
        # 2. Migrate Users and configs
        if os.path.exists(users_file):
            try:
                with open(users_file, "r", encoding="utf-8") as f:
                    users_registry = json.load(f)
                
                for phone, udata in users_registry.items():
                    name = udata.get("name", "Unknown")
                    api_id = udata.get("api_id")
                    api_hash = udata.get("api_hash")
                    if not api_id or not api_hash:
                        continue
                        
                    # Load detailed settings from users/{phone}.json
                    config_file = os.path.join(users_dir, f"{phone}.json")
                    cycle_delay_min = 7
                    msg_delay_sec = 30
                    groups = []
                    plan_expiry = "Lifetime"
                    
                    if os.path.exists(config_file):
                        try:
                            with open(config_file, "r", encoding="utf-8") as cf:
                                cdata = json.load(cf)
                            cycle_delay_min = cdata.get("cycle_delay_min", 7)
                            msg_delay_sec = cdata.get("msg_delay_sec", 30)
                            groups = _normalize_groups(cdata.get("groups", []))
                            plan_expiry = cdata.get("plan_expiry", "Lifetime")
                        except Exception:
                            pass
                            
                    cursor.execute(
                        """
                        INSERT OR REPLACE INTO users 
                        (phone, name, api_id, api_hash, cycle_delay_min, msg_delay_sec, groups, plan_expiry, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (phone, name, int(api_id), api_hash, cycle_delay_min, msg_delay_sec, json.dumps(groups), plan_expiry, time.time())
                    )
                    conn.commit()
                    print(f"  [OK] Migrated user credentials & config for {phone}")
                    
                    # Migrate errors if they exist
                    errors_file = os.path.join(users_dir, f"{phone}_errors.json")
                    if os.path.exists(errors_file):
                        try:
                            with open(errors_file, "r", encoding="utf-8") as ef:
                                errors_list = json.load(ef)
                            for err in errors_list:
                                cursor.execute(
                                    """
                                    INSERT INTO errors (phone, timestamp, message, details)
                                    VALUES (?, ?, ?, ?)
                                    """,
                                    (phone, err.get("timestamp"), err.get("message"), err.get("details"))
                                )
                            conn.commit()
                            os.remove(errors_file)
                            print(f"  [OK] Migrated error logs for {phone}")
                        except Exception as e:
                            print(f"  [!] Failed to migrate errors for {phone}: {e}")
                            
                    if os.path.exists(config_file):
                        try:
                            os.remove(config_file)
                        except Exception:
                            pass
                            
                os.remove(users_file)
                print("  [OK] Migrated users.json")
            except Exception as e:
                print(f"  [!] Failed to migrate users.json: {e}")
                
        # Clean up users/ dir if empty
        if os.path.exists(users_dir) and not os.listdir(users_dir):
            try:
                os.rmdir(users_dir)
            except Exception:
                pass
                
        print("[*] Legacy JSON migration completed successfully.")
    finally:
        conn.close()

# ---------- API functions for CLI and Runner ----------

def get_users_dict() -> Dict[str, Dict[str, Any]]:
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT phone, name, api_id, api_hash FROM users")
        rows = cursor.fetchall()
        return {
            row["phone"]: {
                "name": row["name"],
                "api_id": row["api_id"],
                "api_hash": row["api_hash"]
            }
            for row in rows
        }
    finally:
        conn.close()

def save_user(phone: str, name: str, api_id: int, api_hash: str):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO users (phone, name, api_id, api_hash, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(phone) DO UPDATE SET
                name=excluded.name,
                api_id=excluded.api_id,
                api_hash=excluded.api_hash,
                updated_at=excluded.updated_at
            """,
            (phone, name, api_id, api_hash, time.time())
        )
        conn.commit()
    finally:
        conn.close()

def delete_user(phone: str):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM users WHERE phone = ?", (phone,))
        conn.commit()
    finally:
        conn.close()

def _normalize_groups(val: Any) -> List[str]:
    """
    Ensures that groups is always converted to a clean List[str],
    handling strings, JSON-encoded strings, double-encoded JSON,
    numeric IDs (int/float), sets, tuples, None, or raw lists.
    """
    if val is None:
        return []
    if isinstance(val, (int, float)):
        return [str(val)]
    if isinstance(val, str):
        val = val.strip()
        if not val:
            return []
        while isinstance(val, str) and (val.startswith('"') or val.startswith("'")):
            try:
                parsed = json.loads(val)
                if isinstance(parsed, str):
                    val = parsed.strip()
                else:
                    val = parsed
                    break
            except Exception:
                break
        if isinstance(val, (int, float)):
            return [str(val)]
        if isinstance(val, str):
            try:
                parsed = json.loads(val)
                return _normalize_groups(parsed)
            except Exception:
                toks = [t.strip(" []\"'\t\r\n\\") for t in re.split(r'[\s,\n]+', val)]
                return _normalize_groups([t for t in toks if t])
    if isinstance(val, (list, tuple, set)):
        result = []
        for item in val:
            if isinstance(item, (int, float)):
                result.append(str(item))
            elif isinstance(item, str):
                item_str = item.strip(" []\"'\t\r\n\\")
                if item_str:
                    if item_str.startswith("@"):
                        item_str = f"https://t.me/{item_str[1:]}"
                    elif item_str.startswith("t.me/"):
                        item_str = f"https://{item_str}"
                    result.append(item_str)
            elif item is not None:
                result.extend(_normalize_groups(item))
        seen = set()
        dedup = []
        for x in result:
            if x not in seen:
                seen.add(x)
                dedup.append(x)
        return dedup
    return []

def get_user_config(phone: str) -> Optional[Dict[str, Any]]:
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT phone, name, api_id, api_hash, cycle_delay_min, msg_delay_sec, groups, plan_expiry, proxy, group_map, blacklist, is_paused FROM users WHERE phone = ?",
            (phone,)
        )
        row = cursor.fetchone()
        if not row:
            return None
        proxy_val = json.loads(row["proxy"]) if row["proxy"] else None
        gmap_val = {}
        if "group_map" in row.keys() and row["group_map"]:
            try:
                gmap_val = json.loads(row["group_map"])
            except Exception:
                gmap_val = {}
        blk_val = []
        if "blacklist" in row.keys() and row["blacklist"]:
            try:
                blk_val = json.loads(row["blacklist"])
            except Exception:
                blk_val = []
        is_p = bool(row["is_paused"]) if "is_paused" in row.keys() and row["is_paused"] else False
        return {
            "phone": row["phone"],
            "name": row["name"],
            "api_id": row["api_id"],
            "api_hash": row["api_hash"],
            "cycle_delay_min": row["cycle_delay_min"],
            "msg_delay_sec": row["msg_delay_sec"],
            "groups": _normalize_groups(row["groups"]),
            "plan_expiry": row["plan_expiry"],
            "proxy": proxy_val,
            "group_map": gmap_val if isinstance(gmap_val, dict) else {},
            "blacklist": _normalize_groups(blk_val),
            "is_paused": is_p
        }
    finally:
        conn.close()

def update_user_config(phone: str, **kwargs):
    conn = get_db()
    try:
        cursor = conn.cursor()
        set_clauses = []
        params = []
        for key, val in kwargs.items():
            if key == "groups":
                val = json.dumps(_normalize_groups(val))
            elif key == "blacklist":
                val = json.dumps(_normalize_groups(val))
            elif key == "group_map":
                val = json.dumps(val) if isinstance(val, dict) else (val or "{}")
            elif key == "proxy":
                val = json.dumps(val) if val is not None else None
            elif key == "is_paused":
                val = 1 if val else 0
            elif val is None:
                pass
            set_clauses.append(f"{key} = ?")
            params.append(val)
        if not set_clauses:
            return
        set_clauses.append("updated_at = ?")
        params.append(time.time())
        params.append(phone)
        
        query = f"UPDATE users SET {', '.join(set_clauses)} WHERE phone = ?"
        cursor.execute(query, params)
        conn.commit()
    finally:
        conn.close()

def get_all_user_configs() -> List[Dict[str, Any]]:
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT phone, name, api_id, api_hash, cycle_delay_min, msg_delay_sec, groups, plan_expiry, proxy, group_map, blacklist, is_paused, updated_at FROM users")
        rows = cursor.fetchall()
        res = []
        for r in rows:
            gmap_val = {}
            if "group_map" in r.keys() and r["group_map"]:
                try:
                    gmap_val = json.loads(r["group_map"])
                except Exception:
                    gmap_val = {}
            blk_val = []
            if "blacklist" in r.keys() and r["blacklist"]:
                try:
                    blk_val = json.loads(r["blacklist"])
                except Exception:
                    blk_val = []
            is_p = bool(r["is_paused"]) if "is_paused" in r.keys() and r["is_paused"] else False
            res.append({
                "phone": r["phone"],
                "name": r["name"],
                "api_id": r["api_id"],
                "api_hash": r["api_hash"],
                "cycle_delay_min": r["cycle_delay_min"],
                "msg_delay_sec": r["msg_delay_sec"],
                "groups": _normalize_groups(r["groups"]),
                "plan_expiry": r["plan_expiry"],
                "proxy": json.loads(r["proxy"]) if r["proxy"] else None,
                "group_map": gmap_val if isinstance(gmap_val, dict) else {},
                "blacklist": _normalize_groups(blk_val),
                "is_paused": is_p,
                "updated_at": r["updated_at"]
            })
        return res
    finally:
        conn.close()

def log_group_analytics(phone: str, group_url: str, success: bool, status_msg: str = "OK"):
    conn = get_db()
    try:
        cursor = conn.cursor()
        now_ts = time.time()
        if success:
            cursor.execute(
                """
                INSERT INTO analytics (phone, group_url, success_count, fail_count, last_status, last_sent_at)
                VALUES (?, ?, 1, 0, ?, ?)
                ON CONFLICT(phone, group_url) DO UPDATE SET
                    success_count = success_count + 1,
                    last_status = excluded.last_status,
                    last_sent_at = excluded.last_sent_at
                """,
                (phone, group_url, status_msg, now_ts)
            )
        else:
            cursor.execute(
                """
                INSERT INTO analytics (phone, group_url, success_count, fail_count, last_status, last_sent_at)
                VALUES (?, ?, 0, 1, ?, ?)
                ON CONFLICT(phone, group_url) DO UPDATE SET
                    fail_count = fail_count + 1,
                    last_status = excluded.last_status,
                    last_sent_at = excluded.last_sent_at
                """,
                (phone, group_url, status_msg, now_ts)
            )
        conn.commit()
    finally:
        conn.close()

def get_group_analytics(phone: str) -> List[Dict[str, Any]]:
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT group_url, success_count, fail_count, last_status, last_sent_at FROM analytics WHERE phone = ? ORDER BY (success_count + fail_count) DESC",
            (phone,)
        )
        rows = cursor.fetchall()
        return [
            {
                "group_url": r["group_url"],
                "success_count": r["success_count"],
                "fail_count": r["fail_count"],
                "last_status": r["last_status"],
                "last_sent_at": r["last_sent_at"]
            }
            for r in rows
        ]
    finally:
        conn.close()

def clear_analytics(phone: str):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM analytics WHERE phone = ?", (phone,))
        conn.commit()
    finally:
        conn.close()

def get_admin_id() -> Optional[int]:
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = 'admin_id'")
        row = cursor.fetchone()
        if row and row["value"]:
            try:
                return int(row["value"])
            except ValueError:
                return None
        return None
    finally:
        conn.close()

def save_admin_id(admin_id: Optional[int]):
    conn = get_db()
    try:
        cursor = conn.cursor()
        val = str(admin_id) if admin_id is not None else ""
        cursor.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('admin_id', ?)",
            (val,)
        )
        conn.commit()
    finally:
        conn.close()

def get_autonight_settings() -> Dict[str, Any]:
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = 'autonight'")
        row = cursor.fetchone()
        if row:
            return json.loads(row["value"])
        return {
            "enabled": True,
            "start": "00:00",
            "end": "06:00",
            "tz": "Asia/Kolkata"
        }
    finally:
        conn.close()

def save_autonight_settings(cfg: Dict[str, Any]):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('autonight', ?)",
            (json.dumps(cfg),)
        )
        conn.commit()
    finally:
        conn.close()

def log_error(phone: str, timestamp: str, message: str, details: Optional[str] = None):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO errors (phone, timestamp, message, details) VALUES (?, ?, ?, ?)",
            (phone, timestamp, message, details)
        )
        cursor.execute(
            """
            DELETE FROM errors 
            WHERE phone = ? 
              AND id NOT IN (
                  SELECT id FROM errors 
                  WHERE phone = ? 
                  ORDER BY id DESC 
                  LIMIT 15
              )
            """,
            (phone, phone)
        )
        conn.commit()
    finally:
        conn.close()

def get_errors(phone: str) -> List[Dict[str, Any]]:
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT timestamp, message, details FROM errors WHERE phone = ? ORDER BY id ASC",
            (phone,)
        )
        rows = cursor.fetchall()
        return [
            {
                "timestamp": r["timestamp"],
                "message": r["message"],
                "details": r["details"]
            }
            for r in rows
        ]
    finally:
        conn.close()

def clear_errors(phone: str):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM errors WHERE phone = ?", (phone,))
        conn.commit()
    finally:
        conn.close()

# Auto-initialize database on import
init_db()
