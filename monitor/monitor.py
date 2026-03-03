import dearpygui.dearpygui as dpg
import time
import websocket
import threading
import queue
import json
from collections import deque
from math import log10, pow
from datetime import datetime
import rel  # Para websocket-client eventos

# Clase ScrollingBuffer mejorada
class ScrollingBuffer:
    def __init__(self, max_size=2000):
        self.max_size = max_size
        self.x_data = deque(maxlen=max_size)
        self.y_data = deque(maxlen=max_size)
        
    def add_point(self, x, y):
        self.x_data.append(x)
        self.y_data.append(y)
        
    def get_data(self):
        return list(self.x_data), list(self.y_data)
    
    def clear(self):
        self.x_data.clear()
        self.y_data.clear()
        
    def size(self):
        return len(self.x_data)
    
    def get_latest(self):
        if self.y_data:
            return self.y_data[-1]
        return 0

# Variables globales
buffers = {
    'temperatura': ScrollingBuffer(500),
    'humedad': ScrollingBuffer(500),
    'presion': ScrollingBuffer(500),
    'cpu': ScrollingBuffer(200),
    'memoria': ScrollingBuffer(200),
    # Buffers para sensores MQ-8
    'mq8_1_raw': ScrollingBuffer(500),
    'mq8_2_raw': ScrollingBuffer(500),
    'mq8_1_voltage': ScrollingBuffer(500),
    'mq8_2_voltage': ScrollingBuffer(500),
    'mq8_1_ppm': ScrollingBuffer(500),
    'mq8_2_ppm': ScrollingBuffer(500)
}

start_time = time.time()
history_seconds = 30.0
update_interval = 0.1
running = True
historical_data = []

# Variables de conexión WebSocket
ws_connection = None
ws_queue = queue.Queue()
connection_status = "Desconectado"
ESP32_IP = "192.168.4.1"
ESP32_PORT = 81
buzzer_mode = "AUTO"  # AUTO, ON, OFF
current_alarm_level = 0

# Parámetros de calibración MQ-8
MQ8_RL = 10.0  # Resistencia de carga en kOhms
MQ8_R0_1 = 60.21  # R0 para el sensor 1
MQ8_R0_2 = 53.46  # R0 para el sensor 2

# Coeficientes de la curva logarítmica del MQ-8 para H2
MQ8_CURVE_M = -1.8  # Pendiente de la curva
MQ8_CURVE_B = 0.76  # Intercepto

# Valores actuales de los sensores MQ-8
mq8_current_values = {
    'sensor1': {'raw': 0, 'voltage': 0, 'ppm': 0, 'ratio': 0},
    'sensor2': {'raw': 0, 'voltage': 0, 'ppm': 0, 'ratio': 0}
}

# Colores para las series
COLORS = {
    'temperatura': [255, 100, 100],  # Rojo
    'humedad': [100, 200, 255],      # Azul
    'presion': [100, 255, 100],      # Verde
    'cpu': [255, 200, 100],          # Naranja
    'memoria': [200, 100, 255],      # Púrpura
    'mq8_1': [255, 165, 0],          # Naranja
    'mq8_2': [0, 255, 255]           # Cyan
}

# Estados de alarma
ALARM_STATES = {
    0: ("OFF",   "OFF", [0, 255, 0]),
    1: ("BAJO",  "BAJO", [255, 255, 0]),
    2: ("MEDIO", "MEDIO", [255, 165, 0]),
    3: ("ALTO",  "ALTO", [255, 0, 0])
}

def export_data_to_json():
    """Guarda el contenido de 'historical_data' en un archivo JSON."""
    if not historical_data:
        print("No hay datos históricos para exportar.")
        return

    file_timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    filename = f"sensor_data_{file_timestamp}.json"

    try:
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(historical_data, f, ensure_ascii=False, indent=4)
        print(f"Datos exportados exitosamente a {filename}")
        
        # Mostrar notificación en GUI
        if dpg.does_item_exist("export_status"):
            dpg.set_value("export_status", f"Exportado: {filename}")
    except Exception as e:
        print(f"Error exportando datos: {e}")
        if dpg.does_item_exist("export_status"):
            dpg.set_value("export_status", f"Error: {str(e)}")

