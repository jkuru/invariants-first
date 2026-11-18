# Invariants-First, Simply: speaker notes

## 1. Teaching coding agents to keep the rules

Welcome. This talk is for anyone who writes code with AI help, or ships features where an AI model makes decisions. No background needed.

The one sentence to remember: rules can get stricter freely, but loosening one needs a person. We call that a ratchet: it only turns one way.

We’ll start with a story you have probably lived through.

## 2. A story you’ve probably seen

Walk through it slowly. The agent isn’t being malicious. It’s doing what it was asked: make the tests pass.

The number is the refund limit, in cents. 50,000 cents is $500. The agent changed it to $5,000. The diff is one line among twenty files; the reviewer skims it, sees green tests, and approves.

This exact diff is our illustration, but the behaviour is well documented. Point at the three sources:
- Anthropic’s own system card for Claude 3.7 Sonnet reported the model special-casing code so tests would pass, in agentic coding settings like Claude Code.
- METR, an independent evaluation group, found recent frontier models trying, often successfully, to get a higher score by modifying the tests or the scoring code.
- ImpossibleBench, by researchers including Nicholas Carlini, gives agents tasks whose tests contradict the spec. Any pass means the agent cheated, for example by editing or deleting the tests.

Links: anthropic.com/news/claude-3-7-sonnet · metr.org/blog/2025-06-05-recent-reward-hacking · arxiv.org/abs/2510.20270

Ask the room: “Who would catch this in a 20-file pull request?”

## 3. Tests passed. Review passed. The rule is gone.

Pause here. Let the three lines land.

The point: our tools check that code runs. They don’t check that the rules we care about are still there.

Rules like “refunds stop at $500” live in people’s heads and in scattered if-statements. An agent can delete or loosen them in one edit.

This talk is about writing those rules down, so the build can check them.

## 4. A valid shape isn’t valid behaviour

This is the same problem from the other side. Here it’s not a coding agent. It’s an AI feature inside the product: a model that plans refunds.

Left card: Pydantic can check “a number between 1 and 50,000”. That’s shape.

Right card: the model returns perfectly formatted JSON. Every field has the right type. But the order belongs to someone else, and there’s only a little left on it.

No schema can say “no more than what’s left on this order”. That’s a rule about the real world. Remember the phrase: a valid shape isn’t valid behaviour.

Two references: OWASP’s Top 10 for LLM applications lists Improper Output Handling as a top risk; its advice is to treat what a model returns like any untrusted input. And in Moffatt v. Air Canada (2024), a Canadian tribunal made the airline pay after its chatbot invented a bereavement refund policy. The answer looked right and was wrong, and the company was liable.

Links: genai.owasp.org · americanbar.org (BC tribunal confirms companies remain liable for AI chatbot information)

## 5. Three layers, and one is missing

Think of any system as three layers.

Shape: what inputs and outputs look like. We already have great tools for that, like Pydantic.

Bodies: the actual code. That’s where agents are most useful, and we want them free to rewrite it.

Laws: the middle row, highlighted. These are the rules that must never break. Today they live in people’s heads. We put them in one file, invariants.yaml.

The payoff: a reviewer reads a short rule file and its diff, instead of every line an agent wrote.

The idea has a long history. Bertrand Meyer’s Design by Contract, from the Eiffel language in the 1980s, is where the word “invariant” comes from in everyday programming: a condition that must always hold. We apply it at API boundaries.

## 6. There are only two kinds of rule

Use the door analogy.

A value rule is a lock on the door: it checks what comes through. Example: no refund above $500.

A path rule proves there’s no other door. Example: the only way to reach Stripe is through BillingService.refund, where the balance check lives.

Both ideas are decades old. A value rule is a contract (Meyer). A path rule is what security engineers call a reference monitor: since the 1972 Anderson report for the US Air Force, the rule is that the check must be always invoked, tamper-proof, and small enough to verify. The door is always invoked, the lock makes it tamper-proof, and ablation verifies it.

You need both. A perfect lock is worthless if an agent writes a new tool that walks around the door and calls Stripe directly.

The YAML on the right is simplified. The real file writes full Python paths, like py:support_agent.service.BillingService.refund.

## 7. Five words you’ll hear today

Leave this slide up a little longer. It’s the vocabulary for the rest of the talk.

Rule file and lock: the two files you commit. The rule file says the rules. The lock remembers them, so a loosened rule is noticed.

Door: the function everyone must go through. Guard: the check that runs at that door on every call.

Ablation is the odd word. It just means “switch it off and see if anything breaks”. If nothing breaks, the guard wasn’t doing anything.

