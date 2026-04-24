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
- Docker container execution for CuraEngine slicing (`cura.py`)

Security-sensitive areas include subprocess invocation in `cura.py`.

Cloud printing, credential handling, and bridge communication are out of scope
here — those live in [boo-cloud](https://github.com/estampo/boo-cloud).
