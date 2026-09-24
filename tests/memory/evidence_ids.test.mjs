// Evidence IDs encode retrieval priority; minting and parsing live together (plan-eng-review D9).
import assert from "node:assert/strict";
import test from "node:test";
import { evidenceId, evidenceRank } from "../../packages/typescript/dist/retrieve.js";

test("evidence id round trip", () => {
  for (const rank of [1, 2, 9, 10, 4999]) assert.equal(evidenceRank(evidenceId(rank)), rank);
});

test("malformed evidence id is rejected", () => {
  assert.throws(() => evidenceRank("ev_x"), RangeError);
});
