import copy

from cereal import car
from openpilot.common.conversions import Conversions as CV
from openpilot.common.numpy_fast import mean
from openpilot.common.filter_simple import FirstOrderFilter
from openpilot.common.realtime import DT_CTRL
from opendbc.can.can_define import CANDefine
from opendbc.can.parser import CANParser
from openpilot.selfdrive.car.interfaces import CarStateBase
from openpilot.selfdrive.car.toyota.values import ToyotaFlags, CAR, DBC, STEER_THRESHOLD, NO_STOP_TIMER_CAR, \
                                                  TSS2_CAR, RADAR_ACC_CAR, EPS_SCALE, UNSUPPORTED_DSU_CAR
from openpilot.selfdrive.controls.lib.drive_helpers import CRUISE_LONG_PRESS

from openpilot.selfdrive.frogpilot.functions.speed_limit_controller import SpeedLimitController

SteerControlType = car.CarParams.SteerControlType

# These steering fault definitions seem to be common across LKA (torque) and LTA (angle):
# - high steer rate fault: goes to 21 or 25 for 1 frame, then 9 for 2 seconds
# - lka/lta msg drop out: goes to 9 then 11 for a combined total of 2 seconds, then 3.
#     if using the other control command, goes directly to 3 after 1.5 seconds
# - initializing: LTA can report 0 as long as STEER_TORQUE_SENSOR->STEER_ANGLE_INITIALIZING is 1,
#     and is a catch-all for LKA
TEMP_STEER_FAULTS = (0, 9, 11, 21, 25)
# - lka/lta msg drop out: 3 (recoverable)
# - prolonged high driver torque: 17 (permanent)
PERM_STEER_FAULTS = (3, 17)

ZSS_THRESHOLD = 4.0
ZSS_THRESHOLD_COUNT = 10

