#!/usr/bin/env python3
"""
Cliente WebSocket para Sistema de Monitoreo H2
Conecta con ESP32 configurado como Access Point
"""

import asyncio
import websockets
import json
import datetime
from collections import deque
import statistics
import sys
from typing import Dict, List, Optional

class H2MonitorClient:
    def __init__(self, uri: str):
        self.uri = uri
        self.data_buffer = deque(maxlen=100)  # Últimas 100 lecturas
        self.connection_active = False
        self.packets_received = 0
        self.last_heartbeat = None
        self.alarm_states = {0: "OFF", 1: "BAJO", 2: "MEDIO", 3: "ALTO"}
        
    def print_header(self):
        """Imprime encabezado del monitor"""
        print("\n" + "="*80)
        print("SISTEMA DE MONITOREO H2 - CLIENTE PYTHON")
        print("="*80)
        print(f"Conectando a: {self.uri}")
        print("="*80 + "\n")
        
    def format_sensor_data(self, data: Dict) -> str:
        """Formatea los datos del sensor para visualización"""
        output = []
        timestamp = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        
        # Encabezado con timestamp y packet ID
        output.append(f"\n[{timestamp}] Packet #{data.get('packet_id', 'N/A')}")
        output.append("-" * 60)
        
        # Datos MQ-8
        if 'mq8_1' in data:
            mq1 = data['mq8_1']
            output.append(f"MQ8-1: RAW={mq1['raw']:4d} | Voltaje={mq1['voltage']:.3f}V")
            
        if 'mq8_2' in data:
            mq2 = data['mq8_2']
            output.append(f"MQ8-2: RAW={mq2['raw']:4d} | Voltaje={mq2['voltage']:.3f}V")
            
        # Datos BME280
        if 'bme280' in data:
            bme = data['bme280']
            output.append(f"\nBME280:")
            output.append(f"  Temperatura: {bme['temperature']:.2f}°C")
            output.append(f"  Presión: {bme['pressure']:.2f} hPa")
            output.append(f"  Humedad: {bme['humidity']:.2f}%")
            
        # Estado de alarma
        alarm_level = data.get('alarm_level', 0)
        alarm_text = self.alarm_states.get(alarm_level, "DESCONOCIDO")
        alarm_indicator = "*" if alarm_level == 0 else "**" if alarm_level == 1 else "***" if alarm_level == 2 else "*****"
        output.append(f"\nNivel de Alarma: {alarm_indicator} {alarm_text}")
        
        return "\n".join(output)
    
    def calculate_statistics(self) -> Optional[Dict]:
        """Calcula estadísticas de los datos almacenados"""
        if len(self.data_buffer) < 10:
            return None
            
        stats = {
            'mq8_1_raw': [],
            'mq8_2_raw': [],
            'temp': [],
            'pres': [],
            'hum': []
        }
        
        for data in self.data_buffer:
            if 'mq8_1' in data:
                stats['mq8_1_raw'].append(data['mq8_1']['raw'])
            if 'mq8_2' in data:
                stats['mq8_2_raw'].append(data['mq8_2']['raw'])
            if 'bme280' in data:
                stats['temp'].append(data['bme280']['temperature'])
                stats['pres'].append(data['bme280']['pressure'])
                stats['hum'].append(data['bme280']['humidity'])
                
        result = {}
        for key, values in stats.items():
            if values:
                result[key] = {
                    'mean': statistics.mean(values),
                    'stdev': statistics.stdev(values) if len(values) > 1 else 0,
                    'min': min(values),
                    'max': max(values)
                }
                
        return result
    
    def print_statistics(self):
        """Imprime estadísticas de los datos recolectados"""
        stats = self.calculate_statistics()
        if not stats:
            return
            
        print("\n" + "="*60)
        print("ESTADÍSTICAS (últimas 100 lecturas)")
        print("="*60)
        
        if 'mq8_1_raw' in stats:
            s = stats['mq8_1_raw']
            print(f"MQ8-1: μ={s['mean']:.1f} σ={s['stdev']:.1f} [{s['min']}-{s['max']}]")
            
        if 'mq8_2_raw' in stats:
            s = stats['mq8_2_raw']
            print(f"MQ8-2: μ={s['mean']:.1f} σ={s['stdev']:.1f} [{s['min']}-{s['max']}]")
            
        if 'temp' in stats:
            s = stats['temp']
            print(f"\nTemperatura: μ={s['mean']:.2f}°C σ={s['stdev']:.2f}")
            
        if 'pres' in stats:
            s = stats['pres']
            print(f"Presión: μ={s['mean']:.1f}hPa σ={s['stdev']:.1f}")
            
        if 'hum' in stats:
            s = stats['hum']
            print(f"Humedad: μ={s['mean']:.1f}% σ={s['stdev']:.1f}")
            
        print("="*60)
    
    async def send_command(self, websocket, command: str, params: Dict = None):
        """Envía comando al ESP32"""
        cmd = {"command": command}
        if params:
            cmd.update(params)
            
        await websocket.send(json.dumps(cmd))
        print(f"\n>> Comando enviado: {command}")
        
    async def handle_message(self, message: str):
        """Procesa mensajes recibidos del ESP32"""
        try:
            data = json.loads(message)
            msg_type = data.get('type', 'unknown')
            
            if msg_type == 'sensor_data':
                self.packets_received += 1
                self.data_buffer.append(data)
                
                # Imprimir datos formateados
                print(self.format_sensor_data(data))
                
                # Cada 50 paquetes, mostrar estadísticas
                if self.packets_received % 50 == 0:
                    self.print_statistics()
                    
            elif msg_type == 'heartbeat':
                self.last_heartbeat = datetime.datetime.now()
                print(f"\n Heartbeat recibido - Paquetes enviados: {data.get('packets_sent', 'N/A')}")
                
            elif msg_type == 'system_info':
                print("\n INFORMACIÓN DEL SISTEMA:")
                print(f"  Versión: {data.get('version', 'N/A')}")
                print(f"  Sensores: {data.get('sensors', 'N/A')}")
                print(f"  ID: {data.get('id', 'N/A')}")
                
            elif msg_type == 'error':
                print(f"\n ERROR: {data.get('message', 'Error desconocido')}")
                
            else:
                # Respuesta a comando
                if data.get('status') == 'ACK':
                    print(f" Comando confirmado: {data.get('command', 'N/A')}")
                else:
                    print(f"\n Respuesta: {json.dumps(data, indent=2)}")
                    
        except json.JSONDecodeError as e:
            print(f"\nError decodificando JSON: {e}")
            print(f"  Mensaje raw: {message}")
    
    async def interactive_console(self, websocket):
        """Consola interactiva para enviar comandos"""
        print("\n" + "="*60)
        print("CONSOLA INTERACTIVA - Comandos disponibles:")
        print("  1 - Activar buzzer")
        print("  2 - Desactivar buzzer")
        print("  3 - Modo automático buzzer")
        print("  4 - Estado del sistema")
        print("  5 - Estadísticas locales")
        print("  q - Salir")
        print("="*60)
        
        while True:
            try:
                # Usar asyncio para entrada no bloqueante
                user_input = await asyncio.get_event_loop().run_in_executor(
                    None, input, "\nComando (1-5, q): "
                )
                
                if user_input.lower() == 'q':
                    print("\nCerrando conexión...")
                    break
                elif user_input == '1':
                    await self.send_command(websocket, "BUZZER_ON")
                elif user_input == '2':
                    await self.send_command(websocket, "BUZZER_OFF")
                elif user_input == '3':
                    await self.send_command(websocket, "BUZZER_AUTO")
                elif user_input == '4':
                    await self.send_command(websocket, "STATUS")
                elif user_input == '5':
                    self.print_statistics()
                else:
                    print("Comando no válido")
                    
            except Exception as e:
                print(f"Error en consola: {e}")
                break
    
    async def connect_and_monitor(self):
        """Conexión principal y bucle de monitoreo"""
        self.print_header()
        
        try:
            async with websockets.connect(self.uri) as websocket:
                self.connection_active = True
                print("Conectado al ESP32!")
                
                # Crear tareas concurrentes
                receive_task = asyncio.create_task(self.receive_messages(websocket))
                console_task = asyncio.create_task(self.interactive_console(websocket))
                
                # Esperar a que alguna tarea termine
                done, pending = await asyncio.wait(
                    [receive_task, console_task],
                    return_when=asyncio.FIRST_COMPLETED
                )
                
                # Cancelar tareas pendientes
                for task in pending:
                    task.cancel()
                    
        except websockets.exceptions.ConnectionRefused:
            print(" No se pudo conectar al ESP32. Verifica:")
            print(" 1. Que estés conectado a la red WiFi del ESP32")
            print(" 2. Que la IP y puerto sean correctos")
        except Exception as e:
            print(f" Error de conexión: {e}")
        finally:
            self.connection_active = False
            print("\n Desconectado")
    
    async def receive_messages(self, websocket):
        """Bucle de recepción de mensajes"""
        try:
            async for message in websocket:
                await self.handle_message(message)
        except websockets.exceptions.ConnectionClosed:
            print("\n Conexión cerrada por el servidor")
        except Exception as e:
            print(f"\n Error recibiendo mensajes: {e}")

async def main():
    # Configuración de conexión
    ESP32_IP = "192.168.4.1"  # IP del ESP32 como AP
    ESP32_PORT = 81           # Puerto WebSocket
    URI = f"ws://{ESP32_IP}:{ESP32_PORT}"
    
    # Crear y ejecutar cliente
    client = H2MonitorClient(URI)
    await client.connect_and_monitor()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n Programa interrumpido por el usuario")
    except Exception as e:
        print(f"\n Error fatal: {e}")
        sys.exit(1)