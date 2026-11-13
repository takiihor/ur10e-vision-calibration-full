# UI copy pass — final copy

Short action sentences replace repeated instructions. Closed Details sections contain repeated checks and long sequences. Visible copy retains the joint-path height/curve warning, endpoint-only workspace scope, TCP and clear-area checks, and unverified MCU replies. This is a plain-language pass, not certified ASD dictionary compliance. IDs, listeners and approval conditions remain unchanged.

## Single-operation setup checks

Before:

```html
<p id="manual-setup-help" class="hint">Check the active TCP against this teaching and calibration. <br>The tool and item must fit the 390 × 80 × 100 mm tool box. <br>Tool, camera and TCP mounting and support height must be unchanged. <br>The marker may move between operations. <br>Both tank01 roles reuse the original taught offset. <br>Review the scene, bounds and workspace. Clear the path and area. <br>Approve the fresh route and confirm grip or release separately.</p>
```

After:

```html
<div id="manual-setup-help" class="hint">Check the TCP against the teaching and calibration. The tool and item must fit the 390 × 80 × 100 mm box.<details><summary>Setup checks</summary><ul><li>Keep tool, camera and TCP mounting fixed. Keep support height unchanged.</li><li>The marker may move between operations. Both tank01 roles use the taught offset.</li><li>Check the scene, bounds and workspace. Clear the path. Approve the fresh plan. Check grip or release separately.</li></ul></details></div>
```

## Separate descent checks

Before:

```html
<p id="descent-setup-help" class="hint">Check the active TCP against this teaching and calibration. <br>The tool and item must fit the selected tool box. <br>The marker, dummy and rod must not have moved since alignment. Support height must be unchanged. <br>Review the taught height, vertical line, workspace and scene. Clear the area for this separate descent. This action does not grip or release.</p>
```

After:

```html
<div id="descent-setup-help" class="hint">Check the active TCP and tool box. Keep the marker, dummy, rod and support height unchanged since alignment.<details><summary>Descent checks</summary><p>Check the taught height, vertical line, workspace and scene. Clear the area. This descent does not grip or release.</p></details></div>
```

## Robot steps — current visible lead

```html
<details id="robot-card"><summary>Robot steps</summary>
<p>+/− prepares an endpoint. Approve it in Current operation.</p><ul><li>XYZ uses robot-base axes.</li><li>Rotation uses tool axes. The TCP endpoint position stays fixed.</li><li>The joint path can curve and change height or orientation.</li></ul><details><summary>Motion limits</summary><p>Workspace bounds apply to the start and destination. After approval, the IPC checks the whole joint path before movement. Only the endpoint is specified.</p></details>

```

## STEP exact-plan review

The visible review shows axis/increment, joint speed and both TCP poses with units. It states that the IPC checks the path after approval and that the path can curve or change TCP height/orientation. Frozen joints, TCP offset, model/scene hashes and bounds remain in closed Operation details. No taught-item or grip text is used for STEP.
