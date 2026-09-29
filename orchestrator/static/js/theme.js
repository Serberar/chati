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
document.getElementById("sidebarHideBtn").addEventListener("click", () => setSidebarHidden(true));
document.getElementById("sidebarShowBtn").addEventListener("click", () => setSidebarHidden(false));
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === "b") {
    e.preventDefault();
    setSidebarHidden(!document.body.classList.contains("sidebar-hidden"));
  }
});
try { if (localStorage.getItem("ia_sidebar_hidden") === "1") document.body.classList.add("sidebar-hidden"); } catch (e) {}
