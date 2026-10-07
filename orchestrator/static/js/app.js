// --- Autenticacion por sesion de usuario (ver ROADMAP.md, punto 0) ---
// El token vive en una cookie HttpOnly que pone el servidor: esta pagina no
// lo ve (un script colado no puede robarlo, auditoria 2026-09-29). Aqui solo
// se recuerda que hay sesion y con que rol, para pintar la interfaz.
try { localStorage.removeItem("ia_session_token"); } catch (e) {}  // el de antes
function isLoggedIn() {
  try { return localStorage.getItem("ia_logged_in") === "1"; } catch (e) { return false; }
}
function setLoggedIn(role) {
  try {
    localStorage.setItem("ia_logged_in", "1");
    localStorage.setItem("ia_session_role", role || "user");
  } catch (e) {}
}
function clearLoggedIn() {
  try {
    localStorage.removeItem("ia_logged_in");
    localStorage.removeItem("ia_session_role");
    // lo del usuario anterior no se queda en el navegador para el siguiente
    localStorage.removeItem("ia_session_id");
    localStorage.removeItem("ia_code_project");
  } catch (e) {}
}
function getSessionRole() {
  try { return localStorage.getItem("ia_session_role") || "user"; } catch (e) { return "user"; }
}

// Para meter texto de fuera (nombres de usuario, archivos, modelos) dentro de
// un innerHTML: un nombre como <img onerror=...> escrito en el login se
// ejecutaba en el panel del administrador (auditoria 2026-09-29).
function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Enlaces e imagenes que vienen de webs ajenas (apps): solo http(s). Un
// "javascript:..." en un resultado seria codigo al pulsarlo (auditoria 2026-09-30).
function webUrl(value) {
  const url = String(value || "").trim();
  return /^https?:\/\//i.test(url) ? url : "#";
}

const _originalFetch = window.fetch;
window.fetch = function (input, init) {
  return _originalFetch(input, init).then((resp) => {
    if (resp.status === 401) {
      clearLoggedIn();
      showLoginScreen("Tu sesion ha caducado. Inicia sesion de nuevo.");
    }
    return resp;
  });
};

// --- Pantalla de login/registro/invitado/recuperacion ---
const loginScreen = document.getElementById("loginScreen");
const loginSubtitle = document.getElementById("loginSubtitle");
const loginFormBox = document.getElementById("loginFormBox");
const registerFormBox = document.getElementById("registerFormBox");
const forgotFormBox = document.getElementById("forgotFormBox");

function showLoginScreen(message) {
  loginScreen.style.display = "flex";
  loginFormBox.classList.remove("hidden");
  registerFormBox.classList.add("hidden");
  forgotFormBox.classList.add("hidden");
  if (message) loginSubtitle.textContent = message;
}
function hideLoginScreen() {
  loginScreen.style.display = "none";
}

// Intro para enviar en los formularios de login/registro/recuperacion, no
// solo con el raton - pedido explicito por Sergio.
function enterSubmits(inputIds, buttonId) {
  inputIds.forEach((inputId) => {
    document.getElementById(inputId).addEventListener("keydown", (e) => {
      if (e.key !== "Enter") return;
      e.preventDefault();
      document.getElementById(buttonId).click();
    });
  });
}
enterSubmits(["loginUsername", "loginPassword"], "loginSubmitBtn");
enterSubmits(["regUsername", "regPassword", "regKey", "regQuestion", "regAnswer"], "registerSubmitBtn");
enterSubmits(["forgotUsername"], "forgotLoadQuestionBtn");
enterSubmits(["forgotAnswer", "forgotNewPassword"], "forgotSubmitBtn");

document.getElementById("loginSubmitBtn").addEventListener("click", async () => {
  const username = document.getElementById("loginUsername").value.trim();
  const password = document.getElementById("loginPassword").value;
  const errEl = document.getElementById("loginError");
  errEl.textContent = "";
  try {
    const resp = await _originalFetch("/auth/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    const data = await resp.json();
    if (!resp.ok) { errEl.textContent = data.detail || "No se pudo iniciar sesion."; return; }
    setLoggedIn(data.role);
    hideLoginScreen();
    location.reload();
  } catch (e) { errEl.textContent = "No se pudo contactar con el servidor."; }
});

document.getElementById("guestBtn").addEventListener("click", async () => {
  const resp = await _originalFetch("/auth/guest", { method: "POST" });
  const data = await resp.json();
  setLoggedIn(data.role);
  hideLoginScreen();
  location.reload();
});

document.getElementById("showRegisterBtn").addEventListener("click", () => {
  loginFormBox.classList.add("hidden");
  registerFormBox.classList.remove("hidden");
});
document.getElementById("backToLoginFromRegisterBtn").addEventListener("click", () => {
  registerFormBox.classList.add("hidden");
  loginFormBox.classList.remove("hidden");
});

document.getElementById("registerSubmitBtn").addEventListener("click", async () => {
  const body = {
    username: document.getElementById("regUsername").value.trim(),
    password: document.getElementById("regPassword").value,
    registration_key: document.getElementById("regKey").value.trim(),
    security_question: document.getElementById("regQuestion").value.trim() || null,
    security_answer: document.getElementById("regAnswer").value.trim() || null,
  };
  const errEl = document.getElementById("registerError");
  errEl.textContent = "";
  const resp = await _originalFetch("/auth/register", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await resp.json();
  if (!resp.ok) { errEl.textContent = data.detail || "No se pudo crear la cuenta."; return; }
  setLoggedIn(data.role);
  hideLoginScreen();
  location.reload();
});

document.getElementById("showForgotBtn").addEventListener("click", () => {
  loginFormBox.classList.add("hidden");
  forgotFormBox.classList.remove("hidden");
});
document.getElementById("backToLoginFromForgotBtn").addEventListener("click", () => {
  forgotFormBox.classList.add("hidden");
  loginFormBox.classList.remove("hidden");
});

document.getElementById("forgotLoadQuestionBtn").addEventListener("click", async () => {
  const username = document.getElementById("forgotUsername").value.trim();
  const errEl = document.getElementById("forgotError");
  errEl.textContent = "";
  const resp = await _originalFetch(`/auth/security-question/${encodeURIComponent(username)}`);
  const data = await resp.json();
  if (!resp.ok) { errEl.textContent = data.detail || "No se encontro la pregunta."; return; }
  document.getElementById("forgotQuestionText").textContent = data.security_question;
  document.getElementById("forgotAnswer").classList.remove("hidden");
  document.getElementById("forgotNewPassword").classList.remove("hidden");
  document.getElementById("forgotSubmitBtn").classList.remove("hidden");
  document.getElementById("forgotWarning").textContent =
    "Aviso: si respondes bien, entraras con una cuenta limpia. Tus conversaciones e imagenes cifradas con la contraseña anterior quedaran inaccesibles para siempre (no hay forma de recuperarlas).";
});

document.getElementById("forgotSubmitBtn").addEventListener("click", async () => {
  const body = {
    username: document.getElementById("forgotUsername").value.trim(),
    security_answer: document.getElementById("forgotAnswer").value,
    new_password: document.getElementById("forgotNewPassword").value,
  };
  const errEl = document.getElementById("forgotError");
  errEl.textContent = "";
  const resp = await _originalFetch("/auth/reset-password", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await resp.json();
  if (!resp.ok) { errEl.textContent = data.detail || "No se pudo restablecer."; return; }
  alert("Contraseña restablecida. Inicia sesion con la nueva contraseña.");
  forgotFormBox.classList.add("hidden");
  loginFormBox.classList.remove("hidden");
});

if (!isLoggedIn()) {
  showLoginScreen();
}

// --- Estado de sesion en el sidebar, panel de admin, cambio de contraseña ---
// Ojo para mostrar/ocultar lo escrito en TODOS los campos de contraseña
function addPasswordToggles() {
  document.querySelectorAll('input[type="password"]:not([data-pw-eye])').forEach((input) => {
    input.dataset.pwEye = "1";
    const wrap = document.createElement("span");
    wrap.className = "pw-wrap";
    // el margen de abajo pasa al envoltorio, para que el ojo quede centrado en el campo
    const margin = getComputedStyle(input).marginBottom;
    wrap.style.marginBottom = margin;
    input.style.marginBottom = "0";
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);
    const eye = document.createElement("button");
    eye.type = "button";
    eye.className = "pw-eye";
    eye.tabIndex = -1;  // el tabulador sigue saltando de campo en campo
    const sync = () => {
      const shown = input.type === "text";
      eye.textContent = shown ? "🙈" : "👁";
      eye.title = shown ? "Ocultar contraseña" : "Mostrar contraseña";
    };
    eye.addEventListener("click", () => {
      input.type = input.type === "password" ? "text" : "password";
      sync();
      input.focus();
    });
    sync();
    wrap.appendChild(eye);
  });
}
addPasswordToggles();

// Solo dentro de la app de escritorio (desktop_app.py expone la api): abre
// Chati en el navegador normal, para usar los dos modos a la vez
function setupOpenInBrowser() {
  const btn = document.getElementById("openInBrowserBtn");
  const api = window.pywebview && window.pywebview.api;
  if (!api || !api.open_in_browser) return;
  btn.style.display = "block";
  btn.onclick = () => api.open_in_browser();
}
window.addEventListener("pywebviewready", setupOpenInBrowser);
setupOpenInBrowser();

async function initSessionUI() {
  if (!isLoggedIn()) return;
  const resp = await fetch("/auth/me");
  if (!resp.ok) { clearLoggedIn(); showLoginScreen(); return; }
  const me = await resp.json();
  setLoggedIn(me.role); // refresca el rol guardado por si cambio

  currentMe = me;
  loadAvatar();
  if (me.role === "admin") document.getElementById("adminBtn").style.display = "block";
  // las metricas son de todo el sistema: solo el administrador
  if (me.role !== "admin") document.getElementById("optMetricsBtn").style.display = "none";
  if (me.role === "admin") {
    // OpenCode tiene contraseña (auditoria 2026-09-29): se copia al pulsar el enlace
    const link = document.getElementById("optCodeAgentLink");
    link.style.display = "";
    link.addEventListener("click", async () => {
      try {
        const r = await fetch("/agent/web-login");
        if (!r.ok) return;
        const cred = await r.json();
        await navigator.clipboard.writeText(cred.password);
        alert(`OpenCode pide usuario y contraseña.
Usuario: ${cred.user}
La contraseña ya está copiada: pégala con Ctrl+V.`);
      } catch (e) {}
    });
  }
  if (!me.pc_access) {
    // sin permiso para usar el ordenador: fuera los modos que lo tocan
    for (const value of ["agente", "codigo"]) {
      const opt = document.querySelector(`#modeSelect option[value="${value}"]`);
      if (opt) opt.remove();
    }
    if (currentMode === "agente" || currentMode === "codigo") {
      document.getElementById("modeSelect").value = "chat";
      document.getElementById("modeSelect").dispatchEvent(new Event("change"));
    }
  }
  if (me.role === "guest") {
    document.getElementById("optMemoryBtn").style.display = "none";
    document.getElementById("optPersonasBtn").style.display = "none";
    document.getElementById("knowledgeBtn").style.display = "none";
    document.getElementById("profileBtn").style.display = "none";
    document.getElementById("newChatBtn").style.display = "none";
    document.getElementById("convSectionLabel").style.display = "none";
    document.getElementById("convList").innerHTML = '<div id="convEmpty">Modo invitado: sin historial guardado.</div>';
  } else {
    await loadConversations();
  }
}

document.getElementById("logoutBtn").addEventListener("click", async () => {
  await fetch("/auth/logout", { method: "POST" });
  clearLoggedIn();
  location.reload();
});

// --- Mi perfil: nombre, contraseña y pregunta de seguridad (el CV esta en Mis apps → Buscar empleo) ---
let currentMe = null;

let avatarUrl = null;  // object URL de la foto (se pide con fetch: <img src> no manda el token)

function paintAvatar(el, name) {
  el.innerHTML = "";
  if (avatarUrl) {
    const img = document.createElement("img");
    img.src = avatarUrl;
    img.alt = "";
    el.appendChild(img);
  } else {
    el.textContent = (name[0] || "?").toUpperCase();
  }
}

async function loadAvatar() {
  if (avatarUrl) { URL.revokeObjectURL(avatarUrl); avatarUrl = null; }
  if (currentMe && currentMe.role !== "guest") {
    try {
      const resp = await fetch("/profile/avatar");
      if (resp.ok) avatarUrl = URL.createObjectURL(await resp.blob());
    } catch (e) { /* sin foto: se queda la inicial */ }
  }
  renderWhoami();
  if (profileModal.style.display === "block") fillProfile();
}

function renderWhoami() {
  const me = currentMe || {};
  const shown = me.display_name || me.username || "Invitado";
  document.getElementById("whoamiLabel").textContent = shown;
  paintAvatar(document.getElementById("whoamiAvatar"), shown);
  document.getElementById("whoamiRole").textContent =
    me.role === "admin" ? "Administrador" : me.role === "guest" ? "Modo invitado" : "Usuario";
}

const profileModal = document.getElementById("profileModal");

function showProfileTab(tab) {
  document.querySelectorAll(".profile-tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  document.querySelectorAll(".profile-pane").forEach((p) => { p.style.display = p.dataset.pane === tab ? "block" : "none"; });
}
document.querySelectorAll(".profile-tab").forEach((b) => b.addEventListener("click", () => showProfileTab(b.dataset.tab)));

function setProfileMsg(id, text, kind) {
  const el = document.getElementById(id);
  el.textContent = text;
  el.className = "profile-msg" + (kind ? " " + kind : "");
}

function fillProfile() {
  const me = currentMe || {};
  const shown = me.display_name || me.username || "";
  paintAvatar(document.getElementById("profileAvatar"), shown);
  document.getElementById("avatarRemoveBtn").style.display = avatarUrl ? "inline" : "none";
  document.getElementById("avatarChangeBtn").textContent = avatarUrl ? "Cambiar foto" : "Subir foto";
  document.getElementById("profileNameLabel").textContent = shown;
  document.getElementById("profileUsernameLabel").textContent = me.username || "";
  document.getElementById("profileDisplayName").value = me.display_name || "";
  document.getElementById("profileQuestionInfo").textContent = me.security_question
    ? `Pregunta actual: "${me.security_question}". Sirve para recuperar el acceso si olvidas la contraseña.`
    : "No tienes pregunta de seguridad. Sin ella, si olvidas la contraseña no hay forma de recuperar la cuenta.";
  ["profileDatosMsg", "profilePasswordMsg", "profileQuestionMsg", "profileAvatarMsg"].forEach((id) => setProfileMsg(id, ""));
}

document.getElementById("profileBtn").addEventListener("click", () => {
  optionsModal.style.display = "none";
  fillProfile();
  showProfileTab("datos");
  profileModal.style.display = "block";
  loadCvStatus();
});
document.getElementById("closeProfileBtn").addEventListener("click", () => { profileModal.style.display = "none"; });
profileModal.addEventListener("click", (e) => { if (e.target === profileModal) profileModal.style.display = "none"; });

async function profilePost(url, body) {
  const resp = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || "No se pudo guardar.");
  return data;
}

document.getElementById("saveDisplayNameBtn").addEventListener("click", async () => {
  const display_name = document.getElementById("profileDisplayName").value.trim();
  try {
    await profilePost("/auth/profile", { display_name });
  } catch (err) { setProfileMsg("profileDatosMsg", err.message, "error"); return; }
  currentMe.display_name = display_name || null;
  renderWhoami();
  fillProfile();
  setProfileMsg("profileDatosMsg", "Guardado ✓", "ok");
});

document.getElementById("submitChangePasswordBtn").addEventListener("click", async () => {
  const old_password = document.getElementById("oldPasswordInput").value;
  const new_password = document.getElementById("newPasswordInput").value;
  if (new_password !== document.getElementById("newPassword2Input").value) {
    setProfileMsg("profilePasswordMsg", "Las contraseñas nuevas no coinciden.", "error"); return;
  }
  setProfileMsg("profilePasswordMsg", "Cambiando...");
  let data;
  try {
    data = await profilePost("/auth/change-password", { old_password, new_password });
  } catch (err) { setProfileMsg("profilePasswordMsg", err.message, "error"); return; }
  setLoggedIn(getSessionRole());
  ["oldPasswordInput", "newPasswordInput", "newPassword2Input"].forEach((id) => { document.getElementById(id).value = ""; });
  setProfileMsg("profilePasswordMsg", "Contraseña cambiada ✓", "ok");
});

document.getElementById("saveSecurityQuestionBtn").addEventListener("click", async () => {
  const question = document.getElementById("securityQuestionInput").value.trim();
  const answer = document.getElementById("securityAnswerInput").value.trim();
  const password = document.getElementById("securityPasswordInput").value;
  setProfileMsg("profileQuestionMsg", "Guardando...");
  try {
    await profilePost("/auth/security-question", { question, answer, password });
  } catch (err) { setProfileMsg("profileQuestionMsg", err.message, "error"); return; }
  currentMe.security_question = question;
  ["securityQuestionInput", "securityAnswerInput", "securityPasswordInput"].forEach((id) => { document.getElementById(id).value = ""; });
  fillProfile();
  setProfileMsg("profileQuestionMsg", "Pregunta guardada ✓", "ok");
});

const avatarInput = document.getElementById("avatarInput");
document.getElementById("avatarChangeBtn").addEventListener("click", () => avatarInput.click());
avatarInput.addEventListener("change", async () => {
  const file = avatarInput.files[0];
  avatarInput.value = "";
  if (!file) return;
  setProfileMsg("profileAvatarMsg", "Subiendo...");
  const form = new FormData();
  form.append("file", file);
  const resp = await fetch("/profile/avatar", { method: "POST", body: form });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    setProfileMsg("profileAvatarMsg", data.detail || "No se pudo subir la foto.", "error");
    return;
  }
  await loadAvatar();
  setProfileMsg("profileAvatarMsg", "Foto guardada ✓", "ok");
});
document.getElementById("avatarRemoveBtn").addEventListener("click", async () => {
  await fetch("/profile/avatar", { method: "DELETE" });
  await loadAvatar();
});

const cvInput = document.getElementById("cvInput");
const cvStatus = document.getElementById("cvStatus");
const cvUploadBtn = document.getElementById("cvUploadBtn");
cvUploadBtn.addEventListener("click", () => cvInput.click());

async function loadCvStatus() {
  if (getSessionRole() === "guest") return;  // invitado: sin CV (la ruta le da 403)
  try {
    const resp = await fetch("/cv");
    const data = await resp.json();
    cvStatus.textContent = data.has_cv ? data.filename : "Todavia no has subido tu CV";
    cvUploadBtn.textContent = data.has_cv ? "Sustituir" : "Subir CV";
  } catch (err) { /* silencioso */ }
}

cvInput.addEventListener("change", async () => {
  if (!cvInput.files[0]) return;
  const form = new FormData();
  form.append("file", cvInput.files[0]);
  cvStatus.textContent = "Subiendo...";
  try {
    await fetch("/cv", { method: "POST", body: form });
    await loadCvStatus();
    if (jobsModal.style.display === "block") loadJobs().catch(() => {});
  } catch (err) {
    cvStatus.textContent = "Error subiendo el CV";
  }
  cvInput.value = "";
});

const adminModal = document.getElementById("adminModal");
document.getElementById("adminBtn").addEventListener("click", async () => {
  optionsModal.style.display = "none";
  adminModal.style.display = "block";
  await loadAdminPanel();
});
document.getElementById("closeAdminBtn").addEventListener("click", () => { adminModal.style.display = "none"; });

async function loadAdminPanel() {
  const [usersResp, eventsResp, regKeyResp] = await Promise.all([
    fetch("/auth/users"), fetch("/auth/security-events"), fetch("/auth/registration-key"),
  ]);
  const usersData = await usersResp.json();
  const eventsData = await eventsResp.json();
  const regKeyData = await regKeyResp.json();
  document.getElementById("regKeyDisplay").textContent = regKeyData.registration_key;

  const meResp = await fetch("/auth/me");
  const me = await meResp.json();

  const usersBox = document.getElementById("adminUsersList");
  usersBox.innerHTML = usersData.users.map((u) => `
    <div style="display:flex; justify-content:space-between; align-items:center; padding:6px 0; border-top:1px solid var(--border); font-size:13px;">
      <span>${esc(u.username)} <span style="color:var(--muted);">(${esc(u.role)})</span></span>
      ${u.role === "admin" ? "" : `<label title="Agente, leer archivos del ordenador y ejecutar código, con tus permisos de Windows" style="font-size:11.5px; color:var(--muted); display:flex; align-items:center; gap:4px; margin-left:auto; margin-right:8px;">
        <input type="checkbox" class="pcAccessChk" data-username="${esc(u.username)}" ${u.pc_access ? "checked" : ""}> Puede usar el ordenador</label>`}
      ${u.username === me.username ? "" : `<button data-username="${esc(u.username)}" class="deleteUserBtn" style="font-size:11px; padding:3px 8px; background:var(--error); color:white; border:none; border-radius:6px; cursor:pointer;">Eliminar</button>`}
    </div>
  `).join("") || '<div style="color:var(--muted); font-size:12px;">Sin usuarios.</div>';

  usersBox.querySelectorAll(".pcAccessChk").forEach((chk) => {
    chk.addEventListener("change", async () => {
      if (chk.checked && !confirm(`"${chk.dataset.username}" podrá usar el agente, leer tus archivos del Escritorio y Documentos y ejecutar código en este ordenador. ¿Seguro?`)) {
        chk.checked = false;
        return;
      }
      const resp = await fetch(`/auth/users/${encodeURIComponent(chk.dataset.username)}/pc_access`, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ allowed: chk.checked }),
      });
      if (!resp.ok) { chk.checked = !chk.checked; alert("No se pudo cambiar el permiso."); }
    });
  });

  usersBox.querySelectorAll(".deleteUserBtn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!confirm(`¿Eliminar la cuenta de "${btn.dataset.username}"? Se borraran tambien todos sus datos (conversaciones, caras, documentos). No se puede deshacer.`)) return;
      await fetch(`/auth/users/${encodeURIComponent(btn.dataset.username)}`, { method: "DELETE" });
      await loadAdminPanel();
    });
  });

  const eventsBox = document.getElementById("adminSecurityEvents");
  eventsBox.innerHTML = eventsData.events.slice().reverse().map((e) => `
    <div style="padding:4px 0; color:var(--muted);">${esc(e.at)} - ${esc(e.event)} (${esc(e.username)})</div>
  `).join("") || '<div style="color:var(--muted);">Sin eventos.</div>';
}

const MODES = {
  chat: { desc: "Automático: Chati elige cómo responder", endpoint: "/chat/stream", showTopbar: true, allowAttach: true, isText: true, credits: ["ollama", "comfyui"] },
  texto: { desc: "Solo conversación", endpoint: "/chat/stream", agent: "text", showTopbar: true, allowAttach: true, isText: true, credits: ["ollama"] },
  image: { desc: "Crear imágenes", endpoint: "/chat/stream", agent: "image", showTopbar: true, allowAttach: true, faceEndpoint: "/image_with_face", modelsEndpoint: "/models/image", credits: ["comfyui"] },
  video: { desc: "Crear vídeos", endpoint: "/chat/stream", agent: "video", showTopbar: true, allowAttach: true, faceEndpoint: "/video_with_face", modelsEndpoint: "/models/video", credits: ["comfyui"] },
  agente: { desc: "Tareas con tus archivos", isAgent: true, allowAttach: true, credits: ["opencode", "ollama"] },
  codigo: { desc: "Programar en tus proyectos", isAgent: true, isCode: true, credits: ["opencode", "ollama"] },
  voice: { desc: "Habla y te responde con voz", isVoice: true, credits: ["whisper", "ollama", "piper"] },
};

// Creditos: Chati se apoya en programas de otros y lo dice (mejoras.md: "no
// quiero llevar merito de otros programas").
const CREDIT_LINKS = {
  ollama: ["Ollama", "https://ollama.com"],
  comfyui: ["ComfyUI", "https://www.comfy.org"],
  opencode: ["OpenCode", "https://opencode.ai"],
  whisper: ["faster-whisper", "https://github.com/SYSTRAN/faster-whisper"],
  piper: ["Piper", "https://github.com/rhasspy/piper"],
};

