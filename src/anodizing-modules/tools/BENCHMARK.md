# Offline module benchmark

Use a dedicated default-capability module service with a fresh IPC data directory containing the bundle's `boards/calibration_stage2` and `boards/stage2_charuco_realsense_80mm` directories. Use an isolated test endpoint, never a production or hardware-enabled service. The script refuses a service reporting camera, teaching hardware, manual, or automatic-calibration capability. For two hosts, the service stays on the IPC and the client uses a separately prepared laptop loopback tunnel. The client does not open IPC files.

```sh
python3 tools/benchmark.py --port 8870 \
  --source-id 'fixture-supplied-candidate-manifest-sha256:...' \
  --fixture 'IPC service, laptop client, isolated test tunnel' \
  --output benchmark-run.json
```

Default: one warm-up and three measured iterations; use `--warmups`, `--repeats`, or `--timeout` to adjust. Select a discovered board with `--picking-board`; otherwise the first 80 mm picking board is used. Each iteration uses new IPC session/project paths and unique job IDs. It performs native 14-view simulation, simulated teaching on the selected board, a PLACE-before-PICK refusal, PICK, and a PLACE shifted 30 mm in base X. Returned IPC paths and the calibration hash returned by Teaching identify outputs; no server path is resolved on the client. A failure after the output file opens is recorded with partial results. Preserve both the client JSON and IPC job/project artifacts.

`source_identity_supplied` and `fixture_supplied` are provided by the operator or test fixture: the client does not independently verify the server source. Server durations use server wall-clock job timestamps. Client acceptance/completion and status latency use the client monotonic clock; completion includes 100 ms polling and transport. The output includes each iteration and measured median/range without a performance threshold. It is a synthetic computation, not a camera, robot, gripper, collision, or physical accuracy test. Browser handoff and two-host reconnect are separate tests.

On service interruption, saved job, result, and simulated held state may reflect different points because they are separate atomic writes. Interrupted jobs require review of the saved artifacts; do not automatically replay them. This benchmark does not implement recovery.
