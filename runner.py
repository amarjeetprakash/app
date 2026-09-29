# Verified Python 3.11 compatible
import os
import sys
import subprocess

# Auto-install dependencies if missing
required_packages = {
    "telethon": "telethon==1.34.0",
    "colorama": "colorama==0.4.6"
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

import json
import asyncio
import logging
import sqlite3
import re
import random
import signal
import tempfile
import time as time_mod
from datetime import datetime, date, time as dt_time, timedelta
from typing import Tuple, List, Optional, Any

try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except AttributeError:
    pass

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:
    ZoneInfo = None  # will fall back to local time without TZ

from telethon import TelegramClient, events
from telethon.errors import (
    SessionPasswordNeededError, 
    RPCError, 
    FloodWaitError, 
    ChatWriteForbiddenError,
    SlowModeWaitError,
    UserBannedInChannelError,
    ChannelPrivateError,
    ChatAdminRequiredError,
    PeerFloodError,
)
import telethon.utils as tel_utils
from colorama import Fore, Style, init

init(autoreset=True)
import db


# =========================
# Auto-Night configuration
# =========================
DEFAULT_AUTONIGHT = {
    "enabled": True,
    "start": "00:00",        # 24h format HH:MM
    "end": "06:00",          # 24h format HH:MM
    "tz": "Asia/Kolkata"
}

def parse_spintax(text: str) -> str:
    """
    Parses Spintax pattern like '{Hello|Hi|Hey} {friend|there}!' recursively.
    """
    if not text:
        return text
    pattern = re.compile(r'\{([^{}]+)\}')
    while True:
        match = pattern.search(text)
        if not match:
            break
        options = match.group(1).split('|')
        choice = random.choice(options)
        text = text[:match.start()] + choice + text[match.end():]
    return text

def get_telethon_proxy(proxy_cfg: Optional[dict]):
    """
    Constructs Telethon-compatible proxy configuration tuple.
    """
    if not proxy_cfg or not isinstance(proxy_cfg, dict):
        return None
    ptype = str(proxy_cfg.get("proxy_type", "socks5")).lower()
    addr = proxy_cfg.get("addr") or proxy_cfg.get("host")
    port = proxy_cfg.get("port")
    if not addr or not port:
        return None

    try:
        import socks
        socks_map = {
            "socks5": socks.SOCKS5,
            "socks4": socks.SOCKS4,
            "http": socks.HTTP
        }
        stype = socks_map.get(ptype, socks.SOCKS5)
        return (
            stype,
            str(addr),
            int(port),
            True,
            proxy_cfg.get("username") or None,
            proxy_cfg.get("password") or None
        )
    except ImportError:
        logger.warning("PySocks not installed. Proxy ignored.")
        return None

def _load_autonight() -> dict:
    return db.get_autonight_settings()

def _save_autonight(cfg: dict) -> None:
    db.save_autonight_settings(cfg)

def _parse_hhmm(s: str) -> dt_time:
    s = s.strip()
    # Accept "7", "07", "7:00", "07:00"
    if re.fullmatch(r"\d{1,2}", s):
        h = int(s)
        if not (0 <= h <= 23):
            raise ValueError("Hour must be 0..23")
        return dt_time(h, 0)
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s)
    if not m:
        raise ValueError("Time must be HH or HH:MM (24h)")
    h, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mm <= 59):
        raise ValueError("Invalid time")
    return dt_time(h, mm)

def _get_now_tz(tz_name: str) -> datetime:
    if not tz_name:
        tz_name = "Asia/Kolkata"
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(tz_name))
        except Exception:
            pass
    # Fallback to timezone offset if we know it's India time
    try:
        from datetime import timezone, timedelta
        if tz_name == "Asia/Kolkata":
            return datetime.now(timezone(timedelta(hours=5, minutes=30)))
    except Exception:
        pass
    # Fallback: naive local time
    return datetime.now()

def _get_cycle_seconds_with_jitter(cycle_min: float) -> int:
    if cycle_min in (7, 20):  # Both legacy default 20 and new default 7 map to 6-8 min (360-480s)
        return random.randint(360, 480)
    else:
        # Custom cycle: add ±15% jitter
        seconds = int(cycle_min * 60)
        jitter = int(seconds * 0.15)
        return random.randint(seconds - jitter, seconds + jitter)

def _in_window(now_t: dt_time, start_t: dt_time, end_t: dt_time) -> bool:
    """True if now is within [start, end) with midnight wrap support."""
    if start_t <= end_t:
        return start_t <= now_t < end_t
    # crosses midnight, e.g., 23:00 -> 07:00
    return (now_t >= start_t) or (now_t < end_t)

def _seconds_until_quiet_end(cfg: dict = None) -> int:
    """Return seconds until the end of quiet window (>= 1), assuming we are currently in quiet."""
    if cfg is None or cfg is AUTONIGHT_CFG:
        cfg = reload_autonight_cfg()
    tz = cfg.get("tz") or DEFAULT_AUTONIGHT["tz"]
    now = _get_now_tz(tz)
    start_t = _parse_hhmm(cfg.get("start", DEFAULT_AUTONIGHT["start"]))
    end_t   = _parse_hhmm(cfg.get("end", DEFAULT_AUTONIGHT["end"]))
    today = now.date()

    # Compute next end datetime
    if start_t <= end_t:
        # non-wrapping window (e.g., 02:00 -> 05:00)
        end_dt = datetime.combine(today, end_t, tzinfo=now.tzinfo)
        if now.time() >= end_t:
            end_dt = end_dt + timedelta(days=1)
    else:
        # wrapping window (e.g., 23:00 -> 07:00)
        if now.time() < end_t:
            end_dt = datetime.combine(today, end_t, tzinfo=now.tzinfo)
        else:
            end_dt = datetime.combine(today + timedelta(days=1), end_t, tzinfo=now.tzinfo)

    seconds = int((end_dt - now).total_seconds())
    return max(1, seconds)

def autonight_is_slow_mode(cfg: dict = None) -> bool:
    if cfg is None or cfg is AUTONIGHT_CFG:
        cfg = reload_autonight_cfg()
    if not cfg.get("enabled", True):
        return False
    try:
        now = _get_now_tz(cfg.get("tz", DEFAULT_AUTONIGHT["tz"]))
        start_t = _parse_hhmm(cfg.get("start", DEFAULT_AUTONIGHT["start"]))
        end_t   = _parse_hhmm(cfg.get("end", DEFAULT_AUTONIGHT["end"]))
        return _in_window(now.time(), start_t, end_t)
    except Exception:
        return False

def autonight_is_quiet(cfg: dict = None) -> bool:
    return autonight_is_slow_mode(cfg)

def autonight_status_text(cfg: dict = None) -> str:
    if cfg is None or cfg is AUTONIGHT_CFG:
        cfg = reload_autonight_cfg()
    state = "ENABLED 🌙 (Slow Speed Mode)" if cfg.get("enabled", True) else "DISABLED ❌"
    return (
        f"🌙 Auto-Night Mode: **{state}**\n"
        f"Slow Window: **{cfg.get('start','00:00')} → {cfg.get('end','07:00')}** (Late Night / After 12 AM)\n"
        f"☀️ Day Speed (07:00 → 00:00): **Safe Normal Speed (20s delay, 7m cycle)**\n"
        f"🌙 Night Speed (00:00 → 07:00): **Ultra-Safe Slow Speed (45s–60s delay, 15m–20m cycle)**\n"
        f"TZ: **{cfg.get('tz','Asia/Kolkata')}**"
    )


def autonight_parse_command(arg: str, cfg: dict) -> Tuple[str, dict]:
    """
    Returns (message_text, updated_cfg or same).
    Supported:
      .night
      .night on | off
      .night 23:00 to 07:00   (also supports -, – , —)
      .night 23-7
    """
    arg = (arg or "").strip()
    if not arg:
        return (autonight_status_text(cfg), cfg)

    low = arg.lower()
    if low in {"on", "enable", "enabled"}:
        cfg = cfg.copy()
        cfg["enabled"] = True
        _save_autonight(cfg)
        return ("✅ Auto-Night **enabled**.\n" + autonight_status_text(cfg), cfg)

    if low in {"off", "disable", "disabled"}:
        cfg = cfg.copy()
        cfg["enabled"] = False
        _save_autonight(cfg)
        return ("🚫 Auto-Night **disabled**.\n" + autonight_status_text(cfg), cfg)

    # Time range
    m = re.fullmatch(
        r"\s*(\d{1,2}(?::\d{2})?)\s*(?:to|–|—|-)\s*(\d{1,2}(?::\d{2})?)\s*",
        arg
    )
    if not m:
        return (
            "❗ Format: `.night 23:00 to 07:00`\n"
            "Also works with a dash: `.night 23:00-07:00` (24-hour times).",
            cfg
        )

    start_raw, end_raw = m.group(1), m.group(2)
    try:
        start_t = _parse_hhmm(start_raw)
        end_t   = _parse_hhmm(end_raw)
    except ValueError as e:
        return (f"❗ {e}", cfg)

    cfg = cfg.copy()
    cfg["start"] = f"{start_t.hour:02d}:{start_t.minute:02d}"
    cfg["end"]   = f"{end_t.hour:02d}:{end_t.minute:02d}"
    _save_autonight(cfg)
    return (f"🕒 Auto-Night window updated:\n**{cfg['start']} → {cfg['end']}** ({cfg.get('tz','Asia/Kolkata')})\n" + autonight_status_text(cfg), cfg)

# =========================
# Original forwarder logic
# =========================

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SESSIONS_DIR = os.path.join(APP_DIR, "sessions")
PID_FILE = os.path.join(APP_DIR, "runner.pid")
clients = {}
started_phones = set()
active_bots = {}

def extract_and_normalize_links(text: str) -> List[str]:
    """
    Extracts and normalizes Telegram group links, usernames, or numeric Chat IDs from a string.
    Handles spaces, commas, and newlines. Normalizes '@username', 't.me/...', and '-100...' / '123456...'.
    """
    tokens = re.split(r'[\s,\n]+', text)
    links = []
    for token in tokens:
        token = token.strip()
        if not token:
            continue
        if token.startswith('@'):
            links.append(f"https://t.me/{token[1:]}")
        elif token.startswith('t.me/'):
            links.append(f"https://{token}")
        elif token.startswith('telegram.me/'):
            links.append(f"https://{token}")
        elif re.match(r'^https?://(?:t\.me|telegram\.me)/\S+$', token):
            links.append(token)
        elif re.match(r'^-?\d+$', token):
            links.append(token)
    return links

def format_seconds(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    parts = []
    if h > 0: parts.append(f"{h}h")
    if m > 0 or h > 0: parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)

