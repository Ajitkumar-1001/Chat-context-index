# Release controls — September 24, 2026

Local implementation, scans and scoped license/attribution checks pass. Registry identity
and final production approval remain open. No package was published and no release tag was created.

The release workflow consumes the exact archives retained by the designated CI installation job.
Before uploading, it checks source commit, CI run, version/tag, source fingerprints, all nine
installation combinations, archive checksums, attribution and an empty artifact secret-scan report.
It never rebuilds packages in a publishing job. `RELEASE_APPROVED_SHA` must match the source commit;
it remains unset until final evidence approval. Both publish jobs also use the protected `release`
environment configured and read back by the parent task.

Local checks:

- 16 artifact verification and safe extraction tests passed; Ruff and workflow YAML parsing passed.
- Fresh wheel/source-wheel/npm installation passed all nine writer/reader combinations.
- `pip-audit` found no known vulnerabilities in 48 resolved Python runtime/tool packages.
- `npm audit` found no vulnerabilities in 13 locked dependencies, including optional/build packages.
- Gitleaks 8.30.1 found no secrets in 15 Git commits, the public working-tree snapshot, or the
  contents of all four unpacked archives. Match redaction was enabled at 100%.
- Package Apache-2.0 licenses and upstream attribution match. The [scoped license review](license-review.json)
  checks 18 Python runtime dependencies and 13 npm dependencies; no release attribution fix remains.
  APSW’s `any-OSI` declaration is explained by its installed license and
  [official documentation](https://rogerbinns.github.io/apsw/copyright.html). Certifi/MPL-2.0 occurs only
  in the audit tooling for this resolution. Six optional Redis npm packages omit top-level license
  files; their MIT declaration is confirmed by the
  [pinned upstream license](https://raw.githubusercontent.com/redis/node-redis/redis@5.12.1/LICENSE).
  Our candidate archives bundle no dependency payload, so no Redis notice is missing from them.
  This closes the current artifact check; it makes no general legal assurance for future bundling.

The updated npm archive hash is `b3eda16aebc9e13116cc68b8418332b3d245c539154c6f836b0f9bf49bfcacc9`;
its metadata now identifies the GitHub repository. The Python wheel remains
`a3286616fa0051b1ba033e4b7cf9e4234036db5c9ed2825ea2cbe5c03d7f32f0`.
See [package verification](package-memory.json) and [complete controls evidence](controls.json).

Both public registry package lookups returned 404. That establishes neither ownership nor a
trusted publisher identity. npm token authentication is retained pending verified registry setup.
The [current npm documentation](https://docs.npmjs.com/trusted-publishers/) requires npm 11.5.1+
and Node 22.14+, a supported hosted runner, matching repository metadata, and a configured trusted
publisher. New configurations must permit the intended direct `npm publish` action. The intended
[PyPI OIDC flow](https://docs.pypi.org/trusted-publishers/using-a-publisher/) also requires registry-side
configuration before publication.

Reproduce local code checks with:

```sh
python -m pytest tests/release -q
ruff check scripts/release_controls.py tests/release --config packages/python/pyproject.toml
python scripts/release_controls.py licenses --output /tmp/cci-licenses.json
python tests/packaging/verify_packages.py --report /tmp/cci-package-check.json --artifacts-out /tmp/cci-verified-artifacts
```

Hosted CI must exercise these new workflow changes before they count as production evidence.
Provider evaluation, independent human review/adoption and reference-runner performance remain
separate release gates. The checks above do not imply those gates passed.
