/*
 * actuador_valvulas.ino - ESP32 DE LAS ELECTROVALVULAS (cama de vermicompost)
 * ===========================================================================
 *
 * Firmware compatible con servicios/actuadores/puente_actuadores.py. El ESP32
 * va conectado por USB a la Raspberry Pi 5; el puente es el UNICO programa que
 * abre ese puerto. Nadie mas (controladores, agente, Node-RED) le habla al
 * ESP32 directamente.
 *
 * PROTOCOLO (texto, 115200 8N1, una orden por linea)
 * --------------------------------------------------
 *   Peticion:  #<id> <ORDEN> [argumentos]\n      (sin "#id" tambien vale, para
 *   Respuesta: #<id> OK ... | #<id> ERR ...\n     probar desde el Monitor Serie)
 *
 *   PING            -> OK PING
 *   CONFIG          -> OK CONFIG N=3 MAX_PULSO=900 MAX_CONTINUO=1800 ...
 *   ABRIR v s       -> OK ABRIR v aplicado [RECORTADO]
 *                      abre la valvula v (1..N) durante s segundos. Si ya
 *                      estaba abierta, el pulso se REINICIA con la nueva
 *                      duracion (el puente lo registra como "REEMPLAZADO").
 *                      s se recorta a MAX_PULSO_S.
 *   CERRAR v        -> OK CERRAR v
 *   CERRAR_TODO     -> OK CERRAR_TODO
 *   ESTADO          -> OK r1 r2 ... rN     segundos restantes de cada valvula
 *                      (0 = cerrada). OJO: sin la palabra ESTADO, el puente
 *                      convierte a entero todo lo que va despues de "OK".
 *   Errores         -> ERR VALVULA | ERR SEGUNDOS | ERR BLOQUEADA v resto |
 *                      ERR ORDEN | ERR LINEA_LARGA
 *
 *   Eventos (sin #id, los emite el ESP32 por su cuenta):
 *     EVT INICIO                       arranque: todo cerrado
 *     EVT CERRADA v MOTIVO seg         MOTIVO = TIEMPO | ORDEN | WATCHDOG | MAXIMO
 *                                      seg = segundos que duro ESTE pulso
 *     EVT WATCHDOG                     se cerro todo por falta de ordenes
 *
 * SEGURIDAD (no depende de la Pi, de Docker ni de la red)
 * ------------------------------------------------------
 *   1. Arranque seguro: los reles quedan en reposo ANTES de configurar los
 *      pines, asi un reinicio nunca da un pulso de riego. Usar electrovalvulas
 *      normalmente cerradas.
 *   2. Tope por pulso (MAX_PULSO_S): ninguna orden abre mas de eso.
 *   3. Tope continuo (MAX_CONTINUO_S): si una valvula lleva abierta ese tiempo
 *      sin interrupcion (aunque le sigan llegando ABRIR), se cierra con motivo
 *      MAXIMO y queda BLOQUEADA durante DESCANSO_S. Cubre un controlador con un
 *      fallo que repita ABRIR para siempre.
 *   4. Latido (WATCHDOG_S): el puente envia PING cada 10 s. Si pasan WATCHDOG_S
 *      sin ninguna orden valida, se cierra todo. Cubre la Pi apagada, el
 *      contenedor caido o el USB desconectado.
 *   5. Watchdog de hardware (esp_task_wdt): si el lazo se cuelga, el ESP32 se
 *      reinicia y vuelve al punto 1.
 *   El bucle no bloquea nunca (sin delay, sin esperas de conexion): los puntos
 *   2-4 se evaluan siempre, pase lo que pase con el puerto serie.
 *
 * CABLEADO
 * --------
 *   GPIO25, GPIO26, GPIO27 -> modulo de reles (zona 1, 2, 3).
 *   Las electrovalvulas llevan su propia fuente; nunca desde el ESP32.
 *   Si la placa de reles es activa a nivel BAJO: RELE_ACTIVO_ALTO = false.
 *
 * COMPILAR: Arduino IDE, placa "ESP32 Dev Module". Sin librerias externas.
 */

#include <Arduino.h>
#include <esp_task_wdt.h>

