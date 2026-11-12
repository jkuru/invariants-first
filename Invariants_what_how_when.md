# Invariants: what, how, when

An invariant is a state that can never exist, together with something that stops the system reaching it. This guide says what that means, how a rule is written so a system can run it, where it is applied, and when.

## The problem: more output than people can read

An agent works faster than a person can check. The output is massive, and a human is still the only thing standing between a wrong result and a decision. Two uses show it.

**Code development.** A coding agent returns a change touching dozens of files in minutes. A reviewer cannot trace every line, so they skim, run the tests, and approve. The tests cover the cases someone thought of. A change that breaks a rule nobody wrote a test for goes through.

**Information to knowledge.** A pipeline reads thousands of documents and proposes thousands of facts and links. A person cannot read every source to confirm every fact. They sample, and a wrong fact in the part they did not sample becomes something the system believes.

The same thing goes wrong in both: **review does not scale with output**. Reading everything is impossible, and reading a sample misses the cases that matter.

Invariants change what a person has to check. Instead of checking every output, they write down the states no output may be in, and a machine checks each output against them, every time, at the cost of one check:

| | Without invariants | With invariants |
|---|---|---|
| A reviewer reads | every change, or a sample | the rules once, and only the changes that broke one |
| Code example | "does this diff look right?" | "no module in ui imports from db" is checked on every diff |
| Knowledge example | "is this fact in the source?" | "no fact quotes text absent from its source" is checked on every fact |
| What scales | the reviewer's hours | the check |

A person's attention moves from thousands of outputs to a few rules and the exceptions.

This does not make the output correct. A rule says an output is valid, not that it is right (see "Valid is not correct" in section 4). What it does is remove the whole class of mistakes a machine can see, so the person spends their time on the ones only a person can.

The rest of the guide says what such a rule is, how to write it so a machine can run it, and where to run it.

## 1. What an invariant is

**An invariant is a state that can never exist, together with something that stops the system reaching it.**

A rule is written as a prohibition: *this state never exists*. That makes the reader ask "what state would be a bug?" and not "what should the system do?", so the rule stays about the code and away from policy. In the literature this is a *safety property*: a bad thing never happens [3]. What the system must eventually do is a different kind of claim, and is not an invariant.

You already use invariants. A compiler is an invariant checker:

```
int age = "forty"; // compile error
```

The prohibited state is *a variable declared int holding a string*. You never write a test for it, and no input, call order or retry can break it, because the compiler refuses to build a program that could. That state cannot exist, and something enforces that.

A type covers one kind of rule. Your own code has more, and the compiler cannot see them. Take a stack:

```python
class Stack:
    def __init__(self):
        self.items = []
        self.size = 0

    def push(self, x):
        self.items.append(x)
        self.size += 1

    def pop(self):
        self.size -= 1   # bug: size changes before items.pop() can fail
        return self.items.pop()
```

The prohibited state is a stack where `size != len(items)` or `size < 0`, after construction or after any public method. That is a *class invariant*, as design by contract defines it: ensured by every constructor and maintained by every exported routine [24]. The state may exist for a moment inside a method. It never exists once the method exits.

Nobody can see this rule in the code, because it lives between two fields. `pop` on an empty stack breaks it: if `size` goes to -1, then `items.pop()` raises, and the stack is left in the bad state. A test only finds that if someone thought to try an empty stack.

To enforce the rule, add a **guard**: a check that runs when each public method exits, including when it raises, and refuses the result if the prohibited state exists.

```python
def _check(self):
    if self.size != len(self.items) or self.size < 0:
        raise InvariantViolation("Stack: size and items disagree")
```

Call `_check()` from a `finally` block (or a decorator) in every public method. A plain `assert` is skipped when Python runs with `-O`, so it is not a guard.

Two questions decide whether something is an invariant:

1. **Can the prohibited state exist?** For any input, any order of calls, any retry. Not just for the cases you tried.
2. **What stops it?** A compiler, a type, an assertion, a guard. If the answer is "a comment" or "the prompt", it is only a wish.

The same idea has other names. A *precondition* and a *postcondition* prohibit a bad state when a function is entered and when it returns, and with the class invariant they make a *contract* [2]. A *loop invariant* prohibits one at the top of every iteration [1]. A *type* prohibits a value of the wrong kind. An invariant is the general form. This guide is about writing them as rules a system can check, including for functions that call a model. Runtime enforcement for agents has the same shape: a trigger, a predicate and an action that stops the step [25].

### Impossible is not illegal

Chess shows the difference between an invariant and a business rule.

| | A bishop moves straight | Two pieces stand on one square |
|---|---|---|
| What it is | An illegal move: a rule of the game | An impossible state: the program is broken |
| Can the outside world cause it? | Yes, a player can try it | No input can produce it |
| What happens | Reject the move and carry on | Stop, and report a bug |
| Does it change with the game? | Yes (Chess960, other variants) | No, it is a bug in every variant |

Business rules are the rules of the game: they can be broken by a user's choice, and they change when the business does. An invariant is the rule of the machinery. "Orders over $100 ship free" is a policy someone can change next quarter. `size != len(items)` must never happen whatever the policy is, because the code breaks when it does.

A test: if someone could change the rule without changing the design, it is a business rule. If the code's correctness rests on a domain fact, such as code that calls `king_square()` never meeting a board where a side has no king or two kings, that prohibition is also an invariant, and the reason is the code, not the game.

### A test is not an invariant

A test checks one case. An invariant covers all of them.

| A test says | An invariant says |
|---|---|
| `sort([3, 1, 2])` returns `[1, 2, 3]` | `sort(xs)` never returns a list that is out of order or has different elements from `xs` |

### What a rule can be about

A compiler already enforces some of every kind of rule. What it cannot see, you enforce with a guard. A guard runs on every move, so the rule has to be cheap enough to compute there. That decides where each rule is checked.

| A prohibition about | The compiler already enforces | The state you prohibit | Checked | Cost per move |
|---|---|---|---|---|
| A value | `int x = "forty"` does not compile | `index < 0` or `index >= len(items)` | at runtime, on access | O(1) |
| One call's input and output | `sort(List<Integer>)` rejects a String | a result that is out of order | at runtime, on return | O(n): fine for small lists, sample or move to tests for large ones |
| One call's input and output | | a result with different elements from the input | in tests, with random inputs | O(n log n): too costly on every call |
| One call's input and output | | `binary_search` given a list that is not sorted | in tests | O(n): too costly on every call |
| Data across calls | a private field can only be changed by the class's own methods | `size != len(items)` after any method | at runtime, after each method | O(1) |
| Data across calls | | in a doubly linked list, a node where `node.next.prev` is not `node` | in tests | O(n) per method: too costly on every call |
| Order in time | `int x; print(x);` does not compile ("might not have been initialized") | a lock released while held is false: keep a held flag and test it | at runtime, before the call | O(1) |
| A loop | the body type-checks | "the loop never ends" cannot be decided in general, so prohibit a state you can see: in a binary search, `lo > hi` at the top of an iteration | at runtime or in tests | O(1) |
| The code itself | private, final, module visibility | a module in `ui` that imports from `db` | at build time, by scanning the code | none at runtime |

All of these are about how the code is built, so none is a choice a product owner makes. The compiler examples are Java; most languages check some of these and not others.

Every prohibition here has something that enforces it. They differ in when the check can afford to run:

- **Every move, at runtime.** The check is O(1) or close to it. This is the guard.
- **Sampled, or only in tests.** The check is exact but costs a full pass. Run it on a share of calls, or on random inputs in the test suite.
- **At build time.** The rule is about the code, not the data, so scan the code once and pay nothing at runtime.
- **Not at all.** If a prohibition cannot be computed ("the loop never ends"), rewrite it as one that can (`lo > hi` never happens). If it cannot be rewritten, it is a wish.

This guide is about writing rules so each one lands in the right bucket, and about functions that call a model, where the compiler checks nothing about the answer.

### What an invariant is not

| It is not a | Because |
|---|---|
| Business rule | "Orders over $100 ship free" is a policy the business sets and changes |
| Goal | "Replies should be friendly" is only sometimes wanted |
| Procedure | "Run the migration, then restart" is a one-off step |
| Test case | It checks one chosen input |
| Eval | "78% of proposals were right" is measured over many runs, never true or false of one |
| Prompt instruction | "Always answer in valid JSON" is a request, and nothing stops the model ignoring it. Parsing and validating the answer is the guard |

