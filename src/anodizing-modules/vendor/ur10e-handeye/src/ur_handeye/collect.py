"""Calibration data collection: automatic views, operator-positioned views, or a replayed path.

Safety model: every view is planned (IK near the previous joints, controller safety limits,
per-move joint-step limit, travel limit from the start pose) *before* the first motion, and the
operator confirms the complete plan. Errors or Ctrl+C request a stop; the collector never makes
a recovery motion on its own, and the physical robot state must still be checked.
"""
import itertools
import math
import time
from dataclasses import dataclass, field

import numpy as np

from .poses import PlanOptions, View, estimate_camera_rotation, plan_views, probe_targets
from .robot import RobotCleanupError, RobotError
from .solver import MIN_SAMPLES, SolveError, _Problem, closed_form, refine
from .transforms import matrix, matrix_to_pose
from .vision import Observation


class CollectError(RuntimeError):
    pass


@dataclass(frozen=True)
class CollectOptions:
    frames: int = 5
    max_jitter_px: float = 0.3
    plan: PlanOptions = field(default_factory=PlanOptions)
    #: Re-plan the remaining views with an interim calibration after this many usable samples (0: never).
    bootstrap_after: int = 6
    #: Camera translation in the flange used for planning before calibration (metres); rotation is probed.
    translation_guess_m: tuple[float, float, float] = (0.0, 0.0, 0.0)
    #: Largest joint motion allowed while a sample is captured (robot must be at rest).
    max_capture_motion_deg: float = 0.02

    def __post_init__(self):
        if type(self.frames) is not int or not 1 <= self.frames <= 300:
            raise ValueError("frames must be an integer in 1..300")
        for name in ("max_jitter_px", "max_capture_motion_deg"):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if type(self.bootstrap_after) is not int or self.bootstrap_after < 0:
            raise ValueError("bootstrap_after must be a nonnegative integer")
        if len(self.translation_guess_m) != 3 or not np.isfinite(self.translation_guess_m).all():
            raise ValueError("translation_guess_m needs three finite values")


@dataclass
class PlannedView:
    label: str
    flange: np.ndarray
    joints: list[float]
    #: Go back to the start pose first, because a direct move would exceed the joint-step limit.
    via_start: bool = False


def _step(a, b) -> float:
    return max(abs(x - y) for x, y in zip(a, b, strict=True))