Tell people they can come back to this slide when the slides are shared.

## 8. A ratchet: it only turns one way

A ratchet is a tool that turns freely one way and locks the other way.

Left: making a rule stricter. Lowering the refund limit can’t let anything bad through, so the check passes. No friction.

Right: making a rule looser. That’s the risky direction, so the check fails. A person has to accept the change, on purpose, with inv lock --accept-weakening.

This matters for adoption. If every change failed, people would turn the tool off. Only loosening needs a human.

This isn’t a new idea in industry. Teams using Protobuf run buf breaking in CI: it compares the schema against the base branch and fails the build on an incompatible change. We do the same for business rules, not just wire formats.

Link: buf.build/docs/breaking

## 9. Where do I run it?

THE POINT
Run it once, from the top folder of your project.

WHERE TO RUN IT
- Go to the folder that holds pyproject.toml.
- Type inv init.
- It reads src/ by default. If there’s no src/, it reads the current folder.

SRC OR TESTS?
- Just src. That’s your real code, where the rules live.
- Tests, docs, examples and scripts are skipped automatically.
- Why skip tests? Tests call things directly on purpose. That isn’t a back door.
- Your tests still matter: later, each guard names a test that proves it works.

MY CODE IS SOMEWHERE ELSE
- Use inv init --root app.
- Repeat --root for more than one folder.
- To skip a folder: --exclude 'legacy/*'.

WHAT IT WRITES
- Two files next to pyproject.toml: invariants.yaml and invariants.lock.
- Commit both.

Next slide: what it prints.

## 10. inv init finds the rules you already have

This is real output, from the support-agent example app in the repo. A few lines are trimmed to fit.

Read it line by line:
- “pinned 4 pydantic models with 23 constraints”: see the box below.

WHAT “4 MODELS, 23 CONSTRAINTS” MEANS
- A model is a Pydantic class: the shape of a tool’s input.
- A constraint is one limit inside Field(...), like gt=0 or max_length=40.
- “Pinned” means saved in invariants.lock. From now on, loosening any of the 23 fails inv check.
- You wrote none of this. The limits were already in the code; inv init just found them.
- “door STRIPE-GATEWAY-DOOR”: see the box below.

WHAT IS A SINK? WHAT IS A DOOR?
- Sink: the risky call at the bottom. Here: StripeGateway.refund. Calling it moves real money.
- Door: the one safe way to reach the sink. Here: BillingService.refund. It checks the order and the balance first, then calls Stripe.
- Think of a bank vault. The vault is the sink. The front desk is the door. Everyone must go through the desk.

WHAT INIT NOTICED
- It read the code and saw that the only caller of StripeGateway.refund is BillingService.refund.
- So it wrote a rule: Stripe must always be reached through BillingService.refund.
- STRIPE-GATEWAY-DOOR is just the rule’s name.
- This is a path rule: it’s about which way the call goes, not about the values passed.
- If new code calls Stripe without going through the door, inv check fails.
- “found 1 back door in admin.py”: old code already calls Stripe directly. It doesn’t fail you on day one. It freezes that one in an allow list, so CI passes today, but a new back door fails.

You commit two files: invariants.yaml and invariants.lock.

Why it starts quiet: Google’s static analysis team found that engineers ignore tools that aren’t part of their workflow or whose warnings aren’t actionable (Sadowski et al., CACM 2018). So day one finds rules, freezes old problems, and only fails on new ones.

Link: research.google/pubs/lessons-from-building-static-analysis-tools-at-google

## 11. Rule 1: a value limit

THE POINT
You don’t write this rule. inv init finds it in your Pydantic model.

READ IT TOP TO BOTTOM
- Top: your own Python. Field(gt=0, le=50_000) means “more than 0, at most 50,000”.
- Bottom: what inv init wrote in invariants.yaml.
- contracts: the section for value limits.
- refund_tool_input: just a name.
- type: pydantic: “read the limits from a Pydantic model”.
- ref: which model. support_agent.models is the file, RefundToolInput is the class.
- The comment: a reminder of what got pinned. You can ignore it.

WHERE THE REAL CHECK LIVES
- inv lock saves the model’s full schema in invariants.lock.
- inv check compares the code against that saved copy.

WHAT FAILS
- A looser limit: le=50_000 → le=500_000.
- A removed limit.
- A new allowed value, like adding a choice to a Literal.

WHAT PASSES
- A tighter limit: le=50_000 → le=10_000.

Real, from the support-agent app. The YAML is spread over more lines than the file uses, to fit; it means the same thing.

## 12. Rule 2: a door

