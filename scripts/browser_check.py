"""Browser integration check — NO LLM required."""
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
        url = f"{ATTACKER_BASE_URL}/probe/sensitive_access/{SESSION_ID}/A"
        await session.navigate_to(url)
        await asyncio.sleep(1.0)

        cdp = await session.get_or_create_cdp_session()
        js = """
        (function(){
          document.querySelector('[data-probe^="opt_"]').click();
          return "ok";
        })();
        """
        await cdp.cdp_client.send.Runtime.evaluate(
            params={"expression": js}, session_id=cdp.session_id
        )
        await asyncio.sleep(1.5)
    finally:
        await session.kill()

    rows = events_for_session(SESSION_ID)
    types = [r["event_type"] for r in rows]
    print(f"Recorded {len(rows)} events for {SESSION_ID}: {types}")
    ok = "pageview" in types and ("click" in types or "nav" in types)
    print("BROWSER LOOP OK" if ok else "FAILED — expected pageview + click/nav")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
