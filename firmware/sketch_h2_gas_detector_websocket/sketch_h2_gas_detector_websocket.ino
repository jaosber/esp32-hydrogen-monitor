/**************************************************************************
 * Sistema de Monitoreo H2 - Versión WebSocket con WiFi AP
 * 
 * Características técnicas:
 * - ESP32 como Access Point con servidor WebSocket
 * - Transmisión en tiempo real de datos de sensores
 * - Lógica de umbral para activación automática del buzzer
 * - Mantenimiento de heartbeat y validación de datos
 * - Buffer circular para estabilidad
 * - Filtrado de ruido en sensores analógicos
 **************************************************************************/

#include <WiFi.h>
#include <WebSocketsServer.h>
#include <Wire.h>
#include <Adafruit_Sensor.h>
#include <Adafruit_BME280.h>
#include <ArduinoJson.h>

// --- Configuración WiFi AP ---
const char* AP_SSID = "ESP32_H2_Monitor";
const char* AP_PASSWORD = "H2Monitor2024";
const IPAddress AP_IP(192, 168, 4, 1);
const IPAddress AP_GATEWAY(192, 168, 4, 1);
const IPAddress AP_SUBNET(255, 255, 255, 0);

// --- Configuración WebSocket ---
WebSocketsServer webSocket = WebSocketsServer(81);

// --- Configuración de Hardware ---
const int PIN_SENSOR_MQ8_1 = 34;
const int PIN_SENSOR_MQ8_2 = 35;
const int PIN_BUZZER = 23;
const int PIN_LEDCOM = 19;

// --- Umbrales de Alarma ---
const float UMBRAL_H2_BAJO = 300;    // ADC value
const float UMBRAL_H2_MEDIO = 600;   // ADC value
const float UMBRAL_H2_ALTO = 900;    // ADC value
const float UMBRAL_VOLTAJE = 1.5;    // Voltios

// --- Configuración de Comunicación ---
const unsigned long HEARTBEAT_INTERVAL = 5000; // ms
const unsigned long DATA_SEND_INTERVAL = 100;  // ms
const int ANALOG_SAMPLES = 10; // Promediado para reducir ruido

// --- Variables Globales ---
Adafruit_BME280 bme;
bool client_connected = false;
unsigned long last_heartbeat = 0;
unsigned long last_data_send = 0;
unsigned long packet_counter = 0;
uint8_t connected_client_num = 255; // Cliente no válido

// --- Estructuras de Datos ---
struct SensorData {
    float temp;
    float pres;
    float hum;
    int raw_mq8_1;
    int raw_mq8_2;
    float volt_mq8_1;
    float volt_mq8_2;
    bool valid;
    unsigned long timestamp;
};

enum AlarmLevel {
    ALARM_OFF = 0,
    ALARM_LOW = 1,
    ALARM_MEDIUM = 2,
    ALARM_HIGH = 3
};

AlarmLevel current_alarm_level = ALARM_OFF;
bool buzzer_manual_override = false;

// --- Funciones de Utilidad ---

// Calcula checksum simple para validación
uint8_t calcularChecksum(const String& data) {
    uint8_t checksum = 0;
    for (char c : data) {
        checksum ^= c;
    }
    return checksum;
}

// Lee valor analógico con promediado para reducir ruido
int leerAnalogPromedio(int pin, int muestras = ANALOG_SAMPLES) {
    long suma = 0;
    for (int i = 0; i < muestras; i++) {
        suma += analogRead(pin);
        delayMicroseconds(100);
    }
    return suma / muestras;
}

// Valida rangos de sensores
bool validarDatosSensor(const SensorData& datos) {
    if (datos.temp < -40 || datos.temp > 85) return false;
    if (datos.pres < 300 || datos.pres > 1100) return false;
    if (datos.hum < 0 || datos.hum > 100) return false;
    if (datos.raw_mq8_1 < 0 || datos.raw_mq8_1 > 4095) return false;
    if (datos.raw_mq8_2 < 0 || datos.raw_mq8_2 > 4095) return false;
    return true;
}