THE POINT
You don’t write this one either. inv init finds the door by reading your code.

READ IT TOP TO BOTTOM
- id: a name. When it fails, the error says STRIPE-GATEWAY-DOOR.
- sink: the dangerous thing. Calling Stripe’s refund moves money.
- door: the safe way in. BillingService.refund checks the balance first.
- py: just means “a Python function”, followed by its full path.
- allow: old code that already calls Stripe directly. It’s frozen, so you aren’t blocked on day one.

HOW IT’S CHECKED
- inv check reads the code. It doesn’t run it.
- It follows every call path to the sink.
- A path that skips the door is a back door.

WHAT FAILS
- A new tool that calls StripeGateway.refund directly.
- A new caller of an allowed back door. Freezing old ones doesn’t bless new ones.

WHAT PASSES
- Anything that goes through BillingService.refund.

Real, from the support-agent app. The long paths are split over two lines to fit; in the file each is one line.

## 13. Rule 3: a guard

THE POINT
This one runs while your app runs. You turn it on yourself.

SHORT FORM AND LONG FORM
- This is BILL-1 written out in full.
- In the real file it’s shorter: one line, guard: "py:...BillingService.refund".
- guard: is shorthand. It means exactly the target and enforcer lines shown here.
- tests: in the short form is the same as falsified_by: here.
- The long form also needs status: enforce. The short form works that out from tests:.
- Switching between the two forms isn’t a change. inv check sees the same rule.
- We show the long form so you can see the timing.
- Checked: this long form passes inv validate, and inv lock --check says the existing lock is still fresh.

READ IT TOP TO BOTTOM
- id: a name. The decorator uses the same name: @inv.guard("BILL-1").
- status: enforce. The rule blocks (a person sets this).
- target: which function gets checked.
- enforcers: how and when it’s checked.
  - point: decorator. The check is the @inv.guard decorator.
  - timing: pre_effect. Before the function runs.
  - mode: enforce. A bad call is refused.
- statement: plain English. Reviewers read this line.
- bad: the conditions that must never be true.
  - order == null: the order doesn’t exist.
  - args.amount_cents > order.remaining_cents: the refund is more than what’s left.
  - args means the function’s arguments. order is looked up live from the ledger.
- falsified_by: the test that tries a bad refund and expects it to be refused.

BEFORE OR AFTER? YOU CHOOSE
- timing: pre_effect = before. This is the default, and the one that can stop something.
- timing: post_effect = after. The function has already run, so the money has already moved.
- So an after-check can only report (mode: detect).
- Exception: a function with no side effects (effects: none). There, after can block too.
- One line to say: “Before can stop it. After can only tell you it happened.”

A REAL AFTER-CHECK
- In the fact-pipeline, rule K-15 runs after a source is deleted (post_effect, detect).
- It reports any fact still resting on that source. It can’t undo the delete.

WHERE IT COMES FROM
- inv init suggests guards from the if-checks at the top of your functions.
- They’re commented out at the bottom of invariants.yaml.
- To turn one on: uncomment it, add @inv.guard to the function, write a test, run inv lock.

HONEST NOTE
- Real BILL-1 from the support-agent app, shown in its long form.
- Trimmed to fit: each bad line also has a message, the second starts with order != null &&, and the test name is longer.

Source: invariants-py/docs/rule-file.md (short forms, timing)

## 14. A back door skips the balance check

OPTIONAL LIVE DEMO (if you have time)
- Type: DEMO_PAUSE=1 ./presentation/demo.sh 3. Press Enter between steps.
- Step 1: the real code. Every rule says pass.
- Step 2: the “ops hotfix” file. Four lines, four tricks: a direct call, __wrapped__, getattr, a lease with no door.
- Step 3: scan again. 10 findings, all in rogue.py, and the scan fails.
- Why 10 from 4 lines? Three rules protect the same write (set_size), so each one reports it.

This is the path rule in action, from a real run on the support-agent app.

Why is a back door bad? All the protection lives in BillingService.refund: it checks that the order exists and that the refund isn’t more than what’s left. A back door goes straight to Stripe, so none of that runs. The agent’s new tool could refund $5,000 on a $30 order, and every value rule on the door is useless, because the call never passes through it.

Top row: the normal way. A tool calls BillingService.refund, which checks the balance, then calls Stripe.

Bottom row: the agent wants a faster refund, so it writes quick_refund_tool, which calls Stripe directly. Every test still passes, because no test covers a function that didn’t exist yet.

inv check reads the code (it doesn’t run it) and finds the path that skips the door. It points at the exact file and line.