function creditsHtml(keys) {
  const links = keys.map((k) => `<a href="${CREDIT_LINKS[k][1]}" target="_blank" rel="noopener">${CREDIT_LINKS[k][0]}</a>`);
  const list = links.length > 1 ? links.slice(0, -1).join(", ") + " y " + links[links.length - 1] : links[0];
  return `Funciona con ${list}`;
}

let currentMode = "chat";

function getSessionId() {
  return localStorage.getItem("ia_session_id") || null;
}
function setSessionId(sid) {
  if (sid) {
    const isNew = sid !== localStorage.getItem("ia_session_id");
    localStorage.setItem("ia_session_id", sid);
    if (isNew) loadConversations();
  }
}

const planPanel = document.getElementById("planPanel");
const ESTADO_ICONO = { pendiente: "", en_progreso: "●", hecho: "✓" };

async function refreshPlan() {
  const sid = getSessionId();
  if (!sid) return;
  let data;
  try {
    const resp = await fetch(`/plan/${sid}`);
    data = await resp.json();
  } catch (e) { return; }

  const pasos = data.pasos || [];
  if (!pasos.length) { planPanel.style.display = "none"; return; }

  planPanel.innerHTML = '<div class="plan-title">Plan de la tarea</div>' + pasos.map((p) => `
    <div class="plan-step ${esc(p.estado)}">
      <span class="dot">${ESTADO_ICONO[p.estado] || ""}</span>
      <span class="texto">${esc(p.texto)}</span>
    </div>
  `).join("");
  planPanel.style.display = "block";
}

// --- Markdown + resaltado de sintaxis + boton de copiar ---
marked.setOptions({ breaks: true, gfm: true });

// Lo que escribe el modelo puede traer HTML (p.ej. copiado de una web con
// instrucciones ocultas): sin limpiar, un <img onerror=...> se ejecutaba y
// podia robar la sesion (auditoria 2026-09-29).
function safeHtml(html) {
  return DOMPurify.sanitize(html, { FORBID_TAGS: ["style", "form", "input", "button", "textarea", "select"],
                                    FORBID_ATTR: ["style"] });
}
// los enlaces que escribe el modelo se abren fuera, sin darle acceso a esta pagina
DOMPurify.addHook("afterSanitizeAttributes", (node) => {
  if (node.tagName === "A" && node.getAttribute("href")) {
    node.setAttribute("target", "_blank");
    node.setAttribute("rel", "noopener noreferrer");
  }
});

function renderMarkdownInto(container, text, { highlight } = { highlight: true }) {
  container.innerHTML = safeHtml(marked.parse(text || ""));
  container.classList.add("markdown");
  if (!highlight) return;
  container.querySelectorAll("pre code").forEach((block) => {
    hljs.highlightElement(block);
    const pre = block.parentElement;
    const btn = document.createElement("button");
    btn.className = "copy-code-btn";
    btn.textContent = "Copiar";
    btn.addEventListener("click", () => {
      navigator.clipboard.writeText(block.textContent).then(() => {
        btn.textContent = "Copiado";
        setTimeout(() => { btn.textContent = "Copiar"; }, 1200);
      });
    });
    pre.appendChild(btn);
  });
}

const log = document.getElementById("log");
const emptyState = document.getElementById("emptyState");
const promptEl = document.getElementById("prompt");
const sendBtn = document.getElementById("send");
const cancelBtn = document.getElementById("cancelBtn");
const modeDesc = document.getElementById("modeDesc");
const micBtn = document.getElementById("micBtn");
const topbar = document.getElementById("topbar");
const profileSelectLabel = document.getElementById("profileSelectLabel");
const profileSelect = document.getElementById("profileSelect");
const verifyToggleLabel = document.getElementById("verifyToggleLabel");
const modelStatusText = document.getElementById("modelStatusText");
const attachBtn = document.getElementById("attachBtn");
const visionImageInput = document.getElementById("visionImageInput");
const attachPreviewRow = document.getElementById("attachPreviewRow");
const attachPreviewImg = document.getElementById("attachPreviewImg");
const removeAttachBtn = document.getElementById("removeAttachBtn");
const verifyToggle = document.getElementById("verifyToggle");

function hideEmptyState() { if (emptyState) emptyState.style.display = "none"; }

// --- Verificar respuestas (anti-alucinacion) por mensaje, elegible por el
// usuario - ver ROADMAP.md: consultas puntuales pueden ir sin verificar
// (mas rapido), conversaciones donde importe la fiabilidad lo mantienen activo.
// desactivado por defecto (cada respuesta verificada es otra llamada al modelo);
// si el usuario lo activa, se recuerda
verifyToggle.checked = localStorage.getItem("ia_verify") === "on";
verifyToggle.addEventListener("change", () => {
  localStorage.setItem("ia_verify", verifyToggle.checked ? "on" : "off");
});

// --- Perfil de modelo (rapido/bueno/seguridad) ---
async function loadProfiles() {
  try {
    const resp = await fetch("/models/profiles");
    const data = await resp.json();
    profileSelect.innerHTML = "";
    (data.profiles || []).forEach((p) => {
      const opt = document.createElement("option");
      opt.value = p.id;
      opt.textContent = p.label;
      profileSelect.appendChild(opt);
    });
    const saved = localStorage.getItem("ia_model_profile");
    profileSelect.value = (saved && data.profiles.some(p => p.id === saved)) ? saved : (data.default || "");
  } catch (err) { /* la interfaz sigue usable con el perfil por defecto del servidor */ }
}
profileSelect.addEventListener("change", () => {
  localStorage.setItem("ia_model_profile", profileSelect.value);
});

async function refreshModelStatus() {
  try {
    const resp = await fetch("/models/status");
    const data = await resp.json();
    const names = (data.models || []).map(m => m.name);
    modelStatusText.textContent = names.length ? names.join(", ") : "sin modelos cargados";
  } catch (err) {
    modelStatusText.textContent = "sin datos";
  }
}

// --- Imagen adjunta para vision (modo chat) ---
let visionImageBase64 = null;
// --- Clip: menu Imagen / Documento (tipo ChatGPT) ---
const attachMenu = document.getElementById("attachMenu");
const docInput = document.getElementById("docInput");

attachBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  if (MODES[currentMode].isAgent) { document.getElementById("agentFilesInput").click(); return; }
  // los documentos se leen en el chat; en imagen/video la foto es para conservar una cara
  const inChat = !!MODES[currentMode].isText;
  document.getElementById("attachDocItem").style.display = inChat ? "flex" : "none";
  document.getElementById("attachImageHint").textContent = inChat ? "Comentarla o describirla" : "Para conservar esa cara";
  attachMenu.classList.toggle("show");
});
document.addEventListener("click", (e) => { if (!attachMenu.contains(e.target)) attachMenu.classList.remove("show"); });
document.getElementById("attachImageItem").addEventListener("click", () => {
  attachMenu.classList.remove("show");
  visionImageInput.click();
});
document.getElementById("attachDocItem").addEventListener("click", () => {
  attachMenu.classList.remove("show");
  docInput.click();
});

const convDocsWrap = document.getElementById("convDocsWrap");
const convDocsPanel = document.getElementById("convDocsPanel");
const convDocsList = document.getElementById("convDocsList");
document.getElementById("convDocsBtn").addEventListener("click", (e) => {
  e.stopPropagation();
  convDocsPanel.classList.toggle("show");
});
document.addEventListener("click", (e) => { if (!convDocsWrap.contains(e.target)) convDocsPanel.classList.remove("show"); });

// Tarjeta del documento dentro del hilo, en el momento en que se adjunto
function addDocCard(name, { uploading = false, error = null } = {}) {
  hideEmptyState();
  const wrap = document.createElement("div");
  wrap.className = "msg user doc";
  const card = document.createElement("div");
  card.className = "doc-card" + (error ? " error" : "");
  const ic = document.createElement("span");
  ic.className = "ic";
  ic.textContent = "📄";
  const text = document.createElement("div");
  const nameEl = document.createElement("div");
  nameEl.className = "name";
  nameEl.textContent = name;
  const sub = document.createElement("div");
  sub.className = "sub";
  sub.textContent = uploading ? "Subiendo..." : error ? error : "Documento adjuntado";
  text.append(nameEl, sub);
  card.append(ic, text);
  wrap.appendChild(card);
  log.appendChild(wrap);
  log.scrollTop = log.scrollHeight;
  return { wrap, sub, card };
}

async function refreshDocChips() {
  convDocsList.innerHTML = "";
  convDocsWrap.classList.remove("show");
  const sid = getSessionId();
  if (!sid) return;
  let docs = [];
  try {
    const resp = await fetch(`/sessions/${sid}/docs`);
    if (resp.ok) docs = await resp.json();
  } catch (e) { return; }
  document.getElementById("convDocsCount").textContent = `${docs.length}`;
  convDocsWrap.classList.toggle("show", docs.length > 0);
  if (!docs.length) convDocsPanel.classList.remove("show");
  for (const d of docs) {
    const row = document.createElement("div");
    row.className = "cdp-row";
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = "📄 " + d.name;
    name.title = d.name;
    const keep = document.createElement("button");
    keep.textContent = "Guardar";
    keep.title = "Guardar tambien en Mis documentos, para usarlo en cualquier conversacion";
    keep.addEventListener("click", async (e) => {
      e.stopPropagation();
      keep.disabled = true;
      const resp = await fetch(`/sessions/${sid}/docs/${encodeURIComponent(d.name)}/keep`, { method: "POST" });
      if (!resp.ok) { keep.disabled = false; alert("No se pudo guardar en Mis documentos."); return; }
      const ok = document.createElement("span");
      ok.className = "kept";
      ok.textContent = "en Mis documentos ✓";
      keep.replaceWith(ok);
    });
    const rm = document.createElement("button");
    rm.className = "rm";
    rm.textContent = "Quitar";
    rm.title = "Chati deja de tenerlo en cuenta en esta conversacion";
    rm.addEventListener("click", async (e) => {
      e.stopPropagation();
      await fetch(`/sessions/${sid}/docs/${encodeURIComponent(d.name)}`, { method: "DELETE" });
      refreshDocChips();
    });
    row.append(name, keep, rm);
    convDocsList.appendChild(row);
  }
}

docInput.addEventListener("change", async () => {
  const files = [...docInput.files];
  docInput.value = "";
  for (const file of files) {
    const card = addDocCard(file.name, { uploading: true });
    const form = new FormData();
    form.append("file", file);
    if (getSessionId()) form.append("session_id", getSessionId());
    let data = {};
    let ok = false;
    try {
      const resp = await fetch("/sessions/docs", { method: "POST", body: form });
      data = await resp.json();
      ok = resp.ok;
    } catch (e) { data = { detail: "error de conexion" }; }
    if (ok) {
      setSessionId(data.session_id);
      localStorage.setItem("ia_session_id", data.session_id);
      card.sub.textContent = "Documento adjuntado";
    } else {
      card.card.classList.add("error");
      card.sub.textContent = data.detail || "No se pudo adjuntar";
    }
  }
  await refreshDocChips();
  promptEl.focus();
});
visionImageInput.addEventListener("change", () => {
  const file = visionImageInput.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    visionImageBase64 = reader.result.split(",")[1];
    attachPreviewImg.src = reader.result;
    attachPreviewRow.classList.add("show");
  };
  reader.readAsDataURL(file);
});
removeAttachBtn.addEventListener("click", () => {
  visionImageBase64 = null;
  visionImageInput.value = "";
  attachPreviewRow.classList.remove("show");
});


if (isLoggedIn()) {
  refreshDocChips();
  loadCvStatus();
  loadProfiles();
  refreshModelStatus();
}
setInterval(() => { if (isLoggedIn()) refreshModelStatus(); }, 20000);

const modeSelect = document.getElementById("modeSelect");
const modelSelectLabel = document.getElementById("modelSelectLabel");
const modelSelect = document.getElementById("modelSelect");
const modelsCache = {};

async function loadModelsFor(mode) {
  const m = MODES[mode];
  if (!m || !m.modelsEndpoint) {
    return;
  }
  if (!modelsCache[mode]) {
    try {
      const resp = await fetch(m.modelsEndpoint);
      const data = await resp.json();
      modelsCache[mode] = data.models || [];
    } catch (err) {
      modelsCache[mode] = [];
    }
  }
  modelSelect.innerHTML = "";
  const autoOpt = document.createElement("option");
  autoOpt.value = "";
  autoOpt.textContent = "Automatico";
  modelSelect.appendChild(autoOpt);
  for (const model of modelsCache[mode]) {
    const opt = document.createElement("option");
    opt.value = model.id;
    opt.textContent = `${model.architecture_label} - ${model.label}`;
    modelSelect.appendChild(opt);
  }
  if (modelsCache[mode].length === 0) {
    const noneOpt = document.createElement("option");
    noneOpt.value = "";
    noneOpt.disabled = true;
    noneOpt.textContent = "No hay modelos instalados";
    modelSelect.appendChild(noneOpt);
  }
}

const personaSelect = document.getElementById("personaSelect");
const personaSelectLabel = document.getElementById("personaSelectLabel");

async function loadPersonaChoices() {
  let ready = [];
  try {
    const data = await (await fetch("/personas")).json();
    ready = (data.personas || []).filter((p) => p.sdxl && p.sdxl.status === "listo").map((p) => p.name);
  } catch (e) { /* sin personas */ }
  const keep = personaSelect.value;
  personaSelect.innerHTML = '<option value="">Ninguna</option>';
  for (const name of ready) {
    const opt = document.createElement("option");
    opt.value = opt.textContent = name;
    personaSelect.appendChild(opt);
  }
  if (ready.includes(keep)) personaSelect.value = keep;
  const show = currentMode === "image" && ready.length > 0;
  personaSelect.style.display = show ? "inline-block" : "none";
  personaSelectLabel.style.display = show ? "inline" : "none";
}

function updateTopbarForMode(m) {
  if (m.agent === "image") loadPersonaChoices();
  else { personaSelect.style.display = "none"; personaSelectLabel.style.display = "none"; }
  const showModelPicker = !!m.modelsEndpoint;
  profileSelectLabel.style.display = showModelPicker ? "none" : "inline";
  profileSelect.style.display = showModelPicker ? "none" : "inline-block";
  verifyToggleLabel.style.display = showModelPicker ? "none" : "inline-flex";
  modelSelectLabel.style.display = showModelPicker ? "inline" : "none";
  modelSelect.style.display = showModelPicker ? "inline-block" : "none";
}

modeSelect.addEventListener("change", () => requestModeChange(modeSelect.value));

const agentPowerToggle = document.getElementById("agentPowerToggle");
try { agentPowerToggle.checked = localStorage.getItem("ia_agent_potente") === "on"; } catch (e) { /* sin almacenamiento */ }
agentPowerToggle.addEventListener("change", () => {
  try { localStorage.setItem("ia_agent_potente", agentPowerToggle.checked ? "on" : "off"); } catch (e) { /* no se recuerda */ }
  if (currentMode === "agente") prepareModelsForMode();
});

function applyMode(mode) {
  modeSelect.value = mode;
  currentMode = mode;
  const m = MODES[currentMode];
  const plainAgent = !!m.isAgent && !m.isCode;
  document.getElementById("agentPowerLabel").classList.toggle("show", plainAgent);
  agentShortcutsRow.classList.toggle("show", plainAgent);
  document.getElementById("agentAttachRow").style.display = plainAgent ? "" : "none";
  document.getElementById("codeBar").classList.toggle("show", !!m.isCode);
  if (plainAgent) loadAgentShortcuts();
  setModeDesc(m);
  promptEl.style.display = m.isVoice ? "none" : "block";
  sendBtn.style.display = m.isVoice ? "none" : "block";
  micBtn.style.display = m.isVoice ? "block" : "none";
  attachBtn.style.display = m.allowAttach ? "flex" : "none";
  topbar.classList.toggle("show", !!m.showTopbar);
  updateTopbarForMode(m);
  if (!m.allowAttach) {
    visionImageBase64 = null;
    visionImageInput.value = "";
    attachPreviewRow.classList.remove("show");
  }
  loadModelsFor(currentMode);
  if (plainAgent) showAgentTaskList();
  // en diferido: al cargar la pagina en modo Codigo, codeState (mas abajo) aun no existe
  if (m.isCode) setTimeout(initCodeMode, 0);
  try { localStorage.setItem("ia_mode", currentMode); } catch (e) { /* sin almacenamiento: no se recuerda */ }
  prepareModelsForMode();
}

// --- Antes de cambiar de modo: ¿hay algo trabajando que se veria afectado? ---
function modeLabel(mode) {
  const opt = modeSelect.querySelector(`option[value="${mode}"]`);
  return opt ? opt.textContent : mode;
}

function choiceModal(title, lines, buttons) {
  const modal = document.getElementById("pendingWorkModal");
  document.getElementById("pendingWorkTitle").textContent = title;
  const list = document.getElementById("pendingWorkList");
  list.innerHTML = "";
  for (const line of lines) {
    const li = document.createElement("div");
    li.className = line.warn ? "pw-line warn" : "pw-line";
    li.textContent = line.text;
    list.appendChild(li);
  }
  const box = document.getElementById("pendingWorkButtons");
  box.innerHTML = "";
  modal.style.display = "block";
  return new Promise((resolve) => {
    const done = (value) => { modal.style.display = "none"; modal.onclick = null; resolve(value); };
    for (const [value, label, cls] of buttons) {
      const btn = document.createElement("button");
      btn.className = "dc-btn " + (cls || "");
      btn.textContent = label;
      btn.onclick = () => done(value);
      box.appendChild(btn);
    }
    modal.onclick = (e) => { if (e.target === modal) done("cancel"); };
  });
}

async function fetchPendingWork() {
  try {
    const resp = await fetch("/work/pending");
    return resp.ok ? await resp.json() : null;
  } catch (e) { return null; }
}

function describePending(p) {
  const lines = p.agent_tasks.map((t) => ({ text: "🤖 Agente trabajando: " + (t.title || "tarea sin titulo") }));
  if (p.generations_running) lines.push({ text: "🖼️ Generando una imagen o video ahora mismo" });
  if (p.generations_queued) lines.push({ text: `⏳ ${p.generations_queued} generacion(es) en cola` });
  return lines;
}

let modeWaitTicket = 0;

// --- Panel "tareas en segundo plano" (barra lateral): todo lo que esta en
// marcha o esperando, con opcion de cerrarlo - asi nada puede quedarse
// atascado para siempre (paso: una tarea del agente llevaba dias esperando
// una respuesta que nadie veia).
const bgTasksWrap = document.getElementById("bgTasksWrap");
const bgTasksPanel = document.getElementById("bgTasksPanel");
document.getElementById("bgTasksBtn").addEventListener("click", (e) => {
  e.stopPropagation();
  bgTasksPanel.classList.toggle("show");
});
document.addEventListener("click", (e) => { if (!bgTasksWrap.contains(e.target)) bgTasksPanel.classList.remove("show"); });

function bgRow(what, state, stateClass, actions) {
  const row = document.createElement("div");
  row.className = "bgp-row";
  const w = document.createElement("div");
  w.className = "what";
  w.textContent = what;
  const st = document.createElement("div");
  st.className = "state " + (stateClass || "");
  st.textContent = state;
  const acts = document.createElement("div");
  acts.className = "acts";
  for (const [label, cls, fn] of actions) {
    const b = document.createElement("button");
    b.textContent = label;
    if (cls) b.className = cls;
    b.addEventListener("click", async (e) => {
      e.stopPropagation();
      b.disabled = true;
      await fn();
      refreshBackgroundTasks();
    });
    acts.appendChild(b);
  }
  row.append(w, st, acts);
  return row;
}

async function refreshBackgroundTasks() {
  const p = await fetchPendingWork();
  if (!p) return;
  const waiting = p.waiting_tasks || [];
  const working = p.agent_tasks.length + p.generations_running + p.generations_queued;
  const total = working + waiting.length;
  bgTasksWrap.classList.toggle("show", total > 0);
  if (!total) { bgTasksPanel.classList.remove("show"); return; }

  const btn = document.getElementById("bgTasksBtn");
  btn.classList.toggle("only-waiting", working === 0);
  const parts = [];
  if (working) parts.push(`${working} en marcha`);
  if (waiting.length) parts.push(`${waiting.length} esperando tu respuesta`);
  document.getElementById("bgTasksLabel").textContent = "Tareas: " + parts.join(" · ");

  const list = document.getElementById("bgTasksList");
  list.innerHTML = "";
  const stopAgent = (sid) => () => fetch(`/work/agent/${sid}/stop`, { method: "POST" });
  const viewAgent = (sid) => () => { bgTasksPanel.classList.remove("show"); openAgentView(sid); };
  for (const t of p.agent_tasks) {
    list.appendChild(bgRow("🤖 " + (t.title || "Tarea del agente"), "Trabajando", "",
      [["Ver", "", viewAgent(t.session_id)], ["Detener", "stop", stopAgent(t.session_id)]]));
  }
  for (const t of waiting) {
    list.appendChild(bgRow("🤖 " + (t.title || "Tarea del agente"),
      t.reason === "permission" ? "Esperando tu permiso" : "Esperando tu respuesta", "waiting",
      [["Ver", "", viewAgent(t.session_id)], ["Cerrar tarea", "stop", stopAgent(t.session_id)]]));
  }
  for (const g of p.generations || []) {
    list.appendChild(bgRow("🖼️ Imagen o video", g.state === "running" ? "Generandose ahora" : "En cola", "",
      [["Detener", "stop", () => fetch(`/work/generation/${encodeURIComponent(g.id)}/stop`, { method: "POST" })]]));
  }
}

// Solo lo que choca con el modo nuevo: una tarea del agente usa el mismo
// modelo que el modo Agente, y una imagen en marcha usa ComfyUI igual que
// Imagen/Video - en esos casos no hay nada que decidir. null si no hay nada.
function conflictingWork(p, target) {
  const agentTasks = target === "agente" ? [] : p.agent_tasks;
  const sameComfy = target === "image" || target === "video";
  const running = sameComfy ? 0 : p.generations_running;
  const queued = sameComfy ? 0 : p.generations_queued;
  if (!agentTasks.length && !running && !queued) return null;
  return { ...p, agent_tasks: agentTasks, generations_running: running, generations_queued: queued };
}

async function requestModeChange(target) {
  const previous = currentMode;
  if (target === previous) return;
  const raw = await fetchPendingWork();
  const pending = raw && conflictingWork(raw, target);
  if (!pending) { applyMode(target); return; }
  modeSelect.value = previous;  // no se cambia hasta que se decida

  const choice = await choiceModal("Hay tareas en marcha", describePending(pending).concat([
    { text: `Cambiar a "${modeLabel(target)}" carga otros modelos. ¿Que hacemos con ellas?` },
  ]), [
    ["cancel", "Cancelar"],
    ["background", "Dejar en segundo plano"],
    ["wait", "Esperar a que terminen"],
    ["stop", "Finalizarlas y cambiar", "danger-solid"],
  ]);

  if (choice === "stop") {
    await fetch("/work/stop", { method: "POST" });
    applyMode(target);
  } else if (choice === "wait") {
    waitThenApply(target);
  } else if (choice === "background") {
    const sure = await choiceModal("⚠️ El ordenador va a ir muy saturado", [
      { text: "Se ejecutaran dos modelos a la vez: lo que esta en marcha y los del modo nuevo.", warn: true },
      { text: "Todo ira bastante mas lento (y puede que tambien el resto del ordenador) hasta que terminen." },
    ], [["cancel", "Volver"], ["yes", "Continuar igualmente", "primary"]]);
    if (sure === "yes") applyMode(target);
  }
}

