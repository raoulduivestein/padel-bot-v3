const state = {
  config: null,
};

const days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"];

const $ = (id) => document.getElementById(id);

function setStatus(message, isError = false) {
  const el = $("statusText");
  el.textContent = message;
  el.style.color = isError ? "#b42318" : "#66707f";
}

async function requestJson(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const text = await response.text();
  const payload = text ? JSON.parse(text) : {};
  if (!response.ok) {
    throw new Error(JSON.stringify(payload, null, 2));
  }
  return payload;
}

function parseCsvNumbers(value) {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean)
    .map((item) => Number.parseInt(item, 10))
    .filter(Number.isFinite);
}

function parseLines(value) {
  return value
    .split("\n")
    .map((item) => item.trim())
    .filter(Boolean);
}

function timesToString(times) {
  return (times || []).join(", ");
}

function parseTimes(value) {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function renderRules(rules) {
  const root = $("rules");
  root.innerHTML = "";

  rules.forEach((rule, index) => {
    const row = document.createElement("div");
    row.className = "row";
    row.innerHTML = `
      <label>Day<select data-field="day"></select></label>
      <label>Times<input data-field="times" type="text" placeholder="18:00, 19:00"></label>
      <label>Hours<input data-field="duration" type="number" min="1" max="4"></label>
      <button type="button" class="danger" title="Remove">x</button>
    `;
    const select = row.querySelector('[data-field="day"]');
    days.forEach((day) => {
      const option = document.createElement("option");
      option.value = day;
      option.textContent = day;
      select.appendChild(option);
    });
    select.value = rule.day;
    row.querySelector('[data-field="times"]').value = timesToString(rule.times);
    row.querySelector('[data-field="duration"]').value = rule.duration;
    row.querySelector("button").addEventListener("click", () => {
      state.config.padel.booking_rules.splice(index, 1);
      renderRules(state.config.padel.booking_rules);
    });
    root.appendChild(row);
  });
}

function renderMembers(members) {
  const root = $("members");
  root.innerHTML = "";

  members.forEach((member, index) => {
    const row = document.createElement("div");
    row.className = "row member-row";
    row.innerHTML = `
      <label>Name<input data-field="name" type="text"></label>
      <label>Member ID<input data-field="member_id" type="text"></label>
      <button type="button" class="danger" title="Remove">x</button>
    `;
    row.querySelector('[data-field="name"]').value = member.name;
    row.querySelector('[data-field="member_id"]').value = member.member_id;
    row.querySelector("button").addEventListener("click", () => {
      state.config.padel.members.splice(index, 1);
      renderMembers(state.config.padel.members);
    });
    root.appendChild(row);
  });
}

function fillForm(config) {
  $("username").value = config.username || "";
  $("password").value = "";
  $("deviceId").value = config.device_id || "";
  $("signatureMode").value = config.signature_mode || "davidlloyd_v1";
  $("prepTime").value = config.padel.run_time?.prep || "07:59:55";
  $("bookingTime").value = config.padel.run_time?.booking || "08:00:00";
  $("daysAhead").value = config.padel.days_ahead ?? 6;
  $("clubId").value = config.padel.club_id ?? 94;
  $("sportsPackageId").value = config.padel.sports_package_id ?? 63;
  $("fallbackToAny").checked = Boolean(config.padel.fallback_to_any);
  $("preferredCourts").value = (config.padel.preferred_courts || []).join(", ");
  $("alwaysAddPlayers").value = (config.padel.always_add_player_ids || []).join("\n");
  renderRules(config.padel.booking_rules || []);
  renderMembers(config.padel.members || []);
  $("preview").textContent = JSON.stringify(config.padel, null, 2);
  renderRunSummary(config);
}

function renderRunSummary(config) {
  const rules = config.padel.booking_rules || [];
  const activeRules = rules
    .filter((rule) => (rule.times || []).length)
    .map((rule) => `${rule.day}: ${rule.times.join(", ")} (${rule.duration}h)`);
  $("summaryDays").textContent = String(config.padel.days_ahead ?? "-");
  $("summaryTarget").textContent = activeRules.length ? activeRules.join(" | ") : "No rules";
  $("summaryClub").textContent = `${config.padel.club_id ?? "-"} / ${config.padel.sports_package_id ?? "-"}`;
}

function collectForm() {
  const rules = [...$("rules").querySelectorAll(".row")].map((row) => ({
    day: row.querySelector('[data-field="day"]').value,
    times: parseTimes(row.querySelector('[data-field="times"]').value),
    duration: Number.parseInt(row.querySelector('[data-field="duration"]').value, 10),
  }));

  const members = [...$("members").querySelectorAll(".row")].map((row) => ({
    name: row.querySelector('[data-field="name"]').value.trim(),
    member_id: row.querySelector('[data-field="member_id"]').value.trim(),
  })).filter((member) => member.name && member.member_id);

  return {
    username: $("username").value.trim(),
    password: $("password").value || null,
    device_id: $("deviceId").value.trim(),
    signature_mode: $("signatureMode").value,
    padel: {
      run_time: {
        prep: $("prepTime").value,
        booking: $("bookingTime").value,
      },
      days_ahead: Number.parseInt($("daysAhead").value, 10),
      club_id: Number.parseInt($("clubId").value, 10),
      sports_package_id: Number.parseInt($("sportsPackageId").value, 10),
      members,
      always_add_player_ids: parseLines($("alwaysAddPlayers").value),
      preferred_courts: parseCsvNumbers($("preferredCourts").value),
      fallback_to_any: $("fallbackToAny").checked,
      booking_rules: rules,
    },
  };
}

async function loadConfig() {
  setStatus("Config laden...");
  state.config = await requestJson("/api/config");
  fillForm(state.config);
  setStatus(state.config.password_is_set ? "Config geladen. Password is set." : "Config geladen. Password ontbreekt.");
}

async function saveConfig(options = {}) {
  if (!options.quiet) setStatus("Opslaan...");
  const payload = collectForm();
  const result = await requestJson("/api/config", {
    method: "PUT",
    body: JSON.stringify(payload),
  });
  state.config = result.config;
  fillForm(state.config);
  if (!options.quiet) setStatus("Config opgeslagen.");
}

async function previewSlots() {
  const slots = await requestJson("/padel/slots");
  $("preview").textContent = JSON.stringify(slots, null, 2);
}

async function refreshAuth() {
  setStatus("Auth refresh...");
  const result = await requestJson("/auth/refresh-token", { method: "POST" });
  $("authStatus").textContent = JSON.stringify(result, null, 2);
  setStatus("Auth bijgewerkt.");
}

async function freshLogin() {
  setStatus("Fresh login...");
  const result = await requestJson("/auth/login", { method: "POST" });
  $("authStatus").textContent = JSON.stringify(result, null, 2);
  setStatus("Fresh login afgerond.");
}

async function checkAuthStatus() {
  const result = await requestJson("/auth/status");
  $("authStatus").textContent = JSON.stringify(result, null, 2);
}

async function runNow() {
  setStatus("Config opslaan voor run...");
  await saveConfig({ quiet: true });
  setStatus("Run gestart...");
  $("runResult").textContent = "Run actief...";
  const attempts = Number.parseInt($("runAttempts").value, 10) || 1;
  const result = await requestJson("/padel/book-generated", {
    method: "POST",
    body: JSON.stringify({ attempts }),
  });
  $("runResult").textContent = JSON.stringify(result, null, 2);
  setStatus(result.ok ? "Run afgerond: boeking gelukt." : "Run afgerond: geen boeking.");
}

function switchTab(name) {
  document.querySelectorAll(".tab").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === name);
  });
  document.querySelectorAll(".tab-page").forEach((page) => {
    page.classList.toggle("active", page.id === `${name}Tab`);
  });
}