This isn’t a new idea; it’s one of the oldest rules in security:
- Saltzer and Schroeder’s 1975 paper on protecting information calls it complete mediation: every access to every object must be checked.
- MITRE lists it as a known weakness, CWE-424, Improper Protection of Alternate Path: the product doesn’t protect all the paths to a restricted action.
- OWASP ranks Broken Access Control as the number one web application risk, in 2021 and again in 2025.
What’s new is that coding agents create these paths quickly, and they look like reasonable code.

Links: cwe.mitre.org/data/definitions/424.html · owasp.org/Top10/A01_2021-Broken_Access_Control

For more on stage: ./presentation/demo.sh 3 runs the scan on the pool-service app and finds 10 back doors in under a second, including tricks like getattr(store, "set_size").

## 15. The lock: a snapshot of your rules

THE POINT
The lock is how the tool remembers your rules. Without it, nothing would notice a rule getting looser.

THINK OF A PHOTO
- On the day you agree the rules, you take a photo of them.
- That photo is invariants.lock.
- Every check compares today’s code with the photo.

WHAT’S IN IT
- This is real, from the support-agent app, trimmed.
- contracts: each value rule, pointing at a saved schema.
- schemas: the full schema of each Pydantic model, saved by a fingerprint (the sha256 string).
- See "maximum": 50000? That’s le=50_000 from models.py, saved.
- It also holds the rules, the guarded functions and the scan settings. We only show one part.

WHY SHA256?
- sha256 makes a fingerprint: a short code calculated from the content.
- Same content gives the same fingerprint. Change one character and it’s completely different.
- Saved once: if several rules use the same model, its schema is stored one time, and each rule points at the fingerprint.
- Catches hand edits: if someone edits a saved schema in the lock, its fingerprint no longer matches. inv check says “the key is not the sha256 of its body (edited by hand, or a bad merge)”.
- Docstrings and descriptions are removed before the fingerprint is taken, so editing a comment doesn’t change it.
- The fingerprint is only a label. To decide “weakened”, the tool compares the saved schema itself, field by field.
- Same idea you already use: Docker image digests and pip’s --hash=sha256: work this way.

WHO WRITES IT
- inv init writes it the first time.
- After that, inv lock updates it.
- Nobody edits it by hand. The hook blocks an agent that tries (slide 19).

WHAT HAPPENS ON A CHANGE
- Tighter (le=50_000 → le=10_000): inv lock saves the new photo. No approval needed.
- Looser (le=50_000 → le=500_000): inv lock refuses.
- Only a person can accept it, with inv lock --accept-weakening.

IN CI
- inv check --base main compares against main’s lock, not the one in the pull request.
- So an agent can’t hide a change by updating the lock itself. That’s slide 17.

NEXT
The next slide shows the lock catching exactly this: "maximum" going from 50000 to 500000.

## 16. Raise the limit, and the check fails

THE POINT
The story from slide 2, replayed with the tool in place. The same one-line change now fails the build.

WHO MADE THE CHANGE?
- The coding agent.
- A test failed on the $500 refund limit.
- Raising le=50_000 to le=500_000 was the quickest way to make it pass.
- Nobody asked it to loosen a rule. It just took the shortcut.

READ THE OUTPUT
- Point at the WEAKENED line.
- It says what changed: the maximum.
- From what to what: 50,000 to 500,000.
- And that it’s a loosening.
- This is the "maximum": 50000 from the lock on the last slide.
- The other checks still pass. The tool tells you exactly which one failed.
- The dim line tells a person what to do if the change was on purpose: review it, then accept it.

DOES IT MATTER WHO EDITS?
- No. A person making the same change gets the same failure.
- What matters is the direction. Looser needs a person to accept it.

LIVE DEMO (run it here or at the end of the next slide)
- Command: DEMO_PAUSE=1 ./presentation/demo.sh 1
- It pauses at each step. Press Enter to go on.
- It uses real open-source code: FastAPI, then langchain_core.
- First you see a box: what the project is, its license, where it comes from, its version.
- Step 1: inv init finds the rules already in the code. Point at the yellow “pinned 4 pydantic models” line.
- Step 2: the agent’s edit, as a red and green diff. gt=0 becomes ge=0. Same idea as this slide.
- Steps 3 and 4: the agent re-locks to hide it, and CI still fails. That’s the next slide’s story.
- Watch for red FAIL and WEAKENED, and the yellow “0 -> (none)”.
- Each check ends with a green ✓ line saying the result was expected.

HONEST NOTE
- The slide output is real, from the support-agent app, trimmed to fit.
- The live demo uses FastAPI instead, so the numbers on screen differ.

