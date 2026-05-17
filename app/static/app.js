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
  if (!response.ok) throw new Error(JSON.stringify(payload, null, 2));
  return payload;
}

function ensureKnownPlayers() {
  state.config.padel.known_players = state.config.padel.known_players || {};
  return state.config.padel.known_players;
}

function rememberPlayer(player) {
  if (!player?.encodedContactId) return;
  ensureKnownPlayers()[player.encodedContactId] = player.fullName || player.memberReferenceNumber || player.encodedContactId;
}

function playerName(playerId) {
  return ensureKnownPlayers()[playerId] || playerId;
}

function parseCsvNumbers(value) {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean)
    .map((item) => Number.parseInt(item, 10))
    .filter(Number.isFinite);
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

function allConfiguredPlayerIds() {
  const memberIds = [...document.querySelectorAll("#members .player-chip")].map((chip) => chip.dataset.playerId);
  const alwaysIds = [...document.querySelectorAll("#alwaysPlayers .player-chip")].map((chip) => chip.dataset.playerId);
  const ruleIds = [...document.querySelectorAll("#rules .rule-players .player-chip")].map((chip) => chip.dataset.playerId);
  return { memberIds, alwaysIds, ruleIds };
}

function validateRulePlayerAllowed(playerId) {
  const { memberIds, alwaysIds } = allConfiguredPlayerIds();
  if (memberIds.includes(playerId) || alwaysIds.includes(playerId)) {
    throw new Error("Deze player staat al in Members of Always add en mag niet in een rule.");
  }
}

function validateMemberPlayerAllowed(playerId) {
  const { memberIds, alwaysIds, ruleIds } = allConfiguredPlayerIds();
  if (memberIds.includes(playerId)) throw new Error("Deze player staat al in Members.");
  if (alwaysIds.includes(playerId) || ruleIds.includes(playerId)) {
    throw new Error("Deze player staat al in Always add of een rule. Verwijder hem daar eerst.");
  }
}

function validateAlwaysPlayerAllowed(playerId) {
  const { memberIds, alwaysIds, ruleIds } = allConfiguredPlayerIds();
  if (memberIds.includes(playerId)) {
    throw new Error("Deze player staat al in Members en mag niet in Always add.");
  }
  if (alwaysIds.includes(playerId)) throw new Error("Deze player staat al in Always add.");
  if (ruleIds.includes(playerId)) {
    throw new Error("Deze player staat al in een rule. Verwijder hem daar eerst.");
  }
}

function renderPlayerChip(root, { playerId, name, className, onRemove }) {
  const chip = document.createElement("div");
  chip.className = `player-chip ${className || ""}`;
  chip.dataset.playerId = playerId;
  chip.dataset.name = name || playerName(playerId);

  const text = document.createElement("div");
  const strong = document.createElement("strong");
  strong.textContent = chip.dataset.name;
  const code = document.createElement("code");
  code.textContent = playerId;
  text.appendChild(strong);
  text.appendChild(code);

  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "danger";
  remove.textContent = "Remove";
  remove.addEventListener("click", () => {
    chip.remove();
    if (onRemove) onRemove(playerId);
  });

  chip.appendChild(text);
  chip.appendChild(remove);
  root.appendChild(chip);
}

function addPlayerChip(root, player, options = {}) {
  const playerId = player.encodedContactId;
  if (!playerId) throw new Error("Player heeft geen encodedContactId.");
  if ([...root.querySelectorAll(".player-chip")].some((chip) => chip.dataset.playerId === playerId)) {
    throw new Error("Deze player staat hier al.");
  }
  if (options.validate) options.validate(playerId);
  rememberPlayer(player);
  renderPlayerChip(root, {
    playerId,
    name: player.fullName || player.memberReferenceNumber || playerId,
    className: options.className,
  });
}

function renderSearchBox(root, { placeholder, onAdd }) {
  root.innerHTML = "";

  const wrap = document.createElement("div");
  wrap.className = "inline-player-search";
  wrap.innerHTML = `
    <label>
      Search player
      <input type="search" placeholder="${placeholder || "Search player"}">
    </label>
    <button type="button">Search</button>
  `;

  const results = document.createElement("div");
  results.className = "player-results";
  const input = wrap.querySelector("input");
  const button = wrap.querySelector("button");
  let debounceTimer = null;
  let lastQuery = "";

  async function doSearch() {
    const query = input.value.trim();
    if (query.length < 2) {
      results.innerHTML = '<p class="empty">Typ minimaal 2 tekens.</p>';
      return;
    }
    if (query === lastQuery) return;
    lastQuery = query;
    setStatus("Players zoeken...");
    results.innerHTML = '<p class="empty">Zoeken...</p>';
    const payload = await requestJson(`/padel/players/search?q=${encodeURIComponent(query)}`);
    renderSearchResults(results, payload.players || [], onAdd);
    setStatus("Players geladen.");
  }

  button.addEventListener("click", () => doSearch().catch((error) => setStatus(error.message, true)));
  input.addEventListener("input", () => {
    window.clearTimeout(debounceTimer);
    const query = input.value.trim();
    if (query.length < 2) {
      lastQuery = "";
      results.innerHTML = "";
      return;
    }
    debounceTimer = window.setTimeout(() => {
      doSearch().catch((error) => {
        results.innerHTML = `<p class="empty">${error.message}</p>`;
        setStatus(error.message, true);
      });
    }, 350);
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      doSearch().catch((error) => setStatus(error.message, true));
    }
  });

  root.appendChild(wrap);
  root.appendChild(results);
}

