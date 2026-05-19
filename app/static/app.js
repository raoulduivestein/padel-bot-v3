const state = {
  config: null,
  phonebook: [],
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

async function upsertPhonebookPlayer(player, source = "tool") {
  if (!player?.encodedContactId) return;
  await requestJson("/phonebook/upsert", {
    method: "POST",
    body: JSON.stringify({
      encodedContactId: player.encodedContactId,
      fullName: player.fullName || player.name || null,
      memberReferenceNumber: player.memberReferenceNumber || null,
      homeClubSiteId: player.homeClubSiteId ?? null,
      source,
    }),
  });
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

function splitMessageTemplates(value) {
  return value
    .split(/\r?\n---\r?\n/g)
    .map((template) => template.trim())
    .filter(Boolean);
}

function joinMessageTemplates(templates, fallback) {
  const usable = (templates || []).filter(Boolean);
  if (usable.length) return usable.join("\n---\n");
  return fallback || "Padel uitnodiging: {date} om {time} bij {club_name}.\n---\n{invite_url}";
}

function collectInviteMessageTemplates() {
  return splitMessageTemplates($("inviteMessageTemplates").value);
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
  if (options.maxPlayers && root.querySelectorAll(".player-chip").length >= options.maxPlayers) {
    throw new Error(`Een boeking kan maximaal ${options.maxPlayers} spelers hebben.`);
  }
  if ([...root.querySelectorAll(".player-chip")].some((chip) => chip.dataset.playerId === playerId)) {
    throw new Error("Deze player staat hier al.");
  }
  if (options.validate) options.validate(playerId);
  rememberPlayer(player);
  upsertPhonebookPlayer(player).catch((error) => setStatus(error.message, true));
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
  $("inviteMessageTemplates").value = joinMessageTemplates(
    config.padel.invite_message_templates,
    config.padel.invite_message_template
  );
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
  const inviteMessageTemplates = collectInviteMessageTemplates();

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
      invite_message_template: inviteMessageTemplates[0] || "",
      invite_message_templates: inviteMessageTemplates,
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

async function saveInviteMessages() {
  const templates = collectInviteMessageTemplates();
  if (!templates.length) throw new Error("Vul minimaal een invite message in.");
  setStatus("Invite messages opslaan...");
  const result = await requestJson("/api/invite-messages", {
    method: "PUT",
    body: JSON.stringify({ invite_message_templates: templates }),
  });
  state.config.padel.invite_message_template = result.invite_message_template;
  state.config.padel.invite_message_templates = result.invite_message_templates;
  $("inviteMessageTemplates").value = joinMessageTemplates(result.invite_message_templates, result.invite_message_template);
  setStatus("Invite messages opgeslagen.");
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
    body: JSON.stringify({ attempts, fresh_login: true }),
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
  return `${date} ${time} - ${court}`;
}

function bookingDurationLabel(booking) {
  const duration = booking.duration;
  if (!duration) return "-";
  return `${duration} min`;
}

function bookingPlayerIds(root) {
  return [...root.querySelectorAll(".player-chip")]
    .map((chip) => chip.dataset.playerId)
    .filter(Boolean);
}

function phonebookPlayersWithPhone() {
  return (state.phonebook || []).filter((player) => player.encodedContactId && player.phone);
}

function normalizePhone(value) {
  let digits = String(value || "").replace(/\D+/g, "");
  if (digits.startsWith("00")) digits = digits.slice(2);
  if (digits.startsWith("0")) digits = `31${digits.slice(1)}`;
  return digits;
}

async function saveBookingPlayers(booking, playersRoot) {
  if (!booking.encodedBookingReference) {
    throw new Error("Deze boeking mist encodedBookingReference.");
  }
  if (bookingPlayerIds(playersRoot).length > 4) {
    throw new Error("Een boeking kan maximaal 4 spelers hebben.");
  }
  setStatus("Booking spelers opslaan...");
  const result = await requestJson("/padel/bookings/players", {
    method: "PUT",
    body: JSON.stringify({
      encodedBookingReference: booking.encodedBookingReference,
      playersEncodedContactIds: bookingPlayerIds(playersRoot),
    }),
  });
  await loadBookings();
  setStatus("Booking spelers opgeslagen.");
  return result;
}

function renderBookingPlayers(root, players) {
  root.innerHTML = "";
  (players || []).forEach((player) => {
    if (!player.encodedContactId) return;
    const normalized = {
      encodedContactId: player.encodedContactId,
      fullName: player.name || player.fullName || player.encodedContactId,
      memberReferenceNumber: player.memberReferenceNumber || null,
      homeClubSiteId: player.homeClubSiteId ?? null,
    };
    rememberPlayer(normalized);
    renderPlayerChip(root, {
      playerId: normalized.encodedContactId,
      name: normalized.fullName,
      className: "booking-player",
    });
  });
}

function renderBookings(bookings) {
  const root = $("bookingsList");
  if (!bookings.length) {
    root.innerHTML = '<p class="empty">Geen boekingen gevonden.</p>';
    return;
  }
  root.innerHTML = "";
  bookings.forEach((booking) => {
    const item = document.createElement("div");
    item.className = "booking-card";

    const head = document.createElement("div");
    head.className = "booking-card-head";
    head.innerHTML = `
      <div>
        <strong>${formatBookingTitle(booking)}</strong>
        <span>${booking.activityName || "Padel"} - ${booking.clubName || "Club"} - ${booking.status || "-"}</span>
      </div>
      <div class="booking-metrics">
        <span>${bookingDurationLabel(booking)}</span>
        <span>${booking.players?.length || 0} players</span>
      </div>
    `;

    const playersRoot = document.createElement("div");
    playersRoot.className = "booking-players";
    renderBookingPlayers(playersRoot, booking.players || []);

    const editor = document.createElement("div");
    editor.className = "booking-editor";

    const searchRoot = document.createElement("div");
    searchRoot.className = "booking-player-search";
    renderSearchBox(searchRoot, {
      placeholder: "Search player to add",
      onAdd: (player) => addPlayerChip(playersRoot, player, { className: "booking-player", maxPlayers: 4 }),
    });

    const invite = document.createElement("div");
    invite.className = "booking-invite";
    const currentIds = new Set(bookingPlayerIds(playersRoot));
    const inviteOptions = phonebookPlayersWithPhone().filter((player) =>
      !currentIds.has(player.encodedContactId)
    );
    const select = document.createElement("select");
    const empty = document.createElement("option");
    empty.value = "";
    empty.textContent = inviteOptions.length ? "Select player from phonebook" : "No inviteable phonebook players";
    select.appendChild(empty);
    inviteOptions.forEach((player) => {
      const option = document.createElement("option");
      option.value = player.encodedContactId;
      option.textContent = `${player.fullName || player.encodedContactId} - ${player.phone}`;
      select.appendChild(option);
    });
    const inviteButton = document.createElement("button");
    inviteButton.type = "button";
    inviteButton.textContent = "Send WhatsApp invite";
    inviteButton.disabled = bookingPlayerIds(playersRoot).length >= 4;
    inviteButton.addEventListener("click", async () => {
      const player = inviteOptions.find((item) => item.encodedContactId === select.value);
      if (!player) {
        setStatus("Selecteer eerst een speler met telefoonnummer.", true);
        return;
      }
      if (bookingPlayerIds(playersRoot).length >= 4) {
        setStatus("Deze boeking heeft al 4 spelers.", true);
        return;
      }
      setStatus("WhatsApp invite versturen...");
      inviteButton.disabled = true;
      inviteButton.textContent = `Sending to ${player.phone}`;
      try {
        await requestJson("/bookings/invites/send", {
          method: "POST",
          body: JSON.stringify({
            encodedBookingReference: booking.encodedBookingReference,
            booking,
            player: {
              encodedContactId: player.encodedContactId,
              fullName: player.fullName,
              phone: player.phone,
              memberReferenceNumber: player.memberReferenceNumber,
              homeClubSiteId: player.homeClubSiteId,
            },
          }),
        });
        await loadInvites();
        setStatus("WhatsApp invite verstuurd.");
      } catch (error) {
        setStatus(error.message, true);
      } finally {
        inviteButton.disabled = false;
        inviteButton.textContent = "Send WhatsApp invite";
      }
    });
    invite.appendChild(select);
    invite.appendChild(inviteButton);

    const actions = document.createElement("div");
    actions.className = "booking-actions";
    const save = document.createElement("button");
    save.type = "button";
    save.className = "primary";
    save.textContent = "Save players";
    save.addEventListener("click", () => {
      saveBookingPlayers(booking, playersRoot).catch((error) => setStatus(error.message, true));
    });
    const raw = document.createElement("details");
    raw.className = "booking-raw";
    raw.innerHTML = `<summary>Raw booking</summary><pre>${JSON.stringify(booking, null, 2)}</pre>`;
    actions.appendChild(save);
    actions.appendChild(raw);

    editor.appendChild(playersRoot);
    editor.appendChild(searchRoot);
    editor.appendChild(invite);
    editor.appendChild(actions);
    item.appendChild(head);
    item.appendChild(editor);
    root.appendChild(item);
  });
}

async function loadBookings() {
  setStatus("Boekingen laden...");
  await loadPhonebook({ quiet: true });
  const payload = await requestJson("/padel/bookings");
  renderBookings(payload.bookings || []);
  await loadInvites();
  setStatus("Boekingen geladen.");
}

function renderInvites(invites) {
  const roots = [...document.querySelectorAll("[data-invites-list]")];
  roots.forEach((root) => {
    if (!invites.length) {
      root.innerHTML = '<p class="empty">Nog geen uitnodigingen.</p>';
      return;
    }
    root.innerHTML = "";
    invites.forEach((invite) => {
      const booking = invite.booking || {};
      const player = invite.player || {};
      const item = document.createElement("div");
      item.className = `invite-item invite-${invite.status || "unknown"}`;
      const canCancel = ["pending", "sent", "send_failed"].includes(invite.status);
      item.innerHTML = `
        <div>
          <strong>${player.fullName || player.encodedContactId || "-"}</strong>
          <span>${booking.date || "-"} ${booking.startTime || "-"} - Court ${booking.courtId || "-"}</span>
          <code>${invite.token || ""}</code>
        </div>
        <span class="invite-status">${invite.status || "-"}</span>
      `;
      const actions = document.createElement("div");
      actions.className = "invite-actions";
      const open = document.createElement("a");
      open.href = `/invite/${invite.token}`;
      open.target = "_blank";
      open.rel = "noreferrer";
      open.textContent = "Open";
      actions.appendChild(open);
      if (canCancel) {
        const cancel = document.createElement("button");
        cancel.type = "button";
        cancel.className = "danger";
        cancel.textContent = "Intrekken";
        cancel.addEventListener("click", async () => {
          setStatus("Uitnodiging intrekken...");
          await requestJson(`/bookings/invites/${encodeURIComponent(invite.token)}/cancel`, { method: "POST" });
          await loadInvites();
          setStatus("Uitnodiging ingetrokken.");
        });
        actions.appendChild(cancel);
      }
      item.appendChild(actions);
      root.appendChild(item);
    });
  });
}

async function loadInvites() {
  const payload = await requestJson("/bookings/invites");
  renderInvites(payload.invites || []);
}

async function checkWhatsAppStatus() {
  setStatus("WhatsApp status laden...");
  const result = await requestJson("/whatsapp/status");
  $("whatsappStatus").textContent = JSON.stringify(result, null, 2);
  if (result.logged_in) setStatus("WhatsApp gekoppeld.");
  else if (result.loading_chats) setStatus("WhatsApp laadt chats. Klik Reload Web als dit blijft hangen.");
  else setStatus("Scan de WhatsApp QR-code.");
  return result;
}

async function refreshWhatsAppQr() {
  $("whatsappQrError").textContent = "";
  $("whatsappQr").removeAttribute("src");
  setStatus("WhatsApp QR laden...");
  const response = await fetch(`/whatsapp/qr?ts=${Date.now()}`, { cache: "no-store" });
  if (!response.ok) {
    const text = await response.text();
    $("whatsappQrError").textContent = text;
    throw new Error(text);
  }
  const blob = await response.blob();
  const oldUrl = $("whatsappQr").dataset.objectUrl;
  if (oldUrl) URL.revokeObjectURL(oldUrl);
  const url = URL.createObjectURL(blob);
  $("whatsappQr").dataset.objectUrl = url;
  $("whatsappQr").src = url;
  setStatus("WhatsApp QR geladen. Scan deze met je telefoon.");
}

async function reloadWhatsAppWeb() {
  setStatus("WhatsApp Web reload...");
  const result = await requestJson("/whatsapp/reload", { method: "POST" });
  $("whatsappStatus").textContent = JSON.stringify(result, null, 2);
  setStatus(result.logged_in ? "WhatsApp gekoppeld." : "WhatsApp Web is herladen.");
}

function renderPhonebook(players) {
  const root = $("phonebookList");
  const query = ($("phonebookFilter")?.value || "").trim().toLowerCase();
  const filtered = players.filter((player) => {
    const text = [
      player.fullName,
      player.phone,
      player.memberReferenceNumber,
      player.encodedContactId,
      (player.sources || []).join(" "),
    ].filter(Boolean).join(" ").toLowerCase();
    return !query || text.includes(query);
  });

  if (!filtered.length) {
    root.innerHTML = '<p class="empty">Nog geen spelers in het telefoonboek.</p>';
    return;
  }

  root.innerHTML = "";
  filtered.forEach((player) => {
    const item = document.createElement("div");
    item.className = "phonebook-item";

    const title = document.createElement("div");
    title.className = "phonebook-title";
    const name = document.createElement("strong");
    name.textContent = player.fullName || player.encodedContactId || "-";
    const meta = document.createElement("span");
    meta.textContent = [
      player.memberReferenceNumber ? `Member ${player.memberReferenceNumber}` : null,
      player.homeClubSiteId ? `Club ${player.homeClubSiteId}` : null,
      (player.sources || []).length ? `Source: ${(player.sources || []).join(", ")}` : null,
    ].filter(Boolean).join(" - ");
    const code = document.createElement("code");
    code.textContent = player.encodedContactId || "";
    title.appendChild(name);
    title.appendChild(meta);
    title.appendChild(code);

    const fields = document.createElement("div");
    fields.className = "phonebook-fields";
    fields.innerHTML = `
      <label>Name<input data-field="name" type="text"></label>
      <label>Phone<input data-field="phone" type="tel" placeholder="+31..."></label>
      <label>Notes<input data-field="notes" type="text" placeholder="Optional"></label>
      <button type="button" class="primary">Save</button>
    `;
    fields.querySelector('[data-field="name"]').value = player.fullName || "";
    fields.querySelector('[data-field="phone"]').value = player.phone || "";
    fields.querySelector('[data-field="notes"]').value = player.notes || "";
    fields.querySelector("button").addEventListener("click", async () => {
      setStatus("Phonebook opslaan...");
      await requestJson("/phonebook", {
        method: "PUT",
        body: JSON.stringify({
          encodedContactId: player.encodedContactId,
          fullName: fields.querySelector('[data-field="name"]').value,
          phone: fields.querySelector('[data-field="phone"]').value,
          notes: fields.querySelector('[data-field="notes"]').value,
        }),
      });
      await loadPhonebook({ quiet: true });
      setStatus("Phonebook opgeslagen.");
    });

    item.appendChild(title);
    item.appendChild(fields);
    root.appendChild(item);
  });
}

async function loadPhonebook(options = {}) {
  if (!options.quiet) setStatus("Phonebook laden...");
  const payload = await requestJson("/phonebook");
  state.phonebook = payload.players || [];
  renderPhonebook(state.phonebook);
  if (!options.quiet) setStatus("Phonebook geladen.");
}

function switchTab(name) {
  document.querySelectorAll(".tab").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === name);
  });
  document.querySelectorAll(".tab-page").forEach((page) => {
    page.classList.toggle("active", page.id === `${name}Tab`);
  });
  if (name === "bookings") loadBookings().catch((error) => setStatus(error.message, true));
  if (name === "invites") loadInvites().catch((error) => setStatus(error.message, true));
  if (name === "phonebook") loadPhonebook().catch((error) => setStatus(error.message, true));
  if (name === "whatsapp") {
    checkWhatsAppStatus()
      .then((result) => {
        if (result.needs_qr) return refreshWhatsAppQr();
        return null;
      })
      .catch((error) => setStatus(error.message, true));
  }
  if (name === "runs") loadRunHistory().catch((error) => setStatus(error.message, true));
}

