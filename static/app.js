"use strict";
const $ = id => document.getElementById(id);
const statusNames = {queued:"В очереди",running:"Выполняется",completed:"Завершено",cancelled:"Остановлено",failed:"Ошибка",interrupted:"Прервано"};
const depthNames = {brief:"Краткий",standard:"Стандартный",deep:"Подробный"};
let currentPage = "new", sourceType = "arxiv", selectedJob = null, paperFilter = null;
let jobs = [], detailKey = "", jobListKey = "", paperListKey = "", busy = false, noticeTimer;
let profileId = null, profileKey = "", demoSeconds = 3, health = null;
let libraryShortcutsKey = "";
const profileDisclosures = new Map();
const el = (tag, text, cls) => {const node=document.createElement(tag); if(text!==undefined) node.textContent=text; if(cls) node.className=cls; return node;};
const date = value => new Date(value).toLocaleString("ru-RU", {day:"2-digit",month:"short",hour:"2-digit",minute:"2-digit"});
function notify(message, error=false){$("notice").textContent=message;$("notice").className=error?"error":"";$("notice").hidden=false;clearTimeout(noticeTimer);noticeTimer=setTimeout(()=>$("notice").hidden=true,error?12000:5000);}
async function api(path, body){
  const options = body === undefined ? {} : {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)};
  const response = await fetch(path, options);
  const data = await response.json();
  if(!response.ok) throw new Error(data.error || "Ошибка сервера");
  return data;
}
function navigate(){
  const path = location.hash.slice(1) || "new";
  const match = /^paper\/([a-f0-9]{32})$/.exec(path);
  currentPage = match ? "paper" : path;
  if(!["new","jobs","library","settings","paper"].includes(currentPage) || (currentPage==="paper" && !match)) currentPage="new";
  if (profileId !== (match?.[1] ?? null)) {
    profileId = match?.[1] ?? null;
    profileKey = "";
    $("paper-profile").replaceChildren(el("p", "Загрузка профиля…", "hint"));
  }
  document.querySelectorAll(".page").forEach(node=>node.hidden=node.id!=="page-"+currentPage);
  const navPage = currentPage === "paper" ? "library" : currentPage;
  document.querySelectorAll("nav a[data-page]").forEach(node=>{const active=node.dataset.page===navPage;node.classList.toggle("active",active);if(active)node.setAttribute("aria-current","page");else node.removeAttribute("aria-current");});
  markLibraryShortcut();
  refresh();
}
document.querySelectorAll("[data-source]").forEach(button=>button.addEventListener("click",()=>{
  sourceType=button.dataset.source;
  document.querySelectorAll("[data-source]").forEach(item=>{item.classList.toggle("selected",item===button);item.setAttribute("aria-pressed",String(item===button));});
  $("arxiv-input").hidden=sourceType!=="arxiv";$("pdf-input").hidden=sourceType!=="pdf";
}));
function fileData(file){return new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(",")[1]);reader.onerror=()=>reject(new Error("Не удалось прочитать файл."));reader.readAsDataURL(file);});}
function renderMode() {
  const demo = $("mode").value === "demo";
  $("demo-options").hidden = !demo;
  $("processing-note").hidden = demo;
  if (!$("start-button").disabled) $("start-button").textContent = demo ? "Запустить демонстрацию →" : "Подготовить профиль →";
  $("duration").textContent = demo ? `Около ${demoSeconds * 5} секунд` : "Результат появится в библиотеке";
  const messages = [];
  if (health && !health.api_configured) messages.push("Задайте OPENAI_API_KEY и перезапустите сервер, чтобы подключить модель.");
  if (health && !health.pdf_configured) messages.push("Установите pypdf для извлечения текста PDF.");
  $("setup-state").textContent = demo ? "" : messages.join(" ");
}
$("mode").addEventListener("change", renderMode);
function showJob(job) {
  selectedJob = job.id;
  paperFilter = null;
  detailKey = "";
  location.hash = "jobs";
  notify("Задание добавлено в очередь.");
  refresh();
}
$("new-form").addEventListener("submit",async event=>{
  event.preventDefault(); const button=$("start-button");button.disabled=true;button.textContent="Сохранение…";
  try{
    const body={source:$("source").value,questions:$("questions").value,depth:$("depth").value,mode:$("mode").value};
    if (body.mode === "demo") body.scenario = $("scenario").value;
    if(sourceType==="pdf"){
      const file=$("pdf").files[0];if(!file)throw new Error("Выберите PDF.");if(file.size>15*1024*1024)throw new Error("Максимальный размер PDF — 15 МБ.");
      body.pdf={name:file.name,data:await fileData(file)};
    }
    showJob(await api("/api/jobs", body));
  }catch(error){notify(error.message,true);}finally{button.disabled=false;renderMode();}
});
function badge(status){return el("span",statusNames[status]||status,"badge "+status);}
function empty(title,subtitle){const div=el("div",undefined,"empty");div.append(el("span","◎"),el("h2",title),el("p",subtitle));return div;}
function renderJobs(){
  const active=jobs.filter(job=>["running","queued"].includes(job.status)).length;
  const completed=jobs.filter(job=>job.status==="completed").length;
  $("queue-count").textContent=active;$("stat-active").textContent=active;$("stat-complete").textContent=completed;$("stat-other").textContent=jobs.length-active-completed;
  const filtered=paperFilter?jobs.filter(job=>job.paper_id===paperFilter.id):jobs;
  if (!filtered.length) selectedJob = null;
  $("library-filter").hidden=!paperFilter;if(paperFilter)$("library-filter").querySelector("span").textContent="Материал: "+paperFilter.title;
  if(filtered.length&&!filtered.some(job=>job.id===selectedJob)){selectedJob=filtered[0].id;detailKey="";}
  const key=JSON.stringify([selectedJob,filtered.map(job=>[job.id,job.title,job.status])]);
  if(key===jobListKey)return;jobListKey=key;
  const list=$("job-list"), focusedId=list.contains(document.activeElement)?document.activeElement.dataset.jobId:null;
  list.replaceChildren();
  if(!filtered.length){list.append(empty("Пока нет заданий","Начните с нового разбора статьи."));return;}
  for(const job of filtered){
    const button=el("button",undefined,"job-item"+(job.id===selectedJob?" selected":""));button.type="button";button.dataset.jobId=job.id;button.setAttribute("aria-pressed",String(job.id===selectedJob));
    button.append(el("strong",job.title),badge(job.status),el("small",date(job.created)+" · "+job.id.slice(0,8)+(job.mode === "demo" ? " · Демо" : "")));
    button.addEventListener("click",()=>{selectedJob=job.id;detailKey="";renderJobs();refresh();});list.append(button);
    if(focusedId===job.id)button.focus({preventScroll:true});
  }
}
function link(label, href, className, external = false) {
  const anchor = el("a", label, className);
  anchor.href = href;
  if (external) {
    anchor.target = "_blank";
    anchor.rel = "noopener noreferrer";
  }
  return anchor;
}
function apiUsage(usage) {
  if (!usage.calls) return "Вызовы API: 0";
  const unknown = usage.calls - usage.reported_calls;
  return `${usage.model} · вызовов: ${usage.calls} · вход: ${usage.input_tokens} · выход: ${usage.output_tokens} токенов`
    + (unknown ? ` · нет данных о токенах по ${unknown} вызовам` : "");
}
async function renderDetail(){
  if(!selectedJob){$("job-detail").replaceChildren(empty("Выберите задание","Здесь появятся этапы, настройки и результат."));return;}
  const requested=selectedJob, job=await api("/api/jobs/"+requested);
  if(requested!==selectedJob)return;
  const key=job.id+job.updated;if(detailKey===key)return;detailKey=key;
  const panel=$("job-detail");const expanded=panel.querySelector("details")?.open;const reportScroll=panel.querySelector(".report")?.scrollTop||0;panel.replaceChildren();
  const heading=el("div",undefined,"detail-heading");heading.append(el("h2",job.title),badge(job.status));panel.append(heading,el("div",date(job.created)+" · № "+job.id.slice(0,8),"subtle"));
  panel.append(el("div",job.stage+" · "+job.progress+"%","stage-line"));
  const progress=el("progress",undefined,"progress");progress.max=100;progress.value=job.progress;progress.setAttribute("aria-label","Ход обработки");panel.append(progress);
  const actions=el("div",undefined,"detail-actions");
  if(["running","queued"].includes(job.status)){
    const cancel=el("button","Остановить задание","secondary danger");cancel.type="button";
    cancel.onclick=async()=>{cancel.disabled=true;try{await api("/api/jobs/"+job.id+"/cancel",{});detailKey="";await refresh();}catch(error){notify(error.message,true);cancel.disabled=false;}};actions.append(cancel);
  }
  if(job.report){const download=el("a","Скачать отчёт .md","secondary");download.href="/api/jobs/"+job.id+"/report";actions.append(download);}
  if (job.mode === "research") actions.append(link("Открыть профиль статьи →", `#paper/${job.paper_id}`, "secondary"));
  panel.append(actions);
  if(job.error)panel.append(el("div",job.error,"error-box"));
  if (job.resolved_version) panel.append(el("p", "Версия arXiv для этого задания: " + job.resolved_version, "subtle"));
  panel.append(el("p", apiUsage(job.api_usage), "subtle"));
  panel.append(el("h2","Журнал этапов"));const events=el("ol",undefined,"events");
  job.events.forEach(event=>{const line=el("li");line.append(el("time",new Date(event.time).toLocaleTimeString("ru-RU")),el("span",event.message));events.append(line);});panel.append(events);
  if(job.report){panel.append(el("h2","Результат · демонстрационный шаблон"),el("pre",job.report,"report"));panel.querySelector(".report").scrollTop=reportScroll;}
  const details=el("details");details.open=Boolean(expanded);details.append(el("summary","Параметры и снимок настроек"));
  const settings=job.settings;details.append(el("div",[
    "Источник: "+job.input_source,"Глубина: "+depthNames[job.depth],
    "Режим: "+(job.mode === "research" ? "Первичная обработка" : "Демонстрация"),
    "Вопросы: "+(job.questions||"Не указаны"),"Темы: "+(settings.interests||"Не заданы"),"Исключения: "+(settings.exclusions||"Не заданы"),
    "Правила: "+settings.rules,"Язык: "+settings.language,"Лимит времени: "+settings.time_limit_seconds+" с",
    job.mode === "research" ? `Модель: ${settings.model} · максимум вызовов: ${settings.max_api_calls} · лимит выхода: ${settings.max_output_tokens} токенов на вызов` : `Длительность этапа: ${settings.stage_seconds} с`
  ].join("\n\n"),"snapshot"));panel.append(details);
}
function renderPapers(papers){
  const list=$("paper-list"), key=JSON.stringify(papers);
  if(key===paperListKey)return;paperListKey=key;list.replaceChildren();
  if(!papers.length){list.append(empty("Библиотека пока пуста","Материалы появятся здесь после создания заданий."));return;}
  for (const paper of papers) {
    const row = el("article", undefined, "card library-row");
    const heading = link(undefined, `#paper/${paper.id}`, "library-row-link");
    const text = el("div");
    text.append(el("span", paper.kind === "pdf" ? "ЛОКАЛЬНЫЙ PDF" : paper.source.replace("arxiv:", "ARXIV · "), "eyebrow"),
      el("h2", paper.title));
    if (paper.authors?.length) text.append(el("p", paper.authors.join(", "), "library-authors"));
    text.append(el("span", `Заданий: ${paper.job_count}`, "subtle"));
    heading.append(text, el("span", paper.has_profile ? "Открыть профиль →" : "Открыть материал →", "library-status"));
    row.append(heading);
    if (paper.has_profile) {
      const abstract = profileDetails(paper.id, "library-abstract", "library-abstract");
      const missing = "Аннотация не найдена в сохранённых материалах.";
      const body = el("p", paper.has_abstract ? "Загрузка…" : missing);
      abstract.append(el("summary", "Аннотация"), body);
      if (paper.has_abstract) {
        let loaded = false;
        abstract.addEventListener("toggle", async () => {
          if (!abstract.open || loaded) return;
          loaded = true;
          try {
            const saved = await api("/api/papers/" + paper.id);
            body.textContent = saved.profile?.article.abstract || missing;
          } catch {
            body.textContent = "Не удалось загрузить аннотацию. Сверните и откройте её снова.";
            loaded = false;
          }
        });
      }
      row.append(abstract);
    }
    list.append(row);
  }
}

