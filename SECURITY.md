# Security policy

## Supported versions

| Version | Supported |
| --- | --- |
| Latest published minor | Yes |
| Older minors | No |
| Unreleased development branch | Best effort |

## Reporting

Do not open a public issue for a suspected vulnerability or include secrets,
tokens, or private source code in a report. Use GitHub private vulnerability
reporting at
`https://github.com/mauhpr/constraintloop/security/advisories/new`.
If private vulnerability reporting is unavailable, email
`mauricio_perez_r@hotmail.com` with the subject prefix
`[SECURITY][ConstraintLoop]`.

Useful reports include affected versions, impact, reproduction steps, and a
minimal proof of concept. Reports are acknowledged on a best-effort basis; no
fixed remediation SLA is promised.

Hooks are guardrails, not a security sandbox. The reviewed contract and an
isolated CI run are the trusted enforcement boundary.
