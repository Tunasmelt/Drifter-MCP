import { useState } from "react";

const REPO = "https://github.com/Tunasmelt/Drifter-MCP";
const PYPI = "https://pypi.org/project/mcp-drifter/";
const DOCS = `${REPO}/blob/master/docs`;

const NAV = [
  ["problem", "The problem"],
  ["how", "How it works"],
  ["mine", "Mining"],
  ["mutations", "Mutations"],
  ["verdicts", "Verdicts"],
  ["evidence", "Evidence"],
  ["start", "Get started"],
  ["limits", "Limits"],
];

const OPERATORS = [
  {
    id: "parameter_rename",
    label: "parameter_rename",
    summary:
      "Renames one input parameter, snake_case to camelCase. Drifter still recognises the resulting call through inverse-mutation key resolution, so replay keeps working even though the schema changed.",
    changes: "One input parameter's name in one tool's schema.",
    never: "Tool names, other parameters, other tools.",
    before: `"get_order": {
  "properties": {
    "order_id": { "type": "string" }
  },
  "required": ["order_id"]
}`,
    after: `"get_order": {
  "properties": {
    "orderId": { "type": "string" }
  },
  "required": ["orderId"]
}`,
    note: "The real change from the release-gate run: an agent that reads the served schema adapts; one hard-wired to order_id is rejected.",
  },
  {
    id: "description_update",
    label: "description_update",
    summary:
      "Bounded synonym substitution and sentence reordering in a tool's description. The wording an agent uses to choose a tool changes; nothing else does.",
    changes: "The natural-language description of a tool.",
    never: "A tool's name or its input schema.",
    guard:
      "Generated text is rejected if it reads as an instruction: ignore, always call, you must, disregard, instead of.",
  },
  {
    id: "tool_addition",
    label: "tool_addition",
    summary:
      "Appends one generic tool from a small, fixed pool of archetypes to the served manifest. It tests whether an agent stays on task when a new, plausible tool appears.",
    changes: "The manifest gains one extra tool, always at the end.",
    never: "Any existing tool.",
  },
];

const CANDIDATE_YAML = `version: 1
server: my-server
candidates:
- id: search_get_customer_create_invoice
  status: candidate     # approve flips it
  support: 12           # seen in 12 ...
  of_trajectories: 40   # ... of 40 trajectories
  sessions: 4           # across 4 sessions
  pattern: [search, get_customer, create_invoice]
  prompt: ''            # you write this
  assert:
    calls: [search, get_customer, create_invoice]
    calls_before: [[search, get_customer], [get_customer, create_invoice]]
    never_calls: []
    no_errors: false`;

const MINING_POINTS = [
  [
    "Gaps allowed",
    "Finds get_customer then create_invoice even when some runs did something in between. Support is counted in trajectories, so a workflow that ran forty times counts forty.",
  ],
  [
    "Proposes, never decides",
    "Every prompt starts empty and approval refuses until you write one. A sequence of calls says what the agent did, not what it was asked.",
  ],
  [
    "Your file stays yours",
    "Re-running mine only appends new patterns. Approving changes one word on one line. Comments, edits and line endings are left exactly as you wrote them.",
  ],
  [
    "Reads recordings only",
    "No server contacted, no agent run, no cost. Sessions recorded by replay-serve are skipped, since they record an agent being replayed rather than what it does.",
  ],
];

const VERDICTS = [
  {
    name: "Behavior",
    tag: "experimental",
    text: "Compares how often the agent stays on the path your recordings established, before and after the mutation, using a pre-registered interval rule. Below a minimum number of valid runs per arm it reports UNKNOWN with the counts, never a confident verdict on thin data.",
  },
  {
    name: "Task",
    tag: "opt-in",
    text: "Assertions you author: which tools must be called, in what order, which must not be, and an answer_matches check on the agent's final answer. With none configured it reports UNKNOWN, and never PASS.",
  },
  {
    name: "Safety",
    tag: "always on",
    text: "Evaluated on every run and never gated by the other axes. Destructive and irreversible calls are flagged, and a call to a tool Drifter couldn't classify is reported explicitly instead of passing silently.",
  },
];

