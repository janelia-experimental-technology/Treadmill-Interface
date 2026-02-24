 // Treadmill-LCD
//
//  Read the encoder and translate to speed and distance
//  Send over USB-Serial and show on display 
//  Start and Stop buttons to zero distance and stop recording distance and speed
//  Uses Adafruit ESP32-S2 Reverse TFT Feather
//
//  Start Button - pin 1
//  Stop Button - pin 2
//  Encoder A  - pin 13
//  Encoder B - pin 14
//  Encoder VCC - Vin
//  Encoder ground - GND
//
// Steve Sawtelle
// jET
// Janelia
// HHMI 
//

#include <Cmd.h>

//#define SHOW_MICROS    // if defined, print micros() with each output
#define SHOW_REVERSE   // if this is defined, show abs value of speed, else ignore reverse distance/speed
#define SHOW_ZERO      // if this is defined, show 0 speed if no movement in SPEED_TIMEOUT micros
#define CYLINDER_6IN   // if this is defined, will compile for 6.06" cylinder, else standard belt 
#define REVERSE_DIR    // if defined will swap direction sense

//#define ZERO_OFFSET    // if this is defined, offset zero to 1/2 Max DAC to allow sending reverse speed through DAC
//                          if used must also define SHOW_REVERSE

#define UPDATE_USECS 20000  // don't print faster than this many micros
#define SPEED_TIMEOUT 100000  // if we don't move in this many micros assume we are stopped and show 0.0 speed 

#define MINON 1
#define MAXON 100


#define VERSION "20260114R"

// ===== VERSIONS ======

// 2026011 sws
// - 9.972" diameter rat wheel 600 count encoder

// 20250729 sws
// - if speed is zero, was showing speed calculated from last distance just once
//    now, if zero, keep updating the average 

// 20250402 sws
//  - version for TRD-SR1000 with 170mm diameter wheel

// 20240410 sws
// - using 2048 cpr encoder, so only use 1 edge of A 

// 20240409 sws
// - from PACHITARIU code, change cylinder size
// -using 'D' rev board - Ain is 13 not A0, Bin is 12 not A1

// 20231107 sws
// - add pull ups and downs on button pins

// 20231106 sws
// - reverse forward and backward
// - show speed as average since the start of the run (distance over elapsed time) 
// - show version number after reset for 2 secs

// 20231018 sws
// - from T3 Encoder Interface 
// - LCD display, work on ESP32-S3 Reverse TFT Feather

// 20230621 sws
// - add ability to compile for cylinder treadmill

// 20230523 sws
// - remove debug output in loop
// - add '?' query for version

// 20220419 sws
// - update lastUsecs during zero speed time so that when the treadmill starts again the speed 
//   is based on the end of last zero time out

// 20221212 sws from suggestions by york at Labmaker
// - strict even intervals causes poor low speed granularity
// - so, return to incremental updates, but limit to no faster than UPDATE_USECS 
// - make define to allow showing zero if no movement in SPEED_TIMEOUT micros
// - add define to allow printing of micros() value

// 20221017 sws
// - add define to print distance and speed at even intervals. 
//     To use allow #define TIMED_UPDATE and UPDATE_RATE sets the rate in updates per second

// 20220720 sws
// - also need to use pin 3 not 0 in LC as 0 doesn't have interrupt capability
// - to keep backward compatibility, set the unused pin as input so they can be tied
//    together on PCB

// 20220711 sws
// - provide alternate compile for Teensy LC, A12 not A14 for DAC

// 20211109 sws
// - was printing absolute run speed, now it shows as negative if reverse

// 20211021 sws
// - add define for offsetting DAC to show reverse speed 

// 20180514 sws
// - DIR pin was set to 12 but hardware has it on 2
// - also had index pin as 2 should be 14

// 20180328 sws
// - remove wait for serial since it will not move past it if no serial port is defined

// 20180319 sws
// - add define to allow showing reverse direction

