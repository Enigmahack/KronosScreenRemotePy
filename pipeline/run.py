"""Script-first change pipeline. Prints a short digest; full logs go to pipeline/out/.

  python pipeline/run.py             tier 0: changed files -> compile, layering, affected tests
  python pipeline/run.py --staged    tier 0 on the staged files only (what the pre-commit hook runs)
  python pipeline/run.py --tier 1    everything: all tests + regress.py
  python pipeline/run.py --cov       also report changed lines no test executed (needs `pip install -r requirements-dev.txt`)
  python pipeline/run.py --update-baseline   accept current failures/violations as known

Exit code 0 = nothing new is broken. Agents should read only the digest, then open
pipeline/out/<test>.log for a specific failure.
"""
import argparse, ast, concurrent.futures as cf, json, os, re, subprocess, sys, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "pipeline" / "out"
BASELINE = ROOT / "pipeline" / "baseline.json"
PACKAGES = ("Core", "Models", "Views", "Data", "Objects", "Rendering", "Commands", "Tools", "Utils", "Conversion")
# package -> packages it must not import (lower layers never reach up into the UI)
FORBIDDEN = {p: {"Views"} for p in ("Core", "Models", "Data", "Objects", "Rendering", "Utils", "Conversion", "Tools", "Commands")}
# destructive-write surface: a change here is the trigger for the safety reviewer
SAFETY_PATHS = re.compile(r"sample_ftp|file_manager|sysex_service|kronos_sysex|conversion/push|librarian|storage|merge|backup", re.I)
TIMEOUT = 300
SKIP_PARTS = {".venv", "__pycache__", "packaging", "pipeline", "local_library", "librarian_backups"}


def git(*args):
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    return r.stdout.splitlines()


def changed_files(base, staged=False):
    if staged:
        names = set(git("diff", "--cached", "--name-only", "--diff-filter=ACMR"))
    else:
        names = set(git("diff", "--name-only", base)) | set(git("ls-files", "--others", "--exclude-standard"))
    return sorted(n for n in names if n.endswith(".py") and (ROOT / n).exists()
                  and not SKIP_PARTS & set(Path(n).parts))


def all_py():
    return sorted(str(p.relative_to(ROOT)).replace("\\", "/") for p in ROOT.rglob("*.py")
                  if not SKIP_PARTS & set(p.relative_to(ROOT).parts))


def compile_check(files):
    bad = []
    for f in files:
        try:
            ast.parse((ROOT / f).read_text(encoding="utf-8"), f)
        except SyntaxError as e:
            bad.append(f"{f}:{e.lineno} {e.msg}")
    return bad


def layering(files):
    found = []
    for f in files:
        top = Path(f).parts[0]
        banned = FORBIDDEN.get(top)
        if not banned:
            continue
        try:
            tree = ast.parse((ROOT / f).read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for n in ast.walk(tree):
            mods = [n.module or ""] if isinstance(n, ast.ImportFrom) and n.level == 0 else \
                   [a.name for a in n.names] if isinstance(n, ast.Import) else []
            for m in mods:
                if m.split(".")[0] in banned:
                    found.append(f"{f}:{n.lineno} imports {m}")
    return found


def all_tests():
    return sorted(str(p.relative_to(ROOT)).replace("\\", "/") for p in (ROOT / "tests").glob("test_*.py")) \
        + ["tests/regress.py"]


def affected_tests(files):
    tests = all_tests()
    hit = {"tests/regress.py"}
    stems = set()
    for f in files:
        if f.startswith("tests/"):
            if f in tests:
                hit.add(f)
            continue
        stems.add(Path(f).stem)
    pats = [re.compile(r"\b%s\b" % re.escape(s)) for s in stems if s != "__init__"]
    for t in tests:
        src = (ROOT / t).read_text(encoding="utf-8", errors="replace")
        if any(p.search(src) for p in pats):
            hit.add(t)
    return sorted(hit)


COV_DIR = OUT / ".cov"
USE_COV = False


def run_test(t):
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONIOENCODING="utf-8")
    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix="kr_pipe_") as d:
        env["KRONOS_DATA_DIR"] = d
        try:
            cmd = [sys.executable, str(ROOT / t)]
            if USE_COV:
                cmd = [sys.executable, "-m", "coverage", "run", "--parallel-mode", f"--data-file={COV_DIR / '.coverage'}",
                       f"--source={ROOT}", "--omit=*/tests/*,*/.venv/*,*/pipeline/*", str(ROOT / t)]
            r = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=TIMEOUT,
                               encoding="utf-8", errors="replace")
            code, text = r.returncode, r.stdout + r.stderr
        except subprocess.TimeoutExpired as e:
            code, text = -1, f"TIMEOUT after {TIMEOUT}s\n{(e.stdout or b'')[-2000:]}"
    (OUT / (Path(t).stem + ".log")).write_text(text, encoding="utf-8")
    return t, code, time.time() - t0, text


