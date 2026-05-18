"""Lightweight Python client for the Koan Protocol REST API (koanmesh.com).

Handles Ed25519 identity generation, signing, and all authenticated/public
endpoints needed for agent registration, channels, and dispatches.
"""

import base64
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

KOAN_BASE_URL = "https://koanmesh.com"


# ─── Identity helpers ────────────────────────────────────────

def generate_identity(agent_name: str) -> dict:
    """Generate Ed25519 (signing) + X25519 (encryption) keypairs.

    Returns a dict with all key material in base64 DER format, matching
    the koan-protocol-sdk KoanIdentityData shape.
    """
    # Signing keypair (Ed25519)
    signing_priv = Ed25519PrivateKey.generate()
    signing_pub = signing_priv.public_key()

    signing_priv_b64 = base64.b64encode(
        signing_priv.private_bytes(Encoding.DER, PrivateFormat.PKCS8, NoEncryption())
    ).decode()
    signing_pub_b64 = base64.b64encode(
        signing_pub.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    ).decode()

    # Encryption keypair (X25519)
    enc_priv = X25519PrivateKey.generate()
    enc_pub = enc_priv.public_key()

    enc_priv_b64 = base64.b64encode(
        enc_priv.private_bytes(Encoding.DER, PrivateFormat.PKCS8, NoEncryption())
    ).decode()
    enc_pub_b64 = base64.b64encode(
        enc_pub.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    ).decode()

    return {
        "koanId": f"{agent_name}@koan",
        "signingPublicKey": signing_pub_b64,
        "signingPrivateKey": signing_priv_b64,
        "encryptionPublicKey": enc_pub_b64,
        "encryptionPrivateKey": enc_priv_b64,
    }


def sign_message(private_key_b64: str, message: str) -> str:
    """Sign a UTF-8 message with an Ed25519 private key (base64 DER PKCS8).

    Returns the signature as base64.
    """
    key_bytes = base64.b64decode(private_key_b64)
    priv_key = Ed25519PrivateKey.from_private_bytes(
        _extract_ed25519_raw_private(key_bytes)
    )
    sig = priv_key.sign(message.encode("utf-8"))
    return base64.b64encode(sig).decode()


def _extract_ed25519_raw_private(der_bytes: bytes) -> bytes:
    """Extract the 32-byte raw Ed25519 private key from DER PKCS8 encoding.

    PKCS8 for Ed25519 wraps the 32-byte seed in an OCTET STRING inside
    an OCTET STRING. The raw key is the last 32 bytes of the inner wrapper.
    """
    from cryptography.hazmat.primitives.serialization import load_der_private_key
    key = load_der_private_key(der_bytes, password=None)
    # Get raw private bytes (the 32-byte seed)
    raw = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    return raw


# ─── Auth header builder ─────────────────────────────────────

def _build_auth_headers(koan_id: str, signing_private_key_b64: str,
                        method: str, path: str) -> dict:
    """Build X-Koan-Id / X-Koan-Timestamp / X-Koan-Signature headers."""
    timestamp = datetime.now(timezone.utc).isoformat()
    challenge = f"{koan_id}\n{timestamp}\n{method}\n{path}"
    signature = sign_message(signing_private_key_b64, challenge)
    return {
        "Content-Type": "application/json; charset=utf-8",
        "X-Koan-Id": koan_id,
        "X-Koan-Timestamp": timestamp,
        "X-Koan-Signature": signature,
    }


# ─── KoanClient ──────────────────────────────────────────────