function bind() {
  $("reloadBtn").addEventListener("click", () => loadConfig().catch((error) => setStatus(error.message, true)));
  $("saveBtn").addEventListener("click", () => saveConfig().catch((error) => setStatus(error.message, true)));
  $("slotsBtn").addEventListener("click", () => previewSlots().catch((error) => setStatus(error.message, true)));
  $("refreshAuthBtn").addEventListener("click", () => refreshAuth().catch((error) => setStatus(error.message, true)));
  $("freshLoginBtn").addEventListener("click", () => freshLogin().catch((error) => setStatus(error.message, true)));
  $("authStatusBtn").addEventListener("click", () => checkAuthStatus().catch((error) => setStatus(error.message, true)));
  $("runNowBtn").addEventListener("click", () => runNow().catch((error) => {
    $("runResult").textContent = error.message;
    setStatus("Run mislukt.", true);
  }));
  document.querySelectorAll(".tab").forEach((button) => {
    button.addEventListener("click", () => switchTab(button.dataset.tab));
  });
  $("addRuleBtn").addEventListener("click", () => {
    state.config.padel.booking_rules.push({ day: "monday", times: ["18:00"], duration: 1 });
    renderRules(state.config.padel.booking_rules);
    renderRunSummary(collectForm());
  });
  $("addMemberBtn").addEventListener("click", () => {
    state.config.padel.members.push({ name: "", member_id: "" });
    renderMembers(state.config.padel.members);
  });
}

bind();
loadConfig().catch((error) => setStatus(error.message, true));
