# Verified Python 3.11 compatible
import os
import sys
import subprocess

# Auto-install dependencies if missing
required_packages = {
    "telethon": "telethon==1.34.0",
    "colorama": "colorama==0.4.6",
    "socks": "PySocks==1.7.1"
}

missing_packages = []
for module_name, package_name in required_packages.items():
    try:
        __import__(module_name)
    except ImportError:
        missing_packages.append(package_name)

if missing_packages:
    print(f"[*] Missing dependencies detected: {missing_packages}")
    print("[*] Installing missing dependencies automatically...")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install"] + missing_packages)
        print("[*] Dependencies installed successfully. Restarting...")
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        print(f"[!] Auto-installation failed: {e}")
        print("[!] Please run: pip install -r requirements.txt")
        sys.exit(1)

import re
import json
import tempfile
import shutil
from datetime import datetime, timedelta, time
from typing import Dict, Any

try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except AttributeError:
    pass

import asyncio
try:
    asyncio.get_event_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

from telethon.sync import TelegramClient
from telethon.errors import (
    SessionPasswordNeededError,
    FloodWaitError,
    PhoneCodeInvalidError,
    PhoneNumberBannedError,
)
from colorama import Fore, Style, init

# ---------- Init ----------
init(autoreset=True)
import db

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SESSIONS_DIR = os.path.join(APP_DIR, "sessions")
PID_FILE = os.path.join(APP_DIR, "runner.pid")
RUNNER_LOG = os.path.join(APP_DIR, "runner.log")
AUTONIGHT_DEFAULT = {
    "enabled": True,
    "start": "00:00",
    "end": "06:00",
    "tz": "Asia/Kolkata"
}

