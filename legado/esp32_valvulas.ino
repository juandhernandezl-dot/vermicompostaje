/*
 * esp32_valvulas.ino  -  ESP32 COMO ESCLAVO MODBUS RTU PARA ELECTROVALVULAS
 * ==========================================================================
 *
 * El PC (maestro) manda las electrovalvulas escribiendo BOBINAS Modbus. El
 * ESP32 vive en el MISMO bus RS-485 que los sensores de suelo, con su propia
 * direccion, asi que todo el sistema usa un unico par trenzado y un unico
 * adaptador USB-RS485.
 *
 *   PC ── USB/RS485 ──┬── sensor zona 0   (dir. 1)
 *                     ├── sensor zona 1   (dir. 2)
 *                     ├── sensor zona 2   (dir. 3)
 *                     └── ESP32 valvulas  (dir. 10)   <-- este programa
 *
 * MAPA MODBUS QUE EXPONE
 * ----------------------
 *   Bobinas (0x01 leer, 0x05 forzar una, 0x0F forzar varias)
 *     0 .. N_VALVULAS-1   electrovalvula de cada zona (0 = zona 0)
 *
 *     OJO: la numeracion empieza en 0. En la interfaz del PC, "Bobina de la
 *     zona 0" debe valer 0. Si se pone 1, el PC pedira las bobinas 1..N y la
 *     ultima no existe: el ESP32 rechaza toda la trama (excepcion 2) y NINGUNA
 *     valvula se mueve.
 *
 *   Registros de retencion (0x03 leer, 0x06 escribir)
 *     0   estado de las valvulas como mapa de bits (solo lectura)
 *     1   segundos desde el arranque
 *     2   tramas validas recibidas
 *     3   tramas descartadas (CRC o direccion ajena)
 *     4   segundos desde la ultima orden valida del maestro
 *     5   tiempo de guarda en segundos (escribible, 0 = desactivado)
 *     6   version del firmware
 *     7   numero de valvulas que expone (el PC lo usa para avisar si le pides
 *         una bobina que no existe)
 *
 * SEGURIDAD (lo importante)
 * -------------------------
 *   TIEMPO DE GUARDA: si pasan mas de GUARDA_S segundos sin una orden valida
 *   del maestro, el ESP32 CIERRA TODAS LAS VALVULAS por su cuenta. Sin esto,
 *   si el PC se cuelga o se desenchufa el USB con una valvula abierta, el lecho
 *   se inunda hasta que alguien lo vea. Es la unica proteccion que no depende
 *   del ordenador, asi que no la quites.
 *
 *   Al arrancar, todas las valvulas quedan CERRADAS antes de configurar los
 *   pines, para que un reset no provoque un pulso de riego.
 *
 * CABLEADO
 * --------
 *   Transceptor MAX485 / MAX3485 (3,3 V para el ESP32; si es de 5 V usa
 *   MAX3485 o un adaptador de nivel):
 *     RO  -> GPIO16 (RX2)
 *     DI  -> GPIO17 (TX2)
 *     DE  -> GPIO4      (RE se une a DE)
 *     RE  -> GPIO4
 *     A, B -> par trenzado del bus (A con A, B con B; NO invertir)
 *     120 Ω entre A y B solo en los DOS extremos del bus
 *     GND del transceptor con el GND del bus (referencia comun)
 *
 *   Reles / MOSFET de las electrovalvulas: GPIO25, 26, 27, 14.
 *   Las electrovalvulas van con su propia fuente (normalmente 12 o 24 V) y
 *   NUNCA alimentadas desde el ESP32. Diodo volante en cada bobina si el rele
 *   no lo lleva. Si tu placa de reles es activa a nivel BAJO, pon
 *   RELE_ACTIVO_ALTO en false (o marca "rele invertido" en la interfaz del PC).
 *
 * COMPILAR
 * --------
 *   Arduino IDE con soporte ESP32. Placa: "ESP32 Dev Module". Sin librerias
 *   externas: el Modbus RTU esta implementado aqui para que se vea exactamente
 *   lo que va por el cable.
 */

#include <Arduino.h>

// ===================== CONFIGURACION =====================
static const uint8_t  DIRECCION_MODBUS = 10;     // 1..254, unica en el bus
static const uint32_t BAUDIOS          = 4800;   // la MISMA que los sensores
static const uint8_t  N_VALVULAS       = 3;      // una por zona
static const uint8_t  PIN_VALVULA[N_VALVULAS] = {25, 26, 27};
static const bool     RELE_ACTIVO_ALTO = true;
static const uint16_t GUARDA_S_INICIAL = 120;    // cierre por falta de ordenes
static const uint16_t VERSION_FW       = 3;

