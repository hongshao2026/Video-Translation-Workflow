from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from scripts import workflow_runtime as runtime
from scripts.build_review_packet import build
from scripts.create_workflow_lock import build_payload, comparable, atomic_write_json
from scripts.workflow_poll_guard import evaluate, state_is_local


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        self.root = self.workspace / "fixture_run"
        self.root.mkdir()
        runtime.atomic(self.root / "qa/workflow_lock.json", {"status": "pass", "test_fixture": True})
        self.lock = {"path": "qa/workflow_lock.json", "sha256": runtime.sha(self.root / "qa/workflow_lock.json"), "status": "pass"}

    def step(self, name, code=None, **fields):
        code = code or f"from pathlib import Path; Path('{name}.txt').write_text('done')"
        return {"id": name, "kind": "machine", "command": [sys.executable, "-c", code],
                "outputs": [{"path": name + ".txt"}], "timeout_seconds": 10, **fields}

    def prepare(self, steps, **state_fields):
        plan = {"schema_version": 1, "job_id": "fixture", "workflow_lock": self.lock, "steps": steps}
        runtime.validate_plan(plan, self.root)
        job = self.root / "runtime/jobs/fixture"
        runtime.atomic(job / "plan.json", plan)
        state = {"job_id": "fixture", "status": "starting", "run_dir": str(self.root),
                 "workspace": str(self.workspace), "steps": {}, "notify_thread": None,
                 "plan_sha256": runtime.sha(job / "plan.json"), **state_fields}
        runtime.save(job, state)
        return job

    def test_chain_runs_without_model_calls_and_checkpoint_reuses(self):
        job = self.prepare([self.step("first"), self.step("second", requires=[{"path": "first.txt"}])])
        runtime.worker(job)
        before = (self.root / "first.txt").stat().st_mtime_ns
        state = runtime.read(job / "state.json")
        self.assertEqual(state["status"], "completed")
        event = runtime.read(Path(state["event_path"]))
        self.assertEqual(event["delivery"]["status"], "manual")
        runtime.worker(job)
        self.assertEqual(before, (self.root / "first.txt").stat().st_mtime_ns)
        self.assertEqual(len(list((job / "events").glob("*.json"))), 1)

    def test_detached_start_and_barrier_resume_finish_locally(self):
        barrier = {"id": "semantic", "kind": "agent", "receipt": "qa/decision.json", "outputs": []}
        plan = {"schema_version": 1, "job_id": "background_fixture", "workflow_lock": self.lock,
                "steps": [self.step("first"), barrier, self.step("second")]}
        plan_path = self.root / "machine_plan.json"
        runtime.atomic(plan_path, plan)
        result = runtime.start(self.root, plan_path, self.workspace, None, True)
        self.assertIsNone(result["media_session_id"])
        job = Path(result["state_path"]).parent

        def local_completion():
            # This is local test supervision, not a model/tool polling loop.
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                state = runtime.read(job / "state.json")
                if state["status"] in runtime.TERMINAL:
                    try:
                        with runtime.exclusive(job / "worker.lock"):
                            return state
                    except RuntimeError:
                        pass
                time.sleep(0.03)
            self.fail("Background worker did not finish: " + (job / "worker.log").read_text(encoding="utf-8", errors="replace") + repr(state))

        state = local_completion()
        self.assertEqual(state["status"], "needs_agent")
        event = runtime.read(Path(state["event_path"]))
        runtime.atomic(self.root / barrier["receipt"], {"status": "pass", "event_id": event["event_id"], "plan_sha256": state["plan_sha256"]})
        runtime.resume(job, None)
        result = local_completion()
        diagnostic = runtime.read(job / "diagnostic.json") if (job / "diagnostic.json").is_file() else {}
        self.assertEqual(result["status"], "completed", diagnostic)

    def test_old_or_duplicate_worker_dispatch_does_not_execute(self):
        job = self.prepare([self.step("first")], dispatch_id="new")
        runtime.worker(job, "old")
        self.assertFalse((self.root / "first.txt").exists())
        runtime.worker(job, "new")
        stamp = (self.root / "first.txt").stat().st_mtime_ns
        runtime.worker(job, "new")
        self.assertEqual(stamp, (self.root / "first.txt").stat().st_mtime_ns)

    def test_failed_output_does_not_run_downstream(self):
        job = self.prepare([self.step("missing", "pass"), self.step("downstream")])
        runtime.worker(job)
        self.assertEqual(runtime.read(job / "state.json")["status"], "failed")
        self.assertFalse((self.root / "downstream.txt").exists())

    def test_changed_cached_input_stops(self):
        (self.root / "input.txt").write_text("old")
        job = self.prepare([self.step("first", requires=[{"path": "input.txt"}])])
        runtime.worker(job)
        (self.root / "input.txt").write_text("new")
        runtime.worker(job)
        self.assertEqual(runtime.read(job / "state.json")["status"], "failed")

    def test_barrier_requires_matching_receipt_and_preserves_checkpoint(self):
        barrier = {"id": "semantic", "kind": "agent", "receipt": "qa/decision.json", "outputs": []}
        job = self.prepare([self.step("first"), barrier, self.step("second")])
        runtime.worker(job)
        state = runtime.read(job / "state.json")
        self.assertEqual(state["status"], "needs_agent")
        with self.assertRaisesRegex(ValueError, "decision receipt"):
            runtime.resume(job, None)
        event = runtime.read(Path(state["event_path"]))
        runtime.atomic(self.root / barrier["receipt"], {"status": "pass", "event_id": event["event_id"], "plan_sha256": state["plan_sha256"]})
        runtime.worker(job)
        self.assertEqual(runtime.read(job / "state.json")["status"], "completed")
        runtime.worker(job)
        self.assertEqual(runtime.read(job / "state.json")["status"], "completed")

    def test_bounded_idempotent_retry_is_local(self):
        code = "from pathlib import Path; p=Path('attempts'); n=int(p.read_text())+1 if p.exists() else 1; p.write_text(str(n)); Path('retry.txt').write_text('done') if n==2 else None; raise SystemExit(0 if n==2 else 3)"
        job = self.prepare([self.step("retry", code, idempotent=True, max_attempts=2)])
        with patch.object(runtime.time, "sleep"):
            runtime.worker(job)
        self.assertEqual(runtime.read(job / "state.json")["status"], "completed")
        self.assertEqual((self.root / "attempts").read_text(), "2")

    def test_paid_failure_is_uncertain_and_cannot_retry(self):
        guards = []
        for role in ["translation_gate", "full_generation_authorization", "dry_run"]:
            path = "qa/" + role + ".json"
            runtime.atomic(self.root / path, {"status": "pass", "test_fixture": True})
            guards.append({"path": path, "role": role, "status": "pass"})
        paid = self.step("paid", "raise SystemExit(3)", effects="paid_tts", requires=guards, executor_enforces_project_gates=True)
        job = self.prepare([paid])
        runtime.worker(job)
        self.assertEqual(runtime.read(job / "state.json")["status"], "uncertain")
        with self.assertRaisesRegex(ValueError, "Uncertain"):
            runtime.resume(job, None)
        with self.assertRaisesRegex(ValueError, "paid TTS"):
            runtime.validate_plan({"schema_version": 1, "job_id": "invalid", "workflow_lock": self.lock,
                                   "steps": [{**paid, "max_attempts": 2, "idempotent": True}]}, self.root)

    def test_notify_queues_once_and_does_not_claim_consumption(self):
        job = self.prepare([self.step("first")], notify_thread=str(uuid.uuid4()), codex_executable="codex")
        with patch.object(runtime.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as send:
            runtime.worker(job)
            state = runtime.read(job / "state.json")
            runtime.notify(job, Path(state["event_path"]))
        self.assertEqual(send.call_count, 1)
        event = runtime.read(Path(state["event_path"]))
        self.assertEqual(event["delivery"]["status"], "accepted")
        self.assertFalse(event["delivery"]["consumption_confirmed"])

    def audition_guards(self, render=False):
        authorization = {"status": "pass", "scope": "one_minute_audition_only", "full_tts_authorized": False}
        for field in ("user_authorization", "voice_selection", "voice_selection_validation",
                      "translation_gate", "audition_items", "audition_validation"):
            path = "qa/" + field + ".json"
            runtime.atomic(self.root / path, {"status": "pass", "test_fixture": field})
            authorization[field] = {"path": path, "sha256": runtime.sha(self.root / path)}
        auth_path = "qa/audition_authorization.json"
        runtime.atomic(self.root / auth_path, authorization)
        guards = [{"path": auth_path, "sha256": runtime.sha(self.root / auth_path),
                   "status": "pass", "role": "audition_authorization"},
                  {**authorization["translation_gate"], "status": "pass", "role": "translation_gate"}]
        if not render:
            guards.append({**authorization["voice_selection_validation"], "status": "pass", "role": "voice_selection_lock"})
        role = "audition_production_gate" if render else "dry_run"
        path = "qa/" + role + ".json"
        runtime.atomic(self.root / path, {"status": "pass", "test_fixture": True})
        guards.append({"path": path, "status": "pass", "role": role})
        return guards

    def test_scoped_audition_accepts_selected_window_but_not_full_tts(self):
        guards = self.audition_guards()
        paid = self.step("audition", effects="paid_audition", requires=guards, executor_enforces_project_gates=True)
        job = self.prepare([paid])
        runtime.worker(job)
        self.assertEqual(runtime.read(job / "state.json")["status"], "completed")
        with self.assertRaisesRegex(ValueError, "Missing production preconditions"):
            self.prepare([{**paid, "effects": "paid_tts"}])
        with self.assertRaisesRegex(ValueError, "Missing production preconditions"):
            self.prepare([{**paid, "requires": guards[1:]}])

    def test_audition_rejects_changed_authorization_scope_or_frozen_text(self):
        for mutation in ({"scope": "full_generation"}, {"full_tts_authorized": True}):
            guards = self.audition_guards()
            path = self.root / guards[0]["path"]
            runtime.atomic(path, {**runtime.read(path), **mutation})
            guards[0]["sha256"] = runtime.sha(path)
            with self.assertRaisesRegex(ValueError, "scope"):
                self.prepare([self.step("audition", effects="paid_audition", requires=guards, executor_enforces_project_gates=True)])
        guards = self.audition_guards()
        runtime.atomic(self.root / "qa/audition_items.json", {"status": "pass", "changed": True})
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.prepare([self.step("audition", effects="paid_audition", requires=guards, executor_enforces_project_gates=True)])

    def test_paid_audition_failure_and_interruption_cannot_retry(self):
        guards = self.audition_guards()
        paid = self.step("audition", "raise SystemExit(3)", effects="paid_audition", requires=guards, executor_enforces_project_gates=True)
        job = self.prepare([paid, self.step("downstream")])
        runtime.worker(job)
        self.assertEqual(runtime.read(job / "state.json")["status"], "uncertain")
        self.assertFalse((self.root / "downstream.txt").exists())
        with self.assertRaisesRegex(ValueError, "Uncertain"):
            runtime.resume(job, None)
        with self.assertRaisesRegex(ValueError, "paid TTS"):
            self.prepare([{**paid, "max_attempts": 2, "idempotent": True}])
        state = runtime.read(job / "state.json")
        state["steps"]["audition"]["status"] = "running"
        runtime.save(job, state)
        with patch.object(runtime, "command_run") as execute:
            runtime.worker(job)
            execute.assert_not_called()
        self.assertEqual(runtime.read(job / "state.json")["status"], "uncertain")

    def test_audition_render_requires_window_production_gate(self):
        guards = self.audition_guards(render=True)
        render = self.step("render", effects="audition_render", requires=guards, executor_enforces_project_gates=True)
        job = self.prepare([render])
        runtime.worker(job)
        self.assertEqual(runtime.read(job / "state.json")["status"], "completed")
        wrong = [{**spec, "role": "production_gate"} if spec["role"] == "audition_production_gate" else spec for spec in guards]
        with self.assertRaisesRegex(ValueError, "Missing production preconditions"):
            self.prepare([{**render, "requires": wrong}])

    def test_notification_timeout_is_not_retried(self):
        job = self.prepare([self.step("first")], notify_thread=str(uuid.uuid4()), codex_executable="codex")
        with patch.object(runtime.subprocess, "run", side_effect=subprocess.TimeoutExpired("codex", 30)) as send:
            runtime.worker(job)
            path = Path(runtime.read(job / "state.json")["event_path"])
            runtime.notify(job, path)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(runtime.read(path)["delivery"]["status"], "uncertain")

    def test_unchanged_recovery_evidence_rejected(self):
        job = self.prepare([self.step("broken", "raise SystemExit(2)")])
        runtime.worker(job)
        evidence = self.root / "repair.json"
        evidence.write_text('{"action":"fixture repair"}')
        with patch.object(runtime, "launch", return_value=123):
            runtime.resume(job, evidence)
            with self.assertRaisesRegex(ValueError, "Unchanged"):
                runtime.resume(job, evidence)

    def test_paths_cannot_escape_run(self):
        with self.assertRaisesRegex(ValueError, "inside"):
            self.prepare([self.step("bad", outputs=[{"path": "../outside.txt"}])])

    def test_running_guard_blocks_probe_and_allows_one_user_query(self):
        session = str(uuid.uuid4())
        job = self.prepare([self.step("first")], notify_thread=session)
        state = runtime.read(job / "state.json")
        state["status"] = "running"
        runtime.save(job, state)
        base = {"cwd": str(self.workspace), "session_id": session, "turn_id": "t1"}
        probe = {**base, "hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "Get-Process ffmpeg"}}
        self.assertEqual(evaluate(probe)["hookSpecificOutput"]["permissionDecision"], "deny")
        evaluate({**base, "hook_event_name": "UserPromptSubmit", "prompt": "现在进度怎么样了"})
        self.assertEqual(evaluate(probe), {})
        self.assertIn("hookSpecificOutput", evaluate(probe))
        allowed = {**probe, "tool_input": {"command": "python -m unittest discover"}}
        self.assertEqual(evaluate(allowed), {})

    def test_review_projection_is_verbatim_and_source_mode_rejects_old_chinese(self):
        source = self.root / "candidate.json"
        source.write_text(json.dumps({"slots": [{"id": 0, "source_text": "hello", "subtitle_zh": "你好。", "words": [{"word": "hello"}]}]}), encoding="utf-8")
        output = self.root / "review.jsonl"
        manifest = build(source, output, "review")
        row = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(row["subtitle_zh"], "你好。")
        self.assertNotIn("words", row)
        self.assertEqual(manifest["slot_count"], 1)
        with self.assertRaisesRegex(ValueError, "historical"):
            build(source, self.root / "source.jsonl", "source")

    def test_guard_handles_linked_media_run_without_reading_arbitrary_external_state(self):
        with tempfile.TemporaryDirectory() as external:
            media = Path(external).resolve()
            linked = self.workspace / "linked_run"
            if os.name == "nt":
                quote = lambda p: "'" + str(p).replace("'", "''") + "'"
                subprocess.run(["powershell", "-NoProfile", "-Command",
                                "New-Item -ItemType Junction -Path " + quote(linked)
                                + " -Target " + quote(media) + " | Out-Null"], check=True)
            else:
                linked.symlink_to(media, target_is_directory=True)
            try:
                session = str(uuid.uuid4())
                runtime.atomic(media / "qa/workflow_lock.json", {"status": "pass", "test_fixture": True})
                plan = {"schema_version": 1, "job_id": "fixture",
                        "workflow_lock": {"path": "qa/workflow_lock.json", "status": "pass",
                                          "sha256": runtime.sha(media / "qa/workflow_lock.json")},
                        "steps": [self.step("first")]}
                runtime.atomic(media / "plan.json", plan)
                with patch.object(runtime, "launch", return_value=123):
                    launched = runtime.start(media, media / "plan.json", self.workspace, None, True)
                self.assertIsNone(launched["media_session_id"])
                self.assertEqual(launched["worker_pid"], 123)
                with self.assertRaisesRegex(ValueError, "inside"):
                    runtime.start(media.parent / "unlinked", media / "plan.json", self.workspace, None, True)
                state_path = media / "runtime/jobs/fixture/state.json"
                runtime.atomic(state_path, {"status": "running"})
                runtime.atomic(self.workspace / ".codex/workflow/active" / (session + ".json"),
                               {"state_path": str(state_path)})
                probe = {"cwd": str(self.workspace), "session_id": session, "turn_id": "t1",
                         "hook_event_name": "PreToolUse", "tool_name": "Bash",
                         "tool_input": {"command": "Get-Process ffmpeg"}}
                self.assertEqual(evaluate(probe)["hookSpecificOutput"]["permissionDecision"], "deny")
                self.assertFalse(state_is_local(self.workspace, media / "arbitrary.json"))
                self.assertFalse(state_is_local(self.workspace, media.parent / "unlinked/runtime/jobs/x/state.json"))
            finally:
                # Remove only the link; never recursively remove its target.
                linked.rmdir() if os.name == "nt" else linked.unlink()

    def test_stage_progress_does_not_change_lock_comparison(self):
        repo = Path(__file__).resolve().parents[1]
        (self.root / "PROJECT.md").write_text("# Fixture\nconstraint=1\n\n## 当前状态\nstarted\n", encoding="utf-8")
        args = argparse.Namespace(run_dir=self.root, rules_root=repo, stage="intake", next_gate="download", frozen_input=[])
        first = build_payload(args)
        self.assertEqual(first["status"], "pass")
        args.stage, args.next_gate = "asr", "translation"
        (self.root / "PROJECT.md").write_text("# Fixture\nconstraint=1\n\n## 当前状态\nadvanced\n", encoding="utf-8")
        second = build_payload(args)
        self.assertEqual(comparable(first), comparable(second))
        atomic_write_json(self.root / "qa/workflow_lock.json", first)
        self.lock["sha256"] = runtime.sha(self.root / "qa/workflow_lock.json")
        job = self.prepare([self.step("first")])
        (self.root / "PROJECT.md").write_text("# Changed constraint\n", encoding="utf-8")
        runtime.worker(job)
        self.assertEqual(runtime.read(job / "state.json")["status"], "failed")


if __name__ == "__main__":
    unittest.main()
