# Operator UI IDs

Generated from `ui-ids.json` with `node generate-ui-ids.mjs`.

Open `/operator/?ui=ids` to label the visible controls and panels. Put the query before the route hash, for example `/operator/?ui=ids#/agv`.
Use the ID when requesting a UI change, for example “make `agv.stop` bigger”.
Select one item in a repeated control by its key, for example `[data-ui="agv.call.station"][data-ui-key="2"]`.
Some keys identify records or choices from the current data. Native HTML select choices use the parent select’s ID and the option value.

357 registered IDs.

| ID | Description | Source |
| --- | --- | --- |
| `agv.call.acknowledge` | Acknowledge the terminal manual station visit result | [AgvPage.vue](AgvPage.vue) |
| `agv.call.disposition` | Verified manual station visit disposition | [AgvPage.vue](AgvPage.vue) |
| `agv.call.grid` | Station call grid | [AgvPage.vue](AgvPage.vue) |
| `agv.call.number` | Manual station number | [AgvPage.vue](AgvPage.vue) |
| `agv.call.panel` | Station and parking calls | [AgvPage.vue](AgvPage.vue) |
| `agv.call.parking` | Call the configured parking station | [AgvPage.vue](AgvPage.vue) |
| `agv.call.reason` | Shared reason station calls are disabled | [AgvPage.vue](AgvPage.vue) |
| `agv.call.reconcile` | Reconcile the original station visit | [AgvPage.vue](AgvPage.vue) |
| `agv.call.station` | Station call button keyed by station number | [AgvPage.vue](AgvPage.vue) |
| `agv.call.submit` | Call station | [AgvPage.vue](AgvPage.vue) |
| `agv.controls.panel` | Connection and commands | [AgvPage.vue](AgvPage.vue) |
| `agv.link.address` | Link address | [AgvPage.vue](AgvPage.vue) |
| `agv.link.channel` | Link channel | [AgvPage.vue](AgvPage.vue) |
| `agv.link.connect` | Connect | [AgvPage.vue](AgvPage.vue) |
| `agv.link.disconnect` | Disconnect | [AgvPage.vue](AgvPage.vue) |
| `agv.link.panel` | Link panel | [AgvPage.vue](AgvPage.vue) |
| `agv.link.port` | Link port | [AgvPage.vue](AgvPage.vue) |
| `agv.link.ports.refresh` | Refresh ports | [AgvPage.vue](AgvPage.vue) |
| `agv.link.settings` | Link settings | [AgvPage.vue](AgvPage.vue) |
| `agv.link.settings.toggle` | Link settings | [AgvPage.vue](AgvPage.vue) |
| `agv.link.silence` | Link silence | [AgvPage.vue](AgvPage.vue) |
| `agv.link.simulator.stations` | Simulator learned-station count | [AgvPage.vue](AgvPage.vue) |
| `agv.link.sound` | Link sound | [AgvPage.vue](AgvPage.vue) |
| `agv.link.transport` | Link transport | [AgvPage.vue](AgvPage.vue) |
| `agv.marker.disposition` | Verified executor marker disposition | [AgvPage.vue](AgvPage.vue) |
| `agv.marker.job` | Original executor job ID | [AgvPage.vue](AgvPage.vue) |
| `agv.marker.reconcile` | Reconcile the unreadable executor marker | [AgvPage.vue](AgvPage.vue) |
| `agv.movement.arm` | Arm or disarm manual AGV commands | [AgvPage.vue](AgvPage.vue) |
| `agv.movement.command` | Movement command | [AgvPage.vue](AgvPage.vue) |
| `agv.movement.extra` | Additional AGV movement commands | [AgvPage.vue](AgvPage.vue) |
| `agv.movement.extra.command` | One additional AGV command | [AgvPage.vue](AgvPage.vue) |
| `agv.movement.extra.toggle` | More commands | [AgvPage.vue](AgvPage.vue) |
| `agv.movement.panel` | Movement panel | [AgvPage.vue](AgvPage.vue) |
| `agv.page` | AGV commissioning workspace | [AgvPage.vue](AgvPage.vue) |
| `agv.pause` | AGV Pause (station-call mode) | [StopBar.vue](StopBar.vue) |
| `agv.read.command` | Status or speed inquiry keyed by command | [AgvPage.vue](AgvPage.vue) |
| `agv.record.checklist` | Record checklist | [AgvRecord.vue](AgvRecord.vue) |
| `agv.record.checklist.notes` | Record checklist notes | [AgvRecord.vue](AgvRecord.vue) |
| `agv.record.checklist.result` | Record checklist result | [AgvRecord.vue](AgvRecord.vue) |
| `agv.record.checklist.row` | Record checklist row | [AgvRecord.vue](AgvRecord.vue) |
| `agv.record.checklist.toggle` | Record checklist toggle | [AgvRecord.vue](AgvRecord.vue) |
| `agv.record.commands` | Record commands | [AgvRecord.vue](AgvRecord.vue) |
| `agv.record.commands.row` | One command result | [AgvRecord.vue](AgvRecord.vue) |
| `agv.record.commands.table` | Record commands table | [AgvRecord.vue](AgvRecord.vue) |
| `agv.record.commands.toggle` | Recent command results | [AgvRecord.vue](AgvRecord.vue) |
| `agv.record.event.add` | Mark event | [AgvRecord.vue](AgvRecord.vue) |
| `agv.record.event.form` | Record event form | [AgvRecord.vue](AgvRecord.vue) |
| `agv.record.event.row` | Record event row | [AgvRecord.vue](AgvRecord.vue) |
| `agv.record.event.text` | Operator observation to add to the record | [AgvRecord.vue](AgvRecord.vue) |
| `agv.record.events` | Record events | [AgvRecord.vue](AgvRecord.vue) |
| `agv.record.export.json` | Download JSON record | [AgvRecord.vue](AgvRecord.vue) |
| `agv.record.export.markdown` | Download readable report | [AgvRecord.vue](AgvRecord.vue) |
| `agv.record.exports` | Record exports | [AgvRecord.vue](AgvRecord.vue) |
| `agv.record.frames` | Record frames | [AgvRecord.vue](AgvRecord.vue) |
| `agv.record.frames.row` | One transmitted or received frame | [AgvRecord.vue](AgvRecord.vue) |
| `agv.record.frames.table` | Record frames table | [AgvRecord.vue](AgvRecord.vue) |
| `agv.record.frames.toggle` | Raw TX/RX log | [AgvRecord.vue](AgvRecord.vue) |
| `agv.record.reset` | New test session | [AgvRecord.vue](AgvRecord.vue) |
| `agv.record.reset.accept` | Confirm a new test session | [AgvRecord.vue](AgvRecord.vue) |
| `agv.record.reset.cancel` | Cancel the new test session | [AgvRecord.vue](AgvRecord.vue) |
| `agv.record.reset.close` | Close the new-session dialog | [AgvRecord.vue](AgvRecord.vue) |
| `agv.record.reset.dialog` | Record reset dialog | [AgvRecord.vue](AgvRecord.vue) |
| `agv.record.settings` | Record settings | [AgvRecord.vue](AgvRecord.vue) |
| `agv.record.settings.field` | One confirmed as-found setting | [AgvRecord.vue](AgvRecord.vue) |
| `agv.record.settings.toggle` | Settings as found | [AgvRecord.vue](AgvRecord.vue) |
| `agv.record.workspace` | Record workspace | [AgvRecord.vue](AgvRecord.vue) |
| `agv.speed.input` | Speed input | [AgvPage.vue](AgvPage.vue) |
| `agv.speed.preset` | Speed preset | [AgvPage.vue](AgvPage.vue) |
| `agv.speed.set` | Set speed | [AgvPage.vue](AgvPage.vue) |
| `agv.status.detail.row` | Status detail row | [AgvPage.vue](AgvPage.vue) |
| `agv.status.details` | Status details | [AgvPage.vue](AgvPage.vue) |
| `agv.status.details.toggle` | Machine details | [AgvPage.vue](AgvPage.vue) |
| `agv.status.metric` | One glanceable AGV status metric | [AgvPage.vue](AgvPage.vue) |
| `agv.status.panel` | Reported AGV status | [AgvPage.vue](AgvPage.vue) |
| `agv.stop` | AGV Stop (software) | [StopBar.vue](StopBar.vue) |
| `calibration.action` | Start a Calibration step action | [CalibrationPage.vue](CalibrationPage.vue) |
| `calibration.camera.field` | Camera field | [CalibrationPage.vue](CalibrationPage.vue) |
| `calibration.camera.intrinsics` | Camera intrinsics | [CalibrationPage.vue](CalibrationPage.vue) |
| `calibration.camera.provider` | Camera provider | [CalibrationPage.vue](CalibrationPage.vue) |
| `calibration.history.inspect` | Show job | [CalibrationPage.vue](CalibrationPage.vue) |
| `calibration.history.job` | Saved Calibration job ID | [CalibrationPage.vue](CalibrationPage.vue) |
| `calibration.history.panel` | History panel | [CalibrationPage.vue](CalibrationPage.vue) |
| `calibration.history.toggle` | Saved job details | [CalibrationPage.vue](CalibrationPage.vue) |
| `calibration.legacy.open` | Legacy Calibration | [CalibrationPage.vue](CalibrationPage.vue) |
| `calibration.messages.panel` | Messages panel | [CalibrationPage.vue](CalibrationPage.vue) |
| `calibration.messages.toggle` | Recent messages | [CalibrationPage.vue](CalibrationPage.vue) |
| `calibration.motion.agv.off` | Confirm the AGV is switched off for Calibration | [CalibrationPage.vue](CalibrationPage.vue) |
| `calibration.operation.approve` | Approve phase | [CalibrationPage.vue](CalibrationPage.vue) |
| `calibration.operation.cancel` | Cancel phase | [CalibrationPage.vue](CalibrationPage.vue) |
| `calibration.operation.capture` | Capture view | [CalibrationPage.vue](CalibrationPage.vue) |
| `calibration.operation.finish` | Finish collection | [CalibrationPage.vue](CalibrationPage.vue) |
| `calibration.operation.image` | Board observation | [CalibrationPage.vue](CalibrationPage.vue) |
| `calibration.operation.panel` | Operation panel | [CalibrationPage.vue](CalibrationPage.vue) |
| `calibration.page` | Calibration workspace | [CalibrationPage.vue](CalibrationPage.vue) |
| `calibration.robot.open` | Open Robot steps | [CalibrationPage.vue](CalibrationPage.vue) |
| `calibration.scene.editor` | Tool box and collision scene editor | [CalibrationPage.vue](CalibrationPage.vue) |
| `calibration.scene.open` | Open full collision editor | [CalibrationPage.vue](CalibrationPage.vue) |
| `calibration.setup.check` | Setup check | [CalibrationPage.vue](CalibrationPage.vue) |
| `calibration.setup.field` | Setup field | [CalibrationPage.vue](CalibrationPage.vue) |
| `calibration.setup.profile` | Load IPC calibration profile | [CalibrationPage.vue](CalibrationPage.vue) |
| `calibration.step.panel` | Current Calibration step | [CalibrationPage.vue](CalibrationPage.vue) |
| `calibration.step.select` | Choose a Calibration step | [CalibrationPage.vue](CalibrationPage.vue) |
| `calibration.steps` | Calibration steps | [CalibrationPage.vue](CalibrationPage.vue) |
| `calibration.teaching.open` | Continue to Teaching | [CalibrationPage.vue](CalibrationPage.vue) |
| `home.agv.open` | Open AGV commissioning | [App.vue](App.vue) |
| `home.agv.panel` | AGV summary on Home | [App.vue](App.vue) |
| `home.calibration.open` | Open current Calibration operation | [App.vue](App.vue) |
| `home.page` | Operator Home overview | [App.vue](App.vue) |
| `home.service.panel` | Service panel | [App.vue](App.vue) |
| `layout.header` | Header | [layout/AppTopbar.vue](layout/AppTopbar.vue) |
| `layout.home` | Operator app | [layout/AppTopbar.vue](layout/AppTopbar.vue) |
| `layout.main` | Main | [layout/AppLayout.vue](layout/AppLayout.vue) |
| `layout.navigation.dismiss` | Dismiss the mobile navigation backdrop | [layout/AppLayout.vue](layout/AppLayout.vue) |
| `layout.navigation.toggle` | Toggle navigation | [layout/AppTopbar.vue](layout/AppTopbar.vue) |
| `layout.sidebar` | Sidebar | [layout/AppSidebar.vue](layout/AppSidebar.vue) |
| `line.archive.download` | Download one original archived file | [LineTestPanel.vue](LineTestPanel.vue) |
| `line.archive.file.row` | Archive file row | [LineTestPanel.vue](LineTestPanel.vue) |
| `line.archive.files` | Archive files | [LineTestPanel.vue](LineTestPanel.vue) |
| `line.archive.files.toggle` | Original files | [LineTestPanel.vue](LineTestPanel.vue) |
| `line.archive.filter` | Archive filter | [LineTestPanel.vue](LineTestPanel.vue) |
| `line.archive.provenance` | Archive provenance | [LineTestPanel.vue](LineTestPanel.vue) |
| `line.archive.provenance.row` | Archive provenance row | [LineTestPanel.vue](LineTestPanel.vue) |
| `line.archive.provenance.toggle` | Historical labels and provenance | [LineTestPanel.vue](LineTestPanel.vue) |
| `line.archive.select` | Select a historical test archive | [LineTestPanel.vue](LineTestPanel.vue) |
| `line.catalog.refresh` | Refresh catalog | [LinePage.vue](LinePage.vue) |
| `line.filter.access` | Filter access | [LinePage.vue](LinePage.vue) |
| `line.filter.group` | Filter group | [LinePage.vue](LinePage.vue) |
| `line.filter.metadata` | Show advanced catalog metadata | [LinePage.vue](LinePage.vue) |
| `line.filter.search` | Filter search | [LinePage.vue](LinePage.vue) |
| `line.filter.tank` | Filter tank | [LinePage.vue](LinePage.vue) |
| `line.filter.type` | Filter type | [LinePage.vue](LinePage.vue) |
| `line.filter.view` | Filter view | [LinePage.vue](LinePage.vue) |
| `line.health.panel` | Line API reachability and PLC sample quality | [LinePage.vue](LinePage.vue) |
| `line.history.download` | Download a saved test run by format | [LineTestPanel.vue](LineTestPanel.vue) |
| `line.history.filter` | History filter | [LineTestPanel.vue](LineTestPanel.vue) |
| `line.history.judgment.row` | History judgment row | [LineTestPanel.vue](LineTestPanel.vue) |
| `line.history.notes` | History notes | [LineTestPanel.vue](LineTestPanel.vue) |
| `line.history.panel` | History panel | [LineTestPanel.vue](LineTestPanel.vue) |
| `line.history.receipts` | History receipts | [LineTestPanel.vue](LineTestPanel.vue) |
| `line.history.receipts.toggle` | Baseline, receipts and readback | [LineTestPanel.vue](LineTestPanel.vue) |
| `line.history.result` | Human judgment for the selected test run | [LineTestPanel.vue](LineTestPanel.vue) |
| `line.history.run` | Select a saved simulator test run | [LineTestPanel.vue](LineTestPanel.vue) |
| `line.history.save` | Save human result | [LineTestPanel.vue](LineTestPanel.vue) |
| `line.log.csv` | Download log CSV | [LinePage.vue](LinePage.vue) |
| `line.log.json` | Download log JSON | [LinePage.vue](LinePage.vue) |
| `line.log.refresh` | Refresh log | [LinePage.vue](LinePage.vue) |
| `line.log.row` | Log row | [LinePage.vue](LinePage.vue) |
| `line.mode.switch` | Switch between Line reads and Unified line test | [LinePage.vue](LinePage.vue) |
| `line.readings.last.panel` | Recorded last read for a signal | [LinePage.vue](LinePage.vue) |
| `line.readings.last.toggle` | Last read | [LinePage.vue](LinePage.vue) |
| `line.readings.panel` | Readings panel | [LinePage.vue](LinePage.vue) |
| `line.readings.row` | Readings row | [LinePage.vue](LinePage.vue) |
| `line.stop` | Line Stop · clear command | [StopBar.vue](StopBar.vue) |
| `line.test.active` | Active simulator write test status | [LineTestPanel.vue](LineTestPanel.vue) |
| `line.test.approval` | Test approval | [LineTestPanel.vue](LineTestPanel.vue) |
| `line.test.approve` | Approve and send 1 | [LineTestPanel.vue](LineTestPanel.vue) |
| `line.test.arm` | Arm simulator writes | [LineTestPanel.vue](LineTestPanel.vue) |
| `line.test.cancel` | Cancel approval | [LineTestPanel.vue](LineTestPanel.vue) |
| `line.test.case` | Test case | [LineTestPanel.vue](LineTestPanel.vue) |
| `line.test.command` | Test command | [LineTestPanel.vue](LineTestPanel.vue) |
| `line.test.disarm` | Disarm simulator writes | [LineTestPanel.vue](LineTestPanel.vue) |
| `line.test.group` | Test group | [LineTestPanel.vue](LineTestPanel.vue) |
| `line.test.panel` | Test panel | [LineTestPanel.vue](LineTestPanel.vue) |
| `line.test.review` | Review 0→1→0 test | [LineTestPanel.vue](LineTestPanel.vue) |
| `line.test.search` | Test search | [LineTestPanel.vue](LineTestPanel.vue) |
| `line.test.unresolved.row` | Test unresolved row | [LineTestPanel.vue](LineTestPanel.vue) |
| `line.warnings.panel` | Warnings panel | [LinePage.vue](LinePage.vue) |
| `line.warnings.row` | Warnings row | [LinePage.vue](LinePage.vue) |
| `line.warnings.toggle` | Catalog and mapping warnings | [LinePage.vue](LinePage.vue) |
| `nav.agv` | Open AGV commissioning | [layout/AppMenu.vue](layout/AppMenu.vue) |
| `nav.calibration` | Open Calibration | [layout/AppMenu.vue](layout/AppMenu.vue) |
| `nav.home` | Open Home | [layout/AppMenu.vue](layout/AppMenu.vue) |
| `nav.line` | Open Line reads | [layout/AppMenu.vue](layout/AppMenu.vue) |
| `nav.panel` | Modules | [layout/AppMenu.vue](layout/AppMenu.vue) |
| `nav.pick.place` | Open Pick/place | [layout/AppMenu.vue](layout/AppMenu.vue) |
| `nav.recorder` | Open Recorder | [layout/AppMenu.vue](layout/AppMenu.vue) |
| `nav.scene` | Open Scene | [layout/AppMenu.vue](layout/AppMenu.vue) |
| `nav.teaching` | Open Teaching | [layout/AppMenu.vue](layout/AppMenu.vue) |
| `pick.agv.open` | AGV commissioning | [PickPlacePage.vue](PickPlacePage.vue) |
| `pick.legacy.open` | Legacy Pick/place | [PickPlacePage.vue](PickPlacePage.vue) |
| `pick.motion.field` | Motion field | [PickPlacePage.vue](PickPlacePage.vue) |
| `pick.motion.panel` | Motion panel | [PickPlacePage.vue](PickPlacePage.vue) |
| `pick.motion.toggle` | Motion settings | [PickPlacePage.vue](PickPlacePage.vue) |
| `pick.operation.approval` | Operation approval | [PickPlaceJob.vue](PickPlaceJob.vue) |
| `pick.operation.confirm` | Operation confirm | [PickPlaceJob.vue](PickPlaceJob.vue) |
| `pick.operation.evidence` | Operation evidence | [PickPlaceJob.vue](PickPlaceJob.vue) |
| `pick.operation.evidence.toggle` | Inspect operation and evidence | [PickPlaceJob.vue](PickPlaceJob.vue) |
| `pick.operation.follow` | Follow active operation | [PickPlaceJob.vue](PickPlaceJob.vue) |
| `pick.operation.id` | Operation ID | [PickPlaceJob.vue](PickPlaceJob.vue) |
| `pick.operation.inspect` | Inspect status | [PickPlaceJob.vue](PickPlaceJob.vue) |
| `pick.operation.panel` | Operation panel | [PickPlaceJob.vue](PickPlaceJob.vue) |
| `pick.page` | Pick/place workspace | [PickPlacePage.vue](PickPlacePage.vue) |
| `pick.recipe.agv.off` | Confirm the AGV is switched off for a recipe | [PickPlacePage.vue](PickPlacePage.vue) |
| `pick.recipe.approval` | Recipe approval | [PickPlacePage.vue](PickPlacePage.vue) |
| `pick.recipe.endpoint.row` | Recipe endpoint row | [PickPlacePage.vue](PickPlacePage.vue) |
| `pick.recipe.field` | Recipe field | [PickPlacePage.vue](PickPlacePage.vue) |
| `pick.recipe.limit` | Recipe limit | [PickPlacePage.vue](PickPlacePage.vue) |
| `pick.recipe.panel` | Recipe panel | [PickPlacePage.vue](PickPlacePage.vue) |
| `pick.recipe.review` | Review bounded recipe | [PickPlacePage.vue](PickPlacePage.vue) |
| `pick.recipe.reviewed` | Reviewed recipe | [PickPlacePage.vue](PickPlacePage.vue) |
| `pick.recipe.sequence` | Recipe sequence | [PickPlacePage.vue](PickPlacePage.vue) |
| `pick.recipe.sequence.toggle` | Motion sequence | [PickPlacePage.vue](PickPlacePage.vue) |
| `pick.recipe.start` | Start automatic test | [PickPlacePage.vue](PickPlacePage.vue) |
| `pick.recipe.station` | Recipe station | [PickPlacePage.vue](PickPlacePage.vue) |
| `pick.recipe.travel` | Recipe travel | [PickPlacePage.vue](PickPlacePage.vue) |
| `pick.recipe.travel.approval` | Recipe travel approval | [PickPlacePage.vue](PickPlacePage.vue) |
| `pick.recipe.travel.check` | Check live travel pose | [PickPlacePage.vue](PickPlacePage.vue) |
| `pick.recipe.travel.teaching` | Change travel pose in Teaching | [PickPlacePage.vue](PickPlacePage.vue) |
| `pick.recipe.travel.toggle` | Travel pose and recipe limits | [PickPlacePage.vue](PickPlacePage.vue) |
| `pick.request.cancel` | Cancel unaccepted request | [PickPlacePage.vue](PickPlacePage.vue) |
| `pick.robot.agv.off` | Confirm the AGV is switched off for Robot steps | [PickPlacePage.vue](PickPlacePage.vue) |
| `pick.robot.field` | Robot field | [PickPlacePage.vue](PickPlacePage.vue) |
| `pick.robot.panel` | Robot panel | [PickPlacePage.vue](PickPlacePage.vue) |
| `pick.robot.rotation` | Cartesian rotation per step | [PickPlacePage.vue](PickPlacePage.vue) |
| `pick.robot.step` | Prepare a signed step on a robot axis | [PickPlacePage.vue](PickPlacePage.vue) |
| `pick.robot.toggle` | Robot steps | [PickPlacePage.vue](PickPlacePage.vue) |
| `pick.robot.translation` | Cartesian translation per step | [PickPlacePage.vue](PickPlacePage.vue) |
| `pick.setup.field` | Setup field | [PickPlacePage.vue](PickPlacePage.vue) |
| `pick.setup.panel` | Setup panel | [PickPlacePage.vue](PickPlacePage.vue) |
| `pick.single.action` | Single action | [PickPlacePage.vue](PickPlacePage.vue) |
| `pick.single.agv.off` | Confirm the AGV is switched off for a single operation | [PickPlacePage.vue](PickPlacePage.vue) |
| `pick.single.evidence` | Single evidence | [PickPlacePage.vue](PickPlacePage.vue) |
| `pick.single.evidence.toggle` | Saved role evidence | [PickPlacePage.vue](PickPlacePage.vue) |
| `pick.single.field` | Single field | [PickPlacePage.vue](PickPlacePage.vue) |
| `pick.single.item` | Single item | [PickPlacePage.vue](PickPlacePage.vue) |
| `pick.single.panel` | Single PICK or PLACE workspace | [PickPlacePage.vue](PickPlacePage.vue) |
| `pick.single.prepare` | Check saved role inputs (read only) | [PickPlacePage.vue](PickPlacePage.vue) |
| `pick.single.setup` | Confirm the taught setup for a single operation | [PickPlacePage.vue](PickPlacePage.vue) |
| `pick.single.toggle` | Single PICK / PLACE | [PickPlacePage.vue](PickPlacePage.vue) |
| `pick.status.panel` | Status panel | [PickPlacePage.vue](PickPlacePage.vue) |
| `pick.tools.agv.off` | Confirm the AGV is switched off for an MCU command | [PickPlaceTools.vue](PickPlaceTools.vue) |
| `pick.tools.check` | Check request | [PickPlaceTools.vue](PickPlaceTools.vue) |
| `pick.tools.command` | One MCU or tool power command | [PickPlaceTools.vue](PickPlaceTools.vue) |
| `pick.tools.evidence` | Tools evidence | [PickPlaceTools.vue](PickPlaceTools.vue) |
| `pick.tools.evidence.toggle` | Recorded request and reply | [PickPlaceTools.vue](PickPlaceTools.vue) |
| `pick.tools.item` | Held-item state for tool power | [PickPlaceTools.vue](PickPlaceTools.vue) |
| `pick.tools.panel` | MCU commands or tool power workspace | [PickPlaceTools.vue](PickPlaceTools.vue) |
| `pick.tools.toggle` | Open MCU commands or tool power | [PickPlaceTools.vue](PickPlaceTools.vue) |
| `programme.basket.id` | Enter basket identifier | [ProgrammePanel.vue](ProgrammePanel.vue) |
| `programme.basket.ready` | Confirm the reviewed basket at input | [ProgrammePanel.vue](ProgrammePanel.vue) |
| `programme.draft.save` | Save a new draft recipe revision | [ProgrammePanel.vue](ProgrammePanel.vue) |
| `programme.inspection.note` | Enter the inspection disposition | [ProgrammePanel.vue](ProgrammePanel.vue) |
| `programme.inspection.record` | Record the inspection disposition | [ProgrammePanel.vue](ProgrammePanel.vue) |
| `programme.limit.input` | Editable limit in seconds | [ProgrammePanel.vue](ProgrammePanel.vue) |
| `programme.panel` | Basket programme and recipe limit editor | [ProgrammePanel.vue](ProgrammePanel.vue) |
| `programme.record.panel` | Show recovered basket record | [ProgrammePanel.vue](ProgrammePanel.vue) |
| `programme.record.step` | Recorded basket dwell and transfer | [ProgrammePanel.vue](ProgrammePanel.vue) |
| `programme.record.summary` | Open the basket record | [ProgrammePanel.vue](ProgrammePanel.vue) |
| `programme.revision.finalize` | Finalize reviewed recipe limits | [ProgrammePanel.vue](ProgrammePanel.vue) |
| `programme.revision.terminal` | Reviewed terminal basket handling | [ProgrammePanel.vue](ProgrammePanel.vue) |
| `programme.revision.timing` | Reviewed PLC cycle and timing evidence | [ProgrammePanel.vue](ProgrammePanel.vue) |
| `programme.run.resume` | Resume the reviewed simulation | [ProgrammePanel.vue](ProgrammePanel.vue) |
| `programme.run.start` | Start the simulated programme | [ProgrammePanel.vue](ProgrammePanel.vue) |
| `programme.run.stop` | Request programme Stop | [ProgrammePanel.vue](ProgrammePanel.vue) |
| `programme.step.row` | Recipe step and effective limits | [ProgrammePanel.vue](ProgrammePanel.vue) |
| `recorder.agv.checklist` | AGV checklist and test exports | [RecordsPage.vue](RecordsPage.vue) |
| `recorder.agv.legacy` | Open legacy AGV page | [RecordsPage.vue](RecordsPage.vue) |
| `recorder.current.panel` | Current recording status | [RecordsPage.vue](RecordsPage.vue) |
| `recorder.current.stop` | Stop recording | [RecordsPage.vue](RecordsPage.vue) |
| `recorder.history.download` | Download one recording in the selected format | [RecordsPage.vue](RecordsPage.vue) |
| `recorder.history.panel` | History panel | [RecordsPage.vue](RecordsPage.vue) |
| `recorder.history.row` | History row | [RecordsPage.vue](RecordsPage.vue) |
| `recorder.history.unreadable.row` | History unreadable row | [RecordsPage.vue](RecordsPage.vue) |
| `recorder.new.devices` | Recording devices field | [RecordsPage.vue](RecordsPage.vue) |
| `recorder.new.form` | New form | [RecordsPage.vue](RecordsPage.vue) |
| `recorder.new.metadata` | New metadata | [RecordsPage.vue](RecordsPage.vue) |
| `recorder.new.metadata.toggle` | Session details and notes | [RecordsPage.vue](RecordsPage.vue) |
| `recorder.new.notes` | Recording session notes | [RecordsPage.vue](RecordsPage.vue) |
| `recorder.new.operator` | Recording operator name | [RecordsPage.vue](RecordsPage.vue) |
| `recorder.new.panel` | Start a new recording | [RecordsPage.vue](RecordsPage.vue) |
| `recorder.new.rate` | Recording sample rate | [RecordsPage.vue](RecordsPage.vue) |
| `recorder.new.site` | Recording site field | [RecordsPage.vue](RecordsPage.vue) |
| `recorder.new.start` | Start recording | [RecordsPage.vue](RecordsPage.vue) |
| `recorder.new.variable` | Include one available status variable | [RecordsPage.vue](RecordsPage.vue) |
| `recorder.page` | Recorder workspace | [RecordsPage.vue](RecordsPage.vue) |
| `safety.module.stop` | Software Stop for an active module job | [StopBar.vue](StopBar.vue) |
| `safety.panel` | Urgent controls | [StopBar.vue](StopBar.vue) |
| `scene.labels.panel` | Labels panel | [ScenePage.vue](ScenePage.vue) |
| `scene.labels.row` | One Scene label in the text fallback | [ScenePage.vue](ScenePage.vue) |
| `scene.legacy.open` | Open Legacy tool and collision preview | [ScenePage.vue](ScenePage.vue) |
| `scene.page` | Scene workspace | [ScenePage.vue](ScenePage.vue) |
| `scene.project.form` | Project form | [ScenePage.vue](ScenePage.vue) |
| `scene.project.load` | Load project | [ScenePage.vue](ScenePage.vue) |
| `scene.project.path` | Project path | [ScenePage.vue](ScenePage.vue) |
| `scene.view.canvas` | 3D display of the line | [SceneCanvas.vue](SceneCanvas.vue) |
| `scene.view.controls` | View controls | [SceneCanvas.vue](SceneCanvas.vue) |
| `scene.view.fit` | Fit view | [SceneCanvas.vue](SceneCanvas.vue) |
| `scene.view.label.row` | One rendered Scene label | [SceneCanvas.vue](SceneCanvas.vue) |
| `scene.view.panel` | 3D Scene panel | [ScenePage.vue](ScenePage.vue) |
| `select.option` | One option in an open PrimeVue selection list | [main.js](main.js) |
| `teaching.advanced.field` | Advanced field | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.advanced.model` | Advanced model | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.advanced.panel` | Advanced panel | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.advanced.refine` | Enable refinement for the simulated project | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.advanced.toggle` | Advanced setup and simulation | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.inspect.id` | Saved Teaching request ID | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.inspect.open` | Inspect without resuming | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.inspect.panel` | Inspect panel | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.inspect.toggle` | Inspect Teacher request and details | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.legacy.open` | Legacy Teaching | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.motion.agv.off` | Confirm the AGV is switched off for a Teaching motion | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.motion.approval` | Motion approval | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.motion.field` | Motion field | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.motion.panel` | Motion panel | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.motion.settings` | Motion settings | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.motion.settings.toggle` | Reviewed motion settings | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.motion.start` | Start the reviewed Teaching motion | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.motion.toggle` | Open the review for a Teaching motion | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.operation.panel` | Operation panel | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.page` | Teaching workspace | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.pick.download` | Download a Teaching project artifact | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.pick.export` | Read marker and export poses | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.pick.handoff` | Continue to Pick/place | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.pick.image` | Export image | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.pick.marker` | Confirm the reference marker is fixed | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.pick.panel` | Pick panel | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.pick.save` | Read TCP and save pick | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.reference.capture` | Capture marker | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.reference.image` | Reference image | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.reference.panel` | Reference panel | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.reference.station` | Reference station | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.reference.tank.mode` | Choose how to update the saved tank reference | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.reference.tank.number` | Reference tank number | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.reference.tank.select` | Reference tank select | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.scene.open` | Open the tool envelope preview | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.setup.board` | Setup board | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.setup.calibration` | Setup calibration | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.setup.camera` | Setup camera | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.setup.check` | Check setup | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.setup.manifest` | Setup manifest | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.setup.marker` | Setup marker | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.setup.open` | Open saved project | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.setup.panel` | Setup panel | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.setup.printed.board` | Select a printed board for Teaching | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.setup.project` | Setup project | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.setup.robot` | Setup robot | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.setup.target` | Setup target | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.setup.tcp` | Tool center point offset | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.setup.tcp.confirm` | Confirm the configured TCP | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.simulate` | Create simulated project | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.status.panel` | Status panel | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.travel.check` | Check live travel pose | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.travel.folded` | Confirm a folded travel pose | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.travel.panel` | Travel panel | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.travel.save` | Save current joints as travel pose | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.validation.aligned` | Alignment looks correct | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.validation.capture` | Capture for validation | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.validation.history` | Validation history | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.validation.history.row` | Validation history row | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.validation.history.toggle` | Saved validation history | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.validation.image` | Validation image | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.validation.measurement` | Validation measurement | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.validation.misaligned` | Alignment needs adjustment | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.validation.panel` | Validation panel | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.validation.saved` | Saved validation image disclosure | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.validation.saved.image` | Past validation image | [TeacherPage.vue](TeacherPage.vue) |
| `teaching.validation.saved.toggle` | View saved image | [TeacherPage.vue](TeacherPage.vue) |
