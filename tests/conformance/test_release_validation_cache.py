"""Real Redis with a deterministic model double for release-runner cache accounting."""

import asyncio
import importlib.util
import json
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import redis

SPEC = importlib.util.spec_from_file_location(
    "release_validation_test_helpers",
    Path(__file__).resolve().parents[1] / "evaluations/test_release_validation.py",
)
HELPERS = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = HELPERS
SPEC.loader.exec_module(HELPERS)


@pytest.fixture
def redis_url() -> Iterator[str]:
    container = subprocess.check_output(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "-p",
            "127.0.0.1::6379",
            "redis:7-alpine",
        ],
        text=True,
        timeout=30,
    ).strip()
    try:
        address = subprocess.check_output(
            ["docker", "port", container, "6379"], text=True, timeout=10
        ).strip()
        url = f"redis://{address}"
        with redis.Redis.from_url(url, socket_timeout=1) as client:
            for _ in range(50):
                try:
                    client.ping()
                    break
                except redis.ConnectionError:
                    time.sleep(0.1)
            else:
                raise RuntimeError("development Redis did not become ready")
        yield url
    finally:
        subprocess.run(["docker", "stop", container], check=True, capture_output=True, timeout=30)


def test_redis_release_experiment_uses_real_hits_and_accounts_for_outage(
    tmp_path: Path, redis_url: str
) -> None:
    runner = HELPERS.MODULE
    settings = HELPERS.plan()
    settings.update(kind="cache", cache_backends=["redis"])
    settings["package_config"]["redis_fallback"] = "none"  # A warm hit must actually come from Redis.
    fixture = {
        "messages": [
            {"role": "user", "content": "The target is Oslo.", "source_id": "unit"},
            {"role": "user", "content": "Use CSV files.", "source_id": "unit"},
        ],
        "query": "Where is the target?",
        "changed_query": "Which city is the target?",
        "append_message": {"role": "user", "content": "Add a backup copy.", "source_id": "unit"},
        "required_source_seq": 1,
    }
    report = asyncio.run(
        runner.execute(
            runner.Plan.model_validate(settings),
            fixture,
            HELPERS.NativeAdapter(),
            tmp_path,
            real_provider=False,
            redis_url=redis_url,
        )
    )
    assert report["status"] == "COMPLETE", json.dumps(report)
    phases = {row["phase"]: row for row in report["cache_results"]}
    assert phases["warm"]["physical_usage"]["calls"] == 1
    assert phases["changed_query"]["physical_usage"]["calls"] > 1
    assert phases["append"]["physical_usage"]["calls"] > 1
    assert phases["cache_boundary_outage"]["physical_usage"]["calls"] > 1
    assert phases["cache_boundary_outage"]["outage_method"] == "cache_protocol_fault"
    assert report["usage"]["calls"] == len(report["calls"])
    assert not report["real_provider"] and not report["quality_pass"]