const EVIDENCE = [
  {
    who: "Unchanged agent",
    kind: "Scripted client, no mutation",
    result: "3/3 valid runs, task PASS",
    tone: "ok",
  },
  {
    who: "Unadapted agent",
    kind: "Same client, parameter_rename active",
    result: "0/3 pass — every get_order rejected with code -31003",
    tone: "bad",
  },
  {
    who: "Adapting agent",
    kind: "Real headless Claude Code session",
    result: "3/3 pass — used the renamed orderId, resolved at the inverse tier",
    tone: "ok",
  },
];

const COMMANDS = [
  ["init", "Scan existing MCP client configs and write a starter drifter.yaml."],
  ["doctor", "Config and connectivity pre-flight; classifies every tool's risk."],
  ["observe", "Transparent recording proxy between your agent and a real server."],
  ["stats", "Per-tool call frequency, error and fault rate, latency."],
  ["coverage", "Projected replay coverage from recordings alone, before you spend anything."],
  ["fixture", "Author and maintain response fixtures from the live server (read-only tools)."],
  ["tasks", "Mine recurring workflows from your recordings into task candidates, then approve the ones you want."],
  ["run", "Baseline plus one mutation operator in replay mode, with a verdict."],
  ["report", "Re-render a prior run from stored sessions. Zero new execution."],
  ["score", "Re-analyse already-recorded sessions."],
  ["replay-serve", "Serve a replayed, optionally mutated manifest for a real agent."],
];

const EXIT_CODES = [
  ["0", "Clean"],
  ["1", "Behavior regression"],
  ["2", "Task assertion failure"],
  ["3", "Safety violation"],
  ["4", "Config or connectivity error"],
  ["5", "Budget exceeded"],
];

function CopyBlock({ text, label }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      setCopied(false);
    }
  };
  return (
    <div className="copyblock">
      <pre>
        <code>{text}</code>
      </pre>
      <button type="button" onClick={copy} aria-label={label}>
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}

function Section({ id, eyebrow, title, children }) {
  return (
    <section id={id} className="section">
      <div className="wrap">
        <p className="eyebrow">{eyebrow}</p>
        <h2>{title}</h2>
        {children}
      </div>
    </section>
  );
}

function Header() {
  return (
    <header className="topbar">
      <div className="wrap topbar-inner">
        <a className="brand" href="#top" aria-label="Drifter home">
          <svg width="26" height="26" viewBox="0 0 32 32" aria-hidden="true">
            <rect width="32" height="32" rx="7" fill="var(--panel-2)" />
            <path
              d="M7 21c4-9 8-9 12-3s5 3 7-2"
              fill="none"
              stroke="var(--accent)"
              strokeWidth="3"
              strokeLinecap="round"
            />
          </svg>
          Drifter
        </a>
        <nav aria-label="Primary">
          {NAV.map(([id, label]) => (
            <a key={id} href={`#${id}`}>
              {label}
            </a>
          ))}
        </nav>
        <a className="btn btn-small" href={REPO}>
          GitHub
        </a>
      </div>
    </header>
  );
}

function Hero() {
  return (
    <section id="top" className="hero">
      <div className="wrap">
        <p className="pill">Alpha · v0.1.0 · on PyPI</p>
        <h1>
          Your agent's tools will change.
          <br />
          <span className="accent">Find out what breaks first.</span>
        </h1>
        <p className="lede">
          Drifter records how your agent really uses an MCP server, then replays those sessions
          against a deliberately mutated version of the tool interface, so you can see whether
          your agent still gets the job done. No live calls, no per-run API cost for replayed
          tool calls.
        </p>
        <div className="hero-actions">
          <CopyBlock text="uv tool install mcp-drifter" label="Copy install command" />
          <a className="btn" href={REPO}>
            View on GitHub
          </a>
          <a className="btn btn-ghost" href={PYPI}>
            PyPI
          </a>
        </div>
        <p className="fine">Python 3.11+. Also: pip install mcp-drifter. MIT licensed.</p>
      </div>
    </section>
  );
}

function Problem() {
  return (
    <Section id="problem" eyebrow="The problem" title="MCP tools change under agents, silently.">
      <div className="cols">
        <p>
          A three-month study of 515 servers found 54.6% of tools modified or deprecated. Frontier
          models degraded 13.7–14.4% under simulated tool evolution, with the damage concentrated
          in planning and reasoning rather than tool-call syntax. The MCP specification's own
          12-month deprecation policy guarantees the churn continues.
        </p>
        <p>
          Existing tools test models, servers, or agents in general. Nobody tests your{" "}
          <em>actual agent</em> against your <em>actual tools</em> under controlled interface
          change. That is the gap Drifter fills: wrap the server you already use, record what your
          agent does, and rerun the same tasks against a changed interface.
        </p>
      </div>
    </Section>
  );
}

