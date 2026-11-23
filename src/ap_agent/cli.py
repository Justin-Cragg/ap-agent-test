"""Command line: ``ap-agent serve | poll | payment-run``."""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date

from ap_agent.config import get_settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="ap-agent")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="Run the HTTP API")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8080)
    poll = sub.add_parser("poll", help="Process new mailbox messages once (for cron/batch jobs)")
    poll.add_argument("--limit", type=int, default=25)
    pr = sub.add_parser("payment-run", help="Propose a payment run for approval")
    pr.add_argument("--date", type=date.fromisoformat)
    pr.add_argument("--next-run", type=date.fromisoformat)
    args = parser.parse_args()

    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("ap_agent.api:app", host=args.host, port=args.port)
        return

    from ap_agent.pipeline import APService

    svc = APService(settings)
    if args.cmd == "poll":
        print(json.dumps(svc.poll_mailbox(args.limit), indent=2, default=str))
    elif args.cmd == "payment-run":
        print(json.dumps(svc.propose_payment_run(args.date, args.next_run).__dict__, indent=2, default=str))


if __name__ == "__main__":
    main()
