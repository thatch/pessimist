import logging
import os
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from queue import Queue
from subprocess import check_call, PIPE, run, STDOUT
from typing import Dict, List, Optional, Set, Tuple

from highlighter import EnvironmentMarkers
from honesty.cache import Cache
from honesty.releases import Package, parse_index
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

LOG = logging.getLogger(__name__)


class DepError(Exception):
    pass


@dataclass
class Plan:
    title: str
    versions: Dict[str, Version]
    fatal: bool
    name: Optional[str] = None
    version: Optional[Version] = None


@dataclass
class Result:
    item: Plan
    exception: Optional[str]
    output: str


# This is a trimmed-down version of dowsing.deps._find_compatible_version that
# returns a list.
def _filter_versions(
    req: Requirement,
    cache: Cache,
    python_version: Version,
    extend: Optional[List[str]] = None,
) -> Tuple[Package, List[Version]]:
    name = canonicalize_name(req.name)
    pkg = parse_index(name, cache, use_json=True)

    possible: List[Version] = []

    for k, v in pkg.releases.items():
        requires_python = None
        for fe in v.files:
            if fe.requires_python:
                requires_python = SpecifierSet(fe.requires_python)
                break
        if not requires_python or python_version in requires_python:
            assert isinstance(k, Version)
            possible.append(k)
    if extend is None or not ("*" in extend or name in extend):
        possible = list(req.specifier.filter(possible))
    return pkg, possible


