from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests

from app.config import AppConfig, read_state, write_state
from app.crypto_utils import (
    code_challenge,
    decrypt_hmac_key,
    generate_code_verifier,
    generate_rsa_keypair,
)
from app.signing import build_signature


MOBILE_BASE = "https://mobile-app-back.davidlloyd.co.uk"
OKTA_BASE = "https://davidlloyd.okta.com"
OIDC_ISSUER = "https://digitalmanager.davidlloyd.co.uk/oauth2/default"


class DavidLloydError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


@dataclass
class LoginResult:
    access_token_expires_at: int
    hmac_expires_at: int
    user_id: str | None
    scopes: list[str]


class DavidLloydClient:
    def __init__(self, config: AppConfig):
        self.config = config
        self.http = requests.Session()
        self.http.trust_env = False

    def status(self) -> dict:
        state = read_state()
        now = int(time.time())
        return {
            "has_access_token": bool(state.get("access_token")),
            "has_refresh_token": bool(state.get("refresh_token")),
            "has_hmac_key": bool(state.get("hmac_key_b64")),
            "device_id": self.config.device_id,
            "session_id": state.get("session_id"),
            "access_token_expires_at": state.get("access_token_expires_at"),
            "hmac_expires_at": state.get("hmac_expires_at"),
            "access_token_valid_for_seconds": max(0, state.get("access_token_expires_at", 0) - now),
            "hmac_valid_for_seconds": max(0, state.get("hmac_expires_at", 0) - now),
        }

    def login(self) -> LoginResult:
        state = read_state()
        state["session_id"] = str(uuid.uuid4())
        self._ensure_rsa_keypair(state, rotate=True)
        self._refresh_hmac(state)

        self._enroll_phone(state)
        session_token = self._okta_authn()
        token_payload = self._authorization_code_login(session_token)
        self._store_tokens(state, token_payload)
        self._register_device(state, is_new_login=True)
        write_state(state)

        return LoginResult(
            access_token_expires_at=state["access_token_expires_at"],
            hmac_expires_at=state["hmac_expires_at"],
            user_id=state.get("user_id"),
            scopes=state.get("scopes", []),
        )

    def refresh_token(self) -> dict:
        state = read_state()
        self._refresh_access_token(state)
        write_state(state)
        return self.status()

    def _refresh_access_token(self, state: dict) -> None:
        refresh_token = state.get("refresh_token")
        if not refresh_token:
            raise DavidLloydError("No refresh token available. Run POST /auth/login first.")

        response = self.http.post(
            f"{OIDC_ISSUER}/v1/token",
            headers=self._okta_headers(),
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.config.client_id,
            },
            timeout=30,
        )
        payload = self._json_response(response)
        self._store_tokens(state, payload)

    def refresh_hmac(self) -> dict:
        state = read_state()
        self._ensure_rsa_keypair(state, rotate=True)
        self._refresh_hmac(state)
        write_state(state)
        return self.status()

    def membership_status(self) -> dict:
        return self.mobile_get("/members/me/membership-status")

    def bookings(self) -> dict:
        return self.mobile_get("/members/me/bookings?include-others-i-can-book-for")

    def search_players(self, search: str) -> dict:
        return self.mobile_get("/players", params={"search": search})

    def mobile_get(self, path: str, *, params: dict[str, Any] | None = None) -> dict:
        state = read_state()
        self._ensure_valid_auth_state(state)
        write_state(state)

        request = requests.Request("GET", f"{MOBILE_BASE}{path}", params=params).prepare()
        url = request.url or f"{MOBILE_BASE}{path}"
        response = self.http.get(
            url,
            headers=self._mobile_headers(
                state,
                signed=True,
                method="GET",
                url=url,
                body="",
                auth_token=state["access_token"],
            ),
            timeout=30,
        )
        response = self._retry_after_auth_issue(response, state, "GET", url, "")
        return self._json_response(response)

    def mobile_post(self, path: str, *, payload: dict[str, Any]) -> dict:
        return self._mobile_body_request("POST", path, payload=payload)

    def mobile_put(self, path: str, *, payload: dict[str, Any]) -> dict:
        return self._mobile_body_request("PUT", path, payload=payload)

    def _mobile_body_request(self, method: str, path: str, *, payload: dict[str, Any]) -> dict:
        state = read_state()
        self._ensure_valid_auth_state(state)
        write_state(state)

        url = f"{MOBILE_BASE}{path}"
        body = self._compact_json(payload)
        response = self.http.request(
            method,
            url,
            headers=self._mobile_headers(
                state,
                signed=True,
                method=method,
                url=url,
                body=body,
                auth_token=state["access_token"],
            ),
            data=body,
            timeout=30,
        )
        response = self._retry_after_auth_issue(response, state, method, url, body)
        return self._json_response(response)

    def _ensure_valid_auth_state(self, state: dict) -> None:
        now = int(time.time())
        if not state.get("access_token"):
            raise DavidLloydError("No access token available. Run POST /auth/login first.")
        if not state.get("refresh_token"):
            raise DavidLloydError("No refresh token available. Run POST /auth/login first.")
        if now >= int(state.get("access_token_expires_at", 0)) - 60:
            self._refresh_access_token(state)
        if (
            not state.get("hmac_key_b64")
            or now >= int(state.get("hmac_expires_at", 0)) - 60
        ):
            self._ensure_rsa_keypair(state)
            self._refresh_hmac(state)

    def _retry_after_auth_issue(
        self,
        response: requests.Response,
        state: dict,
        method: str,
        url: str,
        body: str,
    ) -> requests.Response:
        safe_body = self._safe_response_body(response)
        body_text = json.dumps(safe_body).lower()

        if response.status_code == 400 and "device_mismatch" in body_text:
            self.login()
            state.clear()
            state.update(read_state())
            return self.http.request(
                method,
                url,
                headers=self._mobile_headers(
                    state,
                    signed=True,
                    method=method,
                    url=url,
                    body=body,
                    auth_token=state["access_token"],
                ),
                data=body if method.upper() != "GET" else None,
                timeout=30,
            )

        if response.status_code == 403 and "hmac_validation_failed" in body_text:
            self._ensure_rsa_keypair(state, rotate=True)
            self._refresh_hmac(state)
            write_state(state)
            return self.http.request(
                method,
                url,
                headers=self._mobile_headers(
                    state,
                    signed=True,
                    method=method,
                    url=url,
                    body=body,
                    auth_token=state["access_token"],
                ),
                data=body if method.upper() != "GET" else None,
                timeout=30,
            )

        if response.status_code != 401:
            return response

        if "token is invalid" not in body_text:
            return response

        self._refresh_access_token(state)
        write_state(state)
        return self.http.request(
            method,
            url,
            headers=self._mobile_headers(
                state,
                signed=True,
                method=method,
                url=url,
                body=body,
                auth_token=state["access_token"],
            ),
            data=body if method.upper() != "GET" else None,
            timeout=30,
        )

    def _ensure_rsa_keypair(self, state: dict, *, rotate: bool = False) -> None:
        if rotate or not state.get("private_key_pem") or not state.get("public_key_b64"):
            private_pem, public_key_b64 = generate_rsa_keypair()
            state["private_key_pem"] = private_pem
            state["public_key_b64"] = public_key_b64

    def _refresh_hmac(self, state: dict) -> None:
        url = f"{MOBILE_BASE}/hmac/key"
        payload = {"publicKey": state["public_key_b64"]}
        response = self.http.post(
            url,
            headers=self._mobile_headers(state, signed=False),
            json=payload,
            timeout=30,
        )
        data = self._json_response(response)
        hmac_key = decrypt_hmac_key(state["private_key_pem"], data["key"])
        state["hmac_key_b64"] = self._b64(hmac_key)
        state["hmac_expires_at"] = int(data["expirationTimestamp"])

    def _enroll_phone(self, state: dict) -> None:
        url = f"{MOBILE_BASE}/login/enroll-phone"
        payload = {"phoneNumber": self.config.username}
        body = self._compact_json(payload)
        response = self.http.post(
            url,
            headers=self._mobile_headers(state, signed=True, method="POST", url=url, body=body),
            data=body,
            timeout=30,
        )
        self._json_response(response)

    def _okta_authn(self) -> str:
        response = self.http.post(
            f"{OKTA_BASE}/api/v1/authn",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": self.config.okta_authn_user_agent,
            },
            json={
                "relayState": None,
                "username": self.config.username,
                "password": self.config.password,
            },
            timeout=30,
        )
        data = self._json_response(response)
        if data.get("status") != "SUCCESS" or not data.get("sessionToken"):
            raise DavidLloydError("Okta authentication did not return SUCCESS", body=self._redact(data))
        return data["sessionToken"]

    def _authorization_code_login(self, session_token: str) -> dict:
        verifier = generate_code_verifier()
        nonce = uuid.uuid4().hex
        state = uuid.uuid4().hex
        authorize = self.http.get(
            f"{OIDC_ISSUER}/v1/authorize",
            headers=self._okta_headers(),
            params={
                "scope": self.config.scope,
                "sessionToken": session_token,
                "response_type": "code",
                "state": state,
                "code_challenge_method": "S256",
                "redirect_uri": self.config.redirect_uri,
                "nonce": nonce,
                "code_challenge": code_challenge(verifier),
                "client_id": self.config.client_id,
            },
            allow_redirects=False,
            timeout=30,
        )
        if authorize.status_code != 302:
            raise DavidLloydError(
                "Authorize request did not return redirect",
                status_code=authorize.status_code,
                body=authorize.text,
            )
        location = authorize.headers.get("location", "")
        parsed = urlparse(location)
        query = parse_qs(parsed.query)
        code = query.get("code", [None])[0]
        returned_state = query.get("state", [None])[0]
        if not code or returned_state != state:
            raise DavidLloydError("Authorize redirect did not contain expected code/state")

        token = self.http.post(
            f"{OIDC_ISSUER}/v1/token",
            headers=self._okta_headers(accept=True),
            data={
                "code": code,
                "redirect_uri": self.config.redirect_uri,
                "grant_type": "authorization_code",
                "nonce": nonce,
                "client_id": self.config.client_id,
                "code_verifier": verifier,
            },
            timeout=30,
        )
        return self._json_response(token)

    def _register_device(self, state: dict, *, is_new_login: bool) -> None:
        url = f"{MOBILE_BASE}/register-device"
        payload = {"isNewLogin": is_new_login}
        body = self._compact_json(payload)
        response = self.http.post(
            url,
            headers=self._mobile_headers(
                state,
                signed=True,
                method="POST",
                url=url,
                body=body,
                auth_token=state["access_token"],
            ),
            data=body,
            timeout=30,
        )
        if response.status_code == 200:
            return
        raise DavidLloydError(
            "Register device failed",
            status_code=response.status_code,
            body=self._safe_response_body(response),
        )

    def _mobile_headers(
        self,
        state: dict,
        *,
        signed: bool,
        method: str = "POST",
        url: str = "",
        body: str = "",
        auth_token: str | None = None,
    ) -> dict:
        headers = {
            "Accept-Encoding": "gzip",
            "Connection": "Keep-Alive",
            "Content-Type": "application/json",
            "User-Agent": self.config.user_agent,
            "X-Device-Id": self.config.device_id,
            "X-Request-Id": str(uuid.uuid4()),
            "X-Session-Id": state["session_id"],
        }
        if auth_token:
            headers["X-Auth-Token"] = auth_token
        if signed:
            timestamp = int(time.time())
            headers["X-Timestamp"] = str(timestamp)
            headers["X-Signature"] = build_signature(
                mode=self.config.signature_mode,
                public_key_b64=state["public_key_b64"],
                hmac_key=self._unb64(state["hmac_key_b64"]),
                method=method,
                url=url,
                timestamp=timestamp,
                body=body,
                device_id=self.config.device_id,
                content_type=headers["Content-Type"],
            )
        return headers

    def _okta_headers(self, *, accept: bool = False) -> dict:
        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "User-Agent": self.config.okta_user_agent,
        }
        if accept:
            headers["Accept"] = "application/json; charset=UTF-8"
        return headers

    def _store_tokens(self, state: dict, payload: dict) -> None:
        access_token = payload["access_token"]
        claims = self._decode_jwt_payload(access_token)
        state["access_token"] = access_token
        state["refresh_token"] = payload.get("refresh_token", state.get("refresh_token"))
        state["id_token"] = payload.get("id_token", state.get("id_token"))
        state["access_token_expires_at"] = int(claims.get("exp", int(time.time()) + payload.get("expires_in", 3600)))
        state["user_id"] = claims.get("uid")
        state["scopes"] = claims.get("scp", [])

    @staticmethod
    def _decode_jwt_payload(token: str) -> dict:
        import base64

        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))

    @staticmethod
    def _json_response(response: requests.Response) -> dict:
        if 200 <= response.status_code < 300:
            return response.json() if response.content else {}
        try:
            body: Any = response.json()
        except ValueError:
            body = response.text
        raise DavidLloydError(
            "HTTP request failed",
            status_code=response.status_code,
            body=body,
        )

    @staticmethod
    def _safe_response_body(response: requests.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            return response.text

    @staticmethod
    def _compact_json(payload: dict) -> str:
        return json.dumps(payload, separators=(",", ":"))

    @staticmethod
    def _b64(value: bytes) -> str:
        import base64

        return base64.b64encode(value).decode("ascii")

    @staticmethod
    def _unb64(value: str) -> bytes:
        import base64

        return base64.b64decode(value)

    @staticmethod
    def _redact(data: Any) -> Any:
        if isinstance(data, dict):
            return {
                key: ("[REDACTED]" if "token" in key.lower() or key.lower() == "password" else DavidLloydClient._redact(value))
                for key, value in data.items()
            }
        if isinstance(data, list):
            return [DavidLloydClient._redact(item) for item in data]
        return data
