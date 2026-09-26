# "Your writes are cheap because they do less"

This is the strongest objection to Echo Memory's design, it is correct on the
mechanism, and it deserves a real answer rather than a slogan. What follows is
the answer, with what is measured separated from what is argued.

## The objection, stated properly

Echo Memory invokes no model when it stores a fact. Zep and Graphiti, the
closest architectural match, describe their own ingestion as "every episode
triggers multiple LLM calls", with "write cost scales with volume". So the
comparison is not really 15ms against 2 seconds. It is: they extract entities
and relations from raw text, and Echo Memory requires the caller to arrive with
entities and facts already extracted.

Put that way, "0 model calls per write" stops being a feature and starts
looking like an unpaid bill. The work did not vanish. It moved.

## The concession

Correct. The work moved, it did not vanish, and anyone reading "0 model calls
per write" as "extraction is free" has been misled. The number is a claim about
the server, and only about the server.

Any honest version of this argument has to begin there, because the rest of it
depends on the move being a good trade rather than on the cost being absent.

## Why the move is not neutral

**The agent already read the conversation.** A server side extractor receives
an episode as text, cold, with none of the session that produced it: not what
the user corrected two turns earlier, not which of three candidate designs was
actually chosen, not that the last line was sarcastic. The calling agent has
all of it in context, because it just had the conversation. Extraction there is
marginal on tokens already paid for, and it is done by the party with the most
information rather than the least.

**A write that calls no model cannot fail for a reason you do not control.**
Median write latency is 15ms and the only dependency is a database
(`echo-memory benchmark`). A write path with a model call in it inherits that
provider's availability, rate limits, key rotation and pricing, at precisely
the moment an agent is trying to record something it just learned. Memory that
is unavailable when the insight happens is memory you do not have.

**The cost shape is different, not just the cost.** Server side extraction
scales with write volume, by its own authors' description. Echo Memory's server
cost per write does not, and the cost that does scale sits inside a context
window the user is already paying for, which is also the only place it can be
spent once rather than twice.

That paragraph originally said the server cost per write was "flat", and it was
not. Measured on 2026-09-18, a write cost 29ms into a store of 1,057 nodes and
129ms into one of 24,054, and throughput over a single ingest fell from 28
writes a second to 8. Two of the three causes were defects rather than
properties: a neighbourhood lookup that walked every fact edge in the database
on every write, and an entity lookup that scanned every node because it went
through Cypher and could not reach the index built for it. Both are fixed, the
same write is now 45ms at 24,054 nodes, and the remaining growth is the vector
index getting larger, which is real and is not free.

It is left in rather than quietly edited because the claim was published before
it was measured, and the correction is more useful than the original sentence
was.

**Provenance means something different when a person's agent asserted the
fact.** `echo-memory why <fact_id>` can answer "who believed this, in which
project, when, and which reads returned it" only because a named agent made the
assertion. A fact extracted by a server side model is the server's inference,
attributed to nobody, and there is no useful answer to "who thought so".

## What the choice actually costs

This is the part that makes the argument worth anything, because every item
here is a real bill and two of them are measured.

**Extraction quality is now entirely the caller's.** A careless agent writes
careless facts and nothing on the server catches it. There is no second opinion
in the system by design.

**It is a contract, and agents do not reliably follow contracts.** Measured on
this author's own store: the Stop gate, the mechanism whose entire job is to
force capture at the end of a session, fired seven times and produced one fact.
That is the design's actual bill, it is not small, and it is why capture became
a hook rather than an instruction.

**Work pushed to the caller takes its failure modes with it.** Measured on
2026-09-17 while ingesting LoCoMo: `write_episode` defers every fact touching
an entity whose resolution is ambiguous, and returns normally while doing it.
One conversation had two speakers, Tim and John, whose names scored inside the
ambiguous band. Every one of John's 344 turns was silently dropped, 680 turns
became 336 facts, and the ingest reported success. A server that does not think
about your write is also a server that does not notice when your write was
wrong.

**A benchmark that feeds raw transcripts measures this design with its
extraction step removed**, which is the worst case for it. That number is now
published rather than avoided: on LoCoMo, 5,882 unfiltered dialogue turns and
1,982 questions, recall@10 is 0.601 and hit@10 is 0.658 with no extraction
whatsoever. See [BENCHMARKS.md](BENCHMARKS.md). It is a floor, and it is worth
printing precisely because it is unflattering.

## What would settle it

The claim that the calling agent extracts better than a server side model would
is a reasoned position, not a measured one, and it should not be quoted as
though it were measured.

The experiment that settles it is the same corpus twice, scored the same way:
once with facts the agent chose, once with raw turns. The raw half exists now
and is reproducible with `echo-memory eval-external locomo`. The extracted half needs a
model key and a stated extraction prompt. Until both halves exist, the position
above is an argument about where information lives, supported by a latency
number, a failure rate and a cost shape.

## The short answer

Yes, the writes do less. The work is done by the thing that already read the
conversation, which is cheaper and better informed than a server reading it
cold. What is proven today is that the server cost is flat, the write path has
no external dependency, and the failure modes moved to the caller where they
are easier to miss. What is not yet proven is that agent extraction beats
server extraction on quality, and that gap is stated rather than papered over.