### Four limits to know now

- **A guard only covers the paths that go through it.** A path around every guard is outside the claim. Such a path is marked `unguarded`, and the rule stays a candidate.
- **The check and the change must happen as one step.** If another change can slip in between, the rule can break after the check passed. Section 7 covers this.
- **It only prohibits.** An invariant says what never happens. A claim that something must happen, such as "the retry eventually succeeds", is an obligation: it is not an invariant, and it needs a different kind of test [3].
- **Invariants do not conflict with each other, but an obligation can conflict with one.** A set of prohibitions is satisfied together by a system that does nothing, so they cannot contradict one another. An obligation, such as "answer within 5 seconds", can collide with one. The invariant wins, and the obligation is met inside it (return "pending" instead of writing). Access-policy engines resolve the same collision the same way: in Cedar a satisfied forbid policy overrides any permit [23]. That is evidence for the ordering, not a claim that policy rules are invariants.

### The precise version (optional)

Think of the system as states and moves, where a move is a call, a write or a retry. Let B be a bad state. B never exists at the start, and no move reaches it: each move either avoids B or is refused. By induction, B exists in no state the system can reach [1]. For a single function the same idea is a contract: an input that meets the precondition never produces an output that breaks the postcondition [2].

A prohibition about order in time becomes a prohibition on stored state. "close() before open()" becomes "state != OPEN when close() is called", once the history is stored in a field.

## 2. How a rule is written

A rule is one record, written once. Everything that applies it reads the record, so the guard cannot drift from the rule.

The types used below (`Rule`, `Enforcer`, `Reads`, `Mechanism`, `Moment`, `OnViolation`) are defined in Appendix A.

```
Rule(
    id="R-1",
    statement="split_work never returns a share below zero, a share above its worker's capacity, or shares whose sum differs from total_jobs.",
    bad="sum(parts) != total || any_outside(parts, caps)",  # a CEL expression, true when the prohibited state exists (see "The two tools")
    reads=Reads.CALL,
    target="jobs.split_work",
    bind={"total": "arg.total_jobs", "caps": "arg.capacities", "parts": "result"},
    when="after",
    why="A rounding bug here corrupts every API call and job record above it.",
    enforcers=(
        Enforcer(Mechanism.GUARD, Moment.BEFORE_EFFECT, "any call to split_work", OnViolation.REJECT),
    ),
    falsified_by=("test_r1_split_work_never_returns_bad_shares",),
)
```

| Field | Meaning |
|---|---|
| id | The rule's name, such as R-1 |
| statement | The rule in words |
| bad | The prohibited state, as an expression that is true when that state exists |
| reads | What bad looks at: a value, a call, stored state, history, a loop or the code |
| target | The function, API method or file it attaches to |
| bind | Which names in bad mean which arguments or result |
| when | before the call (a precondition) or after it (a postcondition) |
| parse | For a model call: the closed schema the reply text is parsed into before bad is evaluated. Not used for a plain function. |
| enforcers | Each enforcer on the rule: its mechanism, what it covers, when it acts, and what happens on a break |
| unguarded | Known moves that can still break the rule |
| falsified_by | The tests that can fail the rule |
| why | The harm the rule prevents |
| on_violation | What happens on a break: reject, retry, escalate, halt, block the merge or detect. An enforcer can set its own, which overrides this one for that enforcer. |
| owner | The person or team who answers for the rule |
| retries | For retry: how many tries before it escalates |

**A rule is an invariant** when it has an enforcer, no known unguarded move, and a test that can fail it. Everything else is a candidate. The backlog is the set of candidates, computed from the rules and never kept as a second list.

**A rule cannot be given to a mechanism that cannot see what it looks at.** A rule about stored data cannot be held by a syntax-tree pattern.

### Where an enforcer goes

An enforcer is what refuses the prohibited state. Where you put it decides which breaks it can see.

| The bad state is refused | What happens |
|---|---|
| In one caller | Any other caller skips it |
| In a prompt | The model can ignore it |
| In the function or API boundary | No caller can skip it |
| In the database or the type | The bad state is refused even when the application has a bug, or another program writes to the table |

A database constraint is an enforcer: `NOT NULL`, `UNIQUE` and `CHECK` refuse the bad write, so the bad state cannot exist. An invariant is the same idea for any rule, in any layer.

A rule is as strong as its weakest unguarded path. If you know a way around every enforcer, list it in `unguarded`, and the rule is only a candidate, not yet an invariant.

### The two tools

- **CEL** (Common Expression Language) is a small language for one expression over the data you hand it [8]; in this guide it is true when the prohibited state exists. The standard environment has `in`, `size`, `contains`, `matches` (RE2 regular expressions, matching a substring), `has` and list macros such as `all`. It has no `sum`, so `sum` and `any_outside` are functions the guard library supplies. The CEL project maintains `cel-go`, with Java and C++ implementations alongside it; others, such as Python's, come from third parties, so check that a runtime exists for your language.
- **A syntax-tree pattern** (tree-sitter parses many languages into concrete syntax trees; ast-grep searches those trees with code patterns, and Semgrep also matches code patterns across many languages) looks at source code, in any language with a grammar.

## 3. The flow

One rule record drives every step. Nobody retypes it as a guard or a test.

```
1 WRITE     rule record (protected file)     bad = a CEL expression, target = where it attaches
2 BUILD     weave: insert a guard call at the target
3 BUILD     scan the woven code with syntax-tree patterns
            - any target without its guard?  yes -> fail the build
            - forbidden code present? (a call to save_lease outside acquire_slots)  yes -> fail the build
4 TEST / CI (continuous integration) property tests run the same bad expression on generated inputs,
            against the variant
            state rules use an in-memory store and an oracle (Appendix A)
            plain and variant get the same inputs: outputs never differ unless a rule fires
            each storage constraint is tested against bad (section 7)
5 SHIP      two artifacts from one source
            plain   = reviewed source as it is (kept for comparison)
            variant = source + guards   <-- this one ships (built at step 2)
6 RUN       every call: guard evaluates bad
            bad absent -> go on
            bad present -> enforce setting: reject / retry; the harness escalates or halts
                           detect setting: report only
7 LIVE      a monitor evaluates bad on stored data and reports breaks
```

| Step | Who does it | What it uses |
|---|---|---|
| 1 | A person (an agent only with review) | The rule record |
| 2 | The weaving pass | target, bind, when |
| 3 | The pattern scan | Syntax-tree patterns |
| 4 | CI | The bad expression, the variant and the plain artifact |
| 5 | The build | The woven source |
| 6 | The guard in the variant | The bad expression |
| 7 | The monitor | The bad expression |

Three mechanisms hold a rule:

- **Pattern** looks at the code. It acts at build time, or on code written at runtime before that code runs. Its outcome is to fail the build (`block_merge`), or to refuse to run the code.
- **Guard** looks at a value, a call, stored data, history or a loop. It acts on every live call. It refuses the call (`reject`), asks for a retry (`retry`), or reports (`detect`). The harness around it escalates to a person or halts the run when retries are spent or the rule says so.
- **Storage** is a constraint or trigger in the database, applied when the schema is deployed. It looks at stored data and acts on every write, including concurrent ones (section 7). It is written by hand and tested against the rule's bad expression.

Property tests (step 4) and the monitor (step 7) evaluate the same bad expression but do not hold the rule. A test fails before ship, and the monitor reports after the effect.

The guard needs something to call it, and the pattern scan in step 3 fails the build when a target has no guard call.

**Enforce and detect.** A guard runs with one of two settings. `enforce` applies the rule's outcome. `detect` reports the break and lets the call through, so you can try a new rule before it blocks anything.

**Checking early is stronger.** From strongest to weakest: the bad value cannot be written down (by construction); a guard refuses it before the effect; a build gate refuses the code that could make it (before ship); it is found and reported after the effect. A convention ("we always do it this way") is the absence of a guard.

## 4. Agentic patterns and non-determinism

A model gives a different answer on each run. You cannot make it repeat. You can fix the set of results you accept, so no run, however different, ever ends with a result outside the accepted set.

**The model proposes. Code checks. Code decides.** The bad expression is evaluated by a guard woven into the call by the build (section 3), so the model cannot edit it.

