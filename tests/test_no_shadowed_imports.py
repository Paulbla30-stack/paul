"""A function-local `import x.y` shadows a module-level `x` for that whole scope.

Found the hard way. Adding a screenshot route to headless.py put
`import urllib.request` inside one branch of `do_GET`; Python then treated
`urllib` as a local of `do_GET`, so an unrelated branch further down --
`/documents/<name>`, which had used the module-level `urllib` since the day it
was written -- raised UnboundLocalError on every request. Nothing changed in
that branch and nothing in the new one touched it.

It is a whole-function fault caused by a one-branch edit, which is the kind
that gets found in production rather than in review. `from urllib.request
import urlopen` binds `urlopen` and not `urllib`, and is the fix; this test is
so the next person does not have to rediscover which of those two forms is
safe.
"""

import ast
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The long-lived request handlers: big functions, many branches, and the place
# where a local import is most likely to be added to one branch of many.
WATCHED = (
    os.path.join("jarvis", "cloud", "headless.py"),
    os.path.join("jarvis", "browser", "service.py"),
    os.path.join("jarvis", "agent", "executor.py"),
    os.path.join("jarvis", "agent", "core.py"),
)


def top_level_modules(tree):
    """Names bound by module-level imports, as the module binds them."""
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


def shadowing_imports(tree, outer):
    """Every function-local import that rebinds a name the module already has."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Import):
                for alias in inner.names:
                    bound = (alias.asname or alias.name).split(".")[0]
                    if bound in outer:
                        found.append((node.name, bound, inner.lineno))
    return found


class TestNoLocalImportShadowsAModuleLevelOne(unittest.TestCase):

    def test_the_request_handlers_are_clean(self):
        for rel in WATCHED:
            path = os.path.join(ROOT, rel)
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=rel)
            bad = shadowing_imports(tree, top_level_modules(tree))
            self.assertEqual(
                bad, [],
                f"{rel}: a local `import` rebinds a module-level name, which "
                f"makes it local to the whole function and unbound in every "
                f"branch that runs before the import. Use `from x import y`. "
                f"Offenders (function, name, line): {bad}")

    def test_the_check_catches_the_shape_it_is_for(self):
        """A test that cannot fail is not a test."""
        tree = ast.parse("import urllib.request\n"
                         "def f():\n"
                         "    if 0:\n"
                         "        import urllib.request\n"
                         "    return urllib.parse\n")
        self.assertEqual(shadowing_imports(tree, top_level_modules(tree)),
                         [("f", "urllib", 4)])

    def test_the_safe_form_is_not_flagged(self):
        tree = ast.parse("import urllib.request\n"
                         "def f():\n"
                         "    from urllib.request import urlopen\n"
                         "    return urlopen\n")
        self.assertEqual(shadowing_imports(tree, top_level_modules(tree)), [])

    def test_a_local_import_of_something_new_is_not_flagged(self):
        """Deferred imports are used all over this codebase on purpose."""
        tree = ast.parse("import os\n"
                         "def f():\n"
                         "    import boto3\n"
                         "    return boto3\n")
        self.assertEqual(shadowing_imports(tree, top_level_modules(tree)), [])


if __name__ == "__main__":
    unittest.main()
