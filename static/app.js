// Frontend logic for the FPL AI Assistant Web App (static/app.js)

const state = {
  currentSessionId: localStorage.getItem("fpl_session_id") || null,
  managerId: localStorage.getItem("fpl_manager_id")
    ? parseInt(localStorage.getItem("fpl_manager_id"), 10)
    : null,
  squadData: null,
};

const sessionListEl = document.getElementById("session-list");
const chatMessagesEl = document.getElementById("chat-messages");
const welcomeScreenEl = document.getElementById("welcome-screen");
const chatForm = document.getElementById("chat-form");
const chatInput = document.getElementById("chat-input");
const sendBtn = document.getElementById("send-btn");
const newChatBtn = document.getElementById("new-chat-btn");
const managerInput = document.getElementById("manager-input");
const syncManagerBtn = document.getElementById("sync-manager-btn");
const managerStatusEl = document.getElementById("manager-status");

// =====================================================================
// INITIALIZATION
// =====================================================================

window.addEventListener("DOMContentLoaded", async () => {
  if (state.managerId) {
    managerInput.value = state.managerId;
    await fetchSavedSquad(state.managerId);
  }
  await loadSessionsList();
  if (state.currentSessionId) {
    await openSession(state.currentSessionId);
  }
});

// =====================================================================
// SIDEBAR NAVIGATION (Chat vs Squad vs Top Players)
// =====================================================================

document.querySelectorAll(".nav-item").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".nav-item").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".view-section").forEach((v) => v.classList.remove("active"));

    btn.classList.add("active");
    const targetView = btn.getAttribute("data-view");
    document.getElementById(targetView).classList.add("active");

    if (targetView === "squad-view" && state.managerId) {
      renderSquadOverview();
    } else if (targetView === "players-view") {
      loadTopPlayersTable();
    }
  });
});

// =====================================================================
// MANAGER ID / FPL URL ONE-TIME SYNC
// =====================================================================

syncManagerBtn.addEventListener("click", async () => {
  const rawVal = managerInput.value.trim();
  if (!rawVal) return;

  syncManagerBtn.disabled = true;
  managerStatusEl.textContent = "Syncing team from official FPL API...";

  try {
    const resp = await fetch("/api/manager/sync", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ manager_input: rawVal }),
    });
    const data = await resp.json();
    if (!resp.ok) {
      throw new Error(data.detail || "Failed to sync manager.");
    }

    state.managerId = data.manager_id;
    state.squadData = data.squad;
    localStorage.setItem("fpl_manager_id", String(state.managerId));
    managerInput.value = String(state.managerId);

    const mgr = data.squad?.manager || {};
    managerStatusEl.innerHTML =
      `<strong>${mgr.team_name || "Synced"}</strong> (ID: ${state.managerId})<br/>` +
      `Bank: £${data.squad?.bank_balance_m ?? 0}m | FT: ${data.squad?.free_transfers ?? 1}`;

    renderSquadOverview();
    await loadSessionsList();
  } catch (err) {
    managerStatusEl.textContent = `Error: ${err.message}`;
  } finally {
    syncManagerBtn.disabled = false;
  }
});

async function fetchSavedSquad(managerId) {
  try {
    const resp = await fetch(`/api/manager/${managerId}/squad`);
    if (!resp.ok) return;
    const data = await resp.json();
    if (data.found) {
      state.squadData = data;
      const mgr = data.manager || {};
      managerStatusEl.innerHTML =
        `<strong>${mgr.team_name || "Synced"}</strong> (ID: ${managerId})<br/>` +
        `Bank: £${data.bank_balance_m ?? 0}m | FT: ${data.free_transfers ?? 1}`;
    }
  } catch (_e) {
    // Ignore on initial load
  }
}

// =====================================================================
// SESSIONS & CONVERSATION HISTORY
// =====================================================================

