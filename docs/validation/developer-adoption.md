# Independent developer adoption tasks

Status: **PREPARED; NO PARTICIPANTS HAVE RUN THESE TASKS**.

The coordinator needs two distinct real developers who have not worked on or previously
used this project. One completes Python, the other TypeScript. They work independently,
using only the frozen supplied documentation and public runtime/tool installation docs.
They do not receive private hints, implementation source tours, or agent-generated
solutions. Record any assistance, including AI assistance; it disqualifies that attempt
as evidence of independent documentation adoption. Do not silently fix their environment.

## Coordinator preparation

Give each participant a fresh session directory containing:

- The exact final candidate wheel (Python) or npm archive (TypeScript), with SHA-256
  copied from the final verified release manifest. Do not substitute an editable install,
  source checkout, registry package of another version, or an earlier build.
- A read-only documentation snapshot: package README, `docs/quickstart-no-redis.md`,
  `docs/rag-and-agent-memory.md`, `docs/supported-runtimes-and-storage.md`, and the relevant
  `examples/python/basic_usage.py` or `examples/typescript/basic-usage.mjs`. These examples
  may be read/adapted as supplied documentation; record the SHA-256 of every supplied file.
- This task sheet and a copy of `adoption-record.template.json`. Record the snapshot's
  commit and file hashes. Supply no generated participant program.
- A supported Python 3.11–3.14 environment or Node 22/24 environment. If installation is
  required, the participant records it. The clock includes package installation/setup;
  record elapsed time rather than inventing a ten-minute release threshold.

Use the documented example's deterministic `EchoProvider` to exercise explicit indexing
without credentials or paid calls. Label this adapter `deterministic_example`, report no
measured live tokens, and retrieve in lexical mode. This adoption trial tests the documented
store/index/retrieve/reopen mechanics; real tree-model quality is a separate R06 gate.
Optional production-provider trials need their own preapproved allocation and logs.

## Python participant

From a new directory outside the repository, verify the supplied wheel's checksum and
create an isolated environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install /absolute/path/to/chat_context_index-0.1.0-py3-none-any.whl
.venv/bin/python -c 'import cci; print(cci.__file__)'
```

Using the supplied Python README and examples, write two small programs:

1. `seed.py` opens a persistent local `history.db`, ingests the following three messages
   with stable source/idempotency identifiers, calls `index()` explicitly with the example
   provider wrapped as documented, prints the ingest receipt and index status/coverage,
   retrieves `Meridian launch`, prints original evidence/pointers, and closes the store.
2. `resume.py`, executed as a **different process**, reopens the same path without ingesting
   again, retrieves the same query, and prints the restored history ID, original IDs,
   pointers, and text. Record the commands and output of both processes.

## TypeScript participant

From a new directory outside the repository, verify the supplied archive's checksum and
create a consumer:

```bash
npm init -y
npm install /absolute/path/to/chat-context-index-0.1.0.tgz
node -e 'console.log(require.resolve("chat-context-index"))'
```

Use `seed.mjs` and `resume.mjs` so Node treats the examples as ES modules. Adapt the supplied
TypeScript README and JavaScript example to perform the same seed/index/retrieve and
separate-process reopen steps as the Python task. Use the public `BoundedProvider` export
with the example provider and store configuration. Record the commands and output.
Native TypeScript currently has no Redis/SQLite provider memoization; it is not required here.

## Shared synthetic messages and expected observations

Ingest these messages in order without modifying their text:

```text
user: The Meridian launch is scheduled for Tuesday.
assistant: Correction: the Meridian launch is scheduled for Thursday instead of Tuesday.
user: The Meridian rollout owner is Rowan.
```

The first process must create three original messages and successfully complete explicit
indexing. Retrieval of `Meridian launch` must expose the newer Thursday correction as
original evidence, with its message ID and source pointer. Keeping the earlier Tuesday
record is correct; the package does not resolve facts automatically.

The second process must recover the same history ID and correction message ID/source
pointer/text without re-ingestion. The package import must resolve inside the fresh
environment's installed package, and the installed version and archive checksum must
match the coordinator's candidate. The database must be on persistent local disk.

Save the participant-authored programs, sanitized terminal transcript, and observations.
Record any confusing step, command failure, documentation page used, unexpected output,
and intervention. A reproducible documentation problem is a failed attempt, not a reason
to omit the record. Correct the documentation, freeze a new snapshot, and rerun the affected
path with an unfamiliar participant to avoid training effects.

## Acceptance checks

One record alone does not close SC-010. The coordinator verifies:

1. Two distinct actual participants attest that they were unfamiliar with the project and
   worked independently; identity evidence is retained privately and referenced by hash.
2. Python and TypeScript are both covered, with exact final archive and documentation
   fingerprints, supported runtime/OS versions, dates, commands, and installed paths.
3. Both records contain successful ingest, explicit index, original-evidence retrieval,
   and a separate-process reopen. The evidence logs show the expected unchanged IDs/text.
4. There were no undocumented steps or assistance. Failures/interventions are retained;
   a later valid repeat identifies the earlier record and changed docs explicitly.
5. Programs, logs, and attestations actually exist, their hashes match the record, and
   a real coordinator has signed the final acceptance. No blank or placeholder field
   is accepted as evidence, and no automated/agent run is counted as a participant.

Only then may the pair's adoption disposition be `PASS`. This is separate from model
quality, performance, registry publication, and the overall release decision.