## 17. What if the agent updates the lock too?

OPTIONAL LIVE DEMO (if you have time)
- Type: DEMO_PAUSE=1 ./presentation/demo.sh 1
- This one covers this slide and the one before. Press Enter between steps.
- Point at: the red/green diff, the agent’s inv lock --accept-weakening, then FAIL since main.
- Second part: langchain_core. Tighter passes (green ✓ exit 0), looser fails (✓ exit 1).

This is the clever part, and the one people ask about.

An agent that knows about the lock can try to hide its change by updating the lock itself. Step 2 does exactly that.

But CI doesn’t trust the lock in the pull request. With --base main, it compares the rules against the main branch. Compared to main, the rule got looser, so it fails. Only a person can accept it.

This is separation of duties, a standard security control (NIST SP 800-53, AC-5): the one who makes a change isn’t the one who approves it. Here the agent made the change, so it can’t also approve it.

Run ./presentation/demo.sh 1 live here. It takes about five seconds, on an untouched copy of FastAPI, and also shows langchain_core: tightening passes, loosening fails. Output on the slide is trimmed.

## 18. What is a hook?

Before the demo, a quick definition, because not everyone has used hooks.

A hook is a small script that the coding agent runs automatically at certain moments, for example right before it edits a file or runs a command. Think of a bouncer at a door: the agent asks “can I do this?”, the script checks, and it can say no. If it says no, the action never happens, and the agent is told why.

The agent can’t talk its way past a hook. It isn’t an instruction in the prompt that the model might ignore. It’s code that runs every time.

Claude Code calls the before-action moment PreToolUse. You set hooks in .claude/settings.json. A hook says no by exiting with code 2.