def calculate_ppm_from_voltage(voltage, sensor_num=1):
    """Calcula PPM de H2 basado en el voltaje del sensor MQ-8"""
    if voltage <= 0.1:  # Evitar división por cero
        return 0
    
    try:
        Vc = 3.3  # Voltaje de alimentación
        Rs = MQ8_RL * (Vc - voltage) / voltage
        
        # Calcular ratio Rs/R0
        ratio = Rs / MQ8_R0_1 if sensor_num == 1 else Rs / MQ8_R0_2
        
        if ratio <= 0:
            return 0
        
        # Aplicar la ecuación de la curva característica
        log_ppm = (log10(ratio) - MQ8_CURVE_B) / MQ8_CURVE_M
        ppm = pow(10, log_ppm)
        
        # Limitar a rango realista
        return max(0, min(10000, ppm))
        
    except Exception as e:
        print(f"Error calculando PPM: {e}")
        return 0

def calculate_ratio_from_voltage(voltage, sensor_num=1):
    """Calcula el ratio Rs/R0 del sensor"""
    if voltage <= 0.1:
        return 0
    
    try:
        Vc = 3.3
        Rs = MQ8_RL * (Vc - voltage) / voltage
        ratio = Rs / MQ8_R0_1 if sensor_num == 1 else Rs / MQ8_R0_2
        return max(0, ratio)
    except:
        return 0

def on_ws_message(ws, message):
    """Callback para mensajes WebSocket recibidos"""
    try:
        data = json.loads(message)
        ws_queue.put(('MESSAGE', data))
    except json.JSONDecodeError as e:
        print(f"Error decodificando JSON: {e}")

def on_ws_error(ws, error):
    """Callback para errores WebSocket"""
    print(f"WebSocket error: {error}")
    ws_queue.put(('ERROR', str(error)))

def on_ws_close(ws, close_status_code, close_msg):
    """Callback cuando se cierra la conexión WebSocket"""
    print("WebSocket cerrado")
    ws_queue.put(('CLOSED', None))

def on_ws_open(ws):
    """Callback cuando se abre la conexión WebSocket"""
    print("WebSocket conectado")
    ws_queue.put(('CONNECTED', None))

def connect_to_esp32():
    """Establece conexión WebSocket con el ESP32"""
    global ws_connection, connection_status
    
    try:
        # Cerrar conexión existente si la hay
        if ws_connection:
            ws_connection.close()
            time.sleep(0.5)
        
        connection_status = "Conectando..."
        
        # Crear URL de WebSocket
        ws_url = f"ws://{ESP32_IP}:{ESP32_PORT}"
        print(f"Conectando a {ws_url}...")
        
        # Crear conexión WebSocket
        ws_connection = websocket.WebSocketApp(
            ws_url,
            on_open=on_ws_open,
            on_message=on_ws_message,
            on_error=on_ws_error,
            on_close=on_ws_close
        )
        
        # Ejecutar en thread separado
        ws_thread = threading.Thread(
            target=lambda: ws_connection.run_forever(
                reconnect=5,  # Reconectar cada 5 segundos si se pierde conexión
                ping_interval=10,
                ping_timeout=5
            ),
            daemon=True
        )
        ws_thread.start()
        
        return True
        
    except Exception as e:
        print(f"Error conectando: {e}")
        connection_status = f"Error: {str(e)}"
        return False

def disconnect_from_esp32():
    """Desconecta del ESP32"""
    global ws_connection, connection_status
    
    try:
        if ws_connection:
            ws_connection.close()
            ws_connection = None
        
        connection_status = "Desconectado"
        print("Desconectado del ESP32")
        
    except Exception as e:
        print(f"Error al desconectar: {e}")

def send_command(command, params=None):
    """Envía comando al ESP32 por WebSocket"""
    global ws_connection
    
    if not ws_connection:
        print("No hay conexión WebSocket activa")
        return False
    
    try:
        cmd = {"command": command}
        if params:
            cmd.update(params)
        
        ws_connection.send(json.dumps(cmd))
        print(f"Comando enviado: {command}")
        return True
        
    except Exception as e:
        print(f"Error enviando comando: {e}")
        return False

def set_buzzer_mode(mode):
    """Cambia el modo del buzzer"""
    global buzzer_mode
    
    commands = {
        "ON": "BUZZER_ON",
        "OFF": "BUZZER_OFF",
        "AUTO": "BUZZER_AUTO"
    }
    
    if mode in commands:
        if send_command(commands[mode]):
            buzzer_mode = mode
            update_buzzer_status()

def update_buzzer_status():
    """Actualiza el estado visual del buzzer en la GUI"""
    if dpg.does_item_exist("buzzer_status"):
        status_text = f"Buzzer: {buzzer_mode}"
        if buzzer_mode == "ON":
            status_text += " On"
        elif buzzer_mode == "OFF":
            status_text += " Off"
        else:
            status_text += " Sonando"
        dpg.set_value("buzzer_status", status_text)

