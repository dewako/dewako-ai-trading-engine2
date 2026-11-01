"""Persisted AI configuration. Provider metadata lives in `ai_settings`; a cloud API key lives only in `ai_credentials`, encrypted by the
existing credential vault. A key is never returned by any read (only its last 4 characters) and never written to the audit log."""
from __future__ import annotations

import dataclasses
import json
import re
from typing import Callable, Optional

from dewako.ai.errors import AIError
from dewako.ai.manager import AIConfig, AIProviderManager
from dewako.ai.providers import LMStudioProvider, MockProvider, OllamaProvider, OpenAICompatCloudProvider, Transport, validate_endpoint
from dewako.persistence.db import Database, now_iso

NAME_RE = re.compile(r"^[a-z][a-z0-9_\-]{1,31}$")
TYPES = {"ollama": ("local", OllamaProvider), "lmstudio": ("local", LMStudioProvider), "openai_compat": ("cloud", OpenAICompatCloudProvider)}
DEFAULT_CONFIG = AIConfig()


class AISettingsStore:
    def __init__(self, db: Database, audit=None):
        self.db, self.audit = db, audit

    def load(self) -> tuple[AIConfig, list[dict]]:
        with self.db.read() as c:
            r = c.execute("SELECT config, providers FROM ai_settings WHERE id=1").fetchone()
        if r is None:
            return AIConfig(), []
        return AIConfig(**json.loads(r["config"])), json.loads(r["providers"])

    def public(self) -> dict:
        cfg, provs = self.load()
        with self.db.read() as c:
            keys = {x["provider"]: x["last4"] for x in c.execute("SELECT provider, last4 FROM ai_credentials").fetchall()}
        return {"config": dataclasses.asdict(cfg), "providers": [{**p, "kind": TYPES[p["type"]][0], "credential_last4": keys.get(p["name"])} for p in provs]}

    def save(self, config: dict, providers: list[dict], actor: dict, correlation_id: str = "") -> dict:
        cfg = AIConfig(**{**dataclasses.asdict(AIConfig()), **config}).validate()
        names, clean = set(), []
        for p in providers[:8]:
            t = p.get("type")
            if t not in TYPES or not NAME_RE.match(str(p.get("name", ""))) or p["name"] in names:
                raise AIError("MISCONFIGURED", "provider needs a unique name and a known type")
            names.add(p["name"])
            model = str(p.get("model", ""))[:80]
            if not model or not re.match(r"^[A-Za-z0-9._:/\-]+$", model):
                raise AIError("MISCONFIGURED", "model name is required (letters, digits, . _ : / -)")
            clean.append({"name": p["name"], "type": t, "endpoint": validate_endpoint(TYPES[t][0], str(p.get("endpoint", ""))), "model": model,
                          "enabled": bool(p.get("enabled", True))})
        ts = now_iso()
        with self.db.tx() as c:
            c.execute("DELETE FROM ai_settings WHERE id=1")
            c.execute("INSERT INTO ai_settings(id,config,providers,updated_by,updated_at) VALUES (1,?,?,?,?)",
                      (json.dumps(dataclasses.asdict(cfg)), json.dumps(clean), actor.get("username", ""), ts))
            if self.audit:
                self.audit.record("AI_SETTINGS_CHANGED", "OK", correlation_id, actor=actor, resource="ai_settings",
                                  metadata={"mode": cfg.mode, "allow_cloud_fallback": cfg.allow_cloud_fallback, "providers": [(p["name"], p["type"], p["enabled"]) for p in clean]}, conn=c)
        return self.public()

    def set_credential(self, provider: str, api_key: str, vault, actor: dict, correlation_id: str = "") -> dict:
        _, provs = self.load()
        match = [p for p in provs if p["name"] == provider]
        if not match or TYPES[match[0]["type"]][0] != "cloud":
            raise AIError("MISCONFIGURED", "credentials can only be stored for a configured cloud provider")
        if not (8 <= len(api_key) <= 512) or any(ch.isspace() for ch in api_key):
            raise AIError("MISCONFIGURED", "api key looks invalid")
        blob = vault.encrypt({"api_key": api_key})
        ts = now_iso()
        with self.db.tx() as c:
            c.execute("DELETE FROM ai_credentials WHERE provider=?", (provider,))
            c.execute("INSERT INTO ai_credentials(provider,encrypted_blob,encryption_key_version,last4,updated_at) VALUES (?,?,?,?,?)",
                      (provider, blob, vault.current_version, api_key[-4:], ts))
            if self.audit:
                self.audit.record("AI_CREDENTIAL_SET", "OK", correlation_id, actor=actor, resource=f"ai_provider:{provider}", metadata={"last4": api_key[-4:]}, conn=c)
        return {"provider": provider, "credential_last4": api_key[-4:]}

    def _key(self, provider: str, vault_factory: Callable) -> str:
        with self.db.read() as c:
            r = c.execute("SELECT encrypted_blob, encryption_key_version FROM ai_credentials WHERE provider=?", (provider,)).fetchone()
        if r is None:
            return ""
        return vault_factory().decrypt(bytes(r["encrypted_blob"]), r["encryption_key_version"]).get("api_key", "")

    def build_manager(self, vault_factory: Callable, *, transport: Optional[Transport] = None, audit=None, **kw) -> AIProviderManager:
        cfg, provs = self.load()
        built = []
        for p in provs:
            kind, cls = TYPES[p["type"]]
            if not p["enabled"] or (cfg.mode == "LOCAL_ONLY" and kind == "cloud") or (cfg.mode == "CLOUD_ONLY" and kind == "local"):
                continue                                     # in LOCAL_ONLY a cloud provider is not even constructed, so it cannot be reached or hold a key
            sup = (lambda n=p["name"]: self._key(n, vault_factory)) if kind == "cloud" else None
            built.append(cls(p["name"], p["endpoint"], p["model"], transport, sup))
        return AIProviderManager(cfg, built, audit=audit or self.audit, **kw)
