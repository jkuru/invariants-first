# Ground truth, written 2026-11-10 before any change was applied or CI was run.
Categories: C = defect breaking a property a written rule covers; U = defect no rule covers; B = benign.
"Expected" is my prediction, recorded for honesty; the measured result is what counts.

## pool-service
P01 C  R-1   work._allocate hands out one remainder too many ([:left] -> [:left+1]). Shares no longer sum to total_jobs.
             expected: runtime guard R-1; baseline tests (examples, hypothesis property) also catch.
P02 C  R-20/H-1 bypass: new bulk-lease endpoint POST /pools/{id}/leases/bulk loops store.insert_lease directly,
             skipping the lease_slots door (and H-1's 1..64 limit). openapi.json regenerated. No new test.
             expected: inv scan (R-20 sink/door). Over-lease still stopped by trigger; H-1 bypass not caught by anything.
P03 C  R-21  approve_resize records the proposer as approved_by (wrong variable). Every approval is now a self-approval;
             nothing can ever be applied.  expected: runtime guard R-21 (happy-path tests get 422). Baseline with
             guards off: the happy path passes, self-approval falsifier fails only because it asserts rule id.
P04 C  R-22 bypass by weakening the rule: R-22 growth cap 4x -> 10x in invariants.yaml, falsifier test edited to 10->150
             so it still passes. expected: inv lock --check only (rule hash). After a re-pin, nothing.
P05 U        GET /pools/{id} reports leased = number of leases instead of the slot sum. expected: baseline test
             (test_concurrent_leases asserts leased == 48).
P06 U        approve_resize drops its PENDING-state check, so an APPLIED proposal can be re-approved and replayed.
             expected: nothing catches.
P07 U        split endpoint labels shares f"lease-{lid + 1}" (off-by-one in an unguarded path). expected: nothing.
P08 U        record_job passes elapsed_s and timeout_s swapped to store.insert_job (job facts recorded wrong, so R-7
             later reasons over wrong data). expected: nothing in CI.
P09 B        store.leased refactor (named query constant). expected: lock re-pin (store_py module hash).
P10 B        new GET /pools list endpoint, with a test and regenerated openapi.json. expected: lock re-pin (pool_api).
P11 B        perf: _allocate uses heapq.nsmallest instead of a full sort. expected: lock re-pin (work_py).
P12 B        add a logging.info line to the lease endpoint. expected: clean pass, no re-pin.

## fact-pipeline
F01 C  K-2 (statement: a quote is at its source offsets)  build_proposal stores evidence end offset one short
             (chunk.start + end - 1). K-2 is enforced only on the model reply, not on stored evidence.
             expected: baseline test test_evidence_offsets_are_absolute_into_the_source; no rule fires.
F02 C  K-1/K-4/K-7/K-11/K-18 bypass: new Store.bulk_import() writes rows with insert_fact_row directly, skipping
             ingest_door. No new test. expected: inv scan (K-1 sink/door). K-1 trigger still enforces evidence.
F03 C  K-12  Store.support drops the trust='trusted' filter (an "SQL simplification"). The K-12 provider now counts
             untrusted publishers, so the guard approves. expected: baseline tests
             (test_k12_untrusted_second_source_does_not_count, hypothesis property); the guard itself cannot see it.
F04 C  K-12 signature change: promote_door(self, fact_id) -> promote_door(self, fact_ids: list[str]) for batch
             promotion; the K-12 provider binding (args.fact_id) no longer matches. expected: lockfile signature
             pin and the load check (inv validate).
F05 U        audit_sample uses retry_count > 1 instead of > 0 (off-by-one; one-retry facts escape sampling).
             expected: baseline (test_worked_flow, test_cli_flow).
F06 U        Store.retrieve returns k-1 chunks ([:k] -> [:k-1]). expected: nothing.
F07 U        extraction review payload drops the "error" and "attempts" fields (lost diagnostics for the human
             reviewer; analogue of a dropped log line). expected: nothing.
F08 U        Store.candidates drops mention.lower(), so capitalised mentions never match stored aliases and the
             resolver mints duplicate entities. expected: baseline (test_worked_flow).
F09 B        Store.facts refactor to a comprehension. expected: lock re-pin (store_py).
F10 B        new CLI subcommand `stats` (facts per status), with a test. expected: clean pass.
F11 B        perf: Pipeline.extract uses list.extend. expected: lock re-pin (pipeline_py).
F12 B        new predicate founded_in (non-functional): models.PREDICATES, the Literal, and K-4's list in
             invariants.yaml. expected: lock re-pin (rule hash, contracts) and a rule-file edit a human must review.
