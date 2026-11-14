"""Where the server keeps its state (maps, corpus index, PhilArchive copy, search log, models)."""
import os

_SYSTEM_STATE = "/var/lib/doi-mcp"


def state_dir() -> str:
    """STATE_DIRECTORY if set (systemd sets it); else /var/lib/doi-mcp if it exists (server
    installs); else the per-user state folder, so a desktop install works without root."""
    env = os.environ.get("STATE_DIRECTORY", "").split(":")[0]
    if env:
        return env
    if os.path.isdir(_SYSTEM_STATE):
        return _SYSTEM_STATE
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "paper-doi-mcp")
