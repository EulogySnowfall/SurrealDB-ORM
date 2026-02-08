# Security Policy

## Supported Versions

The following versions of SurrealDB-ORM are currently supported with security updates:

| Version | Supported          |
| ------- | ------------------ |
| 0.7.x   | :white_check_mark: |
| 0.6.x   | :white_check_mark: |
| 0.5.x   | :x:                |
| 0.4.x   | :x:                |
| 0.3.x   | :x:                |
| 0.2.x   | :x:                |
| 0.1.x   | :x:                |
| 0.0.x   | :x:                |

## Versioning Scheme

We follow a 3-digit versioning scheme: **X.Y.Z**

| Digit | Purpose                       | Example        |
| ----- | ----------------------------- | -------------- |
| X     | LTS / breaking changes        | 1.0.0          |
| Y     | Feature updates               | 0.7.0 -> 0.8.0 |
| Z     | Security & dependency patches | 0.7.0 -> 0.7.1 |

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
