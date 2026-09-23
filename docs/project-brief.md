# ContIndex: preserve the conversation, recover the evidence

## Product position

ContIndex is an embedded conversation-memory library for applications that need to resume a
conversation and supply relevant original messages to an agent. The application owns identity,
authorization, model calls, and its chat loop. ContIndex provides durable history and source
evidence; public context APIs combine recent conversation with older tree-retrieved context.

The reusable product is a memory layer for existing RAG chats and agents. The
[integration guide](rag-and-agent-memory.md) covers Python and native TypeScript, explicit
hierarchical indexing, provider adapters, and host responsibilities. Lower total model cost
is a hypothesis to test after counting indexing and routing overhead.

The initial use case is a brand assistant whose user returns after a break, refers to an earlier
decision, or changes an offer. The intended benefit is fewer repeated instructions and fewer
manual corrections. Those benefits are hypotheses until measured in a real workflow.

## The engineering work sample

**Question:** does reserving space for recent messages help an assistant recover corrections and
follow-ups while retaining access to older evidence?

The [example](../examples/python/brand_memory.py) stores two synthetic brands in separate SQLite
histories, exits the writing process, and prepares context in another process. One brand changes
its workshop price from $499 to $799; the other has an independent $199 offer. No model is called.

The host-side [context helper](../examples/python/conversation_context.py) selects up to four
recent messages and fills remaining slots with lexical matches. It deduplicates source messages,
renders them in stored order, and limits the final evidence text to eight messages and 4,000
Unicode characters, including labels. Each excerpt is capped at 200 characters. Limits and
omissions are visible to the caller. These character limits are not model token limits.

That lexical-only helper now delegates to the public `cci.memory` API while retaining its original
evaluation policy. The caller must select an authorized history and serialize turns. It preserves separate statements and corrections;
it does not automatically decide which statement is true or convert conversation into a profile.

## Observed evidence

The [development evaluation](../evaluations/README.md) compares three approaches on the same
eight answerable cases, plus two cases with no supporting evidence, under the same output limits:

| Approach | Cases containing all annotated source evidence |
|---|---:|
| Most recent eight messages | 6 / 8 |
| Current lexical retrieval | 4 / 8 |
| Four recent messages plus lexical retrieval | 7 / 8 |

All three stayed within the example's context limits and returned records from only the selected
history in these cases. The lifecycle checks preserved both histories and safely replayed the
ingestion receipt. The separate demonstration used two different processes. Five example tests
cover persistence, bounded rendering, escaped delimiters, separate histories, and changes during
assembly; seven existing persistence/ingestion tests also passed locally.

**Known regression:** the combined approach missed a timezone preference outside its four-message
recent reserve. The eight-message recent baseline retained it. Recency recovered the price
paraphrase in this fixture; this does not demonstrate semantic search. The fixture was authored
while developing this example, so these results are not held-out accuracy, release-gate evidence,
or a comparison with a commercial product. Answer quality and model abstention were not tested.

## Relevance to UgenticAI

Research reference: September 22, 2026. UgenticAI's CloneIQ documentation describes separate brand
conversations and knowledge, and refinement of a brain clone through user feedback. This makes
decision recall, later corrections, and keeping brands separate plausible evaluation topics.
Their documentation does not establish that these behaviors are broken:

- [Understanding Brands and Second Brains](https://ugenticai.zendesk.com/hc/en-us/articles/53766361089819-Understanding-Brands-and-Second-Brains)
- [Training Your Brain Clone](https://ugenticai.zendesk.com/hc/en-us/articles/53768918677147-Training-Your-Brain-Clone)

The [published AI Developer role](https://www.linkedin.com/jobs/view/ai-developer-at-ugenticai-4435249412)
listed TypeScript, Next.js, Supabase/PostgreSQL, Inngest, and Claude, along with practical use of
coding agents. It was marked closed when researched. That posting is evidence about a described
role, not verification of every CloneIQ service's architecture or a current hiring opening.

The present SQLite library is not a demonstrated integration with their infrastructure. A useful
next work sample would exercise the native TypeScript memory API inside a host using their stated
stack and its authorized history. The source implementation now passes the same tree-memory
fixture in Python and TypeScript. A Supabase
adapter or production deployment would need a confirmed requirement and explicit design; neither
is implied by this demonstration.

## Next work, in order

1. **Verify recall on new conversations.** Freeze an independent evaluation set containing old
   decisions, corrections, vague follow-ups, irrelevant matches, and absent answers. Compare at
   equal context budgets, report every category, and retain the existing specification's quality
   gates. Do not tune on the held-out cases or describe this development probe as SC-011.
2. **Verify native TypeScript behavior.** Run the same fixture through the TypeScript implementation
   from a packed artifact. Require matching source identity, ordering, history separation, and
   limits. Dual-language package readiness remains unverified by this work sample.
3. **Complete execution integration.** Specify recent exchange selection, source roles, tool
   call/result grouping, and checkpoint handling. The context API now accepts a model tokenizer
   for its memory budget. Keep historical material inside evidence
   boundaries. A message-count window alone does not guarantee a complete tool exchange.
4. **Measure a real model workflow.** Test whether selected evidence produces the right answer,
   whether corrections are respected, and whether missing facts remain unspecified. Measure input
   tokens, latency, and editing effort; a source-reference check alone cannot prove these outcomes.
5. **Demonstrate a small integration in the target stack.** Choose the host interface with the team,
   implement one vertical slice, and explain deployment/storage assumptions. The current deployment
   target is one owning process per history on durable local storage.

The [tree-mechanics report](../evaluations/results/tree-memory.json) additionally demonstrates
bounded hierarchical retrieval and incremental summary reuse. It separates index and routing
work from final context size; its deterministic provider does not establish real-model recall
or dollar savings. The library restores conversation context, while the host restores task and
tool execution checkpoints.

## How to present the work

A concise, supportable description is:

> I built a durable conversation-memory prototype and tested a small way to combine recent
> conversation with older source evidence. On eight synthetic development cases, it recovered
> the required evidence in seven cases, compared with six for recent history alone and four for
> keyword retrieval. I also found and documented a regression. I can show the implementation,
> failure tests, and what still needs validation before integration.

Walk through the real process restart, an old decision, a recent correction, and the failing
timezone case. Explain why the limits exist, how the caller selects the history, and how a clear
invalidates context being assembled. Present the actual agent-assisted changes and verification
commands without inventing a development history or implying company endorsement.
