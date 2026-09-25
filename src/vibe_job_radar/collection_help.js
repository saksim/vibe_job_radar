"use strict";
// Local field guidance only. Presets never supply URLs, credentials or authorization.
window.CollectionGuide = class {
  constructor({api, note, act, chosen, setChecks, platforms, keyConfigured}) {
    Object.assign(this, {api, note, act, chosen, setChecks, platforms, keyConfigured});
    this.form = document.getElementById("collect-form");
    this.mode = this.form.elements.mode.value;
    this.locked = false;
    this.categories = {
      architect: {name: "架构师", roles: ["architect"], url: "https://www.liepin.com/career/360321/"},
      algorithm: {name: "算法工程师", roles: ["domain_algorithm", "time_series"], url: "https://www.liepin.com/career/suanfakaifa/"}
    };
    this.fieldModes = {
      category_id: ["liepin_category"],
      api_key: ["search", "feed"], urls: ["urls"], endpoint: ["feed"], contract_ref: ["feed"],
      search_budget: ["search"], pages: ["search"], detail_budget: ["urls", "search", "liepin_category"],
      feed_budget: ["feed"], fresh_hours: ["urls", "search", "liepin_category"], search_storage_rights: ["search"]
    };
    this.build(); this.sync();
    this.form.elements.mode.addEventListener("change", () => this.changeMode());
    this.form.elements.detail_budget.addEventListener("input", () => this.sync());
    this.form.elements.category_id.addEventListener("change", () => {
      this.selectCategory(); this.sync(); this.result.replaceChildren();
      this.note("分类已切换，请重新核对用途、许可和本批预算；尚未执行。");
    });
    document.getElementById("collect-check").onclick = () => this.act(() => this.inspect());
  }
  node(tag, value, parent) {
    const n = document.createElement(tag); n.textContent = value;
    if (parent) parent.append(n); return n;
  }
  link(parent, label, href) {
    const a = this.node("a", label, parent); a.href = href;
    if (href.startsWith("https://")) { a.target = "_blank"; a.rel = "noopener noreferrer"; }
    return a;
  }
  action(parent, label, fn) {
    const b = this.node("button", label, parent); b.type = "button";
    b.addEventListener("click", () => this.act(fn)); return b;
  }
  build() {
    const root = document.getElementById("collection-guide"); root.className = "guide-box";
    this.node("h3", "不知道从哪里填？先选你手里有什么", root);
    this.node("p", "有具体职位链接 → URL 路线；希望程序帮你找链接 → 搜索路线；数据供应方已经给了接口 → 数据源路线。不是所有字段都要填。", root);
    this.action(root, "我有职位链接：套用 URL 入门参数", () => this.preset("urls"));
    this.action(root, "我想先搜索：套用搜索入门参数", () => this.preset("search"));
    this.action(root, "自动读取猎聘架构师公开分类", () => this.preset("liepin_category"));
    this.action(root, "自动读取猎聘算法工程师公开分类", () => this.preset("liepin_category", "algorithm"));
    this.link(root, "没有链接也没有 Key：返回基础页粘贴 JD", "/#job-form");
    this.node("p", "入门按钮只调整岗位、平台和小预算，不联网，不填假链接或密钥，不替你勾选授权。原有 URL、用途说明会保留；跨路线切换会清空服务 Key。", root);
    this.panels = {};
    for (const [mode, heading] of [["urls", "案例 A｜我有一个职位链接，无需任何 API Key"],
                                  ["search", "案例 B｜让程序找职位，先只取搜索摘要"],
                                  ["liepin_category", "猎聘公开分类｜无需 Key，最多5个职位"],
                                  ["feed", "案例 C｜仅限已经有数据提供方接口的用户"]]) {
      const panel = this.node("div", "", root); panel.id = `guide-${mode}`;
      this.panels[mode] = panel; this.node("h3", heading, panel);
    }
    let p = this.panels.urls;
    const ol = this.node("ol", "", p);
    this.node("li", "在招聘网站搜索“时间序列算法工程师”，点进一个具体职位；优先选择能独立打开的职位详情页。", ol);
    this.node("li", "浏览器点地址栏 → Ctrl+L → Ctrl+C，把复制的完整 HTTPS 地址粘到下方“职位 URL 列表”。每行一个；第一次只放一个。", ol);
    this.node("li", "App 中可用分享/复制链接，再用浏览器打开确认是该职位。只有小程序口令、短链跳转失败或没有网页地址时，改用基础页粘贴正文。", ol);
    this.node("li", "点击“从链接识别来源平台”，核对真实访问范围，再点“检查填写”。只有本人确有依据时才勾选正文许可与执行确认。", ol);
    const siteLinks = this.node("p", "招聘入口：", p);
    this.link(siteLinks, "BOSS直聘", "https://www.zhipin.com/"); this.node("span", " / ", siteLinks);
    this.link(siteLinks, "猎聘", "https://www.liepin.com/"); this.node("span", " / ", siteLinks);
    this.link(siteLinks, "前程无忧", "https://www.51job.com/");
    this.node("p", "这些是找职位的入口，不是可填进 URL 列表的具体职位地址。格式示意（不能直接提交）：", p);
    this.node("code", "https://www.zhipin.com/job_detail/REPLACE_WITH_REAL_JOB_ID.html", p);
    this.node("p", "URL 来自你实际打开的职位，不是自己拼出来的；不要填招聘首页、列表页、本机 127.0.0.1/advanced 或 D: 文件路径。", p);
    this.node("p", "入门参数：目标岗位=时间序列；平台=链接对应来源；正文预算=1；复用窗口=24小时。Key、搜索预算、数据源地址和契约全部不用填。", p);
    this.node("p", "授权说明参考格式（仅在确实成立时填写）：来源为【实际页面/接口】；依据【允许自动访问的条款或提供方授权】；仅用于本人岗位要求研究，最多获取1个职位，不公开转发原文。没有这样的依据，不要照抄，改用获准文本输入。", p);

    p = this.panels.search;
    const steps = this.node("ol", "", p);
    const account = this.node("li", "打开 ", steps);
    this.link(account, "Brave Search API 控制台", "https://api-dashboard.search.brave.com/");
    this.node("span", "，注册/登录并验证邮箱。", account);
    this.node("li", "在 Plans 中启用支持 Web Search 的 Search 套餐。按控制台提示处理支付资料与用量限制，核对保存搜索结果的权限；价格和赠送额度以官方当前页面为准。", steps);
    this.node("li", "进入 API Keys → Add API Key（或创建密钥），名称可写 vibe-job-radar，复制生成的 Key。", steps);
    this.node("li", "回本页面，仅粘贴到“Brave Search API Key”。不要填写账号密码、API网址、招聘平台密码或大模型服务的Key。不要发给聊天、Issue或仓库。", steps);
    this.node("li", "先用1个平台、1个岗位、搜索预算=1、每任务页数=1、正文预算=0。核对用途和套餐保存权限后检查填写，再明确点击创建执行。", steps);
    this.node("p", "这次无需职位 URL、数据源地址、契约链接或任何正文访问许可。检索词由岗位和工具词配置自动组合，不需要自己编 API 参数。", p);
    this.node("p", "用途说明参考句（需符合自己的实际情况）：个人求职研究；只保存本人套餐允许保存的搜索结果；本次仅获取标题、链接和摘要，不自动访问招聘网站详情正文。", p);
    this.node("p", "正文预算0意味着没有完整JD；拿到链接/摘要后仍需补齐获准正文。无Key时“检查填写”仍能展示计划，但不能执行在线搜索。", p);
    this.keyNote = this.node("p", "", p);
    this.link(p, "官方创建 Key 步骤（2026-09-14核对）", "https://api-dashboard.search.brave.com/documentation/quickstart");

    p = this.panels.liepin_category;
    this.categoryLink = this.link(p, "查看猎聘原始架构师分类", "https://www.liepin.com/career/360321/");
    this.node("p", "自动读取该分类第一页，按发布方顺序选择前1～5个不同职位，再取得完整正文并生成本批报告。失败项保留，不以后面的职位补齐。", p);
    this.categoryScope = this.node("p", "", p);
    this.node("p", "核对分类页及正文的实际访问依据，勾选猎聘许可和执行确认后开始。无需账号、搜索Key或手工收集职位链接。", p);

    p = this.panels.feed;
    this.node("p", "只有公司、招聘平台或数据供应商已经提供可用接口时才选这项。没有接口的人不需要去编一个，也不用填写所谓通用招聘API。", p);
    this.node("p", "向提供方索取：HTTPS JSON服务地址、只读Token（需要认证时）、接口/授权说明、允许用途。返回格式须符合项目的 jobs 数组与 next_cursor 契约；普通招聘网页或别家API不自动兼容。", p);
    this.node("p", "没有上述资料，请切换 URL / 搜索路线；数据源地址与契约只属于这条路线，不是所有用户的必填项。", p);

    const hints = {
      api_key: "来源：自己的服务控制台。只用于本次操作，不写入采集任务；不是职位链接。",
      urls: "来源：具体职位详情页的地址栏或可打开的分享链接。每行一个；先用1条。",
      endpoint: "来源：真实数据供应方提供的接口。没有接口就换路线，不填招聘首页。",
      contract_ref: "来源：该数据提供方的接口文档/授权说明链接，不是随便找一篇网页。",
      search_budget: "最多调用搜索API多少次，不是职位条数。首次填1，熟悉后再增加。",
      pages: "每组检索词最多翻几页；首次填1。总调用仍受上面的搜索预算限制。",
      detail_budget: "最多尝试多少条正文。URL首次填1；搜索首次填0=只发现摘要。一次尝试可能含robots请求。",
      feed_budget: "接口最多获取多少页；仅数据源路线使用。首次填1。",
      fresh_hours: "保留24即可：同一链接24小时内已有新鲜正文时优先复用，减少重复请求。",
      rights_note: "填写真实来源、用途和允许使用依据，不是密码或API地址。参考本路线案例；示例不构成授权。"
    };
    for (const [name, value] of Object.entries(hints)) {
      const input = this.form.elements[name]; input.id ||= `collect-field-${name}`;
      const hint = this.node("small", value, input.closest("label")); hint.className = "field-help";
      hint.id = `help-${name}`; input.setAttribute("aria-describedby", hint.id);
    }
    this.form.elements.urls.placeholder = "粘贴你实际打开的职位详情页 HTTPS 地址，每行一个（不要粘贴上方格式示例）";
    this.form.elements.rights_note.placeholder = "说明实际来源/许可依据/用途；可参考上方对应案例，不要编造授权";
    const urlLabel = this.form.elements.urls.closest("label");
    this.action(urlLabel, "从链接识别来源平台（不授予权限）", async () => {
      const result = await this.api("/api/collection/preview", this.data());
      if (result.detected_platforms.length) {
        this.setChecks("collect-platforms", result.detected_platforms);
        this.setChecks("collect-permits", []);
        this.note("已根据域名勾选来源；正文访问许可没有自动勾选，请自行确认真实依据。不要直接复制示例链接。");
      }
      await this.inspect();
    });
    this.result = document.getElementById("collect-check-results");
    this.form.addEventListener("input", () => {
      if (this.result.childElementCount) this.result.replaceChildren(this.node("p", "输入已修改；请重新检查填写，旧预检不再作为执行依据。"));
    });
  }
  changeMode() {
    const next = this.form.elements.mode.value;
    if (next !== this.mode) {
      this.form.elements.api_key.value = "";
      this.form.elements.consent.checked = false;
      this.form.elements.search_storage_rights.checked = false;
      if (next === "liepin_category") {
        this.selectCategory();
        this.form.elements.detail_budget.value = "5";
      }
      this.mode = next;
    }
    this.sync();
  }
  selectMode(mode) { this.form.elements.mode.value = mode; this.changeMode(); }
  selectCategory() {
    const category = this.categories[this.form.elements.category_id.value];
    this.setChecks("collect-roles", category.roles);
    this.setChecks("collect-platforms", ["liepin"]);
    this.setChecks("collect-permits", []);
    this.form.elements.consent.checked = false;
  }
  sync(locked = this.locked) {
    this.locked = locked;
    const mode = this.form.elements.mode.value;
    const category = this.categories[this.form.elements.category_id.value];
    this.categoryLink.textContent = `查看猎聘原始${category.name}分类`;
    this.categoryLink.href = category.url;
    this.categoryScope.textContent = `范围为猎聘${category.name}分类，不含自定义关键词、地区筛选或翻页。` +
      (category.name === "算法工程师" ? "算法工程师是宽分类；报告按垂直领域与时间序列规则筛选，不匹配正文单独说明。" : "列表卡片不作为完整JD；只显示本次实得结果。");
    const noDetail = mode === "search" && Number(this.form.elements.detail_budget.value) === 0;
    for (const [name, modes] of Object.entries(this.fieldModes)) {
      const input = this.form.elements[name];
      const visible = modes.includes(mode) && !(name === "fresh_hours" && noDetail);
      input.closest("label").hidden = !visible; input.disabled = locked || !visible;
    }
    for (const name of ["mode", "rights_note", "consent"]) this.form.elements[name].disabled = locked;
    const permitBox = document.getElementById("collect-permission-fields");
    permitBox.hidden = mode === "feed" || noDetail;
    for (const i of document.querySelectorAll("#collect-permits input")) i.disabled = locked || permitBox.hidden;
    for (const i of document.querySelectorAll("#collect-roles input, #collect-platforms input")) i.disabled = locked || mode === "liepin_category";
    this.form.elements.detail_budget.max = mode === "liepin_category" ? "5" : "300";
    this.form.elements.detail_budget.min = mode === "liepin_category" ? "1" : "0";
    this.form.elements.api_key.closest("label").firstChild.textContent = mode === "feed" ? "数据提供方的 Bearer Token（按接口要求）" : "Brave Search API Key（从官方控制台取得）";
    for (const [key, panel] of Object.entries(this.panels)) panel.hidden = key !== mode;
    this.keyNote.textContent = this.keyConfigured ? "启动环境中已发现 BRAVE_SEARCH_API_KEY，可将输入框留空使用它；尚未验证Key有效性。" : "当前启动环境未配置搜索Key：在输入框粘贴即可，无需重启、改代码或配置环境变量。";
  }
  preset(mode, categoryId = "architect") {
    this.form.elements.category_id.value = categoryId;
    this.selectMode(mode);
    const f = this.form.elements;
    this.setChecks("collect-roles", mode === "liepin_category" ? this.categories[categoryId].roles : ["time_series"]);
    this.setChecks("collect-platforms", [mode === "liepin_category" ? "liepin" : "boss"]);
    this.setChecks("collect-permits", []);
    Object.assign(f.search_budget, {value: "1"}); f.pages.value = "1";
    f.detail_budget.value = mode === "search" ? "0" : (mode === "liepin_category" ? "5" : "1");
    f.fresh_hours.value = "24"; f.feed_budget.value = "1";
    f.consent.checked = false; f.search_storage_rights.checked = false;
    this.sync(); this.result.replaceChildren();
    this.note(mode === "liepin_category" ? `已选择猎聘${this.categories[categoryId].name}公开分类，最多5个职位。请核对用途与许可；尚未联网或创建任务。` : (mode === "urls" ? "已套用URL入门参数。接下来只需粘贴1条真实职位链接、识别来源、核对许可与用途；没有填入假链接，也没有发请求。" : "已套用搜索入门参数：1岗位/1平台/1请求，只取摘要。接下来去Brave控制台取得Key并核对保存权限；未开始搜索。"));
  }
  data() {
    // Explicit construction: disabled/hidden fields must not become NaN or leak a different provider key.
    const f = this.form.elements, d = {mode: f.mode.value, roles: this.chosen("collect-roles"),
      platforms: this.chosen("collect-platforms"), rights_note: f.rights_note.value, consent: f.consent.checked,
      permit_platforms: document.getElementById("collect-permission-fields").hidden ? [] : this.chosen("collect-permits")};
    for (const [name, modes] of Object.entries(this.fieldModes)) if (modes.includes(d.mode) && !f[name].closest("label").hidden) {
      if (name === "search_storage_rights") d[name] = f[name].checked;
      else d[name] = f[name].type === "number" ? (f[name].value === "" ? null : Number(f[name].value)) : f[name].value;
    }
    return d;
  }
  async inspect() {
    const result = await this.api("/api/collection/preview", this.data()); this.render(result); return result;
  }
  render(result) {
    const root = this.result; root.replaceChildren(); root.className = "guide-box";
    this.node("h3", result.ready ? "填写检查通过（不是实站认证）" : "还缺什么？按字段处理下面几项", root);
    this.node("p", "本次检查：外部网络请求0、创建任务0；不会验证Key是否有效，不会自动登录招聘网站。", root);
    for (const error of result.errors) {
      const p = this.node("p", `${error.label}：${error.message}`, root); p.dataset.field = error.field;
    }
    if (result.unique_url_count) this.node("p", `已识别 ${result.unique_url_count} 个不同职位链接；来源：${result.url_rows.map(r => r.label).filter((v,i,a)=>a.indexOf(v)===i).join("、")}。识别不等于允许抓取。`, root);
    if (result.category_url) {
      this.link(root, "本次公开分类入口", result.category_url);
      this.node("p", `分类读取最多1次，最多选择 ${result.budgets.detail_budget} 个职位。每次访问还可能包含 robots 或跳转请求；预检尚未读取列表。`, root);
    }
    if (result.query_preview.length) {
      this.node("h3", `将如何检索：共 ${result.query_count} 组候选词，实际受 ${result.budgets.search_budget} 次请求预算限制`, root);
      this.node("pre", result.query_preview.join("\n"), root);
    }
    for (const warning of result.warnings) this.node("p", warning, root);
    this.node("p", result.message, root); this.note(result.message);
  }
};
