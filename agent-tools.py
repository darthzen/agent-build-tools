#!/usr/bin/env python3
"""agent-build-tools: GitHub-Issues-native ticket toolkit for agent-driven builds.

Project-agnostic: swap languages/gates via the `tooling:` section of
build-plan/config.yaml (see config.example.yaml).

A ticket IS a GitHub Issue — that is the single source of truth (open = to-do,
closed = done). There is no tasks.yaml and no sync step. This toolkit offloads
task selection, context assembly, verification, and the git/PR ceremony onto
deterministic commands so the model only has to write code.

  python3 scripts/agent-tools.py new "Title" ...   # create a correctly-formatted ticket issue
  python3 scripts/agent-tools.py next               # id of the next runnable agent ticket
  python3 scripts/agent-tools.py show T-012         # ticket body + its files + cited schema section
  python3 scripts/agent-tools.py verify T-012       # run the ticket's Acceptance commands
  python3 scripts/agent-tools.py start T-012        # flag the issue "In Progress" on the Project board
  python3 scripts/agent-tools.py finish T-012       # verify, branch, commit, push, PR, merge, close (serial)
  python3 scripts/agent-tools.py doctor [--fix]     # reconcile issues ↔ commits ↔ board ↔ branches

Parallel execution (Model A — one orchestrator, N isolated worker worktrees):

  python3 scripts/agent-tools.py capacity                   # safe worker count from this machine's cores+RAM
  python3 scripts/agent-tools.py batch --running A,B --max N # runnable tickets, file-disjoint from the in-flight set
  python3 scripts/agent-tools.py worktree-add T-032         # make ../<repo>-worktrees/T-032 on branch task/T-032
  python3 scripts/agent-tools.py submit T-032               # (in worktree) gate+build, add -A, commit, push, open PR
  python3 scripts/agent-tools.py land T-032                 # (from main checkout) serialized squash-merge + cleanup
  python3 scripts/agent-tools.py worktree-rm T-032          # tear down a worker worktree

Ticket data (milestone, depends-on, files, acceptance) is parsed from the issue
title + body; project-wide config (repo, project number, build frontier) lives
in build-plan/config.yaml. `submit` runs inside a worktree and is parallel-safe;
`land` serializes the merge to main behind a file lock and rebases-then-retries
on conflict.

Board routing: a ticket's milestone default (a `projects:` map in config.yaml)
or $PROJECT_NUMBER / the global `project.project_number` decides which GitHub
Project its status updates target, so different phases can live on different boards.
"""
import os, re, sys, json, ast, subprocess, yaml, fcntl

BUILD_PLAN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(BUILD_PLAN)
# Project-type specifics (runnable commands, per-command cwd, build gates,
# test-path template) come from the `tooling:` section of build-plan/config.yaml.
WORKTREES = os.path.join(os.path.dirname(REPO), os.path.basename(REPO) + "-worktrees")
LOCKS = os.path.join(BUILD_PLAN, ".locks")


def sh(args, check=False, **kw):
    r = subprocess.run(args, text=True, capture_output=True, cwd=REPO, **kw)
    if check and r.returncode != 0:
        sys.exit(f"BLOCKED: `{' '.join(args)}` failed:\n{(r.stderr or r.stdout).strip()}")
    return r

# ---- config + ticket loading (GitHub Issues = single source of truth) -------
_CONFIG = None
_TICKETS = None

def config():
    """Project-wide settings from build-plan/config.yaml (never ticket data)."""
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = yaml.safe_load(open(os.path.join(BUILD_PLAN, "config.yaml"))) or {}
    return _CONFIG

