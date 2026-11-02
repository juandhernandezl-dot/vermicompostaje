#include <WiFi.h>
#include <PubSubClient.h>


const char* WIFI_SSID     = "karoll's A16";
const char* WIFI_PASSWORD = "KHS031003";

const char* MQTT_BROKER_IP   = "10.157.249.102";
const int   MQTT_BROKER_PORT = 1883;

const char* TOPIC_HUMEDAD = "Humedad/Cama1";
const char* TOPIC_TIEMPO  = "Tiempo/Transcurrido"; 

const int PIN_DAC = 25; 

const char* TOPIC_COMANDO_MANUAL = "control/Valvula"; 
const long PERIODO_PUBLICACION_MS = 2000; 


// ============================================================
// CONFIG DEL CONTROLADOR PI
// ============================================================

const bool MODO_AUTOMATICO_INICIAL = true;

float setpointHumedad = 50.0; 

const float KC    =  0.08;
const float TAU_I = 48.0;

const long PERIODO_PI_MS = 10000; 

const float ZONA_MUERTA_PORCENTAJE = 3.0;
const float FACTOR_DECAIMIENTO_INTEGRAL = 0.99;

const char* TOPIC_SETPOINT    = "control/Setpoint";
const char* TOPIC_MODO        = "control/Modo";
const char* TOPIC_PI_ERROR    = "PI/Error";
const char* TOPIC_PI_SALIDA   = "PI/Salida";   // u[k], 0.0 a 1.0
const char* TOPIC_PI_INTEGRAL = "PI/Integral";


// ============================================================
// Estado interno (no tocar)
// ============================================================
WiFiClient espClient;
PubSubClient mqttClient(espClient);

bool modoAutomatico = MODO_AUTOMATICO_INICIAL;

float humedadActual = 0.0;
bool  humedadValida = false;

float integralPI = 0.0;
unsigned long ultimoCalculoPIMs = 0;
float salidaPI = 0.0;       // u[k], 0.0 a 1.0, lo que de verdad sale al DAC
float errorPI = 0.0;

float salidaManualPorcentaje = 0.0; // 0-100, usado solo en modo MANUAL

unsigned long ultimoCambioModoMs = 0;
unsigned long ultimaPublicacionMs = 0;


void validarConfig() {
  bool faltaAlgo = false;

  if (strlen(WIFI_SSID) == 0)            { Serial.println("Falta definir WIFI_SSID"); faltaAlgo = true; }
  if (strlen(WIFI_PASSWORD) == 0)        { Serial.println("Falta definir WIFI_PASSWORD"); faltaAlgo = true; }
  if (strlen(MQTT_BROKER_IP) == 0)       { Serial.println("Falta definir MQTT_BROKER_IP"); faltaAlgo = true; }
  if (MQTT_BROKER_PORT < 0)              { Serial.println("Falta definir MQTT_BROKER_PORT"); faltaAlgo = true; }
  if (strlen(TOPIC_HUMEDAD) == 0)        { Serial.println("Falta definir TOPIC_HUMEDAD"); faltaAlgo = true; }
  if (strlen(TOPIC_TIEMPO) == 0)         { Serial.println("Falta definir TOPIC_TIEMPO"); faltaAlgo = true; }
  if (strlen(TOPIC_COMANDO_MANUAL) == 0) { Serial.println("Falta definir TOPIC_COMANDO_MANUAL"); faltaAlgo = true; }
  if (PERIODO_PUBLICACION_MS < 0)        { Serial.println("Falta definir PERIODO_PUBLICACION_MS"); faltaAlgo = true; }
  if (PERIODO_PI_MS < 0)                 { Serial.println("Falta definir PERIODO_PI_MS"); faltaAlgo = true; }

  if (faltaAlgo) {
    Serial.println("Deteniendo: completa la seccion CONFIG antes de subir este codigo.");
    while (true) { delay(1000); }
  }
}

void conectarWiFi() {
  Serial.print("Conectando a WiFi");
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println(" conectado.");
}

void alRecibirComando(char* topic, byte* payload, unsigned int length); // forward declaration

void conectarMQTT() {
  mqttClient.setServer(MQTT_BROKER_IP, MQTT_BROKER_PORT);
  mqttClient.setCallback(alRecibirComando);
  while (!mqttClient.connected()) {
    Serial.print("Conectando a MQTT...");
    if (mqttClient.connect("esp32_nodo_prueba1")) {
      Serial.println(" conectado.");
      mqttClient.subscribe(TOPIC_COMANDO_MANUAL);
      mqttClient.subscribe(TOPIC_SETPOINT);
      mqttClient.subscribe(TOPIC_MODO);
      mqttClient.subscribe(TOPIC_HUMEDAD);
    } else {
      Serial.print(" fallo, estado="); Serial.println(mqttClient.state());
      delay(2000);
    }
  }
}

void aplicarSalida(float u) {
  u = constrain(u, 0.0, 1.0);
  salidaPI = u;
  dacWrite(PIN_DAC, (int)(u * 255));
}

