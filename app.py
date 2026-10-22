#!/usr/bin/env python3
"""Small LAN-first admin panel for Project Reclaimer dedicated servers."""
from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import struct
import subprocess
import threading
import time
import tomllib
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from pathlib import PurePosixPath
from urllib.parse import parse_qs, urlparse

import tomlkit

SERVER_DIR = Path(os.environ.get("SERVER_DIR", "/server")).resolve()
STATIC_DIR = Path(__file__).parent / "static"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
PORT = int(os.environ.get("PORT", "8780"))
BIND_HOST = os.environ.get("BIND_HOST", "127.0.0.1")
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false").lower() == "true"
SESSIONS: dict[str, tuple[str, float]] = {}
SESSIONS_LOCK = threading.Lock()
SESSION_TTL = 12 * 60 * 60
MOD_UPLOADS: dict[str, dict] = {}
MOD_UPLOADS_LOCK = threading.Lock()
MAX_MOD_UPLOAD_BYTES = 8 * 1024 * 1024 * 1024
MAX_MOD_UPLOAD_FILE_BYTES = 4 * 1024 * 1024 * 1024
STEAMCMD_DIR = Path(os.environ.get("STEAMCMD_DIR", "/opt/steamcmd")).resolve()
STEAMCMD_PATH = Path(os.environ.get("STEAMCMD_PATH", str(STEAMCMD_DIR / "steamcmd.sh"))).resolve()
STEAMCMD_HOME = Path(os.environ.get("STEAMCMD_HOME", "/home/steamcmd")).resolve()
STEAM_WORKSHOP_APP_ID = "976730"  # Halo: The Master Chief Collection
WORKSHOP_DOWNLOADS: dict[str, dict] = {}
WORKSHOP_DOWNLOADS_LOCK = threading.Lock()
WORKSHOP_DOWNLOAD_LOCK = threading.Lock()
SCHEDULES_PATH = SERVER_DIR / "admin-ui-playlist-schedules.json"
SCHEDULES_LOCK = threading.RLock()
PLAYLIST_VALIDATION_DIR = SERVER_DIR / ".admin-playlist-validation"
PLAYLIST_VALIDATION_LOCK = threading.Lock()
CONTENT_CATALOG_PATH = PLAYLIST_VALIDATION_DIR / "catalog.txt"
CONTENT_CATALOG_CACHE: dict = {"mtime": None, "data": None}
CONTENT_CATALOG_SECTIONS = {"Maps:": "maps", "Map variants:": "map_variants",
                            "Game types:": "game_types", "Game variants:": "game_variants"}
CONTENT_CATALOG_ENTRY = re.compile(r'^\s+"([^"]*)"(?:\s+(.*))?$')


class PanelError(Exception):
    pass


def read_toml():
    path = SERVER_DIR / "dedicated.toml"
    if not path.is_file():
        raise PanelError("dedicated.toml was not found in the shared server folder")
    return tomlkit.parse(path.read_text(encoding="utf-8"))


def config_servers():
    doc = read_toml()
    global_rcon = doc.get("rcon", {})
    servers = []
    for index, server in enumerate(doc.get("server", []), start=1):
        port = int(server.get("port", 0))
        campaign = server.get("campaign")
        campaign_data = None
        if campaign:
            modifiers = campaign.get("modifiers", {})
            campaign_data = {
                "mission": int(campaign.get("mission", 3010)),
                "insertion": int(campaign.get("insertion", 0)),
                "difficulty": str(campaign.get("difficulty", "normal")),
                "custom_characters": bool(campaign.get("custom_characters", False)),
                "modifiers": {key: int(modifiers[key]) for key in (
                    "health_percent", "damage_percent",
                    "health_per_player_percent", "damage_per_player_percent"
                ) if key in modifiers},
            }
        servers.append({
            "index": index,
            "name": str(server.get("name", f"Server {index}")),
            "port": port,
            "max_players": int(server.get("max_players", doc.get("defaults", {}).get("max_players", 16))),
            "mode": "campaign" if campaign_data is not None else "multiplayer",
            "campaign": campaign_data,
            # The panel displays playlist basenames, while dedicated.toml
            # paths are relative to SERVER_DIR and must include "playlists/".
            "playlist": Path(str(server.get("playlist", ""))).name if server.get("playlist") else "",
            "enabled": bool(server.get("enabled", True)),
            "rcon_port": int(server.get("rcon_port", port)),
            "rcon_password": str(server.get("rcon_password", global_rcon.get("password", ""))),
            "rcon_address": str(global_rcon.get("address", "127.0.0.1")),
        })
    return doc, servers


def safe_playlist_path(name: str) -> Path:
    if not name or Path(name).name != name or not name.endswith(".json"):
        raise PanelError("Choose a playlist JSON file from the playlists folder")
    root = (SERVER_DIR / "playlists").resolve()
    path = (root / name).resolve()
    if path.parent != root:
        raise PanelError("Invalid playlist path")
    return path


def validate_playlist(data):
    if not isinstance(data, dict) or not isinstance(data.get("games"), list) or len(data["games"]) < 3:
        raise PanelError("Playlist JSON needs a games list with at least 3 games")
    for game in data["games"]:
        if not isinstance(game, dict) or not isinstance(game.get("map"), str) or not isinstance(game.get("game"), str):
            raise PanelError("Each playlist entry needs string map and game values")
    return data


