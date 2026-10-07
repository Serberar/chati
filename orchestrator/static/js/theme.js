// --- Tema claro/oscuro ---
function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  document.getElementById("themeToggle").innerHTML = theme === "light" ? "&#9788;" : "&#9789;";
}
function initTheme() {
  const saved = localStorage.getItem("ia_theme") || "dark";
  applyTheme(saved);
}
document.getElementById("themeToggle").addEventListener("click", () => {
  const current = document.documentElement.getAttribute("data-theme") || "dark";
  const next = current === "light" ? "dark" : "light";
  localStorage.setItem("ia_theme", next);
  applyTheme(next);
});
initTheme();

// Barra lateral ocultable (mejoras.md): se recuerda en este navegador
function setSidebarHidden(hidden) {
  document.body.classList.toggle("sidebar-hidden", hidden);
  try { localStorage.setItem("ia_sidebar_hidden", hidden ? "1" : "0"); } catch (e) {}
}
// En el movil la barra lateral es un menu que se desliza por encima del chat
// (☰) y se cierra al tocar fuera o al elegir algo (2026-10-07)
const mobileQuery = window.matchMedia("(max-width: 760px)");
function setMobileMenu(open) {
  document.body.classList.toggle("mobile-menu-open", open);
}
const mobileBackdrop = document.createElement("div");
mobileBackdrop.id = "mobileBackdrop";
document.body.appendChild(mobileBackdrop);
mobileBackdrop.addEventListener("click", () => setMobileMenu(false));
document.getElementById("sidebar").addEventListener("click", (e) => {
  if (!mobileQuery.matches) return;
  const pickedConversation = e.target.closest(".conv-row") && !e.target.closest(".conv-rename, .conv-del, input");
  if (pickedConversation || e.target.closest("#newChatBtn, #optionsBtn, #appsBtn")) setMobileMenu(false);
});
mobileQuery.addEventListener("change", () => setMobileMenu(false));

document.getElementById("sidebarHideBtn").addEventListener("click", () => {
  if (mobileQuery.matches) setMobileMenu(false); else setSidebarHidden(true);
});
document.getElementById("sidebarShowBtn").addEventListener("click", () => {
  if (mobileQuery.matches) setMobileMenu(true); else setSidebarHidden(false);
});
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === "b") {
    e.preventDefault();
    setSidebarHidden(!document.body.classList.contains("sidebar-hidden"));
  }
});
try { if (localStorage.getItem("ia_sidebar_hidden") === "1") document.body.classList.add("sidebar-hidden"); } catch (e) {}