class Manager:
    def __init__(
        self,
        path: Path,
        variable: List[str],
        fixed: List[str],
        command: str,
        extend: List[str],
        fast: bool,
    ) -> None:
        self.path = path
        self.command = command
        self.extend = extend
        self.fast = fast

        self.names: Set[str] = set()
        self.packages: Dict[str, Package] = {}
        self.versions: Dict[str, List[Version]] = {}
        python_version_str = ".".join(map(str, sys.version_info[:3]))
        python_version = Version(python_version_str)
        env = EnvironmentMarkers.for_python(python_version_str, sys.platform)

        self.pip_lines = [line for line in fixed if self._is_pip_line(line)]
        fixed = [line for line in fixed if not self._is_pip_line(line)]

        for req_str in [*fixed, *variable]:
            req = Requirement(req_str)
            if req.marker and not env.match(req.marker):
                continue
            self.names.add(canonicalize_name(req.name))

        with Cache(fresh_index=True) as cache:
            # First fetch "fixed" and see how many match:
            # 0: that's an error
            # 1: great!  # >1: warning, and pick the newest (because that's what CI is likely
            # to do; open to other ideas here though)

            for req_str in fixed:
                req = Requirement(req_str)
                if req.marker and not env.match(req.marker):
                    continue

                pkg, versions = _filter_versions(req, cache, python_version)

                if len(versions) == 0:
                    raise DepError("No versions match {req_str!r}; maybe pre-only?")
                if len(versions) > 1:
                    LOG.warning(
                        f"More than one version matched {req_str!r}; picking one arbitrarily."
                    )

                name = canonicalize_name(req.name)
                self.versions[name] = [versions[-1]]
                LOG.info(
                    f"  [fixed] fetched {req.name}: {len(versions)}/{len(pkg.releases)} allowed; keeping {versions[-1]!r}"
                )

            for req_str in variable:
                req = Requirement(req_str)
                if req.marker and not env.match(req.marker):
                    continue

                pkg, versions = _filter_versions(
                    req, cache, python_version, extend=self.extend
                )

                name = canonicalize_name(req.name)
                self.packages[name] = pkg

                LOG.info(
                    f"  [variable] fetched {name}: {len(versions)}/{len(pkg.releases)} allowed"
                )

                if len(versions) == 0:
                    raise DepError("No versions match {req_str!r}; maybe pre-only?")

                if name in self.versions:
                    # Presumably this came from being in 'fixed' too; not being
                    # in 'variable' twice.  If so it will only have one version.
                    if self.versions[name][0] not in versions:
                        LOG.warning(
                            f"  [variable] fixed version {self.versions[name][0]!r} not in {versions!r} for {req_str!r}"
                        )

                    LOG.info(
                        f"  [variable] widen due to variable: {req_str!r} -> {versions!r}"
                    )

                if fast:
                    if len(versions) == 1:
                        self.versions[name] = [versions[0]]
                    else:
                        # zero-length already raised DepError
                        self.versions[name] = [versions[0], versions[-1]]
                else:
                    self.versions[name] = versions

    def get_max_plan(self) -> Plan:
        return Plan(
            title="max",
            versions={k: v[-1] for k, v in self.versions.items()},
            fatal=True,
        )

    def get_min_plan(self) -> Plan:
        return Plan(
            title="min",
            versions={k: v[0] for k, v in self.versions.items()},
            fatal=True,
        )

    def get_intermediate_plans(self) -> List[Plan]:
        # this might look like an unreasonable number, but note that we aren't
        # using all combinations so this is only linear.

        max_vers = self.get_max_plan().versions
        ret: List[Plan] = []
        for k, versions in self.versions.items():
            for v in versions[:-1]:
                vers = max_vers.copy()
                vers[k] = v
                ret.append(
                    Plan(
                        title=f"{k}:{v}",
                        versions=vers,
                        fatal=False,
                        name=k,
                        version=v,
                    )
                )
        return ret

    def solve(self, parallelism: int = 10) -> int:

        queue: Queue[Optional[Plan]] = Queue()
        results: Queue[Result] = Queue()
        should_cancel: bool = False

        def runner() -> None:
            with tempfile.TemporaryDirectory() as d:
                check_call([sys.executable, "-m", "venv", d])

                env = os.environ.copy()
                cur_path = env["PATH"]
                if os.sep != "/":
                    if hasattr(sys, "base_prefix"):
                        # Running in a venv; make SURE this venv is not
                        # polluting the env
                        cur_path = env["PATH"].split(";", 1)[1]
                    env["PATH"] = f"{d}\\scripts;{cur_path}"
                    env["PYTHON"] = f"{d}\\scripts\\python.exe"
                else:
                    if hasattr(sys, "base_prefix"):
                        # Running in a venv; make SURE this venv is not
                        # polluting the env
                        cur_path = env["PATH"].split(":", 1)[1]
                    env["PATH"] = f"{d}/bin:{cur_path}"
                    env["PYTHON"] = f"{d}/bin/python"
                env["COVERAGE_FILE"] = f"{d}/.coverage"

                # Pip 20.3+ includes the new solver and it doesn't appear
                # there's a long-term ability to disable it.  We want to just
                # overlay a pile of requirements to see if they play nice
                # together.  This might get unintentionally upgraded later in
                # the process though.
                buf = [env["PYTHON"], "-m", "pip", "install", "pip==20.2"]
                proc = run(
                    buf,
                    env=env,
                    stdout=PIPE,
                    stderr=STDOUT,
                    cwd=self.path,
                    encoding="utf-8",
                )

                while True:
                    item: Optional[Plan] = queue.get(block=True)
                    if item is None:
                        break

                    if should_cancel:
                        break

                    # TODO keep track of what's installed, avoid issuing
                    # duplicate install commands, and detect when an unexpected
                    # version was installed (e.g. from a dep constraint).
                    output: str = ""
                    try:
                        buf = [env["PYTHON"], "-m", "pip", "install"]
                        installed_something_flag = False
                        for line in self.pip_lines:
                            # TODO: It's probably not split, nor shlex.
                            buf.extend(line.split())
                            installed_something_flag = True

                        for k, v in item.versions.items():
                            buf.append(f"{k}=={v}")
                            installed_something_flag = True

                        if installed_something_flag:
                            # TODO: escaping is wrong.
                            output += f"$ {' '.join(buf)}"
                            proc = run(
                                buf,
                                env=env,
                                stdout=PIPE,
                                stderr=STDOUT,
                                cwd=self.path,
                                encoding="utf-8",
                            )
                            output += proc.stdout

                            if proc.returncode != 0:
                                raise Exception("Install failed")

                        output += f"$ {self.command}\n"
                        proc = run(
                            self.command,
                            shell=True,
                            env=env,
                            stdout=PIPE,
                            stderr=STDOUT,
                            cwd=self.path,
                            encoding="utf-8",
                        )
                        output += proc.stdout
                        if proc.returncode != 0:
                            raise Exception("Test failed")

                    except Exception as e:
                        results.put(Result(item, str(e), output))
                    else:
                        results.put(Result(item, None, output))
                    queue.task_done()

        threads: List[threading.Thread] = []
        if self.fast:
            parallelism = min(parallelism, 2)
        for i in range(parallelism):
            t = threading.Thread(target=runner)
            # t.setDaemon(True)
            t.start()
            threads.append(t)

        # TODO consider these phases?
        outstanding = 0

        queue.put(self.get_max_plan())
        outstanding += 1
        if self.fast:
            queue.put(self.get_min_plan())
            outstanding += 1
        else:
            for plan in self.get_intermediate_plans():
                queue.put(plan)
                outstanding += 1

        min_versions: Dict[str, Version] = {}
        rv = 0

        while outstanding and not should_cancel:
            result = results.get(block=True)
            outstanding -= 1
            if result.exception:
                print(f"FAIL {result.item.title}: {result.exception}")
                for line in result.output.splitlines():
                    print(f"  {line}")

                if (
                    result.item.name is not None
                    and result.item.version is not None
                    and result.item.name in min_versions
                    and min_versions[result.item.name] < result.item.version
                ):
                    LOG.warning("  Inconsistent result")

                if result.item.fatal:
                    should_cancel = True
                    rv = 1

            else:
                print(f"OK   {result.item.title}")
                if result.item.name:
                    assert result.item.version is not None
                    if result.item.name in min_versions:
                        min_versions[result.item.name] = min(
                            min_versions[result.item.name], result.item.version
                        )
                    else:
                        min_versions[result.item.name] = result.item.version

        if not self.fast and min_versions:
            print("Final test")
            print("==========")
            print(min_versions)

            tmp = self.get_max_plan().versions.copy()
            tmp.update(min_versions)

            queue.put(Plan(title="min", versions=tmp, fatal=True))
            result = results.get(block=True)
            if result.exception:
                print(f"FAIL {result.item.title}: {result.exception}")
                print(result.output)
                rv = 2
            else:
                print(f"OK   {result.item.title}")
                suggested = False
                for k, v in min_versions.items():
                    if self.versions[k][0] != v:
                        print(f"Suggest narrowing: {k}>={v}")
                        suggested = True
                if not suggested:
                    print("Everything is fine.")

        for i in range(parallelism):
            queue.put(None)

        for t in threads:
            t.join()

        return rv

    @classmethod
    def _is_pip_line(self, line: str) -> bool:
        # This is a very incomplete support for stuff that pip supports that
        # isn't considered a single Requirement.  We pass these through as a
        # special kind of "fixed".
        #
        # ./
        # -e ../foo
        # -r requirements2.txt
        # git+https://example.com/#egg=Foo
        return (
            line.startswith("-")
            or "/" in line
            or line.startswith("git+")
            or line.startswith("hg+")
        )
