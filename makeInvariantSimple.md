# Invariant-First, Made Simple

## 1. The Intro: Why We Need "Invariant-First"

### The Problem: AI Writes Faster Than Humans Can Read
AI agents produce more output than any human can realistically check:
- **In Code:** A coding agent edits 30 files in two minutes. The human reviewer gets overwhelmed, skims the diff, sees that tests pass, and clicks **Approve**.
- **In Data / Knowledge:** An AI pipeline extracts 10,000 facts from source documents. A human checks a sample of 20, sees they look fine, and lets the other 9,980 into the database—including subtle hallucinations.

**Human review does not scale with AI output.**

### The Shift: From API-First to Invariant-First
Think of a software system as having two layers of contracts:

1. **API-First (The Shape):** Tools like OpenAPI, Protobuf, or JSON Schema sit at the center of a system to define *what requests and responses look like* (e.g., "`pool_id` is a string, `new_size` is an integer").
2. **Invariant-First (The Laws):** Invariant-first adds the layer directly above API-first. It defines *the states that must never exist* (e.g., "A server pool must never be resized without a matching `APPROVED` ticket").

**The new division of labor:**
- **People** write the **shapes** (API signatures) and the **laws** (invariants).
- **AI Agents** write the **implementations** (the application code or extracted data).
- **Deterministic Code**—not another AI prompt, and not a tired human skimming a pull request—checks every single output against those laws and decides whether it is allowed through.

| Question | Without Invariants | With Invariants |
| :--- | :--- | :--- |
| **What does a human reviewer read?** | Every line of AI-generated code, or a tiny random sample. | Only the **rules**, rule changes, rule violations, unguarded paths, and a small sample of outputs for meaning. |
| **Code example** | *"Does this 40-file pull request look right?"* | *"No file in `ui/` may import from `db/`"* — checked automatically on every diff. |
| **AI Knowledge example** | *"Is this extracted fact actually in the document?"* | *"No fact may quote text that doesn't appear word-for-word in its source"* — checked on 100% of facts. |
| **What scales?** | The reviewer's hours (which hits a wall). | The automated check (which scales infinitely). |

### Two Honest Limits Up Front
Invariant-first is powerful, but it is not magic:
1. **"Valid" is not "Correct":** Passing every rule only proves an output didn't break the laws you wrote. If an AI restarts the right server (allowed by the rules) for the completely wrong reason, it is **valid**, but still **incorrect**.
2. **The coverage gap moves; it does not disappear:** A machine can only block mistakes that someone wrote a rule for. If an AI invents a brand-new type of bug, it will pass until you add a rule for it. That is why human sampling remains essential.

---

## 2. The Definition: What Is an Invariant?

> **An invariant is a condition the system must never be in, written as a yes/no check (`bad`) over a single run, and enforced at a chokepoint that every path must pass through *before* the change takes effect.**

Broken down into four plain-English rules:

1. **"A condition the system must never be in" (A Prohibition):**
   Instead of writing vague goals (*"the system should work properly"*), you define what a bug or illegal state looks like (`bad`).
2. **"A yes/no check over a single run" (Decidable Predicate):**
   The rule is deterministic code that finishes immediately and returns `True` (*violation!*) or `False` (*safe*) by looking at the current action and state.
3. **"Every path passes through" (No Back Doors / Complete Mediation):**
   A lock on the front door is useless if the back window is wide open. Every code path or script that can change the data must pass through the check.