function setLibraryShortcuts(open) {
  const toggle = $("library-toggle");
  $("library-shortcuts").hidden = !open;
  toggle.setAttribute("aria-expanded", String(open));
  toggle.setAttribute("aria-label", open ? "Скрыть профили библиотеки" : "Показать профили библиотеки");
  if (open) refresh();
}

function markLibraryShortcut() {
  for (const anchor of $("library-shortcuts").querySelectorAll("a")) {
    if (anchor.hash === location.hash) anchor.setAttribute("aria-current", "page");
    else anchor.removeAttribute("aria-current");
  }
}

function renderLibraryShortcuts(papers) {
  const profiles = papers.filter(paper => paper.has_profile);
  const key = JSON.stringify(profiles.map(paper => [paper.id, paper.title]));
  if (key === libraryShortcutsKey) return;
  libraryShortcutsKey = key;
  const list = $("library-shortcuts");
  const rows = profiles.map(paper => {
    const anchor = link(paper.title, "#paper/" + paper.id);
    anchor.addEventListener("click", () => setLibraryShortcuts(false));
    return anchor;
  });
  list.replaceChildren(...(rows.length ? rows : [el("p", "Подготовленных профилей пока нет.")]));
  markLibraryShortcut();
}

$("library-toggle").addEventListener("click", () => setLibraryShortcuts($("library-shortcuts").hidden));
$("library-shortcuts").addEventListener("keydown", event => {
  if (event.key === "Escape") {
    setLibraryShortcuts(false);
    $("library-toggle").focus();
  }
});

