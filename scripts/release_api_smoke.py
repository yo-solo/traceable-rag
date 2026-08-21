from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlencode


PROBES = (
    {
        "query": "compilation front end middle end optimization back end target code",
        "mode": "keyword",
        "minimum_sources": 1,
        "expected_document_names": (
            "openstax-introduction-to-computer-science-p357",
        ),
        "require_visual": True,
    },
    {
        "query": (
            "hierarchical inheritance multiple classes single superclass "
            "multiple inheritance one class multiple classes Table 13.2"
        ),
        "mode": "keyword",
        "minimum_sources": 1,
        "expected_document_names": (
            "openstax-introduction-to-python-programming-p334-335",
        ),
    },
    {
        "query": "income inequality decline after 2014-15 peak S80/S20 EU-27",
        "mode": "keyword",
        "minimum_sources": 1,
        "expected_document_names": (
            "eu-joint-employment-report-2025-p97",
        ),
    },
    {
        "query": (
            "gender segregation future-oriented occupations "
            "no or nearly no women colleague 36%"
        ),
        "mode": "keyword",
        "minimum_sources": 1,
        "expected_document_names": (
            "eu-future-oriented-occupations-p19",
        ),
    },
    {
        "query": "quantum error correction lunar communications network",
        "mode": "keyword",
        "minimum_sources": 0,
        "expected_document_names": (),
    },
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify the Retrieval and Source contracts against an accepted corpus."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--knowledge-base-id", default="default")
    parser.add_argument("--expected-document-count", type=int, default=4)
    parser.add_argument("--report", required=True)
    args = parser.parse_args(argv)

    base_url = args.base_url.rstrip("/")
    report: dict[str, Any] = {
        "base_url": base_url,
        "knowledge_base_id": args.knowledge_base_id,
        "expected_document_count": args.expected_document_count,
        "checks": [],
    }
    try:
        health = _request_json(f"{base_url}/api/health")
        if health.get("ok") is not True:
            raise AssertionError("service health check failed")
        knowledge_bases = _request_json(f"{base_url}/api/knowledge-bases").get(
            "knowledge_bases"
        )
        if not isinstance(knowledge_bases, list) or not any(
            item.get("id") == args.knowledge_base_id for item in knowledge_bases
        ):
            raise AssertionError("accepted knowledge base is unavailable")
        documents = _request_json(
            f"{base_url}/api/knowledge-bases/{args.knowledge_base_id}/documents"
        ).get("documents")
        if not isinstance(documents, list) or len(documents) != args.expected_document_count:
            raise AssertionError("accepted document count does not match")
        for probe in PROBES:
            report["checks"].append(
                _run_retrieval_probe(
                    base_url,
                    args.knowledge_base_id,
                    **probe,
                )
            )
        report["ok"] = True
    except (AssertionError, OSError, urllib.error.URLError, ValueError) as exc:
        report["ok"] = False
        report["error"] = str(exc)

    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report.get("ok") is True else 1


def _run_retrieval_probe(
    base_url: str,
    knowledge_base_id: str,
    *,
    query: str,
    mode: str = "auto",
    minimum_sources: int,
    expected_document_names: tuple[str, ...],
    require_visual: bool = False,
) -> dict[str, Any]:
    request = {
        "query": query,
        "knowledge_base_ids": [knowledge_base_id],
        "mode": mode,
        "top_k": 50,
    }
    response = _request_json(f"{base_url}/api/retrieval", request)
    if response.get("mode_requested") != mode:
        raise AssertionError(f"Retrieval did not preserve mode={mode}")
    if response.get("pipeline_used") not in {"keyword", "dense", "dense_reranker"}:
        raise AssertionError("Retrieval did not report the actual pipeline")
    if mode == "keyword" and response.get("pipeline_used") != "keyword":
        raise AssertionError("Retrieval did not preserve the keyword pipeline")
    if not isinstance(response.get("warnings"), list):
        raise AssertionError("Retrieval warnings are invalid")
    results = response.get("results")
    if not isinstance(results, list) or len(results) < minimum_sources:
        raise AssertionError("Retrieval returned too few Source candidates")

    result_filenames = {item.get("document_name") for item in results}
    missing = [
        name for name in expected_document_names if name not in result_filenames
    ]
    if missing:
        returned = sorted(name for name in result_filenames if isinstance(name, str))
        raise AssertionError(
            "Retrieval missed expected documents: "
            + ", ".join(missing)
            + "; returned documents: "
            + ", ".join(returned)
        )

    if require_visual and not any(
        isinstance(result.get("asset"), dict) for result in results
    ):
        raise AssertionError("Retrieval did not return a visual Source")

    checked_sources = []
    results_to_check = results[: max(minimum_sources, 1)]
    if require_visual:
        visual_result = next(
            result for result in results if isinstance(result.get("asset"), dict)
        )
        results_to_check = [visual_result]
    for result in results_to_check:
        _validate_source_result(result)
        source_id = result["source_id"]
        context = _request_json(
            f"{base_url}/api/sources/{source_id}/context?"
            + urlencode({"before": 2, "after": 2})
        )
        if context.get("source", {}).get("source_id") != source_id:
            raise AssertionError("Source Context did not resolve the requested Source")
        if not isinstance(context.get("before"), list) or not isinstance(
            context.get("after"), list
        ):
            raise AssertionError("Source Context neighbours are invalid")
        if isinstance(result.get("asset"), dict):
            context_source = context.get("source")
            if not isinstance(context_source, dict):
                raise AssertionError("Visual Source Context is invalid")
            context_asset = context_source.get("asset")
            if not isinstance(context_asset, dict):
                raise AssertionError("Visual Source Context asset is invalid")
            if context_asset.get("modality") != result["asset"].get("modality"):
                raise AssertionError("Visual Source modality changed in Context")
            _validate_visual_asset(context_asset, detailed=True)
            for key in ("asset_url", "preview_url"):
                asset_url = context_asset.get(key)
                if asset_url is None:
                    continue
                content_type, content = _request_bytes(f"{base_url}{asset_url}")
                if content_type not in {"image/jpeg", "image/png", "image/webp"}:
                    raise AssertionError("Visual Source asset content type is invalid")
                if not content:
                    raise AssertionError("Visual Source asset is empty")
        source_url = result["source_url"]
        _request_text(f"{base_url}{source_url}", accept="text/html")
        checked_sources.append(source_id)

    return {
        "query": query,
        "mode_requested": mode,
        "pipeline_used": response["pipeline_used"],
        "degraded": bool(response.get("degraded")),
        "result_count": len(results),
        "checked_source_ids": checked_sources,
    }


