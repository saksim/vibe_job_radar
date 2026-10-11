# Current browser and source qualification budgets

Refs #179 / #57 / #210. The browser workflow runs its existing 39 commands in
two sequential phases: 21 onboarding and network-route commands, then 18
collection, report and policy-binding commands. Each phase has a fixed
ten-minute ceiling. Installation retains eight minutes; the enclosing browser
job has 30 minutes, leaving two minutes for checkout and evidence upload.
This changes the overall browser allowance from ten to twenty minutes; it
does not give any individual browser assertion, request or subprocess a new
deadline. Both phases must succeed before source qualification can start.

The reason is the first main run after
[PR #308](https://github.com/saksim/vibe_job_radar/pull/308):
[run 38118975904, job 114409591223](https://github.com/saksim/vibe_job_radar/actions/runs/38118975904/job/114409591223)
was stopped by GitHub's explicit ten-minute step-timeout annotation. Its
completed results through Windows startup settings were successful, but the
remaining collection and report scenarios did not finish. The dependent
source gate also failed; this is not a second independent browser defect.
The same-tree candidate completed all browser commands in 467 seconds.
On main, the first phase's last JSON was emitted about 486 seconds after the
exercise step started (about 272 seconds on the candidate). These are observed
wall-clock/output times, not measurements of individual internal operations.
They justify separate bounded phases but do not establish the physical cause
of runner slowdown or fix any earlier unknown timing failure.

All 39 commands, their order, installation, browser artifacts and the final
success gate remain unchanged. The second phase uses the default success
condition, so it does not run after a failed or cancelled first phase.
Evidence upload still uses always(); source qualification still explicitly
requires browser success. There is no automatic retry or adaptive extension.
A new candidate and its actual main must each pass their own first CI run.
The failed main308 run remains failed; later success cannot qualify it.

Source qualification separately retains 1200 seconds for units and 180 seconds
for each of three auxiliary checks, inside its 35-minute job. See
[SOURCE_QUALIFICATION.md](SOURCE_QUALIFICATION.md). The older figures below
describe their original revisions, not the current budgets.

Validation parses the before/after workflow, compares the exact ordered
command list and unchanged jobs, and checks both phase limits and the enclosing
allowance. Production code, browser scripts and unit tests are unchanged.

---

## Historical separation of browser and source jobs

Refs #199. The first `bb10e77` run `36184850311` was cancelled with GitHub's
explicit annotation that the job exceeded its ten-minute execution limit.
All browser exercises succeeded at 20:24:42 UTC; source qualification started
at 20:24:45 and was cancelled at 20:27:31, during its unit-test subprocess.
The source verifier's own unchanged unit deadline is 600 seconds. It received
about 166 seconds of this job's remaining budget. Source verification evidence
and the source candidate were not produced. Other jobs' passing results cannot
replace this missing qualification.

`browser-exercises` retains the ten-minute limit, installation commands,
all browser exercises and always-uploaded browser evidence. A separate
`chromium-user-journey` job keeps the original required-check name as the final
gate. It uses `always()` and explicitly requires the browser result to be
`success`, so a failed, cancelled or skipped browser job produces a failure
instead of a skipped required check. After that gate succeeds, it checks out
the same workflow revision, installs the same dependencies, and runs the
original source verification, evidence upload, source build and candidate
upload in their existing order.

The new source job has 25 minutes: its child checks retain the original
600-second unit limit and three 180-second auxiliary limits (19 minutes in
total), with six minutes for setup, packaging and uploads. This does not change
any test assertion, subprocess deadline or production request deadline. A
browser failure prevents source checkout, verification and building; a source
verification failure still prevents building. No existing evidence is downloaded or reused
to qualify the new checkout. The builder still verifies the source fingerprint
against all four successful source checks, including zero skipped unit tests.

Validation compares the parsed old/new workflows: the complete browser step
sequence is retained; all four source steps retain their exact definitions;
dependency installation is identical; the final job requires browser success;
production, scripts and tests are byte-identical. The new workflow still needs
its own CI execution. The original cancellation, the parent's two Windows
failures and their artifacts remain unchanged; this budget correction does
not claim to fix those separate Windows timing failures.

The dependency and `always()` behavior follows the
[GitHub workflow reference](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#jobsjob_idneeds).

Current integration preserves all 28 browser commands from the qualified PR94 tree, including guided ownership and the pending-login/automatic continuation exercises. The separate #126 cleanup only changes the offline matrix progress flag and a test fixture, so it does not replace browser or source steps here. The new candidate and actual main must each supply fresh browser evidence, source qualification, source archive and Windows executable evidence; original PR200 figures are historical. The integration starts from qualified main/11634913; current candidate and main CI results are recorded in the associated PR.
