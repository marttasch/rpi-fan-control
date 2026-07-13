#!/usr/bin/env python3
"""
PWM Fan Controller for Raspberry Pi with MQTT for Home Assistant

Controls a PWM fan and publishes CPU temperature, target fan speed (PWM %), and actual fan speed (RPM) to MQTT.
Auto-discovery for Home Assistant via Mosquitto MQTT broker.

Usage:
    python3 pwm_fan_controller.py

Dependencies:
    - pigpio library
    - paho-mqtt library
"""

import pigpio
import time
import sys
import logging
import signal
import os
import json
import paho.mqtt.client as mqtt

# --- Configuration ---
CONFIG_FILE = "fan_config.json"

DEFAULT_CONFIG = {
    "FAN_GPIO_PIN": 15,
    "TACH_GPIO_PIN": 14,
    "TEMP_OFF": 35,
    "TEMP_FULL": 65,
    "PWM_MIN": 0,
    "PWM_MAX": 255,
    "SLEEP_INTERVAL": 2,
    "HYSTERESIS": 2,
    "TEMP_FILE": "/sys/class/thermal/thermal_zone0/temp",
    "PULSES_PER_REV": 2,
    # MQTT Configuration
    "MQTT_BROKER": "192.168.2.25",
    "MQTT_PORT": 1883,
    "MQTT_USER": None,
    "MQTT_PASSWORD": None,
    "MQTT_DEVICE_ID": "raspberry_pi_fan_controller",
    "MQTT_DEVICE_NAME": "Raspberry Pi Fan Controller",
    "MQTT_TOPIC_PREFIX": "homeassistant",
}

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


