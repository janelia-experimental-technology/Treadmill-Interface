 // EncoderInterfaceT4
//
//  Read the encoder and translate to a distance and send over USB-Serial and PWM DAC
//  Teensy 4.0 with Teensy Extensions

//  Encoder A - pin 14
//  Encoder B - pin 15
//  Encoder VCC - Vin
//  Encoder ground - GND
//  PWM Analog Out - pin 3
//  Direction pin - pin 21
//
// Steve Sawtelle
// jET
// Janelia
// HHMI 
//

#define SHOW_MICROS    // if defined, print micros() with each output
#define SHOW_REVERSE   // if this is defined, show abs value of speed, else ignore reverse distance/speed
#define SHOW_ZERO      // if this is defined, show 0 speed if no movement in SPEED_TIMEOUT micros
//#define CYLINDER_8IN   // if this is defined, will compile for 8" cyclinder, else standard belt 

#define ZERO_OFFSET    // if this is defined, offset zero to 1/2 Max DAC to allow sending reverse speed through DAC
//                          if used must also define SHOW_REVERSE

#define UPDATE_USECS 20000  // don't print faster than this many micros
#define SPEED_TIMEOUT 100000  // if we don't move in this many micros assume we are stopped and show 0.0 speed 


#define VERSION "20260331"
// ===== VERSIONS ======

// 20260331 sws
// - implemement zero offset 

// 20240909 sws
// - from EncoderInterfaceT3, changes to use Teensy 4.0
//   pin changes
//   add in spped and dir outputs, speed is PWM analog

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

#define MAXSPEED    1000.0f  // maximum speed for dac out (mm/sec)
#define MAXDACVOLTS 2.5f    // DAC ouput voltage at maximum speed
#define MAXDACCNTS  4095.0f // maximum dac value - should be 2^(analogWriteResolution)

float maxDACval = MAXDACVOLTS * MAXDACCNTS / 3.3; // limit dac output to max allowed

#define speedPin 3    
#define encAPin 14 // 12
#define encBPin 15 // 11
#define dirPin 21    // 0 = forward,1 = reverse


#ifdef CYLINDER_8IN
// counts per rotation of cylindrical treadmill encoder wheel: 200 counts per rev, 7.90" per rev (was 7.95)
// so - 7.90 * 25.4 * pi / 200  = microns/cnt
// when using rising and falling edges of both channels, must divide by 800
// first version of cylinder used /200 and then /4 in distance per count, but the /4 should be here
#define MM_PER_COUNT 787990  // 3171909  // actually 1/10^6mm per count since we divide by usecs
#else
// counts per rotation of treadmill encoder wheel
// 200 counts per rev
// 1.03" per rev
// so - 1.03 * 25.4 * pi / 200 /1000 = microns/cnt
#define MM_PER_COUNT 410950  // actually 1/10^6mm per count since we divide by usecs
#endif

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

#define FW 1
#define BW -1

int dir = FW;

float lastDistance;
float zeroDistance;
volatile boolean movement = false;

// ------------------------------------------
// interrupt routine for ENCODER_A rising edge (and falling if cylinder)
// ---------------------------------------------
void encoderInt()
{   
   thisUsecs = micros();
  
  int ENCA = digitalReadFast(encAPin);  // always update output 
  int ENCB = digitalReadFast(encBPin); 
  // figure out the direction  
    
  if (ENCA == ENCB )  // if same, then backwards
  {   
    dir = BW;
    #ifdef SHOW_REVERSE 
        distance -= DIST_PER_COUNT;
    #endif
  }  
  else
  {
      dir = FW;
      distance += DIST_PER_COUNT;
  }  
   
  #ifndef SHOW_REVERSE   // if not showing reverse speed force reverse to 0
    if( dir == BW ) runSpeed = 0;      
  #endif  

   movement = true;  
     
}


