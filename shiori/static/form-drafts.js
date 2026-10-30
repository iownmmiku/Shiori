"use strict";

// Keep only unsaved form edits in this authenticated page's memory. Nothing is
// persisted to browser storage, and a logout destroys both values and keys.
window.ShioriFormDrafts = (() => {
  const drafts = new Map(), pending = new Set();
  let baselines = new WeakMap(), mounted = "", generation = -1;
  const context = () => `${state.page}:${state.page === "persona" ? state.botTab || "basic" : ""}:${state.scope || ""}`;
  const forms = () => [...(document.querySelectorAll?.('#content form[data-form]') || [])].filter(form => form.dataset.form !== "chat");
  const key = form => `${mounted}:${form.dataset.form}:${form.id || ""}`;
  function fields(form) {
    const counts = new Map();
    return [...form.querySelectorAll('input[name],textarea[name],select[name]')].filter(node => !["file", "submit", "button", "hidden"].includes(node.type)).map(node => {
      const index = counts.get(node.name) || 0; counts.set(node.name,index+1);
      return [`${node.name}:${index}`,node];
    });
  }
  const value = node => ["checkbox","radio"].includes(node.type) ? node.checked : node.multiple ? [...node.options].filter(option => option.selected).map(option => option.value) : node.value;
  const snapshot = form => new Map(fields(form).map(([name,node]) => [name,value(node)]));
  const equal = (a,b) => JSON.stringify(a) === JSON.stringify(b);
  function clear() { drafts.clear(); pending.clear(); baselines = new WeakMap(); mounted = ""; generation = state.authGeneration || 0; }
  function authenticated() {
    if (generation !== (state.authGeneration || 0)) clear();
    return state.authenticated !== false;
  }
  function capture() {
    if (!authenticated() || !mounted) return;
    for (const form of forms()) {
      const id = key(form), baseline = baselines.get(form);
      if (!baseline || pending.has(id)) continue;
      const changed = new Map([...snapshot(form)].filter(([name,item]) => baseline.has(name) && !equal(item,baseline.get(name))));
      if (changed.size) drafts.set(id,changed); else drafts.delete(id);
    }
  }
  function restore() {
    if (!authenticated()) return;
    mounted = context(); baselines = new WeakMap();
    for (const form of forms()) {
      baselines.set(form,snapshot(form));
      const id = key(form), saved = !pending.has(id) && drafts.get(id);
      if (!saved) continue;
      for (const [name,node] of fields(form)) {
        if (!saved.has(name)) continue;
        const item = saved.get(name);
        if (["checkbox","radio"].includes(node.type)) node.checked = item;
        else if (node.multiple) for (const option of node.options) option.selected = item.includes(option.value);
        else if (node.tagName !== "SELECT" || [...node.options].some(option => option.value === item)) node.value = item;
        if (typeof syncRangePair === "function") syncRangePair(node);
      }
    }
  }
  function begin(form) {
    if (!authenticated() || !forms().includes(form)) return null;
    capture(); const ticket = {id:key(form),generation,form,submitted:snapshot(form)}; pending.add(ticket.id); return ticket;
  }
  function end(ticket,success) {
    if (!ticket || !authenticated() || ticket.generation !== generation) return;
    pending.delete(ticket.id);
    if (success) {
      drafts.delete(ticket.id);
      // Some saves update a small status area without rebuilding their form.
      // Keep edits made while the request was in flight dirty against the values
      // actually submitted, rather than the old server baseline.
      if (baselines.has(ticket.form)) baselines.set(ticket.form,ticket.submitted);
    }
  }
  return {capture,restore,begin,end,clear};
})();
