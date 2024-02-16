/*
  Code by Jia Han
  Created Feb.13.2024
 */
#include <WiFiS3.h>
#include <cstring> // For strchr

#include "LedMatrixConfig.h";
#include "WiFiFunctions.h";

WiFiServer server(80);

// Global time variables
unsigned long currentMillis = millis(); // current time, maybe not neccessary

const unsigned long maxTimeout = 1000; // Maximum allowed timeout for client operation

// Global variables for interrupt handling
volatile bool interruptTriggered = false;
unsigned long lastInterruptTime = 0;    // Time of the last interrupt
unsigned long prevInterruptTime = 0;    // Time of the interrupt before the last one
const int interruptPin = 2; // interrupt pin, adjust according to setup

// Global variables for Analog write
unsigned int signalDuration = 0; // Duration of high level in milliseconds
unsigned int highLevel = 0;
unsigned int lowLevel  = 0;
unsigned int analogOutRes = 12; // Analog output resolution; 8 or 12-bit
int maxAnalogOut = 4096; // Maximum output value for 12-bit resolution

//============================================================
//============================================================

void setup() {
  //Initialize serial and wait for port to open:
  Serial.begin(9600);
  while (!Serial) {
    ; // wait for serial port to connect. Needed for native USB port only
  }

  matrix.begin(); // enable LED matrix
  LEDmouth();
  matrix.renderBitmap(frame, 8, 12);

  connectWiFi();

  server.begin();
  Serial.println("Server started");
  LEDleftEye();
  LEDrightEye();
  matrix.renderBitmap(frame, 8, 12);

  // Analog setup
  analogWriteResolution(analogOutRes); //change to 12-bit resolution
  analogWrite(A0, 0); // set output to zero, otherwise will float at some random voltage

  // Interrupt setup
  pinMode(interruptPin, INPUT_PULLUP); // Set the interrupt pin as input with pull-up
  attachInterrupt(digitalPinToInterrupt(interruptPin), onInterrupt, FALLING); // Attach interrupt
}

void loop() {
  if (interruptTriggered) {
    setOutputLevel();
    interruptTriggered = false; // Reset the flag after handling the interrupt
  } else {
    clientOperation();
  }
}

void onInterrupt() {
  prevInterruptTime = lastInterruptTime; // Store the previous interrupt time
  lastInterruptTime = millis(); // Update the last interrupt time
  interruptTriggered = true;

  // Temporarily disable the interrupt
  detachInterrupt(digitalPinToInterrupt(interruptPin));
}

void setOutputLevel() {
  analogWrite(A0, highLevel);
  LEDwink();
  matrix.renderBitmap(frame, 8, 12);
  delay(signalDuration);
  analogWrite(A0, lowLevel);
  LEDleftEye();
  LEDrightEye();
  matrix.renderBitmap(frame, 8, 12);

  // Re-enable the interrupt after handling to be ready for the next one
  attachInterrupt(digitalPinToInterrupt(interruptPin), onInterrupt, FALLING);
}

// Function to get the time difference between the previous two interrupts
unsigned long getTimeDifferenceBetweenInterrupts() {
  if (prevInterruptTime == 0) {
    // If there's no previous interrupt recorded, return 0
    return 0;
  } else {
    return lastInterruptTime - prevInterruptTime;
  }
}

bool clientOperation() {
  unsigned long maxSignalDuration = getTimeDifferenceBetweenInterrupts();
  unsigned long timeout = min(maxSignalDuration-signalDuration, maxTimeout); // upper time limit for client operation
  unsigned long initMillis = millis();

  // Check WiFi connection status and attempt to reconnect if necessary
  if (initMillis - millis() > timeout && !checkAndReconnectWiFi()){
    return false; // No WiFi connection, return early
  }

  WiFiClient client = server.available();
  if (!client) {
    return false; // No client is connected, return early
  }

  Serial.println("New client connected");
  while (client.connected() && millis() - initMillis < timeout) {
    
    if (client.available()) {
      char buf[101]; // Buffer to hold incoming data, increased by one for null terminator
      int len = client.read((uint8_t*)buf, sizeof(buf) - 1); // Read data into buffer
      if (len > 0) { // If data was read
        buf[len] = '\0'; // Null-terminate the string
        
        int tempHighLevel, tempLowLevel, tempSignalDuration;
        // Check for the correct message format and store values in temporary variables
        if (sscanf(buf, "%d,%d,%d", &tempHighLevel, &tempLowLevel, &tempSignalDuration) != 3) {
          client.println("Message format incorrect");
          Serial.print("Format incorrect, message is "); Serial.println(buf);
          client.stop();
          return false; // Message wrong, return early
        }

        // Validate the range of each variable individually
        bool rangeError = false;
        if (tempHighLevel < 0 || tempHighLevel > 4096) {
          client.println("HighLevel value out of range!");
          Serial.println("HighLevel value out of range!");
          rangeError = true;
        }
        if (tempLowLevel < 0 || tempLowLevel > 4096) {
          client.println("LowLevel value out of range!");
          Serial.println("LowLevel value out of range!");
          rangeError = true;
        }    
        if (tempSignalDuration < 0 || tempSignalDuration > maxSignalDuration) {
          client.println("SignalDuration value out of range!");
          Serial.println("SignalDuration value out of range!");
          rangeError = true;
        }

        if (rangeError) {
          client.stop();
          return false; // At least one value is out of range, return early
        }

        // If values are within range, update the global variables
        highLevel = tempHighLevel;
        lowLevel = tempLowLevel;
        signalDuration = tempSignalDuration;

        // Respond to the client
        client.println("Message received and processed");
        Serial.print("High Level = "); Serial.println(highLevel);
        Serial.print("Low Level = "); Serial.println(lowLevel);
        Serial.print("Signal Duration = "); Serial.println(signalDuration);

        client.stop(); // Close the connection
        Serial.println("Client disconnected");
        return true; // Client message received successfully
      }
    }
  }

  if (!client.connected()) {
    client.stop(); // Close the connection if not connected
    Serial.println("Client disconnected due to lack of activity");
  } else {
    // Handling the case where the operation times out
    client.println("Operation timed out");
    client.stop();
    Serial.println("Client disconnected due to timeout");
  }

  return false; // Return false if operation timed out or no data was received
}