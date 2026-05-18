"""Local storage for Koan identities — one per gateway.

Each gateway's Koan identity is stored as `gateways/{gw_id}/koan_identity.json`.
Private keys are kept locally and MUST be in .gitignore.
"""

import json
from pathlib import Path
from typing import Optional


def _identity_path(hub_dir: Path, gw_id: str) -> Path:
    return hub_dir / "gateways" / gw_id / "koan_identity.json"


def load_identity(hub_dir: Path, gw_id: str) -> Optional[dict]:
    """Load a gateway's Koan identity, or None if not registered."""
    p = _identity_path(hub_dir, gw_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_identity(hub_dir: Path, gw_id: str, identity: dict) -> None:
    """Persist a gateway's Koan identity to disk."""
    p = _identity_path(hub_dir, gw_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(identity, indent=2, ensure_ascii=False),
                 encoding="utf-8")


def delete_identity(hub_dir: Path, gw_id: str) -> bool:
    """Delete a gateway's local Koan identity. Returns True if deleted."""
    p = _identity_path(hub_dir, gw_id)
    if p.exists():
        p.unlink()
        return True
    return False


def list_identities(hub_dir: Path) -> list[dict]:
    """List all gateways that have a Koan identity.

    Returns a list of dicts with gw_id + identity summary (no private keys).
    """
    results = []
    gw_dir = hub_dir / "gateways"
    if not gw_dir.exists():
        return results
    for sub in sorted(gw_dir.iterdir()):
        if not sub.is_dir() or sub.name.startswith("_"):
            continue
        identity = load_identity(hub_dir, sub.name)
        if identity:
            results.append({
                "gwId": sub.name,
                "koanId": identity.get("koanId"),
                "displayName": identity.get("persona", {}).get("displayName"),
                "registeredAt": identity.get("registeredAt"),
            })
    return results


# ── External (manually added) Koan units ─────────────────────

def _external_path(hub_dir: Path) -> Path:
    return hub_dir / "koan_external.json"


def _load_external(hub_dir: Path) -> list[dict]:
    p = _external_path(hub_dir)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _save_external(hub_dir: Path, data: list[dict]) -> None:
    p = _external_path(hub_dir)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def add_external_unit(hub_dir: Path, koan_id: str, label: str = "") -> dict:
    """Add a manually tracked external koan ID. Returns the new entry."""
    units = _load_external(hub_dir)
    for u in units:
        if u["koanId"] == koan_id:
            return u  # already exists
    import datetime
    entry = {
        "koanId": koan_id,
        "label": label or koan_id,
        "addedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    units.append(entry)
    _save_external(hub_dir, units)
    return entry


def remove_external_unit(hub_dir: Path, koan_id: str) -> bool:
    """Remove an external koan ID. Returns True if removed."""
    units = _load_external(hub_dir)
    new = [u for u in units if u["koanId"] != koan_id]
    if len(new) == len(units):
        return False
    _save_external(hub_dir, new)
    return True


def list_external_units(hub_dir: Path) -> list[dict]:
    """List all manually-added external koan units."""
    return _load_external(hub_dir)


def list_all_units(hub_dir: Path) -> list[dict]:
    """List all koan units — both local (gateway-linked) and external."""
    units = []
    for ident in list_identities(hub_dir):
        units.append({
            "koanId": ident["koanId"],
            "source": "local",
            "gwId": ident["gwId"],
            "label": ident.get("displayName") or ident["gwId"],
            "registeredAt": ident.get("registeredAt"),
        })
    for ext in list_external_units(hub_dir):
        units.append({
            "koanId": ext["koanId"],
            "source": "external",
            "gwId": None,
            "label": ext.get("label") or ext["koanId"],
            "addedAt": ext.get("addedAt"),
        })
    return units