def update_data_thread():
    """Thread para actualizar datos continuamente"""
    global running, mq8_current_values, connection_status, current_alarm_level
    
    last_heartbeat = time.time()
    
    while running:
        current_time = time.time() - start_time
        
        # Procesar mensajes de la cola WebSocket
        while not ws_queue.empty():
            try:
                msg_type, data = ws_queue.get_nowait()
                
                if msg_type == 'CONNECTED':
                    connection_status = "Conectado"
                    last_heartbeat = time.time()
                
                elif msg_type == 'CLOSED':
                    connection_status = "Desconectado"
                
                elif msg_type == 'ERROR':
                    connection_status = f"Error: {data}"
                
                elif msg_type == 'MESSAGE':
                    # Procesar diferentes tipos de mensajes
                    data_type = data.get('type', '')
                    
                    if data_type == 'sensor_data':
                        # Actualizar buffers con datos del sensor
                        if 'bme280' in data:
                            bme = data['bme280']
                            buffers['temperatura'].add_point(current_time, bme['temperature'])
                            buffers['humedad'].add_point(current_time, bme['humidity'])
                            buffers['presion'].add_point(current_time, bme['pressure'])
                        
                        # Procesar datos MQ-8
                        if 'mq8_1' in data and 'mq8_2' in data:
                            mq1 = data['mq8_1']
                            mq2 = data['mq8_2']
                            
                            # Calcular PPM y ratio
                            ppm1 = calculate_ppm_from_voltage(mq1['voltage'], 1)
                            ppm2 = calculate_ppm_from_voltage(mq2['voltage'], 2)
                            ratio1 = calculate_ratio_from_voltage(mq1['voltage'], 1)
                            ratio2 = calculate_ratio_from_voltage(mq2['voltage'], 2)
                            
                            # Actualizar valores actuales
                            mq8_current_values['sensor1'] = {
                                'raw': mq1['raw'],
                                'voltage': mq1['voltage'],
                                'ppm': ppm1,
                                'ratio': ratio1
                            }
                            mq8_current_values['sensor2'] = {
                                'raw': mq2['raw'],
                                'voltage': mq2['voltage'],
                                'ppm': ppm2,
                                'ratio': ratio2
                            }
                            
                            # Actualizar buffers
                            buffers['mq8_1_raw'].add_point(current_time, mq1['raw'])
                            buffers['mq8_1_voltage'].add_point(current_time, mq1['voltage'])
                            buffers['mq8_1_ppm'].add_point(current_time, ppm1)
                            
                            buffers['mq8_2_raw'].add_point(current_time, mq2['raw'])
                            buffers['mq8_2_voltage'].add_point(current_time, mq2['voltage'])
                            buffers['mq8_2_ppm'].add_point(current_time, ppm2)
                            
                            # Guardar en histórico
                            historical_entry = {
                                "timestamp": datetime.now().isoformat(),
                                "packet_id": data.get('packet_id', 0),
                                "temperature": bme['temperature'] if 'bme280' in data else 0,
                                "pressure": bme['pressure'] if 'bme280' in data else 0,
                                "humidity": bme['humidity'] if 'bme280' in data else 0,
                                "mq8_1_raw": mq1['raw'],
                                "mq8_1_voltage": mq1['voltage'],
                                "mq8_1_ppm": ppm1,
                                "mq8_1_ratio": ratio1,
                                "mq8_2_raw": mq2['raw'],
                                "mq8_2_voltage": mq2['voltage'],
                                "mq8_2_ppm": ppm2,
                                "mq8_2_ratio": ratio2,
                                "alarm_level": data.get('alarm_level', 0)
                            }
                            historical_data.append(historical_entry)
                        
                        # Actualizar nivel de alarma
                        current_alarm_level = data.get('alarm_level', 0)
                        last_heartbeat = time.time()
                    
                    elif data_type == 'heartbeat':
                        last_heartbeat = time.time()
                    
                    elif data_type == 'system_info':
                        print(f"Información del sistema: {data}")
                        
            except queue.Empty:
                break
        
        # Verificar timeout de conexión
        if connection_status == "Conectado" and time.time() - last_heartbeat > 10:
            connection_status = "Timeout - Sin datos"
        
        # Actualizar gráficos
        update_plots(current_time)
        
        # Actualizar estadísticas
        update_statistics()
        update_mq8_statistics()
        
        # Actualizar estado de conexión en GUI
        update_connection_status()
        
        # Actualizar indicador de alarma
        update_alarm_indicator()
        
        time.sleep(update_interval)

