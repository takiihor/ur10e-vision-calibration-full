# AGV simulator

From the checkout root, run:

```bash
.agents/skills/verify-anodizing/scripts/verify agv /absolute/path/to/evidence
```

After Doctor, open the AGV test page at `/agv` and confirm the link is disconnected. Connect the in-process simulator and query a fresh 29-byte status with reply prefix `00 FF 04`, raw obstacle input `CC` and both attachment fields absent. Check that Query status shows `status received` and Read speed shows `reply: 1500 m/h` as soon as the short speed reply arrives. Arm the simulator, send Reverse and check that the AGV reports −1500 m/h and that the command has been acknowledged. Call station 2 and verify the target during travel and that the AGV stops at station 2 after simulated travel. Disarm and send Stop. Query status again and verify that the simulator reports the stopped state and confirms Stop by status. Disconnect the simulator before teardown.

The server waits up to 1.5 s for each read. An unanswered read shows `no reply`. Stop and Pause can send during that window, so their transmissions can collide with a reply on the half-duplex radio.

Keep `agv.html`, `agv.json`, the `agv-*.json` action responses and `requests.jsonl` with the network isolation and teardown evidence. This journey exercises the server and simulator over loopback. Serial hardware, radio and physical motion are outside its scope.
