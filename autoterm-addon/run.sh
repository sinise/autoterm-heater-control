#!/usr/bin/with-contenv bashio
# shellcheck shell=bash

PANEL_PORT=$(bashio::config 'panel_port')
HEATER_PORT=$(bashio::config 'heater_port')
BAUD=$(bashio::config 'baud')
PREHEAT_DEFAULT=$(bashio::config 'preheat_default_minutes')
AUTO_TARGET=$(bashio::config 'auto_target_default')
PREVENT_FREEZING_TARGET=$(bashio::config 'prevent_freezing_target_default')
HEATER_PROFILE=$(bashio::config 'heater_profile')
DEBUG_MODE_DEFAULT=$(bashio::config 'debug_mode_default')
DEBUG_INTERVAL_DEFAULT=$(bashio::config 'debug_interval_seconds_default')
CAPTURE_LOG_DEFAULT=$(bashio::config 'capture_log_default')
CAPTURE_LOG_MAX_MB=$(bashio::config 'capture_log_max_mb')
EXTERNAL_TEMP_SENSOR_ENTITY=$(bashio::config 'external_temp_sensor_entity')

if bashio::config.true 'autodiscover_ports'; then
    bashio::log.info "autodiscover_ports enabled -- probing serial ports for panel/heater"
    export AUTOTERM_BAUD="${BAUD}"
    if DISCOVERY_OUT=$(python3 /app/autoterm_addon.py --discover-ports); then
        DISCOVERED_PANEL=$(echo "${DISCOVERY_OUT}" | grep '^PANEL_PORT=' | cut -d= -f2)
        DISCOVERED_HEATER=$(echo "${DISCOVERY_OUT}" | grep '^HEATER_PORT=' | cut -d= -f2)
        if [ -n "${DISCOVERED_PANEL}" ] && [ -n "${DISCOVERED_HEATER}" ]; then
            bashio::log.info "Discovered panel=${DISCOVERED_PANEL} heater=${DISCOVERED_HEATER} -- saving to add-on options"
            PANEL_PORT="${DISCOVERED_PANEL}"
            HEATER_PORT="${DISCOVERED_HEATER}"
            # Best-effort: still use the discovered ports for this run even
            # if the Supervisor can't be updated (e.g. older bashio).
            bashio::addon.option 'panel_port' "${PANEL_PORT}" \
                || bashio::log.warning "Could not save discovered panel_port to add-on options"
            bashio::addon.option 'heater_port' "${HEATER_PORT}" \
                || bashio::log.warning "Could not save discovered heater_port to add-on options"
        else
            bashio::log.warning "Discovery ran but produced no usable ports -- using configured panel_port/heater_port"
        fi
    else
        bashio::log.warning "Port discovery failed -- using configured panel_port=${PANEL_PORT} heater_port=${HEATER_PORT}"
    fi
fi

# bashio logs its own ERROR line here when no add-on provides the mqtt
# service at all (as opposed to it being merely unconfigured) -- harmless,
# the `else` branch below handles that case either way, so it's quieted.
if bashio::services.available 'mqtt' 2>/dev/null; then
    bashio::log.info "Using MQTT service auto-discovery"
    MQTT_HOST=$(bashio::services 'mqtt' 'host')
    MQTT_PORT=$(bashio::services 'mqtt' 'port')
    MQTT_USER=$(bashio::services 'mqtt' 'username')
    MQTT_PASS=$(bashio::services 'mqtt' 'password')
else
    bashio::log.warning "No MQTT service found -- falling back to add-on options (install the Mosquitto broker add-on for auto-discovery, or set mqtt_host/mqtt_port in this add-on's Configuration)"
    MQTT_HOST=$(bashio::config 'mqtt_host')
    MQTT_PORT=$(bashio::config 'mqtt_port')
    MQTT_USER=$(bashio::config 'mqtt_username')
    MQTT_PASS=$(bashio::config 'mqtt_password')
fi

mkdir -p /config/autoterm_debug

export AUTOTERM_PANEL_PORT="${PANEL_PORT}"
export AUTOTERM_HEATER_PORT="${HEATER_PORT}"
export AUTOTERM_BAUD="${BAUD}"
export AUTOTERM_PREHEAT_DEFAULT="${PREHEAT_DEFAULT}"
export AUTOTERM_AUTO_TARGET_DEFAULT="${AUTO_TARGET}"
export AUTOTERM_PREVENT_FREEZING_TARGET_DEFAULT="${PREVENT_FREEZING_TARGET}"
export AUTOTERM_HEATER_PROFILE="${HEATER_PROFILE}"
export AUTOTERM_MQTT_HOST="${MQTT_HOST}"
export AUTOTERM_MQTT_PORT="${MQTT_PORT}"
export AUTOTERM_MQTT_USERNAME="${MQTT_USER}"
export AUTOTERM_MQTT_PASSWORD="${MQTT_PASS}"
export AUTOTERM_DEBUG_MODE_DEFAULT="${DEBUG_MODE_DEFAULT}"
export AUTOTERM_DEBUG_INTERVAL_DEFAULT="${DEBUG_INTERVAL_DEFAULT}"
export AUTOTERM_CAPTURE_LOG_DEFAULT="${CAPTURE_LOG_DEFAULT}"
export AUTOTERM_CAPTURE_LOG_MAX_MB="${CAPTURE_LOG_MAX_MB}"
export AUTOTERM_CAPTURE_LOG_DIR="/config/autoterm_debug"
export AUTOTERM_EXTERNAL_TEMP_SENSOR_ENTITY="${EXTERNAL_TEMP_SENSOR_ENTITY}"

bashio::log.info "Starting Autoterm Heater bridge: panel=${PANEL_PORT} heater=${HEATER_PORT} mqtt=${MQTT_HOST}:${MQTT_PORT} debug_default=${DEBUG_MODE_DEFAULT} capture_default=${CAPTURE_LOG_DEFAULT} heater_profile=${HEATER_PROFILE}"

exec python3 /app/autoterm_addon.py
