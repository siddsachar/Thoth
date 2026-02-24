import os
import json
import pathlib

# Store data in %APPDATA%/Thoth (writable even when app is in Program Files)
DATA_DIR = pathlib.Path(os.environ.get("THOTH_DATA_DIR", pathlib.Path.home() / ".thoth"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

KEYS_PATH = DATA_DIR / "api_keys.json"

# All API keys the app can use – label shown in UI → env-var name
API_KEY_DEFINITIONS = {
    "Tavily": "TAVILY_API_KEY",
}


def _load_keys() -> dict[str, str]:
    """Load saved keys from disk. Returns {env_var: value}."""
    if KEYS_PATH.exists():
        try:
            with open(KEYS_PATH, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_keys(keys: dict[str, str]):
    """Persist keys to disk."""
    with open(KEYS_PATH, "w") as f:
        json.dump(keys, f, indent=2)


def get_key(env_var: str) -> str:
    """Return a key value (empty string if not set)."""
    return _load_keys().get(env_var, "")


def set_key(env_var: str, value: str):
    """Save a single key and push it into the environment."""
    keys = _load_keys()
    keys[env_var] = value
    _save_keys(keys)
    os.environ[env_var] = value


def apply_keys():
    """Load all saved keys into environment variables."""
    for env_var, value in _load_keys().items():
        if value:
            os.environ[env_var] = value