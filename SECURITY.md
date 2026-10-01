# Security Policy

## Supported Versions

Two release lines are actively maintained, one per SurrealDB major version:

| Version     | SurrealDB     | Branch | Supported          |
| ----------- | ------------- | ------ | ------------------ |
| **0.34.x**  | 3.2.x – 3.3.x | `main` | :white_check_mark: |
| **0.21.x**  | 2.6.x – 2.7.x | `v2`   | :white_check_mark: |
| any other   | -             | -      | :x:                |

`main` supports **SurrealDB 3.2.x – 3.3.x** (tested against 3.3.0 as of ORM
0.34.0); 3.1.x and earlier are not supported there. Security fixes on `main`
ship in the current minor only — upgrade from 0.33.x to 0.34.x, which needs no
server upgrade. The `v2` line supports SurrealDB 2.6.x – 2.7.x (tested against
2.7.0 as of 0.21.8) and receives security and bug fixes only.

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