#include <Adafruit_GFX.h>    // Core graphics library
#include <Adafruit_ST7789.h> // Hardware-specific library for ST7789
#include <SPI.h>
#include <elapsedMillis.h>

Adafruit_ST7789 tft = Adafruit_ST7789(TFT_CS, TFT_DC, TFT_RST);

#define MAXSPEED    1000.0f  // maximum speed for dac out (mm/sec)
#define MAXDACVOLTS 2.5f    // DAC ouput voltage at maximum speed
#define MAXDACCNTS  4095.0f // maximum dac value


#define encAPin 13
#define encBPin 12 
#define startButton 1
#define stopButton 2
#define sol1Pin 16

#define MM_PER_COUNT 1326217   // TDR600, 9.972" dia wheel, 600 count encoder
//#define MM_PER_COUNT 534000  // for TDR-SR1000 1000 counts, 170mm diameter// 787990  // 3171909  // actually 1/10^6mm per count since we divide by usecs

#define DIST_PER_COUNT ((float)MM_PER_COUNT/1000000.0)   //(float)0.41095
 
#define SPEED_TIMEOUT 100000  // if we don't move in this many microseconds assume we are stopped

static float runSpeed = 0;
static float lastSpeed = 0;
volatile uint32_t lastUsecs;
volatile uint32_t thisUsecs;
volatile uint32_t encoderUsecs;
volatile float distance = 0;
volatile float deltaDistance = 0;
volatile int8_t encoderCounts = 0;

#define MAXIDX 200
volatile float distVals[MAXIDX];
volatile uint32_t microVals[MAXIDX];
volatile uint8_t idx = 0;

#define ST77XX_GRAY 0x8410

#define FW 1
#define BW -1

int dir = FW;

float lastDistance;
float zeroDistance;
volatile boolean movement = false;
uint32_t startMillis;

// ------------------------------------------
// interrupt routine for ENCODER_A rising edge (and falling if cylinder)
// ---------------------------------------------
void encoderInt()
{   
   thisUsecs = micros();
  
  int ENCA = digitalRead(encAPin);  // always update output 
  int ENCB = digitalRead(encBPin); 
  // figure out the direction  

#ifdef REVERSE_DIR    
  if (ENCA != ENCB )  // if not same, then backwards
#else
  if (ENCA == ENCB )  // if same, then backwards
#endif 

  
  {   
//    Serial.print('B');
    dir = BW;
    #ifdef SHOW_REVERSE 
        distance -= DIST_PER_COUNT;
    #endif
  }  
  else
  {
//    Serial.print('F');
      dir = FW;
      distance += DIST_PER_COUNT;
  }  
   
  #ifndef SHOW_REVERSE   // if not showing reverse speed force reverse to 0
    if( dir == BW ) runSpeed = 0;      
  #endif  

   movement = true;

//   if( idx < MAXIDX)
//   {
//      distVals[idx] = distance;
//      microVals[idx] = thisUsecs;
//      idx++;
//   }   
     
}


// ------------------------------------------
// interrupt routine for ENCODER_B rising/falling edge
// ---------------------------------------------
void encoderBInt()
{   
   thisUsecs = micros();
  
  int ENCA = digitalRead(encAPin);  // always update output 
  int ENCB = digitalRead(encBPin); 
  // figure out the direction  
  
#ifdef REVERSE_DIR    
  if (ENCA == ENCB )  // if not, then backwards
#else
  if (ENCA != ENCB )  // if not same, then backwards
#endif    

  {   
//    Serial.print('B');
    dir = BW;
    #ifdef SHOW_REVERSE 
        distance -= DIST_PER_COUNT;
    #endif
  }  
  else
  {
//    Serial.print('F');
      dir = FW;
      distance += DIST_PER_COUNT;
  }  
   
  #ifndef SHOW_REVERSE   // if not showing reverse speed force reverse to 0
    if( dir == BW ) runSpeed = 0;      
  #endif  

   movement = true;  
     
}

