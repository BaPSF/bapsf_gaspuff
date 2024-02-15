/*
  Code by Jia Han
  Created Feb.13.2024
 */
#include <WiFiS3.h>
#include <cstring> // For strchr

#include "LedMatrixConfig.h";
#include "WiFiFunctions.h";

WiFiServer server(80);

// Global variables for interrupt handling
unsigned long currentMillis = millis();
volatile bool interruptTriggered = false;
unsigned long lastInterruptTime = 0; // Time of the last interrupt
unsigned long prevInterruptTime = 0; // Time of the interrupt before the last one
const int interruptPin = 2; // Example interrupt pin, adjust according to your setup

// Global variables for Analog write
unsigned int signalDuration = 1000; // Duration for signal in milliseconds
unsigned int highLevel = 0;
unsigned int lowLevel  = 0;
int outputValue = 4096; // Example output value for 12-bit resolution

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
  analogWriteResolution(12); //change to 12-bit resolution
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
  analogWrite(A0, outputValue);
  LEDwink();
  matrix.renderBitmap(frame, 8, 12);
  delay(500);
  analogWrite(A0, 0);
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

void clientOperation() {

  unsigned long timeout = (getTimeDifferenceBetweenInterrupts() - signalDuration);

  WiFiClient client = server.available();
  if (client) {                             // if you get a client,
    Serial.println("new client");           // print a message out the serial port
    unsigned long initMillis = millis();
    
    while (client.connected()) { // loop while the client's connected
      currentMillis = millis();  // update current time
      char buf[100];             // make a buffer to hold incoming data from the client
      if (client.available()) { // if there's bytes to read from the client,
        // client.read takes two inputs--unsigned char* buffer and its size. Output size of the message received from client
        int c = client.read((unsigned char*)buf, 99);
        client.println("message received");
        buf[c] = 0;          // message needs to be terminated with a zero
        Serial.println(buf); // print message to serial window
        if (!strchr(buf, ',')) {
          client.println("Message wrong!");
          break;
        }

        sscanf(buf, "%d,%d,%d", highLevel, lowLevel, signalDuration);
        Serial.print("High Level = ");
        Serial.println(highLevel);
        Serial.print("Low Level = ");
        Serial.println(lowLevel);
        Serial.print("On time = ");
        Serial.println(signalDuration);

        break;

      } else if (currentMillis - initMillis >= timeout) {
          client.println("TIMEOUT");
          break;
        }
    }
    // close the connection:
    client.stop();
    Serial.println("client disonnected");
  }
  
}