4. **"Before the condition takes effect" (Prevent, Don't Just Report):**
   If your check runs *after* the database was already corrupted or the money already left the bank, that is just a smoke alarm (**detection**). A true invariant acts like a circuit breaker—it stops the action **before** the side effect happens (**enforcement**).

### The 3 Levels of a Rule ("Standing")
Because not every rule is wired up perfectly on day one, the system grades every rule into one of three levels:

| Standing | Plain-English Meaning | Real-World Analogy |
| :--- | :--- | :--- |
| **1. Candidate** | A proposed rule (drafted by a human or suggested by an AI) that **no human has approved yet**. It runs silently in the background and logs what *would* have failed as evidence for review. | A trainee guard taking notes on a clipboard without stopping anyone. |
| **2. Monitored** | An approved rule that **only detects problems after the fact** (e.g., it sends an alert or runs after a side effect already happened). | A security camera: it catches the thief on video, but doesn't stop the theft. |
| **3. Held (True Invariant)** | The gold standard. It **blocks** bad states *before* they take effect, and **no back doors exist** around it. *(If it blocks known callers in code, but a back door like raw SQL still exists, it gets a middle grade: `enforce(known-callers)`).* | A bank vault door that physically cannot be opened without passing the lock. |

---

## 3. Concrete Example: Resizing a Cloud Server Pool

### The Scenario
Imagine your system has an endpoint called `ResizePool` that changes how many GPU servers are running in a cluster.
Because GPU servers are expensive, your company has one strict law:
> **Company Law:** *"Before any server pool is resized, a manager must approve a ticket in the database for that **exact pool** and that **exact new size**."*

Now suppose an AI agent calls `ResizePool` with this request:
```json
{
  "pool_id": "gpu-prod",
  "new_size": 500
}
```
Meanwhile, in your database, the manager only approved **50** servers (not **500**):
```json
// Approval ticket in database:
{
  "pool_id": "gpu-prod",
  "new_size": 50,
  "state": "APPROVED"
}
```

Let's see how **API-First** vs. **Invariant-First** handle this exact request.

---

### Layer 1: API-First (Checks the Shape Only)
Your API contract (OpenAPI / Protobuf) defines the **shape** of `ResizePoolRequest`:
```protobuf
message ResizePoolRequest {
  string pool_id = 1;   // Must be a string
  int32 new_size = 2;   // Must be an integer
}
```
* **What API-first catches:** If the agent sends `new_size: "five hundred"`, API-first blocks it because `"five hundred"` is not an integer.
* **Why API-first fails here:** In `{ "pool_id": "gpu-prod", "new_size": 500 }`, `"gpu-prod"` is a valid string and `500` is a valid integer. API-first only checks the shape of the request message—it has **no idea** that the approval ticket in the database was only for `50` servers! So API-first says **"Valid!"** and lets the 500-server resize through.

---

### Layer 2: Invariant-First (Checks the Law Before the Action Happens)
Instead of hoping the AI agent wrote the right check in Python, a human writes Rule `R-21` in a locked `rules.yaml` file.

This rule tells the system:
1. **Read the incoming request** (`req`).
2. **Fetch the approval ticket from the database** (`approval`).
3. **Block the call (`bad`)** if there is no ticket, if the ticket isn't `"APPROVED"`, or if the ticket's `pool_id` or `new_size` doesn't match the request.

```yaml
providers:
  # 1. How to look up the approval ticket in the database
  approval:
    from: "py:pool.providers.load_approval"
    args: { pool_id: req.pool_id }
    consistency: same_txn

rules:
  - id: R-21
    status: enforce
    statement: Never resize a pool without a matching APPROVED ticket.
    subject:
      rpc: acme.pool.v1.PoolService/ResizePool
      target: "py:pool.PoolService.resize_door"
    bind:
      req: request                 # The incoming API request ({ pool_id: "gpu-prod", new_size: 500 })
      approval: provider.approval  # The DB ticket ({ pool_id: "gpu-prod", new_size: 50, state: "APPROVED" })

    # 2. THE PROHIBITED STATE (bad):
    bad: >
      approval == null ||
      approval.state != "APPROVED" ||
      approval.pool_id != req.pool_id ||
      approval.new_size != req.new_size

    # 3. WHERE IT IS ENFORCED:
    enforcers:
      - point: decorator      # Guard #1: Blocks at the Python function door BEFORE servers are resized
        timing: pre_effect
        mode: enforce
        on_violation: reject
      - point: storage        # Guard #2: Blocks in Postgres DB trigger (so raw SQL scripts can't bypass Python)
        timing: pre_effect
        mode: enforce
        on_violation: reject
```

### What Happens When the Request Runs:

| Incoming Request (`req`) | Database Ticket (`approval`) | API-First (Shape) | Invariant `R-21` (Law) | Result |
| :--- | :--- | :--- | :--- | :--- |
| `{ pool_id: "gpu-prod", new_size: "five hundred" }` | *(any)* | **Fails** (wrong type) | *(not reached)* | **Blocked by API-First** |
| `{ pool_id: "gpu-prod", new_size: 500 }` | `null` (No ticket exists) | Passes | **`bad` is True** (`approval == null`) | **Blocked by Invariant** |
| `{ pool_id: "gpu-prod", new_size: 500 }` | `{ pool_id: "gpu-prod", new_size: 50, state: "APPROVED" }` | Passes | **`bad` is True** (`50 != 500`) | **Blocked by Invariant** |
| `{ pool_id: "gpu-prod", new_size: 50 }` | `{ pool_id: "gpu-prod", new_size: 50, state: "APPROVED" }` | Passes | **`bad` is False** (Everything matches!) | **Allowed Through!** |

---

### How `R-21` Moves Through the 3 Levels ("Standing"):
1. **As a `Candidate`:** When you first draft `R-21`, you set `status: candidate`. It blocks nothing yet—it just watches real traffic and logs whenever `bad` is `True` so a human can verify the rule works properly.
2. **As `Monitored`:** If `R-21` only ran *after* the 500 servers were already created (`timing: post_effect`) and sent a Slack alert, it would be graded **Monitored**—it detected the bug, but didn't stop the bill.
3. **As `Held`:** Because `R-21` checks `bad` **before** the resize happens (`timing: pre_effect`) AND blocks it at both the Python function and the Postgres database table (closing all back doors), the system grades `R-21` as **Held**.

---

## 4. Why Is This Different? ("Why Not Just Write an `if` Statement?")

When developers first see a rule like *"don't resize without approval"* or *"balance cannot be negative,"* their first reaction is:
> *"Why do I need Invariant-first? I can just write an `if` statement inside my Python function!"*

In a world where **AI agents write and refactor your code**, a hand-written `if` statement buried inside application code fails for four reasons:

### 1. AI Agents Can Break or Delete Your `if` Statements
If your business law is just an `if` statement on line 340 of `pool_service.py`, the next time an AI coding agent refactors that file, it might accidentally delete the `if` statement, weaken the condition, or create a brand-new helper function (`fast_resize()`) that skips the check entirely.
* **With Invariant-First:** The laws live in a locked `rules.yaml` contract **outside** the agent's code. Even if the AI writes buggy code, the rule enforcer intercepts the call and blocks the bad state.

### 2. An `if` Statement Cannot Tell You About Back Doors
If a developer or agent writes a second endpoint or an admin script that updates the database directly without calling your `if` statement, Python stays completely silent.
* **With Invariant-First:** The system scans all places where side effects happen ("effect sinks") and checks whether every path passes through the rule. If an unguarded path exists, the build fails and the rule's standing drops from `Held` to `enforce(known-callers)`.

### 3. One Rule Record Drives Multiple Layers (Code + Database + Tests)
An `if` statement in Python only protects Python. It does not protect your Postgres database from another microservice, and it does not write its own tests.
* **With Invariant-First:** A single rule (`R-21`) acts as the source of truth ("oracle") that powers:
  - The in-process Python guard (`point: decorator`)
  - The database constraint/trigger (`point: storage`)
  - Automated property tests and conformance tests that prove Python and SQL agree 100%.

### 4. It Changes What Humans Have to Review
- **With `if` statements:** A human reviewer must read every line of a 2,000-line AI pull request to make sure no `if` statements were subtly broken or bypassed.
- **With Invariant-First:** The human reviews the **small, readable `rules.yaml` file** and checks the automated coverage report:
  `R-21: HELD (0 violations, 0 unguarded paths)`.

---

## 5. What Kinds of Rules Can You Write? (And Where Each Guard Lives)

Not every rule can be checked in the same place. **What data your rule (`bad`) needs to read decides which guard (enforcer) can actually enforce it.**

For example, a JSON schema parser only sees one incoming message—it cannot see your Postgres database! Here are the 11 kinds of rules from the spec, translated into plain English:

| Kind | `bad` reads *(What data the rule looks at)* | Example | Strongest enforcer *(Where to block it)* | Plain-English Meaning |
| :--- | :--- | :--- | :--- | :--- |
| **1. Value / type** | One datum *(a single value)* | `index` out of range | Type, or a parser | **Single-field check:** e.g., `age < 0`. Block it right in Pydantic, OpenAPI, or Protobuf. |
| **2. Call contract** | Arguments and result *(inputs & output of one function)* | `cause ∉ set` | Precondition; result guard on a pure function | **Function input/output check:** Check inputs before a function runs, or check the return value of a pure calculation (`@inv.guard`). |
| **3. Representation** | One object's private fields | `size ≠ len(items)` | Remove the redundancy | **Internal class consistency:** Two private variables inside one object got out of sync. Best fix: delete the duplicate variable (`size`). |
| **4. Abstract** | The interface's abstract value | Leases exceed pool size | Owning module; storage | **Module-level rule:** A rule about a domain concept (like a "Pool"), enforced by the module or database that owns it. |
| **5. Shared state** | Rows across writers *(data edited by multiple servers at once)* | A cross-row cap *(e.g., sum of all rows > 100)* | DB constraint, serializable transaction, single owner | **Multi-user race condition:** Two servers can pass a Python check at the same millisecond. Must be enforced in the **Database**. |
| **6. Transition** | Old and new state (`old_self`) | `APPROVED` reverts to `PENDING` | Guard on the next state | **Before-vs-After check:** Compares what data looked like *before* the update (`old_self`) to what it looks like *after* (`self`). |
| **7. History / order** | A finite event prefix *(the log of steps so far)* | A write before its approval | History variable plus guard | **Step-order check:** Step B is never allowed unless Step A already happened earlier in this run. |
| **8. Bounded response** | Counters, clocks | No reply within 5 s | Counter guard; timeout plus fallback | **Time or retry limit:** Never allow more than $N$ retries or $X$ seconds to pass without returning a response or fallback. |
| **9. Provenance** | Output versus data code holds | Quote not in source | Parse plus comparison | **Anti-hallucination check:** Compares what an AI claims against the trusted source document or DB record you already hold. |
| **10. Code structure** | Program text *(your `.py` source files)* | `ui` imports `db` | Build scan; visibility rules | **Architecture rule:** Checked in CI by scanning your `import` statements (`inv scan`), not while the app runs. |
| **11. Information flow** | Several runs *(can untrusted input leak to another agent?)* | Untrusted text reaches the next agent | Structure, labels, differential tests; **no guard** | **Prompt-injection / data-leak rule:** A runtime `if` statement can't catch this. Fix by removing raw text fields from the next agent's input schema. |

### The 3 Quick Takeaways from This Table
1. **In-Memory Python Guards (Rows 1–3, 6–9):** Work great when all the data lives inside a single function call or object.
2. **Database Guards (Rows 4–5):** Required whenever multiple users or servers write to shared data at the same time.
3. **Build-Time & Schema Guards (Rows 10–11):** Rules about code imports or data leaks are enforced by CI scanners and strict types, not runtime decorators.

---

### What an Invariant Is NOT
People often try to turn everything into an invariant. These **five things do NOT belong in `rules.yaml`**:

1. **Open-ended promises (*"The payment will eventually succeed"*):** At any given second, if it hasn't succeeded yet, is it broken or still retrying? A runtime guard can't tell (unless you add a hard limit like *"within 5 seconds"*).
2. **AI Evals (*"Our chatbot is 78% accurate"*):** That is an average across 1,000 runs. An invariant is a `True/False` check on **one specific run right now**.
3. **Vague human meaning (*"The summary is helpful"*):** Deterministic code cannot compute *"helpful."*
4. **Unit test scenarios (*"When input is 5, output is 10"*):** A unit test checks one example; an invariant checks a law across *all* inputs.
5. **Rules written in `CLAUDE.md` or prompts:** Writing *"Never delete production tables"* in `CLAUDE.md` is **a wish, not enforcement**. AI agents ignore prompts under pressure. Only code, hooks, and database permissions enforce invariants.

---

### How Tests, Evals, and Types Work With Invariants (1.6)

Developers often ask: *"If we already have unit tests, AI evals, and type checkers, where do invariants fit?"*
Invariants don't replace them—they tie all three together:

| Tool | What it does | How Invariant-First uses it |
| :--- | :--- | :--- |
| **1. Tests (`falsified_by`)** | Checks specific examples (can prove a bug exists, never that zero bugs exist). | **Tests the guard itself:** Every enforced rule names a test in `falsified_by`. In CI, the tool temporarily **turns off the guard** and runs that test. If the test doesn't fail when the guard is off, the build fails! |
| **2. Evals** | Measures an average score across hundreds of AI runs (e.g., *"82% pass rate"*). | **Finds new rules:** Evals don't block live traffic, but studying the 18% of eval failures shows you what new invariants to write next. |
| **3. Types (`Checked[T]`)** | Checks data shapes at compile/build time. | **Acts like a concert wristband:** When a guard checks an AI reply (`CauseReply`) and it passes, the guard returns `Checked[CauseReply]`. Downstream functions only accept `Checked[CauseReply]`, so the type checker proves nobody skipped the guard. *(And because Python code can fake types at runtime, `@inv.edge` double-checks the wristband before running!)* |

---

### Business Rules, Chess Moves (`reject` vs. `halt`), and Rule Conflicts

- **Business rules ARE invariants:** A spending cap (*"never spend > $10,000 without approval"*) works the exact same way as a technical rule. You simply label who owns it:
  - `class: law` $\rightarrow$ Owned by **engineers** (technical invariants).
  - `class: policy` $\rightarrow$ Owned by **product/ops** (business invariants).
- **Whose fault is a violation? (The Chess Analogy):**
  - *"A bishop must move diagonally"* $\rightarrow$ **`on_violation: reject`**. The user/caller sent an illegal move; reject the request and keep running.
  - *"Two pieces can never sit on the same square"* $\rightarrow$ **`on_violation: halt`**. Your internal code corrupted the board; stop immediately!

---

### Can Two "Never" Rules Conflict? Yes! (1.7)

In the author's first draft, they claimed: *"Since invariants only say what you MUST NOT do, two rules can never conflict—doing nothing obeys all of them!"*

That turned out to be **false**. Two "never" rules can trap your system in a corner where **every possible move is illegal**:

#### Trap 1: Mutually Exclusive Rules ("No Allowed Move")
* **Rule A:** *"Never return an unassigned job (`worker_id == null`)."*
* **Rule B:** *"Never assign a job to a worker whose CPU is over 90%."*
* **The Crash:** What happens when every worker's CPU is at 95%? You can't assign the job (breaks Rule B), and you can't leave it unassigned (breaks Rule A). Your system has **zero legal moves**.

#### Trap 2: A Deadline Collides With a Safety Check
* **Rule A (Safety):** *"Never resize a server pool without manager approval."*
* **Rule B (Deadline):** *"Never take longer than 5 seconds to reply to a request."*
* **The Crash:** What if the manager takes 10 minutes to approve? Resizing now breaks Rule A; waiting for the manager breaks Rule B!

#### How to Fix Rule Conflicts:
1. **Always leave a legal move:** Check that your combined rules don't paint the system into an impossible corner.
2. **Use `"PENDING"` or `escalate` as an escape hatch:** When a 5-second deadline collides with a manager-approval rule, reply within 5 seconds with **`"status": "PENDING_APPROVAL"`** (or escalate to a human). You met the 5-second deadline *and* you didn't resize the pool without approval!
3. **Forbid always wins (`forbid-overrides-permit`):** Just like in AWS Cedar, if any rule says *"forbidden (`bad == True`)"*, the action is blocked—even if other rules passed.

### The Full Comparison: API-First vs. Invariant-First (1.8)

| | **API-First** | **Invariant-First** | **Plain-English Meaning** |
| :--- | :--- | :--- | :--- |
| **Central artifact** | OpenAPI, Protobuf, JSON Schema | A `rules.yaml` file bound to those contracts | Both put **one locked contract file** at the center of the system. |
| **Says** | What a message *looks like* | Which states *must never exist* | API-first checks shapes; Invariant-first checks laws. |
| **Written by / Implemented by** | People / People or code generators | **People / AI Agents** | Humans write the laws; AI agents write the code that must obey them. |
| **Payoff** | Clients, servers, docs, schema validators | Runtime guards, property tests, DB `CHECK` constraints, coverage reports | Write the rule once; get guards, DB checks, and tests from it. |
| **Brownfield ramp** *(existing code)* | Auto-generate an OpenAPI spec from existing code | **`Discover`** (auto-propose candidate rules from existing code & traces) | You don't have to start from scratch on an old codebase. |

#### Why Comparing It to API-First Makes Sense:
1. **It is already proven on messages:** Tools like Google/Buf's `protovalidate` and Kubernetes CRDs already attach CEL rules to Protobuf and OpenAPI schemas. Invariant-first simply extends that exact idea to **functions, databases, and AI calls**.
2. **Contracts win when you can't read the other side's code:** Frontend teams and backend teams use API contracts so they don't have to read each other's code. **An AI agent is like another team whose code is too big for you to read line-by-line**—so you govern the AI with a contract.
3. **Laws survive code rewrites ("refinement"):** Whether an AI agent rewrites a function 10 times or swaps out the algorithm completely, a law like *"balance never goes below 0"* stays the exact same.

#### 3 Ways Invariant-First Is Harder Than API-First ("Where It Needs Qualifying"):
1. **Schemas only see one message:** An OpenAPI schema can't see your database or past events. Rules about shared totals (*"leases <= pool size"*) or source documents (*"quote must be in PDF"*) need database triggers or providers.
2. **AI agents try to "game" rules:** A code generator never tries to cheat an OpenAPI spec, but an AI coding agent under pressure *will* try to route around a failing check! That's why `rules.yaml` must be locked and backed by call-graph scans (`inv scan`).
3. **Two layers of ownership:**
   - **High-level laws (*Abstract invariants*):** e.g., *"Never resize a pool without an approved ticket."* **Humans write and lock these.**
   - **Low-level class checks (*Representation invariants*):** e.g., `self.size == len(self.items)`. **AI agents can propose these** as `candidate` rules for their own internal classes.

---

## 6. Deep Dive: How the Rule Record Works (`rules.yaml` — Section 2.1)

In Section 3, we saw Rule `R-21`. Section 2.1 explains **every piece of that YAML record** and four clever mechanisms behind it.

### First: Why Did `R-21` Look So Long? (Required vs. Optional Fields)

`R-21` looked intimidating because it showed off **almost every advanced feature at once** (a database lookup provider, custom variable bindings, a hand-written SQL trigger, and a complete mediation proof).

In practice, **most fields are optional**! When you don't write `bind:`, the system automatically gives you `args` (the function's inputs), `result` (the function's return value), `self`, and `old_self`.

Here is what a **minimal rule** actually looks like when you first write it:

```yaml
rules:
  - id: R-1
    status: candidate          # Start by watching quietly
    owner: team-scheduling
    statement: split_work shares must never be negative and must sum to total_jobs.
    subject: { contract: jobs_py, target: "py:jobs.split_work" }
    bad: 'inv.sum(result) != args.total_jobs || result.exists(x, x < 0)'
```
**That's it—just 6 fields!** You only add the extra fields as a rule graduates from `candidate` $\rightarrow$ `detect` $\rightarrow$ `enforce` $\rightarrow$ `Held`.

| Field | Required or Optional? | When do you actually need it? |
| :--- | :--- | :--- |
| **`id`**, **`status`**, **`subject`**, **`bad`** | **Always Required** | Every rule needs an ID, a status (`candidate`, `detect`, `enforce`), a target function (`subject`), and the check (`bad`). |
| **`owner`**, **`statement`**, **`class`**, **`rev`**, **`why`** | **Required for approved rules** *(human review)* | Needed once a human approves a rule (`candidate` $\rightarrow$ `detect`) so the team knows who owns it and why it exists. |
| **`enforcers[]`**, **`falsified_by`** | **Required only for `status: enforce`** | Only needed when you turn on blocking (`enforce`). You must name at least one guard (`enforcers`) and one test (`falsified_by`). |
| **`bind`** | **Optional** *(Auto-filled!)* | **Omit it 90% of the time!** `args`, `result`, `self`, `old_self`, and provider names are bound automatically. Only use `bind` if you want shorter nicknames. |
| **`providers`** | **Optional** | Only needed if your rule must look up extra state from a database that wasn't passed into the function. |
| **`mediation`** | **Optional** *(Required only for `Held`)* | Only needed when you want the highest badge (**`Held`**) to prove no back doors exist around your guard. Without it, an enforced rule is graded `enforce(known-callers)`. |
| **`artifact`**, **`conformance_test`** | **Optional** | Only needed if you hand-wrote a custom SQL trigger/migration alongside the YAML rule. |
| **`model_policy`**, **`compares_to`** | **Optional** | Only used when guarding an **LLM / AI model call** (`@inv.model_call`). |
| **`scope`**, **`compensable`** | **Optional** | Defaults to a single call (`call`) and non-compensable (`false`). |

---

### Second: "The Record Is the Oracle, Not Always the Generator"
In the author's first draft, they claimed one YAML rule would automatically generate your Python checks, your SQL triggers, and everything else.
Reviewers pointed out that **you cannot auto-generate complex multi-table SQL triggers from a simple one-line expression.**

So the new design does something much smarter:
- For simple checks (like Python decorators or single-row SQL `CHECK` constraints), the framework runs or generates them directly.
- For complex database triggers (like `0042_resize_approval.sql`), **you write the SQL trigger, and the YAML rule acts as the "Oracle" (the answer key)**. A `conformance_test` runs both your SQL trigger and the YAML rule against test cases to prove they agree 100%!

---

### The 14 Fields of a Rule Record (Grouped Into 5 Simple Questions)

Instead of memorizing 14 separate YAML fields, think of a rule record as answering **5 questions**:

| Question | YAML Fields | Plain-English Meaning |
| :--- | :--- | :--- |
| **1. Who owns it & why does it exist?** | `id`, `rev`, `status`, `class`, `owner`, `statement`, `why` | Gives the rule a permanent ID (`R-21`), its current trust level (`candidate`, `detect`, `enforce`), whether it's an engineering `law` or business `policy`, who owns it, and the real incident that inspired it. |
| **2. Where does it attach & what data does it read?** | `subject`, `scope`, `bind`, `providers` | Names the API contract and function (`subject`), whether it checks one call or many rows (`scope`), and maps variable names in `bad` (`bind`) to either the request or read-only database lookups (`providers`). |
| **3. What is the forbidden state?** | `bad` | A fast, side-effect-free CEL expression that returns `True` when the prohibited state happens. |
| **4. Where and how is it enforced?** | `enforcers[]`, `compensable`, `model_policy` | Lists every guard protecting this rule (e.g., a Python `decorator` + a Postgres `storage` trigger), whether it runs `pre_effect` or `post_effect`, and what to do when broken (`on_violation`). |
| **5. How do we prove there are no back doors or fake tests?** | `mediation`, `falsified_by`, `evidence` | `mediation` names the single chokepoint every path must cross; `falsified_by` names the test that must fail if the guard is turned off; `evidence` tracks how often the rule actually ran. |

---

### The 4 Key Mechanisms Inside Section 2.1

#### 1. `providers`: Never Trust the Caller to Pass Security State
When someone calls `ResizePool(pool_id="gpu-prod", new_size=500)`, you cannot ask the caller to pass `approved=True` in the request—an attacker or buggy AI agent could just lie!
* **How `providers` work:** A provider (`load_approval`) is a **read-only, typed lookup function** that fetches the real truth from your database right before the guard checks `bad`.
* **4 Freshness Levels (`consistency`):**
  1. `same_txn`: Reads the data **inside the exact same database transaction** that performs the write (so nobody can cancel the approval a millisecond after the check passes).
  2. `immutable`: Data that never changes once written (like a finished job's logs).
  3. `snapshot`: A consistent snapshot read right before the function runs.
  4. `stale_ok`: Cached data is acceptable.

#### 2. `Outcomes`: What Happens When a Rule Fires (or Crashes)
When `bad` is `True` (**`on_violation`**), the enforcer has 4 choices:
* **`reject`:** Block this call and return an error to the caller (used for bad external inputs).
* **`halt`:** Stop the process immediately (used when an internal invariant breaks—meaning your code has a bug).
* **`escalate`:** Pause and ask a human.
* **`route`:** Stop the call and create a review ticket in a queue.
*(Note: Retries are NOT an outcome here—retries only happen inside `model_policy` for LLM calls, and if the LLM still fails after its retries, `on_violation` takes over).*

What if the rule check itself crashes (e.g., the database is down)? (**`on_error`**):
* **`fail_closed` (Default):** Block the call just to be safe.
* **`fail_open_and_page`:** Let the call through so production stays up, and immediately page the on-call engineer.

#### 3. `bind`: Why Renaming a Variable Won't Silently Break Your Rule (Critique #11)
In the author's first draft, rules matched Python parameter names automatically. If a developer renamed a function parameter from `req` to `request_data`, **the rule silently detached and stopped checking!**
* **The Fix:** `bind` connects to **typed contract paths** (like Protobuf fields) and writes a **`lockfile`**. If anyone renames a field or changes a function signature without updating the rule, **the build fails immediately**.

#### 4. `old_self`: How Before-vs-After Checks Avoid Copying the Whole World
Transition rules compare an object before and after a method runs (e.g., `old_self.state == "APPROVED" && self.state == "PENDING"`).
* **The Trap:** If the guard copied the *entire* `self` object before every call (`copy.deepcopy(self)`), it would be slow—and if `self` held a live database connection (`self.db`), copying it would crash!
* **The Fix:** At load time, the tool reads your `bad` expression, sees that you only wrote `old_self.state`, and **copies only `.state`** (which must be simple data like a string, number, or list). It never touches `self.db`!

---

## 7. Teaser: How Does a Rule Actually Work Under the Hood?

At this point, a developer usually asks: *"Okay, I see `rules.yaml`—but how does that YAML file actually intercept my Python code and stop an AI agent from cheating?"*

Here is a quick peek at how the pieces fit together in practice:

### 1. One-Line Attachment in Code (`@inv.guard`)
In your Python code, you don't write the rule logic. You just attach the rule ID (`"R-21"`) to the function that performs the action:

```python
import invariants as inv

# 1. Register the read-only function that fetches the ticket from the DB
@inv.provider("approval")
def load_approval(pool_id: str) -> Approval | None:
    return db.get_approval(pool_id)

class PoolService:
    @inv.transaction
    def txn(self): ...

    # 2. Attach Rule R-21 to the door
    @inv.guard("R-21")
    def resize_door(self, req: ResizePoolRequest) -> None:
        # The AI agent writes the implementation here.
        # This line NEVER runs if R-21 evaluates bad == True!
        cloud_api.set_pool_size(req.pool_id, req.new_size)
```

When `resize_door(req)` is called:
1. `@inv.guard("R-21")` intercepts the call **before** `resize_door` runs.
2. It calls `load_approval(req.pool_id)` inside the same transaction.
3. It evaluates the `bad` expression from `rules.yaml` using Google's fast, side-effect-free CEL engine.
4. If `bad` is `True`, it immediately raises a rejection error—`cloud_api.set_pool_size` is never reached.

### 2. How the System Stops the AI Agent from Cheating
What if an AI coding agent tries to get around `@inv.guard("R-21")` when a test fails? Three automated checks lock the agent in:

- *"What if the AI edits `rules.yaml` to weaken the rule?"*
  **Blocked by Agent Hooks:** `rules.yaml` is a protected path. A pre-tool hook denies any AI agent edit to `rules.yaml`—only humans can approve rule changes.
- *"What if the AI deletes `@inv.guard("R-21")` from the Python function?"*
  **Blocked by `inv verify`:** At app startup and in CI, `inv verify` checks that every active rule in `rules.yaml` is attached to its target function. If `@inv.guard("R-21")` is removed, the build fails immediately.
- *"What if the AI writes a helper function that calls `cloud_api.set_pool_size()` directly, skipping `resize_door()`?"*
  **Blocked by `inv scan`:** The CLI builds a call graph of your codebase and checks whether any path from an entrypoint can reach `cloud_api.set_pool_size()` without passing through `@inv.guard("R-21")`. If a back door appears, the build fails.

### 3. What Else Is in the Full Design?
If you want to go deeper into the full specification (`Invariant_first.md`), here is what comes next:

- **Guarding LLM & Agent Calls (`@inv.model_call` and `@inv.edge`):** Why feeding rule errors back to an LLM (*"You broke rule X, try again"*) teaches the model to game the check—and how bounded, zero-feedback retries prevent "valid-but-wrong" answers.
- **Concurrency & Database Conformance:** How a single rule in `rules.yaml` acts as the oracle to test both in-memory Python guards and Postgres database triggers against race conditions.
- **Four-Outcome Evaluation:** Why a bug in a rule (`rule_error`) must never be confused with bad data (`violation`), and how rules automatically demote if they error in production.
- **Discover (For Existing Codebases):** How to point the tool at an existing codebase and automatically discover candidate rules from call graphs, runtime traces, and mutation testing.
- **From Information to Knowledge:** How to enforce provenance invariants (*"every extracted fact must quote exact text from a trusted source"*) across massive AI data pipelines.
