#!/usr/bin/env python3
"""Round 2: official-style /weblogin renewal using the LOGIN RESPONSE tokens.

Round 1 findings: the baseline /web/login/renewal works (rotates all cookies);
five /weblogin renewal shapes built from COOKIE values were rejected (legacy:
errCode=-1 "login fail"; ink-style: needRemoteLoginVerify). The login response
itself carries ['accessToken', 'refreshToken', 'vid'] - the inputs the official
client's renewSession uses. This round captures those tokens via a tap and
retries the renewal shapes with them, plus tests /web/login/session/init as a
renewal candidate and a variant that carries the login's one-time code.

Privacy: no cookie, token, or API-key value is printed or saved. Only presence,
length, equality booleans (sha256 compared in-process), cookie names with
emptiness, JSON field names, and non-secret status fields are reported.

Usage:
    python3 scripts/verify_weblogin_renewal.py [--open-browser] [--seed SEED]
"""

from __future__ import annotations

import argparse
import hashlib
import random
import time
from typing import Any

import requests

import verify_weblogin_identity as lab

CAPTURED: dict[str, Any] = {}


def _tap_send_json(original):
    def wrapper(session, method, url, **kwargs):
        response, data = original(session, method, url, **kwargs)
        if isinstance(data, dict):
            token = lab.first_value(data, lab.TOKEN_KEYS)
            refresh = lab.first_value(data, lab.REFRESH_KEYS)
            if token and not CAPTURED.get("access"):
                CAPTURED["access"] = token
            if refresh and not CAPTURED.get("refresh"):
                CAPTURED["refresh"] = refresh
        return response, data
    return wrapper


def _tap_variants(original):
    def wrapper(vid, skey, code, identity, pf):
        CAPTURED.setdefault("vid_login", vid)
        CAPTURED.setdefault("skey_login", skey)
        CAPTURED.setdefault("code_login", code)
        CAPTURED.setdefault("pf_login", pf)
        return original(vid, skey, code, identity, pf)
    return wrapper


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--open-browser", action="store_true")
    parser.add_argument("--seed", default="ko-renewal-probe")
    return parser.parse_args()


def same(one: str, two: str) -> bool:
    if not one or not two:
        return False
    return hashlib.sha256(one.encode()).hexdigest() == hashlib.sha256(two.encode()).hexdigest()


def described(value: str) -> str:
    return f"len={len(value)}" if value else "missing"


def attempt(session: requests.Session, stage: str, url: str, payload: dict[str, Any],
            params: dict[str, str] | None) -> dict[str, Any] | None:
    response, data = lab.send_json(session, "POST", url, timeout=20, stage=stage,
                                   headers={"Origin": lab.BASE_URL, "Referer": f"{lab.BASE_URL}/"},
                                   payload=payload, params=params)
    if response is None:
        print(f"[{stage}] transport error", flush=True)
        return None
    err_code, err_msg, logic = lab.error_and_logic(data)
    token = lab.first_value(data, lab.TOKEN_KEYS) if isinstance(data, dict) else ""
    refresh = lab.first_value(data, lab.REFRESH_KEYS) if isinstance(data, dict) else ""
    returned = lab.set_cookie_values(response)
    print(f"[{stage}] HTTP {response.status_code}; errCode={err_code}; errMsg={err_msg!r}; "
          f"logic={logic!r}\n"
          f"[{stage}] body fields={lab.field_names(data)}; "
          f"new accessToken={described(token)}; new refreshToken={described(refresh)}\n"
          f"[{stage}] Set-Cookie: {lab.describe_cookies(returned)}", flush=True)
    return {"errCode": err_code, "logic": logic, "new_token": bool(token),
            "set_cookie_names": sorted(returned)}