void alRecibirComando(char* topic, byte* payload, unsigned int length) {
  String mensaje;
  for (unsigned int i = 0; i < length; i++) mensaje += (char)payload[i];

  // --- Humedad, publicada por sensor_sn3002_mqtt.py ---
  if (strcmp(topic, TOPIC_HUMEDAD) == 0) {
    float valor = mensaje.toFloat();
    if (valor >= 0 && valor <= 100) {
      humedadActual = valor;
      humedadValida = true;
    }
    return;
  }

  // --- Salida manual (numero 0-100), solo aplica en modo MANUAL ---
  if (strcmp(topic, TOPIC_COMANDO_MANUAL) == 0) {
    if (!modoAutomatico) {
      float valor = mensaje.toFloat();
      if (valor >= 0 && valor <= 100) {
        salidaManualPorcentaje = valor;
        aplicarSalida(salidaManualPorcentaje / 100.0);
        Serial.print("Salida manual: "); Serial.print(salidaManualPorcentaje); Serial.println("%");
      }
    } else {
      Serial.println("Comando manual ignorado: el sistema esta en modo AUTOMATICO");
    }
    return;
  }

  // --- Cambiar setpoint ---
  if (strcmp(topic, TOPIC_SETPOINT) == 0) {
    float nuevoSetpoint = mensaje.toFloat();
    if (nuevoSetpoint > 0 && nuevoSetpoint <= 100) {
      setpointHumedad = nuevoSetpoint;
      Serial.print("Nuevo setpoint: "); Serial.println(setpointHumedad);
    }
    return;
  }

  // --- Cambiar modo auto/manual ---
  if (strcmp(topic, TOPIC_MODO) == 0) {
    mensaje.toLowerCase();
    if (mensaje == "auto") {
      modoAutomatico = true;
      integralPI = 0.0; // reset del integrador al entrar a automatico, evita salto brusco
      ultimoCambioModoMs = millis();
      Serial.println("Modo: AUTOMATICO (PI)");
    } else if (mensaje == "manual") {
      modoAutomatico = false;
      salidaManualPorcentaje = 0.0;
      aplicarSalida(0.0); // arranca en 0 por seguridad al entrar a manual
      ultimoCambioModoMs = millis();
      Serial.println("Modo: MANUAL (salida en 0%, espera un numero en control/Valvula)");
    }
    return;
  }
}

// ============================================================
// CONTROLADOR PI 
// ============================================================
void calcularPI() {
  float Ts = PERIODO_PI_MS / 1000.0;

  errorPI = setpointHumedad - humedadActual;

  if (fabs(errorPI) < ZONA_MUERTA_PORCENTAJE) {
    integralPI *= FACTOR_DECAIMIENTO_INTEGRAL;
    aplicarSalida(0.0);
    Serial.print("[PI] humedad="); Serial.print(humedadActual);
    Serial.print(" error="); Serial.print(errorPI);
    Serial.println(" -> dentro de zona muerta, bomba apagada");
    return;
  }

  float integralPropuesto = integralPI + errorPI * Ts;
  float uPropuesto = KC * errorPI + (KC / TAU_I) * integralPropuesto;

  float u = constrain(uPropuesto, 0.0, 1.0);

  bool saturadoAlto = (uPropuesto > 1.0) && (errorPI > 0);
  bool saturadoBajo = (uPropuesto < 0.0) && (errorPI < 0);
  if (!saturadoAlto && !saturadoBajo) {
    integralPI = integralPropuesto;
  }

  aplicarSalida(u);

  Serial.print("[PI] humedad="); Serial.print(humedadActual);
  Serial.print(" setpoint="); Serial.print(setpointHumedad);
  Serial.print(" error="); Serial.print(errorPI);
  Serial.print(" u="); Serial.println(u, 3);
}

void publicarDatos() {
  unsigned long tiempoTranscurridoS = (millis() - ultimoCambioModoMs) / 1000;

  char bufferTiempo[10];
  ltoa(tiempoTranscurridoS, bufferTiempo, 10);
  mqttClient.publish(TOPIC_TIEMPO, bufferTiempo);

  if (modoAutomatico) {
    char bufferError[10];    dtostrf(errorPI, 4, 2, bufferError);
    char bufferSalida[10];   dtostrf(salidaPI, 4, 3, bufferSalida);
    char bufferIntegral[10]; dtostrf(integralPI, 6, 2, bufferIntegral);

    mqttClient.publish(TOPIC_PI_ERROR, bufferError);
    mqttClient.publish(TOPIC_PI_SALIDA, bufferSalida);
    mqttClient.publish(TOPIC_PI_INTEGRAL, bufferIntegral);
  }

  Serial.print("Humedad (ultima recibida): "); Serial.print(humedadActual);
  Serial.print("% | Salida: "); Serial.print(salidaPI * 100, 1); Serial.print("%");
  Serial.print(" | Modo: "); Serial.print(modoAutomatico ? "AUTO" : "MANUAL");
  Serial.print(" | t="); Serial.print(bufferTiempo); Serial.println("s");
}

void setup() {
  Serial.begin(115200);
  validarConfig();

  aplicarSalida(0.0); // arranca con la bomba en 0

  conectarWiFi();
  conectarMQTT();

  ultimoCalculoPIMs = millis();
  ultimoCambioModoMs = millis();
}

void loop() {
  if (!mqttClient.connected()) {
    conectarMQTT();
  }
  mqttClient.loop();

  unsigned long ahora = millis();

  if (modoAutomatico) {
    if (!humedadValida) {
      static unsigned long ultimoAvisoMs = 0;
      if (ahora - ultimoAvisoMs > 5000) {
        Serial.println("Esperando primer dato de humedad por MQTT antes de activar el PI...");
        ultimoAvisoMs = ahora;
      }
    } else if (ahora - ultimoCalculoPIMs >= (unsigned long)PERIODO_PI_MS) {
      calcularPI();
      ultimoCalculoPIMs = ahora;
    }
  }

  if (ahora - ultimaPublicacionMs >= (unsigned long)PERIODO_PUBLICACION_MS) {
    publicarDatos();
    ultimaPublicacionMs = ahora;
  }
}