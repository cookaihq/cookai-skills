"use strict";
// One draft per file; viewing inheritance never consumes an independent draft.
class VideoConfigDrafts {
  constructor(snapshot) { this.snapshot = snapshot; this.files = {}; this.secrets = {}; }
  field(id) { return this.snapshot.fields.find(f => f.id === id); }
  row(id, target) { return this.field(id)?.rows.find(r => r.layer === target); }
  baseMode(target) { const r = this.row("ask", target); return !r?.present ? "inherit" : r.value ? "ask" : "fixed"; }
  file(target) { return this.files[target] ||= { mode: this.baseMode(target), edits: {}, review: false, future: false }; }
  mode(target) { return this.file(target).mode; }
  setMode(target, mode) { this.file(target).mode = mode; }
  value(target, id, fallback = "") {
    const op = this.file(target).edits[id];
    return op ? op.op === "unset" ? fallback : op.value : this.row(id, target)?.value ?? fallback;
  }
  edit(target, id, value) {
    // An empty password cancels a replacement; deletion has its own action.
    if (this.field(id).sensitive && value === "") delete this.file(target).edits[id];
    else this.file(target).edits[id] = value === "" || value === null ? { op: "unset" } : { op: "set", value };
  }
  removeKey(target) { this.file(target).edits.api_key = { op: "unset" }; }
  undo(target, id) { delete this.file(target).edits[id]; }
  reveal(target, id, value) { (this.secrets[target] ||= {})[id] = value; }
  selectMethod(target, method) {
    this.edit(target, "method", method);
    if (["api", "independent-agent"].includes(method)) this.edit(target, "protocol", method === "api" ? "chat-completions" : "responses");
    if (method === "independent-agent") this.edit(target, "runtime", "codex");
  }
  normalized(target, id, operation) {
    const row = this.row(id, target);
    if (operation.op === "unset") return row?.present ? operation : null;
    const method = this.row("method", target)?.value;
    const defaults = { accept_unverified_model: false,
      protocol: method === "api" ? "chat-completions" : method === "independent-agent" ? "responses" : undefined,
      runtime: method === "independent-agent" ? "codex" : undefined };
    const original = this.field(id).sensitive ? this.secrets[target]?.[id] : row?.value ?? defaults[id];
    return operation.value === original ? null : operation;
  }
  changes(target) {
    const file = this.file(target), changes = {};
    const unused = { "main-agent": ["model", "base_url", "api_key", "runtime", "protocol", "accept_unverified_model"],
      "host-agent": ["base_url", "api_key", "runtime", "protocol"], api: ["runtime"] }[this.value(target, "method")] || [];
    for (const [id, op] of Object.entries(file.edits)) {
      if (this.field(id).group === "vision" && file.mode === "inherit") continue;
      if (unused.includes(id)) continue;
      const change = this.normalized(target, id, op);
      if (change) changes[id] = change;
    }
    if (file.mode === "inherit") {
      if (this.row("ask", target)?.present) changes.inherit = { op: "set", value: true };
    } else {
      const ask = file.mode === "ask";
      if (!this.row("ask", target)?.present || this.row("ask", target).value !== ask) changes.ask = { op: "set", value: ask };
    }
    return changes;
  }
  parked(target) {
    if (this.mode(target) !== "inherit") return 0;
    return Object.entries(this.file(target).edits).filter(([id, op]) => this.field(id).group === "vision" && this.normalized(target, id, op)).length;
  }
  pending() { return Object.keys(this.files).filter(t => Object.keys(this.changes(t)).length || this.parked(t)); }
  discard(target) { delete this.files[target]; delete this.secrets[target]; }
  acknowledge(target) { this.file(target).review = false; }
  rebase(snapshot, savedTarget = null, results = null) {
    const changed = snapshot.revision !== this.snapshot.revision, pending = this.pending();
    const previous = savedTarget ? structuredClone(this.file(savedTarget)) : null;
    const submitted = savedTarget ? this.changes(savedTarget) : {};
    const parked = savedTarget && this.parked(savedTarget) ? { ...this.file(savedTarget),
      edits: Object.fromEntries(Object.entries(this.file(savedTarget).edits).filter(([id]) => this.field(id).group === "vision")) } : null;
    if (savedTarget) this.discard(savedTarget);
    this.snapshot = snapshot;
    this.secrets = {};
    if (parked) this.files[savedTarget] = { ...parked, review: false };
    if (previous && results) {
      const remaining = Object.fromEntries(Object.entries(previous.edits).filter(([id]) => results[id]?.status !== 'saved'));
      const visionFailed = Object.keys(submitted).some(id => (id === 'inherit' || this.field(id)?.group === 'vision') && results[id]?.status !== 'saved');
      this.files[savedTarget] = { ...this.file(savedTarget), edits: remaining,
        mode: visionFailed ? previous.mode : this.baseMode(savedTarget),
        review: Object.values(results).some(result => result.code === 'conflict') };
    }
    if (changed) for (const target of pending) if (target !== savedTarget) this.file(target).review = true;
  }
}
if (typeof module !== "undefined") module.exports = { VideoConfigDrafts };
else globalThis.VideoConfigDrafts = VideoConfigDrafts;