async function waitThenApply(target) {
  const ticket = ++modeWaitTicket;
  prepareSkipped = false;
  showPrepare("loading", "Esperando a que terminen las tareas en marcha…",
    `En cuanto acaben se cargara el modo "${modeLabel(target)}". Puedes seguir mirando mientras tanto.`,
    "Cancelar el cambio");
  while (ticket === modeWaitTicket) {
    await new Promise((r) => setTimeout(r, 3000));
    if (ticket !== modeWaitTicket || prepareSkipped) return;
    const pending = await fetchPendingWork();
    if (pending && !conflictingWork(pending, target)) {
      prepareOverlay.classList.remove("show");
      applyMode(target);
      return;
    }
  }
}

// --- Cada modo deja cargados sus modelos (y libera los de los demas) ---
const prepareOverlay = document.getElementById("prepareOverlay");
let prepareTicket = 0;
let prepareSkipped = false;

function showPrepare(kind, title, sub, skipLabel) {
  document.getElementById("prepSpin").style.display = kind === "loading" ? "block" : "none";
  const icon = document.getElementById("prepIcon");
  icon.style.display = kind === "loading" ? "none" : "block";
  icon.textContent = kind === "ready" ? "✓" : "⚠";
  document.getElementById("prepTitle").textContent = title;
  document.getElementById("prepSub").textContent = sub;
  document.getElementById("prepSkip").textContent = skipLabel || (kind === "loading" ? "Seguir sin esperar" : "Cerrar");
  prepareOverlay.classList.add("show");
}
document.getElementById("prepSkip").addEventListener("click", () => {
  prepareSkipped = true;
  modeWaitTicket++;  // si estaba esperando a que terminaran tareas, se cancela el cambio
  prepareOverlay.classList.remove("show");
});

async function prepareModelsForMode() {
  const ticket = ++prepareTicket;
  prepareSkipped = false;
  prepareOverlay.classList.remove("show");
  const body = { mode: currentMode };
  if (profileSelect.value) body.model_profile = profileSelect.value;
  if (currentMode === "agente") body.potente = agentPowerToggle.checked;
  if (currentMode === "codigo") {  // localStorage: al cargar la pagina, codeState aun no existe
    let fast = false;
    try { fast = localStorage.getItem("ia_code_fast") === "on"; } catch (e) { /* nada */ }
    body.potente = !fast;
  }
  if (currentMode === "image" && modelSelect.value) body.image_model = modelSelect.value;
  if (currentMode === "video" && modelSelect.value) body.video_model = modelSelect.value;
  try {
    const resp = await fetch("/models/prepare", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (!resp.ok) return;
  } catch (e) { return; }

  const startedAt = Date.now();
  while (ticket === prepareTicket) {
    await new Promise((r) => setTimeout(r, 700));
    if (ticket !== prepareTicket) return;  // se cambio de modo: manda el nuevo
    let st;
    try { st = await (await fetch("/models/prepare")).json(); } catch (e) { continue; }
    if (st.state === "preparing") {
      // si ya estaba cargado acaba enseguida: el aviso solo sale si de verdad tarda
      if (!prepareSkipped && Date.now() - startedAt > 900) {
        showPrepare("loading", "Cargando " + (st.label || "los modelos") + "…",
          "Por favor, espera. La primera vez puede tardar hasta un par de minutos; despues ya queda listo.");
      }
      continue;
    }
    if (st.state === "error") {
      if (!prepareSkipped) showPrepare("error", "No se pudo preparar " + (st.label || "el modo"), st.detail || "");
      return;
    }
    if (prepareOverlay.classList.contains("show")) {
      showPrepare("ready", "Listo", (st.label ? st.label.charAt(0).toUpperCase() + st.label.slice(1) : "El modelo") + " ya esta cargado.");
      setTimeout(() => { if (ticket === prepareTicket) prepareOverlay.classList.remove("show"); }, 1300);
    }
    return;
  }
}

profileSelect.addEventListener("change", () => { if (["chat", "texto", "voice"].includes(currentMode)) prepareModelsForMode(); });
modelSelect.addEventListener("change", () => { if (["image", "video"].includes(currentMode)) prepareModelsForMode(); });

// al abrir la app: el ultimo modo usado, ya preparado
function restoreModeAndPrepare() {
  let saved = null;
  try { saved = localStorage.getItem("ia_mode"); } catch (e) { /* sin almacenamiento */ }
  if (saved && MODES[saved] && saved !== currentMode) {
    applyMode(saved);  // ya llama a prepareModelsForMode
  } else {
    prepareModelsForMode();
  }
}
setModeDesc(MODES.chat);
topbar.classList.add("show");
updateTopbarForMode(MODES.chat);
attachBtn.style.display = "flex";

let mediaRecorder = null;
let audioChunks = [];
let isRecording = false;

async function toggleRecording() {
  if (!isRecording) {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      mediaRecorder = new MediaRecorder(stream);
      audioChunks = [];
      mediaRecorder.ondataavailable = (e) => audioChunks.push(e.data);
      mediaRecorder.onstop = () => {
        stream.getTracks().forEach(t => t.stop());
        sendVoice(new Blob(audioChunks, { type: "audio/webm" }));
      };
      mediaRecorder.start();
      isRecording = true;
      micBtn.textContent = "Detener y enviar";
      micBtn.classList.add("recording");
    } catch (err) {
      alert("No se pudo acceder al microfono: " + err);
    }
  } else {
    mediaRecorder.stop();
    isRecording = false;
    micBtn.textContent = "Grabar";
    micBtn.classList.remove("recording");
  }
}

async function sendVoice(blob) {
  hideEmptyState();
  addMessage("user").textContent = "(mensaje de voz)";
  const bubble = addMessage("assistant", "voz");
  const stopTyping = startTypingIndicator(bubble);

  const form = new FormData();
  form.append("audio", blob, "recording.webm");
  const sid = getSessionId();
  if (sid) form.append("session_id", sid);

  try {
    const resp = await fetch("/voice_chat", { method: "POST", body: form });
    const data = await resp.json();
    setSessionId(data.session_id);

    const userMsgs = document.querySelectorAll(".msg.user .bubble");
    if (userMsgs.length) userMsgs[userMsgs.length - 1].textContent = data.transcript || "(no se entendio)";

    bubble.innerHTML = "";
    const tagWrap = bubble.parentElement.querySelector(".tag");
    if (tagWrap && data.agent_used) tagWrap.textContent = data.agent_used;
    if (data.verifier_gated) bubble.classList.add("gated");

    const textNode = document.createElement("div");
    textNode.textContent = data.response;
    bubble.appendChild(textNode);

    if (data.file_url) {
      const audio = document.createElement("audio");
      audio.src = data.file_url;
      audio.controls = true;
      audio.autoplay = true;
      bubble.appendChild(audio);
    }
  } catch (err) {
    bubble.className = "bubble error";
    bubble.textContent = "Error de conexion: " + err;
  } finally {
    stopTyping();
  }
}

micBtn.addEventListener("click", toggleRecording);

function startTypingIndicator(bubble) {
  // el spinner solo no basta cuando un perfil tarda de verdad (p.ej.
  // "Uncensored" en CPU) - un contador que sube deja claro que sigue vivo,
  // no que se ha quedado colgado. A partir de 20s se añade una pista sobre
  // por que puede estar tardando.
  const startedAt = Date.now();
  bubble.innerHTML =
    '<div class="typing-wrap">' +
    '<span class="typing-dots"><span></span><span></span><span></span></span>' +
    '<span class="typing-elapsed"></span>' +
    '</div>';
  const tick = () => {
    const wrap = bubble.querySelector(".typing-wrap");
    if (!wrap) return false; // ya se reemplazo el contenido con la respuesta real
    const secs = Math.floor((Date.now() - startedAt) / 1000);
    const elapsedEl = wrap.querySelector(".typing-elapsed");
    if (elapsedEl) elapsedEl.textContent = secs >= 2 ? `${secs}s` : "";
    let hintEl = bubble.querySelector(".typing-hint");
    if (secs >= 20 && !hintEl) {
      hintEl = document.createElement("span");
      hintEl.className = "typing-hint";
      hintEl.textContent = "Algunos perfiles/modelos tardan bastante en CPU - sigue en marcha.";
      bubble.appendChild(hintEl);
    }
    return true;
  };
  tick();
  const interval = setInterval(() => { if (!tick()) clearInterval(interval); }, 1000);
  return () => clearInterval(interval);
}

function addMessage(role, tag) {
  hideEmptyState();
  const wrap = document.createElement("div");
  wrap.className = "msg " + role;
  const roleEl = document.createElement("div");
  roleEl.className = "role";
  roleEl.textContent = role === "user" ? "Tu" : "Chati";
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  if (tag) {
    const tagEl = document.createElement("span");
    tagEl.className = "tag";
    tagEl.textContent = tag;
    wrap.appendChild(roleEl);
    wrap.appendChild(tagEl);
  } else {
    wrap.appendChild(roleEl);
  }
  wrap.appendChild(bubble);
  log.appendChild(wrap);
  log.scrollTop = log.scrollHeight;
  return bubble;
}

// retry: {text, mode, b64, file, preview} de una peticion anterior que fallo
// (boton "Reintentar"): se vuelve a enviar igual, foto incluida
async function send(retry = null) {
  const text = retry ? retry.text : promptEl.value.trim();
  if (!text) return;
  const modeKey = retry ? retry.mode : currentMode;
  const mode = MODES[modeKey];
  if (mode.isAgent) {
    promptEl.value = "";
    promptEl.style.height = "auto";
    await startAgentTask(text);
    return;
  }

  addMessage("user").textContent = text;
  const attachedImageB64 = retry ? retry.b64 : (mode.allowAttach ? visionImageBase64 : null);
  const attachedFile = retry ? retry.file : (mode.allowAttach ? visionImageInput.files[0] : null);
  const preview = retry ? retry.preview : (attachedImageB64 || attachedFile ? attachPreviewImg.src : null);
  if (preview) {
    const img = document.createElement("img");
    img.className = "attach";
    img.src = preview;
    log.lastElementChild.querySelector(".bubble").appendChild(img);
  }
  if (!retry) {
    promptEl.value = "";
    promptEl.style.height = "auto";
    visionImageBase64 = null;
    visionImageInput.value = "";
    attachPreviewRow.classList.remove("show");
  }
  sendBtn.disabled = true;

  // en modo imagen/video, una foto adjunta preserva esa cara (IPAdapter
  // FaceID); sin foto, genera normal a partir solo del texto
  // modo Imagen con foto: por el chat en streaming, como el automatico (se
  // edita y se ve en que fase va); video sigue con su ruta propia
  const useFaceEndpoint = mode.faceEndpoint && attachedFile && modeKey !== "image";
  const usePersona = modeKey === "image" && !attachedFile && personaSelect.value;
  // "Detener" para todo lo que se genera (mejoras.md: tener que esperar a que
  // termine algo que ya se ve que esta mal era incomodo)
  currentAbort = new AbortController();
  currentAbortIsMedia = mode.agent === "image" || mode.agent === "video" || useFaceEndpoint || usePersona;
  const signal = currentAbort.signal;
  cancelBtn.style.display = "inline-block";

  const bubble = addMessage("assistant", "generando...");
  const stopTyping = startTypingIndicator(bubble);
  // para el boton "Reintentar" si no sale (appendRetry)
  bubble._retry = { text, mode: modeKey, b64: attachedImageB64, file: attachedFile, preview };

  try {
    if (usePersona) {
      const form = new FormData();
      form.append("prompt", text);
      form.append("persona", personaSelect.value);
      if (modelSelect.value) form.append("model_id", modelSelect.value);
      const resp = await fetch("/image_with_persona", { method: "POST", body: form, signal });
      renderResult(bubble, await resp.json());
    } else if (useFaceEndpoint) {
      const form = new FormData();
      form.append("prompt", text);
      form.append("image", attachedFile);
      if (modeKey === "image" && modelSelect.value) form.append("model_id", modelSelect.value);
      // la foto queda en ESTA conversacion: luego "ahora ponle..." o "deshaz eso" la siguen editando
      if (getSessionId()) form.append("session_id", getSessionId());
      const resp = await fetch(mode.faceEndpoint, { method: "POST", body: form, signal });
      const data = await resp.json();
      renderResult(bubble, data);
    } else {
      const imgForVision = (mode.isText || modeKey === "image") ? attachedImageB64 : null;
      await streamChatInto(bubble, mode.endpoint, text, mode.agent, imgForVision, signal);
    }
  } catch (err) {
    if (err.name === "AbortError") {
      // lo que ya se habia escrito se queda; se marca que se paro a medias
      if (bubble.querySelector(".typing-wrap") || !bubble.textContent.trim()) bubble.textContent = "";
      const note = document.createElement("div");
      note.className = "stopped-note";
      note.textContent = "(detenido)";
      bubble.appendChild(note);
      if (currentAbortIsMedia) appendRetry(bubble);
    } else {
      bubble.className = "bubble error";
      bubble.textContent = "Error de conexion: " + err;
      appendRetry(bubble);
    }
  } finally {
    stopTyping();
    currentAbort = null;
    sendBtn.disabled = false;
    cancelBtn.style.display = "none";
    refreshBackgroundTasks();  // el contador de tareas, sin esperar a los 8s
  }
}

let currentAbort = null;
let currentAbortIsMedia = false;
cancelBtn.addEventListener("click", async () => {
  cancelBtn.disabled = true;
  cancelBtn.textContent = "Deteniendo…";
  // imagen/video: ademas hay que parar ComfyUI, que sigue aunque se corte la espera
  if (currentAbortIsMedia) {
    try { await fetch("/cancel", { method: "POST" }); } catch (err) { /* se corta igualmente abajo */ }
  }
  if (currentAbort) currentAbort.abort();
  cancelBtn.disabled = false;
  cancelBtn.textContent = "■ Detener";
});

// --- Modo Agente: interfaz propia encima de OpenCode (ver opencode_client.py).
// El agente es de terceros (OpenCode, licencia MIT) - Chati solo le pone esta
// interfaz, por eso el credito "powered by OpenCode" siempre visible.

const OPENCODE_CREDIT_HTML = 'Agente: <a href="https://opencode.ai" target="_blank" rel="noopener">powered by OpenCode</a>' +
  ' · modelos con <a href="https://ollama.com" target="_blank" rel="noopener">Ollama</a>';
const AGENT_POLL_MS = 2500;
const AGENT_TOOL_LABELS = {
  read: "Leyendo", write: "Creando archivo", edit: "Editando", patch: "Editando",
  bash: "Ejecutando", glob: "Buscando archivos", grep: "Buscando texto", list: "Mirando carpeta",
  webfetch: "Consultando web", websearch: "Buscando en internet", task: "Subtarea",
};
const AGENT_PERMISSION_LABELS = {
  edit: "modificar archivos", write: "crear archivos", bash: "ejecutar un comando",
  external_directory: "acceder a una carpeta fuera de su carpeta de trabajo",
  webfetch: "acceder a internet", websearch: "buscar en internet", read: "leer archivos",
};
const AGENT_STATUS_ICON = { completed: "✓", error: "✕", running: "●", pending: "○" };
const agentViews = {};  // session_id -> vista abierta en el log (no duplicar)

function setModeDesc(m) {
  modeDesc.textContent = m.desc;
  if (m.credits) {
    const credit = document.createElement("span");
    credit.className = "mode-credit";
    credit.innerHTML = " · " + creditsHtml(m.credits);
    modeDesc.appendChild(credit);
  }
}

function agentTimeAgo(ms) {
  if (!ms) return "";
  const mins = Math.round((Date.now() - ms) / 60000);
  if (mins < 1) return "ahora";
  if (mins < 60) return `hace ${mins} min`;
  const hours = Math.round(mins / 60);
  return hours < 24 ? `hace ${hours} h` : `hace ${Math.round(hours / 24)} d`;
}

async function agentPost(url, body) {
  const resp = await fetch(url, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || `Error ${resp.status}`);
  return data;
}

async function showAgentTaskList() {
  let tasks;
  try {
    const resp = await fetch("/agent/tasks");
    if (!resp.ok) return;
    tasks = await resp.json();
  } catch (e) { return; }
  if (!tasks.length || currentMode !== "agente") return;
  const bubble = addMessage("assistant", "agente");
  const title = document.createElement("div");
  title.className = "agent-tasks-title";
  title.textContent = "Tareas recientes del agente - pulsa una para ver su estado:";
  const list = document.createElement("div");
  list.className = "agent-tasks";
  for (const t of tasks.slice(0, 6)) {
    const btn = document.createElement("button");
    const icon = document.createElement("span");
    icon.textContent = t.status === "idle" ? "✓" : "●";
    icon.style.color = t.status === "idle" ? "var(--muted)" : "var(--accent)";
    const name = document.createElement("span");
    name.textContent = t.title || t.session_id;
    const when = document.createElement("span");
    when.className = "when";
    when.textContent = t.status === "idle" ? agentTimeAgo(t.updated) : "en marcha";
    btn.append(icon, name, when);
    btn.addEventListener("click", () => openAgentView(t.session_id));
    list.appendChild(btn);
  }
  bubble.append(title, list);
}

// --- Modo Codigo: trabajar en un proyecto del usuario (como un asistente de programacion) ---
const codeState = { project: null, mode: "plan", fast: false, recent: [] };
try {
  codeState.mode = localStorage.getItem("ia_code_mode") || "plan";
  codeState.fast = localStorage.getItem("ia_code_fast") === "on";
} catch (e) { /* sin almacenamiento */ }
const codeMenu = document.getElementById("codeProjectMenu");

function renderCodeBar() {
  const p = codeState.project;
  document.getElementById("codeProjectName").textContent = p ? p.name : "Abrir un proyecto…";
  document.getElementById("codeProjectBtn").title = p ? p.path : "Elige la carpeta de tu proyecto";
  document.querySelectorAll("[data-code-mode]").forEach((b) => b.classList.toggle("active", b.dataset.codeMode === codeState.mode));
  document.getElementById("codeFast").checked = codeState.fast;
  document.getElementById("codeGitWarn").style.display = p && !p.git ? "inline" : "none";
  document.getElementById("codeAnalyzeBtn").style.display = p ? "inline-block" : "none";
  document.getElementById("codeAnalyzeBtn").textContent = p && p.agents_md ? "📋 Actualizar AGENTS.md" : "📋 Analizar proyecto";
}

async function initCodeMode() {
  renderCodeBar();
  try {
    const data = await (await fetch("/code/projects")).json();
    codeState.recent = data.projects || [];
  } catch (e) { codeState.recent = []; }
  let last = null;
  try { last = localStorage.getItem("ia_code_project"); } catch (e) { /* nada */ }
  const known = codeState.recent.find((p) => p.path === last && p.exists);
  if (known && !codeState.project) await openCodeProject(known.path, true);
  else if (codeState.project) showCodeWelcome();
  else {
    const bubble = addMessage("assistant", "código");
    bubble.innerHTML = "Abre la carpeta de un proyecto con <b>📁</b> (abajo) y pregúntame lo que quieras sobre él: " +
      "cómo está organizado, cómo mejorar su estructura, o que haga cambios.<br><br>" +
      "<b>🧭 Planificar</b> mira y propone sin tocar nada · <b>🛠 Construir</b> hace los cambios, pidiendo permiso.";
  }
}

async function openCodeProject(path, quiet = false) {
  let info;
  try {
    info = await agentPost("/code/projects", { path });
  } catch (err) {
    if (!quiet) alert(err.message || err);
    return;
  }
  codeState.project = info;
  try { localStorage.setItem("ia_code_project", info.path); } catch (e) { /* nada */ }
  codeState.recent = [info, ...codeState.recent.filter((p) => p.path !== info.path)];
  renderCodeBar();
  codeMenu.classList.remove("show");
  showCodeWelcome();
}

async function showCodeWelcome() {
  const p = codeState.project;
  if (!p || currentMode !== "codigo") return;
  const bubble = addMessage("assistant", "código");
  bubble.classList.add("code-welcome");
  const title = document.createElement("div");
  title.innerHTML = "📁 <b></b>";
  title.querySelector("b").textContent = p.name;
  const path = document.createElement("div");
  path.className = "files";
  path.textContent = p.path;
  const files = document.createElement("div");
  files.className = "files";
  files.textContent = p.entries.join("   ") + (p.total > p.entries.length ? `   … (+${p.total - p.entries.length})` : "");
  const tip = document.createElement("div");
  tip.style.fontSize = "12.5px";
  tip.textContent = p.agents_md
    ? "Tiene AGENTS.md: el agente lo lee en cada conversación."
    : "Consejo: pulsa «📋 Analizar proyecto» para que escriba un AGENTS.md con la estructura y cómo se trabaja en él.";
  bubble.append(title, path, files, tip);
  try {
    const tasks = await (await fetch(`/code/tasks?path=${encodeURIComponent(p.path)}`)).json();
    if (Array.isArray(tasks) && tasks.length) {
      const t = document.createElement("div");
      t.className = "agent-tasks-title";
      t.style.marginTop = "10px";
      t.textContent = "Conversaciones anteriores en este proyecto:";
      const list = document.createElement("div");
      list.className = "agent-tasks";
      for (const task of tasks.slice(0, 6)) {
        const btn = document.createElement("button");
        const name = document.createElement("span");
        name.textContent = task.title || task.session_id;
        const when = document.createElement("span");
        when.className = "when";
        when.textContent = task.status === "idle" ? agentTimeAgo(task.updated) : "en marcha";
        btn.append(name, when);
        btn.addEventListener("click", () => openAgentView(task.session_id, null, codeState.mode));
        list.appendChild(btn);
      }
      bubble.append(t, list);
    }
  } catch (e) { /* sin historial */ }
}

function renderCodeMenu() {
  codeMenu.innerHTML = "";
  for (const p of codeState.recent.filter((x) => x.exists).slice(0, 8)) {
    const b = document.createElement("button");
    b.className = "item";
    b.textContent = p.name;
    const small = document.createElement("small");
    small.textContent = p.path;
    b.appendChild(small);
    b.addEventListener("click", () => openCodeProject(p.path));
    codeMenu.appendChild(b);
  }
  const pick = document.createElement("button");
  pick.className = "item";
  pick.textContent = "📂 Abrir otra carpeta…";
  pick.addEventListener("click", async () => {
    codeMenu.classList.remove("show");
    pick.disabled = true;
    try {
      const data = await agentPost("/code/pick_folder");
      if (data.path) await openCodeProject(data.path);
    } catch (err) { alert(err.message || err); }
  });
  codeMenu.appendChild(pick);
  const row = document.createElement("div");
  row.className = "path-row";
  const input = document.createElement("input");
  input.placeholder = "o escribe la ruta: C:\\Users\\...\\mi-proyecto";
  const go = document.createElement("button");
  go.textContent = "Abrir";
  const openTyped = () => { if (input.value.trim()) openCodeProject(input.value.trim()); };
  go.addEventListener("click", openTyped);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") openTyped(); });
  row.append(input, go);
  codeMenu.appendChild(row);
}

document.getElementById("codeProjectBtn").addEventListener("click", (e) => {
  e.stopPropagation();
  if (codeMenu.classList.toggle("show")) renderCodeMenu();
});
codeMenu.addEventListener("click", (e) => e.stopPropagation());
document.addEventListener("click", () => codeMenu.classList.remove("show"));
document.querySelectorAll("[data-code-mode]").forEach((b) => b.addEventListener("click", () => {
  codeState.mode = b.dataset.codeMode;
  try { localStorage.setItem("ia_code_mode", codeState.mode); } catch (e) { /* nada */ }
  renderCodeBar();
}));
document.getElementById("codeFast").addEventListener("change", (e) => {
  codeState.fast = e.target.checked;
  try { localStorage.setItem("ia_code_fast", codeState.fast ? "on" : "off"); } catch (err) { /* nada */ }
  prepareModelsForMode();
});
document.getElementById("codeGitInit").addEventListener("click", async () => {
  const p = codeState.project;
  if (!p || !confirm(`¿Activar git en «${p.name}»?\n\nSe crea una carpeta oculta .git en el proyecto. Sirve para que Chati pueda enseñarte y deshacer los cambios del agente. No se sube nada a internet.`)) return;
  try {
    codeState.project = await agentPost("/code/git_init", { path: p.path });
    renderCodeBar();
  } catch (err) { alert(err.message || err); }
});
document.getElementById("codeAnalyzeBtn").addEventListener("click", () => {
  startAgentTask("Analiza este proyecto: para qué sirve, estructura de carpetas, tecnologías, cómo se ejecuta y cómo " +
    "se prueba, y las convenciones del código. Después crea (o actualiza, si ya existe) el archivo AGENTS.md en la raíz " +
    "del proyecto con ese resumen: breve, concreto y útil para quien vaya a programar en él.", { codeMode: "build" });
});