```
model proposes --> woven guard checks the reply --> code decides
                                                   accept | retry with the problem | escalate | halt
```

**Escalate** hands the case to a person. **Halt** stops the run.

### The four patterns

An agentic system is built from four parts. Each is a target, and each rule about it is a rule record like any other (section 2).

| Pattern | Target | when | Rule | reads | On break |
|---|---|---|---|---|---|
| Node: one model call or function | The function that calls the model | after | A reply that does not fit a closed typed model is never used, and the model never supplies an id (code computes them). | call | retry, then escalate |
| Edge: a handoff between nodes or agents | The function that passes content on | before | An unchecked field never crosses, and neither does untrusted text. | call or state | reject |
| Loop: plan, act, check, repeat | The function that runs the rounds | before | A fourth round never starts, and the same problem never comes back. The harness keeps the round count and the last problem for the run, and the bad expression reads them. (Appendix A implements the cap only.) | loop | escalate or halt |
| Graph: the whole workflow | The function that writes the protected data | before | A write without an APPROVED approval never happens. | state | reject |

Some rules are about the code around the model. A pattern stops these, within the limit that it sees static calls only (section 8):

- No model call outside a guarded function.
- No model client is built outside the one factory, which grants no tools.
- No function except the single door, the one allowed to write the protected data, calls the writer.
- No agent function is called from anywhere but its declared handoff function.

A guard stops a call from receiving a prohibited input. When several agents vote on one question, no vote's input contains another voter's output.

### From text to a checked value

A model returns text. The woven guard turns that text into something it can compare in two steps.

```
model reply (text)
{"cause": "TIMEOUT"}
        |
        v
+-------------------------------+
| STEP 1  PARSE  schema: CauseReply |
| valid JSON?  required field?  extra field? |
+-------------------------------+
        | no                          | yes
        v                             v
   violation                    typed value: reply.cause = "TIMEOUT"
   retry, feeding the problem back;        |
   escalate at the cap                     v
                                     +-------------------------------+
                                     | STEP 2  COMPARE  CEL bad      |
                                     | closed set | string shape     |
                                     | vs data code holds | vs source text |
                                     +-------------------------------+
                                             | bad: true          | bad: false
                                             v                    v
                                        violation            accept: the reply
                                        retry, feeding...    goes on
                                        escalate at the cap
```

Four replies through the same guard:

| The model returns | Step 1: parse | Step 2: compare | Result |
|---|---|---|---|
| `{"cause": "TIMEOUT"}` | passes | bad is false: cause is in the set | accept |
| `{"cause": "BROKE"}` | passes | bad is true: cause is not in the set | retry: "R-7: the reply never names a cause outside the closed set" |
| `{"cause": "TIMEOUT", "pool_size": 40}` | fails: extra field pool_size | not reached | retry: "reply fields differ from 'cause'" |
| Sure, I'll restart it! | fails: not JSON | not reached | retry: "reply is not JSON" |

**Step 1: parse.** The guard parses the reply into fields. It uses the structure the call already asks the model for, such as JSON checked against a closed schema.

- If the text won't parse, a required field is missing, or an extra field is present, that is a violation. It is applied as the rule's `on_violation` (for R-7, retry), with the parse error fed back.
- The guard does this before bad is evaluated, so the expression only ever sees typed fields and never raw prose.
- A schema with no `pool_size` or `id` field makes it impossible for the model to supply them.

**Step 2: compare.** The CEL bad expression runs on the parsed fields. There are four kinds of comparison.

| Kind | Example bad | What it catches |
|---|---|---|
| Closed set | `!(reply.cause in ["TIMEOUT", "OUT_OF_MEMORY", "BAD_INPUT"])` | A cause the model made up |
| Shape of a string | `size(reply.message) > 500 \|\| reply.message.matches("(?i)rm -rf")` | Too long, or a forbidden command |
| Against data code holds | `reply.job_id != job.id \|\| reply.attempt != plan.attempt` | The model naming a different job or attempt than the records say |
| Against the source text | `!source.contains(reply.quote)` | A quote the model invented |

Comparing with data code holds is the strongest, because the model's words are checked against facts and not against another opinion.

A forbidden-phrase pattern is the weakest row. The model can rephrase around it ("remove -r -f"). Prefer a closed field the code turns into the message, as in Appendix A.

**What the guard cannot do.** It checks the form of the text and its agreement with known data. It cannot check meaning: whether a reply is polite, accurate or a good answer. There are three options:

- Leave meaning to the approving person, example tests and evals.
- Make the meaning checkable: ask the model for a field the code can compare, such as a category, a number or a quote, and not a free-text judgment.
- Use a second model as a judge. Its verdict is also non-deterministic, so code decides again, for example "code never accepts a reply that two of three judges reject". This is the weakest check of the three.

### One rule, from model call to run

```
Rule(
    id="R-7",
    statement="A proposed cause is never outside the closed set, and the reply never carries a pool size or id.",
    parse="CauseReply",                    # closed schema: only a cause field, extras rejected
    bad='!(reply.cause in ["TIMEOUT", "OUT_OF_MEMORY", "BAD_INPUT"])',
    reads=Reads.CALL,
    target="triage.propose_cause",         # the function that calls the model
    bind={"reply": "result"},
    when="after",                          # check the reply as it comes back
    on_violation=OnViolation.RETRY,        # feed the problem back, then try again
    retries=3,                             # after 3 retries, escalate
    ...
)
```

The build weaves a guard after `triage.propose_cause` returns, scans that no model call is left without one, and ships the variant. On each run the model replies differently and the guard parses the text into `CauseReply` (an extra `pool_size` field fails here) and then evaluates bad on the parsed reply. False: the reply goes on. True, or a failed parse: retry with the specific problem, up to retries, then escalate.

The model decides what to say. It does not decide what is accepted. A prompt is a request, not a guard: "never run a destructive command" is an invariant only when a woven guard refuses the command.

### At build time

The flow is the same when the proposer is an agent that writes code. It proposes a change to the source, and the weaving pass and the pattern scan check it. A failure fails the build (`block_merge`). A person reviews the rule file and any change to it, not every line the agent wrote. Code an agent writes at runtime is parsed and matched against the same patterns before it runs.

### Valid is not correct

The flow makes the output **valid**: well formed and inside the rules. It does not make it **correct**. A restart for the wrong cause, on the right job, is valid. Correctness stays with the approving person, example tests and evals. An eval measures a population of runs ("78% of proposals were right"); a failure that keeps appearing is a rule waiting to be stated.

## 5. Agents calling agents

In an agent graph, each agent is a node and each handoff is an edge. The flow of section 3 applies unchanged. The edge is the target: the function that takes agent A's output and passes it to agent B.

```
build log   (untrusted: read by the triage agent only)
        |
        v
[ triage agent ] model
        | text reply
        v
+-------------------------------+
| GUARD on the reply   R-7     |---- bad found --> retry with the problem,
| parse CauseReply; bad: cause not in set |        escalate at the cap
+-------------------------------+
        | typed reply
        v
+-------------------------------+
| GUARD on the handoff  R-8    |---- bad found --> reject: the remediation
| parse RemediationHandoff;    |                 agent never runs
| bad: job missing or cause not in set |
+-------------------------------+
        | typed handoff (the only thing the next agent sees)
        v
[ remediation agent ] model
        | text proposal
        v
+-------------------------------+
| GUARD on the proposal  R-9 (not written) |---- bad found --> retry, then escalate
| parse; bad: differs from the approval |
+-------------------------------+
        |
        v
person approves --> restart_job (the one door) --> storage

hop counter across the whole graph: no run exceeds the cap, and the cap escalates, so agents never hand a problem back and forth forever
```

The handoff function builds `RemediationHandoff` from the checked reply's cause and the job the session is about, which code holds. The model does not supply the job id.

Three things to read from it:

- **No arrow between agents lacks a guard.** The next agent never receives the previous agent's raw text, only a typed value.
- **Each guard is the same two steps as for a single call: parse, then test bad.**
- **Untrusted text stops at the first agent.** A field is no defence against instructions inside it, so the build log is not passed on. The next agent never receives a field that no guard has tested.

