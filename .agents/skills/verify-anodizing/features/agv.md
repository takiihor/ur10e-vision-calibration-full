# AGV simulator

From the checkout root, run:

```bash
.agents/skills/verify-anodizing/scripts/verify agv /absolute/path/to/evidence
```

After Doctor, open the AGV test page at `/agv` and confirm the link is disconnected. Connect the in-process simulator, query its status and verify a fresh 31-byte report. Send Stop while unarmed. Query status again and verify that the simulator reports the stopped state and that the page's API recorded Stop as confirmed by status. Disconnect the simulator before teardown.

Keep `agv.html`, `agv.json`, the `agv-*.json` action responses and `requests.jsonl` with the network isolation and teardown evidence. This journey exercises the server and simulator over loopback. Serial hardware, radio and physical motion are outside its scope.
