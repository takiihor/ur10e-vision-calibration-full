"""Operator guidance for Katana AGV and Bluetooth refusals.

Keep legacy executor strings at the boundary. The controller and its journals
keep their original values, and unknown reasons remain visible to technicians.
"""
import re
from datetime import datetime, timezone
from math import isfinite

from .agv import RECIPE_STALE_S, station_limit


# Code: plain reason (None keeps the existing explanation), next step, wire strings.
COPY = {
    'gateway_unavailable': (
        "The IPC job gateway is unavailable.",
        "Tell the technician to check the IPC gateway.",
        ("Job gateway unavailable.",
         "The IPC HTTPS gateway is not ready.",
         "The job gateway is unavailable.",),
    ),
    'executor_unreachable': (
        "The IPC on the AGV is not answering.",
        "Check that the AGV is powered on. Check the line-link alert before reconnecting.",
        ("Executor status unavailable.",
         "Executor unavailable. Bluetooth settings are read-only.",
         "Executor unavailable. Use the AGV e-stop if needed.",
         "IPC executor unavailable.",
         "executor_not_answering",
         "executor_unreachable",),
    ),
    'adapter_no_link': (
        "Katana's line Wi-Fi has no link.",
        "Unplug the USB Wi-Fi dongle and plug it back in. If the link stays down for 1 minute, reboot Katana.",
        ("adapter_no_link",),
    ),
    'not_armed': (
        "The AGV is disarmed for station and movement commands.",
        "When the AGV is safely stopped, select the AGV page as owner if shown, then Arm.",
        ("Arm explicitly and refresh the Stop generation before submitting.",
         "Review the current executor status and arm before requesting a station.",
         "Tick Armed first.",
         "not_armed",),
    ),
    'owner_arm': (
        "The AGV page is not the armed job owner.",
        "Select AGV page as the job owner and Arm when the AGV is safely stopped.",
        ("Select the AGV page as job owner and Arm for it before calling a station.",),
    ),
    'stale_status': (
        "Katana has no fresh AGV status.",
        "Press Query status. Check the connection if a new reading does not arrive.",
        ("AGV status is stale. Check the radio and reconnect on the AGV page.",
         "Device feedback is stale or retained simulation state is unknown.",
         "Manual travel requires fresh AGV status.",
         "stale_status",),
    ),
    'arm_feedback': (
        "Arm requires a fresh reading showing the AGV stopped and fault-free.",
        "Press Query status and check that the AGV is stopped and the reported fault is clear.",
        ("AGV movement needs fresh vehicle status with no fault or obstacle.",
         "Arm requires fresh stopped wired feedback without faults.",
         "Connect and verify fresh stopped feedback before arming.",),
    ),
    'battery_floor': (
        "The AGV battery reserve check did not pass.",
        "Charge the AGV if needed, then press Query status and verify its battery reading.",
        ("Insufficient battery reserve for job and recovery.",
         "Insufficient battery reserve for travel and recovery.",
         "Invalid simulated reserve reading.",
         "Safe hold reserve is unverified.",
         "Simulated battery reserve feedback is stale or unknown.",
         "AGV battery must be above the floor on the operator-checked track.",
         "battery_floor",),
    ),
    'agv_fault': (
        "The AGV reports a fault.",
        "Check the AGV, clear the reported fault when safe, then press Query status.",
        ("agv_fault",
         "fault",),
    ),
    'obstacle_stop': (
        None,
        "Check the track and clear any obstacle when safe, then press Query status.",
        ("AGV obstacle stop exceeded 30 s. Clear the line before starting a new run.",
         "AGV obstacle stop is active or unavailable. Park the AGV before robot work.",
         "Clear the AGV obstacle stop before manual travel.",
         "obstacle_stop",),
    ),
    'station_range': (
        "The requested station is outside the learned range.",
        "Teach the missing stations using the XBOT station-learning procedure, then press Query status.",
        ("AGV station learning is unverified. Learn the route before calling a station.",
         "station_range",),
    ),
    'station_number': (
        "The station number must be a whole number from 1 to 255.",
        "Enter a station number inside the learned range.",
        ("Choose a station from 1 to 255.",
         "The saved AGV station must be an integer from 1 to 255.",),
    ),
    'station_mode': (
        "The AGV needs station-call mode for this command.",
        "Select station-call mode in the XBOT app, then press Query status.",
        ("AGV left station-call mode or the reviewed station is unavailable.",
         "Pick/place needs station-call mode to verify and call learned stations. Enable the station-call function in the XBOT app before starting a new run.",
         "Resume needs the station-call function. In standard mode, use Forward/Reverse.",
         "Station call needs the station-call function, set in the XBOT app. In standard mode, use Forward/Reverse.",),
    ),
    'travel_window_closed': (
        "The supervisor's manual travel window has ended.",
        "Ask the supervisor to review the travel window before making another call.",
        ("The isolated test track window expired.",
         "The manual travel window expired before dispatch.",
         "The manual travel window expired.",
         "travel_window_closed",),
    ),
    'travel_disabled': (
        "Live travel or dips are disabled for this mode.",
        "Ask the supervisor to check the supervised travel setup and interlocks.",
        ("Phase 1 live station visits are disabled.",
         "Phase 1 live travel is disabled.",
         "Recipe and programme travel and live dips are disabled.",
         "Supervised live travel is disabled. An active manual travel window and qualified interlocks are required.",
         "live_travel_disabled",),
    ),
    'attestation': (
        "The confirmation for this station call is missing, expired, already used, or for a different call.",
        "Select the station again and confirm the folded arm and clear track and tank equipment from the e-stop.",
        ("Give a fresh confirmation for this station call. \"I am at the e-stop, the arm is folded, and the path and tank equipment are clear.\"",
         "Live manual travel requires the operator’s per-call confirmation. Ask the operator to use the AGV page.",
         "The confirmation must match the station call ID.",
         "This station confirmation has already been used.",
         "attestation_required",),
    ),
    'module_attestation': (
        None,
        "Review the current module on the AGV page and confirm Arm at the e-stop.",
        ("The module Arm confirmation expired. Confirm and Arm again.",
         "Enter the operator name and confirm at the e-stop before arming this module.",
         "Confirm and Arm for this module at the e-stop before requesting a move.",),
    ),
    'track_interlocks': (
        "The required travel interlocks have no clear, current reading.",
        "Ask the supervisor to verify the folded arm, battery reserve, and tank and equipment clearance for this track.",
        ("Fresh clear tank and equipment-off evidence is required before wired travel.",
         "Fresh stopped feedback, equipment off and a qualified travel pose are required.",
         "Live travel requires a manual station call and qualified local interlocks.",
         "Manual travel requires qualified robot pose, battery reserve, and clear tank and equipment-off feedback.",
         "isolated_track",),
    ),
    'module_isolated_track': (
        "The isolated track blocks module and raw motion.",
        "Ask the operator to review the supervised station-call procedure.",
        ("Module and raw motion are disabled on the isolated track.",),
    ),
    'robot_pose': (
        None,
        "Have the supervisor verify the folded arm and travel-pose setup before moving.",
        ("Live robot observation is unavailable. Check the robot before AGV movement.",
         "Lost confirmation that the robot is still at the travel pose.",
         "Robot interlock has no qualified pose.",
         "Robot is outside the saved AGV travel pose or moving. Fold the arm and wait for it to stop.",
         "Robot travel pose evidence is missing or stale.",
         "Robot travel-pose check expired or was stopped. Start a new run.",
         "Save the AGV travel pose on Teaching before AGV movement.",
         "The saved travel pose belongs to another robot. Save the pose for the configured robot.",),
    ),
    'stop_pending': (
        "Stop has no confirmed safe halt.",
        "Verify the AGV is stopped. Use its physical e-stop if needed, then check the Stop status.",
        ("AGV Stop or Pause is awaiting delivery. Check the AGV before trying again.",
         "AGV Stop or Pause is awaiting delivery. Wait before disconnecting.",
         "Finish the pending Stop before arming.",
         "Stop delivery is unconfirmed.",
         "Stop is unconfirmed. Verify a safe halt before arming or submitting.",
         "Stop remains unconfirmed.",
         "Stop unconfirmed. Verify a safe halt first.",
         "Wait for the physical job and Stop to finish.",
         "stop_pending",
         "stop_unconfirmed",),
    ),
    'operator_check': (
        None,
        "Check the AGV and its location, then complete Operator check on the page that owns the job.",
        ("Complete the operator check before arming.",
         "Complete the operator check before submitting.",
         "Complete the operator check for the retained station visit.",
         "Reconcile the retained basket before manual travel.",
         "The executor restarted during a basket or station visit.",
         "The executor restarted during this job.",
         "The executor restarted with an unverified device effect.",
         "The job stopped with a physical effect or outcome in doubt.",
         "operator_check",),
    ),
    'operator_check_review': (
        None,
        "Open the current Operator check and review the location, job and basket before confirming.",
        ("Choose the step to resume.",
         "Confirm the physical basket and location, and supply the operator.",
         "Review the basket and job named in this operator check.",
         "Select the retained manual station visit and confirm its disposition.",
         "Supply the reason for the operator check.",
         "The operator check contains an invalid step.",
         "The operator check contains invalid evidence.",
         "The operator check is unavailable. Review status or call a technician.",),
    ),
    'hold_unverified': (
        None,
        "Check the physical AGV and basket, then follow Operator check before making any new call.",
        ("AGV moved after arrival. Check the AGV before a new run.",
         "AGV moved during robot work or was already moving. Check the AGV before a new run.",
         "AGV pause state is active or unavailable. Check the AGV before starting a new run.",
         "AGV station is unconfirmed. Check route learning and station-call mode on the AGV page.",
         "Arrival at a station is unverified.",
         "Confirm a safe, released hold before continuing.",
         "Device execution is uncertain. Physical reconciliation is required before another hardware job.",
         "Recipe stopped during AGV travel. Check the AGV before starting a new run.",
         "Resume requires arrival at a station.",
         "Robot state is uncertain. Check the robot before moving.",
         "Safe hold is unverified.",
         "Station submission was interrupted.",
         "Stopped station hold is unverified.",
         "The AGV is reporting, so it is not switched off. Use AGV parked.",
         "The confirmed basket or location conflicts with the observed hold.",
         "The confirmed station conflicts with the observed station.",
         "The executor did not confirm the operator decision.",
         "The simulated basket is not in a verified hold.",
         "The station visit ended in an uncertain hold.",
         "The station visit outcome is uncertain. Check the AGV station.",
         "The stopped station is not freshly verified.",),
    ),
    'station_visit_pending': (
        "The previous station visit is still retained.",
        "Review and acknowledge its result, or complete its Operator check, before starting another call.",
        ("Check the retained station visit against the IPC evidence.",
         "Confirm the outcome of the retained station visit first.",
         "Resolve the retained station visit before changing the job owner.",
         "Review the retained station visit before starting another.",),
    ),
    'bluetooth_active': (
        "Bluetooth maintenance is open, so RS485 Connect and Arm are blocked.",
        "Disconnect on the Bluetooth page, then return to AGV controls.",
        ("Disconnect the Bluetooth maintenance session before connecting or arming RS485.",
         "bluetooth_active",),
    ),
    'bluetooth_session_open': (
        "A Bluetooth maintenance session is already open.",
        "Use the tab with the open session to disconnect it before connecting again.",
        ("A Bluetooth maintenance session is already open.",),
    ),
    'bluetooth_disconnect_pending': (
        "Katana could not confirm Bluetooth disconnected. RS485 controls stay blocked.",
        "Return to the Bluetooth page and check the session. Tell the technician if it stays blocked.",
        ("Bluetooth disconnect is unconfirmed. The RS485 controls remain blocked.",),
    ),
    'bluetooth_wired_active': (
        "The wired link is connected or armed.",
        "Disarm and disconnect on the AGV page before changing Bluetooth settings.",
        ("Disconnect and disarm the RS485 link before changing Bluetooth settings.",
         "Disconnect and disarm the executor before changing Bluetooth settings.",),
    ),
    'bluetooth_executor_busy': (
        "An executor job, Stop, or Operator check is still open.",
        "Resolve it on the AGV page or Calibration before changing Bluetooth settings.",
        ("Resolve the current executor job or operator check before changing Bluetooth settings.",),
    ),
    'bluetooth_profile': (
        "Katana needs the AGV's Bluetooth name or address in the private site profile.",
        "Ask the technician to configure the AGV's Bluetooth identity.",
        ("Configure the AGV Bluetooth name or address in the private site profile.",),
    ),
    'bluetooth_ambiguous': (
        "More than one AGV matched the Bluetooth profile.",
        "Ask the technician to set the intended AGV's address in the private site profile.",
        ("More than one AGV matches the site profile. Set its address.",),
    ),
    'bluetooth_not_found': (
        "Katana did not find the AGV over Bluetooth.",
        "Check that the AGV is powered on and in Bluetooth range, then try Connect again.",
        ("AGV not found.",),
    ),
    'bluetooth_off': (
        "Bluetooth is off or unavailable on Katana.",
        "Check Katana's Bluetooth adapter, then try Connect again.",
        ("Bluetooth is off or unavailable on Katana.",),
    ),
    'bluetooth_unavailable': (
        "The Bluetooth settings service could not open.",
        "Check the AGV's power and Bluetooth range. Tell the technician if it still fails.",
        ("AGV Bluetooth characteristics do not support settings transport.",
         "AGV Bluetooth settings service is unavailable.",
         "Bluetooth support is unavailable in the app environment.",
         "Could not connect to the AGV Bluetooth settings service.",),
    ),
    'bluetooth_session_lost': (
        "This page no longer has a Bluetooth session.",
        "Press Connect on the Bluetooth page before trying again.",
        ("Bluetooth connection was lost. Connect again.",
         "Open a Bluetooth maintenance page session first.",
         "The Bluetooth page session expired. Connect again.",
         "The Bluetooth page session expired. Reconnect before writing.",
         "This Bluetooth page session already closed. Reload and connect again.",),
    ),
    'bluetooth_transfer': (
        None,
        "Check the AGV's Bluetooth connection, then press Read settings.",
        ("Bluetooth transmission failed. Check the AGV connection.",
         "Reading the route took too long. Check the link and read again.",),
    ),
    'bluetooth_audit': (
        "Katana could not read or save Bluetooth maintenance evidence.",
        "Tell the technician to check the private Bluetooth audit directory before making more changes.",
        ("Could not open the private Bluetooth audit directory.",
         "Could not save the private Bluetooth byte log.",
         "Could not save the private Bluetooth settings snapshot or undo record.",
         "The Bluetooth audit directory must be private.",
         "The Bluetooth undo record is unreadable.",
         "The Bluetooth undo snapshot is unreadable.",
         "The private Bluetooth byte log is unavailable.",),
    ),
    'bluetooth_setting': (
        None,
        "Read settings, choose an editable field and review its allowed value before confirming.",
        ("Choose an editable Bluetooth setting.",
         "Confirm the old and new setting values before writing.",
         "Review the latest Bluetooth write before undoing.",),
    ),
    'bluetooth_setting_dependency': (
        None,
        "Read settings and review the related setting before making another change.",
        ("Disable the multi-station remote before disabling multi-station mode.",
         "Enable multi-station mode before enabling its remote.",
         "Front/rear key swap requires double-button mode (86 = 0). Check button mode and review again.",
         "Master sound is off (6D = 0). The driving sound pattern cannot be changed in this mode.",
         "Track memory requires standard mode (8C = 0). Check multi-station mode and review again.",),
    ),
    'bluetooth_write_unconfirmed': (
        None,
        "Read settings to see the current value before trying again.",
        ("Write not confirmed by read-back. Read settings again before retrying.",
         "Write not confirmed by read-back. Review the current setting.",),
    ),
    'route_review': (
        None,
        "Read settings again and review the latest outbound and return counts.",
        ("Review the current route counts, then confirm the clear.",),
    ),
    'route_empty': (
        "Both stored route counts are 0. The route is already empty.",
        "Press Read settings to verify the counts.",
        ("Outbound 0 and return 0. The stored route is already empty.",),
    ),
    'route_count': (
        None,
        "Read settings and check the counts with the technician before making any route change.",
        (),
    ),
    'route_profile': (
        None,
        "Ask the technician to check the vendor procedure for this controller profile.",
        (),
    ),
    'route_clear_unconfirmed': (
        None,
        "Press Read settings and check both counts before taking any further action.",
        ("Route clear is unconfirmed. Read the counts again before taking any further action.",),
    ),
    'wrong_transport': (
        "The requested transport or operation is unavailable on this installation.",
        "Choose the Wired transport for the IPC executor, or ask the technician about available controls.",
        ("AGV link is status-only.",
         "Connect the serial AGV on the AGV page before running a robot recipe.",
         "Only executor job controls are available on this installation.",
         "Select a supported AGV transport.",
         "Status display is read-only.",
         "The isolated test track requires the wired AGV.",
         "The wired handler accepts only a station visit.",
         "This installation uses the IPC executor link.",),
    ),
    'not_connected': (
        "The AGV connection is closed.",
        "Choose Wired and press Connect before using this control.",
        ("Connect an AGV transport first.",
         "Connect the AGV on the AGV page before starting the recipe.",),
    ),
    'already_connected': (
        "There is an open AGV connection.",
        "Disconnect the existing transport before opening another one.",
        ("Disconnect the existing link first.",
         "Disconnect the open AGV transport first.",),
    ),
    'connection_changed': (
        "The connection changed while Katana was processing the request.",
        "Check the AGV and refresh the page before trying again.",
        ("The AGV connection changed. Refresh before sending another command.",),
    ),
    'serial_setup': (
        None,
        "Ask the technician to check the selected transport and Katana service configuration.",
        ("Configure agv.wired_port before selecting Wired RS485.",
         "Serial connection requires pyserial.",
         "Serial permission denied. Log out and back in on the IPC, then select Connect.",
         "Serial port is busy or missing. Reseat the USB plug, reload this page, then select Connect.",
         "Serial port is busy. Disconnect the AGV link in the other instance or close the app using the port, then select Connect.",),
    ),
    'radio_setup': (
        None,
        "Check the values in Link settings or ask the technician to check the site profile.",
        (),
    ),
    'serial_link_lost': (
        "Katana lost the AGV radio link. The AGV may still be moving.",
        "Use the physical e-stop if needed. Check the radio connection before reconnecting.",
        ("AGV link lost. Check the radio and reconnect on the AGV page.",
         "Link lost. The AGV may still be moving. Use its e-stop if needed.",),
    ),
    'invalid_agv_reply': (
        "Katana received an incomplete or invalid AGV reply.",
        "Check the radio link and press Query status again.",
        ("Incomplete AGV status.",
         "Invalid frame checksum (SUM does not match).",
         "Invalid frame header (expected A5 5A).",
         "Invalid frame length (LEN does not match the bytes received).",
         "Missing or unexpected vehicle ID.",),
    ),
    'serial_no_gap': (
        "The AGV reports left no gap to send the command.",
        "Check the radio connection and press Query status before retrying.",
        ("Could not find a gap in the AGV reports. Check the radio and try again.",
         "Could not find a gap for the raw command.",),
    ),
    'delivery_unconfirmed': (
        None,
        "Check the physical AGV and use the e-stop if needed. Check status before retrying.",
        ("AGV station call write failed. Check the radio before a new run.",
         "AGV travel timed out. Check the line and target station.",
         "An earlier AGV command is awaiting acknowledgement. Check the AGV and try again.",
         "Do not resend an isolated test track station call. Check the AGV.",
         "Incomplete write. The command may have reached the AGV.",
         "No matching reply to the raw command.",
         "Raw station travel was stopped. Verify the AGV hold.",
         "No AGV acknowledgement within 3 s. Check the radio before starting a new run.",),
    ),
    'job_active': (
        None,
        "Review the active operation and wait for it to finish. Use Stop if needed.",
        ("A job is active. Use Stop first.",
         "A job is active. Wait for it to finish before claiming ownership.",
         "A job is active. Wait for it to finish or use Stop before releasing.",
         "A line test owns the service. Wait for confirmed restore.",
         "A robot job is running. Wait for it to finish before sending movement.",
         "An MCU request is still pending. Wait for its outcome.",
         "Another job owns the service. Wait for it to finish.",
         "Executor busy.",
         "Stop the active recipe before disconnecting the AGV.",
         "The Calibration module owns the executor. Resolve the basket before changing Bluetooth settings.",
         "The Calibration module owns the executor. Resolve the basket or use Stop.",
         "The recipe is checking AGV status. Follow Current operation.",),
    ),
    'bluetooth_calibration': (
        "The Calibration module owns the IPC executor.",
        "Resolve the Calibration basket before changing Bluetooth settings.",
        (),
    ),
    'owner_changed': (
        None,
        "Refresh the AGV page and review the current owner before trying again.",
        ("Claim an owner before arming.",
         "No current module lease matches this request. Claim ownership first.",
         "Only the current owner can release ownership.",
         "Only the current owner can submit a new job. Claim ownership first.",
         "The job owner changed. Review the AGV page before arming.",
         "The module owner changed. Review the AGV page before arming.",
         "The module owner or lease changed. Review the AGV page before arming.",
         "The module owner or lease changed. Confirm and Arm again.",
         "The owner or Stop changed. Review the AGV page before selecting an owner.",),
    ),
    'owner_selection': (
        None,
        "Choose an available owner in Job ownership on the AGV page.",
        ("Choose agv-page or calibration as the built-in owner.",
         "Choose the AGV page or Calibration module as job owner.",
         "Configure the Calibration module before assigning the executor to it.",),
    ),
    'stop_changed': (
        "Stop, Pause, disarm, or lease expiry invalidated the reviewed controls.",
        "Refresh the AGV page and verify Stop and the current owner before trying again.",
        ("AGV Stop or Pause cancelled the recipe. Check the line and start a new reviewed run.",
         "Review the Stop generation before arming or submitting.",
         "Review the current Stop generation before confirming.",
         "Review the current Stop generation before holding.",
         "Stop cancelled the reviewed operator check.",
         "Stop or lease expiry cancelled the reviewed operator check.",
         "Stop or lease expiry cancelled the reviewed Arm generation.",
         "Raw travel was stopped before dispatch.",
         "Stop, Pause or disarming cancelled this movement. Refresh and review before trying again.",),
    ),
    'lease_unavailable': (
        None,
        "Refresh the AGV page. Tell the technician if the executor stays unavailable.",
        ("The controller lease expired or was fenced.",
         "The module owner or lease changed before dispatch.",
         "The controller lease expired.",
         "The controller session is fenced.",
         "The executor control lease is unavailable.",
         "The module heartbeat expired. Verify the AGV hold before arming again.",
         "The raw scope expired. Verify the AGV hold before arming again.",
         "Waiting for executor capabilities.",),
    ),
    'operator_stop_busy': (
        "The primary IPC Stop route is busy.",
        "Verify the halt. Use the AGV e-stop if immediate intervention is needed.",
        ("Operator Stop workers are busy.",),
    ),
    'raw_link_unavailable': (
        None,
        "Verify the connected wired AGV is idle, then refresh its status.",
        ("Raw commands need an idle, connected wired link.",
         "The wired link changed during raw access.",),
    ),
    'raw_settings_changed': (
        None,
        "Disarm, verify the AGV is stopped, and read the setting again before retrying.",
        ("Raw settings require fresh, disarmed and stopped feedback.",
         "The stopped vehicle or speed changed before write.",),
    ),
    'executor_changed': (
        "The executor restarted or its control session changed.",
        "Wait for the AGV status to refresh and review it before trying again.",
        ("Incompatible protocol, boot identity or controller session.",
         "The controller session changed. Review its status before arming or submitting.",
         "The executor boot changed while establishing the lease.",
         "The executor boot changed while sampling status.",
         "The executor boot changed. Review its status before arming or submitting.",
         "The executor boot was retired while applying status.",
         "The executor boot was retired while sampling status.",),
    ),
    'review_changed': (
        "The controls or request changed since the last review.",
        "Refresh the AGV page and review the current status before trying again.",
        ("AGV approval expired. Review the link and approve the recipe again.",
         "Request ID was already used for a different request.",
         "Request ID was cancelled before acceptance.",
         "Review the current controls and supply an operator request ID.",
         "The reviewed controls changed. Refresh before sending this action.",
         "This operator request ID already belongs to another action.",),
    ),
    'request_pending': (
        None,
        "Check the outcome on the AGV page before trying again.",
        ("Review the outcome of the original operator action before trying again.",
         "This operator action is already in progress. Check status.",
         "Wait for the device disarm to complete.",),
    ),
    'operator_enrollment': (
        "This browser has no active operator enrollment.",
        "Enroll the browser with the saved operator key, then reload the page.",
        ("Calibration may use only its own key.",
         "Enroll this browser with the saved operator key to make changes.",
         "Enroll this browser again before confirming Arm for a module.",
         "Request is not authorized.",
         "The request owner does not match the authenticated key.",
         "This operation requires the operator app key.",),
    ),
    'protocol_incompatible': (
        "Katana and the IPC disagree about the job protocol or version.",
        "Tell the technician to check the Katana and IPC software versions.",
        ("Incompatible job type or version.",
         "The executor protocol is incompatible. Cached status is read-only and local control is unavailable.",),
    ),
    'journal_fault': (
        "A saved action, job or wire log is missing, unreadable, or could not be written.",
        "Keep the AGV stopped and tell the technician to check the saved records.",
        ("AGV wire logging failed. Please call a technician.",
         "Executor intents require regular journal files.",
         "Executor journal chain is damaged.",
         "Executor journal entry differs from its durable tip.",
         "Executor journal sequence is damaged.",
         "Expected a regular operator journal.",
         "Invalid executor journal event.",
         "Invalid executor module Arm record.",
         "Invalid operator journal record.",
         "Invalid saved executor job.",
         "Invalid saved open-air evidence.",
         "Invalid saved terminal executor result.",
         "Job storage failed. Report the problem before moving.",
         "Job storage failed. Restart only after checking the IPC data directory.",
         "Manual visit history requires a regular directory.",
         "Operator actions require a regular journal directory.",
         "PLC completion is unqualified for lift: verify this cycle, basket and equipment off.",
         "Short wire journal write.",
         "The Stop journal has no valid report.",
         "The executor journal count differs from its durable tip.",
         "The executor journal has no valid ID.",
         "The executor journal requires a regular directory.",
         "The executor journal requires regular files.",
         "The manual visit journal has no valid intent.",
         "The manual visit witness has no valid intent.",
         "Wait for agv-wire.jsonl to finish writing before confirming.",),
    ),
    'executor_evidence': (
        "The IPC returned invalid or conflicting status or job evidence.",
        "Keep the AGV stopped and tell the technician to check the executor evidence.",
        ("Contradictory PLC completion or measured dwell evidence.",
         "Executor Stop identity does not match the request.",
         "Executor job identity does not match the request.",
         "Executor response boot does not match the request.",
         "Executor response contains a non-finite number.",
         "Executor response integer is outside the interoperable range.",
         "Invalid IPC executor response.",
         "Invalid Stop evidence.",
         "Invalid executor generation.",
         "Invalid executor job ID.",
         "Invalid executor module Arm confirmation.",
         "Malformed IPC preadmission refusal.",
         "Malformed executor event ID.",
         "Malformed executor event stream.",
         "Malformed executor evidence faults.",
         "Malformed executor job ID.",
         "Malformed executor job outcome.",
         "Malformed executor job state.",
         "Malformed executor job versions.",
         "Malformed executor measured row.",
         "Malformed executor measured rows.",
         "Malformed executor open-air evidence.",
         "Malformed executor recovery IDs.",
         "Malformed executor response.",
         "Malformed executor result or checkpoint.",
         "Malformed manual travel battery floor.",
         "Measured rows are missing a placement, PLC sample or lift.",
         "Measured rows are missing their checkpoint copy.",
         "Measured rows are missing their confirmed effects.",
         "Measured rows disagree with the confirmed placement, PLC sample or lift.",
         "Result and checkpoint disagree on measured rows.",
         "The Stop reply conflicts with the pending request.",
         "The executor Arm and owner do not agree.",
         "The executor is armed during an operator check.",
         "The executor job outcomes conflict.",
         "The executor returned an outcome for an unfinished job.",
         "The legacy status boot does not match health.",
         "The returned executor job does not match the manual station visit.",
         "The saved station result differs from the IPC evidence.",
         "The terminal station evidence does not confirm the requested visit.",),
    ),
    'ipc_service_error': (
        "The IPC could not complete or confirm the request.",
        "Check the AGV and page status before trying again. Tell the technician if the error persists.",
        ("IPC device or storage unavailable.",
         "IPC executor refused the request.",
         "Interrupted IPC executor response.",),
    ),
    'invalid_request': (
        "Katana or the IPC could not accept the requested action or value.",
        "Reload the page and use the displayed controls. Tell the technician if it happens again.",
        ("An executor step must be a positive integer.",
         "Choose Arm or Disarm.",
         "Choose Calibration Stop.",
         "Choose Refresh catalog.",
         "Choose Start or Stop recording.",
         "Choose a Bluetooth settings action.",
         "Choose a valid Stop ID.",
         "Choose a valid job ID.",
         "Heartbeat needs a nonnegative sequence.",
         "Invalid AGV connection action.",
         "Job parameters must be an object.",
         "Not found.",
         "Request ID must contain 8 to 80 letters, numbers, underscores or hyphens.",
         "Request size is invalid.",
         "Select a known AGV command.",
         "Select one job ID.",
         "Send a JSON object.",
         "Send application/json.",
         "Send one bounded Content-Length.",
         "Send one bounded JSON object.",
         "The Stop request contains an unsupported field.",
         "The request contains an unsupported field.",
         "This AGV command takes no value.",
         "Unsupported wired raw exchange.",
         "Unknown control operation.",),
    ),
    'simulation': (
        None,
        "Check the simulator settings and availability on this installation.",
        ("Configure fake-plc-setpoints.json as a tank-to-seconds object.",
         "Configure the simulated PLC tank setpoint to match the reviewed plan.",
         "Invalid simulated device observation.",
         "Select simulator AGV mode 0 (standard) or 2 (station calling).",
         "Simulation is disabled on this service.",
         "Simulator loss fraction must be from 0 to 1.",
         "Simulator station count must be a whole number from 0 to 255.",
         "The simulated device snapshot must be a regular file.",
         "The simulation programme is unavailable.",
         "Unknown simulated effect.",
         "Use a simulator loss fraction from 0 to 1 and a whole number seed.",),
    ),
    'service_closing': (
        "The Katana app is shutting down.",
        "Reload the page once the service is back.",
        ("Service is closing.",
         "Service is closing. Reopen the page after the service restarts.",
         "The service is closing.",),
    ),
    'service_configuration': (
        "The IPC service configuration is incomplete.",
        "Tell the technician to check the executor setup.",
        ("Automatic calibration is disabled on this service.",
         "Choose a different display source port.",
         "Choose a fixed executor port.",
         "Manual movement requires reviewed --allow-manual-pick-place and a factory robot model.",
         "Set a finite manual travel expiry and a battery floor from 0 to 100.",
         "Set finite positive heartbeat and lease intervals with heartbeat shorter than lease.",
         "Start the service with --allow-teacher-motion after review.",
         "Store the Operator and Calibration service keys as hashes on the IPC.",
         "Teacher movement requires the selected factory robot model.",
         "The line-LAN executor requires a pinned HTTPS certificate.",),
    ),
    'basket_evidence': (
        None,
        "Check the retained job and physically verify the basket before completing Operator check.",
        ("A dip needs a reviewed basket, ordered steps and a verified receiving hold.",
         "A dip step contains an invalid limit.",
         "A dip step has invalid identifiers, limits or timing.",
         "A time-critical dip requires its immediate rinse.",
         "A verified receiving hold is unavailable.",
         "Cross-job exposure needs a verified lift and positive air limit.",
         "Lift and immersion times are out of order.",
         "Lift has no qualified evidence after PLC completion.",
         "Only a time-critical dip can be paired.",
         "PLC done is stale or belongs to another timer cycle.",
         "PLC has no fresh placement and timer-start evidence.",
         "PLC level alarm prevents immersion.",
         "PLC placement has no qualified immersion timestamp.",
         "The AGV is not at the required immersion station.",
         "The basket has not been verified out of the tank after the lift.",
         "The lift did not reach the required destination and gripper state.",
         "The previous physical effect has no confirmed observation.",
         "The requested job does not match the retained basket.",
         "The retained basket belongs to a different run.",
         "The retained basket does not match the basket in the job.",
         "The source or gripper does not match the verified basket handoff.",
         "The travel hold does not belong to this basket and run.",
         "The verified released basket did not arrive at the required destination.",
         "The verified transfer must retain the basket in the gripper.",
         "Verify an available input or unload the previous basket from a verified output.",),
    ),
    'speed_limit': (
        None,
        "Check Set speed on the AGV page and verify the reading before travel.",
        (),
    ),
    'station_mismatch': (
        None,
        "Physically check the AGV and confirm its station before making another call.",
        (),
    ),
}
EXACT_CODES = {reason: code for code, (_, _, reasons) in COPY.items() for reason in reasons}

