# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | Yes       |

## Reporting a vulnerability

If you discover a security vulnerability, please report it privately:

1. **Do not** open a public GitHub issue
2. Email the maintainers or use [GitHub's private vulnerability reporting](https://github.com/estampo/bambox/security/advisories/new)
3. Include steps to reproduce, impact assessment, and any suggested fix

We will acknowledge reports within 48 hours and aim to release a fix within 7 days for critical issues.

## Scope

bambox handles:
- Archive construction (ZIP files with XML metadata)
- Credential loading for cloud printing
- Docker container execution for slicing and bridge communication

Security-sensitive areas include credential handling in `bridge.py` and subprocess invocation in `cura.py`.
