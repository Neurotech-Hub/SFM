// Node – Full SFM node sketch.
//
// Bring-up checklist:
//   1. Flash to an ESP32-S3-MINI-1 with the SFM hardware attached.
//   2. Open Serial Monitor at 115200 baud.
//   3. On first boot the node has no ID; it will print "WaitAEI" until the
//      base station (or the test bench) drives GPIO14 HIGH.
//   4. Bench shortcut: type "id <n>" in Serial Monitor to assign an ID without
//      a base station (e.g. "id 4" assigns node ID 4).
//   5. Once an ID is assigned, the node listens for CAN commands on
//      0x104 (for node 4) and the broadcast address 0x100.
//
// On-board button (BTN_IO_11):
//   short click – recalibrate the mouse-presence pad (keep the pad CLEAR)
//   3 s hold    – clear the NVS node ID, then re-enter discovery
//
// Presence threshold, calibration factor, and node ID live in NVS, so a
// calibrated node keeps its settings across reboots. Serial 'cal' does the
// same as a button click. Calibration rule: thr = mean + factor * std_dev.
//
// CAN frame reference (250 kbps, 11-bit IDs):
//   Commands  base->node : 0x100 + nodeId  (0x100 = broadcast)
//   Heartbeat node->base : 0x200 + nodeId  every ~1 s
//   Events    node->base : 0x300 + nodeId  on OnPlate/Loaded/DomeOpened/PelletTaken/Fault
//   Discovery node<->base: 0x080-0x083

#include <SFM.h>

sfm::SFM gSfm;

// ---------------------------------------------------------------------------
// Serial command helpers
// ---------------------------------------------------------------------------
static void printHelp() {
    Serial.println(F("Commands:"));
    Serial.println(F("  id <n>     assign node ID (1-254)"));
    Serial.println(F("  d          dispense pellet"));
    Serial.println(F("  a          recover (stop motion / clear fault)"));
    Serial.println(F("  s          print status"));
    Serial.println(F("  cal        calibrate presence pad (5 s, keep pad CLEAR)"));
    Serial.println(F("  thr        print presence raw + threshold + factor"));
    Serial.println(F("  factor     print calibration factor"));
    Serial.println(F("  factor <n> set factor (thr = mean + factor*σ); saves to NVS"));
    Serial.println(F("  thrclr     forget saved presence cal (back to defaults)"));
    Serial.println(F("  clr        clear NVS node ID (forces first-boot next reset)"));
}

static const char *discStr(sfm::DiscoveryState s) {
    switch (s) {
        case sfm::DiscoveryState::WaitAEI:    return "WaitAEI";
        case sfm::DiscoveryState::CheckNVS:   return "CheckNVS";
        case sfm::DiscoveryState::Announce:   return "Announce";
        case sfm::DiscoveryState::WaitAssign: return "WaitAssign";
        case sfm::DiscoveryState::Rejoin:     return "Rejoin";
        case sfm::DiscoveryState::Enabled:    return "Enabled";
    }
    return "?";
}

static const char *stateStr(sfm::DispenseState s) {
    switch (s) {
        case sfm::DispenseState::Idle:        return "Idle";
        case sfm::DispenseState::Seeking:  return "Seeking";
        case sfm::DispenseState::Lowering:    return "Lowering";
        case sfm::DispenseState::Loading:  return "Loading";
        case sfm::DispenseState::Raising:     return "Raising";
        case sfm::DispenseState::Loaded:   return "Loaded";
        case sfm::DispenseState::Dwelling:    return "Dwelling";
        case sfm::DispenseState::Fault:       return "Fault";
    }
    return "?";
}

static void printStatus() {
    Serial.print(F("[SFM] nodeId="));   Serial.print(gSfm.identity().nodeId());
    Serial.print(F(" discovery="));     Serial.print(discStr(gSfm.identity().discoveryState()));
    Serial.print(F(" dispense="));      Serial.print(stateStr(gSfm.dispenser().state()));
    Serial.print(F(" pellets="));       Serial.print(gSfm.dispenser().pelletCount());
    Serial.print(F(" presence="));      Serial.print(gSfm.mousePresent());
    Serial.print(F(" pellet="));        Serial.print(gSfm.dispenser().pelletOnPlate());
    Serial.print(F(" load_position=")); Serial.print(gSfm.dispenser().atLoadPosition());
    Serial.print(F(" dome_open="));      Serial.println(gSfm.dispenser().domeOpen());
}

static void printPresence() {
    Serial.print(F("[PRESENCE] raw="));
    Serial.print(gSfm.presenceRaw());
    Serial.print(F("  thr="));
    Serial.print(gSfm.presenceThreshold());
    Serial.print(F("  factor="));
    Serial.print(gSfm.presence().factor(), 2);
    if (gSfm.presence().hasCalStats()) {
        Serial.print(F("  mean="));
        Serial.print(gSfm.presence().calMean(), 1);
        Serial.print(F("  σ="));
        Serial.print(gSfm.presence().calStdDev(), 1);
    }
    Serial.print(F("  -> "));
    Serial.println(gSfm.mousePresent() ? F("PRESENT") : F("clear"));
}