class Collector:
    def __init__(self, robot, observer, session, options: CollectOptions = CollectOptions(), *,
                 log=print, confirm=lambda summary: False, on_capture=None, on_cleanup_failure=None):
        self.robot, self.observer, self.session, self.options = robot, observer, session, options
        self.log, self.confirm = log, confirm
        self.on_capture = on_capture
        self.on_cleanup_failure = on_cleanup_failure
        self.usable = 0
        self.intrinsics = None
        self.collision_checks = []
        self.collision_candidates = []

    def _check_paths(self, segments, *, check_segment=None):
        collision = getattr(self.robot, 'collision', None)
        if collision is None:
            return
        from .collision import check_plan
        result = check_plan(collision, segments, check_segment=check_segment)
        self.collision_checks.append(result)
        self.session.update_metadata(collision_checks=self.collision_checks)
        if hasattr(self.session, 'directory'):
            from .dataset import write_new_json
            write_new_json(self.session.directory / f'collision-plan-{len(self.collision_checks):03d}.json', result)
        for path in result['segments']:
            if path['status'] != 'clear_in_model':
                pair = ' / '.join(path.get('pair', []))
                raise CollectError(f"The path check failed for {path['label']}: {path['status']}. Check {pair}.")

    # -- capture -----------------------------------------------------------------------------
    def capture(self, label: str) -> tuple[dict, Observation]:
        self.robot.check_ready()
        capture_state = None
        capture_reasons = set()
        self.observer.captured_frames = []
        observation = None
        joints = flange = None
        try:
            if hasattr(self.robot, "capture_snapshot"):
                capture_state = self.robot.capture_snapshot(after=time.monotonic())
                joints, flange = list(capture_state.joints), capture_state.flange

                def monitor():
                    capture_reasons.update(self.robot.capture_motion(
                        capture_state, math.radians(self.options.max_capture_motion_deg)))

                monitor()
                observation = self.observer.observe(self.options.frames, monitor=monitor)
                self.robot.capture_snapshot(after=time.monotonic())
                monitor()
            else:
                joints = self.robot.joints()
                flange = self.robot.flange_pose()
                def monitor():
                    self.robot.check_ready()
                    current = self.robot.joints()
                    if max(abs(a - b) for a, b in zip(joints, current, strict=True)) > math.radians(
                            self.options.max_capture_motion_deg):
                        capture_reasons.add("robot_moved_during_capture")
                    if not np.allclose(self.robot.tcp_offset(), self.session.metadata["tcp_offset"], rtol=0, atol=1e-9):
                        capture_reasons.add("tcp_changed_during_capture")

                observation = self.observer.observe(self.options.frames, monitor=monitor)
            after = self.robot.joints()
            self.robot.check_ready()
        except BaseException as exc:
            captured = self.observer.captured_frames
            if captured:
                failed = {"label": label, "status": "capture_error",
                          "reasons": [f"{type(exc).__name__}: {exc}"], "joints": joints,
                          "flange_pose": matrix_to_pose(flange) if flange is not None else None,
                          "observation": observation.to_record() if observation is not None else None}
                try:
                    self.session.add(failed, captured[0][0].bgr, captured)
                except Exception as save_exc:
                    self.log(f"WARNING: could not save partial capture evidence: {save_exc}")
            raise
        usable, reasons = observation.usable(self.options.max_jitter_px)
        if capture_reasons:
            usable, reasons = False, [*reasons, *sorted(capture_reasons)]
        motion = max(abs(a - b) for a, b in zip(joints, after, strict=True))
        if motion > math.radians(self.options.max_capture_motion_deg):
            usable, reasons = False, [*reasons, "robot_moved_during_capture"]
        if self.intrinsics is None and observation.intrinsics:
            self.intrinsics = observation.intrinsics
            self.session.update_metadata(intrinsics=self.intrinsics)
        elif observation.intrinsics and observation.intrinsics != self.intrinsics:
            usable, reasons = False, [*reasons, "intrinsics_changed"]
        record = {"label": label, "status": "ok" if usable else "rejected", "reasons": reasons,
                  "joints": joints, "flange_pose": matrix_to_pose(flange),
                  "observation": observation.to_record()}
        if capture_state is not None:
            record["robot_state"] = {"source": "primary_read_only", "port": 30011,
                                     "timestamp_us": capture_state.timestamp_us,
                                     "tcp_pose": list(capture_state.tcp_pose),
                                     "tcp_offset": list(capture_state.tcp_offset)}
        index = self.session.add(record, observation.image_bgr, observation.frames)
        record["index"] = index
        self.usable += usable
        pose = observation.pose
        detail = (f"{len(observation.charuco_ids)} corners, rmse {pose['reprojection_rmse_px']:.2f} px, "
                  f"jitter {observation.jitter_px or 0:.2f} px" if pose else f"{len(observation.charuco_ids)} corners")
        self.log(f"  [{index:02d}] {label:<10} {'ok' if usable else 'REJECTED ' + ','.join(reasons)}  ({detail})")
        if self.on_capture is not None:
            self.on_capture(record, observation)
        return record, observation

    def _move(self, joints: list[float]) -> None:
        try:
            self.robot.move_joints(joints)
        except BaseException as exc:
            self._halt(report_success=not isinstance(exc, RobotCleanupError))
            raise

    def _halt(self, *, report_success=True) -> None:
        try:
            self.robot.stop()
        except Exception as exc:
            message = f"stop command failed: {exc}"
            if self.on_cleanup_failure:
                self.on_cleanup_failure(message)
            self.log(f"WARNING: {message}")
        else:
            if report_success:
                self.log("Stop request completed. Check the robot and cell before moving again.")

    # -- planning ----------------------------------------------------------------------------
    def _collision_checker(self):
        collision = getattr(self.robot, 'collision', None)
        checked, attempts = {}, []
        def certificate(a, b):
            key = (tuple(a), tuple(b))
            if key not in checked:
                checked[key] = collision.check_segment(a, b)
            return checked[key]
        def clear(label, a, b):
            if _step(a, b) > self.robot.limits.max_joint_step_rad:
                return False, 'joint step exceeds the native limit'
            if collision is None:
                return True, ''
            self.log(f'Collision check: {label}')
            result = certificate(a, b)
            attempts.append((label, list(a), list(b)))
            status = result['status']
            reason = status + ('; ' + ' / '.join(result['pair']) if result.get('pair') else '')
            self.log(f'  {label}: {reason}')
            return status == 'clear_in_model', reason
        return certificate, clear, attempts

    def _checked_transition(self, label, current, target, start, clear):
        direct, reason = clear(label + ' direct', current, target)
        if direct:
            return False
        usable, reason = clear(label + ' via start return', current, start)
        if usable:
            usable, reason = clear(label + ' via start outbound', start, target)
        if not usable:
            raise CollectError('No checked path for ' + label + ': ' + reason)
        return True

    def _record_candidates(self, attempts, certificate):
        collision = getattr(self.robot, 'collision', None)
        if collision is not None:
            from .collision import check_plan
            result = check_plan(collision, attempts, check_segment=certificate)
            self.collision_candidates.append(result)
            self.session.update_metadata(collision_candidates=self.collision_candidates)
            if hasattr(self.session, 'directory'):
                from .dataset import write_new_json
                write_new_json(self.session.directory / f'collision-candidates-{len(self.collision_candidates):03d}.json', result)

    def plan(self, views: list[View], start_flange: np.ndarray, q_start: list[float], *,
             q_from: list[float] | None = None, return_each: bool = False) -> tuple[list[PlannedView], list]:
        """IK-check every view from the start joints, then order them to keep each move short.

        Solving every view from q_start keeps one arm configuration throughout (chained IK lets wrist
        changes accumulate). Views are visited nearest-first in joint space; where a direct move
        would still exceed the joint-step limit, the path goes back through the start pose, which
        every planned view is within one step of by construction.
        """
        planned, skipped = [], []
        for view in views:
            travel = float(np.linalg.norm(view.flange[:3, 3] - start_flange[:3, 3]))
            try:
                if travel > self.robot.limits.max_travel_m:
                    raise RobotError(f"{travel * 1000:.0f} mm from the start exceeds the "
                                     f"{self.robot.limits.max_travel_m * 1000:.0f} mm travel limit")
                q = self.robot.plan(view.flange, q_start)
            except RobotError as exc:
                skipped.append((view.label, str(exc)))
                continue
            planned.append(PlannedView(view.label, view.flange, q))
        collision = getattr(self.robot, 'collision', None)
        certificate, clear, attempts = self._collision_checker()
        ordered, current, remaining, segments = [], q_from or q_start, planned, []
        try:
            while remaining:
                nearest = min(remaining, key=lambda view: _step(current, view.joints))
                remaining = [view for view in remaining if view is not nearest]
                q = nearest.joints
                usable, reason = clear(nearest.label + ' endpoint', q, q)
                if usable:
                    usable, reason = clear(nearest.label + ' return', q, q_start)
                if not usable:
                    skipped.append((nearest.label, reason))
                    self.log(f'Skipped {nearest.label}: {reason}')
                    continue
                try:
                    via = self._checked_transition(nearest.label, current, q, q_start, clear)
                except CollectError as exc:
                    skipped.append((nearest.label, str(exc)))
                    self.log(f'Skipped {nearest.label}: {exc}')
                    continue
                if via:
                    segments.append(('via_start', list(current), q_start))
                    current = q_start
                nearest.via_start = via
                ordered.append(nearest)
                segments.append((nearest.label, list(current), q))
                current = q
                if return_each:
                    segments.append(('probe_return', current, q_start))
                    current = q_start
            if not return_each:
                usable, reason = clear('return_start', current, q_start)
                if not usable:
                    raise CollectError('The path check failed for return_start: ' + reason)
                segments.append(('return_start', list(current), q_start))
            self._check_paths(segments, check_segment=certificate if collision is not None else None)
        finally:
            self._record_candidates(attempts, certificate)
        return ordered, skipped

    def _summary(self, planned: list[PlannedView], skipped, start_flange, q_start) -> str:
        lines = [f"{len(planned)} views planned, {len(skipped)} skipped."]
        for label, reason in skipped:
            lines.append(f"  skipped {label}: {reason}")
        if planned:
            travel = max(np.linalg.norm(p.flange[:3, 3] - start_flange[:3, 3]) for p in planned) * 1000
            step = max(_step(p.joints, q_start) for p in planned)
            via = sum(p.via_start for p in planned)
            lines.append(f"  max flange travel from start {travel:.0f} mm; max joint change from start "
                         f"{math.degrees(step):.0f} deg; joint speed {self.robot.limits.joint_speed_rad_s:.2f} rad/s"
                         + (f"; {via} view(s) reached through the start pose" if via else ""))
        for view in planned:
            lines.append(f"  {view.label}: flange pose (m/rad) {np.round(matrix_to_pose(view.flange), 6).tolist()}; "
                         f"joints (deg) {np.round(np.degrees(view.joints), 2).tolist()}"
                         + (" via start" if view.via_start else ""))
        return "\n".join(lines)

    def _check_coverage(self, planned, records):
        from .solver import Thresholds, rotation_diversity_poses
        from .transforms import pose_to_matrix
        actual = [pose_to_matrix(record['flange_pose']) for record in records if record['status'] == 'ok']
        diversity = rotation_diversity_poses(actual + [view.flange for view in planned])
        limits = Thresholds()
        count = len(actual) + len(planned)
        self.session.update_metadata(planned_coverage={'sample_count': count, 'captured_samples': len(actual),
            'remaining_views': len(planned), 'rotation_diversity': diversity,
            'minimum_samples': MIN_SAMPLES + 2, 'min_rotation_range_deg': limits.min_rotation_range_deg,
            'min_axis_spread': limits.min_axis_spread})
        self.log(f"Planned coverage: {count} samples; rotation range {diversity['max_relative_rotation_deg']:.1f} deg; "
                 f"axis spread {diversity['axis_spread']:.3f}. This is a plan, not a calibration result.")
        if count < MIN_SAMPLES + 2:
            raise CollectError('Too few useful collision-clear views for the native sample requirement.')
        if (diversity['max_relative_rotation_deg'] < limits.min_rotation_range_deg
                or diversity['axis_spread'] < limits.min_axis_spread):
            raise CollectError('Insufficient planned rotation diversity; use a start pose with more safe view coverage.')

    def _interim(self, samples_records, corners) -> np.ndarray | None:
        from .dataset import samples_from_records
        samples = samples_from_records(samples_records)
        if len(samples) < MIN_SAMPLES:
            return None
        try:
            from charuco_realsense.calibration import opencv_calibration
            camera_matrix, coeffs = opencv_calibration(self.intrinsics)
            candidates = closed_form(samples)
            x, y = candidates.get("shah_robot_world") or next(iter(candidates.values()))
            x, _, _ = refine(_Problem(samples, corners, camera_matrix, coeffs), x, y)
            return x
        except (SolveError, ValueError):
            return None

    # -- modes -------------------------------------------------------------------------------
    def run_auto(self, *, dry_run: bool = False) -> int:
        robot, options = self.robot, self.options
        robot.check_ready()
        q_start, start_flange = robot.joints(), robot.flange_pose()
        self.session.update_metadata(start_joints=q_start, start_flange_pose=matrix_to_pose(start_flange),
                                     mode="auto")
        self.log("Start view:")
        record, start = self.capture("start")
        if record["status"] != "ok":
            raise CollectError("The board is not fully and sharply visible from the start pose "
                               f"({', '.join(record['reasons'])}). Jog the robot so the camera sees the whole "
                               "board from 0.3-0.6 m, then start again")
        records = [record]
        corners = self.observer.target.corners
        if dry_run:
            rotation = np.eye(3)
            self.log("Dry run: planning with the camera optical axis assumed along flange Z (no probe moves)")
        else:
            probe_plan, skipped = self.plan(probe_targets(start_flange, options.plan.probe_mm / 1000),
                                            start_flange, q_start, return_each=True)
            if len(probe_plan) < 3 or skipped:
                raise CollectError(f"Probe moves of {options.plan.probe_mm:g} mm are not possible here: {skipped}")
            summary = (f"Probe: 3 flange endpoint translations of {options.plan.probe_mm:g} mm to measure the camera "
                       "orientation, returning to the start after each. These are joint-space moves.\n"
                       + self._summary(probe_plan, skipped, start_flange, q_start))
            if not self.confirm(summary):
                raise CollectError("Cancelled by the operator before any motion")
            probes = []
            for planned in probe_plan:
                self._move(planned.joints)
                probe_record, observation = self.capture(planned.label)
                records.append(probe_record)
                if probe_record["status"] != "ok":
                    raise CollectError("Probe observation rejected; stopped without a recovery move: "
                                       + ", ".join(probe_record["reasons"]))
                probes.append((robot.flange_pose(), observation.board_in_camera))
                self._move(q_start)
            rotation, residual = estimate_camera_rotation(start_flange, start.board_in_camera, probes)
            self.log(f"Camera orientation measured from probes (fit residual {residual:.2f} mm)")
            if residual > 0.2 * options.plan.probe_mm:
                raise CollectError("Probe motion and camera motion disagree; is the camera rigidly mounted "
                                   "on the tool, and is the board fixed?")
        guess = matrix(rotation, options.translation_guess_m)
        views = plan_views(start_flange, guess, start.board_in_camera, self.observer.target.centre, options.plan)
        planned, skipped = self.plan(views, start_flange, q_start, q_from=robot.joints())
        summary = self._summary(planned, skipped, start_flange, q_start)
        self.log(summary)
        self.session.update_metadata(plan={"views": [p.label for p in planned], "skipped": skipped,
                                           "targets": [{"label": p.label, "joints": p.joints,
                                                        "flange_pose": matrix_to_pose(p.flange),
                                                        "via_start": p.via_start} for p in planned]})
        self._check_coverage(planned, records)
        if dry_run:
            return len(planned)
        if not self.confirm(summary + "\nMove through these views now?"):
            raise CollectError("Cancelled by the operator before the calibration moves")
        bootstrapped = options.bootstrap_after == 0
        position = 0
        while position < len(planned):
            view = planned[position]
            if view.via_start:
                self._move(q_start)
            self._move(view.joints)
            record, _ = self.capture(view.label)
            records.append(record)
            position += 1
            if not bootstrapped and self.usable >= options.bootstrap_after:
                bootstrapped = True
                interim = self._interim(records, corners)
                if interim is not None:
                    remaining = {p.label for p in planned[position:]}
                    better = [v for v in plan_views(start_flange, interim, start.board_in_camera,
                                                    self.observer.target.centre, options.plan)
                              if v.label in remaining]
                    replanned, skipped_again = self.plan(better, start_flange, q_start, q_from=robot.joints())
                    self._check_coverage(replanned, records)
                    if not self.confirm("Updated calibration plan:\n"
                                        + self._summary(replanned, skipped_again, start_flange, q_start)):
                        raise CollectError("Cancelled by the operator before the updated calibration moves")
                    planned = planned[:position] + replanned
                    self.log(f"Re-planned {len(replanned)} remaining views with the interim calibration"
                             + (f" ({len(skipped_again)} now unreachable)" if skipped_again else ""))
        self.log("Returning to the start pose")
        self._move(q_start)
        return self.usable

    def run_manual(self, read_line=input, *, freedrive: bool = False) -> int:
        """Operator positions the robot (pendant jog or freedrive); Enter captures, q finishes."""
        self.session.update_metadata(mode="manual")
        self.log("Manual mode: vary the view direction by 20-40 degrees about different axes between captures.")
        try:
            while True:
                if freedrive:
                    self.robot.set_freedrive(True)
                answer = read_line(f"[{self.usable} usable] Position the robot, then Enter to capture (q to finish): ")
                if freedrive:
                    self.robot.set_freedrive(False)  # capture with the arm held still, not compliant
                if answer.strip().lower() in ("q", "quit", "done"):
                    break
                self.capture(f"manual{self.session.count:02d}")
        finally:
            if freedrive:
                try:
                    self.robot.set_freedrive(False)
                except RobotError as exc:
                    self.log(f"WARNING: {exc}")
        return self.usable

    def run_replay(self, joint_targets: list[list[float]], *, dry_run: bool = False) -> int:
        """Repeat a previous session's views, e.g. for a periodic recalibration check."""
        self.robot.check_ready()
        q_start = self.robot.joints()
        start_flange = self.robot.flange_pose()
        self.session.update_metadata(mode="replay", start_joints=q_start)
        path = [q_start, *joint_targets, q_start]
        via_start = []
        segments = []
        certificate, clear, attempts = self._collision_checker()
        try:
            for index, (a, b) in enumerate(itertools.pairwise(path)):
                flange = self.robot.validate_joints(b)
                travel = float(np.linalg.norm(flange[:3, 3] - start_flange[:3, 3]))
                if travel > self.robot.limits.max_travel_m:
                    raise CollectError("Replay target exceeds the flange travel limit from the current start")
                label = 'return_start' if index == len(joint_targets) else 'replay'
                usable, reason = clear(label + ' endpoint', b, b)
                if not usable:
                    raise CollectError('Replay path refused: ' + reason)
                via = self._checked_transition(label, a, b, q_start, clear)
                via_start.append(via)
                if via:
                    segments.extend([('replay_via_start', a, q_start), (label, q_start, b)])
                else:
                    segments.append((label, a, b))
            self._check_paths(segments, check_segment=certificate)
        finally:
            self._record_candidates(attempts, certificate)
        if dry_run:
            self.log(f"Dry run: {len(joint_targets)} replay views checked; no motion")
            return len(joint_targets)
        self.session.update_metadata(replay_plan={"targets": joint_targets, "via_start": via_start})
        summary = (f"Replay {len(joint_targets)} recorded views and return to the current pose? "
                   f"{sum(via_start)} transition(s) go through the start pose to stay within joint limits.\n"
                   + "\n".join(f"  replay{number:02d}: joints (deg) {np.round(np.degrees(joints), 2).tolist()}"
                               + (" via start" if via_start[number] else "")
                               for number, joints in enumerate(joint_targets)))
        if not self.confirm(summary):
            raise CollectError("Cancelled by the operator before any motion")
        for number, joints in enumerate(joint_targets):
            if via_start[number]:
                self._move(q_start)
            self._move(joints)
            self.capture(f"replay{number:02d}")
        self._move(q_start)
        return self.usable