def validate_with_reclaimer(name: str, data):
    """Ask the companion Reclaimer container to validate a staged playlist."""
    validate_playlist(data)
    ready_file = PLAYLIST_VALIDATION_DIR / "worker-ready"
    if not ready_file.is_file() or time.time() - ready_file.stat().st_mtime > 10:
        raise PanelError("Reclaimer playlist validation is unavailable. Start the playlist-validator service from the Compose file.")
    (SERVER_DIR / "playlists").mkdir(parents=True, exist_ok=True)
    request_id = uuid.uuid4().hex
    staged_name = f"admin-validation-{request_id}.playlist.json"
    staged_path = safe_playlist_path(staged_name)
    queue_dir = PLAYLIST_VALIDATION_DIR / "requests"
    result_dir = PLAYLIST_VALIDATION_DIR / "results"
    queue_dir.mkdir(parents=True, exist_ok=True)
    result_dir.mkdir(parents=True, exist_ok=True)
    request_path = queue_dir / f"{request_id}.txt"
    result_path = result_dir / f"{request_id}.txt"
    staged_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        with PLAYLIST_VALIDATION_LOCK:
            request_path.write_text(staged_name + "\n", encoding="ascii")
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                if result_path.is_file():
                    lines = result_path.read_text(encoding="utf-8", errors="replace").splitlines()
                    result_path.unlink(missing_ok=True)
                    try:
                        exit_code = int(lines[0])
                    except (IndexError, ValueError):
                        raise PanelError("Reclaimer validator returned an unreadable result")
                    output = "\n".join(lines[1:]).strip()
                    if exit_code != 0 or re.search(r"(?im)^Error:", output):
                        raise PanelError(f"Reclaimer rejected {name}: {output or 'playlist validation failed'}")
                    return output
                time.sleep(0.25)
            raise PanelError("Timed out waiting for the Reclaimer playlist validator. Check that the playlist-validator service is running.")
    finally:
        request_path.unlink(missing_ok=True)
        result_path.unlink(missing_ok=True)
        staged_path.unlink(missing_ok=True)


def delete_playlist(name: str):
    name = uploaded_playlist_name(name)
    path = safe_playlist_path(name)
    if not path.is_file():
        raise PanelError("That playlist was not found")
    _, servers = config_servers()
    assigned = [server["name"] for server in servers if server["playlist"] == name]
    schedules = read_schedules()
    scheduled = [port for port, schedule in schedules.items()
                 if name in schedule.get("playlists", [])]
    if assigned or scheduled:
        used_by = []
        if assigned:
            used_by.append("servers: " + ", ".join(assigned))
        if scheduled:
            used_by.append("saved schedules on ports: " + ", ".join(scheduled))
        raise PanelError("Remove this playlist from " + "; ".join(used_by) + " before deleting it")
    write_backup(path)
    path.unlink()
    return "Playlist deleted. A .bak backup was kept."


def uploaded_playlist_name(name: str) -> str:
    if Path(name).name != name or "/" in name or "\\" in name or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}\.playlist\.json", name):
        raise PanelError("Playlist filename must use letters, numbers, dots, underscores or hyphens and end in .playlist.json")
    return name


def packet(sock: socket.socket, request_id: int, kind: int, payload: str):
    body = payload.encode("utf-8") + b"\0\0"
    sock.sendall(struct.pack("<iii", len(body) + 8, request_id, kind) + body)


def recv_exact(sock: socket.socket, count: int) -> bytes:
    data = bytearray()
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise PanelError("RCON closed the connection")
        data.extend(chunk)
    return bytes(data)


def recv_packet(sock: socket.socket):
    size = struct.unpack("<i", recv_exact(sock, 4))[0]
    if size < 10 or size > 1_048_576:
        raise PanelError("RCON returned an invalid packet")
    body = recv_exact(sock, size)
    request_id, kind = struct.unpack("<ii", body[:8])
    text = body[8:-2].decode("utf-8", errors="replace")
    return request_id, kind, text


class SourceRcon:
    def __init__(self, host: str, port: int, password: str):
        self.host, self.port, self.password = host, port, password

    def command(self, command: str) -> str:
        if not self.password:
            raise PanelError("RCON is off. Set a password in the [rcon] section of dedicated.toml")
        with socket.create_connection((self.host, self.port), timeout=3) as sock:
            sock.settimeout(4)
            packet(sock, 1, 3, self.password)
            # Some Source RCON servers send a RESPONSE_VALUE packet before
            # the AUTH_RESPONSE packet. Reclaimer does this; ignore the
            # preamble and wait for the actual authentication result.
            auth_deadline = time.monotonic() + 4
            while True:
                remaining = auth_deadline - time.monotonic()
                if remaining <= 0:
                    raise PanelError("Timed out waiting for RCON authentication response")
                sock.settimeout(remaining)
                auth_id, auth_type, _ = recv_packet(sock)
                if auth_id == -1:
                    raise PanelError("RCON authentication failed; check the [rcon] password")
                if auth_id == 1 and auth_type == 0:
                    continue
                if auth_id == 1 and auth_type == 2:
                    break
                raise PanelError(
                    f"Unexpected RCON authentication response (id={auth_id}, type={auth_type})"
                )
            packet(sock, 2, 2, command)
            # Source RCON uses a second, empty command as an end-of-response marker.
            packet(sock, 3, 2, "")
            chunks = []
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    request_id, _, response = recv_packet(sock)
                except socket.timeout:
                    break
                if request_id == 2:
                    chunks.append(response)
                if request_id == 3:
                    return "\n".join(chunks).strip()
            if chunks:
                return "\n".join(chunks).strip()
            raise PanelError("RCON did not return a response")