// Tarea que parece compleja para el agente rapido: se recomienda el
// potente y el usuario elige (ver _assess_for_fast_agent en main.py)
function renderAgentChoice(task, reason, attachments = []) {
  const bubble = addMessage("assistant", "agente");
  const card = document.createElement("div");
  card.className = "agent-choice";
  const title = document.createElement("div");
  title.className = "ac-title";
  title.textContent = "🧠 Esta tarea parece compleja para el modelo rapido";
  const why = document.createElement("div");
  why.className = "ac-why";
  why.textContent = reason ? "Motivo: " + reason : "";
  const rec = document.createElement("div");
  rec.className = "ac-rec";
  rec.textContent = "Te recomiendo el modelo potente: se equivoca menos, pero tarda minutos en vez de segundos y ocupa casi toda la RAM mientras trabaja.";
  const btns = document.createElement("div");
  btns.className = "ac-btns";
  const launch = async (potente) => {
    btns.querySelectorAll("button").forEach((b) => { b.disabled = true; });
    try {
      const data = await agentPost("/agent/tasks", { task, potente, confirmed: true, attachments });
      btns.innerHTML = "";
      const done = document.createElement("div");
      done.className = "ac-why";
      done.textContent = potente ? "Lanzada con el modelo potente." : "Lanzada con el modelo rapido.";
      btns.appendChild(done);
      openAgentView(data.session_id, Date.now());
    } catch (err) {
      btns.querySelectorAll("button").forEach((b) => { b.disabled = false; });
      alert(String(err.message || err));
    }
  };
  const strong = document.createElement("button");
  strong.className = "primary";
  strong.textContent = "Usar el modelo potente";
  strong.addEventListener("click", () => launch(true));
  const fast = document.createElement("button");
  fast.textContent = "Probar con el rapido";
  fast.addEventListener("click", () => launch(false));
  btns.append(strong, fast);
  card.append(title, why, rec, btns);
  bubble.appendChild(card);
  log.scrollTop = log.scrollHeight;
}

// --- Atajos del agente: tareas que se repiten, a un clic ---
let agentShortcuts = [];
const agentShortcutsRow = document.getElementById("agentShortcutsRow");

async function loadAgentShortcuts() {
  try {
    const resp = await fetch("/agent/shortcuts");
    if (resp.ok) agentShortcuts = await resp.json();
  } catch (e) { /* sin atajos */ }
  renderAgentShortcuts();
}

function renderAgentShortcuts() {
  agentShortcutsRow.innerHTML = "";
  for (const sc of agentShortcuts) {
    const b = document.createElement("button");
    b.textContent = "⚡ " + sc.name + (sc.potente ? " 🧠" : "");
    b.title = sc.task + (sc.potente ? "\n(con el modelo potente)" : "");
    b.addEventListener("click", () => startAgentTask(sc.task, { potente: sc.potente }));
    agentShortcutsRow.appendChild(b);
  }
  const manage = document.createElement("button");
  manage.className = "manage";
  manage.textContent = agentShortcuts.length ? "⚙ Atajos" : "+ Crear atajo";
  manage.addEventListener("click", () => openShortcutsModal());
  agentShortcutsRow.appendChild(manage);
}

// --- Opciones > Creditos ---
const CREDITS = [
  ["Ollama", "https://ollama.com", "Ejecuta los modelos de texto, programacion y vision."],
  ["Qwen (Alibaba)", "https://qwenlm.github.io", "Los modelos de texto, del agente y de vision (qwen2.5, qwen3, qwen3-coder, qwen2.5-vl)."],
  ["ComfyUI", "https://www.comfy.org", "Genera las imagenes y los videos."],
  ["FLUX.1 (Black Forest Labs)", "https://blackforestlabs.ai", "Modelo de imagen por defecto."],
  ["Stable Diffusion XL (Stability AI)", "https://stability.ai", "Modelo de imagen para caras, personas y retoques."],
  ["IP-Adapter FaceID", "https://github.com/tencent-ailab/IP-Adapter", "Mantener una cara real en las imagenes."],
  ["LTX-Video (Lightricks)", "https://github.com/Lightricks/LTX-Video", "Modelo de video."],
  ["sd-scripts (kohya-ss)", "https://github.com/kohya-ss/sd-scripts", "Entrena las personas."],
  ["OpenCode", "https://opencode.ai", "El agente que trabaja con tus archivos."],
  ["faster-whisper", "https://github.com/SYSTRAN/faster-whisper", "Entiende lo que dices en el modo Voz."],
  ["Piper", "https://github.com/rhasspy/piper", "La voz con la que responde Chati."],
  ["Playwright", "https://playwright.dev", "Maneja el navegador del buscador de empleo."],
  ["ChromaDB", "https://www.trychroma.com", "Guarda la memoria y tus documentos para buscarlos."],
  ["FastAPI", "https://fastapi.tiangolo.com", "El servidor interno de Chati."],
  ["pywebview", "https://pywebview.flowrl.com", "La ventana de la aplicacion."],
];
const creditsModal = document.getElementById("creditsModal");
document.getElementById("optCreditsBtn").addEventListener("click", () => {
  optionsModal.style.display = "none";
  const list = document.getElementById("creditsList");
  list.innerHTML = "";
  for (const [name, url, what] of CREDITS) {
    const row = document.createElement("div");
    const a = document.createElement("a");
    a.href = webUrl(url);
    a.target = "_blank";
    a.rel = "noopener";
    a.textContent = name;
    const w = document.createElement("div");
    w.className = "what";
    w.textContent = what;
    row.append(a, w);
    list.appendChild(row);
  }
  creditsModal.style.display = "block";
});
document.getElementById("closeCreditsBtn").addEventListener("click", () => { creditsModal.style.display = "none"; });
creditsModal.addEventListener("click", (e) => { if (e.target === creditsModal) creditsModal.style.display = "none"; });

// --- Busqueda profunda (Mis apps, ver deep_search.py) ---
const deepModal = document.getElementById("deepModal");
let deepData = { history: [], status: { running: false } };
let deepPoll = null;
let deepShownId = null;
let deepUnseen = false;

async function loadDeep() {
  const resp = await fetch("/deep");
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.detail || resp.status);
  deepData = data;
  renderDeepHistory();
  renderDeepStatus();
}

function openDeepModal() {
  deepUnseen = false;
  deepModal.style.display = "block";
  document.getElementById("deepRunText").textContent = "Cargando…";
  loadDeep().then(() => {
    const st = deepData.status || {};
    if (!st.running && !deepShownId && deepData.history.length) showDeepResult(deepData.history[0].id);
    if (st.running) followDeep();
  }).catch((err) => { document.getElementById("deepRunText").textContent = "Error: " + err.message; });
}
document.getElementById("closeDeepBtn").addEventListener("click", () => { deepModal.style.display = "none"; updateJobsChip(); });

function renderDeepHistory() {
  const sel = document.getElementById("deepHistory");
  sel.innerHTML = '<option value="">Búsquedas anteriores…</option>';
  for (const h of deepData.history) {
    const opt = document.createElement("option");
    opt.value = h.id;
    const when = new Date(h.fecha * 1000).toLocaleString("es-ES", { dateStyle: "short", timeStyle: "short" });
    opt.textContent = `${h.consulta.split("\n")[0].slice(0, 60)} · ${when}`;
    sel.appendChild(opt);
  }
  sel.value = deepShownId || "";
}
document.getElementById("deepHistory").addEventListener("change", (e) => { if (e.target.value) showDeepResult(e.target.value); });

function renderDeepStatus() {
  const st = deepData.status || {};
  const text = document.getElementById("deepRunText");
  const bar = document.getElementById("deepRunBar");
  document.getElementById("deepSearchBtn").style.display = st.running ? "none" : "inline-block";
  document.getElementById("deepCancelBtn").style.display = st.running ? "inline-block" : "none";
  document.getElementById("deepRefineBtn").disabled = !!st.running;
  bar.style.display = st.running && st.total ? "block" : "none";
  if (st.running) {
    const secs = Math.floor(Date.now() / 1000 - st.started);
    const mins = secs >= 60 ? `${Math.floor(secs / 60)} min ${secs % 60}s` : `${secs}s`;
    text.textContent = `${st.text} (${mins}) · puedes cerrar esta ventana, sigue buscando`;
    bar.firstElementChild.style.width = st.total ? `${Math.round(100 * st.done / st.total)}%` : "0";
  } else if (st.error) {
    text.textContent = st.error;
  } else {
    text.textContent = "Entiende lo que pides, elige dónde buscar, lee cada página y te lo resume (tarda unos minutos).";
  }
  updateJobsChip();
}

function deepChips(label, values) {
  const wrap = document.createElement("div");
  wrap.style.marginTop = "8px";
  const l = document.createElement("div");
  l.className = "lbl";
  l.textContent = label;
  const chips = document.createElement("div");
  chips.className = "deep-chips";
  for (const v of values) {
    const c = document.createElement("span");
    c.textContent = v;
    chips.appendChild(c);
  }
  wrap.append(l, chips);
  return wrap;
}

function deepItemRow(it, why) {
  const row = document.createElement("div");
  row.className = "deep-item" + (why ? " top" : "");
  const score = document.createElement("div");
  score.className = "deep-score" + (it.encaje < 8 ? " mid" : "");
  score.textContent = it.encaje;
  score.title = "Lo bien que cumple lo pedido (0-10)";
  const t = document.createElement("div");
  t.className = "t";
  t.textContent = it.titulo;
  const a = document.createElement("a");
  a.href = webUrl(it.url);
  a.target = "_blank";
  a.rel = "noopener";
  a.textContent = `Ver en ${it.fuente} ↗`;
  const d = document.createElement("div");
  d.className = "d";
  for (const [k, v] of Object.entries(it.datos || {})) {
    const span = document.createElement("span");
    const b = document.createElement("b");
    b.textContent = k + ": ";
    span.append(b, document.createTextNode(v));
    d.appendChild(span);
  }
  const n = document.createElement("div");
  n.className = "n";
  n.textContent = [why || it.nota, it.tambien_en && it.tambien_en.length ? "También en " + it.tambien_en.join(", ") : ""]
    .filter(Boolean).join(" · ");
  row.append(score, t, a, d, n);
  return row;
}

function showDeepResult(id) {
  const h = deepData.history.find((x) => x.id === id);
  const box = document.getElementById("deepResults");
  box.innerHTML = "";
  deepShownId = id;
  renderDeepHistory();
  document.getElementById("deepRefineRow").style.display = h ? "flex" : "none";
  if (!h) return;
  const u = document.createElement("div");
  u.className = "deep-understood";
  u.innerHTML = '<div class="lbl">Lo que he entendido</div>';
  u.appendChild(document.createTextNode(h.entendido));
  if (h.criterios.length) u.appendChild(deepChips("Criterios", h.criterios));
  if (h.que_es && h.que_es.si.length) u.appendChild(deepChips("Solo si es", h.que_es.si));
  if (h.que_es && h.que_es.no.length) u.appendChild(deepChips("Descarto si es", h.que_es.no));
  if (h.webs.length) u.appendChild(deepChips("Dónde he buscado, además de todo internet", h.webs));
  const meta = document.createElement("div");
  meta.className = "lbl";
  meta.style.marginTop = "8px";
  meta.textContent = `${h.paginas} páginas revisadas`;
  u.appendChild(meta);
  box.appendChild(u);
  for (const w of h.avisos || []) {
    const div = document.createElement("div");
    div.className = "jobs-warn";
    div.textContent = "⚠ " + w;
    box.appendChild(div);
  }
  if (h.tipo === "informe") {
    const rep = document.createElement("div");
    rep.className = "deep-report";
    renderMarkdownInto(rep, h.informe || "No se encontró información útil.", { highlight: false });
    box.appendChild(rep);
    if (h.fuentes.length) {
      const src = document.createElement("div");
      src.className = "deep-sources";
      h.fuentes.forEach((f, i) => {
        const a = document.createElement("a");
        a.href = webUrl(f.url);
        a.target = "_blank";
        a.rel = "noopener";
        a.textContent = `[${i + 1}] ${f.titulo} — ${f.fuente}`;
        src.appendChild(a);
      });
      box.appendChild(src);
    }
    return;
  }
  if (h.resumen) {
    const sum = document.createElement("div");
    sum.className = "deep-summary";
    sum.textContent = h.resumen;
    box.appendChild(sum);
  }
  if (h.destacados.length) {
    const t = document.createElement("div");
    t.className = "shop-section";
    t.textContent = "Destacados";
    box.appendChild(t);
    for (const dst of h.destacados) {
      const it = h.resultados.find((x) => x.url === dst.url && x.titulo === dst.titulo) || h.resultados.find((x) => x.url === dst.url);
      if (it) box.appendChild(deepItemRow(it, dst.por_que));
    }
  }
  if (h.resultados.length) {
    const t = document.createElement("div");
    t.className = "shop-section";
    t.textContent = `Todos (${h.resultados.length}), de más a menos encaje`;
    box.appendChild(t);
    for (const it of h.resultados) box.appendChild(deepItemRow(it));
  } else {
    box.insertAdjacentHTML("beforeend", '<p class="personas-intro">No se encontró nada que cumpla lo pedido. Prueba a afinar o a pedirlo de otra forma.</p>');
  }
}

async function startDeep(body) {
  try {
    await agentPost("/deep/search", body);
  } catch (err) {
    document.getElementById("deepRunText").textContent = err.message;
    return;
  }
  deepData.status = { running: true, text: "Empezando…", started: Date.now() / 1000, done: 0, total: 0 };
  renderDeepStatus();
  followDeep();
}
document.getElementById("deepSearchBtn").addEventListener("click", () => {
  const consulta = document.getElementById("deepQuery").value.trim();
  if (!consulta) { document.getElementById("deepQuery").focus(); return; }
  startDeep({ consulta });
});
document.getElementById("deepRefineBtn").addEventListener("click", () => {
  const input = document.getElementById("deepRefine");
  const extra = input.value.trim();
  if (!extra || !deepShownId) { input.focus(); return; }
  input.value = "";
  startDeep({ consulta: extra, afinar_de: deepShownId });
});
document.getElementById("deepRefine").addEventListener("keydown", (e) => { if (e.key === "Enter") document.getElementById("deepRefineBtn").click(); });
document.getElementById("deepCancelBtn").addEventListener("click", () => agentPost("/deep/cancel").catch(() => {}));

function followDeep() {
  if (deepPoll) return;
  deepPoll = setInterval(async () => {
    let st;
    try { st = await (await fetch("/deep/status")).json(); } catch (e) { return; }
    deepData.status = st;
    if (st.running) { renderDeepStatus(); return; }
    clearInterval(deepPoll);
    deepPoll = null;
    await loadDeep().catch(() => {});
    if (st.last_id && !st.error) {
      showDeepResult(st.last_id);
      if (deepModal.style.display !== "block") deepUnseen = true;
    }
    updateJobsChip();
  }, 2000);
}

// --- Compras (Mis apps, ver shopping.py) ---
const shopModal = document.getElementById("shopModal");
let shopData = { history: [], status: { running: false } };
let shopPoll = null;
let shopPhotoB64 = null;
let shopShownId = null;
let shopUnseen = false;

function shopMoney(v, cur) {
  if (v === null || v === undefined) return "—";
  return v.toLocaleString("es-ES", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + (cur && cur !== "EUR" ? " " + cur : " €");
}

async function loadShopping() {
  const resp = await fetch("/shopping");
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.detail || resp.status);
  shopData = data;
  renderShopHistory();
  renderShopStatus();
}

function openShopModal() {
  shopUnseen = false;
  shopModal.style.display = "block";
  document.getElementById("shopRunText").textContent = "Cargando…";
  loadShopping().then(() => {
    const st = shopData.status || {};
    if (!st.running && !shopShownId && shopData.history.length) showShopResult(shopData.history[0].id);
    if (st.running) followShopping();
  }).catch((err) => { document.getElementById("shopRunText").textContent = "Error: " + err.message; });
}
document.getElementById("closeShopBtn").addEventListener("click", () => { shopModal.style.display = "none"; updateJobsChip(); });

function renderShopHistory() {
  const sel = document.getElementById("shopHistory");
  sel.innerHTML = '<option value="">Búsquedas anteriores…</option>';
  for (const h of shopData.history) {
    const opt = document.createElement("option");
    opt.value = h.id;
    const when = new Date(h.fecha * 1000).toLocaleString("es-ES", { dateStyle: "short", timeStyle: "short" });
    opt.textContent = `${h.producto} · ${when}`;
    sel.appendChild(opt);
  }
  sel.value = shopShownId || "";
}
document.getElementById("shopHistory").addEventListener("change", (e) => { if (e.target.value) showShopResult(e.target.value); });

function renderShopStatus() {
  const st = shopData.status || {};
  const text = document.getElementById("shopRunText");
  const bar = document.getElementById("shopRunBar");
  document.getElementById("shopSearchBtn").style.display = st.running ? "none" : "inline-block";
  document.getElementById("shopCancelBtn").style.display = st.running ? "inline-block" : "none";
  bar.style.display = st.running && st.total ? "block" : "none";
  if (st.running) {
    const secs = Math.floor(Date.now() / 1000 - st.started);
    const mins = secs >= 60 ? `${Math.floor(secs / 60)} min ${secs % 60}s` : `${secs}s`;
    text.textContent = `${st.text} (${mins}) · puedes cerrar esta ventana, sigue buscando`;
    bar.firstElementChild.style.width = st.total ? `${Math.round(100 * st.done / st.total)}%` : "0";
  } else if (st.error) {
    text.textContent = st.error;
  } else {
    text.textContent = "Busca en todo internet, lee cada tienda y ordena por precio (tarda unos minutos).";
  }
  updateJobsChip();
}

function shopOfferRow(o, cls) {
  const row = document.createElement("div");
  row.className = cls;
  if (o.imagen) {
    const img = document.createElement("img");
    img.src = webUrl(o.imagen);
    img.loading = "lazy";
    img.onerror = () => img.remove();
    row.appendChild(img);
  }
  const info = document.createElement("div");
  info.className = "info";
  const name = document.createElement("div");
  name.className = "name";
  name.textContent = o.nombre;
  name.title = o.nombre;
  if (o.sospechoso) name.insertAdjacentHTML("beforeend", '<span class="shop-flag warn" title="Mucho más barato que el resto: comprueba que la tienda es de fiar">PRECIO SOSPECHOSO</span>');
  if (!o.disponible) name.insertAdjacentHTML("beforeend", '<span class="shop-flag off">AGOTADO</span>');
  const meta = document.createElement("div");
  meta.className = "meta";
  const envio = o.coste_envio === 0 ? "envío gratis" : o.coste_envio ? `envío ${shopMoney(o.coste_envio, o.moneda)}`
    : o.envio_espana === "si" ? "envía a España" : o.envio_espana === "no" ? "no envía a España" : "envío: ?";
  const rating = o.valoracion ? `★ ${o.valoracion.toFixed(1)}` + (o.opiniones ? ` (${o.opiniones})` : "") : "";
  meta.textContent = [o.tienda, envio, rating, o.estado !== "nuevo" ? o.estado : ""].filter(Boolean).join(" · ");
  info.append(name, meta);
  const price = document.createElement("div");
  price.className = "shop-price";
  price.textContent = shopMoney(o.precio, o.moneda);
  const link = document.createElement("a");
  link.href = webUrl(o.url);
  link.target = "_blank";
  link.rel = "noopener";
  link.textContent = "Ver en la tienda ↗";
  row.append(info, price, link);
  return { row, info };
}

function showShopResult(id) {
  const h = shopData.history.find((x) => x.id === id);
  const box = document.getElementById("shopResults");
  box.innerHTML = "";
  shopShownId = id;
  renderShopHistory();
  if (!h) return;
  const head = document.createElement("div");
  head.className = "shop-head";
  const f = h.peticion;
  const rango = f.precio_min !== null || f.precio_max !== null
    ? ` · ${f.precio_min !== null ? shopMoney(f.precio_min) : "0 €"} – ${f.precio_max !== null ? shopMoney(f.precio_max) : "sin límite"}` : "";
  head.textContent = `«${h.producto}»${rango} · ${h.ofertas.length} ofertas de ${h.revisadas} páginas revisadas`
    + (h.descripcion_foto ? ` · en la foto: ${h.descripcion_foto}` : "");
  box.appendChild(head);
  for (const w of h.avisos || []) {
    const div = document.createElement("div");
    div.className = "jobs-warn";
    div.textContent = "⚠ " + w;
    box.appendChild(div);
  }
  if (h.recomendadas.length) {
    const t = document.createElement("div");
    t.className = "shop-section";
    t.textContent = "Recomendadas";
    box.appendChild(t);
    for (const r of h.recomendadas) {
      const o = h.ofertas.find((x) => x.url === r.url);
      if (!o) continue;
      const { row, info } = shopOfferRow(o, "shop-rec");
      const why = document.createElement("div");
      why.className = "why";
      why.textContent = r.por_que;
      info.appendChild(why);
      box.appendChild(row);
    }
  }
  if (h.ofertas.length) {
    const t = document.createElement("div");
    t.className = "shop-section";
    t.textContent = "Todas, de más barata a más cara";
    box.appendChild(t);
    for (const o of h.ofertas) box.appendChild(shopOfferRow(o, "shop-item").row);
  } else {
    box.insertAdjacentHTML("beforeend", '<p class="personas-intro">No se encontró nada que cumpla lo pedido. Prueba a describirlo de otra forma o a quitar filtros.</p>');
  }
}

document.getElementById("shopPhotoBtn").addEventListener("click", () => document.getElementById("shopPhotoInput").click());
document.getElementById("shopPhotoInput").addEventListener("change", (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    shopPhotoB64 = String(reader.result).split(",")[1];
    const img = document.getElementById("shopPhotoPreview");
    img.src = reader.result;
    img.style.display = "inline";
    document.getElementById("shopPhotoClear").style.display = "inline";
  };
  reader.readAsDataURL(file);
  e.target.value = "";
});
document.getElementById("shopPhotoClear").addEventListener("click", () => {
  shopPhotoB64 = null;
  document.getElementById("shopPhotoPreview").style.display = "none";
  document.getElementById("shopPhotoClear").style.display = "none";
});

document.getElementById("shopSearchBtn").addEventListener("click", async () => {
  const body = {
    descripcion: document.getElementById("shopDesc").value.trim(),
    image_base64: shopPhotoB64,
    precio_min: document.getElementById("shopMin").value || null,
    precio_max: document.getElementById("shopMax").value || null,
    solo_espana: document.getElementById("shopSpain").checked,
    valoraciones: document.getElementById("shopRatings").checked,
  };
  try {
    await agentPost("/shopping/search", body);
  } catch (err) {
    document.getElementById("shopRunText").textContent = err.message;
    return;
  }
  shopData.status = { running: true, text: "Empezando…", started: Date.now() / 1000, done: 0, total: 0 };
  renderShopStatus();
  followShopping();
});
document.getElementById("shopCancelBtn").addEventListener("click", () => agentPost("/shopping/cancel").catch(() => {}));

function followShopping() {
  if (shopPoll) return;
  shopPoll = setInterval(async () => {
    let st;
    try { st = await (await fetch("/shopping/status")).json(); } catch (e) { return; }
    shopData.status = st;
    if (st.running) { renderShopStatus(); return; }
    clearInterval(shopPoll);
    shopPoll = null;
    await loadShopping().catch(() => {});
    if (st.last_id && !st.error) {
      showShopResult(st.last_id);
      if (shopModal.style.display !== "block") shopUnseen = true;
    }
    updateJobsChip();
  }, 2000);
}