```
Rule(
    id="R-8",
    statement="A handoff from the triage agent to the remediation agent never carries a job that does not exist, a cause outside the closed set, or the build log.",
    parse="RemediationHandoff",            # closed schema: job_id and cause only; no field for the build log
    bad='!(handoff.job_id in jobs.ids) || !(handoff.cause in ["TIMEOUT", "OUT_OF_MEMORY", "BAD_INPUT"])',
    reads=Reads.STATE,                     # it compares with stored jobs; jobs is supplied by the guard library
    target="graph.hand_off_triage_to_remediation",
    bind={"handoff": "arg.message"},
    when="before",                         # check before the remediation agent runs
    on_violation=OnViolation.REJECT,
    ...
)
```

The rules about the graph's code (declared edges only, one door) are the pattern rules listed under the four patterns. No voter's input reaches a vote untested by a guard.

A loop that spans agents is a loop target like any other. No run exceeds the hop cap across the whole graph: the guard counts the hops and escalates at the cap, so two agents never hand a problem back and forth forever.

## 6. The library, and finding the rules

This guide describes one library-shaped design: one rule file, and parts that each read it and run in their own mode. Appendix A is a small teaching version of it. It is not the library.

```
your API + code          granularity
        |                (system ... endpoint ... function)
        v
+-------------------------------+
| DISCOVER  build the call chain, |
| a model proposes rules from it  |
+-------------------------------+
        | candidate rules
        v
a person approves
        |
        v
+-------------------------------+
| RULE FILE  protected          |
+-------------------------------+
        |         |         |         |
        v         v         v         v
   +-------+  +-------+  +-------+  +-------+
   | BUILD |  | TEST  |  | RUN   |  | LIVE  |
   | weave |  | property |  | woven |  | monitor |
   | + scan|  | tests |  | guard |  |       |
   +-------+  +-------+  +-------+  +-------+
   fail build  fail test   reject/retry/  report
                           report
```

| Mode | What it does | Reads | Outcome |
|---|---|---|---|
| Discover | Builds the call chain and has a model propose candidate rules from it | The code and a granularity | Candidates for a person to approve |
| Build | Weaves the guards, then scans the result | The rule file and the code | Fails the build |
| Test | Evaluates each bad expression on generated inputs and call sequences | The rule file | Fails the test |
| Run | The woven guard checks every call, with the enforce or detect setting | The rule file, as woven | Reject, retry or report (the harness escalates or halts) |
| Live | A monitor evaluates the bad expression on stored data | The rule file | Reports breaks |

A language needs four things: a tree-sitter program, a CEL runtime, a guard library, and a weaving rule for its returns and exceptions. The last two you write once per language. The first two exist for many languages.

### Discover: finding the rules

A person points the library at an API and its code and picks a granularity. It returns candidate rules. Nothing it returns is an invariant until a person approves it, an enforcer is bound, no known gap is left, and a test can fail it.

1. **Build the call chain (code, deterministic).** A tree-sitter pass maps who calls whom, from each entry point down to each effect (database write, outbound call, model call, process start). It records the order of the calls, the values passed along each one, and the checks already on the path.
2. **A model proposes rules from the chain.** It is given the chain, not the whole code base. For each call it is asked what must never happen there, and in what order.
3. **Code checks the proposal**, as it does any model output (section 4). The proposal is parsed into a candidate: a Rule plus its evidence. Then: - the target must be a call in the chain (a fact code holds); - the evidence the candidate carries must cite a call and line that exist (the source text); - the reads must be something its mechanism can see.
4. **Filter by evidence.** Run the surviving candidates with the detect setting, on the plain build, over real executions (tests, recorded traffic): - a candidate that a real run breaks is wrong, so drop it; - run them again on copies of the code with a seeded bug (mutants). A candidate that catches none says nothing, so drop it or rank it last; - rank the rest by how bad the effect it guards is, and by how much evidence it has.
5. **A person approves, edits or drops** each remaining candidate, reading its evidence. Approved rules go into the protected rule file.

The model gives different candidates on different runs. That is fine, because a person decides and the approved rules sit in the rule file, which does not change. A later scan only adds candidates that are new, matched by target and bad expression.

The call chain is what makes this work. It gives the model facts instead of guesses, and it knows the order of the calls, so it can propose rules about sequence ("started before it is approved" never happens).

**Why there is a filter.** Coding agents use tree-sitter successfully because what they produce is checked: the code compiles and the tests pass [17]. A proposed rule has no compiler, so Discover supplies one: real runs and seeded bugs. Work on model-generated invariants and specifications does the same, putting a checker behind the model. Daikon keeps only the invariants that hold on observed runs [10]. Later work puts a verifier or a ranker behind a model that proposes loop invariants and program specifications [11][12][13][14]. Appendix A (test 11) runs the filter on three candidates: one is kept, one is dropped because a real run breaks it, and one is dropped because it catches no bug. The filter removes wrong and empty rules. It cannot find a rule nobody proposed, or tell an important rule from a trivial one, so a person still decides.

**Granularity.** The target of each candidate sits at the level you choose. The default is endpoint. The lowest is a function.

| Level | Example candidate |
|---|---|
| System | A job never starts while its confirmation is missing |
| API or service | Nothing reaches the job store except through its API |
| Endpoint | No effect happens before auth |
| Module or class | No other module writes the table |
| Function | split_work never loses or adds jobs |

**Limits.** The chain shows static calls only, so dynamic dispatch and reflection are missing from it. It shows syntax, not intent, so a model can propose a rule that sounds right and is wrong: the person who approves is the check on that. It produces noise, so a dismissed candidate is recorded and does not return on every scan. Re-scan on every change: a new call or effect becomes a new candidate.

## 7. Concurrency and state

A guard in one process checks, then acts. Two callers can both check against the same old state, both pass, and both act. This check-then-act race breaks any rule about shared data, such as "leases on a pool never exceed its size".

A guard cannot stop a bad state while the check and the commit are separate steps (section 1). A rule over shared state needs something that makes them one atomic step:

| Mechanism | Covers | Notes |
|---|---|---|
| A key or check constraint (`UNIQUE`, `CHECK`, `FOREIGN KEY`) | Rules about one row or one key | Atomic by the database |
| A trigger that reads other rows | Rules across rows, such as the pool size | Safe only if the database serializes the writes that matter. SQLite allows one writer at a time [22]. In PostgreSQL, lock the pool row with `SELECT ... FOR UPDATE` [20], or use `SERIALIZABLE` isolation [18][19]. |
| Serializable transactions | Any rule, if every writer uses them | The application must retry transactions that fail with a serialization error [19] |
| A conditional write or version check | Stores with no triggers | The write fails if the data changed since it was read |
| One owner of the data | A rule only that service's writes can break | The service serializes its own writes |
| An idempotency key with `UNIQUE` | Retries | A retry never creates a second row |

The library keeps the in-process guard as the early, friendly refusal. A rule over shared data also carries a `Mechanism.STORAGE` enforcer: a constraint written by hand [21]. A rule written twice, once as a bad expression and once as SQL, can drift apart, so two tests tie them together (Appendix A, tests 9 and 10):

- **Forced interleaving.** Two callers read the same stale count, both pass their guard, and both insert. Without the trigger the pool is leased 120 of 100. With it, the second insert is refused.
- **Differential test.** On random sequences, storage never accepts a row for which the rule's bad expression is true, and never refuses one for which it is false.

The differential test covers only the sequences it generates. A different database or isolation level needs both tests again, and a distributed store has its own mechanism from the table.

## 8. What this does not give you

