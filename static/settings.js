async function apiGet(path){
  const r = await fetch(path, {credentials:"same-origin"});
  if(!r.ok) throw new Error(await r.text());
  return await r.json();
}
async function apiPost(path, body){
  const r = await fetch(path, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body), credentials:"same-origin"});
  if(!r.ok) throw new Error(await r.text());
  return await r.json();
}
async function apiDelete(path){
  const r = await fetch(path, {method:"DELETE", credentials:"same-origin"});
  if(!r.ok) throw new Error(await r.text());
  return await r.json();
}

function $(id){return document.getElementById(id);}

function setBadge(id, configured){
  const el = $(id);
  if(!el) return;
  el.textContent = configured ? "configured" : "not set";
  el.className = configured ? "badge ok" : "badge";
}

function renderHealth(state){
  const h = state.health || {};
  const feeds = state.feeds_count || 0;
  const s = state.settings || {};
  const html = `
    <table>
      <tr><th>Component</th><th>Status</th><th>Notes</th></tr>
      <tr><td>Directus</td><td>${h.directus ? "✅" : "❌"}</td><td>${h.directus ? "Connected" : "Set Directus URL + Token in System tab"}</td></tr>
      <tr><td>Slack</td><td>${h.slack ? "✅" : "❌"}</td><td>${h.slack ? "Ready to post" : "Set Slack channel + bot token"}</td></tr>
      <tr><td>Tavily</td><td>${h.tavily ? "✅" : "❌"}</td><td>${h.tavily ? "Research enabled" : "Set Tavily key"}</td></tr>
      <tr><td>Together</td><td>${h.together ? "✅" : "❌"}</td><td>${h.together ? "LLM/Image available" : "Set Together key"}</td></tr>
      <tr><td>OpenRouter</td><td>${h.openrouter ? "✅" : "❌"}</td><td>${h.openrouter ? "LLM routing available" : "Optional"}</td></tr>
      <tr><td>RSS Feeds</td><td>${feeds>0 ? "✅" : "❌"}</td><td>${feeds} configured</td></tr>
    </table>
  `;
  $("health").innerHTML = html;
}

function bindTabs(){
  const tabs = document.querySelectorAll(".tab");
  tabs.forEach(t=>{
    t.addEventListener("click", ()=>{
      tabs.forEach(x=>x.classList.remove("active"));
      document.querySelectorAll(".panel").forEach(p=>p.classList.remove("active"));
      t.classList.add("active");
      document.getElementById(t.dataset.tab).classList.add("active");
    });
  });
}

async function loadSettings(){
  const s = await apiGet("/api/settings");
  // simple inputs
  const keys = [
    "directus_url","directus_leads_collection","directus_articles_collection","directus_categories_collection",
    "slack_channel_id","publish_interval_minutes","scout_interval_minutes","http_timeout","user_agent",
    "prefer_extracted_image"
  ];
  keys.forEach(k=>{
    const el = $(k);
    if(el && s[k] !== undefined && s[k] !== null) el.value = s[k];
  });

  // badges for secrets
  const present = s._secrets_present || {};
  setBadge("directus_token_badge", present.directus_token);
  setBadge("slack_bot_token_badge", present.slack_bot_token);
  setBadge("slack_signing_secret_badge", present.slack_signing_secret);
  setBadge("tavily_api_key_badge", present.tavily_api_key);
  setBadge("together_api_key_badge", present.together_api_key);
  setBadge("openrouter_api_key_badge", present.openrouter_api_key);
}

async function saveSystem(){
  const payload = {
    directus_url: $("directus_url").value.trim(),
    directus_leads_collection: $("directus_leads_collection").value.trim(),
    directus_articles_collection: $("directus_articles_collection").value.trim(),
    directus_categories_collection: $("directus_categories_collection").value.trim(),
    slack_channel_id: $("slack_channel_id").value.trim(),
    publish_interval_minutes: $("publish_interval_minutes").value.trim(),
    scout_interval_minutes: $("scout_interval_minutes").value.trim(),
    http_timeout: $("http_timeout").value.trim(),
    user_agent: $("user_agent").value.trim(),
    prefer_extracted_image: $("prefer_extracted_image").value,
  };

  // secrets only if filled
  const secrets = ["directus_token","slack_bot_token","slack_signing_secret","tavily_api_key","together_api_key","openrouter_api_key"];
  secrets.forEach(k=>{
    const el = $(k);
    if(el && el.value.trim() !== "") payload[k] = el.value.trim();
  });

  await apiPost("/api/settings", payload);

  // clear secret fields after save
  secrets.forEach(k=>{
    const el = $(k);
    if(el) el.value = "";
  });

  await refreshAll();
  alert("Saved.");
}

async function loadFeeds(){
  const feeds = await apiGet("/api/feeds");
  const box = $("feeds_list");
  if(!feeds.length){
    box.innerHTML = "<p class='muted'>No feeds configured.</p>";
    return;
  }
  let html = "<table><tr><th>ID</th><th>URL</th><th>Enabled</th><th>Hint</th><th>Keys</th><th></th></tr>";
  for(const f of feeds){
    const keys = [f.title_key, f.description_key, f.content_key, f.category_key].filter(Boolean).join(" | ");
    html += `<tr>
      <td>${f.id}</td>
      <td><a href="${f.url}" target="_blank" rel="noreferrer">${f.url}</a></td>
      <td>${f.enabled ? "✅" : "–"}</td>
      <td>${f.category_hint || ""}</td>
      <td>${keys}</td>
      <td><button data-del="${f.id}">Delete</button></td>
    </tr>`;
  }
  html += "</table>";
  box.innerHTML = html;
  box.querySelectorAll("button[data-del]").forEach(btn=>{
    btn.addEventListener("click", async ()=>{
      const id = btn.getAttribute("data-del");
      if(confirm("Delete this feed?")){
        await apiDelete(`/api/feeds/${id}`);
        await loadFeeds();
      }
    });
  });
}