static const int PIN_RX  = 16;
static const int PIN_TX  = 17;
static const int PIN_DE  = 4;     // DE y RE unidos
static const int PIN_LED = 2;     // LED de la placa: parpadea con cada trama

#define BUS Serial2

// ===================== ESTADO =====================
static bool     valvula[N_VALVULAS];
static uint32_t tramas_ok = 0, tramas_malas = 0;
static uint32_t ultima_orden_ms = 0;
static uint16_t guarda_s = GUARDA_S_INICIAL;
static bool     cerrado_por_guarda = false;

// Tiempos de Modbus RTU: una trama termina tras 3,5 tiempos de caracter de
// silencio. Un caracter son 11 bits (arranque + 8 datos + parada).
static uint32_t T35_us;
static uint8_t  buf[256];
static size_t   n_buf = 0;
static uint32_t ultimo_byte_us = 0;

// ===================== UTILIDADES =====================
static uint16_t crc16(const uint8_t *d, size_t n) {
  uint16_t crc = 0xFFFF;
  for (size_t i = 0; i < n; i++) {
    crc ^= d[i];
    for (uint8_t j = 0; j < 8; j++)
      crc = (crc & 1) ? (crc >> 1) ^ 0xA001 : crc >> 1;
  }
  return crc;
}

static void aplicar_salidas() {
  for (uint8_t i = 0; i < N_VALVULAS; i++) {
    bool nivel = RELE_ACTIVO_ALTO ? valvula[i] : !valvula[i];
    digitalWrite(PIN_VALVULA[i], nivel ? HIGH : LOW);
  }
}

static void cerrar_todas() {
  for (uint8_t i = 0; i < N_VALVULAS; i++) valvula[i] = false;
  aplicar_salidas();
}

static void enviar(const uint8_t *cuerpo, size_t n) {
  uint8_t t[256];
  memcpy(t, cuerpo, n);
  uint16_t c = crc16(cuerpo, n);
  t[n] = c & 0xFF;          // CRC byte bajo primero
  t[n + 1] = c >> 8;
  // El bus es half duplex: hay que tomar la linea, esperar el silencio
  // reglamentario, transmitir, esperar a que salga el ultimo bit y soltarla.
  // Si se suelta antes de tiempo se corta el final de la trama.
  delayMicroseconds(T35_us);
  digitalWrite(PIN_DE, HIGH);
  delayMicroseconds(50);
  BUS.write(t, n + 2);
  BUS.flush();
  delayMicroseconds(100);
  digitalWrite(PIN_DE, LOW);
}

static void excepcion(uint8_t funcion, uint8_t codigo) {
  uint8_t r[3] = {DIRECCION_MODBUS, (uint8_t)(funcion | 0x80), codigo};
  enviar(r, 3);
}

static uint16_t registro(uint16_t d) {
  switch (d) {
    case 0: {
      uint16_t m = 0;
      for (uint8_t i = 0; i < N_VALVULAS; i++) if (valvula[i]) m |= (1 << i);
      return m;
    }
    case 1: return (uint16_t)(millis() / 1000UL);
    case 2: return (uint16_t)tramas_ok;
    case 3: return (uint16_t)tramas_malas;
    case 4: return (uint16_t)((millis() - ultima_orden_ms) / 1000UL);
    case 5: return guarda_s;
    case 6: return VERSION_FW;
    case 7: return N_VALVULAS;
    default: return 0xFFFF;
  }
}

