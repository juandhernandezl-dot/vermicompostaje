#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rl_monitor.py  -  VENTANA DE OBSERVACION DE LOS CONTROLADORES RL
================================================================

Muestra EN VIVO como se actualiza la tabla (o la politica) de cada controlador
de zona mientras aprende. Lee las instantaneas que los controladores publican
en 'estado_rl/z<N>_<algoritmo>.json' (escritura atomica), asi que no interfiere
con el aprendizaje ni lo ralentiza.

QUE SE VE
---------
  * MAPA DE LA TABLA: filas = bins de humedad (arriba mas humedo), columnas =
    bins de temperatura. El color indica que accion prefiere el agente en esa
    celda y con cuanta ventaja; las celdas nunca visitadas quedan apagadas.
      - Celda con BORDE BLANCO  -> celda en la que esta el proceso ahora mismo.
      - Celda con BORDE AMARILLO-> celda que ACABA de cambiar de valor.
      - Franja verde horizontal -> banda objetivo de humedad.
  * REBANADA: la tabla tiene ademas las dimensiones "tendencia" y "aporte desde
    la zona de arriba". Se elige cual de las 9 rebanadas se dibuja, o se deja
    en 'seguir al proceso' para ver siempre la que se esta usando.
  * PANEL DERECHO: valores exactos de la celda actual, error TD, epsilon,
    numero de celdas aprendidas, metricas del lazo y el registro de las ultimas
    actualizaciones (humedad, temperatura, aporte de arriba, accion, premio).
  * REINFORCE no tiene tabla: se dibuja la MISMA rejilla evaluando su politica
    (probabilidad de abrir) y ademas los pesos theta de cada caracteristica.

USO
---
    python3 rl_monitor.py                  # detecta solos los controladores
    python3 rl_monitor.py --dir estado_rl --refresco 400
