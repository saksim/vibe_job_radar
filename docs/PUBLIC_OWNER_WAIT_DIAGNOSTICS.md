# Windows public-owner wait evidence

Refs #89, #126. Candidate `1987986` first CI run `36182315131` finished
19/21 checks. Windows Python 3.10 ran all 2304 tests in 656.062 seconds with
three failures in `test_public_owner`: resume after a real process crash still
running after 15 seconds, child startup returning no task ID, and the observer
fixture not entering its synthetic transport after 5 seconds.

The first failure's thread dump shows the task waiting for report writers;
those writers were in temporary-file creation and flush. The third failure
shows the task in `RateLedger.reserve`. The second failure discarded the child
diagnostic. These observations do not establish a common cause, an ownership
violation, or an fsync explanation for all three failures.

The tests now include bounded task/worker/report-writer observations when the
original 5/15-second waits fail. They do not call `snapshot` or acquire a task
lock while diagnosing a possibly stalled writer. State fields use fixed
allowlists; frames contain only file basenames, line numbers and function names.
No task IDs, queries, messages, source URLs, locals or exception bodies are
included. The existing test-only fsync probe calls the real fsync and includes
the owned report pool. Cumulative completed time may overlap across writers;
it is not elapsed wall time.

A child that fails its five-second readiness check emits a structured failure
packet before teardown. The parent reports that failure and still runs its
registered child/service cleanup. Successful child protocol and all behavioral
assertions remain. No production code, wait duration, persistence, source
request, rate limit or CI retry changes. This adds evidence; it does not repair
or certify the underlying Windows timing failures.
