"use strict";
// Editing permission belongs to the loaded draft, never to an unrelated observation.
window.EvidenceDraft = class {
  constructor() { this.revision = null; this.observedRevision = null; this.evidenceId = ""; this.dirty = false; }
  begin(revision, evidenceId = "") {
    this.revision = revision; this.observedRevision = revision;
    this.evidenceId = evidenceId; this.dirty = false;
  }
  observe(revision) {
    this.observedRevision = revision;
    if (this.revision === null) this.begin(revision);
  }
  touch() { this.dirty = true; }
  applied(expected, revision) {
    // A successful command against this exact base accounts for our own change.
    // A newer observed profile cannot authorize overwriting an older draft.
    if (this.revision === expected && revision === expected + 1) this.revision = revision;
    this.observedRevision = revision;
  }
  get stale() { return this.revision !== null && this.observedRevision !== this.revision; }
};
