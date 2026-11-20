# Operator page HCI guide (machines that move)

For pages that command hardware: the AGV test page now, and the robot pages too. The user is an operator standing on the floor. Their eyes are on the machine, not the screen; they glance at the page from a metre or two away, maybe wearing gloves, and they may not be a programmer. Every rule below serves one question: **at a glance, does the operator know what the machine is doing, what the page just did, and what to do next?**

Sources: ISA-101 (high-performance HMI), ISA-18.2 (alarm management), ISO 9241-110 (interaction principles), ISO 13850 / IEC 60204-1 (stop and emergency-stop controls), WCAG 2.2 AA, and Nielsen's response-time limits.

## 1. The machine is the truth
- **Show what the machine last reported, and how old that report is.** Never show a commanded state as the actual state.
- Keep four words distinct and use them consistently:

  | Word | Meaning |
  |---|---|
  | **Sent** | The service wrote the command. |
  | **Acknowledged** | The AGV sent the short XBOT reply. A later status confirms the machine state. |
  | **Replied** | The AGV answered a read. |
  | **Confirmed** | A later AGV status shows the expected change. |
  | **Observed** | The operator ticked that they saw it. |

- **The link's age is always visible.** Raise a stale-link alarm after 3 s in a recipe or 5 s on the AGV commissioning page (see 6).
- On a connected link the service uses timed 485 reports while they arrive. After 2.5 report intervals without an unsolicited report, the indication expires. The service queries about once a second when timed reports are absent, including while the browser is closed. It yields to command retries and to an active recipe's status checks. **Status received** is the Query status outcome because the same frame can also arrive on its own. Read speed has a distinct reply frame.

## 2. Stop and Pause
- **Placement.** Stop and Pause stay in one fixed position at the top and are always visible. Scroll, reflow or an open panel never moves them.
- **Availability.**
  - They are enabled whenever a link is open.
  - They never ask for confirmation.
  - They never wait behind another request.
- **Feedback.** Name the command sent. Software Stop sends Stop in standard mode (0) and Pause in station-call mode (2). It starts with Stop and can try Pause if the operating mode has yet to be identified. “AGV reports stopped” requires a fresh report with the stopped flag and zero speed. An ACK or a stop flag alone leaves the motion outcome unconfirmed.
- Pick/place reads the AGV link independently of the robot travel-pose check. Escape sends a Stop request even while the travel-pose feedback is unavailable.
- **It is not an e-stop.** Label it "Stop (software)", and keep the line "Not an emergency stop. Use the AGV's e-stop." next to it. Never style it red-on-yellow, which ISO 13850 reserves for emergency stops.
- **Size.** Make Stop and Pause the largest controls on the page: at least 64 px tall, with space around them.
- **Shortcut.** Press Esc anywhere on the page to send one software Stop while connected. The Stop outcome shows `(Esc)` next to the send time. While disconnected, pressing Esc shows “Connect first” in the status line. Esc is the only command shortcut.

## 3. Movement is deliberate
- **One press authorizes one command on AGV commissioning.** The page shows “Sending…” immediately. With a status query in flight and timed reports absent, the first Stop or Pause write waits for the reply or until 0.95 s after the query write, whichever comes first. Otherwise the first write sends immediately. Stop and Pause can retry up to four times after the first write. A confirming status can end retries. An ACK can end retries only when it identifies the outstanding halt unambiguously. Scheduled writes wait at least 0.75 s from the last write and use the 100–150 ms gap when timed reports are on schedule. Retries without timed reports wait at least 1.5 s. Ordinary commands wait for an open read to finish. A repeated Stop press joins the pending Stop. An unchanged parked status on a timed stream or from a query sent before the command only confirms the vehicle's current state.
- Repeatable movement and setting commands get up to four tries at least 2 s apart. Relative 180° rotation gets one write and reports uncertain delivery when no reply arrives. Each movement write needs valid, fresh feedback. A station call also needs station-call mode and a learned destination.
- When timed reports are detected, scheduled writes align with the measured 100–150 ms gap after a report. Stop and Pause invalidate earlier movement approvals and supersede ordinary retries. Disarm or recipe handoff on the same link preserves a pending halt. The service refuses new foreground requests during the urgent delivery window.
- Without timed reports, the page polls every 1.2 s and the recipe polls every 0.9 s, subject to the open reply window. Automatic polls yield to an urgent write waiting for a status reply, its pending ACK and its retries. A report that could answer a pre-halt query updates telemetry while halt delivery remains unconfirmed. An unambiguous ACK within 0.3 s of a write command's no-reply outcome changes the message to “AGV acknowledged late” and updates the link counts.
- **Arming.**
  - Movement needs a visible Armed control.
  - Armed resets on reconnect and is never restored from storage.
  - While Armed is on, the page shows it plainly, for example by tinting the movement column.
  - If Stop or Pause is pressed after a Pick/place recipe was approved, review the live AGV status and tick the recipe approvals again before pressing Start.
