#include <ESP32Servo.h>
#include <ctype.h>

Servo garbageServo;
Servo recyclingServo;
Servo compostServo;

// Servo pins.
const int GARBAGE_SERVO_PIN = 22;
const int RECYCLING_SERVO_PIN = 12;
const int COMPOST_SERVO_PIN = 14;

// Servo angles.
const int GARBAGE_CLOSED = 0;
const int GARBAGE_OPEN = 90;

const int RECYCLING_CLOSED = 0;
const int RECYCLING_OPEN = 90;

const int COMPOST_CLOSED = 60;   // reversed servo
const int COMPOST_OPEN = 170;

// Ultrasonic sensors.
const int GARBAGE_TRIG_PIN = 25;
const int GARBAGE_ECHO_PIN = 34;

const int RECYCLING_TRIG_PIN = 26;
const int RECYCLING_ECHO_PIN = 27;

const int COMPOST_TRIG_PIN = 5;
const int COMPOST_ECHO_PIN = 18;

// Detection range.
const float MIN_DISTANCE_CM = 2.0;
const float MAX_DISTANCE_CM = 27.0;

// Timing.
const unsigned long SENSOR_DELAY_MS = 10UL;
const unsigned long US_TIMEOUT_MS = 12000UL;
const unsigned long SERVO_SETTLE_MS = 350UL;
const unsigned long SERVO_BOOT_SILENCE_MS = 3000UL;

const int REQUIRED_NEAR_READINGS = 2;
const bool DEBUG_DISTANCE = false;

enum Route {
  ROUTE_NONE,
  ROUTE_GARBAGE,
  ROUTE_RECYCLING,
  ROUTE_COMPOST
};

Route activeRoute = ROUTE_NONE;
unsigned long routeStartedAt = 0;
unsigned long lastSensorReadingAt = 0;
int nearReadingCount = 0;

void attachGarbageServo() {
  if (!garbageServo.attached()) {
    garbageServo.attach(GARBAGE_SERVO_PIN, 500, 2400);
  }
}

void attachRecyclingServo() {
  if (!recyclingServo.attached()) {
    recyclingServo.attach(RECYCLING_SERVO_PIN, 500, 2400);
  }
}

void attachCompostServo() {
  if (!compostServo.attached()) {
    compostServo.attach(COMPOST_SERVO_PIN, 500, 2400);
  }
}

void detachAllServos() {
  garbageServo.detach();
  recyclingServo.detach();
  compostServo.detach();
}

void closeGarbage() {
  attachGarbageServo();
  garbageServo.write(GARBAGE_CLOSED);
  delay(SERVO_SETTLE_MS);
  garbageServo.detach();
}

void openGarbage() {
  attachGarbageServo();
  garbageServo.write(GARBAGE_OPEN);
  delay(SERVO_SETTLE_MS);
}

void closeRecycling() {
  attachRecyclingServo();
  recyclingServo.write(RECYCLING_CLOSED);
  delay(SERVO_SETTLE_MS);
  recyclingServo.detach();
}

void openRecycling() {
  attachRecyclingServo();
  recyclingServo.write(RECYCLING_OPEN);
  delay(SERVO_SETTLE_MS);
}

void closeCompost() {
  attachCompostServo();
  compostServo.write(COMPOST_CLOSED);
  delay(SERVO_SETTLE_MS);
  compostServo.detach();
}

void openCompost() {
  attachCompostServo();
  compostServo.write(COMPOST_OPEN);
  delay(SERVO_SETTLE_MS);
}

void closeAll() {
  attachGarbageServo();
  attachRecyclingServo();
  attachCompostServo();

  garbageServo.write(GARBAGE_CLOSED);
  recyclingServo.write(RECYCLING_CLOSED);
  compostServo.write(COMPOST_CLOSED);

  delay(SERVO_SETTLE_MS);
  detachAllServos();

  activeRoute = ROUTE_NONE;
  nearReadingCount = 0;
}

float readDistanceCM(int trigPin, int echoPin) {
  digitalWrite(trigPin, LOW);
  delayMicroseconds(2);
  digitalWrite(trigPin, HIGH);
  delayMicroseconds(10);
  digitalWrite(trigPin, LOW);

  unsigned long duration = pulseIn(echoPin, HIGH, 30000);
  if (duration == 0) {
    return -1.0;
  }

  return duration * 0.0343 / 2.0;
}

bool objectDetected(float distanceCm) {
  return distanceCm >= MIN_DISTANCE_CM && distanceCm <= MAX_DISTANCE_CM;
}

void resetRouteState() {
  activeRoute = ROUTE_NONE;
  routeStartedAt = 0;
  lastSensorReadingAt = 0;
  nearReadingCount = 0;
}

void finishGarbageRoute(float distanceCm) {
  closeGarbage();
  resetRouteState();
  Serial.print("OBJECT,GARBAGE,");
  Serial.println(distanceCm, 1);
}

void finishRecyclingRoute(float distanceCm) {
  closeRecycling();
  resetRouteState();
  Serial.print("OBJECT,RECYCLING,");
  Serial.println(distanceCm, 1);
}

void finishCompostRoute(float distanceCm) {
  closeCompost();
  resetRouteState();
  Serial.print("OBJECT,COMPOST,");
  Serial.println(distanceCm, 1);
}