// ===================== PROCESAR UNA TRAMA =====================
static void procesar(const uint8_t *t, size_t n) {
  if (n < 4) { tramas_malas++; return; }
  uint16_t crc_rx = (uint16_t)t[n - 1] << 8 | t[n - 2];
  if (crc16(t, n - 2) != crc_rx) { tramas_malas++; return; }
  if (t[0] != DIRECCION_MODBUS) { tramas_malas++; return; }   // no es para mi

  tramas_ok++;
  ultima_orden_ms = millis();
  cerrado_por_guarda = false;
  digitalWrite(PIN_LED, !digitalRead(PIN_LED));

  const uint8_t f = t[1];
  const uint16_t dir = (uint16_t)t[2] << 8 | t[3];
  const uint16_t cnt = (uint16_t)t[4] << 8 | t[5];

  switch (f) {
    case 0x01: {                                  // leer bobinas
      if (dir + cnt > N_VALVULAS || cnt == 0) { excepcion(f, 2); return; }
      uint8_t nb = (cnt + 7) / 8;
      uint8_t r[8] = {DIRECCION_MODBUS, 0x01, nb, 0, 0, 0, 0, 0};
      for (uint16_t i = 0; i < cnt; i++)
        if (valvula[dir + i]) r[3 + i / 8] |= (1 << (i % 8));
      enviar(r, 3 + nb);
      return;
    }
    case 0x05: {                                  // forzar una bobina
      if (dir >= N_VALVULAS) { excepcion(f, 2); return; }
      if (cnt != 0xFF00 && cnt != 0x0000) { excepcion(f, 3); return; }
      valvula[dir] = (cnt == 0xFF00);
      aplicar_salidas();
      enviar(t, 6);                               // eco de la peticion
      return;
    }
    case 0x0F: {                                  // forzar varias bobinas
      if (dir + cnt > N_VALVULAS || cnt == 0 || n < 9) { excepcion(f, 2); return; }
      const uint8_t *datos = &t[7];
      for (uint16_t i = 0; i < cnt; i++)
        valvula[dir + i] = (datos[i / 8] >> (i % 8)) & 1;
      aplicar_salidas();
      uint8_t r[6] = {DIRECCION_MODBUS, 0x0F, t[2], t[3], t[4], t[5]};
      enviar(r, 6);
      return;
    }
    case 0x03: {                                  // leer registros de estado
      const uint16_t N_REGS = 8;
      if (cnt == 0 || cnt > N_REGS || dir + cnt > N_REGS) { excepcion(f, 2); return; }
      uint8_t r[3 + 2 * N_REGS] = {DIRECCION_MODBUS, 0x03, (uint8_t)(2 * cnt)};
      for (uint16_t i = 0; i < cnt; i++) {
        uint16_t v = registro(dir + i);
        r[3 + 2 * i] = v >> 8;                    // byte alto primero
        r[4 + 2 * i] = v & 0xFF;
      }
      enviar(r, 3 + 2 * cnt);
      return;
    }
    case 0x06: {                                  // escribir registro
      if (dir != 5) { excepcion(f, 2); return; }  // solo el tiempo de guarda
      guarda_s = cnt;
      enviar(t, 6);
      return;
    }
    default:
      excepcion(f, 1);
  }
}

// ===================== ARRANQUE Y BUCLE =====================
void setup() {
  // Primero las salidas en reposo: un reset no debe abrir ninguna valvula.
  for (uint8_t i = 0; i < N_VALVULAS; i++) {
    valvula[i] = false;
    pinMode(PIN_VALVULA[i], OUTPUT);
    digitalWrite(PIN_VALVULA[i], RELE_ACTIVO_ALTO ? LOW : HIGH);
  }
  pinMode(PIN_DE, OUTPUT);
  digitalWrite(PIN_DE, LOW);            // en recepcion
  pinMode(PIN_LED, OUTPUT);

  BUS.begin(BAUDIOS, SERIAL_8N1, PIN_RX, PIN_TX);
  T35_us = (uint32_t)(3.5f * 11.0f * 1000000.0f / (float)BAUDIOS);
  if (T35_us < 750) T35_us = 750;       // minimo recomendado por la norma
  ultima_orden_ms = millis();

  Serial.begin(115200);
  Serial.printf("\nESP32 valvulas v%u | direccion Modbus %u | %lu baudios 8N1 | "
                "%u valvulas | guarda %u s\n",
                VERSION_FW, DIRECCION_MODBUS, (unsigned long)BAUDIOS,
                N_VALVULAS, guarda_s);
}

void loop() {
  // --- recepcion delimitada por silencio (Modbus RTU) ---
  while (BUS.available()) {
    uint32_t ahora = micros();
    if (n_buf && (ahora - ultimo_byte_us) > T35_us) {
      procesar(buf, n_buf);             // hueco: la trama anterior termino
      n_buf = 0;
    }
    uint8_t b = BUS.read();
    if (n_buf < sizeof(buf)) buf[n_buf++] = b;
    ultimo_byte_us = micros();
  }
  if (n_buf && (micros() - ultimo_byte_us) > T35_us) {
    procesar(buf, n_buf);
    n_buf = 0;
  }

  // --- tiempo de guarda: sin ordenes, se cierra todo ---
  if (guarda_s > 0 && !cerrado_por_guarda) {
    if ((millis() - ultima_orden_ms) > (uint32_t)guarda_s * 1000UL) {
      bool alguna = false;
      for (uint8_t i = 0; i < N_VALVULAS; i++) alguna |= valvula[i];
      cerrar_todas();
      cerrado_por_guarda = true;
      if (alguna)
        Serial.printf("GUARDA: %u s sin ordenes del maestro -> valvulas "
                      "cerradas\n", guarda_s);
    }
  }
}
