// Ejecuta el firmware en el PC: lee la linea serie de stdin (o de un pty) y
// escribe en stdout. ESCALA_TIEMPO>1 acelera millis() para pruebas rapidas.
// Los pines de los reles se imprimen en stderr al cambiar (para verificar
// que un reinicio nunca deja un rele activo).
#include "Arduino.h"
#include <chrono>
#include <unistd.h>
#include <fcntl.h>
#include <poll.h>
SerialSim Serial;
static double escala = 1.0;
static auto t0 = std::chrono::steady_clock::now();
static int pines[64];
uint32_t millis() {
  auto d = std::chrono::steady_clock::now() - t0;
  return (uint32_t)(std::chrono::duration<double, std::milli>(d).count() * escala);
}
void digitalWrite(int p, int v) {
  if (pines[p] != v) fprintf(stderr, "PIN %d=%d t=%u\n", p, v, millis());
  pines[p] = v;
}
int digitalRead(int p) { return pines[p]; }
void pinMode(int, int) {}
static int fd_in = 0;
int SerialSim::available() {
  struct pollfd f = {fd_in, POLLIN, 0};
  return poll(&f, 1, 0) > 0 && (f.revents & (POLLIN | POLLHUP));
}
int SerialSim::read() {
  char c;
  if (::read(fd_in, &c, 1) == 1) return (unsigned char)c;
  exit(0);                                   // fin de la entrada
}
void SerialSim::println(const char *s) { fputs(s, stdout); fputc('\n', stdout); fflush(stdout); }
int SerialSim::printf(const char *fmt, ...) {
  va_list a; va_start(a, fmt); int n = vfprintf(stdout, fmt, a); va_end(a); fflush(stdout); return n;
}
#include "../actuador_valvulas/actuador_valvulas.ino"
int main() {
  // -1 = nunca escrito: asi la PRIMERA escritura de cada pin (la de reposo en
  // setup) siempre se reporta y la prueba puede verificar el arranque seguro.
  for (int &p : pines) p = -1;
  const char *e = getenv("ESCALA_TIEMPO");
  if (e) escala = atof(e);
  setup();
  for (;;) { loop(); usleep(2000); }
}
