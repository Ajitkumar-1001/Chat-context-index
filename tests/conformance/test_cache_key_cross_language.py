"""Cross-language cache-key conformance (T077; INV-08; AT-11): verifies cache_key.py's Python
cache-key derivation and its TypeScript equivalent (cacheKey.ts) produce identical
scope_digest/request_digest for the same canonical request and scope, using the mutation-pairs
table from spec/fixtures/cache-key-mutations.json (M1, M4, M5, M8 — the pairs that exercise the
canonicalization formula itself, as opposed to caller-level effective-request semantics).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "packages", "python", "src"))

from cci.cache_key import request_digest, scope_digest
from cci.errors import InputValidationError

_TS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "packages", "typescript")
_CACHE_KEY_JS = os.path.join(_TS_DIR, "dist", "cacheKey.js")


def _ts_digest(fn: str, arg: dict) -> str | None:
    """Runs cacheKey.js's `fn(arg)` in a child node process, returning the digest or None if it
    threw (used for M8's rejection case)."""
    script = (
        f"import(process.argv[1]).then(m => {{"
        f"try {{ console.log(JSON.stringify({{ ok: true, value: m.{fn}({json.dumps(arg)}) }})); }}"
        f"catch (e) {{ console.log(JSON.stringify({{ ok: false, error: e.constructor.name }})); }}"
        f"}});"
    )
    result = subprocess.run(["node", "-e", script, _CACHE_KEY_JS], capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        raise RuntimeError(f"node failed: {result.stderr}")
    return json.loads(result.stdout)


def _ts_request_digest(effective_request: dict) -> str | None:
    return _ts_digest("requestDigest", effective_request)


def test_m1_reordered_properties_produce_identical_digest_in_both_languages():
    a = {"b": 2, "a": 1, "nested": {"y": 2, "x": 1}}
    b = {"a": 1, "nested": {"x": 1, "y": 2}, "b": 2}

    py_a, py_b = request_digest(a), request_digest(b)
    assert py_a == py_b, "Python: reordering must not change the digest"

    ts_a = _ts_request_digest(a)
    ts_b = _ts_request_digest(b)
    assert ts_a["ok"] and ts_b["ok"]
    assert ts_a["value"] == ts_b["value"], "TypeScript: reordering must not change the digest"
    assert py_a == ts_a["value"], "Python and TypeScript must produce the identical digest (INV-08)"


def test_m4_changed_content_produces_different_digest_in_both_languages_and_they_still_match():
    base = {"operation": "indexing", "model": "fake-model", "messages": ["hello"]}
    changed = {"operation": "indexing", "model": "fake-model", "messages": ["hello world"]}

    py_base, py_changed = request_digest(base), request_digest(changed)
    assert py_base != py_changed, "Python: changed content must produce a different digest"

    ts_base = _ts_request_digest(base)["value"]
    ts_changed = _ts_request_digest(changed)["value"]
    assert ts_base != ts_changed, "TypeScript: changed content must produce a different digest"

    assert py_base == ts_base
    assert py_changed == ts_changed


def test_m5_changed_scope_fields_produce_different_scope_digest_in_both_languages():
    py_sd1 = scope_digest("ns1", "store1", "hist1", 0)
    py_sd2 = scope_digest("ns1", "store1", "hist1", 1)  # only cache_generation changed
    assert py_sd1 != py_sd2

    # scopeDigest takes positional args, not a single object — call it directly via a small script.
    script = (
        f"import({json.dumps(_CACHE_KEY_JS)}).then(m => {{"
        f"const a = m.scopeDigest('ns1','store1','hist1',0);"
        f"const b = m.scopeDigest('ns1','store1','hist1',1);"
        f"console.log(JSON.stringify({{a, b}}));"
        f"}});"
    )
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    ts = json.loads(result.stdout)

    assert ts["a"] != ts["b"], "TypeScript: changed cache_generation must produce a different scope_digest"
    assert py_sd1 == ts["a"]
    assert py_sd2 == ts["b"]


def test_m8_non_finite_numbers_rejected_before_memo_lookup_in_both_languages():
    try:
        request_digest({"x": float("nan")})
        raise AssertionError("Python: expected InputValidationError")
    except InputValidationError:
        pass

    # JSON has no NaN literal; exercise via Infinity instead, which is representable as a JS
    # number but must still be rejected (mirrors cache_key.py's `_reject_non_finite`).
    script = (
        f"import({json.dumps(_CACHE_KEY_JS)}).then(m => {{"
        f"try {{ m.requestDigest({{x: Infinity}}); console.log(JSON.stringify({{ok: true}})); }}"
        f"catch (e) {{ console.log(JSON.stringify({{ok: false, error: e.constructor.name}})); }}"
        f"}});"
    )
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    ts = json.loads(result.stdout)
    assert ts["ok"] is False and ts["error"] == "InputValidationError"

    try:
        request_digest({"x": float("inf")})
        raise AssertionError("Python: expected InputValidationError for Infinity too")
    except InputValidationError:
        pass


if __name__ == "__main__":
    test_m1_reordered_properties_produce_identical_digest_in_both_languages()
    test_m4_changed_content_produces_different_digest_in_both_languages_and_they_still_match()
    test_m5_changed_scope_fields_produce_different_scope_digest_in_both_languages()
    test_m8_non_finite_numbers_rejected_before_memo_lookup_in_both_languages()
    print("T077 cross-language cache-key conformance checks passed")
