# Third-party notices

Traceable RAG includes and depends on third-party software. Those components
remain under their own licenses; the repository's MIT License does not replace
or modify those terms.

This file highlights direct runtime components. It is not an exhaustive list of
transitive packages. The authoritative dependency inputs are `pyproject.toml`,
`evaluation/pyproject.toml`, `web/package.json`, and `web/package-lock.json`.
Release SBOMs, when published, describe the resolved container contents for that
release.

## Python runtime

| Component | License | Project |
| --- | --- | --- |
| cryptography | Apache-2.0 OR BSD-3-Clause | <https://github.com/pyca/cryptography> |
| FastAPI | MIT | <https://github.com/fastapi/fastapi> |
| Pillow | HPND | <https://github.com/python-pillow/Pillow> |
| Uvicorn | BSD-3-Clause | <https://github.com/encode/uvicorn> |
| pypdf | BSD-3-Clause | <https://github.com/py-pdf/pypdf> |

## Web runtime

The versions below are the direct dependencies resolved in
`web/package-lock.json` at the time this notice was written.

| Component | Resolved version | License | Project |
| --- | ---: | --- | --- |
| Scalar API Reference React | 0.9.60 | MIT | <https://github.com/scalar/scalar> |
| DOMPurify | 3.4.13 | MPL-2.0 OR Apache-2.0 | <https://github.com/cure53/DOMPurify> |
| KaTeX | 0.18.1 | MIT | <https://github.com/KaTeX/KaTeX> |
| Lucide React | 0.561.0 | ISC | <https://github.com/lucide-icons/lucide> |
| PDF.js | 6.2.108 | Apache-2.0 | <https://github.com/mozilla/pdf.js> |
| React | 19.2.7 | MIT | <https://github.com/facebook/react> |
| React DOM | 19.2.7 | MIT | <https://github.com/facebook/react> |
| React Router DOM | 7.18.2 | MIT | <https://github.com/remix-run/react-router> |

## Evaluation dependencies

The optional ViDoRe evaluation package declares additional dependencies in
`evaluation/pyproject.toml`. Installing that package accepts each dependency's
own license terms.

## Community documents

`CODE_OF_CONDUCT.md` is adapted from Contributor Covenant version 2.1,
copyright Contributor Covenant contributors, and is used under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). The Community
Impact Guidelines in that document were inspired by Mozilla's code of conduct
enforcement ladder. The repository MIT License does not replace those
attribution terms.

## Container contents

The runtime container is based on the official Python Debian image and installs
Debian packages, including LibreOffice Writer and Noto CJK fonts. Their license
and copyright files are retained in the image under `/usr/share/doc` where the
Debian packages provide them.

## Public ViDoRe sample PDFs

The MIT License in `LICENSE` applies to Traceable RAG source code and original
project documentation. It does not apply to the third-party sample PDFs below.
Each excerpt remains under its source license. No endorsement by the source
authors or institutions is implied.

All four samples are declared in `examples/vidore/manifest.json`, including
their pinned ViDoRe dataset revisions, input and output SHA-256 hashes, source
pages, upstream URLs, and preparation statement.

| Excerpt | Copyright and attribution | License |
| --- | --- | --- |
| `openstax-introduction-to-computer-science-p357.pdf` | Copyright 2024 Rice University. OpenStax, *Introduction to Computer Science*. Extracted PDF page only; no content changes. Access for free at openstax.org. | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) |
| `openstax-introduction-to-python-programming-p334-335.pdf` | Copyright 2024 Rice University. OpenStax, *Introduction to Python Programming*. Extracted PDF pages only; no content changes. Access for free at openstax.org. | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) |
| `eu-joint-employment-report-2025-p97.pdf` | Copyright European Union, 2025, *Joint Employment Report 2025*. Extracted PDF page only; no content changes. | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) |
| `eu-future-oriented-occupations-p19.pdf` | Copyright European Union, 2025, *Future-oriented occupations in the EU*. Extracted PDF page only; no content changes. | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) |

The ViDoRe annotations, qrels, and related metadata are themselves distributed
under CC BY 4.0 by the dataset publisher. This repository records only the
minimum identifiers needed to trace each excerpt; it does not redistribute the
annotation or qrel files.

Dataset names, citations, manifests, or download scripts do not by themselves
grant a right to redistribute other third-party documents. Do not reuse a
different ViDoRe source PDF without checking that document's upstream license.

Third-party names and marks belong to their respective owners. See
[TRADEMARKS.md](TRADEMARKS.md) for the Traceable RAG brand policy.
