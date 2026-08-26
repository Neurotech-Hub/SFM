# Wiring map

Firmware pin names → physical job. Source of truth: `[src/hardware/VFMPins.h](../src/hardware/VFMPins.h)`.
Cycle behaviour for the sensors: [DISPENSE_CYCLE.md](DISPENSE_CYCLE.md).

## Symbol → job


| Symbol          | Job                               | GPIO    | Notes                                                                       |
| --------------- | --------------------------------- | ------- | --------------------------------------------------------------------------- |
| **PG1**         | Pellet sensor                     | 46      | Beam break = LOW (`INPUT_PULLUP`). ESP32-S3 strapping pin — safe after boot |
| **PG2**         | Load-position sensor              | 45      | Beam break = LOW (`INPUT_PULLUP`). Actuator at load height. Strapping pin   |
| **PG3**         | Dome sensor                       | 44      | Dome open = HIGH (`INPUT_PULLDOWN`). Idle LOW                               |
| **M1**          | Pellet motor (feed wheel)         | 35–38   | 28BYJ-48 via ULN2003                                                        |
| **M2**          | Actuator motor (elevator / plate) | 40–43   | 28BYJ-48 via ULN2003                                                        |
| **Touch**    | Animal presence sensor            | 5       | Capacitive `touchRead`; presence = raw > threshold                          |
| **STATUS_LED**  | Status LED                        | 39      | Fault / discovery / ping blink                                              |
| **LED 9**       | Dome mirror (when enabled)        | 9       | Also boot / discovery / button-hold                                         |
| **LED 10**      | Pellet-sensor mirror              | 10      | Debounced PG1                                                               |
| **BTN**         | User button                       | 11      | Active LOW; long-press clears NVS node ID                                   |
| **AEI**         | Address Enable In                 | 14      | Daisy-chain discovery in                                                    |
| **AEO**         | Address Enable Out                | 47      | Daisy-chain discovery out                                                   |
| **CAN TX / RX** | TWAI                              | 33 / 13 | 250 kbps                                                                    |




## Motors (coil pins)

AccelStepper `HALF4WIRE` argument order: `(A1, A3, A2, A4)` = Orange, Pink, Yellow, Blue.


| Motor  | Job            | A1 (Orange) | A2 (Yellow) | A3 (Pink) | A4 (Blue) |
| ------ | -------------- | ----------- | ----------- | --------- | --------- |
| **M1** | Pellet motor   | GPIO35      | GPIO36      | GPIO37    | GPIO38    |
| **M2** | Actuator motor | GPIO40      | GPIO41      | GPIO42    | GPIO43    |


M2 direction in firmware: **+speed = UP**, **−speed = DOWN**.

## Sensors (quick check)


| Sensor           | Symbol   | Asserted when           | Live LED |
| ---------------- | -------- | ----------------------- | -------- |
| Pellet on plate  | PG1      | Beam broken (LOW)       | LED 10   |
| At load position | PG2      | Beam broken (LOW)       | —        |
| Dome open        | PG3      | Pin HIGH                | LED 9    |
| Animal present   | PRESENCE | `touchRead` > threshold | —        |


