import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
const { VideoConfigDrafts } = createRequire(import.meta.url)("../ui/configuration.js");

function snapshot(files = {}, revision = "one") {
  const definitions = { ask: "vision", method: "vision", model: "vision", base_url: "vision", api_key: "vision",
    protocol: "vision", runtime: "vision", accept_unverified_model: "vision", call_limit: "vision", output: "advanced" };
  return { revision, fields: Object.entries(definitions).map(([id, group]) => ({
    id, group, sensitive: id === "api_key", rows: ["project", "global"].map(layer => ({
      layer, present: files[layer]?.[id] != null, value: id === "api_key" ? undefined : files[layer]?.[id],
      configured: id === "api_key" && !!files[layer]?.[id]
    }))
  })) };
}
const saved = { project: { ask: false, method: "api", model: "saved-model", api_key: "synthetic-only" } };

test("unknown earlier write retains an unattempted vision mode draft", () => {
  const store = new VideoConfigDrafts(snapshot({project: {ask: true, method: "main-agent", output: "old"}}));
  store.edit("project", "output", "new");
  store.setMode("project", "fixed");
  store.rebase(snapshot({project: {ask: true, method: "main-agent", output: "new"}}, "two"), "project",
    {output: {status: "uncertain", code: "readback"}});
  assert.equal(store.mode("project"), "fixed");
  assert.deepEqual(store.changes("project"), {ask: {op: "set", value: false}});
  assert.deepEqual(store.file("project").edits.output, {op: "set", value: "new"});
});

test("inherit round trip retains route, model and replacement without submitting them", () => {
  const store = new VideoConfigDrafts(snapshot(saved));
  store.edit("project", "model", "draft-model");
  store.edit("project", "api_key", "replacement");
  store.setMode("project", "inherit");
  assert.deepEqual(store.changes("project"), { inherit: { op: "set", value: true } });
  assert.equal(store.parked("project"), 2);
  store.setMode("project", "fixed");
  assert.equal(store.value("project", "model"), "draft-model");
  assert.equal(store.changes("project").api_key.value, "replacement");
});
test("new independent groups do not copy a global connection", () => {
  const store = new VideoConfigDrafts(snapshot({ global: saved.project }));
  store.setMode("project", "fixed");
  assert.equal(store.value("project", "model"), "");
  assert.equal(store.value("project", "api_key"), "");
  assert.deepEqual(store.changes("project"), { ask: { op: "set", value: false } });
});
test("untouched placeholder and cancelled Key replacement retain the saved Key", () => {
  const store = new VideoConfigDrafts(snapshot(saved));
  assert.deepEqual(store.changes("project"), {});
  store.edit("project", "api_key", "replace-me");
  store.edit("project", "api_key", "");
  assert.deepEqual(store.changes("project"), {});
});
test("Key removal is explicit, reversible as a draft, and independent from replacement", () => {
  const store = new VideoConfigDrafts(snapshot(saved));
  store.removeKey("project");
  assert.deepEqual(store.changes("project").api_key, { op: "unset" });
  store.undo("project", "api_key");
  assert.deepEqual(store.changes("project"), {});
});
test("revealed original is a comparison baseline, not an edit", () => {
  const store = new VideoConfigDrafts(snapshot(saved));
  store.reveal("project", "api_key", "synthetic-only");
  assert.deepEqual(store.changes("project"), {});
  store.edit("project", "api_key", "new-key");
  store.edit("project", "api_key", "synthetic-only");
  assert.deepEqual(store.changes("project"), {});
});
test("ask-only configuration does not silently select a fixed method", () => {
  const store = new VideoConfigDrafts(snapshot({ project: { ask: true } }));
  store.setMode("project", "fixed");
  assert.equal(store.value("project", "method"), "");
  assert.deepEqual(store.changes("project"), { ask: { op: "set", value: false } });
  store.setMode("project", "ask");
  assert.deepEqual(store.changes("project"), {});
});
test("returning method and ordinary fields to the saved baseline clears changes", () => {
  const store = new VideoConfigDrafts(snapshot({ project: { ask: false, method: "main-agent", output: "old" } }));
  store.selectMethod("project", "independent-agent");
  store.edit("project", "model", "other");
  store.selectMethod("project", "main-agent");
  store.edit("project", "output", "new");
  store.edit("project", "output", "old");
  assert.deepEqual(store.changes("project"), {});
});
test("implicit protocol defaults do not produce a dirty draft after a round trip", () => {
  const store = new VideoConfigDrafts(snapshot(saved));
  store.selectMethod("project", "independent-agent");
  store.selectMethod("project", "api");
  assert.deepEqual(store.changes("project"), {});
});
test("per-file changes and submission remain separate across scope navigation", () => {
  const store = new VideoConfigDrafts(snapshot(saved));
  store.edit("project", "model", "project-draft");
  store.setMode("global", "ask");
  store.edit("global", "output", "global-output");
  assert.deepEqual(store.pending(), ["project", "global"]);
  assert.equal(store.changes("global").model, undefined);
  assert.equal(store.changes("project").output, undefined);
});
test("save consumes one file and requires review of remaining drafts against a new revision", () => {
  const store = new VideoConfigDrafts(snapshot(saved));
  store.edit("project", "model", "project-draft");
  store.edit("global", "output", "global-draft");
  store.rebase(snapshot({ ...saved, global: { output: "saved-elsewhere" } }, "two"), "project");
  assert.deepEqual(store.changes("project"), {});
  assert.equal(store.value("global", "output"), "global-draft");
  assert.equal(store.file("global").review, true);
  store.acknowledge("global");
  assert.equal(store.file("global").review, false);
});
test("saving inherited removal preserves the independent draft that was not submitted", () => {
  const store = new VideoConfigDrafts(snapshot(saved));
  store.edit("project", "model", "parked");
  store.setMode("project", "inherit");
  store.rebase(snapshot({}, "two"), "project");
  assert.deepEqual(store.changes("project"), {});
  assert.equal(store.parked("project"), 1);
  store.setMode("project", "fixed");
  assert.equal(store.value("project", "model"), "parked");
});
test("external reload preserves input, invalidates review, and hides revealed secrets", () => {
  const store = new VideoConfigDrafts(snapshot(saved));
  store.edit("project", "model", "draft");
  store.reveal("project", "api_key", "synthetic-only");
  store.rebase(snapshot({ project: { ...saved.project, model: "external" } }, "two"));
  assert.equal(store.value("project", "model"), "draft");
  assert.equal(store.file("project").review, true);
  assert.deepEqual(store.secrets, {});
  store.discard("project");
  assert.equal(store.value("project", "model"), "external");
});
