// Shared term-normalization contract: TypeScript must match every row test_term_normalization.py checks.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { ftsTermExpression, queryTerms } from "../../packages/typescript/dist/relevance.js";

const { cases } = JSON.parse(await readFile(new URL("../../spec/fixtures/term-normalization.json", import.meta.url), "utf8"));

for (const row of cases) {
  test(`term normalization: ${row.word}`, () => {
    const terms = queryTerms(row.word);
    assert.deepEqual(terms, row.query_terms);
    assert.equal(terms.length ? ftsTermExpression(terms[0]) : null, row.fts_expression);
  });
}
