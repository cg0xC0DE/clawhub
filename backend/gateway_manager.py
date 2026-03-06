import subprocess
import threading
import time
import json
import os
import re
import socket
from datetime import datetime
from pathlib import Path

ANSI_ESCAPE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\[\?[0-9;]*[a-zA-Z]')
GATEWAYS_FILE = Path(__file__).parent.parent / "gateways.json"
HUB_DIR = Path(__file__).parent.parent
GATEWAYS_DIR = HUB_DIR / "gateways"
TEMPLATE_FILE = GATEWAYS_DIR / "_template" / "openclaw.json"
MAX_LOG_LINES = 3000

_node_cmd_cache: dict = {}


def _resolve_node_openclaw() -> tuple[str, str]:
    """Parse ~/.openclaw/gateway.cmd to extract node exe and openclaw dist/index.js paths."""
    if _node_cmd_cache:
        return _node_cmd_cache.get("node", ""), _node_cmd_cache.get("js", "")

    gateway_cmd = Path(os.environ.get("USERPROFILE", "~")) / ".openclaw" / "gateway.cmd"
    if not gateway_cmd.exists():
        return "", ""

    try:
        content = gateway_cmd.read_text(encoding="utf-8", errors="ignore")
        for line in content.splitlines():
            line = line.strip()
            # Match: "C:\...\node.exe" "C:\...\openclaw\dist\index.js" gateway --port ...
            if "node.exe" in line and "dist\\index.js" in line:
                # Strip leading quote, split on space between quoted args
                parts = []
                buf = ""
                in_q = False
                for ch in line:
                    if ch == '"':
                        in_q = not in_q
                    elif ch == ' ' and not in_q:
                        if buf:
                            parts.append(buf)
                            buf = ""
                        continue
                    else:
                        buf += ch
                if buf:
                    parts.append(buf)
                if len(parts) >= 2:
                    _node_cmd_cache["node"] = parts[0]
                    _node_cmd_cache["js"] = parts[1]
                    return parts[0], parts[1]
    except Exception:
        pass
    return "", ""


def provision_gateway(gw_id: str, name: str, emoji: str, port: int,
                      primary_model: str = "") -> Path:
    """Create gateways/<gw_id>/ from template, return config path."""
    import secrets
    gw_dir = GATEWAYS_DIR / gw_id
    ws_dir = gw_dir / "state" / "workspace"
    ws_dir.mkdir(parents=True, exist_ok=True)

    cfg_path = gw_dir / "openclaw.json"
    if cfg_path.exists():
        return cfg_path

    if not TEMPLATE_FILE.exists():
        raise FileNotFoundError(f"Template not found: {TEMPLATE_FILE}")

    raw = TEMPLATE_FILE.read_text(encoding="utf-8")
    if raw and ord(raw[0]) == 0xFEFF:
        raw = raw[1:]
    cfg = json.loads(raw)

    # Fill dynamic fields
    cfg["gateway"]["port"] = port
    # name/emoji are stored in gateways.json only (openclaw rejects unknown keys)
    cfg.pop("hub", None)
    cfg["gateway"]["auth"]["token"] = f"{gw_id}-gateway-{secrets.token_hex(8)}"
    cfg["agents"]["defaults"]["workspace"] = str(ws_dir)
    cfg["channels"]["telegram"]["botToken"] = f"{gw_id.upper()}_BOT_TOKEN_HERE"

    if primary_model:
        cfg["agents"]["defaults"]["model"]["primary"] = primary_model

    cfg_path.write_text(
        json.dumps(cfg, indent=4, ensure_ascii=False), encoding="utf-8"
    )

    # Copy auth-profiles.json from default openclaw install (shared API keys)
    _copy_auth_profiles(gw_dir)

    # Copy workspace scripts from template
    _provision_workspace_scripts(gw_id, name, ws_dir)

    return cfg_path


def _copy_auth_profiles(gw_dir: Path):
    """Copy ~/.openclaw auth-profiles.json into the gateway's agent dir, keeping only keys."""
    src = Path(os.environ.get("USERPROFILE", "~")) / ".openclaw" / "agents" / "main" / "agent" / "auth-profiles.json"
    if not src.exists():
        return
    dst_dir = gw_dir / "state" / "agents" / "main" / "agent"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / "auth-profiles.json"
    if dst.exists():
        return
    try:
        raw = src.read_text(encoding="utf-8")
        if raw and ord(raw[0]) == 0xFEFF:
            raw = raw[1:]
        data = json.loads(raw)
        # Keep only version + profiles (strip usageStats, lastGood, etc.)
        clean = {"version": data.get("version", 1), "profiles": data.get("profiles", {})}
        dst.write_text(json.dumps(clean, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def cleanup_gateway_files(gw_id: str) -> list[str]:
    """Remove all disk artifacts for a deleted gateway.

    Returns a list of what was cleaned up (for logging / API response).
    """
    import shutil
    cleaned = []

    # 1. Remove gateway directory
    gw_dir = GATEWAYS_DIR / gw_id
    if gw_dir.exists() and gw_dir.is_dir():
        try:
            shutil.rmtree(gw_dir)
            cleaned.append(f"gateways/{gw_id}/")
        except Exception as e:
            print(f"[cleanup] Failed to remove {gw_dir}: {e}")

    if cleaned:
        print(f"[cleanup] Cleaned up for {gw_id}: {cleaned}")
    return cleaned


def _provision_workspace_scripts(gw_id: str, display_name: str, ws_dir: Path):
    """Copy workspace scripts from _template/workspace into the new gateway workspace.

    Replaces __AGENT_ID__ with the gateway id and __AGENT_NAME__ with the display name.
    Skips files that already exist (don't overwrite user edits).
    """
    template_ws = GATEWAYS_DIR / "_template" / "workspace"
    if not template_ws.exists():
        return

    for src_file in template_ws.iterdir():
        if not src_file.is_file():
            continue
        dst_file = ws_dir / src_file.name
        if dst_file.exists():
            continue
        try:
            content = src_file.read_text(encoding="utf-8")
            content = content.replace("__AGENT_ID__", gw_id)
            content = content.replace("__AGENT_NAME__", display_name)
            dst_file.write_text(content, encoding="utf-8")
        except Exception:
            pass


def _port_listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _load_gateways_json() -> list:
    if GATEWAYS_FILE.exists():
        raw = GATEWAYS_FILE.read_text(encoding="utf-8")
        if raw and ord(raw[0]) == 0xFEFF:
            raw = raw[1:]
        return json.loads(raw)
    return []


def _save_gateways_json(data: list):
    GATEWAYS_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _read_openclaw_config(config_path: Path) -> dict:
    try:
        raw = config_path.read_text(encoding="utf-8")
        if raw and ord(raw[0]) == 0xFEFF:
            raw = raw[1:]
        return json.loads(raw)
    except Exception:
        return {}


class GatewayProcess:
    def __init__(self, gw_def: dict):
        self.id: str = gw_def["id"]
        self.name: str = gw_def.get("name", self.id)
        self.emoji: str = gw_def.get("emoji", "🪭")
        self.port: int = int(gw_def.get("port", 0))
        self.config_file: str = gw_def.get("config_file", "")
        self.workspace_dir: str = gw_def.get("workspace_dir", "")
        self.state_dir: str = gw_def.get("state_dir", "")
        self.editable_files: list = gw_def.get("editable_files", [])
        self.role: str = gw_def.get("role", "")  # "herald" for non-player bots

        self.process: subprocess.Popen = None
        self.log_buffer: list = []   # list of (timestamp_float, str)
        self.log_pruned_count: int = 0
        self.status: str = "stopped"
        self.pid: int = None
        self.started_at: str = None
        self.exit_code: int = None
        self.restart_count: int = 0
        self._lock = threading.Lock()

    def _abs(self, rel: str) -> Path:
        return HUB_DIR / rel

    def _get_token(self) -> str:
        cfg_path = self._abs(self.config_file)
        cfg = _read_openclaw_config(cfg_path)
        return cfg.get("channels", {}).get("telegram", {}).get("botToken", "")

    def _build_command(self) -> str | None:
        cfg_path = self._abs(self.config_file)
        state_dir = self._abs(self.state_dir)

        node_exe, openclaw_js = _resolve_node_openclaw()
        if not node_exe or not openclaw_js:
            return None

        env_block = (
            f'set "OPENCLAW_STATE_DIR={state_dir}" && '
            f'set "OPENCLAW_CONFIG_PATH={cfg_path}" && '
            f'set "HTTP_PROXY=http://127.0.0.1:10020" && '
            f'set "HTTPS_PROXY=http://127.0.0.1:10020" && '
            f'set "ALL_PROXY=socks5://127.0.0.1:10020" && '
        )
        return (
            f'cmd.exe /c {env_block}'
            f'"{node_exe}" "{openclaw_js}" gateway --port {self.port} --verbose'
        )

    def start(self) -> tuple[bool, str]:
        if self.process and self.process.poll() is None:
            return False, "already running"

        token = self._get_token()
        if not token or "_TOKEN_HERE" in token:
            return False, "Bot Token not configured"

        cfg_path = self._abs(self.config_file)
        if not cfg_path.exists():
            return False, f"config not found: {self.config_file}"

        state_dir = self._abs(self.state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        ws_dir = self._abs(self.workspace_dir)
        ws_dir.mkdir(parents=True, exist_ok=True)

        cmd = self._build_command()
        if not cmd:
            return False, "gateway.cmd not found (run openclaw gateway install first)"

        try:
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            self.process = subprocess.Popen(
                cmd,
                cwd=str(HUB_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                shell=False,
                text=False,
                bufsize=0,
                env=env,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
            self.status = "starting"
            self.pid = self.process.pid
            self.started_at = datetime.now().isoformat()
            self.exit_code = None
            threading.Thread(target=self._read_output, daemon=True).start()
            threading.Thread(target=self._watch_status, daemon=True).start()
            return True, "started"
        except Exception as e:
            self.status = "error"
            return False, str(e)

    def _watch_status(self):
        """Poll port to confirm running, update status."""
        for _ in range(20):
            time.sleep(1)
            if _port_listening(self.port):
                self.status = "running"
                return
            if self.process and self.process.poll() is not None:
                break
        if self.process and self.process.poll() is None:
            self.status = "running"

    def _read_output(self):
        try:
            fd = self.process.stdout.fileno()
            buf = b""
            while True:
                try:
                    chunk = os.read(fd, 8192)
                except OSError:
                    break
                if not chunk:
                    if buf:
                        self._emit_line(buf)
                    break
                buf += chunk
                while True:
                    idx_n = buf.find(b"\n")
                    idx_r = buf.find(b"\r")
                    if idx_n == -1 and idx_r == -1:
                        break
                    if idx_n == -1:
                        idx = idx_r
                    elif idx_r == -1:
                        idx = idx_n
                    else:
                        idx = min(idx_n, idx_r)
                    line = buf[:idx]
                    if idx == idx_r and idx + 1 < len(buf) and buf[idx + 1 : idx + 2] == b"\n":
                        buf = buf[idx + 2 :]
                    else:
                        buf = buf[idx + 1 :]
                    if line:
                        self._emit_line(line)
        except Exception:
            pass
        finally:
            if self.process:
                self.exit_code = self.process.poll()
            self.status = "stopped"
            self.pid = None

    def _emit_line(self, raw: bytes):
        for enc in ("utf-8", "gbk", "cp936", "latin-1"):
            try:
                line = raw.decode(enc)
                break
            except (UnicodeDecodeError, LookupError):
                continue
        else:
            line = raw.decode("latin-1")
        line = ANSI_ESCAPE.sub("", line)
        now = time.time()
        with self._lock:
            self.log_buffer.append((now, line))
            if len(self.log_buffer) > MAX_LOG_LINES:
                self.log_buffer.pop(0)
                self.log_pruned_count += 1

    def stop(self) -> tuple[bool, str]:
        # Try port-based kill first (more reliable for openclaw)
        killed = self._kill_by_port()
        if killed:
            self.status = "stopped"
            self.pid = None
            self._cleanup_openclaw_lock()
            return True, "stopped via port"

        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self.status = "stopped"
            self.pid = None
            self._cleanup_openclaw_lock()
            return True, "stopped"

        # Even if not running, clean stale lock (previous crash may have left it)
        self._cleanup_openclaw_lock()
        return False, "not running"

    def _cleanup_openclaw_lock(self):
        """Remove stale openclaw gateway lock file from %TEMP%/openclaw/."""
        try:
            cfg_abs = str(self._abs(self.config_file))
            lock_dir = Path(os.environ.get("TEMP", os.environ.get("TMP", ""))) / "openclaw"
            if not lock_dir.exists():
                return
            for lock_file in lock_dir.glob("gateway.*.lock"):
                try:
                    content = lock_file.read_text(encoding="utf-8").strip()
                    if not content:
                        continue
                    data = json.loads(content)
                    if data.get("configPath", "").replace("/", "\\") == cfg_abs.replace("/", "\\"):
                        lock_file.unlink(missing_ok=True)
                        print(f"[gateway] 清理锁文件: {lock_file.name} (was pid {data.get('pid')})")
                except Exception:
                    pass
        except Exception as e:
            print(f"[gateway] 锁文件清理失败: {e}")

    def _kill_by_port(self) -> bool:
        if not self.port:
            return False
        my_pid = os.getpid()
        try:
            import psutil
            killed = False
            for conn in psutil.net_connections(kind="tcp"):
                if conn.laddr.port == self.port and conn.status == "LISTEN":
                    if conn.pid == my_pid:
                        print(f"[gateway] _kill_by_port: skipping own process (pid {my_pid}) on port {self.port}")
                        continue
                    try:
                        p = psutil.Process(conn.pid)
                        p.terminate()
                        p.wait(timeout=3)
                        killed = True
                    except Exception:
                        try:
                            p.kill()
                            killed = True
                        except Exception:
                            pass
            return killed
        except ImportError:
            # fallback: netstat + taskkill on Windows
            try:
                out = subprocess.check_output(
                    f'netstat -ano | findstr ":{self.port} "',
                    shell=True, text=True, stderr=subprocess.DEVNULL
                )
                pids = set()
                for line in out.strip().splitlines():
                    parts = line.split()
                    if parts and parts[-1].isdigit():
                        pids.add(parts[-1])
                for pid in pids:
                    subprocess.run(f"taskkill /F /PID {pid}", shell=True,
                                   capture_output=True)
                return bool(pids)
            except Exception:
                return False

    def restart(self) -> tuple[bool, str]:
        self.stop()
        time.sleep(1)
        self.restart_count += 1
        return self.start()

    def get_logs(self, offset: int = 0) -> tuple[list, int, int]:
        with self._lock:
            buf = list(self.log_buffer)
            pruned = self.log_pruned_count
        total = pruned + len(buf)
        idx = max(0, offset - pruned)
        lines = [entry[1] for entry in buf[idx:]]
        return lines, total, pruned

    def check_status(self) -> str:
        if self.process and self.process.poll() is None:
            if _port_listening(self.port):
                self.status = "running"
            else:
                self.status = "starting"
        elif _port_listening(self.port):
            self.status = "running"
        else:
            self.status = "stopped"
            if self.process:
                self.exit_code = self.process.poll()
                self.pid = None
        return self.status

    def to_dict(self) -> dict:
        status = self.check_status()
        return {
            "id": self.id,
            "name": self.name,
            "emoji": self.emoji,
            "port": self.port,
            "config_file": self.config_file,
            "workspace_dir": self.workspace_dir,
            "state_dir": self.state_dir,
            "editable_files": self.editable_files,
            "status": status,
            "pid": self.pid,
            "started_at": self.started_at,
            "exit_code": self.exit_code,
            "restart_count": self.restart_count,
            "role": self.role,
            "token_configured": bool(self._get_token()) and "_TOKEN_HERE" not in self._get_token(),
        }

    def to_persist(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "emoji": self.emoji,
            "port": self.port,
            "config_file": self.config_file,
            "workspace_dir": self.workspace_dir,
            "state_dir": self.state_dir,
            "editable_files": self.editable_files,
            "role": self.role,
        }


class GatewayManager:
    def __init__(self):
        self.gateways: dict[str, GatewayProcess] = {}
        self._load()

    def _load(self):
        data = _load_gateways_json()
        for gw_def in data:
            gw = GatewayProcess(gw_def)
            self.gateways[gw.id] = gw

    def reload(self):
        """Hot-reload gateways.json metadata without affecting running processes."""
        data = _load_gateways_json()
        new_ids = {d["id"] for d in data}
        # Update existing + add new
        for gw_def in data:
            gw_id = gw_def["id"]
            if gw_id in self.gateways:
                existing = self.gateways[gw_id]
                # Update metadata fields only (preserve runtime state)
                for key in ("name", "emoji", "port", "config_file",
                            "workspace_dir", "state_dir", "editable_files"):
                    if key in gw_def:
                        setattr(existing, key, gw_def[key])
            else:
                self.gateways[gw_id] = GatewayProcess(gw_def)
        # Remove gateways no longer in json (stop them first)
        for gw_id in list(self.gateways.keys()):
            if gw_id not in new_ids:
                self.gateways[gw_id].stop()
                del self.gateways[gw_id]

    def _save(self):
        data = [gw.to_persist() for gw in self.gateways.values()]
        _save_gateways_json(data)

    def list_all(self) -> list[GatewayProcess]:
        return list(self.gateways.values())

    def get(self, gw_id: str) -> GatewayProcess | None:
        return self.gateways.get(gw_id)

    def add(self, gw_def: dict) -> GatewayProcess:
        gw = GatewayProcess(gw_def)
        self.gateways[gw.id] = gw
        self._save()
        return gw

    def remove(self, gw_id: str) -> bool:
        gw = self.gateways.get(gw_id)
        if not gw:
            return False
        gw.stop()
        del self.gateways[gw_id]
        self._save()
        return True

    def update(self, gw_id: str, fields: dict) -> GatewayProcess | None:
        gw = self.gateways.get(gw_id)
        if not gw:
            return None
        for k, v in fields.items():
            if hasattr(gw, k):
                setattr(gw, k, v)
        self._save()
        return gw
