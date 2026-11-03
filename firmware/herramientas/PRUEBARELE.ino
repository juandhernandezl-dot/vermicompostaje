// Prueba: ¿el módulo de relés se activa con LOW o con HIGH?
// Proyecto Minciencias 113982 - UAO
// Mira los LEDs del módulo (o escucha el "clic") y anota en qué estado se encienden.

const int PINES_RELE[6] = {13, 27, 26, 25, 33, 32};   // IN1 ... IN6

void todosSueltos() {
  // Pines en alta impedancia: con módulos de lógica LOW esto los deja apagados
  for (int i = 0; i < 6; i++) pinMode(PINES_RELE[i], INPUT);
}

void setup() {
  todosSueltos();                      // nada se activa al arrancar
  Serial.begin(115200);
  delay(1500);
  Serial.println("\n=== Prueba de nivel de activacion de los reles ===");
  Serial.println("Observa el LED de cada rele y escucha el clic.");
}

void loop() {
  for (int i = 0; i < 6; i++) {
    int pin = PINES_RELE[i];
    pinMode(pin, OUTPUT);

    digitalWrite(pin, LOW);
    Serial.printf("IN%d (GPIO%d) en LOW  -> ¿se encendio?\n", i + 1, pin);
    delay(3000);

    digitalWrite(pin, HIGH);
    Serial.printf("IN%d (GPIO%d) en HIGH -> ¿se encendio?\n", i + 1, pin);
    delay(3000);

    pinMode(pin, INPUT);               // lo suelta antes de pasar al siguiente
    Serial.println("----");
    delay(1000);
  }
  Serial.println("=== Ciclo completo. Se repite en 5 s ===\n");
  delay(5000);
}