# Separate browser and source-candidate job budgets

Refs #199. The first `bb10e77` run `36184850311` was cancelled with GitHub's
explicit annotation that the job exceeded its ten-minute execution limit.
All browser exercises succeeded at 20:24:42 UTC; source qualification started
at 20:24:45 and was cancelled at 20:27:31, during its unit-test subprocess.
The source verifier's own unchanged unit deadline is 600 seconds. It received
about 166 seconds of this job's remaining budget. Source verification evidence
and the source candidate were not produced. Other jobs' passing results cannot
replace this missing qualification.

`chromium-user-journey` retains its ten-minute limit, installation commands,
all browser exercises and always-uploaded browser evidence. A separate
`verified-source-candidate` job depends on that job succeeding. It checks out
the same workflow revision, installs the same dependencies, and runs the
original source verification, evidence upload, source build and candidate
upload in their existing order.

The new source job has 25 minutes: its child checks retain the original
600-second unit limit and three 180-second auxiliary limits (19 minutes in
total), with six minutes for setup, packaging and uploads. This does not change
any test assertion, subprocess deadline or production request deadline. A
browser failure prevents the source job from starting; a source verification
failure still prevents building. No existing evidence is downloaded or reused
to qualify the new checkout. The builder still verifies the source fingerprint
against all four successful source checks, including zero skipped unit tests.

Validation compares the parsed old/new workflows: the complete browser step
sequence is retained; all four source steps retain their exact definitions;
dependency installation is identical; the new job depends on browser success;
production, scripts and tests are byte-identical. The new workflow still needs
its own CI execution. The original cancellation, the parent's two Windows
failures and their artifacts remain unchanged; this budget correction does
not claim to fix those separate Windows timing failures.