function bind() {
  $("reloadBtn").addEventListener("click", () => loadConfig().catch((error) => setStatus(error.message, true)));
  $("saveBtn").addEventListener("click", () => saveConfig().catch((error) => setStatus(error.message, true)));
  $("saveInviteMessagesBtn").addEventListener("click", () => saveInviteMessages().catch((error) => setStatus(error.message, true)));
  $("slotsBtn").addEventListener("click", () => previewSlots().catch((error) => setStatus(error.message, true)));
  $("refreshAuthBtn").addEventListener("click", () => refreshAuth().catch((error) => setStatus(error.message, true)));
  $("freshLoginBtn").addEventListener("click", () => freshLogin().catch((error) => setStatus(error.message, true)));
  $("authStatusBtn").addEventListener("click", () => checkAuthStatus().catch((error) => setStatus(error.message, true)));
  $("runHistoryBtn").addEventListener("click", () => loadRunHistory().catch((error) => setStatus(error.message, true)));
  $("bookingsRefreshBtn").addEventListener("click", () => loadBookings().catch((error) => setStatus(error.message, true)));
  $("invitesRefreshBtn").addEventListener("click", () => loadInvites().catch((error) => setStatus(error.message, true)));
  $("invitesTabRefreshBtn").addEventListener("click", () => loadInvites().catch((error) => setStatus(error.message, true)));
  $("phonebookRefreshBtn").addEventListener("click", () => loadPhonebook().catch((error) => setStatus(error.message, true)));
  $("phonebookFilter").addEventListener("input", () => renderPhonebook(state.phonebook));
  $("whatsappStatusBtn").addEventListener("click", () => checkWhatsAppStatus().catch((error) => setStatus(error.message, true)));
  $("whatsappQrBtn").addEventListener("click", () => refreshWhatsAppQr().catch((error) => setStatus(error.message, true)));
  $("whatsappReloadBtn").addEventListener("click", () => reloadWhatsAppWeb().catch((error) => setStatus(error.message, true)));
  $("whatsappQr").addEventListener("error", () => {
    setStatus("WhatsApp QR kon niet worden geladen.", true);
  });
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
