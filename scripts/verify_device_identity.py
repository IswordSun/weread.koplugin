#!/usr/bin/env python3
"""Verify whether a per-device User-Agent prevents the WeRead session kick.

Background
----------
Logging into WeRead on a second device replaces the first device's web session:
the older session starts failing with ``-2012`` ("登录超时") and its renewal
returns ``-2013`` with an empty-valued clearing ``Set-Cookie``, so silent
recovery is impossible (upstream finlater/weread.koplugin issue #158; reproduced
by ``verify_session_renewal.py`` and ``verify_session_recovery.py``). The
maintainer hinted the client "could avoid fixing the User-Agent", and the
official client's UA embeds a device brand string (``WRBrand/remarkable``).
This script tests the Phase 1 hypothesis from ``../02-plugin-patch-design.md``
section 3.5 (L4 device identity): a distinct per-device User-Agent MAY make the
server treat the two sessions as separate devices and stop replacing the older
one.

Experiment
----------
1. Session A logs in (QR #1) with ``UA_A`` - by default the plugin's current
   desktop User-Agent - then verifies baseline Web API access and renews once
   with the canonical payload ``{"rq":"%2Fweb%2Fbook%2Fread","ql":false}``.
2. Session B logs in (QR #2) with a DIFFERENT ``UA_B`` (default: an Android
   phone UA). ``--ua-a``/``--ua-b`` override the defaults; both are printed.
3. Kick detector: session A's Web API is probed immediately after B's login -
   the decisive observation (kicked vs survived).
4. If A survived, coexistence-sustain checks run: renew A then probe A, renew B
   then probe B, then probe A once more (delayed re-check).

Privacy
-------
No cookie value, token, or API key is printed or saved. Only presence, type,
length, cookie *names*, and non-secret status fields (``succ``/``errCode``/
``errMsg``) are reported. The temporary QR image is deleted after use.

Usage
-----
    python3 scripts/verify_device_identity.py [--open-browser] \\
        [--ua-a UA] [--ua-b UA]

Both QR scans must be confirmed with the same WeRead account. The plain confirm
URL is printed so it can be scanned from another device. The script performs
fewer than thirty requests in total; keep the volume low.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests


BASE_URL = "https://weread.qq.com"
SKILLS_PAGE_URL = f"{BASE_URL}/r/weread-skills"
LOGIN_UID_URL = f"{BASE_URL}/api/auth/getLoginUid"
LOGIN_INFO_URL = f"{BASE_URL}/api/auth/getLoginInfo"
RENEWAL_URL = f"{BASE_URL}/web/login/renewal"
SHELF_API_URL = f"{BASE_URL}/web/shelf/sync?onlyBookid=1"

# Session A uses the plugin's current desktop User-Agent (baseline status quo).
DEFAULT_UA_A = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Safari/537.36 Edg/135.0.0.0"
)
# Session B uses a distinct per-device User-Agent (Android phone).
DEFAULT_UA_B = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Mobile Safari/537.36"
)
RENEWAL_PAYLOAD: dict[str, Any] = {"rq": "%2Fweb%2Fbook%2Fread", "ql": False}


class ProtocolError(RuntimeError):
    """Raised when WeRead returns an unexpected login response."""


def describe_shape(value: Any, depth: int = 0) -> Any:
    """Return JSON structure metadata without exposing credential values."""
    if depth >= 3:
        return type(value).__name__
    if isinstance(value, dict):
        return {
            str(key): describe_shape(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return {
            "type": "list",
            "length": len(value),
            "item": describe_shape(value[0], depth + 1) if value else None,
        }
    if isinstance(value, str):
        return {"type": "str", "length": len(value)}
    return type(value).__name__


def safe_json(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def response_cookie_names(response: requests.Response) -> list[str]:
    """Extract Set-Cookie names only; never values."""
    values: list[str] = []
    raw = getattr(response, "raw", None)
    if raw is not None and hasattr(raw.headers, "getlist"):
        try:
            values = raw.headers.getlist("Set-Cookie")
        except Exception:  # noqa: BLE001 - header containers differ by version
            values = []
    if not values:
        joined = response.headers.get("Set-Cookie")
        values = [joined] if joined else []
    names = []
    for value in values:
        first = value.split(";", 1)[0]
        if "=" in first:
            names.append(first.split("=", 1)[0].strip())
    return sorted(set(names))


def session_cookie_names(session: requests.Session) -> list[str]:
    return sorted({cookie.name for cookie in session.cookies})


def request_json(
    session: requests.Session,
    url: str,
    *,
    timeout: int,
    headers: dict[str, str] | None = None,
    stage: str,
) -> dict[str, Any]:
    response = session.get(url, headers=headers, timeout=timeout)
    print(
        f"[{stage}] HTTP {response.status_code}; "
        f"content-type={response.headers.get('content-type', 'unknown')}",
        flush=True,
    )
    data = safe_json(response)
    if not response.ok:
        shape = describe_shape(data) if data is not None else "non-JSON body"
        raise ProtocolError(
            f"{stage} returned HTTP {response.status_code}; response shape: {shape!r}"
        )
    if not isinstance(data, dict):
        raise ProtocolError(f"{stage} did not return a JSON object")
    return data


def establish_session(session: requests.Session) -> str:
    response = session.get(
        SKILLS_PAGE_URL,
        headers={"Referer": f"{BASE_URL}/"},
        timeout=20,
    )
    print(
        f"[skills page] HTTP {response.status_code}; redirects={len(response.history)}; "
        f"Set-Cookie={bool(response.headers.get('Set-Cookie'))}",
        flush=True,
    )
    response.raise_for_status()

    data = request_json(
        session,
        LOGIN_UID_URL,
        timeout=20,
        headers={"Referer": SKILLS_PAGE_URL},
        stage="getLoginUid",
    )
    uid = data.get("uid")
    if not isinstance(uid, str) or not uid:
        raise ProtocolError("WeRead did not return a valid login UID")
    return uid


def poll_login(
    session: requests.Session,
    uid: str,
    otp: str = "",
) -> dict[str, Any]:
    # The empty form is intentionally `&otp`, not `&otp=`.
    url = f"{LOGIN_INFO_URL}?uid={quote(uid, safe='')}&otp"
    if otp:
        url += f"={quote(otp, safe='')}"
    return request_json(
        session,
        url,
        timeout=70,
        headers={"Referer": SKILLS_PAGE_URL},
        stage="getLoginInfo",
    )


def wait_for_login(session: requests.Session, uid: str) -> dict[str, Any]:
    result = poll_login(session, uid)
    while result.get("succeed") is not True:
        logic_code = str(result.get("logicCode") or "")
        print(f"Login state: {logic_code or 'UNKNOWN'}", flush=True)

        if logic_code == "NEED_OTP":
            otp = input("Enter the four-digit code shown on your phone: ").strip()
            if len(otp) != 4 or not otp.isdigit():
                print("The verification code must contain four digits.")
                continue
            result = poll_login(session, uid, otp)
            continue

        if logic_code == "OTP_NOT_MATCH":
            print("The verification code did not match.")
            result = {"logicCode": "NEED_OTP"}
            continue

        if logic_code in {"LOGIN_TIMEOUT", "OTP_EXPIRED"}:
            raise ProtocolError(f"Login stopped with {logic_code}")

        raise ProtocolError(f"Unexpected login state: {logic_code or result!r}")

    return result


def open_qr_image(confirm_url: str) -> Path:
    try:
        import qrcode
    except ImportError as exc:
        raise ProtocolError(
            "Opening a local QR image requires `pip install qrcode[pil]`"
        ) from exc

    handle = tempfile.NamedTemporaryFile(
        prefix="weread-qr-",
        suffix=".png",
        delete=False,
    )
    handle.close()
    path = Path(handle.name)
    image = qrcode.make(confirm_url)
    image.save(path)
    webbrowser.open(path.as_uri())
    return path


def install_login_cookies(
    session: requests.Session,
    login_result: dict[str, Any],
) -> str:
    web_login_vid = str(login_result.get("webLoginVid") or "")
    access_token = str(login_result.get("accessToken") or "")
    refresh_token = str(login_result.get("refreshToken") or "")
    if not web_login_vid or not access_token:
        raise ProtocolError("Successful response is missing account credentials")

    cookie_values = {
        "wr_vid": web_login_vid,
        "wr_skey": access_token,
        "wr_ql": "0",
    }
    if refresh_token:
        cookie_values["wr_rt"] = quote(refresh_token, safe="")
    for name, value in cookie_values.items():
        session.cookies.set(name, value, domain=".weread.qq.com", path="/")
    return web_login_vid


def login_flow(
    label: str,
    *,
    user_agent: str,
    open_browser: bool,
) -> tuple[requests.Session, dict[str, Any]]:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": user_agent,
            "Accept": "application/json, text/plain, */*",
        }
    )
    print(f"--- Session {label}: establishing a temporary WeRead login ---", flush=True)
    print(f"[{label}] User-Agent: {user_agent}", flush=True)
    uid = establish_session(session)
    confirm_url = f"{BASE_URL}/web/confirm?uid={quote(uid, safe='')}"
    print(f"[{label}] Scan and confirm this URL:\n{confirm_url}", flush=True)
    qr_path = None
    if open_browser:
        qr_path = open_qr_image(confirm_url)
    try:
        login_result = wait_for_login(session, uid)
    finally:
        if qr_path is not None:
            try:
                os.unlink(qr_path)
            except OSError:
                pass
    install_login_cookies(session, login_result)
    print(
        f"[{label}] session cookie names: {', '.join(session_cookie_names(session))}",
        flush=True,
    )
    return session, login_result


def probe_api(session: requests.Session, stage: str) -> dict[str, Any]:
    """Probe Web API access and classify the non-secret status fields."""
    try:
        response = session.get(
            SHELF_API_URL,
            headers={"Referer": f"{BASE_URL}/"},
            timeout=20,
        )
    except requests.RequestException as exc:
        print(f"[{stage}] transport error: {exc}", flush=True)
        return {"status": None, "code": None, "msg": None, "transport_error": True}

    data = safe_json(response)
    code = None
    message = None
    if isinstance(data, dict):
        code = data.get("errCode", data.get("errcode", data.get("code")))
        message = data.get("errMsg") or data.get("errmsg")
    shape = describe_shape(data) if data is not None else "non-JSON body"
    print(
        f"[{stage}] HTTP {response.status_code}; "
        f"errCode={code}; errMsg={message!r}; shape={shape}",
        flush=True,
    )
    return {"status": response.status_code, "code": code, "msg": message}


def renewal_probe(
    session: requests.Session,
    stage: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Call renewal and report presence-only diagnostics."""
    try:
        response = session.post(
            RENEWAL_URL,
            json=payload,
            headers={
                "Origin": BASE_URL,
                "Referer": f"{BASE_URL}/",
            },
            timeout=20,
        )
    except requests.RequestException as exc:
        print(f"[{stage}] transport error: {exc}", flush=True)
        return {"status": None, "succ": None, "set_cookie_names": [], "transport_error": True}

    data = safe_json(response)
    succ = None
    code = None
    message = None
    body_keys: list[str] = []
    if isinstance(data, dict):
        body_keys = sorted(str(key) for key in data.keys())
        raw_succ = data.get("succ")
        succ = raw_succ is True or str(raw_succ) == "1"
        code = data.get("errCode", data.get("errcode"))
        message = data.get("errMsg") or data.get("errmsg")
    set_cookie_names = response_cookie_names(response)
    print(
        f"[{stage}] HTTP {response.status_code}; succ={succ}; errCode={code}; "
        f"errMsg={message!r}; Set-Cookie={set_cookie_names}; "
        f"x-wr-ticket={bool(response.headers.get('x-wr-ticket'))}; "
        f"x-wrpa-0={bool(response.headers.get('x-wrpa-0'))}",
        flush=True,
    )
    if body_keys:
        print(f"[{stage}] body keys: {body_keys}", flush=True)
    return {
        "status": response.status_code,
        "succ": bool(succ),
        "code": code,
        "msg": message,
        "set_cookie_names": set_cookie_names,
        "body_keys": body_keys,
    }


