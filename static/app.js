// Frontend logic for the AskFPL AI Assistant Web App (static/app.js)

const state = {
  currentSessionId: localStorage.getItem("fpl_session_id") || null,
  managerId: localStorage.getItem("fpl_manager_id")
    ? parseInt(localStorage.getItem("fpl_manager_id"), 10)
    : null,
  squadData: null,
  squadLoading: false,
  sessionsLoading: false,
  allPlayers: [],
  allTeams: [],
  playersLoading: false,
  playersLoaded: false,
  playersLoadPromise: null,
  playersError: null,
  sortField: "total_points",
  sortDir: "desc",
  activeModalPlayerId: null,
};

const appLayoutEl = document.getElementById("app-layout");
const sidebarBackdropEl = document.getElementById("sidebar-backdrop");
const openSidebarBtn = document.getElementById("open-sidebar-btn");
const closeSidebarBtn = document.getElementById("close-sidebar-btn");
const themeToggleBtn = document.getElementById("theme-toggle-btn");
const themeToggleLabel = document.getElementById("theme-toggle-label");
const topbarTitleEl = document.getElementById("topbar-title");
const brandHomeLink = document.getElementById("brand-home-link");

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

// Players Explorer elements
const filterSearchEl = document.getElementById("filter-search");
const filterTeamEl = document.getElementById("filter-team");
const filterPosEl = document.getElementById("filter-position");
const filterMaxPriceEl = document.getElementById("filter-max-price");
const filterSortEl = document.getElementById("filter-sort");
const loadPlayersBtn = document.getElementById("load-players-btn");
const playersCountBadgeEl = document.getElementById("players-count-badge");
const playersTableContainerEl = document.getElementById("players-table-container");

// Player Modal elements
const playerModalEl = document.getElementById("player-modal");
const playerModalBackdropEl = document.getElementById("player-modal-backdrop");
const playerModalHeaderEl = document.getElementById("player-modal-header");
const playerModalBodyEl = document.getElementById("player-modal-body");
const closePlayerModalBtn = document.getElementById("close-player-modal-btn");

let chartTooltipEl = null;

// =====================================================================
// UTILITIES
// =====================================================================

function escapeHtml(val) {
  return String(val ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function num(val, fallback = 0) {
  const n = Number(val);
  return Number.isFinite(n) ? n : fallback;
}

function fmt(val, decimals = 1) {
  return num(val, 0).toFixed(decimals);
}

function renderLoadingHtml(title, subtext = "", inline = false) {
  return `
    <div class="loading-state ${inline ? "loading-state-inline" : ""}">
      <div class="spinner ${inline ? "spinner-sm" : ""}" aria-hidden="true"></div>
      <div>
        <div class="loading-title">${escapeHtml(title)}</div>
        ${subtext ? `<div class="loading-subtext">${escapeHtml(subtext)}</div>` : ""}
      </div>
    </div>
  `;
}

function getStatusLabel(status, chance) {
  const s = (status || "a").toLowerCase();
  if (s === "a") return { cls: "status-a", text: "Available" };
  if (s === "d") return { cls: "status-d", text: `Doubtful (${chance ?? "?"}%)` };
  if (s === "i") return { cls: "status-i", text: "Injured" };
  if (s === "s") return { cls: "status-s", text: "Suspended" };
  if (s === "u") return { cls: "status-u", text: "Unavailable" };
  return { cls: "status-n", text: "Not in squad" };
}

// =====================================================================
// THEME TOGGLE (DARK MODE / LIGHT MODE)
// =====================================================================

function applyTheme(theme) {
  const normalized = theme === "light" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", normalized);
  localStorage.setItem("fpl_theme", normalized);
  if (themeToggleLabel) {
    themeToggleLabel.textContent = normalized === "dark" ? "Light Mode" : "Dark Mode";
  }
  if (themeToggleBtn) {
    themeToggleBtn.title =
      normalized === "dark" ? "Switch to Light Mode" : "Switch to Dark Mode";
  }
}

function initTheme() {
  const current =
    document.documentElement.getAttribute("data-theme") ||
    localStorage.getItem("fpl_theme") ||
    "dark";
  applyTheme(current);
}

if (themeToggleBtn) {
  themeToggleBtn.addEventListener("click", () => {
    const current = document.documentElement.getAttribute("data-theme") || "dark";
    applyTheme(current === "dark" ? "light" : "dark");
  });
}

// =====================================================================
// SIDEBAR OPEN / CLOSE CONTROLS
// =====================================================================

function setSidebarCollapsed(collapsed, persist = true) {
  appLayoutEl.classList.toggle("sidebar-collapsed", collapsed);
  if (persist) {
    localStorage.setItem("fpl_sidebar_collapsed", collapsed ? "1" : "0");
  }
}

function initSidebarState() {
  const saved = localStorage.getItem("fpl_sidebar_collapsed");
  if (saved !== null) {
    setSidebarCollapsed(saved === "1", false);
  } else if (window.innerWidth <= 840) {
    setSidebarCollapsed(true, false);
  }
}

if (closeSidebarBtn) {
  closeSidebarBtn.addEventListener("click", () => setSidebarCollapsed(true));
}

if (openSidebarBtn) {
  openSidebarBtn.addEventListener("click", () => setSidebarCollapsed(false));
}

if (sidebarBackdropEl) {
  sidebarBackdropEl.addEventListener("click", () => setSidebarCollapsed(true, false));
}

// =====================================================================
// PROGRESSIVE NON-BLOCKING INITIALIZATION
// =====================================================================

window.addEventListener("DOMContentLoaded", () => {
  // 1) Immediately render interactive shell (theme, sidebar, welcome screen)
  initTheme();
  initSidebarState();

  // 2) Fire independent background data loads progressively without blocking UI
  if (state.managerId) {
    managerInput.value = state.managerId;
    fetchSavedSquad(state.managerId);
  }

  loadSessionsList();

  if (state.currentSessionId) {
    openSession(state.currentSessionId);
  }

  // Warm-up full players list in background after initial paint
  setTimeout(() => {
    fetchAllPlayers(false);
  }, 80);
});

// =====================================================================
// SIDEBAR NAVIGATION (Chat vs Squad vs Players Explorer)
// =====================================================================

function activateView(targetView) {
  document.querySelectorAll(".nav-item").forEach((b) => {
    const isTarget = b.getAttribute("data-view") === targetView;
    b.classList.toggle("active", isTarget);
    if (isTarget && topbarTitleEl) {
      topbarTitleEl.textContent = b.getAttribute("data-title") || "AskFPL";
    }
  });
  document.querySelectorAll(".view-section").forEach((v) => {
    v.classList.toggle("active", v.id === targetView);
  });

  if (targetView === "squad-view") {
    renderSquadOverview();
  } else if (targetView === "players-view") {
    loadPlayersExplorer();
  }

  if (window.innerWidth <= 840) {
    setSidebarCollapsed(true, false);
  }
}

document.querySelectorAll(".nav-item").forEach((btn) => {
  btn.addEventListener("click", () => {
    const targetView = btn.getAttribute("data-view");
    if (targetView) activateView(targetView);
  });
});

if (brandHomeLink) {
  brandHomeLink.addEventListener("click", (e) => {
    e.preventDefault();
    activateView("chat-view");
  });
}

// =====================================================================
// MANAGER ID / FPL URL ONE-TIME SYNC
// =====================================================================

async function syncManager() {
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
      `<strong>${escapeHtml(mgr.team_name || "Synced")}</strong> (ID: ${state.managerId})<br/>` +
      `Bank: £${data.squad?.bank_balance_m ?? 0}m · FT: ${data.squad?.free_transfers ?? 1}`;

    renderSquadOverview();
    loadSessionsList();
  } catch (err) {
    managerStatusEl.textContent = `Error: ${err.message}`;
  } finally {
    syncManagerBtn.disabled = false;
  }
}

syncManagerBtn.addEventListener("click", syncManager);

managerInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    syncManager();
  }
});

