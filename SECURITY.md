# Security Policy

This document describes the threat model of `llm-relay` and how to report a
vulnerability. It is deliberately specific: read it before exposing the relay
beyond your own machine.

## Design boundaries

`llm-relay` is a single-user, single-host tool. Out of the box it listens on
loopback only:

- relay: `127.0.0.1:9110`
- dashboard: `127.0.0.1:9111`

Binding either of them to a LAN interface is an explicit choice (`--host 0.0.0.0`). Non-loopback
dashboard visitors still need the access key and remain read-only, but you are the one widening
the surface — a container or NAS deployment is the usual way this leaks, so do it deliberately.

There is **no TLS**, **no multi-tenancy**, and the project is **not designed to
be exposed to the public internet**. If you choose to expose it publicly, you
must put your own reverse proxy and authentication layer in front of it. That
is your responsibility and your risk.

## Secret handling

- Provider keys are read only from **environment variables** or a local file
  (`keys.env`; `chmod 600 keys.env` is recommended).
- Secrets are **never** stored in the repository. `config.json`, `keys.env`,
  and `access-key.txt` are all listed in `.gitignore`.
- Logs and dashboard responses **never** contain plaintext keys.

## Dashboard access

- **Loopback** requests are password-free.
- **Non-loopback** requests (LAN, phone) require an access key, supplied either
  as `?k=…` or via a 30-day cookie.
- **Remote access is read-only.** Every write operation (changing config,
  restarting the service, key management) is allowed from loopback only.

## Caller authentication (`auth.mode`)

Two modes are available:

- `loopback_trust` (default): loopback callers are trusted without a caller
  key. This is suitable only for single-machine, personal use.
- `require_key`: every caller, from any source, must present a caller key.
  **Use this mode when deploying on a LAN or across multiple machines.**

Callers can be isolated with per-caller `rpm`, `daily_tokens`, and
`allow_routes` limits:

- over a quota → `429`
- route outside the allowlist → `403`
- unknown key → `401`

## Reporting a vulnerability

Please use this repository's GitHub **Private vulnerability reporting**
(Security → Report a vulnerability). **Do not** open a public issue that
contains keys, internal addresses, or exploit details.

In your report, include:

- the version / commit you tested,
- clear reproduction steps,
- the impact you believe it has.

## Supported versions

Only the latest revision of the `main` branch is supported. Compliance with the
terms of service of any upstream provider whose free tier you route through is
the deployer's own responsibility; this project makes no guarantee about the
availability of any upstream service.
