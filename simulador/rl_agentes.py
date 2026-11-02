#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rl_agentes.py  -  LAS TRES ESTRATEGIAS DE CONTROL POR REFUERZO (multizona)
==========================================================================

Un agente por ZONA. Los tres cumplen la misma interfaz (ver rl_common_mz.py) y
los tres publican una INSTANTANEA de su estado interno -> snapshot() -> que la
ventana de monitorizacion dibuja en vivo.

  1) Q-LEARNING          TD de un paso, OFF-POLICY, tabular.
        Q(s,a) <- Q(s,a) + alpha * [ r + gamma * max_a' Q(s',a') - Q(s,a) ]
     Aprende el valor de actuar OPTIMAMENTE despues, aunque explore. Rapido y
     agresivo; tiende a conmutar mas.

  2) SARSA(lambda)       TD, ON-POLICY, con TRAZAS DE ELEGIBILIDAD.
        delta = r + gamma*Q(s',a') - Q(s,a)      (a' = accion realmente tomada)
        e(s,a) <- e(s,a) + 1 ; Q <- Q + alpha*delta*e ; e <- gamma*lambda*e
     Valora el riesgo de su propia exploracion: mas conservador y suave. Las
     trazas propagan el credito hacia atras, util aqui porque el efecto del
     riego tarda varias muestras en verse (y mas aun aguas abajo).

  3) REINFORCE           POLICY GRADIENT, Monte Carlo, con linea base y entropia.
        pi(a|s) = softmax(theta[a] . phi(s))
        theta[a] += alpha * (G_t - b) * grad log pi(a_t|s_t)
     No discretiza: usa el vector continuo de caracteristicas, asi que
     generaliza entre estados parecidos y aprovecha bien las entradas nuevas
     (tendencia, carga desde la zona de arriba).

ACCIONES
--------
Dos: cerrar (0) y abrir (1). El actuador real es una electrovalvula de todo o
nada, asi que la politica aprendida es directamente aplicable.