class CarState(CarStateBase):
  def __init__(self, CP):
    super().__init__(CP)
    can_define = CANDefine(DBC[CP.carFingerprint]["pt"])
    self.shifter_values = can_define.dv["GEAR_PACKET"]["GEAR"]
    self.eps_torque_scale = EPS_SCALE[CP.carFingerprint] / 100.
    self.cluster_speed_hyst_gap = CV.KPH_TO_MS / 2.
    self.cluster_min_speed = CV.KPH_TO_MS / 2.

    # On cars with cp.vl["STEER_TORQUE_SENSOR"]["STEER_ANGLE"]
    # the signal is zeroed to where the steering angle is at start.
    # Need to apply an offset as soon as the steering angle measurements are both received
    self.accurate_steer_angle_seen = False
    self.angle_offset = FirstOrderFilter(None, 60.0, DT_CTRL, initialized=False)

    self.low_speed_lockout = False
    self.acc_type = 1
    self.lkas_hud = {}

    # FrogPilot variables
    self.profile_restored = False
    self.zss_compute = False
    self.zss_cruise_active_last = False

    self.zss_angle_offset = 0
    self.zss_threshold_count = 0

    self.traffic_signals = {}

  def update(self, cp, cp_cam, frogpilot_variables):
    ret = car.CarState.new_message()

    ret.doorOpen = any([cp.vl["BODY_CONTROL_STATE"]["DOOR_OPEN_FL"], cp.vl["BODY_CONTROL_STATE"]["DOOR_OPEN_FR"],
                        cp.vl["BODY_CONTROL_STATE"]["DOOR_OPEN_RL"], cp.vl["BODY_CONTROL_STATE"]["DOOR_OPEN_RR"]])
    ret.seatbeltUnlatched = cp.vl["BODY_CONTROL_STATE"]["SEATBELT_DRIVER_UNLATCHED"] != 0
    ret.parkingBrake = cp.vl["BODY_CONTROL_STATE"]["PARKING_BRAKE"] == 1

    ret.brakePressed = cp.vl["BRAKE_MODULE"]["BRAKE_PRESSED"] != 0
    ret.brakeHoldActive = cp.vl["ESP_CONTROL"]["BRAKE_HOLD_ACTIVE"] == 1
    if self.CP.enableGasInterceptor:
      ret.gas = (cp.vl["GAS_SENSOR"]["INTERCEPTOR_GAS"] + cp.vl["GAS_SENSOR"]["INTERCEPTOR_GAS2"]) // 2
      ret.gasPressed = ret.gas > 805
    else:
      # TODO: find a common gas pedal percentage signal
      ret.gasPressed = cp.vl["PCM_CRUISE"]["GAS_RELEASED"] == 0

    ret.wheelSpeeds = self.get_wheel_speeds(
      cp.vl["WHEEL_SPEEDS"]["WHEEL_SPEED_FL"],
      cp.vl["WHEEL_SPEEDS"]["WHEEL_SPEED_FR"],
      cp.vl["WHEEL_SPEEDS"]["WHEEL_SPEED_RL"],
      cp.vl["WHEEL_SPEEDS"]["WHEEL_SPEED_RR"],
    )
    ret.vEgoRaw = mean([ret.wheelSpeeds.fl, ret.wheelSpeeds.fr, ret.wheelSpeeds.rl, ret.wheelSpeeds.rr])
    ret.vEgo, ret.aEgo = self.update_speed_kf(ret.vEgoRaw)
    if self.CP.carFingerprint == CAR.LEXUS_ES_TSS2:
      ret.vEgoCluster = ret.vEgo * 1.023456789
    else:
      ret.vEgoCluster = ret.vEgo * 1.015  # minimum of all the cars

    ret.standstill = abs(ret.vEgoRaw) < 1e-3

    ret.steeringAngleDeg = cp.vl["STEER_ANGLE_SENSOR"]["STEER_ANGLE"] + cp.vl["STEER_ANGLE_SENSOR"]["STEER_FRACTION"]
    ret.steeringRateDeg = cp.vl["STEER_ANGLE_SENSOR"]["STEER_RATE"]
    torque_sensor_angle_deg = cp.vl["STEER_TORQUE_SENSOR"]["STEER_ANGLE"]

    # On some cars, the angle measurement is non-zero while initializing
    if abs(torque_sensor_angle_deg) > 1e-3 and not bool(cp.vl["STEER_TORQUE_SENSOR"]["STEER_ANGLE_INITIALIZING"]):
      self.accurate_steer_angle_seen = True

    if self.accurate_steer_angle_seen:
      # Offset seems to be invalid for large steering angles and high angle rates
      if abs(ret.steeringAngleDeg) < 90 and abs(ret.steeringRateDeg) < 100 and cp.can_valid:
        self.angle_offset.update(torque_sensor_angle_deg - ret.steeringAngleDeg)

      if self.angle_offset.initialized:
        ret.steeringAngleOffsetDeg = self.angle_offset.x
        ret.steeringAngleDeg = torque_sensor_angle_deg - self.angle_offset.x

    can_gear = int(cp.vl["GEAR_PACKET"]["GEAR"])
    ret.gearShifter = self.parse_gear_shifter(self.shifter_values.get(can_gear, None))
    ret.leftBlinker = cp.vl["BLINKERS_STATE"]["TURN_SIGNALS"] == 1
    ret.rightBlinker = cp.vl["BLINKERS_STATE"]["TURN_SIGNALS"] == 2

    if self.CP.carFingerprint != CAR.MIRAI:
      ret.engineRpm = cp.vl["ENGINE_RPM"]["RPM"]

    ret.steeringTorque = cp.vl["STEER_TORQUE_SENSOR"]["STEER_TORQUE_DRIVER"]
    ret.steeringTorqueEps = cp.vl["STEER_TORQUE_SENSOR"]["STEER_TORQUE_EPS"] * self.eps_torque_scale
    # we could use the override bit from dbc, but it's triggered at too high torque values
    ret.steeringPressed = abs(ret.steeringTorque) > STEER_THRESHOLD

    # Check EPS LKA/LTA fault status
    ret.steerFaultTemporary = cp.vl["EPS_STATUS"]["LKA_STATE"] in TEMP_STEER_FAULTS
    ret.steerFaultPermanent = cp.vl["EPS_STATUS"]["LKA_STATE"] in PERM_STEER_FAULTS

    if self.CP.steerControlType == SteerControlType.angle:
      ret.steerFaultTemporary = ret.steerFaultTemporary or cp.vl["EPS_STATUS"]["LTA_STATE"] in TEMP_STEER_FAULTS
      ret.steerFaultPermanent = ret.steerFaultPermanent or cp.vl["EPS_STATUS"]["LTA_STATE"] in PERM_STEER_FAULTS

    if self.CP.carFingerprint in UNSUPPORTED_DSU_CAR:
      # TODO: find the bit likely in DSU_CRUISE that describes an ACC fault. one may also exist in CLUTCH
      ret.cruiseState.available = cp.vl["DSU_CRUISE"]["MAIN_ON"] != 0
      ret.cruiseState.speed = cp.vl["DSU_CRUISE"]["SET_SPEED"] * CV.KPH_TO_MS
      cluster_set_speed = cp.vl["PCM_CRUISE_ALT"]["UI_SET_SPEED"]
    else:
      ret.accFaulted = cp.vl["PCM_CRUISE_2"]["ACC_FAULTED"] != 0
      ret.cruiseState.available = cp.vl["PCM_CRUISE_2"]["MAIN_ON"] != 0
      ret.cruiseState.speed = cp.vl["PCM_CRUISE_2"]["SET_SPEED"] * CV.KPH_TO_MS
      cluster_set_speed = cp.vl["PCM_CRUISE_SM"]["UI_SET_SPEED"]

    # UI_SET_SPEED is always non-zero when main is on, hide until first enable
    if ret.cruiseState.speed != 0:
      is_metric = cp.vl["BODY_CONTROL_STATE_2"]["UNITS"] in (1, 2)
      conversion_factor = CV.KPH_TO_MS if is_metric else CV.MPH_TO_MS
      ret.cruiseState.speedCluster = cluster_set_speed * conversion_factor

    cp_acc = cp_cam if self.CP.carFingerprint in (TSS2_CAR - RADAR_ACC_CAR) else cp

    if self.CP.carFingerprint in TSS2_CAR and not self.CP.flags & ToyotaFlags.DISABLE_RADAR.value:
      if not (self.CP.flags & ToyotaFlags.SMART_DSU.value):
        self.acc_type = cp_acc.vl["ACC_CONTROL"]["ACC_TYPE"]
      ret.stockFcw = bool(cp_acc.vl["PCS_HUD"]["FCW"])

    # some TSS2 cars have low speed lockout permanently set, so ignore on those cars
    # these cars are identified by an ACC_TYPE value of 2.
    # TODO: it is possible to avoid the lockout and gain stop and go if you
    # send your own ACC_CONTROL msg on startup with ACC_TYPE set to 1
    if (self.CP.carFingerprint not in TSS2_CAR and self.CP.carFingerprint not in UNSUPPORTED_DSU_CAR) or \
       (self.CP.carFingerprint in TSS2_CAR and self.acc_type == 1):
      self.low_speed_lockout = cp.vl["PCM_CRUISE_2"]["LOW_SPEED_LOCKOUT"] == 2

    self.pcm_acc_status = cp.vl["PCM_CRUISE"]["CRUISE_STATE"]
    if self.CP.carFingerprint not in (NO_STOP_TIMER_CAR - TSS2_CAR):
      # ignore standstill state in certain vehicles, since pcm allows to restart with just an acceleration request
      ret.cruiseState.standstill = self.pcm_acc_status == 7
    ret.cruiseState.enabled = bool(cp.vl["PCM_CRUISE"]["CRUISE_ACTIVE"])
    ret.cruiseState.nonAdaptive = cp.vl["PCM_CRUISE"]["CRUISE_STATE"] in (1, 2, 3, 4, 5, 6)
    self.pcm_neutral_force = cp.vl["PCM_CRUISE"]["NEUTRAL_FORCE"]

    ret.genericToggle = bool(cp.vl["LIGHT_STALK"]["AUTO_HIGH_BEAM"])
    ret.espDisabled = cp.vl["ESP_CONTROL"]["TC_DISABLED"] != 0

    if not self.CP.enableDsu and not self.CP.flags & ToyotaFlags.DISABLE_RADAR.value:
      ret.stockAeb = bool(cp_acc.vl["PRE_COLLISION"]["PRECOLLISION_ACTIVE"] and cp_acc.vl["PRE_COLLISION"]["FORCE"] < -1e-5)

    if self.CP.enableBsm:
      ret.leftBlindspot = (cp.vl["BSM"]["L_ADJACENT"] == 1) or (cp.vl["BSM"]["L_APPROACHING"] == 1)
      ret.rightBlindspot = (cp.vl["BSM"]["R_ADJACENT"] == 1) or (cp.vl["BSM"]["R_APPROACHING"] == 1)

    if self.CP.carFingerprint != CAR.PRIUS_V:
      self.lkas_hud = copy.copy(cp_cam.vl["LKAS_HUD"])

    # if openpilot does not control longitudinal, in this case, assume 0x343 is on bus0
    # 1) the car is no longer sending standstill
    # 2) the car is still in standstill
    if not self.CP.openpilotLongitudinalControl:
      self.stock_resume_ready = cp.vl["ACC_CONTROL"]["RELEASE_STANDSTILL"] == 1

    # FrogPilot functions
    if self.CP.carFingerprint in (TSS2_CAR - RADAR_ACC_CAR) or (self.CP.flags & ToyotaFlags.SMART_DSU and not self.CP.flags & ToyotaFlags.RADAR_CAN_FILTER):
      # distance button is wired to the ACC module (camera or radar)
      if self.CP.carFingerprint in (TSS2_CAR - RADAR_ACC_CAR):
        distance_pressed = cp_acc.vl["ACC_CONTROL"]["DISTANCE"]
      else:
        distance_pressed = cp.vl["SDSU"]["FD_BUTTON"]

    # Switch the current state of Experimental Mode if the LKAS button is double pressed
    if frogpilot_variables.experimental_mode_via_lkas and ret.cruiseState.available and self.CP.carFingerprint != CAR.PRIUS_V:
      message_keys = ["LDA_ON_MESSAGE", "SET_ME_X02"]
      lkas_pressed = any(self.lkas_hud.get(key) == 1 for key in message_keys)

      if lkas_pressed and not self.lkas_previously_pressed:
        if frogpilot_variables.conditional_experimental_mode:
          self.fpf.update_cestatus_lkas()
        else:
          self.fpf.update_experimental_mode()

      self.lkas_previously_pressed = lkas_pressed

    # Distance button functions
    if ret.cruiseState.available:
      if distance_pressed:
        self.distance_pressed_counter += 1
      elif self.distance_previously_pressed:
        # Set the distance lines on the dash to match the new personality if the button was held down for less than 0.5 seconds
        if self.distance_pressed_counter < CRUISE_LONG_PRESS:
          self.previous_personality_profile = (self.personality_profile + 2) % 3
          self.fpf.distance_button_function(self.previous_personality_profile)
          self.profile_restored = False
        self.distance_pressed_counter = 0

      # Switch the current state of Experimental Mode if the button is held down for 0.5 seconds
      if self.distance_pressed_counter == CRUISE_LONG_PRESS and frogpilot_variables.experimental_mode_via_distance:
        if frogpilot_variables.conditional_experimental_mode:
          self.fpf.update_cestatus_distance()
        else:
          self.fpf.update_experimental_mode()

      # Switch the current state of Traffic Mode if the button is held down for 2.5 seconds
      if self.distance_pressed_counter == CRUISE_LONG_PRESS * 5 and frogpilot_variables.traffic_mode:
        self.fpf.update_traffic_mode()

        # Revert the previous changes to Experimental Mode
        if frogpilot_variables.conditional_experimental_mode:
          self.fpf.update_cestatus_distance()
        else:
          self.fpf.update_experimental_mode()

      self.distance_previously_pressed = distance_pressed

    # Update the distance lines on the dash upon ignition/onroad UI button clicked
    if frogpilot_variables.personalities_via_wheel and ret.cruiseState.available:
      # Need to subtract by 1 to comply with the personality profiles of "0", "1", and "2"
      self.personality_profile = cp.vl["PCM_CRUISE_SM"]["DISTANCE_LINES"] - 1

      # Sync with the onroad UI button
      if self.fpf.personality_changed_via_ui:
        self.profile_restored = False
        self.previous_personality_profile = self.fpf.current_personality
        self.fpf.reset_personality_changed_param()

      # Set personality to the previous drive's personality or when the user changes it via the UI
      if self.personality_profile == self.previous_personality_profile:
        self.profile_restored = True
      if not self.profile_restored:
        self.distance_button = not self.distance_button

    # Traffic signals for Speed Limit Controller - Credit goes to the DragonPilot team!
    self.update_traffic_signals(cp_cam)
    SpeedLimitController.car_speed_limit = self.calculate_speed_limit(frogpilot_variables)
    SpeedLimitController.write_car_state()

    # ZSS Support - Credit goes to the DragonPilot team!
    if self.CP.flags & ToyotaFlags.ZSS and self.zss_threshold_count < ZSS_THRESHOLD_COUNT:
      zorro_steer = cp.vl["SECONDARY_STEER_ANGLE"]["ZORRO_STEER"]
      # Only compute ZSS offset when acc is active
      zss_cruise_active = ret.cruiseState.available
      if zss_cruise_active and not self.zss_cruise_active_last:
        self.zss_compute = True  # Cruise was just activated, so allow offset to be recomputed
        self.zss_threshold_count = 0
      self.zss_cruise_active_last = zss_cruise_active

      # Compute ZSS offset
      if self.zss_compute:
        if abs(ret.steeringAngleDeg) > 1e-3 and abs(zorro_steer) > 1e-3:
          self.zss_compute = False
          self.zss_angle_offset = zorro_steer - ret.steeringAngleDeg

      # Error check
      new_steering_angle_deg = zorro_steer - self.zss_angle_offset
      if abs(ret.steeringAngleDeg - new_steering_angle_deg) > ZSS_THRESHOLD:
        self.zss_threshold_count += 1
      else:
        # Apply offset
        ret.steeringAngleDeg = new_steering_angle_deg

    return ret

  def update_traffic_signals(self, cp_cam):
    signals = ["TSGN1", "SPDVAL1", "SPLSGN1", "TSGN2", "SPLSGN2", "TSGN3", "SPLSGN3", "TSGN4", "SPLSGN4"]
    new_values = {signal: cp_cam.vl["RSA1"].get(signal, cp_cam.vl["RSA2"].get(signal)) for signal in signals}

    if new_values != self.traffic_signals:
      self.traffic_signals.update(new_values)

  def calculate_speed_limit(self, frogpilot_variables):
    tsgn1 = self.traffic_signals.get("TSGN1", None)
    spdval1 = self.traffic_signals.get("SPDVAL1", None)

    if tsgn1 == 1 and not frogpilot_variables.force_mph_dashboard:
      return spdval1 * CV.KPH_TO_MS
    elif tsgn1 == 36 or frogpilot_variables.force_mph_dashboard:
      return spdval1 * CV.MPH_TO_MS
    else:
      return 0

  @staticmethod
  def get_can_parser(CP):
    messages = [
      ("GEAR_PACKET", 1),
      ("LIGHT_STALK", 1),
      ("BLINKERS_STATE", 0.15),
      ("BODY_CONTROL_STATE", 3),
      ("BODY_CONTROL_STATE_2", 2),
      ("ESP_CONTROL", 3),
      ("EPS_STATUS", 25),
      ("BRAKE_MODULE", 40),
      ("WHEEL_SPEEDS", 80),
      ("STEER_ANGLE_SENSOR", 80),
      ("PCM_CRUISE", 33),
      ("PCM_CRUISE_SM", 1),
      ("STEER_TORQUE_SENSOR", 50),
    ]

    if CP.carFingerprint != CAR.MIRAI:
      messages.append(("ENGINE_RPM", 42))

    if CP.carFingerprint in UNSUPPORTED_DSU_CAR:
      messages.append(("DSU_CRUISE", 5))
      messages.append(("PCM_CRUISE_ALT", 1))
    else:
      messages.append(("PCM_CRUISE_2", 33))

    # add gas interceptor reading if we are using it
    if CP.enableGasInterceptor:
      messages.append(("GAS_SENSOR", 50))

    if CP.enableBsm:
      messages.append(("BSM", 1))

    if not CP.openpilotLongitudinalControl or (CP.carFingerprint in RADAR_ACC_CAR and not CP.flags & ToyotaFlags.DISABLE_RADAR.value):
      if not CP.flags & ToyotaFlags.SMART_DSU.value:
        messages += [
          ("ACC_CONTROL", 33),
        ]
      messages += [
        ("PCS_HUD", 1),
      ]

    if CP.carFingerprint not in (TSS2_CAR - RADAR_ACC_CAR) and not CP.enableDsu and not CP.flags & ToyotaFlags.DISABLE_RADAR.value:
      messages += [
        ("PRE_COLLISION", 33),
      ]

    if CP.flags & ToyotaFlags.SMART_DSU and not CP.flags & ToyotaFlags.RADAR_CAN_FILTER:
      messages += [
        ("SDSU", 100),
      ]

    messages += [("SECONDARY_STEER_ANGLE", 0)]

    return CANParser(DBC[CP.carFingerprint]["pt"], messages, 0)

  @staticmethod
  def get_cam_can_parser(CP):
    messages = []

    messages += [
      ("RSA1", 0),
      ("RSA2", 0),
    ]

    if CP.carFingerprint != CAR.PRIUS_V:
      messages += [
        ("LKAS_HUD", 1),
      ]

    if CP.carFingerprint in (TSS2_CAR - RADAR_ACC_CAR):
      messages += [
        ("PRE_COLLISION", 33),
        ("ACC_CONTROL", 33),
        ("PCS_HUD", 1),
      ]

    if not CP.openpilotLongitudinalControl and CP.carFingerprint not in (TSS2_CAR, UNSUPPORTED_DSU_CAR):
      messages.append(("ACC_CONTROL", 33))

    return CANParser(DBC[CP.carFingerprint]["pt"], messages, 2)
