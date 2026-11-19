# AGV simulator

From the checkout root, run:

```bash
.agents/skills/verify-anodizing/scripts/verify agv /absolute/path/to/evidence
```

After Doctor, open the AGV test page at `/agv` and confirm the link is disconnected. Connect the in-process simulator and query a fresh 29-byte status with reply prefix `00 FF 04`, raw obstacle input `CC` and both attachment fields absent. Read speed and verify the short reply decodes to 1500 m/h. Send Stop while unarmed. Query status again and verify that the simulator reports the stopped state and confirms Stop and Read speed by status. Disconnect the simulator before teardown.

Keep `agv.html`, `agv.json`, the `agv-*.json` action responses and `requests.jsonl` with the network isolation and teardown evidence. This journey exercises the server and simulator over loopback. Serial hardware, radio and physical motion are outside its scope.
