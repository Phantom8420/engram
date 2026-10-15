from engram.loss import meter
from engram.load import module_of, kind_of


def test_meter_hand_built():
    holdings = {"a.py": {"x": 3.0, "y": 1.0}, "b.py": {"y": 2.0}, "c.py": {"z": 1.0}}
    imp = {"a.py": 1.0, "b.py": 1.0, "c.py": 2.0}
    assert meter(holdings, set(), {}, imp)["meter"] == 0
    r = meter(holdings, {"y"}, {}, imp)
    # a: lost 0.25, b: lost 1.0, c: untouched -> (0.25 + 1.0) / 4
    assert r["meter"] == round(100 * 1.25 / 4, 1)
    assert r["items"][0]["module"] == "b.py" and r["items"][0]["fragile"]
    documented = meter(holdings, {"y"}, {"b.py": True}, imp)
    assert documented["meter"] < r["meter"]


def test_greedy_matches_brute_force():
    from itertools import combinations
    from engram.interrogate import greedy
    gain = {"a": 5, "b": 4, "c": 3, "d": 1}
    qs = [{"id": i, "modules": ms} for i, ms in enumerate([["a"], ["a", "b"], ["c"], ["b", "c"], ["d"]])]
    value = lambda picked: sum(gain[m] for m in {m for q in picked for m in q["modules"]})
    for k in (1, 2):
        best = max(value(c) for c in combinations(qs, k))
        assert value(greedy(qs, gain, k)) == best
    assert [q["id"] for q in greedy(qs, gain, 3)] == [1, 2, 4]  # stops re-asking about covered modules


def test_modules():
    assert module_of("src/black/linegen.py") == "src/black/linegen.py"
    assert module_of("src/blib2to3/pgen2/driver.py") == "src/blib2to3/pgen2"
    assert kind_of("docs/index.md") == "doc" and kind_of("src/black/mode.py") == "code"