ESTADO
------
Ni Q-learning ni SARSA usan ya la TEMPERATURA. La tabla es
(humedad propia, humedad de la zona de ARRIBA, tendencia, humedad de la de
ABAJO): en una cama en cascada lo que condiciona a una zona son sus vecinas, y
el efecto termico llega igual a traves de la tendencia de la humedad.
"""

import json
import math
import os
import random

from rl_common_mz import DiscretizadorZona, features, CARACTERISTICAS, NF, ruta_tabla


# El actuador es una electrovalvula de todo o nada: dos acciones y punto.
ACCIONES = [0.0, 1.0]
ETIQUETAS_ACCION = ["cerrar", "abrir"]


# =============================================================================
# BASE COMUN
# =============================================================================

class AgenteBase:
    nombre = "base"
    tipo = "tabla"

    def __init__(self, zona=0, acciones=None, path=None):
        self.zona = zona
        self.acciones = list(acciones or ACCIONES)
        self.etiquetas = list(ETIQUETAS_ACCION[:len(self.acciones)])
        self.path = path or ruta_tabla(zona, self.etiqueta())
        self.ultimo_update = {}
        self.n_updates = 0

    @classmethod
    def etiqueta(cls):
        return cls.nombre.lower().replace("(", "").replace(")", "").replace(" ", "_")

    # ---- persistencia --------------------------------------------------------
    def existe_guardado(self):
        return os.path.exists(self.path)

    def _volcar(self, datos, path=None):
        path = path or self.path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(datos, f)
        os.replace(tmp, path)

    def _cargar(self, path=None):
        path = path or self.path
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    # ---- interfaz (la implementan las subclases) -----------------------------
    def act(self, o, explore=False):
        raise NotImplementedError

    def observe(self, o, a, r, o2, a2, done):
        raise NotImplementedError

    def end_episode(self):
        pass

    def on_reset(self):
        pass

    def snapshot(self):
        raise NotImplementedError


# =============================================================================
# BASE TABULAR (Q-learning y SARSA comparten tabla, exploracion y volcado)
# =============================================================================

class AgenteTabular(AgenteBase):

    def __init__(self, zona=0, disc: DiscretizadorZona = None, acciones=None,
                 alpha=0.2, gamma=0.95, epsilon_inicial=1.0, epsilon_final=0.02,
                 epsilon_decay=0.9995, epsilon_online=0.02, path=None):
        super().__init__(zona, acciones, path)
        self.disc = disc or DiscretizadorZona()
        self.alpha, self.gamma = alpha, gamma
        self.epsilon = epsilon_inicial
        self.epsilon_final = epsilon_final
        self.epsilon_decay = epsilon_decay
        # Suelo de exploracion SOLO en tiempo real: offline conviene bajar hasta
        # epsilon_final para que la evaluacion greedy sea representativa; en
        # linea se mantiene algo de exploracion para poder seguir adaptandose.
        self.epsilon_online = epsilon_online
        self.en_linea = False
        self.q = {}
        self.visitas = {}
        self.ultima_celda = None
        self.respaldos = 0          # veces que hubo que ceder a la histeresis
        self.sin_experiencia = 0    # decisiones en celdas nunca actualizadas

    @property
    def nA(self):
        return len(self.acciones)

    def _fila(self, key):
        if key not in self.q:
            self.q[key] = [0.0] * self.nA
        return self.q[key]

    def act(self, o, explore=False):
        key = self.disc.key(o)
        fila = self._fila(key)
        self.ultima_celda = key
        if explore and random.random() < self.epsilon:
            return random.randrange(self.nA)
        if self.visitas.get(key, 0) == 0:
            self.sin_experiencia += 1
        if self.visitas.get(key, 0) == 0 and not any(fila):
            # Celda SIN EXPERIENCIA. Con la tabla a cero todas las acciones
            # empatan y elegir por desempate es tirar una moneda: en un estado
            # que nunca vio, el agente decidiria al azar si regar o no. En su
            # lugar cede el mando al control clasico por histeresis, que es
            # seguro aunque no sea optimo. Solo afecta a celdas virgenes: en
            # cuanto hay una sola actualizacion manda lo aprendido.
            self.respaldos += 1
            return self.accion_de_respaldo(o)
        mejor = max(fila)
        candidatos = [i for i, v in enumerate(fila) if v == mejor]
        return candidatos[0] if len(candidatos) == 1 else random.choice(candidatos)

    def sembrar_histeresis(self, low, high, sesgo=1.0):
        """Arranque por imitacion: se rellena la tabla con la accion que tomaria
        un control clasico en cada celda, con una ventaja pequena.

        Partir de ceros obliga al agente a descubrir por ensayo y error algo que
        ya sabemos (regar si esta seco), y con epsilon decayendo rapido muchas
        ejecuciones se quedaban atascadas en una politica mala. Sembrando, la
        exploracion ocurre ALREDEDOR de un controlador que ya funciona, y el
        aprendizaje solo tiene que mejorarlo. El sesgo es pequeno: bastan unas
        pocas visitas con recompensa real para sobrescribirlo donde se
        equivoque.

        En banda no hay accion obvia, asi que se usa la tendencia: bajando ->
        abrir, subiendo -> cerrar. Es, en la practica, un control proporcional
        derivativo hecho a mano.
        """
        d = self.disc
        i_abrir = max(range(self.nA), key=lambda i: self.acciones[i])
        i_cerrar = min(range(self.nA), key=lambda i: self.acciones[i])
        n = 0
        for mb in range(d.moist_bins):
            m = d.centro_humedad(mb)
            for ub in range(d.up_bins):
                for tr in range(3):
                    for ab in range(3):
                        if m < low:
                            a = i_abrir
                        elif m > high:
                            a = i_cerrar
                        elif tr == 0:
                            a = i_abrir
                        elif tr == 2:
                            a = i_cerrar
                        else:
                            continue
                        fila = self._fila((mb, ub, tr, ab))
                        fila[a] = sesgo
                        n += 1
        return n

    def accion_de_respaldo(self, o):
        """Histeresis sobre la banda: abrir por debajo, cerrar por encima y
        mantener dentro."""
        m = float(o.get("moisture", 0.0))
        low = float(o.get("band_low", 0.60))
        high = float(o.get("band_high", 0.70))
        if m < low:
            objetivo = 1.0
        elif m > high:
            objetivo = 0.0
        else:
            objetivo = float(o.get("nivel_previo", 0.0))
        return min(range(self.nA), key=lambda i: abs(self.acciones[i] - objetivo))

    def _decaer_epsilon(self):
        if self.epsilon > self.epsilon_final:
            self.epsilon *= self.epsilon_decay
        piso = self.epsilon_online if self.en_linea else self.epsilon_final
        if self.epsilon < piso:
            self.epsilon = piso

    def reiniciar_exploracion(self, valor=0.35):
        """Reabre la exploracion cuando el entrenamiento se estanca. Con el
        decaimiento normal epsilon llega al minimo en tres episodios: si para
        entonces la politica no ha dado con la solucion, se queda explotando una
        mala y ya no sale. Esto la saca."""
        self.epsilon = max(self.epsilon, valor)

    def activar_online(self):
        """Al pasar a tiempo real se recupera un minimo de exploracion."""
        self.en_linea = True
        self.epsilon = max(self.epsilon, self.epsilon_online)

    def _anotar(self, key, a, antes, despues, delta):
        self.visitas[key] = self.visitas.get(key, 0) + 1
        self.n_updates += 1
        self.ultima_celda = key
        self.ultimo_update = {"celda": list(key), "accion": a, "q_antes": antes,
                              "q_despues": despues, "td": delta,
                              "visitas": self.visitas[key]}

    # ---- persistencia --------------------------------------------------------
    def save(self, path=None):
        self._volcar({"tipo": "tabla", "algoritmo": self.nombre, "zona": self.zona,
                      "acciones": self.acciones, "bins": self.disc.info(),
                      "firma": self.firma(), "epsilon": self.epsilon,
                      "q": {",".join(map(str, k)): v for k, v in self.q.items()},
                      "visitas": {",".join(map(str, k)): v for k, v in self.visitas.items()}},
                     path)

    def firma(self):
        return f"{self.disc.firma()}|acciones{self.nA}"

    def load(self, path=None):
        datos = self._cargar(path)
        if not datos:
            return False
        guardada = datos.get("firma")
        if guardada and guardada != self.firma():
            print(f"  [{self.nombre} | zona {self.zona}] La tabla guardada se hizo con "
                  f"otra configuracion y sus celdas ya no significan lo mismo:")
            print(f"      guardada: {guardada}")
            print(f"      actual  : {self.firma()}")
            print(f"      -> se entrena de nuevo (el archivo se sobrescribira).")
            return False
        q = datos.get("q", {})
        self.q = {}
        for k, v in q.items():
            partes = tuple(int(x) for x in k.split(","))
            if len(partes) != 4:
                print(f"  [{self.nombre}] Formato de clave no reconocido: se entrena "
                      f"de nuevo.")
                return False
            self.q[partes] = list(v)
        self.visitas = {tuple(int(x) for x in k.split(",")): v
                        for k, v in (datos.get("visitas") or {}).items()}
        self.epsilon = max(self.epsilon_online, float(datos.get("epsilon", self.epsilon)))
        print(f"  [{self.nombre} | zona {self.zona}] Tabla cargada de {path or self.path} "
              f"({len(self.q)} celdas).")
        return True

    # ---- instantanea para el monitor ----------------------------------------
    def snapshot(self):
        return {
            "tipo": "tabla", "algoritmo": self.nombre, "escala": "valor Q",
            "acciones": self.acciones, "etiquetas": self.etiquetas,
            "bins": self.disc.info(), "epsilon": self.epsilon,
            "n_celdas": len(self.q), "n_updates": self.n_updates,
            "cobertura": len(self.visitas) / max(1, self.disc.n_celdas),
            "respaldos": self.respaldos, "sin_experiencia": self.sin_experiencia,
            "celda_actual": list(self.ultima_celda) if self.ultima_celda else None,
            "ultimo_update": self.ultimo_update,
            "q": {",".join(map(str, k)): [round(x, 4) for x in v]
                  for k, v in self.q.items()},
            "visitas": {",".join(map(str, k)): v for k, v in self.visitas.items()},
        }


# =============================================================================
# 1) Q-LEARNING
# =============================================================================

class AgenteQLearning(AgenteTabular):
    """TD(0) off-policy: el objetivo usa max_a' Q(s',a')."""
    nombre = "Q-learning"

    def observe(self, o, a, r, o2, a2, done):
        k, k2 = self.disc.key(o), self.disc.key(o2)
        fila, fila2 = self._fila(k), self._fila(k2)
        objetivo = r + (0.0 if done else self.gamma * max(fila2))
        antes = fila[a]
        delta = objetivo - antes
        fila[a] = antes + self.alpha * delta
        self._anotar(k, a, antes, fila[a], delta)
        self._decaer_epsilon()