function profileDetails(id, key, className, initiallyOpen = false) {
  const storageKey = "research-agent:profile-sections:" + id;
  if (!profileDisclosures.has(id)) {
    let saved;
    try { saved = JSON.parse(localStorage.getItem(storageKey)); }
    catch { /* The page can still remember disclosures in memory. */ }
    profileDisclosures.set(id, saved && typeof saved === "object" && !Array.isArray(saved) ? saved : {});
  }
  const state = profileDisclosures.get(id);
  const details = el("details", undefined, className);
  details.open = typeof state[key] === "boolean" ? state[key] : initiallyOpen;
  details.addEventListener("toggle", () => {
    if (!details.isConnected) return;
    state[key] = details.open;
    try { localStorage.setItem(storageKey, JSON.stringify(state)); }
    catch { /* Keep the state in memory when storage is unavailable. */ }
  });
  return details;
}

function referenceRow(item, pdfUrl) {
  const row = el("li", undefined, "reference-entry");
  const number = el("span", item.number ? "[" + item.number + "]" : "", "reference-number");
  const body = el("div", undefined, "reference-body");
  const title = item.href ? link(item.label, item.href, "reference-title", true) : el("strong", item.label, "reference-title");
  body.append(title);
  if (item.authors) body.append(el("div", item.authors, "reference-authors"));
  if (item.text) body.append(el("div", item.text, "reference-publication"));
  if (item.citations.length) {
    const citations = el("div", undefined, "reference-citations");
    citations.append(el("span", "В тексте"));
    for (const place of item.citations) {
      citations.append(link(place.label, pdfUrl + "#page=" + place.page, undefined, true));
    }
    body.append(citations);
  }
  row.append(number, body);
  return row;
}

