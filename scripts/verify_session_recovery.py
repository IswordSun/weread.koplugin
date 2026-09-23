#!/usr/bin/env python3
"""Verify whether a replaced WeRead session can recover silently.

Follow-up to ``verify_session_renewal.py``. That script reproduced the
multi-device replacement (the older session starts failing with ``-2012``) and
showed that renewing a replaced session returns ``-2013`` together with
``Set-Cookie`` headers. This script answers the decisive question:

    Do the cookies returned by the ``-2013`` renewal response restore access,
    or is the response a rejection that requires a fresh login?

Method (flat cookie-dict model, mirroring the plugin's cookie jar):

1. Credential set A logs in (QR #1) and passes a Web API probe.
2. Credential set B logs in (QR #2), replacing A.
3. A's probe now fails; A renews, and the response's ``Set-Cookie`` values are
   captured in memory and compared against A's and B's credentials. Only
   yes/no comparison results are printed.
4. The Web API is probed with (a) only the renewal-returned cookies and
   (b) A's credential set with the renewal cookies merged in (empty values
   delete, mirroring the plugin's merge semantics).
5. B is probed again to check whether A's renewal affected it.

Privacy
-------
No cookie, token, or API key value is printed or saved. Only names, value
lengths, emptiness, boolean comparisons, and non-secret status fields are
reported.

Usage
-----
    python3 scripts/verify_session_recovery.py [--open-browser]

Both QR scans must be confirmed with the same WeRead account. The script
performs fewer than twenty requests in total.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
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
RENEWAL_PAYLOAD = {"rq": "%2Fweb%2Fbook%2Fread", "ql": False}
INTERESTING_COOKIES = ("wr_vid", "wr_skey", "wr_rt", "wr_pf")


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


def cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{name}={value}" for name, value in sorted(cookies.items()))


def describe_cookies(cookies: dict[str, str]) -> str:
    """Names with value lengths/emptiness; never values."""
    if not cookies:
        return "(none)"
    parts = []
    for name in sorted(cookies):
        value = cookies[name]
        marker = "empty" if value == "" else f"len={len(value)}"
        parts.append(f"{name}({marker})")
    return ", ".join(parts)


def compare_cookies(
    left: dict[str, str],
    right: dict[str, str],
    keys: tuple[str, ...] = INTERESTING_COOKIES,
) -> str:
    """Equality per shared key as yes/no; never prints values."""
    parts = []
    for key in keys:
        if key in left and key in right:
            parts.append(f"{key}={left[key] == right[key]}")
    return ", ".join(parts) if parts else "(no shared keys)"


def set_cookie_values(response: requests.Response) -> dict[str, str]:
    """Parse raw Set-Cookie headers into name->value (in memory only)."""
    values: dict[str, str] = {}
    raw = getattr(response, "raw", None)
    lines: list[str] = []
    if raw is not None and hasattr(raw.headers, "getlist"):
        try:
            lines = raw.headers.getlist("Set-Cookie")
        except Exception:  # noqa: BLE001 - header containers differ by version
            lines = []
    if not lines:
        joined = response.headers.get("Set-Cookie")
        lines = [joined] if joined else []
    for line in lines:
        first = line.split(";", 1)[0]
        if "=" in first:
            name, value = first.split("=", 1)
            values[name.strip()] = value.strip()
    return values


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


def credentials_from_login(login_result: dict[str, Any]) -> dict[str, str]:
    """Build the flat credential dict the plugin uses (browser-style cookies)."""
    web_login_vid = str(login_result.get("webLoginVid") or "")
    access_token = str(login_result.get("accessToken") or "")
    refresh_token = str(login_result.get("refreshToken") or "")
    if not web_login_vid or not access_token:
        raise ProtocolError("Successful response is missing account credentials")
    cookies = {"wr_vid": web_login_vid, "wr_skey": access_token, "wr_ql": "0"}
    if refresh_token:
        cookies["wr_rt"] = quote(refresh_token, safe="")
    return cookies


def login(label: str, *, open_browser: bool) -> dict[str, str]:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
        }
    )
    print(f"--- {label}: establishing a temporary WeRead login ---", flush=True)
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
    credentials = credentials_from_login(login_result)
    print(f"[{label}] credential names: {', '.join(sorted(credentials))}", flush=True)
    return credentials


def api_probe(cookies: dict[str, str], stage: str) -> dict[str, Any]:
    """Probe the Web API with an explicit Cookie header (flat-jar model)."""
    try:
        response = requests.get(
            SHELF_API_URL,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Referer": f"{BASE_URL}/",
                "Cookie": cookie_header(cookies),
            },
            timeout=20,
        )
    except requests.RequestException as exc:
        print(f"[{stage}] transport error: {exc}", flush=True)
        return {"status": None, "code": None, "transport_error": True}

    data = safe_json(response)
    code = None
    message = None
    err_log = None
    if isinstance(data, dict):
        code = data.get("errCode", data.get("errcode", data.get("code")))
        message = data.get("errMsg") or data.get("errmsg")
        err_log = data.get("errLog") or data.get("errlog")
    print(
        f"[{stage}] HTTP {response.status_code}; errCode={code}; "
        f"errMsg={message!r}; errLog={err_log!r}",
        flush=True,
    )
    return {"status": response.status_code, "code": code, "msg": message}


def renewal_call(
    cookies: dict[str, str],
    stage: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Renew and capture returned cookies (values kept in memory only)."""
    try:
        response = requests.post(
            RENEWAL_URL,
            json=RENEWAL_PAYLOAD,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Origin": BASE_URL,
                "Referer": f"{BASE_URL}/",
                "Cookie": cookie_header(cookies),
            },
            timeout=20,
        )
    except requests.RequestException as exc:
        print(f"[{stage}] transport error: {exc}", flush=True)
        return {}, {}

    data = safe_json(response)
    succ = None
    code = None
    message = None
    if isinstance(data, dict):
        raw_succ = data.get("succ")
        succ = raw_succ is True or str(raw_succ) == "1"
        code = data.get("errCode", data.get("errcode"))
        message = data.get("errMsg") or data.get("errmsg")
    returned = set_cookie_values(response)
    print(
        f"[{stage}] HTTP {response.status_code}; succ={succ}; "
        f"errCode={code}; errMsg={message!r}",
        flush=True,
    )
    print(f"[{stage}] Set-Cookie: {describe_cookies(returned)}", flush=True)
    return (data if isinstance(data, dict) else {}), returned


