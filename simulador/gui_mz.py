#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gui_mz.py  -  INTERFAZ DE LA CAMA DE VERMICOMPOSTAJE MULTIZONA
==============================================================

Interfaz Tkinter del modelo multizona (vermicompost_mz.py). Muestra la cama en
PERFIL, con su pendiente real, una zona por aspersor, y el agua bajando de la
zona alta a la baja.

PESTANAS
--------
  Cama         : dibujo del lecho, aspersores, flujos y control de CADA zona
                 (Manual / Auto por histeresis / Externo para el RL), mas los
                 botones para lanzar los controladores y el monitor.
  Telemetria   : humedad y temperatura de todas las zonas frente al tiempo.
  Parametros   : todos los parametros del modelo, agrupados y explicados.
  Registro     : mensajes del servicio local.

MODOS POR ZONA
--------------
  Manual  : el boton de la fila fija el actuador de esa zona.
  Auto    : histeresis sobre la humedad con la banda de esa zona.
  Externo : lo gobierna un proceso de control (rl_zona.py). La interfaz no
            toca ese actuador. Al arrancar un controlador la zona pasa sola a
            'Externo', y al pararlo vuelve a 'Manual'.

    python3 gui_mz.py                 # interfaz + servicio local en 127.0.0.1:8765
    python3 gui_mz.py --port 8800     # otro puerto