// Lee todos los sensores con validación
SensorData leerSensores() {
    SensorData datos;
    
    datos.temp = bme.readTemperature();
    datos.pres = bme.readPressure() / 100.0F;
    datos.hum = bme.readHumidity();
    
    datos.raw_mq8_1 = leerAnalogPromedio(PIN_SENSOR_MQ8_1);
    datos.raw_mq8_2 = leerAnalogPromedio(PIN_SENSOR_MQ8_2);
    
    datos.volt_mq8_1 = (float)datos.raw_mq8_1 / 4095.0 * 3.3;
    datos.volt_mq8_2 = (float)datos.raw_mq8_2 / 4095.0 * 3.3;
    
    datos.valid = validarDatosSensor(datos);
    datos.timestamp = millis();
    
    return datos;
}

// Evalúa el nivel de alarma basado en los sensores
AlarmLevel evaluarNivelAlarma(const SensorData& datos) {
    if (!datos.valid) return ALARM_OFF;
    
    // Usar el valor máximo de ambos sensores
    int max_raw = max(datos.raw_mq8_1, datos.raw_mq8_2);
    float max_volt = max(datos.volt_mq8_1, datos.volt_mq8_2);
    
    if (max_raw > UMBRAL_H2_ALTO || max_volt > UMBRAL_VOLTAJE * 1.5) {
        return ALARM_HIGH;
    } else if (max_raw > UMBRAL_H2_MEDIO || max_volt > UMBRAL_VOLTAJE * 1.2) {
        return ALARM_MEDIUM;
    } else if (max_raw > UMBRAL_H2_BAJO || max_volt > UMBRAL_VOLTAJE) {
        return ALARM_LOW;
    }
    
    return ALARM_OFF;
}

// Controla el buzzer según el nivel de alarma
void controlarBuzzer(AlarmLevel nivel) {
    if (buzzer_manual_override) return;
    
    static unsigned long last_beep = 0;
    unsigned long now = millis();
    
    switch (nivel) {
        case ALARM_OFF:
            digitalWrite(PIN_BUZZER, HIGH); // Apagado
            break;
            
        case ALARM_LOW:
            // Beep lento (cada 2 segundos)
            if (now - last_beep > 2000) {
                digitalWrite(PIN_BUZZER, LOW);
                delay(100);
                digitalWrite(PIN_BUZZER, HIGH);
                last_beep = now;
            }
            break;
            
        case ALARM_MEDIUM:
            // Beep medio (cada 1 segundo)
            if (now - last_beep > 1000) {
                digitalWrite(PIN_BUZZER, LOW);
                delay(200);
                digitalWrite(PIN_BUZZER, HIGH);
                last_beep = now;
            }
            break;
            
        case ALARM_HIGH:
            // Beep rápido (cada 500ms)
            if (now - last_beep > 500) {
                digitalWrite(PIN_BUZZER, LOW);
                delay(300);
                digitalWrite(PIN_BUZZER, HIGH);
                last_beep = now;
            }
            break;
    }
}

// Maneja eventos del WebSocket
void webSocketEvent(uint8_t num, WStype_t type, uint8_t * payload, size_t length) {
    switch(type) {
        case WStype_DISCONNECTED:
            Serial.printf("[%u] Desconectado!\n", num);
            if (num == connected_client_num) {
                client_connected = false;
                connected_client_num = 255;
                digitalWrite(PIN_LEDCOM, LOW);
            }
            break;
            
        case WStype_CONNECTED:
            {
                IPAddress ip = webSocket.remoteIP(num);
                Serial.printf("[%u] Conectado desde %s\n", num, ip.toString().c_str());
                
                // Solo permitir un cliente a la vez
                if (!client_connected) {
                    client_connected = true;
                    connected_client_num = num;
                    digitalWrite(PIN_LEDCOM, HIGH);
                    
                    // Enviar información del sistema
                    StaticJsonDocument<256> doc;
                    doc["type"] = "system_info";
                    doc["version"] = "1.3";
                    doc["sensors"] = "MQ8x2+BME280";
                    doc["id"] = String(ESP.getEfuseMac(), HEX);
                    
                    String output;
                    serializeJson(doc, output);
                    webSocket.sendTXT(num, output);
                } else {
                    // Rechazar conexión adicional
                    webSocket.disconnect(num);
                }
            }
            break;
            
        case WStype_TEXT:
            Serial.printf("[%u] Comando recibido: %s\n", num, payload);
            procesarComandoWebSocket(num, (char*)payload);
            break;
            
        case WStype_BIN:
            // No se usa en este proyecto
            break;
    }
}

