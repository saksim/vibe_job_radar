# Refused requests when the page closes

Refs #139, #63, #57. Integration requires the qualified current visible-search main; exact parent and tested trees are recorded in the associated PR.

The SDK ownership guard first records its existing refusal/cancellation, then aborts an unowned or stopped request. A deferred target close can make that abort fail. The callback now tolerates the failure only if the public page API reports is_closed() is True, or the exception is the installed optional Playwright TargetClosedError type. It preserves the original stop code and never continues, fetches, fulfills, retries, or reopens the refused request.

A live or unreadable page is not evidence of closure. If the optional exception type is unavailable, or a different exception merely has the same name or message, the original exception propagates. Fixed-name counters distinguish a closed page, the exact closed-target type, and an unconfirmed failure without retaining arbitrary exception text.

This guard is installed only for the existing SDK non-CORS route path. Native CORS and direct CDP Chrome keep their independent Fetch/target guards. Those launch, request, TLS, ownership and pacing rules are unchanged.

## Verification

Eight added behavior tests preserve every existing test method. The old implementation failed four of the eight tests with errors; the revised prototype passed 164 related tests with no failures, errors or skips. They cover closure during abort, closure before a public frame event, unreadable page state, a fake exception name, missing optional driver type, original hard failure retention, and unknown/live errors.

On 2026-10-03, an isolated local HTTP probe used installed Edge 154.0.4258.48 with Playwright 1.63.0 and direct Chrome 156.0.8072.0. In Edge, closing the actual page before abort returned without a callback error, preserved native_surface_unsupported, and sent zero requests to the refused path. That SDK version suppressed the post-close abort exception itself, so this probe did not exercise the new catch branch. A separate evaluation on the closed page produced the exact driver type. Direct Chrome has no SDK route layer and its closed-page operation error was not mistaken for that type.

The probe's first attempt incorrectly expected the interrupted goto call itself to produce TargetClosedError; it actually returned generic ERR_ABORTED. That first failure remains recorded. Only that probe assumption changed; the production classifier does not infer closure from this message.

The eight deterministic tests exercise the catch branches. Candidate and actual main still require their own first CI and source/portable qualification, including the six existing native browser modes and original popup refusal path. Existing popup assertions require no uncontrolled page and zero target HTTP. A later green run is not presented as a fresh reproduction or diagnosis of every historical timing failure.

This change does not certify real recruiting login, manual CAPTCHA/SMS, or full platform access. It uses no user browser profile or host certificate changes.