void startGarbageRoute() {
  if (activeRoute != ROUTE_NONE) {
    Serial.println("BUSY,ROUTE_ACTIVE");
    return;
  }

  openGarbage();
  activeRoute = ROUTE_GARBAGE;
  routeStartedAt = millis();
  lastSensorReadingAt = millis();
  nearReadingCount = 0;
  Serial.println("OPEN,GARBAGE");
}

void startRecyclingRoute() {
  if (activeRoute != ROUTE_NONE) {
    Serial.println("BUSY,ROUTE_ACTIVE");
    return;
  }

  openRecycling();
  activeRoute = ROUTE_RECYCLING;
  routeStartedAt = millis();
  lastSensorReadingAt = millis();
  nearReadingCount = 0;
  Serial.println("OPEN,RECYCLING");
}

void startCompostRoute() {
  if (activeRoute != ROUTE_NONE) {
    Serial.println("BUSY,ROUTE_ACTIVE");
    return;
  }

  openCompost();
  activeRoute = ROUTE_COMPOST;
  routeStartedAt = millis();
  lastSensorReadingAt = millis();
  nearReadingCount = 0;
  Serial.println("OPEN,COMPOST");
}

void updateSensorRoute(int trigPin, int echoPin, const char *destination, void (*finishRoute)(float)) {
  if (millis() - routeStartedAt >= US_TIMEOUT_MS) {
    if (destination[0] == 'G') closeGarbage();
    else if (destination[0] == 'R') closeRecycling();
    else closeCompost();

    resetRouteState();
    Serial.print("TIMEOUT,");
    Serial.println(destination);
    return;
  }

  if (millis() - lastSensorReadingAt < SENSOR_DELAY_MS) {
    return;
  }

  lastSensorReadingAt = millis();
  float distanceCm = readDistanceCM(trigPin, echoPin);

  if (DEBUG_DISTANCE && distanceCm > 0) {
    Serial.print("DISTANCE,");
    Serial.print(destination);
    Serial.print(",");
    Serial.println(distanceCm, 1);
  }

  if (objectDetected(distanceCm)) {
    nearReadingCount++;
    if (nearReadingCount >= REQUIRED_NEAR_READINGS) {
      finishRoute(distanceCm);
    }
  } else {
    nearReadingCount = 0;
  }
}

void handleCommand(char command) {
  command = toupper(command);

  if (command == 'G' || command == '1') {
    startGarbageRoute();
    return;
  }

  if (command == 'R' || command == '2') {
    startRecyclingRoute();
    return;
  }

  if (command == 'C' || command == '3') {
    startCompostRoute();
    return;
  }

  if (command == 'O') {
    if (activeRoute != ROUTE_NONE) {
      Serial.println("BUSY,ROUTE_ACTIVE");
      return;
    }

    attachGarbageServo();
    attachRecyclingServo();
    attachCompostServo();

  garbageServo.write(GARBAGE_OPEN);
  recyclingServo.write(RECYCLING_OPEN);
  compostServo.write(COMPOST_OPEN);

  delay(SERVO_SETTLE_MS);

  Serial.println("OPEN,ALL");
  return;
}

  if (command == '0' || command == 'X') {
    closeAll();
    Serial.println("CLOSED,ALL");
    return;
  }

  if (command == 'E') {
    Serial.println("E-WASTE,NO-SERVO");
    return;
  }

  if (command == 'U') {
    Serial.println("UNKNOWN,NO-ACTION");
    return;
  }
}

void setup() {
  Serial.begin(9600);

  pinMode(GARBAGE_TRIG_PIN, OUTPUT);
  pinMode(GARBAGE_ECHO_PIN, INPUT);
  pinMode(RECYCLING_TRIG_PIN, OUTPUT);
  pinMode(RECYCLING_ECHO_PIN, INPUT);
  pinMode(COMPOST_TRIG_PIN, OUTPUT);
  pinMode(COMPOST_ECHO_PIN, INPUT);

  digitalWrite(GARBAGE_TRIG_PIN, LOW);
  digitalWrite(RECYCLING_TRIG_PIN, LOW);
  digitalWrite(COMPOST_TRIG_PIN, LOW);

  detachAllServos();
  delay(SERVO_BOOT_SILENCE_MS);

  garbageServo.setPeriodHertz(50);
  recyclingServo.setPeriodHertz(50);
  compostServo.setPeriodHertz(50);

  closeAll();

  Serial.println("READY");
  Serial.println("G=garbage R=recycling C=compost O=open all X=close all");
}

void loop() {
  while (Serial.available() > 0) {
    char command = Serial.read();
    if (command != '\n' && command != '\r' && command != ' ') {
      handleCommand(command);
    }
  }

  if (activeRoute == ROUTE_GARBAGE) {
    updateSensorRoute(GARBAGE_TRIG_PIN, GARBAGE_ECHO_PIN, "GARBAGE", finishGarbageRoute);
    return;
  }

  if (activeRoute == ROUTE_RECYCLING) {
    updateSensorRoute(RECYCLING_TRIG_PIN, RECYCLING_ECHO_PIN, "RECYCLING", finishRecyclingRoute);
    return;
  }

  if (activeRoute == ROUTE_COMPOST) {
    updateSensorRoute(COMPOST_TRIG_PIN, COMPOST_ECHO_PIN, "COMPOST", finishCompostRoute);
    return;
  }
}
