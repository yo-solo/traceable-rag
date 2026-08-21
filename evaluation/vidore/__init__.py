"""ViDoRe v3 retrieval benchmark integration."""

from .config import BenchmarkConfig, load_config
from .metrics import evaluate_run
from .pipeline import PageDensePipeline, TraceableChunkPipeline

__all__ = [
    "BenchmarkConfig",
    "PageDensePipeline",
    "TraceableChunkPipeline",
    "evaluate_run",
    "load_config",
]
