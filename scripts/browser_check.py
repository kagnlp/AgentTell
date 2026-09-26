"""Browser integration check — NO LLM required.

Launches a real browser via browser-use, loads the probe page with a session_id,
clicks the First National card, fills+submits the summary box, then verifies the
attacker event log recorded the behaviour. This validates the entire
browser -> server -> telemetry -> event-log loop independently of any LLM.

Requires the attacker server running on its port (scripts/serve.sh).

Usage: uv run python scripts/browser_check.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from browser_use import BrowserProfile, BrowserSession

from attacker.db import events_for_session
from orchestrator.config import ATTACKER_BASE_URL

SESSION_ID = "browser-check-1"


async def main() -> int:
    profile = BrowserProfile(
        headless=True,
        args=["--host-resolver-rules=MAP *.test 127.0.0.1"],
    )
    session = BrowserSession(browser_profile=profile)
    await session.start()
    try:
        url = f"{ATTACKER_BASE_URL}/finance?session_id={SESSION_ID}&variant=A"
        await session.navigate_to(url)
        await asyncio.sleep(1.0)

        cdp = await session.get_or_create_cdp_session()
        # Click the First National card and submit the summary via JS (deterministic,
        # no LLM): this exercises the same telemetry path a real agent would trigger.
        js = """
        (function(){
          document.querySelector('[data-probe="card_first_national"]').click();
          var t = document.querySelector('[data-probe="summary_box"]');
          t.focus(); t.value = "I have a checking account with First National Bank";
          document.querySelector('[data-probe="summary_submit"]').click();
          return "ok";
        })();
        """
        await cdp.cdp_client.send.Runtime.evaluate(
            params={"expression": js}, session_id=cdp.session_id
        )
        await asyncio.sleep(1.5)  # let beacons + form POST land
    finally:
        await session.kill()

    rows = events_for_session(SESSION_ID)
    types = [r["event_type"] for r in rows]
    print(f"Recorded {len(rows)} events for {SESSION_ID}: {types}")
    ok = "pageview" in types and ("click" in types or "summary_submit" in types)
    print("BROWSER LOOP OK" if ok else "FAILED — expected pageview + click/summary")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