def update_connection_status():
    """Actualiza el estado de conexión en la GUI"""
    if dpg.does_item_exist("connection_status"):
        status_color = [0, 255, 0] if connection_status == "Conectado" else [255, 0, 0]
        dpg.set_value("connection_status", f"Estado: {connection_status}")
        
        # Cambiar color del indicador
        if dpg.does_item_exist("connection_indicator"):
            if connection_status == "Conectado":
                dpg.configure_item("connection_indicator", default_value="On")
            elif "Error" in connection_status:
                dpg.configure_item("connection_indicator", default_value="Off")
            else:
                dpg.configure_item("connection_indicator", default_value="En espera")

def update_alarm_indicator():
    """Actualiza el indicador de alarma global"""
    if dpg.does_item_exist("alarm_indicator"):
        alarm_info = ALARM_STATES.get(current_alarm_level, ALARM_STATES[0])
        indicator_text = f"Nivel de Alarma: {alarm_info[1]} {alarm_info[0]}"
        dpg.set_value("alarm_indicator", indicator_text)

def update_plots(current_time):
    """Actualiza todos los gráficos con los datos actuales"""
    # Actualizar sensores ambientales
    for sensor_name in ['temperatura', 'humedad', 'presion']:
        buffer = buffers[sensor_name]
        series_tag = f"{sensor_name}_series"
        
        if dpg.does_item_exist(series_tag) and buffer.size() > 0:
            x_data, y_data = buffer.get_data()
            dpg.set_value(series_tag, [x_data, y_data])
        
        # Actualizar límites del eje X
        axis_tag = f"{sensor_name}_x_axis"
        if dpg.does_item_exist(axis_tag):
            dpg.set_axis_limits(axis_tag, current_time - history_seconds, current_time)
    
    # Actualizar gráficos MQ-8
    for i in [1, 2]:
        # Actualizar serie de voltaje
        voltage_series_tag = f"mq8_{i}_voltage_series"
        voltage_buffer = buffers[f'mq8_{i}_voltage']
        
        if dpg.does_item_exist(voltage_series_tag) and voltage_buffer.size() > 0:
            x_data, y_data = voltage_buffer.get_data()
            dpg.set_value(voltage_series_tag, [x_data, y_data])
        
        # Actualizar barras
        sensor_data = mq8_current_values[f'sensor{i}']
        
        # Barra de valor analógico
        bar_tag = f"mq8_{i}_raw_bar"
        if dpg.does_item_exist(bar_tag):
            dpg.set_value(bar_tag, [[i], [sensor_data['raw']]])
        
        # Barra de voltaje
        voltage_bar_tag = f"mq8_{i}_raw_voltage_bar"
        if dpg.does_item_exist(voltage_bar_tag):
            dpg.set_value(voltage_bar_tag, [[i], [sensor_data['voltage']]])
        
        # Barra de PPM
        ppm_bar_tag = f"mq8_{i}_ppm_bar"
        if dpg.does_item_exist(ppm_bar_tag):
            dpg.set_value(ppm_bar_tag, [[i], [sensor_data['ppm']]])
    
    # Actualizar límites del eje X para gráfico de voltaje MQ-8
    if dpg.does_item_exist("mq8_voltage_x_axis"):
        dpg.set_axis_limits("mq8_voltage_x_axis", current_time - history_seconds, current_time)

def update_statistics():
    """Actualiza las estadísticas de sensores ambientales"""
    for sensor_name in ['temperatura', 'humedad', 'presion']:
        buffer = buffers[sensor_name]
        if buffer.size() > 0:
            y_data = list(buffer.y_data)
            latest = y_data[-1]
            avg = sum(y_data) / len(y_data)
            min_val = min(y_data)
            max_val = max(y_data)
            
            stat_tag = f"{sensor_name}_stats"
            if dpg.does_item_exist(stat_tag):
                unit = get_unit(sensor_name)
                stats_text = (f"Actual: {latest:.2f}{unit}\n"
                            f"Promedio: {avg:.2f}{unit}\n"
                            f"Mín: {min_val:.2f}{unit} | Máx: {max_val:.2f}{unit}")
                dpg.set_value(stat_tag, stats_text)