// --- Buscador de empleo autonomo (n.º 7 del roadmap, ver job_search.py) ---
const jobsModal = document.getElementById("jobsModal");
let jobsData = null;
let jobsPoll = null;
let jobsUnseen = 0;  // ofertas nuevas de una busqueda que acabo con la ventana cerrada

// el boton "Mis apps" avisa de una busqueda en marcha o de ofertas nuevas
function updateJobsChip() {
  const running = jobsData && jobsData.status && jobsData.status.running;
  const note = running ? "buscando…" : jobsUnseen ? `${jobsUnseen} ofertas nuevas` : "";
  const shopRunning = shopData.status && shopData.status.running;
  const shopNote = shopRunning ? "comparando precios…" : shopUnseen ? "resultados listos" : "";
  const deepRunning = deepData.status && deepData.status.running;
  const deepNote = deepRunning ? "buscando a fondo…" : deepUnseen ? "búsqueda lista" : "";
  document.getElementById("appDeepDesc").textContent = deepNote ? `🔎 ${deepNote[0].toUpperCase()}${deepNote.slice(1)}`
    : "Pídele lo que quieras encontrar en internet y se lo curra: casas, coches, cursos, información…";
  const all = note || shopNote || deepNote;
  document.getElementById("appsBtn").textContent = "🧩 Mis apps" + (all ? ` · ${all}` : "");
  document.getElementById("appJobsDesc").textContent = note ? `💼 ${note[0].toUpperCase()}${note.slice(1)}`
    : "Busca ofertas según tu CV, las puntúa y te prepara las cartas de presentación.";
  document.getElementById("appShopDesc").textContent = shopNote ? `🛒 ${shopNote[0].toUpperCase()}${shopNote.slice(1)}`
    : "Dile qué buscas (o pásale una foto) y compara precios en todo internet.";
}

const appsModal = document.getElementById("appsModal");
document.getElementById("appsBtn").addEventListener("click", () => { appsModal.style.display = "block"; });
document.getElementById("closeAppsBtn").addEventListener("click", () => { appsModal.style.display = "none"; });
appsModal.addEventListener("click", (e) => { if (e.target === appsModal) appsModal.style.display = "none"; });
document.querySelector('[data-app="jobs"]').addEventListener("click", () => {
  appsModal.style.display = "none";
  openJobsModal();
});
document.querySelector('[data-app="shopping"]').addEventListener("click", () => {
  appsModal.style.display = "none";
  openShopModal();
});
document.querySelector('[data-app="deep"]').addEventListener("click", () => {
  appsModal.style.display = "none";
  openDeepModal();
});

async function loadJobs() {
  const resp = await fetch("/jobs");
  jobsData = await resp.json();
  if (!resp.ok) throw new Error(jobsData.detail || resp.status);
  renderJobs();
}

function showJobsTab(tab) {
  document.querySelectorAll("[data-jobs-tab]").forEach((x) => x.classList.toggle("active", x.dataset.jobsTab === tab));
  document.querySelectorAll("[data-jobs-pane]").forEach((pane) => {
    pane.style.display = pane.dataset.jobsPane === tab ? "block" : "none";
  });
}

function openJobsModal() {
  jobsUnseen = 0;
  jobsModal.style.display = "block";
  document.getElementById("jobsRunText").textContent = "Cargando…";
  loadCvStatus();
  loadJobs().then(() => { showJobsTab(jobsData.has_cv ? "ofertas" : "cv"); })
    .catch((err) => { document.getElementById("jobsRunText").textContent = "Error: " + err.message; });
}
document.getElementById("closeJobsBtn").addEventListener("click", () => {
  jobsModal.style.display = "none";
  updateJobsChip();
});

document.querySelectorAll("[data-jobs-tab]").forEach((b) => b.addEventListener("click", () => showJobsTab(b.dataset.jobsTab)));

function renderJobsStatus() {
  const st = jobsData.status || {};
  const text = document.getElementById("jobsRunText");
  const bar = document.getElementById("jobsRunBar");
  const searchBtn = document.getElementById("jobsSearchBtn");
  const cancelBtn = document.getElementById("jobsCancelBtn");
  searchBtn.style.display = st.running ? "none" : "inline-block";
  cancelBtn.style.display = st.running ? "inline-block" : "none";
  bar.style.display = st.running && st.total ? "block" : "none";
  if (st.running) {
    const secs = Math.floor(Date.now() / 1000 - st.started);
    const mins = secs >= 60 ? `${Math.floor(secs / 60)} min ${secs % 60}s` : `${secs}s`;
    text.textContent = `${st.text} (${mins}) · puedes cerrar esta ventana, sigue buscando`;
    bar.firstElementChild.style.width = st.total ? `${Math.round(100 * st.done / st.total)}%` : "0";
  } else if (st.error) {
    text.textContent = st.error;
  } else if (!jobsData.has_cv) {
    text.textContent = "Primero sube tu CV en la pestaña «Mi CV».";
  } else {
    text.textContent = "Busca en las webs que tengas activadas y puntúa cada oferta según tu CV (tarda unos minutos).";
  }
  searchBtn.disabled = !jobsData.has_cv;
  updateJobsChip();
}

function jobCard(o) {
  const card = document.createElement("div");
  card.className = "job-card";
  const score = document.createElement("div");
  score.className = "job-score" + (o.puntuacion >= 70 ? " good" : o.puntuacion >= 50 ? " mid" : "");
  score.textContent = o.puntuacion;
  score.title = "Encaje con tu CV (0-100)";
  const title = document.createElement("div");
  title.className = "job-title";
  title.textContent = o.titulo;
  if (o.nueva) {
    const n = document.createElement("span");
    n.className = "new";
    n.textContent = "NUEVA";
    title.appendChild(n);
  }
  const meta = document.createElement("div");
  meta.className = "job-meta";
  meta.textContent = [o.empresa, o.ubicacion, "vía " + o.fuente].filter(Boolean).join(" · ");
  const why = document.createElement("div");
  why.className = "job-why";
  why.textContent = o.motivos;
  const actions = document.createElement("div");
  actions.className = "job-actions";
  const link = document.createElement("a");
  link.href = webUrl(o.url);
  link.target = "_blank";
  link.rel = "noopener";
  link.textContent = "Ver oferta e inscribirme ↗";
  actions.appendChild(link);
  card.append(score, title, meta, why, actions);
  if (o.carta) {
    const letter = document.createElement("details");
    letter.className = "job-letter";
    const sum = document.createElement("summary");
    sum.textContent = "✉ Carta de presentación preparada (revísala antes de enviarla)";
    const ta = document.createElement("textarea");
    ta.value = o.carta;
    const copy = document.createElement("button");
    copy.className = "dc-btn";
    copy.textContent = "Copiar carta";
    copy.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(ta.value); } catch (e) { ta.select(); document.execCommand("copy"); }
      copy.textContent = "Copiada ✓";
      setTimeout(() => { copy.textContent = "Copiar carta"; }, 1500);
    });
    letter.append(sum, ta, copy);
    card.appendChild(letter);
  }
  return card;
}

function renderJobs() {
  renderJobsStatus();
  const box = document.getElementById("jobsOffers");
  box.innerHTML = "";
  const res = jobsData.results || {};
  for (const w of res.warnings || []) {
    const div = document.createElement("div");
    div.className = "jobs-warn";
    div.textContent = "⚠ " + w;
    box.appendChild(div);
  }
  if (!res.run_at) {
    box.insertAdjacentHTML("beforeend", '<p class="personas-intro">Todavía no has buscado. Pulsa «Buscar ofertas»: ' +
      "Chati leerá tu CV, buscará en las webs de empleo, puntuará cada oferta y te preparará una carta de " +
      "presentación para las que mejor encajen. Inscribirte lo haces tú desde el enlace de cada oferta.</p>");
  } else {
    const head = document.createElement("div");
    head.className = "jobs-head";
    const when = new Date(res.run_at * 1000).toLocaleString("es-ES", { dateStyle: "medium", timeStyle: "short" });
    head.textContent = `Última búsqueda: ${when} · ${res.offers.length} ofertas, de más a menos encaje`;
    box.appendChild(head);
    for (const o of res.offers) box.appendChild(jobCard(o));
  }
  // preferencias
  const pr = jobsData.prefs;
  document.getElementById("jobsPuestos").value = pr.puestos.join(", ");
  document.getElementById("jobsLugar").value = pr.lugar;
  document.getElementById("jobsPrefsText").value = pr.preferencias;
  document.getElementById("jobsMax").value = pr.max_ofertas;
  document.getElementById("jobsInternet").checked = pr.internet;
  renderJobSites();
}

function renderJobSites() {
  const box = document.getElementById("jobsSites");
  box.innerHTML = "";
  jobsData.prefs.sites.forEach((site, i) => {
    const row = document.createElement("div");
    row.className = "jobs-site";
    const chk = document.createElement("input");
    chk.type = "checkbox";
    chk.style.width = "auto";
    chk.checked = site.enabled;
    chk.addEventListener("change", () => { site.enabled = chk.checked; });
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = site.name;
    const url = document.createElement("span");
    url.className = "url";
    url.textContent = site.url.includes("{puesto}") ? "búsqueda directa en la web" : "búsqueda con Bing dentro de " + site.url;
    const del = document.createElement("button");
    del.className = "sc-del";
    del.textContent = "Quitar";
    del.addEventListener("click", () => { jobsData.prefs.sites.splice(i, 1); renderJobSites(); });
    row.append(chk, name, url, del);
    box.appendChild(row);
  });
}

document.getElementById("jobsAddSiteBtn").addEventListener("click", () => {
  const input = document.getElementById("jobsNewSite");
  const url = input.value.trim().replace(/^https?:\/\//, "").replace(/\/.*$/, "");
  if (!url) return;
  jobsData.prefs.sites.push({ name: url.replace(/^www\./, ""), url, enabled: true });
  input.value = "";
  renderJobSites();
  setProfileMsg("jobsWebsMsg", "Pulsa Guardar para quedártela.");
});

async function saveJobPrefs(msgId) {
  const pr = jobsData.prefs;
  pr.puestos = document.getElementById("jobsPuestos").value.split(",").map((x) => x.trim()).filter(Boolean);
  pr.lugar = document.getElementById("jobsLugar").value.trim();
  pr.preferencias = document.getElementById("jobsPrefsText").value.trim();
  pr.max_ofertas = parseInt(document.getElementById("jobsMax").value, 10) || 12;
  pr.internet = document.getElementById("jobsInternet").checked;
  const resp = await fetch("/jobs/prefs", {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ prefs: pr }),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) { setProfileMsg(msgId, data.detail || "No se pudo guardar.", "error"); return false; }
  jobsData.prefs = data;
  renderJobSites();
  setProfileMsg(msgId, "Guardado ✓", "ok");
  return true;
}
document.getElementById("jobsSaveBuscoBtn").addEventListener("click", () => saveJobPrefs("jobsBuscoMsg"));
document.getElementById("jobsSaveWebsBtn").addEventListener("click", () => saveJobPrefs("jobsWebsMsg"));

document.getElementById("jobsSearchBtn").addEventListener("click", async () => {
  const btn = document.getElementById("jobsSearchBtn");
  btn.disabled = true;
  try {
    await agentPost("/jobs/search");
  } catch (err) {
    document.getElementById("jobsRunText").textContent = err.message;
    btn.disabled = false;
    return;
  }
  jobsData.status = { running: true, text: "Empezando…", started: Date.now() / 1000, done: 0, total: 0 };
  renderJobsStatus();
  followJobs();
});
document.getElementById("jobsCancelBtn").addEventListener("click", () => agentPost("/jobs/cancel").catch(() => {}));

// sigue la busqueda aunque se cierre la ventana: al acabar, avisa en el boton
function followJobs() {
  if (jobsPoll) return;
  jobsPoll = setInterval(async () => {
    let st;
    try { st = await (await fetch("/jobs/status")).json(); } catch (e) { return; }
    jobsData.status = st;
    if (st.running) { if (jobsModal.style.display === "block") renderJobsStatus(); else updateJobsChip(); return; }
    clearInterval(jobsPoll);
    jobsPoll = null;
    await loadJobs().catch(() => {});
    if (jobsModal.style.display !== "block") {
      jobsUnseen = ((jobsData.results || {}).offers || []).filter((o) => o.nueva).length;
      updateJobsChip();
    }
  }, 2000);
}

// --- Ventana para gestionarlos ---
const shortcutsModal = document.getElementById("shortcutsModal");
const shortcutsList = document.getElementById("shortcutsList");

function shortcutRow(sc) {
  const row = document.createElement("div");
  row.className = "sc-row";
  const name = document.createElement("input");
  name.placeholder = "Nombre del atajo (p.ej. Ordenar Descargas)";
  name.maxLength = 40;
  name.value = sc.name || "";
  name.className = "sc-name";
  const task = document.createElement("textarea");
  task.placeholder = "Que tiene que hacer el agente";
  task.value = sc.task || "";
  task.className = "sc-task";
  const foot = document.createElement("div");
  foot.className = "sc-foot";
  const lbl = document.createElement("label");
  const chk = document.createElement("input");
  chk.type = "checkbox";
  chk.className = "sc-potente";
  chk.checked = !!sc.potente;
  chk.style.width = "auto";
  lbl.append(chk, document.createTextNode("Usar el modelo potente"));
  const del = document.createElement("button");
  del.className = "sc-del";
  del.textContent = "Borrar";
  del.addEventListener("click", () => row.remove());
  foot.append(lbl, del);
  row.append(name, task, foot);
  return row;
}

function openShortcutsModal(extra) {
  shortcutsList.innerHTML = "";
  for (const sc of agentShortcuts) shortcutsList.appendChild(shortcutRow(sc));
  let focusRow = null;
  if (extra) {
    focusRow = shortcutRow(extra);
    shortcutsList.appendChild(focusRow);
  }
  setProfileMsg("shortcutsMsg", "");
  shortcutsModal.style.display = "block";
  if (focusRow) { focusRow.scrollIntoView({ block: "nearest" }); focusRow.querySelector(".sc-name").focus(); }
}

document.getElementById("addShortcutBtn").addEventListener("click", () => {
  const row = shortcutRow({});
  shortcutsList.appendChild(row);
  row.querySelector(".sc-name").focus();
});
const closeShortcuts = () => { shortcutsModal.style.display = "none"; };
document.getElementById("closeShortcutsBtn").addEventListener("click", closeShortcuts);
document.getElementById("cancelShortcutsBtn").addEventListener("click", closeShortcuts);
document.getElementById("saveShortcutsBtn").addEventListener("click", async () => {
  const list = [...shortcutsList.querySelectorAll(".sc-row")].map((r) => ({
    name: r.querySelector(".sc-name").value.trim(),
    task: r.querySelector(".sc-task").value.trim(),
    potente: r.querySelector(".sc-potente").checked,
  })).filter((sc) => sc.name || sc.task);  // filas vacias: se ignoran
  const resp = await fetch("/agent/shortcuts", {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ shortcuts: list }),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) { setProfileMsg("shortcutsMsg", data.detail || "No se pudo guardar.", "error"); return; }
  agentShortcuts = data;
  renderAgentShortcuts();
  closeShortcuts();
});

// --- Adjuntos para la proxima tarea del agente (n.º 4 del roadmap) ---
let agentPendingFiles = [];
const agentAttachRow = document.getElementById("agentAttachRow");
const agentFilesInput = document.getElementById("agentFilesInput");

function renderAgentAttachments() {
  agentAttachRow.innerHTML = "";
  agentPendingFiles.forEach((f, i) => {
    const chip = document.createElement("span");
    chip.className = "aa-chip";
    const name = document.createElement("span");
    name.textContent = (f.type.startsWith("image/") ? "🖼️ " : "📄 ") + f.name;
    name.title = f.name;
    const rm = document.createElement("button");
    rm.textContent = "✕";
    rm.title = "Quitar";
    rm.addEventListener("click", () => { agentPendingFiles.splice(i, 1); renderAgentAttachments(); });
    chip.append(name, rm);
    agentAttachRow.appendChild(chip);
  });
}
agentFilesInput.addEventListener("change", () => {
  for (const f of agentFilesInput.files) {
    if (!agentPendingFiles.some((x) => x.name === f.name && x.size === f.size)) agentPendingFiles.push(f);
  }
  agentFilesInput.value = "";
  renderAgentAttachments();
  promptEl.focus();
});

async function uploadAgentAttachments(files) {
  const form = new FormData();
  for (const f of files) form.append("files", f);
  const resp = await fetch("/agent/attachments", { method: "POST", body: form });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || "No se pudieron subir los adjuntos.");
  return data.files;
}

async function startAgentTask(text, opts = {}) {
  const files = agentPendingFiles;
  agentPendingFiles = [];
  renderAgentAttachments();
  const userBubble = addMessage("user");
  userBubble.textContent = text;
  if (files.length) {
    const list = document.createElement("div");
    list.className = "agent-attach-list";
    list.textContent = "📎 " + files.map((f) => f.name).join(", ");
    userBubble.appendChild(list);
  }
  sendBtn.disabled = true;
  let attachments = [];
  try {
    if (files.length) {
      const hasImages = files.some((f) => f.type.startsWith("image/"));
      prepareSkipped = false;
      showPrepare("loading", "Preparando los adjuntos…",
        hasImages ? "Copiando los archivos y describiendo las imagenes para el agente (unos segundos por imagen)."
                  : "Copiando los archivos para el agente.", "Ocultar");
      try {
        attachments = await uploadAgentAttachments(files);
      } finally {
        prepareOverlay.classList.remove("show");
      }
    }
    const body = { task: text, potente: !!opts.potente || agentPowerToggle.checked, attachments };
    if (MODES[currentMode].isCode) {
      if (!codeState.project) throw new Error("Primero abre un proyecto (📁 abajo a la izquierda).");
      Object.assign(body, { project: codeState.project.path, plan: (opts.codeMode || codeState.mode) === "plan",
                            rapido: codeState.fast, potente: false });
    }
    const data = await agentPost("/agent/tasks", body);
    if (data.needs_choice) renderAgentChoice(data.task, data.reason, data.attachments || []);
    else openAgentView(data.session_id, Date.now(), MODES[currentMode].isCode ? (opts.codeMode || codeState.mode) : null);
  } catch (err) {
    const bubble = addMessage("assistant", "agente");
    bubble.className = "bubble error";
    bubble.textContent = String(err.message || err);
  } finally {
    sendBtn.disabled = false;
  }
}

function openAgentView(sessionId, startedAt, codeMode = null) {
  const existing = agentViews[sessionId];
  if (existing && document.body.contains(existing.card)) {
    existing.card.scrollIntoView({ behavior: "smooth", block: "center" });
    return;
  }
  const bubble = addMessage("assistant", "agente");
  const card = document.createElement("div");
  card.className = "agent-card";
  card.innerHTML =
    '<div class="agent-head"><span class="agent-status">Conectando con el agente...</span>' +
    '<button class="agent-stop" style="display:none;">Detener</button></div>' +
    '<div class="agent-steps"></div><div class="agent-asks"></div>' +
    '<div class="agent-changes" style="display:none;"></div>' +
    '<div class="agent-follow" style="display:none;"><input type="text" placeholder="Corregir o continuar la tarea… (p.ej. no, ponlo en la carpeta Trabajo)">' +
    '<button>Enviar</button></div>' +
    `<div class="agent-credit">${OPENCODE_CREDIT_HTML}</div>`;
  bubble.appendChild(card);

  const view = {
    sessionId, card, startedAt: startedAt || null, timer: null, askCards: {}, lastSteps: null,
    openedAt: Date.now(),
    statusEl: card.querySelector(".agent-status"),
    stopBtn: card.querySelector(".agent-stop"),
    stepsEl: card.querySelector(".agent-steps"),
    asksEl: card.querySelector(".agent-asks"),
    followEl: card.querySelector(".agent-follow"),
    changesEl: card.querySelector(".agent-changes"), changesKey: null,
    saveScEl: null, task: "",
    stepsAtSend: 0, sentAt: null,
    codeMode,  // modo Codigo: "plan" o "build"; null en el modo Agente
  };
  const followInput = view.followEl.querySelector("input");
  const followBtn = view.followEl.querySelector("button");
  if (codeMode) {
    followInput.placeholder = "Sigue… (p.ej. explícame el archivo X, o cambia esto)";
    // tras un plan: hacerlo tal cual, ya en modo Construir
    const doIt = document.createElement("button");
    doIt.className = "do-it";
    doIt.textContent = "▶ Hacerlo";
    doIt.title = "Sigue en modo Construir: hace lo que ha propuesto (pidiendo permiso para cada cambio)";
    doIt.addEventListener("click", () => sendFollowUp("Adelante: hazlo tal como lo has propuesto.", "build"));
    view.followEl.appendChild(doIt);
    view.doItBtn = doIt;
  }
  const sendFollowUp = async (presetText, presetMode) => {
    const text = presetText || followInput.value.trim();
    if (!text) return;
    followInput.disabled = followBtn.disabled = true;
    const body = { text };
    if (view.codeMode) {
      view.codeMode = presetMode || codeState.mode;
      body.code_mode = view.codeMode;
    }
    try {
      await agentPost(`/agent/tasks/${sessionId}/message`, body);
    } catch (err) {
      alert("No se pudo mandar: " + (err.message || err));
      followInput.disabled = followBtn.disabled = false;
      return;
    }
    followInput.value = "";
    followInput.disabled = followBtn.disabled = false;
    view.followEl.style.display = "none";
    // no dar la tarea por terminada hasta ver la respuesta a esto
    // +1: el propio mensaje del usuario aparece como paso nuevo y no es
    // respuesta del agente (sin esto la tarjeta decia "Terminado" al segundo)
    view.stepsAtSend = (JSON.parse(view.lastSteps || "[]")).length + 1;
    view.sentAt = Date.now();
    view.startedAt = Date.now();
    followAgentView(view);
  };
  followBtn.addEventListener("click", () => sendFollowUp());
  followInput.addEventListener("keydown", (e) => { if (e.key === "Enter") sendFollowUp(); });
  agentViews[sessionId] = view;
  view.stopBtn.addEventListener("click", async () => {
    view.stopBtn.disabled = true;
    try { await agentPost(`/agent/tasks/${sessionId}/abort`); } catch (e) { /* el siguiente sondeo mostrara el estado real */ }
    followAgentView(view);
  });
  followAgentView(view);
}

async function pollAgentView(view) {
  clearTimeout(view.timer);
  if (!document.body.contains(view.card)) return;  // se cambio de conversacion
  let data;
  try {
    const resp = await fetch(`/agent/tasks/${view.sessionId}`);
    data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || `Error ${resp.status}`);
  } catch (err) {
    view.statusEl.className = "agent-status failed";
    view.statusEl.textContent = "Sin conexion con el agente - reintentando... (" + (err.message || err) + ")";
    view.timer = setTimeout(() => pollAgentView(view), AGENT_POLL_MS * 2);
    return;
  }
  renderAgentView(view, data);
  if (!agentDone(view, data)) view.timer = setTimeout(() => pollAgentView(view), AGENT_POLL_MS);
}

// justo despues de lanzar la tarea puede verse "idle" un instante antes de
// que OpenCode la empiece - no darla por terminada hasta ver algo real
function agentDone(view, data) {
  const settled = data.status === "idle" &&
    (data.steps.length > view.stepsAtSend || data.error || Date.now() - (view.sentAt || view.openedAt) > 20000);
  return settled && !data.questions.length && !data.permissions.length;
}

// Seguir la tarea EN DIRECTO (n.º 3 del roadmap): el servidor reenvia los
// eventos de OpenCode y cada uno trae el estado completo de la tarea - el
// texto aparece mientras se escribe. Si el directo falla, se vuelve a
// preguntar cada 2,5s como antes (pollAgentView).
let liveAgentStreams = 0;