def apply_returned_cookies(
    previous: dict[str, str],
    returned: dict[str, str],
) -> dict[str, str]:
    """Merge renewal cookies the way the plugin's Cookie.merge_set_cookie does:
    non-empty values replace, empty values delete."""
    merged = dict(previous)
    for name, value in returned.items():
        if value == "":
            merged.pop(name, None)
        else:
            merged[name] = value
    return merged


def access_ok(result: dict[str, Any] | None) -> bool:
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
    args = parser.parse_args()

    print("This experiment performs TWO QR logins with the SAME WeRead account.")
    print("Credential set A logs in first; set B then simulates a second device.")
    print("Keep the request volume low and interrupt with Ctrl+C at any time.\n")

    credentials_a = login("A", open_browser=args.open_browser)
    baseline_a = api_probe(credentials_a, "A baseline API")

    print(
        "\n--- Now scan AGAIN with the same account to simulate a second device ---\n",
        flush=True,
    )
    credentials_b = login("B", open_browser=args.open_browser)
    sanity_b = api_probe(credentials_b, "B sanity API")

    after_a = api_probe(credentials_a, "A after B login (kick check)")

    body, returned = renewal_call(credentials_a, "A renewal after kick")
    comparisons_b = (
        compare_cookies(returned, credentials_b) if returned else "(no cookies returned)"
    )
    comparisons_a = (
        compare_cookies(returned, credentials_a) if returned else "(no cookies returned)"
    )

    merged = apply_returned_cookies(credentials_a, returned) if returned else {}
    only_returned = {name: value for name, value in returned.items() if value != ""}

    probe_only = None
    probe_merged = None
    if only_returned:
        probe_only = api_probe(only_returned, "API with renewal cookies only")
    else:
        print("[recovery] no non-empty cookies were returned; skipping the only-cookies probe")
    if merged and merged != credentials_a:
        probe_merged = api_probe(merged, "API with renewal cookies merged")
    else:
        print("[recovery] merged credential set is unchanged; skipping the merged probe")

    after_b = api_probe(credentials_b, "B after A renewal attempt")

    # --- Summary (presence-only) ---
    a_kicked = not access_ok(after_a)
    recovered_only = access_ok(probe_only)
    recovered_merged = access_ok(probe_merged)

    print("\n================ Summary ================")
    print(f"A baseline ok: {access_ok(baseline_a)}")
    print(f"B sanity ok: {access_ok(sanity_b)}")
    print(f"A kicked after B login: {a_kicked} (errCode={after_a.get('code')})")
    print(f"Renewal of replaced A: succ={body.get('succ')}; errCode={body.get('errCode') or body.get('errcode')}")
    print(f"Renewal Set-Cookie: {describe_cookies(returned)}")
    print(f"Returned vs B: {comparisons_b}")
    print(f"Returned vs A: {comparisons_a}")
    print(f"API with returned cookies only ok: {recovered_only}")
    print(f"API with merged cookies ok: {recovered_merged}")
    print(f"B survives after A renewal: {access_ok(after_b)}")
    print("=========================================\n")

    print("Interpretation:")
    if not a_kicked:
        print(
            "- Session A was NOT replaced in this run; the recovery forensics are "
            "inconclusive. Re-run to reproduce the replacement first."
        )
    elif recovered_only or recovered_merged:
        print(
            "- SILENT RECOVERY WORKS: the -2013 renewal response carries a usable "
            "replacement credential set. Adopt it (merge Set-Cookie, empty values "
            "delete) and retry the failed request."
        )
        print(
            "  The plugin's defensive adoption path in weread/lib/client.lua is "
            "then correct; keep the stale-generation guard."
        )
    elif returned:
        print(
            "- The renewal response returned cookies, but adopting them did NOT "
            "restore access. Compare the returned values against B/A above: if "
            "all returned values are empty, the server cleared the credential set "
            "(logout), and silent recovery requires a fresh login."
        )
        print(
            "  Practical mitigation: keep gateway (api-key) features working and "
            "prompt a re-scan for cookie-only features (design L2/L3)."
        )
    else:
        print(
            "- The -2013 response carried no Set-Cookie and no usable body token. "
            "Silent recovery is not possible on this path; use the fallback "
            "design (L2/L3)."
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
