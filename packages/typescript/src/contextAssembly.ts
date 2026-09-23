/**
 * Shared context-assembly renderer (contracts/operations.md Context assembly; constitution
 * Principle VI, NON-NEGOTIABLE) — mirrors context_assembly.py.
 */

export interface EvidenceBlock {
  evidenceId: string;
  sourcePointer: string;
  excerpt: string;
}

const DELIMITER_OPEN = "<<<CCI_EVIDENCE";
const DELIMITER_CLOSE = "CCI_EVIDENCE_END>>>";

function escapeDelimiters(text: string): string {
  return text.split(DELIMITER_OPEN).join("‹CCI_EVIDENCE›").split(DELIMITER_CLOSE).join("‹CCI_EVIDENCE_END›");
}

export function renderEvidenceContext(blocks: EvidenceBlock[]): string {
  return blocks
    .map((block) => {
      const escaped = escapeDelimiters(block.excerpt);
      return `${DELIMITER_OPEN} id=${block.evidenceId} source=${block.sourcePointer}\n${escaped}\n${DELIMITER_CLOSE}`;
    })
    .join("\n");
}
