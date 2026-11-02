#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rl_zona.py  -  UN CONTROLADOR RL INDEPENDIENTE POR ZONA
=======================================================

Cada zona de la cama se controla con su PROPIO agente, su PROPIA tabla y su
PROPIO proceso. Se pueden mezclar algoritmos: por ejemplo SARSA(lambda) en la
cabecera (mas suave, es la que inunda a las demas) y Q-learning en el pie.

USO
---
    python3 gui_mz.py                              # 1) interfaz + servicio
    python3 rl_zona.py --zona 0 --algoritmo sarsa  # 2) controlador de la zona 0
    python3 rl_zona.py --zona 1 --algoritmo qlearning
    python3 rl_zona.py --zona 2 --algoritmo reinforce

    python3 rl_zona.py --todas --algoritmo qlearning     # una por zona, en paralelo
    python3 rl_zona.py --todas --algoritmos sarsa,qlearning,reinforce

    python3 rl_monitor.py                          # 3) ver las tablas en vivo

MODOS
-----
    El actuador es de todo o nada: las acciones son cerrar (0) y abrir (1).

    --modo entrenar     continua sobre lo guardado (por defecto si existe)
    --modo reentrenar   empieza de cero
    --modo controlar    solo aplica lo aprendido, sin explorar ni aprender
    --sin-offline       salta el pre-entrenamiento acelerado y va a tiempo real
    --solo-offline      solo pre-entrena (no necesita la interfaz abierta)