def parse_live(status: str, players: str):
    combined = f"{status}\n{players}"
    current, maximum, game = None, None, None
    patterns = [
        r"(?:players?|occupancy)\s*[:=]\s*(\d+)\s*/\s*(\d+)",
        r"\b(\d+)\s*/\s*(\d+)\s+players?\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, combined, re.I)
        if match:
            current, maximum = int(match.group(1)), int(match.group(2))
            break
    if current is None:
        ids = re.findall(r"\b[0-9a-f]{64}\b", players, re.I)
        if ids:
            current = len(set(ids))
        elif re.search(r"\bno players?\b|\bplayers?:\s*0\b", players, re.I):
            current = 0
    for line in status.splitlines():
        if re.search(r"^\s*(current game|game(?: mode| type)?|now playing|playing|map)\s*[:=]", line, re.I):
            game = line.strip()
            break
    if game is None:
        # Reclaimer's status response reports the active rotation as a plain
        # line such as "Slayer on Avalanche · in the lobby", without a label.
        for line in status.splitlines():
            candidate = line.strip()
            if re.search(r"\s+on\s+", candidate, re.I):
                game = candidate.split(" · ", 1)[0].strip()
                break
    return {"player_count": current, "reported_max": maximum, "current_game": game}


def parse_players(text: str):
    """Extract selectable player IDs and a useful name from RCON player rows."""
    rows = []
    seen = set()
    for line in text.splitlines():
        for match in re.finditer(r"(?<![0-9a-f])([0-9a-f]{64})(?![0-9a-f])", line, re.I):
            player_id = match.group(1)
            if player_id.lower() in seen:
                continue
            seen.add(player_id.lower())
            name_match = re.search(r"(?:name|player)\s*[:=]\s*[\"']?([^,;|\"']+)", line, re.I)
            if name_match:
                name = name_match.group(1).strip()
            else:
                name = line[:match.start()].strip()
                name = re.sub(r"^\s*(?:player\s*)?#?\d+\s*[:.)-]?\s*", "", name, flags=re.I)
                name = name.strip(" \t:-|,()[]{}\"")
                if not name or name.lower() in {"player", "id", "player id"}:
                    name = line[match.end():].strip(" \t:-|,()[]{}\"")
                    name = re.split(r"\s+(?:address|ip)\s*[:=]?\s*", name, maxsplit=1, flags=re.I)[0]
                    name = name.strip()
            rows.append({"id": player_id, "name": name or f"Player {len(rows) + 1}"})
    return rows


def collect_state():
    _, servers = config_servers()
    playlists_dir = SERVER_DIR / "playlists"
    playlist_names = sorted(p.name for p in playlists_dir.glob("*.json")
                            if not p.name.startswith("admin-validation-")) if playlists_dir.exists() else []
    doc = read_toml()
    workshop_items = list(doc.get("workshop", {}).get("items", []))
    result = []
    for server in servers:
        item = {key: value for key, value in server.items() if not key.startswith("rcon_")}
        item.update({"status": "offline", "player_count": None, "current_game": None,
                     "status_text": "", "players_text": "", "error": None})
        try:
            rcon = SourceRcon(server["rcon_address"], server["rcon_port"], server["rcon_password"])
            item["status_text"] = rcon.command("status")
            item["players_text"] = rcon.command("players")
            item["players"] = parse_players(item["players_text"])
            item.update(parse_live(item["status_text"], item["players_text"]))
            item["status"] = "online"
        except Exception as exc:
            item["error"] = str(exc)
            item["players"] = []
        result.append(item)
    return {"servers": result, "playlists": playlist_names, "workshop_items": workshop_items,
            "workshop_maps": installed_workshop_maps(),
            "content_catalog": load_content_catalog(),
            "playlist_schedules": read_schedules(),
            "steamcmd_enabled": STEAMCMD_PATH.is_file(),
            "updated_at": int(time.time())}


def installed_workshop_maps():
    """Return playlist map references for Workshop packages installed in Mods."""
    root = SERVER_DIR / "content" / "Mods"
    found = {}
    if not root.is_dir():
        return []
    for info in root.rglob("ModInfo.json"):
        item_dir = info.parent
        item_id = item_dir.name if item_dir.name.isdigit() else ""
        if not item_id:
            item_id = next((parent.name for parent in item_dir.parents if parent.name.isdigit()), "")
        try:
            json.loads(info.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        maps_dir = item_dir / "maps"
        if not maps_dir.is_dir():
            continue
        for map_file in maps_dir.rglob("*.map"):
            if map_file.stem.lower() in {"shared", "campaign"}:
                continue
            name = map_file.stem
            reference = f"workshop:{item_id}/{name}" if item_id else name
            found[reference] = {"map": reference,
                                "label": f"{name} · Workshop {item_id}" if item_id else f"{name} · Installed mod"}
    return sorted(found.values(), key=lambda item: item["label"].casefold())


def parse_content_catalog(text: str):
    """Parse `dedicated content` output into base names and variants with their base map/mode."""
    catalog = {"maps": [], "map_variants": [], "game_types": [], "game_variants": []}
    section, source = None, "builtin"
    for line in text.splitlines():
        header = next((key for prefix, key in CONTENT_CATALOG_SECTIONS.items() if line.startswith(prefix)), None)
        if header:
            section, source = header, "builtin"
            continue
        if line and not line[0].isspace():
            section = None
            continue
        stripped = line.strip()
        if stripped.startswith("From ") and stripped.endswith(":"):
            source = "content" if stripped == "From your content folder:" else "builtin"
            continue
        match = CONTENT_CATALOG_ENTRY.match(line)
        if section is None or not match or not match.group(1):
            continue
        value, rest = match.group(1), (match.group(2) or "").strip()
        if section in ("maps", "game_types"):
            # Workshop maps already come from installed_workshop_maps().
            if not value.startswith("workshop:"):
                catalog[section].append(value)
            continue
        label, _, base = rest.rpartition(" · ")
        if not base:
            continue
        name = label[1:-1] if label.startswith('"') and label.endswith('"') else value
        catalog[section].append({"value": value, "name": name, "base": base, "source": source})
    for key in ("map_variants", "game_variants"):
        catalog[key].sort(key=lambda item: (item["name"].casefold(), item["value"].casefold()))
    return catalog


def load_content_catalog():
    """Return the cached Reclaimer content catalog written by the playlist-validator sidecar."""
    if not CONTENT_CATALOG_PATH.is_file():
        return None
    mtime = CONTENT_CATALOG_PATH.stat().st_mtime
    if CONTENT_CATALOG_CACHE["mtime"] != mtime:
        parsed = parse_content_catalog(CONTENT_CATALOG_PATH.read_text(encoding="utf-8", errors="replace"))
        CONTENT_CATALOG_CACHE.update(mtime=mtime, data={**parsed, "updated_at": int(mtime)} if parsed["game_types"] else None)
    return CONTENT_CATALOG_CACHE["data"]


def read_schedules():
    try:
        content = json.loads(SCHEDULES_PATH.read_text(encoding="utf-8"))
        if isinstance(content, dict):
            return content
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        pass
    return {}


def write_schedules(schedules):
    temp = SCHEDULES_PATH.with_name(f".{SCHEDULES_PATH.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(json.dumps(schedules, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, SCHEDULES_PATH)
    finally:
        temp.unlink(missing_ok=True)


def set_scheduled_playlist(port, playlist):
    doc, servers = config_servers()
    target = next((server for server in doc.get("server", []) if int(server.get("port", 0)) == port), None)
    panel_server = next((server for server in servers if server["port"] == port), None)
    if target is None or panel_server is None or panel_server["mode"] == "campaign":
        raise PanelError("The scheduled server is no longer configured for multiplayer")
    safe_playlist_path(playlist)
    target["playlist"] = f"playlists/{playlist}"
    for key in ("map", "game", "variant", "base_map"):
        target.pop(key, None)
    write_doc(doc)
    reply = SourceRcon(panel_server["rcon_address"], panel_server["rcon_port"],
                       panel_server["rcon_password"]).command("reload")
    if "configuration not reloaded" in reply.lower():
        raise PanelError(reply)
    return reply


def send_server_message(port, message):
    server = require_port(port)
    message = str(message).strip()
    if not message:
        raise PanelError("Enter a message to send")
    if len(message) > 300:
        raise PanelError("Messages must be 300 characters or fewer")
    if any(ord(char) < 32 or ord(char) == 127 for char in message):
        raise PanelError("Messages must be a single line without control characters")
    return SourceRcon(server["rcon_address"], server["rcon_port"],
                      server["rcon_password"]).command("say " + message)


def playlist_display_name(filename):
    try:
        data = json.loads(safe_playlist_path(filename).read_text(encoding="utf-8"))
        name = str(data.get("name", "")).strip()
        if name:
            return name[:120]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, PanelError):
        pass
    return Path(filename).stem[:120]


def playlist_schedule_loop():
    while True:
        time.sleep(10)
        now = time.time()
        with SCHEDULES_LOCK:
            schedules = read_schedules()
            changed = False
            for port_text, schedule in list(schedules.items()):
                if not schedule.get("enabled"):
                    continue
                playlists = schedule.get("playlists", [])
                if len(playlists) < 2:
                    continue
                next_switch_at = int(schedule.get("next_switch_at", 0))
                announce_minutes = int(schedule.get("announce_minutes", 5))
                if (announce_minutes > 0 and now < next_switch_at
                        and now >= next_switch_at - announce_minutes * 60
                        and int(schedule.get("announcement_sent_for", 0)) != next_switch_at):
                    next_index = (int(schedule.get("current_index", 0)) + 1) % len(playlists)
                    next_name = playlist_display_name(playlists[next_index])
                    minute_word = "minute" if announce_minutes == 1 else "minutes"
                    announcement = (f"The playlist will change to {next_name} in "
                                   f"{announce_minutes} {minute_word}. It takes effect at the next vote.")
                    try:
                        send_server_message(int(port_text), announcement)
                        schedule["announcement_sent_for"] = next_switch_at
                        schedule["announcement_error"] = ""
                    except Exception as exc:
                        schedule["announcement_error"] = str(exc)[:500]
                    changed = True
                if next_switch_at > now:
                    continue
                next_index = (int(schedule.get("current_index", 0)) + 1) % len(playlists)
                try:
                    set_scheduled_playlist(int(port_text), playlists[next_index])
                    schedule["current_index"] = next_index
                    schedule["last_switch_at"] = int(now)
                    schedule["next_switch_at"] = int(now + int(schedule["interval_minutes"]) * 60)
                    schedule["last_error"] = ""
                    schedule["announcement_sent_for"] = 0
                    schedule["announcement_error"] = ""
                except Exception as exc:
                    schedule["last_error"] = str(exc)[:1000]
                    schedule["next_switch_at"] = int(now + 60)
                changed = True
            if changed:
                write_schedules(schedules)


def write_backup(path: Path):
    backup = path.with_suffix(path.suffix + ".bak")
    temp = backup.with_name(f".{backup.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(path, temp)
        os.replace(temp, backup)
    finally:
        temp.unlink(missing_ok=True)


def write_doc(doc):
    path = SERVER_DIR / "dedicated.toml"
    temp = path.with_suffix(".toml.tmp")
    if path.exists():
        write_backup(path)
    temp.write_text(tomlkit.dumps(doc), encoding="utf-8")
    os.replace(temp, path)


def workshop_item_id(value: str) -> str:
    value = value.strip()
    if value.isdigit():
        item = value
    else:
        parsed = urlparse(value)
        if (parsed.scheme not in {"http", "https"}
                or parsed.hostname not in {"steamcommunity.com", "www.steamcommunity.com"}
                or parsed.path.rstrip("/") != "/sharedfiles/filedetails"):
            raise PanelError("Enter a Steam Workshop item URL or numeric item ID (collections are not supported)")
        item = parse_qs(parsed.query).get("id", [""])[0]
    if not item.isdigit() or len(item) < 5 or len(item) > 20:
        raise PanelError("The Workshop URL does not contain a valid item ID")
    return item


def workshop_content_path(item_id: str) -> Path:
    relative = Path("steamapps") / "workshop" / "content" / STEAM_WORKSHOP_APP_ID / item_id
    candidates = (
        STEAMCMD_DIR / relative,
        STEAMCMD_HOME / relative,
        STEAMCMD_HOME / ".steam" / "steam" / relative,
        STEAMCMD_HOME / ".steam" / "steamcmd" / relative,
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    raise PanelError("SteamCMD finished, but its downloaded Workshop folder was not found")


def perform_workshop_download(job_id: str, item_id: str, username: str, session_token: str):
    def update(**values):
        with WORKSHOP_DOWNLOADS_LOCK:
            job = WORKSHOP_DOWNLOADS.get(job_id)
            if job:
                job.update(values)

    if not WORKSHOP_DOWNLOAD_LOCK.acquire(blocking=False):
        update(status="failed", message="Another Workshop download is already running. Wait for it to finish.")
        return
    try:
        update(status="downloading", message=f"SteamCMD is downloading Workshop item {item_id}…")
        env = os.environ.copy()
        env["HOME"] = str(STEAMCMD_HOME)
        command = [str(STEAMCMD_PATH), "+login", username, "+workshop_download_item",
                   STEAM_WORKSHOP_APP_ID, item_id, "+quit"]
        try:
            result = subprocess.run(command, cwd=STEAMCMD_DIR, env=env, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=3600, check=False)
        except subprocess.TimeoutExpired:
            update(status="failed", message="SteamCMD timed out after 60 minutes. Check its sign-in and logs, then retry.")
            return
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        if result.returncode != 0 or "Success. Downloaded item" not in output:
            tail = "\n".join(output.splitlines()[-12:]).strip()
            update(status="failed", message=(tail or f"SteamCMD exited with code {result.returncode}")[-1800:])
            return

        source = workshop_content_path(item_id)
        info_path = source / "ModInfo.json"
        if not info_path.is_file():
            update(status="failed", message="The downloaded item has no top-level ModInfo.json; it does not look like a Reclaimer mod.")
            return
        try:
            json.loads(info_path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            update(status="failed", message="The downloaded item’s ModInfo.json is not valid JSON.")
            return

        total_size = 0
        for root, dirs, files in os.walk(source, followlinks=False):
            if any((Path(root) / name).is_symlink() for name in dirs + files):
                update(status="failed", message="The Workshop folder contains symbolic links and was not installed.")
                return
            for name in files:
                total_size += (Path(root) / name).stat().st_size
                if total_size > MAX_MOD_UPLOAD_BYTES:
                    update(status="failed", message="This Workshop item exceeds the 8 GiB installation limit.")
                    return

        mods = SERVER_DIR / "content" / "Mods" / "Workshop"
        mods.mkdir(parents=True, exist_ok=True)
        destination = mods / item_id
        staging = mods / f".{item_id}.installing-{uuid.uuid4().hex}"
        backup = mods / f".{item_id}.previous-{uuid.uuid4().hex}"
        moved_old = False
        try:
            shutil.copytree(source, staging)
            if destination.exists():
                os.replace(destination, backup)
                moved_old = True
            os.replace(staging, destination)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            if moved_old and backup.exists() and not destination.exists():
                os.replace(backup, destination)
            raise
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)

        doc = read_toml()
        if "workshop" not in doc:
            doc["workshop"] = tomlkit.table()
        items = doc["workshop"].get("items", [])
        changed = False
        if int(item_id) not in [int(value) for value in items]:
            items.append(int(item_id))
            doc["workshop"]["items"] = items
            changed = True
        if str(doc["workshop"].get("steamcmd", "")) != "/opt/steamcmd/reclaimer-steamcmd.sh":
            doc["workshop"]["steamcmd"] = "/opt/steamcmd/reclaimer-steamcmd.sh"
            changed = True
        if str(doc["workshop"].get("username", "")) != username:
            doc["workshop"]["username"] = username
            changed = True
        if changed:
            write_doc(doc)
        update(status="complete", message=f"Downloaded item {item_id} into content/Mods/Workshop/{item_id} and saved its ID in dedicated.toml. Use `dedicated mods` to find its map name for a playlist.")
    except Exception as exc:
        update(status="failed", message=str(exc))
    finally:
        WORKSHOP_DOWNLOAD_LOCK.release()


def require_port(port: int):
    _, servers = config_servers()
    for server in servers:
        if server["port"] == port:
            return server
    raise PanelError("That game port is not configured in dedicated.toml")


def valid_target(target: str):
    target = target.strip()
    if re.fullmatch(r"[0-9a-fA-F]{64}", target):
        return target
    try:
        if "/" in target:
            return str(ipaddress.ip_network(target, strict=False))
        return str(ipaddress.ip_address(target))
    except ValueError:
        raise PanelError("Ban target must be a 64-character player ID or an IP address/range")


class Handler(BaseHTTPRequestHandler):
    server_version = "ReclaimerAdmin/0.1"

    def log_message(self, fmt, *args):
        print("admin-ui:", fmt % args)

    def send_json(self, obj, status=200, headers=None):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if headers:
            for key, value in headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def body_json(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size > 2_000_000:
                raise PanelError("Request is too large")
            return json.loads(self.rfile.read(size) or b"{}")
        except (ValueError, json.JSONDecodeError):
            raise PanelError("Request body must be valid JSON")

    def session(self):
        token = ""
        for pair in self.headers.get("Cookie", "").split(";"):
            key, sep, value = pair.strip().partition("=")
            if sep and key == "admin_session":
                token = value
        with SESSIONS_LOCK:
            entry = SESSIONS.get(token)
            if entry and entry[1] > time.time():
                return entry
            SESSIONS.pop(token, None)
        return None

    def authenticated(self, write=False):
        session = self.session()
        if not session:
            self.send_json({"error": "Sign in to continue"}, 401)
            return None
        if write:
            origin = self.headers.get("Origin", "")
            host = self.headers.get("Host", "")
            if origin and urlparse(origin).netloc != host:
                self.send_json({"error": "Request origin did not match this panel"}, 403)
                return None
            if not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), session[0]):
                self.send_json({"error": "Refresh the page and try again"}, 403)
                return None
        return session

    def get_mod_upload(self, token: str, session):
        with MOD_UPLOADS_LOCK:
            upload = MOD_UPLOADS.get(token)
            if not upload or not secrets.compare_digest(upload["session"], session[0]):
                raise PanelError("Upload session expired; choose the folder again")
            return upload

    def receive_mod_upload_file(self, parsed, session):
        query = parse_qs(parsed.query)
        token = query.get("upload", [""])[0]
        relative = query.get("path", [""])[0]
        upload = self.get_mod_upload(token, session)
        rel = PurePosixPath(relative)
        if not relative or rel.is_absolute() or any(part in ("", ".", "..") for part in rel.parts) or "\\" in relative:
            raise PanelError("Invalid file path in selected folder")
        size = int(self.headers.get("Content-Length", "-1"))
        if size < 0 or size > MAX_MOD_UPLOAD_FILE_BYTES:
            raise PanelError("A mod file is too large to upload (4 GiB maximum per file)")
        if relative in upload["files"]:
            raise PanelError("The selected folder contains a duplicate file path")
        if upload["total"] + size > MAX_MOD_UPLOAD_BYTES:
            raise PanelError("The selected mod folder exceeds the 8 GiB upload limit")
        target = upload["staging"].joinpath(*rel.parts)
        if upload["staging"].resolve() not in target.parent.resolve().parents and target.parent.resolve() != upload["staging"].resolve():
            raise PanelError("Invalid file path in selected folder")
        target.parent.mkdir(parents=True, exist_ok=True)
        received = 0
        try:
            with target.open("xb") as out:
                while received < size:
                    chunk = self.rfile.read(min(1024 * 1024, size - received))
                    if not chunk:
                        raise PanelError("Upload ended before the whole file arrived")
                    out.write(chunk)
                    received += len(chunk)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        upload["files"].add(relative)
        upload["total"] += received
        self.send_json({"ok": True, "bytes": received})

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self.send_json({"ok": True})
            return
        if parsed.path.startswith("/api/"):
            session = self.authenticated()
            if not session:
                return
            try:
                if parsed.path == "/api/session":
                    self.send_json({"csrf": self.session()[0]})
                elif parsed.path == "/api/state":
                    self.send_json(collect_state())
                elif parsed.path == "/api/workshop-download/status":
                    job_id = parse_qs(parsed.query).get("job", [""])[0]
                    with WORKSHOP_DOWNLOADS_LOCK:
                        job = WORKSHOP_DOWNLOADS.get(job_id)
                        if not job or not secrets.compare_digest(job["session"], session[0]):
                            raise PanelError("Workshop download job was not found; start it again")
                        self.send_json({key: job[key] for key in ("status", "message", "item_id")})
                elif parsed.path == "/api/playlist":
                    name = parse_qs(parsed.query).get("file", [""])[0]
                    data = json.loads(safe_playlist_path(name).read_text(encoding="utf-8"))
                    self.send_json({"file": name, "data": data})
                else:
                    self.send_json({"error": "Not found"}, 404)
            except Exception as exc:
                self.send_json({"error": str(exc)}, 400)
            return
        if parsed.path == "/":
            path = STATIC_DIR / "index.html"
        else:
            path = (STATIC_DIR / parsed.path.lstrip("/")).resolve()
            if STATIC_DIR.resolve() not in path.parents:
                self.send_error(404)
                return
        if not path.is_file():
            self.send_error(404)
            return
        data = path.read_bytes()
        content_type = "text/html; charset=utf-8" if path.suffix == ".html" else "text/plain; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/mod-upload/file":
                session = self.authenticated(write=True)
                if session is None:
                    return
                self.receive_mod_upload_file(parsed, session)
                return
            data = self.body_json()
            if parsed.path == "/api/login":
                if not ADMIN_PASSWORD:
                    raise PanelError("Set ADMIN_PASSWORD in the Compose environment before using the panel")
                if not secrets.compare_digest(str(data.get("password", "")), ADMIN_PASSWORD):
                    self.send_json({"error": "Incorrect password"}, 401)
                    return
                token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
                with SESSIONS_LOCK:
                    SESSIONS[token] = (csrf, time.time() + SESSION_TTL)
                cookie = f"admin_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL}"
                if COOKIE_SECURE:
                    cookie += "; Secure"
                self.send_json({"ok": True, "csrf": csrf}, headers={"Set-Cookie": cookie})
                return
            if parsed.path == "/api/logout":
                session = self.authenticated(write=True)
                if session is None:
                    return
                token = self.session_token()
                with SESSIONS_LOCK:
                    SESSIONS.pop(token, None)
                self.send_json({"ok": True}, headers={"Set-Cookie": "admin_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"})
                return
            if self.authenticated(write=True) is None:
                return
            if parsed.path == "/api/server":
                port = int(data.get("port", 0))
                _, configured_servers = config_servers()
                panel_server = next((s for s in configured_servers if s["port"] == port), None)
                if panel_server is None:
                    raise PanelError("That game port is not configured in dedicated.toml")
                doc = read_toml()
                server = next(s for s in doc["server"] if int(s.get("port", 0)) == port)
                name = str(data.get("name", "")).strip()
                playlist = str(data.get("playlist", "")).strip()
                max_players = int(data.get("max_players", 0))
                if not name or len(name) > 48:
                    raise PanelError("Server name must be 1–48 characters")
                if panel_server["mode"] == "campaign":
                    if not 1 <= max_players <= 16:
                        raise PanelError("Halo 3 campaign co-op supports 1–16 players")
                    campaign_data = data.get("campaign")
                    if not isinstance(campaign_data, dict):
                        raise PanelError("Campaign settings are required for this server")
                    mission = int(campaign_data.get("mission", 0))
                    valid_missions = {3010, 3020, 3030, 3040, 3050, 3070, 3100, 3110, 3120}
                    if mission not in valid_missions:
                        raise PanelError("Choose a valid Halo 3 campaign mission")
                    insertion = int(campaign_data.get("insertion", 0))
                    if not 0 <= insertion <= 32:
                        raise PanelError("Mission starting point must be between 0 and 32")
                    difficulty = str(campaign_data.get("difficulty", "")).lower()
                    if difficulty not in {"easy", "normal", "heroic", "legendary"}:
                        raise PanelError("Choose Easy, Normal, Heroic, or Legendary difficulty")
                    custom_characters = campaign_data.get("custom_characters", False)
                    if not isinstance(custom_characters, bool):
                        raise PanelError("Custom characters must be enabled or disabled")
                    raw_modifiers = campaign_data.get("modifiers", {})
                    if not isinstance(raw_modifiers, dict):
                        raise PanelError("Campaign modifiers must be a settings object")
                    modifiers = tomlkit.inline_table()
                    for key, low, high, step in (
                        ("health_percent", 25, 1000, 25),
                        ("damage_percent", 25, 1000, 25),
                        ("health_per_player_percent", 0, 100, 5),
                        ("damage_per_player_percent", 0, 100, 5),
                    ):
                        value = raw_modifiers.get(key)
                        if value in (None, ""):
                            continue
                        value = int(value)
                        if value < low or value > high or value % step:
                            raise PanelError(f"{key} must be {low}–{high} in steps of {step}")
                        modifiers[key] = value
                    new_campaign = tomlkit.inline_table()
                    old_campaign = server.get("campaign", {})
                    for key, value in old_campaign.items():
                        if key not in {"mission", "insertion", "difficulty", "custom_characters", "modifiers"}:
                            new_campaign[key] = value
                    new_campaign["mission"] = mission
                    new_campaign["insertion"] = insertion
                    new_campaign["difficulty"] = difficulty
                    new_campaign["custom_characters"] = custom_characters
                    if modifiers:
                        new_campaign["modifiers"] = modifiers
                    server["campaign"] = new_campaign
                    for key in ("playlist", "map", "game", "variant", "base_map", "odst"):
                        server.pop(key, None)
                else:
                    if not 1 <= max_players <= 127:
                        raise PanelError("Dedicated multiplayer servers support 1–127 players; the server uses the 128th slot")
                if panel_server["mode"] != "campaign" and playlist:
                    safe_playlist_path(Path(playlist).name)
                    if Path(playlist).name != playlist:
                        raise PanelError("Playlist must be selected from the playlists folder")
                    # Reclaimer resolves playlist paths from SERVER_DIR, so
                    # persist the directory prefix even though the UI select
                    # contains only the basename.
                    server["playlist"] = f"playlists/{playlist}"
                    server.pop("map", None)
                    server.pop("game", None)
                    server.pop("variant", None)
                if panel_server["mode"] != "campaign":
                    server.pop("campaign", None)
                server["name"] = name
                server["max_players"] = max_players
                write_doc(doc)
                message = "Saved."
                try:
                    reply = SourceRcon(panel_server["rcon_address"], panel_server["rcon_port"],
                                       panel_server["rcon_password"]).command("reload")
                    message += " Requested a live configuration reload."
                    if reply:
                        message += f" Reclaimer: {reply}"
                except Exception as exc:
                    message += (f" Could not confirm live reload: {exc}. Check that Reclaimer is 0.9.18+ "
                                "and `dedicated check` passes; older versions need a restart.")
                self.send_json({"ok": True, "message": message})
            elif parsed.path == "/api/mod-upload/start":
                folder = str(data.get("folder", "")).strip()
                file_count = int(data.get("file_count", 0))
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,99}", folder):
                    raise PanelError("Folder name must use letters, numbers, spaces, dots, underscores or hyphens")
                if folder in (".", "..") or file_count < 1 or file_count > 10000:
                    raise PanelError("Choose a mod folder containing between 1 and 10,000 files")
                mods = SERVER_DIR / "content" / "Mods"
                destination = mods / folder
                if destination.exists():
                    raise PanelError(f"A mod folder named '{folder}' already exists in content/Mods")
                mods.mkdir(parents=True, exist_ok=True)
                staging_root = SERVER_DIR / ".admin-mod-uploads"
                staging_root.mkdir(parents=True, exist_ok=True)
                token = uuid.uuid4().hex
                staging = staging_root / token
                staging.mkdir(mode=0o700)
                with MOD_UPLOADS_LOCK:
                    MOD_UPLOADS[token] = {"session": self.session()[0], "folder": folder,
                                          "file_count": file_count, "staging": staging,
                                          "files": set(), "total": 0}
                self.send_json({"ok": True, "upload": token})
            elif parsed.path == "/api/mod-upload/finish":
                token = str(data.get("upload", ""))
                upload = self.get_mod_upload(token, self.session())
                if len(upload["files"]) != upload["file_count"]:
                    raise PanelError("Some files did not finish uploading; try the folder upload again")
                info_path = upload["staging"] / "ModInfo.json"
                if not info_path.is_file():
                    raise PanelError("The selected folder must contain ModInfo.json at its top level")
                try:
                    json.loads(info_path.read_text(encoding="utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise PanelError("ModInfo.json is not valid JSON")
                destination = SERVER_DIR / "content" / "Mods" / upload["folder"]
                if destination.exists():
                    raise PanelError(f"A mod folder named '{upload['folder']}' appeared during upload")
                destination.parent.mkdir(parents=True, exist_ok=True)
                # Some unprivileged LXC / Docker bind-mount combinations deny
                # directory renames between sibling paths despite both being
                # writable. Copy into the final folder, then remove staging.
                try:
                    shutil.copytree(upload["staging"], destination)
                except Exception:
                    shutil.rmtree(destination, ignore_errors=True)
                    raise
                shutil.rmtree(upload["staging"])
                with MOD_UPLOADS_LOCK:
                    MOD_UPLOADS.pop(token, None)
                self.send_json({"ok": True, "message": f"Uploaded {upload['folder']} to content/Mods. Point a playlist or server at its map, then restart the server to load the mod."})
            elif parsed.path == "/api/mod-upload/abort":
                token = str(data.get("upload", ""))
                upload = self.get_mod_upload(token, self.session())
                shutil.rmtree(upload["staging"], ignore_errors=True)
                with MOD_UPLOADS_LOCK:
                    MOD_UPLOADS.pop(token, None)
                self.send_json({"ok": True})
            elif parsed.path == "/api/playlist":
                name = uploaded_playlist_name(str(data.get("file", "")))
                obj = validate_playlist(data.get("data"))
                path = safe_playlist_path(name)
                validate_with_reclaimer(name, obj)
                if path.exists():
                    write_backup(path)
                path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                self.send_json({"ok": True, "message": "Reclaimer validated the playlist. It was saved; running servers will use it from the next vote."})
            elif parsed.path == "/api/playlist-upload":
                name = uploaded_playlist_name(str(data.get("file", "")))
                obj = validate_playlist(data.get("data"))
                path = safe_playlist_path(name)
                if path.exists() and not bool(data.get("replace", False)):
                    raise PanelError("A playlist with that name already exists; confirm replacement to overwrite it")
                validate_with_reclaimer(name, obj)
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.exists():
                    write_backup(path)
                temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
                try:
                    temp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                    os.replace(temp, path)
                finally:
                    temp.unlink(missing_ok=True)
                self.send_json({"ok": True, "file": name, "message": f"Reclaimer validated and saved {name}. Assign it to a server in Server settings."})
            elif parsed.path == "/api/playlist-delete":
                name = str(data.get("file", ""))
                self.send_json({"ok": True, "message": delete_playlist(name), "file": name})
            elif parsed.path == "/api/workshop":
                ids = data.get("items")
                if not isinstance(ids, list) or len(ids) > 64:
                    raise PanelError("Workshop IDs must be a list of up to 64 items")
                clean = []
                for value in ids:
                    item = str(value).strip()
                    if not item.isdigit() or len(item) < 5 or len(item) > 20:
                        raise PanelError(f"Invalid Workshop item ID: {item}")
                    if item not in clean:
                        clean.append(item)
                doc, _ = config_servers()
                if "workshop" not in doc:
                    doc["workshop"] = tomlkit.table()
                doc["workshop"]["items"] = [int(i) for i in clean]
                write_doc(doc)
                self.send_json({"ok": True, "message": "Saved Workshop IDs. Use Download from Workshop to install an item from its URL."})
            elif parsed.path == "/api/schedule":
                port = int(data.get("port", 0))
                enabled = bool(data.get("enabled", True))
                _, configured = config_servers()
                server = next((item for item in configured if item["port"] == port), None)
                if server is None or server["mode"] == "campaign":
                    raise PanelError("Choose a configured multiplayer server")
                with SCHEDULES_LOCK:
                    schedules = read_schedules()
                    if not enabled:
                        schedules.pop(str(port), None)
                        write_schedules(schedules)
                        self.send_json({"ok": True, "message": "Playlist schedule disabled. The server keeps its current playlist."})
                        return
                    playlists = data.get("playlists")
                    if not isinstance(playlists, list):
                        raise PanelError("Select at least two playlists")
                    clean = []
                    for value in playlists:
                        filename = str(value).strip()
                        safe_playlist_path(filename)
                        if filename not in clean:
                            clean.append(filename)
                    if len(clean) < 2 or len(clean) > 20:
                        raise PanelError("Select between 2 and 20 playlists for rotation")
                    interval = int(data.get("interval_minutes", 40))
                    if not 5 <= interval <= 10080:
                        raise PanelError("Switch interval must be 5 minutes to 7 days")
                    announce_minutes = int(data.get("announce_minutes", 5))
                    if not 0 <= announce_minutes <= 60:
                        raise PanelError("Announcement lead time must be 0 to 60 minutes")
                    current = server["playlist"]
                    current_index = clean.index(current) if current in clean else 0
                    message = "Playlist schedule saved. Each change takes effect at the next vote; a game already in progress is not interrupted."
                    if current not in clean:
                        try:
                            reply = set_scheduled_playlist(port, clean[0])
                            if reply:
                                message += f" Reclaimer: {reply}"
                        except Exception as exc:
                            message += f" The first playlist was saved to dedicated.toml, but live reload could not be confirmed: {exc}"
                    schedules[str(port)] = {
                        "enabled": True,
                        "interval_minutes": interval,
                        "announce_minutes": announce_minutes,
                        "playlists": clean,
                        "current_index": current_index,
                        "next_switch_at": int(time.time() + interval * 60),
                        "last_switch_at": int(time.time()),
                        "last_error": "",
                        "announcement_sent_for": 0,
                        "announcement_error": "",
                    }
                    write_schedules(schedules)
                self.send_json({"ok": True, "message": message})
            elif parsed.path == "/api/say":
                port = int(data.get("port", 0))
                reply = send_server_message(port, str(data.get("message", "")))
                self.send_json({"ok": True, "reply": reply or "Message sent to server chat."})
            elif parsed.path == "/api/workshop-download/start":
                item_id = workshop_item_id(str(data.get("url", "")))
                username = str(data.get("username", "")).strip()
                if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", username):
                    raise PanelError("Enter the Steam account name you signed in to SteamCMD with (no password)")
                if not STEAMCMD_PATH.is_file() or not os.access(STEAMCMD_PATH, os.X_OK):
                    raise PanelError("SteamCMD is missing. Rebuild the admin UI with the updated project files.")
                if shutil.disk_usage(SERVER_DIR).free < 1_073_741_824:
                    raise PanelError("Free at least 1 GiB on the server volume before downloading a Workshop item")
                if WORKSHOP_DOWNLOAD_LOCK.locked():
                    raise PanelError("Another Workshop download is already running. Wait for it to finish.")
                job_id = uuid.uuid4().hex
                session_token = self.session()[0]
                with WORKSHOP_DOWNLOADS_LOCK:
                    WORKSHOP_DOWNLOADS[job_id] = {"session": session_token, "status": "queued",
                                                  "message": "Waiting for SteamCMD…", "item_id": item_id}
                    if len(WORKSHOP_DOWNLOADS) > 64:
                        for old_id, old in list(WORKSHOP_DOWNLOADS.items()):
                            if old.get("status") in {"complete", "failed"}:
                                WORKSHOP_DOWNLOADS.pop(old_id, None)
                                if len(WORKSHOP_DOWNLOADS) <= 48:
                                    break
                threading.Thread(target=perform_workshop_download,
                                 args=(job_id, item_id, username, session_token), daemon=True).start()
                self.send_json({"ok": True, "job": job_id, "item_id": item_id}, 202)
            elif parsed.path == "/api/ban":
                port = int(data.get("port", 0))
                server = require_port(port)
                target = valid_target(str(data.get("target", "")))
                duration = str(data.get("duration", "")).strip()
                if duration and not re.fullmatch(r"\d+[mhdw]", duration, re.I):
                    raise PanelError("Ban duration must look like 30m, 12h, 7d, or 2w")
                reason = str(data.get("reason", "Admin panel ban")).strip()[:120]
                command = " ".join(["ban", target, *([duration] if duration else []), json.dumps(reason, ensure_ascii=False)])
                reply = SourceRcon(server["rcon_address"], server["rcon_port"], server["rcon_password"]).command(command)
                self.send_json({"ok": True, "reply": reply})
            elif parsed.path == "/api/unban":
                port = int(data.get("port", 0))
                server = require_port(port)
                target = valid_target(str(data.get("target", "")))
                reply = SourceRcon(server["rcon_address"], server["rcon_port"], server["rcon_password"]).command("unban " + target)
                self.send_json({"ok": True, "reply": reply})
            else:
                self.send_json({"error": "Not found"}, 404)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 400)

    def session_token(self):
        for pair in self.headers.get("Cookie", "").split(";"):
            key, sep, value = pair.strip().partition("=")
            if sep and key == "admin_session":
                return value
        return ""


def main():
    if not ADMIN_PASSWORD:
        raise SystemExit("Set ADMIN_PASSWORD before starting Reclaimer Control")
    server = ThreadingHTTPServer((BIND_HOST, PORT), Handler)
    print(f"Project Reclaimer Admin UI listening on {BIND_HOST}:{PORT}; server folder {SERVER_DIR}")
    threading.Thread(target=playlist_schedule_loop, name="playlist-scheduler", daemon=True).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