function Lane({ title, nodes, note, dashed }) {
  return (
    <div className="lane">
      <p className="lane-title">{title}</p>
      <div className="lane-row">
        {nodes.map((n, i) => (
          <div className="lane-item" key={n.label}>
            <div className={`node ${n.hot ? "node-hot" : ""} ${n.ghost ? "node-ghost" : ""}`}>
              <strong>{n.label}</strong>
              <span>{n.sub}</span>
            </div>
            {i < nodes.length - 1 && (
              <span className={`arrow ${dashed ? "arrow-dashed" : ""}`} aria-hidden="true">
                →
              </span>
            )}
          </div>
        ))}
      </div>
      <p className="lane-note">{note}</p>
    </div>
  );
}

const STEPS = [
  {
    n: "1",
    name: "Record",
    cmd: "drifter observe",
    text: "A transparent passthrough proxy sits between your agent and a real server and writes each trajectory to a local JSONL corpus: which tools, in what order, with what shapes of arguments and results. Payloads are never written by default, only shapes, and secrets are pattern-matched and redacted.",
  },
  {
    n: "2",
    name: "Mine (optional)",
    cmd: "drifter tasks mine",
    text: "Recurring workflows in your recordings become editable task candidates: the tool sequence, how often it appeared, and draft assertions. You supply the prompt and approve. It reads recordings only, so it costs nothing to run.",
  },
  {
    n: "3",
    name: "Replay",
    cmd: "drifter replay-serve",
    text: "Your recordings become an offline stand-in for the server. Matching calls resolve instantly from the recording with no live connection. Because a real agent explores, coverage matters: calls with no recording MISS, those runs are excluded for low fidelity, and too few survivors means UNKNOWN.",
  },
  {
    n: "4",
    name: "Mutate",
    cmd: "drifter run --operator …",
    text: "One structural, closed-set operator changes the tool interface the agent sees. No free-text generation anywhere: every operator's output space is fixed and reviewable as data.",
  },
  {
    n: "5",
    name: "Evaluate",
    cmd: "drifter run / drifter report",
    text: "The agent runs the same task against the unmutated and mutated manifests. Drifter compares behavior, checks your task assertions, and screens for safety. A verdict defaults to UNKNOWN, never a false pass.",
  },
];

function How() {
  return (
    <Section id="how" eyebrow="How it works" title="Record once. Replay against a changed interface.">
      <div className="lanes">
        <Lane
          title="Recording"
          note="Real server, real traffic, shapes only."
          nodes={[
            { label: "Your agent", sub: "unchanged" },
            { label: "drifter observe", sub: "records", hot: true },
            { label: "Real MCP server", sub: "live" },
          ]}
        />
        <Lane
          dashed
          title="Replay + mutation"
          note="No upstream server is contacted. A live call under a mutated schema never happens."
          nodes={[
            { label: "Your agent", sub: "same task" },
            { label: "drifter run", sub: "replayed, mutated", hot: true },
            { label: "Your recordings", sub: "offline", ghost: true },
          ]}
        />
      </div>
      <ol className="steps">
        {STEPS.map((s) => (
          <li key={s.n} className="step">
            <span className="step-n">{s.n}</span>
            <div>
              <h3>{s.name}</h3>
              <code className="inline">{s.cmd}</code>
              <p>{s.text}</p>
            </div>
          </li>
        ))}
      </ol>
    </Section>
  );
}