| Limit | Why | What to do |
|---|---|---|
| A pattern and the call chain see syntax only. | `getattr`, an alias, reflection, dynamic dispatch and raw SQL can reach a target unseen. | Add a storage enforcer for the same prohibition where the data lives, and say so in `unguarded`. |
| A guard in one process cannot see two concurrent callers. | "Leases on a pool never exceed its size" is not visible to two calls running at once. | Enforce the prohibition in storage or in a single owner of the data (section 7). The constraint is written by hand and tested against the rule's bad expression. |
| The weaver and the discoverer are part of what you trust. | The code that runs is not the code that was reviewed, and a model proposes the candidate rules. | Scan the woven output, keep the plain artifact for comparison, and have a person approve every rule. |
| Valid is not correct. | A rule says the result is not in a prohibited state, not that it is right. A restart for the wrong cause, on the right job, is valid. | Leave correctness to the approving person, example tests and evals. |
| The guard checks form and known facts, not meaning. | It cannot tell whether a reply is polite or accurate. A second model as a judge is non-deterministic. | Ask the model for a field the code can compare. Use a judge only as the weakest check. |
| The detect setting lets the call through. | It reports a break and does not stop it. | Use it to try a rule, then switch the guard to `enforce`. |
| A guard costs time. | Each call is parsed and checked. | Compare the plain artifact with the variant to see the cost. |
| A test name is a claim. | A named test might not fail when the guard is gone. | Break the guard on purpose and confirm the test fails (mutation testing). |
| A candidate is not a rule. | A model can propose a rule that sounds right and is wrong, and at volume an approver can rubber-stamp. | Let the evidence filter drop wrong and empty rules first. Then review the rules, not the volume, and show the approver the evidence. |
| Rules go stale. | A renamed target no longer points at anything. | The build fails on a rule whose target does not exist. Record an owner on each rule and retire the ones nobody needs. |
| The rules need protecting. | An agent that can edit a rule can drop its guard. | Change the rule list, its checks and its tests only with human review. |

## 9. Starting with one function

1. Pick one function whose failure costs the most.
2. Write its rule as one sentence that says what never happens, then as a bad expression.
3. Put the rule in the rule file. It is reviewed like code, and the people who own the function own the rule.
4. Weave it and run it with the detect setting. Watch what it reports.
5. Add a property test that runs the same bad expression on generated inputs.
6. If the rule is about shared stored data, add the storage constraint and test it against the bad expression (section 7).
7. Switch the guard to `enforce`.
8. Run Discover to propose the next rules, and approve the ones that are right.

A rule that proves wrong or too strict is changed or retired in the rule file by the same review that added it. Appendix A shows every step on a small service.

## 10. Related work and status

None of the parts is new. The proposal is to put them behind one rule record.

| Part | Prior work |
|---|---|
| Rules as prohibited states on calls and data | Hoare logic [1]; design by contract [2]; class invariants maintained by every exported routine [24]; safety and liveness [3] |
| A check every access must pass | Reference monitors and complete mediation [4] |
| Tests on generated inputs | QuickCheck [5] |
| Testing the tests by breaking the code | Mutation testing [6] |
| Policy as data, checked at runtime | Open Policy Agent [7]; CEL [8]; Cedar, where a satisfied forbid policy overrides any permit [23] |
| Runtime rules for an agent: a trigger, a predicate and an enforcement action | AgentSpec [25] |
| Gates on what an agent may do with untrusted content | CaMeL [9] |
| Finding invariants from real runs | Daikon [10] |
| A model proposing invariants and specifications, with a checker behind it | [11] to [14] |
| Proofs as the long-term route | [15][16] |
| Tree-sitter in coding agents | [17] |
| Atomic checks on shared state | [18] to [22] |

What the guide adds is the combination: one rule record applied at every stage, a variant that ships with the guards woven in, and discovery filtered by evidence before a person sees it.

| Piece | Status |
|---|---|
| Rule record, guard, weave, scan, plain and variant artifacts, storage constraint, evidence filter | Built as a teaching example (Appendix A), in Python, with stand-ins for tree-sitter and CEL |
| Weaving for real languages, a CEL engine, a guard library per language | Proposed, not built |
| Discover with a model | Proposed. Appendix A shows only the filter. |
| Measured results on a real service | None |

## Appendix A. A small working example

This is a runnable example of the whole flow on a three-function job-pool service: write the rules, weave the guards, scan, build the two artifacts, test, enforce a shared-state prohibition in storage, filter candidate rules by evidence, and run. It is Python, to keep it short. It uses two stand-ins:

- Python's `ast` module (syntax trees) for the tree-sitter pass.
- A few lines that translate `&&`, `||` and `!` and call `eval`, for a CEL engine. The bind expressions are also evaluated. The rule file is trusted. Never `eval` text that is not.

A production version uses tree-sitter and CEL. Everything else is as described in the guide. The weaver handles top-level plain `def` functions with an explicit return value. Methods, `async def` and functions with no return fail the build, because the scan finds no guard. Generators are rejected. The storage example uses SQLite from Python's standard library.

### A.1 The types (`invtypes.py`)

```python
from dataclasses import dataclass
from enum import Enum, IntEnum

class Reads(Enum):  # what the bad expression looks at
    VALUE = "one value"
    CALL = "a call's input and output"
    STATE = "stored data across calls"
    HISTORY = "order in time"
    LOOP = "a loop"
    CODE = "the source code"

class Mechanism(Enum):  # how the bad state is stopped
    PATTERN = "build time, over the source"
    GUARD = "run time, woven into the target"
    STORAGE = "deploy time: a constraint in the database"

class Moment(IntEnum):  # when it acts; smaller is stronger
    BY_CONSTRUCTION = 1
    BEFORE_EFFECT = 2
    BEFORE_SHIP = 3
    AFTER_EFFECT = 4

class OnViolation(Enum):
    REJECT = "refuse the move"
    RETRY = "retry with the problem fed back"
    ESCALATE = "hand to a person"
    HALT = "stop the run"
    BLOCK_MERGE = "fail the build"
    DETECT = "report only"

SEES = {  # what each mechanism can look at
    Mechanism.PATTERN: {Reads.CODE},
    Mechanism.GUARD: {Reads.VALUE, Reads.CALL, Reads.STATE, Reads.HISTORY, Reads.LOOP},
    Mechanism.STORAGE: {Reads.VALUE, Reads.STATE, Reads.HISTORY},
}

@dataclass(frozen=True)
class Enforcer:
    mechanism: Mechanism
    moment: Moment
    covers: str
    on_violation: OnViolation | None = None

@dataclass(frozen=True)
class Rule:
    id: str
    statement: str
    bad: str                          # true when the prohibited state exists. CEL in the guide; Python-like here (&&, || and ! are translated)
    reads: Reads
    target: str                       # a function name, or "endpoint:..." for a coarse rule
    bind: dict                        # name used in bad -> expression over arg / result
    when: str                         # "before" or "after"
    why: str
    parse: str | None = None
    enforcers: tuple = ()
    unguarded: tuple = ()
    falsified_by: tuple = ()
    on_violation: OnViolation | None = None
    retries: int = 0
    owner: str = ""

    def __post_init__(self):
        assert self.when in ("before", "after")
        for e in self.enforcers:
            assert self.reads in SEES[e.mechanism], f"{e.mechanism.name} cannot see {self.reads.name}"

    @property
    def is_invariant(self):
        return bool(self.enforcers) and not self.unguarded and bool(self.falsified_by)

    @property
    def is_backlog(self):
        return not self.is_invariant
```

### A.2 The guard, the weaver, the scan and the build (`invguard.py`)

guard runs the two steps (parse, then evaluate bad). weave inserts the guard calls. scan is the pattern check. build makes the plain artifact or the variant. run_with_retries is the harness loop. The guard reads the rule's on_violation, not the enforcer's.