def _seconds_until_quiet_start(cfg: dict = None) -> int:
    if cfg is None or cfg is AUTONIGHT_CFG:
        cfg = reload_autonight_cfg()
    tz = cfg.get("tz") or DEFAULT_AUTONIGHT["tz"]
    now = _get_now_tz(tz)
    start_t = _parse_hhmm(cfg.get("start", DEFAULT_AUTONIGHT["start"]))
    today = now.date()
    start_dt = datetime.combine(today, start_t, tzinfo=now.tzinfo)
    if now.time() >= start_t:
        start_dt = start_dt + timedelta(days=1)
    return int((start_dt - now).total_seconds())

def parse_number_ranges(input_str: str, max_count: int) -> List[int]:
    """
    Parses strings like '1, 2, 5', '1-5, 8, 10-12', '1 2 3', or 'all'/'al'.
    Returns a sorted list of valid 1-indexed integers up to max_count.
    """
    s = input_str.strip().lower()
    if s in ("all", "al", "*"):
        return list(range(1, max_count + 1))

    indices = set()
    clean_s = re.sub(r'[,;\n]+', ' ', s)
    tokens = clean_s.split()

    for token in tokens:
        range_match = re.fullmatch(r'(\d+)\s*(?:-|\.\.)\s*(\d+)', token)
        if range_match:
            start_num = int(range_match.group(1))
            end_num = int(range_match.group(2))
            if start_num > end_num:
                start_num, end_num = end_num, start_num
            for n in range(start_num, end_num + 1):
                if 1 <= n <= max_count:
                    indices.add(n)
        elif token.isdigit():
            n = int(token)
            if 1 <= n <= max_count:
                indices.add(n)

    return sorted(list(indices))

def get_entity_display_name(entity: Any, fallback: str) -> str:
    """Safely returns entity title, name, or username without raising AttributeError."""
    if not entity or isinstance(entity, str):
        return fallback
    if getattr(entity, 'title', None):
        return entity.title
    first_name = getattr(entity, 'first_name', None)
    if first_name:
        last_name = getattr(entity, 'last_name', None) or ""
        name = f"{first_name} {last_name}".strip()
        if name:
            return name
    username = getattr(entity, 'username', None)
    if username:
        return f"@{username}"
    return fallback

async def check_write_permission(client, entity) -> str:
    try:
        if not entity:
            return "Unknown Entity"
        if isinstance(entity, str):
            return "Access Denied"
        from telethon.tl.types import Channel, Chat, User, ChatForbidden, ChannelForbidden
        from datetime import datetime, timezone
        if isinstance(entity, User):
            return "User Account (Not a Group)"
        if isinstance(entity, (ChatForbidden, ChannelForbidden)):
            return "Forbidden Group"
        if not isinstance(entity, (Channel, Chat)):
            return f"Non-Group Entity ({type(entity).__name__})"

        if isinstance(entity, Channel):
            if getattr(entity, 'left', False):
                return "Not Joined"
            if getattr(entity, 'broadcast', False) and not getattr(entity, 'admin_rights', None):
                return "Read-Only Channel"
        elif isinstance(entity, Chat):
            if getattr(entity, 'deactivated', False):
                return "Group Deactivated"
            if getattr(entity, 'left', False):
                return "Not Joined"

        # Query user-specific permissions for 'me'
        if client:
            try:
                permissions = await client.get_permissions(entity, 'me')
                if permissions:
                    if getattr(permissions, 'is_banned', False):
                        return "Banned"
                    if getattr(permissions, 'has_left', False):
                        return "Not Joined"

                    part = getattr(permissions, 'participant', None)
                    if part:
                        banned_rights = getattr(part, 'banned_rights', None)
                        if banned_rights:
                            send_msgs = getattr(banned_rights, 'send_messages', False)
                            until_date = getattr(banned_rights, 'until_date', None)
                            if send_msgs:
                                now = datetime.now(timezone.utc)
                                if until_date:
                                    if isinstance(until_date, (int, float)):
                                        if 0 < until_date <= now.timestamp():
                                            send_msgs = False
                                    elif hasattr(until_date, 'timestamp'):
                                        if until_date <= now:
                                            send_msgs = False
                                if send_msgs:
                                    return "Muted"

                    # If user is admin, creator, or regular member with default permissions, they can send!
                    if getattr(permissions, 'is_admin', False) or getattr(permissions, 'is_creator', False) or getattr(permissions, 'has_default_permissions', False):
                        return "Healthy"
            except Exception as pe:
                logger.debug(f"Permissions check for 'me' note: {pe}")
                err_str = str(pe).lower()
                if "not participant" in err_str or "usernotparticipant" in err_str:
                    return "Not Joined"

        return "Healthy"
    except Exception as e:
        logger.warning(f"check_write_permission error: {e}")
        return "Healthy"

SYSTEM_MESSAGE_PATTERNS = [
    r"^\s*[\./]",  # Dot commands (.status, .delay, .help, etc.) or slash commands (/start)
    r"^\s*⚙️\s*System Status Panel",
    r"^\s*📊\s*System Statistics",
    r"^\s*📊\s*Group Health Report",
    r"^\s*📊\s*Sequence Group Selection",
    r"^\s*📊\s*Target Group Performance",
    r"^\s*❀\s*User Info:",
    r"^\s*📋\s*Joined Target Groups",
    r"^\s*📋\s*All joined groups",
    r"^\s*📋\s*No groups configured",
    r"^\s*📋\s*No errors recorded",
    r"^\s*🎁\s*TELETHON V5 ELITE",
    r"^\s*❌\s*Recent Error Console",
    r"^\s*❌\s*Error Detail",
    r"^\s*🗑️\s*Target groups list",
    r"^\s*🗑️\s*Error logs cleared",
    r"^\s*🧠\s*Smart Ad Sender",
    r"^\s*🕒\s*Auto-Night window",
    r"^\s*🌙\s*Auto-Night Mode:",
    r"^\s*🛡️\s*Minimum safe",
    r"^\s*🛡️\s*Safe message delay",
    r"^\s*🛡️\s*Errors detected",
    r"^\s*🛡️\s*Group Blacklist",
    r"^\s*🛡️\s*Quarantined",
    r"^\s*⏳\s*Fetching unadded",
    r"^\s*⏳\s*Preparing to join",
    r"^\s*🔍\s*Auditing permissions",
    r"^\s*✅\s*Cycle delay set",
    r"^\s*✅\s*Safe message delay",
    r"^\s*✅\s*Auto-Night enabled",
    r"^\s*✅\s*Added",
    r"^\s*✅\s*Removed",
    r"^\s*✅\s*Mode set",
    r"^\s*🚫\s*Auto-Night disabled",
    r"^\s*⚠️\s*Minimum",
    r"^\s*⚠️\s*Usage:",
    r"^\s*⚠️\s*Format:",
    r"^\s*⚠️\s*No valid",
    r"^\s*⚠️\s*Invalid",
    r"^\s*⚠️\s*No fetched",
    r"^\s*❗\s*Usage:",
    r"^\s*❗\s*Format:",
    r"^\s*⏸️\s*Engine Paused",
    r"^\s*▶️\s*Engine Resumed",
    r"^\s*🏓\s*Pong",
    r"^\s*🎲\s*Spintax Live Generator",
    r"^\s*📁\s*Target Groups Exported",
    r"^\s*📥\s*Import Session Complete",
    r"^\s*🧹\s*Target Groups Cleaned",
    r"^\s*🧹\s*Auditing and cleaning",
]

def is_valid_user_ad(msg: Any) -> bool:
    """
    Checks if a message from Saved Messages ('me') is a genuine user ad message,
    strictly excluding bot commands (.status, .delay, etc.) and system status responses.
    """
    if not msg:
        return False
        
    text = (getattr(msg, 'text', '') or "").strip()
    
    from telethon.tl.types import MessageMediaWebPage
    media = getattr(msg, 'media', None)
    has_media = media and not isinstance(media, MessageMediaWebPage)
    
    if not text and not has_media:
        return False
        
    if text:
        for pattern in SYSTEM_MESSAGE_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                return False

    return True

async def get_active_ad_messages(client: Any) -> List[Any]:
    """
    Fetches raw messages from Saved Messages ('me') and filters out any deleted,
    empty, bot command, or system response messages, returning only valid active user ads.
    """
    try:
        messages = await client.get_messages("me", limit=100)
        valid_ads = [m for m in messages if is_valid_user_ad(m)]
        valid_ads.reverse() # Send in chronological order (oldest to newest)
        return valid_ads
    except Exception as e:
        logger.error(f"Error fetching active ad messages: {e}")
        return []

STOP_WORDS = {
    "the", "and", "for", "with", "this", "that", "from", "group", "chat", "official",
    "link", "https", "http", "telegram", "tme", "channel", "join", "admin", "owner",
    "public", "free", "main", "new", "real", "best", "top", "all", "get", "sub"
}

def extract_message_keywords(text: str) -> set:
    """Extracts all significant topic keywords and hashtags from message text."""
    if not text:
        return set()
    words = re.findall(r'\b[a-zA-Z0-9_]{3,}\b', text.lower())
    keywords = {w for w in words if w not in STOP_WORDS and not w.isdigit()}
    return keywords

def get_group_search_keywords(group_url: Any, target_entity: Any) -> set:
    """Extracts significant keywords from group title, username, and URL."""
    keywords = set()
    clean_url = str(group_url or "").lower().strip().rstrip('/')
    url_parts = re.split(r'[/_.-]+', clean_url)
    for p in url_parts:
        if len(p) >= 3 and p not in STOP_WORDS and not p.startswith("http") and not p.isdigit():
            keywords.add(p)

    title = getattr(target_entity, 'title', None)
    if title and isinstance(title, str):
        title_parts = re.split(r'[\s/_.-]+', title.lower())
        for p in title_parts:
            p_clean = re.sub(r'\W+', '', p)
            if len(p_clean) >= 3 and p_clean not in STOP_WORDS and not p_clean.isdigit():
                keywords.add(p_clean)

    username = getattr(target_entity, 'username', None)
    if username and isinstance(username, str):
        u_parts = re.split(r'[/_.-]+', username.lower())
        for p in u_parts:
            if len(p) >= 3 and p not in STOP_WORDS and not p.isdigit():
                keywords.add(p)

    return keywords

def find_smart_matched_message(group_url: str, target_entity: Any, valid_messages: List[Any]) -> Optional[Any]:
    """
    Finds message in valid_messages whose full text keywords best match group keywords.
    Returns None if no keyword match found (score == 0).
    """
    group_kw = get_group_search_keywords(group_url, target_entity)
    if not group_kw:
        return None

    best_msg = None
    best_score = 0

    for msg in valid_messages:
        msg_text = msg.text or ""
        msg_kw = extract_message_keywords(msg_text)
        if not msg_kw:
            continue

        # Score matching keywords
        overlap = group_kw.intersection(msg_kw)
        score = len(overlap)
        
        # Substring keyword check boost
        if score == 0:
            for g_word in group_kw:
                for m_word in msg_kw:
                    if len(g_word) >= 4 and len(m_word) >= 4:
                        if g_word in m_word or m_word in g_word:
                            score += 1
                            
        if score > best_score:
            best_score = score
            best_msg = msg

    return best_msg

