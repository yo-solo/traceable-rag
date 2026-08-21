# Contributing to Traceable RAG

Thank you for helping improve Traceable RAG. Keep each change focused, explain
the user-visible outcome, and include evidence that the affected path works.

## Before opening a pull request

1. Search existing issues and pull requests for related work.
2. Open an issue before a large change that alters an API, storage contract,
   deployment boundary, or user workflow.
3. Work from a current branch and keep unrelated changes out of the pull
   request.
4. Add or update tests and documentation with the implementation.

## Local checks

The backend requires Python 3.10 or newer:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest -q
```

The web application uses the versions resolved by `web/package-lock.json`:

```bash
cd web
npm ci
npm test -- --run --maxWorkers=1
npm run build
```

If an API schema changes, regenerate `docs/openapi.json` and run the OpenAPI
contract tests:

```bash
python scripts/generate_openapi.py
python -m pytest -q tests/test_openapi.py
```

Before submitting, run `git diff --check` and review the complete diff.

## Public-data and secret rules

- Do not commit API keys, tokens, credentials, private endpoints, local paths,
  private documents, personal data, or production data.
- Do not use real user or organization documents as test or demo fixtures.
- A third-party document may be included only when its redistribution terms are
  documented and compatible with the repository. When redistribution rights
  are unclear, provide a pinned manifest and preparation or download script
  instead of the document bytes.
- Use synthetic, anonymized, or expressly redistributable fixtures in tests.
- Keep generated runtime data, caches, build output, and local environment files
  out of commits.

## Pull request checklist

A pull request should state:

- what changed and why;
- the tests or manual checks performed;
- any migration, compatibility, security, or operational impact; and
- whether documentation, OpenAPI output, or screenshots changed.

The repository's pull request template contains the complete checklist.

## Licensing and brand assets

Unless explicitly stated otherwise, submitted code and documentation are
licensed under the repository's MIT License. By submitting a contribution, you
confirm that you have the right to provide it on those terms.

The MIT License does not grant rights to Traceable RAG names, logos, or other
brand assets. See [TRADEMARKS.md](TRADEMARKS.md) before adding, changing, or
redistributing branding.

## Conduct and security

Participation is governed by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Report
security vulnerabilities through the private process in
[SECURITY.md](SECURITY.md), not through a public issue.