async function saveFeed(){
  const payload = {
    url: $("feed_url").value.trim(),
    enabled: $("feed_enabled").value === "1",
    category_hint: $("feed_category_hint").value.trim(),
    title_key: $("feed_title_key").value.trim(),
    description_key: $("feed_description_key").value.trim(),
    content_key: $("feed_content_key").value.trim(),
    category_key: $("feed_category_key").value.trim(),
  };
  // empty -> null
  Object.keys(payload).forEach(k=>{
    if(typeof payload[k] === "string" && payload[k] === "") payload[k] = null;
  });
  await apiPost("/api/feeds", payload);
  $("feed_url").value = "";
  await loadFeeds();
  alert("Feed saved.");
}

async function testFeed(){
  const payload = {
    url: $("feed_url").value.trim(),
    title_key: $("feed_title_key").value.trim(),
    description_key: $("feed_description_key").value.trim(),
    content_key: $("feed_content_key").value.trim(),
    category_key: $("feed_category_key").value.trim(),
  };
  Object.keys(payload).forEach(k=>{ if(payload[k]==="") payload[k]=null; });
  const out = $("feed_test_out");
  out.textContent = "Testing...";
  const res = await apiPost("/api/feeds/test", payload);
  out.textContent = JSON.stringify(res, null, 2);
}

async function loadModels(){
  const routes = await apiGet("/api/models");
  const box = $("models_form");
  const stages = [
    ["generation","Generation (Draft article)"],
    ["humanize","Humanization (Rewrite)"],
    ["seo","SEO (Meta/tags/short desc)"],
    ["image","Image Generation"]
  ];
  let html = "<table><tr><th>Stage</th><th>Provider</th><th>Model</th><th>Temp</th><th>Max tokens</th><th>Width</th><th>Height</th></tr>";
  for(const [stage,label] of stages){
    const r = routes[stage] || {};
    const isImage = stage === "image";
    html += `<tr>
      <td>${label}</td>
      <td>
        <select id="prov_${stage}">
          <option value="together" ${r.provider==="together"?"selected":""}>together</option>
          <option value="openrouter" ${r.provider==="openrouter"?"selected":""}>openrouter</option>
        </select>
      </td>
      <td><input id="model_${stage}" value="${(r.model||"").replaceAll('"','&quot;')}" placeholder="model id"/></td>
      <td><input id="temp_${stage}" type="number" step="0.1" value="${r.temperature??""}" ${isImage?"disabled":""} /></td>
      <td><input id="max_${stage}" type="number" step="50" value="${r.max_tokens??""}" ${isImage?"disabled":""} /></td>
      <td><input id="width_${stage}" type="number" step="64" value="${r.width??""}" ${!isImage?"disabled":""} /></td>
      <td><input id="height_${stage}" type="number" step="64" value="${r.height??""}" ${!isImage?"disabled":""} /></td>
    </tr>`;
  }
  html += "</table>";
  box.innerHTML = html;
}

async function saveModels(){
  const stages = ["generation","humanize","seo","image"];
  const payload = {};
  for(const stage of stages){
    const isImage = stage === "image";
    payload[stage] = {
      provider: document.getElementById(`prov_${stage}`).value,
      model: document.getElementById(`model_${stage}`).value.trim(),
    };
    if(!isImage){
      payload[stage].temperature = document.getElementById(`temp_${stage}`).value;
      payload[stage].max_tokens = document.getElementById(`max_${stage}`).value;
    } else {
      payload[stage].width = document.getElementById(`width_${stage}`).value;
      payload[stage].height = document.getElementById(`height_${stage}`).value;
    }
  }
  await apiPost("/api/models", payload);
  alert("Saved model routing.");
}

async function loadCategories(){
  const res = await apiGet("/api/categories");
  const box = $("categories_table");
  if(!res.ok){
    box.innerHTML = `<p class="muted">Failed to load categories: ${res.error}</p>`;
    return;
  }
  const cats = res.categories || [];
  if(!cats.length){
    box.innerHTML = "<p class='muted'>No categories found.</p>";
    return;
  }
  let html = "<table><tr><th>Priority</th><th>Slug</th><th>Name</th><th>Posts/Scout</th><th>Keywords</th></tr>";
  for(const c of cats){
    html += `<tr>
      <td>${c.priority}</td>
      <td>${c.slug}</td>
      <td>${c.name}</td>
      <td>${c.posts_per_scout}</td>
      <td>${(c.keywords||[]).slice(0,12).join(", ")}${(c.keywords||[]).length>12 ? "…" : ""}</td>
    </tr>`;
  }
  html += "</table>";
  box.innerHTML = html;
}

async function refreshAll(){
  const state = await apiGet("/api/state");
  renderHealth(state);
  await loadSettings();
  await loadFeeds();
  await loadModels();
  await loadCategories();
}

function bindButtons(){
  ["save_system_1","save_system_2","save_system_3","save_system_4"].forEach(id=>{
    $(id).addEventListener("click", saveSystem);
  });
  $("feed_save").addEventListener("click", saveFeed);
  $("feed_test").addEventListener("click", testFeed);
  $("models_save").addEventListener("click", saveModels);
}

window.addEventListener("DOMContentLoaded", async ()=>{
  bindTabs();
  bindButtons();
  await refreshAll();
});