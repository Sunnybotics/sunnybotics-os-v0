# =============================================================================
# MachineState -- SunnyboticsOS Common Machine Interface (CMI)
# =============================================================================
# The complete observable state of ONE machine, whatever that machine is.
# Published at 1 Hz on /machines/<machine_id>/state.
#
# The whole point of this message is that a cleaning rover, an inspection rover
# and a machine nobody has built yet all describe themselves with these same
# fields. Onboarding a new machine type must never require editing this file.
# =============================================================================

# --- Envelope ----------------------------------------------------------------
# Bumped when this message changes shape, so the OS can talk to a fleet that is
# only partly upgraded instead of needing a flag-day across every machine.
string interface_version

# Monotonic per-publisher counter. A gap means messages were dropped, which is
# information you cannot recover from the timestamp alone.
uint32 seq

# --- Identity (static for the lifetime of the machine) -----------------------
string machine_id                 # unique across the fleet, e.g. "rover_01"
string machine_type               # model / class, e.g. "cleaning_rover"

# What this machine can be asked to do, e.g. ["CLEANING"].
# Deliberately flat free-form strings rather than an enum: a new capability
# must not require a schema change, a rebuild, or a coordinated redeploy.
string[] capabilities

# --- Mission lifecycle state -------------------------------------------------
uint8 AVAILABLE = 0
uint8 ASSIGNED = 1
uint8 RUNNING = 2
uint8 COMPLETED = 3
uint8 EXCEPTION = 4
uint8 state

# --- Health ------------------------------------------------------------------
# Deliberately separate from `state`: a machine can be RUNNING while DEGRADED,
# and perfectly OK while sitting idle. Collapsing the two would lose the
# difference between "not working" and "not well".
uint8 OK = 0
uint8 DEGRADED = 1
uint8 FAULT = 2
uint8 health

# --- Operating mode ----------------------------------------------------------
uint8 MANUAL = 0
uint8 TELEOP = 1
uint8 SEMI_AUTO = 2
uint8 AUTOMATIC = 3
uint8 operating_mode

# --- Link status -------------------------------------------------------------
# Deliberately separate from `state`: without it, a machine that has gone silent
# keeps reporting its last known state, and a silent machine reading AVAILABLE
# would have missions dispatched into a void.
uint8 ONLINE = 0
uint8 OFFLINE = 1
uint8 CONNECTION_BROKEN = 2
uint8 connection_status

# --- Battery -----------------------------------------------------------------
# has_battery guards battery_percent, so a mains-powered machine reports
# has_battery=false instead of a misleading 0.0.
bool has_battery
float32 battery_percent

# --- Location ----------------------------------------------------------------
# has_location guards the coordinates below, so 0,0 is never ambiguous between
# "sitting exactly on the origin" and "no idea where this thing is".
bool has_location
string frame_id
float64 latitude
float64 longitude

# --- Current work ------------------------------------------------------------
# Empty string when the machine is not assigned to anything.
string current_mission_id

# --- Active errors -----------------------------------------------------------
# Parallel arrays: active_error_severities[i] is the severity of active_errors[i].
string[] active_errors
uint8 SEVERITY_WARNING = 0
uint8 SEVERITY_FATAL = 1
uint8[] active_error_severities

# --- Timestamp ---------------------------------------------------------------
builtin_interfaces/Time stamp