def _get_config_groups(config: dict) -> List[str]:
    groups = config.get("groups", [])
    if not isinstance(groups, list):
        groups = db._normalize_groups(groups)
        config["groups"] = groups
    return groups

entity_cache = {} # clean_link -> (entity, timestamp)

async def resolve_group_entity(client, group_url: Any, config: dict = None, phone: str = None):
    """
    Resolves a group URL (public or private invite link) or Channel ID to a Telethon entity
    with caching and multi-stage fallback resolution.
    """
    group_str = str(group_url or "").strip()
    clean_link = group_str.rstrip('/')
    if not clean_link:
        return group_url

    now_ts = time_mod.time()
    if clean_link in entity_cache:
        ent, cached_at = entity_cache[clean_link]
        if now_ts - cached_at < 300 and not isinstance(ent, str):
            return ent

    ent = None

    # Step 1: Extract numeric channel_id from link, raw digits, or group_map
    channel_id = None
    if config:
        gmap = config.get("group_map", {})
        if isinstance(gmap, dict) and clean_link in gmap:
            channel_id = gmap[clean_link]

    if not channel_id:
        if clean_link.startswith("-100"):
            channel_id = int(clean_link[4:])
        elif clean_link.startswith("-"):
            channel_id = int(clean_link[1:])
        elif clean_link.isdigit():
            channel_id = int(clean_link)
        elif "/c/" in clean_link:
            cid_part = clean_link.split("/c/")[-1].split("/")[0]
            if cid_part.isdigit():
                channel_id = int(cid_part)

    # Step 2: Fast resolution via Channel ID
    if channel_id:
        cid_clean = int(str(channel_id).replace("-100", "").replace("-", ""))
        try:
            from telethon.tl.types import PeerChannel
            ent = await client.get_entity(PeerChannel(cid_clean))
        except Exception:
            try:
                ent = await client.get_entity(channel_id)
            except Exception:
                pass

        if not ent:
            try:
                dialogs = await client.get_dialogs()
                match_ids = {channel_id, cid_clean, -cid_clean}
                for d in dialogs:
                    d_ent = d.entity
                    ent_id = getattr(d_ent, 'id', None)
                    dlg_id = getattr(d, 'id', None)
                    if (ent_id in match_ids) or (dlg_id in match_ids) or (ent_id and abs(ent_id) == cid_clean) or (dlg_id and abs(dlg_id) == cid_clean):
                        ent = d_ent
                        break
            except Exception as e:
                logger.debug(f"Dialog check note for channel ID {channel_id}: {e}")

    # Step 3: Handle private invite links (t.me/+hash)
    if not ent and ("t.me/+" in clean_link or "t.me/joinchat/" in clean_link):
        hash_val = clean_link.split('+')[-1] if "t.me/+" in clean_link else clean_link.split('joinchat/')[-1]
        from telethon.tl.functions.messages import CheckChatInviteRequest
        from telethon.tl.types import ChatInviteAlready
        try:
            res = await client(CheckChatInviteRequest(hash_val))
            if isinstance(res, ChatInviteAlready) and getattr(res, 'chat', None):
                ent = res.chat
        except Exception as e:
            logger.debug(f"Invite check note for {group_url}: {e}")

    # Step 4: Direct username/link resolution via client.get_entity()
    if not ent and not clean_link.startswith("-") and not clean_link.isdigit() and "/c/" not in clean_link:
        try:
            ent = await client.get_entity(clean_link)
        except Exception as e:
            logger.debug(f"Direct entity resolution for {group_url} failed: {e}")
            ent = None

    # Step 5: Fallback dialog scan by username or title match
    if not ent:
        try:
            target_name = clean_link.split('/')[-1].replace('@', '').lower()
            dialogs = await client.get_dialogs()
            for d in dialogs:
                d_ent = d.entity
                u_name = getattr(d_ent, 'username', None)
                if u_name and u_name.lower() == target_name:
                    ent = d_ent
                    break
        except Exception:
            pass

    # Save mapping and cache ONLY if valid entity resolved
    if ent and hasattr(ent, 'id'):
        entity_cache[clean_link] = (ent, now_ts)
        if config is not None:
            gmap = config.get("group_map", {})
            if not isinstance(gmap, dict):
                gmap = {}
            if gmap.get(clean_link) != ent.id:
                gmap[clean_link] = ent.id
                config["group_map"] = gmap
                if phone:
                    await asyncio.to_thread(db.update_user_config, phone, group_map=gmap)
        return ent

    # Return original group_url string (do NOT cache string failure)
    return group_url

async def interruptible_sleep(get_target_time, tz_name: str, wake_event: Optional[Any] = None):
    while True:
        if wake_event and wake_event.is_set():
            break
        target = get_target_time()
        if not target:
            break
        now = _get_now_tz(tz_name)
        if now >= target:
            break
        rem = (target - now).total_seconds()
        if rem <= 0:
            break
        sleep_chunk = min(rem, 0.5)
        if wake_event:
            try:
                await asyncio.wait_for(wake_event.wait(), timeout=sleep_chunk)
                break
            except asyncio.TimeoutError:
                pass
        else:
            await asyncio.sleep(sleep_chunk)

# Global Auto-Night config (shared across accounts)
AUTONIGHT_CFG = _load_autonight()

def reload_autonight_cfg() -> dict:
    global AUTONIGHT_CFG
    AUTONIGHT_CFG = _load_autonight()
    return AUTONIGHT_CFG