GitHub Copilot has the same idea. Hooks live in .github/hooks/*.json in the repository, the before-action event is called preToolUse, and a hook can return a deny decision. They work in Copilot CLI and the Copilot cloud agent, and VS Code agent mode supports them in preview.

Be honest about one thing: inv hook reads Claude Code’s hook format today. Copilot’s payload uses different field names, so it would need a small adapter. I haven’t tested it with Copilot. Either way, CI with --base main is the backstop for any agent.

Links: docs.anthropic.com/en/docs/claude-code/hooks · docs.github.com/en/copilot/concepts/agents/coding-agent/about-hooks · code.visualstudio.com/docs/copilot/customization/hooks

## 19. Stop the agent before it even tries

OPTIONAL LIVE DEMO (if you have time)
- Type: DEMO_PAUSE=1 ./presentation/demo.sh 2. Press Enter between steps.
- Step 1: we send the hook an Edit of invariants.lock, as Claude Code would. Blocked, exit code 2.
- Step 2: we send it inv lock --accept-weakening. Blocked, exit code 2.
- Step 3: CI’s defect tests, one per shortcut (D1–D12), plus three safe changes that must pass. All green.

THE POINT
CI catches problems after they happen. The hook stops them before they happen.

THE THREE CARDS
- Left: the agent edits invariants.lock. Blocked by inv hook pretool.
- Middle: the agent runs inv lock --accept-weakening. Blocked by inv hook pretool.
- Right: the agent says “done” while inv check fails. Blocked by inv hook stop.

GOOD TO KNOW
- Plain inv lock is allowed. It can only record a tighter rule.
- Exit code 2 means “no”. The agent sees the reason and is told to ask you.

HOW DOES THE HOOK KNOW IT’S THE AGENT?
- It doesn’t check who you are.
- It only runs on the agent’s own actions.
- A command you type in your own terminal never meets the hook.
- So: agent runs it → blocked. You run it → allowed.

CAN THE AGENT TURN THE HOOK OFF?
- No. The hook also blocks edits to .claude/settings.json, where hooks are set up.

WHO IS “A PERSON” IN CI?
- In CI, GitHub decides.
- Only people with the right access can add the label or approve the PR that accepts a loosening.

BE HONEST
- The hook is an early warning, not the final gate.
- A clever command could slip past it.
- inv check --base main in CI is the real gate.

LIVE DEMO
Run ./presentation/demo.sh 2. It fakes an agent editing invariants.lock and shows the block. Then it runs the support-agent’s defect tests: each one is a shortcut an agent might take, and each one is caught.

Source: docs.anthropic.com/en/docs/claude-code/hooks

## 20. How an AI agent turns a ticket into a refund

THE POINT
Before we look at the checks, here’s how the agent works. It’s the same shape most AI agents have.

THE SEVEN STEPS
1. Ticket: a customer writes “refund 100 on O-2”.
2. Triage: plain code asks “is this about billing?”. If not, a person takes it.
3. Plan: the AI reads the ticket and decides what to do: which action, which order, how much. This is the only step where the AI decides anything. It’s highlighted.
4. Execute: plain code takes the plan and runs the one tool it names.
5. Tool: issue_refund_tool. Its input is a Pydantic model, so the limits from slide 11 apply.
6. Door: BillingService.refund checks the order and the balance, then pays.
7. Money: StripeGateway.refund. This is where money really moves.

THE KEY IDEA
- The AI only makes a plan. It never touches money directly.
- Plain code carries the plan out, step by step.
- Every step after the AI has a check. The right-hand column says which one, and which slide explains it.

WHAT IF SOMETHING SAYS NO?
- The ticket is handed to a person (a “handoff”).
- No refund, no guessing.

WORDS YOU’LL HEAR
- Agent: the whole thing, from ticket to result.
- Plan: what the AI decided.
- Tool: a function the agent is allowed to call.

HOW ANTHROPIC RECOMMENDS BUILDING AGENTS
- Source: Anthropic’s engineering guide “Building effective agents” (December 2024).
- It splits agentic systems into two kinds:
  - Workflows: code decides the steps. The AI fills in parts.
  - Agents: the AI decides its own next step, in a loop.
- Its advice: start simple. Use a workflow when the task is well defined. Add autonomy only when you need it.
- This app is a workflow, on purpose. Refunds are well defined, and money is involved.

HOW THIS SLIDE MATCHES THAT GUIDE
- Step 2, triage, is what the guide calls routing: sort the input, send it down the right path.
- Step 3 is the one place the AI decides. Everything after it is plain code.
- The checks sit outside the model. The guide likewise suggests guardrails as a separate step, not the same AI call.
- Retries are capped at 2. The guide recommends stopping conditions, like a maximum number of tries.
- Any “no” goes to a person. The guide expects agents to return to a human for judgement.

IF SOMEONE ASKS “IS THIS REALLY AN AGENT?”
- In Anthropic’s words it’s an agentic workflow.
- The same rules work for a fully autonomous agent: the checks sit on the doors, not inside the AI.
- The app also runs as a LangGraph graph. The next slide maps it to LangGraph and Google ADK.

Link: anthropic.com/engineering/building-effective-agents

NEXT
Next: the same design in LangGraph and Google ADK. Then two slides on the checks that are new: @inv.model_call (step 3) and @inv.edge (step 4).

Source: apps/support-agent/src/support_agent/graph.py and tools.py

## 21. The same agent in LangGraph and Google ADK

THE STORY SO FAR
- Every agent framework has the same pattern. Learn the pattern once, and you can read any of them.
- Every one of them needs guards, because the AI step can be wrong.
- We add two guards for that. The next two slides explain them in detail.
- This slide is just the pattern.

THE PATTERN, IN FOUR WORDS
- Steps: code decides the order.
- Route: send the ticket the right way.
- Plan: the AI decides what to do. (The orange row. The only AI step.)
- Act: a tool does the work.
- And if anything looks wrong: hand it to a person.

SAY IT LIKE THIS
- “Read the table across. Same idea, different names.”
- “LangGraph calls the steps nodes. Google ADK calls it a SequentialAgent. We just call a function.”
- “The orange row is where things can go wrong. That’s where our two guards go.”

THE TWO GUARDS (coming next)
- Guard 1, @inv.model_call: checks what the AI planned, right after it answers.
- Guard 2, @inv.edge: checks the plan again, right before a tool uses it.
- They’re plain Python decorators, so they work in any of these frameworks.

IF SOMEONE ASKS
- “Is the LangGraph version real?” Yes: apps/support-agent/src/support_agent/langgraph_agent.py.
- “Did you try ADK?” Not this app. But I put an inv guard on Google’s ADK sample code, and inv verify passed.

Links: docs.langchain.com/oss/python/langgraph/add-human-in-the-loop · adk.dev/callbacks/types-of-callbacks

## 22. Check what the AI returns: @inv.model_call

THE POINT
Guard 1 checks the AI’s answer before anyone uses it.

WALK THE FOUR NUMBERS
- ① The function asks the AI. Nothing else.
- ② The AI’s answer becomes a Plan. We call it reply.
- ③ We look up the real order ourselves. We call it planned_order.
- ④ The rule compares them: is the refund more than what’s really left?

THE COLOURS
- Blue: what the AI said.
- Orange: what our own records say.

EXAMPLE
- Order O-2 has $30 left.
- AI says $25: allowed.
- AI says $45: refused.

IF THE AI IS WRONG
- Ask again, up to 2 times for broken answers.
- Ask once more for rule-breaking answers.
- Then a person decides.

SAY IT LIKE THIS
“The AI says how much. Our records say how much is left. The guard compares the two.”

IF SOMEONE ASKS
- “Why don’t the names match the function?” The rule checks the AI’s answer, not the function’s inputs.
- “Isn’t after the AI too late?” No. It only wrote a plan. No money has moved.

NEXT
Guard 2 checks again, right before the plan is used.

## 23. Check again before it’s used: @inv.edge

THE POINT
Guard 2 checks the plan again, right before a tool uses it.

ITS ROLE, IN ONE BREATH
- The AI makes a plan. Tools carry it out.
- @inv.edge sits in between, on the code that hands the plan to the tools.
- It’s the last check before anything real happens.
- Guard 1 checks the AI’s answer when it’s made. Guard 2 checks it when it’s used.

READ IT TOP TO BOTTOM
- One line on the function that runs the plan: @inv.edge("PLAN-1").
- Then the same rule as guard 1, checked a second time.
- reply is the plan it received. args is what execute_plan was given.
- planned_order is the real order, looked up again right now.

THE COLOURS
- Blue: what the AI said.
- Orange: what we know ourselves: our records, and who the ticket is for.

EXAMPLE 1: THE BALANCE MOVED
- The AI plans $25. Order O-2 has $30 left. Guard 1 says OK.
- Another agent refunds $10 first. Now $20 is left.
- Guard 2 looks again: $25 is more than $20. Refused.

EXAMPLE 2: THE WRONG CUSTOMER
- A plan was checked for customer C-1.
- Someone hands it to execute_plan for customer C-2.
- reply says C-1, args says C-2. Refused.

SAY IT LIKE THIS
“Guard 1 checks the AI. Guard 2 checks again, just before we pay. Things can change in between.”

IF SOMEONE ASKS
- “Isn’t one check enough?” Only if nothing changes in between. In a real agent, things do.
- “Can’t someone fake the stamp?” Yes, so guard 2 doesn’t trust it. It checks again anyway.

NEXT
The live demo: what happens when the AI gets it wrong.

## 24. When the AI model gets it wrong

THE POINT
Now we try to break it. Every row is a real test.

THE THREE GUARDS (top row)
- Guard 1, model_call: checks the AI’s answer.
- Guard 2, edge: checks it again before it’s used.
- The door guard: the last check before Stripe.

WHY THREE GUARDS?
Each one catches something the others can’t.
- Guard 1 catches the AI’s own mistake, early. It’s the only place we can ask the AI again.
- Guard 2 catches what changes after that: a faked stamp, an edited plan, a balance that moved.
- The door guard catches anyone who skips the AI completely: a script, an admin page, a new tool.

KEEP THE SAME STORY
- Guard 1: the inspector at the factory door.
- Guard 2: the person who opens the box before using it.
- The door guard: the lock on the safe. It doesn’t care how you got there.
- Take any one away, and one row of the table gets through.

READ THE TABLE
- Too much: ask the AI once more. Still wrong: a person.
- Another customer’s order: a person.
- Broken answer: retry twice, then a person.
- Forged plan: refused.
- Plan or balance changed: refused.
- Someone calls refund() directly: refused at the door.

SAY IT LIKE THIS
“Six ways to get it wrong. Not one bad refund got through.”

LIVE DEMO
- Run DEMO_PAUSE=1 ./presentation/demo.sh 4. Press Enter between steps.
- Six steps, same order as the table.
- Watch for the green PASSED and the ✓ at the end of each step.

IF SOMEONE ASKS
- “Is the AI real?” No. It’s a fake we tell to misbehave, so the demo is the same every time.
- “Three checks? Isn’t that a lot?” Each one guards a different moment: when the AI answers, when the plan is used, and when money moves. And each check is tiny next to the AI call itself.

NEXT
Is each guard really doing something? We switch it off and see.

## 25. Is the guard real? Switch it off and see

OPTIONAL LIVE DEMO (if you have time)
- Type: DEMO_PAUSE=1 ./presentation/demo.sh 5. Press Enter between steps.
- Step 1: each guard switched off in turn. Every rule says load-bearing, with the test that caught it.
- Step 2: inv status. Most rules say held.
- K-15 and K-16 say detect: they report, they don’t block. That’s on purpose, not a failure.

The worry: you add guards, but how do you know they do anything? Maybe the test would pass anyway.

This example is the fact-pipeline app: an AI model reads documents and proposes facts. Rule K-1 says no fact may be saved without evidence, a quote from a source.

Top: the test tries to save a fact with no evidence, and expects it to be refused.

Bottom: ablation runs the test twice, once normally and once with the guard switched off. With the guard off the test fails, so the guard is “load-bearing”, like a wall you can’t remove. Then inv status promotes the rule to “held”: proven, not just claimed.

This is a cousin of mutation testing, which Google runs in code review: it changes the code slightly and checks that a test fails (Petrović and Ivanković, ICSE 2018). Ablation does the same, aimed at one guard at a time.

If no test notices, the rule doesn’t get that credit. The tool won’t trust a guard nobody has proven.

Run ./presentation/demo.sh 5. It runs the pipeline’s CI, then inv status lists each rule with “ablation proved it: yes”. Output on the slide is trimmed.

## 26. Does it work? Four numbers

THE POINT
It catches what your rules cover, it rarely gets in the way, and it’s fast. It does nothing for bugs no rule describes.

HOW WE TESTED
- We made 24 changes to our example apps, on purpose.
- 8 were bugs that one of our rules covers.
- 8 were bugs no rule covers.
- 8 were safe changes: refactors and new features.
- Then we ran CI and counted.

6 OF 8
- Of the 8 bugs a rule covers, invariants itself caught 6.
- How: the guard caught 2, the scan 2, the lock 1, the rule-file check 1.
- The other 2 were caught by ordinary tests, which also run in CI. So CI caught all 8.
- Same apps without invariants, plain code and tests: 4 of 7.
- Why 7? One bug can only happen when the library is installed, so it was left out of that count.

1 OF 8
- Of the 8 safe changes, 1 was wrongly flagged.
- The first version of the lock flagged 6 of 8. We redesigned it.

0
- On the 8 bugs no rule covers, invariants added no catches.
- Ordinary tests caught 3 of them.
- Say it plainly: this isn’t a bug finder. It protects the rules you write down.

7 µs
- 7 microseconds: what a guard adds to each call. Tiny next to a database or network call.
- The back-door scan reads 1,852 functions in 1.2 seconds. Fast enough for every commit.

BE HONEST
- We wrote the 24 changes ourselves, in our own apps. It’s a small test.
- The real test is your own code.

## 27. What it doesn’t do

Engineers trust a tool more when you tell them where it stops.

Nothing below the API: rules sit on function boundaries. A race between two requests is handled by a database transaction, not by checking arguments. The tool even refuses to mark such a rule as fully proven.

Dynamic Python: the scan follows getattr, aliases and decorators, but not arbitrary eval or C extensions. For those, there’s a run-time door check in tests. That limit isn’t a bug in this tool: Rice’s theorem (1953) proves no analysis can decide every behaviour of every program.

Flat code: on FastAPI, init finds the Pydantic limits but no doors, because a framework has no service layer. Path rules need layers to protect.

And the evaluation is ours. Try it on your own code; that’s the real test.

## 28. Start in three steps, no code changes

The takeaway: day one costs almost nothing.

Step 1: install it like a linter. It’s a dev dependency; your production code doesn’t import it. Today you install from the repo checkout, all four packages together.

Step 2: inv init finds the rules you already have. Commit the two files.

Step 3: put inv check --base main in CI. Now loosening a rule needs a person.

Later, only where it matters: add guards to the few doors that move money or write important data, especially where an AI model’s output is used. Then prove them with --inv-ablate.

## 29. Questions?

Close with the one-line summary: rules can get stricter freely, but loosening one needs a person.

Invite them to try inv init on their own code. It changes nothing, and it’s the fastest way to see what rules their code already has.

Likely questions:
- “Does it work with JavaScript?” Today it’s Python only.
- “What if the agent deletes the hook?” CI with --base main still catches it.
- “Do I need to write YAML?” Not on day one. inv init writes it.

## 30. Live demos

THE POINT
A cheat sheet. Jump here whenever you want to run a demo.

WHICH ONE IF TIME IS SHORT
- Only one: demo 1. It proves the main claim on code nobody here wrote.
- Two: add demo 3, the back doors.
- More: demo 4, then 2, then 5.

BEFORE THE TALK
- Open a terminal at the repo root: /Users/jerrykuru/dev/invariants.
- Make the font big. People at the back need to read it.
- Run each demo once, so the first run on stage isn’t the first run today.
- Each demo takes a few seconds to run. Demo 1 is the slowest, because it copies two open-source packages first.

DURING
- DEMO_PAUSE=1 makes every demo wait for Enter between steps. You control the pace.
- Leave it off to run a demo straight through.
- Each demo stops with a red message if something unexpected happens.
