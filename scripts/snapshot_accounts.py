"""Read-only snapshot of the 49 TGR responder accounts for bridge49.

Reads Account rows of business 51 with a RESPONDER_OUTREACH policy and emits a
JSON inventory. SELECT only; no writes, no Telegram, no restarts.
"""
import asyncio
import json
import os
import sys

import asyncpg

QUERY = """
SELECT a.id,
       a.label,
       a.role,
       a.business_id,
       a.program_id,
       p.code           AS program_code,
       a.runtime_state,
       a.desired_state,
       a.is_active,
       a.last_heartbeat_at,
       a.last_seen_at,
       a.business_binding_epoch,
       a.config -> 'RESPONDER_OUTREACH'      AS outreach,
       a.config -> 'RESPONDER_WORKER_ACTIONS' AS worker_actions
FROM account a
LEFT JOIN program p ON p.id = a.program_id
WHERE a.business_id = 51
  AND a.platform_id = 1
ORDER BY a.id
"""


async def main() -> int:
    dsn = (
        f"postgresql://{os.environ['RADAR_ANALYST_RO_USER']}:"
        f"{os.environ['RADAR_ANALYST_RO_PASSWORD']}@"
        f"{os.environ['RADAR_ANALYST_RO_HOST']}:"
        f"{os.environ['RADAR_ANALYST_RO_PORT']}/"
        f"{os.environ['RADAR_ANALYST_RO_DATABASE']}"
    )
    conn = await asyncpg.connect(dsn, ssl=False, statement_cache_size=0, timeout=20)
    try:
        rows = await conn.fetch(QUERY)
    finally:
        await conn.close()

    out = []
    for row in rows:
        record = dict(row)
        for key in ("last_heartbeat_at", "last_seen_at"):
            if record[key] is not None:
                record[key] = record[key].isoformat()
        for key in ("outreach", "worker_actions"):
            if isinstance(record[key], str):
                record[key] = json.loads(record[key])
        out.append(record)

    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(f"\n-- rows: {len(out)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