function detailRow(item, id, key, pdfUrl) {
  const row = el("li", undefined, "profile-entry");
  const disclosureKey = (key === "analogs" ? "analog" : "claim") + ":" + (item.href || item.label);
  const details = profileDetails(id, disclosureKey, "profile-entry-details");
  const summary = el("summary", undefined, "profile-entry-summary");
  const caption = el("span", undefined, "profile-entry-caption");
  caption.append(el("strong", item.label));
  if (item.byline) caption.append(el("span", item.byline, "subtle"));
  summary.append(caption);
  const facts = el("dl", undefined, "profile-entry-facts");
  for (const detail of item.details) {
    const fact = el("div", undefined, "profile-entry-fact");
    const value = el("dd");
    if (detail.location) {
      const href = detail.page ? pdfUrl + "#page=" + detail.page : detail.href;
      value.append(href ? link(detail.location, href, "profile-entry-location", true)
        : el("div", detail.location, "profile-entry-location"));
    }
    value.append(el("div", detail.text));
    fact.append(el("dt", detail.label), value);
    facts.append(fact);
  }
  details.append(summary, facts);
  if (item.href) details.append(link("Открыть публикацию", item.href, "profile-entry-source", true));
  row.append(details);
  return row;
}

function profileSection(section, id, pdfUrl, nested = false) {
  const classes = nested ? "profile-section profile-subsection" : "card profile-section";
  const card = profileDetails(id, section.key, classes, section.key === "overview");
  card.dataset.section = section.key;
  const summary = el("summary");
  const title = el(nested ? "h3" : "h2", section.title);
  if (nested || ["keywords", "claims", "references", "analogs"].includes(section.key)) {
    title.append(el("span", String(section.items.length), "profile-count"));
  }
  summary.append(title);
  const content = el("div", undefined, "profile-section-content");
  for (const paragraph of section.paragraphs || []) content.append(el("p", paragraph));
  if (section.items.length) {
    const list = el("ul", undefined, "profile-items");
    for (const item of section.items) {
      if (section.key === "references") {
        list.append(referenceRow(item, pdfUrl));
        continue;
      }
      if (item.details) {
        list.append(detailRow(item, id, section.key, pdfUrl));
        continue;
      }
      const row = el("li");
      const label = /^https?:\/\//i.test(item.href) ? link(item.label, item.href, undefined, true) : el("strong", item.label);
      row.append(label, el("div", item.text, "profile-item-text"));
      list.append(row);
    }
    content.append(list);
  }
  for (const subsection of section.subsections || []) {
    content.append(profileSection(subsection, id, pdfUrl, true));
  }
  card.append(summary, content);
  return card;
}

