"""Explicit authorization/approval policy for tool and agent actions."""
from __future__ import annotations

from dataclasses import dataclass, field
import base64
import hashlib
import hmac
import json
import time
from typing import Callable, Iterable

from common.errors import AgentError, ErrorKind


@dataclass(frozen=True)
class ActionDescriptor:
    name: str
    read_only: bool = True
    destructive: bool = False
    external_side_effect: bool = False
    idempotent: bool = False
    allowed_roles: frozenset[str] = frozenset({"user", "agent"})


class ApprovalToken:
    """Small HMAC token for demos; production can replace it with the IAM service."""

    @staticmethod
    def issue(secret: str, *, subject: str, action: str, ttl_s: int = 300, now: int | None = None) -> str:
        if not secret or ttl_s < 1:
            raise ValueError("secret and positive ttl_s are required")
        payload = {"sub": subject, "action": action, "exp": int(now if now is not None else time.time()) + ttl_s}
        encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()).decode().rstrip("=")
        signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
        return encoded + "." + signature

    @staticmethod
    def verify(token: str, secret: str, *, subject: str, action: str, now: int | None = None) -> bool:
        try:
            encoded, signature = token.split(".", 1)
            expected = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                return False
            padded = encoded + "=" * (-len(encoded) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded).decode())
            current = int(now if now is not None else time.time())
            return payload.get("sub") == subject and payload.get("action") == action and current < int(payload.get("exp", 0))
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            return False


class ToolPolicy:
    def __init__(self, actions: Iterable[ActionDescriptor], *, require_approval_for_writes: bool = True, approval_secret: str | None = None, approval_verifier: Callable[[str, str], bool] | None = None):
        self._actions = {action.name: action for action in actions}
        self.require_approval_for_writes = require_approval_for_writes
        self.approval_secret = approval_secret
        self.approval_verifier = approval_verifier

    def descriptor(self, name: str) -> ActionDescriptor | None:
        return self._actions.get(name)

    def authorize(self, name: str, *, role: str = "agent", approval_token: str | None = None) -> ActionDescriptor:
        action = self._actions.get(name)
        if action is None:
            raise AgentError("action_not_allowed", f"动作 {name!r} 不在 allowlist 中", ErrorKind.AUTHORIZATION)
        if role not in action.allowed_roles:
            raise AgentError("role_not_allowed", "当前身份无权执行该动作", ErrorKind.AUTHORIZATION)
        if self.require_approval_for_writes and (action.destructive or action.external_side_effect):
            valid = False
            if approval_token and self.approval_secret:
                valid = ApprovalToken.verify(approval_token, self.approval_secret, subject=role, action=name)
            if approval_token and self.approval_verifier is not None:
                valid = bool(self.approval_verifier(name, approval_token))
            if not valid:
                raise AgentError("approval_required", "该动作需要绑定主体、动作和有效期的审批令牌", ErrorKind.APPROVAL_REQUIRED)
        return action
