# Security policy

## Reporting a vulnerability

Please report security issues **privately**, through GitHub's private
vulnerability reporting:

1. Go to <https://github.com/Logos-Flux/fincas/security>.
2. Choose **Report a vulnerability**.
3. Describe the issue, the affected version, and the steps or a proof of
   concept that reproduce it.

Do not open a public issue, pull request or discussion for a security problem,
and do not include real customer data in a report.

You can expect an acknowledgment within a few working days. We will keep you
updated while we investigate, agree a disclosure date with you, and credit you
in the advisory unless you ask us not to.

## Supported versions

| Version | Supported |
| :--- | :--- |
| latest 0.4.x | yes |
| anything older | no; upgrade to the latest 0.4.x |

## How fixes ship

A fix ships as a **patch release** (for example 0.4.2) published to PyPI, with a
[GitHub Security Advisory](https://github.com/Logos-Flux/fincas/security/advisories)
that names the affected versions and the fixed one. The release notes say
whether any action beyond upgrading is needed (rotating a secret, re-issuing
tokens).

## Scope

In scope: the `fincas` package (the `golden_erp` package and its apps), the
bundled plugins, the storefront in `storefront/`, and the deployment files in
this repository. Out of scope: vulnerabilities in a deployment's own
configuration or plugins, and in third-party services Fincas talks to.

Hardening a deployment (required secrets, `TRUSTED_PROXIES`,
`LLMHUB_ALLOWED_BASE_URLS`) is covered in
[docs/deployment.md](docs/deployment.md).