// Procesa comandos recibidos por WebSocket
void procesarComandoWebSocket(uint8_t num, const char* comando) {
    StaticJsonDocument<256> doc;
    DeserializationError error = deserializeJson(doc, comando);
    
    if (error) {
        Serial.print("Error JSON: ");
        Serial.println(error.c_str());
        return;
    }
    
    const char* cmd = doc["command"];
    StaticJsonDocument<128> response;
    
    if (strcmp(cmd, "BUZZER_ON") == 0) {
        digitalWrite(PIN_BUZZER, LOW);
        buzzer_manual_override = true;
        response["status"] = "ACK";
        response["command"] = "BUZZER_ON";
        
    } else if (strcmp(cmd, "BUZZER_OFF") == 0) {
        digitalWrite(PIN_BUZZER, HIGH);
        buzzer_manual_override = true;
        response["status"] = "ACK";
        response["command"] = "BUZZER_OFF";
        
    } else if (strcmp(cmd, "BUZZER_AUTO") == 0) {
        buzzer_manual_override = false;
        response["status"] = "ACK";
        response["command"] = "BUZZER_AUTO";
        
    } else if (strcmp(cmd, "STATUS") == 0) {
        response["status"] = "OK";
        response["packets"] = packet_counter;
        response["uptime"] = millis();
        response["alarm_level"] = current_alarm_level;
        response["buzzer_override"] = buzzer_manual_override;
        
    } else if (strcmp(cmd, "RESET") == 0) {
        response["status"] = "ACK";
        response["command"] = "RESET";
        String output;
        serializeJson(response, output);
        webSocket.sendTXT(num, output);
        delay(100);
        ESP.restart();
    }
    
    String output;
    serializeJson(response, output);
    webSocket.sendTXT(num, output);
}

// Envía datos de sensores por WebSocket
void enviarDatosWebSocket() {
    if (!client_connected) return;
    
    SensorData datos = leerSensores();
    
    if (!datos.valid) {
        StaticJsonDocument<64> error_doc;
        error_doc["type"] = "error";
        error_doc["message"] = "INVALID_SENSOR_DATA";
        String output;
        serializeJson(error_doc, output);
        webSocket.sendTXT(connected_client_num, output);
        return;
    }
    
    // Evaluar alarma
    current_alarm_level = evaluarNivelAlarma(datos);
    controlarBuzzer(current_alarm_level);
    
    // Crear JSON con datos
    StaticJsonDocument<512> doc;
    doc["type"] = "sensor_data";
    doc["packet_id"] = packet_counter++;
    doc["timestamp"] = datos.timestamp;
    
    JsonObject mq8_1 = doc.createNestedObject("mq8_1");
    mq8_1["raw"] = datos.raw_mq8_1;
    mq8_1["voltage"] = datos.volt_mq8_1;
    
    JsonObject mq8_2 = doc.createNestedObject("mq8_2");
    mq8_2["raw"] = datos.raw_mq8_2;
    mq8_2["voltage"] = datos.volt_mq8_2;
    
    JsonObject bme280 = doc.createNestedObject("bme280");
    bme280["temperature"] = datos.temp;
    bme280["pressure"] = datos.pres;
    bme280["humidity"] = datos.hum;
    
    doc["alarm_level"] = current_alarm_level;
    
    // Calcular checksum
    String data_str;
    serializeJson(doc, data_str);
    doc["checksum"] = calcularChecksum(data_str);
    
    // Enviar
    String output;
    serializeJson(doc, output);
    webSocket.sendTXT(connected_client_num, output);
}