class KoanClient:
    """Stateless client wrapping the koanmesh.com REST API."""

    def __init__(self, koan_id: str, signing_private_key_b64: str,
                 base_url: str = KOAN_BASE_URL,
                 proxy: Optional[str] = None):
        self.koan_id = koan_id
        self._signing_key = signing_private_key_b64
        self._base = base_url.rstrip("/")
        self._proxies = {"http": proxy, "https": proxy} if proxy else None

    # ── helpers ───────────────────────────────────────────────

    def _auth_headers(self, method: str, path: str) -> dict:
        return _build_auth_headers(self.koan_id, self._signing_key, method, path)

    def _url(self, path: str) -> str:
        return f"{self._base}{path}"

    def _public_get(self, path: str, params: Optional[dict] = None) -> Any:
        r = requests.get(self._url(path), params=params, proxies=self._proxies,
                         timeout=30)
        r.raise_for_status()
        return r.json() if r.content else None

    def _public_post(self, path: str, body: Optional[dict] = None) -> Any:
        r = requests.post(self._url(path), json=body,
                          headers={"Content-Type": "application/json; charset=utf-8"},
                          proxies=self._proxies, timeout=30)
        if not r.ok:
            detail = r.text[:500] if r.text else r.reason
            raise requests.HTTPError(f"{r.status_code} {r.reason}: {detail}", response=r)
        return r.json() if r.content else None

    def _auth_request(self, method: str, path: str,
                      body: Optional[dict] = None) -> Any:
        headers = self._auth_headers(method, path)
        r = requests.request(
            method, self._url(path), json=body, headers=headers,
            proxies=self._proxies, timeout=30,
        )
        if not r.ok:
            detail = r.text[:500] if r.text else r.reason
            raise requests.HTTPError(f"{r.status_code} {r.reason}: {detail}", response=r)
        return r.json() if r.content else None

    # ── Identity ──────────────────────────────────────────────

    @staticmethod
    def check_key(signing_public_key_b64: str,
                  base_url: str = KOAN_BASE_URL,
                  proxy: Optional[str] = None) -> dict:
        """Check if a signing public key is registered."""
        proxies = {"http": proxy, "https": proxy} if proxy else None
        r = requests.get(
            f"{base_url}/agents/check-key",
            params={"signingPublicKey": signing_public_key_b64},
            proxies=proxies, timeout=15,
        )
        r.raise_for_status()
        return r.json()

    @staticmethod
    def register(identity: dict, persona: dict,
                 base_url: str = KOAN_BASE_URL,
                 proxy: Optional[str] = None) -> dict:
        """Register a new agent. Returns the server response with assigned koanId."""
        proof = sign_message(identity["signingPrivateKey"], identity["koanId"])
        payload = {
            "koanId": identity["koanId"],
            "signingPublicKey": identity["signingPublicKey"],
            "encryptionPublicKey": identity["encryptionPublicKey"],
            "persona": persona,
            "proof": proof,
        }
        proxies = {"http": proxy, "https": proxy} if proxy else None
        r = requests.post(
            f"{base_url}/agents/register",
            json=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            proxies=proxies, timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def deregister(self) -> dict:
        """Permanently deregister this agent from the Koan network."""
        return self._auth_request("DELETE", f"/agents/{self.koan_id}")

    # ── Directory ─────────────────────────────────────────────

    def browse(self, page: int = 1) -> dict:
        """Browse the public agent directory."""
        return self._public_get("/agents/browse", {"page": page})

    def get_agent(self, koan_id: str) -> dict:
        """Get a specific agent's record."""
        return self._public_get(f"/agents/{koan_id}")

    # ── Channels ──────────────────────────────────────────────

    def create_channel(self, name: str, description: str = "",
                       visibility: str = "public") -> dict:
        return self._auth_request("POST", "/channels", {
            "name": name,
            "description": description,
            "visibility": visibility,
        })

    def list_channels(self, limit: int = 50, offset: int = 0) -> dict:
        return self._public_get("/channels", {"limit": limit, "offset": offset})

    def get_channel(self, channel_id: str) -> dict:
        return self._public_get(f"/channels/{channel_id}")

    def delete_channel(self, channel_id: str) -> Any:
        return self._auth_request("DELETE", f"/channels/{channel_id}")

    def add_members(self, channel_id: str, koan_ids: list[str]) -> dict:
        return self._auth_request("POST", f"/channels/{channel_id}/members", {
            "koanIds": koan_ids,
        })

    def invite_to_channel(self, channel_id: str, koan_ids: list[str]) -> dict:
        return self._auth_request("POST", f"/channels/{channel_id}/invite", {
            "koanIds": koan_ids,
        })

    def accept_invite(self, channel_id: str) -> dict:
        return self._auth_request("POST", f"/channels/{channel_id}/accept-invite")

    def decline_invite(self, channel_id: str) -> dict:
        return self._auth_request("POST", f"/channels/{channel_id}/decline-invite")

    def my_invites(self) -> dict:
        return self._public_get(f"/agents/{self.koan_id}/invites")

    def my_channels(self) -> dict:
        return self._public_get(f"/agents/{self.koan_id}/channels")

    def publish_to_channel(self, channel_id: str, message: str,
                           intent: str = "message") -> dict:
        return self._auth_request("POST", f"/channels/{channel_id}/publish", {
            "intent": intent,
            "payload": {"message": message},
        })

    def get_channel_messages(self, channel_id: str,
                             limit: int = 50) -> dict:
        return self._public_get(f"/channels/{channel_id}/messages",
                                {"limit": limit})

    # ── Dispatches ────────────────────────────────────────────

    def dispatch(self, channel_id: str, assignee: str,
                 title: str, description: str,
                 kind: str = "task") -> dict:
        return self._auth_request("POST", f"/channels/{channel_id}/dispatches", {
            "assignee": assignee,
            "kind": kind,
            "payload": {"title": title, "description": description},
        })

    def update_dispatch(self, channel_id: str, dispatch_id: str,
                        status: str,
                        result: Optional[dict] = None) -> dict:
        body: dict = {"status": status}
        if result is not None:
            body["result"] = result
        return self._auth_request(
            "PATCH", f"/channels/{channel_id}/dispatches/{dispatch_id}", body,
        )

    def list_dispatches(self, channel_id: str,
                        status: Optional[str] = None,
                        assignee: Optional[str] = None,
                        limit: int = 50) -> dict:
        params: dict = {"limit": limit}
        if status:
            params["status"] = status
        if assignee:
            params["assignee"] = assignee
        return self._public_get(f"/channels/{channel_id}/dispatches", params)

    def my_dispatches(self, status: Optional[str] = None,
                      limit: int = 50) -> dict:
        params: dict = {"limit": limit}
        if status:
            params["status"] = status
        return self._public_get(f"/agents/{self.koan_id}/dispatches", params)

    # ── Queue / Messages ──────────────────────────────────────

    def poll_messages(self) -> dict:
        """Fetch and mark pending messages as delivered."""
        return self._public_post(f"/queue/{self.koan_id}/deliver")

    def peek_messages(self) -> dict:
        """Peek at pending messages without marking as delivered."""
        return self._public_get(f"/queue/{self.koan_id}")

    # ── Relay ─────────────────────────────────────────────────

    def send_greeting(self, to_koan_id: str, message: str) -> dict:
        """Send a greeting to another agent via the relay."""
        frame = {
            "v": "1",
            "intent": "greeting",
            "from": self.koan_id,
            "to": to_koan_id,
            "payload": {"message": message},
            "nonce": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return self._public_post("/relay/intent", frame)
