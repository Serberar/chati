// Vincular este dispositivo (el movil) con Chati: el codigo llega en el QR
// (tras "#", que no se envia al servidor ni queda en ningun registro) o se
// teclea el corto que enseña el ordenador. Ver devices.py.
const form = document.getElementById("form");
const codeEl = document.getElementById("code");
const goBtn = document.getElementById("go");
const msg = document.getElementById("msg");

function deviceName() {
  const ua = navigator.userAgent;
  if (/iPhone/.test(ua)) return "iPhone";
  if (/iPad/.test(ua)) return "iPad";
  if (/Android/.test(ua)) return /Mobile/.test(ua) ? "Móvil Android" : "Tablet Android";
  if (/Windows/.test(ua)) return "Ordenador Windows";
  if (/Mac/.test(ua)) return "Mac";
  return "Dispositivo";
}

function show(text, cls) {
  msg.textContent = text;
  msg.className = cls || "";
}

async function claim(code) {
  goBtn.disabled = true;
  show("Vinculando…");
  try {
    const resp = await fetch("/pair/claim", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code, name: deviceName() }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      show(data.detail || "No se ha podido vincular.", "error");
      goBtn.disabled = false;
      return;
    }
    show("¡Vinculado! Abriendo Chati…", "ok");
    history.replaceState(null, "", "/pair");  // el codigo ya no sirve: fuera de la barra
    setTimeout(() => { location.href = "/"; }, 700);
  } catch (err) {
    show("No hay conexión con el ordenador. ¿Está encendido y con Tailscale activo?", "error");
    goBtn.disabled = false;
  }
}

form.addEventListener("submit", (e) => {
  e.preventDefault();
  const code = codeEl.value.trim();
  if (code) claim(code);
});

const fromQr = location.hash.slice(1);
if (fromQr) {
  document.getElementById("intro").textContent = "Vinculando este dispositivo con el código del QR…";
  form.style.display = "none";
  claim(fromQr).then(() => { if (goBtn.disabled === false) form.style.display = ""; });
}
