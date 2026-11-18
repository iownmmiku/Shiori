"use strict";
// The image generation extension's settings page: services, persona looks and a test picture.
(() => {
  const ext = window.ShioriExtension, { ui } = ext, e = ext.escape;
  const kinds = { openai: "OpenAI 兼容图像接口", gemini: "Gemini 图像模型", novelai: "NovelAI", comfyui: "ComfyUI 工作流" };
  const endpoints = { openai: "https://api.openai.com/v1", gemini: "https://generativelanguage.googleapis.com/v1beta", novelai: "https://image.novelai.net", comfyui: "http://127.0.0.1:8188" };
  const aspects = [["square", "方图"], ["portrait", "竖图"], ["landscape", "横图"]];
  const mappings = [["prompt", "正面提示词（必填）"], ["negative_prompt", "负面提示词"], ["reference", "参考图（LoadImage 的 image）"], ["width", "宽度"], ["height", "高度"], ["seed", "随机种子"]];
  const keys = ["id", "name", "kind", "endpoint", "model", "timeout_seconds", "negative_prompt", "natural_negative", "reference", "persona_reference", "quality", "steps", "scale", "sampler", "strength", "extra_body", "workflow", "workflow_fields", "output_node"];
  const root = document.getElementById("app");
  const view = { config: null, test: null, testPrompt: "一只坐在窗台上晒太阳的橘猫，水彩风格", testAspect: "square", testPersona: "" };
  const MAX_PERSONA_REFERENCES = 3, MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
  const services = () => view.config?.services || [];
  const plain = item => Object.fromEntries(keys.filter(key => item[key] !== undefined).map(key => [key, item[key]]));
  const json = (text, label) => {
    const value = String(text || "").trim();
    if (!value) return {};
    let parsed;
    try { parsed = JSON.parse(value); } catch { throw new Error(`${label}不是有效的 JSON。`); }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error(`${label}须为 JSON 对象。`);
    return parsed;
  };
  const number = (value, label) => { const result = Number(value); if (!Number.isFinite(result)) throw new Error(`${label}须为数字。`); return result; };

  function usageText(usage) {
    const counts = Object.values(usage?.counts || {}), total = counts.reduce((sum, value) => sum + value, 0);
    return `今天普通成员已画 ${total} 张（${counts.length} 人）· 排队 ${usage?.queued ?? 0} 张 · 正在画 ${usage?.running ?? 0} 张`;
  }

  function serviceCard(item) {
    const current = view.config.service_id === item.id;
    const facts = [kinds[item.kind] || item.kind, item.model || "工作流自带模型", item.api_key_set ? "已保存密钥" : "未填写密钥", item.supports_reference ? "支持参考图改图" : "不支持参考图"];
    return `<article class="record-card"><h3>${e(item.name)}${current ? " · 当前使用" : ""}</h3><p class="help-caption">${e(facts.join(" · "))}</p><p class="help-caption">${e(item.endpoint)}</p><div class="record-actions">${current ? "" : ui.button("设为当前", "use", "primary small", `data-id="${e(item.id)}"`)}${ui.button("编辑", "edit", "ghost small", `data-id="${e(item.id)}"`)}${ui.button("删除", "delete", "ghost small", `data-id="${e(item.id)}"`)}</div></article>`;
  }

  function render() {
    const cfg = view.config, list = services();
    const current = list.find(item => item.id === cfg.service_id);
    const warning = cfg.warning ? `<p class="help-caption" role="alert">${e(cfg.warning)}</p>` : "";
    const summary = `<section class="panel settings-section"><h2>生图</h2>${warning}<p>在 QQ 群聊和私聊里，对方请角色画画、拍照、自拍或改图时，聊天模型可以调用画图工具；角色先按自己的意愿决定画不画，文字回复照常先发，图片在后台画好后单独发出，一般要几十秒。会话需要允许使用工具。对方消息里附了图片时，可以照着它改图（当前服务需支持参考图）。</p><p><strong>当前服务：</strong>${current ? e(current.name) : "未选择（选好服务后角色才会画图）"}</p><p class="help-caption">${e(usageText(cfg.usage))} · 每人每天最多 ${e(cfg.daily_per_user)} 张（管理员不限），在「扩展」页生图的「设置」里修改。</p></section>`;
    const add = Object.entries(kinds).map(([kind, title]) => ui.button("添加 " + title, "add", "ghost small", `data-kind="${kind}"`)).join("");
    const cards = list.length ? `<div class="record-grid">${list.map(serviceCard).join("")}</div>` : '<p class="help-caption">还没有生图服务。先添加一个，第一个添加的会自动设为当前使用。</p>';
    const servicePanel = `<section class="panel settings-section"><h2>生图服务</h2><p>支持 OpenAI 兼容的图像接口（gpt-image、DALL·E 及多数中转站的绘图模型）、Gemini 图像模型、NovelAI 和本机或局域网的 ComfyUI。密钥只保存在本机，页面不会回显。</p>${cards}<div class="imagegen-add">${add}</div><p class="help-caption">生图配置保存在数据目录的 extension_data/imagegen 文件夹里，普通应用备份不包含它。</p></section>`;
    root.innerHTML = summary + servicePanel + personaSection() + (list.length ? testSection() : "");
  }

  function personaSection() {
    const list = view.config.persona_list || [], looks = view.config.personas || {};
    if (!list.length) return "";
    const rows = list.map(persona => {
      const look = looks[persona.id] || {};
      return `<div class="imagegen-persona"><h3>${e(persona.name)}</h3>${ui.textarea(`prompt__${persona.id}`, "外貌提示词", look.prompt || "", "发型、发色、瞳色、脸型、身材、常穿的衣服等。NovelAI / ComfyUI 建议用英文标签，例如 1girl, long brown hair, amber eyes。", 'maxlength="2000" rows="3"')}${ui.textarea(`negative__${persona.id}`, "外貌负面提示词", look.negative_prompt || "", "自拍时不想出现的特征，例如 short hair, glasses。", 'maxlength="2000" rows="2"')}${referenceRow(persona, look.references || [])}</div>`;
    }).join("");
    return `<section class="panel settings-section"><h2>人格外貌</h2><p>角色画自己（自拍、发自己的照片）时，会自动把当前人格的外貌提示词加进画面描述，负面提示词并入服务的负面提示词；上传了参考图的，还会把参考图一起交给生图服务，让每张图里的她长得一样。画别的东西时都不会加。</p><form data-form="personas">${rows}<div class="section-actions"><button type="submit" class="btn primary">保存人格外貌</button></div></form></section>`;
  }

  function referenceRow(persona, references) {
    const items = references.map((item, index) => `<figure class="imagegen-reference">${item.thumb ? `<img src="${e(item.thumb)}" alt="${e(persona.name)}的参考图 ${index + 1}">` : '<span class="help-caption">无法预览</span>'}${ui.button("移除", "remove-reference", "ghost small", `data-persona="${e(persona.id)}" data-id="${e(item.id)}"`)}</figure>`).join("");
    const full = references.length >= MAX_PERSONA_REFERENCES;
    const add = full ? "" : `<label class="btn ghost small imagegen-upload">添加参考图<input type="file" accept="image/png,image/jpeg,image/webp" data-upload="${e(persona.id)}" hidden></label>`;
    return `<div class="imagegen-references"><strong>参考图</strong><p class="help-caption">最多 ${MAX_PERSONA_REFERENCES} 张，选清楚的正脸或半身图，上传后立即保存。只有打开了「自拍时带上人格参考图」的服务会用；OpenAI 兼容接口（gpt-image）和 Gemini 效果最好。</p><div class="imagegen-reference-list">${items}${add}</div></div>`;
  }

  function readFile(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result));
      reader.onerror = () => reject(new Error("读取图片失败，请重新选择。"));
      reader.readAsDataURL(file);
    });
  }

  async function upload(input) {
    const file = input.files?.[0], personaId = input.dataset.upload;
    input.value = "";
    if (!file) return;
    if (!/^image\/(png|jpeg|webp)$/.test(file.type)) throw new Error("请选择 PNG、JPEG 或 WebP 图片。");
    if (file.size > MAX_UPLOAD_BYTES) throw new Error("参考图不能超过 10 MB。");
    ext.toast("正在上传参考图…");
    view.config = await ext.api("reference", { method: "POST", body: { persona_id: personaId, image: await readFile(file) } });
    ext.toast("参考图已添加");
    render();
  }

  function testSection() {
    const result = view.test;
    const used = result?.persona_references ? ` · 带了 ${e(result.persona_references)} 张人格参考图` : "";
    const personas = view.config.persona_list || [];
    const who = personas.length ? ui.select("persona_id", "画谁", [["", "不加人格外貌"], ...personas.map(item => [item.id, `${item.name}的自拍`])], view.testPersona) : "";
    const preview = result?.image ? `<figure class="imagegen-preview"><img src="${e(result.image)}" alt="测试生成的图片"><figcaption class="help-caption">用时 ${e(Math.round((result.elapsed_ms || 0) / 100) / 10)} 秒 · ${e(Math.round((result.bytes || 0) / 1024))} KB${used}</figcaption></figure>` : "";
    return `<section class="panel settings-section"><h2>测试生图</h2><p>用当前服务画一张，确认地址、密钥和模型能用；「画谁」选一个人格时，按她自拍的方式加上外貌和参考图。测试不占任何人的次数，但服务商照常计费。</p><form data-form="test"><div class="form-grid">${ui.textarea("prompt", "画面描述", view.testPrompt, "", 'maxlength="1500" rows="3" required')}${ui.select("aspect", "比例", aspects, view.testAspect)}${who}</div><div class="section-actions"><button type="submit" class="btn primary">生成测试图</button></div></form>${preview}</section>`;
  }

  function serviceForm(item) {
    const kind = item.kind, saved = Boolean(item.id);
    let body = ui.field("name", "服务名称", item.name || kinds[kind], "text", "", 'maxlength="60" data-full')
      + ui.field("endpoint", "接口地址", item.endpoint || endpoints[kind], "url", kind === "openai" ? "填到 /v1 为止，例如 https://api.openai.com/v1；会请求 /images/generations 和 /images/edits。" : kind === "gemini" ? "例如 https://generativelanguage.googleapis.com/v1beta；会请求 /models/模型:generateContent。" : kind === "novelai" ? "官方地址是 https://image.novelai.net。" : "ComfyUI 的地址，例如 http://127.0.0.1:8188。", 'required maxlength="2048" data-full')
      + ui.field("api_key", kind === "comfyui" ? "访问令牌（可选）" : "API 密钥", "", "password", item.api_key_set ? "已保存密钥；留空继续使用，页面不会回显。" : kind === "comfyui" ? "ComfyUI 前面有鉴权代理时才需要，会以 Bearer 方式发送。" : "填写这个服务的密钥。", 'autocomplete="new-password" spellcheck="false" data-full')
      + (item.api_key_set ? ui.toggle("clear_api_key", "删除已保存的密钥", "勾选并保存后删除；留空输入框不会删除密钥。", false) : "")
      + ui.field("model", kind === "comfyui" ? "名称标记（可选）" : "模型名称", item.model || (kind === "openai" ? "gpt-image-1" : kind === "novelai" ? "nai-diffusion-4-5-full" : ""), "text", kind === "gemini" ? "例如 gemini-2.5-flash-image。" : kind === "comfyui" ? "模型由工作流里的节点决定，这里只用来标记。" : "", `maxlength="200" ${kind === "openai" || kind === "gemini" ? "required" : ""}`)
      + ui.field("timeout_seconds", "等待上限（秒）", item.timeout_seconds ?? 180, "number", "5—600 秒，超过就算失败并退回次数。", 'min="5" max="600" step="1" required')
      + ui.textarea("negative_prompt", "负面提示词", item.negative_prompt || "", kind === "openai" || kind === "gemini" ? "这类模型没有独立的负面字段，默认不发送；打开下面的开关才会写成「Avoid: …」附在描述后面。" : "不想出现的内容，例如 lowres, bad anatomy, watermark。", 'maxlength="2000" rows="2"');
    if (kind === "openai" || kind === "gemini") body += ui.toggle("natural_negative", "把负面提示词写进描述", "有的模型看到「不要画 X」反而会画出 X，按需开启。", item.natural_negative === true);
    if (kind === "openai") body += ui.select("quality", "画质", [["", "默认"], ["auto", "auto"], ["low", "low"], ["medium", "medium"], ["high", "high"], ["standard", "standard（DALL·E 3）"], ["hd", "hd（DALL·E 3）"]], item.quality || "");
    if (kind === "novelai") body += `<div class="form-grid">${ui.field("steps", "采样步数", item.steps ?? 28, "number", "", 'min="1" max="100" step="1"')}${ui.field("scale", "提示词引导值", item.scale ?? 5, "number", "", 'min="0" max="100" step="0.1"')}${ui.field("sampler", "采样器", item.sampler || "k_euler_ancestral", "text", "", 'maxlength="60"')}${ui.field("strength", "改图强度", item.strength ?? .6, "number", "0—1，越大越不像原图。", 'min="0" max="1" step="0.05"')}</div>`;
    if (kind !== "comfyui") {
      body += ui.toggle("reference", "支持参考图改图", kind === "openai" ? "使用 /images/edits；DALL·E 3 和不少中转站不支持，不支持就关掉。" : kind === "novelai" ? "使用 img2img，只取第一张参考图。" : "把对方发的图一起交给模型。", item.reference !== false)
        + personaToggle(kind, item)
        + `<details class="details"><summary>高级 · 附加请求参数</summary>${ui.textarea("extra_body", "附加请求参数（JSON）", item.extra_body && Object.keys(item.extra_body).length ? JSON.stringify(item.extra_body, null, 2) : "", "会合并进请求体，不能覆盖模型、提示词和参考图字段。", 'rows="4" spellcheck="false"')}</details>`;
    } else {
      const fields = item.workflow_fields || {};
      body += ui.textarea("workflow", "API 工作流（JSON）", item.workflow && Object.keys(item.workflow).length ? JSON.stringify(item.workflow, null, 2) : "", "在 ComfyUI 里用「导出（API）」得到的 JSON，不是普通的工作流保存文件。", 'rows="8" spellcheck="false" required')
        + `<p class="help-caption">把提示词等参数映射到工作流节点的输入，写成 节点ID.inputs.输入名，例如 6.inputs.text。没映射的输入沿用工作流里的值；映射了参考图才支持改图。</p><div class="form-grid">${mappings.map(([key, title]) => ui.field("map_" + key, title, fields[key] || "", "text", "", `maxlength="200" ${key === "prompt" ? 'required placeholder="6.inputs.text"' : ""}`)).join("")}${ui.field("output_node", "输出节点 ID（可选）", item.output_node || "", "text", "留空时取第一个有图片的输出。", 'maxlength="40"')}</div>`
        + personaToggle(kind, item);
    }
    ext.dialog(saved ? "编辑生图服务" : "添加 " + kinds[kind], saved ? kinds[kind] + "；改完可以在页面底部测试生图。" : "保存后可以在页面底部测试生图；第一个添加的服务会自动设为当前使用。", body,
      data => saveService(item, data), saved ? "保存服务" : "添加服务");
  }

  function personaToggle(kind, item) {
    const multi = kind === "openai" || kind === "gemini";
    const help = multi ? "角色画自己时，把人格参考图和对方发的图一起交给模型（最多 3 张），帮助保持长相。需要同时打开上面的改图开关。"
      : kind === "novelai" ? "NovelAI 用 img2img，带上参考图后成品会很像那张图本身（构图、姿势都接近），一般不建议打开。对方发了图时只用对方的。"
      : "只有工作流用参考图来保持人物（例如 IPAdapter）时才打开；普通的图生图会照着参考图的构图画。对方发了图时只用对方的。";
    return ui.toggle("persona_reference", "自拍时带上人格参考图", help, item.persona_reference ?? multi);
  }

  async function saveService(record, data) {
    const serviceKind = record.kind;
    const item = { ...plain(record), kind: serviceKind, name: data.name, endpoint: data.endpoint, model: data.model || "",
      timeout_seconds: number(data.timeout_seconds, "等待上限"), negative_prompt: data.negative_prompt || "" };
    if (data.api_key) item.api_key = data.api_key;
    if (data.clear_api_key === true) item.clear_api_key = true;
    if (serviceKind === "openai" || serviceKind === "gemini") item.natural_negative = data.natural_negative === true;
    if (serviceKind === "openai") item.quality = data.quality || "";
    if (serviceKind === "novelai") Object.assign(item, { steps: number(data.steps, "采样步数"), scale: number(data.scale, "提示词引导值"), sampler: data.sampler, strength: number(data.strength, "改图强度") });
    item.persona_reference = data.persona_reference === true;
    if (serviceKind !== "comfyui") Object.assign(item, { reference: data.reference === true, extra_body: json(data.extra_body, "附加请求参数") });
    else {
      item.workflow = json(data.workflow, "API 工作流");
      item.workflow_fields = Object.fromEntries(mappings.map(([key]) => [key, String(data["map_" + key] || "").trim()]).filter(([, value]) => value));
      item.output_node = String(data.output_node || "").trim();
    }
    const list = record.id ? services().map(x => x.id === record.id ? item : plain(x)) : [...services().map(plain), item];
    const first = !view.config.service_id && !record.id;
    view.config = await ext.api("config", { method: "PUT", body: { services: list } });
    if (first) {
      const added = services().at(-1);
      if (added) view.config = await ext.api("config", { method: "PUT", body: { service_id: added.id } });
    }
    ext.toast(record.id ? "生图服务已保存" : "生图服务已添加");
    render();
  }

  async function action(name, target) {
    if (name === "add") return serviceForm({ kind: target.dataset.kind });
    if (name === "remove-reference") {
      const persona = (view.config.persona_list || []).find(x => x.id === target.dataset.persona);
      return ext.confirm("移除这张参考图？", `${persona ? persona.name : "这个人格"}以后自拍不再参考它，图片文件会一起删除。`, async () => {
        view.config = await ext.api("reference", { method: "DELETE", body: { persona_id: target.dataset.persona, id: target.dataset.id } });
        ext.toast("参考图已移除");
        render();
      }, "移除");
    }
    const item = services().find(x => x.id === target.dataset.id);
    if (!item) throw new Error("请刷新页面后重试。");
    if (name === "edit") return serviceForm(item);
    if (name === "use") {
      view.config = await ext.api("config", { method: "PUT", body: { service_id: item.id } });
      ext.toast(`已改用「${item.name}」`);
      return render();
    }
    if (name === "delete") {
      const current = view.config.service_id === item.id;
      ext.confirm(`删除「${item.name}」？`, current ? "这是当前使用的服务，删除后角色不再画图，直到选择另一个服务。" : "删除后它保存的密钥也会一起删除。", async () => {
        view.config = await ext.api("config", { method: "PUT", body: { services: services().filter(x => x.id !== item.id).map(plain), ...(current ? { service_id: "" } : {}) } });
        ext.toast("生图服务已删除");
        render();
      }, "删除服务");
    }
  }

  async function submit(kind, form) {
    const data = ui.values(form);
    if (kind === "personas") {
      const personas = {};
      for (const persona of view.config.persona_list || []) {
        const prompt = String(data["prompt__" + persona.id] || "").trim(), negative = String(data["negative__" + persona.id] || "").trim();
        if (prompt || negative) personas[persona.id] = { prompt, negative_prompt: negative };
      }
      view.config = await ext.api("config", { method: "PUT", body: { personas } });
      ext.toast("人格外貌已保存");
      return render();
    }
    if (kind === "test") {
      view.testPrompt = data.prompt; view.testAspect = data.aspect; view.testPersona = data.persona_id || "";
      const button = form.querySelector('[type="submit"]');
      if (button) { button.disabled = true; button.textContent = "正在生成…"; }
      ext.toast("正在生成测试图，可能要几十秒…");
      try {
        view.test = await ext.api("test", { method: "POST", body: { prompt: data.prompt, aspect: data.aspect, ...(data.persona_id ? { persona_id: data.persona_id } : {}) } });
        ext.toast("测试图已生成");
      } finally {
        render();
      }
    }
  }

  root.addEventListener("click", async event => {
    const target = event.target.closest("[data-action]");
    if (!target) return;
    try { await action(target.dataset.action, target); } catch (exc) { ext.toast(exc.message || String(exc), true); }
  });
  root.addEventListener("change", async event => {
    const input = event.target.closest("input[data-upload]");
    if (!input) return;
    try { await upload(input); } catch (exc) { ext.toast(exc.message || String(exc), true); }
  });
  root.addEventListener("submit", async event => {
    const form = event.target.closest("form[data-form]");
    if (!form) return;
    event.preventDefault();
    try { await submit(form.dataset.form, form); } catch (exc) { ext.toast(exc.message || String(exc), true); }
  });

  ext.api("state").then(config => { view.config = config; render(); })
    .catch(exc => { root.innerHTML = `<section class="panel settings-section"><h2>生图</h2><p role="alert">${e(exc.message || String(exc))}</p></section>`; });
})();