function Mining() {
  return (
    <Section id="mine" eyebrow="Mining" title="Let your recordings propose the tasks.">
      <p className="lead-in">
        Writing every task by hand is the slow part. <code className="inline">drifter tasks mine</code>{" "}
        reads what your agent already did, finds the workflows that recur, and writes each as an
        editable candidate with its evidence. You write the prompt, review the assertions, and
        approve the ones you want. An approved candidate is an ordinary task:{" "}
        <code className="inline">drifter run --task-id</code> uses it as is.
      </p>
      <div className="cols cols-code">
        <div>
          <CopyBlock
            label="Copy mining commands"
            text={`drifter tasks mine
# edit task_candidates.yaml:
#   write each prompt, review its assert
drifter tasks approve <id>
drifter run --task-id <id> --yes`}
          />
          <ul className="bullets" style={{ marginTop: 20 }}>
            {MINING_POINTS.map(([h, t]) => (
              <li key={h}>
                <strong>{h}.</strong> {t}
              </li>
            ))}
          </ul>
        </div>
        <div>
          <p className="diff-label">Example candidate (task_candidates.yaml)</p>
          <pre>
            <code>{CANDIDATE_YAML}</code>
          </pre>
        </div>
      </div>
      <p className="fine">
        It also lists the tools no approved task covers, so you can see what is still untested.
        The thresholds (<code className="inline">mine:</code> in{" "}
        <code className="inline">calibration.yaml</code>) are guesses, and mining is only as good
        as the corpus: run against this project's own recordings it found 4 trajectories and one
        recurring pattern. It works, and on a small corpus it has little to find.
      </p>
    </Section>
  );
}

function Mutations() {
  const [active, setActive] = useState(0);
  const op = OPERATORS[active];
  return (
    <Section id="mutations" eyebrow="Mutations" title="Three structural operators, all closed-set.">
      <div className="tabs" role="tablist" aria-label="Mutation operators">
        {OPERATORS.map((o, i) => (
          <button
            key={o.id}
            role="tab"
            type="button"
            aria-selected={i === active}
            className={i === active ? "tab tab-on" : "tab"}
            onClick={() => setActive(i)}
          >
            {o.label}
          </button>
        ))}
      </div>
      <div className="panel" role="tabpanel">
        <p>{op.summary}</p>
        <dl className="facts">
          <div>
            <dt>Changes</dt>
            <dd>{op.changes}</dd>
          </div>
          <div>
            <dt>Never changes</dt>
            <dd>{op.never}</dd>
          </div>
          {op.guard && (
            <div>
              <dt>Safety guard</dt>
              <dd>{op.guard}</dd>
            </div>
          )}
        </dl>
        {op.before && (
          <>
            <div className="diff">
              <div>
                <p className="diff-label">Served to the agent, before</p>
                <pre>
                  <code>{op.before}</code>
                </pre>
              </div>
              <div>
                <p className="diff-label diff-label-new">After</p>
                <pre className="pre-new">
                  <code>{op.after}</code>
                </pre>
              </div>
            </div>
            <p className="fine">{op.note}</p>
          </>
        )}
      </div>
    </Section>
  );
}

function Verdicts() {
  return (
    <Section id="verdicts" eyebrow="Verdicts" title="Three axes. Honest about what it doesn't know.">
      <div className="cards">
        {VERDICTS.map((v) => (
          <article key={v.name} className="card">
            <div className="card-head">
              <h3>{v.name}</h3>
              <span className="tag">{v.tag}</span>
            </div>
            <p>{v.text}</p>
          </article>
        ))}
      </div>
      <p className="callout">
        Every verdict defaults to <strong>UNKNOWN</strong> when there is not enough evidence. That
        applies to task assertions, fidelity gating, and behavior scoring alike: Drifter would
        rather say it doesn't know than show a plausible-looking wrong answer.
      </p>
    </Section>
  );
}

function Evidence() {
  return (
    <Section id="evidence" eyebrow="Evidence" title="What the release gate showed.">
      <p className="lead-in">
        Before release, the built package was installed into a clean environment outside the
        repository and taken through the whole workflow against a controlled orders server
        (<code className="inline">find_order</code> then <code className="inline">get_order</code>
        ), with the mutation renaming <code className="inline">order_id</code> to{" "}
        <code className="inline">orderId</code>.
      </p>
      <div className="evidence">
        {EVIDENCE.map((e) => (
          <div key={e.who} className={`ev ev-${e.tone}`}>
            <p className="ev-who">{e.who}</p>
            <p className="ev-kind">{e.kind}</p>
            <p className="ev-result">{e.result}</p>
          </div>
        ))}
      </div>
      <p className="fine">
        Replay ran with the upstream server's file removed from disk, and rebuilding each report
        from stored sessions reproduced the original results exactly. Caveat: the real-agent run
        was small (3 repeats per arm, one baseline run excluded for low fidelity), so its
        Behavior verdict correctly reported UNKNOWN rather than claim more than the data
        supported.
      </p>
    </Section>
  );
}