def update_mq8_statistics():
    """Actualiza las estadísticas de los sensores MQ-8"""
    for i in [1, 2]:
        sensor_data = mq8_current_values[f'sensor{i}']
        
        # Actualizar texto de valores actuales
        text_tag = f"mq8_{i}_values"
        if dpg.does_item_exist(text_tag):
            # Determinar estado de alarma
            alarma = "ALARMA!" if sensor_data['ppm'] > 100 else "✓ Normal"
            
            text = (f"Sensor MQ-8 #{i}:\n"
                   f"Valor Analógico: {sensor_data['raw']}\n"
                   f"Voltaje: {sensor_data['voltage']:.3f} V\n"
                   f"PPM (H₂): {sensor_data['ppm']:.1f} {alarma}\n"
                   f"Ratio Rs/R0: {sensor_data['ratio']:.2f}")
            dpg.set_value(text_tag, text)
        
        # Actualizar estadísticas detalladas
        stats_tag = f"mq8_{i}_stats"
        if dpg.does_item_exist(stats_tag) and buffers[f'mq8_{i}_ppm'].size() > 0:
            ppm_data = list(buffers[f'mq8_{i}_ppm'].y_data)
            avg_ppm = sum(ppm_data) / len(ppm_data)
            max_ppm = max(ppm_data)
            min_ppm = min(ppm_data)
            
            # Calcular tiempo sobre umbral
            alarma_count = sum(1 for ppm in ppm_data if ppm > 100)
            alarma_percent = (alarma_count / len(ppm_data)) * 100 if ppm_data else 0
            
            stats_text = (f"PPM Promedio: {avg_ppm:.1f}\n"
                         f"PPM Máximo: {max_ppm:.1f}\n"
                         f"PPM Mínimo: {min_ppm:.1f}\n"
                         f"Tiempo en alarma: {alarma_percent:.1f}%")
            dpg.set_value(stats_tag, stats_text)

def get_unit(sensor_name):
    """Devuelve la unidad de medida para cada sensor"""
    units = {
        'temperatura': '°C',
        'humedad': '%',
        'presion': ' hPa',
        'cpu': '%',
        'memoria': '%'
    }
    return units.get(sensor_name, '')