- Standalone MCU gripper actuation requires AGV parked or an operator confirmation that it is switched off. If the AGV starts reporting during a job declared switched off, the job is interrupted. The read-sensors button stays available under the existing MCU rules.
- **Disabled controls say why.** For example "Tick Armed to enable movement", "Connect first" or "A robot job is running". A greyed-out button with no reason is a bug.
- **Validate input before sending.** Set speed defaults to 500 m/h, the slowest setting. Check ranges (speed 500–4000 m/h, stations 1–N) and explain any limit next to the field.
- In standard mode (0), show the stop counter and distance to the stop line. Forward and Reverse are the vehicle command names. The Stations panel explains how to enable station call in the XBOT app.

## 4. Feedback timing
| After a press | The page must show |
|---|---|
| ≤ 0.1 s | That the press registered: "Sending…" and a pressed state. |
| Any wait > 1 s | Progress, with a countdown to the deadline: "Waiting for the AGV… 4 s". |
| At the end | Exactly one outcome, in words: replied, confirmed, not confirmed, no reply, or refused. |

**Each outcome appears in two places:** next to the control the operator pressed, and once in a single status line at the top. Never only in the log.

## 5. Plain language
- **Messages follow one pattern:** what happened, then why if it is known, then what to do next. For example: "No reply in 1.5 s. The radio may be unplugged. Reseat the USB plug, reload, Connect."
- **No hex, no exception text, no Python names in primary messages.** Those belong in the log and the record.
- **Use units everywhere:** m/h with km/h alongside, %, s, station numbers.
- **Name modes and faults.** For example "Mode 2: station calling" and "Fault 61: off track (manual p. 15)".
- **Language.** English labels. Keep the vendor's Chinese term in brackets where the operator has to find it in the XBOT app, e.g. "485 timed reports [定时发送485]".
- **Local time.** Times appear in local time (HH:MM:SS, with milliseconds for events). The record keeps ISO timestamps.

## 6. Calm when normal, loud when abnormal
- **Normal state is calm.** Use neutral panels (the app's white panels and grey borders), and save colour for meaning.
- **Use the app's existing state colours** (`style.css` `data-state`):

  | Colour | Meaning |
  |---|---|
  | Blue | In progress |
  | Amber | Waiting or attention |
  | Green | Confirmed OK |
  | Red | Failure, fault, or link lost |

- **Never use colour alone.** Every state also has a word, and an icon where it helps. Contrast follows WCAG 2.2 AA: text 4.5:1, controls 3:1.
- **Alarms are only conditions that need the operator to act:**
  - the link is stale or lost;
  - a fault code is not 0;
  - an obstacle stop;
  - a transport error.
  
  Each alarm states the action to take. It stays visible until the condition clears, and the record logs both its start and its clear time.
- **An alarm sound is optional, off by default, and set per session.**

## 7. Glanceable layout
- **Top bar (sticky):**
  - the connection banner: Disconnected / Connected · last status report n s ago / **Link stale** / Error;
  - Stop and Pause.
- **Main column:**
  - big state readouts first: moving or stopped, current station → target, speed, battery, fault;
  - then the stations;
  - then the guided checklist.
- **Right column:** the app's sticky operation column, holding Armed and the movement commands, as on the other pages.
- **Readability.** The primary state is readable from about 2 m, at about 28 px or more for moving/stopped and station. Show no more than about 7 primary readouts; everything else goes in "Details".
- **The raw log** sits at the bottom, collapsed by default, for experts and for the record.

## 8. Errors and recovery
- **Prevent first, explain second.** Disable impossible actions and validate input. Ask for confirmation only for actions that destroy data, such as "New test session"; never for Stop or Pause.
- **Every error names its fix.** For example:
  - permission denied: refresh serial permissions for the service user on the IPC;
  - port missing: reseat the plug, reload;
  - link lost mid-move: the AGV may still be moving, use its e-stop.
- **Reload-safe.** The test session (checklist, notes, settings as found) saves as you go and survives a reload. Storage failures must not break the page.

## 9. Accessibility and input
- **WCAG 2.2 AA.** Keyboard operable, visible focus, labels on every control.
- **Status changes go in an `aria-live` region:** polite for outcomes, assertive for alarms.
- **Targets.** At least 44 px for normal controls (gloves), and larger for Stop and Pause.

## 10. Records an operator and a manager can use
- The record includes:
  - the session (operator, site, AGV, radio, app settings as found);
  - each test (result, notes, evidence);
  - events;
  - the full TX/RX log with the try number on each send;
  - per-connection link quality: frames sent, commands acknowledged by a short ACK, retry frames, commands with no reply, and the detected timed-report interval.
- **Two exports:** a readable report (summary table first) and the full JSON.
- **Simulator evidence is never presented as hardware evidence.** The record states the transport used.

## Review checklist (part of "done" for any operator page)
- [ ] Stop and Pause stay in place, stay enabled while a link is open, and show “Sending…” on press. An open query reply is the only reason the first write waits for a wire slot.
- [ ] Every press shows "Sending…" within 0.1 s and ends in exactly one worded outcome, beside the control and in the status line.
- [ ] Sent, replied, confirmed and observed are never conflated.
- [ ] The link's age is always visible; stale shows as a red alarm with an action.
- [ ] Every disabled control says why.
- [ ] No primary message contains hex or exception text; every error names the next step.
- [ ] No state is shown by colour alone; the contrast checks pass.
- [ ] Armed is visible, resets on reconnect, and is never restored.
- [ ] A reload keeps the test session; blocked storage doesn't break the page.
- [ ] The readable report opens with the test summary table and names the transport (simulator or serial).
