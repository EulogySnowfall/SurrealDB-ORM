# Security Policy

## Supported Versions

Two release lines are maintained, one per SurrealDB major version:

| Version     | SurrealDB   | Branch | Supported          |
| ----------- | ----------- | ------ | ------------------ |
| **0.30.x+** | >= 3.0      | `main` | :white_check_mark: |
| **0.21.x**  | 2.6.x       | `v2`   | :warning:          |
| < 0.21.x    | -           | -      | :x:                |

> [!WARNING]
> :warning: The `v2` branch is **deprecated** (security & bug fixes only) in favor of
> [SurrealDB-ORM-lite](https://github.com/EulogySnowfall/SurrealDB-ORM-lite), which supports
> SurrealDB 2.x via the official SurrealDB Python SDK v2. The `v2` branch receives **security and
> bug fixes only** until SurrealDB-ORM-lite reaches **v0.20.0**, after which it will be retired.

## Versioning Scheme

We follow a 3-digit versioning scheme: **X.Y.Z**

| Digit | Purpose                                   | Example        |
| ----- | ----------------------------------------- | -------------- |
| X     | LTS / breaking changes                    | 1.0.0          |
| Y     | Feature updates                           | 0.8.0 -> 0.9.0 |
| Z     | Bug fixes, security & dependency patches  | 0.9.0 -> 0.9.1 |

Security patches automatically bump the **patch (Z)** digit via CI workflows.

## Reporting a Vulnerability

If you discover a security vulnerability in SurrealDB-ORM, please report it responsibly:

1. **Do NOT** open a public GitHub issue for security vulnerabilities
2. **Email** the maintainer directly at: <croteau.yannick@gmail.com>
3. Include:
   - Description of the vulnerability
   - Steps to reproduce
   - Potential impact
   - Suggested fix (if any)

### Response Timeline

- **Acknowledgment**: Within 48 hours
- **Initial Assessment**: Within 7 days
- **Fix Timeline**: Depends on severity (critical: ASAP, high: 14 days, medium: 30 days)

### What to Expect

- We will acknowledge receipt of your report
- We will investigate and validate the vulnerability
- We will work on a fix and coordinate disclosure
- Credit will be given to reporters (unless anonymity is requested)