# Legacy replies with live values. Match their full shape so new codes surface.
VALUE_CODES = {
    "adapter_no_link": re.compile(r"AGV link lost\. Katana's line Wi-Fi .*"),
    "executor_unreachable": re.compile(r"AGV link lost\. The IPC on the AGV is not answering\..*"),
    "station_range": re.compile(r"Choose a station in the learned range \([^)]+\) with station-call mode enabled\. The request was for station \d+, mode [^.]+\.|Choose a learned station from 1 to \d+\.|AGV station \d+ is not learned\. Check the AGV route\."),
    "agv_fault": re.compile(r"Clear AGV fault .+ before manual travel\.|AGV fault .+: .+\. Check the AGV\."),
    "battery_floor": re.compile(r"AGV battery reading is .+\. Verify at least [\d.]+% before travel\."),
    "bluetooth_transfer": re.compile(r"No valid Bluetooth reply for [0-9A-F]{2} [0-9A-F]{2}\. Check the link and read again\."),
    "bluetooth_setting": re.compile(r"Field \w+ (?:is read-only\..*|received .* and review again\.|changed from .* Read settings and review again\.|is unavailable for controller profile 18 = .*\.)"),
    "bluetooth_write_unconfirmed": re.compile(r"Field \w+ read-back was .+, expected .+\. Read settings and review again\."),
    "route_review": re.compile(r"Counts changed from .+ to .+ \(outbound, return\)\. Review again\."),
    "route_count": re.compile(r"The \w+ count is \d+\. The Bluetooth row index only covers 256 records\.|Outbound \d+ and return \d+ exceed the supported route count\..*"),
    "route_profile": re.compile(r"Route clear is unavailable for controller profile 18 = .*"),
    "route_clear_unconfirmed": re.compile(r"Route clear is unconfirmed\. Read-back was outbound \d+, return \d+\..*"),
    "radio_setup": re.compile(r"\w+ is required\. Enter it in Link settings or configure agv\.\w+\.|\w+ must contain \d+ hex byte\(s\)\."),
    "owner_changed": re.compile(r"[\w-]+ owns the executor\. (?:Ask that client to release control or use Stop|Wait for it to release ownership)\."),
    "invalid_request": re.compile(r"\w+(?: \w+)* must be an integer in \d+–\d+\."),
    "journal_fault": re.compile(r"(?:.*(?:executor-stop|executor-manual|agv-wire|executor-operator-actions)[^ ]*:\s|(?:executor-stop|executor-manual)[\w.-]* is (?:missing|unreadable|inconsistent)|(?:executor-stop|executor-manual)[\w.-]* conflicts with its intent).*|.*: (?:could not save the operator action\..*|retry the uncommitted wire frame first\.|wire journal was (?:removed|replaced)\.)"),
    "speed_limit": re.compile(r"AGV set speed is unavailable or above the recipe cap of \d+(?:\.\d+)? m/h\. Check Set speed on the AGV page\."),
    "station_mismatch": re.compile(r"AGV reported target station .+\. Expected station \d+\.|Park the AGV at station \d+ before starting robot work there\."),
}


