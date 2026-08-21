# Public ViDoRe sample corpus

This directory contains five pages extracted from four documents represented in
the pinned ViDoRe V3 Computer Science and HR datasets. The samples exercise
technical prose, code, a cross-page table, a process diagram, two-column text,
footnotes, and statistical charts without using any private Traceable RAG
documents.

The repository MIT License does not apply to these PDFs. Every excerpt remains
under CC BY 4.0. Full source, revision, page, checksum, license, and attribution
records are in [`manifest.json`](manifest.json) and
[`THIRD_PARTY_NOTICES.md`](../../THIRD_PARTY_NOTICES.md).

To verify the committed files:

```bash
python scripts/prepare_vidore_examples.py --verify-only
```

To reproduce them from their pinned source PDFs:

```bash
python scripts/prepare_vidore_examples.py
```

The reproduction command downloads exact source files from the pinned
Hugging Face dataset revisions, verifies their SHA-256 hashes, extracts only
the declared pages, and checks the output hashes. No ViDoRe annotations or
qrels are redistributed here.
