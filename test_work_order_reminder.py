import csv
import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from pathlib import Path

import work_order_reminder as app


class ReminderTests(unittest.TestCase):
    def test_temporary_excel_lock_file_is_ignored(self):
        self.assertFalse(app.is_supported_input(Path("~$正在编辑.xlsx")))
        self.assertFalse(app.is_supported_input(Path(".hidden.xlsx")))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = {
            "paths": {
                "inbox": str(root / "inbox"),
                "archive": str(root / "archive"),
                "failed": str(root / "failed"),
                "state_db": str(root / "state" / "db.sqlite3"),
            },
            "excel": {
                "sheet": None,
                "header_row": 1,
                "handler_columns": ["当前处理人", "受理人"],
                "status_columns": ["工单状态"],
                "ticket_columns": ["工单流水号"],
                "excluded_statuses": ["已完成", "已关闭"],
            },
            "message": {
                "title": "待处理工单催办",
                "max_people_per_message": 2,
                "max_characters": 500,
                "name_aliases": {"小叶": "叶于琳"},
            },
            "delivery": {
                "enabled": False,
                "target": "group:test-group",
                "member_mapping_file": str(root / "members.json"),
            },
        }
        (root / "members.json").write_text(
            json.dumps(
                {
                    "test-group": {
                        "叶于琳": "user-1",
                        "李四": "user-2",
                        "王五": "user-3",
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        for path in self.config["paths"].values():
            if not path.endswith(".sqlite3"):
                Path(path).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.temp.cleanup()

    def make_csv(self) -> Path:
        path = Path(self.config["paths"]["inbox"]) / "orders.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["工单流水号", "当前处理人", "工单状态", "无关列"])
            writer.writerow(["001", "小叶", "处理中", "x"])
            writer.writerow(["002", "小叶", "待处理", "x"])
            writer.writerow(["003", "张三", "已完成", "x"])
            writer.writerow(["004", "李四", "待处理", "x"])
            writer.writerow(["005", "王五", "待处理", "x"])
            writer.writerow(["", "王五", "待处理", "x"])
        return path

    def test_collect_group_and_split(self):
        path = self.make_csv()
        total, pending, counts = app.collect_pending(path, self.config)
        self.assertEqual(total, 6)
        self.assertEqual(pending, 5)
        self.assertEqual(counts["叶于琳"], 2)
        self.assertEqual(counts["王五"], 2)
        _, work_orders = app.collect_work_orders(path, self.config)
        messages = app.build_detail_messages(work_orders, self.config)
        self.assertEqual(len(messages), 2)
        self.assertIn("@叶于琳 今日到期 2 单", messages[0])
        self.assertNotIn("张三", "\n".join(messages))
        self.assertIn("工单：未提供", "\n".join(messages))

    def test_blank_handler_uses_nearest_name_in_same_workgroup(self):
        path = Path(self.config["paths"]["inbox"]) / "投诉表格.csv"
        self.config["excel"].update(
            {
                "workgroup_columns": ["当前处理工作组"],
                "acceptance_columns": ["受理号码"],
                "due_columns": ["本环节到期时限"],
            }
        )
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["工单流水号", "受理号码", "当前处理工作组", "当前处理人", "本环节到期时限"])
            writer.writerow(["001", "13800000001", "北网格|100", "甲", "2026-07-24 08:00:00"])
            writer.writerow(["002", "13800000002", "南网格|200", "乙", "2026-07-24 09:00:00"])
            writer.writerow(["003", "13800000003", "北网格|100", "", "2026-07-24 10:00:00"])
            writer.writerow(["004", "13800000004", "北网格|100", "甲", "2026-07-24 11:00:00"])

        total, rows = app.collect_work_orders(path, self.config)
        self.assertEqual(total, 4)
        inferred = next(row for row in rows if row.ticket == "003")
        self.assertEqual(inferred.handler, "甲")

    def test_blank_handler_with_no_same_workgroup_candidate_is_skipped(self):
        path = Path(self.config["paths"]["inbox"]) / "投诉表格.csv"
        self.config["excel"]["workgroup_columns"] = ["当前处理工作组"]
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["工单流水号", "当前处理工作组", "当前处理人", "工单状态"])
            writer.writerow(["001", "孤立网格|999", "", "待处理"])
            writer.writerow(["002", "其他网格|100", "甲", "待处理"])

        total, rows = app.collect_work_orders(path, self.config)
        self.assertEqual(total, 2)
        self.assertEqual([row.ticket for row in rows], ["002"])

    def test_blank_handler_does_not_guess_when_nearest_names_tie(self):
        path = Path(self.config["paths"]["inbox"]) / "投诉表格.csv"
        self.config["excel"]["workgroup_columns"] = ["当前处理工作组"]
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["工单流水号", "当前处理工作组", "当前处理人", "工单状态"])
            writer.writerow(["001", "同一网格|100", "甲", "待处理"])
            writer.writerow(["002", "同一网格|100", "", "待处理"])
            writer.writerow(["003", "同一网格|100", "乙", "待处理"])

        _, rows = app.collect_work_orders(path, self.config)
        self.assertEqual([row.ticket for row in rows], ["001", "003"])

    def test_send_batch_reservation_is_atomic_across_connections(self):
        database = Path(self.config["paths"]["state_db"])
        first = app.init_db(database)
        first.close()
        barrier = threading.Barrier(2)
        results = []

        def reserve():
            connection = sqlite3.connect(database, timeout=5)
            try:
                barrier.wait()
                results.append(app.reserve_send_batch(connection, "file", "group:test", "message", "source.xlsx"))
            finally:
                connection.close()

        threads = [threading.Thread(target=reserve) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(sum(result is None for result in results), 1)

    def test_stale_send_batch_lock_expires(self):
        database = Path(self.config["paths"]["state_db"])
        connection = app.init_db(database)
        old_created = (datetime.now().astimezone() - timedelta(hours=2)).isoformat(timespec="seconds")
        connection.execute(
            "INSERT INTO send_batches VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("oldlock", "file", "group:test", "message", "source.xlsx", old_created, "sending", None),
        )
        connection.commit()

        batch_id = app.reserve_send_batch(connection, "file", "group:test", "message", "source.xlsx", stale_after_seconds=60)
        old_status = connection.execute("SELECT status,error FROM send_batches WHERE id='oldlock'").fetchone()
        connection.close()

        self.assertIsNotNone(batch_id)
        self.assertEqual("error", old_status[0])
        self.assertIn("Stale sending lock expired", old_status[1])

    def test_gateway_preflight_retries_until_ready(self):
        self.config["delivery"].update(
            {
                "openclaw_cmd": "openclaw.cmd",
                "preflight_gateway": True,
                "gateway_ready_timeout_seconds": 5,
                "gateway_ready_poll_seconds": 0.01,
                "gateway_ready_markers": ["Connectivity probe: ok", "Listening:"],
            }
        )
        not_ready = app.subprocess.CompletedProcess([], 1, stdout="Connectivity probe: failed", stderr="")
        ready = app.subprocess.CompletedProcess(
            [],
            0,
            stdout="Connectivity probe: ok\nListening: 127.0.0.1:18789",
            stderr="",
        )
        with patch.object(app.subprocess, "run", side_effect=[not_ready, ready]) as run:
            app.ensure_gateway_ready(self.config)
        self.assertEqual(run.call_count, 2)

    def test_read_real_xlsx(self):
        from openpyxl import Workbook

        path = Path(self.config["paths"]["inbox"]) / "orders.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["工单流水号", "受理人", "工单状态"])
        sheet.append(["X001", "叶于琳", "待处理"])
        sheet.append(["X002", "叶于琳", "已关闭"])
        workbook.save(path)
        workbook.close()

        total, pending, counts = app.collect_pending(path, self.config)
        self.assertEqual((total, pending), (2, 1))
        self.assertEqual(counts, {"叶于琳": 1})

    def test_preview_records_audit_without_skip_side_effect(self):
        path = self.make_csv()
        connection = app.init_db(Path(self.config["paths"]["state_db"]))
        total, work_orders = app.collect_work_orders(path, self.config)
        result = app.ImportResult(
            path,
            app.file_digest(path),
            total,
            len(work_orders),
            app.Counter(row.handler for row in work_orders),
            ["preview only"],
        )
        app.record_import(connection, result, "preview", [])
        self.assertTrue(app.already_imported(connection, app.file_digest(path)))
        connection.close()

    def test_same_file_is_not_skipped_by_history(self):
        path = self.make_csv()
        connection = app.init_db(Path(self.config["paths"]["state_db"]))
        try:
            self.assertFalse(app.already_imported(connection, app.file_digest(path)))
            total, work_orders = app.collect_work_orders(path, self.config)
            result = app.ImportResult(
                path,
                app.file_digest(path),
                total,
                len(work_orders),
                app.Counter(row.handler for row in work_orders),
                ["audit only"],
            )
            app.record_import(connection, result, "delivered", [])
            self.assertTrue(app.already_imported(connection, app.file_digest(path)))
            self.assertEqual(app.collect_pending(path, self.config)[1], 5)
        finally:
            connection.close()

    def test_detail_message_lists_ticket_and_acceptance_number(self):
        rows = [
            app.WorkOrderRow("邵嫣然", "GD001", "13800000001", "2026-07-23 09:00:00"),
            app.WorkOrderRow("邵嫣然", "GD002", "13800000002", "2026-07-23 10:00:00"),
            app.WorkOrderRow("口十林", "GD003", "13800000003", "2026-07-23 11:00:00"),
        ]
        messages = app.build_detail_messages(rows, self.config)
        text = "\n".join(messages)
        self.assertIn("@邵嫣然 今日到期 2 单", text)
        self.assertIn("工单：GD001｜受理：13800000001", text)
        self.assertIn("工单：GD002｜受理：13800000002", text)
        self.assertIn("@口十林 今日到期 1 单", text)

    def test_overdue_message_only_contains_counts(self):
        rows = [
            app.WorkOrderRow("邵嫣然", "GD001", "13800000001", ""),
            app.WorkOrderRow("邵嫣然", "GD002", "13800000002", ""),
            app.WorkOrderRow("口十林", "GD003", "13800000003", ""),
        ]
        messages = app.build_overdue_messages(rows, self.config)
        text = "\n".join(messages)
        self.assertIn("@邵嫣然 还有 2 单已过期未处理", text)
        self.assertIn("@口十林 还有 1 单已过期未处理", text)
        self.assertNotIn("GD001", text)
        self.assertIn(
            "@口十林 还有 1 单已过期未处理，请尽快处理。\n"
            "@邵嫣然 还有 2 单已过期未处理，请尽快处理。",
            text,
        )

    def test_file_mode_uses_filename_keywords(self):
        self.assertEqual(app.classify_file(Path("投诉表格.xlsx"), self.config), "detail")
        self.assertEqual(app.classify_file(Path("过期表格.xlsx"), self.config), "overdue")

    def test_extract_inbound_member_from_gateway_log(self):
        payload = {
            "chatType": "group",
            "groupCode": "924443429",
            "fromAccount": "user-id-123",
            "senderNickname": "叶于琳",
        }
        line = json.dumps({"1": "[at-probe][inbound] decoded message " + json.dumps(payload, ensure_ascii=False)}, ensure_ascii=False)
        self.assertEqual(
            app.extract_inbound_member(line),
            ("924443429", "叶于琳", "user-id-123"),
        )

    def test_missing_mapping_is_sent_without_native_at(self):
        self.config["member_discovery"] = {"enabled": True}
        resolution = app.resolve_mentions(app.Counter({"new-member": 1}), self.config)
        self.assertEqual(resolution.mentionable, set())
        self.assertEqual(resolution.missing, {"new-member"})
        rows = [app.WorkOrderRow("new-member", "GD001", "13800000001", "")]
        message = app.build_detail_messages(rows, self.config, resolution.mentionable)[0]
        self.assertIn("new-member 今日到期 1 单", message)
        self.assertNotIn("@new-member", message)
        self.assertIn("new-member 没查到，请问是否在本群里面。", message)

    def test_supported_business_file_name_is_required(self):
        path = Path(self.config["paths"]["inbox"]) / "orders.csv"
        path.write_text("当前处理人,工单流水号\n叶于琳,001\n", encoding="utf-8-sig")
        connection = app.init_db(Path(self.config["paths"]["state_db"]))
        try:
            with self.assertRaisesRegex(ValueError, "Unsupported work-order file name"):
                app.process_file(path, self.config, connection, dry_run=True)
        finally:
            connection.close()

    @patch("work_order_reminder.subprocess.run")
    def test_create_cron_puts_multiline_message_after_delivery_options(self, run):
        run.return_value.returncode = 0
        run.return_value.stderr = ""
        run.return_value.stdout = json.dumps(
            {
                "id": "job-1",
                "delivery": {
                    "channel": "yuanbao",
                    "to": "group:test-group",
                },
            }
        )
        config = {
            "delivery": {
                "openclaw_cmd": "openclaw.cmd",
                "target": "group:test-group",
                "channel": "yuanbao",
                "account": "default",
            }
        }
        app.create_cron("第一行\n第二行", 1, config)
        command = run.call_args.args[0]
        self.assertLess(command.index("--to"), command.index("--message"))
        self.assertNotIn("\n", command[-1])
        self.assertIn("第一行[[YB_LINE_BREAK]]第二行", command[-1])
        self.assertIn("替换为一个真实换行", command[-1])

    def test_cron_prompt_preserves_blank_lines_with_markers(self):
        prompt = app.build_cron_prompt("标题\n\n@张三 内容\n@李四 内容")
        self.assertIn("标题[[YB_LINE_BREAK]][[YB_LINE_BREAK]]@张三", prompt)
        self.assertNotIn("\n", prompt)

    def test_yuanbao_send_ok_log_confirms_delivery(self):
        root = Path(self.temp.name)
        log_dir = root / "logs"
        log_dir.mkdir()
        line = {
            "_meta": {"time": "2026-07-24T10:07:21.501+08:00"},
            "message": '[yuanbao:2.17.0][transport] [group] send ok {"groupCode":"test-group","msgId":"msg-123"}',
        }
        (log_dir / "openclaw-2026-07-24.log").write_text(
            json.dumps(line, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        config = {
            "delivery": {
                "target": "group:test-group",
                "gateway_log_dir": str(log_dir),
            }
        }
        entry = {
            "runAtMs": 1784858839000,
            "ts": 1784858842000,
            "deliveryStatus": "not-delivered",
        }
        self.assertEqual(app.yuanbao_send_ok_from_logs(entry, config), "msg-123")


if __name__ == "__main__":
    unittest.main()