```python
import ast
import json
import re
from dataclasses import replace
from types import SimpleNamespace

from invtypes import OnViolation

class Violation(Exception):
    """A rule was broken."""

class Retry(Violation):
    """A rule was broken and the rule says: try again with this problem."""

class Escalate(Exception):
    """Retries are spent; hand to a person."""

def any_outside(parts, caps):  # a helper the guard library supplies to every bad expression
    return any(p < 0 or p > x for p, x in zip(parts, caps))

SCHEMAS = {"CauseReply": {"cause": str}}  # closed: a reply with another field, or missing one, is never accepted

def parse(text, schema):
    """Step 1: text -> typed value, or a violation that names the problem."""
    try:
        data = json.loads(text)
    except ValueError:
        raise Violation("reply is not JSON")
    if not isinstance(data, dict) or set(data) != set(schema):
        raise Violation(f"reply fields differ from {sorted(schema)}")
    for name, kind in schema.items():
        if not isinstance(data[name], kind):
            raise Violation(f"{name} is not {kind.__name__}")
    return SimpleNamespace(**data)

def evaluate(bad, env):
    """Stand-in for a CEL engine. The rule file is trusted; never eval untrusted text."""
    bad = bad.replace("&&", " and ").replace("||", " or ")
    bad = re.sub(r"!(\w)", r"not \1", bad)
    return eval(bad, {"__builtins__": {}}, env)

def make_guard(rules, setting="enforce", report=None):
    by_id = {r.id: r for r in rules}

    def guard(rule_id, scope, result=None):
        rule = guard.by_id[rule_id]

        def fail(problem):                     # apply the rule's outcome
            if setting == "detect":
                report.append(problem)
            elif rule.on_violation is OnViolation.RETRY:
                raise Retry(problem)
            else:
                raise Violation(problem)

        result_value = result
        if rule.parse:
            try:
                result_value = parse(result, SCHEMAS[rule.parse])   # Step 1 (model calls only)
            except Violation as v:
                fail(f"{rule.id}: {v}")
                return result

        ns = {"arg": SimpleNamespace(**scope), "result": result_value}
        env = {name: eval(expr, {"__builtins__": {}}, ns) for name, expr in rule.bind.items()}
        env.update(sum=sum, any_outside=any_outside, len=len)
        try:
            bad = evaluate(rule.bad, env)                          # Step 2
        except Exception:
            bad = True                                             # a rule that cannot be evaluated is treated as broken
        if bad:
            fail(f"{rule.id}: {rule.statement}")
        return result

    guard.by_id = by_id
    return guard

def expand(rule, chain, tree):
    """A coarse rule (target 'endpoint:X') becomes one rule per reachable function that has the params it binds."""
    if not rule.target.startswith("endpoint:"):
        return [rule]
    reachable, todo = [], list(chain[rule.target.split(":", 1)[1]])
    while todo:
        name = todo.pop()
        if name not in reachable:
            reachable.append(name)
            todo.extend(chain.get(name, []))
    needed = set(re.findall(r"arg\.(\w+)", " ".join(rule.bind.values())))
    out = []
    for fn in [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in reachable]:
        if needed <= {a.arg for a in fn.args.args}:
            out.append(replace(rule, id=f"{rule.id}@{fn.name}", target=fn.name))
    return out

def _guard_call(rule_id, value=None):
    call = ast.parse(f"__guard__({rule_id!r}, dict(locals()))", mode="eval").body
    call.args.append(value if value is not None else ast.Constant(None))
    return call

def weave(source, rules, chain=None):
    """Insert a guard call at each target. Stand-in for a tree-sitter pass.

    Handles top-level plain `def` functions with an explicit `return value`.
    For an `after` rule, `arg` is the function's locals at the return, which include its arguments.
    """
    tree = ast.parse(source)
    concrete = [c for r in rules for c in expand(r, chain or {}, tree)]
    for fn in [n for n in tree.body if isinstance(n, ast.FunctionDef)]:
        mine = [r for r in concrete if r.target == fn.name]
        if mine and any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in ast.walk(fn)):
            raise ValueError(f"{fn.name}: generators are not supported")
        start = 1 if ast.get_docstring(fn) is not None else 0   # keep the docstring first
        for r in [r for r in mine if r.when == "before"]:
            fn.body.insert(start, ast.Expr(_guard_call(r.id)))
        after = [r for r in mine if r.when == "after"]

        class Returns(ast.NodeTransformer):                      # do not touch nested functions
            def visit_FunctionDef(self, node):
                return node

            def visit_Return(self, node):
                for r in after:
                    node.value = _guard_call(r.id, node.value)
                return node

        if after:
            for i, stmt in enumerate(fn.body):
                fn.body[i] = Returns().visit(stmt)
    return ast.fix_missing_locations(tree), concrete

def scan(tree, concrete):
    """The pattern scan: no target lacks the guard call for its rule."""
    problems = []
    for r in concrete:
        fns = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == r.target]
        if not fns:
            problems.append(f"{r.id}: target {r.target} does not exist")
            continue
        found = [c for c in ast.walk(fns[0]) if isinstance(c, ast.Call)
                 and getattr(c.func, "id", "") == "__guard__" and c.args[0].value == r.id]
        if not found:
            problems.append(f"{r.id}: {r.target} has no guard")
    return problems

def build(source, rules, chain=None, guard=None):
    """Two artifacts from one source: the plain one and the invariant variant."""
    ns = {}
    if guard is None:
        exec(compile(source, "<plain>", "exec"), ns)
        return ns
    tree, concrete = weave(source, rules, chain)
    problems = scan(tree, concrete)
    if problems:
        raise ValueError(problems)
    guard.by_id.update({r.id: r for r in concrete})
    ns["__guard__"] = guard
    exec(compile(tree, "<variant>", "exec"), ns)
    return ns

def run_with_retries(call, retries):
    """The harness loop: retry with the problem fed back, then escalate."""
    problem = None
    for _ in range(retries + 1):
        try:
            return call(problem)
        except Retry as r:
            problem = str(r)
    raise Escalate(problem)
```

### A.3 Storage and discovery (`invstorage.py`, `invdiscover.py`)

invstorage.py holds the schema and the hand-written trigger for R-4. invdiscover.py runs candidate rules with the detect setting on a good build and on seeded-bug builds.

```python
import sqlite3

SCHEMA = """
CREATE TABLE pools (id TEXT PRIMARY KEY, size_slots INTEGER NOT NULL CHECK (size_slots > 0));
CREATE TABLE leases (lease_id TEXT PRIMARY KEY,          -- no two leases share a request
                     pool_id TEXT NOT NULL REFERENCES pools (id),
                     slots INTEGER NOT NULL CHECK (slots > 0));
"""

TRIGGER = """
CREATE TRIGGER leases_never_exceed_pool BEFORE INSERT ON leases
BEGIN
    SELECT RAISE(ABORT, 'leases exceed the pool size')
    WHERE (SELECT COALESCE(SUM(slots), 0) FROM leases WHERE pool_id = NEW.pool_id)
          + NEW.slots > (SELECT size_slots FROM pools WHERE id = NEW.pool_id);
END;
"""

def new_db(size_slots, with_trigger=True):
    db = sqlite3.connect(":memory:")
    db.executescript(SCHEMA + (TRIGGER if with_trigger else ""))
    db.execute("INSERT INTO pools VALUES ('p1', ?)", (size_slots,))
    db.commit()
    return db

def leased(db):
    return db.execute("SELECT COALESCE(SUM(slots), 0) FROM leases WHERE pool_id = 'p1'").fetchone()[0]

def insert(db, lease_id, slots):
    """True if storage accepted the row."""
    try:
        db.execute("INSERT INTO leases VALUES (?, 'p1', ?)", (lease_id, slots))
        db.commit()
        return True
    except sqlite3.IntegrityError:
        db.rollback()
        return False
```

```python
from invguard import build, make_guard

def fired(source, candidates, chain, calls):
    """Which candidate rules fire when `calls` run on this build, with the detect setting."""
    seen = []
    artifact = build(source, candidates, chain, make_guard(candidates, "detect", seen))
    for name, args in calls:
        artifact[name](*args)
    return {problem.split(":")[0] for problem in seen}

def evidence(candidates, good, mutants, chain, calls):
    """For each candidate: does a real run falsify it, and how many seeded bugs does it catch?"""
    falsified = fired(good, candidates, chain, calls)          # a real run breaks it: the rule is wrong
    caught = {c.id: 0 for c in candidates}
    for mutant in mutants:
        for rule_id in fired(mutant, candidates, chain, calls):  # a seeded bug breaks it: the rule says something
            caught[rule_id] += 1
    return [(c.id, c.id in falsified, caught[c.id]) for c in candidates]
```

### A.4 The system and its rules (`demo.py`, first half)

SOURCE is the system under test. BUGGY is the same code with the remainder loop removed. CHAIN is the call chain, from which the coarse rule R-2 is expanded to function targets.

