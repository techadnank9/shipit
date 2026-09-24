// Mock backend for NEXT_PUBLIC_MOCK=1. Realistic sample data for the "Refunds desk"
// sample app, kept in sessionStorage so state survives page loads within a tab.
// Runs advance through the pipeline by elapsed time so the live run page can be exercised.

import type {
  AuditResult,
  CheckResult,
  DbCheck,
  Health,
  Project,
  Requirements,
  Run,
  RunState,
  RunStatus,
  SavedTest,
  Schedule,
  Step,
  SystemMap,
  TestSuiteResult,
  Trigger,
} from "./types";

// ---------- tiny generated screenshots (SVG, base64) ----------

function b64(s: string): string {
  // UTF-8 safe: SVG images must be valid UTF-8.
  const bytes = new TextEncoder().encode(s);
  let bin = "";
  bytes.forEach((b) => (bin += String.fromCharCode(b)));
  return btoa(bin);
}

function esc(s: string) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/** A 440x280 sketch of the sample app's orders page. */
function shot(opts: { msg?: string; tone?: "ok" | "err" | "none"; focus?: number; amount?: string; refunds?: number }): string {
  const { msg = "", tone = "none", focus = -1, amount = "", refunds = 0 } = opts;
  const rows = [
    ["#1", "Ada Lovelace", "$42.00"],
    ["#2", "Grace Hopper", "$120.00"],
    ["#3", "Alan Turing", "$980.00"],
  ];
  const rowSvg = rows
    .map((r, i) => {
      const y = 92 + i * 34;
      const hl = i === focus ? `<rect x="16" y="${y - 20}" width="408" height="30" fill="#E8EEFC"/>` : "";
      return `${hl}<text x="28" y="${y}" font-size="12" fill="#15212B">${r[0]}</text>
<text x="70" y="${y}" font-size="12" fill="#15212B">${r[1]}</text>
<text x="210" y="${y}" font-size="12" fill="#15212B">${r[2]}</text>
<rect x="268" y="${y - 15}" width="70" height="20" rx="3" fill="#fff" stroke="#9AA7B2"/>
<text x="274" y="${y}" font-size="11" fill="#5A6874">${i === focus ? esc(amount) : ""}</text>
<rect x="348" y="${y - 15}" width="64" height="20" rx="3" fill="#2753C9"/>
<text x="362" y="${y}" font-size="11" fill="#fff">Refund</text>`;
    })
    .join("");
  const msgSvg =
    tone === "none"
      ? ""
      : `<rect x="16" y="206" width="408" height="30" rx="3" fill="${tone === "ok" ? "#DDF1E5" : "#F8E1E1"}"/>
<text x="28" y="225" font-size="12" fill="${tone === "ok" ? "#2E7D55" : "#B23A3A"}">${esc(msg)}</text>`;
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="440" height="280" viewBox="0 0 440 280" font-family="Helvetica, Arial, sans-serif">
<rect width="440" height="280" fill="#FFFFFF"/>
<rect width="440" height="36" fill="#15212B"/>
<text x="16" y="23" font-size="13" fill="#fff" font-weight="bold">Refunds desk</text>
<text x="330" y="23" font-size="11" fill="#C9D3DC">refunds today: ${refunds}</text>
<text x="28" y="62" font-size="10" fill="#5A6874">ORDER</text><text x="70" y="62" font-size="10" fill="#5A6874">CUSTOMER</text>
<text x="210" y="62" font-size="10" fill="#5A6874">TOTAL</text><text x="268" y="62" font-size="10" fill="#5A6874">AMOUNT</text>
<line x1="16" x2="424" y1="70" y2="70" stroke="#D5DCE2"/>
${rowSvg}
${msgSvg}
<text x="16" y="264" font-size="10" fill="#9AA7B2">copy · 127.0.0.1:18000</text>
</svg>`;
  return b64(svg);
}

function step(
  kind: "ACT" | "ASSERT",
  action: string,
  target: string,
  value: string,
  why: string,
  ms: number,
  screen: string,
  error: string | null = null,
): Step {
  return { kind, action, target, value, why, ok: error === null, error, ms, screenshot: screen };
}

// ---------- scripted results ----------

const REQS: Requirements = {
  title: "Make refunds safe to retry and cap at $500",
  summary:
    "A refund can be recorded twice when a support agent double-clicks Refund or the browser retries the request. Make POST /refunds idempotent per order and reject any single refund above $500.00 with a clear message. The receipts bucket must stop being public.",
  risk: "high",
  acceptance_criteria: [
    "Submitting the same refund twice for one order creates exactly one row in refunds.",
    "A refund above $500.00 is rejected with HTTP 422 and the page shows \"Refunds over $500 need a manager\".",
    "A refund of exactly $500.00 is accepted.",
    "Every accepted refund still writes a receipt object to the receipts S3 bucket.",
    "aws_s3_bucket_acl.receipts is private (fixes policy violation: public-read ACL).",
  ],
  api_tests: [
    "test_refund_is_idempotent_per_order",
    "test_refund_over_500_is_rejected",
    "test_refund_of_exactly_500_is_accepted",
  ],
  browser_tests: [
    {
      name: "Double-click refund",
      instructions:
        "Open the orders page. Type 20 in the Amount field for order #2 and double-click Refund. Expect \"Refund issued\" to appear and no error.",
    },
    {
      name: "Refund over $500 blocked",
      instructions:
        "Open the orders page. Type 750 in the Amount field for order #3 and click Refund. Expect the message \"Refunds over $500 need a manager\".",
    },
  ],
  db_invariants: [
    {
      description: "Exactly one refund row for order 2",
      sql: "SELECT count(*) FROM refunds WHERE order_id = 2",
      expect: "1",
    },
    {
      description: "No refund above $500",
      sql: "SELECT count(*) FROM refunds WHERE amount_cents > 50000",
      expect: "0",
    },
  ],
  infra_changes: ["Set aws_s3_bucket_acl.receipts acl from \"public-read\" to \"private\" (terraform/main.tf)"],
};

function beforeResult(): TestSuiteResult {
  const home = shot({});
  return {
    passed: false,
    tests: [
      {
        name: "Double-click refund",
        instructions: REQS.browser_tests[0].instructions,
        passed: true,
        video: "before/video-0/page.webm",
        steps: [
          step("ACT", "goto", "/", "", "Start on the orders page", 412, home),
          step("ACT", "fill", "Amount for order #2", "20", "Enter the refund amount", 88, shot({ focus: 1, amount: "20" })),
          step("ACT", "double_click", "Refund", "", "Simulate an impatient double-click", 640, shot({ focus: 1, amount: "20", refunds: 2 })),
          step("ASSERT", "expect_text", "", "Refund issued", "Screen confirms the refund", 51, shot({ focus: 1, amount: "20", refunds: 2, tone: "ok", msg: "Refund issued for order #2" })),
        ],
      },
      {
        name: "Refund over $500 blocked",
        instructions: REQS.browser_tests[1].instructions,
        passed: false,
        video: "before/video-1/page.webm",
        steps: [
          step("ACT", "goto", "/", "", "Start on the orders page", 398, home),
          step("ACT", "fill", "Amount for order #3", "750", "Enter an amount above the cap", 92, shot({ focus: 2, amount: "750" })),
          step("ACT", "click", "Refund", "", "Submit the refund", 310, shot({ focus: 2, amount: "750", refunds: 1, tone: "ok", msg: "Refund issued for order #3" })),
          step(
            "ASSERT",
            "expect_text",
            "",
            "Refunds over $500 need a manager",
            "The cap message must be shown",
            5004,
            shot({ focus: 2, amount: "750", refunds: 1, tone: "ok", msg: "Refund issued for order #3" }),
            "Timeout 5000ms exceeded: text \"Refunds over $500 need a manager\" not found on page",
          ),
        ],
      },
    ],
    db_checks: [
      { ...REQS.db_invariants[0], got: "2", passed: false },
      { ...REQS.db_invariants[1], got: "1", passed: false },
    ],
  };
}

function afterResult(): TestSuiteResult {
  const home = shot({});
  return {
    passed: true,
    tests: [
      {
        name: "Double-click refund",
        instructions: REQS.browser_tests[0].instructions,
        passed: true,
        video: "after/video-0/page.webm",
        steps: [
          step("ACT", "goto", "/", "", "Start on the orders page", 377, home),
          step("ACT", "fill", "Amount for order #2", "20", "Enter the refund amount", 81, shot({ focus: 1, amount: "20" })),
          step("ACT", "double_click", "Refund", "", "Simulate an impatient double-click", 602, shot({ focus: 1, amount: "20", refunds: 1 })),
          step("ASSERT", "expect_text", "", "Refund issued", "Screen confirms the refund", 44, shot({ focus: 1, amount: "20", refunds: 1, tone: "ok", msg: "Refund issued for order #2" })),
        ],
      },
      {
        name: "Refund over $500 blocked",
        instructions: REQS.browser_tests[1].instructions,
        passed: true,
        video: "after/video-1/page.webm",
        steps: [
          step("ACT", "goto", "/", "", "Start on the orders page", 365, home),
          step("ACT", "fill", "Amount for order #3", "750", "Enter an amount above the cap", 85, shot({ focus: 2, amount: "750" })),
          step("ACT", "click", "Refund", "", "Submit the refund", 288, shot({ focus: 2, amount: "750", tone: "err", msg: "Refunds over $500 need a manager" })),
          step("ASSERT", "expect_text", "", "Refunds over $500 need a manager", "The cap message must be shown", 39, shot({ focus: 2, amount: "750", tone: "err", msg: "Refunds over $500 need a manager" })),
        ],
      },
    ],
    db_checks: [
      { ...REQS.db_invariants[0], got: "1", passed: true },
      { ...REQS.db_invariants[1], got: "0", passed: true },
    ],
  };
}

const ROUNDS = [
  {
    round: 1,
    passed: false,
    checks: [
      { name: "Tests on the copy", passed: false, summary: "2 failed, 9 passed in 2.87s", details: ["FAILED tests/test_refunds.py::test_refund_is_idempotent_per_order - assert 2 == 1", "FAILED tests/test_refunds.py::test_refund_over_500_is_rejected - assert 201 == 422"] },
      { name: "Infrastructure policy (OPA)", passed: false, summary: "1 violation(s)", details: ["aws_s3_bucket_acl.receipts: S3 bucket ACL must not be public-read"] },
      { name: "IaC scan (Checkov, advisory)", passed: true, summary: "3 advisory finding(s)", details: [] },
      { name: "Secret scan", passed: true, summary: "clean", details: [] },
    ],
  },
  {
    round: 2,
    passed: true,
    checks: [
      { name: "Tests on the copy", passed: true, summary: "12 passed in 2.52s", details: [] },
      { name: "Infrastructure policy (OPA)", passed: true, summary: "2 resources, 0 violations", details: [] },
      { name: "IaC scan (Checkov, advisory)", passed: true, summary: "3 advisory finding(s)", details: [] },
      { name: "Secret scan", passed: true, summary: "clean", details: [] },
    ],
  },
];

const FINAL: CheckResult[] = [
  { name: "Tests on the copy", passed: true, summary: "12 passed in 2.41s", details: ["tests/test_orders.py ....", "tests/test_refunds.py ........", "12 passed in 2.41s"] },
  { name: "Infrastructure policy (OPA)", passed: true, summary: "2 resources, 0 violations", details: [] },
  {
    name: "IaC scan (Checkov, advisory)",
    passed: true,
    summary: "3 advisory finding(s)",
    details: [
      "CKV_AWS_18 Ensure the S3 bucket has access logging enabled (aws_s3_bucket.receipts)",
      "CKV_AWS_21 Ensure all data stored in the S3 bucket have versioning enabled (aws_s3_bucket.receipts)",
      "CKV_AWS_145 Ensure that S3 buckets are encrypted with KMS by default (aws_s3_bucket.receipts)",
    ],
  },
  { name: "Secret scan", passed: true, summary: "clean", details: [] },
];

const AGENT = {
  summary:
    "Added a unique index on refunds(order_id, idempotency_key) and made POST /refunds return the existing refund when the same key is replayed; the page now sends a per-click key and disables Refund while the request is in flight. Added a $500.00 cap that returns 422 with \"Refunds over $500 need a manager\". Set the receipts bucket ACL to private.",
  cost_usd: 1.87,
  edited_files: ["app/main.py", "app/templates/index.html", "db/init.sql", "terraform/main.tf", "tests/test_refunds.py"],
};

const PATCH = `diff --git a/app/main.py b/app/main.py
@@ -51,6 +51,14 @@ def create_refund(body: RefundIn):
+    if body.amount_cents > 50_000:
+        raise HTTPException(422, "Refunds over $500 need a manager")
+    existing = cur.execute(
+        "SELECT id FROM refunds WHERE order_id = %s AND idempotency_key = %s",
+        (body.order_id, body.idempotency_key),
+    ).fetchone()
+    if existing:
+        return {"id": existing[0], "replayed": True}
diff --git a/terraform/main.tf b/terraform/main.tf
-  acl    = "public-read"
+  acl    = "private"
`;

// ---------- store ----------

interface Sim {
  mode: "prepare" | "execute" | "test";
  start: number; // ms epoch
  failTest?: boolean;
}

interface DB {
  projects: Project[];
  tests: SavedTest[];
  checks: DbCheck[];
  schedules: Record<string, Schedule>;
  runs: (Run & { sim?: Sim })[];
  seq: number;
}

const KEY = "ccopy.mock.v2";
let db: DB | null = null;

function nowS() {
  return Date.now() / 1000;
}

function seed(): DB {
  const t = nowS();
  const H = 3600;
  const D = 86400;
  const pid = "p_refunds";
  const repo = "/Users/lead/code/refunds-desk";

  const baseState = (id: string, kind: "change" | "test", status: RunStatus, request: string, created: number): RunState => ({
    id,
    kind,
    status,
    repo,
    request,
    created_at: created,
    updated_at: created,
    requirements: null,
    fidelity: null,
    before: null,
    after: null,
    final_checks: null,
    rounds: [],
    agent: null,
    error: null,
    report: null,
    patch: null,
  });

  const r1id = "20260924-1530-ab12";
  const r1: Run = {
    id: r1id,
    project_id: pid,
    kind: "change",
    status: "awaiting_requirements",
    trigger: "manual",
    title: "Make refunds safe to retry and cap at $500",
    created_at: t - 14 * 60,
    updated_at: t - 11 * 60,
    passed: null,
    state: {
      ...baseState(r1id, "change", "awaiting_requirements", "Make refunds safe to retry and cap at $500", t - 14 * 60),
      updated_at: t - 11 * 60,
      requirements: REQS,
      fidelity: { score: 94, missing_env: ["API_URL"], unemulated_aws: [] },
    },
  };

  const r2id = "20260923-1012-9f3c";
  const r2: Run = {
    id: r2id,
    project_id: pid,
    kind: "change",
    status: "shipped",
    trigger: "pr",
    title: "Block duplicate refunds and cap single refunds at $500",
    created_at: t - D - 5 * H,
    updated_at: t - D - 4 * H,
    passed: true,
    state: {
      ...baseState(r2id, "change", "shipped", "Block duplicate refunds and cap single refunds at $500 (PR #41)", t - D - 5 * H),
      updated_at: t - D - 4 * H,
      requirements: { ...REQS, title: "Block duplicate refunds and cap single refunds at $500" },
      fidelity: { score: 100, missing_env: [], unemulated_aws: [] },
      before: beforeResult(),
      after: afterResult(),
      rounds: ROUNDS,
      final_checks: FINAL,
      agent: AGENT,
      report: "report.html",
      patch: "change.patch",
    },
  };

  const r3id = "20260924-0800-5d21";
  const failedAfter: TestSuiteResult = {
    passed: false,
    tests: [
      {
        name: "Orders page loads",
        instructions: "Open the orders page. Expect to see order #1, #2 and #3 with a Refund button each.",
        passed: true,
        steps: [
          step("ACT", "goto", "/", "", "Start on the orders page", 402, shot({})),
          step("ASSERT", "expect_text", "", "Grace Hopper", "Seeded orders are listed", 33, shot({})),
        ],
      },
      {
        name: "Issue a $20 refund",
        instructions: "Type 20 in the Amount field for order #1, click Refund, expect \"Refund issued\".",
        passed: true,
        steps: [
          step("ACT", "goto", "/", "", "Start on the orders page", 388, shot({})),
          step("ACT", "fill", "Amount for order #1", "20", "Enter the refund amount", 90, shot({ focus: 0, amount: "20" })),
          step("ACT", "click", "Refund", "", "Submit the refund", 301, shot({ focus: 0, amount: "20", refunds: 1, tone: "ok", msg: "Refund issued for order #1" })),
          step("ASSERT", "expect_text", "", "Refund issued", "Screen confirms the refund", 40, shot({ focus: 0, amount: "20", refunds: 1, tone: "ok", msg: "Refund issued for order #1" })),
        ],
      },
      {
        name: "Receipt link opens",
        instructions: "After issuing a refund for order #1, click Download receipt and expect a receipt for order #1.",
        passed: false,
        steps: [
          step("ACT", "goto", "/", "", "Start on the orders page", 391, shot({})),
          step("ACT", "fill", "Amount for order #1", "20", "Enter the refund amount", 87, shot({ focus: 0, amount: "20" })),
          step("ACT", "click", "Refund", "", "Submit the refund", 296, shot({ focus: 0, amount: "20", refunds: 1, tone: "ok", msg: "Refund issued for order #1" })),
          step(
            "ACT",
            "click",
            "Download receipt",
            "",
            "Open the receipt stored in S3",
            5003,
            shot({ focus: 0, amount: "20", refunds: 1, tone: "ok", msg: "Refund issued for order #1" }),
            "Timeout 5000ms exceeded: no visible element with text \"Download receipt\"",
          ),
        ],
      },
    ],
    db_checks: [
      { description: "Every refund has a receipt key", sql: "SELECT count(*) FROM refunds WHERE receipt_key IS NULL", expect: "0", got: "1", passed: false },
      { description: "Refunds never exceed the order total", sql: "SELECT count(*) FROM refunds r JOIN orders o ON o.id = r.order_id WHERE r.amount_cents > o.total_cents", expect: "0", got: "0", passed: true },
    ],
  };
  const r3: Run = {
    id: r3id,
    project_id: pid,
    kind: "test",
    status: "failed",
    trigger: "schedule",
    title: "Scheduled test run · 3 tests, 2 DB checks",
    created_at: t - 7 * H,
    updated_at: t - 7 * H + 94,
    passed: false,
    state: {
      ...baseState(r3id, "test", "failed", "", t - 7 * H),
      updated_at: t - 7 * H + 94,
      fidelity: { score: 100, missing_env: [], unemulated_aws: [] },
      after: failedAfter,
      report: "report.html",
    },
  };

  const r4id = "20260922-1644-02be";
  const r4: Run = {
    id: r4id,
    project_id: pid,
    kind: "change",
    status: "rejected",
    trigger: "mcp",
    title: "Let support agents refund any amount without approval",
    created_at: t - 2 * D - 2 * H,
    updated_at: t - 2 * D - H,
    passed: false,
    state: {
      ...baseState(r4id, "change", "rejected", "Let support agents refund any amount without approval", t - 2 * D - 2 * H),
      requirements: {
        ...REQS,
        title: "Let support agents refund any amount without approval",
        risk: "high",
        acceptance_criteria: ["Any refund amount up to the order total is accepted without a manager."],
      },
      fidelity: { score: 100, missing_env: [], unemulated_aws: [] },
      error: "Rejected by maria.chen: conflicts with the finance policy on refunds over $500.",
    },
  };

  const r5id = "20260922-0800-7aa0";
  const r5: Run = {
    id: r5id,
    project_id: pid,
    kind: "test",
    status: "passed",
    trigger: "schedule",
    title: "Scheduled test run · 3 tests, 2 DB checks",
    created_at: t - 2 * D - 9 * H,
    updated_at: t - 2 * D - 9 * H + 88,
    passed: true,
    state: {
      ...baseState(r5id, "test", "passed", "", t - 2 * D - 9 * H),
      after: {
        passed: true,
        tests: failedAfter.tests.map((x) => ({ ...x, passed: true, steps: x.steps.map((s) => ({ ...s, ok: true, error: null })) })),
        db_checks: failedAfter.db_checks.map((d) => ({ ...d, got: d.expect, passed: true })),
      },
      report: "report.html",
    },
  };

  return {
    seq: 1,
    projects: [
      { id: pid, name: "Refunds desk", repo_path: repo, git_url: null, created_at: t - 9 * D, last_run: null },
      { id: "p_billing", name: "Billing portal", repo_path: null, git_url: "https://github.com/acme-pay/billing-portal.git", created_at: t - 3 * D, last_run: null },
    ],
    tests: [
      { id: "t_orders", project_id: pid, name: "Orders page loads", instructions: failedAfter.tests[0].instructions, created_at: t - 9 * D },
      { id: "t_refund20", project_id: pid, name: "Issue a $20 refund", instructions: failedAfter.tests[1].instructions, created_at: t - 9 * D },
      { id: "t_receipt", project_id: pid, name: "Receipt link opens", instructions: failedAfter.tests[2].instructions, created_at: t - 5 * D },
    ],
    checks: [
      { id: "c_receipt", project_id: pid, description: failedAfter.db_checks[0].description, sql: failedAfter.db_checks[0].sql, expect: "0", created_at: t - 9 * D },
      { id: "c_total", project_id: pid, description: failedAfter.db_checks[1].description, sql: failedAfter.db_checks[1].sql, expect: "0", created_at: t - 9 * D },
    ],
    schedules: {
      [pid]: { project_id: pid, cron: "0 8 * * *", next_run_at: nextDaily8() },
      p_billing: { project_id: "p_billing", cron: null, next_run_at: null },
    },
    runs: [r1, r3, r2, r4, r5],
  };
}

function nextDaily8(): number {
  const d = new Date();
  d.setHours(8, 0, 0, 0);
  if (d.getTime() <= Date.now()) d.setDate(d.getDate() + 1);
  return d.getTime() / 1000;
}

function load(): DB {
  if (db) return db;
  try {
    const raw = typeof window !== "undefined" ? window.sessionStorage.getItem(KEY) : null;
    if (raw) db = JSON.parse(raw) as DB;
  } catch {
    /* storage blocked */
  }
  if (!db) db = seed();
  return db;
}

function save() {
  try {
    if (typeof window !== "undefined" && db) window.sessionStorage.setItem(KEY, JSON.stringify(db));
  } catch {
    /* storage full or blocked; state stays in memory */
  }
}

function delay<T>(v: T, ms = 180): Promise<T> {
  return new Promise((r) => setTimeout(() => r(structuredClone(v)), ms));
}

function notFound(what: string): Promise<never> {
  return new Promise((_, rej) => setTimeout(() => rej(new Error(`${what} not found`)), 120));
}

// ---------- time-based simulation ----------

function tick(run: Run & { sim?: Sim }) {
  const sim = run.sim;
  if (!sim) return;
  const s = (Date.now() - sim.start) / 1000;
  const st = run.state as RunState;
  const set = (status: RunStatus) => {
    if (run.status !== status) {
      run.status = status;
      st.status = status;
      run.updated_at = nowS();
      st.updated_at = run.updated_at;
    }
  };
  if (sim.mode === "prepare") {
    if (s < 2) set("queued");
    else if (s < 5) set("mapping");
    else if (s < 9) set("copying");
    else if (s < 13) {
      st.fidelity = { score: 94, missing_env: ["API_URL"], unemulated_aws: [] };
      set("writing_requirements");
    } else {
      st.requirements = { ...REQS, title: run.title };
      set("awaiting_requirements");
      delete run.sim;
    }
  } else if (sim.mode === "execute") {
    if (s < 5) set("baseline_testing");
    else if (s < 14) {
      st.before = beforeResult();
      st.rounds = s < 9 ? [] : s < 12 ? ROUNDS.slice(0, 1) : ROUNDS;
      set("agent_working");
    } else if (s < 19) {
      st.rounds = ROUNDS;
      st.agent = AGENT;
      set("final_gate");
    } else {
      st.after = afterResult();
      st.final_checks = FINAL;
      st.patch = "change.patch";
      st.report = "report.html";
      run.passed = true;
      set("awaiting_ship");
      delete run.sim;
    }
  } else if (sim.mode === "test") {
    if (s < 2) set("queued");
    else if (s < 5) set("copying");
    else if (s < 10) set("testing");
    else {
      const res = structuredClone(load().runs.find((r) => r.status === "passed" && r.kind === "test")?.state?.after ?? null);
      st.after = res;
      st.fidelity = { score: 100, missing_env: [], unemulated_aws: [] };
      st.report = "report.html";
      run.passed = true;
      set("passed");
      delete run.sim;
    }
  }
}

function tickAll() {
  const d = load();
  d.runs.forEach(tick);
  save();
  return d;
}

function withLastRun(p: Project, d: DB): Project {
  const last = d.runs.filter((r) => r.project_id === p.id).sort((a, b) => b.created_at - a.created_at)[0];
  if (!last) return { ...p, last_run: null };
  const { state: _s, sim: _m, ...rest } = last;
  void _s;
  void _m;
  return { ...p, last_run: rest };
}

function newRunId(d: DB) {
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  d.seq += 1;
  return `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}-${Math.random().toString(16).slice(2, 6)}`;
}

function strip(r: Run & { sim?: Sim }, withState: boolean): Run {
  const { sim: _m, state, ...rest } = r;
  void _m;
  return withState ? { ...rest, state } : rest;
}

// ---------- API surface ----------

export function health(): Promise<Health> {
  return delay({ ok: true, ai: "standin" }, 60);
}

export function listProjects(): Promise<Project[]> {
  const d = tickAll();
  return delay(d.projects.map((p) => withLastRun(p, d)));
}

export function createProject(b: { name: string; repo_path?: string; git_url?: string }): Promise<Project> {
  const d = load();
  const p: Project = {
    id: "p_" + Math.random().toString(36).slice(2, 8),
    name: b.name,
    repo_path: b.repo_path ?? null,
    git_url: b.git_url ?? null,
    created_at: nowS(),
    last_run: null,
  };
  d.projects.unshift(p);
  d.schedules[p.id] = { project_id: p.id, cron: null, next_run_at: null };
  save();
  return delay(p);
}

export function getProject(pid: string): Promise<Project> {
  const d = tickAll();
  const p = d.projects.find((x) => x.id === pid);
  return p ? delay(withLastRun(p, d)) : notFound("Project");
}

export function getMap(pid: string): Promise<SystemMap> {
  if (pid !== "p_refunds") {
    return delay({ repo: pid, routes: [], tables: [], env_vars: [], aws_services: [], infrastructure: [], schema_files: [], has_dockerfile: false });
  }
  return delay({
    repo: "refunds-desk",
    routes: [
      { method: "GET", path: "/", handler: "page", file: "app/main.py" },
      { method: "GET", path: "/health", handler: "health", file: "app/main.py" },
      { method: "GET", path: "/orders", handler: "orders", file: "app/main.py" },
      { method: "POST", path: "/refunds", handler: "create_refund", file: "app/main.py" },
    ],
    tables: ["orders", "refunds"],
    env_vars: ["API_URL", "DATABASE_URL", "RECEIPTS_BUCKET", "S3_ENDPOINT"],
    aws_services: ["s3"],
    infrastructure: [
      { type: "aws_s3_bucket", name: "receipts", file: "terraform/main.tf" },
      { type: "aws_s3_bucket_acl", name: "receipts", file: "terraform/main.tf" },
    ],
    schema_files: ["db/init.sql"],
    has_dockerfile: true,
  });
}

export function listTests(pid: string): Promise<SavedTest[]> {
  return delay(load().tests.filter((t) => t.project_id === pid));
}

export function addTest(pid: string, b: { name: string; instructions: string }): Promise<SavedTest> {
  const d = load();
  const t: SavedTest = { id: "t_" + Math.random().toString(36).slice(2, 8), project_id: pid, ...b, created_at: nowS() };
  d.tests.push(t);
  save();
  return delay(t);
}

export function deleteTest(tid: string): Promise<{ ok: boolean }> {
  const d = load();
  d.tests = d.tests.filter((t) => t.id !== tid);
  save();
  return delay({ ok: true });
}

export function listChecks(pid: string): Promise<DbCheck[]> {
  return delay(load().checks.filter((c) => c.project_id === pid));
}

export function addCheck(pid: string, b: { description: string; sql: string; expect: string }): Promise<DbCheck> {
  const d = load();
  const c: DbCheck = { id: "c_" + Math.random().toString(36).slice(2, 8), project_id: pid, ...b, created_at: nowS() };
  d.checks.push(c);
  save();
  return delay(c);
}

function newRun(pid: string, kind: "change" | "test", trigger: string, title: string, request: string, mode: Sim["mode"]): Run {
  const d = load();
  const id = newRunId(d);
  const t = nowS();
  const run: Run & { sim?: Sim } = {
    id,
    project_id: pid,
    kind,
    status: "queued",
    trigger,
    title,
    created_at: t,
    updated_at: t,
    passed: null,
    sim: { mode, start: Date.now() },
    state: {
      id,
      kind,
      status: "queued",
      repo: d.projects.find((p) => p.id === pid)?.repo_path ?? "",
      request,
      created_at: t,
      updated_at: t,
      requirements: null,
      fidelity: null,
      before: null,
      after: null,
      final_checks: null,
      rounds: [],
      agent: null,
      error: null,
      report: null,
      patch: null,
    },
  };
  d.runs.unshift(run);
  save();
  return strip(run, false);
}

export function requestChange(pid: string, request: string): Promise<Run> {
  return delay(newRun(pid, "change", "manual", request, request, "prepare"));
}

export function startTestRun(pid: string, trigger: Trigger): Promise<Run> {
  const d = load();
  const nt = d.tests.filter((t) => t.project_id === pid).length;
  const nc = d.checks.filter((c) => c.project_id === pid).length;
  return delay(newRun(pid, "test", trigger, `Test run · ${nt} tests, ${nc} DB checks`, "", "test"));
}

export function listRuns(q: { project_id?: string; kind?: string; status?: string; limit?: number }): Promise<Run[]> {
  const d = tickAll();
  let rs = d.runs.slice().sort((a, b) => b.created_at - a.created_at);
  if (q.project_id) rs = rs.filter((r) => r.project_id === q.project_id);
  if (q.kind) rs = rs.filter((r) => r.kind === q.kind);
  if (q.status) rs = rs.filter((r) => r.status === q.status);
  if (q.limit) rs = rs.slice(0, q.limit);
  return delay(rs.map((r) => strip(r, false)));
}

export function getRun(rid: string): Promise<Run> {
  const d = tickAll();
  const r = d.runs.find((x) => x.id === rid);
  return r ? delay(strip(r, true), 120) : notFound("Run");
}

function mutate(rid: string, fn: (r: Run & { sim?: Sim }, st: RunState) => string | null): Promise<Run> {
  const d = tickAll();
  const r = d.runs.find((x) => x.id === rid);
  if (!r) return notFound("Run");
  const err = fn(r, r.state as RunState);
  if (err) return new Promise((_, rej) => setTimeout(() => rej(new Error(err)), 150));
  r.updated_at = nowS();
  save();
  return delay(strip(r, true));
}

export function approveRequirements(rid: string, _who: string): Promise<Run> {
  return mutate(rid, (r, st) => {
    if (r.status !== "awaiting_requirements") return `Run is ${r.status}, not awaiting_requirements`;
    r.status = "baseline_testing";
    st.status = "baseline_testing";
    r.sim = { mode: "execute", start: Date.now() };
    return null;
  });
}

export function reject(rid: string, who: string, reason: string): Promise<Run> {
  return mutate(rid, (r, st) => {
    if (r.status !== "awaiting_requirements" && r.status !== "awaiting_ship") return `Run is ${r.status}; nothing to reject`;
    r.status = "rejected";
    st.status = "rejected";
    r.passed = false;
    st.error = `Rejected by ${who}${reason ? `: ${reason}` : ""}`;
    return null;
  });
}

export function approveShip(rid: string, _who: string): Promise<Run> {
  return mutate(rid, (r, st) => {
    if (r.status !== "awaiting_ship") return `Run is ${r.status}, not awaiting_ship`;
    r.status = "shipped";
    st.status = "shipped";
    r.passed = true;
    return null;
  });
}

export function audit(rid: string): Promise<AuditResult> {
  const r = load().runs.find((x) => x.id === rid);
  if (!r) return notFound("Run");
  const t = r.created_at;
  const entries = [
    { event: "run.created", who: "maria.chen", ts: t },
    { event: "system.mapped", who: "reader", ts: t + 12 },
    { event: "copy.up", who: "copier", ts: t + 41 },
    { event: "requirements.written", who: "claude", ts: t + 66 },
  ];
  if (r.status !== "awaiting_requirements" && r.kind === "change") {
    entries.push({ event: "requirements.approved", who: "maria.chen", ts: t + 400 });
  }
  return delay({ verified: true, entries });
}

export function getSchedule(pid: string): Promise<Schedule> {
  return delay(load().schedules[pid] ?? { project_id: pid, cron: null, next_run_at: null });
}

export function putSchedule(pid: string, cron: string | null): Promise<Schedule> {
  const d = load();
  const s: Schedule = { project_id: pid, cron, next_run_at: cron ? nowS() + 3600 : null };
  if (cron === "0 8 * * *") s.next_run_at = nextDaily8();
  d.schedules[pid] = s;
  save();
  return delay(s);
}

export function artifactBlobUrl(rid: string, which: "report" | "patch"): string {
  if (typeof window === "undefined") return "#";
  const r = load().runs.find((x) => x.id === rid);
  const body =
    which === "patch"
      ? PATCH
      : `<!doctype html><meta charset="utf-8"><title>Proof report ${rid}</title>
<body style="font-family:system-ui;max-width:720px;margin:40px auto;padding:0 16px;color:#15212B">
<h1>Proof report</h1><p>Run <code>${rid}</code> · ${esc(r?.title ?? "")}</p>
<p>Mock mode: the real report is generated by the Carbon Copy engine (report.html).</p></body>`;
  const blob = new Blob([body], { type: which === "patch" ? "text/plain" : "text/html" });
  return URL.createObjectURL(blob);
}
