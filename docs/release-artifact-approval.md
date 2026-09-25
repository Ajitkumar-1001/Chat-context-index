# Approval of exact release archives

Publication requires both `RELEASE_APPROVED_SHA` and an independently reviewed
artifact manifest. A passing candidate report cannot approve its own archives.
Both publisher jobs compare the candidate with the owner-controlled manifest
before creating publication files; neither job rebuilds packages.

The manifest has exactly four fields: `schema_version` (integer `1`), `source_sha`
(full commit), `version` (package version without `v`), and `artifacts` (four
objects with `filename` and lowercase `sha256`). It binds the wheel, source
distribution, npm archive, and the verification wheel rebuilt from the source
distribution. Only the first three are copied for publication. Paths are relative
to `verified-artifacts/`. Duplicate keys, duplicate filenames, unexpected fields,
unsafe paths, missing entries, and invalid checksums fail closed.

## Prepare a review draft from retained CI evidence

Use the actual archives retained by the trusted CI run and the corresponding
source checkout. `reports/package-memory.json`, `reports/artifact-secrets.json`,
and all four archives must be present. The command still checks report status,
source commit and runtime fingerprints, CI run, the nine installed-package
combinations, documentation, licenses, secret scan, package identities and
versions, archive layout, and each archive checksum.

For the retained September 24 candidate, the recorded source is
`b3bb918be1edb21bddb735ad92b5a52d59deafb7`, run is `36043232899`, and version is
`0.1.0`. Its [recorded hashes](../evaluations/results/production-readiness-20260924/hosted-artifact-manifest.json)
and [hosted verification](../evaluations/results/production-readiness-20260924/hosted-verification.json)
provide a concrete review baseline. This historical candidate is **not approved
for release**; remaining gates are recorded in the [release status](release-status.md).

With that run's downloaded artifact at the following local path, generate a draft:

```bash
python scripts/release_controls.py approval-manifest \
  --candidate /tmp/cont-index-production-ci-36043232899/release-candidate-b3bb918be1edb21bddb735ad92b5a52d59deafb7 \
  --sha b3bb918be1edb21bddb735ad92b5a52d59deafb7 \
  --run-id 36043232899 \
  --tag v0.1.0 \
  --output /tmp/release-artifact-review.json
```

The output file must not already exist. Generation writes compact JSON without
a trailing newline and prints `DRAFT_NOT_APPROVED` plus
`approval_manifest_sha256`. It creates no publication directory and changes no
GitHub approval values. Retain these exact file bytes and digest with the broader
candidate record, including the CI run and reports. The approval manifest is the
archive identity subset of that record, not a replacement for evaluation,
performance, adoption, or human-review evidence.

For a later candidate, use its actual source/run/version and newly retained
archives. Review each archive hash against every applicable test/evaluation and
participant record. Source equality alone does not allow evidence reuse when
archive bytes differ. The run ID is retained in the broader evidence record:
publication separately requires the current tag run's report and archives to
match each other and these approved bytes.

## Owner configuration after final evidence acceptance

Complete the release gates first. The project owner reviews the source, the four
filename/hash pairs, and the complete evidence record, then retains an attributable
approval of the manifest's exact SHA-256. A generated draft or a local successful
copy is not that approval.

Configure the existing protected `release` environment with required reviewers
and the intended tag restrictions, and retain the protected version-tag rules.
Restrict repository/environment variable administration to the release owners.
Set these controls only for the accepted candidate:

| Scope | Variable | Reviewed value |
| --- | --- | --- |
| Repository | `RELEASE_APPROVED_SHA` | Manifest `source_sha`; the source authorization job reads this before environment jobs start |
| `release` environment | `RELEASE_APPROVED_ARTIFACT_MANIFEST` | Entire exact compact JSON file, without added whitespace or newline |
| `release` environment | `RELEASE_APPROVED_ARTIFACT_MANIFEST_SHA256` | Lowercase 64-character digest of that exact file, taken from the independently retained approval |

Do not derive the digest in the release workflow or load approval from the
downloaded candidate. Do not store fallback artifact-approval variables at the
repository or organization level: leaving the environment approval unset should
block publication. The workflow supplies variable values through environment
variables and writes the manifest with `printf '%s'`; JSON is never evaluated as
shell code. Reformatting the JSON changes its digest and requires updating the
owner's reviewed record.

For a local check after supplying an explicitly pinned manifest, use:

```bash
python scripts/release_controls.py artifacts \
  --candidate "$CANDIDATE_DIRECTORY" \
  --sha "$CANDIDATE_SHA" --run-id "$CANDIDATE_RUN_ID" --tag "$CANDIDATE_TAG" \
  --approval "$REVIEWED_MANIFEST_PATH" \
  --approval-sha256 "$INDEPENDENTLY_APPROVED_MANIFEST_SHA256" \
  --output "$NEW_PUBLICATION_DIRECTORY"
```

The manifest must be a regular file outside the candidate directory, and the
output directory must not exist. Omitting either approval argument fails.
Replacing the manifest without changing the protected digest fails. Changing an
archive and rewriting the candidate's own report to match still fails. The output
bytes are checked again after copying; a changed copy fails before a success
manifest is written and the workflow cannot reach the publisher step. A failed
copy may leave an incomplete output directory: inspect it and use a fresh output
path when retrying. The success manifest records the approval digest and
`rebuilt_for_publication: false`.

Tag CI may produce different bytes even from the same source (for example, if a
build includes timestamps). Such a mismatch blocks publication. Keep the approved
pins intact and investigate reproducibility or select and review a new candidate;
never automatically replace approval values with the current run's hashes. Final
tag creation, protected-environment approval, and registry publication remain
separate delivery actions after evidence acceptance.
