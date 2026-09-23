"""Shared context-assembly renderer (contracts/operations.md Context assembly; constitution
Principle VI, NON-NEGOTIABLE; PRD §11).

Retrieved message content is rendered into a clearly separated evidence structure before it is
included in any request sent to a model — never concatenated as if it were an instruction to
the model itself. This is PRD §6's already-specified "bounded context assembly" sub-block of
the `ContextIndex` API and §11's "shared renderer," not a new component (T086 task note).

Consumers: `retrieve()`'s `tree`/`auto` routing modes (navigation prompts) and `ask()`'s
synthesis prompt — both pass retrieved content through this one shared component. `index()`'s
classification/summary prompts are equally in scope but reach this renderer through the same
shared provider path, per contracts/operations.md.

Reducing prompt-injection risk, not eliminating it (constitution Principle VI): this is not a
claim that injection is impossible.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EvidenceBlock:
    """One retrieved item, delimited and labeled with its own source pointer — never inlined
    as if it were developer-authored instruction text."""

    evidence_id: str
    source_pointer: str
    excerpt: str


_DELIMITER_OPEN = "<<<CCI_EVIDENCE"
_DELIMITER_CLOSE = "CCI_EVIDENCE_END>>>"


def _escape_delimiters(text: str) -> str:
    """Neutralizes any occurrence of this renderer's own delimiter tokens inside retrieved
    text, so retrieved content can never forge a fake evidence-block boundary."""
    return text.replace(_DELIMITER_OPEN, "‹CCI_EVIDENCE›").replace(
        _DELIMITER_CLOSE, "‹CCI_EVIDENCE_END›"
    )


def render_evidence_context(blocks: list[EvidenceBlock]) -> str:
    """Renders `blocks` into one clearly delimited text region, each evidence item labeled with
    its `evidence_id`/`source_pointer` and fenced so a model reads it as quoted material, not
    as an instruction. Tool calls recorded in old messages are never re-executed here — only
    their already-rendered text projection is ever passed through."""
    rendered = []
    for block in blocks:
        escaped = _escape_delimiters(block.excerpt)
        rendered.append(
            f"{_DELIMITER_OPEN} id={block.evidence_id} source={block.source_pointer}\n"
            f"{escaped}\n"
            f"{_DELIMITER_CLOSE}"
        )
    return "\n".join(rendered)
