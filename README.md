# Treadmill-Interface
Interface to treadmill encoder with USB-serial and analog outputs

Provides an interface to a treadmill that uses an incremental encoder to monitor speed and distance.
The speed and distance are calculated at each increment of the encoder. Time, speed, and distance are sent out a serial-USB port. The minimum time between readinsg can be set, as well as the time to wait to set zero speed.
The absolute value of speed is also sent out to an SMA connector as an analog signal. Direction is sent out another SMA connector as a 3.3V digital signal. 
The design utilizes a Teensy 4.0.
The encoder voltage provided is 5 volts.

PCB design done in Eagle
Firmware design in Arduino with Teensy extensions
Processor - Teensy 4.0


**Opportunity:** Free to make for Non-Profit Research by downloading the design here. See included hardware license.

Commercial licenses are also available, contact innovation@janelia.hhmi.org and reference this DOI.

For inquiries, please contact [innovation@janelia.hhmi.org](mailto:innovation@janelia.hhmi.org) and reference: Janelia 2017-055

To cite the designs, please cite this DOI: [https://doi.org/10.25378/janelia.33179270](https://doi.org/10.25378/janelia.33179270)

**Related to:** https://doi.org/10.25378/janelia.24691311, item 2017-049

**Other:** supersedes Flintbox ID: 2017-055

