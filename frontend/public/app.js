/* CrediMarket - frontend (JavaScript sin dependencias).
 * Solo habla con las APIs de los cuatro servicios: nunca con bases de datos, Redis, el broker ni el modelo.
 * Todo lo que se muestra viene de respuestas reales; no hay resultados simulados. */
"use strict";

const CFG = window.CREDIMARKET_CONFIG || {};
const NOMBRES = { productos: "Productos", clientes: "Clientes", prestamos: "Préstamos", inventario: "Inventario" };
const PLAZOS = [6, 12, 18, 24, 36];
const CANTIDAD_MAX = 5;
const EMPLEOS = ["dependiente", "independiente", "temporal"];
const ESTADOS = {
  PENDIENTE_INVENTARIO: ["espera", "Pendiente de inventario"],
  APROBADA: ["ok", "Aprobada"],
  ENTREGA_PENDIENTE: ["espera", "Entrega en curso"],
  COMPLETADA: ["ok", "Completada"],
  CANCELACION_PENDIENTE: ["espera", "Cancelando"],
  CANCELADA: ["", "Cancelada"],
  CANCELADA_SIN_STOCK: ["error", "Sin existencias"],
  RECHAZADA: ["error", "Rechazada"],
  VENCIDA: ["error", "Vencida"],
};
const CANCELABLES = ["PENDIENTE_INVENTARIO", "APROBADA"];