def create_monitoring_interface():
    """Crea la interfaz principal mejorada para WebSocket"""
    dpg.create_context()
    
    # Tema personalizado
    with dpg.theme() as global_theme:
        with dpg.theme_component(dpg.mvAll):
            dpg.add_theme_style(dpg.mvStyleVar_WindowRounding, 5)
            dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 3)
            dpg.add_theme_style(dpg.mvStyleVar_GrabRounding, 3)
            dpg.add_theme_style(dpg.mvStyleVar_TabRounding, 3)
    
    dpg.bind_theme(global_theme)
    
    # Ventana principal
    with dpg.window(label="Monitor de Sensores H2 - WebSocket", tag="main_window"):
        
        # Barra de estado de conexión mejorada
        with dpg.group(horizontal=True):
            dpg.add_text("🔴", tag="connection_indicator")
            dpg.add_text("Estado: Desconectado", tag="connection_status")
            dpg.add_separator()
            dpg.add_button(label="Conectar", callback=lambda: connect_to_esp32())
            dpg.add_button(label="Desconectar", callback=lambda: disconnect_from_esp32())
            dpg.add_separator()
            dpg.add_input_text(label="IP ESP32", default_value=ESP32_IP, width=120, 
                             callback=lambda s, v: globals().update({"ESP32_IP": v}))
            dpg.add_input_int(label="Puerto", default_value=ESP32_PORT, width=80,
                            callback=lambda s, v: globals().update({"ESP32_PORT": v}))
        
        dpg.add_separator()
        
        # Panel de control del sistema
        with dpg.group(horizontal=True):
            dpg.add_text("Sistema de Alarma:", tag="alarm_indicator")
            dpg.add_separator()
            dpg.add_text("Buzzer: AUTO", tag="buzzer_status")
            dpg.add_button(label=" ON", callback=lambda: set_buzzer_mode("ON"), width=60)
            dpg.add_button(label=" OFF", callback=lambda: set_buzzer_mode("OFF"), width=60)
            dpg.add_button(label=" AUTO", callback=lambda: set_buzzer_mode("AUTO"), width=60)
            dpg.add_separator()
            dpg.add_button(label=" Estado", callback=lambda: send_command("STATUS"))
            dpg.add_button(label=" Reset ESP32", callback=lambda: send_command("RESET"))
        
        dpg.add_separator()
        
        # Tabs principales (mantener estructura original)
        with dpg.tab_bar():
            # Tab de sensores ambientales
            with dpg.tab(label="Sensores Ambientales"):
                
                with dpg.group(horizontal=False):
                    # Panel de control
                    with dpg.child_window(autosize_x=True, height=250):
                        dpg.add_text("Panel de Control", tag="control_title")
                        dpg.add_separator()
                        
                        # Control de historial
                        dpg.add_slider_float(
                            label="Historial (s)",
                            width=300,
                            default_value=30.0,
                            min_value=5.0,
                            max_value=120.0,
                            callback=lambda s, v: globals().update({"history_seconds": v})
                        )
                        
                        # Control de velocidad
                        dpg.add_slider_float(
                            label="Actualización (s)",
                            width=300,
                            default_value=0.1,
                            min_value=0.05,
                            max_value=1.0,
                            callback=lambda s, v: globals().update({"update_interval": v})
                        )
                        
                        dpg.add_separator()
                        
                        with dpg.group(horizontal=True):
                            # Estadísticas en columnas
                            for sensor_name in ['temperatura', 'humedad', 'presion']:
                                with dpg.group():
                                    dpg.add_text(f"{sensor_name.capitalize()}")
                                    dpg.add_text("Cargando...", tag=f"{sensor_name}_stats")
                                    if sensor_name != 'presion':
                                        dpg.add_text("     ")
                        
                        dpg.add_separator()
                        with dpg.group(horizontal=True):
                            dpg.add_button(label="🗑️ Limpiar Datos", callback=clear_all_data)
                            dpg.add_button(label="💾 Exportar JSON", callback=export_data_to_json)
                            dpg.add_text("", tag="export_status")
                    
                    # Contenedor para los tres gráficos
                    with dpg.child_window(autosize_x=True, height=600):
                        
                        # Gráfico de Temperatura
                        with dpg.plot(label="Temperatura", height=180, width=-1):
                            dpg.add_plot_legend()
                            
                            # Eje X
                            dpg.add_plot_axis(dpg.mvXAxis, label="Tiempo (s)", tag="temperatura_x_axis")
                            
                            # Eje Y
                            with dpg.plot_axis(dpg.mvYAxis, label="Temperatura (°C)") as y_axis:
                                # Serie de temperatura
                                dpg.add_line_series(
                                    [], [], 
                                    label="Temperatura",
                                    tag="temperatura_series"
                                )
                                
                                # Configurar color
                                dpg.bind_item_theme(dpg.last_item(), create_line_theme(COLORS['temperatura']))
                        
                        dpg.add_spacer(height=10)
                        
                        # Gráfico de Humedad
                        with dpg.plot(label="Humedad", height=180, width=-1):
                            dpg.add_plot_legend()
                            
                            # Eje X
                            dpg.add_plot_axis(dpg.mvXAxis, label="Tiempo (s)", tag="humedad_x_axis")
                            
                            # Eje Y
                            with dpg.plot_axis(dpg.mvYAxis, label="Humedad (%)") as y_axis:
                                # Serie de humedad
                                dpg.add_line_series(
                                    [], [], 
                                    label="Humedad",
                                    tag="humedad_series"
                                )
                                
                                # Configurar color
                                dpg.bind_item_theme(dpg.last_item(), create_line_theme(COLORS['humedad']))
                        
                        dpg.add_spacer(height=10)
                        
                        # Gráfico de Presión
                        with dpg.plot(label="Presión Atmosférica", height=180, width=-1):
                            dpg.add_plot_legend()
                            
                            # Eje X
                            dpg.add_plot_axis(dpg.mvXAxis, label="Tiempo (s)", tag="presion_x_axis")
                            
                            # Eje Y
                            with dpg.plot_axis(dpg.mvYAxis, label="Presión (hPa)") as y_axis:
                                # Serie de presión
                                dpg.add_line_series(
                                    [], [], 
                                    label="Presión",
                                    tag="presion_series"
                                )
                                
                                # Configurar color
                                dpg.bind_item_theme(dpg.last_item(), create_line_theme(COLORS['presion']))
            
            # Tab de detección de hidrógeno
            with dpg.tab(label="Detección de Hidrógeno"):
                
                with dpg.group(horizontal=False):
                    # Panel superior con valores en texto
                    with dpg.child_window(autosize_x=True, height=200):
                        dpg.add_text("Sensores MQ-8 - Detección de Hidrógeno")
                        dpg.add_separator()
                        
                        with dpg.group(horizontal=True):
                            with dpg.group():
                                dpg.add_text("Valores Actuales", tag="mq8_1_values")
                                dpg.add_spacer(height=10)
                                dpg.add_text("Estadísticas", tag="mq8_1_stats")
                            
                            dpg.add_spacer(width=50)
                            
                            with dpg.group():
                                dpg.add_text("Valores Actuales", tag="mq8_2_values")
                                dpg.add_spacer(height=10)
                                dpg.add_text("Estadísticas", tag="mq8_2_stats")
                    
                    # Gráficos
                    with dpg.child_window(autosize_x=True, height=700):
                        # Gráficos de barras en la parte superior
                        with dpg.group(horizontal=True):
                            # Gráfico de barras para valores analógicos
                            with dpg.plot(label="Valores Analógicos (0-4095)", height=250, width=400):
                                dpg.add_plot_legend()
                                
                                # Configurar eje X
                                x_axis = dpg.add_plot_axis(dpg.mvXAxis, label="Sensor", no_gridlines=True)
                                dpg.set_axis_limits(x_axis, 0, 3)
                                dpg.set_axis_ticks(x_axis, (("MQ8-1", 1), ("MQ8-2", 2)))
                                
                                # Configurar eje Y
                                with dpg.plot_axis(dpg.mvYAxis, label="Valor ADC") as y_axis:
                                    dpg.set_axis_limits(y_axis, 0, 4095)
                                    
                                    # Barras para cada sensor
                                    dpg.add_bar_series(
                                        [1], [0],
                                        label="MQ8-1",
                                        tag="mq8_1_raw_bar",
                                        weight=0.5
                                    )
                                    dpg.bind_item_theme(dpg.last_item(), create_bar_theme(COLORS['mq8_1']))
                                    
                                    dpg.add_bar_series(
                                        [2], [0],
                                        label="MQ8-2",
                                        tag="mq8_2_raw_bar",
                                        weight=0.5
                                    )
                                    dpg.bind_item_theme(dpg.last_item(), create_bar_theme(COLORS['mq8_2']))
                            
                            dpg.add_spacer(width=20)
                            
                            # Gráfico de barras para voltajes
                            with dpg.plot(label="Voltaje VRL", height=250, width=400):
                                dpg.add_plot_legend()
                                
                                # Configurar eje X
                                x_axis = dpg.add_plot_axis(dpg.mvXAxis, label="Sensor", no_gridlines=True)
                                dpg.set_axis_limits(x_axis, 0, 3)
                                dpg.set_axis_ticks(x_axis, (("MQ8-1", 1), ("MQ8-2", 2)))
                                
                                # Configurar eje Y
                                with dpg.plot_axis(dpg.mvYAxis, label="Vout") as y_axis:
                                    dpg.set_axis_limits(y_axis, 0, 5)
                                    
                                    # Barras para cada sensor
                                    dpg.add_bar_series(
                                        [1], [0],
                                        label="MQ8-1",
                                        tag="mq8_1_raw_voltage_bar",
                                        weight=0.5
                                    )
                                    dpg.bind_item_theme(dpg.last_item(), create_bar_theme(COLORS['mq8_1']))
                                    
                                    dpg.add_bar_series(
                                        [2], [0],
                                        label="MQ8-2",
                                        tag="mq8_2_raw_voltage_bar",
                                        weight=0.5
                                    )
                                    dpg.bind_item_theme(dpg.last_item(), create_bar_theme(COLORS['mq8_2']))
                            
                            dpg.add_spacer(width=20)
                            
                            # Gráfico de barras para PPM
                            with dpg.plot(label="Concentración H₂ (PPM)", height=250, width=400):
                                dpg.add_plot_legend()
                                
                                # Configurar eje X
                                x_axis = dpg.add_plot_axis(dpg.mvXAxis, label="Sensor", no_gridlines=True)
                                dpg.set_axis_limits(x_axis, 0, 3)
                                dpg.set_axis_ticks(x_axis, (("MQ8-1", 1), ("MQ8-2", 2)))
                                
                                # Configurar eje Y
                                with dpg.plot_axis(dpg.mvYAxis, label="PPM") as y_axis:
                                    dpg.set_axis_limits(y_axis, 0, 1000)
                                    
                                    # Barras para cada sensor
                                    dpg.add_bar_series(
                                        [1], [0],
                                        label="MQ8-1",
                                        tag="mq8_1_ppm_bar",
                                        weight=0.5
                                    )
                                    dpg.bind_item_theme(dpg.last_item(), create_bar_theme(COLORS['mq8_1']))
                                    
                                    dpg.add_bar_series(
                                        [2], [0],
                                        label="MQ8-2",
                                        tag="mq8_2_ppm_bar",
                                        weight=0.5
                                    )
                                    dpg.bind_item_theme(dpg.last_item(), create_bar_theme(COLORS['mq8_2']))
                        
                        dpg.add_spacer(height=20)
                        
                        # Gráfico de líneas para voltaje vs tiempo
                        with dpg.plot(label="Voltaje vs Tiempo", height=350, width=-1):
                            dpg.add_plot_legend()
                            
                            # Eje X
                            dpg.add_plot_axis(dpg.mvXAxis, label="Tiempo (s)", tag="mq8_voltage_x_axis")
                            
                            # Eje Y
                            with dpg.plot_axis(dpg.mvYAxis, label="Voltaje (V)") as y_axis:
                                dpg.set_axis_limits(y_axis, 0, 5.5)
                                
                                # Series de voltaje para cada sensor
                                dpg.add_line_series(
                                    [], [],
                                    label="MQ8-1",
                                    tag="mq8_1_voltage_series"
                                )
                                dpg.bind_item_theme(dpg.last_item(), create_line_theme(COLORS['mq8_1']))
                                
                                dpg.add_line_series(
                                    [], [],
                                    label="MQ8-2",
                                    tag="mq8_2_voltage_series"
                                )
                                dpg.bind_item_theme(dpg.last_item(), create_line_theme(COLORS['mq8_2']))
                                
                                # Línea de referencia para detección
                                dpg.add_hline_series(
                                    x=[0.5],
                                    label="Umbral Detección"
                                )
            
            # Nueva tab para control avanzado
            with dpg.tab(label="Control Avanzado"):
                with dpg.child_window(autosize_x=True, height=600):
                    dpg.add_text("Control Avanzado del Sistema")
                    dpg.add_separator()
                    
                    # Calibración de sensores
                    with dpg.group():
                        dpg.add_text("Calibración de Sensores MQ-8")
                        with dpg.group(horizontal=True):
                            dpg.add_input_float(
                                label="R0 Sensor 1",
                                default_value=MQ8_R0_1,
                                width=150,
                                callback=lambda s, v: globals().update({"MQ8_R0_1": v})
                            )
                            dpg.add_input_float(
                                label="R0 Sensor 2",
                                default_value=MQ8_R0_2,
                                width=150,
                                callback=lambda s, v: globals().update({"MQ8_R0_2": v})
                            )
                    
                    dpg.add_separator()
                    
                    # Umbrales de alarma personalizados
                    dpg.add_text("Umbrales de Alarma (PPM)")
                    with dpg.group():
                        dpg.add_slider_int(label="Umbral Bajo", default_value=100, min_value=50, max_value=500, width=300)
                        dpg.add_slider_int(label="Umbral Medio", default_value=300, min_value=100, max_value=1000, width=300)
                        dpg.add_slider_int(label="Umbral Alto", default_value=500, min_value=200, max_value=2000, width=300)
                    
                    dpg.add_separator()
                    
                    # Registro de eventos
                    dpg.add_text("Registro de Eventos")
                    dpg.add_input_text(
                        multiline=True,
                        readonly=True,
                        width=-1,
                        height=200,
                        tag="event_log"
                    )

