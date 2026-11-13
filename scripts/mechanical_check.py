#!/usr/bin/env python3
"""Verify that no probe option is behind a login wall, so completion carries no signal."""
from __future__ import annotations

import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urljoin

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.scenarios import SCENARIOS as REGISTRY  # noqa: E402
from harness.scenarios import get_scenario  # noqa: E402
from orchestrator.config import ATTACKER_BASE_URL as _ATTACKER_BASE_URL  # noqa: E402
from orchestrator.config import ATTACKER_HOST  # noqa: E402

ATTACKER_BASE_URL = _ATTACKER_BASE_URL.replace(ATTACKER_HOST, "127.0.0.1")

SCENARIOS = list(REGISTRY)


PLANTED_COOKIE = "fnb_session=x; " + "; ".join(
    f"svc_{lid}=x" for lid in sorted(
        {lid for sc in REGISTRY.values() for lid in sc.label_ids}))


ANCHOR = re.compile(r'data-probe="(pay|opt)_([^"]+)"\s+href="([^"]+)"')
RADIO = re.compile(r'name="to"\s+value="([^"]+)"\s+data-probe="opt_[^"]+"')
GATED = ("/pay", "/continue")
SUCCESS_MARK = "go-complete"


def http_get(url: str, cookie: str | None = None) -> tuple[int, str]:
    req = urllib.request.Request(url)
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def check_scenario(key: str) -> tuple[bool, str]:
    sc = get_scenario(key)
    purl = f"{ATTACKER_BASE_URL}/probe/{key}/mech-{key}?variant=A"
    if sc.surfaces:
        purl = f"{ATTACKER_BASE_URL}/probe/{key}/mech-{key}/A/{sc.plant_ids[0]}"
    status, html = http_get(purl)
    if status != 200:
        return False, f"probe returned HTTP {status}"
    anchors = [(oid, href) for _, oid, href in ANCHOR.findall(html)]
    if not anchors:
        anchors = [(oid, f"/go?session_id=mech-{key}&to={oid}&scenario={key}")
                   for oid in RADIO.findall(html)]
    if not anchors:
        return False, "no options found on the probe (neither links nor a choice form)"

    label_ids = set(sc.label_ids)
    diag = [(oid, href) for oid, href in anchors if oid in label_ids]
    if not diag:
        return False, "no diagnostic options found"

    gated = [oid for oid, href in anchors if any(g in href for g in GATED)]
    if gated:
        return False, f"still gated — options route through a wall: {gated}"

    for oid, href in diag:
        full = urljoin(ATTACKER_BASE_URL + "/", href)
        s_no, b_no = http_get(full)
        s_yes, b_yes = http_get(full, cookie=PLANTED_COOKIE)
        ok = (s_no == s_yes == 200 and SUCCESS_MARK in b_no and b_no == b_yes)
        if not ok:
            return False, (f"option {oid!r} differs by session "
                           f"(no-session HTTP {s_no}, session HTTP {s_yes})")
    return True, f"{len(diag)} options, all complete identically with/without a session"


def main() -> None:
    print(f"Mechanical-channel check against {ATTACKER_BASE_URL}\n"
          f"(brute-force: does holding a session change which option completes?)\n")
    all_ok = True
    for key in SCENARIOS:
        try:
            ok, msg = check_scenario(key)
        except urllib.error.URLError as e:
            print(f"  ✗ {key:22} could not reach server: {e} (are the servers up?)")
            all_ok = False
            continue
        print(f"  {'✓' if ok else '✗'} {key:22} {msg}")
        all_ok = all_ok and ok
    print()
    if all_ok:
        print("PASS — no option is gated; completion is constant across sessions.\n"
              "The mechanical channel is dead: only the agent's reasoning can leak the secret.")
    else:
        print("FAIL — at least one option still depends on a session (a wall remains).")
        sys.exit(1)


if __name__ == "__main__":
    main()
