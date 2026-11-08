# Security Policy

## Supported Versions

One release line is maintained:

| Version     | SurrealDB     | Branch | Supported          |
| ----------- | ------------- | ------ | ------------------ |
| **0.39.x**  | 3.2.x – 3.3.x | `main` | :white_check_mark: |
| **0.21.x**  | 2.6.x – 2.7.x | `v2`   | :x: (unmaintained) |
| any other   | -             | -      | :x:                |

`main` supports **SurrealDB 3.2.x – 3.3.x** (tested against 3.3.5 as of ORM
0.35.7); 3.1.x and earlier are not supported there. Security fixes on `main`
ship in the current minor only — upgrade from 0.38.x to 0.39.x, which needs no
server upgrade.

The `v2` line (0.21.x, SurrealDB 2.x) is **no longer maintained from 0.39.0**:
0.21.62 is its last release and it receives no further fixes, security fixes
included. Users of SurrealDB 2.x should move to
[SurrealDB-ORM-lite](https://github.com/EulogySnowfall/SurrealDB-ORM-lite), and
report vulnerabilities affecting SurrealDB 2.x there.

`main` moves to SurrealDB 4.x with 0.40.0. SurrealDB 3.x support then continues
on a permanent `v3` branch, cut from `main` right after 0.39.0, which receives
bug and security fixes only; this table will list it once it has a release.

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