function Start() {
  return (
    <Section id="start" eyebrow="Get started" title="From install to first report.">
      <div className="cols cols-code">
        <div>
          <h3 className="sub">1. Install and set up</h3>
          <CopyBlock
            label="Copy setup commands"
            text={`uv tool install mcp-drifter
drifter init      # finds servers in .mcp.json, Cursor, Claude Desktop
drifter doctor    # config, connectivity, tool risk classification`}
          />
          <h3 className="sub">2. Record real sessions</h3>
          <CopyBlock
            label="Copy record command"
            text={`drifter observe --server my-server
# point your agent's MCP config at this, then use it normally`}
          />
          <h3 className="sub">3. Turn recurring workflows into tasks (optional)</h3>
          <CopyBlock
            label="Copy mining commands"
            text={`drifter tasks mine
# write each candidate's prompt in task_candidates.yaml
drifter tasks approve <id>`}
          />
          <h3 className="sub">4. Check coverage, then run</h3>
          <CopyBlock
            label="Copy run commands"
            text={`drifter coverage --server my-server
drifter run --fixture .drifter/runs --server my-server \\
  --task-id my-task --operator parameter_rename --yes
drifter report --task-id my-task`}
          />
          <p className="fine">
            <code className="inline">drifter run</code> also needs an{" "}
            <code className="inline">agent:</code> block in{" "}
            <code className="inline">drifter.yaml</code> saying how to launch your agent. See the{" "}
            <a href={`${REPO}#quickstart`}>quickstart</a> for the subprocess and HTTP modes,
            including how to drive Claude Code.
          </p>
        </div>
        <div>
          <h3 className="sub">Commands</h3>
          <ul className="cmds">
            {COMMANDS.map(([c, d]) => (
              <li key={c}>
                <code className="inline">drifter {c}</code>
                <span>{d}</span>
              </li>
            ))}
          </ul>
          <h3 className="sub">Exit codes for CI</h3>
          <ul className="exits">
            {EXIT_CODES.map(([c, d]) => (
              <li key={c}>
                <code className="inline">{c}</code>
                <span>{d}</span>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </Section>
  );
}

function Limits() {
  return (
    <Section id="limits" eyebrow="Status and limits" title="Alpha, and upfront about it.">
      <div className="cols">
        <div>
          <h3 className="sub">What is solid</h3>
          <ul className="bullets">
            <li>Recording, replay, scoring and safety checks, exercised end to end.</li>
            <li>No telemetry. Payloads aren't recorded by default, only shapes.</li>
            <li>A mutated call is never forwarded to a live server.</li>
            <li>Tested on Python 3.11 to 3.13, over stdio and HTTP.</li>
          </ul>
        </div>
        <div>
          <h3 className="sub">What to keep in mind</h3>
          <ul className="bullets">
            <li>
              Replay serves recorded response <em>shapes</em>, so a task that depends on reading a
              tool's content needs an authored response fixture.
            </li>
            <li>A real agent explores, so one recording rarely covers the next run. Check coverage first.</li>
            <li>
              Detecting a genuine regression with a real agent is not yet separately validated.
              Treat Behavior verdicts as experimental, not as a release gate.
            </li>
            <li>
              Task mining needs a real corpus: a handful of trajectories has little that recurs,
              and its thresholds are unvalidated guesses.
            </li>
            <li>Expect the interface to change while it is pre-1.0.</li>
          </ul>
        </div>
      </div>
      <p className="fine">
        The full, plainly stated list lives in{" "}
        <a href={`${DOCS}/SPEC.md`}>the spec's limitations section</a>.
      </p>
    </Section>
  );
}

function Footer() {
  return (
    <footer className="footer">
      <div className="wrap footer-inner">
        <span>Drifter · MIT licensed</span>
        <span className="footer-links">
          <a href={REPO}>GitHub</a>
          <a href={PYPI}>PyPI</a>
          <a href={`${REPO}/blob/master/SECURITY.md`}>Security</a>
          <a href={DOCS}>Docs</a>
        </span>
      </div>
    </footer>
  );
}

export default function App() {
  return (
    <>
      <Header />
      <main>
        <Hero />
        <Problem />
        <How />
        <Mining />
        <Mutations />
        <Verdicts />
        <Evidence />
        <Start />
        <Limits />
      </main>
      <Footer />
    </>
  );
}