// Report the outcome of a calibration started by serial or by the button.
static void reportPresenceEvents() {
    switch (gSfm.takePresenceEvent()) {
        case sfm::PresenceEvent::CalibrationStarted:
            Serial.print(F("[PRESENCE] CAL START - keep pad CLEAR for 5 s (LED9 solid)  factor="));
            Serial.println(gSfm.presence().factor(), 2);
            break;

        case sfm::PresenceEvent::CalibrationDone: {
            const sfm::PresenceCalibration &c = gSfm.presence().lastCalibration();
            Serial.print(F("[PRESENCE] CAL DONE  samples=")); Serial.print(c.samples);
            Serial.print(F("  mean=")); Serial.print(c.mean, 1);
            Serial.print(F("  std_dev=")); Serial.print(c.stdDev, 1);
            Serial.print(F("  factor=")); Serial.println(c.factor, 2);
            Serial.print(F("[PRESENCE] thr = mean + factor*σ = "));
            Serial.print(c.threshold);
            Serial.println(F("  (saved to NVS)"));
            break;
        }

        case sfm::PresenceEvent::CalibrationFailed:
            Serial.println(F("[PRESENCE] CAL FAILED - not enough samples; threshold unchanged"));
            break;

        default:
            break;
    }
}

// ---------------------------------------------------------------------------
// Serial line buffer
// ---------------------------------------------------------------------------
static char  lineBuf[32];
static uint8_t lineIdx = 0;

static void handleSerialLine(const char *line) {
    if (strncmp(line, "id ", 3) == 0) {
        uint8_t id = (uint8_t)atoi(line + 3);
        if (id > 0) {
            gSfm.identity().assignId(id);
            Serial.print(F("Node ID set to ")); Serial.println(id);
        }
    } else if (strcmp(line, "d") == 0) {
        if (gSfm.dispenser().dispense()) Serial.println(F("Dispense started."));
        else { Serial.print(F("Cannot dispense - ")); Serial.println(stateStr(gSfm.dispenser().state())); }
    } else if (strcmp(line, "a") == 0) {
        gSfm.dispenser().recover();
        Serial.println(F("Recovered."));
    } else if (strcmp(line, "s") == 0) {
        printStatus();
    } else if (strcmp(line, "cal") == 0) {
        if (!gSfm.startPresenceCalibration()) {
            Serial.println(F("[PRESENCE] Calibration already running"));
        }
    } else if (strcmp(line, "thr") == 0) {
        printPresence();
    } else if (strcmp(line, "factor") == 0) {
        Serial.print(F("[PRESENCE] factor="));
        Serial.println(gSfm.presence().factor(), 2);
        Serial.println(F("Usage: factor <n>  (e.g. factor 3 or factor 2.5)"));
        if (!gSfm.presence().hasCalStats()) {
            Serial.println(F("No cal stats yet – run 'cal' or press button first to re-apply."));
        }
    } else if (strncmp(line, "factor ", 7) == 0) {
        const char *p = line + 7;
        char *end = nullptr;
        float f = strtof(p, &end);
        if (end == p || f <= 0.0f) {
            Serial.println(F("Invalid factor. Usage: factor <n>"));
        } else if (!gSfm.presence().setFactor(f)) {
            Serial.println(F("Invalid factor."));
        } else {
            if (gSfm.presence().hasCalStats()) {
                Serial.println(F("[PRESENCE] Factor saved; threshold re-applied from last cal."));
            } else {
                Serial.println(F("[PRESENCE] Factor saved; run 'cal' to apply (no cal stats yet)."));
            }
            printPresence();
        }
    } else if (strcmp(line, "thrclr") == 0) {
        gSfm.presence().clearStoredThreshold();
        Serial.println(F("Saved presence cal cleared; using compile-time defaults."));
        printPresence();
    } else if (strcmp(line, "clr") == 0) {
        gSfm.identity().clearId();
        Serial.println(F("NVS id cleared. Node waits for AEI / discovery to ANNOUNCE."));
    } else if (strcmp(line, "h") == 0 || strcmp(line, "help") == 0) {
        printHelp();
    } else {
        Serial.print(F("Unknown command: ")); Serial.println(line);
    }
}

// ---------------------------------------------------------------------------
void setup() {
    Serial.begin(115200);
    while (!Serial && millis() < 3000) {}
    Serial.println(F("\n===== SFM Node ====="));

    if (!gSfm.begin()) {
        Serial.println(F("WARNING: one or more services failed to initialise"));
    }

    // Print MAC UUID
    const uint8_t *m = gSfm.identity().mac();
    Serial.printf("MAC UUID: %02X:%02X:%02X:%02X:%02X:%02X\n",
                  m[0], m[1], m[2], m[3], m[4], m[5]);
    Serial.printf("Saved nodeId: %d\n", gSfm.identity().nodeId());
    printPresence();
    Serial.println(F("BTN: click = presence calibration, 3 s hold = clear node ID"));
    Serial.println(F("Type 'h' for help."));
}

void loop() {
    gSfm.update();
    reportPresenceEvents();

    // Non-blocking serial line reader
    while (Serial.available()) {
        char c = (char)Serial.read();
        if (c == '\r') continue;
        if (c == '\n') {
            lineBuf[lineIdx] = '\0';
            if (lineIdx > 0) handleSerialLine(lineBuf);
            lineIdx = 0;
        } else if (lineIdx < (sizeof(lineBuf) - 1)) {
            lineBuf[lineIdx++] = c;
        }
    }
}