async def run_user_bot(config):
    phone = config["phone"]
    if phone in started_phones:
        return

    # Track this session to avoid concurrent start attempts
    started_phones.add(phone)

    session_path = os.path.join(SESSIONS_DIR, f"{phone}.session")
    api_id = int(config["api_id"])
    api_hash = config["api_hash"]
    delay = config.get("msg_delay_sec", 20)
    cycle = config.get("cycle_delay_min", 7)

    # Load persistent errors asynchronously
    loaded_errors = await asyncio.to_thread(db.get_errors, phone)

    user_state = {
        "delay": delay,   # seconds between forwards
        "cycle": cycle,   # minutes between cycles
        "use_copy": True, # Copy instead of Forward (removes 'forwarded from' tag)
        "success_total": 0,
        "fail_total": 0,
        "current_cycle_success": 0,
        "current_cycle_fail": 0,
        "next_msg_at": None,
        "status": "Idle 😴",
        "logs": [],
        "errors": loaded_errors,
        "msg_seq": 0,
        "ads_updated": False,
        "wake_event": asyncio.Event(),
        "start_time": _get_now_tz(reload_autonight_cfg().get("tz", DEFAULT_AUTONIGHT["tz"]))
    }

    async def auto_remove_group(group_url: str, reason: str = "Banned/Not Joined"):
        groups = _get_config_groups(config)
        norm_url = str(group_url or "").strip().rstrip('/')
        removed = False
        for g in list(groups):
            if g.strip().rstrip('/') == norm_url or g == group_url:
                groups.remove(g)
                removed = True
        if removed:
            config["groups"] = groups
            await asyncio.to_thread(db.update_user_config, phone, groups=groups)
            log_event(f"🗑️ Auto-removed unusable group '{group_url}' ({reason}) from target list.")

    active_bots[phone] = {
        "client": None,
        "state": user_state,
        "config": config
    }

    def log_event(msg, details=None):
        tz = reload_autonight_cfg().get("tz", DEFAULT_AUTONIGHT["tz"])
        now = _get_now_tz(tz)
        ts = now.strftime("%H:%M:%S")
        
        # Determine color and icon
        color = Fore.WHITE
        icon = "ℹ"
        
        lower_msg = msg.lower()
        is_err = False
        if "success" in lower_msg:
            color = Fore.GREEN
            icon = "✔"
        elif "failed" in lower_msg or "error" in lower_msg or "floodwait" in lower_msg:
            color = Fore.RED
            icon = "✖"
            is_err = True
        elif "processing" in lower_msg:
            color = Fore.CYAN
            icon = "📡"
        
        clean_msg = msg.replace("**", "") # Remove markdown for console
        print(f"{Fore.MAGENTA}[{ts}] {color}{icon} {Fore.WHITE}{clean_msg}")
        
        user_state["logs"].append(f"[{ts}] {msg}")
        if len(user_state["logs"]) > 10:
            user_state["logs"].pop(0)
            
        if is_err:
            def _async_err_log():
                db.log_error(phone, ts, msg, details)
                user_state["errors"] = db.get_errors(phone)
            asyncio.create_task(asyncio.to_thread(_async_err_log))
        logger.info(f"[{phone}] {msg}")

    proxy = get_telethon_proxy(config.get("proxy"))
    if proxy:
        logger.info(f"[{phone}] Connecting using proxy: {config['proxy'].get('addr')}:{config['proxy'].get('port')}")

    client = TelegramClient(session_path, api_id, api_hash, proxy=proxy)
    active_bots[phone]["client"] = client
    try:
        await client.connect()
        if not await client.is_user_authorized():
            logger.error(f"[{phone}] Session revoked or unauthorized.")
            return

        me = await client.get_me()
        me_id = me.id
        log_event(f"Bot connected: {config.get('name','N/A')} (ID: {me_id})")
    except Exception as e:
        logger.error(f"[{phone}] Connection failure: {e}")
        return

    async def auto_populate_chat_ids():
        try:
            from telethon.tl.types import Channel, Chat
            dialogs = await client.get_dialogs()
            gmap = config.get("group_map", {})
            if not isinstance(gmap, dict):
                gmap = {}
            updated = False
            for d in dialogs:
                ent = d.entity
                is_group = False
                if isinstance(ent, Chat):
                    is_group = True
                elif isinstance(ent, Channel):
                    if getattr(ent, 'megagroup', False) or getattr(ent, 'gigagroup', False) or not getattr(ent, 'broadcast', False):
                        is_group = True

                if is_group and hasattr(ent, 'id'):
                    cid = ent.id
                    username = getattr(ent, 'username', None)
                    if username:
                        u1 = f"https://t.me/{username}"
                        if gmap.get(u1) != cid:
                            gmap[u1] = cid
                            updated = True
                    u2 = f"https://t.me/c/{cid}"
                    if gmap.get(u2) != cid:
                        gmap[u2] = cid
                        updated = True
                    gmap[str(cid)] = cid
            if updated:
                config["group_map"] = gmap
                await asyncio.to_thread(db.update_user_config, phone, group_map=gmap)
        except Exception as e:
            logger.debug(f"Auto-populate chat IDs background note: {e}")

    asyncio.create_task(auto_populate_chat_ids())


    async def delayed_delete(chat_id, msg_ids, delay=40):
        await asyncio.sleep(delay)
        try:
            await client.delete_messages(chat_id, msg_ids)
        except Exception:
            pass

    def command_wrapper(func):
        async def wrapper(event):
            try:
                await func(event)
            except Exception as e:
                import traceback
                tb_str = traceback.format_exc()
                logger.error(f"Error in command handler: {e}", exc_info=True)
                log_event(f"Command Error: {type(e).__name__} - {e}", details=tb_str)
        return wrapper

    @client.on(events.NewMessage(outgoing=True))
    async def out_ad_handler(event):
        if not event.message:
            return
        if not is_valid_user_ad(event.message):
            return
        chat = await event.get_chat()
        is_saved = getattr(chat, 'is_self', False)
        if not is_saved:
            try:
                me_user = await client.get_me()
                is_saved = (event.chat_id == me_user.id)
            except Exception:
                pass
        if is_saved:
            log_event("📢 New ad message detected in Saved Messages! Reloading ads real-time...")
            user_state["ads_updated"] = True
            user_state["wake_event"].set()

    @client.on(events.MessageDeleted())
    async def deleted_ad_handler(event):
        chat_id = getattr(event, 'chat_id', None)
        is_saved = False
        if chat_id is not None:
            try:
                me_user = await client.get_me()
                if chat_id == me_user.id:
                    is_saved = True
            except Exception:
                pass
        else:
            is_saved = True
        if is_saved:
            log_event("🗑️ Ad deletion detected in Saved Messages! Reloading ads real-time...")
            user_state["ads_updated"] = True
            user_state["wake_event"].set()

    @client.on(events.MessageEdited(outgoing=True))
    async def edited_ad_handler(event):
        if not event.message:
            return
        if not is_valid_user_ad(event.message):
            return
        chat = await event.get_chat()
        is_saved = getattr(chat, 'is_self', False)
        if not is_saved:
            try:
                me_user = await client.get_me()
                is_saved = (event.chat_id == me_user.id)
            except Exception:
                pass
        if is_saved:
            log_event("✏️ Ad edit detected in Saved Messages! Reloading ads real-time...")
            user_state["ads_updated"] = True
            user_state["wake_event"].set()

    @client.on(events.NewMessage)
    @command_wrapper
    async def command_handler(event):
        admin_id = await asyncio.to_thread(db.get_admin_id)
        is_owner = event.out
        is_admin = (admin_id is not None) and (event.sender_id == admin_id)
        if not (is_owner or is_admin):
            return

        text = (event.raw_text or "").strip()
        if not text.startswith("."):
            return

        # Setup auto-delete for command and its responses
        orig_respond = event.respond
        async def auto_delete_respond(*args, **kwargs):
            resp = await orig_respond(*args, **kwargs)
            if resp:
                asyncio.create_task(delayed_delete(event.chat_id, [event.id, resp.id]))
            return resp
        event.respond = auto_delete_respond

        # Ensure command itself is deleted after 40s even if no respond() is called
        asyncio.create_task(delayed_delete(event.chat_id, [event.id]))

        if text.startswith(".time"):
            value = int(''.join(filter(str.isdigit, text)) or "0")
            if value <= 0:
                await event.respond("❗ Usage: `.time 7m` or `.time 1h`")
                return
            if 'h' in text.lower():
                value = value * 60
            
            if value < 5:
                await event.respond("⚠️ Minimum cycle interval is **5 minutes**. Setting to 5m.")
                value = 5
                
            user_state["cycle"] = value
            config["cycle_delay_min"] = value
            db.update_user_config(phone, cycle_delay_min=value)
            
            tz = AUTONIGHT_CFG.get("tz", DEFAULT_AUTONIGHT["tz"])
            sleep_seconds = _get_cycle_seconds_with_jitter(value)
            user_state["next_msg_at"] = _get_now_tz(tz) + timedelta(seconds=sleep_seconds)
            await event.respond(f"✅ Cycle delay set to **{value} minutes**")

        elif text.startswith(".delay"):
            value = int(''.join(filter(str.isdigit, text)) or "0")
            if value <= 0:
                await event.respond("❗ Usage: `.delay 25` (seconds)")
                return
            
            if value < 20:
                await event.respond("🛡️ Minimum safe message delay is **20 seconds** to prevent Telegram bans. Setting to 20s.")
                value = 20
                
            user_state["delay"] = value
            config["msg_delay_sec"] = value
            db.update_user_config(phone, msg_delay_sec=value)
            
            tz = AUTONIGHT_CFG.get("tz", DEFAULT_AUTONIGHT["tz"])
            user_state["next_msg_at"] = _get_now_tz(tz) + timedelta(seconds=value)
            await event.respond(f"🛡️ Safe message delay set to **{value} seconds** (Organic Human Jitter ±20%)")


        elif text.startswith(".status"):
            tz = AUTONIGHT_CFG.get("tz", DEFAULT_AUTONIGHT["tz"])
            now = _get_now_tz(tz)
            quiet_countdown = ""
            if AUTONIGHT_CFG.get("enabled", True):
                if autonight_is_quiet(AUTONIGHT_CFG):
                    rem = _seconds_until_quiet_end(AUTONIGHT_CFG)
                    quiet_countdown = f"\n🌙 **Quiet Hours Active** (Ends in `{format_seconds(rem)}`)"
                else:
                    rem = _seconds_until_quiet_start(AUTONIGHT_CFG)
                    quiet_countdown = f"\n🌙 **Next Quiet Period**: In `{format_seconds(rem)}`"
            
            next_msg_str = "N/A"
            if user_state["next_msg_at"]:
                next_msg_str = user_state["next_msg_at"].strftime("%H:%M:%S")

            reply = (
                f"⚙️ **System Status Panel**\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🔄 **Current State:** `{user_state['status']}`\n"
                f"📍 **Target Groups:** `{len(_get_config_groups(config))}`\n"
                f"🕒 **Next Action at:** `{next_msg_str}`\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"⏱ **Cycle Interval:** `{user_state['cycle']} min` (±15% jitter)\n"
                f"Spacing: `{user_state['delay']} sec` (between groups)\n"
                f"Mode: `{'Copy' if user_state['use_copy'] else 'Forward'}`\n"
                f"━━━━━━━━━━━━━━━━━━"
                + quiet_countdown
            )
            await event.respond(reply)

        elif text.startswith(".stats"):
            tz = AUTONIGHT_CFG.get("tz", DEFAULT_AUTONIGHT["tz"])
            now = _get_now_tz(tz)
            uptime = str(now - user_state["start_time"]).split('.')[0]
            
            # Performance Metrics
            elapsed_seconds = (now - user_state["start_time"]).total_seconds()
            total_sends = user_state["success_total"] + user_state["fail_total"]
            sends_per_hour = (total_sends / (elapsed_seconds / 3600)) if elapsed_seconds > 0 else 0.0
            
            # Formatting next delivery time
            next_msg_str = "N/A"
            if user_state["next_msg_at"]:
                next_msg_str = user_state["next_msg_at"].strftime("%H:%M:%S")
            
            # Label change based on status
            next_label = "🕒 Next Delivery"
            if "Idle" in user_state["status"] or "Waiting" in user_state["status"]:
                next_label = "🕒 Next Cycle"
            elif "Msg" in user_state["status"]:
                next_label = "🕒 Next Group"

            quiet_countdown = ""
            if AUTONIGHT_CFG.get("enabled", True):
                if autonight_is_quiet(AUTONIGHT_CFG):
                    rem = _seconds_until_quiet_end(AUTONIGHT_CFG)
                    quiet_countdown = f"🌙 **Quiet Mode**: Ends in `{format_seconds(rem)}`"
                else:
                    rem = _seconds_until_quiet_start(AUTONIGHT_CFG)
                    quiet_countdown = f"🌙 **Next Quiet**: In `{format_seconds(rem)}`"

            log_text = "\n".join(user_state["logs"][-5:]) if user_state["logs"] else "No logs yet."
            
            reply = (
                f"📊 **System Statistics**\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"👤 **Account:** {config.get('name')} ({phone})\n"
                f"⏱ **Uptime:** `{uptime}`\n"
                f"🔄 **Status:** {user_state['status']}\n"
                f"📍 **Groups:** {len(_get_config_groups(config))}\n"
                f"⚡ **Average Speed:** `{sends_per_hour:.1f} posts/hour`\n"
                f"{quiet_countdown}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"✅ **Total Success:** `{user_state['success_total']}`\n"
                f"❌ **Total Failed:** `{user_state['fail_total']}`\n"
                f"{next_label}: `{next_msg_str}`\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📜 **Recent Logs:**\n`{log_text}`"
            )
            await event.respond(reply)

        elif text.startswith(".info"):
            me = await client.get_me()
            expiry = "Lifetime"
            reply = (
                f"❀ User Info:\n"
                f"❀ Name: {config.get('name')}\n"
                f"❀ Cycle Delay: {user_state['cycle']} min\n"
                f"❀ Message Delay: {user_state['delay']} sec\n"
                f"❀ Groups: {len(_get_config_groups(config))}\n"
                f"❀ Plan Access: {expiry}\n\n"
                + autonight_status_text(AUTONIGHT_CFG)
            )

            await event.respond(reply)

        elif text.startswith(".fetch"):
            progress_msg = await event.respond("⏳ **Fetching unadded joined groups from your account...**")
            try:
                from telethon.tl.types import Channel, Chat
                dialogs = await client.get_dialogs()
                groups_list = _get_config_groups(config)
                gmap = config.get("group_map", {})
                if not isinstance(gmap, dict):
                    gmap = {}

                # Pre-build lookup set of already added group URLs and Chat IDs
                already_added = set()
                for g_item in groups_list:
                    clean_g = g_item.strip().rstrip('/').lower()
                    already_added.add(clean_g)
                    if clean_g in gmap:
                        already_added.add(str(gmap[clean_g]))

                fetched = []
                for d in dialogs:
                    ent = d.entity
                    # Filter: Keep ONLY groups & supergroups (exclude Users, Bots, and Broadcast Channels)
                    is_group = False
                    if isinstance(ent, Chat):
                        is_group = True
                    elif isinstance(ent, Channel):
                        if getattr(ent, 'megagroup', False) or getattr(ent, 'gigagroup', False):
                            is_group = True
                        elif not getattr(ent, 'broadcast', False):
                            is_group = True

                    if is_group:
                        username = getattr(ent, 'username', None)
                        ent_id = getattr(ent, 'id', None)
                        cid_str = str(ent_id) if ent_id else None

                        if username:
                            link = f"https://t.me/{username}"
                        elif ent_id:
                            cid = str(ent_id)
                            if cid.startswith("-100"):
                                cid = cid[4:]
                            elif cid.startswith("-"):
                                cid = cid[1:]
                            link = f"https://t.me/c/{cid}"
                        else:
                            continue

                        # Exclude groups already added to target groups list!
                        clean_link = link.strip().rstrip('/').lower()
                        if clean_link in already_added or (username and f"https://t.me/{username}".lower() in already_added) or (cid_str and cid_str in already_added):
                            continue

                        name = get_entity_display_name(ent, "Group")
                        fetched.append({
                            "title": name,
                            "link": link,
                            "username": username,
                            "entity": ent,
                            "id": ent_id
                        })

                if not fetched:
                    await progress_msg.edit("📋 All joined groups are already added to your target list!")
                    return

                # Store in user_state for sequence selection
                user_state["fetched_groups"] = fetched

                lines = [
                    f"📋 **Joined Target Groups ({len(fetched)})**",
                    "━━━━━━━━━━━━━━━━━━"
                ]
                for idx, item in enumerate(fetched, 1):
                    tag_str = f" (@{item['username']})" if item['username'] else f" ({item['link']})"
                    id_str = f" `[ID: {item['id']}]`" if item.get('id') else ""
                    lines.append(f"{idx}. 👥 **{item['title']}**{tag_str}{id_str}")

                lines.append("━━━━━━━━━━━━━━━━━━")
                lines.append("💡 **Option 1**: `.addnum 1,3,5` or `.addnum 1-5` or `.addnum all` (Add via sequence numbers)")
                lines.append("💡 **Option 2**: `.add <url>` or `.addgroup <url1>, <url2>` (Add via manual link)")

                try:
                    await progress_msg.delete()
                except Exception:
                    pass

                current_chunk = []
                current_len = 0
                for line in lines:
                    if current_len + len(line) + 1 > 4000:
                        await event.respond("\n".join(current_chunk))
                        current_chunk = [line]
                        current_len = len(line)
                    else:
                        current_chunk.append(line)
                        current_len += len(line) + 1
                if current_chunk:
                    await event.respond("\n".join(current_chunk))

            except Exception as e:
                import traceback
                logger.error(f"Fetch error: {e}", exc_info=True)
                await progress_msg.edit(f"❌ Failed to fetch groups: {type(e).__name__} - {e}")

        elif text.startswith(".addnum") or text.startswith(".select"):
            fetched = user_state.get("fetched_groups", [])
            if not fetched:
                await event.respond("⚠️ No fetched groups available.\n💡 Please run `.fetch` first to view your joined groups list!")
                return

            arg = text[len(".addnum"):].strip() if text.startswith(".addnum") else text[len(".select"):].strip()
            if not arg:
                await event.respond("⚠️ Usage: `.addnum 1,3,5` or `.addnum 1-5` or `.addnum all`\n(Run `.fetch` first to view list)")
                return

            selected_indices = parse_number_ranges(arg, len(fetched))
            if not selected_indices:
                await event.respond(f"⚠️ Invalid selection. Range: 1 to {len(fetched)}")
                return

            groups_list = _get_config_groups(config)
            gmap = config.get("group_map", {})
            if not isinstance(gmap, dict):
                gmap = {}
            added_names, skipped_names = [], []

            for idx in selected_indices:
                item = fetched[idx - 1]
                link = item["link"]
                ent = item.get("entity")
                if ent and hasattr(ent, 'id'):
                    gmap[link.strip().rstrip('/')] = ent.id
                if link not in groups_list:
                    groups_list.append(link)
                    added_names.append(f"{idx}. {item['title']}")
                else:
                    skipped_names.append(f"{idx}. {item['title']}")

            config["groups"] = groups_list
            config["group_map"] = gmap
            await asyncio.to_thread(db.update_user_config, phone, groups=groups_list, group_map=gmap)

            reply = [
                "📊 **Sequence Group Selection Results**",
                "━━━━━━━━━━━━━━━━━━"
            ]
            if added_names:
                reply.append(f"✅ **Added ({len(added_names)}):**")
                reply.extend(added_names[:25])
                if len(added_names) > 25:
                    reply.append(f"...and {len(added_names) - 25} more.")
            if skipped_names:
                reply.append(f"\n⚠️ **Skipped Duplicates ({len(skipped_names)}):**")
                reply.extend(skipped_names[:10])

            reply.append("\n━━━━━━━━━━━━━━━━━━")
            reply.append(f"📍 **Total Active Target Groups:** `{len(groups_list)}`")
            await event.respond("\n".join(reply))

        elif text.startswith(".addgroup") or text.startswith(".add"):
            cmd_arg = text[len(".addgroup"):].strip() if text.startswith(".addgroup") else text[len(".add"):].strip()
            links = extract_and_normalize_links(cmd_arg)
            if not links:
                await event.respond("⚠️ No valid group links or usernames found.\nFormat: `.add @group1` or `.addgroup @group1, https://t.me/group2` or split by newlines.")
                return
            added, skipped = [], []
            groups_list = _get_config_groups(config)
            gmap = config.get("group_map", {})
            if not isinstance(gmap, dict):
                gmap = {}
            for link in links:
                if link not in groups_list:
                    groups_list.append(link)
                    added.append(link)
                else:
                    skipped.append(link)
                clean_lk = link.strip().rstrip('/')
                try:
                    target_ent = await resolve_group_entity(client, link, config=config, phone=phone)
                    if hasattr(target_ent, 'id'):
                        gmap[clean_lk] = target_ent.id
                except Exception:
                    pass
            config["groups"] = groups_list
            config["group_map"] = gmap
            await asyncio.to_thread(db.update_user_config, phone, groups=groups_list, group_map=gmap)
            msg = []
            if added:
                msg.append(f"✅ Added **{len(added)}** new group(s).")
            if skipped:
                msg.append(f"⚠️ Skipped **{len(skipped)}** duplicate(s).")
            await event.respond("\n".join(msg) or "No changes.")

        elif text.startswith(".delall"):
            config["groups"] = []
            db.update_user_config(phone, groups=[])
            await event.respond("🗑️ Target groups list cleared completely.")
            return

        elif text.startswith(".delgroup"):
            arg = text[len(".delgroup"):].strip().lower()
            if arg == "all" or arg == "al":
                config["groups"] = []
                db.update_user_config(phone, groups=[])
                await event.respond("🗑️ Target groups list cleared completely.")
                return
                
            cmd_arg = text[len(".delgroup"):].strip()
            links = extract_and_normalize_links(cmd_arg)
            if not links:
                await event.respond("⚠️ Usage: `.delgroup <link1> ...` or `.delgroup all` to clear list.")
                return
            removed, skipped = [], []
            groups_list = _get_config_groups(config)
            for link in links:
                normalized_link = link.rstrip('/')
                found = None
                for g in groups_list:
                    if g.rstrip('/') == normalized_link:
                        found = g
                        break
                if found:
                    groups_list.remove(found)
                    removed.append(link)
                else:
                    skipped.append(link)
            config["groups"] = groups_list
            db.update_user_config(phone, groups=groups_list)
            msg = []
            if removed:
                msg.append(f"✅ Removed **{len(removed)}** group(s).")
            if skipped:
                msg.append(f"⚠️ Skipped **{len(skipped)}** group(s) (not in list).")
            await event.respond("\n".join(msg) or "No changes.")

        elif text.startswith(".groups"):
            groups_list = _get_config_groups(config)
            gmap = config.get("group_map", {})
            if not isinstance(gmap, dict):
                gmap = {}
            if not groups_list:
                await event.respond("📋 No groups configured.")
            else:
                lines = [f"❀ Target Groups ({len(groups_list)}):", "━━━━━━━━━━━━━━━━━━"]
                for idx, g in enumerate(groups_list, 1):
                    clean_g = g.strip().rstrip('/')
                    cid = gmap.get(clean_g) or gmap.get(g)
                    id_tag = f" `[ID: {cid}]`" if cid else ""
                    lines.append(f"{idx}. **{g}**{id_tag}")
                
                # Chunk sending to avoid Telegram MessageTooLongError
                current_chunk = []
                current_len = 0
                for line in lines:
                    if current_len + len(line) + 1 > 4000:
                        await event.respond("\n".join(current_chunk))
                        current_chunk = [line]
                        current_len = len(line)
                    else:
                        current_chunk.append(line)
                        current_len += len(line) + 1
                if current_chunk:
                    await event.respond("\n".join(current_chunk))

        elif text.startswith(".night"):
            # .night, .night on/off, .night 23:00 to 07:00
            arg = text[6:].strip() if len(text) > 6 else ""
            msg, new_cfg = autonight_parse_command(arg, AUTONIGHT_CFG)
            # Update global config in memory
            for k in list(AUTONIGHT_CFG.keys()):
                AUTONIGHT_CFG[k] = new_cfg.get(k, AUTONIGHT_CFG[k])
            await event.respond(msg)

        elif text.startswith(".mode"):
            if "forward" in text.lower():
                user_state["use_copy"] = False
                await event.respond("✅ Mode set to **Forward** (will show 'Forwarded from...')")
            else:
                user_state["use_copy"] = True
                await event.respond("✅ Mode set to **Copy** (looks like a fresh message)")


        elif text.startswith(".join"):
            cmd_arg = text[len(".join"):].strip()
            links = extract_and_normalize_links(cmd_arg)
            if not links:
                await event.respond("⚠️ Usage: `.join <link1> <link2> ...` (supports usernames and invite links)")
                return
            
            progress_msg = await event.respond(f"🔄 Preparing to join {len(links)} groups...")
            success, fail = 0, 0
            for idx, link in enumerate(links, 1):
                try:
                    await progress_msg.edit(f"⏳ **[{idx}/{len(links)}] Joining:** {link}\n*(Anti-Flood delay active)*")
                    clean_link = link.strip().rstrip('/')
                    if "t.me/+" in clean_link:
                        hash_val = clean_link.split('+')[-1]
                        from telethon.tl.functions.messages import ImportChatInviteRequest
                        await client(ImportChatInviteRequest(hash_val))
                    elif "t.me/joinchat/" in clean_link:
                        hash_val = clean_link.split('joinchat/')[-1]
                        from telethon.tl.functions.messages import ImportChatInviteRequest
                        await client(ImportChatInviteRequest(hash_val))
                    else:
                        username = clean_link.split('/')[-1]
                        from telethon.tl.functions.channels import JoinChannelRequest
                        await client(JoinChannelRequest(username))
                    success += 1
                except Exception as e:
                    logger.error(f"Join error {link}: {e}")
                    fail += 1
                
                if idx < len(links):
                    await asyncio.sleep(random.randint(10, 20))
            await progress_msg.edit(f"📊 **Join Session Complete!**\n━━━━━━━━━━━━━━━━━━\n✅ Successfully Joined: **{success}**\n❌ Failed / Already Joined: **{fail}**")

        elif text.startswith(".check"):
            groups_list = _get_config_groups(config)
            if not groups_list:
                await event.respond("📋 No groups configured to check.")
                return
            
            progress_msg = await event.respond(f"🔍 Auditing permissions on {len(groups_list)} groups...")
            results = [
                f"📊 **Group Health Report ({len(groups_list)})**",
                "━━━━━━━━━━━━━━━━━━"
            ]
            groups_to_check = list(groups_list)
            for idx, group in enumerate(groups_to_check, 1):
                group_str = str(group or "").strip()
                try:
                    target_entity = await resolve_group_entity(client, group_str, config=config, phone=phone)
                    display_name = get_entity_display_name(target_entity, group_str)

                    if isinstance(target_entity, str):
                        results.append(f"{idx}. ❌ **{display_name}** | Access Denied")
                        continue
                    
                    status = await check_write_permission(client, target_entity)
                    if status == "Healthy":
                        results.append(f"{idx}. ✅ **{display_name}** | Healthy")
                    else:
                        results.append(f"{idx}. ⚠️ **{display_name}** | {status}")
                except Exception as e:
                    logger.error(f"Check error on {group_str}: {e}", exc_info=True)
                    results.append(f"{idx}. ❌ **{group_str}** | Error: {type(e).__name__}")
            
            # Delete progress message safely
            try:
                await progress_msg.delete()
            except Exception:
                pass

            # Send chunked responses
            current_chunk = []
            current_len = 0
            for line in results:
                if current_len + len(line) + 1 > 4000:
                    await event.respond("\n".join(current_chunk))
                    current_chunk = [line]
                    current_len = len(line)
                else:
                    current_chunk.append(line)
                    current_len += len(line) + 1
            if current_chunk:
                await event.respond("\n".join(current_chunk))

        elif text.startswith(".smart"):
            curr = user_state.get("smart_ad_mode", True)
            user_state["smart_ad_mode"] = not curr
            status_text = "ENABLED 🎯 (Prioritizes group topic tag matches)" if user_state["smart_ad_mode"] else "DISABLED 🔄 (Standard sequential flow)"
            await event.respond(f"🧠 Smart Ad Sender is now **{status_text}**")

        elif text.startswith(".errors") or text.startswith(".error"):
            arg = text[len(".error"):].strip() if text.startswith(".error") else text[len(".errors"):].strip()
            # If command started with space, strip it further
            if arg.startswith("s"): # just in case of typos
                arg = arg[1:].strip()
            
            if arg.lower() == "clear":
                user_state["errors"] = []
                db.clear_errors(phone)
                await event.respond("🗑️ Error logs cleared successfully.")
                return

            if arg.isdigit():
                idx = int(arg) - 1
                errs = user_state.get("errors", [])
                if idx < 0 or idx >= len(errs):
                    await event.respond(f"⚠️ Invalid error index. Range: 1-{len(errs)}")
                else:
                    err = errs[idx]
                    details = err.get("details") or "No further traceback details available."
                    # Send traceback details inside code block
                    reply = (
                        f"❌ **Error Detail #{idx + 1}**\n"
                        f"🕒 **Time:** `{err['timestamp']}`\n"
                        f"📝 **Message:** `{err['message']}`\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"🔍 **Traceback / Context:**\n"
                        f"```python\n{details}\n```"
                    )
                    await event.respond(reply)
                return

            err_list = user_state.get("errors", [])
            if not err_list:
                await event.respond("📋 No errors recorded.")
            else:
                lines = [
                    "❌ **Recent Error Console**",
                    f"👤 **Account:** {config.get('name')} ({phone})",
                    "━━━━━━━━━━━━━━━━━━"
                ]
                for i, err in enumerate(err_list, 1):
                    # Show index and formatted error time/message
                    lines.append(f"{i}. `[{err['timestamp']}]` {err['message']}")
                lines.append("━━━━━━━━━━━━━━━━━━")
                lines.append("💡 Type `.error <num>` to see detailed tracebacks.")
                lines.append("💡 Type `.error clear` to reset the log.")
                await event.respond("\n".join(lines))

        elif text.startswith(".pause"):
            user_state["is_paused"] = True
            config["is_paused"] = True
            await asyncio.to_thread(db.update_user_config, phone, is_paused=True)
            log_event("⏸️ Engine paused by user command.")
            await event.respond("⏸️ **Engine Paused.** Message forwarding sequence paused until `.resume` is typed.")

        elif text.startswith(".resume"):
            user_state["is_paused"] = False
            config["is_paused"] = False
            await asyncio.to_thread(db.update_user_config, phone, is_paused=False)
            user_state["wake_event"].set()
            log_event("▶️ Engine resumed by user command.")
            await event.respond("▶️ **Engine Resumed.** Message forwarding sequence active!")

        elif text.startswith(".ping"):
            import time as t_mod
            start_ping = t_mod.time()
            me = await client.get_me()
            latency = int((t_mod.time() - start_ping) * 1000)
            
            tz = AUTONIGHT_CFG.get("tz", DEFAULT_AUTONIGHT["tz"])
            now = _get_now_tz(tz)
            uptime = str(now - user_state["start_time"]).split('.')[0]
            p_status = "Paused ⏸️" if user_state.get("is_paused") else "Active ▶️"
            
            await event.respond(
                f"🏓 **Pong!**\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"⚡ **Telegram Latency:** `{latency} ms`\n"
                f"⏱ **Uptime:** `{uptime}`\n"
                f"⚙️ **Engine Status:** `{p_status}`\n"
                f"👤 **Account:** `{get_entity_display_name(me, 'Me')}` (`{phone}`)"
            )

        elif text.startswith(".spintax"):
            sample = text[len(".spintax"):].strip()
            if not sample:
                sample = "{Hello|Hi|Hey} {friend|buddy|mate}! Check out our {amazing|best|top} offer!"
            result = parse_spintax(sample)
            await event.respond(
                f"🎲 **Spintax Live Generator**\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📝 **Template:** `{sample}`\n"
                f"✨ **Output:** `{result}`"
            )

        elif text.startswith(".analytics"):
            stats = await asyncio.to_thread(db.get_group_analytics, phone)
            if not stats:
                await event.respond("📊 No group analytics data recorded yet.")
            else:
                lines = [
                    "📊 **Target Group Performance Analytics**",
                    f"👤 **Account:** {config.get('name')} ({phone})",
                    "━━━━━━━━━━━━━━━━━━"
                ]
                total_posts = 0
                for idx, s in enumerate(stats[:20], 1):
                    tot = s["success_count"] + s["fail_count"]
                    total_posts += tot
                    rate = (s["success_count"] / tot * 100) if tot > 0 else 0
                    lines.append(f"{idx}. **{s['group_url']}**\n   ✅ {s['success_count']} | ❌ {s['fail_count']} | `{rate:.1f}% success` | Status: `{s['last_status']}`")
                
                lines.append("━━━━━━━━━━━━━━━━━━")
                lines.append(f"📍 **Tracked Groups:** `{len(stats)}` | **Total Post Attempts:** `{total_posts}`")
                
                current_chunk = []
                current_len = 0
                for line in lines:
                    if current_len + len(line) + 1 > 4000:
                        await event.respond("\n".join(current_chunk))
                        current_chunk = [line]
                        current_len = len(line)
                    else:
                        current_chunk.append(line)
                        current_len += len(line) + 1
                if current_chunk:
                    await event.respond("\n".join(current_chunk))

        elif text.startswith(".blacklist") or text.startswith(".quarantine"):
            arg = text.split(maxsplit=1)[1].strip() if len(text.split()) > 1 else ""
            links = extract_and_normalize_links(arg)
            blk_list = list(user_state.get("blacklist") or config.get("blacklist") or [])
            if not links:
                if not blk_list:
                    await event.respond("🛡️ **Group Blacklist / Quarantine is empty.**\nUsage: `.blacklist <link1> <link2>`")
                else:
                    lines = [f"🛡️ **Group Blacklist ({len(blk_list)}):**", "━━━━━━━━━━━━━━━━━━"]
                    for idx, b in enumerate(blk_list, 1):
                        lines.append(f"{idx}. `{b}`")
                    await event.respond("\n".join(lines))
            else:
                groups_list = _get_config_groups(config)
                added = []
                for lk in links:
                    norm = lk.rstrip('/')
                    if norm not in blk_list:
                        blk_list.append(norm)
                        added.append(norm)
                    if norm in groups_list:
                        groups_list.remove(norm)
                user_state["blacklist"] = blk_list
                config["blacklist"] = blk_list
                config["groups"] = groups_list
                await asyncio.to_thread(db.update_user_config, phone, blacklist=blk_list, groups=groups_list)
                await event.respond(f"🛡️ **Quarantined {len(added)} group(s)** to Blacklist and removed from target active list.")

        elif text.startswith(".unblacklist") or text.startswith(".unquarantine"):
            arg = text.split(maxsplit=1)[1].strip() if len(text.split()) > 1 else ""
            links = extract_and_normalize_links(arg)
            if not links:
                await event.respond("⚠️ Usage: `.unblacklist <link1> <link2>`")
            else:
                blk_list = list(user_state.get("blacklist") or config.get("blacklist") or [])
                removed = []
                for lk in links:
                    norm = lk.rstrip('/')
                    if norm in blk_list:
                        blk_list.remove(norm)
                        removed.append(norm)
                user_state["blacklist"] = blk_list
                config["blacklist"] = blk_list
                await asyncio.to_thread(db.update_user_config, phone, blacklist=blk_list)
                await event.respond(f"✅ Removed **{len(removed)} group(s)** from Blacklist.")

        elif text.startswith(".export"):
            groups_list = _get_config_groups(config)
            if not groups_list:
                await event.respond("📋 No groups to export.")
            else:
                file_content = "\n".join(groups_list)
                with tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".txt", prefix="groups_export_") as tmp:
                    tmp.write(file_content)
                    tmp_path = tmp.name
                
                try:
                    await client.send_file(
                        event.chat_id,
                        tmp_path,
                        caption=f"📁 **Target Groups Exported ({len(groups_list)} groups)**"
                    )
                finally:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)

        elif text.startswith(".import"):
            imported_text = ""
            if event.is_reply:
                reply_msg = await event.get_reply_message()
                if reply_msg and reply_msg.media and getattr(reply_msg.media, 'document', None):
                    file_bytes = await client.download_media(reply_msg, file=bytes)
                    if file_bytes:
                        imported_text = file_bytes.decode('utf-8', errors='ignore')
                elif reply_msg and reply_msg.text:
                    imported_text = reply_msg.text

            if not imported_text:
                imported_text = text[len(".import"):].strip()

            links = extract_and_normalize_links(imported_text)
            if not links:
                await event.respond("⚠️ Usage: `.import <urls>` or reply `.import` to an attached `.txt` file containing group links.")
            else:
                groups_list = _get_config_groups(config)
                added, skipped = [], []
                for lk in links:
                    norm = lk.rstrip('/')
                    if norm not in groups_list:
                        groups_list.append(norm)
                        added.append(norm)
                    else:
                        skipped.append(norm)
                config["groups"] = groups_list
                await asyncio.to_thread(db.update_user_config, phone, groups=groups_list)
                await event.respond(f"📥 **Import Session Complete!**\n✅ Added **{len(added)}** new groups.\n⚠️ Skipped **{len(skipped)}** duplicate groups.")

        elif text.startswith(".clean"):
            progress_msg = await event.respond("🧹 **Auditing and cleaning unreachable groups...**")
            groups_list = _get_config_groups(config)
            cleaned, kept = [], []
            for g in list(groups_list):
                try:
                    ent = await resolve_group_entity(client, g, config=config, phone=phone)
                    if isinstance(ent, str):
                        cleaned.append(g)
                        continue
                    status = await check_write_permission(client, ent)
                    if status in ("Banned", "Muted", "Not Joined", "Read-Only Channel", "Group Deactivated", "Forbidden Group"):
                        cleaned.append(g)
                    else:
                        kept.append(g)
                except Exception:
                    cleaned.append(g)

            for c_group in cleaned:
                if c_group in groups_list:
                    groups_list.remove(c_group)

            config["groups"] = groups_list
            await asyncio.to_thread(db.update_user_config, phone, groups=groups_list)
            try:
                await progress_msg.delete()
            except Exception:
                pass
            await event.respond(f"🧹 **Target Groups Cleaned!**\n━━━━━━━━━━━━━━━━━━\n🗑️ Purged Unusable Groups: **{len(cleaned)}**\n✅ Remaining Active Groups: **{len(kept)}**")

        elif text.startswith(".help"):
            await event.respond(
                "🎁 **TELETHON V5 ELITE ADVANCED MODULE**\n\n"
                "🛠 **Timing & Mode Configuration:**\n"
                "• `.time <m|h>` — Set cycle interval\n"
                "• `.delay <sec>` — Set message spacing\n"
                "• `.mode <copy|forward>` — Switch sending style\n"
                "• `.pause` | `.resume` — Pause or resume ad automation\n"
                "\n🛰 **Target Groups Management:**\n"
                "• `.add <url>` (or `.addgroup`) — Add target group(s)\n"
                "• `.fetch` — List joined groups with sequence numbers\n"
                "• `.addnum <1-5|all>` — Add groups by sequence number\n"
                "• `.delgroup <url>` | `.delall` — Clear target groups\n"
                "• `.groups` — Show all target groups\n"
                "• `.join <url>` — Join new groups in bulk\n"
                "• `.check` — Audit permissions on all groups\n"
                "• `.clean` — Purge all banned/forbidden groups automatically\n"
                "• `.export` | `.import` — Export or import groups via text file\n"
                "• `.blacklist` | `.unblacklist` — Quarantine management\n"
                "\n📊 **Analytics & System Monitoring:**\n"
                "• `.ping` — Check latency and engine health\n"
                "• `.stats` — Display live runtime metrics & speed\n"
                "• `.analytics` — View top group delivery success rates\n"
                "• `.status` — Display sleek system configuration panel\n"
                "• `.spintax <text>` — Test dynamic spintax template\n"
                "• `.info` | `.night` — Account details and Auto-Night window\n"
                "• `.error` — Display recent error/failure tracebacks"
            )

    async def forward_loop():
        while True:
            tz = AUTONIGHT_CFG.get("tz", DEFAULT_AUTONIGHT["tz"])
            user_state["ads_updated"] = False
            user_state["wake_event"].clear()
            ad_reload_needed = False
            try:
                # ⏸️ Pause Check
                if user_state.get("is_paused", False) or config.get("is_paused", False):
                    user_state["status"] = "Paused ⏸️"
                    now = _get_now_tz(tz)
                    user_state["next_msg_at"] = now + timedelta(minutes=user_state["cycle"])
                    await interruptible_sleep(lambda: user_state["next_msg_at"], tz, wake_event=user_state["wake_event"])
                    continue

                # 🎯 Check if target groups are configured first & filter out Blacklisted groups
                groups_list = _get_config_groups(config)
                blk_set = {b.rstrip('/') for b in (user_state.get("blacklist") or config.get("blacklist") or [])}
                groups_list = [g for g in groups_list if g.rstrip('/') not in blk_set]

                if not groups_list:
                    log_event("No active target groups configured (or all blacklisted).")
                    user_state["status"] = "Idle (No Groups) 😴"
                    now = _get_now_tz(tz)
                    user_state["next_msg_at"] = now + timedelta(minutes=user_state["cycle"])
                    await interruptible_sleep(lambda: user_state["next_msg_at"], tz, wake_event=user_state["wake_event"])
                    continue

                # 💎 Fetch active user ad messages from Saved Messages (up to 100)
                user_state["status"] = "Fetching Msgs 🔍"
                valid_messages = await get_active_ad_messages(client)

                if not valid_messages:
                    log_event("No valid user ad messages in Saved Messages.")
                    user_state["status"] = "Idle (No Ads) 😴"
                    now = _get_now_tz(tz)
                    user_state["next_msg_at"] = now + timedelta(minutes=user_state["cycle"])
                    await interruptible_sleep(lambda: user_state["next_msg_at"], tz, wake_event=user_state["wake_event"])
                    continue

                # Forward messages one by one
                for msg_idx, msg in enumerate(valid_messages, 1):
                    if user_state.get("ads_updated", False) or ad_reload_needed:
                        log_event("📢 Ad update detected! Aborting queue to reload fresh ads immediately...")
                        user_state["ads_updated"] = False
                        user_state["wake_event"].clear()
                        ad_reload_needed = True
                        break

                    log_event(f"Processing message {msg_idx}/{len(valid_messages)}")
                    interrupted_by_night = False
                    
                    user_state["current_cycle_success"] = 0
                    user_state["current_cycle_fail"] = 0

                    groups_list = _get_config_groups(config)
                    blk_set = {b.rstrip('/') for b in (user_state.get("blacklist") or config.get("blacklist") or [])}
                    groups_list = [g for g in groups_list if g.rstrip('/') not in blk_set]

                    for i, group in enumerate(groups_list, 1):
                        if user_state.get("ads_updated", False) or ad_reload_needed:
                            log_event("📢 Ad update detected mid-group loop! Aborting to reload fresh ads immediately...")
                            user_state["ads_updated"] = False
                            user_state["wake_event"].clear()
                            ad_reload_needed = True
                            break

                        # Check if paused mid-loop
                        if user_state.get("is_paused", False):
                            break

                        # If night starts mid-cycle, break early
                        if autonight_is_quiet(AUTONIGHT_CFG):
                            interrupted_by_night = True
                            break

                        user_state["status"] = f"Msg {msg_idx} -> Grp {i}/{len(groups_list)} 📡"
                        send_start = _get_now_tz(tz)
                        custom_sleep_done = False
                        
                        try:
                            target_entity = await resolve_group_entity(client, group, config=config, phone=phone)
                            if isinstance(target_entity, str):
                                log_event(f"Cannot resolve {group}. Auto-removing from target groups.")
                                user_state["fail_total"] += 1
                                user_state["current_cycle_fail"] += 1
                                await asyncio.to_thread(db.log_group_analytics, phone, group, False, "Cannot Resolve")
                                await auto_remove_group(group, "Cannot Resolve / Access Denied")
                                continue

                            # 🛡️ Audit write permission prior to sending
                            perm_status = await check_write_permission(client, target_entity)
                            if perm_status != "Healthy":
                                log_event(f"Group {group} is '{perm_status}'. Auto-removing from target groups.")
                                user_state["fail_total"] += 1
                                user_state["current_cycle_fail"] += 1
                                await asyncio.to_thread(db.log_group_analytics, phone, group, False, perm_status)
                                await auto_remove_group(group, perm_status)
                                continue

                            # 🔄 Refresh Saved Messages list real-time so smart keyword matching & deletion checks reflect current state
                            fresh_valid = await get_active_ad_messages(client)
                            if not fresh_valid:
                                log_event("⚠️ All ads deleted or removed from Saved Messages. Skipping ad loop.")
                                ad_reload_needed = True
                                break

                            valid_ids = {m.id for m in fresh_valid}

                            # 🎯 Smart Ad Sender: Select message matching group topic tags, or fall back to default flow
                            send_msg = msg
                            if user_state.get("smart_ad_mode", True):
                                matched_msg = find_smart_matched_message(group, target_entity, fresh_valid)
                                if matched_msg:
                                    send_msg = matched_msg
                                    log_event(f"🎯 Smart Ad Tag Match for {group}")

                            # 🔄 Live Saved Message Verification: Ensure send_msg is STILL active in Saved Messages
                            if send_msg.id not in valid_ids:
                                log_event(f"⚠️ Saved Message #{send_msg.id} was deleted or removed from Saved Messages. Skipping deleted ad.")
                                continue

                            # Use latest version of send_msg from fresh_valid (reflecting any real-time text edits)
                            send_msg = next((m for m in fresh_valid if m.id == send_msg.id), send_msg)

                            if user_state["use_copy"]:
                                # 🌈 Copy Mode (with Spintax support & formatting)
                                user_state["msg_seq"] += 1
                                base_text = (send_msg.text or "").strip()
                                caption = parse_spintax(base_text)

                                from telethon.tl.types import MessageMediaWebPage
                                has_media = send_msg.media and not isinstance(send_msg.media, MessageMediaWebPage)

                                # Send with transient retry loop
                                for retry_attempt in range(3):
                                    try:
                                        if has_media:
                                            await client.send_file(target_entity, send_msg.media, caption=caption, formatting_entities=send_msg.entities)
                                        else:
                                            await client.send_message(target_entity, caption, formatting_entities=send_msg.entities)
                                        break
                                    except (ConnectionError, TimeoutError, asyncio.TimeoutError) as ne:
                                        if retry_attempt < 2:
                                            await asyncio.sleep(1.5)
                                        else:
                                            raise ne
                            else:
                                # 🔄 Forward Mode with transient retry loop
                                for retry_attempt in range(3):
                                    try:
                                        await client.forward_messages(target_entity, send_msg)
                                        break
                                    except (ConnectionError, TimeoutError, asyncio.TimeoutError) as ne:
                                        if retry_attempt < 2:
                                            await asyncio.sleep(1.5)
                                        else:
                                            raise ne

                            user_state["success_total"] += 1
                            user_state["current_cycle_success"] += 1
                            log_event(f"Msg {msg_idx} Success -> {group}")
                            await asyncio.to_thread(db.log_group_analytics, phone, group, True, "OK")

                        except FloodWaitError as e:
                             log_event(f"FloodWait! Sleeping {e.seconds}s. Increasing delay for account safety.")
                             user_state["status"] = f"FloodWait ⏳ ({e.seconds}s)"
                             user_state["delay"] = min(user_state["delay"] + 25, 600)
                             config["msg_delay_sec"] = user_state["delay"]
                             db.update_user_config(phone, msg_delay_sec=user_state["delay"])
                             now = _get_now_tz(tz)
                             user_state["next_msg_at"] = now + timedelta(seconds=e.seconds + 10)
                             await interruptible_sleep(lambda: user_state["next_msg_at"], tz, wake_event=user_state["wake_event"])
                             custom_sleep_done = True
                        except PeerFloodError as e:
                             log_event("⚠️ Telegram PeerFlood limit! Cool-down mode activated. Pausing 20m & increasing delay.")
                             user_state["status"] = "Peer Flood Limit ⏳ (20m)"
                             user_state["delay"] = min(user_state["delay"] + 30, 300)
                             config["msg_delay_sec"] = user_state["delay"]
                             db.update_user_config(phone, msg_delay_sec=user_state["delay"])
                             now = _get_now_tz(tz)
                             user_state["next_msg_at"] = now + timedelta(minutes=20)
                             await interruptible_sleep(lambda: user_state["next_msg_at"], tz, wake_event=user_state["wake_event"])
                             custom_sleep_done = True
                        except SlowModeWaitError as e:
                             log_event(f"Slowmode in {group}. Waiting {e.seconds}s")
                             user_state["status"] = f"Slowmode ⏳ ({e.seconds}s)"
                             now = _get_now_tz(tz)
                             user_state["next_msg_at"] = now + timedelta(seconds=e.seconds + 2)
                             await interruptible_sleep(lambda: user_state["next_msg_at"], tz, wake_event=user_state["wake_event"])
                             custom_sleep_done = True
                        except (UserBannedInChannelError, ChatWriteForbiddenError, ChannelPrivateError, ChatAdminRequiredError) as e:
                             err_name = type(e).__name__
                             log_event(f"Forbidden in {group} ({err_name}). Auto-removing group from target list.")
                             await asyncio.to_thread(db.log_group_analytics, phone, group, False, err_name)
                             await auto_remove_group(group, err_name)
                             user_state["fail_total"] += 1
                             user_state["current_cycle_fail"] += 1
                        except RPCError as e:
                             err_str = str(e).lower()
                             err_name = type(e).__name__
                             if "not participant" in err_str or "usernotparticipant" in err_str or "banned" in err_str or "forbidden" in err_str or "private" in err_str:
                                 log_event(f"Group {group} restriction ({err_name}). Auto-removing group from target list.")
                                 await auto_remove_group(group, err_name)
                             else:
                                 log_event(f"RPCError for {group}: {e}")
                             await asyncio.to_thread(db.log_group_analytics, phone, group, False, err_name)
                             user_state["fail_total"] += 1
                             user_state["current_cycle_fail"] += 1
                        except Exception as e:
                             import traceback
                             tb_str = traceback.format_exc()
                             log_event(f"Unable to send message to {group} ({type(e).__name__}: {e}). Skipping group.", details=tb_str)
                             await asyncio.to_thread(db.log_group_analytics, phone, group, False, type(e).__name__)
                             user_state["fail_total"] += 1
                             user_state["current_cycle_fail"] += 1

                        # Always sleep the delay between groups (unless custom sleep occurred or it is the last group)
                        if i < len(groups_list) and not custom_sleep_done:
                            if autonight_is_slow_mode(AUTONIGHT_CFG):
                                eff_delay = max(user_state["delay"], 60.0) # 60s-75s slow delay overnight
                            else:
                                eff_delay = max(user_state["delay"], 35.0) # 35s-45s safe delay daytime

                            wait_time = eff_delay * random.uniform(0.85, 1.25)
                            elapsed = (_get_now_tz(tz) - send_start).total_seconds()
                            remaining_wait = max(0.5, wait_time - elapsed)
                            
                            now = _get_now_tz(tz)
                            user_state["next_msg_at"] = now + timedelta(seconds=remaining_wait)
                            await interruptible_sleep(lambda: user_state["next_msg_at"], tz, wake_event=user_state["wake_event"])
                            if user_state.get("ads_updated", False):
                                log_event("📢 Ad update detected during inter-group sleep! Aborting cycle...")
                                user_state["ads_updated"] = False
                                user_state["wake_event"].clear()
                                ad_reload_needed = True
                                break
                        elif i == len(groups_list):
                            user_state["next_msg_at"] = None

                    if ad_reload_needed:
                        break

                    # 🛡️ Adaptive Safety Backoff: If cycle had errors, increase delay by +5s to protect account
                    if user_state["current_cycle_fail"] > 0:
                        user_state["delay"] = min(user_state["delay"] + 5, 120)
                        config["msg_delay_sec"] = user_state["delay"]
                        db.update_user_config(phone, msg_delay_sec=user_state["delay"])
                        log_event(f"🛡️ Errors detected in cycle. Adaptively increased delay to {user_state['delay']}s for account safety.")
                    elif user_state["current_cycle_success"] > 0 and user_state["delay"] > 25:
                        user_state["delay"] -= 1
                        config["msg_delay_sec"] = user_state["delay"]
                        db.update_user_config(phone, msg_delay_sec=user_state["delay"])

                    log_event(f"Msg {msg_idx} cycle complete. Success: {user_state['current_cycle_success']}, Fail: {user_state['current_cycle_fail']}")
                    
                    # Interval delay between different messages (with organic Timing Jitter)
                    if msg_idx < len(valid_messages):
                        if autonight_is_slow_mode(AUTONIGHT_CFG):
                            user_state["status"] = "Waiting 🌙 (Night Slow Mode)"
                            sleep_seconds = random.randint(900, 1200) # 15-20 minutes overnight
                        else:
                            user_state["status"] = f"Waiting for next msg ⏳"
                            sleep_seconds = _get_cycle_seconds_with_jitter(user_state["cycle"])
                        now = _get_now_tz(tz)
                        user_state["next_msg_at"] = now + timedelta(seconds=sleep_seconds)
                        await interruptible_sleep(lambda: user_state["next_msg_at"], tz, wake_event=user_state["wake_event"])
                        if user_state.get("ads_updated", False):
                            log_event("📢 Ad update detected during inter-msg sleep! Reloading ads...")
                            user_state["ads_updated"] = False
                            user_state["wake_event"].clear()
                            ad_reload_needed = True
                            break

                if ad_reload_needed or user_state.get("ads_updated", False):
                    user_state["ads_updated"] = False
                    user_state["wake_event"].clear()
                    log_event("🔄 Reloading updated ads real-time without delay...")
                    continue

                # After all messages are processed, wait the cycle delay again before checking for new messages
                if autonight_is_slow_mode(AUTONIGHT_CFG):
                    user_state["status"] = "Idle 🌙 (Night Slow Mode)"
                    sleep_seconds = random.randint(900, 1200) # 15-20 minutes overnight
                else:
                    user_state["status"] = "Idle 😴"
                    sleep_seconds = _get_cycle_seconds_with_jitter(user_state["cycle"])
                now = _get_now_tz(tz)
                user_state["next_msg_at"] = now + timedelta(seconds=sleep_seconds)
                await interruptible_sleep(lambda: user_state["next_msg_at"], tz, wake_event=user_state["wake_event"])
                if user_state.get("ads_updated", False):
                    user_state["ads_updated"] = False
                    user_state["wake_event"].clear()
                    log_event("🔄 Reloading updated ads real-time after sleep interrupt...")
                    continue

            except Exception as e:
                import traceback
                tb_str = traceback.format_exc()
                log_event(f"Error in forward loop: {e}", details=tb_str)
                await asyncio.sleep(60)


    asyncio.create_task(forward_loop())
    try:
        await client.run_until_disconnected()
    except Exception as e:
        logger.error(f"[{phone}] Disconnected with error: {e}")
    finally:
        active_bots.pop(phone, None)
        try:
            await client.disconnect()
        except Exception:
            pass
        if phone in started_phones:
            started_phones.remove(phone)
        log_event(f"Bot for {phone} stopped.")