def repo_slug():
    return config()["project"].get("repo") or sh(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"]).stdout.strip()

# ---- project-type tooling (config.yaml `tooling:`; defaults preserve the
# ---- original swift-project behavior so legacy configs keep working) --------
_DEFAULT_RUNNABLE = ("swift", "swiftlint", "xcodebuild", "python", "python3", "cd",
                     "bash", "sh", "kubectl", "kubeconform", "helm", "pytest",
                     "shellcheck", "make", "go", "npm", "cargo")

def tooling():
    return config().get("tooling") or {}

def runnable():
    return tuple(tooling().get("runnable") or _DEFAULT_RUNNABLE)

def _cmd_cwd(c):
    """cwd for an acceptance command: first matching cwd_rule
    ({command: swift, dir: Packages/NifflerCore}), else the repo root."""
    head = c.split()[0]
    for rule in tooling().get("cwd_rules") or []:
        if head == rule.get("command"):
            p = os.path.join(REPO, rule.get("dir") or ".")
            if os.path.isdir(p):
                return p
    return REPO

def _env2(name, default=None):
    """AGENT_<name> env var with legacy NIFFLER_<name> fallback."""
    return os.environ.get("AGENT_" + name) or os.environ.get("NIFFLER_" + name) or default

def _ticket_id(title):
    m = re.match(r"\s*(T-\d+)", title or "")
    return m.group(1) if m else None

def parse_issue(it):
    """Turn a GitHub issue (number,title,state,labels,milestone,body) into the
    task-dict shape the rest of the toolkit expects. The body header is the
    format `agent-tools.py new` (and the retired sync) emit, so it round-trips."""
    title = it.get("title", "")
    tid = _ticket_id(title)
    name = title.split("·", 1)[1].strip() if "·" in title else title
    labels = [l["name"] if isinstance(l, dict) else l for l in (it.get("labels") or [])]
    ms = it.get("milestone")
    milestone = (ms or {}).get("title") if isinstance(ms, dict) else ms
    body = it.get("body") or ""
    deps = []
    md = re.search(r"\*\*Depends on:\*\*\s*([^|\n]*)", body)
    if md:
        deps = re.findall(r"T-\d+", md.group(1))
    files = {"create": [], "modify": []}
    mf = re.search(r"\*\*Files\*\*.*?create:\s*(\[[^\]]*\])\s*;\s*modify:\s*(\[[^\]]*\])", body, re.S)
    if mf:
        for kind, grp in (("create", mf.group(1)), ("modify", mf.group(2))):
            try:
                files[kind] = list(ast.literal_eval(grp))
            except Exception:
                files[kind] = []
    parts = re.split(r"(?m)^---\s*$", body, maxsplit=1)
    md_body = parts[1].strip() if len(parts) > 1 else body
    return {
        "id": tid, "title": name, "labels": labels,
        "assignee": "agent" if "agent" in labels else ("human" if "human" in labels else "agent"),
        "milestone": milestone, "depends_on": deps, "files": files,
        "body": md_body, "status": (it.get("state") or "").lower(), "number": it.get("number"),
    }

def tickets():
    """All ticket issues, parsed, cached for the process. Issues without a T-id
    title prefix are ignored (they are not build tickets)."""
    global _TICKETS
    if _TICKETS is None:
        out = sh(["gh", "issue", "list", "--repo", repo_slug(), "--state", "all",
                  "--limit", "1000", "--json", "number,title,state,labels,milestone,body"])
        if out.returncode != 0:
            sys.exit(f"BLOCKED: cannot read GitHub issues (gh auth / network?):\n{out.stderr.strip()}")
        try:
            raw = json.loads(out.stdout or "[]")
        except Exception:
            raw = []
        _TICKETS = [parse_issue(it) for it in raw if _ticket_id(it.get("title", ""))]
    return _TICKETS

def load():
    """Compatibility shim: a cfg dict shaped like the old tasks.yaml
    (project / projects / tasks), but tickets come from GitHub Issues."""
    c = dict(config())
    c["tasks"] = tickets()
    return c

def by_id(cfg):
    # later issues win on duplicate ids (e.g. historical T-160 collisions); both
    # are closed so selection is unaffected.
    return {t["id"]: t for t in cfg["tasks"] if t["id"]}

# ---- Project board status (best-effort; never blocks the build) ------------
def _json(r):
    try:
        return json.loads(r.stdout or "null")
    except Exception:
        return None

def project_number_for(tid):
    """Which GitHub Project board this ticket belongs to. Resolution order:
    the ticket's own `project_number`, then its milestone default (a
    `projects:` map in tasks.yaml keyed by milestone id), then $PROJECT_NUMBER,
    then the global `project.project_number`. Lets phases live on separate boards."""
    cfg = load(); t = by_id(cfg).get(tid) or {}
    if t.get("project_number"):
        return str(t["project_number"])
    pmap = cfg.get("projects") or {}
    ms = (t.get("milestone") or "").split(" ", 1)[0].split("·")[0].strip()
    if ms and pmap.get(ms):
        return str(pmap[ms])
    return os.environ.get("PROJECT_NUMBER") or str(cfg["project"].get("project_number") or "")

def _project(owner, num):
    """(project_node_id, number) for Project `num`, or (None, None)."""
    if not num:
        return (None, None)
    v = _json(sh(["gh", "project", "view", num, "--owner", owner, "--format", "json"]))
    return ((v or {}).get("id"), num)

def set_status(slug, tid, option, force=False):
    """Set the issue's Project Status field to `option` (e.g. 'In Progress', 'Done').

    OPT-IN: each call costs several GraphQL queries (project view + field-list +
    item-list), which drains the GitHub API quota fast under an autonomous loop.
    The build doesn't need it — issue open/closed state is the source of truth,
    and GitHub's built-in "item closed → Done" Project workflow covers the column.
    Enable with $NIFFLER_BOARD=1 or project.board_status: true in config.yaml.
    `doctor --fix` passes force=True to reconcile the board regardless."""
    if not (force or _env2("BOARD") or config()["project"].get("board_status")):
        return
    owner = slug.split("/")[0]
    pid, num = _project(owner, project_number_for(tid))
    if not pid:
        print(f"(status: no project for {tid} — skipping '{option}'. Set project_number in tasks.yaml or export PROJECT_NUMBER.)")
        return
    n = issue_number(slug, tid)
    if not n:
        print("(status: issue not found; skipping)"); return
    fd = _json(sh(["gh", "project", "field-list", num, "--owner", owner, "--format", "json"]))
    fields = (fd or {}).get("fields", fd if isinstance(fd, list) else [])
    field = next((f for f in fields if f.get("name") == "Status"), None)
    opt = next((o for o in (field or {}).get("options", []) if o.get("name") == option), None) if field else None
    if not field or not opt:
        print(f"(status: no Status field / '{option}' option; skipping)"); return
    il = _json(sh(["gh", "project", "item-list", num, "--owner", owner, "--format", "json", "--limit", "800"]))
    items = (il or {}).get("items", il if isinstance(il, list) else [])
    item = next((i for i in items if (i.get("content") or {}).get("number") == n), None)
    if not item:
        sh(["gh", "project", "item-add", num, "--owner", owner, "--url", f"https://github.com/{slug}/issues/{n}"])
        il = _json(sh(["gh", "project", "item-list", num, "--owner", owner, "--format", "json", "--limit", "800"]))
        items = (il or {}).get("items", il if isinstance(il, list) else [])
        item = next((i for i in items if (i.get("content") or {}).get("number") == n), None)
    if not item:
        print("(status: item not in project; skipping)"); return
    r = sh(["gh", "project", "item-edit", "--id", item["id"], "--project-id", pid,
            "--field-id", field["id"], "--single-select-option-id", opt["id"]])
    print(f"status: {tid} → {option} (project {num})" if r.returncode == 0 else f"(status: failed: {r.stderr.strip()})")

def cmd_start(tid):
    cfg = load(); t = by_id(cfg).get(tid)
    if not t:
        sys.exit(f"unknown task {tid}")
    print(f"START {tid}: {t['title']}")
    set_status(repo_slug(), tid, "In Progress")

# ---- selection -------------------------------------------------------------
def gh_states():
    """{T-id: 'open'|'closed'} from GitHub (the authoritative build queue), or
    None if GitHub is unreachable. A ticket is only ever built if it exists here
    as an OPEN issue — creating the issue is what makes it buildable."""
    try:
        return {t["id"]: t["status"] for t in tickets() if t["id"]}
    except SystemExit:
        return None
    except Exception:
        return None

def gh_closed():
    """Back-compat: set of closed T-ids, or None if gh is unavailable."""
    s = gh_states()
    return None if s is None else {k for k, v in s.items() if v == "closed"}

def done(t, closed):
    return (t["id"] in closed) if closed is not None else (t.get("status") == "closed")

def milestone_rank(m):
    mm = re.match(r"M(\d+)", str(m or ""))
    return int(mm.group(1)) if mm else 999

def build_frontier():
    """Highest milestone the builder may work, e.g. 'M4'. Tickets in later
    milestones are skipped even if their issues exist (so M5–M11 can sit on the
    board / roadmap without being built). Set via $BUILD_THROUGH or
    project.build_through in tasks.yaml; absent → no limit."""
    v = os.environ.get("BUILD_THROUGH") or (load()["project"].get("build_through"))
    return milestone_rank(v) if v else 999

def selectable(t, closed, states, frontier):
    """A ticket is a build candidate only if: it's an agent task, not done,
    within the milestone frontier, and (when gh is available) it exists as an
    OPEN issue. The last clause stops the builder running ahead of `sync`."""
    if t["assignee"] != "agent" or done(t, closed):
        return False
    if milestone_rank(t.get("milestone")) > frontier:
        return False
    if states is not None and states.get(t["id"]) != "open":
        return False
    return True

def cmd_next():
    cfg = load(); states = gh_states(); idx = by_id(cfg)
    closed = None if states is None else {k for k, v in states.items() if v == "closed"}
    frontier = build_frontier()
    for t in cfg["tasks"]:
        if not selectable(t, closed, states, frontier):
            continue
        if all(d in idx and done(idx[d], closed) for d in (t.get("depends_on") or [])):
            print(t["id"]); return
    print("NONE — nothing runnable (synced, in-frontier, deps closed)", file=sys.stderr)
    sys.exit(1)

# ---- context ---------------------------------------------------------------
def extract_section(text, n):
    m = re.search(rf"(?ms)^##\s+{n}\.\s.*?(?=^##\s+\d+\.\s|\Z)", text)
    return m.group(0).strip() if m else ""

def cmd_show(tid):
    cfg = load(); t = by_id(cfg).get(tid)
    if not t:
        sys.exit(f"unknown task {tid}")
    print(f"# {tid} · {t['title']}   [{t['milestone']} / {t['assignee']}]")
    print(t["body"])
    files = t.get("files") or {}
    for kind in ("modify", "create"):
        for f in files.get(kind, []):
            p = os.path.join(REPO, f)
            print(f"\n----- {kind}: {f} -----")
            print(open(p).read() if os.path.exists(p) else "(does not exist yet)")
    schema_path = os.path.join(REPO, tooling().get("schema_doc") or os.path.join("docs", "Data-Model-Schema.md"))
    secs = sorted(set(re.findall(r"§\s?(\d+)", t["body"])), key=int)
    if secs and os.path.exists(schema_path):
        text = open(schema_path).read()
        for n in secs:
            sec = extract_section(text, n)
            if sec:
                print(f"\n----- {os.path.basename(schema_path)} §{n} -----\n{sec}")

# ---- verification ----------------------------------------------------------
def acceptance_commands(t):
    cmds, in_acc = [], False
    for line in t["body"].splitlines():
        s = line.strip()
        if s.startswith("## Acceptance"):
            in_acc = True; continue
        if in_acc and s.startswith("## "):
            break
        if in_acc:
            for m in re.findall(r"`([^`]+)`", line):
                m = m.strip()
                if m and m.split()[0] in runnable():
                    cmds.append(m)
    return cmds

def run_cmds(cmds):
    ok = True
    for c in cmds:
        cwd = _cmd_cwd(c)
        print(f">>> {c}  (cwd={os.path.relpath(cwd, REPO) or '.'})")
        r = subprocess.run(c, shell=True, cwd=cwd, text=True, capture_output=True)
        sys.stdout.write(r.stdout or ""); sys.stderr.write(r.stderr or "")
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode != 0:
            ok = False; print(f"FAIL: {c}")
        elif "swift test" in c and "--filter" in c and re.search(r"Executed 0 tests", out):
            ok = False; print(f"FAIL: {c} — filter matched no tests (the named test is missing)")
    return ok

def cmd_verify(tid):
    cfg = load(); t = by_id(cfg).get(tid)
    if not t:
        sys.exit(f"unknown task {tid}")
    cmds = gate_commands(t)   # acceptance + app build for app-target tickets
    if not cmds:
        sys.exit("no runnable acceptance commands in this ticket")
    ok = run_cmds(cmds)
    print("PASS" if ok else "FAILED")
    sys.exit(0 if ok else 1)

# ---- finish (git + PR + issue) --------------------------------------------
def issue_number(slug, tid):
    out = sh(["gh", "issue", "list", "--repo", slug, "--state", "all",
              "--search", tid, "--json", "number,title", "--limit", "50"])
    for it in json.loads(out.stdout or "[]"):
        if it["title"].split(" ", 1)[0] == tid:
            return it["number"]
    return None

def cmd_finish(tid):
    cfg = load(); t = by_id(cfg).get(tid)
    if not t:
        sys.exit(f"unknown task {tid}")

    # 1. gate on acceptance (build + tests/lint must pass); app-target tickets
    #    additionally must compile the app so broken UI can't merge.
    cmds = gate_commands(t)
    if cmds and not run_cmds(cmds):
        sys.exit(f"BLOCKED: acceptance/build checks failed for {tid}; nothing committed.")

    # 2. branch off current HEAD, carrying the working changes
    branch = f"task/{tid}"
    if sh(["git", "checkout", "-b", branch]).returncode != 0:
        sh(["git", "checkout", branch], check=True)

    slug = repo_slug()
    n = issue_number(slug, tid)

    # 3. stage the ticket's declared files, PLUS the test files its acceptance
    #    references (tickets often list only the source) — never anything else.
    files = t.get("files") or {}
    paths = list(files.get("create", [])) + list(files.get("modify", []))
    tmpl = tooling().get("test_path_template")
    if tmpl:
        for name in re.findall(r"--filter\s+(\w+)", t["body"]):
            tp = tmpl.format(name=name)
            if os.path.exists(os.path.join(REPO, tp)) and tp not in paths:
                paths.append(tp)
    for p in paths:
        sh(["git", "add", "-A", "--", p])
    if sh(["git", "diff", "--cached", "--quiet"]).returncode == 0:
        # Acceptance passed but nothing changed → the ticket's goal is already met
        # (e.g. a redundant/no-op ticket). Close the issue, skip the commit/PR.
        sh(["git", "checkout", "main"])
        sh(["git", "branch", "-D", branch])
        if n:
            sh(["gh", "issue", "close", str(n), "--repo", slug,
                "--comment", f"{tid}: acceptance already satisfied; no code change required."])
        set_status(slug, tid, "Done")
        print(f"DONE: {tid} already satisfied (no changes)" + (f"; closed #{n}" if n else "") + ".")
        sys.exit(0)

    # 4. commit
    closes = f"\n\nCloses #{n}" if n else ""
    sh(["git", "commit", "-m", f"[{tid}] {t['title']}{closes}"], check=True)

    # 5. push, open PR, squash-merge (closes the issue via "Closes #n")
    sh(["git", "push", "-u", "origin", branch], check=True)
    sh(["gh", "pr", "create", "--repo", slug, "--base", "main", "--head", branch,
        "--title", f"[{tid}] {t['title']}", "--body", (closes.strip() or t["title"])])
    m = sh(["gh", "pr", "merge", branch, "--repo", slug, "--squash", "--delete-branch"])
    if m.returncode != 0:
        sh(["git", "checkout", "main"])
        sys.exit(f"BLOCKED: PR opened but auto-merge failed:\n{m.stderr.strip()}\n"
                 f"Merge it manually; the issue stays open until then. "
                 f"(If you've added required CI checks, switch this to `gh pr merge --auto`.)")

    # 6. sync main, drop the local task branch
    sh(["git", "checkout", "main"], check=True)
    sh(["git", "pull", "--ff-only"])
    sh(["git", "branch", "-D", branch])
    set_status(slug, tid, "Done")
    if sh(["git", "status", "--porcelain"]).stdout.strip():
        print(f"WARNING: {tid} merged but the working tree is still dirty — those edits "
              f"were NOT committed (run `git status`). The parallel flow avoids this: "
              f"each worktree holds exactly one ticket's edits.")
    print(f"DONE: {tid} merged to main" + (f", closed #{n}" if n else "") + ".")

# ---- parallel execution: capacity, worktrees, batch, submit, land ----------
def current_branch():
    return sh(["git", "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()

def _int_env(name, default):
    try:
        return int(_env2(name) or default)
    except ValueError:
        return default

def capacity():
    """Workers this machine can safely run in parallel.

    The binding constraint is not raw CPU but that each worker may fire a
    Swift/Xcode build — itself multi-core and RAM-hungry — so we deliberately
    under-subscribe. Knobs (env): NIFFLER_MAX_WORKERS (cap, default 4),
    NIFFLER_BUILD_RAM_GB (per worker, 4), NIFFLER_RAM_RESERVE_GB (held back for
    OS+Xcode+editor, 8).
    """
    hard_cap = _int_env("MAX_WORKERS", 4)
    build_ram = max(1, _int_env("BUILD_RAM_GB", 4))
    reserve = _int_env("RAM_RESERVE_GB", 8)
    try:
        if sys.platform.startswith("darwin"):
            cores = int(sh(["sysctl", "-n", "hw.physicalcpu"]).stdout or 0)
            ram_gb = int(sh(["sysctl", "-n", "hw.memsize"]).stdout or 0) / (1024 ** 3)
        else:
            cores = os.cpu_count() or 2
            ram_gb = reserve + build_ram
            try:
                for line in open("/proc/meminfo"):
                    if line.startswith("MemTotal"):
                        ram_gb = int(line.split()[1]) / (1024 ** 2); break
            except Exception:
                pass
    except Exception:
        cores, ram_gb = (os.cpu_count() or 2), reserve + build_ram
    by_cpu = max(1, cores // 2)              # each build already grabs several cores
    by_ram = max(1, int((ram_gb - reserve) // build_ram))
    return max(1, min(hard_cap, by_cpu, by_ram))

def cmd_capacity():
    print(capacity())

def runnable_tasks(cfg, closed, states=None, frontier=None):
    """Build candidates: agent tasks that are not done, within the milestone
    frontier, exist as an open issue (when gh is available), and whose deps are
    all closed."""
    if frontier is None:
        frontier = build_frontier()
    idx = by_id(cfg); out = []
    for t in cfg["tasks"]:
        if not selectable(t, closed, states, frontier):
            continue
        if all(d in idx and done(idx[d], closed) for d in (t.get("depends_on") or [])):
            out.append(t)
    return out

def ticket_files(t):
    f = t.get("files") or {}
    return set(f.get("create") or []) | set(f.get("modify") or [])

def cmd_batch(running, maxn):
    """Print up to `maxn` runnable tickets that are file-disjoint from the
    in-flight `running` set and from each other (a best-effort conflict dodge;
    `land` is the real safety net). Greedy in dependency/id order."""
    cfg = load(); states = gh_states()
    closed = None if states is None else {k for k, v in states.items() if v == "closed"}
    taken = set()
    for tid in running:
        t = by_id(cfg).get(tid)
        if t:
            taken |= ticket_files(t)
    picked = []
    for t in runnable_tasks(cfg, closed, states):
        if t["id"] in running:
            continue
        tf = ticket_files(t)
        if tf & taken:                      # predictable file overlap → defer it
            continue
        picked.append(t["id"]); taken |= tf
        if len(picked) >= maxn:
            break
    for tid in picked:
        print(tid)

# ---- worktrees -------------------------------------------------------------
def wt_path(tid):
    return os.path.join(WORKTREES, tid)

def cmd_worktree_add(tid):
    """Create an isolated worktree on a fresh task branch off origin/main."""
    os.makedirs(WORKTREES, exist_ok=True)
    sh(["git", "worktree", "prune"])        # clear stale (missing-dir) worktree records first
    path, branch = wt_path(tid), f"task/{tid}"
    sh(["git", "fetch", "origin", "main"])
    if os.path.exists(path):
        sh(["git", "worktree", "remove", "--force", path])
    sh(["git", "branch", "-D", branch])     # ignore failure if absent
    r = sh(["git", "worktree", "add", "-b", branch, path, "origin/main"])
    if r.returncode != 0:
        sys.exit(f"BLOCKED: worktree add failed for {tid}:\n{(r.stderr or r.stdout).strip()}")
    print(path)

def cmd_worktree_rm(tid):
    sh(["git", "worktree", "remove", "--force", wt_path(tid)])
    sh(["git", "branch", "-D", f"task/{tid}"])
    print(f"removed worktree {tid}")

# ---- app-build gate --------------------------------------------------------
def needs_app_build(t):
    ab = tooling().get("app_build") or {}
    prefixes = tuple(ab.get("paths") or ())
    if prefixes and any(p.startswith(prefixes) for p in ticket_files(t)):
        return True
    return any(l in (t.get("labels") or []) for l in (ab.get("labels") or []))

def app_build_cmd():
    return _env2("APP_BUILD_CMD") or (tooling().get("app_build") or {}).get("command") or ""

def gate_commands(t):
    cmds = acceptance_commands(t)
    ab = app_build_cmd()
    if ab and needs_app_build(t):
        cmds = cmds + [ab]
    return cmds

# ---- submit (parallel-safe: runs inside the worktree) ----------------------
def cmd_submit(tid):
    cfg = load(); t = by_id(cfg).get(tid)
    if not t:
        sys.exit(f"unknown task {tid}")
    cmds = gate_commands(t)
    if cmds and not run_cmds(cmds):
        sys.exit(f"BLOCKED: acceptance/build failed for {tid}; nothing pushed.")
    slug = repo_slug(); n = issue_number(slug, tid); branch = current_branch()
    # An isolated worktree contains ONLY this ticket's edits, so add -A is safe
    # and captures cross-file changes the Files list forgot.
    sh(["git", "add", "-A"])
    if sh(["git", "diff", "--cached", "--quiet"]).returncode == 0:
        if n:
            sh(["gh", "issue", "close", str(n), "--repo", slug,
                "--comment", f"{tid}: acceptance already satisfied; no code change required."])
        set_status(slug, tid, "Done")
        print(f"NOOP {tid}: already satisfied; closed" + (f" #{n}" if n else "") + ".")
        sys.exit(0)
    closes = f"\n\nCloses #{n}" if n else ""
    sh(["git", "commit", "-m", f"[{tid}] {t['title']}{closes}"], check=True)
    sh(["git", "push", "-u", "origin", branch], check=True)
    pr = sh(["gh", "pr", "create", "--repo", slug, "--base", "main", "--head", branch,
             "--title", f"[{tid}] {t['title']}", "--body", (closes.strip() or t["title"])])
    ok = pr.returncode == 0 or "already exists" in (pr.stderr or "")
    print(f"SUBMITTED {tid} branch={branch} pr={'ok' if ok else pr.stderr.strip()}")

# ---- land (serialized merge to main behind a lock) -------------------------
def _land_lock():
    os.makedirs(LOCKS, exist_ok=True)
    f = open(os.path.join(LOCKS, "land.lock"), "w")
    fcntl.flock(f, fcntl.LOCK_EX)           # blocks until the prior land releases
    return f

def ensure_pr(slug, tid, branch):
    """Make sure an open PR exists for `branch`. submit may have pushed the branch
    but failed before opening the PR (e.g. an API rate-limit mid-run); create one
    with a Closes-#n body so the squash-merge still closes the issue."""
    existing = _json(sh(["gh", "pr", "list", "--repo", slug, "--head", branch,
                         "--state", "open", "--json", "number"])) or []
    if existing:
        return
    cfg = load(); t = by_id(cfg).get(tid)
    n = issue_number(slug, tid)
    title = f"[{tid}] {t['title']}" if t else tid
    body = f"Closes #{n}" if n else (t["title"] if t else tid)
    sh(["gh", "pr", "create", "--repo", slug, "--base", "main", "--head", branch,
        "--title", title, "--body", body])

def cmd_land(tid):
    """Squash-merge tid's PR to main. Run from the MAIN checkout (not a worktree).
    Serialized so concurrent lands can't race main; on a merge conflict, rebase
    the branch on fresh main once and retry, else BLOCK for re-queue. Opens the PR
    first if submit pushed the branch but didn't create one."""
    cfg = load(); t = by_id(cfg).get(tid)
    if not t:
        sys.exit(f"unknown task {tid}")
    slug = repo_slug(); branch = f"task/{tid}"; wt = wt_path(tid)
    lock = _land_lock()
    try:
        ensure_pr(slug, tid, branch)
        m = sh(["gh", "pr", "merge", branch, "--repo", slug, "--squash", "--delete-branch"])
        if m.returncode != 0 and os.path.isdir(wt):
            sh(["git", "-C", wt, "fetch", "origin", "main"])
            rb = sh(["git", "-C", wt, "rebase", "origin/main"])
            if rb.returncode != 0:
                sh(["git", "-C", wt, "rebase", "--abort"])
                sys.exit(f"BLOCKED: CONFLICT landing {tid}; re-queue after other lands.\n"
                         f"{(m.stderr or '').strip()}")
            sh(["git", "-C", wt, "push", "--force-with-lease"], check=True)
            m = sh(["gh", "pr", "merge", branch, "--repo", slug, "--squash", "--delete-branch"])
        if m.returncode != 0:
            sys.exit(f"BLOCKED: land failed for {tid}:\n{(m.stderr or m.stdout).strip()}")
        sh(["git", "checkout", "main"]); sh(["git", "pull", "--ff-only"])
        sh(["git", "worktree", "remove", "--force", wt])
        sh(["git", "branch", "-D", branch])
        set_status(slug, tid, "Done")
        n = issue_number(slug, tid)
        print(f"LANDED {tid}" + (f" (closed #{n})" if n else "") + ".")
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN); lock.close()

# ---- new (create a correctly-formatted ticket issue) -----------------------
def next_ticket_id():
    nums = [int(t["id"][2:]) for t in tickets() if t["id"]]
    return f"T-{(max(nums) + 1) if nums else 1:03d}"

def resolve_milestone(slug, key):
    """Map a milestone key like 'M7' to its full GitHub title 'M7 · ...'."""
    if not key:
        return None
    ms = _json(sh(["gh", "api", f"repos/{slug}/milestones?state=all", "--paginate"])) or []
    pref = key.split("·")[0].strip()
    for m in ms:
        if m.get("title", "").split("·")[0].strip() == pref or m.get("title") == key:
            return m["title"]
    return key

def cmd_new(args):
    """Create a new ticket as a GitHub issue in the format the toolkit parses.
    Usage: new "Title" [--id T-500] [--milestone M7] [--assignee agent|human] [--area core|ui|...]
                       [--depends T-1,T-2] [--create a.swift,b.swift] [--modify c.swift]
                       [--body "markdown"  | --body-file path]
    --id pins an explicit ticket id (e.g. authoring Phase-2 work into the T-500+
    block while Phase-1 continuation keeps the lower ranges); default is max+1."""
    if not args or args[0].startswith("--"):
        sys.exit("usage: new \"Title\" [--milestone M7] [--area ui] [--depends ...] "
                 "[--create ...] [--modify ...] [--body ... | --body-file f]")
    title = args[0]
    opt = {}
    i = 1
    while i < len(args):
        if args[i].startswith("--") and i + 1 < len(args):
            opt[args[i][2:]] = args[i + 1]; i += 2
        else:
            i += 1
    slug = repo_slug()
    if opt.get("id"):
        tid = opt["id"].strip()
        if not re.fullmatch(r"T-\d{3,}", tid):
            sys.exit(f"BLOCKED: --id must look like T-500 (got {tid!r})")
        if any(t["id"] == tid for t in tickets()):
            sys.exit(f"BLOCKED: {tid} already exists — pick a free id")
    else:
        tid = next_ticket_id()
    assignee = opt.get("assignee", "agent")
    deps = [d for d in re.split(r"[,\s]+", opt.get("depends", "")) if d] or []
    create = [f for f in re.split(r"[,\s]+", opt.get("create", "")) if f]
    modify = [f for f in re.split(r"[,\s]+", opt.get("modify", "")) if f]
    if opt.get("body-file"):
        md = open(os.path.join(REPO, opt["body-file"])).read()
    else:
        md = opt.get("body") or ("## Context\n\n## Steps\n\n## Acceptance\n- [ ] `"
          + (tooling().get("default_acceptance") or "swift build") + "`\n")
    ms_key = opt.get("milestone", "")
    header = (f"**Milestone:** {ms_key or '—'}  |  **Assignee:** {assignee}  |  "
              f"**Depends on:** {', '.join(deps) if deps else 'none'}\n\n"
              f"**Files** — create: {create}; modify: {modify}\n\n---\n{md}")
    labels = [assignee]
    if opt.get("area"):
        labels.append(f"area:{opt['area']}")
    cmd = ["gh", "issue", "create", "--repo", slug, "--title", f"{tid} · {title}",
           "--body", header]
    for l in labels:
        cmd += ["--label", l]
    ms_title = resolve_milestone(slug, ms_key)
    if ms_title:
        cmd += ["--milestone", ms_title]
    r = sh(cmd)
    if r.returncode != 0:
        sys.exit(f"BLOCKED: issue create failed:\n{r.stderr.strip()}")
    url = r.stdout.strip()
    # Add to the Project board immediately so it shows as Todo — the Project's
    # auto-add workflow can lag (or be off); item-add is idempotent (an issue can
    # only be on a project once), so this is safe alongside auto-add.
    # Board routing mirrors project_number_for(): milestone default from the
    # `projects:` map first, so phase-2 milestones land on their own board.
    cfg_all = config()
    pmap = cfg_all.get("projects") or {}
    ms_pref = ms_key.split("·")[0].strip() if ms_key else ""
    num = (pmap.get(ms_pref) or os.environ.get("PROJECT_NUMBER")
           or cfg_all["project"].get("project_number"))
    if num:
        sh(["gh", "project", "item-add", str(num), "--owner", slug.split("/")[0], "--url", url])
    print(f"{tid}: {url}")

# ---- doctor (reconcile issues ↔ commits ↔ board ↔ branches) ----------------
def cmd_doctor(fix=False):
    from collections import Counter
    slug = repo_slug(); owner = slug.split("/")[0]
    tk = tickets()
    sh(["git", "fetch", "origin", "--prune"])
    log = sh(["git", "log", "origin/main", "--oneline", "-n", "8000"]).stdout
    committed = set(re.findall(r"\[(T-\d+)\]", log))
    num = config()["project"].get("project_number")
    board = {}
    if num:
        for it in _board_items(owner, str(num)):
            c = it.get("content") or {}
            if c.get("number") is not None:
                board[c["number"]] = it.get("status")
    problems = 0

    def flag(msg):
        nonlocal problems
        problems += 1
        print(msg)

    # 1. duplicate ticket ids
    for tid, c in Counter(t["id"] for t in tk if t["id"]).items():
        if c > 1:
            flag(f"DUP-ID  {tid}: {c} issues share this id (collision)")

    # 2/3. board ↔ issue-state drift
    for t in tk:
        st = board.get(t["number"])
        if t["status"] == "closed" and st is not None and st != "Done":
            flag(f"BOARD   {t['id']} #{t['number']} closed but board={st} → should be Done"
                 + (" [FIXING]" if fix else ""))
            if fix:
                set_status(slug, t["id"], "Done", force=True)
        if t["status"] == "open" and st == "Done":
            flag(f"BOARD   {t['id']} #{t['number']} open but board=Done (re-opened?)")

    # 4. merged work whose issue is still open
    for t in tk:
        if t["status"] == "open" and t["id"] in committed:
            flag(f"MERGED  {t['id']} #{t['number']} has a [{t['id']}] commit on main but issue is OPEN")

    # 5. closed agent ticket with no traceable commit
    for t in tk:
        if t["status"] == "closed" and t["assignee"] == "agent" and t["id"] not in committed:
            flag(f"NOCOMMIT {t['id']} #{t['number']} closed (agent) but no [{t['id']}] commit on main")

    # 6. stray branches: merged-but-undeleted, or unmerged with no PR
    for ref in sh(["git", "branch", "-r"]).stdout.splitlines():
        b = ref.strip()
        if "->" in b or not b.startswith("origin/") or b == "origin/main":
            continue  # skip the symbolic 'origin/HEAD -> origin/main' line
        head = b[len("origin/"):]
        prs = _json(sh(["gh", "pr", "list", "--repo", slug, "--head", head,
                        "--state", "all", "--json", "number,state"])) or []
        states = {p["state"] for p in prs}
        if "OPEN" in states:
            continue
        if "MERGED" in states:
            flag(f"BRANCH  {head}: PR merged but branch not deleted"
                 + (" [DELETING]" if fix else ""))
            if fix:
                sh(["git", "push", "origin", "--delete", head])
        else:
            flag(f"BRANCH  {head}: no open/merged PR — unmerged or abandoned (review)")

    print(f"\n{'doctor --fix: ' if fix else ''}{problems} issue(s) found." if problems
          else "doctor: clean — issues, commits, board and branches all consistent.")
    sys.exit(0)

def _board_items(owner, num):
    il = _json(sh(["gh", "project", "item-list", num, "--owner", owner,
                   "--format", "json", "--limit", "800"]))
    return (il or {}).get("items", il if isinstance(il, list) else [])

# ---- dispatch --------------------------------------------------------------
if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    verb = sys.argv[1]
    if verb == "next":
        cmd_next()
    elif verb == "new":
        cmd_new(sys.argv[2:])
    elif verb == "doctor":
        cmd_doctor(fix="--fix" in sys.argv[2:])
    elif verb == "show" and len(sys.argv) > 2:
        cmd_show(sys.argv[2])
    elif verb == "verify" and len(sys.argv) > 2:
        cmd_verify(sys.argv[2])
    elif verb == "finish" and len(sys.argv) > 2:
        cmd_finish(sys.argv[2])
    elif verb == "start" and len(sys.argv) > 2:
        cmd_start(sys.argv[2])
    elif verb == "capacity":
        cmd_capacity()
    elif verb == "batch":
        running, maxn = [], None
        a = sys.argv[2:]; i = 0
        while i < len(a):
            if a[i] == "--running" and i + 1 < len(a):
                running = [x for x in a[i + 1].split(",") if x]; i += 2
            elif a[i] == "--max" and i + 1 < len(a):
                maxn = int(a[i + 1]); i += 2
            else:
                i += 1
        cmd_batch(running, maxn if maxn is not None else capacity())
    elif verb == "worktree-add" and len(sys.argv) > 2:
        cmd_worktree_add(sys.argv[2])
    elif verb == "worktree-rm" and len(sys.argv) > 2:
        cmd_worktree_rm(sys.argv[2])
    elif verb == "submit" and len(sys.argv) > 2:
        cmd_submit(sys.argv[2])
    elif verb == "land" and len(sys.argv) > 2:
        cmd_land(sys.argv[2])
    else:
        sys.exit(__doc__)