function followAgentView(view) {
  clearTimeout(view.timer);
  if (view.es) return;  // ya en directo: el cambio llegara como evento
  // cada directo ocupa una de las ~6 conexiones que el navegador abre a la
  // vez con el servidor: con mas de 2, el resto de la app se quedaria esperando
  if (!window.EventSource || view.liveFailed || liveAgentStreams >= 2) { pollAgentView(view); return; }
  liveAgentStreams++;
  const es = new EventSource(`/agent/tasks/${view.sessionId}/live`);
  view.es = es;
  let closed = false;
  const stop = () => {
    es.close();
    if (!closed) { closed = true; liveAgentStreams--; }
    if (view.es === es) view.es = null;
  };
  es.onmessage = (e) => {
    if (!document.body.contains(view.card)) { stop(); return; }
    const data = JSON.parse(e.data);
    view.lastData = data;
    renderAgentView(view, data);
    if (agentDone(view, data)) { stop(); refreshBackgroundTasks(); }
  };
  const fallback = () => { stop(); view.liveFailed = true; pollAgentView(view); };
  es.addEventListener("end", fallback);
  es.onerror = fallback;
  // el contador "Trabajando... Xs" avanza aunque no lleguen eventos (el
  // modelo puede pasar un rato pensando sin decir nada)
  if (!view.ticker) {
    view.ticker = setInterval(() => {
      if (!document.body.contains(view.card)) { clearInterval(view.ticker); return; }
      if (view.es && view.lastData && view.lastData.status !== "idle") renderAgentView(view, view.lastData);
    }, 1000);
  }
}

function renderAgentView(view, data) {
  const busy = data.status !== "idle";
  let cls = "agent-status", text;
  if (data.questions.length) { cls += " waiting"; text = "Esperando tu respuesta ↓"; }
  else if (data.permissions.length) { cls += " waiting"; text = "Necesita tu permiso para continuar ↓"; }
  else if (data.status === "retry") { text = "Reintentando: " + (data.retry_message || ""); }
  else if (busy && data.model_loading) {
    text = "Cargando el modelo del agente… un momento";
  }
  else if (busy) {
    // tareas reabiertas desde la lista no tienen hora de inicio conocida
    if (view.startedAt) {
      const secs = Math.floor((Date.now() - view.startedAt) / 1000);
      const elapsed = secs >= 60 ? `${Math.floor(secs / 60)} min ${secs % 60}s` : `${secs}s`;
      text = `Trabajando... ${elapsed}` + (secs > 40 ? " (el modelo corre en CPU, es normal que tarde)" : "");
    } else {
      text = "Trabajando...";
    }
  }
  else if (data.error) { cls += " failed"; text = "Se detuvo con un error: " + data.error; }
  // parada pero sin nada nuevo todavia: OpenCode aun no ha empezado la tarea
  // (el directo manda el estado nada mas conectar, antes que el sondeo de 2,5s)
  else if (data.steps.length <= view.stepsAtSend && Date.now() - (view.sentAt || view.openedAt) < 20000) {
    text = "Empezando…";
  }
  else { cls += " done"; text = "Terminado ✓"; }
  view.statusEl.className = cls;
  view.statusEl.textContent = text;
  view.stopBtn.style.display = busy ? "inline-block" : "none";
  view.stopBtn.disabled = false;

  const stepsKey = JSON.stringify(data.steps);
  const streaming = data.streaming_text || "";
  if (stepsKey !== view.lastSteps || streaming !== view.lastStreaming) {
    view.lastSteps = stepsKey;
    view.lastStreaming = streaming;
    view.stepsEl.innerHTML = "";
    data.steps.forEach((step, i) => view.stepsEl.appendChild(buildAgentStep(step, view, i)));
    if (streaming) {
      // texto que el agente esta escribiendo ahora mismo (solo en directo)
      const live = document.createElement("div");
      live.className = "agent-streaming";
      renderMarkdownInto(live, streaming, { highlight: false });
      view.stepsEl.appendChild(live);
    }
  }

  if (data.task) view.task = data.task;
  if (!busy && data.steps.length && view.task && !view.saveScEl) {
    view.saveScEl = document.createElement("button");
    view.saveScEl.className = "agent-save-sc";
    view.saveScEl.textContent = "☆ Guardar como atajo";
    view.saveScEl.title = "Para repetir esta tarea con un clic desde el modo Agente";
    view.saveScEl.addEventListener("click", async () => {
      if (!agentShortcuts.length) await loadAgentShortcuts();
      openShortcutsModal({ name: "", task: view.task, potente: false });
    });
    view.card.querySelector(".agent-credit").before(view.saveScEl);
  }

  renderAgentChanges(view, data, busy);

  // continuar la tarea: solo cuando el agente ha terminado y no espera nada
  const canFollow = !busy && !data.questions.length && !data.permissions.length && data.steps.length > view.stepsAtSend;
  if (canFollow && view.followEl.style.display === "none") {
    view.followEl.style.display = "flex";
  } else if (!canFollow) {
    view.followEl.style.display = "none";
  }
  if (view.doItBtn) view.doItBtn.style.display = view.codeMode === "plan" ? "inline-block" : "none";

  // las tarjetas de pregunta/permiso se crean una sola vez por id (no en cada
  // sondeo) para no borrar lo que el usuario este escribiendo
  const liveIds = new Set([...data.questions.map((q) => q.id), ...data.permissions.map((p) => p.id)]);
  for (const id of Object.keys(view.askCards)) {
    if (!liveIds.has(id)) { view.askCards[id].remove(); delete view.askCards[id]; }
  }
  for (const q of data.questions) {
    if (!view.askCards[q.id]) view.askCards[q.id] = buildQuestionCard(view, q);
  }
  for (const p of data.permissions) {
    if (!view.askCards[p.id]) view.askCards[p.id] = buildPermissionCard(view, p);
  }
  log.scrollTop = log.scrollHeight;
}

// Terminal bajo cada comando: abierta mientras corre, plegada al acabar. Si el
// usuario la abre o cierra a mano se respeta (los pasos se repintan enteros en
// cada actualizacion del directo).
function buildAgentTerminal(step, view, idx) {
  const running = step.status === "running" || step.status === "pending";
  if (!step.output && !running) return null;
  const box = document.createElement("details");
  box.className = "agent-term";
  view.termOpen = view.termOpen || {};
  box.open = idx in view.termOpen ? view.termOpen[idx] : running;
  const summary = document.createElement("summary");
  if (running) summary.textContent = "Salida del comando (en directo)";
  else {
    summary.textContent = "Ver salida";
    if (step.exit !== null && step.exit !== undefined && step.exit !== 0) {
      const bad = document.createElement("span");
      bad.className = "exit-bad";
      bad.textContent = ` (terminó con código ${step.exit})`;
      summary.appendChild(bad);
    }
  }
  const pre = document.createElement("pre");
  pre.textContent = step.output || "(sin salida todavía)";
  box.append(summary, pre);
  summary.addEventListener("click", () => { view.termOpen[idx] = !box.open; });
  // pegado al final como una terminal, salvo que el usuario haya subido a leer
  const key = "term" + idx;
  pre.addEventListener("scroll", () => {
    view[key] = pre.scrollTop + pre.clientHeight < pre.scrollHeight - 8;
  });
  requestAnimationFrame(() => { if (!view[key]) pre.scrollTop = pre.scrollHeight; });
  return box;
}

// --- Ver cambios / Deshacer (n.º 6 del roadmap) ---
// OpenCode solo registra lo que la tarea cambia dentro de su carpeta de
// trabajo (Documentos/Chati); lo de otras carpetas no se puede deshacer.
const AGENT_CHANGE_TOOLS = new Set(["edit", "write", "patch", "bash"]);

function renderAgentChanges(view, data, busy) {
  const el = view.changesEl;
  const ch = data.changes || { files: 0, additions: 0, deletions: 0 };
  const touched = data.steps.some((s) => s.kind === "tool" && AGENT_CHANGE_TOOLS.has(s.tool) && s.status === "completed");
  const show = !busy && data.undoable && (ch.files > 0 || data.reverted || touched);
  const key = JSON.stringify([show, ch, data.reverted]);
  if (key === view.changesKey) return;
  view.changesKey = key;
  el.style.display = show ? "block" : "none";
  el.innerHTML = "";
  if (!show) return;

  // modo Codigo: la carpeta es el proyecto; modo Agente: Documentos/Chati
  const folder = (data.directory || "").split(/[\\/]/).filter(Boolean).pop() || "";
  const where = view.codeMode ? `el proyecto «${folder}»` : "su carpeta de trabajo (Documentos/Chati)";
  const whereShort = view.codeMode ? `el proyecto «${folder}»` : "su carpeta de trabajo";
  const bar = document.createElement("div");
  bar.className = "bar";
  const note = document.createElement("div");
  note.className = "note";
  const button = (label, onClick) => {
    const b = document.createElement("button");
    b.textContent = label;
    b.addEventListener("click", () => onClick(b));
    return b;
  };
  const act = async (b, path, confirmText) => {
    if (confirmText && !confirm(confirmText)) return;
    b.disabled = true;
    try {
      await agentPost(`/agent/tasks/${view.sessionId}/${path}`);
    } catch (err) {
      alert("No se pudo: " + (err.message || err));
      b.disabled = false;
      return;
    }
    view.changesKey = null;
    followAgentView(view);
  };

  if (data.reverted) {
    const msg = document.createElement("span");
    msg.textContent = "↩ Has deshecho los cambios de esta tarea.";
    bar.append(msg, button("Rehacer", (b) => act(b, "unrevert")));
    note.textContent = "Si continúas la tarea, lo deshecho ya no se podrá recuperar.";
  } else if (ch.files > 0) {
    const msg = document.createElement("span");
    // numeros a la fuerza: vienen de la API de OpenCode y van dentro de innerHTML
    const n = ch.files === 1 ? "1 archivo" : `${Number(ch.files) || 0} archivos`;
    msg.innerHTML = `📝 Ha cambiado <b>${n}</b> en <span class="where"></span> ` +
      `<span class="plus">+${Number(ch.additions) || 0}</span> <span class="minus">−${Number(ch.deletions) || 0}</span>`;
    msg.querySelector(".where").textContent = whereShort;
    const list = document.createElement("div");
    list.className = "diff-list";
    list.style.display = "none";
    const seeBtn = button("Ver cambios", async (b) => {
      if (list.style.display !== "none") { list.style.display = "none"; b.textContent = "Ver cambios"; return; }
      b.disabled = true;
      try {
        const resp = await fetch(`/agent/tasks/${view.sessionId}/changes`);
        const info = await resp.json();
        if (!resp.ok) throw new Error(info.detail || resp.status);
        renderAgentDiffs(list, info);
      } catch (err) {
        alert("No se pudieron cargar los cambios: " + (err.message || err));
        b.disabled = false;
        return;
      }
      b.disabled = false;
      list.style.display = "flex";
      b.textContent = "Ocultar cambios";
    });
    bar.append(msg, seeBtn, button("Deshacer", (b) => act(b, "revert",
      `¿Deshacer lo que ha hecho esta tarea en ${where}?\n\n` +
      "Los archivos vuelven a como estaban antes. Podrás rehacerlo mientras no continúes la tarea.")));
    note.textContent = `Solo se puede deshacer lo hecho dentro de ${where}; lo de otras carpetas no.`;
    el.append(bar, note, list);
    return;
  } else {
    note.textContent = `No ha cambiado nada en ${where}: ` +
      "lo que haya hecho en otras carpetas no se puede deshacer desde aquí.";
  }
  el.append(bar, note);
}

function renderAgentDiffs(list, info) {
  list.innerHTML = "";
  const turns = info.turns.filter((t) => t.files.length);
  const STATUS = { added: "nuevo", deleted: "borrado", modified: "modificado" };
  for (const turn of turns) {
    if (turns.length > 1) {
      const t = document.createElement("div");
      t.className = "turn";
      t.textContent = "«" + turn.text + "»";
      list.appendChild(t);
    }
    for (const f of turn.files) {
      const box = document.createElement("div");
      const head = document.createElement("div");
      head.className = "file-head";
      head.innerHTML = "";
      head.append(`${f.file} · ${STATUS[f.status] || f.status} `);
      const plus = document.createElement("span");
      plus.className = "plus";
      plus.textContent = `+${f.additions} `;
      const minus = document.createElement("span");
      minus.className = "minus";
      minus.textContent = `−${f.deletions}`;
      head.append(plus, minus);
      const pre = document.createElement("pre");
      // se quitan las cabeceras del parche (Index:, ===, ---, +++): solo lineas
      for (const line of (f.patch || "").split("\n")) {
        if (/^(Index:|={5,}|--- |\+\+\+ )/.test(line) || line === "") continue;
        const div = document.createElement("div");
        div.textContent = line.replace(/\r$/, "");
        if (line.startsWith("@@")) div.className = "hunk";
        else if (line.startsWith("+")) div.className = "add";
        else if (line.startsWith("-")) div.className = "del";
        pre.appendChild(div);
      }
      box.append(head, pre);
      list.appendChild(box);
    }
  }
  if (!list.children.length) list.textContent = "No hay cambios que mostrar.";
}

function buildAgentStep(step, view, idx) {
  if (step.kind === "user") {
    const div = document.createElement("div");
    div.className = "agent-user-step";
    div.textContent = step.text;
    return div;
  }
  if (step.kind === "text") {
    const div = document.createElement("div");
    renderMarkdownInto(div, step.text, { highlight: false });
    return div;
  }
  const row = document.createElement("div");
  row.className = "agent-tool" + (step.status === "error" ? " error" : "");
  const ic = document.createElement("span");
  ic.className = "ic";
  ic.textContent = AGENT_STATUS_ICON[step.status] || "•";
  const label = document.createElement("span");
  label.textContent = AGENT_TOOL_LABELS[step.tool] || step.tool;
  row.append(ic, label);
  const detail = step.detail || step.title;
  if (detail) {
    const code = document.createElement("code");
    code.textContent = detail;
    row.appendChild(code);
  }
  if (step.error) {
    const err = document.createElement("span");
    err.textContent = "- " + step.error;
    row.appendChild(err);
  }
  const term = step.tool === "bash" && view ? buildAgentTerminal(step, view, idx) : null;
  if (!term) return row;
  const wrap = document.createElement("div");
  wrap.append(row, term);
  return wrap;
}

function setAskDisabled(box, disabled) {
  box.querySelectorAll("button, input").forEach((el) => { el.disabled = disabled; });
}

function buildQuestionCard(view, request) {
  const box = document.createElement("div");
  box.className = "agent-ask";
  const answers = request.questions.map(() => []);
  const readyToSend = () => answers.every((a) => a.length > 0);

  const submit = async (rejected) => {
    setAskDisabled(box, true);
    try {
      if (rejected) await agentPost(`/agent/questions/${request.id}/reject`);
      else await agentPost(`/agent/questions/${request.id}`, { answers });
    } catch (err) {
      alert("No se pudo enviar la respuesta: " + (err.message || err));
      setAskDisabled(box, false);
      return;
    }
    followAgentView(view);
  };

  request.questions.forEach((q, idx) => {
    const qEl = document.createElement("div");
    qEl.className = "q";
    qEl.textContent = q.question;
    box.appendChild(qEl);
    const opts = document.createElement("div");
    opts.className = "opts";
    for (const opt of q.options || []) {
      const b = document.createElement("button");
      b.textContent = opt.label;
      if (opt.description) b.title = opt.description;
      b.addEventListener("click", () => {
        if (q.multiple) {
          const i = answers[idx].indexOf(opt.label);
          if (i >= 0) answers[idx].splice(i, 1); else answers[idx].push(opt.label);
          b.classList.toggle("primary", i < 0);
          return;
        }
        answers[idx] = [opt.label];
        opts.querySelectorAll("button").forEach((x) => x.classList.remove("primary"));
        b.classList.add("primary");
        if (request.questions.length === 1) submit(false);
      });
      opts.appendChild(b);
    }
    box.appendChild(opts);
    const row = document.createElement("div");
    row.className = "custom-row";
    const input = document.createElement("input");
    input.placeholder = (q.options || []).length ? "...o escribe otra respuesta" : "Escribe tu respuesta";
    input.addEventListener("input", () => { answers[idx] = input.value.trim() ? [input.value.trim()] : []; });
    input.addEventListener("keydown", (e) => { if (e.key === "Enter" && readyToSend()) submit(false); });
    row.appendChild(input);
    box.appendChild(row);
  });

  const actions = document.createElement("div");
  actions.className = "opts";
  const send = document.createElement("button");
  send.className = "primary";
  send.textContent = "Responder";
  send.addEventListener("click", () => { if (readyToSend()) submit(false); });
  const skip = document.createElement("button");
  skip.className = "danger";
  skip.textContent = "No responder";
  skip.addEventListener("click", () => submit(true));
  actions.append(send, skip);
  box.appendChild(actions);

  view.asksEl.appendChild(box);
  return box;
}

function buildPermissionCard(view, perm) {
  const box = document.createElement("div");
  box.className = "agent-ask";
  const q = document.createElement("div");
  q.className = "q";
  q.textContent = "El agente quiere " + (AGENT_PERMISSION_LABELS[perm.permission] || perm.permission) + ":";
  box.appendChild(q);
  for (const pattern of perm.patterns || []) {
    const code = document.createElement("code");
    code.className = "pattern";
    code.textContent = pattern;
    box.appendChild(code);
  }
  const opts = document.createElement("div");
  opts.className = "opts";
  const choices = [["once", "Permitir", "primary"], ["always", "Permitir siempre", ""], ["reject", "Rechazar", "danger"]];
  for (const [reply, label, cls] of choices) {
    const b = document.createElement("button");
    b.textContent = label;
    if (cls) b.className = cls;
    if (reply === "always") b.title = "No volvera a preguntar por esto mismo mientras el agente siga arrancado";
    b.addEventListener("click", async () => {
      setAskDisabled(box, true);
      try {
        await agentPost(`/agent/permissions/${perm.id}`, { reply });
      } catch (err) {
        alert("No se pudo enviar la respuesta: " + (err.message || err));
        setAskDisabled(box, false);
        return;
      }
      followAgentView(view);
    });
    opts.appendChild(b);
  }
  box.appendChild(opts);
  view.asksEl.appendChild(box);
  return box;
}

async function streamChatInto(bubble, endpoint, text, agentOverride, imageBase64, signal) {
  const body = { message: text, agent: agentOverride || null, session_id: getSessionId(), verify: verifyToggle.checked };
  if (MODES[currentMode].isText && profileSelect.value) body.model_profile = profileSelect.value;
  if (imageBase64) body.image_base64 = imageBase64;
  if (currentMode === "image" && modelSelect.value) body.image_model = modelSelect.value;
  if (currentMode === "video" && modelSelect.value) body.video_model = modelSelect.value;

  const resp = await fetch(endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!resp.ok || !resp.body) {
    bubble.className = "bubble error";
    bubble.textContent = "Error de conexion (" + resp.status + ")";
    return;
  }

  // OJO: el fetch() se resuelve en cuanto llegan las cabeceras HTTP, mucho
  // antes de que el servidor mande el primer trozo de texto de verdad (antes
  // tiene que pasar el router, y a veces una ronda de decision de
  // herramientas) - si se borra aqui el indicador de "escribiendo", el
  // cuadro se queda vacio y sin animacion durante ese hueco, pareciendo
  // colgado. Se deja el indicador tal cual hasta que llegue contenido real.
  let textNode = null;
  let liveText = "";
  let finalData = null;
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop();
    for (const line of lines) {
      if (!line.trim()) continue;
      let evt;
      try { evt = JSON.parse(line); } catch (e) { continue; }
      if (evt.type === "status") {
        // aviso mientras no hay texto todavia (p.ej. "Cargando el modelo...")
        const wrap = bubble.querySelector(".typing-wrap");
        if (wrap) {
          let st = bubble.querySelector(".typing-status");
          if (!st) {
            st = document.createElement("div");
            st.className = "typing-status";
            wrap.after(st);
          }
          st.textContent = evt.text;
        }
      } else if (evt.type === "start") {
        setSessionId(evt.session_id);
        const tagWrap = bubble.parentElement.querySelector(".tag");
        if (tagWrap) tagWrap.textContent = evt.agent_used;
        if (["image", "video", "image_edit"].includes(evt.agent_used)) {
          cancelBtn.style.display = "inline-block";
          currentAbortIsMedia = true;
        }
      } else if (evt.type === "chunk") {
        if (!textNode) {
          bubble.innerHTML = "";
          textNode = document.createElement("div");
          bubble.appendChild(textNode);
        }
        liveText += evt.text;
        renderMarkdownInto(textNode, liveText, { highlight: false });
        log.scrollTop = log.scrollHeight;
      } else if (evt.type === "done") {
        finalData = evt;
      }
    }
  }

  if (!textNode) {
    bubble.innerHTML = "";
    textNode = document.createElement("div");
    bubble.appendChild(textNode);
  }

  if (finalData) {
    setSessionId(finalData.session_id);
    refreshPlan();
    refreshModelStatus();
    const shown = (!liveText || finalData.corrected) ? finalData.response : liveText;
    renderMarkdownInto(textNode, shown, { highlight: true });
    if (finalData.verifier_gated) bubble.classList.add("gated");
    if (finalData.verifier_reason) {
      const reasonNode = document.createElement("div");
      reasonNode.style.fontSize = "12px";
      reasonNode.style.color = "var(--warn)";
      reasonNode.style.marginTop = "6px";
      reasonNode.textContent = "Verificador: " + finalData.verifier_reason;
      bubble.appendChild(reasonNode);
    }
    if (finalData.file_url) {
      appendMediaWithActions(bubble, finalData.file_url, finalData.file_path);
    } else if (isRetryable(finalData.response)) {
      appendRetry(bubble);
    }
    // el chat detecto una accion sobre el equipo y se la paso al agente
    if (finalData.agent_task_id) openAgentView(finalData.agent_task_id, Date.now());
    if (finalData.agent_choice) renderAgentChoice(finalData.agent_choice.task, finalData.agent_choice.reason);
  }
}

function renderResult(bubble, data) {
  setSessionId(data.session_id);
  refreshPlan();
  bubble.innerHTML = "";
  const tagWrap = bubble.parentElement.querySelector(".tag");
  if (tagWrap) tagWrap.textContent = data.agent_used;

  if (data.verifier_gated) bubble.classList.add("gated");

  const textNode = document.createElement("div");
  renderMarkdownInto(textNode, data.response, { highlight: true });
  bubble.appendChild(textNode);

  if (data.verifier_reason) {
    const reasonNode = document.createElement("div");
    reasonNode.style.fontSize = "12px";
    reasonNode.style.color = "var(--warn)";
    reasonNode.style.marginTop = "6px";
    reasonNode.textContent = "Verificador: " + data.verifier_reason;
    bubble.appendChild(reasonNode);
  }

  if (data.file_url) {
    appendMediaWithActions(bubble, data.file_url, data.file_path);
    if (data.agent_used === "image_edit") appendEditShortcuts(bubble);
  } else if (isRetryable(data.response)) {
    appendRetry(bubble);
  }
}

// Una imagen, un video o una edicion que no ha salido (cancelada, tiempo
// agotado, fallo): se avisa y se puede repetir lo mismo con un boton, sin
// volver a escribirlo ni a adjuntar la foto (Sergio, 2026-10-07). Los textos
// salen de _generation_error_message en main.py.
const RETRYABLE = /^(Se ha cancelado la generación|No ha terminado a tiempo|No se ha podido terminar)/;

function isRetryable(text) {
  return RETRYABLE.test((text || "").trim());
}

function appendRetry(bubble) {
  const ctx = bubble._retry;
  if (!ctx || bubble.querySelector(".retry-actions")) return;
  const row = document.createElement("div");
  row.className = "media-actions retry-actions";
  const btn = document.createElement("button");
  btn.textContent = "↻ Reintentar";
  btn.title = `Volver a pedir "${ctx.text}"`;
  btn.addEventListener("click", () => {
    if (sendBtn.disabled) return;  // hay algo en marcha
    send(ctx);
  });
  row.appendChild(btn);
  bubble.appendChild(row);
}

