// Arduino simulado para compilar actuador_valvulas.ino en el PC (pruebas de
// contrato contra el puente real). Solo lo minimo que usa el firmware.
#pragma once
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cstdarg>
#include <string>
#define HIGH 1
#define LOW 0
#define OUTPUT 1
uint32_t millis();
void digitalWrite(int pin, int v);
int digitalRead(int pin);
void pinMode(int pin, int m);
struct SerialSim {
  void begin(uint32_t) {}
  int available();
  int read();
  void println(const char *s);
  int printf(const char *fmt, ...);
};
extern SerialSim Serial;