async function loadSessionsList() {
  const url = state.managerId
    ? `/api/sessions?manager_id=${state.managerId}`
    : "/api/sessions";
  const resp = await fetch(url);
  const data = await resp.json();

  sessionListEl.innerHTML = "";
  (data.sessions || []).forEach((sess) => {
    const li = document.createElement("li");
    li.className =
      "session-item" + (sess.session_id === state.currentSessionId ? " active" : "");

    const titleSpan = document.createElement("span");
    titleSpan.className = "session-title";
    titleSpan.textContent = sess.title;
    titleSpan.addEventListener("click", () => openSession(sess.session_id));

    const delBtn = document.createElement("button");
    delBtn.className = "delete-session-btn";
    delBtn.textContent = "×";
    delBtn.title = "Delete conversation";
    delBtn.addEventListener("click", async (e) => {
      e.stopPropagation();
      await fetch(`/api/sessions/${sess.session_id}`, { method: "DELETE" });
      if (state.currentSessionId === sess.session_id) {
        startNewChat();
      }
      await loadSessionsList();
    });

    li.appendChild(titleSpan);
    li.appendChild(delBtn);
    sessionListEl.appendChild(li);
  });
}

function startNewChat() {
  state.currentSessionId = null;
  localStorage.removeItem("fpl_session_id");
  chatMessagesEl.innerHTML = "";
  chatMessagesEl.appendChild(welcomeScreenEl);
  welcomeScreenEl.style.display = "flex";
  loadSessionsList();
}

newChatBtn.addEventListener("click", startNewChat);

async function openSession(sessionId) {
  try {
    const resp = await fetch(`/api/sessions/${sessionId}`);
    if (!resp.ok) {
      startNewChat();
      return;
    }
    const data = await resp.json();
    state.currentSessionId = sessionId;
    localStorage.setItem("fpl_session_id", sessionId);

    chatMessagesEl.innerHTML = "";
    if (!data.messages || data.messages.length === 0) {
      chatMessagesEl.appendChild(welcomeScreenEl);
      welcomeScreenEl.style.display = "flex";
    } else {
      welcomeScreenEl.style.display = "none";
      data.messages.forEach((msg) => {
        appendMessageRow(msg.role, msg.content, msg.tools_used || []);
      });
    }
    await loadSessionsList();
  } catch (_err) {
    startNewChat();
  }
}

// =====================================================================
// SENDING & RENDERING CHAT MESSAGES
// =====================================================================

function appendMessageRow(role, content, toolsUsed = []) {
  welcomeScreenEl.style.display = "none";
  const row = document.createElement("div");
  row.className = `message-row ${role}`;

  if (role === "assistant" && toolsUsed && toolsUsed.length > 0) {
    const badges = document.createElement("div");
    badges.className = "tool-badges";
    toolsUsed.forEach((tName) => {
      const b = document.createElement("span");
      b.className = "tool-badge";
      b.textContent = `Tool: ${tName}`;
      badges.appendChild(b);
    });
    row.appendChild(badges);
  }

  const bubble = document.createElement("div");
  bubble.className = "message-bubble";
  if (role === "assistant" && window.marked) {
    bubble.innerHTML = window.marked.parse(content || "");
  } else {
    bubble.textContent = content;
  }

  row.appendChild(bubble);
  chatMessagesEl.appendChild(row);
  chatMessagesEl.scrollTop = chatMessagesEl.scrollHeight;
  return row;
}

async function sendUserMessage(messageText) {
  const text = messageText.trim();
  if (!text) return;

  appendMessageRow("user", text);
  chatInput.value = "";
  sendBtn.disabled = true;

  const loadingRow = appendMessageRow("assistant", "Thinking & querying FPL data...");

  try {
    const resp = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: state.currentSessionId,
        manager_id: state.managerId,
        message: text,
      }),
    });
    const data = await resp.json();
    loadingRow.remove();

    if (!resp.ok) {
      appendMessageRow("assistant", `Error: ${data.detail || "Request failed."}`);
      return;
    }

    state.currentSessionId = data.session_id;
    localStorage.setItem("fpl_session_id", data.session_id);
    appendMessageRow("assistant", data.reply, data.tools_used || []);
    await loadSessionsList();
  } catch (err) {
    loadingRow.remove();
    appendMessageRow("assistant", `Network error: ${err.message}`);
  } finally {
    sendBtn.disabled = false;
  }
}