// ===================== CONFIGURACION =====================
static const uint8_t  N_VALVULAS = 3;
static const uint8_t  PIN_VALVULA[N_VALVULAS] = {25, 26, 27};
static const bool     RELE_ACTIVO_ALTO = true;
static const uint32_t BAUDIOS        = 115200;
static const uint32_t MAX_PULSO_S    = 900;    // 15 min por orden
static const uint32_t MAX_CONTINUO_S = 1800;   // 30 min seguidos como maximo
static const uint32_t DESCANSO_S     = 300;    // bloqueo tras el tope continuo
static const uint32_t WATCHDOG_S     = 60;     // sin ordenes -> cerrar todo
static const uint32_t HW_WDT_S       = 8;      // watchdog de hardware
static const uint8_t  VERSION_FW     = 1;
static const int      PIN_LED        = 2;

// ===================== ESTADO =====================
struct Valvula {
  bool     abierta;
  uint32_t inicio_pulso_ms;     // inicio del pulso actual
  uint32_t duracion_ms;         // duracion del pulso actual
  uint32_t inicio_continuo_ms;  // desde cuando esta abierta sin interrupcion
  uint32_t bloqueada_hasta_ms;  // 0 = no bloqueada
};
static Valvula val[N_VALVULAS];
static uint32_t ultima_orden_ms = 0;
static bool     watchdog_disparado = false;
static char     linea[96];
static size_t   n_linea = 0;
static bool     linea_desbordada = false;

// ===================== SALIDAS =====================
static void escribir_rele(uint8_t i, bool abierto) {
  bool nivel = RELE_ACTIVO_ALTO ? abierto : !abierto;
  digitalWrite(PIN_VALVULA[i], nivel ? HIGH : LOW);
}

static void cerrar(uint8_t i, const char *motivo) {
  if (!val[i].abierta) return;
  uint32_t seg = (millis() - val[i].inicio_pulso_ms) / 1000UL;
  val[i].abierta = false;
  escribir_rele(i, false);
  Serial.printf("EVT CERRADA %u %s %lu\n", i + 1, motivo, (unsigned long)seg);
}

static void cerrar_todas(const char *motivo) {
  for (uint8_t i = 0; i < N_VALVULAS; i++) cerrar(i, motivo);
}

static uint32_t restante_s(uint8_t i) {
  if (!val[i].abierta) return 0;
  uint32_t pasado = millis() - val[i].inicio_pulso_ms;
  if (pasado >= val[i].duracion_ms) return 0;
  return (val[i].duracion_ms - pasado + 999UL) / 1000UL;
}

// ===================== RESPUESTAS =====================
static void responder(const char *id, const char *texto) {
  if (id[0]) Serial.printf("#%s %s\n", id, texto);
  else       Serial.printf("%s\n", texto);
}

static int valvula_arg(const char *s) {          // "1".."N" -> 0..N-1, -1 si no
  char *fin;
  long v = strtol(s, &fin, 10);
  if (s[0] == '\0' || *fin != '\0' || v < 1 || v > N_VALVULAS) return -1;
  return (int)(v - 1);
}