def describe(error, state=None):
    code = EXACT_CODES.get(error)
    if code is None:
        code = next((name for name, pattern in VALUE_CODES.items() if pattern.fullmatch(error)), None)
    if code is None:
        return dict(code=error, message=f'Reason reported: {error.rstrip(".")}.', hint='Tell the technician.')
    message, hint, _ = COPY[code]
    state = state or {}
    report = state.get('status') or {}
    health = state.get('executor_health') or {}
    if code == 'station_range':
        station = re.search(r'The request was for station (\d+), mode ([^.]+)', error)
        raw = report.get('max_stations')
        maximum = station_limit(raw)
        if station and station[2] != '2':
            return dict(code=code, message=f'The AGV reported mode {station[2]} for station {station[1]}.',
                        hint=COPY['station_mode'][1])
        if type(raw) is int and 0 <= raw <= 255:
            legs = 'leg' if raw == 1 else 'legs'
            learned = f'stations 1 to {maximum}' if maximum else 'no callable stations'
            message = f'The AGV reports {raw} learned {legs}: {learned}.'
            reported_range = re.search(r'learned range \(([^)]+)\)|learned station from (1 to \d+)', error)
            if reported_range:
                rejected_range = reported_range[1] or reported_range[2]
                if rejected_range not in (f'1 to {maximum}', learned):
                    message += f' The refusal reported {rejected_range}.'
        elif error != code:
            message = error
        target = station or re.match(r'AGV station (\d+) is not learned', error)
        if target:
            message = f'The request was for station {target[1]}. {message}'
    elif code == 'battery_floor':
        reading = re.search(r'AGV battery reading is (.+)\. Verify at least ([\d.]+)%', error)
        if reading:
            battery, floor = reading.groups()
            battery = 'unavailable' if battery in ('None', 'nan', 'inf', '-inf') else f'{battery}%'
            message = f'The reported battery is {battery}. Manual travel requires at least {floor}%.'
        elif type(health.get('manual_travel_min_battery')) in (int, float):
            message += f" The configured floor is {health['manual_travel_min_battery']:g}%."
    elif code == 'agv_fault':
        reading = re.match(r'Clear AGV fault (.+) before manual travel\.|AGV fault ([^:]+):', error)
        fault = (reading[1] or reading[2]) if reading else report.get('fault_code')
        if fault is not None and fault != 'None':
            message = f'The AGV reported fault {fault}.'
        else:
            message = 'The AGV fault reading is unavailable.'
    elif code == 'obstacle_stop' and error == code:
        message = 'The AGV reports an obstacle stop.' if report.get('obstacle_stop') is True else (
            'The obstacle-stop reading is unavailable.')
    elif code == 'stale_status':
        age = state.get('status_age_s')
        if type(age) in (int, float):
            message += f' The last report is {age:.1f} seconds old. Travel needs a report under {RECIPE_STALE_S:g} seconds old.'
    elif code == 'travel_window_closed':
        deadline = health.get('manual_travel_until')
        if type(deadline) in (int, float) and isfinite(deadline) and 0 <= deadline < 253402300800:
            message += f" The deadline was {datetime.fromtimestamp(deadline, timezone.utc):%H:%M:%S} UTC."
    return dict(code=code, message=message or error, hint=hint)