"""

import argparse
import os
import queue
import time
import subprocess
import sys
import threading
from collections import deque

from vermicompost_mz import (BedParams, MultiZoneBed, MultiZoneService, HisteresisZona,
                             PARAM_SCHEMA, PARAM_INFO, flags_por_defecto, HOST, PORT)

try:
    import tkinter as tk
    from tkinter import ttk
    _HAS_TK = True
except ImportError:  # pragma: no cover
    _HAS_TK = False

    class _SinTk:
        """Marcador: permite importar el modulo (y sus funciones de calculo) en
        entornos sin Tkinter, pero avisa con claridad si se intenta abrir la
        interfaz."""
        def __init__(self, *a, **kw):
            raise RuntimeError(
                "Tkinter no esta disponible. Instala 'python3-tk' (Debian/Ubuntu) "
                "para usar la interfaz. El nucleo y el servicio funcionan sin el: "
                "python3 vermicompost_mz.py --serve")

    class tk:  # noqa: N801 - espacio de nombres sustituto
        Canvas = Text = Button = Label = Spinbox = Tk = _SinTk
        StringVar = IntVar = DoubleVar = _SinTk
        TclError = Exception

    class ttk:  # noqa: N801
        Frame = Scrollbar = Style = Notebook = Label = Button = _SinTk
        Combobox = Checkbutton = Scale = Entry = _SinTk


UI = {
    "bg": "#0e1116", "panel": "#161b22", "panel2": "#1f2630",
    "stroke": "#2b323c", "stroke2": "#3a434f", "text": "#e6edf3",
    "dim": "#8b949e", "water": "#38bdf8", "water_dk": "#0c4a6e",
    "temp": "#f4795b", "gold": "#ffd166", "accent": "#5b8cff",
    "ok": "#4ade80", "bad": "#f87171", "soil": "#2a2018", "soil_dk": "#171208",
}

COLOR_ZONA = ["#38bdf8", "#a78bfa", "#4ade80", "#facc15", "#f472b6",
              "#fb923c", "#22d3ee", "#c084fc"]

MODOS = ["Manual", "Auto", "Externo"]
CLAVE_MODO = {"Manual": "manual", "Auto": "auto", "Externo": "externo"}
MODO_CLAVE = {v: k for k, v in CLAVE_MODO.items()}


# =============================================================================
# UTILIDADES DE DIBUJO (puras)
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


def color_humedad(m, low=0.6, high=0.7):
    """Marron seco -> azul en banda -> azul intenso saturado."""
    if m < low:
        return mezcla("#6b4f2a", UI["water"], max(0.0, m / max(1e-9, low)) * 0.75)
    if m <= high:
        return mezcla(UI["water"], "#0ea5e9", (m - low) / max(1e-9, high - low))
    return mezcla("#0ea5e9", "#1e3a8a", min(1.0, (m - high) / 0.4))


def color_temp(T):
    t = max(0.0, min(1.0, (T - 10.0) / 30.0))
    if t < 0.5:
        return mezcla("#2e74d6", "#4ab674", t / 0.5)
    return mezcla("#4ab674", "#e76044", (t - 0.5) / 0.5)


def geometria_cama(W, n, pendiente_pct, margen=44, techo=72, alto_lecho=None):
    """Geometria del dibujo en perfil.

    Devuelve (zonas, y_sup(x), x0, x1, alto, alto_total). Todo se dimensiona a
    partir del ANCHO disponible, no del alto: asi el lienzo puede recortarse
    justo a lo que ocupa el dibujo y no quedan huecos muertos.
    La caida se exagera respecto a la escala real (una pendiente del 2 % seria
    imperceptible) pero se mantiene proporcional a la pendiente."""
    x0 = margen
    x1 = max(margen + 80, W - margen - 44)      # hueco derecho para el lixiviado
    alto = alto_lecho or max(64, min(124, (x1 - x0) / 15.0))
    caida = min(alto * 1.1, alto * max(0.0, pendiente_pct) / 4.0)
    ancho = (x1 - x0) / max(1, n)

    def y_sup(x):
        f = (x - x0) / max(1e-9, (x1 - x0))
        return techo + caida * f

    zonas = []
    for i in range(n):
        a, b = x0 + i * ancho, x0 + (i + 1) * ancho
        zonas.append({"i": i, "x0": a, "x1": b, "xc": (a + b) / 2.0,
                      "y0": y_sup(a), "y1": y_sup(b), "alto": alto})
    return zonas, y_sup, x0, x1, alto, techo + caida + alto + 30


def decimar(serie, objetivo):
    n = len(serie)
    if n <= objetivo:
        return list(serie)
    paso = n / objetivo
    return [serie[int(i * paso)] for i in range(objetivo)]


# =============================================================================
# MARCO CON SCROLL
# =============================================================================

class MarcoScroll(ttk.Frame):
    """Contenedor con barra de desplazamiento vertical.

    Si la ventana es mas alta que el contenido, el interior se estira para que
    los hijos con expand=True ocupen el sobrante (no quedan huecos muertos); si
    es mas baja, aparece la barra y se puede bajar hasta los controles.
    """

    def __init__(self, parent, **kw):
        super().__init__(parent, **kw)
        self.lienzo = tk.Canvas(self, bg=UI["bg"], highlightthickness=0)
        self.barra = ttk.Scrollbar(self, orient="vertical", command=self.lienzo.yview)
        self.interior = ttk.Frame(self.lienzo)
        self._ventana = self.lienzo.create_window((0, 0), window=self.interior,
                                                  anchor="nw")
        self.lienzo.configure(yscrollcommand=self.barra.set)
        self.lienzo.pack(side="left", fill="both", expand=True)
        self.barra.pack(side="right", fill="y")
        self.interior.bind("<Configure>", self._contenido)
        self.lienzo.bind("<Configure>", self._viewport)
        for w in (self.lienzo, self.interior):
            w.bind("<Enter>", self._activar_rueda)
            w.bind("<Leave>", self._desactivar_rueda)

    def _contenido(self, _ev=None):
        self.lienzo.configure(scrollregion=self.lienzo.bbox("all"))

    def _viewport(self, ev):
        self.lienzo.itemconfigure(self._ventana, width=ev.width)
        # alto 0 = tamano natural del interior
        sobra = ev.height if self.interior.winfo_reqheight() < ev.height else 0
        self.lienzo.itemconfigure(self._ventana, height=sobra)

    # --- rueda del raton ---
    def _activar_rueda(self, _ev=None):
        self.lienzo.bind_all("<MouseWheel>", self._rueda)
        self.lienzo.bind_all("<Button-4>", self._rueda)
        self.lienzo.bind_all("<Button-5>", self._rueda)

    def _desactivar_rueda(self, _ev=None):
        for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.lienzo.unbind_all(ev)

    def _rueda(self, ev):
        if not self._hay_scroll():
            return
        if getattr(ev, "num", None) == 4:
            paso = -1
        elif getattr(ev, "num", None) == 5:
            paso = 1
        else:
            paso = -1 if ev.delta > 0 else 1
        self.lienzo.yview_scroll(paso, "units")

    def _hay_scroll(self):
        return self.interior.winfo_reqheight() > self.lienzo.winfo_height()


# =============================================================================
# APLICACION
# =============================================================================

class AppMZ:

    FRAME_MS = 50
    MAX_PUNTOS = 900
    ESPERA_EXTERNO = 20.0   # s sin ordenes tras los que se recupera una zona
    # (titulo, ancho) de cada columna de la tabla de zonas
    COLUMNAS = (("Zona", 5), ("Modo", 9), ("Banda baja", 10), ("Banda alta", 10),
                ("Actuador", 10), ("Humedad", 8), ("Temp.", 6),
                ("Aporte", 7), ("Envio", 7))

    def __init__(self, root, sim, lock, flags, service, log_queue):
        self.root, self.sim, self.lock = root, sim, lock
        self.flags, self.service, self.log_queue = flags, service, log_queue
        self.hist = []                 # muestras (dict de estado global)
        self.ctrl = [HisteresisZona() for _ in range(sim.n_zones)]
        self.manual = [False] * sim.n_zones
        self.filas = []                # widgets por zona
        self.n_vista = sim.n_zones
        self.procesos = []
        self.var_param = {}
        self._avisos = []       # mensajes generados dentro del lazo (con lock)
        root.title("Cama de vermicompostaje multizona")
        root.configure(bg=UI["bg"])
        root.geometry("1320x820")
        root.minsize(940, 520)
        self._estilo()
        self._construir()
        self._reconstruir_zonas()
        self.root.protocol("WM_DELETE_WINDOW", self._cerrar)
        self._tick()

    # ---- estilo --------------------------------------------------------------
    def _estilo(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=UI["bg"], foreground=UI["text"])
        for nombre, bg in (("TFrame", UI["bg"]), ("Card.TFrame", UI["panel"])):
            st.configure(nombre, background=bg)
        st.configure("TLabel", background=UI["bg"], foreground=UI["text"])
        st.configure("Card.TLabel", background=UI["panel"], foreground=UI["text"])
        st.configure("Dim.TLabel", background=UI["panel"], foreground=UI["dim"])
        st.configure("TButton", background=UI["panel2"], foreground=UI["text"],
                     borderwidth=0, padding=6)
        st.map("TButton", background=[("active", UI["stroke2"])])
        st.configure("TCombobox", fieldbackground=UI["panel2"], background=UI["panel2"],
                     foreground=UI["text"], arrowcolor=UI["text"],
                     bordercolor=UI["stroke"], lightcolor=UI["panel2"],
                     darkcolor=UI["panel2"], selectbackground=UI["panel2"],
                     selectforeground=UI["text"])
        st.map("TCombobox",
               fieldbackground=[("readonly", UI["panel2"]), ("disabled", UI["panel"])],
               foreground=[("readonly", UI["text"])],
               selectbackground=[("readonly", UI["panel2"])],
               selectforeground=[("readonly", UI["text"])])
        st.configure("TNotebook", background=UI["bg"], borderwidth=0)
        st.configure("TNotebook.Tab", background=UI["panel"], foreground=UI["dim"],
                     padding=(16, 8), borderwidth=0)
        st.map("TNotebook.Tab", background=[("selected", UI["panel2"])],
               foreground=[("selected", UI["text"])])
        st.configure("TScale", background=UI["bg"], troughcolor=UI["panel2"],
                     bordercolor=UI["stroke"], lightcolor=UI["accent"],
                     darkcolor=UI["accent"])
        st.configure("Card.TScale", background=UI["panel"], troughcolor=UI["panel2"],
                     lightcolor=UI["accent"], darkcolor=UI["accent"])
        st.configure("TEntry", fieldbackground=UI["panel2"], foreground=UI["text"])
        st.configure("TScrollbar", background=UI["panel2"], troughcolor=UI["bg"],
                     bordercolor=UI["stroke"], arrowcolor=UI["dim"], relief="flat")
        st.map("TScrollbar", background=[("active", UI["stroke2"])])

    # ---- estructura ----------------------------------------------------------
    def _construir(self):
        self._barra_superior()
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        self.tab_cama = ttk.Frame(self.nb)
        self.tab_tele = ttk.Frame(self.nb)
        self.tab_par = ttk.Frame(self.nb)
        self.tab_log = ttk.Frame(self.nb)
        self.nb.add(self.tab_cama, text="Cama")
        self.nb.add(self.tab_tele, text="Telemetria")
        self.nb.add(self.tab_par, text="Parametros")
        self.nb.add(self.tab_log, text="Registro")
        self._construir_cama()
        self._construir_telemetria()
        self._construir_parametros()
        self._construir_log()

    def _barra_superior(self):
        b = ttk.Frame(self.root)
        b.pack(fill="x", padx=10, pady=(10, 0))
        self.btn_run = ttk.Button(b, text="Pausar", command=self._toggle_run)
        self.btn_run.pack(side="left")
        ttk.Button(b, text="Reiniciar", command=self._reset).pack(side="left", padx=6)
        ttk.Label(b, text="Velocidad").pack(side="left", padx=(18, 6))
        self.var_speed = tk.IntVar(value=int(self.flags.get("speed", 1)))
        esc = ttk.Scale(b, from_=1, to=50, variable=self.var_speed, length=160,
                        command=lambda v: self._set_speed())
        esc.pack(side="left")
        self.lbl_speed = ttk.Label(b, text="x1", width=5)
        self.lbl_speed.pack(side="left", padx=(6, 0))
        self.lbl_reloj = ttk.Label(b, text="", foreground=UI["dim"])
        self.lbl_reloj.pack(side="right")

    # ---- pestana CAMA --------------------------------------------------------
    def _construir_cama(self):
        # Toda la pestana va dentro de un marco con scroll: en pantallas bajas
        # se puede bajar hasta los controles, y en pantallas altas el grafico
        # intermedio se estira para que no sobre espacio.
        self.scroll_cama = MarcoScroll(self.tab_cama)
        self.scroll_cama.pack(fill="both", expand=True)
        cont = self.scroll_cama.interior

        self.canvas = tk.Canvas(cont, bg=UI["panel"], highlightthickness=0, height=260)
        self.canvas.pack(side="top", fill="x", pady=(6, 6))
        self._alto_canvas = 260

        inf = ttk.Frame(cont)
        inf.pack(side="bottom", fill="x", pady=(6, 4))

        medio = ttk.Frame(cont, style="Card.TFrame")
        medio.pack(side="top", fill="both", expand=True)
        ttk.Label(medio, text="  Humedad reciente por zona", style="Dim.TLabel"
                  ).pack(anchor="w", pady=(6, 0))
        self.canvas_mini = tk.Canvas(medio, bg=UI["panel"], highlightthickness=0,
                                     height=140)
        self.canvas_mini.pack(fill="both", expand=True, padx=6, pady=(2, 6))

        # ---- tarjeta izquierda: una fila por zona -------------------------------
        izq = ttk.Frame(inf, style="Card.TFrame")
        izq.pack(side="left", fill="both", expand=True)
        ttk.Label(izq, text="  Control por zona   (0 = cabecera, la mas alta)",
                  style="Card.TLabel", font=("TkDefaultFont", 10, "bold")
                  ).pack(anchor="w", pady=(8, 2))
        cab = ttk.Frame(izq, style="Card.TFrame")
        cab.pack(fill="x", padx=10)
        for txt, w in self.COLUMNAS:
            ttk.Label(cab, text=txt, style="Dim.TLabel", width=w).pack(side="left", padx=2)
        ttk.Label(cab, text="Situacion frente a la banda", style="Dim.TLabel"
                  ).pack(side="left", padx=8)
        self.marco_zonas = ttk.Frame(izq, style="Card.TFrame")
        self.marco_zonas.pack(fill="both", expand=True, padx=10, pady=(2, 10))

        # ---- tarjeta derecha: control por refuerzo ------------------------------
        der = ttk.Frame(inf, style="Card.TFrame", width=430)
        der.pack(side="right", fill="both", padx=(10, 0))
        der.pack_propagate(False)
        ttk.Label(der, text="  Control por refuerzo", style="Card.TLabel",
                  font=("TkDefaultFont", 10, "bold")).grid(row=0, column=0, columnspan=4,
                                                           sticky="w", pady=(8, 2))
        ttk.Label(der, text="Cada zona se controla con su propio proceso y su propia "
                            "tabla, y decide una vez por muestra (Ts). Puedes "
                            "mezclar algoritmos.", style="Dim.TLabel",
                  wraplength=406, justify="left").grid(row=1, column=0, columnspan=4,
                                                       sticky="w", padx=10, pady=(0, 8))
        self.var_algo = tk.StringVar(value="qlearning")
        self.var_zona_rl = tk.StringVar(value="0")
        ttk.Label(der, text="Algoritmo", style="Dim.TLabel").grid(row=2, column=0,
                                                                  sticky="e", padx=(10, 4))
        ttk.Combobox(der, textvariable=self.var_algo, width=12, state="readonly",
                     values=["qlearning", "sarsa", "reinforce"]
                     ).grid(row=2, column=1, sticky="w", pady=3)
        ttk.Label(der, text="acciones: cerrar / abrir", style="Dim.TLabel"
                  ).grid(row=2, column=2, columnspan=2, sticky="w", padx=(12, 10))
        ttk.Label(der, text="Zona", style="Dim.TLabel").grid(row=3, column=0,
                                                             sticky="e", padx=(10, 4))
        self.combo_zona_rl = ttk.Combobox(der, textvariable=self.var_zona_rl, width=5,
                                          state="readonly")
        self.combo_zona_rl.grid(row=3, column=1, sticky="w", pady=3)
        ttk.Button(der, text="Lanzar en esta zona",
                   command=lambda: self._lanzar_rl(int(self.var_zona_rl.get()))
                   ).grid(row=3, column=2, columnspan=2, sticky="ew", padx=(4, 10))
        ttk.Button(der, text="Lanzar en TODAS las zonas",
                   command=lambda: self._lanzar_rl(None)
                   ).grid(row=4, column=0, columnspan=4, sticky="ew", padx=10, pady=(8, 3))
        ttk.Button(der, text="Monitor de aprendizaje", command=self._abrir_monitor
                   ).grid(row=5, column=0, columnspan=2, sticky="ew", padx=(10, 4), pady=3)
        ttk.Button(der, text="Detener controladores", command=self._detener_rl
                   ).grid(row=5, column=2, columnspan=2, sticky="ew", padx=(4, 10), pady=3)
        self.lbl_rl = ttk.Label(der, text="Sin controladores lanzados desde aqui.",
                                style="Dim.TLabel", wraplength=406, justify="left")
        self.lbl_rl.grid(row=6, column=0, columnspan=4, sticky="w", padx=10, pady=(8, 8))
        der.grid_columnconfigure(1, weight=1)
        der.grid_columnconfigure(3, weight=1)

    def _reconstruir_zonas(self):
        """Rehace la fila de controles cuando cambia el numero de zonas."""
        for w in self.marco_zonas.winfo_children():
            w.destroy()
        self.filas = []
        with self.lock:
            n = self.sim.n_zones
            bandas = self.flags.get("bands") or []
            modos = self.flags.get("zone_modes") or []
        while len(bandas) < n:
            bandas.append([0.60, 0.70])
        while len(modos) < n:
            modos.append("auto")
        self.flags["bands"], self.flags["zone_modes"] = bandas[:n], modos[:n]
        self.ctrl = [HisteresisZona(*bandas[i]) for i in range(n)]
        self.manual = (self.manual + [False] * n)[:n]
        self.n_vista = n
        self.combo_zona_rl["values"] = [str(i) for i in range(n)]
        if int(self.var_zona_rl.get() or 0) >= n:
            self.var_zona_rl.set("0")

        for i in range(n):
            fila = ttk.Frame(self.marco_zonas, style="Card.TFrame")
            fila.pack(fill="x", pady=1)
            tk.Label(fila, text=f" Z{i} ", bg=COLOR_ZONA[i % len(COLOR_ZONA)],
                     fg="#0b0f14", font=("TkDefaultFont", 9, "bold"), width=5
                     ).pack(side="left", padx=2)
            v_modo = tk.StringVar(value=MODO_CLAVE.get(modos[i], "Auto"))
            cb = ttk.Combobox(fila, textvariable=v_modo, width=8, state="readonly",
                              values=MODOS)
            cb.pack(side="left", padx=2)
            cb.bind("<<ComboboxSelected>>",
                    lambda e, k=i: self._cambiar_modo(k))
            v_lo = tk.DoubleVar(value=bandas[i][0])
            v_hi = tk.DoubleVar(value=bandas[i][1])
            for var in (v_lo, v_hi):
                sp = tk.Spinbox(fila, textvariable=var, from_=0.05, to=1.30,
                                increment=0.01, width=8, bg=UI["panel2"],
                                fg=UI["text"], relief="flat", justify="center",
                                buttonbackground=UI["stroke2"],
                                command=lambda k=i: self._cambiar_banda(k))
                sp.pack(side="left", padx=4)
                sp.bind("<Return>", lambda e, k=i: self._cambiar_banda(k))
                sp.bind("<FocusOut>", lambda e, k=i: self._cambiar_banda(k))
            btn = tk.Button(fila, text="CERRADO", width=10, relief="flat",
                            bg=UI["panel2"], fg=UI["dim"],
                            command=lambda k=i: self._toggle_manual(k))
            btn.pack(side="left", padx=4)
            l_hum = ttk.Label(fila, text="-", style="Card.TLabel", width=8)
            l_hum.pack(side="left", padx=2)
            l_T = ttk.Label(fila, text="-", style="Card.TLabel", width=6)
            l_T.pack(side="left", padx=2)
            l_ap = ttk.Label(fila, text="-", style="Dim.TLabel", width=7)
            l_ap.pack(side="left", padx=2)
            l_env = ttk.Label(fila, text="-", style="Dim.TLabel", width=7)
            l_env.pack(side="left", padx=2)
            # Barra de situacion: aprovecha el ancho sobrante de la tarjeta para
            # mostrar de un vistazo donde esta cada zona respecto a su banda.
            barra = tk.Canvas(fila, height=18, bg=UI["panel"], highlightthickness=0)
            barra.pack(side="left", fill="x", expand=True, padx=(8, 4))
            self.filas.append({"modo": v_modo, "lo": v_lo, "hi": v_hi, "btn": btn,
                               "hum": l_hum, "T": l_T, "ap": l_ap, "env": l_env,
                               "barra": barra})

    # ---- pestana TELEMETRIA --------------------------------------------------
    def _construir_telemetria(self):
        top = ttk.Frame(self.tab_tele)
        top.pack(fill="x", pady=(8, 0))
        self.var_ver_T = tk.IntVar(value=1)
        self.var_ver_banda = tk.IntVar(value=1)
        ttk.Checkbutton(top, text="Temperatura", variable=self.var_ver_T
                        ).pack(side="left", padx=6)
        ttk.Checkbutton(top, text="Banda objetivo", variable=self.var_ver_banda
                        ).pack(side="left", padx=6)
        ttk.Label(top, text="  (humedad en trazo grueso, una por zona)",
                  foreground=UI["dim"]).pack(side="left")
        self.canvas_tele = tk.Canvas(self.tab_tele, bg=UI["panel"], highlightthickness=0)
        self.canvas_tele.pack(fill="both", expand=True, pady=8)

    # ---- pestana PARAMETROS --------------------------------------------------
    def _construir_parametros(self):
        self.scroll_par = MarcoScroll(self.tab_par)
        self.scroll_par.pack(fill="both", expand=True, pady=8)
        marco = self.scroll_par.interior

        grupo_actual = None
        for clave, grupo, etq, unidad, mn, mx, res, desc in PARAM_SCHEMA:
            if grupo != grupo_actual:
                grupo_actual = grupo
                ttk.Label(marco, text=grupo, font=("TkDefaultFont", 11, "bold")
                          ).pack(anchor="w", pady=(14, 4), padx=8)
            f = ttk.Frame(marco, style="Card.TFrame")
            f.pack(fill="x", padx=8, pady=2)
            ttk.Label(f, text=f"{etq}  [{unidad}]", style="Card.TLabel", width=26
                      ).pack(side="left", padx=8, pady=4)
            with self.lock:
                valor = float(getattr(self.sim.params, clave))
            var = tk.DoubleVar(value=valor)
            self.var_param[clave] = var
            ent = tk.Entry(f, textvariable=var, width=12, bg=UI["panel2"],
                           fg=UI["text"], relief="flat", justify="right")
            ent.pack(side="left", padx=4)
            ent.bind("<Return>", lambda e, k=clave: self._aplicar_param(k))
            ent.bind("<FocusOut>", lambda e, k=clave: self._aplicar_param(k))
            ttk.Scale(f, from_=mn, to=mx, variable=var, length=260,
                      command=lambda v, k=clave: self._aplicar_param(k)).pack(side="left", padx=8)
            ttk.Label(f, text=desc, style="Dim.TLabel", wraplength=520,
                      justify="left").pack(side="left", padx=10)

    # ---- pestana REGISTRO ----------------------------------------------------
    def _construir_log(self):
        self.txt_log = tk.Text(self.tab_log, bg=UI["panel"], fg=UI["text"],
                               relief="flat", font=("TkFixedFont", 9))
        self.txt_log.pack(fill="both", expand=True, pady=8)
        self.txt_log.insert("end", "Servicio local iniciado.\n")
        self.txt_log.configure(state="disabled")

    # =========================================================================
    # ACCIONES
    # =========================================================================
    def _toggle_run(self):
        with self.lock:
            self.flags["running"] = not self.flags.get("running", True)
            corriendo = self.flags["running"]
        self.btn_run.configure(text="Pausar" if corriendo else "Iniciar")

    def _reset(self):
        with self.lock:
            self.sim.reset()
        self.hist = []

    def _set_speed(self):
        v = int(float(self.var_speed.get()))
        with self.lock:
            self.flags["speed"] = v
        self.lbl_speed.configure(text=f"x{v}")

    def _cambiar_modo(self, i):
        modo = CLAVE_MODO.get(self.filas[i]["modo"].get(), "auto")
        with self.lock:
            modos = self.flags.setdefault("zone_modes", [])
            while len(modos) <= i:
                modos.append("auto")
            modos[i] = modo

    def _cambiar_banda(self, i):
        try:
            lo = float(self.filas[i]["lo"].get())
            hi = float(self.filas[i]["hi"].get())
        except (ValueError, tk.TclError):
            return
        if hi <= lo:
            hi = lo + 0.02
            self.filas[i]["hi"].set(round(hi, 3))
        with self.lock:
            bandas = self.flags.setdefault("bands", [])
            while len(bandas) <= i:
                bandas.append([0.60, 0.70])
            bandas[i] = [lo, hi]
            self.flags["band_low"], self.flags["band_high"] = lo, hi
        self.ctrl[i].low, self.ctrl[i].high = lo, hi

    def _toggle_manual(self, i):
        self.manual[i] = not self.manual[i]
        with self.lock:
            modos = self.flags.get("zone_modes") or []
            if i < len(modos) and modos[i] != "manual":
                modos[i] = "manual"
                self.filas[i]["modo"].set("Manual")
            self.sim.set_level(i, 1.0 if self.manual[i] else 0.0)

    def _aplicar_param(self, clave):
        try:
            valor = float(self.var_param[clave].get())
        except (ValueError, tk.TclError):
            return
        info = PARAM_INFO[clave]
        valor = max(info["min"], min(info["max"], valor))
        with self.lock:
            self.sim.set_param(clave, valor)
            n = self.sim.n_zones
        if n != self.n_vista:
            self._reconstruir_zonas()
            self.hist = []

    # ---- lanzamiento de controladores ---------------------------------------
    def _lanzar_rl(self, zona):
        base = os.path.dirname(os.path.abspath(__file__))
        cmd = [sys.executable, os.path.join(base, "rl_zona.py"),
               "--algoritmo", self.var_algo.get(),
               "--port", str(self.service.port)]
        cmd += ["--todas"] if zona is None else ["--zona", str(zona)]
        try:
            self.procesos.append(subprocess.Popen(cmd, cwd=base))
        except OSError as e:
            self._log(f"[gui] No se pudo lanzar el controlador: {e}")
            return
        destino = "todas las zonas" if zona is None else f"la zona {zona}"
        self._log(f"[gui] Controlador {self.var_algo.get()} lanzado en {destino}. "
                  f"Se entrena primero en acelerado y luego pasa a tiempo real.")
        self.lbl_rl.configure(
            text=f"{len(self.procesos)} lanzamiento(s). Las zonas controladas pasan "
                 f"a 'Externo' solas. Mira la ventana del monitor para ver como "
                 f"cambian las tablas.")

    def _abrir_monitor(self):
        """Abre la ventana de monitorizacion. Su salida se guarda en un archivo:
        si el proceso muere al arrancar, sin esto no se veria nada en absoluto y
        pareceria que el boton no hace nada."""
        base = os.path.dirname(os.path.abspath(__file__))
        os.makedirs(os.path.join(base, "estado_rl"), exist_ok=True)
        registro = os.path.join(base, "estado_rl", "monitor.log")
        try:
            f = open(registro, "w", encoding="utf-8")
            p = subprocess.Popen([sys.executable, os.path.join(base, "rl_monitor.py")],
                                 cwd=base, stdout=f, stderr=subprocess.STDOUT)
        except OSError as e:
            self._log(f"[gui] No se pudo abrir el monitor: {e}")
            return
        self.procesos.append(p)
        self._log("[gui] Monitor de aprendizaje abierto. Si no aparece ninguna "
                  "ventana, mira el detalle mas abajo.")
        self.root.after(2000, lambda: self._revisar_monitor(p, registro))

    def _revisar_monitor(self, p, registro):
        if p.poll() is None:
            return                      # sigue vivo: la ventana esta abierta
        detalle = ""
        try:
            with open(registro, "r", encoding="utf-8") as f:
                detalle = f.read().strip().splitlines()[-3:]
            detalle = " | ".join(detalle)
        except OSError:
            pass
        self._log(f"[gui] El monitor se cerro nada mas arrancar (codigo {p.returncode}). "
                  f"{detalle or 'Sin detalle.'}")
        self._log("[gui] Comprueba desde una terminal:  python3 rl_monitor.py --test")

    def _detener_rl(self):
        for p in self.procesos:
            try:
                p.terminate()
            except OSError:
                pass
        self.procesos = []
        with self.lock:
            modos = self.flags.get("zone_modes") or []
            for i, m in enumerate(modos):
                if m == "externo":
                    modos[i] = "manual"
                    if i < len(self.filas):
                        self.filas[i]["modo"].set("Manual")
        self.lbl_rl.configure(text="Controladores detenidos.")

    def _log(self, msg):
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", msg + "\n")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    # =========================================================================
    # LAZO PRINCIPAL
    # =========================================================================
    def _tick(self):
        with self.lock:
            corriendo = self.flags.get("running", True)
            externo = self.flags.get("external_step_mode", False)
            if corriendo and not externo:
                for _ in range(int(self.flags.get("speed", 1))):
                    self._decidir_actuadores()
                    self.sim.step()
            nuevas = self.sim.pop_new_samples()
            estado = self.sim.get_state()
            n = self.sim.n_zones
        if n != self.n_vista:
            self._reconstruir_zonas()
        self.hist.extend(nuevas)
        if len(self.hist) > self.MAX_PUNTOS:
            del self.hist[:len(self.hist) - self.MAX_PUNTOS]
        for aviso in self._avisos:
            self._log(aviso)
            try:
                i = int(aviso.split("zona ")[1].split()[0])
                if i < len(self.filas):
                    self.filas[i]["modo"].set("Auto")
            except (IndexError, ValueError):
                pass
        self._avisos = []
        self._drenar_log()
        self.lbl_reloj.configure(
            text=f"t = {estado['time_hours']:.2f} h   |   muestras: {estado['sample_index']}"
                 f"   |   agua: {estado['water_kg']:.1f} kg   |   "
                 f"lixiviado: {estado['leachate_kg']:.1f} kg")
        self._refrescar_filas(estado)
        pestana = self.nb.index(self.nb.select())
        if pestana == 0:
            self._pintar(estado)
            self._pintar_mini()
        elif pestana == 1:
            self._pintar_telemetria()
        self.root.after(self.FRAME_MS, self._tick)

    def _decidir_actuadores(self):
        """Decide el actuador de cada zona segun su modo. Las zonas en 'Externo'
        no se tocan: las gobierna el proceso de control. Si ese proceso deja de
        dar senales de vida (se cerro sin liberar la zona), se cierra el riego y
        la zona vuelve a automatico: una valvula abierta y sin dueno inundaria
        el lecho."""
        modos = self.flags.get("zone_modes") or []
        bandas = self.flags.get("bands") or []
        latidos = self.flags.get("latido_externo") or {}
        ahora = time.time()
        estado = self.sim.get_state()
        for i in range(self.sim.n_zones):
            modo = modos[i] if i < len(modos) else "auto"
            if modo == "externo":
                ultimo = latidos.get(i, latidos.get(str(i), 0.0))
                if ultimo and (ahora - ultimo) > self.ESPERA_EXTERNO:
                    modos[i] = "auto"
                    latidos.pop(i, None)
                    latidos.pop(str(i), None)
                    self.sim.set_level(i, 0.0)
                    self._avisos.append(
                        f"[gui] La zona {i} llevaba {self.ESPERA_EXTERNO:.0f} s sin "
                        f"ordenes de su controlador: riego cerrado y zona devuelta "
                        f"a modo automatico.")
                    modo = "auto"
            if modo == "auto":
                if i < len(bandas):
                    self.ctrl[i].low, self.ctrl[i].high = bandas[i]
                self.sim.set_level(i, self.ctrl[i].decidir(
                    estado["zones"][i]["moisture"]))
            elif modo == "manual":
                self.sim.set_level(i, 1.0 if self.manual[i] else 0.0)

    def _drenar_log(self):
        try:
            while True:
                self._log(self.log_queue.get_nowait())
        except queue.Empty:
            pass

    # ---- dibujo de la cama ---------------------------------------------------
    def _pintar(self, s):
        c = self.canvas
        c.delete("all")
        W = max(400, c.winfo_width())
        H = c.winfo_height()  # solo informativo: el alto se fija abajo
        n = s["n_zones"]
        with self.lock:
            pend = self.sim.params.bed_slope
            radio = self.sim.params.spray_radius
            zlen = self.sim.zone_len
            # Humedad (W/W_fc) a la que el lecho queda saturado: es el "lleno"
            # fisico del dibujo, en vez de un tope arbitrario.
            m_sat = max(1.01, self.sim.params.saturation_capacity /
                        max(1e-9, self.sim.params.field_capacity))
            bandas = list(self.flags.get("bands") or [])
            modos = list(self.flags.get("zone_modes") or [])
        zonas, y_sup, x0, x1, alto, necesario = geometria_cama(W, n, pend)
        # El lienzo se recorta EXACTAMENTE a lo que ocupa el dibujo: sin esto la
        # cama flotaba arriba y dejaba un hueco muerto en toda la pestana.
        necesario = int(necesario)
        if abs(necesario - self._alto_canvas) > 4:
            self._alto_canvas = necesario
            c.configure(height=necesario)

        c.create_text(16, 16, anchor="w", fill=UI["text"], font=("TkDefaultFont", 11, "bold"),
                      text=f"Cama de {s['bed_length']:.1f} m  |  {n} zonas de "
                           f"{zlen:.2f} m  |  {s.get('subzonas', 1)} sub-zonas por zona "
                           f"({s.get('n_cells', n)} celdas de {s.get('cell_length', zlen):.2f} m)"
                           f"  |  pendiente {pend:.2f} %  |  aspersion {radio:.2f} m")
        c.create_text(16, 36, anchor="w", fill=UI["dim"],
                      text="El agua baja celda a celda: dentro de una zona el riego tarda "
                           "en repartirse y la humedad mostrada es el promedio de sus sub-zonas.")

        # --- suelo y zonas ---
        for z in zonas:
            i = z["i"]
            zs = s["zones"][i]
            m = zs["moisture"]
            lo, hi = (bandas[i] if i < len(bandas) else [0.6, 0.7])
            ytop0, ytop1 = z["y0"], z["y1"]
            ybot0, ybot1 = ytop0 + alto, ytop1 + alto
            c.create_polygon(z["x0"], ytop0, z["x1"], ytop1, z["x1"], ybot1,
                             z["x0"], ybot0, fill=UI["soil_dk"], outline=UI["stroke2"])
            # Relleno de agua: una franja por SUB-ZONA, para que se vea el frente
            # de humedad avanzar dentro de la zona en vez de un bloque uniforme.
            frac = max(0.0, min(1.0, m / m_sat))
            celdas = zs.get("cells") or [m]
            ncel = len(celdas)
            ancho_cel = (z["x1"] - z["x0"]) / ncel
            for j, mc in enumerate(celdas):
                fc = max(0.0, min(1.0, mc / m_sat))
                cx0 = z["x0"] + j * ancho_cel
                cx1 = cx0 + ancho_cel
                yt0 = y_sup(cx0) + alto * (1 - fc)
                yt1 = y_sup(cx1) + alto * (1 - fc)
                c.create_polygon(cx0, yt0, cx1, yt1, cx1, y_sup(cx1) + alto,
                                 cx0, y_sup(cx0) + alto,
                                 fill=color_humedad(mc, lo, hi), outline="")
                if ncel > 1 and j:
                    c.create_line(cx0, yt0, cx0, y_sup(cx0) + alto,
                                  fill=UI["panel"], width=1)
            # marcas de banda objetivo
            for val, col in ((lo, UI["ok"]), (hi, UI["ok"])):
                yy0 = ybot0 - alto * max(0.0, min(1.0, val / m_sat))
                yy1 = ybot1 - alto * max(0.0, min(1.0, val / m_sat))
                c.create_line(z["x0"], yy0, z["x1"], yy1, fill=col, dash=(3, 3))
            # Etiquetas: el texto oscuro solo se lee sobre el agua, asi que se
            # elige el color segun donde caiga cada linea respecto al nivel.
            cx = z["xc"]
            y_sustrato = (y_sup(cx) + y_sup(cx) + alto) / 2 - 6
            nivel_y = y_sup(cx) + alto * (1 - frac)

            def tinta(y):
                return "#08131c" if y > nivel_y else UI["text"]
            c.create_text(cx, y_sustrato, fill=tinta(y_sustrato),
                          font=("TkDefaultFont", 10, "bold"),
                          text=f"Z{i}   {m*100:.0f} %")
            c.create_text(cx, y_sustrato + 17, fill=tinta(y_sustrato + 17),
                          font=("TkFixedFont", 9), text=f"{zs['temperature']:.1f} C")
            modo = modos[i] if i < len(modos) else "auto"
            col_modo = UI["accent"] if modo == "externo" else UI["dim"]
            c.create_text(cx, y_sup(cx) + alto + 15, fill=col_modo,
                          font=("TkFixedFont", 8), text=modo)

        # --- aspersores y su cono de riego ---
        escala_px = (x1 - x0) / max(1e-9, s["bed_length"])
        for z in zonas:
            i = z["i"]
            abierto = s["levels"][i] > 0.0
            nivel = s["levels"][i]
            xt, yt = z["xc"], y_sup(z["xc"]) - 44
            c.create_line(xt, yt, xt, y_sup(z["xc"]) - 12,
                          fill=UI["stroke2"], width=3)
            c.create_oval(xt - 7, yt - 7, xt + 7, yt + 7,
                          fill=UI["water"] if abierto else UI["panel2"],
                          outline=UI["stroke2"])
            c.create_text(xt, yt - 18, fill=UI["dim"], font=("TkFixedFont", 8),
                          text=f"A{i}")
            if abierto:
                r_px = radio * escala_px
                c.create_polygon(xt, yt + 6, xt - r_px, y_sup(max(x0, xt - r_px)),
                                 xt + r_px, y_sup(min(x1, xt + r_px)),
                                 fill=mezcla(UI["panel"], UI["water"], 0.22 * nivel),
                                 outline="")
                for k in range(-3, 4):
                    xd = xt + k * r_px / 3.5
                    c.create_line(xt, yt + 8, xd, y_sup(min(max(xd, x0), x1)) - 4,
                                  fill=UI["water"], width=1, dash=(2, 6))

        # --- flechas de escorrentia entre zonas ---
        for i in range(n - 1):
            q = s["zones"][i]["flux_lateral_out"]
            if q <= 1e-9:
                continue
            x = zonas[i]["x1"]
            y = y_sup(x) + alto * 0.72
            grosor = max(1, min(7, int(q / max(1e-9, s["zones"][i]["inflow_rate"]) * 10)))
            c.create_line(x - 16, y, x + 16, y + 6, fill=UI["water"], width=grosor,
                          arrow="last")
        # lixiviado final
        if s["zones"][n - 1]["flux_lateral_out"] > 1e-9:
            x = zonas[n - 1]["x1"]
            yb = y_sup(x) + alto
            c.create_line(x, yb - alto * 0.2, x + 22, yb + 6,
                          fill=UI["dim"], width=2, arrow="last")
            c.create_text(x + 24, yb + 16, anchor="e", fill=UI["dim"],
                          font=("TkFixedFont", 8), text="lixiviado")


    def _refrescar_filas(self, s):
        for i, f in enumerate(self.filas):
            if i >= len(s["zones"]):
                break
            zs = s["zones"][i]
            abierto = zs["valve_level"] > 0.0
            f["btn"].configure(
                text="ABIERTO" if abierto else "CERRADO",
                bg=UI["water_dk"] if abierto else UI["panel2"],
                fg=UI["water"] if abierto else UI["dim"])
            lo, hi = f["lo"].get(), f["hi"].get()
            m = zs["moisture"]
            col = UI["ok"] if lo <= m <= hi else (UI["bad"] if m > hi else UI["gold"])
            f["hum"].configure(text=f"{m:.3f}", foreground=col)
            f["T"].configure(text=f"{zs['temperature']:.1f}")
            ref = max(1e-12, zs.get("flux_ref", 6e-5))
            f["ap"].configure(text=f"{zs['flux_lateral_in'] / ref:.2f}")
            f["env"].configure(text=f"{zs['flux_lateral_out'] / ref:.2f}")
            self._pintar_barra(f["barra"], m, lo, hi, zs)

    def _pintar_barra(self, c, m, lo, hi, zs):
        """Barra por zona: escala de humedad con la banda objetivo marcada, la
        posicion actual y hacia donde se mueve."""
        c.delete("all")
        W, H = c.winfo_width(), c.winfo_height()
        if W < 60 or H < 8:
            return
        m_max = 1.4
        y0, y1 = 3, H - 3

        def px(v):
            return 4 + (W - 8) * max(0.0, min(1.0, v / m_max))
        c.create_rectangle(4, y0, W - 4, y1, fill=UI["panel2"], outline="")
        c.create_rectangle(px(lo), y0, px(hi), y1, fill="#14532d", outline=UI["ok"])
        c.create_rectangle(4, y0, px(m), y1,
                           fill=color_humedad(m, lo, hi), outline="")
        lim = hi + 0.05
        c.create_line(px(lim), y0 - 1, px(lim), y1 + 1, fill=UI["bad"], width=2)
        # Rango ocupado por las sub-zonas: ancho = cuanto le falta a la zona
        # para estar homogenea.
        mn, mx = zs.get("moisture_min", m), zs.get("moisture_max", m)
        if mx - mn > 1e-4:
            c.create_line(px(mn), (y0 + y1) / 2, px(mx), (y0 + y1) / 2,
                          fill="#0b0f14", width=3)
            for v in (mn, mx):
                c.create_line(px(v), y0 + 2, px(v), y1 - 2, fill="#0b0f14", width=1)
        x = px(m)
        c.create_line(x, y0 - 2, x, y1 + 2, fill=UI["text"], width=2)
        # flecha de tendencia
        tend = zs.get("trend", 0.0) or 0.0
        if abs(tend) > 1e-6:
            d = 9 if tend > 0 else -9
            c.create_line(x, (y0 + y1) / 2, x + d, (y0 + y1) / 2,
                          fill=UI["text"], width=1, arrow="last")
        c.create_text(W - 8, (y0 + y1) / 2, anchor="e", fill=UI["dim"],
                      font=("TkFixedFont", 7), text=f"{m_max:.1f}")

    # ---- telemetria ----------------------------------------------------------
    def _pintar_telemetria(self):
        """Grafico grande de la pestana Telemetria."""
        self._dibujar_grafico(self.canvas_tele, compacto=False)

    def _pintar_mini(self):
        """Version reducida dentro de la pestana Cama: aprovecha el espacio que
        antes quedaba vacio entre el dibujo y los controles."""
        self._dibujar_grafico(self.canvas_mini, compacto=True)

    def _dibujar_grafico(self, c, compacto=False):
        c.delete("all")
        W = max(320, c.winfo_width())
        H = max(90, c.winfo_height())
        if len(self.hist) < 2:
            c.create_text(W / 2, H / 2, fill=UI["dim"], text="Esperando muestras...")
            return
        ver_T = (not compacto) and bool(self.var_ver_T.get())
        ver_banda = compacto or bool(self.var_ver_banda.get())
        datos = decimar(self.hist, 700 if not compacto else 400)
        n = len(datos[0]["zones"])
        x0 = 46 if compacto else 58
        x1 = W - (14 if not ver_T else 60)
        y0 = 10 if compacto else 30
        y1 = H - (20 if compacto else 44)
        if x1 - x0 < 40 or y1 - y0 < 30:
            return
        t0, t1 = datos[0]["time_hours"], datos[-1]["time_hours"]
        span_t = max(1e-9, t1 - t0)
        m_max = max(1.0, max(max(z["moisture"] for z in d["zones"]) for d in datos)) * 1.1
        T_lo = min(min(z["temperature"] for z in d["zones"]) for d in datos) - 1
        T_hi = max(max(z["temperature"] for z in d["zones"]) for d in datos) + 1

        def px(t):
            return x0 + (t - t0) / span_t * (x1 - x0)

        def py_m(m):
            return y1 - (m / m_max) * (y1 - y0)

        def py_T(T):
            return y1 - (T - T_lo) / max(1e-9, T_hi - T_lo) * (y1 - y0)

        c.create_rectangle(x0, y0, x1, y1, outline=UI["stroke"], fill=UI["panel2"])
        pasos = 3 if compacto else 5
        for k in range(pasos + 1):
            m = m_max * k / pasos
            y = py_m(m)
            c.create_line(x0, y, x1, y, fill=UI["stroke"])
            c.create_text(x0 - 6, y, anchor="e", fill=UI["dim"],
                          font=("TkFixedFont", 8), text=f"{m:.2f}")
        if not compacto:
            c.create_text(x0 - 8, y0 - 14, anchor="e", fill=UI["water"],
                          font=("TkFixedFont", 8), text="humedad")
        if ver_T:
            for k in range(6):
                T = T_lo + (T_hi - T_lo) * k / 5
                c.create_text(x1 + 8, py_T(T), anchor="w", fill=UI["temp"],
                              font=("TkFixedFont", 8), text=f"{T:.0f}")
            c.create_text(x1 + 8, y0 - 14, anchor="w", fill=UI["temp"],
                          font=("TkFixedFont", 8), text="C")
        for k in range(pasos + 1):
            t = t0 + span_t * k / pasos
            c.create_text(px(t), y1 + 10, fill=UI["dim"], font=("TkFixedFont", 8),
                          text=f"{t:.1f}")
        if not compacto:
            c.create_text((x0 + x1) / 2, y1 + 30, fill=UI["dim"], text="tiempo (h)")

        if ver_banda:
            with self.lock:
                bandas = list(self.flags.get("bands") or [])
            if bandas:
                lo = min(b[0] for b in bandas)
                hi = max(b[1] for b in bandas)
                c.create_rectangle(x0, py_m(hi), x1, py_m(lo), outline=UI["ok"],
                                   dash=(4, 3))
        for i in range(n):
            col = COLOR_ZONA[i % len(COLOR_ZONA)]
            pts = []
            for d in datos:
                pts += [px(d["time_hours"]), py_m(d["zones"][i]["moisture"])]
            if len(pts) >= 4:
                c.create_line(*pts, fill=col, width=2)
            if ver_T:
                ptsT = []
                for d in datos:
                    ptsT += [px(d["time_hours"]), py_T(d["zones"][i]["temperature"])]
                if len(ptsT) >= 4:
                    c.create_line(*ptsT, fill=col, width=1, dash=(3, 3))
            c.create_text(x0 + 8 + i * (52 if compacto else 78), y0 + 10, anchor="w",
                          fill=col, font=("TkFixedFont", 9, "bold"), text=f"Z{i}")

    # ---- cierre --------------------------------------------------------------
    def _cerrar(self):
        self._detener_rl()
        try:
            self.service.stop()
        except Exception:
            pass
        self.root.destroy()


# =============================================================================
# ARRANQUE
# =============================================================================

def run_gui(host=HOST, port=PORT):
    if not _HAS_TK:
        print("Tkinter no esta disponible. Instala 'python3-tk' o usa "
              "'python3 vermicompost_mz.py --serve'.")
        return 1
    sim = MultiZoneBed()
    lock = threading.Lock()
    flags = flags_por_defecto(sim.n_zones)
    cola = queue.Queue()
    svc = MultiZoneService(sim, lock, flags, host, port, log=cola.put)
    svc.start()
    root = tk.Tk()
    AppMZ(root, sim, lock, flags, svc, cola)
    root.mainloop()
    svc.stop()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Interfaz de la cama multizona.")
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args(argv)
    return run_gui(args.host, args.port)


if __name__ == "__main__":
    raise SystemExit(main())