def _validate_source_result(result: Any) -> None:
    if not isinstance(result, dict):
        raise AssertionError("Retrieval Source result is invalid")
    required_strings = (
        "source_id",
        "source_url",
        "text",
        "document_id",
        "document_name",
    )
    if any(not isinstance(result.get(key), str) or not result[key] for key in required_strings):
        raise AssertionError("Retrieval Source is missing required fields")
    if not result["source_url"].startswith("/sources/"):
        raise AssertionError("Retrieval Source URL is not a Source verification route")
    if not isinstance(result.get("human_page_number"), int) or result[
        "human_page_number"
    ] < 1:
        raise AssertionError("Retrieval Source page number is invalid")
    if not isinstance(result.get("score"), (int, float)):
        raise AssertionError("Retrieval Source score is invalid")
    asset = result.get("asset")
    if asset is None:
        return
    _validate_visual_asset(asset, detailed=False)


def _validate_visual_asset(asset: Any, *, detailed: bool) -> None:
    if not isinstance(asset, dict):
        raise AssertionError("Visual Source asset is invalid")
    required = {"asset_id", "modality", "asset_url", "preview_url"}
    if detailed:
        required.update(
            {"content_type", "file_hash", "width", "height", "caption"}
        )
    if not required.issubset(asset):
        raise AssertionError("Visual Source asset is missing contract fields")
    if not isinstance(asset.get("asset_id"), str) or not asset["asset_id"]:
        raise AssertionError("Visual Source asset identity is invalid")
    if asset.get("modality") not in {"image", "chart"}:
        raise AssertionError("Visual Source modality is invalid")
    asset_url = asset.get("asset_url")
    preview_url = asset.get("preview_url")
    if asset_url is not None and (
        not isinstance(asset_url, str)
        or not asset_url.startswith("/api/knowledge-bases/")
        or "/assets/" not in asset_url
    ):
        raise AssertionError("Visual Source asset URL is invalid")
    if preview_url is not None and (
        not isinstance(preview_url, str)
        or not preview_url.endswith("?variant=preview")
    ):
        raise AssertionError("Visual Source preview URL is invalid")
    if preview_url is not None and asset_url is None:
        raise AssertionError("Visual Source preview URL has no original asset URL")
    if not detailed:
        return
    content_type = asset.get("content_type")
    if content_type is not None and content_type not in {
        "image/jpeg",
        "image/png",
        "image/webp",
    }:
        raise AssertionError("Visual Source content type is invalid")
    if asset.get("file_hash") is not None and not isinstance(asset["file_hash"], str):
        raise AssertionError("Visual Source file hash is invalid")
    for key in ("width", "height"):
        value = asset.get(key)
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value <= 0
        ):
            raise AssertionError("Visual Source dimensions are invalid")
    if asset.get("caption") is not None and not isinstance(asset["caption"], str):
        raise AssertionError("Visual Source caption is invalid")


def _request_json(url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Accept": "application/json",
            **({"Content-Type": "application/json"} if data is not None else {}),
        },
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _request_text(url: str, *, accept: str = "text/plain") -> str:
    request = urllib.request.Request(url, headers={"Accept": accept}, method="GET")
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


def _request_bytes(url: str) -> tuple[str, bytes]:
    request = urllib.request.Request(url, headers={"Accept": "image/*"}, method="GET")
    with urllib.request.urlopen(request, timeout=30) as response:
        content_type = response.headers.get_content_type()
        return content_type, response.read()


if __name__ == "__main__":
    raise SystemExit(main())
