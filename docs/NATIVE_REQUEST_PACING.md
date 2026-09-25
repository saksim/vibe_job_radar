# Native request pacing

The native controller used to call the synchronous transport wait from inside
`Fetch.requestPaused`. A local reproduction with 20 permitted requests and the
default 0.5-second request interval exhausted a seven-second `Runtime.evaluate`
deadline despite receiving its successful reply. The earlier response-order fix
only excluded events arriving after that reply; preceding callbacks could still
sleep through the deadline.

Request permission, headers, robots and body size are checked before admission.
Chromium keeps a paced request paused. A bounded owner-thread queue retains only
its identifiers and prepared accounting metadata, never its headers or body.
The connection processes queued events before cooperative pacing work. A due
FIFO request retries the same atomic quota reservation and is continued once.
This is a delayed continuation, not an HTTP retry or a reconstructed request.

The existing 128 outstanding-request bound includes queued logical requests;
queued metadata also has a one-megabyte bound. The original short-wait budget
starts at the first reservation attempt for each FIFO request and is not reset
on contention. Hourly/daily limits, publisher windows, cooldown, cancellation,
policy and robots checks remain authoritative. Page/login action budgets are
unchanged. Navigation epochs, browser cancellations and detached sessions retire
queued work. Closing removes the pump callback and clears its private metadata.
New business intent invalidates older observations as soon as it is admitted;
only the eventual continuation consumes a request count and tracks its response.

Deterministic tests cover command responsiveness, actual ledger spacing,
contention, deadlines and refusal/cleanup boundaries. The artificial TLS browser
acceptance also adds 20 permitted stylesheets with the real default interval,
then requires one visible keyword search, the full recorded JD, and the original
report. These tests do not certify a real recruiting-site session or login.
