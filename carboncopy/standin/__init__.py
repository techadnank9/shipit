"""Stand-in AI: scripted answers so the whole product runs before Claude is connected.

Requirements and the coding agent are scripted for `sample-app` only. Browser tests use a
deterministic sentence parser ("Go to /. Click Refund order 2. Expect to see Refund issued."),
which also works for any app whose tests are written in that form."""
import re
import shutil
from pathlib import Path

FIX = Path(__file__).parent / "sample_fix"


class StandInUnavailable(RuntimeError):
    pass


def is_sample(system_map: dict) -> bool:
    return any(r["path"] == "/refunds" for r in system_map.get("routes", []))


def requirements(request: str, system_map: dict) -> dict:
    if not is_sample(system_map):
        raise StandInUnavailable("Stand-in AI only knows the sample refunds app. Connect the AI provider (CCOPY_AI=claude) for other repos.")
    return {
        "title": "Retry-safe refunds with a $500 cap",
        "summary": "Refunds must be idempotent per Idempotency-Key (a retry or double-click never creates a second refund), "
                   "a single refund is capped at $500, total refunds can't exceed the order, and the receipts bucket must not be public.",
        "risk": "medium",
        "acceptance_criteria": [
            "POST /refunds requires an Idempotency-Key header (400 without it).",
            "Repeating a request with the same Idempotency-Key returns the original refund (200) and creates no new row, even when sent concurrently.",
            "A refund above $500 (50,000 cents) is rejected with 422 and a message mentioning $500.",
            "Total refunds for an order never exceed the order amount.",
            "The refund-receipts S3 bucket blocks all public access and is encrypted.",
        ],
        "api_tests": [
            "Same Idempotency-Key twice → one refund, same id, second response 200",
            "60,000 cents → 422 mentioning $500",
            "Missing Idempotency-Key → 400",
            "Refund cannot push an order past its total",
        ],
        "browser_tests": [
            {"name": "Double-click refund creates one refund",
             "instructions": "Go to /. Type 25 into Refund amount for order 2. Double-click Refund order 2. Expect to see Refund issued."},
            {"name": "Refund over $500 is blocked",
             "instructions": "Go to /. Type 600 into Refund amount for order 2. Click Refund order 2. Expect to see Refund failed."},
        ],
        "db_invariants": [
            {"description": "Exactly one refund row for order 2", "sql": "select count(*) from refunds where order_id = 2", "expect": "1"},
            {"description": "Order 2 refunded exactly $25.00", "sql": "select coalesce(sum(amount_cents), 0) from refunds where order_id = 2", "expect": "2500"},
        ],
        "infra_changes": ["Remove the public-read ACL on refund-receipts; add a public access block and KMS encryption."],
    }


_RULES = [
    (re.compile(r"^(?:go to|open|visit)\s+(\S+)$", re.I), lambda m: ("ACT", "goto", m[1], "")),
    (re.compile(r"^(?:type|enter)\s+(.+?)\s+into\s+(.+)$", re.I), lambda m: ("ACT", "fill", m[2], m[1])),
    (re.compile(r"^double[- ]click\s+(.+)$", re.I), lambda m: ("ACT", "double_click", m[1], "")),
    (re.compile(r"^(?:click|press|tap)\s+(.+)$", re.I), lambda m: ("ACT", "click", m[1], "")),
    (re.compile(r"^expect not to see\s+(.+)$", re.I), lambda m: ("ASSERT", "expect_no_text", "", m[1])),
    (re.compile(r"^(?:expect to see|expect|verify|see)\s+(.+)$", re.I), lambda m: ("ASSERT", "expect_text", "", m[1])),
    (re.compile(r"^wait\s+(\d+(?:\.\d+)?)", re.I), lambda m: ("ACT", "wait", "", m[1])),
]


def browser_plan(instructions: str) -> list[dict]:
    steps = []
    for sentence in re.split(r"\.\s+(?=[A-Z])|\.$|\n|;|,?\s+then\s+", instructions.strip()):
        s = sentence.strip().strip(".").strip()
        if not s:
            continue
        for rx, build in _RULES:
            if m := rx.match(s):
                kind, action, target, value = build(m)
                steps.append({"kind": kind, "action": action, "target": target.strip("\"'"), "value": value.strip("\"'"), "why": s})
                break
        else:
            raise StandInUnavailable(f"Stand-in AI can't read the step '{s}'. Use: Go to / · Type X into Label · Click Button · "
                                     "Double-click Button · Expect to see Text · Expect not to see Text · Wait N. Or connect the AI provider.")
    if not steps or steps[0]["action"] != "goto":
        steps.insert(0, {"kind": "ACT", "action": "goto", "target": "/", "value": "", "why": "start at the home page"})
    return steps


def agent_run(workspace: Path, system_map: dict, run_checks, on_event=None) -> dict:
    """Two rounds, like a real agent: fix the code first, then the Terraform the policy gate flags."""
    if not is_sample(system_map):
        raise StandInUnavailable("Stand-in coding agent only knows the sample refunds app. Connect the AI provider.")
    rounds = []

    def put(rel: str):
        (workspace / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FIX / rel, workspace / rel)
        if on_event:
            on_event("tool", {"tool": "Edit" if (workspace / rel).exists() else "Write", "target": rel})

    for rel in ("app/main.py", "db/init.sql"):
        if on_event:
            on_event("tool", {"tool": "Read", "target": rel})
        put(rel)
    put("tests/test_refund_safety.py")
    rounds.append(run_checks())
    if on_event:
        on_event("tool", {"tool": "Read", "target": "terraform/main.tf"})
    put("terraform/main.tf")
    rounds.append(run_checks())
    return {
        "summary": "Made POST /refunds idempotent: it now requires an Idempotency-Key, returns the original refund on a retry, "
                   "and a unique index on refunds.idempotency_key stops concurrent double-clicks from creating a second row.\n"
                   "Added a $500 cap per refund and a check that refunds never exceed the order total.\n"
                   "Locked down the receipts bucket: removed the public-read ACL, added a public access block and KMS encryption.\n"
                   "Added 4 API tests in tests/test_refund_safety.py.",
        "cost_usd": 0.0,
        "rounds": rounds,
        "edited_files": ["app/main.py", "db/init.sql", "terraform/main.tf", "tests/test_refund_safety.py"],
    }