void testdrawtext(char *text, uint16_t color) {
  tft.setCursor(0, 0);
  tft.setTextColor(color);
  tft.setTextWrap(true);
  tft.print(text);
}

void showDistanceAndSpeed(float distance, float speed)
{
  tft.fillRect(100, 50, 139, 21, ST77XX_BLACK);
  tft.fillRect(100, 110, 139, 21, ST77XX_BLACK); 
  tft.setTextColor(ST77XX_YELLOW);
  tft.setTextSize(3);
  tft.setCursor(100, 50);
  tft.print(distance, 0);
  tft.setCursor(100, 110);
  float aveSpeed = 1000.0 * distance  / (millis() - startMillis);
  tft.print(aveSpeed, 1); 
}

elapsedMillis showData;


// ======================
// === H E L P    C M D  ===
// ======================

void helpCmd(int arg_cnt, char **args)
{
   Serial.print("Treadmill-LCD V:");
   Serial.println(VERSION);
#ifdef CYLINDER_6IN
   Serial.println("6.06in Cylindrical Treadmill 2048 cpr");
#endif      

}  

// ================================
// === S O L E N O I D   C M D  ===
// ================================

void solCmd(int arg_cnt, char **args)
{
  if ( arg_cnt > 1)
  {  
     uint8_t onTime =  cmdStr2Num(args[1], 10);
//     Serial.println(onTime);
     if( (onTime >= MINON) && (onTime <= MAXON ) )
     {
        digitalWrite( sol1Pin, HIGH);
        delay( onTime);
        digitalWrite( sol1Pin, LOW);
     }   
  }  
}  


void setup()
{

  Serial.begin(192000);
  // while( !Serial);  // if no USB serial connection, this will hang the program
  
  pinMode(encAPin, INPUT_PULLUP); // sets the digital pin as input
  pinMode(encBPin, INPUT_PULLUP); // sets the digital pin as input
  pinMode(0, INPUT_PULLUP);       // buttons need pull ups and downs
  pinMode(1, INPUT_PULLDOWN);
  pinMode(2, INPUT_PULLDOWN);
  
  pinMode(sol1Pin, OUTPUT);
  digitalWrite(sol1Pin, LOW); 
  
//while(1)
//{
//  digitalWrite(sol1Pin, HIGH);
//  delay(100);
//  digitalWrite(sol1Pin, LOW);
//  delay(100); 
//}

  // turn on backlite
  pinMode(TFT_BACKLITE, OUTPUT);
  digitalWrite(TFT_BACKLITE, HIGH);

  // turn on the TFT / I2C power supply
  pinMode(TFT_I2C_POWER, OUTPUT);
  digitalWrite(TFT_I2C_POWER, HIGH);
  delay(10);

  // initialize TFT
  tft.init(135, 240); // Init ST7789 240x135
  tft.setRotation(3);
  tft.fillScreen(ST77XX_BLACK);
  
  tft.setTextColor(ST77XX_WHITE);
  tft.setTextWrap(false);

  tft.setTextSize(2);
  tft.setCursor(0,0);
  tft.print("V: ");
  tft.print(VERSION);
  tft.setCursor(0,32);
  tft.print("9.972\" Cylinder");
  tft.setCursor(0,64);
  tft.print("600 cpr");
  delay(2000);

  tft.fillScreen(ST77XX_BLACK);
  
  tft.setTextSize(2);
  tft.setCursor(89, 32);
  tft.print("Distance (mm)");
  tft.setCursor(92, 90);
  tft.print("Speed (mm/s)");
  
  tft.setTextSize(3);
  tft.setCursor(40, 0);
  tft.print("Treadmill");
  tft.setCursor(0, 55);
  tft.print("GO"); //START");
  tft.setCursor(0, 114);
  tft.setTextColor(0x8410); //ST77XX_MAGENTA);
  tft.print("STOP");

  showDistanceAndSpeed(0.0, 0.0);

  
  cmdInit(&Serial);
  cmdAdd("???", helpCmd);  
  cmdAdd("SOL", solCmd);  
  
//  tft.setTextColor(ST77XX_YELLOW);
//  tft.setTextSize(3);
//  tft.setCursor(120, 50);
//  tft.print("1113");
//  tft.setCursor(120, 110);
//  tft.print("21.3"); 
  
  lastUsecs = micros();
  thisUsecs = lastUsecs;
  lastDistance = 0;
  zeroDistance = 0;
  #ifdef BOTH-EDGES-AB
    attachInterrupt(encAPin, encoderInt, CHANGE); // check encoder every A pin edge
    attachInterrupt(encBPin, encoderBInt, CHANGE); // check encoder every B pin edge
  #else
    attachInterrupt(encAPin, encoderInt, RISING); // check encoder every A pin rising edge
  #endif  
}