function renderSearchResults(root, players, onAdd) {
  if (!players.length) {
    root.innerHTML = '<p class="empty">Geen players gevonden.</p>';
    return;
  }

  root.innerHTML = "";
  players.forEach((player) => {
    const row = document.createElement("div");
    row.className = "player-result";

    const info = document.createElement("div");
    const name = document.createElement("strong");
    name.textContent = player.fullName || "-";
    const meta = document.createElement("span");
    meta.textContent = `${player.memberReferenceNumber || "-"} - Club ${player.homeClubSiteId || "-"}`;
    info.appendChild(name);
    info.appendChild(meta);

    const id = document.createElement("code");
    id.textContent = player.encodedContactId || "";

    const add = document.createElement("button");
    add.type = "button";
    add.textContent = "Add";
    add.addEventListener("click", () => {
      try {
        onAdd(player);
        setStatus("Player toegevoegd. Klik Save om op te slaan.");
      } catch (error) {
        setStatus(error.message, true);
      }
    });

    row.appendChild(info);
    row.appendChild(id);
    row.appendChild(add);
    root.appendChild(row);
  });
}

function renderMembers(members) {
  const root = $("members");
  root.innerHTML = "";
  members.forEach((member) => {
    ensureKnownPlayers()[member.member_id] = member.name;
    renderPlayerChip(root, { playerId: member.member_id, name: member.name, className: "member-player" });
  });
  renderSearchBox($("memberSearch"), {
    placeholder: "Search member",
    onAdd: (player) => addPlayerChip(root, player, { className: "member-player", validate: validateMemberPlayerAllowed }),
  });
}

function renderAlwaysPlayers(playerIds) {
  const root = $("alwaysPlayers");
  root.innerHTML = "";
  playerIds.forEach((playerId) => {
    renderPlayerChip(root, { playerId, name: playerName(playerId), className: "always-player" });
  });
  renderSearchBox($("alwaysPlayerSearch"), {
    placeholder: "Search always-add player",
    onAdd: (player) => addPlayerChip(root, player, { className: "always-player", validate: validateAlwaysPlayerAllowed }),
  });
}

function renderRulePlayers(root, playerIds) {
  root.innerHTML = "";
  playerIds.forEach((playerId) => {
    renderPlayerChip(root, { playerId, name: playerName(playerId), className: "rule-player" });
  });
}