async def user_loader():
    config_mtimes = {} # phone -> last_updated_at
    while True:
        try:
            configs = await asyncio.to_thread(db.get_all_user_configs)
            for config in configs:
                phone = config.get("phone")
                updated_at = config.get("updated_at", 0.0)
                if not phone:
                    continue
                # Only load if new or modified
                if phone not in config_mtimes or updated_at > config_mtimes[phone]:
                    if phone not in started_phones:
                        asyncio.create_task(run_user_bot(config))
                    else:
                        # Update active bot in place
                        if phone in active_bots:
                            bot = active_bots[phone]
                            bot["config"].update(config)
                            # Sync state values
                            state = bot["state"]
                            state["delay"] = config.get("msg_delay_sec", 20)
                            state["cycle"] = config.get("cycle_delay_min", 7)
                    config_mtimes[phone] = updated_at
        except Exception as e:
            logger.error(f"Error loading user configs from database: {e}")
        await asyncio.sleep(10) # Check every 10s for faster configuration updates

async def auto_restart_watchdog():
    start_time = time_mod.time()
    max_uptime = 30 * 3600 # 30 hours from startup
    while True:
        await asyncio.sleep(60)
        elapsed = time_mod.time() - start_time
        if elapsed >= max_uptime:
            logger.info("30-hour process uptime reached. Auto-restarting runner process...")
            print(Fore.YELLOW + "\n[🔁] 30-hour process uptime reached. Auto-restarting engine...")
            if os.path.exists(PID_FILE):
                try:
                    os.remove(PID_FILE)
                except Exception:
                    pass
            os.execv(sys.executable, [sys.executable] + sys.argv)

