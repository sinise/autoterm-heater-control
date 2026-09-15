# Autoterm Heater

Home Assistant app for an Autoterm-family diesel heater: owns
both UART ports directly (inline between the comfort panel and the heater),
publishes live status and exposes start/stop/thermostat controls as Home
Assistant entities via MQTT discovery. Also includes optional
extended-telemetry probing (across 19 vendor heater profiles), a
downloadable raw traffic capture log, and a Bypass mode for capturing a
clean baseline -- all off by default, useful for continuing the protocol
reverse-engineering or just watching more of what the heater is doing.

See the Documentation tab for wiring, configuration, and what each sensor
means -- read it before enabling debug mode, since it involves sending an
experimental handshake frame toward the heater on the live bus (see DOCS.md
for exactly what's confirmed safe and what's still experimental).