function renderRules(rules) {
  const root = $("rules");
  root.innerHTML = "";

  rules.forEach((rule, index) => {
    const row = document.createElement("div");
    row.className = "rule-row";
    row.innerHTML = `
      <div class="rule-fields">
        <label>Day<select data-field="day"></select></label>
        <label>Times<input data-field="times" type="text" placeholder="18:00, 19:00"></label>
        <label>Hours<input data-field="duration" type="number" min="1" max="4"></label>
        <button type="button" class="danger" data-action="remove-rule">Remove rule</button>
      </div>
      <div class="rule-player-area">
        <h3>Rule players</h3>
        <div class="rule-players"></div>
        <div class="rule-player-search"></div>
      </div>
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

    const playersRoot = row.querySelector(".rule-players");
    renderRulePlayers(playersRoot, rule.player_ids || []);
    renderSearchBox(row.querySelector(".rule-player-search"), {
      placeholder: "Search rule player",
      onAdd: (player) => addPlayerChip(playersRoot, player, { className: "rule-player", validate: validateRulePlayerAllowed }),
    });

    row.querySelector('[data-action="remove-rule"]').addEventListener("click", () => {
      row.remove();
      renderRunSummary(collectForm());
    });
    root.appendChild(row);
  });
}

function fillForm(config) {
  state.config.padel.known_players = state.config.padel.known_players || {};
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
  renderRules(config.padel.booking_rules || []);
  renderMembers(config.padel.members || []);
  renderAlwaysPlayers(config.padel.always_add_player_ids || []);
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
  const members = [...$("members").querySelectorAll(".player-chip")].map((chip) => ({
    name: chip.dataset.name || chip.textContent.trim(),
    member_id: chip.dataset.playerId,
  })).filter((member) => member.name && member.member_id);

  const alwaysAddPlayerIds = [...$("alwaysPlayers").querySelectorAll(".player-chip")]
    .map((chip) => chip.dataset.playerId)
    .filter(Boolean);

  const rules = [...$("rules").querySelectorAll(".rule-row")].map((row) => ({
    day: row.querySelector('[data-field="day"]').value,
    times: parseTimes(row.querySelector('[data-field="times"]').value),
    duration: Number.parseInt(row.querySelector('[data-field="duration"]').value, 10),
    player_ids: [...row.querySelectorAll(".rule-players .player-chip")]
      .map((chip) => chip.dataset.playerId)
      .filter(Boolean),
  }));

  const reservedPlayerIds = new Set([...members.map((member) => member.member_id), ...alwaysAddPlayerIds]);
  const invalidRulePlayers = rules.flatMap((rule) =>
    rule.player_ids
      .filter((playerId) => reservedPlayerIds.has(playerId))
      .map((playerId) => `${rule.day}: ${playerId}`)
  );
  if (invalidRulePlayers.length) {
    throw new Error(`Rule players mogen niet ook in Members of Always add staan:\n${invalidRulePlayers.join("\n")}`);
  }

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
      known_players: ensureKnownPlayers(),
      always_add_player_ids: alwaysAddPlayerIds,
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
  await loadRunHistory();
  setStatus(result.ok ? "Run afgerond: boeking gelukt." : "Run afgerond: geen boeking.");
}

function renderRunHistory(runs) {
  const root = $("runHistory");
  if (!runs.length) {
    root.innerHTML = '<p class="empty">Nog geen runs.</p>';
    return;
  }
  root.innerHTML = "";
  runs.forEach((run) => {
    const item = document.createElement("details");
    item.className = `history-item ${run.ok ? "success" : "failed"}`;
    const message = run.error?.message || (run.ok ? "Boeking gelukt" : "Geen boeking");
    item.innerHTML = `
      <summary>
        <span class="status-dot"></span>
        <strong>${run.timestamp || "-"}</strong>
        <span>${run.source || "-"}</span>
        <span>${message}</span>
      </summary>
      <pre>${JSON.stringify(run, null, 2)}</pre>
    `;
    root.appendChild(item);
  });
}

async function loadRunHistory() {
  const payload = await requestJson("/padel/runs?limit=50");
  renderRunHistory(payload.runs || []);
}

function formatBookingTitle(booking) {
  const date = booking.date || "-";
  const time = booking.startTime || "-";
  const court = booking.courtId ? `Court ${booking.courtId}` : "Court -";
  const status = booking.status || "-";
  return `${date} ${time} - ${court} - ${status}`;
}

function renderBookings(bookings) {
  const root = $("bookingsList");
  if (!bookings.length) {
    root.innerHTML = '<p class="empty">Geen boekingen gevonden.</p>';
    return;
  }
  root.innerHTML = "";
  bookings.forEach((booking) => {
    const players = (booking.players || []).map((player) => player.name).filter(Boolean).join(", ");
    const item = document.createElement("details");
    item.className = "booking-item";
    item.innerHTML = `
      <summary>
        <strong>${formatBookingTitle(booking)}</strong>
        <span>${booking.activityName || ""}</span>
        <span>${players || "Geen spelers"}</span>
      </summary>
      <pre>${JSON.stringify(booking, null, 2)}</pre>
    `;
    root.appendChild(item);
  });
}

async function loadBookings() {
  setStatus("Boekingen laden...");
  const payload = await requestJson("/padel/bookings");
  renderBookings(payload.bookings || []);
  setStatus("Boekingen geladen.");
}

function switchTab(name) {
  document.querySelectorAll(".tab").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === name);
  });
  document.querySelectorAll(".tab-page").forEach((page) => {
    page.classList.toggle("active", page.id === `${name}Tab`);
  });
  if (name === "bookings") loadBookings().catch((error) => setStatus(error.message, true));
  if (name === "runs") loadRunHistory().catch((error) => setStatus(error.message, true));
}

function bind() {
  $("reloadBtn").addEventListener("click", () => loadConfig().catch((error) => setStatus(error.message, true)));
  $("saveBtn").addEventListener("click", () => saveConfig().catch((error) => setStatus(error.message, true)));
  $("slotsBtn").addEventListener("click", () => previewSlots().catch((error) => setStatus(error.message, true)));
  $("refreshAuthBtn").addEventListener("click", () => refreshAuth().catch((error) => setStatus(error.message, true)));
  $("freshLoginBtn").addEventListener("click", () => freshLogin().catch((error) => setStatus(error.message, true)));
  $("authStatusBtn").addEventListener("click", () => checkAuthStatus().catch((error) => setStatus(error.message, true)));
  $("runHistoryBtn").addEventListener("click", () => loadRunHistory().catch((error) => setStatus(error.message, true)));
  $("bookingsRefreshBtn").addEventListener("click", () => loadBookings().catch((error) => setStatus(error.message, true)));
  $("runNowBtn").addEventListener("click", () => runNow().catch((error) => {
    $("runResult").textContent = error.message;
    setStatus("Run mislukt.", true);
  }));
  document.querySelectorAll(".tab").forEach((button) => {
    button.addEventListener("click", () => switchTab(button.dataset.tab));
  });
  $("addRuleBtn").addEventListener("click", () => {
    const root = $("rules");
    const current = collectForm().padel.booking_rules;
    current.push({ day: "monday", times: ["18:00"], duration: 1, player_ids: [] });
    renderRules(current);
    renderRunSummary(collectForm());
  });
}

bind();
loadConfig().then(() => loadRunHistory()).catch((error) => setStatus(error.message, true));