async function renderProfile() {
  const id = profileId;
  let paper;
  try { paper = await api(`/api/papers/${id}`); }
  catch (error) {
    if (id === profileId) {
      profileKey = "";
      $("paper-profile").replaceChildren(empty("Профиль недоступен", error.message));
    }
    return;
  }
  if (id !== profileId) return;
  const profile = paper.profile, job = paper.latest_job;
  const key = JSON.stringify([id, profile?.job_id, profile?.schema_version, job?.updated]);
  if (key === profileKey) return;
  profileKey = key;
  const panel = $("paper-profile");
  const heading = el("header", undefined, "page-heading profile-heading");
  const label = paper.kind === "arxiv" ? paper.source.replace("arxiv:", "ARXIV · ") : "ЛОКАЛЬНЫЙ PDF";
  heading.append(el("div", profile?.article.version ? `${label} · ${profile.article.version}` : label, "eyebrow"),
    el("h1", profile?.article.title ?? paper.title));
  if (profile) heading.append(el("p", `Профиль подготовлен ${date(profile.prepared_at)}.`));
  const actions = el("div", undefined, "detail-actions profile-actions");
  const base = `/api/papers/${id}`;
  const snapshot = profile ? "?snapshot=" + encodeURIComponent(profile.job_id) : "";
  const pdfUrl = base + "/article.pdf" + snapshot;
  function addJobAction(label, action) {
    const button = el("button", label, action === "analogs" ? "secondary warm-action" : "secondary");
    button.type = "button";
    button.disabled = ["queued", "running"].includes(job?.status);
    button.addEventListener("click", async () => {
      button.disabled = true;
      try { showJob(await api(`${base}/${action}`, {})); }
      catch (error) { button.disabled = false; notify(error.message, true); }
    });
    actions.append(button);
  }
  if (profile) {
    actions.append(link("Открыть профиль", base + "/profile.html" + snapshot, "primary", true),
      link("Открыть PDF", pdfUrl, "secondary", true),
      link("Скачать профиль .md", `${base}/profile.md${snapshot}`, "secondary"));
    if (profile.article.manuscript?.file === "manuscript.tex") {
      actions.append(link("Открыть исходник .tex", base + "/manuscript.html" + snapshot, "secondary", true));
    }
    if (profile.content.summary.length) addJobAction("Повторить поиск аналогов", "analogs");
  }
  if (paper.kind === "arxiv") {
    addJobAction(profile ? "Обновить с arXiv" : "Обработать с arXiv", "refresh");
  }
  const history = link("Задания по этой статье", "#jobs", "secondary");
  history.addEventListener("click", () => { paperFilter = paper; selectedJob = null; detailKey = ""; });
  actions.append(history);
  panel.replaceChildren(heading, actions);
  if (job && job.status !== "completed") {
    const saved = profile?.article.version ? ` Сохранённый профиль относится к версии ${profile.article.version}.` : "";
    const found = job.resolved_version && job.resolved_version !== profile?.article.version ? ` arXiv сообщил версию ${job.resolved_version}.` : "";
    panel.append(el("p", `${job.error || job.stage}.${found}${saved}`.replace(/\.\./g, "."), job.error ? "error-box" : "hint"));
  }
  if (!profile) {
    panel.append(empty("Профиль ещё не подготовлен", "Результаты первичной обработки появятся на этой странице."));
    return;
  }
  const folder = el("div", undefined, "profile-folder");
  folder.append(el("strong", "Папка на диске"), el("code", paper.directory));
  if (navigator.clipboard) {
    const copy = el("button", "Копировать путь", "secondary");
    copy.type = "button";
    copy.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(paper.directory); notify("Путь скопирован."); }
      catch { notify("Скопируйте выделенный путь вручную.", true); }
    });
    folder.append(copy);
  }
  panel.append(folder, ...paper.sections.map(section => profileSection(section, id, pdfUrl)));
}

