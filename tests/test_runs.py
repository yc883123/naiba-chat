from __future__ import annotations

import gc
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from async_tasks import ConversationRunManager, _validate_completion
from storage import ChatStorage


class RunStorageTests(unittest.TestCase):
    def make_storage(self, root: str) -> ChatStorage:
        return ChatStorage(Path(root) / "chat.db")

    def test_chat_run_creation_is_atomic_and_freezes_history(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            storage = self.make_storage(root)
            conversation = storage.create_conversation()
            storage.add_message(conversation["id"], "assistant", "earlier", {})
            run, history = storage.create_chat_run(
                conversation["id"],
                "new request",
                [{"name": "image.png", "path": "workspace/image.png", "size": 12}],
                {"id": "agent", "name": "Agent"},
                {"interaction_mode": "craft", "marker": "frozen"},
                "craft",
            )

            self.assertEqual("queued", run["status"])
            self.assertEqual("new request", history[-1]["content"])
            self.assertEqual(run["id"], history[-1]["metadata"]["run_id"])
            frozen = storage.get_run_snapshot(run["id"])
            self.assertEqual("frozen", frozen["marker"])
            self.assertEqual(history, frozen["conversation_messages"])

            storage.add_message(conversation["id"], "assistant", "later", {})
            self.assertEqual(history, storage.get_run_snapshot(run["id"])["conversation_messages"])
            del storage
            gc.collect()

    def test_one_active_run_per_conversation_but_other_conversations_can_run(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            storage = self.make_storage(root)
            first = storage.create_conversation()
            second = storage.create_conversation()
            run = storage.create_run(first["id"], "one", {}, {})

            with self.assertRaisesRegex(RuntimeError, f"ACTIVE_RUN:{run['id']}"):
                storage.create_run(first["id"], "two", {}, {})

            other = storage.create_run(second["id"], "parallel", {}, {})
            self.assertEqual(second["id"], other["conversation_id"])
            self.assertEqual(2, len(storage.list_background_tasks(active_only=True)))
            del storage
            gc.collect()

    def test_run_events_are_persistent_and_replay_from_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            storage = self.make_storage(root)
            conversation = storage.create_conversation()
            run = storage.create_run(conversation["id"], "event test", {}, {})
            first = storage.append_run_event(run["id"], {"type": "status", "message": "start"})
            second = storage.append_run_event(run["id"], {"type": "delta", "content": "hello"})

            self.assertEqual(1, first["sequence"])
            self.assertEqual(2, second["sequence"])
            self.assertEqual([second], storage.list_run_events(run["id"], after=1))
            del storage
            gc.collect()

    def test_restart_marks_active_run_interrupted_and_appends_terminal_event(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            storage = self.make_storage(root)
            conversation = storage.create_conversation()
            run = storage.create_run(conversation["id"], "interrupted", {}, {})
            storage.append_run_event(run["id"], {"type": "status", "message": "running"})
            del storage
            gc.collect()

            reopened = self.make_storage(root)
            recovered = reopened.get_background_task(run["id"])
            events = reopened.list_run_events(run["id"])
            self.assertEqual("interrupted", recovered["status"])
            self.assertIn("服务重启", recovered["error"])
            self.assertEqual("error", events[-1]["type"])
            self.assertIn("服务重启", events[-1]["message"])
            del reopened
            gc.collect()

    def test_confirmation_must_belong_to_waiting_run(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            storage = self.make_storage(root)
            conversation = storage.create_conversation()
            run = storage.create_run(conversation["id"], "confirm", {}, {})
            storage.update_background_task(
                run["id"], status="waiting", detail={"confirm_id": "confirm-1"}
            )
            manager = ConversationRunManager(SimpleNamespace(storage=storage))

            self.assertTrue(manager.owns_confirmation(run["id"], "confirm-1"))
            self.assertFalse(manager.owns_confirmation(run["id"], "other"))
            storage.update_background_task(run["id"], status="running")
            self.assertFalse(manager.owns_confirmation(run["id"], "confirm-1"))
            del manager
            del storage
            gc.collect()


class CompletionValidationTests(unittest.TestCase):
    @staticmethod
    def run_with_artifacts(artifacts):
        return [{"success": True, "result": json.dumps({"artifacts": artifacts})}]

    def test_png_cannot_satisfy_two_requested_videos(self) -> None:
        result = _validate_completion(
            {"kind": "video", "count": 2, "duration_min_sec": 0, "duration_max_sec": 0},
            self.run_with_artifacts([{"kind": "images", "filename": "preview.png", "local_path": "preview.png"}]),
        )

        self.assertFalse(result["passed"])
        self.assertTrue(any("PNG/JPG" in issue for issue in result["issues"]))
        self.assertTrue(any("需要 2 个" in issue for issue in result["issues"]))

    def test_two_existing_videos_with_valid_durations_pass(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            first = Path(root) / "segment-1.mp4"
            second = Path(root) / "segment-2.mp4"
            first.write_bytes(b"video")
            second.write_bytes(b"video")
            artifacts = [
                {"kind": "videos", "filename": first.name, "local_path": str(first)},
                {"kind": "videos", "filename": second.name, "local_path": str(second)},
            ]
            with patch("async_tasks._probe_video_duration", return_value=(15.1, None)):
                result = _validate_completion(
                    {"kind": "video", "count": 2, "duration_min_sec": 15, "duration_max_sec": 15},
                    self.run_with_artifacts(artifacts),
                )

        self.assertTrue(result["passed"])
        self.assertEqual([15.1, 15.1], [item["duration_sec"] for item in result["artifacts"]])

    def test_video_without_artifact_fails(self) -> None:
        result = _validate_completion(
            {"kind": "video", "count": 1, "duration_min_sec": 15, "duration_max_sec": 15},
            self.run_with_artifacts([]),
        )

        self.assertFalse(result["passed"])
        self.assertTrue(any("未找到" in issue for issue in result["issues"]))

    def test_wrong_duration_fails(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            video = Path(root) / "segment.mp4"
            video.write_bytes(b"video")
            with patch("async_tasks._probe_video_duration", return_value=(9.0, None)):
                result = _validate_completion(
                    {"kind": "video", "count": 1, "duration_min_sec": 15, "duration_max_sec": 15},
                    self.run_with_artifacts([{"kind": "videos", "filename": video.name, "local_path": str(video)}]),
                )

        self.assertFalse(result["passed"])
        self.assertTrue(any("时长 9.00 秒" in issue for issue in result["issues"]))


class RunFrontendTests(unittest.TestCase):
    def test_frontend_detaches_and_resumes_conversation_owned_runs(self) -> None:
        source = Path("public/app.js").read_text(encoding="utf-8")

        self.assertIn("function detachRunSubscription()", source)
        self.assertIn("async function resumeConversationRun(conversationId)", source)
        self.assertIn("/api/runs?conversation_id=", source)
        self.assertIn("/events?after=${state.runSequence}", source)
        self.assertIn("body: { run_id: runId, confirm_id: confirmId }", source)
        self.assertNotIn("data-cancel-task", source)
        self.assertNotIn("data-confirm-task", source)
        self.assertNotIn("data-reject-task", source)


if __name__ == "__main__":
    unittest.main()
