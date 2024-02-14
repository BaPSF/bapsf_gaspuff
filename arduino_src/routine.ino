/*
  Code by Jia Han
  Created Feb.13.2024
 */
#include <WiFiS3.h>
#include "Arduino_LED_Matrix.h"
#include "arduino_secrets.h" 

ArduinoLEDMatrix matrix;
const uint32_t happy[] = {
    0x19819,
    0x80000001,
    0x81f8000
};
const uint32_t heart[] = {
    0x3184a444,
    0x44042081,
    0x100a0040
};
///////please enter your sensitive data in the Secret tab/arduino_secrets.h
char ssid[] = SECRET_SSID;        // your network SSID (name)
char pass[] = SECRET_PASS;    // your network password (use for WPA, or use as key for WEP)
int status = WL_IDLE_STATUS;     // the WiFi radio's status
IPAddress ip(128, 97, 13, 156);
WiFiClient client;

String serverIP = "128.97.13.180"; // Server IP address
int serverPort = 5000;             // Server port

//Only one DAC output available (Pin A0), so it's not defined here
const int interruptPin = 2; // Digital pin used for the interrupt

volatile bool interruptTriggered = false;
unsigned long interruptTime = 0;
unsigned long lastInterruptTime = 0; // To prevent debounce effect

volatile int outputValue = 0; // Variable to store the analog output value, volatile because it is changed in the loop and read in the ISR
volatile long signalDuration = 1000; // Duration to maintain the output signal level in milliseconds (e.g., 5000ms or 5 seconds)


void setup() {
  //Initialize serial and wait for port to open:
  Serial.begin(9600);
  while (!Serial) {
    ; // wait for serial port to connect. Needed for native USB port only
  }

  matrix.begin(); // enable LED matrix
  matrix.loadFrame(heart);

  // check for the WiFi module:
  if (WiFi.status() == WL_NO_MODULE) {
    Serial.println("Communication with WiFi module failed!");
    // don't continue
    while (true);
  }

  String fv = WiFi.firmwareVersion();
  if (fv < WIFI_FIRMWARE_LATEST_VERSION) {
    Serial.println("Please upgrade the firmware");
  }

  WiFi.config(ip);
  // attempt to connect to WiFi network:
  while (status != WL_CONNECTED) {
    Serial.print("Attempting to connect to WPA SSID: ");
    Serial.println(ssid);
    // Connect to WPA/WPA2 network:
    status = WiFi.begin(ssid, pass);

    // wait 10 seconds for connection:
    delay(10000);
  }

  // you're connected now, so print out the data:
  Serial.print("You're connected to the network");
  printCurrentNet();
  printWifiData();
  matrix.loadFrame(happy);

  // Analog setup
  analogWriteResolution(12); //change to 12-bit resolution
  analogWrite(A0, 0); // set output to zero, otherwise will float at some random voltage

  // Interrupt setup
  pinMode(interruptPin, INPUT_PULLUP); // Set the interrupt pin as input with pull-up

  // Attach an interrupt to the interrupt pin. The interrupt is triggered on the falling edge (HIGH to LOW transition)
  attachInterrupt(digitalPinToInterrupt(interruptPin), onInterrupt, FALLING);
  Serial.println("Attached interrupt pin");
  Serial.println(interruptTriggered);
}

void loop() {

  if (interruptTriggered) {
    setOutputLevel();
    interruptTriggered = false;
  } else {
    if (client...){}
    attachInterrupt(digitalPinToInterrupt(interruptPin), onInterrupt, FALLING);
  }

//  if (!interruptTriggered && client.connect(serverIP.c_str(), serverPort)) {
//      Serial.println("Client connected");
//      client.println("Connected");
//      client.stop();
//  }

}

void onInterrupt() {
  interruptTriggered = true;
  interruptTime = millis();
  // Temporarily disable the interrupt to prevent re-triggering during the signal duration
  detachInterrupt(digitalPinToInterrupt(interruptPin));
}

void setOutputLevel() {
      analogWrite(A0, outputValue);
      matrix.loadFrame(heart);
      
      delay(signalDuration);

      analogWrite(A0, 0);
      matrix.loadFrame(happy);
}

////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////
// WiFi functions
////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////

void printWifiData() {
  // print your board's IP address:
  IPAddress ip = WiFi.localIP();
  Serial.print("IP Address: ");
  
  Serial.println(ip);

  // print your MAC address:
  byte mac[6];
  WiFi.macAddress(mac);
  Serial.print("MAC address: ");
  printMacAddress(mac);
}

void printCurrentNet() {
  // print the SSID of the network you're attached to:
  Serial.print("SSID: ");
  Serial.println(WiFi.SSID());

  // print the MAC address of the router you're attached to:
  byte bssid[6];
  WiFi.BSSID(bssid);
  Serial.print("BSSID: ");
  printMacAddress(bssid);

  // print the received signal strength:
  long rssi = WiFi.RSSI();
  Serial.print("signal strength (RSSI):");
  Serial.println(rssi);

  // print the encryption type:
  byte encryption = WiFi.encryptionType();
  Serial.print("Encryption Type:");
  Serial.println(encryption, HEX);
  Serial.println();
}

void printMacAddress(byte mac[]) {
  for (int i = 0; i < 6; i++) {
    if (i > 0) {
      Serial.print(":");
    }
    if (mac[i] < 16) {
      Serial.print("0");
    }
    Serial.print(mac[i], HEX);
  }
  Serial.println();
}