// Atajos bajo una foto editada: lo mismo que escribir "deshaz eso" u "otra
// vez" (Sergio edita fotos a menudo y prueba varias versiones)
const EDIT_SHORTCUTS = [
  ["↶ Deshacer", "deshaz eso"],
  ["↻ Otra versión", "otra vez"],
  ["Volver a la original", "vuelve a la original"],
];

function appendEditShortcuts(bubble) {
  const row = document.createElement("div");
  row.className = "media-actions edit-shortcuts";
  for (const [label, text] of EDIT_SHORTCUTS) {
    const btn = document.createElement("button");
    btn.textContent = label;
    btn.title = `Igual que escribir "${text}"`;
    btn.addEventListener("click", () => {
      if (sendBtn.disabled) return;  // hay algo en marcha
      promptEl.value = text;
      send();
    });
    row.appendChild(btn);
  }
  bubble.appendChild(row);
}

function downloadLink(fileUrl) {
  // mismo origen: el atributo download guarda el archivo en vez de abrirlo
  const a = document.createElement("a");
  a.href = fileUrl;
  a.download = fileUrl.split("?")[0].split("/").pop() || "chati";
  a.textContent = "⬇ Descargar";
  a.title = "Guardar en tu ordenador";
  return a;
}

function appendMediaWithActions(bubble, fileUrl, filePath) {
  const actionsRow = document.createElement("div");
  actionsRow.className = "media-actions";
  actionsRow.appendChild(downloadLink(fileUrl));

  if (fileUrl.endsWith(".mp4")) {
    const vid = document.createElement("video");
    vid.src = fileUrl;
    vid.controls = true;
    vid.autoplay = true;
    vid.loop = true;
    bubble.append(vid, actionsRow);
    return;
  }

  const img = document.createElement("img");
  img.className = "attach";
  img.src = fileUrl;
  bubble.append(img, actionsRow);

  if (!filePath) return;  // sin ruta del servidor no se puede escalar ni editar

  const upscaleBtn = document.createElement("button");
  upscaleBtn.textContent = "Escalar x4";
  upscaleBtn.addEventListener("click", async () => {
    upscaleBtn.disabled = true;
    upscaleBtn.textContent = "Escalando...";
    const form = new FormData();
    form.append("file_path", filePath);
    try {
      const resp = await fetch("/image/upscale", { method: "POST", body: form });
      const data = await resp.json();
      if (data.file_url) {
        const resultBubble = addMessage("assistant", "image_upscale");
        resultBubble.textContent = data.response;
        appendMediaWithActions(resultBubble, data.file_url, data.file_path);
      } else {
        alert(data.response || "Fallo al escalar.");
      }
    } finally {
      upscaleBtn.disabled = false;
      upscaleBtn.textContent = "Escalar x4";
    }
  });

  const editBtn = document.createElement("button");
  editBtn.textContent = "Editar (repintar zona)";
  editBtn.addEventListener("click", () => openInpaintEditor(fileUrl, filePath));

  actionsRow.append(upscaleBtn, editBtn);
}

// --- Sidebar: conversaciones (siempre visibles, tipo ChatGPT) ---
const newChatBtn = document.getElementById("newChatBtn");
const convList = document.getElementById("convList");

function formatRelativeTime(iso) {
  if (!iso) return "";
  // SQLite guarda CURRENT_TIMESTAMP en UTC sin zona ("2026-09-25 11:37:47") -
  // sin la Z el navegador lo toma como hora local y todo sale "hace 2 h"
  let normalized = iso.includes("T") ? iso : iso.replace(" ", "T");
  if (!/[zZ]|[+-]\d\d:?\d\d$/.test(normalized)) normalized += "Z";
  const then = new Date(normalized);
  if (isNaN(then)) return iso;
  const diffMin = Math.round((Date.now() - then.getTime()) / 60000);
  if (diffMin < 1) return "ahora mismo";
  if (diffMin < 60) return `hace ${diffMin} min`;
  const diffH = Math.round(diffMin / 60);
  if (diffH < 24) return `hace ${diffH} h`;
  const diffD = Math.round(diffH / 24);
  if (diffD < 7) return `hace ${diffD} d`;
  return then.toLocaleDateString();
}

async function loadConversations() {
  if (getSessionRole() === "guest") return;  // invitado: sin historial
  let sessions;
  try {
    const resp = await fetch("/sessions");
    sessions = await resp.json();
  } catch (err) { return; }

  convList.innerHTML = "";
  if (!sessions.length) {
    convList.innerHTML = '<div id="convEmpty">Sin conversaciones todavia. Escribe algo para empezar una.</div>';
    return;
  }
  const currentSid = getSessionId();
  sessions.forEach((s) => {
    const row = document.createElement("div");
    row.className = "conv-row" + (s.session_id === currentSid ? " current" : "");
    const defaultTitle = `Conversacion · ${s.messages} mensaje${s.messages === 1 ? "" : "s"}`;
    const titleEl = document.createElement("div");
    titleEl.className = "conv-title";
    titleEl.textContent = s.title || defaultTitle;
    const metaEl = document.createElement("div");
    metaEl.className = "conv-meta";
    metaEl.textContent = formatRelativeTime(s.last_used);
    const renameBtn = document.createElement("button");
    renameBtn.className = "conv-rename";
    renameBtn.title = "Renombrar";
    renameBtn.textContent = "✎";
    const delBtn = document.createElement("button");
    delBtn.className = "conv-del";
    delBtn.title = "Borrar conversacion";
    delBtn.textContent = "×";
    row.appendChild(titleEl);
    row.appendChild(metaEl);
    row.appendChild(renameBtn);
    row.appendChild(delBtn);

    row.addEventListener("click", (e) => {
      if (e.target.closest(".conv-del") || e.target.closest(".conv-rename")) return;
      loadConversationIntoLog(s.session_id);
    });

    renameBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      const input = document.createElement("input");
      input.type = "text";
      input.value = s.title || "";
      input.placeholder = defaultTitle;
      input.maxLength = 80;
      input.className = "conv-rename-input";
      row.replaceChild(input, titleEl);
      input.focus();
      input.select();

      const finishRename = async (save) => {
        const newTitle = input.value.trim();
        if (save && newTitle && newTitle !== s.title) {
          const form = new FormData();
          form.append("title", newTitle);
          await fetch(`/sessions/${s.session_id}/title`, { method: "PUT", body: form });
        }
        await loadConversations();
      };
      input.addEventListener("keydown", (ke) => {
        if (ke.key === "Enter") finishRename(true);
        if (ke.key === "Escape") finishRename(false);
      });
      input.addEventListener("blur", () => finishRename(true));
    });

    delBtn.addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!(await askDeleteConversation(s.session_id))) return;
      await fetch(`/sessions/${s.session_id}`, { method: "DELETE" });
      if (s.session_id === currentSid) {
        localStorage.removeItem("ia_session_id");
        startNewConversation(false);
      }
      await loadConversations();
    });
    convList.appendChild(row);
  });
}

// Al borrar una conversacion con documentos adjuntos, el usuario decide si
// pasan a "Mis documentos" (memoria permanente) o se borran con ella.
// Devuelve true si hay que seguir con el borrado.
async function askDeleteConversation(sessionId) {
  let docs = [];
  try {
    const resp = await fetch(`/sessions/${sessionId}/docs`);
    if (resp.ok) docs = await resp.json();
  } catch (e) { /* sin adjuntos */ }
  const modal = document.getElementById("deleteConvModal");
  const keepBtn = document.getElementById("deleteConvKeep");
  const list = document.getElementById("deleteConvDocs");
  list.innerHTML = "";
  if (docs.length) {
    document.getElementById("deleteConvText").textContent =
      `Esta conversacion tiene ${docs.length === 1 ? "un documento adjunto" : docs.length + " documentos adjuntos"}. ` +
      (docs.length === 1
        ? "Puedes guardarlo en Mis documentos para que Chati lo siga teniendo en cuenta en cualquier conversacion, o borrarlo junto con ella."
        : "Puedes guardarlos en Mis documentos para que Chati los siga teniendo en cuenta en cualquier conversacion, o borrarlos junto con ella.");
    for (const d of docs) {
      const li = document.createElement("li");
      li.textContent = d.name;
      list.appendChild(li);
    }
  } else {
    document.getElementById("deleteConvText").textContent = "¿Borrar toda esta conversacion? No se puede deshacer.";
  }
  keepBtn.style.display = docs.length ? "inline-block" : "none";
  keepBtn.textContent = docs.length === 1 ? "Guardar documento y borrar" : "Guardar documentos y borrar";
  document.getElementById("deleteConvAll").textContent = docs.length ? "Borrar todo" : "Borrar";
  modal.style.display = "block";

  const choice = await new Promise((resolve) => {
    const done = (value) => {
      modal.style.display = "none";
      ["deleteConvCancel", "deleteConvAll", "deleteConvKeep"].forEach((id) => { document.getElementById(id).onclick = null; });
      modal.onclick = null;
      resolve(value);
    };
    document.getElementById("deleteConvCancel").onclick = () => done("cancel");
    document.getElementById("deleteConvAll").onclick = () => done("all");
    keepBtn.onclick = () => done("keep");
    modal.onclick = (e) => { if (e.target === modal) done("cancel"); };
  });
  if (choice === "cancel") return false;
  if (choice === "keep") {
    for (const d of docs) {
      const resp = await fetch(`/sessions/${sessionId}/docs/${encodeURIComponent(d.name)}/keep`, { method: "POST" });
      if (!resp.ok) {
        alert(`No se pudo guardar "${d.name}" en Mis documentos - no se borra la conversacion.`);
        return false;
      }
    }
  }
  return true;
}

function startNewConversation(clearStorage = true) {
  if (clearStorage) localStorage.removeItem("ia_session_id");
  log.innerHTML = "";
  log.appendChild(emptyState);
  emptyState.style.display = "block";
  planPanel.style.display = "none";
  document.querySelectorAll(".conv-row").forEach(r => r.classList.remove("current"));
  refreshDocChips();
}
newChatBtn.addEventListener("click", () => startNewConversation());

// --- Opciones navegable: flecha para volver al menu desde cada ventana ---
["profileModal", "adminModal", "knowledgeModal", "metricsModal", "modelUpdatesModal", "memoryModal", "personasModal"]
  .forEach((id) => {
    const modal = document.getElementById(id);
    const header = modal.querySelector(".modal-header");
    const back = document.createElement("button");
    back.className = "modal-back";
    back.textContent = "←";
    back.title = "Volver a Opciones";
    back.addEventListener("click", () => {
      modal.querySelector(".modal-close").click();  // su propio cierre (p.ej. para temporizadores)
      document.getElementById("optionsModal").style.display = "block";
    });
    header.insertBefore(back, header.firstChild);
  });

const ATTACHED_DOC_PREFIX = "📄 Documento adjuntado: ";

function addLoadedMessage(role, content, agent, messageId, sessionId, media) {
  if (agent === "adjunto" && content.startsWith(ATTACHED_DOC_PREFIX)) {
    addDocCard(content.slice(ATTACHED_DOC_PREFIX.length));
    return;
  }
  const wrap = document.createElement("div");
  wrap.className = "msg " + role;
  const roleEl = document.createElement("div");
  roleEl.className = "role";
  roleEl.textContent = role === "user" ? "Tu" : ("Chati" + (agent ? ` (${agent})` : ""));

  const actions = document.createElement("span");
  actions.className = "msg-actions";
  const editBtn = document.createElement("button");
  editBtn.textContent = "✎";
  editBtn.title = "Editar";
  const delBtn = document.createElement("button");
  delBtn.textContent = "🗑";
  delBtn.title = "Borrar mensaje";
  actions.appendChild(editBtn);
  actions.appendChild(delBtn);
  roleEl.appendChild(actions);

  const bubble = document.createElement("div");
  bubble.className = "bubble";
  if (role === "assistant") {
    renderMarkdownInto(bubble, content, { highlight: true });
  } else {
    bubble.textContent = content;
  }
  // la foto subida o la imagen resultante de ese mensaje (se guardan 30 dias)
  if (media && /^[0-9a-f]{32}\.(png|jpg|webp|mp4)$/.test(media)) {
    appendMediaWithActions(bubble, "/media/" + media, media);
  }

  wrap.appendChild(roleEl);
  wrap.appendChild(bubble);
  log.appendChild(wrap);

  editBtn.addEventListener("click", () => {
    const textarea = document.createElement("textarea");
    textarea.value = content;
    textarea.style.cssText = "width:100%; min-height:60px; background:var(--panel-3); color:var(--text); border:1px solid var(--border); border-radius:6px; padding:8px; font-family:inherit; font-size:13.5px; resize:vertical;";
    const saveRow = document.createElement("div");
    saveRow.style.cssText = "margin-top:6px; display:flex; gap:6px;";
    const saveBtn = document.createElement("button");
    saveBtn.textContent = "Guardar";
    saveBtn.style.cssText = "font-size:12px; padding:4px 10px; background:var(--accent); color:white; border:none; border-radius:6px; cursor:pointer;";
    const cancelEditBtn = document.createElement("button");
    cancelEditBtn.textContent = "Cancelar";
    cancelEditBtn.style.cssText = "font-size:12px; padding:4px 10px; background:var(--panel-2); color:var(--text); border:1px solid var(--border); border-radius:6px; cursor:pointer;";
    saveRow.appendChild(saveBtn);
    saveRow.appendChild(cancelEditBtn);
    bubble.innerHTML = "";
    bubble.appendChild(textarea);
    bubble.appendChild(saveRow);
    textarea.focus();

    cancelEditBtn.addEventListener("click", () => loadConversationIntoLog(sessionId));
    saveBtn.addEventListener("click", async () => {
      const form = new FormData();
      form.append("content", textarea.value);
      await fetch(`/sessions/${sessionId}/messages/${messageId}`, { method: "PUT", body: form });
      await loadConversationIntoLog(sessionId);
    });
  });

  delBtn.addEventListener("click", async () => {
    if (!confirm("¿Borrar este mensaje?")) return;
    await fetch(`/sessions/${sessionId}/messages/${messageId}`, { method: "DELETE" });
    wrap.remove();
  });

  return bubble;
}

async function loadConversationIntoLog(sessionId) {
  let messages;
  try {
    const resp = await fetch(`/sessions/${sessionId}`);
    // 404: ya no existe o es de otro usuario (id recordado en este navegador)
    if (!resp.ok) { startNewConversation(); return; }
    messages = await resp.json();
  } catch (err) { return; }

  setSessionId(sessionId);
  localStorage.setItem("ia_session_id", sessionId);
  refreshDocChips();
  log.innerHTML = "";
  messages.forEach(m => addLoadedMessage(m.role, m.content, m.agent, m.id, sessionId, m.media));
  log.scrollTop = log.scrollHeight;
  planPanel.style.display = "none";
  document.querySelectorAll(".conv-row").forEach(r => r.classList.remove("current"));
  document.querySelectorAll(".conv-row").forEach(r => {
    if (r.querySelector(".conv-title") && r.contains(document.activeElement)) return;
  });
  await loadConversations();
}

const knowledgeBtn = document.getElementById("knowledgeBtn");
const knowledgeModal = document.getElementById("knowledgeModal");
const closeKnowledgeBtn = document.getElementById("closeKnowledgeBtn");
const knowledgeFileInput = document.getElementById("knowledgeFileInput");
const knowledgeListBox = document.getElementById("knowledgeListBox");

knowledgeBtn.addEventListener("click", async () => {
  optionsModal.style.display = "none";
  knowledgeModal.style.display = "block";
  await loadKnowledgeList();
});
closeKnowledgeBtn.addEventListener("click", () => { knowledgeModal.style.display = "none"; });
knowledgeModal.addEventListener("click", (e) => { if (e.target === knowledgeModal) knowledgeModal.style.display = "none"; });

async function loadKnowledgeList() {
  const resp = await fetch("/knowledge");
  const docs = await resp.json();
  knowledgeListBox.innerHTML = "";
  if (!docs.length) {
    knowledgeListBox.innerHTML = '<div style="color:var(--muted); font-size:12px;">No hay documentos todavia.</div>';
    return;
  }
  docs.forEach(d => {
    const row = document.createElement("div");
    row.style.cssText = "display:flex; justify-content:space-between; align-items:center; padding:8px 0; border-bottom:1px solid var(--border); font-size:13px;";
    row.innerHTML = `<div>${esc(d.filename)} <span style="color:var(--muted); font-size:11px;">(${esc(d.chunks)} fragmentos)</span></div>`;
    const delBtn = document.createElement("button");
    delBtn.textContent = "Borrar";
    delBtn.style.cssText = "font-size:12px; padding:4px 10px; background:var(--panel-2); color:var(--text); border:1px solid var(--border); border-radius:6px; cursor:pointer;";
    delBtn.addEventListener("click", async () => {
      await fetch(`/knowledge/${encodeURIComponent(d.filename)}`, { method: "DELETE" });
      await loadKnowledgeList();
    });
    row.appendChild(delBtn);
    knowledgeListBox.appendChild(row);
  });
}

knowledgeFileInput.addEventListener("change", async () => {
  if (!knowledgeFileInput.files[0]) return;
  const form = new FormData();
  form.append("file", knowledgeFileInput.files[0]);
  knowledgeListBox.innerHTML = '<div style="color:var(--muted); font-size:12px;"><span class="spinner"></span> procesando documento...</div>';
  try {
    await fetch("/knowledge", { method: "POST", body: form });
  } catch (err) { /* se refleja al recargar la lista */ }
  knowledgeFileInput.value = "";
  await loadKnowledgeList();
});

// --- Opciones (menu que agrupa Metricas/Modelos/Codigo/Perfil) ---
const optionsBtn = document.getElementById("optionsBtn");
const optionsModal = document.getElementById("optionsModal");
const closeOptionsBtn = document.getElementById("closeOptionsBtn");

optionsBtn.addEventListener("click", () => { optionsModal.style.display = "block"; });
closeOptionsBtn.addEventListener("click", () => { optionsModal.style.display = "none"; });
optionsModal.addEventListener("click", (e) => { if (e.target === optionsModal) optionsModal.style.display = "none"; });

// --- Memoria (contexto manual + lo que ya se indexa solo de las conversaciones) ---
const optMemoryBtn = document.getElementById("optMemoryBtn");
const memoryModal = document.getElementById("memoryModal");
const closeMemoryBtn = document.getElementById("closeMemoryBtn");
const memoryListBox = document.getElementById("memoryListBox");
const memoryNoteInput = document.getElementById("memoryNoteInput");
const addMemoryNoteBtn = document.getElementById("addMemoryNoteBtn");

const MEMORY_TYPE_LABEL = { note: "Nota tuya", conversation: "De una conversacion" };

optMemoryBtn.addEventListener("click", async () => {
  optionsModal.style.display = "none";
  memoryModal.style.display = "block";
  await loadMemoryEntries();
});
closeMemoryBtn.addEventListener("click", () => { memoryModal.style.display = "none"; });
memoryModal.addEventListener("click", (e) => { if (e.target === memoryModal) memoryModal.style.display = "none"; });

async function loadMemoryEntries() {
  memoryListBox.innerHTML = '<span class="spinner"></span> cargando...';
  const resp = await fetch("/memory/entries");
  const entries = await resp.json();

  if (!entries.length) {
    memoryListBox.innerHTML = '<div style="color:var(--muted); font-size:12px;">Todavia no hay nada guardado en la memoria a largo plazo.</div>';
    return;
  }

  memoryListBox.innerHTML = "";
  entries.forEach((entry) => {
    const row = document.createElement("div");
    row.style.cssText = "padding:10px 0; border-top:1px solid var(--border);";

    const label = document.createElement("div");
    label.style.cssText = "font-size:10.5px; color:var(--muted); text-transform:uppercase; letter-spacing:0.04em; margin-bottom:4px;";
    label.textContent = MEMORY_TYPE_LABEL[entry.type] || entry.type;
    row.appendChild(label);

    const textEl = document.createElement("div");
    textEl.style.cssText = "font-size:13px; white-space:pre-wrap; margin-bottom:6px;";
    textEl.textContent = entry.text;
    row.appendChild(textEl);

    if (entry.type === "note") {
      const btnRow = document.createElement("div");
      btnRow.style.cssText = "display:flex; gap:6px;";

      const editBtn = document.createElement("button");
      editBtn.textContent = "Editar";
      editBtn.style.cssText = "font-size:11.5px; padding:3px 9px; background:var(--panel-2); color:var(--text); border:1px solid var(--border); border-radius:6px; cursor:pointer;";
      editBtn.addEventListener("click", () => {
        const textarea = document.createElement("textarea");
        textarea.value = entry.text;
        textarea.rows = 2;
        textarea.style.cssText = "width:100%; background:var(--panel-3); color:var(--text); border:1px solid var(--accent); border-radius:6px; padding:8px; font-family:inherit; resize:vertical; box-sizing:border-box; margin-bottom:6px;";
        row.replaceChild(textarea, textEl);
        textarea.focus();
        editBtn.textContent = "Guardar";
        editBtn.addEventListener("click", async () => {
          const form = new FormData();
          form.append("text", textarea.value.trim());
          await fetch(`/memory/entries/${encodeURIComponent(entry.id)}`, { method: "PUT", body: form });
          await loadMemoryEntries();
        }, { once: true });
      }, { once: true });

      const delBtn = document.createElement("button");
      delBtn.textContent = "Borrar";
      delBtn.style.cssText = editBtn.style.cssText;
      delBtn.addEventListener("click", async () => {
        if (!confirm("¿Borrar esta nota de la memoria?")) return;
        await fetch(`/memory/entries/${encodeURIComponent(entry.id)}`, { method: "DELETE" });
        await loadMemoryEntries();
      });

      btnRow.appendChild(editBtn);
      btnRow.appendChild(delBtn);
      row.appendChild(btnRow);
    }

    memoryListBox.appendChild(row);
  });
}

addMemoryNoteBtn.addEventListener("click", async () => {
  const text = memoryNoteInput.value.trim();
  if (!text) return;
  addMemoryNoteBtn.disabled = true;
  try {
    const form = new FormData();
    form.append("text", text);
    await fetch("/memory/entries", { method: "POST", body: form });
    memoryNoteInput.value = "";
    await loadMemoryEntries();
  } finally {
    addMemoryNoteBtn.disabled = false;
  }
});

// --- Personas (crear LoRA a partir de fotos, fase 1 solo SDXL - ver
// ROADMAP.md y pendiente/pendiente.md) ---
const optPersonasBtn = document.getElementById("optPersonasBtn");
const personasModal = document.getElementById("personasModal");
const closePersonasBtn = document.getElementById("closePersonasBtn");
const personasListBox = document.getElementById("personasListBox");
const personaNameInput = document.getElementById("personaNameInput");
const personaPhotosInput = document.getElementById("personaPhotosInput");
const personaPhotosCount = document.getElementById("personaPhotosCount");
const createPersonaBtn = document.getElementById("createPersonaBtn");
let personasPollTimer = null;

const PERSONA_STATUS_LABEL = {
  sin_empezar: "sin empezar", training: "entrenando...", listo: "lista", error: "error",
};

optPersonasBtn.addEventListener("click", async () => {
  optionsModal.style.display = "none";
  personasModal.style.display = "block";
  const personas = await loadPersonas();
  showPersonasTab(personas.length ? "list" : "create");
  if (personasPollTimer) clearInterval(personasPollTimer);
  personasPollTimer = setInterval(() => {
    if (personasModal.style.display === "block") loadPersonas();
  }, 5000);
});
closePersonasBtn.addEventListener("click", () => {
  personasModal.style.display = "none";
  if (personasPollTimer) { clearInterval(personasPollTimer); personasPollTimer = null; }
});
personasModal.addEventListener("click", (e) => {
  if (e.target === personasModal) closePersonasBtn.click();
});

const personaDropzone = document.getElementById("personaDropzone");
const personaThumbs = document.getElementById("personaThumbs");
const PERSONA_MIN_PHOTOS = 3;
let personaFiles = [];

