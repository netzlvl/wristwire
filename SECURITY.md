# Security policy

WristWire drives a phone and hands watch messages to an agent that can use tools, so security
reports are taken seriously.

## Reporting a vulnerability

Please **do not open a public issue**. Use GitHub's private reporting instead:
**Security → Report a vulnerability** on this repository. Include what you found, how to reproduce
it, and what an attacker could do with it. You should hear back within a few days.

## In scope

- Bypassing the sender allowlist, HMAC device signatures, or nonce/replay checks
- Getting a Tier C action to run without its bound approval code
- Prompt text from the watch overriding tool-approval policy
- Secrets leaking into logs, the audit trail, replies, or the relay database

## Hardening reminders for operators

- Keep the relay bound to `127.0.0.1` or a Tailscale address; never expose port 8788 to the internet.
- Generate fresh secrets with `./deploy.sh init-repo` and never commit `.env`.
- Use a dedicated phone and a number you can afford to lose for the Garmin Messenger side.
- Keep `AGENT_CLI_ALLOWED_TOOLS` as narrow as the job allows.
