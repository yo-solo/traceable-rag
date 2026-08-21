# Security policy

## Supported versions

Security updates are provided for the latest published release. Older releases,
development snapshots, and uncommitted builds are not supported release
targets.

## Report a vulnerability privately

Do not disclose an unpatched vulnerability in a public issue, discussion, or
pull request. Use GitHub's private vulnerability reporting form:

<https://github.com/yo-solo/traceable-rag/security/advisories/new>

If the form is unavailable, contact the repository owner through a private
contact method published on the owner's GitHub profile. Do not include exploit
details in a public message.

Include enough information to reproduce and assess the report:

- the affected release, image digest, or commit;
- deployment topology and relevant configuration, with secrets removed;
- impact and preconditions;
- reproducible steps or a minimal proof of concept; and
- any mitigation already tested.

Never include API keys, provider credentials, access tokens, private documents,
or personal data. Redact logs and screenshots before attaching them.

## Deployment boundary

Packaged Traceable RAG deployments are designed to bind the host port to the
loopback interface. Do not expose the application directly to an untrusted
network or treat it as an internet-facing, multi-tenant security boundary.
Review the deployment configuration before placing any proxy or gateway in
front of it.

## Scope

Reports about the application, release installers, official container images,
and repository automation are in scope. Vulnerabilities in an upstream project
should also be reported to that project's security contact when the issue is
not specific to Traceable RAG.
