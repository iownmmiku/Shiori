"use strict";
// Helpers for extension pages embedded by the Shiori console (docs/EXTENSIONS.md).
// A page includes /static/extension-page.css and this script, then calls
// ShioriExtension.api("route") for the routes its extension registered.
window.ShioriExtension = (() => {
  const match = location.pathname.match(/^\/extensions\/([a-z][a-z0-9_]{1,39})\/pages\//);
  const id = match ? match[1] : "";
  const escape = value => String(value ?? "").replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
  let fieldId = 0;

  async function api(route, { method = "GET", body } = {}) {
    if (!id) throw new Error("这个页面不在扩展地址下打开，无法调用扩展接口。");
    const options = { method, credentials: "same-origin", headers: { Accept: "application/json" } };
    if (body !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    const response = await fetch(`/api/extensions/${encodeURIComponent(id)}/api/${String(route).replace(/^\/+/, "")}`, options);
    let data = null;
    try { data = await response.json(); } catch { /* an empty or non-JSON body */ }
    if (response.status === 401) throw new Error("登录已失效，请刷新控制台后重新登录。");
    if (!response.ok) throw new Error(typeof data?.detail === "string" ? data.detail : `请求失败（HTTP ${response.status}）`);
    return data;
  }

  // Form pieces that match the console's look.
  const ui = {
    field(name, title, value = "", type = "text", hint = "", extra = "") {
      const key = `ext-field-${++fieldId}`;
      return `<div class="field ${extra.includes("data-full") ? "full" : ""}"><label for="${key}">${escape(title)}</label><input id="${key}" name="${escape(name)}" type="${escape(type)}" value="${type === "password" ? "" : escape(value)}" ${extra}>${hint ? `<small>${escape(hint)}</small>` : ""}</div>`;
    },
    textarea(name, title, value = "", hint = "", extra = "") {
      const key = `ext-field-${++fieldId}`;
      return `<div class="field full"><label for="${key}">${escape(title)}</label><textarea id="${key}" name="${escape(name)}" ${extra}>${escape(value)}</textarea>${hint ? `<small>${escape(hint)}</small>` : ""}</div>`;
    },
    select(name, title, options, value, extra = "") {
      const key = `ext-field-${++fieldId}`;
      return `<div class="field ${extra.includes("data-full") ? "full" : ""}"><label for="${key}">${escape(title)}</label><select id="${key}" name="${escape(name)}" ${extra}>${options.map(([option, text]) => `<option value="${escape(option)}" ${String(value) === String(option) ? "selected" : ""}>${escape(text)}</option>`).join("")}</select></div>`;
    },
    toggle(name, title, hint, checked) {
      return `<div class="toggle-field"><div class="toggle-copy"><strong>${escape(title)}</strong><small>${escape(hint)}</small></div><label class="switch"><input type="checkbox" name="${escape(name)}" aria-label="${escape(title)}" ${checked ? "checked" : ""}><span></span></label></div>`;
    },
    button(text, action, style = "", attrs = "") {
      return `<button type="button" class="btn ${style}" data-action="${escape(action)}" ${attrs}>${escape(text)}</button>`;
    },
    values(form) {
      const values = Object.fromEntries(new FormData(form));
      form.querySelectorAll('input[type="checkbox"]').forEach(item => { values[item.name] = item.checked; });
      return values;
    },
  };

  let toastTimer = 0;
  function toast(message, error = false) {
    let node = document.getElementById("extension-toast");
    if (!node) {
      node = document.createElement("div");
      node.id = "extension-toast";
      node.setAttribute("role", "status");
      document.body.appendChild(node);
    }
    node.textContent = message;
    node.className = error ? "extension-toast error" : "extension-toast";
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { node.className = "extension-toast hidden"; }, error ? 6000 : 3000);
  }

  // One dialog at a time; `onSubmit(values, form)` may throw to keep it open.
  function dialog(title, description, body, onSubmit, submitLabel = "保存") {
    let node = document.getElementById("extension-dialog");
    if (!node) {
      node = document.createElement("dialog");
      node.id = "extension-dialog";
      node.className = "modal";
      document.body.appendChild(node);
    }
    node.innerHTML = `<form method="dialog" novalidate><div class="modal-head"><div><h2>${escape(title)}</h2>${description ? `<p>${escape(description)}</p>` : ""}</div><button type="button" class="btn ghost small" data-close aria-label="关闭">关闭</button></div><div class="modal-body">${body}<div class="modal-error" hidden></div></div><div class="modal-footer"><button type="button" class="btn ghost" data-close>取消</button><button type="submit" class="btn primary">${escape(submitLabel)}</button></div></form>`;
    const form = node.querySelector("form"), error = node.querySelector(".modal-error");
    node.querySelectorAll("[data-close]").forEach(item => item.addEventListener("click", () => node.close()));
    form.addEventListener("submit", async event => {
      event.preventDefault();
      if (!form.reportValidity()) return;
      error.hidden = true;
      try {
        await onSubmit(ui.values(form), form);
        node.close();
      } catch (exc) {
        error.textContent = exc?.message || String(exc);
        error.hidden = false;
      }
    });
    node.showModal();
    return node;
  }

  function confirm(title, text, perform, label = "确认") {
    return dialog(title, text, "", perform, label);
  }

  // The console gives the frame a fixed height and the page scrolls inside it,
  // so fixed-position dialogs and toasts stay in view.
  return { id, api, escape, ui, toast, dialog, confirm };
})();