chatForm.addEventListener("submit", (e) => {
  e.preventDefault();
  sendUserMessage(chatInput.value);
});

chatInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendUserMessage(chatInput.value);
  }
});

document.querySelectorAll(".prompt-chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    const prompt = chip.getAttribute("data-prompt");
    if (prompt) sendUserMessage(prompt);
  });
});

// =====================================================================
// EXPANDABLE LEFT-SIDEBAR FEATURES (MY SQUAD & TOP PLAYERS EXPLORER)
// =====================================================================

function renderSquadOverview() {
  const container = document.getElementById("squad-content");
  if (!state.squadData || !state.squadData.found) {
    container.innerHTML =
      "<p>No squad loaded yet. Enter your FPL Manager ID or URL in the top-left box and click <strong>Sync</strong>.</p>";
    return;
  }

  const sq = state.squadData;
  const allPlayers = [...(sq.starting_xi || []), ...(sq.bench || [])];
  const rowsHtml = allPlayers
    .map(
      (p) => `
      <tr>
        <td>${p.squad_position <= 11 ? "XI" : "Bench"}</td>
        <td><strong>${p.web_name}</strong> ${p.is_captain ? "(C)" : p.is_vice_captain ? "(VC)" : ""}</td>
        <td>${p.team}</td>
        <td>${p.position}</td>
        <td>£${p.price}m</td>
        <td>${p.form}</td>
        <td>${p.ep_next}</td>
        <td>${p.total_points}</td>
      </tr>`
    )
    .join("");

  container.innerHTML = `
    <p style="margin-bottom: 12px;">
      <strong>Team:</strong> ${sq.manager?.team_name || ""} |
      <strong>GW:</strong> ${sq.squad_gw} |
      <strong>Team Value:</strong> £${sq.team_value_m}m |
      <strong>Bank:</strong> £${sq.bank_balance_m}m |
      <strong>Free Transfers:</strong> ${sq.free_transfers ?? 1}
    </p>
    <table class="data-table">
      <thead>
        <tr>
          <th>Role</th>
          <th>Player</th>
          <th>Club</th>
          <th>Pos</th>
          <th>Price</th>
          <th>Form</th>
          <th>ep_next</th>
          <th>Total Pts</th>
        </tr>
      </thead>
      <tbody>${rowsHtml}</tbody>
    </table>
  `;
}

async function loadTopPlayersTable() {
  const pos = document.getElementById("filter-position").value;
  const sortBy = document.getElementById("filter-sort").value;
  const container = document.getElementById("players-table-container");
  container.innerHTML = "<p>Loading players...</p>";

  const params = new URLSearchParams({ sort_by: sortBy, limit: "15" });
  if (pos) params.set("position", pos);

  const resp = await fetch(`/api/players/top?${params.toString()}`);
  const data = await resp.json();
  const players = data.players || [];

  const rowsHtml = players
    .map(
      (p) => `
      <tr>
        <td><strong>${p.web_name}</strong></td>
        <td>${p.team}</td>
        <td>${p.position}</td>
        <td>£${p.price}m</td>
        <td>${p.form}</td>
        <td>${p.ep_next}</td>
        <td>${p.total_points}</td>
        <td>${p.value_points_per_m}</td>
      </tr>`
    )
    .join("");

  container.innerHTML = `
    <table class="data-table">
      <thead>
        <tr>
          <th>Player</th>
          <th>Club</th>
          <th>Pos</th>
          <th>Price</th>
          <th>Form</th>
          <th>ep_next</th>
          <th>Total Pts</th>
          <th>Pts / £m</th>
        </tr>
      </thead>
      <tbody>${rowsHtml}</tbody>
    </table>
  `;
}

document
  .getElementById("load-players-btn")
  .addEventListener("click", loadTopPlayersTable);