class PWMFanController:
    def __init__(self, config):
        # Fan control settings
        self.gpio_pin = config["FAN_GPIO_PIN"]
        self.tach_pin = config["TACH_GPIO_PIN"]
        self.pulses_per_rev = config["PULSES_PER_REV"]
        self.temp_off = config["TEMP_OFF"]
        self.temp_full = config["TEMP_FULL"]
        self.pwm_min = config["PWM_MIN"]
        self.pwm_max = config["PWM_MAX"]
        self.sleep_interval = config["SLEEP_INTERVAL"]
        self.hysteresis = config["HYSTERESIS"]
        self.temp_file = config["TEMP_FILE"]

        # MQTT settings
        self.mqtt_broker = config["MQTT_BROKER"]
        self.mqtt_port = config["MQTT_PORT"]
        self.mqtt_user = config["MQTT_USER"]
        self.mqtt_password = config["MQTT_PASSWORD"]
        self.mqtt_device_id = config["MQTT_DEVICE_ID"]
        self.mqtt_device_name = config["MQTT_DEVICE_NAME"]
        self.mqtt_topic_prefix = config["MQTT_TOPIC_PREFIX"]

        # State
        self.last_set_pwm = -1
        self.last_checked_temp = 0
        self.pi = pigpio.pi()
        self.pulse_count = 0
        self.last_pulse_time = time.time()
        self.mqtt_client = None

        # Validate and initialize GPIO
        if not self.pi.connected:
            logging.error("Could not connect to pigpiod. Is it running?")
            sys.exit(1)

        if not (0 <= self.gpio_pin <= 27) or not (0 <= self.tach_pin <= 27):
            logging.error("Invalid GPIO pin number.")
            sys.exit(1)

        self.pi.set_mode(self.gpio_pin, pigpio.OUTPUT)
        self.pi.set_PWM_dutycycle(self.gpio_pin, 0)
        self.pi.set_mode(self.tach_pin, pigpio.INPUT)
        self.pi.set_pull_up_down(self.tach_pin, pigpio.PUD_UP)

        # Tachometer callback
        self.cb = self.pi.callback(self.tach_pin, pigpio.FALLING_EDGE, self._pulse_callback)

        # Initialize MQTT if configured
        if self.mqtt_broker:
            self._setup_mqtt()

        signal.signal(signal.SIGTERM, self.signal_handler)
        signal.signal(signal.SIGINT, self.signal_handler)
        self.last_set_pwm = 0

    def _pulse_callback(self, gpio, level, tick):
        """Callback for tachometer pulse counting."""
        self.pulse_count += 1

    def _setup_mqtt(self):
        """Set up MQTT client and connect to broker."""
        self.mqtt_client = mqtt.Client(client_id=f"{self.mqtt_device_id}_publisher")
        if self.mqtt_user and self.mqtt_password:
            self.mqtt_client.username_pw_set(self.mqtt_user, self.mqtt_password)
        self.mqtt_client.on_connect = self._on_mqtt_connect
        try:
            self.mqtt_client.connect(self.mqtt_broker, self.mqtt_port, 60)
            self.mqtt_client.loop_start()
            logging.info(f"Connected to MQTT broker at {self.mqtt_broker}")
        except Exception as e:
            logging.error(f"Failed to connect to MQTT broker: {e}")
            self.mqtt_client = None

    def _on_mqtt_connect(self, client, userdata, flags, rc):
        """MQTT connection callback."""
        if rc == 0:
            logging.info("MQTT connected, publishing discovery messages")
            self._publish_discovery()
        else:
            logging.error(f"MQTT connection failed with code {rc}")

    def _publish_discovery(self):
        """Publish Home Assistant auto-discovery messages."""
        if not self.mqtt_client:
            return

        device_info = {
            "identifiers": [self.mqtt_device_id],
            "name": self.mqtt_device_name,
            "manufacturer": "Custom",
            "model": "PWM Fan Controller",
        }

        base_topic = f"{self.mqtt_topic_prefix}/sensor/{self.mqtt_device_id}"

        # CPU Temperature Sensor
        cpu_temp_config = {
            "name": "CPU Temperature",
            "device_class": "temperature",
            "unit_of_measurement": "°C",
            "state_topic": f"{base_topic}/cpu_temp/state",
            "value_template": "{{ value | float }}",
            "unique_id": f"{self.mqtt_device_id}_cpu_temp",
            "device": device_info,
        }
        self.mqtt_client.publish(
            f"{base_topic}/cpu_temp/config",
            json.dumps(cpu_temp_config),
            qos=1,
            retain=True
        )

        # Speed Percentage Sensor
        speed_pct_config = {
            "name": "Fan Speed Target",
            "unit_of_measurement": "%",
            "state_topic": f"{base_topic}/speed_percentage/state",
            "value_template": "{{ value | int }}",
            "unique_id": f"{self.mqtt_device_id}_speed_percentage",
            "device": device_info,
        }
        self.mqtt_client.publish(
            f"{base_topic}/speed_percentage/config",
            json.dumps(speed_pct_config),
            qos=1,
            retain=True
        )

        # Tacho RPM Sensor
        tacho_rpm_config = {
            "name": "Fan RPM",
            "unit_of_measurement": "rpm",
            "state_topic": f"{base_topic}/tacho_rpm/state",
            "value_template": "{{ value | int }}",
            "unique_id": f"{self.mqtt_device_id}_tacho_rpm",
            "device": device_info,
        }
        self.mqtt_client.publish(
            f"{base_topic}/tacho_rpm/config",
            json.dumps(tacho_rpm_config),
            qos=1,
            retain=True
        )

    def _publish_state(self, cpu_temp, target_pwm, fan_rpm):
        """Publish current state to MQTT."""
        if not self.mqtt_client:
            return

        base_topic = f"{self.mqtt_topic_prefix}/sensor/{self.mqtt_device_id}"

        # Publish CPU temperature
        if cpu_temp is not None:
            self.mqtt_client.publish(
                f"{base_topic}/cpu_temp/state",
                f"{cpu_temp:.1f}",
                qos=1,
                retain=False
            )

        # Publish speed percentage (0-100%)
        if target_pwm is not None:
            speed_pct = int((target_pwm / 255) * 100)
            self.mqtt_client.publish(
                f"{base_topic}/speed_percentage/state",
                str(speed_pct),
                qos=1,
                retain=False
            )

        # Publish tacho RPM
        if fan_rpm is not None:
            self.mqtt_client.publish(
                f"{base_topic}/tacho_rpm/state",
                f"{int(fan_rpm)}",
                qos=1,
                retain=False
            )

    def signal_handler(self, sig, frame):
        logging.info("Signal received, shutting down.")
        self.cleanup()
        sys.exit(0)

    def get_cpu_temperature(self):
        try:
            with open(self.temp_file, 'r') as f:
                return int(f.read().strip()) / 1000.0
        except FileNotFoundError:
            logging.error(f"Temperature file not found at {self.temp_file}")
        except ValueError:
            logging.error(f"Could not parse temperature from {self.temp_file}")
        except Exception as e:
            logging.error(f"Error reading temperature: {e}")
        return None

    def get_fan_rpm(self):
        """Calculate fan RPM from tachometer pulses."""
        current_time = time.time()
        time_elapsed = current_time - self.last_pulse_time

        if time_elapsed >= 1.0:
            rpm = (self.pulse_count / self.pulses_per_rev) * 60 / time_elapsed
            self.pulse_count = 0
            self.last_pulse_time = current_time
            return rpm
        return None

    def calculate_pwm(self, temp):
        if temp is None:
            return self.pwm_min

        if temp <= self.temp_off:
            return self.pwm_min
        elif temp >= self.temp_full:
            return self.pwm_max
        else:
            temp_range = float(self.temp_full - self.temp_off)
            pwm_range = float(self.pwm_max - self.pwm_min)
            pwm = int(((temp - self.temp_off) * pwm_range / temp_range) + self.pwm_min)
            return max(self.pwm_min, min(self.pwm_max, pwm))

    def run(self):
        logging.info("PWM Fan Controller started.")
        try:
            while True:
                cpu_temp = self.get_cpu_temperature()
                fan_rpm = self.get_fan_rpm()

                if cpu_temp is not None:
                    target_pwm = self.calculate_pwm(cpu_temp)

                    if target_pwm != self.last_set_pwm:
                        self.pi.set_PWM_dutycycle(self.gpio_pin, target_pwm)
                        self.last_set_pwm = target_pwm

                    # Console output
                    rpm_str = f"{fan_rpm:.0f} RPM" if fan_rpm is not None else "N/A"
                    print(f"CPU: {cpu_temp:.1f}°C | Target PWM: {target_pwm} | Actual: {rpm_str}")

                    # MQTT output
                    self._publish_state(cpu_temp, target_pwm, fan_rpm)

                time.sleep(self.sleep_interval)

        except Exception as e:
            logging.error(f"An error occurred: {e}")
        finally:
            self.cleanup()

    def cleanup(self):
        if self.pi and self.pi.connected:
            try:
                if hasattr(self, 'cb') and self.cb:
                    self.cb.cancel()
                logging.info("Setting fan PWM to 0 before exiting.")
                self.pi.set_PWM_dutycycle(self.gpio_pin, 0)
                self.pi.stop()
                logging.info("pigpio connection stopped.")
            except Exception as e:
                logging.error(f"Error during pigpio cleanup: {e}")

        if self.mqtt_client:
            try:
                self.mqtt_client.loop_stop()
                self.mqtt_client.disconnect()
                logging.info("MQTT client disconnected.")
            except Exception as e:
                logging.error(f"Error during MQTT cleanup: {e}")

        logging.info("Fan controller stopped.")


def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                config = json.load(f)
                logging.info(f"Loaded configuration from {CONFIG_FILE}")
                return {**DEFAULT_CONFIG, **config}
        except Exception as e:
            logging.error(f"Error loading config file: {e}")
    logging.info("Using default configuration.")
    return DEFAULT_CONFIG


if __name__ == "__main__":
    config = load_config()
    controller = PWMFanController(config)
    controller.run()
