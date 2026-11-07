"""DroneOps core: config storage, SSH/SFTP connection, resumable downloads."""
import json
import os
import stat
import threading
from pathlib import Path

import paramiko

APP_DIR = Path.home() / ".droneops"
CFG_FILE = APP_DIR / "config.json"

DEFAULT_CFG = {
    "profiles": [],          # {name, host, port, user, key}
    "presets": [],           # {name, profile, path, is_dir}
    "download_dir": str(Path.home() / "DroneOps"),
    "coord_order": "lat,lon",
    "last_profile": "",
}


class Cancelled(Exception):
    pass


# ---------------------------------------------------------------- config
def load_cfg():
    cfg = json.loads(json.dumps(DEFAULT_CFG))
    try:
        cfg.update(json.loads(CFG_FILE.read_text()))
    except FileNotFoundError:
        pass
    except Exception as e:  # corrupt file: keep a copy, start fresh
        try:
            CFG_FILE.replace(CFG_FILE.with_suffix(".corrupt"))
        except OSError:
            pass
        print("config unreadable, starting fresh:", e)
    return cfg


def save_cfg(cfg):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CFG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    tmp.replace(CFG_FILE)


# ------------------------------------------------------------ connection
class Connection:
    """One SSH connection. Browsing uses a shared SFTP channel (locked);
    each transfer opens its own SFTP channel so transfers run in parallel."""

    def __init__(self, profile, password=None):
        self.profile = profile
        self.password = password
        self.client = None
        self.sftp = None
        self.lock = threading.RLock()

    def connect(self):
        p = self.profile
        c = paramiko.SSHClient()
        c.load_system_host_keys()
        # MVP: trust new hosts automatically (drones on a private network).
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kw = dict(
            hostname=p["host"],
            port=int(p.get("port") or 22),
            username=p["user"],
            timeout=8,
            banner_timeout=8,
            auth_timeout=8,
            allow_agent=True,
            look_for_keys=not p.get("key"),
        )
        if p.get("key"):
            kw["key_filename"] = os.path.expanduser(p["key"])
        if self.password:
            kw["password"] = self.password
        c.connect(**kw)
        c.get_transport().set_keepalive(10)
        self.client = c
        self.sftp = c.open_sftp()

    def alive(self):
        t = self.client.get_transport() if self.client else None
        return bool(t and t.is_active())

    def ensure(self):
        """Reconnect if the link dropped (e.g. Wi-Fi blip)."""
        with self.lock:
            if not self.alive():
                self.close()
                self.connect()

    def close(self):
        try:
            if self.client:
                self.client.close()
        except Exception:
            pass
        self.client = None
        self.sftp = None

    def listdir(self, path):
        with self.lock:
            self.ensure()
            try:
                return self.sftp.listdir_attr(path)
            except (EOFError, OSError, paramiko.SSHException) as e:
                if isinstance(e, IOError) and getattr(e, "errno", None) in (2, 13):
                    raise  # missing dir / permission denied: not a link problem
                self.close()
                self.connect()
                return self.sftp.listdir_attr(path)

    def new_sftp(self):
        with self.lock:
            self.ensure()
            return self.client.open_sftp()

    def read_head(self, path, max_bytes=65536):
        s = self.new_sftp()
        try:
            with s.open(path, "rb") as f:
                return f.read(max_bytes)
        finally:
            s.close()

    def exec(self, cmd, timeout=10):
        with self.lock:
            self.ensure()
            _, out, err = self.client.exec_command(cmd, timeout=timeout)
        return out.read().decode(errors="replace"), err.read().decode(errors="replace")


def is_dir(attr):
    return stat.S_ISDIR(attr.st_mode or 0)


# ------------------------------------------------------------- transfers
def local_path_for(download_dir, profile_name, remote_path):
    """~/DroneOps/<drone>/<remote path without leading slash>"""
    rel = remote_path.lstrip("/")
    return Path(download_dir) / profile_name / rel


def unique_target(path, size):
    """Duplicate handling: reuse the first name that is absent or already complete."""
    path = Path(path)
    cand, i = path, 0
    while cand.exists() and cand.stat().st_size != size:
        i += 1
        cand = path.with_name(f"{path.stem}_{i}{path.suffix}")
    return cand


def download_file(conn, remote, local, on_progress, cancel):
    """Download with resume (.part file) and size verification.
    Never touches the remote file. Returns the final local path."""
    sftp = conn.new_sftp()
    try:
        size = sftp.stat(remote).st_size
        target = unique_target(local, size)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():  # already downloaded
            on_progress(1.0)
            return str(target)
        part = target.with_name(target.name + ".part")
        offset = part.stat().st_size if part.exists() else 0
        if offset > size:
            offset = 0
        done = offset
        with sftp.open(remote, "rb") as rf, open(part, "ab" if offset else "wb") as lf:
            if offset:
                rf.seek(offset)
            while True:
                if cancel.is_set():
                    raise Cancelled()
                chunk = rf.read(256 * 1024)
                if not chunk:
                    break
                lf.write(chunk)
                done += len(chunk)
                on_progress(done / size if size else 1.0)
        if part.stat().st_size != size:
            raise IOError(f"incomplete: got {part.stat().st_size} of {size} bytes (retry will resume)")
        part.replace(target)
        on_progress(1.0)
        return str(target)
    finally:
        try:
            sftp.close()
        except Exception:
            pass


def walk_remote(conn, root):
    """Yield (remote_path, size) for every file under root."""
    sftp = conn.new_sftp()
    try:
        stack = [root]
        while stack:
            d = stack.pop()
            for a in sftp.listdir_attr(d):
                p = d.rstrip("/") + "/" + a.filename
                if is_dir(a):
                    stack.append(p)
                else:
                    yield p, a.st_size
    finally:
        sftp.close()