def main() -> int:
    args = parse_args()
    fp = lab.derive_fp(args.seed)
    identity = lab.DeviceIdentity(fp, lab.derive_device_id(fp))

    lab.send_json = _tap_send_json(lab.send_json)
    lab.weblogin_variants = _tap_variants(lab.weblogin_variants)

    print("Round 2: capture login tokens, then official-style renewal shapes.")
    print(f"experiment fp (non-secret): {identity.fp}\n")
    outcome = lab.login_session("R2", identity=identity, open_browser=args.open_browser)
    if not outcome.login_ok:
        print("login did not yield credentials; aborting.", flush=True)
        return 1

    session = outcome.session
    cookies = {name: session.cookies.get(name) or "" for name in lab.LOGIN_COOKIE_NAMES}
    access = lab.scalar_text(CAPTURED.get("access", ""))
    refresh = lab.scalar_text(CAPTURED.get("refresh", ""))
    print(f"[R2] login tokens: accessToken={described(access)}; refreshToken={described(refresh)}\n"
          f"[R2] cookie vs token equality: wr_skey==accessToken: {same(cookies.get('wr_skey', ''), access)}; "
          f"wr_rt==refreshToken: {same(cookies.get('wr_rt', ''), refresh)}; "
          f"wr_skey==skey_from_getinfo: {same(cookies.get('wr_skey', ''), lab.scalar_text(CAPTURED.get('skey_login', '')))}\n"
          f"[R2] cookie presence: {lab.describe_cookies(cookies)}\n", flush=True)

    if not access:
        print("[R2] no accessToken captured; continuing with cookie values.", flush=True)
    token_value = access or cookies.get("wr_skey", "")
    refresh_value = refresh or cookies.get("wr_rt", "")
    vid = lab.scalar_text(cookies.get("wr_vid", "")) or lab.scalar_text(CAPTURED.get("vid_login", ""))
    code = CAPTURED.get("code_login")
    cgi = random.randint(100, 999)
    fp_value = identity.fp

    print("[R2] baseline API + current web renewal:", flush=True)
    lab.probe_api(session, "R2 baseline API")
    time.sleep(1)
    lab.renewal_probe(session, "R2 web renewal", lab.RENEWAL_PAYLOAD)
    time.sleep(1)
    lab.probe_api(session, "R2 API after web renewal")
    time.sleep(1)

    print("\n[R2] official-style renewal shapes with the captured tokens:", flush=True)
    shapes: list[tuple[str, str, dict[str, Any], dict[str, str] | None]] = [
        ("R1 legacy vid/accessToken/refreshToken",
         lab.LEGACY_WEBLOGIN_URL,
         {"vid": vid, "skey": token_value, "rt": refresh_value, "isAutoLogout": 0, "pf": 2,
          "cgiKey": cgi, "fp": fp_value}, {"platform": "desktop"}),
        ("R2 legacy accessToken/refreshToken fields",
         lab.LEGACY_WEBLOGIN_URL,
         {"vid": vid, "accessToken": token_value, "refreshToken": refresh_value, "pf": 2,
          "cgiKey": cgi, "fp": fp_value}, {"platform": "desktop"}),
        ("R3 legacy tokens + login code",
         lab.LEGACY_WEBLOGIN_URL,
         {"vid": vid, "skey": token_value, "accessToken": token_value,
          "refreshToken": refresh_value, "rt": refresh_value, "code": code,
          "isAutoLogout": 0, "pf": 2, "cgiKey": cgi, "fp": fp_value}, {"platform": "desktop"}),
        ("R4 session/init as renewal",
         lab.SESSION_INIT_URL,
         {"vid": vid, "skey": token_value, "pf": 2, "ql": 0, "rt": refresh_value}, None),
        ("R5 ink-style accessToken/refreshToken",
         lab.WEB_WEBLOGIN_URL,
         {"vid": vid, "accessToken": token_value, "refreshToken": refresh_value, "pf": 2,
          "fp": fp_value}, None),
        ("R6 legacy tokens only",
         lab.LEGACY_WEBLOGIN_URL,
         {"accessToken": token_value, "refreshToken": refresh_value},
         {"platform": "desktop"}),
    ]
    results: list[tuple[str, dict[str, Any] | None]] = []
    for name, url, payload, params in shapes:
        results.append((name, attempt(session, name, url, payload, params)))
        time.sleep(1)
        lab.probe_api(session, f"API after {name}")
        time.sleep(1)

    print("\n================ Summary ================")
    for name, result in results:
        if result is None:
            print(f"{name}: transport error")
            continue
        print(f"{name}: errCode={result['errCode']}; logic={result['logic']!r}; "
              f"new accessToken={result['new_token']}; Set-Cookie={result['set_cookie_names']}")
    print("=========================================\n")
    return 0


if __name__ == "__main__":
    import sys

    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        raise SystemExit(130)
    except (requests.RequestException, lab.ProtocolError, ValueError) as exc:
        print(f"Experiment failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
