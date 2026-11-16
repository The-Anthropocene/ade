# id: tools.slow_tests
# what: runs the unit suite in parallel, one subprocess per test module (one per core), and prints the 15 slowest modules with the total
# cmd: python tools/slow_tests.py   (exit 1 if any module failed, errored or crashed)
# reads: tests/ (discovered as python -m unittest does; each module is run by python -m unittest -v)
# writes: nothing but the report on stdout: each module's verbose test log, printed whole as it finishes
import collections
import concurrent.futures
import os
import subprocess
import sys
import time
import unittest


def modules(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from modules(item)
        else:
            mod = type(item).__module__
            # a module that failed to import shows up as a _FailedTest named after it
            yield item._testMethodName if mod == "unittest.loader" else mod


def run_module(name):
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "-v", name],
            cwd=os.getcwd(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace",
        )
        out, code = proc.stdout, proc.returncode
    except Exception as exc:  # a crashed module counts as failed
        out, code = f"{name}: could not run: {exc!r}\n", 1
    return name, out, code, time.perf_counter() - t0


def main():
    suite = unittest.defaultTestLoader.discover("tests", top_level_dir=".")
    # modules() yields once per test case, so the counts are the cases per module.
    # Start the biggest modules first: they are the slowest (test_stages, test_loop,
    # test_conduct), and with one worker per core a big module started last in
    # alphabetical order would run alone at the end and set the wall time.
    counts = collections.Counter(modules(suite))
    names = sorted(counts, key=lambda n: (-counts[n], n))
    times = []
    ok = True
    start = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count() or 1) as pool:
        futures = [pool.submit(run_module, n) for n in names]
        for fut in concurrent.futures.as_completed(futures):
            name, out, code, secs = fut.result()
            print(f"=== {name} ===")
            print(out, end="" if out.endswith("\n") else "\n", flush=True)
            times.append((secs, name))
            ok = ok and code == 0
    total = time.perf_counter() - start
    print("slowest modules:")
    for secs, name in sorted(times, reverse=True)[:15]:
        print(f"{secs:8.1f} s  {name}")
    print(f"{total:8.1f} s  total")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
