"""Example target for R-7 (guide 4.2, 4.4): propose_cause returns CheckedCause, never raw text.
The closed set lives in the schema (rung 1); R-7's bad compares the reply with data code holds (D4).
The guide imports these from triage.models; one flat module here."""
import dataclasses, typing
import invariants as inv

@dataclasses.dataclass(frozen=True)
class CauseReply:                      # closed schema: extra fields forbidden, no id fields
    cause: typing.Literal["TIMEOUT", "OUT_OF_MEMORY", "BAD_INPUT", "CANNOT_COMPLY"]
    message: str

@dataclasses.dataclass(frozen=True)
class JobFacts:                        # data code holds (scheduler and kernel), never from the model
    job_id: str
    oom_killed: bool
    elapsed_s: int
    timeout_s: int

JOBS = {"J-oom": JobFacts("J-oom", True, 40, 300), "J-slow": JobFacts("J-slow", False, 300, 300)}

@inv.provider("job")
def load_job(job_id: str) -> JobFacts:
    return JOBS[job_id]

class CheckedCause(inv.Checked):       # refined type: only inv.model_call constructs it (honestly)
    __slots__ = ()

class ScriptedClient:
    """Stands in for client.messages.create(...) in provider structured-output mode."""
    def __init__(self): self.replies, self.calls = [], []
    def script(self, *replies): self.replies, self.calls = list(replies), []
    def create(self, prompt, feedback=None):
        self.calls.append({"prompt": prompt, "feedback": feedback})
        return self.replies.pop(0)

client = ScriptedClient()

@inv.model_call("R-7", schema=CauseReply)
def propose_cause(log_excerpt: str, job_id: str, inv_feedback: str | None = None) -> CheckedCause:
    return client.create(prompt=log_excerpt, feedback=inv_feedback)

@inv.edge("R-7")
def remediate(cause: CheckedCause, job_id: str) -> str:
    """Accepts only the checked type (convenience); the edge re-checks verdict and bad (D9)."""
    return f"remediate {job_id}: {cause.cause}"