# =============================================================================
# 2) SARSA(lambda)
# =============================================================================

class AgenteSarsaLambda(AgenteTabular):
    """TD(lambda) on-policy con trazas de elegibilidad."""
    nombre = "SARSA(lambda)"

    def __init__(self, *a, lambda_=0.85, traza_acumulativa=True, traza_min=1e-3, **kw):
        kw.setdefault("alpha", 0.15)
        super().__init__(*a, **kw)
        self.lambda_ = lambda_
        self.traza_acumulativa = traza_acumulativa
        self.traza_min = traza_min
        self.e = {}

    def observe(self, o, a, r, o2, a2, done):
        k, k2 = self.disc.key(o), self.disc.key(o2)
        fila, fila2 = self._fila(k), self._fila(k2)
        q_sa = fila[a]
        q_s2a2 = 0.0 if done else fila2[a2]
        delta = r + self.gamma * q_s2a2 - q_sa

        traza = self.e.setdefault(k, [0.0] * self.nA)
        if self.traza_acumulativa:
            traza[a] += 1.0
        else:
            traza[a] = 1.0

        gl = self.gamma * self.lambda_
        muertas = []
        for celda, ev in self.e.items():
            fq = self._fila(celda)
            for j in range(self.nA):
                if ev[j] != 0.0:
                    fq[j] += self.alpha * delta * ev[j]
                    ev[j] *= gl
            if max(ev) < self.traza_min:
                muertas.append(celda)
        for celda in muertas:
            del self.e[celda]

        self._anotar(k, a, q_sa, self._fila(k)[a], delta)
        self._decaer_epsilon()

    def on_reset(self):
        self.e = {}   # las trazas no cruzan de un episodio al siguiente

    def snapshot(self):
        s = super().snapshot()
        s["escala"] = "valor Q"
        s["trazas"] = {",".join(map(str, k)): [round(x, 4) for x in v]
                       for k, v in self.e.items()}
        s["lambda"] = self.lambda_
        return s