def atomic_save_json(path: str, data: Any) -> bool:
    """Save JSON data to a file atomically using a temporary file."""
    temp_fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(temp_fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        try:
            os.replace(temp_path, path)
        except OSError:
            shutil.move(temp_path, path)
        return True
    except Exception as e:
        print(Fore.RED + f"  [!] Failed to save JSON to {path}: {e}")
        if os.path.exists(temp_path):
            os.remove(temp_path)
        return False




# ---------- Files & Config ----------
def ensure_dirs():
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    db.init_db()

def load_users() -> Dict[str, Any]:
    return db.get_users_dict()

def save_users(users: Dict[str, Any]) -> None:
    pass

def save_user_config(phone: str, data: Dict[str, Any]) -> None:
    db.save_user(phone, data["name"], int(data["api_id"]), data["api_hash"])

# ---------- UI Helpers ----------
def list_users(users: Dict[str, Any]) -> None:
    if not users:
        print(Fore.YELLOW + "  [!] No users logged in yet.")
        return
    print(Fore.CYAN + Style.BRIGHT + "\n  ⭐ Registered Premium Users:")
    print(Fore.CYAN + "  " + "─" * 45)
    for i, (phone, data) in enumerate(users.items(), 1):
        name = data.get('name', 'Unknown')
        print(f"  {Fore.WHITE}{i:<2} {Fore.GREEN}{name:<15} {Fore.WHITE}| {Fore.YELLOW}{phone:<15} {Fore.BLUE}| Lifetime")
    print(Fore.CYAN + "  " + "─" * 45)
    input(Fore.WHITE + "\n  Press Enter to return to menu...")



def is_runner_running() -> bool:
    pid_file = PID_FILE
    if not os.path.exists(pid_file):
        return False
    try:
        with open(pid_file, "r") as f:
            pid = int(f.read().strip())
    except Exception:
        return False

    if pid <= 0:
        return False

    if os.name == 'nt':
        try:
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            err = ctypes.windll.kernel32.GetLastError()
            return err == 5
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

def stop_runner():
    pid_file = PID_FILE
    pid = None
    if os.path.exists(pid_file):
        try:
            with open(pid_file, "r") as f:
                pid = int(f.read().strip())
        except Exception:
            pass

    if pid:
        print(Fore.YELLOW + f"  [🔁] Stopping background engine (PID: {pid})...")
        try:
            if os.name == 'nt':
                subprocess.run(f"taskkill /F /PID {pid}", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                import signal
                os.kill(pid, signal.SIGTERM)
                # Wait up to 3 seconds for the process to exit gracefully
                for _ in range(30):
                    import time as pytime
                    pytime.sleep(0.1)
                    try:
                        os.kill(pid, 0)
                    except OSError:
                        break
                else:
                    # Force kill if still alive
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass
        except Exception as e:
            print(Fore.RED + f"  [!] Failed to stop engine PID {pid}: {e}")

    # Backup: Terminate any orphaned/duplicate runner.py processes on the system
    print(Fore.YELLOW + "  [🔁] Cleaning up any remaining runner processes...")
    try:
        if os.name == 'nt':
            subprocess.run('wmic process where "CommandLine like \'%runner.py%\'" call terminate', shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.run("pkill -9 -f runner.py", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

    # Clean up PID file
    if os.path.exists(pid_file):
        try:
            os.remove(pid_file)
        except Exception:
            pass

def start_runner_if_needed():
    if is_runner_running():
        print(Fore.GREEN + "  [✔] Background engine is already running.")
        return
    
    # Remove stale pid file if exists
    if os.path.exists(PID_FILE):
        try:
            os.remove(PID_FILE)
        except Exception:
            pass

    import sys
    python_cmd = sys.executable
    app_dir = os.path.dirname(os.path.abspath(__file__))
    runner_path = os.path.join(app_dir, "runner.py")

    if os.name == "nt":
        # Windows: Start in a new console window so logs are visible
        try:
            subprocess.Popen(
                [python_cmd, runner_path],
                cwd=app_dir,
                creationflags=subprocess.CREATE_NEW_CONSOLE
            )
            print(Fore.CYAN + f"  [🔁] Background engine started in a NEW window.")
        except Exception as e:
            print(Fore.RED + f"  [!] Failed to start engine: {e}")
    else:
        # Linux: Start in background with log file
        log_path = RUNNER_LOG
        with open(log_path, "ab") as logf:
            subprocess.Popen(
                [python_cmd, runner_path],
                cwd=app_dir,
                stdout=logf,
                stderr=logf,
                start_new_session=True
            )
        print(Fore.CYAN + f"  [🔁] Background engine started successfully.")


# ---------- Auto-Night editor ----------
def _parse_hhmm(s: str) -> time:
    s = s.strip()
    if re.fullmatch(r"\d{1,2}", s):
        h = int(s)
        if not (0 <= h <= 23):
            raise ValueError("Hour must be 0..23")
        return time(h, 0)
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s)
    if not m:
        raise ValueError("Time must be HH or HH:MM (24h)")
    h, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mm <= 59):
        raise ValueError("Invalid time")
    return time(h, mm)

def show_autonight():
    cfg = db.get_autonight_settings()
    state = f"{Fore.GREEN}ACTIVE ✅" if cfg.get("enabled", True) else f"{Fore.RED}DISABLED ❌"
    print(Fore.MAGENTA + Style.BRIGHT + "\n  🌙 Auto-Night Configuration")
    print(Fore.WHITE + f"  Current Status : {state}")
    print(Fore.WHITE + f"  Quiet Window   : {Fore.YELLOW}{cfg.get('start','00:00')} → {cfg.get('end','06:00')}")
    print(Fore.WHITE + f"  Timezone       : {Fore.BLUE}{cfg.get('tz','Asia/Kolkata')}")
    print(Fore.CYAN + "  " + "─" * 40)

def edit_autonight():
    cfg = db.get_autonight_settings()

    print(Style.BRIGHT + "\n  Edit Auto-Night (Press Enter to skip)")
    en = input(f"  Enable? [y/n] (Current: {'on' if cfg.get('enabled',True) else 'off'}): ").strip().lower()
    if en in {"y", "yes", "on"}:
        cfg["enabled"] = True
    elif en in {"n", "no", "off"}:
        cfg["enabled"] = False

    start_in = input(f"  Start HH[:MM] (Current {cfg.get('start','00:00')}): ").strip()
    if start_in:
        try:
            t = _parse_hhmm(start_in)
            cfg["start"] = f"{t.hour:02d}:{t.minute:02d}"
        except ValueError as e:
            print(Fore.RED + f"  [!] Invalid start time: {e}")

    end_in = input(f"  End HH[:MM] (Current {cfg.get('end','06:00')}): ").strip()
    if end_in:
        try:
            t = _parse_hhmm(end_in)
            cfg["end"] = f"{t.hour:02d}:{t.minute:02d}"
        except ValueError as e:
            print(Fore.RED + f"  [!] Invalid end time: {e}")

    tz_in = input(f"  Timezone (Current {cfg.get('tz','Asia/Kolkata')}): ").strip()
    if tz_in:
        cfg["tz"] = tz_in

    db.save_autonight_settings(cfg)
    print(Fore.GREEN + "  [✔] Auto-Night settings updated.")
    show_autonight()

# ---------- Login / Delete ----------
def login_new_user(users: Dict[str, Any]):
    print(Fore.CYAN + "\n  [ NEW SESSION LOGIN ]")
    name = input("  Account Name: ").strip()
    api_id = input("  API ID: ").strip()
    api_hash = input("  API HASH: ").strip()
    phone_raw = input("  Phone (with +country): ").strip()

    if not api_id.isdigit():
        print(Fore.RED + "  [!] API ID must be numeric.")
        return

    phone = re.sub(r'[^\d+]', '', phone_raw)

    session_path = os.path.join(SESSIONS_DIR, f"{phone}.session")
    client = TelegramClient(session_path, int(api_id), api_hash)

    try:
        client.connect()
        if not client.is_user_authorized():
            try:
                client.send_code_request(phone)
            except FloodWaitError as e:
                print(Fore.RED + f"  [!] FloodWait: {e.seconds}s remaining.")
                return
            except Exception as e:
                print(Fore.RED + f"  [!] Failed to send verification code request.")
                print(Fore.RED + f"      Details: {e}")
                return

            code = input("  Enter Telegram Code: ").strip()
            try:
                client.sign_in(phone, code)
            except SessionPasswordNeededError:
                password = input("  2FA Password: ").strip()
                try:
                    client.sign_in(password=password)
                except Exception as e:
                    print(Fore.RED + f"  [!] 2FA Sign-in Failed: {e}")
                    return
            except Exception as e:
                print(Fore.RED + f"  [!] Sign-in Failed: {e}")
                return

        me = client.get_me()
        user_display = (getattr(me, "first_name", "") or getattr(me, "username", "") or str(me.id))
        print(Fore.GREEN + f"  [✔] Logged in as: {user_display}")

        users[phone] = {"name": name or user_display, "api_id": int(api_id), "api_hash": api_hash}
        save_users(users)
        save_user_config(phone, users[phone])

    finally:
        client.disconnect()

def delete_user(users: Dict[str, Any]):
    phone = input("  Phone number to delete: ").strip()
    if phone in users:
        session_file = os.path.join(SESSIONS_DIR, f"{phone}.session")
        if os.path.exists(session_file):
            try:
                os.remove(session_file)
            except Exception:
                pass
        db.delete_user(phone)
        print(Fore.RED + f"  [✖] Account {phone} removed.")
    else:
        print(Fore.YELLOW + "  [!] Account not found.")

def check_account_health(users: Dict[str, Any]):
    if not users:
        print(Fore.YELLOW + "  [!] No sessions found.")
        return
    
    print(Fore.CYAN + "\n  [ VERIFYING SESSIONS ]")
    print(Fore.CYAN + "  " + "─" * 40)
    for phone, data in users.items():
        session_path = os.path.join(SESSIONS_DIR, f"{phone}.session")
        if not os.path.exists(session_path):
            print(Fore.RED + f"  ✖ {phone:<15} | File Missing")
            continue
            
        client = TelegramClient(session_path, data["api_id"], data["api_hash"])
        try:
            client.connect()
            if not client.is_user_authorized():
                print(Fore.RED + f"  ✖ {phone:<15} | Session Revoked")
            else:
                print(Fore.GREEN + f"  ✔ {phone:<15} | Healthy")
        except Exception as e:
            print(Fore.RED + f"  ✖ {phone:<15} | Error: {type(e).__name__}")
        finally:
            client.disconnect()
    print(Fore.CYAN + "  " + "─" * 40)
    input(Fore.WHITE + "\n  Scan complete. Press Enter...")

def configure_proxy(users: Dict[str, Any]):
    if not users:
        print(Fore.YELLOW + "  [!] No users registered yet.")
        return
    print(Fore.CYAN + "\n  [ CONFIGURE ACCOUNT PROXY ]")
    user_list = list(users.items())
    for i, (phone, data) in enumerate(user_list, 1):
        print(f"  {i}. {data.get('name')} ({phone})")
    choice = input("\n  Select account number (or 0 to cancel): ").strip()
    if not choice.isdigit() or int(choice) < 1 or int(choice) > len(user_list):
        return
    
    phone, udata = user_list[int(choice) - 1]
    cfg = db.get_user_config(phone) or {}
    curr_proxy = cfg.get("proxy") or {}
    print(f"\n  Configuring proxy for {udata.get('name')} ({phone})")
    if curr_proxy:
        print(f"  Current Proxy: {curr_proxy.get('proxy_type', 'socks5')}://{curr_proxy.get('addr')}:{curr_proxy.get('port')}")
    else:
        print("  Current Proxy: None (Direct Connection)")
    
    enable = input("  Enable proxy for this account? [y/n]: ").strip().lower()
    if enable not in ('y', 'yes'):
        db.update_user_config(phone, proxy=None)
        print(Fore.GREEN + "  [✔] Proxy disabled for account.")
        return

    ptype = input("  Proxy type [socks5/socks4/http] (default: socks5): ").strip().lower() or "socks5"
    addr = input("  Proxy Host/IP: ").strip()
    port = input("  Proxy Port: ").strip()
    user = input("  Username (optional): ").strip()
    password = input("  Password (optional): ").strip()

    if not addr or not port.isdigit():
        print(Fore.RED + "  [!] Invalid Host or Port.")
        return

    proxy_data = {
        "proxy_type": ptype,
        "addr": addr,
        "port": int(port),
        "username": user or None,
        "password": password or None
    }
    db.update_user_config(phone, proxy=proxy_data)
    print(Fore.GREEN + f"  [✔] Proxy ({ptype}://{addr}:{port}) configured successfully for {phone}.")

def configure_admin_id():
    curr = db.get_admin_id()
    print(Fore.CYAN + "\n  [ REMOTE ADMIN TELEGRAM ID ]")
    print(Fore.WHITE + f"  Current Admin ID: {Fore.YELLOW}{curr or 'Not Set'}")
    new_id = input("  Enter Telegram User ID for remote admin commands (or press Enter to clear): ").strip()
    if not new_id:
        db.save_admin_id(None)
        print(Fore.YELLOW + "  [!] Remote Admin ID cleared.")
    elif new_id.isdigit():
        db.save_admin_id(int(new_id))
        print(Fore.GREEN + f"  [✔] Remote Admin ID set to {new_id}.")
    else:
        print(Fore.RED + "  [!] User ID must be numeric.")

def view_account_analytics(users: Dict[str, Any]):
    if not users:
        print(Fore.YELLOW + "  [!] No users registered yet.")
        return
    print(Fore.CYAN + "\n  [ ACCOUNT GROUP PERFORMANCE ANALYTICS ]")
    user_list = list(users.items())
    for i, (phone, data) in enumerate(user_list, 1):
        print(f"  {i}. {data.get('name')} ({phone})")
    choice = input("\n  Select account number (or 0 to cancel): ").strip()
    if not choice.isdigit() or int(choice) < 1 or int(choice) > len(user_list):
        return
    phone, udata = user_list[int(choice) - 1]
    stats = db.get_group_analytics(phone)
    print(Fore.CYAN + f"\n  📊 Performance Analytics for {udata.get('name')} ({phone}):")
    print(Fore.CYAN + "  " + "─" * 60)
    if not stats:
        print(Fore.YELLOW + "  [!] No analytics recorded for this account yet.")
    else:
        for idx, s in enumerate(stats[:25], 1):
            tot = s["success_count"] + s["fail_count"]
            rate = (s["success_count"] / tot * 100) if tot > 0 else 0
            print(f"  {idx:<2}. {s['group_url']:<35} | {Fore.GREEN}✔ {s['success_count']:<3} {Fore.RED}✖ {s['fail_count']:<3} {Fore.WHITE}| {rate:>5.1f}% | {s['last_status']}")
    print(Fore.CYAN + "  " + "─" * 60)
    input(Fore.WHITE + "\n  Press Enter to return...")

def manage_blacklist(users: Dict[str, Any]):
    if not users:
        print(Fore.YELLOW + "  [!] No users registered yet.")
        return
    print(Fore.CYAN + "\n  [ GROUP BLACKLIST / QUARANTINE MANAGER ]")
    user_list = list(users.items())
    for i, (phone, data) in enumerate(user_list, 1):
        print(f"  {i}. {data.get('name')} ({phone})")
    choice = input("\n  Select account number (or 0 to cancel): ").strip()
    if not choice.isdigit() or int(choice) < 1 or int(choice) > len(user_list):
        return
    phone, udata = user_list[int(choice) - 1]
    cfg = db.get_user_config(phone) or {}
    blk = cfg.get("blacklist", [])
    print(Fore.CYAN + f"\n  🛡️ Quarantined Groups for {udata.get('name')} ({phone}): {len(blk)}")
    for idx, b in enumerate(blk, 1):
        print(f"  {idx}. {b}")
    print(Fore.WHITE + "\n  Options: [1] Clear Blacklist  [2] Return to Menu")
    opt = input("  Select option: ").strip()
    if opt == "1":
        db.update_user_config(phone, blacklist=[])
        print(Fore.GREEN + "  [✔] Blacklist cleared successfully.")

def toggle_pause(users: Dict[str, Any]):
    if not users:
        print(Fore.YELLOW + "  [!] No users registered yet.")
        return
    print(Fore.CYAN + "\n  [ PAUSE / RESUME ENGINE TOGGLE ]")
    user_list = list(users.items())
    for i, (phone, data) in enumerate(user_list, 1):
        cfg = db.get_user_config(phone) or {}
        p_status = f"{Fore.RED}Paused ⏸️" if cfg.get("is_paused") else f"{Fore.GREEN}Active ▶️"
        print(f"  {i}. {data.get('name')} ({phone}) - Status: {p_status}")
    choice = input("\n  Select account number (or 0 to cancel): ").strip()
    if not choice.isdigit() or int(choice) < 1 or int(choice) > len(user_list):
        return
    phone, udata = user_list[int(choice) - 1]
    cfg = db.get_user_config(phone) or {}
    new_state = not cfg.get("is_paused", False)
    db.update_user_config(phone, is_paused=new_state)
    state_str = "PAUSED ⏸️" if new_state else "RESUMED ▶️"
    print(Fore.GREEN + f"  [✔] Account engine for {phone} is now {state_str}.")

# ---------- Main Menu ----------
def start():
    ensure_dirs()
    while True:
        users = load_users()
        total_users = len(users)
        admin_id = db.get_admin_id()
        admin_str = str(admin_id) if admin_id else "Not Set"
        
        banner = f"""
{Fore.CYAN}{Style.BRIGHT}╔══════════════════════════════════════════════════════╗
║        {Fore.YELLOW}TELETHON MULTI-USER MANAGER V5 ELITE{Fore.CYAN}          ║
║           {Fore.GREEN}Lifetime Premium Access Enabled{Fore.CYAN}            ║
╠══════════════════════════════════════════════════════╣
║  {Fore.WHITE}Registered Sessions: {Fore.YELLOW}{total_users:<2}                             {Fore.CYAN}║
║  {Fore.WHITE}Remote Admin ID:     {Fore.YELLOW}{admin_str:<15}                {Fore.CYAN}║
╚══════════════════════════════════════════════════════╝{Style.RESET_ALL}"""

        print(banner)
        print(Fore.WHITE + Style.BRIGHT + "  [ MAIN CONTROL PANEL ]\n")
        print(f"  {Fore.CYAN}1.{Fore.WHITE} List Registered Users")
        print(f"  {Fore.CYAN}2.{Fore.WHITE} {Fore.GREEN}Login New Premium Session")
        print(f"  {Fore.CYAN}3.{Fore.WHITE} {Fore.RED}Delete User Data")
        print(f"  {Fore.CYAN}─" * 25)
        print(f"  {Fore.CYAN}4.{Fore.WHITE} View Auto-Night Status")
        print(f"  {Fore.CYAN}5.{Fore.WHITE} Config Auto-Night Mode")
        print(f"  {Fore.CYAN}─" * 25)
        print(f"  {Fore.CYAN}6.{Fore.WHITE} {Fore.YELLOW}RESTART BACKGROUND ENGINE")
        print(f"  {Fore.CYAN}7.{Fore.WHITE} {Fore.BLUE}Account Health Verification")
        print(f"  {Fore.CYAN}8.{Fore.WHITE} {Fore.MAGENTA}Configure Account Proxy")
        print(f"  {Fore.CYAN}9.{Fore.WHITE} Set Remote Admin Telegram ID")
        print(f"  {Fore.CYAN}─" * 25)
        print(f"  {Fore.CYAN}10.{Fore.WHITE} View Group Analytics")
        print(f"  {Fore.CYAN}11.{Fore.WHITE} Manage Group Blacklist / Quarantine")
        print(f"  {Fore.CYAN}12.{Fore.WHITE} Pause / Resume Engine")
        print(f"  {Fore.CYAN}13.{Fore.WHITE} Close Manager")
        
        choice = input(Fore.YELLOW + "\n  ❯ Select an option [1-13]: " + Style.RESET_ALL).strip()

        if choice == '1':
            list_users(users)
        elif choice == '2':
            login_new_user(users)
        elif choice == '3':
            delete_user(users)
        elif choice == '4':
            show_autonight()
        elif choice == '5':
            edit_autonight()
        elif choice == '6':
            stop_runner()
            import time as pytime
            pytime.sleep(1.5)
            start_runner_if_needed()
        elif choice == '7':
            check_account_health(users)
        elif choice == '8':
            configure_proxy(users)
        elif choice == '9':
            configure_admin_id()
        elif choice == '10':
            view_account_analytics(users)
        elif choice == '11':
            manage_blacklist(users)
        elif choice == '12':
            toggle_pause(users)
        elif choice == '13':
            print(Fore.CYAN + "\n  Goodbye!")
            break
        else:
            print(Fore.RED + "  [!] Invalid selection.")

if __name__ == "__main__":
    try:
        start()
    except KeyboardInterrupt:
        print(Fore.CYAN + "\n  Exiting...")
