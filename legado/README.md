# Legado
Arquitectura descartada el 27 oct 2026: planta.py era maestro Modbus de sensores y de un ESP32 esclavo Modbus,
en conflicto con el stack (dueño único del RS-485 y puente HTTP). NO EJECUTAR junto al stack. Ver main.
