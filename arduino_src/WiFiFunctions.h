// Include guard to prevent multiple inclusions
#ifndef WIFI_FUNCTIONS_H
#define WIFI_FUNCTIONS_H

#include <WiFiS3.h> // Include necessary libraries

#include "LedMatrixConfig.h"
#include "arduino_secrets.h"


///////please enter your sensitive data in the Secret tab/arduino_secrets.h
char ssid[] = SECRET_SSID;        // your network SSID (name)
char pass[] = SECRET_PASS;    // your network password (use for WPA, or use as key for WEP)
int status = WL_IDLE_STATUS;     // the WiFi radio's status
IPAddress ip(SECRET_IP1, SECRET_IP2, SECRET_IP3, SECRET_IP4);

const unsigned long WiFiTimeout = 30000; // Timeout in milliseconds (e.g., 30000ms = 30 seconds)
const unsigned long retryInterval = 5000; // Time between retries in milliseconds (e.g., 5000ms = 5 seconds)
bool isConnected = false; // Flag to check connection status
unsigned long lastReconnectMillis = 0;  // Time for last attempt to establish a WiFi connection


//============================================================
//============================================================


void printMacAddress(byte mac[]) {
  for (int i = 0; i < 6; i++) {
    Serial.print(mac[i] < 16 ? "0" : "");
    Serial.print(mac[i], HEX);
    if (i < 5) Serial.print(":");
  }
  Serial.println();
}

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

void connectWiFi() {
  if (WiFi.status() == WL_NO_MODULE) {
    Serial.println("Communication with WiFi module failed!");
    while (true); // Don't continue if WiFi module is not present
  }

  String fv = WiFi.firmwareVersion();
  if (fv < WIFI_FIRMWARE_LATEST_VERSION) {
    Serial.println("Please upgrade the firmware");
  }

  WiFi.config(ip);
  unsigned long startTime = millis();

  while (!isConnected && millis() - startTime < WiFiTimeout) {
    if (WiFi.status() != WL_CONNECTED) {
      Serial.print("Attempting to connect to WPA SSID: ");
      Serial.println(ssid);

      WiFi.begin(ssid, pass); // Attempt to connect
      unsigned long retryStartTime = millis(); // Start time for this retry

      // Wait for connection or retry interval timeout
      while ((WiFi.status() != WL_CONNECTED) && (millis() - retryStartTime < retryInterval)) {
        delay(100); // Short delay to allow other tasks to run
      }

      if (WiFi.status() == WL_CONNECTED) {
        isConnected = true;
      }
    } else {
      isConnected = true; // Already connected
    }
  }

  if (isConnected) {
    Serial.print("You're connected to the network");
    printCurrentNet();
    printWifiData();
  } else {
    Serial.println("Failed to connect to the WiFi network within the timeout period.");
  }

}

bool checkAndReconnectWiFi() { // Check WiFi connection and reconnect if needed
  isConnected = WiFi.status() == WL_CONNECTED; // Update connection status

  if (!isConnected && millis() - lastReconnectMillis >= retryInterval) {
    Serial.println("Disconnected from WiFi. Attempting to reconnect...");
    WiFi.begin(ssid, pass); // Attempt to reconnect
    lastReconnectMillis = millis(); // Update the last reconnect time
  }

  return isConnected; // Return the current connection status
}

#endif