```python
import ast
import random
import time
from invtypes import *
from dataclasses import replace
from invguard import *
from invdiscover import evidence
from invstorage import new_db, leased, insert

SOURCE = '''
def split_work(total_jobs, capacities):
    capacity_total = sum(capacities)
    parts = [total_jobs * x // capacity_total for x in capacities]
    for i in range(total_jobs - sum(parts)):
        parts[i] += 1
    return parts

def acquire_slots(store, slots):
    store.running += slots
    return store.running

def propose_cause(model, problem):
    return model(problem)
'''

BUGGY = SOURCE.replace(" for i in range(total_jobs - sum(parts)):\n        parts[i] += 1\n", "")

CHAIN = {"POST /jobs": ["acquire_slots"], "acquire_slots": ["split_work"], "split_work": []}

R1 = Rule(id="R-1", statement="split_work never returns a share below zero, a share above its worker's capacity, or shares whose sum differs from total_jobs",
          bad="sum(parts) != total || any_outside(parts, caps)",
          reads=Reads.CALL, target="split_work",
          bind={"total": "arg.total_jobs", "caps": "arg.capacities", "parts": "result"},
          when="after",
          why="A rounding bug corrupts everything above it.",
          enforcers=(Enforcer(Mechanism.GUARD, Moment.BEFORE_EFFECT, "any call to split_work", OnViolation.REJECT),),
          falsified_by=("property test on the variant",))

R2 = Rule(id="R-2", statement="a slot request is never zero or negative",
          bad="slots <= 0",
          reads=Reads.VALUE, target="endpoint:POST /jobs",
          bind={"slots": "arg.slots"}, when="before",
          why="A zero or negative request corrupts the count.",
          enforcers=(Enforcer(Mechanism.GUARD, Moment.BEFORE_EFFECT, "every function the endpoint reaches that takes slots", OnViolation.REJECT),),
          falsified_by=("test 5: the variant never lets a zero-slot request through",))

R4 = Rule(id="R-4", statement="leases never exceed the pool size",
          bad="running + slots > limit",
          reads=Reads.STATE, target="acquire_slots",
          bind={"running": "arg.store.running", "slots": "arg.slots", "limit": "arg.store.limit"},
          when="before",
          why="Never lease more slots than the pool holds.",
          enforcers=(Enforcer(Mechanism.GUARD, Moment.BEFORE_EFFECT, "one caller, early and friendly", OnViolation.REJECT),
                     Enforcer(Mechanism.STORAGE, Moment.BEFORE_EFFECT, "every insert, including concurrent callers", OnViolation.REJECT),),
          falsified_by=("oracle test on the variant", "trigger never differs from the bad expression", "forced interleaving"))

R7 = Rule(id="R-7", statement="the reply never names a cause outside the closed set",
          parse="CauseReply",
          bad='!(reply.cause in ["TIMEOUT", "OUT_OF_MEMORY", "BAD_INPUT"])',
          reads=Reads.CALL, target="propose_cause",
          bind={"reply": "result"}, when="after",
          why="A model never invents a cause.",
          enforcers=(Enforcer(Mechanism.GUARD, Moment.BEFORE_EFFECT, "every reply, before anything uses it", OnViolation.RETRY),),
          retries=3,
          falsified_by=("test 7: bad replies are retried, then escalated",))

RULES = (R1, R2, R4, R7)

class Store:
    def __init__(self, limit):
        self.running, self.limit = 0, limit

guard = make_guard(RULES)
plain, variant = build(SOURCE, RULES), build(SOURCE, RULES, CHAIN, guard)
rng = random.Random(1)
```

### A.5 The tests (`demo.py`, second half)

```python
# 1. a bug never gets past the guard
bad_plain, bad_variant = build(BUGGY, RULES), build(BUGGY, RULES, CHAIN, guard)
assert sum(bad_plain["split_work"](1, [1, 1, 1])) != 1
try:
    bad_variant["split_work"](1, [1, 1, 1]); raise SystemExit("guard missed the bug")
except Violation as v: print("1 caught:", v)

# 2. property test on the variant: a correct function never trips the guard
for _ in range(2000):
    caps = [rng.randint(1, 50) for _ in range(rng.randint(1, 6))]
    variant["split_work"](rng.randint(1, sum(caps)), caps)
print("2 property test passed on the variant")

# 3. differential test: plain and variant never differ on an input
for _ in range(2000):
    caps = [rng.randint(1, 50) for _ in range(rng.randint(1, 6))]
    t = rng.randint(1, sum(caps))
    assert plain["split_work"](t, caps) == variant["split_work"](t, caps)
print("3 plain and variant never differ")

# 4. a state rule, with an in-memory store and an oracle
plain_exceeded = 0
for _ in range(500):
    limit = rng.randint(1, 100)
    p_store, v_store = Store(limit), Store(limit)
    for slots in [rng.randint(1, 60) for _ in range(5)]:
        reached = v_store.running + slots > limit          # the oracle: does this request reach the bad state?
        plain["acquire_slots"](p_store, slots)             # the plain artifact has no check
        try:
            variant["acquire_slots"](v_store, slots); refused = False
        except Violation: refused = True
        assert refused == reached
        if not refused:
            v_store.running += slots                       # simulate the effect only if not refused
        if p_store.running > limit:
            plain_exceeded += 1
print("4 state rule never differs from the oracle; the plain artifact exceeded the limit in", plain_exceeded, "of 500 runs")

# 5. the coarse rule expanded to function targets
tree, concrete = weave(SOURCE, RULES, CHAIN)
print("5 expanded:", sorted(r.id for r in concrete))
try:
    variant["acquire_slots"](Store(100), 0); raise SystemExit("R-2 missed")
except Violation as v: print("  R-2 refused 0 slots:", v)

# 6. the scan fails the build when a guard is missing
unguarded = ast.fix_missing_locations(ast.parse(SOURCE))
missing = scan(unguarded, concrete)
assert missing and not scan(tree, concrete)
print("6 scan of unwoven code:", missing[:2])              # unwoven code fails; woven code passes

# 7. a model call: parse, check, retry with the problem fed back, escalate
def model_of(replies, log):
    it = iter(replies)
    def model(problem):
        log.append(problem)                                # what the harness fed back to the model
        return next(it)
    return model

call = lambda m: (lambda problem: variant["propose_cause"](m, problem))
log = []
good = run_with_retries(call(model_of(["Sure, I'll restart it!", '{"cause": "BROKE"}', '{"cause": "TIMEOUT", "pool_size": 40}', '{"cause": "TIMEOUT"}'], log)), R7.retries)
assert good == '{"cause": "TIMEOUT"}'                      # the extra-field reply was rejected, not accepted
assert log[0] is None and all(p.startswith("R-7") for p in log[1:])
print("7 accepted after retries:", good)
print("  problems fed back:")
for problem in log[1:]:
    print("   ", problem)

try:
    run_with_retries(call(model_of(["nope"] * 5, [])), 1); raise SystemExit("no escalation")
except Escalate as e: print("  escalated:", e)

# 8. detect setting reports and lets the call through
seen = []
det = build(SOURCE, RULES, CHAIN, make_guard(RULES, "detect", seen))
det["acquire_slots"](Store(10), 50)
print("8 detect setting reported:", seen)

# 9. two callers pass the in-process guard on the same stale read; only storage holds the rule
for with_trigger in (False, True):
    db = new_db(100, with_trigger)
    stale = leased(db)                                     # both callers read 0 before either writes
    reached = [evaluate(R4.bad, {"running": stale, "slots": 60, "limit": 100}) for _ in range(2)]  # R-4's bad state looks absent to both callers
    accepted = [insert(db, f"a{i}", 60) for i, r in enumerate(reached) if not r]
    print("9", "with" if with_trigger else "without", "the trigger: accepted", accepted, "| leased", leased(db))
    assert (leased(db) <= 100) == with_trigger

# 10. the hand-written trigger agrees with the rule's check on random sequences
for _ in range(300):
    limit = rng.randint(1, 100)
    db = new_db(limit)
    for i in range(6):
        slots = rng.randint(1, 60)
        reached = evaluate(R4.bad, {"running": leased(db), "slots": slots, "limit": limit})
        refused = not insert(db, f"a{i}", slots)
        assert refused == reached                          # storage refuses a row exactly when it reaches the bad state
print("10 trigger never differs from the bad expression on 300 random sequences")

# 11. Discover: filter a model's candidate rules by evidence before a person sees them
CANDIDATES = (
    replace(R1, id="C-1", statement="the parts never differ from the total", bad="sum(parts) != total"),
    replace(R1, id="C-2", statement="split_work never returns other than 3 parts", bad="len(parts) != 3"),
    replace(R1, id="C-3", statement="the total is never negative", bad="total < 0"),
)
calls = []
for _ in range(200):
    caps = [rng.randint(1, 50) for _ in range(rng.randint(1, 6))]
    calls.append(("split_work", (rng.randint(1, sum(caps)), caps)))
verdicts = {}
for rule_id, falsified, caught in evidence(CANDIDATES, SOURCE, [BUGGY], CHAIN, calls):
    verdicts[rule_id] = "drop: a real run breaks it" if falsified else ("keep" if caught else "drop: catches no seeded bug")
print("11", rule_id, verdicts[rule_id])
assert verdicts == {"C-1": "keep", "C-2": "drop: a real run breaks it", "C-3": "drop: catches no seeded bug"}

# 12. the cost of the guards
def timed(fn):
    s = time.perf_counter()
    for _ in range(20000):
        fn()
    return time.perf_counter() - s

print("12 plain %.3fs, variant %.3fs for 20000 calls" % (
    timed(lambda: plain["split_work"](100, [30, 30, 40])),
    timed(lambda: variant["split_work"](100, [30, 30, 40]))))
print("backlog:", [r.id for r in RULES if r.is_backlog])
```

