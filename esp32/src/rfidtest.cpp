// #include <SPI.h>
// #include <MFRC522.h>

// #define RST_PIN  4
// #define SS_PIN   5

// MFRC522 rfid(SS_PIN, RST_PIN);

// // Store previously read cards to avoid duplicates
// String lastUID = "";
// unsigned long lastReadTime = 0;

// void setup() {
//   Serial.begin(115200);
//   delay(1000);
  
//   Serial.println("\n========================================");
//   Serial.println("RFID Card Reader - Multiple Cards");
//   Serial.println("========================================\n");
  
//   SPI.begin();
//   rfid.PCD_Init();
  
//   Serial.println("[OK] RFID initialized");
//   Serial.println("\n[INFO] Place any 13.56MHz RFID card near the reader...");
//   Serial.println("      Compatible: MIFARE Classic, Ultralight, DESFire\n");
// }

// void loop() {
//   // Check for new cards (non-blocking)
//   if (!rfid.PICC_IsNewCardPresent()) {
//     delay(50);
//     return;
//   }
  
//   // Try to read the card
//   if (!rfid.PICC_ReadCardSerial()) {
//     Serial.println("[WARN] Card detected but couldn't read UID");
//     delay(500);
//     return;
//   }
  
//   // Get the UID
//   String uid = "";
//   for (byte i = 0; i < rfid.uid.size; i++) {
//     if (rfid.uid.uidByte[i] < 0x10) {
//       uid += "0";
//     }
//     uid += String(rfid.uid.uidByte[i], HEX);
//     if (i < rfid.uid.size - 1) uid += " ";
//   }
//   uid.toUpperCase();
  
//   // Get current time
//   unsigned long now = millis();
  
//   // Check if this is a new card (not same as last one within 2 seconds)
//   if (uid != lastUID || (now - lastReadTime) > 2000) {
//     Serial.println("\n========================================");
//     Serial.println("[RFID] Card Detected!");
//     Serial.print("UID: ");
//     Serial.println(uid);
    
//     // Get card type
//     MFRC522::PICC_Type piccType = rfid.PICC_GetType(rfid.uid.sak);
//     Serial.print("Card Type: ");
//     Serial.println(rfid.PICC_GetTypeName(piccType));
    
//     // Get card size (for MIFARE cards)
//     if (piccType == MFRC522::PICC_TYPE_MIFARE_1K ||
//         piccType == MFRC522::PICC_TYPE_MIFARE_4K ||
//         piccType == MFRC522::PICC_TYPE_MIFARE_UL) {
//       Serial.print("Card Size: ");
//       if (piccType == MFRC522::PICC_TYPE_MIFARE_1K) Serial.println("1KB");
//       else if (piccType == MFRC522::PICC_TYPE_MIFARE_4K) Serial.println("4KB");
//       else Serial.println("Ultralight");
//     }
    
//     Serial.println("========================================\n");
    
//     lastUID = uid;
//     lastReadTime = now;
//   } else {
//     // Same card, just ignore
//     Serial.print(".");
//   }
  
//   // Halt the card
//   rfid.PICC_HaltA();
  
//   // Stop encryption
//   rfid.PCD_StopCrypto1();
  
//   delay(100);
// }