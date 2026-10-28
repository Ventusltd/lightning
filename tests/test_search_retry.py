"""The EUMETSAT search (and its paging) is retried on transient errors (run 40690260149)."""
import unittest
import unittest.mock

import collect


# Stand-ins named like the requests classes: errors are classified by class name,
# and CI runs without the requests package installed.
class ConnectionError(Exception):  # noqa: A001
    pass


class ReadTimeout(Exception):
    pass


class FakeProducts:
    def __init__(self, items, fail_on_iter=None):
        self.items, self.fail_on_iter = items, fail_on_iter
        self.total_results = len(items)

    def __iter__(self):
        if self.fail_on_iter:
            raise self.fail_on_iter
        return iter(self.items)


class SearchRetryTests(unittest.TestCase):
    def run_with(self, outcomes):
        calls, waits = [], []

        def run_search():
            calls.append(1)
            outcome = outcomes[len(calls) - 1]
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        result = collect.search_with_retries(run_search, sleep=waits.append, rng=lambda: 0.0)
        return result, calls, waits

    def test_read_timeout_on_search_is_retried(self):
        timeout = ConnectionError("HTTPSConnectionPool(host='api.eumetsat.int', port=443): Read timed out.")
        result, calls, waits = self.run_with([timeout, FakeProducts(["a", "b"])])
        self.assertEqual(result, ["a", "b"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(waits, [2.0])

    def test_timeout_while_paging_is_retried_and_results_are_listed(self):
        paged = FakeProducts(["x"], fail_on_iter=ReadTimeout("paging"))
        result, calls, _ = self.run_with([paged, FakeProducts(["x", "y"])])
        self.assertEqual(result, ["x", "y"])
        self.assertIsInstance(result, list)
        self.assertEqual(len(calls), 2)

    def test_gives_up_after_the_retry_budget(self):
        timeout = ReadTimeout("still down")
        with self.assertRaises(ReadTimeout):
            self.run_with([timeout] * (collect.TRANSIENT_RETRIES + 1))

    def test_fatal_errors_are_not_retried(self):
        with self.assertRaises(ValueError):
            self.run_with([ValueError("bad query")])

    def test_empty_search_still_refuses(self):
        with self.assertRaises(RuntimeError):
            self.run_with([FakeProducts([])])


if __name__ == "__main__":
    unittest.main()


class SearchRetryDefaultsTests(unittest.TestCase):
    def test_default_rng_and_sleep_work_without_injection(self):
        """Run 40699133665 died with NameError: random was not imported at module level."""
        calls = []

        def run_search():
            calls.append(1)
            if len(calls) == 1:
                raise ReadTimeout("once")
            return ["p"]

        with unittest.mock.patch.object(collect, "_sleep", lambda s: None):
            self.assertEqual(collect.search_with_retries(run_search), ["p"])