def create_line_theme(color):
    """Crea un tema personalizado para una línea con el color especificado"""
    with dpg.theme() as theme:
        with dpg.theme_component(dpg.mvLineSeries):
            dpg.add_theme_color(dpg.mvPlotCol_Line, color, category=dpg.mvThemeCat_Plots)
    return theme

def create_bar_theme(color):
    """Crea un tema personalizado para barras con el color especificado"""
    with dpg.theme() as theme:
        with dpg.theme_component(dpg.mvBarSeries):
            dpg.add_theme_color(dpg.mvPlotCol_Fill, color, category=dpg.mvThemeCat_Plots)
    return theme

def clear_all_data():
    """Limpia todos los buffers de datos"""
    for buffer in buffers.values():
        buffer.clear()
    
    # También limpiar datos históricos si se desea
    if dpg.does_item_exist("event_log"):
        current_time = datetime.now().strftime("%H:%M:%S")
        dpg.set_value("event_log", dpg.get_value("event_log") + f"\n[{current_time}] Datos limpiados")

def main():
    """Función principal"""
    global running
    
    # Crear interfaz
    create_monitoring_interface()
    
    # Configurar viewport
    dpg.create_viewport(
        title="Monitor de Sensores H2 - ESP32 WebSocket", 
        width=1400, 
        height=1000
    )
    dpg.setup_dearpygui()
    dpg.show_viewport()
    dpg.set_primary_window("main_window", True)
    
    # Iniciar thread de actualización
    update_thread = threading.Thread(target=update_data_thread, daemon=True)
    update_thread.start()
    
    # Loop principal
    while dpg.is_dearpygui_running():
        dpg.render_dearpygui_frame()
    
    # Limpiar
    running = False
    disconnect_from_esp32()
    update_thread.join(timeout=1)
    dpg.destroy_context()

if __name__ == "__main__":
    main()