"""

import argparse
import json
import os
import time
from collections import deque

try:
    import tkinter as tk
    from tkinter import ttk
    _HAS_TK = True
except ImportError:  # pragma: no cover
    _HAS_TK = False
    tk = ttk = None


DIR_ESTADO = "estado_rl"

UI = {
    "bg": "#0e1116", "panel": "#161b22", "panel2": "#1f2630",
    "stroke": "#2b323c", "stroke2": "#3a434f", "text": "#e6edf3",
    "dim": "#8b949e", "water": "#38bdf8", "water_dk": "#0c4a6e",
    "temp": "#f4795b", "gold": "#ffd166", "accent": "#5b8cff",
    "ok": "#4ade80", "bad": "#f87171", "vacio": "#141a21",
}


# =============================================================================
# UTILIDADES DE COLOR Y GEOMETRIA (puras: se pueden probar sin Tkinter)
# =============================================================================

def _rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _hex(rgb):
    r, g, b = (max(0, min(255, int(v))) for v in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


def mezcla(c1, c2, t):
    t = max(0.0, min(1.0, t))
    return _hex(tuple(a + (b - a) * t for a, b in zip(_rgb(c1), _rgb(c2))))


COLOR_ACCION = ["#f4795b", "#facc15", "#38bdf8", "#a78bfa"]  # cerrar ... abrir


def color_celda(valores, escala="valor Q", n_acciones=2):
    """Color de una celda: tono = accion preferida, saturacion = ventaja.

    Con 'valor Q' la ventaja es la diferencia con la segunda mejor accion,
    normalizada de forma suave; con 'probabilidad' es la propia probabilidad.
    """
    if not valores:
        return UI["vacio"], 0.0
    mejor = max(range(len(valores)), key=lambda i: valores[i])
    if len(valores) > 1:
        resto = [v for i, v in enumerate(valores) if i != mejor]
        margen = valores[mejor] - max(resto)
    else:
        margen = 0.0
    if escala == "probabilidad":
        fuerza = max(0.0, min(1.0, (valores[mejor] - 1.0 / len(valores)) /
                              (1.0 - 1.0 / len(valores) + 1e-9)))
    else:
        fuerza = margen / (abs(margen) + 3.0)      # compresion suave a [0,1)
        fuerza = max(0.0, min(1.0, fuerza))
    base = COLOR_ACCION[mejor % len(COLOR_ACCION)]
    return mezcla(UI["panel2"], base, 0.15 + 0.85 * fuerza), fuerza


def rejilla(x0, y0, ancho, alto, filas, columnas):
    """Devuelve la geometria de cada celda: (fila, col, x0, y0, x1, y1).
    La fila 0 (humedad mas baja) se dibuja ABAJO, como en un eje normal."""
    cw = ancho / max(1, columnas)
    ch = alto / max(1, filas)
    out = []
    for f in range(filas):
        for c in range(columnas):
            cx0 = x0 + c * cw
            cy0 = y0 + (filas - 1 - f) * ch
            out.append((f, c, cx0, cy0, cx0 + cw, cy0 + ch))
    return out


def carpetas_candidatas(carpeta=DIR_ESTADO):
    """Sitios donde buscar las instantaneas.

    Si el monitor se abre desde otro directorio que el controlador (pasa al
    lanzarlo desde el explorador o desde otro terminal), la ruta relativa no
    coincide. Se miran tanto el directorio de trabajo como el del propio script.
    """
    aqui = os.path.dirname(os.path.abspath(__file__))
    vistos, salida = set(), []
    for base in (os.path.abspath(carpeta), os.path.join(aqui, carpeta)):
        if base not in vistos:
            vistos.add(base)
            salida.append(base)
    return salida


def leer_snapshots(carpeta=DIR_ESTADO):
    """Lee todas las instantaneas disponibles. Devuelve {nombre: datos}."""
    salida = {}
    for base in carpetas_candidatas(carpeta):
        if not os.path.isdir(base):
            continue
        for nombre in sorted(os.listdir(base)):
            if not nombre.endswith(".json") or nombre.endswith(".tmp"):
                continue
            try:
                with open(os.path.join(base, nombre), "r", encoding="utf-8") as f:
                    datos = json.load(f)
            except (OSError, json.JSONDecodeError, ValueError):
                continue    # se estaba reescribiendo: se leera en el siguiente ciclo
            if not isinstance(datos, dict) or "q" not in datos:
                continue
            datos["_archivo"] = os.path.join(base, nombre)
            salida[nombre[:-5]] = datos
    return salida


def ejes_de(datos):
    """Descriptor de los ejes de la tabla, con respaldo para formatos antiguos."""
    bins = datos.get("bins") or {}
    filas = bins.get("filas") or {"nombre": "humedad de la zona",
                                  "min": bins.get("moist_min", 0.0),
                                  "max": bins.get("moist_max", 1.4),
                                  "bins": bins.get("moist_bins", 24)}
    cols = bins.get("columnas") or {"nombre": "humedad de la zona de ARRIBA",
                                    "min": bins.get("temp_min", 0.0),
                                    "max": bins.get("temp_max", 1.4),
                                    "bins": bins.get("temp_bins", 8)}
    reb = bins.get("rebanadas") or [
        {"nombre": "tendencia", "etiquetas": ["baja", "estable", "sube"]},
        {"nombre": "zona de abajo", "etiquetas": ["seca", "en banda", "humeda"]}]
    return filas, cols, reb


def celdas_cambiadas(previo, actual, tol=1e-9):
    """Claves cuyo valor ha cambiado entre dos instantaneas de tabla."""
    if not previo:
        return set()
    cambios = set()
    for k, v in actual.items():
        w = previo.get(k)
        if w is None or len(w) != len(v):
            cambios.add(k)
            continue
        if any(abs(a - b) > tol for a, b in zip(v, w)):
            cambios.add(k)
    return cambios


# =============================================================================
# VENTANA
# =============================================================================

class MonitorApp:
    """Ventana unica con un panel por controlador detectado."""

    def __init__(self, root, carpeta=DIR_ESTADO, refresco_ms=400):
        self.root = root
        self.carpeta = carpeta
        self.refresco = refresco_ms
        self.datos = {}
        self.previo = {}
        self.sel = None
        self.pausado = False
        self.historia = {}          # nombre -> deque de (r_medio, td)
        self.cambios = set()
        self.hover = None
        self.error = None
        root.title("Monitor de aprendizaje - controladores RL por zona")
        root.configure(bg=UI["bg"])
        root.geometry("1240x780")
        root.minsize(980, 620)
        self._estilo()
        self._construir()
        self._refrescar()

    # ---- estilo y estructura -------------------------------------------------
    def _estilo(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=UI["bg"], foreground=UI["text"])
        st.configure("TFrame", background=UI["bg"])
        st.configure("Card.TFrame", background=UI["panel"])
        st.configure("TLabel", background=UI["bg"], foreground=UI["text"])
        st.configure("Card.TLabel", background=UI["panel"], foreground=UI["text"])
        st.configure("Dim.TLabel", background=UI["panel"], foreground=UI["dim"])
        st.configure("TButton", background=UI["panel2"], foreground=UI["text"])
        st.configure("TCheckbutton", background=UI["bg"], foreground=UI["text"])
        st.configure("TCombobox", fieldbackground=UI["panel2"], background=UI["panel2"])

    def _construir(self):
        barra = ttk.Frame(self.root)
        barra.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(barra, text="Controlador:").pack(side="left")
        self.var_ctrl = tk.StringVar()
        self.combo = ttk.Combobox(barra, textvariable=self.var_ctrl, width=34,
                                  state="readonly")
        self.combo.pack(side="left", padx=(6, 16))
        self.combo.bind("<<ComboboxSelected>>", lambda e: self._seleccionar(self.var_ctrl.get()))

        ttk.Label(barra, text="Rebanada:").pack(side="left")
        self.var_seguir = tk.IntVar(value=1)
        ttk.Checkbutton(barra, text="seguir al proceso", variable=self.var_seguir
                        ).pack(side="left", padx=(6, 10))
        self.var_reb = [tk.StringVar(value=""), tk.StringVar(value="")]
        self.combo_reb = []
        for v in self.var_reb:
            c = ttk.Combobox(barra, textvariable=v, width=12, state="readonly")
            c.pack(side="left", padx=3)
            c.bind("<<ComboboxSelected>>", lambda e: self._dibujar())
            self.combo_reb.append(c)

        self.var_visitas = tk.IntVar(value=0)
        ttk.Checkbutton(barra, text="ver visitas", variable=self.var_visitas
                        ).pack(side="left", padx=(16, 4))
        self.btn_pausa = ttk.Button(barra, text="Pausar", command=self._pausa)
        self.btn_pausa.pack(side="right")
        self.lbl_estado = ttk.Label(barra, text="", foreground=UI["dim"])
        self.lbl_estado.pack(side="right", padx=10)

        cuerpo = ttk.Frame(self.root)
        cuerpo.pack(fill="both", expand=True, padx=10, pady=6)

        izq = ttk.Frame(cuerpo)
        izq.pack(side="left", fill="both", expand=True)
        self.canvas = tk.Canvas(izq, bg=UI["panel"], highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Motion>", self._mover_raton)
        self.canvas.bind("<Leave>", lambda e: self._set_hover(None))

        der = ttk.Frame(cuerpo, width=430)
        der.pack(side="right", fill="both", padx=(10, 0))
        der.pack_propagate(False)
        self.panel = tk.Canvas(der, bg=UI["panel"], highlightthickness=0, height=330)
        self.panel.pack(fill="x")
        ttk.Label(der, text="Ultimas actualizaciones", style="Dim.TLabel"
                  ).pack(anchor="w", pady=(8, 2))
        self.log = tk.Text(der, height=16, bg=UI["panel2"], fg=UI["text"],
                           insertbackground=UI["text"], relief="flat",
                           font=("TkFixedFont", 9), wrap="none")
        self.log.pack(fill="both", expand=True)
        self.log.configure(state="disabled")

    def _pausa(self):
        self.pausado = not self.pausado
        self.btn_pausa.configure(text="Reanudar" if self.pausado else "Pausar")

    def _seleccionar(self, nombre):
        self.sel = nombre
        self.previo = {}
        self._dibujar()

    def _set_hover(self, celda):
        if celda != self.hover:
            self.hover = celda
            self._dibujar()

    # ---- ciclo de refresco ---------------------------------------------------
    def _refrescar(self):
        try:
            self._refrescar_una_vez()
        except Exception as e:      # el monitor nunca debe morir en silencio
            self.error = f"{type(e).__name__}: {e}"
            try:
                self._dibujar()
            except Exception:
                pass
        self.root.after(self.refresco, self._refrescar)

    def _refrescar_una_vez(self):
        if not self.pausado:
            nuevos = leer_snapshots(self.carpeta)
            nombres = sorted(nuevos)
            if nombres != list(self.combo["values"]):
                self.combo["values"] = nombres
            if self.sel not in nuevos:
                self.sel = nombres[0] if nombres else None
                if self.sel:
                    self.var_ctrl.set(self.sel)
            if self.sel and self.sel in nuevos:
                antes = (self.datos.get(self.sel) or {}).get("q", {})
                self.cambios = celdas_cambiadas(antes, nuevos[self.sel].get("q", {}))
                d = nuevos[self.sel]
                h = self.historia.setdefault(self.sel, deque(maxlen=240))
                met = d.get("metricas") or {}
                upd = d.get("ultimo_update") or {}
                h.append((met.get("r_medio", 0.0), upd.get("td", 0.0) or 0.0,
                          met.get("pct_banda", float("nan"))))
            self.datos = nuevos
            self.error = None
            self._dibujar()

    # ---- dibujo --------------------------------------------------------------
    def _dibujar(self):
        c = self.canvas
        c.delete("all")
        W = max(320, c.winfo_width())
        H = max(240, c.winfo_height())
        d = self.datos.get(self.sel) if self.sel else None
        if not d:
            vigiladas = "\n".join("   " + x for x in carpetas_candidatas(self.carpeta))
            existe = [x for x in carpetas_candidatas(self.carpeta) if os.path.isdir(x)]
            detalle = ("La carpeta de instantaneas todavia no existe: la crea el "
                       "controlador al arrancar." if not existe else
                       "La carpeta existe pero no hay ninguna instantanea dentro.")
            aviso = f"\n\nERROR AL LEER: {self.error}" if self.error else ""
            c.create_text(W / 2, H / 2, fill=UI["dim"], font=("TkDefaultFont", 11),
                          justify="center",
                          text=("No hay ningun controlador publicando.\n\n"
                                f"{detalle}\n\nCarpetas vigiladas:\n{vigiladas}\n\n"
                                "Arranca un controlador, por ejemplo:\n"
                                "  python3 rl_zona.py --zona 0 --algoritmo qlearning\n"
                                "(o el boton 'Lanzar en esta zona' del simulador)."
                                + aviso))
            self.lbl_estado.configure(text="esperando", foreground=UI["gold"])
            self._panel_derecho(None)
            return

        edad = time.time() - float(d.get("actualizado", 0) or 0)
        vivo = edad < 5.0
        self.lbl_estado.configure(
            text=("en marcha" if vivo else f"sin datos desde hace {edad:.0f} s"),
            foreground=UI["ok"] if vivo else UI["bad"])

        filas, cols, reb = ejes_de(d)
        nm, nt = int(filas["bins"]), int(cols["bins"])
        celda_actual = d.get("celda_actual") or []
        tr, cg = self._rebanada(celda_actual, reb)

        # --- cabecera ---
        etq0 = reb[0]["etiquetas"][min(tr, len(reb[0]["etiquetas"]) - 1)]
        etq1 = reb[1]["etiquetas"][min(cg, len(reb[1]["etiquetas"]) - 1)]
        titulo = (f"Zona {d.get('zona','?')}  -  {d.get('algoritmo','?')}"
                  f"   |   rebanada:  {reb[0]['nombre']} '{etq0}'  ·  "
                  f"{reb[1]['nombre']} '{etq1}'")
        c.create_text(16, 14, anchor="w", fill=UI["text"], text=titulo,
                      font=("TkDefaultFont", 11, "bold"))
        modo = "visitas" if self.var_visitas.get() else d.get("escala", "valor Q")
        c.create_text(16, 34, anchor="w", fill=UI["dim"],
                      text=f"color = accion preferida ({modo}) | "
                           f"celdas aprendidas: {d.get('n_celdas', 0)} | "
                           f"actualizaciones: {d.get('n_updates', 0)}")

        x0, y0 = 62, 56
        ancho = W - x0 - 20
        alto = H - y0 - 56
        q = d.get("q", {})
        visitas = d.get("visitas", {})
        escala = d.get("escala", "valor Q")
        acciones = d.get("etiquetas") or ["cerrar", "abrir"]
        max_vis = max([1] + [v for v in visitas.values()])

        for f, col, cx0, cy0, cx1, cy1 in rejilla(x0, y0, ancho, alto, nm, nt):
            clave = f"{f},{col},{tr},{cg}"
            valores = q.get(clave)
            if self.var_visitas.get():
                v = visitas.get(clave, 0)
                color = (UI["vacio"] if not v else
                         mezcla(UI["panel2"], UI["accent"], min(1.0, v / max_vis)))
            else:
                color, _ = color_celda(valores, escala, len(acciones))
            c.create_rectangle(cx0 + 1, cy0 + 1, cx1 - 1, cy1 - 1,
                               fill=color, outline="", width=0)
            if valores and (cx1 - cx0) > 34 and (cy1 - cy0) > 15:
                mejor = max(valores)
                c.create_text((cx0 + cx1) / 2, (cy0 + cy1) / 2, fill="#0b0f14",
                              font=("TkFixedFont", 8),
                              text=(f"{mejor:.2f}" if escala == "probabilidad"
                                    else f"{mejor:.0f}"))
            if clave in self.cambios:
                c.create_rectangle(cx0 + 1, cy0 + 1, cx1 - 1, cy1 - 1,
                                   outline=UI["gold"], width=2)
            if celda_actual[:2] == [f, col] and (tr, cg) == tuple(celda_actual[2:4] or (tr, cg)):
                c.create_rectangle(cx0, cy0, cx1, cy1, outline="#ffffff", width=2)
            if self.hover == (f, col):
                c.create_rectangle(cx0, cy0, cx1, cy1, outline=UI["accent"], width=2)

        self._ejes(c, x0, y0, ancho, alto, filas, cols)
        self._banda(c, d, x0, y0, ancho, alto, filas)
        self._leyenda(c, acciones, x0, H - 30)
        self._panel_derecho(d)

    def _rebanada(self, celda_actual, reb):
        """Que rebanada de la tabla se dibuja. Los desplegables se rellenan con
        las etiquetas que publica el propio controlador, asi que el monitor no
        necesita saber que dimensiones tiene la tabla."""
        for k, desc in enumerate(reb[:2]):
            etiquetas = list(desc.get("etiquetas") or [])
            if list(self.combo_reb[k]["values"]) != etiquetas:
                self.combo_reb[k]["values"] = etiquetas
                if self.var_reb[k].get() not in etiquetas and etiquetas:
                    self.var_reb[k].set(etiquetas[min(1, len(etiquetas) - 1)])
        if self.var_seguir.get() and len(celda_actual) >= 4:
            idx = (int(celda_actual[2]), int(celda_actual[3]))
            for k in range(2):
                etiquetas = list(reb[k].get("etiquetas") or [])
                if idx[k] < len(etiquetas):
                    self.var_reb[k].set(etiquetas[idx[k]])
            return idx
        out = []
        for k in range(2):
            etiquetas = list(reb[k].get("etiquetas") or [])
            v = self.var_reb[k].get()
            out.append(etiquetas.index(v) if v in etiquetas else 0)
        return out[0], out[1]

    def _ejes(self, c, x0, y0, ancho, alto, filas, cols):
        fmin, fmax, nm = float(filas["min"]), float(filas["max"]), int(filas["bins"])
        cmin, cmax, nt = float(cols["min"]), float(cols["max"]), int(cols["bins"])
        # Los cortes no son uniformes (el eje se alinea con la banda), asi que
        # cada linea se etiqueta con su corte real.
        cortes_f = filas.get("cortes")
        cortes_c = cols.get("cortes")
        c.create_text(x0 - 52, y0 - 12, anchor="w", fill=UI["dim"],
                      text=filas["nombre"], font=("TkFixedFont", 8))
        for k in range(nm + 1):
            y = y0 + alto * (1 - k / nm)
            v = cortes_f[k] if cortes_f and k < len(cortes_f) else fmin + (fmax - fmin) * k / nm
            if nm <= 22 or k % max(1, nm // 10) == 0:
                c.create_text(x0 - 8, y, anchor="e", fill=UI["dim"],
                              font=("TkFixedFont", 7), text=f"{v:.2f}")
                c.create_line(x0 - 4, y, x0, y, fill=UI["stroke2"])
        for k in range(nt + 1):
            x = x0 + ancho * k / nt
            v = cortes_c[k] if cortes_c and k < len(cortes_c) else cmin + (cmax - cmin) * k / nt
            c.create_text(x, y0 + alto + 12, fill=UI["dim"],
                          font=("TkFixedFont", 7), text=f"{v:.2f}")
        c.create_text(x0 + ancho / 2, y0 + alto + 28, fill=UI["dim"],
                      text=cols["nombre"])

    def _banda(self, c, d, x0, y0, ancho, alto, filas):
        banda = d.get("banda") or filas.get("banda") or []
        if len(banda) != 2:
            return
        cortes = filas.get("cortes")
        nm = int(filas["bins"])
        mmin, mmax = float(filas["min"]), float(filas["max"])

        def ay(m):
            if cortes:
                # posicion de m dentro de la rejilla de cortes (no uniforme)
                for k in range(len(cortes) - 1):
                    if cortes[k] <= m <= cortes[k + 1]:
                        f = (k + (m - cortes[k]) /
                             max(1e-9, cortes[k + 1] - cortes[k])) / nm
                        return y0 + alto * (1 - f)
                return y0 if m > cortes[-1] else y0 + alto
            return y0 + alto * (1 - (m - mmin) / max(1e-9, (mmax - mmin)))
        y_lo, y_hi = ay(banda[0]), ay(banda[1])
        c.create_rectangle(x0, y_hi, x0 + ancho, y_lo, outline=UI["ok"], width=2)
        c.create_text(x0 + ancho - 6, y_hi - 9, anchor="e", fill=UI["ok"],
                      font=("TkFixedFont", 8),
                      text=f"banda objetivo {banda[0]:.2f}-{banda[1]:.2f}")
        lim = d.get("limite")
        if lim:
            c.create_line(x0, ay(lim), x0 + ancho, ay(lim), fill=UI["bad"], dash=(4, 3))

    def _leyenda(self, c, acciones, x0, y):
        x = x0
        for i, etq in enumerate(acciones):
            col = COLOR_ACCION[i % len(COLOR_ACCION)]
            c.create_rectangle(x, y - 6, x + 14, y + 6, fill=col, outline="")
            c.create_text(x + 20, y, anchor="w", fill=UI["dim"], text=etq,
                          font=("TkFixedFont", 9))
            x += 26 + 8 * len(etq)
        c.create_text(x + 20, y, anchor="w", fill=UI["gold"],
                      font=("TkFixedFont", 9), text="borde amarillo = recien actualizada")

    # ---- panel derecho -------------------------------------------------------
    def _panel_derecho(self, d):
        p = self.panel
        p.delete("all")
        W = max(300, p.winfo_width())
        if not d:
            return
        y = 16
        obs = d.get("observacion") or {}
        met = d.get("metricas") or {}
        upd = d.get("ultimo_update") or {}

        def fila(etq, val, color=None):
            nonlocal y
            p.create_text(12, y, anchor="w", fill=UI["dim"], text=etq,
                          font=("TkFixedFont", 9))
            p.create_text(W - 12, y, anchor="e", fill=color or UI["text"], text=val,
                          font=("TkFixedFont", 9, "bold"))
            y += 18

        p.create_text(12, y, anchor="w", fill=UI["text"], font=("TkDefaultFont", 10, "bold"),
                      text="Estado del proceso")
        y += 22
        fila("humedad (media)", f"{obs.get('moisture', float('nan')):.4f}")
        fila("  cabecera / pie", f"{obs.get('moisture_head', 0.0):.3f} / "
                                 f"{obs.get('moisture_tail', 0.0):.3f}")
        fila("humedad zona arriba", f"{obs.get('moisture_up', 0.0):.4f}")
        fila("humedad zona abajo", f"{obs.get('moisture_down', 0.0):.4f}")
        fila("tendencia", f"{(obs.get('trend') or 0.0)*3600:+.4f} /h")
        fila("aporte desde arriba", f"{obs.get('carga_arriba', 0.0):.3f}")
        fila("envio hacia abajo", f"{obs.get('carga_abajo', 0.0):.3f}")
        fila("apertura actual", f"{obs.get('valve_level', 0.0):.2f}")
        fila("tiempo simulado", f"{obs.get('time_hours', 0.0):.2f} h")
        y += 6
        p.create_text(12, y, anchor="w", fill=UI["text"], font=("TkDefaultFont", 10, "bold"),
                      text="Aprendizaje")
        y += 22
        if d.get("tipo") == "tabla":
            celda = d.get("celda_actual")
            fila("celda actual", str(celda))
            valores = (d.get("q") or {}).get(",".join(map(str, celda))) if celda else None
            etq = d.get("etiquetas") or []
            if valores:
                for i, v in enumerate(valores):
                    nombre = etq[i] if i < len(etq) else str(i)
                    mejor = v == max(valores)
                    fila(f"  Q({nombre})", f"{v:+.3f}",
                         UI["ok"] if mejor else UI["dim"])
            fila("error TD", f"{(upd.get('td') or 0.0):+.4f}",
                 UI["gold"] if abs(upd.get("td") or 0) > 1e-6 else UI["dim"])
            fila("visitas a la celda", str(upd.get("visitas", 0)))
            fila("epsilon", f"{d.get('epsilon', 0.0):.3f}")
        else:
            pi = d.get("pi_actual") or []
            etq = d.get("etiquetas") or []
            for i, v in enumerate(pi):
                fila(f"  pi({etq[i] if i < len(etq) else i})", f"{v:.3f}",
                     UI["ok"] if v == max(pi) else UI["dim"])
            fila("linea base", f"{d.get('baseline', 0.0):+.2f}")
            fila("retorno medio ep.", f"{(upd.get('retorno_medio') or 0.0):+.2f}")
        y += 6
        fila("% en banda (movil)", f"{met.get('pct_banda', float('nan')):.1f} %",
             UI["ok"] if (met.get("pct_banda") or 0) > 80 else UI["gold"])
        fila("% saturado", f"{met.get('pct_sat', float('nan')):.1f} %")
        fila("premio medio", f"{met.get('r_medio', 0.0):+.3f}")
        fila("cierres de seguridad", str(d.get("forzadas", 0)))

        if d.get("tipo") == "politica":
            self._pesos_theta(p, d, W, y + 8)
        else:
            self._grafico(p, W, y + 8)
        self._volcar_log(d)

    def _grafico(self, p, W, y):
        """Evolucion reciente del premio medio y del error TD."""
        h = self.historia.get(self.sel)
        if not h or len(h) < 2:
            return
        alto, x0, ancho = 70, 12, W - 24
        p.create_text(x0, y, anchor="w", fill=UI["dim"], font=("TkFixedFont", 9),
                      text="premio medio (azul) / error TD (ambar)")
        y += 12
        p.create_rectangle(x0, y, x0 + ancho, y + alto, outline=UI["stroke"], fill=UI["panel2"])
        for idx, color in ((0, UI["water"]), (1, UI["gold"])):
            serie = [v[idx] for v in h]
            lo, hi = min(serie), max(serie)
            span = (hi - lo) or 1.0
            pts = []
            for i, v in enumerate(serie):
                pts += [x0 + ancho * i / max(1, len(serie) - 1),
                        y + alto - (v - lo) / span * (alto - 6) - 3]
            if len(pts) >= 4:
                p.create_line(*pts, fill=color, width=1.5)

    def _pesos_theta(self, p, d, W, y):
        theta = d.get("theta") or []
        nombres = d.get("caracteristicas") or []
        if not theta:
            return
        p.create_text(12, y, anchor="w", fill=UI["dim"], font=("TkFixedFont", 9),
                      text="pesos theta (accion 'abrir' menos 'cerrar')")
        y += 14
        fila = [theta[-1][i] - theta[0][i] for i in range(len(theta[0]))]
        top = max(1e-9, max(abs(v) for v in fila))
        alto = 12
        for i, v in enumerate(fila):
            yy = y + i * (alto + 2)
            cx = 150
            p.create_text(140, yy + alto / 2, anchor="e", fill=UI["dim"],
                          font=("TkFixedFont", 8),
                          text=nombres[i] if i < len(nombres) else str(i))
            largo = (W - 170) / 2 * (v / top)
            p.create_rectangle(cx, yy, cx + largo, yy + alto,
                               fill=UI["water"] if v > 0 else UI["temp"], outline="")
            p.create_line(cx, y - 2, cx, yy + alto + 2, fill=UI["stroke2"])

    def _volcar_log(self, d):
        filas = d.get("ultimos") or []
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        cab = (f"{'t(h)':>7} {'hum':>6} {'arriba':>7} {'abajo':>6} {'a':>2} "
               f"{'premio':>8} {'dQ':>9}\n")
        self.log.insert("end", cab)
        for f in list(filas)[-40:][::-1]:
            dq = ""
            if "q_despues" in f and "q_antes" in f:
                dq = f"{f['q_despues'] - f['q_antes']:+9.4f}"
            elif "td" in f:
                dq = f"{f['td']:+9.4f}"
            self.log.insert("end", f"{f.get('t', 0):7.2f} {f.get('h', 0):6.3f} "
                                   f"{f.get('h_arriba', 0):7.3f} {f.get('h_abajo', 0):6.3f} "
                                   f"{f.get('a', 0):>2} {f.get('r', 0):8.3f} {dq}\n")
        self.log.configure(state="disabled")

    # ---- interaccion ---------------------------------------------------------
    def _mover_raton(self, ev):
        d = self.datos.get(self.sel) if self.sel else None
        if not d:
            return
        filas, cols, _ = ejes_de(d)
        nm, nt = int(filas["bins"]), int(cols["bins"])
        W = max(320, self.canvas.winfo_width())
        H = max(240, self.canvas.winfo_height())
        x0, y0 = 62, 56
        ancho, alto = W - x0 - 20, H - y0 - 56
        if not (x0 <= ev.x <= x0 + ancho and y0 <= ev.y <= y0 + alto):
            self._set_hover(None)
            return
        col = min(nt - 1, int((ev.x - x0) / (ancho / nt)))
        fila = nm - 1 - min(nm - 1, int((ev.y - y0) / (alto / nm)))
        self._set_hover((fila, col))


def diagnostico(carpeta=DIR_ESTADO):
    """Comprueba sin abrir ventana que el monitor encontraria los datos.
    Util cuando el boton del simulador no parece hacer nada."""
    print("Carpetas vigiladas:")
    for base in carpetas_candidatas(carpeta):
        marca = "existe" if os.path.isdir(base) else "NO existe"
        print(f"   {base}   [{marca}]")
    datos = leer_snapshots(carpeta)
    if not datos:
        print("\nNo hay ninguna instantanea. Arranca antes un controlador:")
        print("   python3 rl_zona.py --zona 0 --algoritmo qlearning")
        return 1
    print(f"\n{len(datos)} controlador(es) encontrados:")
    for nombre, d in sorted(datos.items()):
        filas, cols, reb = ejes_de(d)
        edad = time.time() - float(d.get("actualizado", 0) or 0)
        print(f"   {nombre}: {d.get('algoritmo')} zona {d.get('zona')}, "
              f"{d.get('n_celdas', 0)} celdas, actualizado hace {edad:.1f} s")
        print(f"      ejes: {filas['nombre']} ({filas['bins']}) x "
              f"{cols['nombre']} ({cols['bins']}); rebanadas: "
              f"{', '.join(r['nombre'] for r in reb)}")
    print("\nTodo correcto: el monitor deberia mostrar estos datos.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Monitor en vivo de los controladores RL.")
    ap.add_argument("--dir", default=DIR_ESTADO, help="Carpeta de instantaneas.")
    ap.add_argument("--refresco", type=int, default=400, help="Milisegundos entre lecturas.")
    ap.add_argument("--test", action="store_true",
                    help="Comprueba que encuentra los datos y sale (sin ventana).")
    args = ap.parse_args(argv)
    if args.test:
        return diagnostico(args.dir)
    if not _HAS_TK:
        print("Tkinter no esta disponible: instala 'python3-tk' para usar el monitor.")
        return 1
    root = tk.Tk()
    MonitorApp(root, args.dir, args.refresco)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
