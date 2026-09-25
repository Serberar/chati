# Cómo usar tu IA personal

## Arrancar

Doble clic en **"Arrancar IA Personal"** del escritorio. Espera ~30-60 segundos
(arranca Ollama, ComfyUI y el orquestador) y se abrirá el navegador solo.

Si no se abre solo: ve a **http://localhost:8899**

## La primera vez: crea tu cuenta

La primera vez que abras la página verás una pantalla de inicio de sesión.
Pulsa **"Crear una cuenta nueva"**. Te pedirá:
- Usuario y contraseña (mínimo 8 caracteres) — esta es la contraseña que
  desbloquea tus conversaciones cifradas, guárdala bien: **si la olvidas y
  no configuraste una pregunta de seguridad, no hay forma de recuperar tus
  conversaciones e imágenes anteriores** (ni yo ni nadie puede leerlas sin
  ella — es la garantía de privacidad real, no un capricho).
- Una **clave de registro**: la primera vez no la necesitas (la primera
  cuenta creada es admin automáticamente), pero para cualquier cuenta
  siguiente hace falta la clave de registro, que puedes ver desde el panel
  de Administración una vez dentro.
- Una pregunta de seguridad (opcional, pero recomendada) — si olvidas la
  contraseña, te deja volver a entrar con una cuenta limpia, aunque las
  conversaciones cifradas con la contraseña anterior quedan inaccesibles
  para siempre (mismo motivo: no se debilita la protección por comodidad).

También puedes entrar como **invitado**, sin crear cuenta: puedes usar el
chat, generar imágenes/vídeo con normalidad, pero no ves la galería de
caras, documentos ni CV de nadie, y tu propia conversación de invitado no
se guarda al cerrar sesión.

El navegador recuerda la sesión después de iniciar sesión una vez (no hace
falta repetirlo cada vez, solo si cierras sesión o borras datos del navegador).

## Los modos (panel de la izquierda)

| Modo | Qué hace |
|---|---|
| **Chat** | Conversación normal. Detecta solo si le pides código, una imagen o un vídeo y usa el agente adecuado. Ahora, cuando la pregunta lo requiere (por ejemplo "¿qué día es hoy?" o algo que ya le contaste antes), consulta la fecha real o tu memoria antes de responder, en vez de adivinar. |
| **Imagen** | Fuerza generación de imagen aunque el texto no lo deje claro. |
| **Video** | Fuerza generación de vídeo corto. |
| **Imagen con cara real** | Subes una foto tuya (o de alguien con su permiso) y describes una escena nueva — la cara se mantiene. |
| **Video con cara real** | Igual pero en vídeo (más lento, genera primero la imagen y luego la anima). |
| **Voz** | Botón de grabar — habla y te responde con voz. |

### Personas guardadas

En los modos de "cara real" hay un desplegable **Persona**. Si subes una foto y
le pones un nombre en "Guardar como", la próxima vez no hace falta volver a
subirla — la eliges del desplegable. Gestionable desde ahí mismo.

### Editar imágenes ya generadas

Debajo de cualquier imagen generada hay dos botones:
- **Escalar x4** — la hace más grande y nítida, sin volver a generarla.
- **Editar (repintar zona)** — pinta con el ratón la parte que quieres cambiar,
  describe qué quieres ahí, y solo se repinta esa zona (el resto queda igual).

## Agente de código (edita tus archivos)

El botón **"Agente de código"** del panel izquierdo abre, en otra pestaña, un
asistente que puede leer y editar archivos de verdad en tu ordenador (como si
tuvieras a Claude Code trabajando en local, con un modelo tuyo). Sirve para
programar cuando no tengas acceso a mí: le pides algo, te enseña qué archivo
va a tocar y qué comando va a ejecutar, y **tú apruebas antes de que lo haga**
— nunca borra, sobrescribe ni ejecuta nada irreversible sin tu confirmación
explícita.

## Los paneles (abajo del todo, en la izquierda)

- **Ver / editar memoria** — todo lo que has hablado con la IA, organizado por
  conversación. Puedes editar o borrar cualquier mensaje suelto, o la
  conversación entera.
- **Base de conocimiento** — sube documentos (PDF, Word, txt) tuyos. La IA los
  usa como fuente real al responder, y te dice de qué documento sacó el dato.
- **Métricas** — cuántas peticiones has hecho, cuánto tarda cada tipo, si algo
  ha fallado.

## Actualizaciones de modelos

El botón **"Actualizaciones de modelos"** comprueba si hay una versión nueva
de algún modelo que ya tienes instalado (mira solo unos KB de metadatos, no
descarga nada). Si hay una disponible, aparece un botón **"Actualizar"** al
lado — solo se descarga (puede tardar varios minutos y varios GB) si tú le
das explícitamente. Nada se actualiza solo, nunca.

## Perfil (CV)

En el panel de "Perfil" puedes subir tu CV. Se guarda cifrado, solo para ti
- ni siquiera un administrador puede leerlo sin tu contraseña.

## Tu cuenta

Abajo del todo del panel izquierdo, junto a tu nombre de usuario:
- **Cambiar contraseña** — en cualquier momento, pidiendo la actual.
- **Cerrar sesión** — vuelve a la pantalla de inicio de sesión.
- **Administración** (solo si eres admin) — ver la clave de registro para
  dar de alta a alguien más, ver quién tiene cuenta, eliminar cuentas
  antiguas (borra también todos sus datos), y el registro de cuándo se ha
  restablecido alguna contraseña.

## Privacidad: qué está cifrado y qué no

Tus conversaciones, documentos subidos, fotos de caras guardadas y tu CV
están cifrados en el disco con una clave derivada de tu contraseña — si
alguien coge tu ordenador y mira los archivos directamente, no puede leer
nada de eso. Lo que **no** se cifra (no hace falta, no es privado tuyo):
los modelos de IA en sí, el código del programa, la configuración.

## Si algo va lento o se cuelga

- Hay un botón **"Cancelar generación"** que aparece mientras se genera una
  imagen o vídeo — písalo si te has arrepentido o tarda demasiado.
- Si una generación de imagen/vídeo es la primera del día, tarda más (tiene
  que cargar el modelo en la memoria de la tarjeta gráfica). Las siguientes
  van más rápido.
- El chat de texto/código ahora usa un modelo más grande e inteligente que
  corre en el procesador (no en la tarjeta gráfica), así que cada respuesta
  puede tardar entre 20 y 40 segundos. Es normal, no está colgado — es el
  precio de que ya no invente datos y sea mejor programando.
- Si el ordenador lleva un rato largo generando cosas seguidas, se puede
  ralentizar (calor de la GPU del portátil). Es normal, no es un fallo.

## Backups

Cada día se hace una copia de seguridad automática de tu memoria, conocimiento
y caras guardadas en tu OneDrive personal, carpeta `IA-Personal-Backups`. No
tienes que hacer nada.

## Si el acceso directo no arranca algo

Abre PowerShell en `C:\AI\setup` y ejecuta:

```
powershell -ExecutionPolicy Bypass -File start_all.ps1
```

Te dirá qué parte no ha arrancado.