async function fetchSavedSquad(managerId) {
  state.squadLoading = true;
  managerStatusEl.textContent = "Loading manager info...";
  if (document.getElementById("squad-view").classList.contains("active")) {
    renderSquadOverview();
  }

  try {
    const resp = await fetch(`/api/manager/${managerId}/squad`);
    if (!resp.ok) return;
    const data = await resp.json();
    if (data.found) {
      state.squadData = data;
      const mgr = data.manager || {};
      managerStatusEl.innerHTML =
        `<strong>${escapeHtml(mgr.team_name || "Synced")}</strong> (ID: ${managerId})<br/>` +
        `Bank: £${data.bank_balance_m ?? 0}m · FT: ${data.free_transfers ?? 1}`;
    } else {
      managerStatusEl.textContent = "No Manager ID linked yet.";
    }
  } catch (_e) {
    managerStatusEl.textContent = "No Manager ID linked yet.";
  } finally {
    state.squadLoading = false;
    if (document.getElementById("squad-view").classList.contains("active")) {
      renderSquadOverview();
    }
  }
}

// =====================================================================
// SESSIONS & CONVERSATION HISTORY
// =====================================================================

async function loadSessionsList() {
  if (!sessionListEl.children.length) {
    sessionListEl.innerHTML = `<li class="session-loading">${renderLoadingHtml("Đang tải lịch sử...", "", true)}</li>`;
  }

  try {
    const url = state.managerId
      ? `/api/sessions?manager_id=${state.managerId}`
      : "/api/sessions";
    const resp = await fetch(url);
    if (!resp.ok) {
      sessionListEl.innerHTML = "";
      return;
    }
    const data = await resp.json();

    sessionListEl.innerHTML = "";
    (data.sessions || []).forEach((sess) => {
      const li = document.createElement("li");
      li.className =
        "session-item" + (sess.session_id === state.currentSessionId ? " active" : "");

      const titleSpan = document.createElement("span");
      titleSpan.className = "session-title";
      titleSpan.textContent = sess.title;
      titleSpan.addEventListener("click", () => {
        activateView("chat-view");
        openSession(sess.session_id);
      });

      const delBtn = document.createElement("button");
      delBtn.className = "delete-session-btn";
      delBtn.textContent = "×";
      delBtn.title = "Delete conversation";
      delBtn.setAttribute("aria-label", "Delete conversation");
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
  } catch (_err) {
    sessionListEl.innerHTML = "";
  }
}

function startNewChat() {
  state.currentSessionId = null;
  localStorage.removeItem("fpl_session_id");
  activateView("chat-view");
  chatMessagesEl.innerHTML = "";
  chatMessagesEl.appendChild(welcomeScreenEl);
  welcomeScreenEl.style.display = "flex";
  loadSessionsList();
}

newChatBtn.addEventListener("click", startNewChat);

async function openSession(sessionId) {
  welcomeScreenEl.style.display = "none";
  chatMessagesEl.innerHTML = renderLoadingHtml(
    "Đang tải cuộc trò chuyện...",
    "Đang lấy lịch sử tin nhắn từ cơ sở dữ liệu"
  );

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

  if (role === "assistant") {
    const layout = document.createElement("div");
    layout.className = "assistant-msg-layout";

    const avatar = document.createElement("div");
    avatar.className = "assistant-avatar";
    const img = document.createElement("img");
    img.src = "/static/images/askFPL_avatar_64.png?v=6";
    img.alt = "AskFPL";
    img.width = 32;
    img.height = 32;
    avatar.appendChild(img);

    const contentCol = document.createElement("div");
    contentCol.className = "assistant-content";

    if (toolsUsed && toolsUsed.length > 0) {
      const badges = document.createElement("div");
      badges.className = "tool-badges";
      toolsUsed.forEach((tName) => {
        const b = document.createElement("span");
        b.className = "tool-badge";
        b.textContent = `Tool: ${tName}`;
        badges.appendChild(b);
      });
      contentCol.appendChild(badges);
    }

    const bubble = document.createElement("div");
    bubble.className = "message-bubble";
    if (window.marked) {
      bubble.innerHTML = window.marked.parse(content || "");
    } else {
      bubble.textContent = content;
    }

    contentCol.appendChild(bubble);
    layout.appendChild(avatar);
    layout.appendChild(contentCol);
    row.appendChild(layout);
  } else {
    const bubble = document.createElement("div");
    bubble.className = "message-bubble";
    bubble.textContent = content;
    row.appendChild(bubble);
  }

  chatMessagesEl.appendChild(row);
  chatMessagesEl.scrollTop = chatMessagesEl.scrollHeight;
  return row;
}

function autoResizeTextarea() {
  chatInput.style.height = "auto";
  chatInput.style.height = Math.min(chatInput.scrollHeight, 150) + "px";
}

chatInput.addEventListener("input", autoResizeTextarea);

async function sendUserMessage(messageText) {
  const text = messageText.trim();
  if (!text) return;

  appendMessageRow("user", text);
  chatInput.value = "";
  autoResizeTextarea();
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
// SHARED PLAYER ROW & SQUAD OVERVIEW
// =====================================================================

const SHARED_PLAYER_STAT_HEADERS = [
  { key: "web_name", label: "Player", cls: "" },
  { key: "team", label: "Club", cls: "" },
  { key: "position", label: "Pos", cls: "" },
  { key: "price", label: "Price", cls: "num-col" },
  { key: "selected_by_percent", label: "TSB%", cls: "num-col" },
  { key: "total_points", label: "Pts", cls: "num-col" },
  { key: "form", label: "Form", cls: "num-col" },
  { key: "points_per_game", label: "PPG", cls: "num-col" },
  { key: "ep_next", label: "ep_next", cls: "num-col" },
  { key: "minutes", label: "Min", cls: "num-col" },
  { key: "goals_scored", label: "G", cls: "num-col" },
  { key: "assists", label: "A", cls: "num-col" },
  { key: "clean_sheets", label: "CS", cls: "num-col" },
  { key: "xG", label: "xG", cls: "num-col" },
  { key: "xA", label: "xA", cls: "num-col" },
  { key: "xGI", label: "xGI", cls: "num-col" },
  { key: "ict_index", label: "ICT", cls: "num-col" },
  { key: "bonus", label: "Bon", cls: "num-col" },
  { key: "bps", label: "BPS", cls: "num-col" },
  { key: "value_points_per_m", label: "Pts/£m", cls: "num-col" },
];

function renderRichPlayerRow(p, firstCellHtml, extraBadgeHtml = "") {
  const pid = p.id ?? p.player_id;
  const st = getStatusLabel(p.status, p.chance_of_playing_next_round);
  const newsTitle = p.news ? `${st.text}: ${p.news}` : st.text;
  const valPerM =
    p.value_points_per_m !== undefined && p.value_points_per_m !== null
      ? p.value_points_per_m
      : num(p.price) > 0
        ? num(p.total_points) / num(p.price)
        : 0;

  return `
    <tr class="player-row-clickable" data-player-id="${pid}" title="Click to view ${escapeHtml(p.web_name)} Gameweek points chart">
      ${firstCellHtml}
      <td>
        <span class="player-name-cell">
          <span class="status-dot ${st.cls}" title="${escapeHtml(newsTitle)}"></span>
          <strong>${escapeHtml(p.web_name)}</strong>${extraBadgeHtml}
          <svg class="chart-hint-icon" width="14" height="14" viewBox="0 0 24 24" fill="none"
            stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <line x1="18" y1="20" x2="18" y2="10" />
            <line x1="12" y1="20" x2="12" y2="4" />
            <line x1="6" y1="20" x2="6" y2="14" />
          </svg>
        </span>
      </td>
      <td>${escapeHtml(p.team)}</td>
      <td><span class="pos-badge pos-${escapeHtml(p.position)}">${escapeHtml(p.position)}</span></td>
      <td class="num-col">£${fmt(p.price, 1)}m</td>
      <td class="num-col">${fmt(p.selected_by_percent, 1)}%</td>
      <td class="num-col"><strong>${num(p.total_points)}</strong></td>
      <td class="num-col">${fmt(p.form, 1)}</td>
      <td class="num-col">${fmt(p.points_per_game, 1)}</td>
      <td class="num-col">${fmt(p.ep_next, 1)}</td>
      <td class="num-col">${num(p.minutes)}</td>
      <td class="num-col">${num(p.goals_scored)}</td>
      <td class="num-col">${num(p.assists)}</td>
      <td class="num-col">${num(p.clean_sheets)}</td>
      <td class="num-col">${fmt(p.xG, 2)}</td>
      <td class="num-col">${fmt(p.xA, 2)}</td>
      <td class="num-col">${fmt(p.xGI, 2)}</td>
      <td class="num-col">${fmt(p.ict_index, 1)}</td>
      <td class="num-col">${num(p.bonus)}</td>
      <td class="num-col">${num(p.bps)}</td>
      <td class="num-col">${fmt(valPerM, 1)}</td>
    </tr>`;
}

function getTeamPointTierClass(pts) {
  if (pts >= 70) return "tier-haul";
  if (pts >= 55) return "tier-good";
  if (pts >= 40) return "tier-normal";
  return "tier-low";
}

function renderTeamGwChart(gwHistory) {
  if (!gwHistory || gwHistory.length === 0) {
    return `
      <div class="gw-chart-section">
        <div class="gw-chart-header">
          <h4>Team Gameweek Points Chart</h4>
        </div>
        <p class="empty-state-text">Chưa có dữ liệu điểm từng vòng đấu cho đội hình này. Hãy bấm <strong>Sync</strong> lại để cập nhật.</p>
      </div>
    `;
  }

  const count = gwHistory.length;
  const ptsValues = gwHistory.map((g) => num(g.points));
  const fplAvgValues = gwHistory
    .map((g) => (g.gw_average !== null && g.gw_average !== undefined ? num(g.gw_average) : null))
    .filter((v) => v !== null);

  const minVal = Math.min(0, ...ptsValues);
  const maxVal = Math.max(60, ...ptsValues, ...fplAvgValues) + 6;
  const span = Math.max(1, maxVal - minVal);
  const avgPts = ptsValues.reduce((s, v) => s + v, 0) / count;

  const padL = 40;
  const padR = 26;
  const padT = 18;
  const padB = 22;
  const slotW = count > 24 ? 32 : count > 14 ? 44 : 64;
  const svgW = Math.max(960, padL + padR + count * slotW);
  const svgH = 128;
  const plotH = svgH - padT - padB;
  const plotW = svgW - padL - padR;

  const yOf = (val) => padT + ((maxVal - val) / span) * plotH;
  const yZero = yOf(0);
  const yAvg = yOf(avgPts);

  const step = Math.max(10, Math.ceil(span / 4 / 5) * 5);
  const gridLines = [];
  for (let tick = 0; tick <= maxVal; tick += step) {
    const y = yOf(tick);
    if (y >= padT - 4 && y <= svgH - padB + 4) {
      gridLines.push(`
        <line x1="${padL}" y1="${y.toFixed(1)}" x2="${svgW - padR}" y2="${y.toFixed(1)}" class="gw-grid-line" />
        <text x="${padL - 8}" y="${(y + 4).toFixed(1)}" text-anchor="end" class="gw-axis-label">${tick}</text>
      `);
    }
  }

  const barW = Math.max(14, Math.min(28, slotW * 0.48));
  const trendPoints = [];
  const fplAvgPoints = [];

  const barsSvg = gwHistory
    .map((g, i) => {
      const pts = num(g.points);
      const cx = padL + (i + 0.5) * (plotW / count);
      const x = cx - barW / 2;
      const yVal = yOf(pts);
      const barTop = Math.min(yVal, yZero);
      const barH = Math.max(2, Math.abs(yZero - yVal));
      const labelY = pts >= 0 ? barTop - 5 : barTop + barH + 11;
      const tier = getTeamPointTierClass(pts);
      trendPoints.push(`${cx.toFixed(1)},${yVal.toFixed(1)}`);

      if (g.gw_average !== null && g.gw_average !== undefined && num(g.gw_average) > 0) {
        fplAvgPoints.push(`${cx.toFixed(1)},${yOf(num(g.gw_average)).toFixed(1)}`);
      }

      const chipBadge = g.chip_name ? `★` : "";
      return `
        <g class="gw-bar-group" data-gw-index="${i}">
          <rect
            x="${(cx - slotW / 2).toFixed(1)}"
            y="${padT}"
            width="${slotW.toFixed(1)}"
            height="${plotH}"
            fill="transparent"
            style="cursor:pointer;"
          />
          <rect
            x="${x.toFixed(1)}"
            y="${barTop.toFixed(1)}"
            width="${barW.toFixed(1)}"
            height="${barH.toFixed(1)}"
            rx="4"
            class="gw-bar ${tier}"
          />
          <text x="${cx.toFixed(1)}" y="${labelY.toFixed(1)}" class="gw-bar-value">${pts}${chipBadge}</text>
          <text x="${cx.toFixed(1)}" y="${svgH - 6}" text-anchor="middle" class="gw-axis-label">GW${g.gw}</text>
        </g>
      `;
    })
    .join("");

  const fplAvgLineSvg =
    fplAvgPoints.length > 1
      ? `<polyline points="${fplAvgPoints.join(" ")}" class="gw-fpl-avg-line" />`
      : "";

  return `
    <div class="gw-chart-section">
      <div class="gw-chart-header">
        <h4>Team Gameweek Points Chart (${count} GWs · Avg ${avgPts.toFixed(1)} pts/GW)</h4>
        <div class="gw-chart-legend">
          <span class="legend-item"><span class="legend-swatch" style="background:#00d492;"></span>70+ Haul</span>
          <span class="legend-item"><span class="legend-swatch" style="background:#22c55e;"></span>55–69</span>
          <span class="legend-item"><span class="legend-swatch" style="background:#38bdf8;"></span>40–54</span>
          <span class="legend-item"><span class="legend-swatch" style="background:#94a3b8;"></span>&lt;40</span>
          <span class="legend-item"><span class="legend-swatch" style="background:#f59e0b;"></span>FPL GW Avg</span>
        </div>
      </div>
      <div class="gw-chart-wrapper">
        <svg class="gw-chart-svg" viewBox="0 0 ${svgW} ${svgH}" role="img" aria-label="Team Gameweek points bar chart">
          ${gridLines.join("")}
          <line x1="${padL}" y1="${yZero.toFixed(1)}" x2="${svgW - padR}" y2="${yZero.toFixed(1)}"
            stroke="var(--border-strong)" stroke-width="1.4" />
          <polyline points="${trendPoints.join(" ")}" class="gw-trend-line" />
          ${fplAvgLineSvg}
          <line x1="${padL}" y1="${yAvg.toFixed(1)}" x2="${svgW - padR}" y2="${yAvg.toFixed(1)}" class="gw-avg-line" />
          <text x="${svgW - padR}" y="${(yAvg - 5).toFixed(1)}" text-anchor="end" class="gw-avg-label">My Avg ${avgPts.toFixed(1)}</text>
          ${barsSvg}
        </svg>
      </div>
    </div>
  `;
}

function renderSquadOverview() {
  const container = document.getElementById("squad-content");
  if (state.squadLoading) {
    container.innerHTML = renderLoadingHtml(
      "Đang tải đội hình FPL...",
      "Vui lòng chờ trong giây lát"
    );
    return;
  }

  if (!state.squadData || !state.squadData.found) {
    container.innerHTML =
      '<p class="empty-state-text">No squad loaded yet. Enter your FPL Manager ID or URL in the bottom-left sidebar and click <strong>Sync</strong>.</p>';
    return;
  }

  const sq = state.squadData;
  const gwHistory = sq.gw_history || [];
  const latestHist = gwHistory.length ? gwHistory[gwHistory.length - 1] : null;
  const overallPts = sq.overall_points ?? sq.manager?.overall_points ?? latestHist?.total_points ?? 0;
  const overallRank = sq.overall_rank ?? sq.manager?.overall_rank ?? latestHist?.overall_rank ?? null;
  const formattedRank = overallRank ? Number(overallRank).toLocaleString() : "—";
  const latestGwLabel = latestHist
    ? `GW ${sq.squad_gw ?? latestHist.gw} (${latestHist.points} pts)`
    : `GW ${sq.squad_gw ?? "—"}`;

  const startingXi = sq.starting_xi || [];
  const bench = sq.bench || [];

  const renderSquadRow = (p, roleLabel) => {
    const capTag = p.is_captain
      ? '<span class="captain-tag">C</span>'
      : p.is_vice_captain
        ? '<span class="captain-tag">VC</span>'
        : "";
    return renderRichPlayerRow(p, `<td><strong>${roleLabel}</strong></td>`, capTag);
  };

  const xiRowsHtml = startingXi.map((p) => renderSquadRow(p, "XI")).join("");
  const benchRowsHtml = bench.map((p) => renderSquadRow(p, "Bench")).join("");
  const benchDividerHtml = bench.length
    ? `<tr class="squad-bench-divider"><td colspan="${SHARED_PLAYER_STAT_HEADERS.length + 1}">Substitutes (Bench)</td></tr>`
    : "";

  const theadColsHtml = [
    "<th>Role</th>",
    ...SHARED_PLAYER_STAT_HEADERS.map((col) => `<th class="${col.cls}">${col.label}</th>`),
  ].join("");

  container.innerHTML = `
    <div class="squad-stats-bar">
      <div class="stat-pill">
        <span class="stat-pill-label">Team</span>
        <span class="stat-pill-value">${escapeHtml(sq.manager?.team_name || "—")}</span>
      </div>
      <div class="stat-pill stat-pill-highlight">
        <span class="stat-pill-label">Total Points</span>
        <span class="stat-pill-value">${Number(overallPts).toLocaleString()} pts</span>
      </div>
      <div class="stat-pill">
        <span class="stat-pill-label">Overall Rank</span>
        <span class="stat-pill-value">#${formattedRank}</span>
      </div>
      <div class="stat-pill">
        <span class="stat-pill-label">Latest GW</span>
        <span class="stat-pill-value">${latestGwLabel}</span>
      </div>
      <div class="stat-pill">
        <span class="stat-pill-label">Team Value</span>
        <span class="stat-pill-value">£${sq.team_value_m ?? 0}m</span>
      </div>
      <div class="stat-pill">
        <span class="stat-pill-label">Bank</span>
        <span class="stat-pill-value">£${sq.bank_balance_m ?? 0}m</span>
      </div>
      <div class="stat-pill">
        <span class="stat-pill-label">Free Transfers</span>
        <span class="stat-pill-value">${sq.free_transfers ?? 1}</span>
      </div>
    </div>

    ${renderTeamGwChart(gwHistory)}

    <div class="squad-table-wrap">
      <table class="data-table">
        <thead>
          <tr>${theadColsHtml}</tr>
        </thead>
        <tbody>
          ${xiRowsHtml}
          ${benchDividerHtml}
          ${benchRowsHtml}
        </tbody>
      </table>
    </div>
  `;

  if (gwHistory.length > 0) {
    attachGwChartTooltips(container, gwHistory, (g) => {
      const costStr = g.event_transfers_cost > 0 ? ` (-${g.event_transfers_cost} hit)` : "";
      const chipStr = g.chip_name ? ` · Chip: ${escapeHtml(g.chip_name)}` : "";
      const gwAvgStr =
        g.gw_average !== null && g.gw_average !== undefined ? `${g.gw_average} pts` : "—";
      const ovRankStr = g.overall_rank ? `#${Number(g.overall_rank).toLocaleString()}` : "—";
      const gwRankStr = g.gw_rank ? `#${Number(g.gw_rank).toLocaleString()}` : "—";
      return `
        <div><strong>GW${g.gw}${chipStr}</strong></div>
        <div>Team Points: <strong>${g.points}</strong>${costStr}</div>
        <div>Total Points: <strong>${Number(g.total_points || 0).toLocaleString()}</strong></div>
        <div>FPL GW Avg: ${gwAvgStr} · Bench: ${g.points_on_bench ?? 0}</div>
        <div>Transfers: ${g.event_transfers ?? 0} · GW Rank: ${gwRankStr}</div>
        <div>Overall Rank: ${ovRankStr}</div>
      `;
    });
  }
}

const squadContentEl = document.getElementById("squad-content");
if (squadContentEl) {
  squadContentEl.addEventListener("click", (e) => {
    const row = e.target.closest(".player-row-clickable");
    if (row) {
      const pid = parseInt(row.getAttribute("data-player-id"), 10);
      if (Number.isFinite(pid)) {
        openPlayerHistoryModal(pid);
      }
    }
  });
}

// =====================================================================
// PLAYERS EXPLORER (METHOD 1: ALL PLAYERS + INSTANT FILTER + RICH STATS)
// =====================================================================

function populateClubFilter(teams) {
  if (!filterTeamEl || !Array.isArray(teams)) return;
  const previousVal = filterTeamEl.value;
  const opts = ['<option value="">All Clubs</option>'];
  teams.forEach((t) => {
    const shortName = escapeHtml(t.short_name);
    const fullName = escapeHtml(t.name);
    opts.push(`<option value="${shortName}">${shortName} — ${fullName}</option>`);
  });
  filterTeamEl.innerHTML = opts.join("");
  if (previousVal) {
    filterTeamEl.value = previousVal;
  }
}

async function fetchAllPlayers(force = false) {
  if (state.playersLoaded && !force) {
    return state.allPlayers;
  }
  if (state.playersLoadPromise && !force) {
    return state.playersLoadPromise;
  }

  state.playersLoading = true;
  state.playersError = null;
  if (loadPlayersBtn) loadPlayersBtn.disabled = true;

  if (document.getElementById("players-view").classList.contains("active")) {
    if (playersCountBadgeEl) playersCountBadgeEl.textContent = "Đang tải...";
    playersTableContainerEl.innerHTML = renderLoadingHtml(
      "Đang tải danh sách toàn bộ cầu thủ...",
      "Đang đồng bộ chỉ số từ cơ sở dữ liệu Premier League"
    );
  }

  state.playersLoadPromise = (async () => {
    try {
      const resp = await fetch("/api/players/top?limit=1500&min_minutes=0&sort_by=total_points");
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status}`);
      }
      const data = await resp.json();
      state.allPlayers = data.players || [];
      state.allTeams = data.teams || [];
      state.playersLoaded = true;
      populateClubFilter(state.allTeams);
      return state.allPlayers;
    } catch (err) {
      state.playersError = err.message || "Failed to load players";
      return [];
    } finally {
      state.playersLoading = false;
      state.playersLoadPromise = null;
      if (loadPlayersBtn) loadPlayersBtn.disabled = false;
    }
  })();

  return state.playersLoadPromise;
}

async function loadPlayersExplorer(force = false) {
  if (!state.playersLoaded || force) {
    if (playersCountBadgeEl) playersCountBadgeEl.textContent = "Đang tải...";
    playersTableContainerEl.innerHTML = renderLoadingHtml(
      "Đang tải danh sách toàn bộ cầu thủ...",
      "Chỉ tải một lần — tìm kiếm & lọc sau đó sẽ tức thì"
    );
    await fetchAllPlayers(force);
  }
  renderPlayersExplorerTable();
}

const EXPLORER_COLUMNS = [
  { key: "rank", label: "#", sortable: false, cls: "num-col" },
  ...SHARED_PLAYER_STAT_HEADERS.map((col) => ({ ...col, sortable: true })),
];

function getSortedFilteredPlayers() {
  const search = (filterSearchEl?.value || "").trim().toLowerCase();
  const team = filterTeamEl?.value || "";
  const pos = filterPosEl?.value || "";
  const maxPriceStr = filterMaxPriceEl?.value || "";
  const maxPrice = maxPriceStr ? parseFloat(maxPriceStr) : null;

  const filtered = state.allPlayers.filter((p) => {
    if (pos && p.position !== pos) return false;
    if (team && p.team !== team) return false;
    if (maxPrice !== null && num(p.price) > maxPrice) return false;
    if (search) {
      const hay = `${p.web_name || ""} ${p.full_name || ""} ${p.team || ""} ${p.team_name || ""}`.toLowerCase();
      if (!hay.includes(search)) return false;
    }
    return true;
  });

  const field = state.sortField;
  const dir = state.sortDir === "asc" ? 1 : -1;
  const textFields = new Set(["web_name", "team", "position"]);

  filtered.sort((a, b) => {
    if (textFields.has(field)) {
      const va = String(a[field] || "").toLowerCase();
      const vb = String(b[field] || "").toLowerCase();
      if (va < vb) return -1 * dir;
      if (va > vb) return 1 * dir;
      return num(b.total_points) - num(a.total_points);
    }
    const diff = num(a[field]) - num(b[field]);
    if (diff !== 0) return diff * dir;
    return num(b.total_points) - num(a.total_points);
  });

  return filtered;
}

function renderPlayersExplorerTable() {
  if (state.playersLoading) {
    playersTableContainerEl.innerHTML = renderLoadingHtml(
      "Đang tải danh sách toàn bộ cầu thủ...",
      "Vui lòng chờ trong giây lát"
    );
    return;
  }

  if (state.playersError) {
    if (playersCountBadgeEl) playersCountBadgeEl.textContent = "Lỗi tải dữ liệu";
    playersTableContainerEl.innerHTML = `<p class="empty-state-text">Không thể tải danh sách cầu thủ: ${escapeHtml(state.playersError)}</p>`;
    return;
  }

  const filtered = getSortedFilteredPlayers();
  if (playersCountBadgeEl) {
    playersCountBadgeEl.textContent = `${filtered.length} / ${state.allPlayers.length} players`;
  }

  if (filtered.length === 0) {
    playersTableContainerEl.innerHTML =
      '<p class="empty-state-text">Không tìm thấy cầu thủ phù hợp với bộ lọc hiện tại.</p>';
    return;
  }

  const theadHtml = EXPLORER_COLUMNS.map((col) => {
    const isActive = col.sortable && state.sortField === col.key;
    const arrow = isActive ? (state.sortDir === "asc" ? " ▲" : " ▼") : "";
    const classes = [
      col.cls,
      col.sortable ? "sortable-th" : "",
      isActive ? "active-sort" : "",
    ]
      .filter(Boolean)
      .join(" ");
    const sortAttr = col.sortable ? `data-sort="${col.key}"` : "";
    return `<th class="${classes}" ${sortAttr}>${col.label}${arrow}</th>`;
  }).join("");

  const rowsHtml = filtered
    .map((p, idx) => renderRichPlayerRow(p, `<td class="num-col">${idx + 1}</td>`))
    .join("");

  playersTableContainerEl.innerHTML = `
    <table class="data-table">
      <thead>
        <tr>${theadHtml}</tr>
      </thead>
      <tbody>${rowsHtml}</tbody>
    </table>
  `;
}

// Instant filtering & sorting listeners
if (filterSearchEl) {
  filterSearchEl.addEventListener("input", renderPlayersExplorerTable);
}
if (filterTeamEl) {
  filterTeamEl.addEventListener("change", renderPlayersExplorerTable);
}
if (filterPosEl) {
  filterPosEl.addEventListener("change", renderPlayersExplorerTable);
}
if (filterMaxPriceEl) {
  filterMaxPriceEl.addEventListener("change", renderPlayersExplorerTable);
}
if (filterSortEl) {
  filterSortEl.addEventListener("change", () => {
    state.sortField = filterSortEl.value;
    state.sortDir = "desc";
    renderPlayersExplorerTable();
  });
}
if (loadPlayersBtn) {
  loadPlayersBtn.addEventListener("click", () => loadPlayersExplorer(true));
}

// Event delegation for sortable headers & clickable player rows
if (playersTableContainerEl) {
  playersTableContainerEl.addEventListener("click", (e) => {
    const th = e.target.closest(".sortable-th");
    if (th) {
      const sortKey = th.getAttribute("data-sort");
      if (sortKey) {
        if (state.sortField === sortKey) {
          state.sortDir = state.sortDir === "desc" ? "asc" : "desc";
        } else {
          state.sortField = sortKey;
          state.sortDir = sortKey === "web_name" || sortKey === "team" ? "asc" : "desc";
        }
        if (filterSortEl && [...filterSortEl.options].some((o) => o.value === sortKey)) {
          filterSortEl.value = sortKey;
        }
        renderPlayersExplorerTable();
      }
      return;
    }

    const row = e.target.closest(".player-row-clickable");
    if (row) {
      const pid = parseInt(row.getAttribute("data-player-id"), 10);
      if (Number.isFinite(pid)) {
        openPlayerHistoryModal(pid);
      }
    }
  });
}

// =====================================================================
// PLAYER DETAIL & GAMEWEEK POINTS CHART MODAL
// =====================================================================

function ensureChartTooltip() {
  if (!chartTooltipEl) {
    chartTooltipEl = document.createElement("div");
    chartTooltipEl.id = "gw-chart-tooltip";
    chartTooltipEl.className = "gw-chart-tooltip hidden";
    document.body.appendChild(chartTooltipEl);
  }
  return chartTooltipEl;
}

function hideChartTooltip() {
  if (chartTooltipEl) {
    chartTooltipEl.classList.add("hidden");
  }
}

function closePlayerModal() {
  state.activeModalPlayerId = null;
  playerModalEl.classList.add("hidden");
  hideChartTooltip();
}

if (closePlayerModalBtn) {
  closePlayerModalBtn.addEventListener("click", closePlayerModal);
}
if (playerModalBackdropEl) {
  playerModalBackdropEl.addEventListener("click", closePlayerModal);
}
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !playerModalEl.classList.contains("hidden")) {
    closePlayerModal();
  }
});

function renderModalHeader(p) {
  const st = getStatusLabel(p.status, p.chance_of_playing_next_round);
  playerModalHeaderEl.innerHTML = `
    <div class="player-modal-title-row">
      <h3 id="player-modal-title">${escapeHtml(p.full_name || p.web_name)}</h3>
      <span class="pos-badge pos-${escapeHtml(p.position)}">${escapeHtml(p.position)}</span>
      <span class="players-count-badge">${escapeHtml(p.team_name || p.team)}</span>
    </div>
    <div class="player-modal-meta">
      <span><strong>Price:</strong> £${fmt(p.price, 1)}m</span>
      <span><strong>Selected:</strong> ${fmt(p.selected_by_percent, 1)}%</span>
      <span><strong>Total Pts:</strong> ${num(p.total_points)}</span>
      <span><strong>Form:</strong> ${fmt(p.form, 1)}</span>
      <span><strong>PPG:</strong> ${fmt(p.points_per_game, 1)}</span>
      <span><strong>ep_next:</strong> ${fmt(p.ep_next, 1)}</span>
      <span><strong>Min:</strong> ${num(p.minutes)}</span>
      <span><strong>G/A:</strong> ${num(p.goals_scored)}/${num(p.assists)}</span>
      <span><strong>xGI:</strong> ${fmt(p.xGI, 2)}</span>
      <span><span class="status-dot ${st.cls}"></span> ${escapeHtml(st.text)}</span>
    </div>
  `;
}

function getPointTierClass(pts) {
  if (pts >= 10) return "tier-haul";
  if (pts >= 6) return "tier-good";
  if (pts >= 3) return "tier-normal";
  if (pts >= 1) return "tier-low";
  return "tier-zero";
}

function renderGwPointsChart(byGameweek) {
  if (!byGameweek || byGameweek.length === 0) {
    return `
      <div class="gw-chart-section">
        <div class="gw-chart-header">
          <h4>Gameweek Points Chart</h4>
        </div>
        <p class="empty-state-text">Chưa có dữ liệu điểm theo từng vòng đấu cho cầu thủ này.</p>
      </div>
    `;
  }

  const count = byGameweek.length;
  const ptsValues = byGameweek.map((g) => num(g.total_points));
  const minVal = Math.min(0, ...ptsValues);
  const maxVal = Math.max(8, ...ptsValues) + 1;
  const span = Math.max(1, maxVal - minVal);
  const avgPts = ptsValues.reduce((s, v) => s + v, 0) / count;

  const padL = 38;
  const padR = 24;
  const padT = 18;
  const padB = 22;
  const slotW = count > 24 ? 30 : count > 14 ? 42 : 58;
  const svgW = Math.max(880, padL + padR + count * slotW);
  const svgH = 128;
  const plotH = svgH - padT - padB;
  const plotW = svgW - padL - padR;

  const yOf = (val) => padT + ((maxVal - val) / span) * plotH;
  const yZero = yOf(0);
  const yAvg = yOf(avgPts);

  // Build ~4 clean Y gridlines
  const step = Math.max(1, Math.ceil(span / 4));
  const gridLines = [];
  const startTick = Math.floor(minVal / step) * step;
  for (let tick = startTick; tick <= maxVal; tick += step) {
    const y = yOf(tick);
    if (y >= padT - 4 && y <= svgH - padB + 4) {
      gridLines.push(`
        <line x1="${padL}" y1="${y.toFixed(1)}" x2="${svgW - padR}" y2="${y.toFixed(1)}" class="gw-grid-line" />
        <text x="${padL - 8}" y="${(y + 4).toFixed(1)}" text-anchor="end" class="gw-axis-label">${tick}</text>
      `);
    }
  }

  const barW = Math.max(14, Math.min(28, slotW * 0.48));
  const trendPoints = [];
  const barsSvg = byGameweek
    .map((g, i) => {
      const pts = num(g.total_points);
      const cx = padL + (i + 0.5) * (plotW / count);
      const x = cx - barW / 2;
      const yVal = yOf(pts);
      const barTop = Math.min(yVal, yZero);
      const barH = Math.max(2, Math.abs(yZero - yVal));
      const labelY = pts >= 0 ? barTop - 5 : barTop + barH + 11;
      const tier = getPointTierClass(pts);
      trendPoints.push(`${cx.toFixed(1)},${yVal.toFixed(1)}`);

      return `
        <g class="gw-bar-group" data-gw-index="${i}">
          <rect
            x="${(cx - slotW / 2).toFixed(1)}"
            y="${padT}"
            width="${slotW.toFixed(1)}"
            height="${plotH}"
            fill="transparent"
            style="cursor:pointer;"
          />
          <rect
            x="${x.toFixed(1)}"
            y="${barTop.toFixed(1)}"
            width="${barW.toFixed(1)}"
            height="${barH.toFixed(1)}"
            rx="4"
            class="gw-bar ${tier}"
          />
          <text x="${cx.toFixed(1)}" y="${labelY.toFixed(1)}" class="gw-bar-value">${pts}</text>
          <text x="${cx.toFixed(1)}" y="${svgH - 6}" text-anchor="middle" class="gw-axis-label">GW${g.gw}</text>
        </g>
      `;
    })
    .join("");

  return `
    <div class="gw-chart-section">
      <div class="gw-chart-header">
        <h4>Gameweek Points Chart (${count} GWs · Avg ${avgPts.toFixed(1)} pts/GW)</h4>
        <div class="gw-chart-legend">
          <span class="legend-item"><span class="legend-swatch" style="background:#00d492;"></span>10+ Haul</span>
          <span class="legend-item"><span class="legend-swatch" style="background:#22c55e;"></span>6–9 pts</span>
          <span class="legend-item"><span class="legend-swatch" style="background:#38bdf8;"></span>3–5 pts</span>
          <span class="legend-item"><span class="legend-swatch" style="background:#94a3b8;"></span>1–2 pts</span>
          <span class="legend-item"><span class="legend-swatch" style="background:#ef4444;"></span>≤0 pts</span>
        </div>
      </div>
      <div class="gw-chart-wrapper">
        <svg class="gw-chart-svg" viewBox="0 0 ${svgW} ${svgH}" role="img" aria-label="Gameweek points bar chart">
          ${gridLines.join("")}
          <line x1="${padL}" y1="${yZero.toFixed(1)}" x2="${svgW - padR}" y2="${yZero.toFixed(1)}"
            stroke="var(--border-strong)" stroke-width="1.4" />
          <polyline points="${trendPoints.join(" ")}" class="gw-trend-line" />
          <line x1="${padL}" y1="${yAvg.toFixed(1)}" x2="${svgW - padR}" y2="${yAvg.toFixed(1)}" class="gw-avg-line" />
          <text x="${svgW - padR}" y="${(yAvg - 5).toFixed(1)}" text-anchor="end" class="gw-avg-label">Avg ${avgPts.toFixed(1)}</text>
          ${barsSvg}
        </svg>
      </div>
    </div>
  `;
}

function attachGwChartTooltips(containerEl, items, formatTooltipFn) {
  const tooltip = ensureChartTooltip();
  const groups = containerEl.querySelectorAll(".gw-bar-group");

  groups.forEach((grp) => {
    const idx = parseInt(grp.getAttribute("data-gw-index"), 10);
    const item = items[idx];
    if (!item) return;

    const showTip = (clientX, clientY) => {
      tooltip.innerHTML = formatTooltipFn(item);
      tooltip.classList.remove("hidden");

      const rect = tooltip.getBoundingClientRect();
      const left = Math.min(window.innerWidth - rect.width - 12, clientX + 14);
      const top = Math.max(12, clientY - rect.height - 12);
      tooltip.style.left = `${left}px`;
      tooltip.style.top = `${top}px`;
    };

    grp.addEventListener("mouseenter", (e) => showTip(e.clientX, e.clientY));
    grp.addEventListener("mousemove", (e) => showTip(e.clientX, e.clientY));
    grp.addEventListener("click", (e) => showTip(e.clientX, e.clientY));
    grp.addEventListener("mouseleave", hideChartTooltip);
  });
}

function attachChartTooltipEvents(byGameweek) {
  attachGwChartTooltips(playerModalBodyEl, byGameweek, (g) => {
    const dgwBadge = g.matches_count > 1 ? " · DGW" : "";
    return `
      <div><strong>GW${g.gw} vs ${escapeHtml(g.opponent)}${dgwBadge}</strong></div>
      <div>Points: <strong>${g.total_points}</strong> | Min: ${g.minutes}'</div>
      <div>G: ${g.goals_scored} · A: ${g.assists} · CS: ${g.clean_sheets}</div>
      <div>Bonus: ${g.bonus} (BPS ${g.bps}) · xGI: ${fmt(g.xGI, 2)}</div>
    `;
  });
}

async function openPlayerHistoryModal(playerId) {
  state.activeModalPlayerId = playerId;
  playerModalEl.classList.remove("hidden");
  hideChartTooltip();

  const cached = state.allPlayers.find((p) => Number(p.id) === Number(playerId));
  if (cached) {
    renderModalHeader(cached);
  } else {
    playerModalHeaderEl.innerHTML = '<h3 id="player-modal-title">Player Details</h3>';
  }

  playerModalBodyEl.innerHTML = renderLoadingHtml(
    "Đang tải lịch sử điểm theo các vòng đấu...",
    "Đang truy vấn dữ liệu từng Gameweek"
  );

  try {
    const resp = await fetch(`/api/players/${playerId}/history`);
    if (!resp.ok) {
      throw new Error(`HTTP ${resp.status}`);
    }
    const data = await resp.json();
    if (state.activeModalPlayerId !== playerId) return;

    const p = data.player || cached;
    const byGw = data.by_gameweek || [];
    if (p) {
      renderModalHeader(p);
    }

    const newsHtml = p?.news
      ? `<div class="player-news-banner"><strong>News:</strong> ${escapeHtml(p.news)}</div>`
      : "";

    const chartHtml = renderGwPointsChart(byGw);

    const historyTableRows = [...byGw]
      .reverse()
      .map(
        (g) => `
        <tr>
          <td><strong>GW${g.gw}</strong></td>
          <td>${escapeHtml(g.opponent)}</td>
          <td class="num-col"><strong>${g.total_points}</strong></td>
          <td class="num-col">${g.minutes}</td>
          <td class="num-col">${g.goals_scored}</td>
          <td class="num-col">${g.assists}</td>
          <td class="num-col">${g.clean_sheets}</td>
          <td class="num-col">${g.goals_conceded}</td>
          <td class="num-col">${g.saves}</td>
          <td class="num-col">${g.yellow_cards}</td>
          <td class="num-col">${g.red_cards}</td>
          <td class="num-col">${g.bonus}</td>
          <td class="num-col">${g.bps}</td>
          <td class="num-col">${fmt(g.xG, 2)}</td>
          <td class="num-col">${fmt(g.xA, 2)}</td>
          <td class="num-col">${fmt(g.xGI, 2)}</td>
          <td class="num-col">${fmt(g.ict_index, 1)}</td>
        </tr>`
      )
      .join("");

    const historyTableHtml = byGw.length
      ? `
      <div class="modal-history-table-wrap">
        <table class="data-table">
          <thead>
            <tr>
              <th>GW</th>
              <th>Opponent</th>
              <th class="num-col">Pts</th>
              <th class="num-col">Min</th>
              <th class="num-col">G</th>
              <th class="num-col">A</th>
              <th class="num-col">CS</th>
              <th class="num-col">GC</th>
              <th class="num-col">Sv</th>
              <th class="num-col">YC</th>
              <th class="num-col">RC</th>
              <th class="num-col">Bon</th>
              <th class="num-col">BPS</th>
              <th class="num-col">xG</th>
              <th class="num-col">xA</th>
              <th class="num-col">xGI</th>
              <th class="num-col">ICT</th>
            </tr>
          </thead>
          <tbody>${historyTableRows}</tbody>
        </table>
      </div>
    `
      : "";

    playerModalBodyEl.innerHTML = `${newsHtml}${chartHtml}${historyTableHtml}`;
    attachChartTooltipEvents(byGw);
  } catch (err) {
    if (state.activeModalPlayerId !== playerId) return;
    playerModalBodyEl.innerHTML = `<p class="empty-state-text">Không thể tải lịch sử vòng đấu: ${escapeHtml(err.message)}</p>`;
  }
}