const settingKeys=["interests","exclusions","rules","language","stage_seconds","time_limit_seconds","model","max_api_calls","max_output_tokens"];
const numericSettings = new Set(["stage_seconds", "time_limit_seconds", "max_api_calls", "max_output_tokens"]);
async function loadSettings(){const settings=await api("/api/settings");for(const key of settingKeys)$(key).value=settings[key];demoSeconds=settings.stage_seconds;renderMode();}
async function loadHealth() {
  health = await api("/api/health");
  if (health.version) {
    document.title = `Research Agent ${health.version} · Научная мастерская`;
    for (const label of document.querySelectorAll("[data-app-version]")) {
      label.textContent = `ВЕРСИЯ / ${health.version}`;
      label.hidden = false;
    }
  }
  $("key-state").textContent = health.api_configured ? "Ключ найден в окружении сервера."
    : "Задайте OPENAI_API_KEY в окружении и перезапустите сервер.";
  renderMode();
}
$("settings-form").addEventListener("submit",async event=>{
  event.preventDefault();const button=event.target.querySelector("button[type=submit]");button.disabled=true;
  try{const data={};for(const key of settingKeys)data[key]=numericSettings.has(key)?Number($(key).value):$(key).value;await api("/api/settings",data);demoSeconds=data.stage_seconds;renderMode();$("settings-state").textContent="Сохранено · "+new Date().toLocaleTimeString("ru-RU");notify("Настройки сохранены. Активные задания используют прежние настройки.");}catch(error){notify(error.message,true);}finally{button.disabled=false;}
});
$("settings-form").addEventListener("input",()=>$("settings-state").textContent="Есть несохранённые изменения");
function showAllJobs() {
  paperFilter = null;
  if (currentPage === "jobs") {
    renderJobs();
    refresh();
  }
}
$("clear-filter").addEventListener("click", showAllJobs);
document.querySelector('nav a[data-page="jobs"]').addEventListener("click", showAllJobs);
async function refresh(){
  if(busy)return;busy=true;
  try{
    jobs=await api("/api/jobs");
    if (!health || currentPage === "settings") await loadHealth();
    $("connection").textContent="● Сервер доступен";$("connection").className="online";
    $("queue-count").textContent=jobs.filter(job=>["queued","running"].includes(job.status)).length;
    if(currentPage==="jobs"){renderJobs();await renderDetail();}
    if(currentPage==="paper")await renderProfile();
    if (currentPage === "library" || !$("library-shortcuts").hidden) {
      const papers = await api("/api/papers");
      if (currentPage === "library") renderPapers(papers);
      if (!$("library-shortcuts").hidden) renderLibraryShortcuts(papers);
    }
  }catch(error){
    health = null;
    $("key-state").textContent = "Состояние подключения недоступно.";
    renderMode();
    $("connection").textContent="● Нет связи с сервером";$("connection").className="offline";
  }
  finally{busy=false;}
}
window.addEventListener("hashchange",navigate);
loadSettings().catch(()=>notify("Не удалось загрузить настройки. Проверьте, запущен ли сервер, и обновите страницу.",true));
navigate();setInterval(refresh,1200);