# =============================================================================
# 3) REINFORCE (policy gradient)
# =============================================================================

class AgenteReinforce(AgenteBase):
    """
    Politica softmax lineal sobre las caracteristicas continuas. Aprende al
    cerrar cada episodio con el retorno de Monte Carlo, con linea base movil y
    bonus de entropia.
    """
    nombre = "REINFORCE"
    tipo = "politica"

    def __init__(self, zona=0, acciones=None, alpha=0.02, gamma=0.95,
                 entropy_beta=0.01, baseline_lr=0.05, path=None,
                 disc: DiscretizadorZona = None):
        super().__init__(zona, acciones, path)
        self.alpha, self.gamma = alpha, gamma
        self.entropy_beta, self.baseline_lr = entropy_beta, baseline_lr
        self.theta = [[0.0] * NF for _ in range(len(self.acciones))]
        self.baseline = 0.0
        self._traj = []
        self.disc = disc or DiscretizadorZona()   # solo para publicar la malla
        self._pi_actual = [1.0 / len(self.acciones)] * len(self.acciones)
        self._ultimo_phi = [0.0] * NF
        self.celda_actual = None

    @property
    def nA(self):
        return len(self.acciones)

    # ---- politica ------------------------------------------------------------
    def _politica(self, phi):
        z = [sum(w * x for w, x in zip(self.theta[a], phi)) for a in range(self.nA)]
        mz = max(z)
        ex = [math.exp(v - mz) for v in z]
        s = sum(ex) or 1.0
        return [e / s for e in ex]

    def act(self, o, explore=False):
        phi = features(o)
        p = self._politica(phi)
        self._pi_actual, self._ultimo_phi = p, phi
        # Aunque REINFORCE no use tabla, se anota en que celda de la rejilla
        # esta el proceso para que el monitor pueda resaltarla igual.
        self.celda_actual = list(self.disc.key(o))
        if explore:
            u, acum = random.random(), 0.0
            for i, pi in enumerate(p):
                acum += pi
                if u <= acum:
                    return i
            return self.nA - 1
        return max(range(self.nA), key=lambda i: p[i])

    def observe(self, o, a, r, o2, a2, done):
        self._traj.append((features(o), a, r))

    def end_episode(self):
        if not self._traj:
            return
        G = 0.0
        retornos = [0.0] * len(self._traj)
        for t in range(len(self._traj) - 1, -1, -1):
            G = self._traj[t][2] + self.gamma * G
            retornos[t] = G
        # VENTAJA NORMALIZADA. Con la recompensa de este proceso los retornos
        # valen desde +1 hasta varios miles en negativo; usar (G - b) en crudo
        # hace que theta explote (es lo que le pasaba a la politica guardada,
        # con pesos del orden de 1e4). Tipificar por episodio deja la ventaja
        # en un rango acotado y estabiliza el gradiente sin cambiar su signo.
        media = sum(retornos) / len(retornos)
        var = sum((g - media) ** 2 for g in retornos) / len(retornos)
        desv = math.sqrt(var) + 1e-6
        self.baseline += self.baseline_lr * (media - self.baseline)
        norm = 0.0
        for (phi, a, _), Gt in zip(self._traj, retornos):
            ventaja = max(-5.0, min(5.0, (Gt - media) / desv))
            p = self._politica(phi)
            H = -sum(pi * math.log(pi + 1e-12) for pi in p)
            for c in range(self.nA):
                indic = 1.0 if a == c else 0.0
                coef = self.alpha * ventaja * (indic - p[c])
                coef += -self.alpha * self.entropy_beta * p[c] * (math.log(p[c] + 1e-12) + H)
                if coef != 0.0:
                    th = self.theta[c]
                    for i in range(NF):
                        th[i] += coef * phi[i]
                    norm += abs(coef)
        self._acotar_theta()
        self.n_updates += 1
        self.ultimo_update = {"episodio_pasos": len(self._traj),
                              "retorno_medio": media, "baseline": self.baseline,
                              "magnitud_gradiente": norm}
        self._traj = []

    def _acotar_theta(self, tope=50.0):
        """Proyeccion sobre una bola: impide que la politica se sature (una
        politica con pesos enormes deja de explorar y no puede corregirse)."""
        n = math.sqrt(sum(w * w for fila in self.theta for w in fila))
        if n > tope:
            k = tope / n
            self.theta = [[w * k for w in fila] for fila in self.theta]

    def on_reset(self):
        self._traj = []

    def reiniciar_exploracion(self, valor=0.35):
        """REINFORCE no tiene epsilon: su exploracion depende de lo plana que
        sea la politica. Encogiendo theta hacia cero se recupera entropia (la
        politica vuelve a dudar) sin perder del todo la forma aprendida."""
        k = max(0.0, min(1.0, 1.0 - valor))
        self.theta = [[w * k for w in fila] for fila in self.theta]

    # ---- persistencia --------------------------------------------------------
    def save(self, path=None):
        self._volcar({"tipo": "politica", "algoritmo": self.nombre, "zona": self.zona,
                      "acciones": self.acciones, "theta": self.theta,
                      "baseline": self.baseline, "nf": NF,
                      "caracteristicas": CARACTERISTICAS}, path)

    def load(self, path=None):
        datos = self._cargar(path)
        if not datos:
            return False
        if datos.get("nf") != NF or len(datos.get("theta", [])) != self.nA:
            print(f"  [{self.nombre}] La politica guardada no coincide en dimensiones: "
                  f"se entrena de nuevo.")
            return False
        self.theta = [list(v) for v in datos["theta"]]
        self.baseline = float(datos.get("baseline", 0.0))
        print(f"  [{self.nombre} | zona {self.zona}] Politica cargada de "
              f"{path or self.path}.")
        return True

    # ---- instantanea para el monitor ----------------------------------------
    def _malla(self, tr_bin, abajo_bin, low=0.60, high=0.70):
        """Evalua pi(a|s) sobre la rejilla (humedad propia x humedad de arriba)
        para poder dibujar la politica con el MISMO mapa que las tablas."""
        d = self.disc
        abajo = [0.45, 0.65, 0.85]
        tendencias = [-3.0e-5, 0.0, 3.0e-5]
        malla = {}
        for mb in range(d.moist_bins):
            m = d.centro_humedad(mb)
            for ub in range(d.up_bins):
                o = {"moisture": m, "moisture_up": d.centro_arriba(ub),
                     "moisture_down": abajo[abajo_bin],
                     "band_low": low, "band_high": high,
                     "trend": tendencias[tr_bin], "carga_arriba": 0.0,
                     "carga_abajo": 0.0, "nivel_previo": 0.0,
                     "pos_norm": 0.5, "moisture_grad": 0.0}
                p = self._politica(features(o))
                malla[f"{mb},{ub},{tr_bin},{abajo_bin}"] = [round(x, 4) for x in p]
        return malla

    def snapshot(self, low=0.60, high=0.70):
        q = {}
        for tr in range(3):
            for ab in range(3):
                q.update(self._malla(tr, ab, low, high))
        return {
            "tipo": "politica", "algoritmo": self.nombre, "escala": "probabilidad",
            "acciones": self.acciones, "etiquetas": self.etiquetas,
            "bins": self.disc.info(), "n_celdas": len(q), "n_updates": self.n_updates,
            "celda_actual": self.celda_actual,
            "theta": [[round(x, 5) for x in fila] for fila in self.theta],
            "caracteristicas": CARACTERISTICAS,
            "baseline": self.baseline, "pi_actual": self._pi_actual,
            "phi_actual": [round(x, 4) for x in self._ultimo_phi],
            "ultimo_update": self.ultimo_update,
            "q": q,
        }


# =============================================================================
# FABRICA
# =============================================================================

ALGORITMOS = {
    "qlearning": AgenteQLearning,
    "sarsa": AgenteSarsaLambda,
    "reinforce": AgenteReinforce,
}


def crear_agente(algoritmo, zona, disc=None, **kw):
    """Instancia el agente pedido con la ruta de guardado propia de la zona."""
    algoritmo = algoritmo.lower()
    if algoritmo not in ALGORITMOS:
        raise ValueError(f"Algoritmo desconocido: {algoritmo}. "
                         f"Opciones: {', '.join(ALGORITMOS)}")
    cls = ALGORITMOS[algoritmo]
    path = ruta_tabla(zona, cls.etiqueta())
    if cls is AgenteReinforce:
        return cls(zona=zona, acciones=list(ACCIONES), path=path, disc=disc, **kw)
    return cls(zona=zona, disc=disc, acciones=list(ACCIONES), path=path, **kw)
