# Contributing to Apex Capital

Apex Capital is a single-owner personal project. Pull requests are
welcome — the repo is public so that peers can read and critique
the design — but merges are Chairman-only (Jack Beard). A PR that
lands here is a design decision, and Jack has to make it.

If that suits you, keep reading.

## Setup

```bash
git clone https://github.com/jbeard1209/apex-capital.git
cd apex-capital
cp .env.example .env       # fill in your own credentials
$EDITOR .env

cd institutional-tracker
python3 -m pip install -r requirements.txt
python3 test_pipeline_smoke.py     # smoke suite
```

Python 3.11 is the target. Older Pythons have a habit of accepting
`X | None` unions at parse time and failing at runtime; the CI job
runs 3.11 and the pre-push gate assumes it.

## The pre-push gate

The single most important local check. Install it from the repo
root (it is not cloned with `.git/`):

```bash
cd "$(git rev-parse --show-toplevel)"
cp scripts/git-hooks/pre-push .git/hooks/pre-push
chmod +x .git/hooks/pre-push
```

The gate runs the wiring-contract suite **twice** — once in the
Mac's local zone (`America/New_York`) and once with `TZ=UTC`
(the VM's zone). "Green on the Mac" is not enough; both must pass.
That two-zone gate exists because host-divergence bugs are a
class, not an accident. See [docs/history/](docs/history/) if
you want the incidents.

A push that times out at the SSH layer (~8 minutes for the full
suite) is not a gate failure — set repo-local SSH keepalives once
per checkout:

```bash
git config core.sshCommand "ssh -o ServerAliveInterval=20 -o ServerAliveCountMax=30"
```

## Coding standards

- **Match surrounding code.** The project has a house style but
  no enforced formatter. Prefer the shape of the file you are
  editing.
- **Explain the *why*, not the diff.** Docstrings and comments
  answer *why the code is shaped this way*, ideally naming the
  incident that shaped it (search for "postmortem", "used to say",
  "until 2026-"). "What the code does" is well-named variables'
  job.
- **Type hints where they help**, not everywhere. `Any` is honest;
  a wrong annotation is a bug.

## Producer / consumer contracts

The system's most common bug class is **silent identifier
mismatches** — a producer emits a rule name, pod name, or category
that no consumer handles, and `.get()` / `try/except` swallow it.

If your PR adds a new routed item, it must also add a case to
`test_pipeline_smoke.py::TestWiringContracts`. Not "should", must.
Reviewers will look for it.

## Coherence laws

If your PR adds a new coherence law (a
`coherence_laws.py::CoherenceLaw` subclass), it must also add a
paired **poison pill** — an input engineered to make the law fail.
A law whose poison pill passes is a blind control, and blind is a
Chairman-level finding. The internal-audit suite will fail your PR
if the pill is missing.

## Protected files

Do not modify these without a Chairman decision recorded in
`data/chairman_decision_queue.json`:

- `alpaca_trader.py`
- `main.py`
- `risk_manager_agent.py`
- `irm_review_agent.py`
- `board_meeting.py`
- `manager_agent.py`

These are the choke points; every incident that ever mattered
touched one of them. Chairman-only means Chairman-only.

## Data hygiene

`institutional-tracker/data/` is **selectively gitignored on
purpose**. Live trade state, PII, agent evaluations, and proposal
queues are runtime artifacts, not source. `git add -f` on an
ignored file will be rejected in review.

There is **no state sync between hosts**. Any change that requires
a data migration must be safe to run on the Mac and on the GCP VM
independently.

## Commit style

- One logical change per commit; the pre-push gate makes it
  expensive to churn.
- Reference the incident or postmortem where relevant.
- Match the tone of `git log --oneline` — direct, specific.
- End the commit message with the Claude co-author trailer.

## Filing an issue

Use the templates. A bug report without reproduction steps or a
feature request without a stated motivation will be closed with a
pointer back here.