El pre-entrenamiento acelerado se hace sobre una COPIA LOCAL del modelo (con
los mismos parametros que la simulacion en marcha), asi que varias zonas pueden
entrenar a la vez sin pelearse por el reloj de la simulacion.
"""

import argparse
import os
import subprocess
import sys

from rl_common_mz import (ConfigZona, PesosRecompensa, PublicadorRL, RunnerZona,
                          DiscretizadorZona, ClienteMZ, conectar_o_avisar,
                          HOST, PORT, DIR_ESTADO)
from rl_agentes import ALGORITMOS, crear_agente


def construir_parser():
    ap = argparse.ArgumentParser(
        description="Controlador RL de una zona de la cama de vermicompostaje.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--zona", type=int, default=0, help="Indice de zona (0 = la mas alta).")
    ap.add_argument("--todas", action="store_true",
                    help="Lanza un proceso por zona (usa --algoritmo o --algoritmos).")
    ap.add_argument("--algoritmo", default="qlearning", choices=sorted(ALGORITMOS),
                    help="Algoritmo del agente.")
    ap.add_argument("--algoritmos", default=None,
                    help="Con --todas: lista separada por comas, uno por zona.")
    ap.add_argument("--modo", default="auto",
                    choices=["auto", "entrenar", "reentrenar", "controlar"])
    ap.add_argument("--sin-offline", dest="offline", action="store_false",
                    help="Sin pre-entrenamiento acelerado.")
    ap.add_argument("--solo-offline", action="store_true",
                    help="Solo pre-entrena (no requiere la interfaz abierta).")
    ap.add_argument("--episodios", type=int, default=None,
                    help="Tope de episodios del pre-entrenamiento.")
    ap.add_argument("--banda", nargs=2, type=float, metavar=("LOW", "HIGH"),
                    default=None, help="Banda objetivo de humedad de esta zona.")
    ap.add_argument("--bins", nargs=2, type=int, metavar=("HUM", "ARRIBA"),
                    default=(20, 6),
                    help="Resolucion de la tabla: bins de humedad propia y de la "
                         "humedad de la zona de arriba.")
    ap.add_argument("--alpha", type=float, default=None, help="Tasa de aprendizaje.")
    ap.add_argument("--gamma", type=float, default=None, help="Factor de descuento.")
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=PORT)
    return ap


def lanzar_todas(args):
    """Arranca un proceso por zona. Cada uno es totalmente independiente."""
    cli = conectar_o_avisar(args.host, args.port)
    if cli is None:
        return 1
    with cli:
        n = int(cli.zonas().get("n_zones", 1))
    if args.algoritmos:
        algos = [a.strip() for a in args.algoritmos.split(",") if a.strip()]
        algos = (algos * n)[:n]
    else:
        algos = [args.algoritmo] * n
    print(f"Lanzando {n} controladores independientes: "
          + ", ".join(f"Z{i}={a}" for i, a in enumerate(algos)))
    procesos = []
    for i, algo in enumerate(algos):
        cmd = [sys.executable, os.path.abspath(__file__), "--zona", str(i),
               "--algoritmo", algo, "--modo", args.modo, "--host", args.host, "--port", str(args.port),
               "--bins", str(args.bins[0]), str(args.bins[1])]
        if not args.offline:
            cmd.append("--sin-offline")
        if args.episodios:
            cmd += ["--episodios", str(args.episodios)]
        if args.banda:
            cmd += ["--banda", str(args.banda[0]), str(args.banda[1])]
        procesos.append(subprocess.Popen(cmd))
    print("Ctrl+C detiene todos.")
    try:
        for p in procesos:
            p.wait()
    except KeyboardInterrupt:
        for p in procesos:
            p.terminate()
    return 0


def main(argv=None):
    args = construir_parser().parse_args(argv)
    if args.todas:
        return lanzar_todas(args)

    cfg = ConfigZona(zona=args.zona, host=args.host, port=args.port,
                     pesos=PesosRecompensa())
    if args.banda:
        cfg.target_low, cfg.target_high = args.banda
    if args.episodios:
        cfg.episodios_max = args.episodios

    # Los cortes del eje de humedad se alinean con la banda objetivo, asi que
    # el discretizador tiene que conocerla.
    disc = DiscretizadorZona(low=cfg.target_low, high=cfg.target_high,
                             moist_bins=args.bins[0], up_bins=args.bins[1])
    extra = {}
    if args.alpha is not None:
        extra["alpha"] = args.alpha
    if args.gamma is not None:
        extra["gamma"] = args.gamma
    agente = crear_agente(args.algoritmo, args.zona, disc, **extra)

    cli = None
    if not args.solo_offline:
        cli = conectar_o_avisar(args.host, args.port)
        if cli is None:
            print("Sugerencia: con --solo-offline puedes pre-entrenar sin la interfaz.")
            return 1
        # Sondeo real del servicio: si no contesta, no tiene sentido entrenar
        # para morir despues al pasar a tiempo real.
        try:
            zs = cli.zonas()
            n = int(zs.get("n_zones", 1))
            if not (0 <= args.zona < n):
                print(f"La cama tiene {n} zonas (0..{n-1}); pediste la {args.zona}.")
                cli.close()
                return 1
            propio = sum(zs["spray_matrix"][c][args.zona]
                         for c in range(len(zs["spray_matrix"]))
                         if c // max(1, len(zs["spray_matrix"]) // n) == args.zona)
            print(f"Cama de {zs.get('bed_length')} m, {n} zonas de "
                  f"{zs.get('zone_length'):.2f} m, pendiente {zs.get('bed_slope')}%. "
                  f"Aspersor {args.zona}: {propio*100:.0f}% del caudal cae en su "
                  f"propia zona.")
            try:
                d = cli.cadencia()
                print(f"Cadencia del simulador: dt={d['dt']:g} s, Ts efectivo="
                      f"{d['sample_time_eff']:g} s -> {d['pasos_por_muestra']} paso(s) "
                      f"por decision.")
                for aviso in d.get("avisos", []):
                    print(f"  AVISO: {aviso}")
            except Exception:
                pass
        except Exception as e:
            print(f"El simulador acepto la conexion pero no responde: {e}")
            print("Cierra y vuelve a abrir la ventana del simulador; si el problema "
                  "sigue, comprueba con:  python3 vermicompost_mz.py --test")
            cli.close()
            return 1

    # REINFORCE publica una malla completa: se refresca algo mas despacio.
    cada = 1.0 if args.algoritmo == "reinforce" else cfg.publicar_cada_s
    pub = PublicadorRL(args.zona, agente, DIR_ESTADO, cada)
    runner = RunnerZona(agente, cfg, cli, pub)
    try:
        if args.solo_offline:
            runner.entrenar_offline()
        else:
            runner.run(modo=args.modo, offline=args.offline)
    finally:
        if cli is not None:
            try:
                cli.liberar(args.zona, "manual")
            except Exception:
                pass
            cli.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