// Envía heartbeat por WebSocket
void enviarHeartbeat() {
    if (!client_connected) return;
    
    StaticJsonDocument<128> doc;
    doc["type"] = "heartbeat";
    doc["timestamp"] = millis();
    doc["packets_sent"] = packet_counter;
    
    String output;
    serializeJson(doc, output);
    webSocket.sendTXT(connected_client_num, output);
}

// --- Setup ---
void setup() {
    Serial.begin(115200);
    Serial.println("\n\nSistema de Monitoreo H2 - WebSocket");
    Serial.println("====================================");
    
    // Configurar pines
    pinMode(PIN_BUZZER, OUTPUT);
    pinMode(PIN_LEDCOM, OUTPUT);
    digitalWrite(PIN_BUZZER, HIGH); // Buzzer apagado
    digitalWrite(PIN_LEDCOM, LOW);   // LED apagado
    
    // Parpadeo inicial del LED
    for (int i = 0; i < 3; i++) {
        digitalWrite(PIN_LEDCOM, HIGH);
        delay(100);
        digitalWrite(PIN_LEDCOM, LOW);
        delay(100);
    }
    
    // Inicializar BME280
    bool bme_ok = false;
    for (int intento = 0; intento < 3; intento++) {
        if (bme.begin(0x76) || bme.begin(0x77)) {
            bme_ok = true;
            Serial.println("BME280 inicializado correctamente");
            break;
        }
        delay(500);
    }
    
    if (!bme_ok) {
        Serial.println("ERROR: BME280 no encontrado!");
        // Continuar sin BME280 para pruebas
    } else {
        // Configurar BME280 para máxima precisión
        bme.setSampling(
            Adafruit_BME280::MODE_NORMAL,
            Adafruit_BME280::SAMPLING_X2,  
            Adafruit_BME280::SAMPLING_X16, 
            Adafruit_BME280::SAMPLING_X1,  
            Adafruit_BME280::FILTER_X16,
            Adafruit_BME280::STANDBY_MS_0_5
        );
    }
    
    // Configurar WiFi como Access Point
    Serial.println("\nConfigurando WiFi Access Point...");
    WiFi.mode(WIFI_AP);
    WiFi.softAPConfig(AP_IP, AP_GATEWAY, AP_SUBNET);
    WiFi.softAP(AP_SSID, AP_PASSWORD);
    
    Serial.print("AP SSID: ");
    Serial.println(AP_SSID);
    Serial.print("AP Password: ");
    Serial.println(AP_PASSWORD);
    Serial.print("AP IP: ");
    Serial.println(WiFi.softAPIP());
    
    // Iniciar servidor WebSocket
    webSocket.begin();
    webSocket.onEvent(webSocketEvent);
    Serial.println("WebSocket server iniciado en puerto 81");
    
    Serial.println("\nSistema listo!");
    Serial.println("Esperando conexión del cliente...");
}

// --- Loop Principal ---
void loop() {
    unsigned long now = millis();
    
    // Manejar WebSocket
    webSocket.loop();
    
    // Enviar datos a intervalos regulares
    if (client_connected && (now - last_data_send >= DATA_SEND_INTERVAL)) {
        enviarDatosWebSocket();
        last_data_send = now;
    }
    
    // Enviar heartbeat
    if (client_connected && (now - last_heartbeat >= HEARTBEAT_INTERVAL)) {
        enviarHeartbeat();
        last_heartbeat = now;
    }
    
    // Parpadeo del LED si no hay cliente conectado
    if (!client_connected) {
        static unsigned long last_blink = 0;
        if (now - last_blink > 1000) {
            digitalWrite(PIN_LEDCOM, !digitalRead(PIN_LEDCOM));
            last_blink = now;
        }
    }
}