// ===================== ORDENES =====================
static void procesar(char *txt) {
  char id[8] = "";
  char *p = txt;
  while (*p == ' ') p++;
  if (*p == '#') {                                 // "#123 ORDEN ..."
    p++;
    size_t k = 0;
    while (*p >= '0' && *p <= '9' && k < sizeof(id) - 1) id[k++] = *p++;
    id[k] = '\0';
    while (*p == ' ') p++;
  }
  char *orden = strtok(p, " ");
  char *a1 = strtok(NULL, " ");
  char *a2 = strtok(NULL, " ");
  if (!orden) return;

  // Cualquier orden reconocible renueva el latido.
  ultima_orden_ms = millis();
  watchdog_disparado = false;
  digitalWrite(PIN_LED, !digitalRead(PIN_LED));
  char r[96];

  if (!strcmp(orden, "PING")) {
    responder(id, "OK PING");
  } else if (!strcmp(orden, "CONFIG")) {
    snprintf(r, sizeof r, "OK CONFIG N=%u MAX_PULSO=%lu MAX_CONTINUO=%lu DESCANSO=%lu WATCHDOG=%lu FW=%u",
             N_VALVULAS, (unsigned long)MAX_PULSO_S, (unsigned long)MAX_CONTINUO_S,
             (unsigned long)DESCANSO_S, (unsigned long)WATCHDOG_S, VERSION_FW);
    responder(id, r);
  } else if (!strcmp(orden, "ABRIR")) {
    int i = a1 ? valvula_arg(a1) : -1;
    if (i < 0) { responder(id, "ERR VALVULA"); return; }
    char *fin;
    long s = a2 ? strtol(a2, &fin, 10) : 0;
    if (!a2 || *fin != '\0' || s < 1) { responder(id, "ERR SEGUNDOS"); return; }
    uint32_t ahora = millis();
    if (val[i].bloqueada_hasta_ms && (int32_t)(val[i].bloqueada_hasta_ms - ahora) > 0) {
      snprintf(r, sizeof r, "ERR BLOQUEADA %d %lu", i + 1,
               (unsigned long)((val[i].bloqueada_hasta_ms - ahora) / 1000UL + 1));
      responder(id, r);
      return;
    }
    val[i].bloqueada_hasta_ms = 0;
    bool recortado = (uint32_t)s > MAX_PULSO_S;
    uint32_t aplicado = recortado ? MAX_PULSO_S : (uint32_t)s;
    if (!val[i].abierta) val[i].inicio_continuo_ms = ahora;   // un re-pulso no lo reinicia
    val[i].abierta = true;
    val[i].inicio_pulso_ms = ahora;
    val[i].duracion_ms = aplicado * 1000UL;
    escribir_rele(i, true);
    snprintf(r, sizeof r, "OK ABRIR %d %lu%s", i + 1, (unsigned long)aplicado,
             recortado ? " RECORTADO" : "");
    responder(id, r);
  } else if (!strcmp(orden, "CERRAR")) {
    int i = a1 ? valvula_arg(a1) : -1;
    if (i < 0) { responder(id, "ERR VALVULA"); return; }
    cerrar(i, "ORDEN");
    snprintf(r, sizeof r, "OK CERRAR %d", i + 1);
    responder(id, r);
  } else if (!strcmp(orden, "CERRAR_TODO")) {
    cerrar_todas("ORDEN");
    responder(id, "OK CERRAR_TODO");
  } else if (!strcmp(orden, "ESTADO")) {
    int n = snprintf(r, sizeof r, "OK");
    for (uint8_t i = 0; i < N_VALVULAS && n < (int)sizeof r - 8; i++)
      n += snprintf(r + n, sizeof r - n, " %lu", (unsigned long)restante_s(i));
    responder(id, r);
  } else {
    responder(id, "ERR ORDEN");
  }
}

// ===================== ARRANQUE Y BUCLE =====================
void setup() {
  // 1) Reles en reposo ANTES de configurar los pines como salida.
  for (uint8_t i = 0; i < N_VALVULAS; i++) {
    escribir_rele(i, false);
    pinMode(PIN_VALVULA[i], OUTPUT);
    escribir_rele(i, false);
    val[i] = {false, 0, 0, 0, 0};
  }
  pinMode(PIN_LED, OUTPUT);

  Serial.begin(BAUDIOS);
  ultima_orden_ms = millis();

  // 5) Watchdog de hardware. La API cambio en el core 3.x de Arduino-ESP32.
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
  esp_task_wdt_config_t cfg = {HW_WDT_S * 1000, 0, true};
  esp_task_wdt_reconfigure(&cfg);
#else
  esp_task_wdt_init(HW_WDT_S, true);
#endif
  esp_task_wdt_add(NULL);

  Serial.println("EVT INICIO");
}

void loop() {
  esp_task_wdt_reset();

  // --- lectura de lineas sin bloquear ---
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      if (linea_desbordada) {
        Serial.println("ERR LINEA_LARGA");
      } else if (n_linea) {
        linea[n_linea] = '\0';
        procesar(linea);
      }
      n_linea = 0;
      linea_desbordada = false;
    } else if (n_linea < sizeof(linea) - 1) {
      linea[n_linea++] = c;
    } else {
      linea_desbordada = true;
    }
  }

  uint32_t ahora = millis();

  // --- 2) y 3) fin de pulso y tope continuo ---
  for (uint8_t i = 0; i < N_VALVULAS; i++) {
    if (!val[i].abierta) continue;
    if (ahora - val[i].inicio_continuo_ms >= MAX_CONTINUO_S * 1000UL) {
      cerrar(i, "MAXIMO");
      val[i].bloqueada_hasta_ms = (ahora + DESCANSO_S * 1000UL) | 1UL;  // nunca 0
    } else if (ahora - val[i].inicio_pulso_ms >= val[i].duracion_ms) {
      cerrar(i, "TIEMPO");
    }
  }

  // --- 4) latido ---
  if (!watchdog_disparado && ahora - ultima_orden_ms > WATCHDOG_S * 1000UL) {
    bool alguna = false;
    for (uint8_t i = 0; i < N_VALVULAS; i++) alguna |= val[i].abierta;
    cerrar_todas("WATCHDOG");
    watchdog_disparado = true;
    if (alguna) Serial.println("EVT WATCHDOG");
  }
}