function showPersonasTab(tab) {
  document.querySelectorAll(".personas-tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  document.getElementById("personasListPane").style.display = tab === "list" ? "block" : "none";
  document.getElementById("personasCreatePane").style.display = tab === "create" ? "block" : "none";
  if (tab === "create") personaNameInput.focus();
}
document.querySelectorAll(".personas-tab").forEach((b) => b.addEventListener("click", () => showPersonasTab(b.dataset.tab)));

function addPersonaFiles(files) {
  for (const f of files) {
    if (!f.type.startsWith("image/")) continue;
    if (personaFiles.some((x) => x.name === f.name && x.size === f.size)) continue;  // la misma foto dos veces
    personaFiles.push(f);
  }
  renderPersonaThumbs();
}

function renderPersonaThumbs() {
  personaThumbs.querySelectorAll("img").forEach((img) => URL.revokeObjectURL(img.src));
  personaThumbs.innerHTML = "";
  personaFiles.forEach((f, i) => {
    const wrap = document.createElement("div");
    wrap.className = "thumb";
    const img = document.createElement("img");
    img.src = URL.createObjectURL(f);
    img.alt = f.name;
    const rm = document.createElement("button");
    rm.textContent = "✕";
    rm.title = "Quitar esta foto";
    rm.addEventListener("click", () => { personaFiles.splice(i, 1); renderPersonaThumbs(); });
    wrap.append(img, rm);
    personaThumbs.appendChild(wrap);
  });
  const n = personaFiles.length;
  personaPhotosCount.className = "personas-count" + (n >= 15 ? " ok" : "");
  personaPhotosCount.textContent = !n ? "" :
    `${n} foto${n === 1 ? "" : "s"}` + (n < PERSONA_MIN_PHOTOS ? ` - hacen falta al menos ${PERSONA_MIN_PHOTOS}` :
      n < 15 ? " - funcionara, pero con 15 o mas el resultado se parece mas" : " - perfecto");
  createPersonaBtn.disabled = n < PERSONA_MIN_PHOTOS;
}

personaDropzone.addEventListener("click", () => personaPhotosInput.click());
personaDropzone.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); personaPhotosInput.click(); } });
personaPhotosInput.addEventListener("change", () => { addPersonaFiles(personaPhotosInput.files); personaPhotosInput.value = ""; });
["dragenter", "dragover"].forEach((ev) => personaDropzone.addEventListener(ev, (e) => {
  e.preventDefault(); personaDropzone.classList.add("drag");
}));
["dragleave", "drop"].forEach((ev) => personaDropzone.addEventListener(ev, (e) => {
  e.preventDefault(); personaDropzone.classList.remove("drag");
}));
personaDropzone.addEventListener("drop", (e) => addPersonaFiles(e.dataTransfer.files));

const PERSONA_STATUS = {
  sin_empezar: ["Sin empezar", "", ""],
  training: ["Entrenando...", "training", "Aprendiendo la cara - puede tardar bastante"],
  listo: ["Lista", "listo", "Lista: elígela en «Persona» del modo Imagen"],
  error: ["Error", "error", ""],
};

async function loadPersonas() {
  const resp = await fetch("/personas");
  const data = await resp.json();
  const personas = data.personas || [];
  personasListBox.innerHTML = "";
  if (!personas.length) {
    const empty = document.createElement("div");
    empty.className = "personas-empty";
    empty.innerHTML = "Todavia no has creado ninguna persona.<br>";
    const btn = document.createElement("button");
    btn.className = "personas-primary";
    btn.textContent = "Crear tu primera persona";
    btn.addEventListener("click", () => showPersonasTab("create"));
    empty.appendChild(btn);
    personasListBox.appendChild(empty);
    return personas;
  }
  for (const p of personas) {
    const st = p.sdxl || { status: "sin_empezar" };
    const [label, cls, hint] = PERSONA_STATUS[st.status] || [st.status, "", ""];
    const card = document.createElement("div");
    card.className = "persona-card";
    const avatar = document.createElement("div");
    avatar.className = "persona-avatar";
    avatar.textContent = (p.name[0] || "?").toUpperCase();
    const info = document.createElement("div");
    info.className = "persona-info";
    const name = document.createElement("div");
    name.className = "name";
    name.textContent = p.name;
    const detail = document.createElement("div");
    detail.className = "detail" + (st.status === "error" ? " error" : "");
    detail.textContent = st.status === "error" ? (st.error || "El entrenamiento fallo") : hint;
    if (st.status === "training") {
      detail.textContent = st.percent === undefined
        ? "Preparando las fotos y el modelo (unos minutos)…"
        : `${st.percent}% hecho` + (st.eta ? ` · quedan ${st.eta}` : "");
    }
    info.append(name, detail);
    const pill = document.createElement("span");
    pill.className = "persona-pill " + cls;
    pill.textContent = label;
    card.append(avatar, info, pill);
    if (st.status !== "training") {
      const del = document.createElement("button");
      del.className = "persona-delete";
      del.textContent = "Borrar";
      del.title = "Borra la cara aprendida de esta persona";
      del.style.cssText = "margin-left:8px; font-size:11px; padding:3px 8px; background:var(--panel-2); color:var(--error, #e05252); border:1px solid var(--border); border-radius:6px; cursor:pointer;";
      del.addEventListener("click", async () => {
        if (!confirm(`¿Borrar a "${p.name}"? Se borra la cara aprendida y no se puede deshacer.`)) return;
        const r = await fetch(`/personas/${encodeURIComponent(p.name)}`, { method: "DELETE" });
        if (!r.ok) { const d = await r.json().catch(() => ({})); alert(d.detail || "No se pudo borrar."); }
        await loadPersonas();
      });
      card.appendChild(del);
    }
    personasListBox.appendChild(card);
  }
  return personas;
}

createPersonaBtn.addEventListener("click", async () => {
  const name = personaNameInput.value.trim();
  if (!name) { alert("Escribe el nombre de la persona."); personaNameInput.focus(); return; }
  if (personaFiles.length < PERSONA_MIN_PHOTOS) { alert(`Hacen falta al menos ${PERSONA_MIN_PHOTOS} fotos.`); return; }

  createPersonaBtn.disabled = true;
  createPersonaBtn.textContent = "Subiendo fotos...";
  try {
    const form = new FormData();
    form.append("name", name);
    for (const file of personaFiles) form.append("photos", file);
    const resp = await fetch("/personas", { method: "POST", body: form });
    const result = await resp.json();
    if (!resp.ok) {
      alert(result.detail || "No se pudo crear la persona.");
      return;
    }
    personaNameInput.value = "";
    personaFiles = [];
    renderPersonaThumbs();
    await loadPersonas();
    showPersonasTab("list");
  } catch (err) {
    alert("Error de conexion al crear la persona.");
  } finally {
    createPersonaBtn.textContent = "Crear persona";
    createPersonaBtn.disabled = personaFiles.length < PERSONA_MIN_PHOTOS;
  }
});
renderPersonaThumbs();

const metricsBtn = document.getElementById("optMetricsBtn");
const metricsModal = document.getElementById("metricsModal");
const closeMetricsBtn = document.getElementById("closeMetricsBtn");
const metricsBox = document.getElementById("metricsBox");

metricsBtn.addEventListener("click", async () => {
  optionsModal.style.display = "none";
  metricsModal.style.display = "block";
  await loadMetrics();
});
closeMetricsBtn.addEventListener("click", () => { metricsModal.style.display = "none"; });
metricsModal.addEventListener("click", (e) => { if (e.target === metricsModal) metricsModal.style.display = "none"; });

async function loadMetrics() {
  metricsBox.innerHTML = '<span class="spinner"></span> cargando...';
  const resp = await fetch("/metrics/summary");
  const data = await resp.json();

  if (!data.total_requests) {
    metricsBox.innerHTML = '<div style="color:var(--muted);">Todavia no hay peticiones registradas.</div>';
    return;
  }

  let html = `<div style="margin-bottom:12px; color:var(--muted);">${data.window} · ${data.total_requests} peticiones · ${data.errors} errores · ${data.verifier_gated} bloqueadas por el verificador</div>`;
  html += '<table style="width:100%; border-collapse:collapse;">';
  html += '<tr style="text-align:left; color:var(--muted); font-size:11px;"><th style="padding:6px 0;">Agente</th><th>Peticiones</th><th>Latencia media</th><th>Errores</th><th>Bloqueadas</th></tr>';
  for (const [agent, s] of Object.entries(data.by_agent)) {
    html += `<tr style="border-top:1px solid var(--border);">
      <td style="padding:6px 0;">${esc(agent)}</td>
      <td>${s.count}</td>
      <td>${(s.avg_latency_ms / 1000).toFixed(1)}s</td>
      <td>${s.errors}</td>
      <td>${s.gated}</td>
    </tr>`;
  }
  html += '</table>';
  metricsBox.innerHTML = html;
}

const modelUpdatesBtn = document.getElementById("optModelUpdatesBtn");
const modelUpdatesModal = document.getElementById("modelUpdatesModal");
const closeModelUpdatesBtn = document.getElementById("closeModelUpdatesBtn");
const checkModelUpdatesBtn = document.getElementById("checkModelUpdatesBtn");
const modelUpdatesBox = document.getElementById("modelUpdatesBox");
const modelStatusBox = document.getElementById("modelStatusBox");
const imageModelsBox = document.getElementById("imageModelsBox");
const videoModelsBox = document.getElementById("videoModelsBox");

modelUpdatesBtn.addEventListener("click", async () => {
  optionsModal.style.display = "none";
  modelUpdatesModal.style.display = "block";
  await loadModelUpdates(false);
  await loadModelStatusBox();
  await loadImageVideoModelsBox();
});
closeModelUpdatesBtn.addEventListener("click", () => { modelUpdatesModal.style.display = "none"; });
modelUpdatesModal.addEventListener("click", (e) => { if (e.target === modelUpdatesModal) modelUpdatesModal.style.display = "none"; });
checkModelUpdatesBtn.addEventListener("click", () => loadModelUpdates(true));

async function loadModelStatusBox() {
  modelStatusBox.textContent = "Cargando estado...";
  try {
    const resp = await fetch("/models/status");
    const data = await resp.json();
    const models = data.models || [];
    modelStatusBox.innerHTML = models.length
      ? "Cargados ahora mismo: " + models.map(m => `<strong style="color:var(--text);">${esc(m.name)}</strong>`).join(", ")
      : "Ningun modelo cargado en memoria ahora mismo.";
  } catch (err) {
    modelStatusBox.textContent = "No se pudo consultar el estado de Ollama.";
  }
}

function _modelStatusLabel(u) {
  return u.local_only
    ? `<span style="color:var(--muted);">variante local (uso interno, sin version en el registro)</span>`
    : u.error
      ? `<span style="color:var(--muted);">no se pudo comprobar (${esc(u.error)})</span>`
      : u.update_available
        ? `<span style="color:var(--accent, #4caf50);">actualizacion disponible</span>`
        : `<span style="color:var(--muted);">al dia</span>`;
}

function _modelRowHtml(u, roleText) {
  const updateBtn = u.update_available
    ? `<button data-model="${esc(u.model)}" class="updateModelBtn" style="font-size:12px; padding:4px 10px; background:var(--panel-2); color:var(--text); border:1px solid var(--border); border-radius:6px; cursor:pointer;">Actualizar</button>`
    : "";
  const deleteBtn = !roleText
    ? `<button data-model="${esc(u.model)}" class="deleteModelBtn" style="font-size:12px; padding:4px 10px; background:var(--panel-2); color:var(--error, #e05252); border:1px solid var(--border); border-radius:6px; cursor:pointer;">Borrar</button>`
    : "";
  const role = roleText
    ? `<div style="font-size:11.5px; color:var(--accent); margin-top:2px;">${esc(roleText)}</div>`
    : "";
  return `<div style="display:flex; justify-content:space-between; align-items:center; padding:8px 0; border-top:1px solid var(--border);">
    <div><strong>${esc(u.model)}</strong>${role}<div style="margin-top:2px;">${_modelStatusLabel(u)}</div></div>
    <div style="display:flex; gap:6px; flex-shrink:0;">${updateBtn}${deleteBtn}</div>
  </div>`;
}

async function loadModelUpdates(force) {
  modelUpdatesBox.innerHTML = '<span class="spinner"></span> comprobando...';
  const [updatesResp, rolesResp] = await Promise.all([
    fetch(`/models/updates${force ? "?force=true" : ""}`),
    fetch("/models/roles"),
  ]);
  const data = await updatesResp.json();
  const roles = await rolesResp.json().catch(() => ({}));

  if (!data.updates || !data.updates.length) {
    modelUpdatesBox.innerHTML = '<div style="color:var(--muted);">No se encontraron modelos instalados de la biblioteca oficial de Ollama.</div>';
    return;
  }

  const inUse = data.updates.filter((u) => roles[u.model]);
  const unused = data.updates.filter((u) => !roles[u.model]);

  let html = `<div style="font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:0.04em; padding:4px 0;">En uso (${inUse.length})</div>`;
  html += inUse.map((u) => _modelRowHtml(u, roles[u.model])).join("");
  if (unused.length) {
    html += `<div style="font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:0.04em; padding:14px 0 4px;">Sin usar - candidatos a borrar (${unused.length})</div>`;
    html += unused.map((u) => _modelRowHtml(u, null)).join("");
  }
  modelUpdatesBox.innerHTML = html;

  modelUpdatesBox.querySelectorAll(".updateModelBtn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const model = btn.dataset.model;
      if (!confirm(`Se va a descargar la ultima version de ${model}. Puede tardar varios minutos y ocupar varios GB. ¿Continuar?`)) return;
      btn.disabled = true;
      btn.textContent = "Descargando...";
      const resp = await fetch("/models/update", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: `model=${encodeURIComponent(model)}`,
      });
      const data = await resp.json();
      alert(data.message || data.detail || "Descarga iniciada.");
    });
  });

  modelUpdatesBox.querySelectorAll(".deleteModelBtn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const model = btn.dataset.model;
      if (!confirm(`Se va a borrar '${model}' de este ordenador para liberar disco. Para volver a usarlo habria que descargarlo de nuevo. ¿Continuar?`)) return;
      btn.disabled = true;
      btn.textContent = "Borrando...";
      try {
        const resp = await fetch(`/models/text/${encodeURIComponent(model)}`, { method: "DELETE" });
        const result = await resp.json();
        if (!resp.ok) {
          alert(result.detail || "No se pudo borrar.");
          btn.disabled = false;
          btn.textContent = "Borrar";
          return;
        }
        await loadModelUpdates(true);
      } catch (err) {
        alert("Error de conexion al borrar.");
        btn.disabled = false;
        btn.textContent = "Borrar";
      }
    });
  });
}

function renderModelArchSection(container, data, kindLabel, endpointKind) {
  const archs = data.architectures || [];
  const models = data.models || [];
  if (!archs.length) {
    container.innerHTML = '<div style="color:var(--muted); padding-top:8px;">Nada soportado todavia.</div>';
    return;
  }
  let html = data.base_folder
    ? `<div style="font-size:12px; color:var(--muted); padding-bottom:10px;">Si quieres añadir modelos nuevos de ${kindLabel}, copia el archivo dentro de <code class="model-arch-folder" style="display:inline;">${esc(data.base_folder)}\\subcarpeta-de-tipo\\nombre-del-modelo</code> (subcarpetas abajo).</div>`
    : "";
  for (const arch of archs) {
    const installedForArch = models.filter((m) => m.architecture === arch.id);
    const installedHtml = installedForArch.length
      ? installedForArch.map((m) => `<div style="display:flex; align-items:center; gap:6px; margin-top:4px;">
          <span>${esc(m.label)}</span>
          <button class="deleteArchModelBtn" data-model-id="${esc(m.id)}" data-label="${esc(m.label)}" style="font-size:10.5px; padding:2px 6px; background:var(--panel-2); color:var(--error, #e05252); border:1px solid var(--border); border-radius:5px; cursor:pointer;">Borrar</button>
        </div>`).join("")
      : `<div style="color:var(--muted); margin-top:4px;">(ninguno instalado)</div>`;
    html += `<div class="model-arch-row">
      <div><strong>${esc(arch.label)}:</strong></div>
      ${installedHtml}
      <div style="display:flex; gap:6px; align-items:center; margin-top:8px;">
        <code class="model-arch-folder">${esc(arch.folder)}</code>
        <button class="copyFolderBtn" data-folder="${esc(arch.folder)}">Copiar</button>
      </div>
    </div>`;
  }
  container.innerHTML = html;

  container.querySelectorAll(".copyFolderBtn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(btn.dataset.folder);
        btn.textContent = "Copiado";
        setTimeout(() => { btn.textContent = "Copiar ruta"; }, 1500);
      } catch (err) {
        // portapapeles no disponible (p.ej. sin HTTPS) - la ruta ya esta visible para seleccionar a mano
      }
    });
  });

  container.querySelectorAll(".deleteArchModelBtn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const modelId = btn.dataset.modelId;
      const label = btn.dataset.label;
      if (!confirm(`Se va a borrar el archivo de '${label}' de este ordenador. Para volver a usarlo habria que descargarlo de nuevo. ¿Continuar?`)) return;
      btn.disabled = true;
      btn.textContent = "...";
      try {
        const resp = await fetch(`/models/${endpointKind}/${encodeURIComponent(modelId)}`, { method: "DELETE" });
        const result = await resp.json();
        if (!resp.ok) {
          alert(result.detail || "No se pudo borrar.");
          btn.disabled = false;
          btn.textContent = "Borrar";
          return;
        }
        await loadImageVideoModelsBox();
      } catch (err) {
        alert("Error de conexion al borrar.");
        btn.disabled = false;
        btn.textContent = "Borrar";
      }
    });
  });
}

async function loadImageVideoModelsBox() {
  imageModelsBox.innerHTML = '<span class="spinner"></span> cargando...';
  videoModelsBox.innerHTML = '<span class="spinner"></span> cargando...';
  try {
    const [imgResp, vidResp] = await Promise.all([fetch("/models/image"), fetch("/models/video")]);
    renderModelArchSection(imageModelsBox, await imgResp.json(), "imagen", "image");
    renderModelArchSection(videoModelsBox, await vidResp.json(), "video", "video");
  } catch (err) {
    imageModelsBox.textContent = "No se pudo cargar.";
    videoModelsBox.textContent = "No se pudo cargar.";
  }
}

// --- Editor de inpainting (pintar mascara sobre una imagen ya generada) ---
const inpaintModal = document.getElementById("inpaintModal");
const closeInpaintBtn = document.getElementById("closeInpaintBtn");
const inpaintBaseImg = document.getElementById("inpaintBaseImg");
const inpaintCanvas = document.getElementById("inpaintCanvas");
const inpaintCtx = inpaintCanvas.getContext("2d");
const brushSize = document.getElementById("brushSize");
const clearMaskBtn = document.getElementById("clearMaskBtn");
const inpaintPrompt = document.getElementById("inpaintPrompt");
const submitInpaintBtn = document.getElementById("submitInpaintBtn");

let inpaintFilePath = null;
let isPainting = false;

function openInpaintEditor(fileUrl, filePath) {
  inpaintFilePath = filePath;
  inpaintPrompt.value = "";
  inpaintModal.style.display = "block";

  inpaintBaseImg.onload = () => {
    // el canvas usa el tamaño real de la imagen (no el tamaño mostrado en pantalla)
    // para que la mascara sea precisa aunque la imagen se vea escalada en el modal
    inpaintCanvas.width = inpaintBaseImg.naturalWidth;
    inpaintCanvas.height = inpaintBaseImg.naturalHeight;
    inpaintCanvas.style.width = inpaintBaseImg.clientWidth + "px";
    inpaintCanvas.style.height = inpaintBaseImg.clientHeight + "px";
    inpaintCtx.clearRect(0, 0, inpaintCanvas.width, inpaintCanvas.height);
  };
  inpaintBaseImg.src = fileUrl;
}

closeInpaintBtn.addEventListener("click", () => { inpaintModal.style.display = "none"; });
clearMaskBtn.addEventListener("click", () => {
  inpaintCtx.clearRect(0, 0, inpaintCanvas.width, inpaintCanvas.height);
});

function canvasCoordsFromEvent(e) {
  const rect = inpaintCanvas.getBoundingClientRect();
  const scaleX = inpaintCanvas.width / rect.width;
  const scaleY = inpaintCanvas.height / rect.height;
  const clientX = e.touches ? e.touches[0].clientX : e.clientX;
  const clientY = e.touches ? e.touches[0].clientY : e.clientY;
  return { x: (clientX - rect.left) * scaleX, y: (clientY - rect.top) * scaleY };
}

function paintAt(x, y) {
  inpaintCtx.fillStyle = "rgba(255, 60, 60, 0.85)";
  inpaintCtx.beginPath();
  inpaintCtx.arc(x, y, Number(brushSize.value), 0, Math.PI * 2);
  inpaintCtx.fill();
}

inpaintCanvas.addEventListener("mousedown", (e) => { isPainting = true; const c = canvasCoordsFromEvent(e); paintAt(c.x, c.y); });
inpaintCanvas.addEventListener("mousemove", (e) => { if (isPainting) { const c = canvasCoordsFromEvent(e); paintAt(c.x, c.y); } });
window.addEventListener("mouseup", () => { isPainting = false; });
inpaintCanvas.addEventListener("touchstart", (e) => { isPainting = true; const c = canvasCoordsFromEvent(e); paintAt(c.x, c.y); e.preventDefault(); });
inpaintCanvas.addEventListener("touchmove", (e) => { if (isPainting) { const c = canvasCoordsFromEvent(e); paintAt(c.x, c.y); e.preventDefault(); } });
window.addEventListener("touchend", () => { isPainting = false; });

function buildMaskBlob() {
  // el trazo visible es rojo translucido (para que se vea bien sobre la foto);
  // la mascara real que se envia es blanco/negro puro segun donde haya trazo
  const maskCanvas = document.createElement("canvas");
  maskCanvas.width = inpaintCanvas.width;
  maskCanvas.height = inpaintCanvas.height;
  const maskCtx = maskCanvas.getContext("2d");
  const painted = inpaintCtx.getImageData(0, 0, inpaintCanvas.width, inpaintCanvas.height);
  const out = maskCtx.createImageData(inpaintCanvas.width, inpaintCanvas.height);
  for (let i = 0; i < painted.data.length; i += 4) {
    const alpha = painted.data[i + 3];
    const v = alpha > 10 ? 255 : 0;
    out.data[i] = v; out.data[i + 1] = v; out.data[i + 2] = v; out.data[i + 3] = 255;
  }
  maskCtx.putImageData(out, 0, 0);
  return new Promise((resolve) => maskCanvas.toBlob(resolve, "image/png"));
}

submitInpaintBtn.addEventListener("click", async () => {
  if (!inpaintPrompt.value.trim()) {
    alert("Describe que quieres que aparezca en la zona marcada.");
    return;
  }
  submitInpaintBtn.disabled = true;
  submitInpaintBtn.textContent = "Repintando...";
  try {
    const maskBlob = await buildMaskBlob();
    const form = new FormData();
    form.append("prompt", inpaintPrompt.value.trim());
    form.append("file_path", inpaintFilePath);
    form.append("mask", maskBlob, "mask.png");
    form.append("denoise", "1.0");
    const resp = await fetch("/image/inpaint", { method: "POST", body: form });
    const data = await resp.json();
    inpaintModal.style.display = "none";
    const resultBubble = addMessage("assistant", "image_inpaint");
    resultBubble.textContent = data.response;
    if (data.file_url) appendMediaWithActions(resultBubble, data.file_url, data.file_path);
  } finally {
    submitInpaintBtn.disabled = false;
    submitInpaintBtn.textContent = "Repintar zona marcada";
  }
});

sendBtn.addEventListener("click", () => send());  // sin el evento: send(x) es "reintentar x"
promptEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    send();
  }
});
promptEl.addEventListener("input", () => {
  promptEl.style.height = "auto";
  promptEl.style.height = Math.min(promptEl.scrollHeight, 200) + "px";
});

initSessionUI().then(() => {
  if (!isLoggedIn()) return;
  restoreModeAndPrepare();
  if (getSessionRole() !== "guest") {
    refreshBackgroundTasks();
    setInterval(refreshBackgroundTasks, 8000);
  }
});
refreshPlan();