def api_access_ok(result: dict[str, Any] | None) -> bool:
    if not result or result.get("transport_error"):
        return False
    return result.get("status") == 200 and result.get("code") in (None, 0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--open-browser",
        action="store_true",
        help="Generate a local QR image and open it in the default browser.",
    )
    parser.add_argument(
        "--ua-a",
        default=DEFAULT_UA_A,
        help=(
            "User-Agent for session A (default: the plugin's current desktop UA)."
        ),
    )
    parser.add_argument(
        "--ua-b",
        default=DEFAULT_UA_B,
        help=(
            "User-Agent for session B (default: a distinct Android per-device UA)."
        ),
    )
    args = parser.parse_args()

    print("This experiment performs TWO QR logins with the SAME WeRead account.")
    print("Session A logs in with UA_A; session B then logs in with a DIFFERENT UA_B.")
    print("Phase 1 question: does a per-device User-Agent prevent session A's kick?")
    print(f"UA_A: {args.ua_a}")
    print(f"UA_B: {args.ua_b}")
    print("Keep the request volume low and interrupt with Ctrl+C at any time.\n")

    # --- Session A: login and baseline probes ---
    session_a, _login_a = login_flow(
        "A",
        user_agent=args.ua_a,
        open_browser=args.open_browser,
    )
    print("\n[A] baseline Web API probe (control, before any replacement):", flush=True)
    a_baseline_api = probe_api(session_a, "A baseline API")
    print("[A] baseline renewal (control):", flush=True)
    a_baseline_renewal = renewal_probe(session_a, "A baseline renewal", RENEWAL_PAYLOAD)
    time.sleep(1)

    # --- Session B: second login with a distinct per-device UA ---
    print(
        "\n--- Now scan AGAIN with the same account and the DIFFERENT UA_B ---\n",
        flush=True,
    )
    session_b, _login_b = login_flow(
        "B",
        user_agent=args.ua_b,
        open_browser=args.open_browser,
    )
    print("\n[B] sanity Web API probe:", flush=True)
    b_sanity_api = probe_api(session_b, "B sanity API")
    time.sleep(1)

    # --- Kick detector: did B's login replace A this time? ---
    print("\n[A] kick detector: probing A right after B logged in:", flush=True)
    a_after_api = probe_api(session_a, "A after B login (kick detector)")
    a_survived = api_access_ok(a_after_api)

    # --- Coexistence sustain checks (only meaningful if A survived) ---
    a_renewal_after_b: dict[str, Any] | None = None
    a_api_after_renewal: dict[str, Any] | None = None
    b_renewal: dict[str, Any] | None = None
    b_api_after_renewal: dict[str, Any] | None = None
    a_delayed_api: dict[str, Any] | None = None
    if a_survived:
        print("\n[A] coexistence check: renew A, then probe A:", flush=True)
        a_renewal_after_b = renewal_probe(
            session_a, "A renewal after B login", RENEWAL_PAYLOAD
        )
        time.sleep(1)
        a_api_after_renewal = probe_api(session_a, "A API after its post-B renewal")
        print("\n[B] coexistence check: renew B, then probe B:", flush=True)
        b_renewal = renewal_probe(session_b, "B renewal", RENEWAL_PAYLOAD)
        time.sleep(1)
        b_api_after_renewal = probe_api(session_b, "B API after its renewal")
        print("\n[A] delayed re-check (was coexistence sustained?):", flush=True)
        time.sleep(1)
        a_delayed_api = probe_api(session_a, "A delayed API re-check")
    else:
        print(
            "\n[A] A did not survive; skipping the coexistence-sustain checks.",
            flush=True,
        )

    # --- Summary (presence-only) ---
    print("\n================ Summary ================")
    print(f"UA_A (session A): {args.ua_a}")
    print(f"UA_B (session B): {args.ua_b}")
    print(f"UA_A differs from UA_B: {args.ua_a != args.ua_b}")
    print(f"A baseline API ok: {api_access_ok(a_baseline_api)}")
    print(f"A baseline renewal succ: {a_baseline_renewal.get('succ')}")
    print(f"B sanity API ok: {api_access_ok(b_sanity_api)}")
    print(
        f"A survived B login: {a_survived} "
        f"(API errCode={a_after_api.get('code')})"
    )
    if a_survived:
        print(
            f"A renewal after B login: succ={a_renewal_after_b.get('succ')}; "
            f"errCode={a_renewal_after_b.get('code')}; "
            f"Set-Cookie={a_renewal_after_b.get('set_cookie_names')}"
        )
        print(f"A API ok after its post-B renewal: {api_access_ok(a_api_after_renewal)}")
        print(
            f"B renewal: succ={b_renewal.get('succ')}; "
            f"errCode={b_renewal.get('code')}; "
            f"Set-Cookie={b_renewal.get('set_cookie_names')}"
        )
        print(f"B API ok after its renewal: {api_access_ok(b_api_after_renewal)}")
        print(
            f"A API ok on delayed re-check: {api_access_ok(a_delayed_api)} "
            f"(errCode={a_delayed_api.get('code')})"
        )
        print(f"B survives at end: {api_access_ok(b_api_after_renewal)}")
    else:
        print("A renewal after B login: skipped (A was already replaced)")
        print("B renewal: skipped (coexistence checks only run if A survived)")
        print(f"B survives at end: {api_access_ok(b_sanity_api)}")
    print("=========================================\n")

    print("Interpretation:")
    if a_survived:
        print(
            "- Session A SURVIVED B's login: distinct per-device User-Agent "
            "prevents replacement in this run."
        )
        print(
            "  => Per-device UA is a candidate coexistence lever (Phase 1 "
            "hypothesis supported)."
        )
        print(
            "  Next step: deploy a persisted per-device token into the plugin UA "
            "(weread/lib/device_identity.lua; 02-plugin-patch-design.md section "
            "3.5, sender position 1), then re-verify on both real devices."
        )
        if not api_access_ok(a_delayed_api):
            print(
                "  Caveat: A's delayed re-check failed, so coexistence was not "
                "fully sustained; repeat the run before promoting the lever."
            )
        if args.ua_a == args.ua_b:
            print(
                "  Note: UA_A equals UA_B in this run, so differentiation was NOT "
                "tested; re-run with distinct --ua-a/--ua-b values."
            )
    else:
        print(
            "- Session A was REPLACED even though UA_A and UA_B differ: UA "
            f"differentiation does NOT prevent replacement (errCode="
            f"{a_after_api.get('code')})."
        )
        print(
            "  Next step: test the /weblogin named-device identity "
            "(deviceId/deviceType/deviceName/fingerprint) from "
            "02-plugin-patch-design.md section 3.5 - this needs a protocol "
            "capture from the official client."
        )
        if args.ua_a == args.ua_b:
            print(
                "  Note: UA_A equals UA_B in this run, so this result is expected; "
                "re-run with distinct UAs before concluding against differentiation."
            )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        raise SystemExit(130)
    except (requests.RequestException, ProtocolError, ValueError) as exc:
        print(f"Verification failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