def changed_line_ranges(f, base):
    """{line numbers added/changed in f}; every line for an untracked file."""
    out = git("diff", "-U0", base, "--", f)
    if not out:
        return set(range(1, len((ROOT / f).read_text(encoding="utf-8", errors="replace").splitlines()) + 1))
    lines = set()
    for l in out:
        m = re.match(r"@@ -\S+ \+(\d+)(?:,(\d+))? @@", l)
        if m:
            a, n = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
            lines.update(range(a, a + n))
    return lines


def compress(nums):
    nums, out = sorted(nums), []
    for n in nums:
        if out and n == out[-1][1] + 1:
            out[-1][1] = n
        else:
            out.append([n, n])
    return ",".join(str(a) if a == b else f"{a}-{b}" for a, b in out)


def coverage_gaps(files, base):
    """Changed executable lines that no selected test ran, as digest lines."""
    run = lambda *a: subprocess.run([sys.executable, "-m", "coverage", *a], cwd=ROOT, capture_output=True, text=True)
    run("combine", f"--data-file={COV_DIR / '.coverage'}", str(COV_DIR))
    js = COV_DIR / "cov.json"
    run("json", f"--data-file={COV_DIR / '.coverage'}", "--ignore-errors", "-o", str(js))
    if not js.exists():
        return ["COV no coverage data produced"]
    data = json.loads(js.read_text())["files"]
    gaps = []
    for f in files:
        if f.startswith("tests/"):
            continue
        rec = next((v for k, v in data.items() if k.replace("\\", "/").endswith(f)), None)
        if rec is None:
            gaps.append(f"COV {f}: not imported by any selected test")
            continue
        miss = set(rec["missing_lines"]) & changed_line_ranges(f, base)
        if miss:
            gaps.append(f"COV {f}: untested changed lines {compress(miss)}")
    return gaps


def failure_line(text):
    lines = [l for l in text.strip().splitlines() if l.strip()]
    for l in reversed(lines):
        if re.match(r"\w*(Error|Exception|Failed|FAIL)|assert|TIMEOUT", l.strip(), re.I):
            return l.strip()[:160]
    return (lines[-1].strip()[:160] if lines else "no output")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", type=int, default=0, choices=(0, 1))
    ap.add_argument("--base", default="HEAD")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--staged", action="store_true", help="only staged files (pre-commit)")
    ap.add_argument("--cov", action="store_true", help="report changed lines no test executed")
    a = ap.parse_args()
    global USE_COV
    if a.cov:
        try:
            import coverage  # noqa: F401
        except ImportError:
            print("COV skipped: coverage not installed (pip install -r requirements-dev.txt)")
        else:
            USE_COV = True
            import shutil
            shutil.rmtree(COV_DIR, ignore_errors=True)
            COV_DIR.mkdir(parents=True)
    OUT.mkdir(parents=True, exist_ok=True)
    base = json.loads(BASELINE.read_text()) if BASELINE.exists() else {"failing_tests": [], "layering": []}

    files = all_py() if a.tier == 1 else changed_files(a.base, a.staged)
    tests = all_tests() if a.tier == 1 else affected_tests(files)

    bad_compile = compile_check(files)
    lay = [v for v in layering(all_py() if a.tier == 1 else files) if v not in base["layering"]]
    results = []
    if not bad_compile:
        with cf.ThreadPoolExecutor(a.workers) as ex:
            results = list(ex.map(run_test, tests))
    failed = [(t, c, text) for t, c, _, text in results if c != 0]
    new_fail = [x for x in failed if x[0] not in base["failing_tests"]]

    if a.update_baseline:
        base["failing_tests"] = sorted(t for t, _, _ in failed)
        base["layering"] = sorted(set(base["layering"]) | set(lay))
        BASELINE.write_text(json.dumps(base, indent=1))

    gaps = coverage_gaps(files, a.base) if USE_COV and not bad_compile else []
    safety = [f for f in files if SAFETY_PATHS.search(f)]
    ui = [f for f in files if f.startswith("Views/")]
    slow = sorted(((s, t) for t, _, s, _ in results), reverse=True)[:2]
    out = [f"tier {a.tier}: {len(files)} files, {len(tests)} tests, "
           f"{len(results) - len(failed)} pass / {len(failed)} fail ({len(failed) - len(new_fail)} known)"]
    out += [f"COMPILE {x}" for x in bad_compile]
    out += [f"LAYER {x}" for x in lay]
    out += [f"FAIL {t}: {failure_line(text)}  [pipeline/out/{Path(t).stem}.log]" for t, _, text in new_fail]
    out += gaps
    known = sorted(Path(t).stem for t, _, _ in failed if t in base["failing_tests"])
    if known:
        out.append("known failing (baseline): " + ", ".join(known))
    out.append("triggers: " + (", ".join(
        x for x, on in (("safety-reviewer", safety), ("ux-reviewer", ui), ("code-reviewer", files)) if on) or "none"))
    if slow:
        out.append("slowest: " + ", ".join(f"{Path(t).stem} {s:.0f}s" for s, t in slow))
    digest = "\n".join(out)
    (OUT / "digest.txt").write_text(digest, encoding="utf-8")
    print(digest)
    return 1 if (bad_compile or lay or new_fail) else 0


if __name__ == "__main__":
    sys.exit(main())
