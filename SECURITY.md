# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | Yes       |
| < 0.1   | No        |

## Reporting a vulnerability

**Please do not open public GitHub issues for security vulnerabilities.**

Email the maintainers (see repository contact) with:

- Description of the issue
- Steps to reproduce
- Impact assessment
- Suggested fix (if any)

We aim to acknowledge within 72 hours and provide a remediation timeline for confirmed issues.

## Scope

In scope: Ordine application code (CLI, core, web UI, default playbooks). Out of scope: third-party provider APIs (OpenAI, Anthropic), misconfiguration (binding serve to `0.0.0.0` without auth), or malicious playbooks the operator chose to run.

See [docs/security.md](docs/security.md) for the product security posture.