// ------------------------------------------
// interrupt routine for ENCODER_B rising/falling edge - for 8" cylinder
// ---------------------------------------------
void encoderBInt()
{   
   thisUsecs = micros();
  
  int ENCA = digitalReadFast(encAPin);  // always update output 
  int ENCB = digitalReadFast(encBPin); 
  // figure out the direction  
    
  if (ENCA != ENCB )  // if same, then backwards
  {   
    dir = BW;
    #ifdef SHOW_REVERSE 
        distance -= DIST_PER_COUNT;
    #endif
  }  
  else
  {
      dir = FW;
      distance += DIST_PER_COUNT;
  }  
   
  #ifndef SHOW_REVERSE   // if not showing reverse speed force reverse to 0
    if( dir == BW ) runSpeed = 0;      
  #endif  

   movement = true;  
     
}

void setDACspeed(float speed)
{

  #ifdef ZERO_OFFSET  
    float dacval = ((speed/MAXSPEED * maxDACval) + maxDACval)/2; 
  #else  
    float dacval = abs(speed)/MAXSPEED * maxDACval; 
  #endif

    if( dacval < 0 ) dacval = 0;             
    if( dacval > maxDACval) dacval = maxDACval; 

    analogWrite(speedPin,(uint16_t) dacval);
       
    if( runSpeed < 0 ) 
       digitalWrite(dirPin, HIGH); // and note reverse dir 
    else
       digitalWrite(dirPin, LOW);  // else note forward
}
       

void setup()
{

  Serial.begin(192000);
  // while( !Serial);  // if no USB serial connection, this will hang the program
  
  pinMode(encAPin, INPUT_PULLUP); // sets the digital pin as input
  pinMode(encBPin, INPUT_PULLUP); // sets the digital pin as input
  pinMode(dirPin, OUTPUT);
  digitalWrite(dirPin, LOW);  //  assume forward
  
  analogWriteFrequency(speedPin, 36621.09);  // optimal freq for 12 bit PWM
  // https://www.pjrc.com/teensy/td_pulse.html
  analogWriteResolution(12);

  lastUsecs = micros();
  thisUsecs = lastUsecs;
  lastDistance = 0;
  zeroDistance = 0;
  #ifdef CYLINDER_8IN
    attachInterrupt(encAPin, encoderInt, CHANGE); // check encoder every A pin edge
    attachInterrupt(encBPin, encoderBInt, CHANGE); // check encoder every B pin edge
  #else
    attachInterrupt(encAPin, encoderInt, RISING); // check encoder every A pin rising edge
  #endif  

  setDACspeed(0);

}

boolean moved = false;

 void loop() 
{  

  if( Serial.available() )
  {
     int8_t charin = Serial.read();
     while(Serial.available()); // clear buffer
     if ( charin == '?' )
     {
       Serial.print("Treadmill V:");
       Serial.println(VERSION);
       #ifdef CYLINDER_8IN
          Serial.println("8in Cylindrical Treadmill");
       #endif   
       #ifdef SHOW_MICROS 
          Serial.print("micros,");
       #endif
       Serial.println("distance (mm),speed (mm/s)");     
     } 
     else if ( charin == 'c' )
     {
       float speed;
      #ifdef ZERO_OFFSET
        Serial.print("Speed output calibrate, ");
        Serial.print(-MAXSPEED);
        Serial.print(" to ");
        Serial.print(-MAXSPEED);
        Serial.println(" mm/sec");
        for( speed =-MAXSPEED; speed <= MAXSPEED; speed += MAXSPEED/5)
      #else  
        Serial.print("Speed output calibrate, 0 to ");
        Serial.print(MAXSPEED);
        Serial.println(" mm/sec");
        for( speed = 0; speed <= MAXSPEED; speed += MAXSPEED/5)
      #endif
        {
           setDACspeed(speed);
           Serial.println(speed);
           delay(5000);
        }  
        setDACspeed(0);   
     }
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
       runSpeed = (1e6 * (cumDistance - lastDistance) ) / (float)usecs;
       #ifdef SHOW_MICROS     
         Serial.print(newUsecs);   
         Serial.print(","); 
         
       #endif     
       Serial.print(cumDistance);
       Serial.print(",");
       Serial.println( runSpeed); 
            
       lastDistance = cumDistance;

       setDACspeed(runSpeed);
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
       
       lastUsecs = zeroUsecs;  // update to most recent time so next speed reading is right
     }   
  } 
  #endif 


} // end loop
