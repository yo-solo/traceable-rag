from __future__ import annotations

import argparse
import hashlib
import shutil
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader, PdfWriter


@dataclass(frozen=True)
class Sample:
    id: str
    dataset_id: str
    dataset_revision: str
    source_file: str
    source_sha256: str
    source_pages: tuple[int, ...]
    output_file: str
    output_sha256: str | None = None

    @property
    def source_url(self) -> str:
        dataset = self.dataset_id.removeprefix("vidore/")
        return (
            "https://huggingface.co/datasets/vidore/"
            f"{dataset}/resolve/{self.dataset_revision}/pdfs/{self.source_file}"
        )


SAMPLES = (
    Sample(
        id="openstax-computer-science-compilation",
        dataset_id="vidore/vidore_v3_computer_science",
        dataset_revision="d5cc75883d92e294f0c0fc2662551c9708a06ebc",
        source_file="Introduction_to_Computer_Science.pdf",
        source_sha256="1003e2a5484f0ef1604fa83cf8176066b156ae686560f72ee48376e341395272",
        source_pages=(357,),
        output_file="openstax-introduction-to-computer-science-p357.pdf",
        output_sha256="ea50e32e25f58b29ca3b64ed6528950cc1aa46614ac6e876fb69ca7f468fd904",
    ),
    Sample(
        id="openstax-python-inheritance-table",
        dataset_id="vidore/vidore_v3_computer_science",
        dataset_revision="d5cc75883d92e294f0c0fc2662551c9708a06ebc",
        source_file="Introduction_to_Python_Programming.pdf",
        source_sha256="ca60aa86a3066f8504eb6e177d16303fcb3e38ff9bd6b07326f4eced94c2f6fc",
        source_pages=(334, 335),
        output_file="openstax-introduction-to-python-programming-p334-335.pdf",
        output_sha256="a1cb8bd9bcfe96593ea55d3646f504cbc5a900a5f395872bb38d6c57a0403af2",
    ),
    Sample(
        id="eu-income-inequality-chart",
        dataset_id="vidore/vidore_v3_hr",
        dataset_revision="0cdf0979f2c5a0fd3e335e6373b9da48a9fe3bc3",
        source_file="joint_employment_report_2025-KE0125018ENN.pdf",
        source_sha256="e88b437140a6407e03bfcbd1c01f3fbb4d1f3540c953e678c0d8ed59ca39692e",
        source_pages=(97,),
        output_file="eu-joint-employment-report-2025-p97.pdf",
        output_sha256="2c0a2220211dff6d45110e3bab2f96a15e0fa63bb299849335784f8643e88b82",
    ),
    Sample(
        id="eu-future-occupations-chart",
        dataset_id="vidore/vidore_v3_hr",
        dataset_revision="0cdf0979f2c5a0fd3e335e6373b9da48a9fe3bc3",
        source_file="future-oriented_occupations_in_the_eu-KE0125043ENN.pdf",
        source_sha256="5bf0319a97dbeb45b6941547912ceb19198841bd0f75d57a8a569ff4fe770965",
        source_pages=(19,),
        output_file="eu-future-oriented-occupations-p19.pdf",
        output_sha256="e2f04690e015283ccd0e04784faffcb01890c68c15e243e7add59239117b014f",
    ),
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_overrides(values: list[str]) -> dict[str, Path]:
    overrides: dict[str, Path] = {}
    for value in values:
        sample_id, separator, raw_path = value.partition("=")
        if not separator or not sample_id or not raw_path:
            raise ValueError("--source-override must use SAMPLE_ID=/path/to/source.pdf")
        if sample_id in overrides:
            raise ValueError(f"duplicate source override: {sample_id}")
        overrides[sample_id] = Path(raw_path).expanduser().resolve()
    unknown = sorted(set(overrides) - {sample.id for sample in SAMPLES})
    if unknown:
        raise ValueError(f"unknown sample ids: {', '.join(unknown)}")
    return overrides


def download_source(sample: Sample, cache_dir: Path) -> Path:
    target = cache_dir / sample.dataset_revision / sample.source_file
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".download")
        request = urllib.request.Request(
            sample.source_url,
            headers={"User-Agent": "traceable-rag-public-sample-preparer/0.5"},
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                with temporary.open("wb") as output:
                    shutil.copyfileobj(response, output)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    return target


def extract_pages(source: Path, output: Path, pages: tuple[int, ...]) -> None:
    reader = PdfReader(source)
    writer = PdfWriter()
    for page_number in pages:
        if page_number < 1 or page_number > len(reader.pages):
            raise ValueError(
                f"page {page_number} is outside {source.name} ({len(reader.pages)} pages)"
            )
        writer.add_page(reader.pages[page_number - 1])
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{output.stem}-",
        suffix=".pdf",
        dir=output.parent,
        delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
        writer.write(temporary)
    temporary_path.replace(output)
    output.chmod(0o644)


def prepare_samples(
    output_dir: Path,
    cache_dir: Path,
    overrides: dict[str, Path],
    *,
    verify_only: bool,
) -> None:
    for sample in SAMPLES:
        output = output_dir / sample.output_file
        if not verify_only:
            source = overrides.get(sample.id) or download_source(sample, cache_dir)
            if not source.is_file():
                raise FileNotFoundError(source)
            actual_source_hash = sha256_file(source)
            if actual_source_hash != sample.source_sha256:
                raise ValueError(
                    f"source checksum mismatch for {sample.id}: {actual_source_hash}"
                )
            extract_pages(source, output, sample.source_pages)

        if not output.is_file():
            raise FileNotFoundError(output)
        actual_output_hash = sha256_file(output)
        if sample.output_sha256 and actual_output_hash != sample.output_sha256:
            raise ValueError(
                f"output checksum mismatch for {sample.id}: {actual_output_hash}"
            )
        print(f"{actual_output_hash}  {output.as_posix()}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reproduce the licensed PDF excerpts used by the public demo corpus."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("examples/vidore/documents"),
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(".artifacts/vidore-public-sources"),
    )
    parser.add_argument(
        "--source-override",
        action="append",
        default=[],
        metavar="SAMPLE_ID=PATH",
    )
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args(argv)
    prepare_samples(
        args.output_dir.resolve(),
        args.cache_dir.resolve(),
        parse_overrides(args.source_override),
        verify_only=args.verify_only,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
