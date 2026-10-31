"""Protocol regression cases independent of model or filesystem persistence."""
import io
import queue
import threading
from types import SimpleNamespace
import unittest

from control import Node


class ProtocolReplies(unittest.TestCase):
    def node(self):
        node = Node.__new__(Node)
        node.events, node.replies = [], queue.Queue()
        node.buffered, node.exit_event = {}, None
        node.receive_lock = threading.Lock()
        return node

    def test_out_of_order_replies_remain_available(self):
        node = self.node()
        node.replies.put({"id": 2, "result": "second"})
        node.replies.put({"id": 1, "result": "first"})
        self.assertEqual(node.result(1, timeout=.1), "first")
        try:
            second = node.result(2, timeout=.1)
        except (queue.Empty, AssertionError):
            self.fail("The response for request 2 was discarded")
        self.assertEqual(second, "second")

    def test_reader_failure_wakes_callers_and_preserves_prior_reply(self):
        node = self.node()
        node.process = SimpleNamespace(stdout=io.StringIO(
            '{"id":1,"result":"saved"}\n{"kind":"bridge-abort"}\ninvalid JSON\n'))
        try:
            node._read()
        except ValueError:
            self.fail("Reader parsing failure escaped without publishing its terminal event")
        self.assertEqual(node.result(1, timeout=.1), "saved")
        for request in (2, 3):
            with self.assertRaisesRegex(AssertionError, "reader failed"):
                node.result(request, timeout=.1)

    def test_response_timeout_is_an_assertion_with_request_id(self):
        with self.assertRaisesRegex(AssertionError, "request 7 timed out"):
            try:
                self.node().result(7, timeout=.01)
            except queue.Empty:
                self.fail("Timeout leaked queue.Empty")
