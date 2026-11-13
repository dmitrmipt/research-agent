"use strict";
const $ = id => document.getElementById(id);
const statusNames = {queued:"В очереди",running:"Выполняется",completed:"Завершено",cancelled:"Остановлено",failed:"Ошибка",interrupted:"Прервано"};
const depthNames = {brief:"16 000 символов",standard:"30 000 символов",deep:"48 000 символов",full:"Полный текст",vision:"Полный текст + картинки"};
const stepNames = {material:"Материал",core:"Обзор",references:"Библиография",analogs:"Аналоги",manuscript:"Исходник"};
const stepStates = {pending:"Ожидает",running:"Выполняется",completed:"Готово",failed:"Ошибка",skipped:"Пропущено",blocked:"Требуется действие",not_applicable:"Не требуется"};
const effortNames = {auto:"По умолчанию модели",none:"Без дополнительного рассуждения",low:"Низкое · low",medium:"Среднее · medium",high:"Высокое · high",xhigh:"Очень высокое · xhigh",max:"Максимальное · max"};
let catalog = null, catalogRequested = false, savedSettings = null, analysisContext = null;
let newModelEdited = false;
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
  const previousPage = currentPage;
  const previousProfile = profileId;
  setLibraryShortcuts(false);
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
  if (previousPage !== currentPage || previousProfile !== profileId) window.scrollTo(0, 0);
  if (currentPage === "new" && previousPage !== "new" && !newModelEdited && savedSettings) fillNewModels(savedSettings.model, savedSettings.reasoning_effort);
  if (currentPage === "settings" && !catalogRequested) { catalogRequested = true; loadCatalog(true); }
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
  $("vision-note").hidden = demo || $("depth").value !== "vision";
  $("bibliography-review-label").hidden = demo;
  $("bibliography-review-note").hidden = demo;
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
$("depth").addEventListener("change", renderMode);
document.querySelectorAll("[data-question]").forEach(button => button.addEventListener("click", () => {
  const field = $("questions");
  const question = button.textContent.trim();
  const current = field.value.trimEnd();
  if (!current.split("\n").some(line => line.trim() === question)) {
    const combined = [current, question].filter(Boolean).join("\n");
    if (combined.length > field.maxLength) {
      notify("Не хватает места для ещё одного вопроса.", true);
      return;
    }
    field.value = combined;
  }
  field.focus();
  field.setSelectionRange(field.value.length, field.value.length);
}));
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
    const body={source:$("source").value,questions:$("questions").value,depth:$("depth").value,mode:$("mode").value,
      settings:{model:$("new-model").value,reasoning_effort:$("new-effort").value,
        review_bibliography:$("mode").value === "research" && $("bibliography-review").checked}};
    if (body.mode === "demo") body.scenario = $("scenario").value;
    if(sourceType==="pdf"){
      const file=$("pdf").files[0];if(!file)throw new Error("Выберите PDF.");
      if(body.depth!=="vision" && file.size>15*1024*1024)throw new Error("Для PDF больше 15 МБ выберите «Полный текст + картинки».");
      body.pdf={name:file.name,data:await fileData(file)};
    }
    showJob(await api("/api/jobs", body)); newModelEdited = false;
  }catch(error){notify(error.message,true);}finally{button.disabled=false;renderMode();}
});
function badge(status){return el("span",statusNames[status]||status,"badge "+status);}
function empty(title,subtitle){const div=el("div",undefined,"empty");div.append(el("h2",title),el("p",subtitle));return div;}
function renderJobs(){
  const active=jobs.filter(job=>["running","queued"].includes(job.status)).length;
  const completed=jobs.filter(job=>job.status==="completed").length;
  $("queue-count").textContent=active;$("stat-active").textContent=active;$("stat-complete").textContent=completed;$("stat-other").textContent=jobs.length-active-completed;
  const filtered=paperFilter?jobs.filter(job=>job.paper_id===paperFilter.id):jobs;
  if (!filtered.length) selectedJob = null;
  $("library-filter").hidden=!paperFilter;if(paperFilter)$("library-filter").querySelector("span").textContent="Материал: "+paperFilter.title;
  if(filtered.length&&!filtered.some(job=>job.id===selectedJob)){selectedJob=filtered[0].id;detailKey="";}
  const key=JSON.stringify([selectedJob,filtered.map(job=>[job.id,job.title,job.status,job.result_status])]);
  if(key===jobListKey)return;jobListKey=key;
  const list=$("job-list"), focusedId=list.contains(document.activeElement)?document.activeElement.dataset.jobId:null;
  list.replaceChildren();
  if(!filtered.length){list.append(empty("Пока нет заданий","Начните с нового разбора статьи."));return;}
  for(const job of filtered){
    const row = el("div", undefined, "job-list-row");
    const button=el("button",undefined,"job-item"+(job.id===selectedJob?" selected":""));button.type="button";button.dataset.jobId=job.id;button.setAttribute("aria-pressed",String(job.id===selectedJob));
    button.append(el("strong",job.title),badge(job.status),el("small",date(job.created)+" · "+job.id.slice(0,8)+(job.mode === "demo" ? " · Демо" : "")));
    if (job.result_status === "partial") button.append(el("small", "Частичный результат"));
    button.addEventListener("click",()=>{selectedJob=job.id;detailKey="";renderJobs();refresh();});
    row.append(button);
    if (!["running", "queued"].includes(job.status)) {
      const remove = el("button", "Удалить задание", "job-delete");
      remove.type = "button";
      remove.onclick = async () => {
        if (!window.confirm(`Удалить задание «${job.title}» из истории? Профиль статьи сохранится.`)) return;
        remove.disabled = true;
        try {
          await api(`/api/jobs/${job.id}/delete`, {});
          if (selectedJob === job.id) { selectedJob = null; detailKey = ""; }
          await refresh();
          notify("Задание удалено.");
        } catch (error) { notify(error.message, true); remove.disabled = false; }
      };
      row.append(remove);
    }
    list.append(row);
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
function stepsView(steps, compact = false) {
  const section = el("section", undefined, "processing-stages" + (compact ? " coverage-stages" : ""));
  section.setAttribute("aria-label", "Этапы обработки");
  const list = el("ol", undefined, "step-list");
  const notes = el("dl", undefined, "step-notes");
  for (const [key, step] of Object.entries(steps || {})) {
    const row = el("li", undefined, "step-state-" + step.status);
    const symbol = el("span", {completed:"✓", running:"·", failed:"!", blocked:"!", skipped:"−", not_applicable:"−"}[step.status] || "", "step-symbol");
    symbol.setAttribute("aria-hidden", "true");
    row.append(symbol, el("strong", stepNames[key] || key), el("span", stepStates[step.status] || step.status, "step-status"));
    if (step.message) notes.append(el("dt", stepNames[key] || key), el("dd", step.message));
    list.append(row);
  }
  section.append(list);
  if (notes.childElementCount && compact) {
    const details = el("details", undefined, "coverage-step-notes");
    details.append(el("summary", "Сообщения этапов"), notes);
    section.append(details);
  } else if (notes.childElementCount) section.append(notes);
  return section;
}
function retryButton(job) {
  const button = el("button", "Продолжить разбор", "primary");
  button.type = "button";
  button.onclick = async () => {
    button.disabled = true;
    try { showJob(await api(`/api/jobs/${job.id}/retry`, {})); }
    catch (error) { notify(error.message, true); button.disabled = false; }
  };
  return button;
}
async function renderDetail(){
  if(!selectedJob){$("job-detail").replaceChildren(empty("Выберите задание","Здесь появятся этапы, настройки и результат."));return;}
  const requested=selectedJob, job=await api("/api/jobs/"+requested);
  if(requested!==selectedJob)return;
  const key=job.id+job.updated;if(detailKey===key)return;detailKey=key;
  const panel=$("job-detail");
  const expanded = panel.dataset.jobId === job.id
    ? Object.fromEntries([...panel.querySelectorAll("details[data-job-disclosure]")].map(node => [node.dataset.jobDisclosure, node.open])) : {};
  const reportScroll=panel.querySelector(".report")?.scrollTop||0;panel.replaceChildren();panel.dataset.jobId=job.id;
  const heading=el("div",undefined,"detail-heading");heading.append(el("h2",job.title),badge(job.status));panel.append(heading,el("div",date(job.created)+" · № "+job.id.slice(0,8),"subtle"));
  if (["queued", "running"].includes(job.status) || job.mode === "demo") {
    panel.append(el("div",job.stage+" · "+job.progress+"%","stage-line"));
    const progress=el("progress",undefined,"progress");progress.max=100;progress.value=job.progress;progress.setAttribute("aria-label","Ход обработки");panel.append(progress);
  }
  const actions=el("div",undefined,"detail-actions");
  if(["running","queued"].includes(job.status)){
    const cancel=el("button","Остановить задание","secondary danger");cancel.type="button";
    cancel.onclick=async()=>{cancel.disabled=true;try{await api("/api/jobs/"+job.id+"/cancel",{});detailKey="";await refresh();}catch(error){notify(error.message,true);cancel.disabled=false;}};actions.append(cancel);
  }
  if(job.report){const download=el("a","Скачать отчёт .md","secondary");download.href="/api/jobs/"+job.id+"/report";actions.append(download);}
  if (job.mode === "research" && (job.result_status !== "none" || job.input_revision)) actions.append(link("Открыть профиль статьи →", `#paper/${job.paper_id}`, "secondary"));
  if (job.retry?.allowed) {
    actions.append(retryButton(job));
    const edit = el("button", "Изменить параметры", "secondary"); edit.type = "button";
    edit.onclick = () => openAnalysis("retry", job);
    actions.append(edit);
  }
  panel.append(actions);
  if (job.mode === "research") {
    panel.append(el("p", job.result_status === "ready" ? "Основной профиль готов и сохранён." : job.result_status === "partial" ? "Сохранён частичный результат." : job.steps.material?.status === "completed" ? "Материалы и текст сохранены. Обзор ещё не подготовлен." : "Новый профиль пока не сохранён.", "result-state" + (job.result_status === "partial" ? " partial" : "")));
    if (job.retry?.allowed) panel.append(el("p", `Продолжение: ${job.retry.steps.join(", ")}. ${job.retry.model_needed ? "Возможны вызовы модели и расходы API." : "Вызов модели не требуется."}`, "hint"));
    else if (job.retry?.message && Object.values(job.steps).some(step => step.code === "no_text")) panel.append(el("p", job.retry.message, "hint"));
    panel.append(stepsView(job.steps));
    if (job.parent_job_id && jobs.some(parent => parent.id === job.parent_job_id)) {
      const parent = el("button", "Исходная попытка · " + job.parent_job_id.slice(0, 8), "secondary"); parent.type = "button";
      parent.onclick = () => { selectedJob = job.parent_job_id; detailKey = ""; renderJobs(); refresh(); };
      panel.append(parent);
    }
  }
  if(job.error)panel.append(el("div",job.error,"error-box"));
  if (job.resolved_version) panel.append(el("p", "Версия arXiv для этого задания: " + job.resolved_version, "subtle"));
  panel.append(el("p", apiUsage(job.api_usage), "subtle"));
  if (job.api_usage.calls) panel.append(el("p", job.estimated_cost === null ? "Расчётная стоимость неизвестна: недостаточно данных." : `Расчётная стоимость: $${job.estimated_cost.toFixed(6)} · тариф ${job.settings.pricing?.date || ""} (Standard).`, "subtle"));
  const history=el("details",undefined,"job-disclosure job-history");history.dataset.jobDisclosure="history";history.open=Boolean(expanded.history);
  history.append(el("summary","Журнал этапов"));const events=el("ol",undefined,"events");
  job.events.forEach(event=>{const line=el("li");line.append(el("time",new Date(event.time).toLocaleTimeString("ru-RU")),el("span",event.message));events.append(line);});history.append(events);panel.append(history);
  if(job.report){panel.append(el("h2","Результат · демонстрационный шаблон"),el("pre",job.report,"report"));panel.querySelector(".report").scrollTop=reportScroll;}
  const details=el("details",undefined,"job-disclosure job-settings");details.dataset.jobDisclosure="settings";details.open=Boolean(expanded.settings);details.append(el("summary","Параметры и снимок настроек"));
  const settings=job.settings;details.append(el("div",[
    "Источник: "+job.input_source,"Охват: "+depthNames[job.depth],"Усилие: "+(effortNames[settings.reasoning_effort] || settings.reasoning_effort),
    "Режим: "+(job.mode === "research" ? "Первичная обработка" : "Демонстрация"),
    "Вопросы: "+(job.questions||"Не указаны"),"Темы: "+(settings.interests||"Не заданы"),"Исключения: "+(settings.exclusions||"Не заданы"),
    "Правила: "+settings.rules,"Язык: "+settings.language,"Лимит времени: "+settings.time_limit_seconds+" с",
    job.mode === "research" ? `Модель: ${settings.model} · максимум вызовов: ${settings.max_api_calls} · ${job.depth === "vision" ? "выход в пределах лимита модели" : `лимит выхода: ${settings.max_output_tokens} токенов на вызов`}` : `Длительность этапа: ${settings.stage_seconds} с`
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
    heading.append(text, el("span", paper.result_status === "partial" ? "Частичный результат →" : paper.has_profile ? "Открыть профиль →" : "Открыть материал →", "library-status"));
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
document.addEventListener("click", event => {
  if (!event.target.closest(".library-nav")) setLibraryShortcuts(false);
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && !$("library-shortcuts").hidden) {
    setLibraryShortcuts(false);
    $("library-toggle").focus();
  }
});
document.querySelector(".skip-link").addEventListener("click", event => {
  event.preventDefault();
  $("main-content").focus();
  $("main-content").scrollIntoView({block:"start"});
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
    const citations = el("details", undefined, "reference-citations");
    citations.append(el("summary", `Места цитирования (${item.citations.length})`));
    for (const place of item.citations) {
      const url = new URL(pdfUrl, location.href);
      url.hash = "page=" + place.page;
      if (place.anchor) {
        url.searchParams.set("citation", place.anchor);
      }
      const mention = link(place.label, url.href, "reference-mention", true);
      if (place.context) mention.append(el("span", place.context));
      citations.append(mention);
    }
    body.append(citations);
  }
  row.append(number, body);
  return row;
}

function mathText(node, mode) {
  if (mode) window.renderProfileMath?.(node, mode);
  return node;
}

function detailRow(item, id, key, pdfUrl, math) {
  const row = el("li", undefined, "profile-entry");
  const disclosureKey = key + ":" + (item.href || item.label);
  const details = profileDetails(id, disclosureKey, "profile-entry-details");
  const summary = el("summary", undefined, "profile-entry-summary");
  const caption = el("span", undefined, "profile-entry-caption");
  caption.append(mathText(el("strong", item.label), math && "inline"));
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
    value.append(mathText(el("div", detail.text), math && "location" in detail ? "inline" : math));
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
  if (nested || ["keywords", "claims", "illustrations", "references", "analogs"].includes(section.key)) {
    title.append(el("span", String(section.items.length), "profile-count"));
  }
  summary.append(title);
  const content = el("div", undefined, "profile-section-content");
  const topics = section.topics || [];
  for (const paragraph of section.paragraphs || []) content.append(mathText(el("p", paragraph), section.math));
  if (section.items.length) {
    const list = el("ul", undefined, "profile-items" + (section.key === "coverage" ? " coverage-facts" : ""));
    for (const item of section.items) {
      if (section.key === "references") {
        list.append(referenceRow(item, pdfUrl));
        continue;
      }
      if (item.details) {
        list.append(detailRow(item, id, section.key, pdfUrl, section.math));
        continue;
      }
      const row = el("li");
      const label = /^https?:\/\//i.test(item.href) ? link(item.label, item.href, undefined, true) : el("strong", item.label);
      row.append(mathText(label, section.math && "inline"), mathText(el("div", item.text, "profile-item-text"), section.math));
      list.append(row);
    }
    content.append(list);
  }
  if (topics.length) {
    const list = el("ul");
    for (const topic of topics) list.append(mathText(el("li", topic), "inline"));
    const block = el("div", undefined, "profile-topics");
    block.append(el("h3", "Темы"), list);
    content.append(block);
  }
  if (section.steps && Object.keys(section.steps).length) content.append(stepsView(section.steps, true));
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
  const continuation = job?.retry?.allowed ? job : paper.continuation_job;
  const key = JSON.stringify([id, profile?.revision || profile?.job_id, profile?.schema_version, job?.updated]);
  if (key === profileKey) return;
  profileKey = key;
  const panel = $("paper-profile");
  const heading = el("header", undefined, "page-heading profile-heading");
  const label = paper.kind === "arxiv" ? paper.source.replace("arxiv:", "ARXIV · ") : "ЛОКАЛЬНЫЙ PDF";
  heading.append(el("div", profile?.article.version ? `${label} · ${profile.article.version}` : label, "eyebrow"),
    el("h1", profile?.article.title ?? paper.title));
  if (profile?.article.authors?.length) heading.append(el("p", profile.article.authors.join(", "), "profile-authors"));
  if (profile) heading.append(el("p", `Профиль подготовлен ${date(profile.prepared_at)}.`, "subtle"));
  const actions = el("div", undefined, "detail-actions profile-actions");
  const base = `/api/papers/${id}`;
  const snapshot = profile ? "?snapshot=" + encodeURIComponent(profile.revision || profile.job_id) : "";
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
    actions.append(link("Просмотреть профиль", base + "/profile.html" + snapshot, "primary", true),
      link("Открыть PDF", pdfUrl, "secondary", true),
      link("Скачать профиль .md", `${base}/profile.md${snapshot}`, "secondary"));
    if (profile.article.manuscript?.file === "manuscript.tex") {
      actions.append(link("Открыть исходник .tex", base + "/manuscript.html" + snapshot, "secondary", true));
    }
    if (profile.content.summary.length) addJobAction("Повторить поиск аналогов", "analogs");
    if (profile.content.references?.length) addJobAction("Уточнить библиографию", "references");
    const reanalyze = el("button", "Разобрать заново", "secondary warm-action"); reanalyze.type = "button";
    reanalyze.disabled = ["queued", "running"].includes(job?.status);
    reanalyze.onclick = () => openAnalysis("reanalyze", paper);
    actions.append(reanalyze);
  }
  if (paper.kind === "arxiv") {
    addJobAction(profile ? "Обновить с arXiv" : "Обработать с arXiv", "refresh");
  }
  const history = link("Задания по этой статье", "#jobs", "secondary");
  history.addEventListener("click", () => { paperFilter = paper; selectedJob = null; detailKey = ""; });
  actions.append(history);
  if (continuation && !["queued", "running"].includes(job?.status)) actions.append(retryButton(continuation));
  panel.replaceChildren(heading, actions);
  if (profile?.result_status === "partial") panel.append(el("p", "Частичный результат: обзор не подготовлен. Подробности — в задании.", "result-state partial"));
  if (continuation) panel.append(el("p", "Не завершены: " + continuation.retry.steps.join(", ") + ". " + (continuation.retry.model_needed ? "Продолжение может вызвать модель." : "Продолжение без вызова модели."), "hint"));
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
  const storage = profileDetails(id, "storage", "profile-storage");
  storage.append(el("summary", "Файлы на этом компьютере"), folder);
  panel.append(...paper.sections.map(section => profileSection(section, id, pdfUrl)), storage);
}

function effortsFor(model, select, value = "auto") {
  const spec = catalog?.models.find(item => item.id === model);
  const values = ["auto", ...(spec?.efforts || [])];
  select.replaceChildren(...values.map(effort => { const option = el("option", effortNames[effort]); option.value = effort; return option; }));
  select.value = values.includes(value) ? value : "auto";
}
function priceText(spec) {
  return spec?.pricing ? `$${spec.pricing.input} / $${spec.pricing.output} за 1 млн` : "Цена неизвестна";
}
function fillNewModels(model = $("new-model").value, effort = $("new-effort").value) {
  const specs = (catalog?.models || []).filter(item => item.available !== false || item.id === model);
  const options = specs.map(spec => { const option = el("option", spec.name); option.value = spec.id; return option; });
  if (!options.some(option => option.value === model)) { const custom = el("option", model); custom.value = model; options.push(custom); }
  $("new-model").replaceChildren(...options); $("new-model").value = model;
  effortsFor(model, $("new-effort"), effort || "auto");
}
$("new-model").onchange = () => { newModelEdited = true; effortsFor($("new-model").value, $("new-effort"), $("new-effort").value); };
$("new-effort").onchange = () => { newModelEdited = true; };
function chooseModel(model, effort = "auto") {
  $("model").value = model;
  $("manual-model").value = model;
  const spec = catalog?.models.find(item => item.id === model);
  $("model-caption").textContent = (spec?.name || model) + " · " + priceText(spec);
  effortsFor(model, $("reasoning_effort"), effort);
  const pricing = $("model-pricing"); pricing.replaceChildren();
  if (spec?.pricing) {
    for (const [label, key] of [["Вход", "input"], ["Кешированный вход", "cached_input"], ["Выход", "output"]]) {
      const cell = el("div"); cell.append(el("span", label), el("strong", "$" + spec.pricing[key])); pricing.append(cell);
    }
    const note = el("p", `За 1 млн токенов · Standard · тариф от ${spec.pricing.date}. `, "hint");
    note.append(link("Официальные цены ↗", spec.pricing.source, undefined, true)); pricing.append(note);
  } else pricing.append(el("p", "Цена и совместимость этой модели не подтверждены. Будет использовано усилие по умолчанию.", "hint"));
  renderModelOptions();
}
function renderModelOptions() {
  const query = $("model-search").value.toLowerCase();
  const rows = (catalog?.models || []).filter(item => (item.name + " " + item.id).toLowerCase().includes(query)).map(spec => {
    const option = el("button", undefined, "model-option"); option.type = "button"; option.setAttribute("role", "option");
    option.setAttribute("aria-selected", String(spec.id === $("model").value)); option.disabled = spec.available === false;
    option.append(el("span", spec.name, "model-option-name"), el("span", priceText(spec), "model-option-price"));
    option.title = spec.id + (spec.available === false ? " · Нет в списке по ключу" : spec.available === true ? " · Есть в списке по ключу" : " · Доступ ещё не проверен");
    option.onclick = () => { chooseModel(spec.id); closeModelPicker(); $("settings-state").textContent = "Есть несохранённые изменения"; };
    return option;
  });
  $("model-options").replaceChildren(...(rows.length ? rows : [el("p", "Совпадений нет. Можно ввести идентификатор вручную.", "hint")]));
}
function closeModelPicker() { $("model-popup").hidden = true; $("model-picker").setAttribute("aria-expanded", "false"); }
function openModelPicker() { $("model-popup").hidden = false; $("model-picker").setAttribute("aria-expanded", "true"); $("model-search").focus(); }
$("model-picker").onclick = () => $("model-popup").hidden ? openModelPicker() : closeModelPicker();
$("model-search").oninput = renderModelOptions;
document.addEventListener("click", event => { if (!event.target.closest(".model-picker-wrap")) closeModelPicker(); });
$("model-picker").onkeydown = event => { if (event.key === "ArrowDown") { event.preventDefault(); openModelPicker(); } };
$("model-popup").onkeydown = event => {
  const options = [...$("model-options").querySelectorAll("button:not(:disabled)")];
  const index = options.indexOf(document.activeElement);
  if (event.key === "Escape") { event.preventDefault(); closeModelPicker(); $("model-picker").focus(); }
  if (event.key === "ArrowDown" || event.key === "ArrowUp") { event.preventDefault(); options[(index + (event.key === "ArrowDown" ? 1 : -1) + options.length) % options.length]?.focus(); }
};
$("manual-model-apply").onclick = () => {
  const model = $("manual-model").value.trim();
  if (!/^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,119}$/.test(model)) { notify("Проверьте идентификатор модели.", true); return; }
  chooseModel(model); $("settings-state").textContent = "Есть несохранённые изменения";
};
async function loadCatalog(remote = false) {
  const button = $("models-refresh");
  if (remote) { button.disabled = true; $("catalog-state").textContent = "Запрашиваем список по ключу сервера…"; }
  try {
    const data = await api(remote ? "/api/models/refresh" : "/api/models", remote ? {} : undefined);
    if (!remote && catalog?.checked_at && (!data.checked_at || data.checked_at < catalog.checked_at)) return;
    catalog = data;
    fillNewModels();
    $("catalog-state").textContent = data.error ? data.error + (data.checked_at ? " Показан последний полученный список." : " Показан справочный каталог.") :
      data.checked_at ? `Список получен ${date(data.checked_at)}. Выполнение анализа для этих моделей ещё не проверялось.` : "Справочный каталог. Обновите список для проверки доступа по ключу.";
    $("other-models").textContent = data.other_models.length ? "Другие идентификаторы по ключу (совместимость не проверена): " + data.other_models.join(", ") : "";
    $("analysis-models").replaceChildren(...data.models.filter(item => item.available !== false).map(item => { const option = el("option"); option.value = item.id; option.label = item.name + " · " + priceText(item); return option; }));
    chooseModel($("model").value, $("reasoning_effort").value || savedSettings?.reasoning_effort || "auto");
  } catch (error) { $("catalog-state").textContent = "Каталог недоступен. " + error.message; }
  finally { if (remote) button.disabled = false; }
}
$("models-refresh").onclick = () => loadCatalog(true);
function fillAnalysisSettings(settings) {
  $("analysis-model").value = settings.model;
  effortsFor(settings.model, $("analysis-effort"), settings.reasoning_effort);
  $("analysis-calls").value = settings.max_api_calls;
  $("analysis-time").value = settings.time_limit_seconds;
  $("analysis-output").value = settings.max_output_tokens;
  syncAnalysisScope(settings.max_api_calls);
}
function syncAnalysisScope(calls) {
  const visual = $("analysis-depth").value === "vision";
  $("analysis-vision-note").hidden = !visual;
  $("analysis-calls").disabled = visual;
  $("analysis-output").disabled = visual;
  if (visual) {
    $("analysis-calls").dataset.textValue = calls;
    $("analysis-calls").value = 6;
    $("analysis-time").value = Math.max(600, Number($("analysis-time").value));
  } else {
    $("analysis-calls").value = calls;
  }
}
async function openAnalysis(kind, target) {
  try {
    const current = await api("/api/settings");
    let previous = target;
    if (kind === "reanalyze") {
      try { previous = await api("/api/jobs/" + target.profile.job_id); } catch { previous = {settings: current, depth: "standard"}; }
      previous = {...previous, settings: target.profile.analysis?.settings || previous.settings, questions: target.profile.questions,
        depth: target.profile.analysis?.depth || previous.depth};
    }
    analysisContext = {kind, target, previous, current};
    $("analysis-title").textContent = kind === "retry" ? "Продолжить разбор" : "Разобрать заново";
    $("analysis-submit").textContent = kind === "retry" ? "Продолжить разбор" : "Начать разбор";
    $("analysis-description").textContent = kind === "retry" ? "Будут выполнены недостающие этапы: " + previous.retry.steps.join(", ") + "." : "Используем сохранённый PDF. Предыдущий профиль останется доступным до готовности нового.";
    $("analysis-preset").value = "previous";
    $("analysis-questions-wrap").hidden = kind === "retry";
    $("analysis-questions").value = previous.questions;
    $("analysis-depth").value = previous.depth;
    $("analysis-error").hidden = true;
    fillAnalysisSettings(previous.settings);
    $("analysis-dialog").showModal();
  } catch (error) { notify(error.message, true); }
}
$("analysis-close").onclick = () => $("analysis-dialog").close();
$("analysis-depth").onchange = () => {
  const calls = $("analysis-calls");
  syncAnalysisScope(calls.disabled ? calls.dataset.textValue : calls.value);
};
$("analysis-preset").onchange = () => fillAnalysisSettings($("analysis-preset").value === "current" ? analysisContext.current : analysisContext.previous.settings);
$("analysis-model").oninput = () => effortsFor($("analysis-model").value, $("analysis-effort"));
$("analysis-form").onsubmit = async event => {
  event.preventDefault(); const button = $("analysis-submit"); button.disabled = true;
  try {
    const context = analysisContext;
    const settings = {...($("analysis-preset").value === "current" ? context.current : context.previous.settings),
      model: $("analysis-model").value.trim(), reasoning_effort: $("analysis-effort").value,
      max_api_calls: Number($("analysis-calls").value), time_limit_seconds: Number($("analysis-time").value), max_output_tokens: Number($("analysis-output").value)};
    const path = context.kind === "retry" ? `/api/jobs/${context.target.id}/retry` : `/api/papers/${context.target.id}/reanalyze`;
    const job = await api(path, {settings, questions: $("analysis-questions").value, depth: $("analysis-depth").value});
    $("analysis-dialog").close(); showJob(job);
  } catch (error) { $("analysis-error").hidden = false; $("analysis-error").textContent = error.message; }
  finally { button.disabled = false; }
};

const settingKeys=["interests","exclusions","rules","language","stage_seconds","time_limit_seconds","model","reasoning_effort","max_api_calls","max_output_tokens"];
const numericSettings = new Set(["stage_seconds", "time_limit_seconds", "max_api_calls", "max_output_tokens"]);
async function loadSettings(){const settings=await api("/api/settings");savedSettings=settings;for(const key of settingKeys)if(key!=="reasoning_effort")$(key).value=settings[key];await loadCatalog();chooseModel(settings.model,settings.reasoning_effort);fillNewModels(settings.model,settings.reasoning_effort);demoSeconds=settings.stage_seconds;renderMode();}
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
  try{const data={};for(const key of settingKeys)data[key]=numericSettings.has(key)?Number($(key).value):$(key).value;await api("/api/settings",data);savedSettings=data;if(!newModelEdited)fillNewModels(data.model,data.reasoning_effort);demoSeconds=data.stage_seconds;renderMode();$("settings-state").textContent="Сохранено · "+new Date().toLocaleTimeString("ru-RU");notify("Настройки сохранены. Активные задания используют прежние настройки.");}catch(error){notify(error.message,true);}finally{button.disabled=false;}
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