**Output:**

```
1 caught: R-1: split_work never returns a share below zero, a share above its worker's capacity, or shares whose sum differs from total_jobs
2 property test passed on the variant
3 plain and variant never differ
4 state rule never differs from the oracle; the plain artifact exceeded the limit in 487 of 500 runs
5 expanded: ['R-1', 'R-2@acquire_slots', 'R-4', 'R-7']
  R-2 refused 0 slots: R-2@acquire_slots: a slot request is never zero or negative
6 scan of unwoven code: ['R-1: split_work has no guard', 'R-2@acquire_slots: acquire_slots has no guard']
7 accepted after retries: {"cause": "TIMEOUT"}
  problems fed back:
    R-7: reply is not JSON
    R-7: the reply never names a cause outside the closed set
    R-7: reply fields differ from ['cause']
  escalated: R-7: reply is not JSON
8 detect setting reported: ['R-4: leases never exceed the pool size']
9 without the trigger: accepted [True, True] | leased 120
9 with the trigger: accepted [True, False] | leased 60
10 trigger never differs from the bad expression on 300 random sequences
11 C-1 keep
11 C-2 drop: a real run breaks it
11 C-3 drop: catches no seeded bug
12 plain 0.004s, variant 0.270s for 20000 calls
backlog: []
```

### A.6 What each test shows

| Test | What it shows |
|---|---|
| 1 | A bug the plain build lets through is refused by the variant. |
| 2 | Property tests run against the **variant**, so the guards are exercised. |
| 3 | Plain and variant never give different outputs when no rule fires. This is the comparison step in section 3. |
| 4 | A state rule is tested with an in-memory store and an oracle that decides whether the bad state is reached. Too lax and too strict both fail. The plain artifact has no check and exceeds the limit. |
| 5 | The coarse rule `endpoint:POST /jobs` expands to the function targets it reaches that take the bound argument. |
| 6 | The scan reports a target with no guard, and passes the woven code. `build` refuses to build when the scan finds a problem. |
| 7 | A model call: the text is parsed into a closed schema, the cause is checked against a closed set, and a bad reply is retried with the problem fed back. The reply with an extra field is rejected, and retries run out in an escalation. The model picks a cause from a closed set. It never writes free text the guard would have to pattern-match. |
| 8 | With the detect setting a break is reported and the call goes through. |
| 9 | Forced interleaving: two callers pass the in-process guard on the same stale read. Without the trigger the pool is over-leased. With it, storage refuses the second insert. |
| 10 | The hand-written trigger never accepts a row for which the rule's bad expression is true, and never refuses one for which it is false, on random sequences. |
| 11 | The evidence filter on three candidates: a real run breaks one, one catches no seeded bug, and one is kept. |
| 12 | The cost of the guards. The gap in the output is the interpretive overhead of the stand-in (`eval`, building the scope for each call). A real CEL engine would differ. The real cost is not measured here. |

The backlog is empty because no rule lacks an enforcer or a test that fails without it. Remove the test named in R-2 and R-2 returns to the backlog.

The example leaves out the pattern rules about code (such as "only the door calls the writer"), a race between separate processes (the interleaving is forced in one), and the model that proposes candidates (the candidates are written by hand).

## References

1. C. A. R. Hoare, "An axiomatic basis for computer programming", Communications of the ACM, 1969. https://doi.org/10.1145/363235.363259
2. B. Meyer, "Applying 'design by contract'", IEEE Computer, 1992. https://doi.org/10.1109/2.161279
3. B. Alpern and F. B. Schneider, "Defining liveness", Information Processing Letters, 1985. https://doi.org/10.1016/0020-0190(85)90056-0
4. J. H. Saltzer and M. D. Schroeder, "The protection of information in computer systems", Proceedings of the IEEE, 1975. https://doi.org/10.1109/proc.1975.9939
5. K. Claessen and J. Hughes, "QuickCheck: a lightweight tool for random testing of Haskell programs", ICFP 2000. https://doi.org/10.1145/351240.351266
6. R. A. DeMillo, R. J. Lipton and F. G. Sayward, "Hints on test data selection: help for the practicing programmer", IEEE Computer, 1978. https://doi.org/10.1109/c-m.1978.218136
7. Open Policy Agent, "Policy Language" (Rego). https://www.openpolicyagent.org/docs/policy-language
8. Common Expression Language. https://cel.dev/ and the language definition, https://github.com/google/cel-spec/blob/master/doc/langdef.md
9. E. Debenedetti, I. Shumailov, T. Fan et al., "Defeating Prompt Injections by Design", arXiv:2503.18813, 2025. https://arxiv.org/abs/2503.18813
10. M. D. Ernst et al., "Dynamically discovering likely program invariants to support program evolution", ICSE 1999. https://doi.org/10.1145/302405.302467
11. S. Chakraborty, S. K. Lahiri, S. Fakhoury et al., "Ranking LLM-Generated Loop Invariants for Program Verification", arXiv:2310.09342, 2023. https://arxiv.org/abs/2310.09342
12. H. Wu, C. Barrett and N. Narodytska, "Lemur: Integrating Large Language Models in Automated Program Verification", arXiv:2310.04870, 2023. https://arxiv.org/abs/2310.04870
13. L. Ma, S. Liu, Y. Li et al., "SpecGen: Automated Generation of Formal Program Specifications via Large Language Models", arXiv:2401.08807, 2024. https://arxiv.org/abs/2401.08807
14. C. Wen, J. Cao, J. Su et al., "Enchanting Program Specification Synthesis by Large Language Models using Static Analysis and Program Verification", arXiv:2404.00762, 2024. https://arxiv.org/abs/2404.00762
15. M. Kleppmann, "Prediction: AI will make formal verification go mainstream", 8 December 2025. https://martin.kleppmann.com/2025/12/08/ai-formal-verification.html
16. S. Bursuc, T. Ehrenborg, S. Lin et al., "A benchmark for vericoding: formally verified program synthesis", arXiv:2509.22908, 2025. https://arxiv.org/abs/2509.22908
17. "Building a better repository map with tree sitter", 22 October 2023. https://aider.chat/2023/10/22/repomap.html
18. D. R. K. Ports and K. Grittner, "Serializable Snapshot Isolation in PostgreSQL", arXiv:1208.4179, 2012. https://arxiv.org/abs/1208.4179
19. PostgreSQL documentation, "Transaction Isolation". https://www.postgresql.org/docs/current/transaction-iso.html
20. PostgreSQL documentation, "Explicit Locking". https://www.postgresql.org/docs/current/explicit-locking.html
21. SQLite documentation, "CREATE TRIGGER". https://sqlite.org/lang_createtrigger.html
22. SQLite documentation, "Isolation In SQLite". https://sqlite.org/isolation.html
23. Cedar, "Authorization" (default deny; forbid overrides permit). https://docs.cedarpolicy.com/auth/authorization.html
24. Eiffel Software, "Design by Contract (tm), Assertions and Exceptions" (class invariants). https://www.eiffel.org/doc/eiffel/ET-Design_by_Contract%28tm%29%2C_Assertions_and_Exceptions
25. H. Wang, C. M. Poskitt and J. Sun, "AgentSpec: Customizable Runtime Enforcement for Safe and Reliable LLM Agents", arXiv:2503.18666, ICSE 2026. https://arxiv.org/abs/2503.18666

