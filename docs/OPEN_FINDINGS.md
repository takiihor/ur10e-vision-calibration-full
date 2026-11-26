# Open findings from the second review of 04b9d31

| ID | Observed failure | File |
| --- | --- | --- |
| H4 | After power loss or a snapshot write error, a pending manual station effect can block both operator decisions. | `app/anodizing_web/executor.py` |
| H5 | Stop after a basket power cut can leave both decisions blocked because the pending basket effect hides stopped AGV feedback. | `app/anodizing_web/executor.py`, `app/anodizing_web/programme.py` |
| H6 | Katana can acknowledge arrival while the returned manual job still has a second station effect pending. | `app/anodizing_web/executor_link.py`, `app/anodizing_web/executor_http.py` |
| H7 | An inconclusive original-job lookup can lose the reference needed to recover a measured dip after Resume or Abort. | `app/anodizing_web/programme.py` |
| H8 | When the final lift reaches a different released hold, the basket record can omit the measured dwell. | `app/anodizing_web/programme.py`, `app/anodizing_web/executor.py` |
| H10 | A manual witness published before a directory-sync failure can trigger a technician check after the operator has resolved the visit. | `app/anodizing_web/executor_link.py` |
| H11 | A failed Start receipt can block the idle standalone programme without exposing a check the operator can release. | `app/anodizing_web/server.py`, `app/anodizing_web/programme.py` |
| H12 | Power loss during a wire-log append can join two entries into one unreadable line without raising Operator check. | `app/anodizing_web/executor_http.py`, `app/anodizing_web/executor.py` |
| H13 | A lost manual submission can attach its check to a completed basket and leave the manual panel unable to release it. | `app/anodizing_web/executor.py`, `app/anodizing_web/executor_link.py` |

Paths are relative to `src/anodizing-modules/`.
