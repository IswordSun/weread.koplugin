#!/usr/bin/env python3
"""Verify WeRead web-session replacement and renewal recovery.

Background
----------
A newer QR login replaces the account's previous web session; this is the
reported cause of "logging in on a second device kicks the first one offline"
(upstream issue #158). The official client treats server error ``-2013``
("鉴权失败") as a recoverable session error when the response carries a
replacement session key, and otherwise requires a fresh login.

Experiment
----------
1. Session A logs in, verifies baseline Web API access, and renews once.
2. Session B logs in to simulate a second device (optionally with a different
   User-Agent via ``--second-ua`` to test the device-differentiation idea).
3. Session A is re-probed: Web API access, then cookie renewal with three
   payload variants, looking for a replacement session key that would allow
   silent recovery.

Privacy
-------
No cookies, tokens, or API keys are printed or saved. Only presence, type,
length, cookie *names*, and non-secret status fields (``succ``/``errCode``/
``errMsg``) are reported.

Usage
-----
    python3 scripts/verify_session_renewal.py [--open-browser] [--second-ua UA]

Both QR scans must be confirmed with the same WeRead account. The script
performs fewer than twenty requests in total; keep the volume low.
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
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Safari/537.36 Edg/135.0.0.0"
)
RENEWAL_PAYLOADS: list[tuple[str, dict[str, Any]]] = [
    ("rq+ql=false", {"rq": "%2Fweb%2Fbook%2Fread", "ql": False}),
    ("rq+ql=true", {"rq": "%2Fweb%2Fbook%2Fread", "ql": True}),
    ("rq-only", {"rq": "%2Fweb%2Fbook%2Fread"}),
]
REPLACEMENT_KEY_COOKIES = {"wr_skey", "wr_gid"}


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
    second_ua: str | None,
    open_browser: bool,
) -> tuple[requests.Session, dict[str, Any]]:
    session = requests.Session()
    user_agent = second_ua or USER_AGENT
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


def api_access_ok(result: dict[str, Any]) -> bool:
    if result.get("transport_error"):
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
        "--second-ua",
        default="",
        help=(
            "Use a different User-Agent for the second login to test whether "
            "device differentiation prevents the replacement."
        ),
    )
    args = parser.parse_args()

    print("This experiment performs TWO QR logins with the SAME WeRead account.")
    print("Session A logs in first; session B then simulates a second device login.")
    print("Keep the request volume low and interrupt with Ctrl+C at any time.\n")

    # --- Session A: login, baseline probes ---
    session_a, _login_a = login_flow(
        "A",
        second_ua=None,
        open_browser=args.open_browser,
    )
    print("\n[A] baseline Web API probe (control, before any replacement):", flush=True)
    a_baseline_api = probe_api(session_a, "A baseline API")
    print("[A] baseline renewal (control):", flush=True)
    a_baseline_renewal = renewal_probe(session_a, "A baseline renewal", RENEWAL_PAYLOADS[0][1])
    time.sleep(1)

    # --- Session B: second login (simulated second device) ---
    print(
        "\n--- Now scan AGAIN with the same account to simulate a second device login ---\n",
        flush=True,
    )
    session_b, _login_b = login_flow(
        "B",
        second_ua=args.second_ua or None,
        open_browser=args.open_browser,
    )
    print("\n[B] sanity Web API probe:", flush=True)
    b_api = probe_api(session_b, "B API")
    time.sleep(1)

    # --- Re-probe session A: was it replaced? Can it recover? ---
    print("\n[A] re-probing the replaced session:", flush=True)
    a_after_api = probe_api(session_a, "A after B login: API")
    a_renewals: list[tuple[str, dict[str, Any]]] = []
    for label, payload in RENEWAL_PAYLOADS:
        result = renewal_probe(session_a, f"A renewal ({label})", payload)
        a_renewals.append((label, result))
        if result.get("succ"):
            break
        time.sleep(1)
    print("[A] post-renewal Web API probe (tests silent recovery):", flush=True)
    a_post_api = probe_api(session_a, "A post-renewal: API")

    # --- Summary (presence-only) ---
    a_kicked = not api_access_ok(a_after_api)
    recovered = api_access_ok(a_post_api)
    replacement_seen = any(
        REPLACEMENT_KEY_COOKIES & set(result.get("set_cookie_names", []))
        for _label, result in a_renewals
        if not result.get("succ")
    )
    renamed = bool(args.second_ua)

    print("\n================ Summary ================")
    print(f"A baseline API ok: {api_access_ok(a_baseline_api)}")
    print(f"A baseline renewal succ: {a_baseline_renewal.get('succ')}")
    print(f"B API ok: {api_access_ok(b_api)}")
    print(f"A kicked after B login: {a_kicked} (API errCode={a_after_api.get('code')})")
    for label, result in a_renewals:
        print(
            f"A renewal ({label}): succ={result.get('succ')}; "
            f"errCode={result.get('code')}; Set-Cookie={result.get('set_cookie_names')}"
        )
    print(f"A silent recovery after renewal: {recovered}")
    print(f"Replacement-key cookies seen on a failed renewal: {replacement_seen}")
    print(f"Second login used a different User-Agent: {renamed}")
    print("=========================================\n")

    print("Interpretation:")
    if not a_kicked:
        print(
            "- The second login did NOT replace session A in this configuration."
        )
        if renamed:
            print(
                "  A different User-Agent was used for session B; re-run without "
                "--second-ua to compare. If the replacement only happens with the "
                "same UA, per-device UA is a candidate coexistence lever."
            )
    elif recovered:
        print(
            "- Session A was replaced but silently recovered via renewal. Mirror "
            "this flow in weread/lib/client.lua (renew_session) with the session "
            "generation guard from 02-plugin-patch-design.md."
        )
    else:
        print(
            "- Session A was replaced and could not recover silently. The practical "
            "mitigation is the fallback design: keep API-key (gateway) features "
            "working and prompt a re-scan for cookie-only features "
            "(see 02-plugin-patch-design.md, layers L2/L3)."
        )
    if replacement_seen and not recovered:
        print(
            "- A replacement-key cookie appeared but the session still failed; "
            "capture the exact key field and ordering before implementing adoption."
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