async def main():
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    asyncio.create_task(auto_restart_watchdog())
    
    # Write PID file
    pid_file = PID_FILE
    with open(pid_file, "w") as f:
        f.write(str(os.getpid()))

    print(f"{Fore.CYAN}{Style.BRIGHT}╔════════════════════════════════════════════╗")
    print(f"{Fore.CYAN}║    {Fore.YELLOW}KURUP ADS V5 ELITE - WORKER ENGINE      {Fore.CYAN}║")
    print(f"{Fore.CYAN}║    {Fore.GREEN}Status: Operational                     {Fore.CYAN}║")
    print(f"{Fore.CYAN}╚════════════════════════════════════════════╝{Style.RESET_ALL}")
    print(f"{Fore.WHITE}Logs will appear below in real-time...\n")
    
    # Setup signal handlers for graceful shutdown
    loop = asyncio.get_running_loop()
    def stop_all():
        logger.info("Shutdown signal received. Stopping...")
        for task in asyncio.all_tasks():
            task.cancel()

    if os.name != 'nt': # Signals not fully supported on Windows this way
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_all)
    
    try:
        await user_loader()
    except asyncio.CancelledError:
        pass
    finally:
        try:
            if os.path.exists(pid_file):
                os.remove(pid_file)
        except Exception:
            pass

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutdown requested. Exiting.")
        try:
            if os.path.exists(PID_FILE):
                os.remove(PID_FILE)
        except Exception:
            pass