def station_snapshot(latest, state):
    if not latest:
        return None
    age = state.get('status_age_s')
    max_age = RECIPE_STALE_S if state.get('executor_link') else 5
    position = latest.get('current_station')
    if position is None and latest['observed_at'] == state.get('status_time'):
        position = (state.get('status') or {}).get('current_station')
    return dict(latest, current_station=position, stale=not state['connected'] or state['stale']
                or latest['observed_at'] != state.get('status_time')
                or (age is not None and age >= max_age))


def status_feedback(state, *, bluetooth=False):
    if bluetooth:
        reasons = {'blocked': state.get('blocked'), 'route': (state.get('route') or {}).get('error')}
    else:
        visit = state.get('manual_visit') or {}
        reasons = {key: state.get(key) for key in ('error', 'bluetooth_block', 'motion_block', 'programme_motion_block')}
        reasons.update(operator_check=(state.get('operator_check') or {}).get('reason'),
                       visit=visit.get('doubt') or (visit.get('result') or {}).get('error')
                       or ((visit.get('result') or {}).get('result') or {}).get('reason'),
                       refused=(state.get('manual_visit_history') or {}).get('refused'))
    return dict(state, feedback={key: describe(reason, state) for key, reason in reasons.items()
                                 if isinstance(reason, str) and reason})