boolean moved = false;
boolean started = false;

char LCDstr[64];

 void loop() 
{  

    cmdPoll();

  if( !started && digitalRead(startButton) )
  {
     started = true;
     tft.fillRect(0, 55, 75, 179, ST77XX_BLACK);
     tft.setCursor(0, 55);
     tft.setTextColor(ST77XX_GRAY);     
     tft.print("GO"); //START");
     tft.setCursor(0, 114);
     tft.setTextColor(ST77XX_WHITE);
     tft.print("STOP");
     showDistanceAndSpeed(0.0, 0.0);   
     
     noInterrupts();
     distance = 0;
     movement = false;
     interrupts();       
     startMillis = millis(); 
  }

  if( started && digitalRead(stopButton) )
  {
     started = false;
     tft.fillRect(0, 55, 75, 179, ST77XX_BLACK);     
     tft.setCursor(0, 55);
     tft.setTextColor(ST77XX_WHITE);     
     tft.print("GO"); //START");
     tft.setCursor(0, 114);
     tft.setTextColor(ST77XX_GRAY);
     tft.print("STOP");   
  }


  
  noInterrupts();
  uint32_t newUsecs = thisUsecs;
  float cumDistance = distance;
  moved = movement;
  movement = false;
  interrupts();
  
  if( (moved) && ((newUsecs - lastUsecs) > UPDATE_USECS ) ) // are we at least past max update rate
  {
  #ifndef SHOW_REVERSE 
     if( (cumDistance - lastDistance) > 0 )
  #endif     
     { 
       int32_t usecs = newUsecs - lastUsecs;        
       lastUsecs = newUsecs;  
       #ifdef SHOW_MICROS     
         Serial.print(newUsecs);   
         Serial.print(",");         
       #endif     
       Serial.print(cumDistance);
       Serial.print(",");
       float speed =  1e6 * (cumDistance - lastDistance) / (float)usecs;
       Serial.println( speed); 
            
       lastDistance = cumDistance;

       if( started && (showData > 100) )
       {
          showData = 0;
          showDistanceAndSpeed(cumDistance, speed);
       }
     }
     
  }
  #ifdef SHOW_ZERO
  else 
  {
     uint32_t zeroUsecs = micros();
     if( (zeroUsecs - lastUsecs) > SPEED_TIMEOUT )
     {
       if( lastDistance != zeroDistance )
       {
         #ifdef SHOW_MICROS        
           Serial.print(zeroUsecs);  
           Serial.print(",");           
         #endif         
         Serial.print(lastDistance);
         Serial.println(",0.0");  
           
         zeroDistance = lastDistance;
       }  
       // take show data out of distance test so we continue to update
       //   the average speed when stopped
       if( started && (showData > 100) )
       {
          showData = 0;
          showDistanceAndSpeed(cumDistance, 0.0);  
       }
         
       lastUsecs = zeroUsecs;  // update to most recent time so next speed reading is right
     }   // 0 speed timeout
  } // endif check on movement
  #endif 


} // end loop