const vista = document.getElementById("vista");
const $ = (sel, raiz = document) => raiz.querySelector(sel);
const $$ = (sel, raiz = document) => [...raiz.querySelectorAll(sel)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const formatoCop = new Intl.NumberFormat("es-CO", { style: "currency", currency: "COP", maximumFractionDigits: 0 });
const cop = (n) => (n === null || n === undefined ? "—" : formatoCop.format(n));
const fecha = (s) => (s ? new Date(s).toLocaleString("es-CO", { dateStyle: "short", timeStyle: "medium" }) : "—");
const pct = (x, d = 1) => (x === null || x === undefined ? "—" : `${(x * 100).toFixed(d)} %`);
const corto = (id) => (id ? String(id).slice(0, 8) : "—");

/* ------------------------------------------------------------------ almacenamiento (tolerante a fallos) */
const almacen = {
  leer(k) { try { return JSON.parse(sessionStorage.getItem(k)); } catch { return null; } },
  guardar(k, v) { try { sessionStorage.setItem(k, JSON.stringify(v)); } catch { /* sin almacenamiento */ } },
  borrar(k) { try { sessionStorage.removeItem(k); } catch { /* sin almacenamiento */ } },
};

let sesion = almacen.leer("cm_sesion");
let temporizadores = [];
let productosCache = {};
const esAdmin = () => sesion?.rol === "admin";
const esCliente = () => sesion?.rol === "cliente";

/* ------------------------------------------------------------------ API */
class ErrorApi extends Error {
  constructor(estado, codigo, mensaje, errores, servicio) {
    super(mensaje);
    Object.assign(this, { estado, codigo, errores: errores || [], servicio });
  }
}

async function api(servicio, ruta, { metodo = "GET", cuerpo, cabeceras = {}, formulario, completo = false } = {}) {
  const h = { ...cabeceras };
  if (sesion?.token) h.Authorization = `Bearer ${sesion.token}`;
  let body;
  if (formulario) body = formulario;
  else if (cuerpo !== undefined) { h["Content-Type"] = "application/json"; body = JSON.stringify(cuerpo); }
  const control = new AbortController();
  const limite = setTimeout(() => control.abort(), 15000);
  let r;
  try {
    r = await fetch(`${CFG[servicio]}${ruta}`, { method: metodo, headers: h, body, signal: control.signal });
  } catch {
    throw new ErrorApi(0, "sin_conexion",
      `No se pudo conectar con el servicio de ${NOMBRES[servicio]}. Verifique que esté en ejecución y vuelva a intentarlo.`, [], servicio);
  } finally { clearTimeout(limite); }
  const texto = await r.text();
  let datos = null;
  try { datos = texto ? JSON.parse(texto) : null; } catch { datos = texto; }
  if (!r.ok) {
    const d = (datos && datos.detail) || {};
    const codigo = typeof d === "string" ? "error" : d.codigo || "error";
    let mensaje = typeof d === "string" ? d : d.mensaje || `Error ${r.status} en ${NOMBRES[servicio]}`;
    if (r.status >= 500 && !d.mensaje) mensaje = `El servicio de ${NOMBRES[servicio]} no está disponible (HTTP ${r.status}).`;
    if (r.status === 401 && sesion) {
      cerrarSesion(codigo === "sesion_vencida"
        ? "Su sesión venció. Inicie sesión de nuevo para continuar."
        : "Su sesión no es válida. Inicie sesión de nuevo.");
    }
    throw new ErrorApi(r.status, codigo, mensaje, d.errores, servicio);
  }
  return completo ? { datos, cabeceras: r.headers, estado: r.status } : datos;
}

/* ------------------------------------------------------------------ avisos y utilidades de UI */
let avisoTimer;
function avisar(texto, tipo = "ok", ms = 6000) {
  const el = document.getElementById("aviso");
  el.className = `aviso ${tipo}`;
  el.textContent = texto;
  el.hidden = false;
  clearTimeout(avisoTimer);
  if (ms) avisoTimer = setTimeout(() => { el.hidden = true; }, ms);
}
function avisarError(e) {
  avisar(e instanceof ErrorApi ? e.message : `Error inesperado: ${e.message || e}`, "error", 9000);
}
function chipEstado(estado) {
  const [clase, texto] = ESTADOS[estado] || ["", estado];
  const pulso = clase === "espera" ? " pulso" : "";
  return `<span class="chip ${clase}${pulso}" title="${esc(estado)}">${esc(texto)}</span>`;
}
function cargando(texto = "Cargando…") { return `<div class="vacio"><span class="cargando"></span> ${esc(texto)}</div>`; }
function bloqueError(e, reintentar = true) {
  return `<div class="tarjeta"><h2>No se pudo cargar esta vista</h2><p>${esc(e.message)}</p>
    ${reintentar ? '<button class="secundario" onclick="enrutar()">Reintentar</button>' : ""}</div>`;
}
async function conBoton(boton, tarea) {
  if (boton.disabled) return;
  const original = boton.innerHTML;
  boton.disabled = true;
  boton.classList.add("cargando");
  try { return await tarea(); } finally {
    boton.disabled = false;
    boton.classList.remove("cargando");
    boton.innerHTML = original;
  }
}
function cada(ms, fn) { temporizadores.push(setInterval(fn, ms)); }
function limpiarTemporizadores() { temporizadores.forEach(clearInterval); temporizadores = []; }

/* Muestra los errores de validación (422) junto a cada campo del formulario. */
function marcarErrores(form, e) {
  $$("[aria-invalid]", form).forEach((i) => i.removeAttribute("aria-invalid"));
  $$(".error-campo", form).forEach((x) => x.remove());
  const sueltos = [];
  for (const err of e.errores || []) {
    const nombre = (err.campo || "").split(".").pop();
    const input = nombre && form.elements[nombre];
    if (input) {
      input.setAttribute("aria-invalid", "true");
      input.insertAdjacentHTML("afterend", `<span class="error-campo">${esc(err.mensaje)}</span>`);
    } else sueltos.push(err.mensaje);
  }
  avisar(sueltos.length ? `${e.message} ${sueltos.join("; ")}` : e.message, "error", 9000);
}
function datosFormulario(form) {
  const d = {};
  for (const el of form.elements) {
    if (!el.name || el.disabled) continue;
    if (el.type === "number") d[el.name] = el.value === "" ? null : Number(el.value);
    else d[el.name] = el.value.trim();
  }
  return d;
}

/* ------------------------------------------------------------------ sesión */
function iniciarSesion(r) {
  sesion = { token: r.access_token, rol: r.rol, nombre: r.nombre, cliente_id: r.cliente_id, expira_en: r.expira_en };
  almacen.guardar("cm_sesion", sesion);
}
function cerrarSesion(motivo) {
  sesion = null;
  almacen.borrar("cm_sesion");
  pintarCabecera();
  if (motivo) avisar(motivo, "info", 0);
  if (!location.hash.startsWith("#/acceso")) location.hash = "#/acceso";
}
setInterval(() => {
  if (sesion && sesion.expira_en && Date.now() / 1000 > sesion.expira_en) {
    cerrarSesion("Su sesión venció. Inicie sesión de nuevo para continuar.");
  }
}, 15000);

function pintarCabecera() {
  const enlaces = [["catalogo", "Catálogo"]];
  if (esCliente()) enlaces.push(["prestamos", "Mis préstamos"], ["perfil", "Mi perfil"]);
  if (esAdmin()) enlaces.push(["prestamos", "Préstamos"], ["clientes", "Clientes"], ["inventario", "Inventario"], ["modelo", "Modelo"]);
  const actual = (location.hash.split("/")[1] || "catalogo").replace("producto", "catalogo").replace(/^prestamo$/, "prestamos");
  document.getElementById("nav").innerHTML = enlaces
    .map(([r, t]) => `<a href="#/${r}" class="${r === actual ? "activo" : ""}">${t}</a>`).join("");
  document.getElementById("sesion").innerHTML = sesion
    ? `<span>${esc(sesion.nombre || "Usuario")}</span><span class="rol">${esAdmin() ? "Administrador" : "Cliente"}</span>
       <button class="secundario" id="salir">Salir</button>`
    : `<a class="boton secundario" href="#/acceso">Ingresar</a>`;
  const salir = document.getElementById("salir");
  if (salir) salir.onclick = () => { cerrarSesion(); avisar("Sesión cerrada.", "ok"); };
}
document.getElementById("menuMovil").onclick = (ev) => {
  const nav = document.getElementById("nav");
  nav.classList.toggle("abierto");
  ev.currentTarget.setAttribute("aria-expanded", nav.classList.contains("abierto"));
};

/* ------------------------------------------------------------------ enrutador */
const RUTAS = {
  acceso: { vista: vistaAcceso },
  catalogo: { vista: vistaCatalogo },
  producto: { vista: vistaProducto },
  prestamos: { vista: vistaPrestamos, rol: "cualquiera" },
  prestamo: { vista: vistaPrestamo, rol: "cualquiera" },
  perfil: { vista: vistaPerfil, rol: "cliente" },
  clientes: { vista: vistaClientes, rol: "admin" },
  inventario: { vista: vistaInventario, rol: "admin" },
  modelo: { vista: vistaModelo, rol: "admin" },
};

async function enrutar() {
  limpiarTemporizadores();
  document.getElementById("nav").classList.remove("abierto");
  const [, nombre = "catalogo", parametro] = location.hash.split("/");
  const ruta = RUTAS[nombre] || RUTAS.catalogo;
  if (ruta.rol && !sesion) {
    avisar("Inicie sesión para continuar.", "info");
    location.hash = "#/acceso";
    return;
  }
  if ((ruta.rol === "admin" && !esAdmin()) || (ruta.rol === "cliente" && !esCliente())) {
    location.hash = "#/catalogo";
    return;
  }
  pintarCabecera();
  vista.innerHTML = cargando();
  try { await ruta.vista(parametro ? decodeURIComponent(parametro) : undefined); } catch (e) {
    if (e instanceof ErrorApi && e.estado === 401) return;
    vista.innerHTML = bloqueError(e);
  }
}
window.addEventListener("hashchange", enrutar);
window.enrutar = enrutar;

async function cargarProductos(incluirInactivos = false) {
  const lista = await api("productos", `/productos${incluirInactivos ? "?incluir_inactivos=true" : ""}`);
  for (const p of lista) productosCache[p.id] = p;
  return lista;
}
const nombreProducto = (id) => productosCache[id]?.nombre || `Producto ${id}`;

/* ================================================================== ACCESO */
async function vistaAcceso() {
  if (sesion) { location.hash = "#/catalogo"; return; }
  vista.innerHTML = `
  <div class="acceso">
    <div class="tarjeta">
      <div class="pestanas"><button class="activa" data-p="login">Iniciar sesión</button><button data-p="registro">Registrarse</button></div>
      <div id="panelAcceso"></div>
    </div>
    <p class="nota">Las cuentas de demostración están en el archivo <span class="mono">.env</span> generado al instalar
      (ver la guía de demostración). El frontend no guarda credenciales.</p>
  </div>`;
  const mostrar = (p) => {
    $$(".pestanas button").forEach((b) => b.classList.toggle("activa", b.dataset.p === p));
    p === "login" ? formLogin() : formRegistro();
  };
  $$(".pestanas button").forEach((b) => (b.onclick = () => mostrar(b.dataset.p)));
  mostrar("login");
}

function formLogin() {
  const panel = $("#panelAcceso");
  panel.innerHTML = `
    <form id="fLogin" novalidate>
      <label>Correo <input name="email" type="email" autocomplete="username" required></label>
      <label>Contraseña <input name="password" type="password" autocomplete="current-password" required></label>
      <div class="acciones"><button type="submit">Ingresar</button></div>
    </form>`;
  const f = $("#fLogin");
  f.onsubmit = async (ev) => {
    ev.preventDefault();
    if (!f.email.value || !f.password.value) { avisar("Escriba su correo y contraseña.", "error"); return; }
    await conBoton($("button[type=submit]", f), async () => {
      try {
        iniciarSesion(await api("clientes", "/auth/login", { metodo: "POST", cuerpo: { email: f.email.value.trim(), password: f.password.value } }));
        avisar(`Bienvenido, ${sesion.nombre || ""}.`, "ok");
        location.hash = esAdmin() ? "#/prestamos" : "#/catalogo";
      } catch (e) { e.estado === 422 ? marcarErrores(f, e) : avisarError(e); }
    });
  };
}

function camposFinancieros(c = {}) {
  const v = (k) => (c[k] === null || c[k] === undefined ? "" : esc(c[k]));
  return `
  <div class="campos">
    <label>Ingresos mensuales (COP)<input name="ingresos_mensuales_cop" type="number" min="0" step="1000" required value="${v("ingresos_mensuales_cop")}"></label>
    <label>Obligaciones mensuales (COP)<input name="obligaciones_mensuales_cop" type="number" min="0" step="1000" required value="${v("obligaciones_mensuales_cop")}"></label>
    <label>Tipo de empleo<select name="tipo_empleo">${EMPLEOS.map((e) => `<option ${c.tipo_empleo === e ? "selected" : ""}>${e}</option>`).join("")}</select></label>
    <label>Antigüedad laboral (meses)<input name="antiguedad_laboral_meses" type="number" min="0" max="600" required value="${v("antiguedad_laboral_meses")}"></label>
    <label>Historial crediticio (meses)<input name="meses_historial_crediticio" type="number" min="0" max="600" required value="${v("meses_historial_crediticio")}"></label>
    <label>Créditos previos<input name="creditos_previos" type="number" min="0" max="100" required value="${v("creditos_previos")}"></label>
    <label>Atrasos previos de 30+ días<input name="atrasos_previos_30d" type="number" min="0" max="100" required value="${v("atrasos_previos_30d")}"></label>
    <label>Pago puntual (%) <small>vacío si no tiene créditos previos</small><input name="porcentaje_pago_puntual" type="number" min="0" max="100" step="0.01" value="${v("porcentaje_pago_puntual")}"></label>
  </div>`;
}
function enlazarHistorial(f) {
  const ajustar = () => {
    const sinCreditos = Number(f.creditos_previos.value || 0) === 0;
    f.porcentaje_pago_puntual.disabled = sinCreditos;
    if (sinCreditos) f.porcentaje_pago_puntual.value = "";
  };
  f.creditos_previos.addEventListener("input", ajustar);
  ajustar();
}
function datosFinancieros(f) {
  const d = datosFormulario(f);
  if (!("porcentaje_pago_puntual" in d)) d.porcentaje_pago_puntual = null;
  return d;
}

function formRegistro() {
  const panel = $("#panelAcceso");
  panel.innerHTML = `
    <form id="fRegistro" novalidate>
      <div class="campos">
        <label>Nombre completo<input name="nombre" required minlength="2"></label>
        <label>Documento <small>6 a 12 dígitos</small><input name="documento" inputmode="numeric" required pattern="[0-9]{6,12}"></label>
        <label>Correo<input name="email" type="email" required autocomplete="email"></label>
        <label>Contraseña <small>mínimo 8 caracteres</small><input name="password" type="password" minlength="8" required autocomplete="new-password"></label>
      </div>
      <h3>Información financiera y laboral</h3>
      ${camposFinancieros({ tipo_empleo: "dependiente", creditos_previos: 0, atrasos_previos_30d: 0 })}
      <div class="acciones"><button type="submit">Crear cuenta</button></div>
    </form>`;
  const f = $("#fRegistro");
  enlazarHistorial(f);
  f.onsubmit = async (ev) => {
    ev.preventDefault();
    if (!f.checkValidity()) { f.reportValidity(); return; }
    await conBoton($("button[type=submit]", f), async () => {
      try {
        iniciarSesion(await api("clientes", "/auth/registro", { metodo: "POST", cuerpo: datosFinancieros(f) }));
        avisar("Cuenta creada. Ya puede solicitar financiación.", "ok");
        location.hash = "#/catalogo";
      } catch (e) { e.estado === 422 ? marcarErrores(f, e) : avisarError(e); }
    });
  };
}

/* ================================================================== CATÁLOGO */
async function vistaCatalogo() {
  const [productos, existencias] = await Promise.all([
    cargarProductos(esAdmin()),
    api("inventario", "/existencias").catch(() => null),
  ]);
  const stock = Object.fromEntries((existencias || []).map((e) => [e.producto_id, e]));
  vista.innerHTML = `
    <div class="encabezado">
      <div><h1>Catálogo</h1><p class="suave">Precios vigentes del servicio de Productos. ${esCliente() ? "Elija un producto para solicitar financiación." : ""}</p></div>
      ${esAdmin() ? '<button id="btnNuevo">Nuevo producto</button>' : ""}
    </div>
    ${existencias ? "" : '<div class="aviso info">Inventario no respondió: las existencias no se muestran, el catálogo sí.</div>'}
    <div id="nuevoProducto"></div>
    <div class="rejilla">${productos.map((p) => `
      <article class="tarjeta producto" data-id="${p.id}" tabindex="0" role="link" aria-label="${esc(p.nombre)}">
        <span class="categoria">${esc(p.categoria)}${p.activo ? "" : " · <b style='color:var(--error)'>inactivo</b>"}</span>
        <h3>${esc(p.nombre)}</h3>
        <span class="suave">${esc(p.descripcion)}</span>
        <span class="precio">${cop(p.precio)}</span>
        <span class="suave">${stock[p.id] ? `${stock[p.id].disponible} disponibles` : existencias ? "Sin existencias" : "Existencias no disponibles"} · versión ${p.version}</span>
      </article>`).join("") || '<div class="vacio">No hay productos.</div>'}
    </div>
    ${esAdmin() ? '<div id="metricasCache"></div>' : ""}`;
  $$(".producto").forEach((el) => {
    const ir = () => { location.hash = `#/producto/${el.dataset.id}`; };
    el.onclick = ir;
    el.onkeydown = (ev) => { if (ev.key === "Enter") ir(); };
  });
  if (esAdmin()) {
    $("#btnNuevo").onclick = () => formProducto($("#nuevoProducto"));
    pintarMetricasCache();
  }
}

function formProducto(contenedor, p = null) {
  contenedor.innerHTML = `
    <div class="tarjeta">
      <h2>${p ? "Editar producto" : "Nuevo producto"}</h2>
      <form id="fProducto" novalidate>
        <div class="campos">
          ${p ? "" : '<label>SKU<input name="sku" required minlength="3" maxlength="40"></label>'}
          <label>Nombre<input name="nombre" required minlength="2" value="${esc(p?.nombre)}"></label>
          <label>Categoría<select name="categoria">${["computadores", "celulares", "tablets", "accesorios", "otros"]
            .map((c) => `<option ${p?.categoria === c ? "selected" : ""}>${c}</option>`).join("")}</select></label>
          <label>Precio (COP)<input name="precio" type="number" min="1" step="1000" required value="${esc(p?.precio)}"></label>
        </div>
        <label>Descripción<textarea name="descripcion" rows="2" maxlength="500">${esc(p?.descripcion)}</textarea></label>
        <div class="acciones"><button type="submit">${p ? "Guardar cambios" : "Crear producto"}</button>
          <button type="button" class="secundario" id="cancelarProducto">Cancelar</button></div>
        ${p ? `<p class="suave">Se envía la versión ${p.version} como control de concurrencia: si otro administrador cambió el producto, se recibirá un conflicto (409).</p>` : ""}
      </form>
    </div>`;
  const f = $("#fProducto", contenedor);
  $("#cancelarProducto", contenedor).onclick = () => { contenedor.innerHTML = ""; };
  f.onsubmit = async (ev) => {
    ev.preventDefault();
    if (!f.checkValidity()) { f.reportValidity(); return; }
    const d = datosFormulario(f);
    await conBoton($("button[type=submit]", f), async () => {
      try {
        if (p) {
          const r = await api("productos", `/productos/${p.id}`, { metodo: "PUT", cuerpo: { ...d, version_esperada: p.version } });
          avisar(`Producto actualizado (versión ${r.version}). La caché se invalidó tras confirmar la escritura.`, "ok");
        } else {
          const r = await api("productos", "/productos", { metodo: "POST", cuerpo: d });
          avisar(`Producto ${r.nombre} creado. Registre existencias en Inventario para poder financiarlo.`, "ok");
        }
        enrutar();
      } catch (e) { e.estado === 422 ? marcarErrores(f, e) : avisarError(e); }
    });
  };
}

async function pintarMetricasCache() {
  const caja = $("#metricasCache");
  if (!caja) return;
  try {
    const m = await api("productos", "/cache/metricas");
    const l = m.latencia_ms || {};
    const fila = (k, t) => `<tr><td>${t}</td><td class="num">${l[k]?.n ?? 0}</td><td class="num">${l[k]?.p50 ?? "—"}</td><td class="num">${l[k]?.p95 ?? "—"}</td></tr>`;
    caja.innerHTML = `
      <div class="tarjeta" style="margin-top:1rem">
        <div class="encabezado"><div><h2>Caché Redis del catálogo</h2>
          <p class="suave">Clave <span class="mono">productos:v1:{id}</span>, TTL ${m.ttl_segundos} s. Redis ${m.redis_disponible ? "disponible" : "<b>no disponible</b> (se responde desde PostgreSQL)"}.</p></div>
          <div class="acciones"><button class="secundario" id="refrescarCache">Actualizar</button><button class="secundario" id="reiniciarCache">Reiniciar métricas</button></div></div>
        <div class="cifras">
          <div class="cifra"><b>${pct(m.tasa_aciertos)}</b><span>tasa de aciertos</span></div>
          <div class="cifra"><b>${m.hits}</b><span>hits</span></div>
          <div class="cifra"><b>${m.misses}</b><span>misses</span></div>
          <div class="cifra"><b>${m.invalidaciones}</b><span>invalidaciones</span></div>
          <div class="cifra"><b>${m.errores_redis}</b><span>errores de Redis</span></div>
        </div>
        <div class="tabla-env"><table><thead><tr><th>Lectura</th><th class="num">n</th><th class="num">p50 (ms)</th><th class="num">p95 (ms)</th></tr></thead>
          <tbody>${fila("hit", "HIT (Redis)")}${fila("miss", "MISS (PostgreSQL + guardar)")}${fila("sin_cache", "Sin Redis (PostgreSQL)")}</tbody></table></div>
      </div>`;
    $("#refrescarCache").onclick = pintarMetricasCache;
    $("#reiniciarCache").onclick = async () => { await api("productos", "/cache/metricas/reiniciar", { metodo: "POST" }); pintarMetricasCache(); };
  } catch (e) { caja.innerHTML = `<div class="aviso error">Métricas de caché: ${esc(e.message)}</div>`; }
}

/* ================================================================== PRODUCTO Y SOLICITUD */
async function leerProductoConCache(id) {
  try {
    const r = await api("productos", `/productos/${id}`, { completo: true });
    return { producto: r.datos, cache: r.cabeceras.get("X-Cache"), latencia: r.cabeceras.get("X-Cache-Latencia-ms") };
  } catch (e) {
    if (e.estado === 404 && esAdmin()) {  // un administrador puede ver y reactivar productos inactivos
      const lista = await cargarProductos(true);
      const p = lista.find((x) => String(x.id) === String(id));
      if (p) return { producto: p, cache: null };
    }
    throw e;
  }
}

async function vistaProducto(id) {
  const [{ producto: p, cache, latencia }, stock] = await Promise.all([
    leerProductoConCache(id),
    api("inventario", `/existencias/${id}`).catch(() => null),
  ]);
  vista.innerHTML = `
    <p><a href="#/catalogo">← Catálogo</a></p>
    <div class="dos">
      <div>
        <div class="tarjeta">
          <span class="categoria">${esc(p.categoria)} · SKU ${esc(p.sku)}</span>
          <h1>${esc(p.nombre)}</h1>
          <p class="suave">${esc(p.descripcion)}</p>
          <p class="precio" style="font-size:1.6rem;font-weight:700;margin:.5rem 0">${cop(p.precio)}</p>
          <dl class="kv">
            <dt>Versión de precio</dt><dd>${p.version}</dd>
            <dt>Existencias</dt><dd>${stock ? `${stock.disponible} disponibles · ${stock.reservado} reservadas` : "Inventario no respondió"}</dd>
            <dt>Estado</dt><dd>${p.activo ? "Activo" : "<b>Inactivo</b>"}</dd>
            ${cache ? `<dt>Caché</dt><dd><span class="chip cache-${esc(cache)}">${esc(cache)}</span> <span class="suave">${Number(latencia).toFixed(2)} ms en el servidor</span>
              <button class="secundario" id="releer" style="margin-left:.5rem;padding:.2rem .6rem">Consultar otra vez</button></dd>` : ""}
          </dl>
        </div>
        <div id="adminProducto"></div>
      </div>
      <div id="panelSolicitud"></div>
    </div>`;
  const releer = $("#releer");
  if (releer) releer.onclick = () => enrutar();

  if (esAdmin()) {
    const caja = $("#adminProducto");
    caja.innerHTML = `<div class="tarjeta"><h2>Administración</h2><div class="acciones">
      <button id="editar">Editar / cambiar precio</button>
      ${p.activo ? '<button class="peligro" id="desactivar">Desactivar</button>' : '<button class="secundario" id="reactivar">Reactivar</button>'}
      <a class="boton secundario" href="#/inventario">Registrar existencias</a></div><div id="formEdicion"></div></div>`;
    $("#editar").onclick = () => formProducto($("#formEdicion"), p);
    const des = $("#desactivar");
    if (des) des.onclick = () => conBoton(des, async () => {
      try { await api("productos", `/productos/${p.id}`, { metodo: "DELETE" }); avisar("Producto desactivado; la caché se invalidó.", "ok"); enrutar(); }
      catch (e) { avisarError(e); }
    });
    const rea = $("#reactivar");
    if (rea) rea.onclick = () => conBoton(rea, async () => {
      try { await api("productos", `/productos/${p.id}/reactivar`, { metodo: "POST" }); avisar("Producto reactivado.", "ok"); enrutar(); }
      catch (e) { avisarError(e); }
    });
  }
  pintarSolicitud($("#panelSolicitud"), p, stock);
}

/* La clave de idempotencia se crea por borrador (producto + cantidad + plazo) y se conserva hasta obtener
 * una respuesta definitiva: si la red falla o la sesión vence, el reintento usa la misma clave. */
function claveIdempotencia(borrador) {
  const guardado = almacen.leer("cm_borrador");
  if (guardado && guardado.firma === borrador) return guardado.clave;
  const clave = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  almacen.guardar("cm_borrador", { firma: borrador, clave });
  return clave;
}

function pintarSolicitud(caja, p, stock) {
  if (!p.activo) { caja.innerHTML = '<div class="tarjeta"><p>Este producto no está disponible para financiación.</p></div>'; return; }
  if (!sesion) {
    caja.innerHTML = `<div class="tarjeta"><h2>Solicitar financiación</h2><p>Inicie sesión como cliente para solicitar.</p>
      <a class="boton" href="#/acceso">Ingresar</a></div>`;
    return;
  }
  if (!esCliente()) {
    caja.innerHTML = '<div class="tarjeta"><h2>Solicitar financiación</h2><p class="suave">Las solicitudes las hace un cliente. El administrador las gestiona en Préstamos e Inventario.</p></div>';
    return;
  }
  caja.innerHTML = `
    <div class="tarjeta">
      <h2>Solicitar financiación</h2>
      <form id="fSolicitud" novalidate>
        <div class="campos">
          <label>Cantidad<input name="cantidad" type="number" min="1" max="${CANTIDAD_MAX}" value="1" required></label>
          <label>Plazo<select name="plazo_meses">${PLAZOS.map((m) => `<option value="${m}" ${m === 12 ? "selected" : ""}>${m} meses</option>`).join("")}</select></label>
        </div>
        <p>Monto de referencia: <b id="montoRef">${cop(p.precio)}</b><br>
          <small class="suave">El monto final lo calcula Préstamos con la cotización vigente del servicio de Productos.</small></p>
        ${stock && stock.disponible === 0 ? '<p class="aviso info" style="margin:0">Hoy no hay unidades disponibles: si el riesgo es favorable, la solicitud terminará como “Sin existencias”.</p>' : ""}
        <div class="acciones"><button type="submit" id="btnSolicitar">Enviar solicitud</button></div>
        <p class="nota">La evaluación de riesgo es automática. Un riesgo favorable <b>no</b> es una aprobación: el préstamo se aprueba solo cuando Inventario confirma la reserva.</p>
      </form>
    </div>`;
  const f = $("#fSolicitud");
  const actualizar = () => {
    const n = Number(f.cantidad.value || 0);
    $("#montoRef").textContent = n >= 1 && n <= CANTIDAD_MAX ? cop(p.precio * n) : "—";
  };
  f.cantidad.addEventListener("input", actualizar);
  f.onsubmit = async (ev) => {
    ev.preventDefault();
    const cantidad = Number(f.cantidad.value);
    if (!Number.isInteger(cantidad) || cantidad < 1 || cantidad > CANTIDAD_MAX) {
      marcarErrores(f, new ErrorApi(422, "entrada_invalida", "Revise el formulario.", [{ campo: "cantidad", mensaje: `Debe ser un entero entre 1 y ${CANTIDAD_MAX}.` }]));
      return;
    }
    const cuerpo = { producto_id: p.id, cantidad, plazo_meses: Number(f.plazo_meses.value) };
    const firma = JSON.stringify(cuerpo);
    const clave = claveIdempotencia(firma);
    await conBoton($("#btnSolicitar"), async () => {
      try {
        const r = await api("prestamos", "/prestamos", { metodo: "POST", cuerpo, cabeceras: { "Idempotency-Key": clave } });
        almacen.borrar("cm_borrador");
        avisar(r.estado === "RECHAZADA"
          ? `Solicitud ${corto(r.id)} registrada: fue rechazada. Vea el motivo en el seguimiento.`
          : `Solicitud ${corto(r.id)} recibida. Decisión pendiente: esperando la reserva de inventario.`, r.estado === "RECHAZADA" ? "error" : "ok");
        location.hash = `#/prestamo/${r.id}`;
      } catch (e) {
        if (e.estado === 422) { almacen.borrar("cm_borrador"); marcarErrores(f, e); }
        else if (e.estado === 409) { almacen.borrar("cm_borrador"); avisarError(e); }
        else if (e.estado !== 401) avisar(`${e.message} Puede reintentar: se reutilizará la misma clave de idempotencia y no se duplicará la solicitud.`, "error", 0);
      }
    });
  };
}

/* ================================================================== PRÉSTAMOS */
async function vistaPrestamos() {
  const productos = Object.keys(productosCache).length ? null : cargarProductos(esAdmin()).catch(() => null);
  vista.innerHTML = `
    <div class="encabezado">
      <div><h1>${esAdmin() ? "Préstamos" : "Mis préstamos"}</h1>
        <p class="suave">Se actualiza automáticamente mientras haya solicitudes en curso.</p></div>
      <label style="min-width:220px">Estado<select id="filtroEstado"><option value="">Todos</option>
        ${Object.entries(ESTADOS).map(([k, [, t]]) => `<option value="${k}">${t}</option>`).join("")}</select></label>
    </div>
    <div class="tarjeta"><div id="tablaPrestamos">${cargando()}</div></div>`;
  await productos;
  const filtro = $("#filtroEstado");
  filtro.value = almacen.leer("cm_filtro") || "";
  filtro.onchange = () => { almacen.guardar("cm_filtro", filtro.value); pintar(); };
  async function pintar() {
    const caja = $("#tablaPrestamos");
    if (!caja) return;
    let lista;
    try { lista = await api("prestamos", `/prestamos?limite=100${filtro.value ? `&estado=${filtro.value}` : ""}`); }
    catch (e) { if (e.estado !== 401) caja.innerHTML = `<div class="aviso error" style="margin:0">${esc(e.message)} Se reintentará automáticamente.</div>`; return; }
    caja.innerHTML = lista.length ? `<div class="tabla-env"><table>
      <thead><tr><th>Fecha</th><th>Solicitud</th>${esAdmin() ? "<th>Cliente</th>" : ""}<th>Producto</th><th class="num">Monto</th><th>Plazo</th><th>Riesgo</th><th>Estado</th></tr></thead>
      <tbody>${lista.map((p) => `<tr class="clic" data-id="${p.id}">
        <td>${fecha(p.creado_en)}</td><td class="mono">${corto(p.id)}</td>${esAdmin() ? `<td>${p.cliente_id}</td>` : ""}
        <td>${esc(nombreProducto(p.producto_id))} × ${p.cantidad}</td><td class="num">${cop(p.monto)}</td><td>${p.plazo_meses} m</td>
        <td>${p.decision_riesgo ? esc(p.decision_riesgo.toLowerCase()) : "—"}</td><td>${chipEstado(p.estado)}</td></tr>`).join("")}</tbody></table></div>`
      : `<div class="vacio">No hay solicitudes${filtro.value ? " con ese estado" : ""}. ${esCliente() ? '<a href="#/catalogo">Ir al catálogo</a>' : ""}</div>`;
    $$("tr.clic", caja).forEach((tr) => (tr.onclick = () => { location.hash = `#/prestamo/${tr.dataset.id}`; }));
  }
  await pintar();
  cada(3000, pintar);
}

async function vistaPrestamo(id) {
  if (!Object.keys(productosCache).length) await cargarProductos(esAdmin()).catch(() => null);
  let ultimoEstado = null;
  async function pintar() {
    let p;
    try { p = await api("prestamos", `/prestamos/${id}`); }
    catch (e) {
      if (e.estado === 401) return;
      if (ultimoEstado === null) { vista.innerHTML = bloqueError(e); limpiarTemporizadores(); return; }
      avisar(`${e.message} Se reintentará automáticamente.`, "error", 4000);
      return;
    }
    if (ultimoEstado && ultimoEstado !== p.estado) avisar(`La solicitud ${corto(p.id)} cambió a: ${ESTADOS[p.estado]?.[1] || p.estado}.`, "ok");
    ultimoEstado = p.estado;
    const puedeCancelar = CANCELABLES.includes(p.estado);
    const puedeEntregar = esAdmin() && p.estado === "APROBADA";
    const marcador = p.umbral !== null ? `<i style="left:${Math.min(100, p.umbral * 100)}%" title="Umbral ${p.umbral}"></i>` : "";
    vista.innerHTML = `
      <p><a href="#/prestamos">← ${esAdmin() ? "Préstamos" : "Mis préstamos"}</a></p>
      <div class="encabezado"><div><h1>Solicitud <span class="mono">${corto(p.id)}</span></h1>
        <p class="suave mono">${esc(p.id)}</p></div>
        <div class="acciones">
          ${puedeEntregar ? '<button id="entregar">Confirmar entrega</button>' : ""}
          ${puedeCancelar ? '<button class="peligro" id="cancelar">Cancelar solicitud</button>' : ""}
        </div></div>
      <div class="dos">
        <div>
          <div class="tarjeta">
            <p style="font-size:1.1rem">${chipEstado(p.estado)} ${ESTADOS[p.estado]?.[0] === "espera" ? '<span class="suave">· actualizando automáticamente…</span>' : ""}</p>
            <p>${esc(p.descripcion_estado)}</p>
            ${p.motivo ? `<p><b>Motivo:</b> ${esc(p.motivo)}</p>` : ""}
            ${p.estado === "PENDIENTE_INVENTARIO" ? '<p class="nota">El riesgo fue favorable, pero el préstamo <b>todavía no está aprobado</b>: falta que Inventario confirme la reserva.</p>' : ""}
          </div>
          <div class="tarjeta"><h2>Historial</h2><ol class="linea">${(p.historial || []).map((h) => `
            <li class="${h.aplicado ? "" : "ignorado"}"><time>${fecha(h.creado_en)}</time>
              ${h.estado_anterior ? `${esc(h.estado_anterior)} → ` : ""}<b>${esc(h.estado_nuevo)}</b>${h.aplicado ? "" : " (evento ignorado)"}
              <div class="suave">${esc(h.motivo)}</div></li>`).join("")}</ol></div>
        </div>
        <div>
          <div class="tarjeta"><h2>Cotización</h2><dl class="kv">
            <dt>Producto</dt><dd>${esc(nombreProducto(p.producto_id))}</dd>
            <dt>Cantidad</dt><dd>${p.cantidad}</dd>
            <dt>Precio unitario</dt><dd>${cop(p.precio_unitario)} <span class="suave">(versión ${p.version_cotizacion})</span></dd>
            <dt>Monto</dt><dd><b>${cop(p.monto)}</b></dd>
            <dt>Plazo</dt><dd>${p.plazo_meses} meses</dd>
            ${esAdmin() ? `<dt>Cliente</dt><dd>${p.cliente_id}</dd>` : ""}
            <dt>Creada</dt><dd>${fecha(p.creado_en)}</dd>
          </dl></div>
          <div class="tarjeta"><h2>Evaluación de riesgo</h2>
            ${p.probabilidad !== null && p.probabilidad !== undefined ? `
            <p>Probabilidad estimada de incumplimiento: <b>${pct(p.probabilidad)}</b> (umbral ${pct(p.umbral, 0)})</p>
            <div class="barra-prob"><span style="width:${(p.probabilidad * 100).toFixed(1)}%"></span>${marcador}</div>
            <dl class="kv"><dt>Decisión de riesgo</dt><dd>${esc(p.decision_riesgo)}</dd>
              <dt>Modelo</dt><dd>${esc(p.model_version)}</dd></dl>
            <p class="suave" style="margin-top:.75rem">La decisión de riesgo no es la aprobación final; los datos son sintéticos.</p>`
            : `<p class="suave">No se ejecutó el modelo: ${esc(p.motivo || "la solicitud no cumplió una regla previa")}.</p>`}
          </div>
          ${p.reserva_id ? `<div class="tarjeta"><h2>Reserva</h2><dl class="kv"><dt>Reserva</dt><dd class="mono">${esc(p.reserva_id)}</dd>
            <dt>Vence</dt><dd>${fecha(p.reserva_expira_en)}</dd></dl></div>` : ""}
          ${esAdmin() ? '<div class="tarjeta"><h2>Mensajes</h2><div id="mensajes"><button class="secundario" id="verMensajes">Ver comandos y eventos</button></div></div>' : ""}
        </div>
      </div>`;
    const c = $("#cancelar");
    if (c) c.onclick = () => conBoton(c, async () => {
      try { await api("prestamos", `/prestamos/${p.id}/cancelar`, { metodo: "POST" }); avisar("Cancelación solicitada: se está liberando la reserva.", "ok"); pintar(); }
      catch (e) { avisarError(e); }
    });
    const en = $("#entregar");
    if (en) en.onclick = () => conBoton(en, async () => {
      try { await api("prestamos", `/prestamos/${p.id}/entregar`, { metodo: "POST" }); avisar("Entrega solicitada: Inventario confirmará la salida.", "ok"); pintar(); }
      catch (e) { avisarError(e); }
    });
    const vm = $("#verMensajes");
    if (vm) vm.onclick = () => pintarMensajes(p.id);
    if (p.es_final) limpiarTemporizadores();
  }
  await pintar();
  if (ultimoEstado && !ESTADOS_FINALES.has(ultimoEstado)) cada(2000, pintar);
}
const ESTADOS_FINALES = new Set(["RECHAZADA", "CANCELADA_SIN_STOCK", "COMPLETADA", "CANCELADA", "VENCIDA"]);

async function pintarMensajes(id) {
  const caja = $("#mensajes");
  try {
    const [m, inv] = await Promise.all([api("prestamos", `/prestamos/${id}/mensajes`),
      api("inventario", `/mensajeria/outbox?prestamo_id=${id}`).catch(() => [])]);
    const filas = [
      ...m.enviados.map((e) => ({ t: e.mensaje.occurred_at, d: "Préstamos → Inventario", tipo: e.mensaje.type, id: e.mensaje.event_id, ok: e.publicado_en ? "publicado" : "en outbox" })),
      ...inv.map((e) => ({ t: e.mensaje.occurred_at, d: "Inventario → Préstamos", tipo: e.mensaje.type, id: e.mensaje.event_id, ok: e.publicado_en ? "publicado" : "en outbox" })),
    ].sort((a, b) => a.t.localeCompare(b.t));
    const recibidos = new Set(m.recibidos.map((r) => r.event_id));
    caja.innerHTML = `<div class="tabla-env"><table><thead><tr><th>Tipo</th><th>Sentido</th><th>event_id</th><th>Outbox</th><th>Procesado</th></tr></thead><tbody>
      ${filas.map((f) => `<tr><td>${esc(f.tipo)}</td><td>${f.d}</td><td class="mono">${corto(f.id)}</td><td>${f.ok}</td>
        <td>${f.d.startsWith("Inventario") ? (recibidos.has(f.id) ? "sí" : "no") : "—"}</td></tr>`).join("")}</tbody></table></div>`;
  } catch (e) { caja.innerHTML = `<div class="aviso error" style="margin:0">${esc(e.message)}</div>`; }
}

/* ================================================================== PERFIL */
async function vistaPerfil() {
  const c = await api("clientes", "/clientes/me");
  vista.innerHTML = `
    <div class="encabezado"><div><h1>Mi perfil</h1><p class="suave">Estos datos los usa Préstamos al evaluar cada solicitud.</p></div></div>
    <div class="tarjeta"><dl class="kv"><dt>Correo</dt><dd>${esc(c.email)}</dd><dt>Documento</dt><dd>${esc(c.documento)}</dd>
      <dt>Identificador</dt><dd class="mono">${esc(c.cliente_externo_id)}</dd></dl></div>
    <div class="tarjeta"><form id="fPerfil" novalidate>
      <label>Nombre<input name="nombre" required minlength="2" value="${esc(c.nombre)}"></label>
      ${camposFinancieros(c)}
      <div class="acciones"><button type="submit">Guardar</button></div></form></div>`;
  const f = $("#fPerfil");
  enlazarHistorial(f);
  f.onsubmit = async (ev) => {
    ev.preventDefault();
    if (!f.checkValidity()) { f.reportValidity(); return; }
    await conBoton($("button[type=submit]", f), async () => {
      try { await api("clientes", "/clientes/me", { metodo: "PUT", cuerpo: datosFinancieros(f) }); avisar("Datos actualizados.", "ok"); }
      catch (e) { e.estado === 422 ? marcarErrores(f, e) : avisarError(e); }
    });
  };
}

/* ================================================================== CLIENTES (admin) */
async function vistaClientes() {
  vista.innerHTML = `
    <div class="encabezado"><div><h1>Clientes</h1></div></div>
    <div class="tarjeta"><h2>Importar clientes desde CSV</h2>
      <p class="suave">Clave externa única: columna <span class="mono">cliente_id</span>. Si existe y los datos cambiaron se actualiza; si son iguales queda “sin cambios”; las filas inválidas se rechazan con su detalle. Importar dos veces el mismo archivo no duplica clientes.</p>
      <form id="fImportar"><div class="acciones"><input type="file" name="archivo" accept=".csv,text/csv" required style="max-width:340px">
        <button type="submit">Importar</button></div></form>
      <div id="resultadoImportacion"></div>
    </div>
    <div class="tarjeta"><div class="encabezado" style="margin-bottom:.5rem"><h2 style="margin:0">Listado</h2>
      <form id="fBuscar" style="display:flex;gap:.5rem"><input name="q" placeholder="Nombre, correo o identificador"><button class="secundario">Buscar</button></form></div>
      <div id="edicionCliente"></div><div id="tablaClientes">${cargando()}</div></div>
    <div class="tarjeta"><h2>Importaciones recientes</h2><div id="historialImportaciones">${cargando()}</div></div>`;
  let desde = 0;
  const f = $("#fImportar");
  f.onsubmit = async (ev) => {
    ev.preventDefault();
    if (!f.archivo.files.length) { avisar("Elija un archivo CSV.", "error"); return; }
    const datos = new FormData();
    datos.append("archivo", f.archivo.files[0]);
    await conBoton($("button", f), async () => {
      try {
        const r = await api("clientes", "/clientes/importar", { metodo: "POST", formulario: datos });
        $("#resultadoImportacion").innerHTML = `
          <div class="cifras" style="margin-top:1rem">
            <div class="cifra"><b>${r.filas}</b><span>filas procesadas</span></div>
            <div class="cifra"><b>${r.insertados}</b><span>insertados</span></div>
            <div class="cifra"><b>${r.actualizados}</b><span>actualizados</span></div>
            <div class="cifra"><b>${r.sin_cambios}</b><span>sin cambios</span></div>
            <div class="cifra"><b>${r.rechazados}</b><span>rechazados</span></div>
          </div>
          ${r.errores.length ? `<h3 style="margin-top:1rem">Errores por fila</h3><div class="tabla-env"><table><thead><tr><th>Fila</th><th>cliente_id</th><th>Errores</th></tr></thead>
            <tbody>${r.errores.map((x) => `<tr><td>${x.fila}</td><td>${esc(x.cliente_id || "—")}</td><td>${x.errores.map(esc).join("<br>")}</td></tr>`).join("")}</tbody></table></div>` : ""}`;
        avisar(`Importación terminada: ${r.insertados} insertados, ${r.actualizados} actualizados, ${r.sin_cambios} sin cambios, ${r.rechazados} rechazados.`, r.rechazados ? "info" : "ok");
        f.reset();
        pintarTabla();
        pintarHistorial();
      } catch (e) { avisarError(e); }
    });
  };
  $("#fBuscar").onsubmit = (ev) => { ev.preventDefault(); desde = 0; pintarTabla(); };

  async function pintarTabla() {
    const q = $("#fBuscar").q.value.trim();
    const caja = $("#tablaClientes");
    try {
      const r = await api("clientes", `/clientes?limite=25&desde=${desde}${q ? `&buscar=${encodeURIComponent(q)}` : ""}`);
      caja.innerHTML = `<div class="tabla-env"><table><thead><tr><th>Id</th><th>Identificador</th><th>Nombre</th><th>Correo</th><th class="num">Ingresos</th><th>Empleo</th><th>Activo</th><th></th></tr></thead>
        <tbody>${r.clientes.map((c) => `<tr><td>${c.id}</td><td class="mono">${esc(c.cliente_externo_id)}</td><td>${esc(c.nombre)}</td><td>${esc(c.email || "—")}</td>
          <td class="num">${cop(c.ingresos_mensuales_cop)}</td><td>${esc(c.tipo_empleo)}</td>
          <td><button class="secundario" data-activo="${c.id}" data-valor="${!c.activo}" style="padding:.2rem .6rem">${c.activo ? "Sí · desactivar" : "No · activar"}</button></td>
          <td><button class="secundario" data-editar="${c.id}" style="padding:.2rem .6rem">Editar</button></td></tr>`).join("")}</tbody></table></div>
        <div class="acciones" style="margin-top:.75rem"><span class="suave">${r.total} clientes · mostrando ${r.total ? desde + 1 : 0}–${Math.min(desde + 25, r.total)}</span>
          <button class="secundario" id="anterior" ${desde === 0 ? "disabled" : ""}>Anterior</button>
          <button class="secundario" id="siguiente" ${desde + 25 >= r.total ? "disabled" : ""}>Siguiente</button></div>`;
      $("#anterior").onclick = () => { desde = Math.max(0, desde - 25); pintarTabla(); };
      $("#siguiente").onclick = () => { desde += 25; pintarTabla(); };
      $$("[data-activo]", caja).forEach((b) => (b.onclick = () => conBoton(b, async () => {
        try { await api("clientes", `/clientes/${b.dataset.activo}/activo?activo=${b.dataset.valor}`, { metodo: "PATCH" }); pintarTabla(); }
        catch (e) { avisarError(e); }
      })));
      $$("[data-editar]", caja).forEach((b) => (b.onclick = () => editarCliente(r.clientes.find((c) => String(c.id) === b.dataset.editar))));
    } catch (e) { caja.innerHTML = `<div class="aviso error" style="margin:0">${esc(e.message)}</div>`; }
  }
  function editarCliente(c) {
    const caja = $("#edicionCliente");
    caja.innerHTML = `<div class="tarjeta" style="background:var(--superficie-2)"><h3>Editar cliente ${c.id} · ${esc(c.cliente_externo_id)}</h3>
      <form id="fEditarCliente" novalidate><label>Nombre<input name="nombre" required value="${esc(c.nombre)}"></label>${camposFinancieros(c)}
      <div class="acciones"><button type="submit">Guardar</button><button type="button" class="secundario" id="cerrarEdicion">Cerrar</button></div></form></div>`;
    const fe = $("#fEditarCliente");
    enlazarHistorial(fe);
    $("#cerrarEdicion").onclick = () => { caja.innerHTML = ""; };
    fe.onsubmit = async (ev) => {
      ev.preventDefault();
      await conBoton($("button[type=submit]", fe), async () => {
        try { await api("clientes", `/clientes/${c.id}`, { metodo: "PUT", cuerpo: datosFinancieros(fe) }); avisar("Cliente actualizado.", "ok"); caja.innerHTML = ""; pintarTabla(); }
        catch (e) { e.estado === 422 ? marcarErrores(fe, e) : avisarError(e); }
      });
    };
    caja.scrollIntoView({ behavior: "smooth" });
  }
  async function pintarHistorial() {
    const caja = $("#historialImportaciones");
    try {
      const h = await api("clientes", "/clientes-importaciones");
      caja.innerHTML = h.length ? `<div class="tabla-env"><table><thead><tr><th>Fecha</th><th>Archivo</th><th class="num">Filas</th><th class="num">Insertados</th><th class="num">Actualizados</th><th class="num">Sin cambios</th><th class="num">Rechazados</th></tr></thead>
        <tbody>${h.map((x) => `<tr><td>${fecha(x.realizada_en)}</td><td>${esc(x.archivo)}</td><td class="num">${x.filas}</td><td class="num">${x.insertados}</td><td class="num">${x.actualizados}</td><td class="num">${x.sin_cambios}</td><td class="num">${x.rechazados}</td></tr>`).join("")}</tbody></table></div>`
        : '<div class="vacio">Aún no hay importaciones.</div>';
    } catch (e) { caja.innerHTML = `<div class="aviso error" style="margin:0">${esc(e.message)}</div>`; }
  }
  await Promise.all([pintarTabla(), pintarHistorial()]);
}

/* ================================================================== INVENTARIO (admin) */
async function vistaInventario() {
  await cargarProductos(true);
  vista.innerHTML = `
    <div class="encabezado"><div><h1>Inventario</h1><p class="suave">Existencias, reservas y movimientos del servicio de Inventario.</p></div></div>
    <div class="pestanas">${[["existencias", "Existencias"], ["entregas", "Entregas y cancelaciones"], ["reservas", "Reservas"], ["movimientos", "Movimientos"], ["mensajeria", "Mensajería"]]
      .map(([k, t]) => `<button data-t="${k}">${t}</button>`).join("")}</div>
    <div id="panelInventario"></div>`;
  const paneles = { existencias: panelExistencias, entregas: panelEntregas, reservas: panelReservas, movimientos: panelMovimientos, mensajeria: panelMensajeria };
  const abrir = async (k) => {
    limpiarTemporizadores();
    almacen.guardar("cm_tab_inv", k);
    $$(".pestanas button").forEach((b) => b.classList.toggle("activa", b.dataset.t === k));
    const caja = $("#panelInventario");
    caja.innerHTML = cargando();
    try { await paneles[k](caja); } catch (e) { if (e.estado !== 401) caja.innerHTML = bloqueError(e); }
  };
  $$(".pestanas button").forEach((b) => (b.onclick = () => abrir(b.dataset.t)));
  await abrir(almacen.leer("cm_tab_inv") || "existencias");
}

async function panelExistencias(caja) {
  const ex = await api("inventario", "/existencias");
  const porId = Object.fromEntries(ex.map((e) => [e.producto_id, e]));
  const productos = Object.values(productosCache);
  caja.innerHTML = `
    <div class="dos">
      <div class="tarjeta"><div class="tabla-env"><table><thead><tr><th>Producto</th><th class="num">Disponible</th><th class="num">Reservado</th><th>Actualizado</th></tr></thead>
        <tbody>${productos.map((p) => { const e = porId[p.id] || { disponible: 0, reservado: 0 };
          return `<tr><td>${esc(p.nombre)} ${p.activo ? "" : '<span class="chip">inactivo</span>'}</td><td class="num">${e.disponible}</td><td class="num">${e.reservado}</td><td>${fecha(e.actualizado_en)}</td></tr>`; }).join("")}</tbody></table></div></div>
      <div class="tarjeta"><h2>Registrar entrada</h2>
        <form id="fEntrada" novalidate>
          <label>Producto<select name="producto">${productos.map((p) => `<option value="${p.id}">${esc(p.nombre)}</option>`).join("")}</select></label>
          <label>Cantidad<input name="cantidad" type="number" min="1" max="10000" value="5" required></label>
          <label>Referencia<input name="referencia" value="compra a proveedor" maxlength="120"></label>
          <div class="acciones"><button type="submit">Registrar</button></div>
        </form></div>
    </div>`;
  const f = $("#fEntrada", caja);
  f.onsubmit = async (ev) => {
    ev.preventDefault();
    if (!f.checkValidity()) { f.reportValidity(); return; }
    await conBoton($("button", f), async () => {
      try {
        const r = await api("inventario", `/existencias/${f.producto.value}/entradas`, { metodo: "POST", cuerpo: { cantidad: Number(f.cantidad.value), referencia: f.referencia.value } });
        avisar(`Entrada registrada: ${nombreProducto(r.producto_id)} ahora tiene ${r.disponible} disponibles.`, "ok");
        panelExistencias(caja);
      } catch (e) { e.estado === 422 ? marcarErrores(f, e) : avisarError(e); }
    });
  };
}

async function panelEntregas(caja) {
  async function pintar() {
    const estados = ["APROBADA", "ENTREGA_PENDIENTE", "CANCELACION_PENDIENTE", "PENDIENTE_INVENTARIO"];
    const listas = await Promise.all(estados.map((e) => api("prestamos", `/prestamos?estado=${e}&limite=100`)));
    const todos = listas.flat();
    caja.innerHTML = `<div class="tarjeta"><p class="suave">La entrega pide a Inventario confirmar la salida (SalidaConfirmada → COMPLETADA). La cancelación libera la reserva (ReservaLiberada → CANCELADA).</p>
      ${todos.length ? `<div class="tabla-env"><table><thead><tr><th>Solicitud</th><th>Producto</th><th>Cliente</th><th>Estado</th><th>Reserva vence</th><th></th></tr></thead><tbody>
      ${todos.map((p) => `<tr><td><a class="mono" href="#/prestamo/${p.id}">${corto(p.id)}</a></td><td>${esc(nombreProducto(p.producto_id))} × ${p.cantidad}</td><td>${p.cliente_id}</td>
        <td>${chipEstado(p.estado)}</td><td>${fecha(p.reserva_expira_en)}</td>
        <td><div class="acciones">${p.estado === "APROBADA" ? `<button data-entregar="${p.id}" style="padding:.25rem .6rem">Entregar</button>` : ""}
          ${CANCELABLES.includes(p.estado) ? `<button class="peligro" data-cancelar="${p.id}" style="padding:.25rem .6rem">Cancelar</button>` : ""}</div></td></tr>`).join("")}
      </tbody></table></div>` : '<div class="vacio">No hay préstamos aprobados ni en curso.</div>'}</div>`;
    $$("[data-entregar]", caja).forEach((b) => (b.onclick = () => conBoton(b, async () => {
      try { await api("prestamos", `/prestamos/${b.dataset.entregar}/entregar`, { metodo: "POST" }); avisar("Entrega solicitada.", "ok"); pintar(); } catch (e) { avisarError(e); }
    })));
    $$("[data-cancelar]", caja).forEach((b) => (b.onclick = () => conBoton(b, async () => {
      try { await api("prestamos", `/prestamos/${b.dataset.cancelar}/cancelar`, { metodo: "POST" }); avisar("Cancelación solicitada; se libera la reserva.", "ok"); pintar(); } catch (e) { avisarError(e); }
    })));
  }
  await pintar();
  cada(3000, () => pintar().catch(() => {}));
}

async function panelReservas(caja) {
  caja.innerHTML = `<div class="tarjeta"><label style="max-width:260px">Estado<select id="fr"><option value="">Todas</option>
    ${["RESERVADA", "CONFIRMADA", "LIBERADA", "RECHAZADA", "VENCIDA"].map((e) => `<option>${e}</option>`).join("")}</select></label><div id="tr" style="margin-top:.75rem"></div></div>`;
  const pintar = async () => {
    const e = $("#fr", caja).value;
    const lista = await api("inventario", `/reservas?limite=200${e ? `&estado=${e}` : ""}`);
    $("#tr", caja).innerHTML = lista.length ? `<div class="tabla-env"><table><thead><tr><th>Creada</th><th>Préstamo</th><th>Producto</th><th class="num">Cant.</th><th>Estado</th><th>Vence</th></tr></thead>
      <tbody>${lista.map((r) => `<tr><td>${fecha(r.creado_en)}</td><td><a class="mono" href="#/prestamo/${r.prestamo_id}">${corto(r.prestamo_id)}</a></td>
        <td>${esc(nombreProducto(r.producto_id))}</td><td class="num">${r.cantidad}</td><td><span class="chip">${esc(r.estado)}</span></td><td>${fecha(r.expira_en)}</td></tr>`).join("")}</tbody></table></div>`
      : '<div class="vacio">Sin reservas.</div>';
  };
  $("#fr", caja).onchange = pintar;
  await pintar();
}

async function panelMovimientos(caja) {
  const lista = await api("inventario", "/movimientos?limite=200");
  caja.innerHTML = `<div class="tarjeta">${lista.length ? `<div class="tabla-env"><table><thead><tr><th>Fecha</th><th>Producto</th><th>Tipo</th><th class="num">Cantidad</th><th>Préstamo</th><th>Referencia</th></tr></thead>
    <tbody>${lista.map((m) => `<tr><td>${fecha(m.creado_en)}</td><td>${esc(nombreProducto(m.producto_id))}</td><td>${esc(m.tipo)}</td><td class="num">${m.cantidad}</td>
      <td>${m.prestamo_id ? `<a class="mono" href="#/prestamo/${m.prestamo_id}">${corto(m.prestamo_id)}</a>` : "—"}</td><td>${esc(m.referencia || "")}</td></tr>`).join("")}</tbody></table></div>`
    : '<div class="vacio">Sin movimientos.</div>'}</div>`;
}

async function panelMensajeria(caja) {
  const m = await api("inventario", "/mensajeria/estado");
  caja.innerHTML = `<div class="tarjeta"><h2>Inventario ↔ RabbitMQ</h2>
    <div class="cifras">
      <div class="cifra"><b>${m.outbox_pendientes}</b><span>eventos pendientes en outbox</span></div>
      <div class="cifra"><b>${m.consumidos ?? "—"}</b><span>comandos consumidos</span></div>
      <div class="cifra"><b>${m.reintentos ?? "—"}</b><span>reintentos</span></div>
      <div class="cifra"><b>${m.a_dlq ?? "—"}</b><span>enviados a DLQ</span></div>
    </div>
    <p style="margin-top:1rem">Comandos procesados por tipo: ${(m.procesados || []).map((p) => `${esc(p.tipo)}: <b>${p.n}</b>`).join(" · ") || "—"}</p>
    ${m.ultimo_error ? `<p class="suave">Último error: <span class="mono">${esc(m.ultimo_error)}</span></p>` : ""}
    <p><a href="${esc(CFG.rabbitmq)}" target="_blank" rel="noopener">Abrir la consola de RabbitMQ</a> (colas, DLQ y confirmaciones).</p></div>`;
}

/* ================================================================== MODELO (admin) */
async function vistaModelo() {
  const [info, mon] = await Promise.all([api("prestamos", "/modelo/info"), api("prestamos", "/modelo/monitoreo").catch(() => null)]);
  const met = info.metricas_prueba || {};
  vista.innerHTML = `
    <div class="encabezado"><div><h1>Modelo de riesgo</h1><p class="suave">Versión cargada por el servicio de Préstamos al iniciar o al recargar la promoción.</p></div>
      <div class="acciones"><a class="boton secundario" href="${esc(info.mlflow_url || CFG.mlflow)}" target="_blank" rel="noopener">Abrir MLflow</a>
        <button id="recargar">Recargar versión promovida</button></div></div>
    <div class="dos">
      <div class="tarjeta"><h2>Versión activa</h2><dl class="kv">
        <dt>Versión</dt><dd><b>${esc(info.model_version)}</b> ${info.listo ? '<span class="chip ok">cargada</span>' : '<span class="chip error">no disponible</span>'}</dd>
        <dt>Run ID (MLflow)</dt><dd class="mono">${esc(info.run_id)}</dd>
        <dt>Algoritmo</dt><dd>${esc(info.algoritmo)}</dd>
        <dt>Umbral</dt><dd>${esc(info.umbral)} <span class="suave">(desfavorable si probabilidad ≥ umbral)</span></dd>
        <dt>Clase positiva</dt><dd>${esc(info.clase_positiva)}</dd>
        <dt>Dataset</dt><dd class="mono">${esc(info.dataset?.version || "")} · sha256 ${esc((info.dataset?.sha256 || "").slice(0, 16))}…</dd>
        <dt>Artefacto</dt><dd class="mono">sha256 ${esc((info.sha256_artefacto || "").slice(0, 16))}…</dd>
        <dt>Promovida</dt><dd>${fecha(info.promocion?.promovido_en)} · ${esc(info.promocion?.motivo || "")}${info.promocion?.anterior ? ` · anterior: ${esc(info.promocion.anterior)}` : ""}</dd>
      </dl>${info.error ? `<p class="aviso error">${esc(info.error)}</p>` : ""}</div>
      <div class="tarjeta"><h2>Métricas en prueba</h2><div class="cifras">
        <div class="cifra"><b>${met.average_precision?.toFixed(3) ?? "—"}</b><span>average precision</span></div>
        <div class="cifra"><b>${met.roc_auc?.toFixed(3) ?? "—"}</b><span>ROC-AUC</span></div>
        <div class="cifra"><b>${met.recall_1?.toFixed(3) ?? "—"}</b><span>recall clase 1</span></div>
        <div class="cifra"><b>${met.precision_1?.toFixed(3) ?? "—"}</b><span>precision clase 1</span></div>
        <div class="cifra"><b>${met.brier?.toFixed(3) ?? "—"}</b><span>Brier</span></div></div>
        <p class="suave" style="margin-top:.75rem">Variables de entrada: ${(info.variables_entrada || []).map(esc).join(", ")}</p></div>
    </div>
    ${mon ? `
    <div class="dos">
      <div class="tarjeta"><h2>Predicciones por versión</h2><div class="tabla-env"><table><thead><tr><th>Versión</th><th class="num">n</th><th class="num">Prob. media</th><th class="num">% desfavorable</th><th>Última</th></tr></thead>
        <tbody>${mon.por_version.map((v) => `<tr><td>${esc(v.model_version)}</td><td class="num">${v.n}</td><td class="num">${pct(v.probabilidad_media)}</td><td class="num">${pct(v.tasa_desfavorable)}</td><td>${fecha(v.ultima)}</td></tr>`).join("")}</tbody></table></div>
        <h3 style="margin-top:1rem">Préstamos por estado</h3><p>${mon.prestamos_por_estado.map((e) => `${chipEstado(e.estado)} ${e.n}`).join(" &nbsp; ") || "—"}</p></div>
      <div class="tarjeta"><h2>Drift de entradas (PSI)</h2><p class="suave">Últimas ${mon.drift.ventana} predicciones frente a entrenamiento. ${esc(mon.drift.criterio)}</p>
        <div class="tabla-env"><table><thead><tr><th>Variable</th><th class="num">PSI</th><th>Alerta</th></tr></thead>
        <tbody>${Object.entries(mon.drift.variables).sort((a, b) => b[1].psi - a[1].psi).map(([k, d]) => `<tr><td>${esc(k)}</td><td class="num">${d.psi.toFixed(3)}</td>
          <td><span class="chip ${d.alerta === "alta" ? "error" : d.alerta === "moderada" ? "espera" : "ok"}">${d.alerta}</span></td></tr>`).join("") || '<tr><td colspan="3">Se necesitan al menos 30 predicciones.</td></tr>'}</tbody></table></div></div>
    </div>` : ""}`;
  const b = $("#recargar");
  b.onclick = () => conBoton(b, async () => {
    try {
      const r = await api("prestamos", "/modelo/recargar", { metodo: "POST" });
      avisar(r.anterior === r.activa ? `Sin cambios: la versión promovida sigue siendo ${r.activa}.` : `Modelo cambiado: ${r.anterior} → ${r.activa}.`, "ok");
      enrutar();
    } catch (e) { avisarError(e); }
  });
}

/* ------------------------------------------------------------------ inicio */
if (!CFG.productos) avisar("Falta config.js con las URL de las APIs.", "error", 0);
enrutar();
