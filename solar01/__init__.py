"""solar01: EG4 FlexBoss21 + JK BMS controller.

One application, two processes:

* ``core``  - owns every serial port (inverter meter port, JK BMS, BMS-emulator
  battery port), enforces the write safety rules and has no network
  dependencies at all.  The inverter always has a battery to talk to as long as
  this process and the three USB adapters are alive.
* ``hub``   - history, the SDG&E TOU planner, the Home Assistant MQTT bridge
  and the local web UI.  It talks to ``core`` over a local socket and can be
  restarted, crash or lose the network without touching the BMS bridge.
"""

__version__ = '1.0.0'
