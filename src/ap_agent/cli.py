"""Command line.

    ap-agent serve                 HTTP API + approval/review UI
    ap-agent migrate               apply database migrations
    ap-agent poll                  enqueue new mail and process the queue (scheduled job)
    ap-agent work                  process queued jobs
    ap-agent payment-run           propose a payment run for approval
    ap-agent execute-payments      payment executor: record approved payments in the ERP
                                   (deploy with its own ERP credentials)
    ap-agent apikey create|revoke  manage scoped API keys
    ap-agent audit-verify          check the audit hash chain
    ap-agent purge                 delete documents past the retention period
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from ap_agent.config import get_settings
from ap_agent.observability import setup_logging


def main() -> None:
    parser = argparse.ArgumentParser(prog="ap-agent")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8080)
    sub.add_parser("migrate")
    poll = sub.add_parser("poll")
    poll.add_argument("--limit", type=int, default=50)
    work = sub.add_parser("work")
    work.add_argument("--max-jobs", type=int, default=200)
    pr = sub.add_parser("payment-run")
    pr.add_argument("--entity")
    pr.add_argument("--date", type=date.fromisoformat)
    pr.add_argument("--next-run", type=date.fromisoformat)
    sub.add_parser("execute-payments")
    key = sub.add_parser("apikey")
    key.add_argument("action", choices=["create", "revoke"])
    key.add_argument("--name", required=True)
    key.add_argument("--scope", action="append", default=[])
    key.add_argument("--entity", action="append")
    sub.add_parser("audit-verify")
    sub.add_parser("purge")
    args = parser.parse_args()

    settings = get_settings()
    setup_logging(settings)

    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("ap_agent.api:app", host=args.host, port=args.port, log_config=None)
        return
    if args.cmd == "migrate":
        from ap_agent.storage.migrate import upgrade

        upgrade(settings.database_url)
        print("migrated")
        return

    from ap_agent.pipeline import APService

    svc = APService(settings)
    out: object
    if args.cmd == "poll":
        out = {"queued": svc.poll_mailboxes(args.limit), "results": svc.work()}
    elif args.cmd == "work":
        out = svc.work(max_jobs=args.max_jobs)
    elif args.cmd == "payment-run":
        entity = args.entity or next(iter(svc.runtimes))
        svc.db.enqueue("propose_payment_run", {"entity": entity, "payment_date": args.date.isoformat() if args.date else None, "next_run_date": args.next_run.isoformat() if args.next_run else None}, entity=entity)
        out = svc.work(kinds=["propose_payment_run"])
    elif args.cmd == "execute-payments":
        out = svc.work(kinds=["execute_payment_run"])
    elif args.cmd == "apikey":
        from ap_agent.auth import create_api_key, revoke_api_key

        if args.action == "create":
            out = {"name": args.name, "key": create_api_key(svc.db, args.name, args.scope, "cli", args.entity), "note": "Shown once."}
        else:
            out = {"revoked": revoke_api_key(svc.db, args.name, "cli")}
    elif args.cmd == "audit-verify":
        ok, n, problem = svc.db.verify_audit()
        out = {"ok": ok, "rows": n, "problem": problem}
        if not ok:
            print(json.dumps(out))
            sys.exit(2)
    elif args.cmd == "purge":
        out = {"purged": svc.purge_expired("cli")}
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
